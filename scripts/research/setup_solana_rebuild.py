#!/usr/bin/env python3
"""Solana pre-signal rebuild for the setup study: amendment A2 (c0a26a8ba) and A2.7 (0df2d65d9) of
docs/audits/2026-09-30-setup-grading-preregistration.md. PILOT by default; the full run is a separate
go from Alpha after the pilot report.

Per decision, the A1 chart features on ONE pool over [t - 1 h, t), strictly before the source's trade:
  pool   `sol_position_pools` + `sol_pool_vaults` (an AMM row's pool IS its coin vault; a Pump curve's
         coin vault is the curve's token account and its quote vault is the curve PDA). Missing, it is
         resolved from OUR entry transaction with the tape's own resolver into an in-memory store.
  t      the source transaction's blockTime when the origin carries its signature, else the signal
         event's created_at (flag `t_basis`).
  pull   Helius getTransactionsForAddress, NEWEST FIRST, full, jsonParsed, succeeded, blockTime in
         [t - 3600, t - 1], 1,000 per page, at most --max-tx transactions (A2.4's 10,000). The address
         is the coin vault's OWNER (the pool PDA, or the curve PDA) with `tokenTransfer.mint` = the
         coin: measured 2026-10-01 on position 3815, the vault itself returned 200 transactions in one
         second of which 180 were zero-delta bot touches, while the owner + mint filter returned only
         swaps (100 of 100). A shared authority is still exact: the filter keeps only this coin's
         transfers and the decode keeps only our vault. An owner that cannot be read falls back to the
         unfiltered vault (`address_kind`).
  swaps  from the POOL's side with the tape's decoders (`vault_rows`, `pump_curve_rows`): opposite-sign
         legs are a swap (price = |quote| / |coin|, volume = |quote|); same-sign legs are a liquidity
         event, excluded and counted. No trader-side decode (`trade_of`) is used.
  source excluded by its signature, else every transaction it signed in [t - 60 s, t) when its address
         is verified in `memecoin_address_registry`, else not excludable (flagged, counted).

Research key only (`pick_research_url`). Before every call: the flat-book check PAUSES while a real
Solana position is open (Long Hold excluded) and resumes where it stopped; pause time is logged and not
budgeted. Hard credit stop (--budget; 10 per page, 1 per getTransaction). Three consecutive 429s, after
5 s and 10 s back-offs, stop the run. <= 4 requests/s. Reads the ledger and tapes read-only; writes only
--out (JSONL, one row per decision) and --out.summary.json. No outcome column is read.

    python3 scripts/research/setup_solana_rebuild.py --out data/research_setup_solana_pilot.jsonl
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPLIT = "2026-09-23T00:00:00+00:00"
WINDOW_S, PAGE, SPACING_S = 3600, 1000, 0.5       # 2 req/s: Lambda's exit tape shares this key
TX_CREDITS = 1


def page_credits(result):
    """Helius: 10 credits per 100 returned transactions, rounded up, 10 minimum."""
    return max(10, 10 * -(-len((result or {}).get("data") or []) // 100))
FLAT_POLL_S = 30.0
BACKOFF_S = (5.0, 10.0)
SOURCE_WALLET_S = 60
POPULATION_SQL = (
    "SELECT p.id, p.mint, p.opened_at, p.source_user_id, e.fomo_event_id, e.created_at "
    "FROM memecoin_positions p JOIN memecoin_events e ON e.fomo_event_id = p.origin_event_id "
    "WHERE p.execution_mode='real' AND p.is_open=0 AND p.realized_pnl IS NOT NULL "
    "AND p.network_id='solana' AND COALESCE(p.signal_source,'') != 'manual' ORDER BY p.id")


class Stop(Exception):
    pass


def epoch(iso):
    return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()


class Caller:
    """Every RPC goes through here: flat-book pause, credit budget, spacing, 429 policy."""

    def __init__(self, post, ledger, budget, *, sleep=time.sleep, clock=time.monotonic):
        self.post, self.ledger, self.budget = post, ledger, float(budget)
        self.sleep, self.clock = sleep, clock
        self.spent = self.calls = 0
        self.paused_s = 0.0
        self.pauses = []

    def open_solana(self):
        return self.ledger.execute(
            "SELECT COUNT(*) FROM memecoin_positions WHERE execution_mode='real' AND is_open=1 "
            "AND network_id='solana' AND tracking_paused_at IS NULL").fetchone()[0]

    def wait_flat(self):
        start = None
        while self.open_solana():
            if start is None:
                start = self.clock()
                print(f"PAUSE {datetime.now(timezone.utc).isoformat()}: book not flat", flush=True)
            self.sleep(FLAT_POLL_S)
        if start is not None:
            waited = self.clock() - start
            self.paused_s += waited
            self.pauses.append(round(waited, 1))
            print(f"RESUME after {waited:.0f}s", flush=True)

    def __call__(self, method, params, credits, max_credits=None):
        """`credits` is a number, or a function of the result with `max_credits` its worst case."""
        worst = max_credits if callable(credits) else credits
        if self.spent + worst > self.budget:
            raise Stop(f"budget: {self.spent} spent, next call up to {worst} > {self.budget:.0f}")
        for attempt in range(len(BACKOFF_S) + 1):
            self.wait_flat()
            self.sleep(SPACING_S)
            try:
                out = self.post(method, params)
            except urllib.error.HTTPError as exc:
                if exc.code != 429:
                    raise Stop(f"HTTP {exc.code} on {method}")
                if attempt == len(BACKOFF_S):
                    raise Stop("three consecutive 429s")
                self.sleep(BACKOFF_S[attempt])
                continue
            except (urllib.error.URLError, OSError) as exc:   # timeouts: same back-off, then stop
                if attempt == len(BACKOFF_S):
                    raise Stop(f"network: {type(exc).__name__} on {method}")
                self.sleep(BACKOFF_S[attempt])
                continue
            self.calls += 1
            self.spent += credits(out.get("result")) if callable(credits) else credits
            if out.get("error"):
                raise Stop(f"RPC error on {method}: {json.dumps(out['error'])[:200]}")
            return out.get("result")
        raise Stop("unreachable")


def http_post(url):
    def post(method, params):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        req = urllib.request.Request(url, body, {"Content-Type": "application/json",
                                                 "User-Agent": "sniper-research/1"})
        return json.loads(urllib.request.urlopen(req, timeout=120).read())
    return post


def get_tx(call, sig):
    return call("getTransaction", [sig, {"encoding": "jsonParsed", "commitment": "confirmed",
                                         "maxSupportedTransactionVersion": 1}], TX_CREDITS)


def owner_of(call, tape, binding):
    """The coin vault's SPL owner: the curve PDA for a curve, the tape's `program` column (the owner,
    despite its name), else one getAccountInfo. None when the account cannot be read."""
    if binding["curve"]:
        return binding["quote_vault"]
    row = tape.execute("SELECT program FROM sol_pool_vaults WHERE pool=?",
                       (binding["coin_vault"],)).fetchone()
    if row and row[0]:
        return row[0]
    res = call("getAccountInfo", [binding["coin_vault"], {"encoding": "jsonParsed"}], TX_CREDITS)
    try:
        return res["value"]["data"]["parsed"]["info"]["owner"]
    except (TypeError, KeyError):
        return None


def pull(call, address, t, max_tx, on_page, mint=None, window_s=WINDOW_S):
    """Newest-first transactions on `address` in [t - 3600, t - 1], handed to `on_page` one page at a
    time and then dropped (10,000 parsed transactions held at once is an OOM risk on the host that
    runs the funded runner). Returns (pages, complete, coverage_start): complete means the pull reached
    the window start, else coverage starts one second after the oldest transaction reached (its own
    second may be cut)."""
    token, pages, seen, oldest = None, 0, 0, None
    while seen < max_tx:
        opts = {"transactionDetails": "full", "sortOrder": "desc", "limit": PAGE,
                "encoding": "jsonParsed", "maxSupportedTransactionVersion": 1,
                "filters": {"status": "succeeded",
                            "blockTime": {"gte": int(t) - window_s, "lte": int(t) - 1}}}
        if mint:
            opts["filters"]["tokenTransfer"] = {"mint": mint}
        if token:
            opts["paginationToken"] = token
        res = call("getTransactionsForAddress", [address, opts], page_credits,
                   max_credits=page_credits({"data": [None] * PAGE})) or {}
        pages += 1
        data = res.get("data") or []
        seen += len(data)
        on_page(data)
        stamps = [tx.get("blockTime") for tx in data if tx.get("blockTime")]
        if stamps:
            oldest = min(stamps + ([oldest] if oldest is not None else []))
        token = res.get("paginationToken")
        if not token or len(data) < PAGE:
            return pages, True, float(int(t) - window_s)
    return pages, False, (float(oldest) + 1.0 if oldest is not None else float(t))


def _signers(tx):
    keys = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    return {k.get("pubkey") for k in keys if isinstance(k, dict) and k.get("signer")}


def _sig(tx):
    sigs = (tx.get("transaction") or {}).get("signatures") or []
    return sigs[0] if sigs else None


def _created(tx, vault):
    """True when this transaction created the vault: absent before, present after."""
    from sniper.services.memecoin_solana_amounts import _account_keys
    keys = _account_keys(tx)
    meta = tx.get("meta") or {}
    idx = {e.get("accountIndex") for e in meta.get("preTokenBalances") or []}
    post = {e.get("accountIndex") for e in meta.get("postTokenBalances") or []}
    try:
        i = keys.index(vault)
    except ValueError:
        return False
    return i in post and i not in idx


class PoolSide:
    """Pool-side swaps, newest first, accumulated page by page (see module doc)."""

    def __init__(self, *, mint, binding, t, source_sig=None, source_wallets=(), keep=0):
        self.mint, self.binding, self.t = mint, binding, t
        self.source_sig, self.source_wallets = source_sig, set(source_wallets or ())
        self.swaps, self.created_at, self.keep, self.kept = [], None, keep, []
        self.n = {"txs": 0, "liquidity": 0, "no_swap": 0, "source_excluded": 0}

    def legs(self, tx):
        from sniper.services.memecoin_solana_amounts import pump_curve_rows, vault_rows
        b, sig = self.binding, _sig(tx)
        rows = (pump_curve_rows(tx, self.mint, b["coin_vault"], b["quote_vault"],
                                slot=tx.get("slot"), signature=sig) if b["curve"] else
                vault_rows(tx, self.mint, b["coin_vault"], slot=tx.get("slot"), signature=sig,
                           quote_vault=b["quote_vault"]))
        return [r for r in rows if r["kind"] in ("sol_swap", "sol_curve_swap", "sol_mint", "sol_burn")]

    def add(self, txs):
        for tx in txs:
            ts = tx.get("blockTime")
            if ts is None:
                continue
            self.n["txs"] += 1
            if (self.source_sig and _sig(tx) == self.source_sig) or (
                    self.source_wallets & _signers(tx) and self.t - SOURCE_WALLET_S <= ts < self.t):
                self.n["source_excluded"] += 1
                continue
            if _created(tx, self.binding["coin_vault"]):
                self.created_at = ts if self.created_at is None else min(self.created_at, ts)
            legs = self.legs(tx)
            if not legs:
                self.n["no_swap"] += 1
                continue
            for r in legs:
                if r["kind"] in ("sol_mint", "sol_burn"):
                    self.n["liquidity"] += 1
                    continue
                coin, quote = abs(int(r["amount0"])), abs(int(r["amount1"]))
                if coin and quote:
                    self.swaps.append((float(ts), quote / coin, float(quote)))
                    if len(self.kept) < self.keep:
                        self.kept.append(hand_row(tx, self.mint, quote / coin))


def hand_row(tx, mint, pool_price):
    """One swap seen from the pool and from its fee payer, for a human to compare (A2.3)."""
    from sniper.services.memecoin_solana_amounts import _fee_payer, _native_state, account_states
    payer = _fee_payer(tx)
    coin = sum(d for _a, (m, o, d, _p) in account_states(tx).items() if m == mint and o == payer)
    native = (_native_state(tx, payer) or (0, 0))[0] + int((tx.get("meta") or {}).get("fee") or 0)
    return {"sig": _sig(tx), "pool_price": pool_price, "trader_coin": coin,
            "trader_sol_lamports": native,
            "trader_price": abs(native) / abs(coin) if coin and native else None}


def features(swaps, t, coverage_start, created_at, complete):
    """A1's features and category over the sub-windows this decision covers (A2.1)."""
    def within(lo, hi=t):
        return [s for s in swaps if lo <= s[0] < hi]
    have = {w: coverage_start <= t - w for w in (300, 900, 3600)}
    out = {"complete_5m": have[300], "complete_15m": have[900], "complete_1h": have[3600],
           "swaps": len(swaps)}
    if not swaps or len(swaps) < 5 or not have[300]:
        out["category"] = "C0"
        out["c0_reason"] = "fewer_than_5_swaps" if len(swaps) < 5 else "no_complete_5m"
        return out
    last = swaps[0][1]                                   # newest first
    hour, pre, five, fifteen = within(t - 3600), within(t - 3600, t - 300), within(t - 300), within(t - 900)
    out.update(
        last=last,
        high_1h=max(p for _, p, _ in hour) if have[3600] else None,
        high_pre=(max(p for _, p, _ in pre) if pre else None) if have[3600] else None,
        low_5m=min(p for _, p, _ in five) if five else None,
        vol_5m=sum(v for *_, v in five),
        vol_base=sum(v for *_, v in pre) / 11.0 if have[3600] else None,
        range_15m=(max(p for _, p, _ in fifteen) / min(p for _, p, _ in fifteen) - 1.0)
        if have[900] and fifteen else None,
        swaps_15m=len(fifteen) if have[900] else None,
        pool_age_s=(t - created_at) if created_at is not None else (">=3600" if complete else None))
    checks = []                                          # (category, scoreable, fires) in A1 order
    age = out["pool_age_s"]
    checks.append(("C1", age is not None, isinstance(age, float) and age < 600))
    checks.append(("C2", have[3600], out["high_pre"] is not None
                   and last >= out["high_pre"] and out["vol_5m"] >= 3.0 * out["vol_base"]))
    checks.append(("C3", True, bool(five) and last >= 1.5 * out["low_5m"]))
    checks.append(("C4", have[3600], have[3600] and last <= 0.60 * out["high_1h"]))
    checks.append(("C5", have[3600], have[3600] and 0.60 * out["high_1h"] < last <= 0.85 * out["high_1h"]))
    checks.append(("C6", have[900], out["range_15m"] is not None
                   and out["range_15m"] <= 0.10 and out["swaps_15m"] >= 10))
    out["scoreable"] = [c for c, ok, _ in checks if ok]
    fired = next((c for c, ok, fires in checks if ok and fires), None)
    if fired:
        out["category"] = fired
    elif len(out["scoreable"]) == len(checks):
        out["category"] = "C7"            # A2.9: "none of C1-C6" only when all six were scoreable
    else:
        out["category"], out["c0_reason"] = "C0", "partly_unscoreable"
    return out


