"""Slot-end chase reference, SHADOW ONLY (2026-09-29, operator via Alpha; Gamma grades, SWITCH at n>=30).

The Solana chase gate compares our quote with the source's price. The operator's case (CARDANO
e3c706bf): source avg 6.34e-5, post-trade 6.90e-5, our quote 1.13e-4 about 0.5 s later, refused at
+64% on a 30% cap. Most of that move was OTHER traders in the source's own slot (bots, bundles),
which a copier cannot beat. Re-run on the 102 Solana chase refusals of 09-22..09-29 (this module,
coin-reserve pricing, venue from the vault's own instruction): 63 known, of which the slot-end
reference admits 56 and the post-trade reference alone 23; unknown 23 with no source signature,
8 split routes, 8 on other venues (4 LaunchLab, 4 Meteora DAMM v2).

Nothing we stream holds the end-of-slot state (the slot lane and the watcher see only the source's
own transaction), and the fill path gets no new read. So this runs in the refusal-capture process,
after the refusal, and only records:
  ref_end_slot  = max(end-of-slot price, post-trade price) in USD per token (post-trade is the floor)
  would_admit   = quote <= ref_end_slot x (1 + cap); NULL when anything is unknown, never 1
  reason        = why a row is unknown (NULL when known)

How the end-of-slot state is read: the source tx (getTransaction) gives the slot S and the pool's
coin vault (the one coin account that fell: it paid the buyer out; two are a split route, unknown)
and its venue (the program whose instruction carries that vault). Helius
getTransactionsForAddress on that vault, filtered to slot S and sorted ascending, gives every later
transaction in S in exact order (transactionIndex); the vault after the last one is the slot end.

Prices come from the coin reserve X alone (Gamma, review of 4a128fae5). All three venues are
constant product in X and an effective quote reserve Y (the curve's virtual SOL; PumpSwap's quote
vault plus a virtual V the balances do not show; CPMM's vault minus accrued fees), so spot = k / X^2
and the pool's paid price for a buy from X0 to X1 is k / (X0 X1). Hence, with no quote side at all:
  post-trade / source fill = X0 / X1        end-of-slot / post-trade = (X1 / Xend)^2
and end_usd = source_price x X0 X1 / Xend^2. The pump curve's X is its token account + 73,000,000.
Pool fees keep the paid price up to one fee above k / (X0 X1), so the reference reads up to one fee
high; the post-trade floor is unaffected by that choice of pool.
ponytail: a liquidity add or withdraw later in the slot changes k and reads as a price move (a
withdraw would read as a rise); rare inside one slot, add a k check if a graded row shows it.

Venues: the pump curve, PumpSwap and Raydium CPMM. LaunchLab and concentrated liquidity are unknown.
Research key only, refused if it is also a funded key, and no read while a real Solana position is
open or a Solana entry is reserved (the research-read rule); queued items wait.
"""
from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("memecoin")

PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
RAYDIUM_CPMM = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
# Pump curve virtual token reserve = its token account + 73,000,000 (1,073,000,000 initial virtual
# tokens against the 1,000,000,000 the account starts with). Checked against TradeEvent on 16 of 16
# curve source txs (Gamma, 2026-09-29).
CURVE_TOKEN_OFFSET = 73_000_000.0
SETTLE_S = 30.0            # read a slot at least this long after the refusal (index lag)
MIN_INTERVAL_S = 1.1       # the research key 429d at 0.13 s spacing (2026-09-27)
FLAT_RETRY_S = 30.0
_SIG = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{80,90}$")


def source_sig(origin):
    """The Solana source signature an origin id carries ('onchain:<sig>', 'swap:raybot:<sig>:...',
    'pump:<sig>:...'), found by shape, or None ('swap:<uuid>')."""
    for part in str(origin or "").split(":"):
        if _SIG.match(part):
            return part
    return None