def binding_for(tape, ledger, pos, call):
    """{coin_vault, quote_vault, quote_mint, curve, source} or None."""
    row = tape.execute(
        "SELECT p.venue_kind, v.coin_vault, v.quote_vault, v.quote_mint FROM sol_position_pools p "
        "JOIN sol_pool_vaults v ON v.pool=p.pool AND v.mint=p.mint WHERE p.position_id=? AND p.mint=?",
        (pos["id"], pos["mint"])).fetchone()
    source = "sol_position_pools"
    if row is None:
        from sniper.persistence.db_ticks import connect_sol_tape
        from sniper.services.memecoin_solana_tape import ensure_sol_cursor, resolve_position_pool
        scratch = connect_sol_tape(":memory:")           # the resolver writes; never the real tape
        ensure_sol_cursor(scratch)
        resolve_position_pool(scratch, ledger, pos, lambda sig: get_tx(call, sig))
        row = scratch.execute(
            "SELECT p.venue_kind, v.coin_vault, v.quote_vault, v.quote_mint FROM sol_position_pools p "
            "JOIN sol_pool_vaults v ON v.pool=p.pool WHERE p.position_id=?", (pos["id"],)).fetchone()
        source = "entry_tx"
    if row is None:
        return None
    kind, coin_vault, quote_vault, quote_mint = row
    return {"coin_vault": coin_vault, "quote_vault": quote_vault, "quote_mint": quote_mint,
            "curve": kind == "pump_curve", "source": source}


def run_one(call, ledger, tape, row, verified, max_tx, keep=0, delta_s=0.0):
    pid, mint, opened_at, uid, origin, created = row
    from sniper.services.memecoin_slot_end_shadow import source_sig
    rec = {"position_id": pid, "split": "train" if opened_at < SPLIT else "test"}
    sig = source_sig(origin)
    t, rec["t_basis"] = epoch(created), "event_created_at"
    if sig:
        tx = get_tx(call, sig)
        if tx and tx.get("blockTime"):
            t, rec["t_basis"] = float(tx["blockTime"]), "source_tx"
    wallets = () if sig else verified.get(uid, ())
    rec["source_excluded_by"] = "signature" if sig else ("wallet" if wallets else "none")
    rec["t"], rec["event_created_at"] = t, epoch(created)
    if rec["source_excluded_by"] == "none" and delta_s:
        # A2.9: the source trade cannot be excluded, so every window ends delta_s before t.
        t = t - float(delta_s)
        rec["window_cut_s"] = float(delta_s)
    rec["t_window"] = t
    binding = binding_for(tape, ledger, {"id": pid, "mint": mint}, call)
    if binding is None:
        rec.update(category="C0", c0_reason="no_pool", pages=0)
        return rec
    rec.update(pool=binding["coin_vault"], quote_mint=binding["quote_mint"],
               pool_source=binding["source"], curve=binding["curve"])
    side = PoolSide(mint=mint, binding=binding, t=t, source_sig=sig, source_wallets=wallets, keep=keep)
    owner = owner_of(call, tape, binding)
    rec["address_kind"] = "owner_mint_filter" if owner else "vault_unfiltered"
    pages, complete, start = pull(call, owner or binding["coin_vault"], t, max_tx, side.add,
                                  mint=mint if owner else None)
    rec.update(pages=pages, pull_complete=complete, coverage_start=start, **side.n)
    rec.update(features(side.swaps, t, start, side.created_at, complete))
    if side.kept:
        rec["_hand"] = side.kept
    return rec