def _keys(tx):
    msg = tx["transaction"]["message"]
    loaded = (tx.get("meta") or {}).get("loadedAddresses") or {}
    return list(msg["accountKeys"]) + list(loaded.get("writable") or []) + list(loaded.get("readonly") or [])


def _balances(tx, when):
    """{account address: (owner, mint, ui amount)} from pre/post token balances."""
    ks, out = _keys(tx), {}
    for b in (tx.get("meta") or {}).get("%sTokenBalances" % when) or []:
        try:
            out[ks[b["accountIndex"]]] = (b.get("owner"), b.get("mint"),
                                          float(b["uiTokenAmount"]["uiAmount"] or 0.0))
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return out


VENUES = {PUMP: "pump_curve", PUMP_AMM: "pumpswap", RAYDIUM_CPMM: "raydium_cpmm"}


def carriers(tx, vault):
    """Program ids of every instruction (outer or inner) whose accounts include `vault`. The venue is
    the one venue program among them; which programs appear elsewhere in the tx says nothing (a
    routed buy can hop through PumpSwap while the coin's pool is LaunchLab: Gamma N1, d8b429bc).
    The vault's token owner is the pool PDA, so this is the owner program without another read."""
    ks = _keys(tx)
    if vault not in ks:
        return set()
    vi = ks.index(vault)
    ixs = list(tx["transaction"]["message"].get("instructions") or [])
    for inner in (tx.get("meta") or {}).get("innerInstructions") or []:
        ixs.extend(inner.get("instructions") or [])
    return {ks[ix["programIdIndex"]] for ix in ixs
            if 0 <= ix.get("programIdIndex", -1) < len(ks) and vi in (ix.get("accounts") or [])}


def pool_of(tx, mint):
    """(pool, reason) from the source BUY tx; reason None when the pool is priceable. The coin
    accounts that fell paid the buyer out; one is the pool's vault, two or more are a split route and
    unknown (Gamma N2: the legs' X0/X1 differ, a DLMM or Whirlpool leg against a constant-product one;
    the live routed_leg rule calls them impact_unknown). `pool` always carries n_coin_legs and, once
    a vault is found, vault_program (the carriers of the largest leg, comma-joined) for audits."""
    pre, post = _balances(tx, "pre"), _balances(tx, "post")
    fell = sorted(((pre[a][2] - post.get(a, pre[a])[2], a) for a in pre
                   if pre[a][1] == mint and post.get(a, pre[a])[2] < pre[a][2]), reverse=True)
    if not fell:
        return {"n_coin_legs": 0}, "pool_not_found"
    vault = fell[0][1]
    progs = carriers(tx, vault)
    pool = {"vault": vault, "owner": pre[vault][0], "n_coin_legs": len(fell),
            "vault_program": ",".join(sorted(progs)) or None}
    if len(fell) > 1:
        return pool, "split_route"
    venues = {VENUES[p] for p in progs if p in VENUES}
    if len(venues) != 1:
        return pool, "venue_ambiguous" if venues else "venue_unsupported"
    pool["venue"] = venues.pop()
    pool["offset"] = CURVE_TOKEN_OFFSET if pool["venue"] == "pump_curve" else 0.0
    pool["coin_pre"] = pre[vault][2] + pool["offset"]
    pool["coin_post"] = coin_after(tx, pool, mint)
    if not pool["coin_post"]:
        return pool, "post_state_unpriced"
    return pool, None


def coin_after(tx, pool, mint):
    """The pool's coin reserve after `tx` (the curve's is virtual), or None when the vault is absent
    or empty (a migrated curve leaves its account at zero)."""
    coin = _balances(tx, "post").get(pool["vault"])
    if not coin or coin[1] != mint or coin[2] <= 0:
        return None
    return coin[2] + pool["offset"]