def vault_moves(txs, coin_vault):
    """Signatures whose transaction changed our coin vault's balance."""
    from sniper.services.memecoin_solana_amounts import account_states
    return {_sig(tx) for tx in txs if (account_states(tx).get(coin_vault) or (0, 0, 0))[2]}


def verify_filter(call, ledger, tape, rec, mint, window_s=300):
    """Gamma's A2.8 freeze condition: over [t - window_s, t), the transactions that moved our vault
    must be the same set whether pulled from the vault unfiltered or from its owner with the mint
    filter. None when the unfiltered pull is capped (the comparison would be incomplete)."""
    binding = binding_for(tape, ledger, {"id": rec["position_id"], "mint": mint}, call)
    owner = owner_of(call, tape, binding)
    t = rec["t"]
    got = {"plain": [], "filtered": []}
    complete = {}
    for kind, address, flt in (("plain", binding["coin_vault"], None), ("filtered", owner, mint)):
        token, n = None, 0
        while True:
            opts = {"transactionDetails": "full", "sortOrder": "desc", "limit": PAGE,
                    "encoding": "jsonParsed", "maxSupportedTransactionVersion": 1,
                    "filters": {"status": "succeeded",
                                "blockTime": {"gte": int(t) - window_s, "lte": int(t) - 1}}}
            if flt:
                opts["filters"]["tokenTransfer"] = {"mint": flt}
            if token:
                opts["paginationToken"] = token
            res = call("getTransactionsForAddress", [address, opts], page_credits,
                       max_credits=page_credits({"data": [None] * PAGE})) or {}
            data = res.get("data") or []
            got[kind].extend(data)
            n += 1
            token = res.get("paginationToken")
            if not token or len(data) < PAGE:
                complete[kind] = True
                break
            if n >= 2:
                complete[kind] = False
                break
    if not complete.get("plain"):
        return None
    plain = vault_moves(got["plain"], binding["coin_vault"])
    filtered = vault_moves(got["filtered"], binding["coin_vault"])
    return {"position_id": rec["position_id"], "window_s": window_s, "plain_txs": len(got["plain"]),
            "filtered_txs": len(got["filtered"]), "vault_moves_plain": len(plain),
            "vault_moves_filtered": len(filtered), "missing_from_filter": sorted(plain - filtered),
            "extra_in_filter": sorted(filtered - plain)}