def chase_fields(trace_json):
    """(quote_price, source_price, cap_pct, gap_pct, reference_price, reference_basis) of the
    refusal's post-quote chase check, or None."""
    try:
        trace = json.loads(trace_json or "{}")
    except (TypeError, ValueError):
        return None
    for s in trace.get("substage_events") or []:
        if s.get("name") == "chase_quote_confirmation":
            try:
                return (float(s["quote_price"]), float(s["source_price"]), float(s["cap_pct"]),
                        float(s.get("gap_pct")) if s.get("gap_pct") is not None else None,
                        s.get("reference_price"), s.get("reference_basis"))
            except (KeyError, TypeError, ValueError):
                return None
    return None


def evaluate(item, trace_json, rpc):
    """One shadow row for a Solana chase refusal. `rpc(method, params)` is the research reader.
    Every unknown is would_admit NULL with a reason; a known row never needs a guess."""
    row = {"attempt_id": item.get("attempt_id"), "origin_event_id": item.get("origin_event_id"),
           "mint": item.get("mint"), "refused_at": item.get("refused_at"), "would_admit": None}
    fields = chase_fields(trace_json)
    if fields is None:
        return {**row, "reason": "no_chase_trace"}
    quote_px, source_px, cap, gap, ref_px, ref_basis = fields
    row.update(quote_price=quote_px, source_price=source_px, cap_pct=cap, gap_pct=gap,
               gate_reference_price=ref_px, gate_reference_basis=ref_basis)
    sig = source_sig(item.get("origin_event_id"))
    if not sig:
        return {**row, "reason": "no_source_signature"}
    row["source_sig"] = sig
    tx = rpc("getTransaction", [sig, {"encoding": "json", "maxSupportedTransactionVersion": 1,
                                      "commitment": "confirmed"}])
    if not tx:
        return {**row, "reason": "source_tx_unreadable"}
    row["slot"] = tx.get("slot")
    pool, why = pool_of(tx, item.get("mint"))
    row.update(n_coin_legs=pool["n_coin_legs"], vault_program=pool.get("vault_program"),
               pool_vault=pool.get("vault"), venue=pool.get("venue"))
    if why:
        return {**row, "reason": why}
    row.update(coin_pre=pool["coin_pre"], coin_post=pool["coin_post"])
    later, token = [], None
    for _page in range(5):
        opts = {"transactionDetails": "full", "sortOrder": "asc", "limit": 100, "encoding": "json",
                "maxSupportedTransactionVersion": 1,
                "filters": {"status": "succeeded", "slot": {"gte": row["slot"], "lte": row["slot"]}}}
        if token:
            opts["paginationToken"] = token
        page = rpc("getTransactionsForAddress", [pool["vault"], opts]) or {}
        later.extend(page.get("data") or [])
        token = page.get("paginationToken")
        if not token:
            break
    else:
        return {**row, "reason": "slot_truncated"}
    index = next((x.get("transactionIndex") for x in later
                  if sig in ((x.get("transaction") or {}).get("signatures") or [])), None)
    if index is None:
        return {**row, "reason": "source_not_in_slot_list"}
    after = [x for x in later if x.get("transactionIndex") is not None and x["transactionIndex"] > index]
    coin_end = coin_after(after[-1], pool, item.get("mint")) if after else pool["coin_post"]
    if not coin_end:
        return {**row, "reason": "slot_end_unpriced"}
    post_usd = source_px * pool["coin_pre"] / pool["coin_post"]
    end_usd = post_usd * (pool["coin_post"] / coin_end) ** 2
    ref_end = max(end_usd, post_usd)                      # post-trade is the floor (Alpha)
    gap_end = (quote_px / ref_end - 1.0) * 100.0
    row.update(in_slot_after=len(after), coin_end=coin_end, ref_post_usd=post_usd,
               ref_end_slot=ref_end, gap_end_pct=gap_end, would_admit=int(gap_end <= cap), reason=None)
    return row


PAUSE_ON_429_S = 60.0