def source_lags(call, ledger, tape, rows, verified, population):
    """Seconds from each EXCLUDED source trade to t, over the pilot's excluded decisions (A2.9):
    signature decisions give event created_at - source blockTime (t is that blockTime; no call);
    wallet decisions re-pull [t - 60 s, t) and take every transaction a verified source wallet
    signed, keeping the LATEST one before t (A2.9a: earlier trades by the source are not the copied
    one). Delta = the largest per-decision lag, rounded up to 5 s, at least 10 s."""
    created = {r[0]: epoch(r[5]) for r in population}
    uids = {r[0]: r[3] for r in population}
    mints = {r[0]: r[1] for r in population}
    lags = []
    for rec in rows:
        pid = rec["position_id"]
        if rec.get("source_excluded_by") == "signature" and rec.get("t_basis") == "source_tx":
            lags.append(("signature", pid, created[pid] - rec["t"]))
        elif rec.get("source_excluded_by") == "wallet" and rec.get("pool"):
            binding = binding_for(tape, ledger, {"id": pid, "mint": mints[pid]}, call)
            owner = owner_of(call, tape, binding)
            seen = []

            def page(txs):
                for tx in txs:
                    if verified.get(uids[pid], set()) & _signers(tx) and tx.get("blockTime"):
                        seen.append(tx["blockTime"])
            t = rec["t"]
            pull(call, owner or binding["coin_vault"], t, 10_000, page, mint=mints[pid] if owner else None,
                 window_s=SOURCE_WALLET_S)
            if seen:
                lags.append(("wallet", pid, t - max(seen)))
    worst = max((lag for *_, lag in lags), default=None)
    delta = max(10.0, 5.0 * -(-worst // 5)) if worst is not None else None
    return {"lags": lags, "max_lag_s": worst, "delta_s": delta}


def full_order(population):
    """A2.4's order: test decisions first, then train, each newest to oldest, so a budget stop
    leaves the scarcer test side complete."""
    test = [r for r in population if r[2] >= SPLIT]
    train = [r for r in population if r[2] < SPLIT]
    return sorted(test, key=lambda r: r[2], reverse=True) + sorted(train, key=lambda r: r[2], reverse=True)


def reused(path):
    """Pilot rows the full run keeps: everything whose source trade was excluded, with A2.9's C7
    rule applied (C7 with an unscoreable category becomes C0). `none` rows are dropped so they are
    pulled again with the delta cut. A reused row costs no new credit."""
    keep = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("source_excluded_by") == "none":
            continue
        if rec.get("category") == "C7" and len(rec.get("scoreable") or []) != 6:
            rec["category"], rec["c0_reason"] = "C0", "partly_unscoreable"
        rec["reused_from"], rec["credits"] = path.name, 0
        keep.append(rec)
    return keep


def _group(done):
    """Completeness, categories and credits for one slice of the decisions."""
    n = len(done) or 1
    cats = {}
    for r in done:
        cats[r["category"]] = cats.get(r["category"], 0) + 1
    return {"n": len(done), "complete_5m": sum(1 for r in done if r.get("complete_5m")) / n,
            "complete_1h": sum(1 for r in done if r.get("complete_1h")) / n,
            "categories": cats, "credits": sum(r.get("credits", 0) for r in done)}


def summarize(recs, population, stopped, caller):
    import statistics
    done = [r for r in recs if r.get("pages") is not None]
    credits = sorted(r.get("credits", 0) for r in done)
    q = lambda xs, p: xs[min(len(xs) - 1, int(p * len(xs)))] if xs else None
    n = len(done) or 1
    cats = {}
    for r in done:
        cats[r["category"]] = cats.get(r["category"], 0) + 1
    n_train = sum(1 for p in population if p[2] < SPLIT)
    n_test = len(population) - n_train
    proj = {c: {"train": round(k / n * n_train), "test": round(k / n * n_test)}
            for c, k in cats.items() if c != "C0"}
    eligible = sorted(c for c, v in proj.items() if v["train"] >= 30 and v["test"] >= 15)
    share = lambda key: sum(1 for r in done if r.get(key)) / n
    go = {"complete_5m>=0.80": share("complete_5m") >= 0.80,
          "complete_1h>=0.60": share("complete_1h") >= 0.60,
          "eligible_cells>=3": len(eligible) >= 3}
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "stopped": stopped,
            "decisions": len(done), "population": len(population), "train": n_train, "test": n_test,
            "credits_spent": caller.spent, "calls": caller.calls,
            "credits_per_decision": {"p50": q(credits, .5), "p90": q(credits, .9),
                                     "max": credits[-1] if credits else None},
            "pages": {"p50": q(sorted(r["pages"] for r in done), .5),
                      "max": max((r["pages"] for r in done), default=None)},
            "share_complete": {w: share("complete_" + w) for w in ("5m", "15m", "1h")},
            "swaps_per_decision_median": statistics.median([r.get("swaps", 0) for r in done]) if done else None,
            "liquidity_excluded_per_decision_median":
                statistics.median([r.get("liquidity", 0) for r in done]) if done else None,
            "categories": cats, "projected": proj, "eligible": eligible,
            "go": go, "go_all": all(go.values()),
            "t_basis": {b: sum(1 for r in done if r.get("t_basis") == b) for b in ("source_tx", "event_created_at")},
            # Gamma 2026-10-01: a decision whose source trade cannot be excluded may need C0 for C3.
            "by_source_exclusion": {b: _group([r for r in done if r.get("source_excluded_by") == b])
                                    for b in ("signature", "wallet", "none")},
            "address_kind": {k: sum(1 for r in done if r.get("address_kind") == k)
                             for k in ("owner_mint_filter", "vault_unfiltered")},
            "paused_s": round(caller.paused_s, 1), "pauses": caller.pauses}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--checkout", default=str(ROOT))
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--budget", type=float, default=15000)
    ap.add_argument("--max-tx", type=int, default=10_000)
    ap.add_argument("--out", required=True)
    ap.add_argument("--full", action="store_true",
                    help="A2.4: the whole population, test first, then train newest to oldest")
    ap.add_argument("--reuse", help="pilot JSONL whose rows the full run keeps (A2.9 applied); "
                                    "rows whose source could not be excluded are pulled again")
    ap.add_argument("--delta-s", type=float, default=0.0,
                    help="A2.9: windows of decisions whose source trade cannot be excluded end this "
                         "many seconds before t (measure it with --measure-lag)")
    ap.add_argument("--measure-lag", action="store_true",
                    help="measure A2.9's delta from the excluded source trades of the rows in --out")
    ap.add_argument("--verify", type=int, default=0,
                    help="run the A2.8 set comparison on this many low-activity decisions in --out")
    ap.add_argument("--resume", action="store_true",
                    help="keep the decisions already in --out and continue the same sample")
    a = ap.parse_args()
    checkout = Path(a.checkout)
    sys.path.insert(0, str(checkout))
    sys.path.insert(0, str(checkout / "scripts" / "research"))
    import os
    from sniper.util.envfiles import load_env_files
    load_env_files(checkout)
    from slot_lane_send_monitor import pick_research_url
    url = pick_research_url(os.environ)
    from sniper.persistence.db_core import get_readonly_connection
    ledger = get_readonly_connection(str(checkout / "sniper_tracker.db"))   # autocommit: no WAL pin
    from sniper.persistence.db_ticks import connect_sol_tape_readonly
    tape = connect_sol_tape_readonly(str(checkout / "data" / "memecoin_sol_tape.db"))
    verified = {}
    for uid, addr in ledger.execute("SELECT source_user_id, address FROM memecoin_address_registry "
                                    "WHERE network_id='solana' AND status='verified'"):
        verified.setdefault(uid, set()).add(addr)
    population = ledger.execute(POPULATION_SQL).fetchall()
    if a.full:
        sample = full_order(population)
    else:
        step = max(1, len(population) // a.n)
        sample = population[::step][:a.n]
    caller = Caller(http_post(url), ledger, a.budget)
    out = Path(a.out)
    if a.measure_lag:
        rows = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
        try:
            res = source_lags(caller, ledger, tape, rows, verified, population)
        except Stop as exc:
            print("STOPPED:", exc, flush=True)
            return 1
        res["credits_spent"] = caller.spent
        Path(str(out) + ".lag.json").write_text(json.dumps(res, indent=1))
        print(json.dumps({k: v for k, v in res.items() if k != "lags"}, indent=1))
        return 0
    if a.verify:
        mints = {r[0]: r[1] for r in population}
        rows = sorted((json.loads(line) for line in out.read_text().splitlines() if line.strip()),
                      key=lambda r: r.get("swaps") or 0)
        results = []
        try:
            for rec in rows:
                if len(results) >= a.verify:
                    break
                if not rec.get("swaps") or rec.get("curve") is None:
                    continue
                res = verify_filter(caller, ledger, tape, rec, mints[rec["position_id"]])
                if res is not None:
                    results.append(res)
                    print(res, flush=True)
        except Stop as exc:
            print("STOPPED:", exc, flush=True)
        Path(str(out) + ".verify.json").write_text(json.dumps(
            {"credits_spent": caller.spent, "results": results}, indent=1))
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    recs, checks, stopped = [], [], None
    if a.resume and out.exists():
        recs = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
        prior = json.loads(Path(str(out) + ".summary.json").read_text())
        checks = prior.get("hand_check") or []
        caller.spent, caller.calls = prior.get("credits_spent", 0), prior.get("calls", 0)
    if a.reuse and not recs:
        recs = reused(Path(a.reuse))
        out.write_text("".join(json.dumps(r) + "\n" for r in recs))
    done_ids = {r["position_id"] for r in recs}
    with out.open("a" if (a.resume or a.reuse) else "w") as fh:
        try:
            for row in sample:
                if row[0] in done_ids:
                    continue
                before = caller.spent
                rec = run_one(caller, ledger, tape, row, verified, a.max_tx,
                              keep=5 if len(checks) < 5 else 0, delta_s=a.delta_s)
                rec["credits"] = caller.spent - before
                hand = rec.pop("_hand", None)
                if hand and rec.get("swaps", 0) >= 5:
                    checks.append({"position_id": rec["position_id"], "swaps": hand})
                recs.append(rec)
                fh.write(json.dumps(rec) + "\n")
                fh.flush()
                print({k: rec.get(k) for k in ("position_id", "category", "pages", "credits",
                                               "swaps", "complete_5m", "complete_1h")}, flush=True)
        except Stop as exc:
            stopped = str(exc)
            print("STOPPED:", stopped, flush=True)
    summary = summarize(recs, population, stopped, caller)
    summary["hand_check"] = checks
    Path(str(out) + ".summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps({k: v for k, v in summary.items() if k != "hand_check"}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