class ResearchRpc:
    """One research Helius key, paced. Raises on HTTP 429 so the worker backs off.

    With `pacer` (memecoin_research_pacer.SharedPacer, what rpc_from_env gives) the gap and a 429's
    pause are SHARED with every other process reading this key (Alpha 2026-10-02); without one, the
    old in-process gap applies."""

    def __init__(self, url, *, min_interval_s=MIN_INTERVAL_S, timeout_s=15.0, pacer=None):
        self._url, self._min, self._timeout, self._last = url, min_interval_s, timeout_s, 0.0
        self._pacer = pacer

    def __call__(self, method, params):
        if self._pacer is not None:
            self._pacer.acquire()
        else:
            wait = self._last + self._min - time.monotonic()
            if wait > 0:
                time.sleep(wait)
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        req = urllib.request.Request(self._url, body, {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                out = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and self._pacer is not None:
                self._pacer.pause(PAUSE_ON_429_S)          # every reader of this key backs off
            raise
        finally:
            self._last = time.monotonic()
        if out.get("error"):
            raise RuntimeError("RPC error on %s: %s" % (method, out["error"].get("code")))
        return out.get("result")


def rpc_from_env(env):
    """(ResearchRpc, None), or (None, why): the first research key, refused when it is also a funded
    key (credits are per account; research load must never spend the funded reads)."""
    from sniper.services.memecoin_research_rpc import funded_overlap, research_urls
    urls = research_urls(env)
    if not urls:
        return None, "no research key"
    overlap = funded_overlap(urls[:1], env)
    if overlap:
        return None, "research key %s is also a funded key" % ",".join(overlap)
    from sniper.services.memecoin_research_pacer import SharedPacer
    return ResearchRpc(urls[0], pacer=SharedPacer.for_url(urls[0])), None


class Worker(threading.Thread):
    """Own thread in the capture process. submit() never blocks the capture loop."""

    def __init__(self, *, rpc, trace_fn, record_fn, flat_fn, now_fn=None, sleep_fn=time.sleep):
        super().__init__(name="slot-end-shadow", daemon=True)
        self._q = queue.Queue()
        self._rpc, self._trace, self._record, self._flat = rpc, trace_fn, record_fn, flat_fn
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep_fn
        self.done = 0

    def submit(self, item):
        self._q.put(item)

    def step(self):
        """Handle one item (True) or nothing ready (False). Separated from run() for tests."""
        try:
            item = self._q.get_nowait()
        except queue.Empty:
            return False
        ready_at = datetime.fromisoformat(str(item["refused_at"]).replace("Z", "+00:00")) \
            + timedelta(seconds=SETTLE_S)
        if self._now() < ready_at or not self._flat():
            self._q.put(item)                    # not yet, or a real Solana position is open
            self._sleep(FLAT_RETRY_S if self._now() >= ready_at else 1.0)
            return True
        try:
            row = evaluate(item, self._trace(item.get("attempt_id")), self._rpc)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                self._q.put(item)
                self._sleep(60.0)
                return True
            row = {"attempt_id": item.get("attempt_id"), "origin_event_id": item.get("origin_event_id"),
                   "mint": item.get("mint"), "refused_at": item.get("refused_at"),
                   "would_admit": None, "reason": "http_%s" % exc.code}
        except Exception as exc:          # noqa: BLE001 - a shadow row names its failure, never raises
            logger.exception("slot-end shadow failed for %s", item.get("attempt_id"))
            row = {"attempt_id": item.get("attempt_id"), "origin_event_id": item.get("origin_event_id"),
                   "mint": item.get("mint"), "refused_at": item.get("refused_at"),
                   "would_admit": None, "reason": "error:%s" % type(exc).__name__}
        row["computed_at"] = self._now().isoformat()
        self._record(row)
        self.done += 1
        return True

    def run(self):
        while True:
            if not self.step():
                self._sleep(2.0)
