"""Dip retry for named wallets (operator order 2026-09-30).

"only for the most botted accounts, not for everyone, make it live for degen". On a heavily copied
wallet, copy bots buy right after the source and push our exact quote over the chase cap; the price
often gives some of that back within seconds. Gamma 8c3bbaba2: DegenCapitalLLC dip fills +22.4%
[+7.0, +32.2] on n=8, every dip within 15 s. Pooled over every wallet no window paid, so the scope is
a wallet list, never a global switch. Spec: docs/superpowers/specs/2026-09-30-memecoin-solana-dip-
retry-design.md (c8d05475e).

FLOW
1. `maybe_enqueue`: a Solana `skipped_chase` result for a wallet in MEMECOIN_REAL_DIP_RETRY_WALLETS
   becomes a dip row on memecoin_entry_retry_queue. `dip_json` carries the gate's own reference
   (quote_price / (1 + premium)), its cap (from the reason), our reserved clip and a deadline of
   SIGNAL time + MEMECOIN_REAL_DIP_RETRY_S. It runs after the refusal is recorded, so the first
   attempt is untouched.
2. `DipWatch` (one daemon thread): one exact buy quote of the clip per tick, round-robin over live
   rows; within the cap marks the row due in memory. It reads the ledger on its own read-only
   connection and writes nothing. It never quotes while a funded Jupiter host is cooling, and pauses
   60 s after a quote that left one cooling (its own 429): exits share those hosts.
3. `run_hits` (main thread, from the retry lane): a due row runs the WHOLE decision once. Every
   gate re-checks, the chase cap on a fresh funded quote, age from the original signal. A fill or
   any other verdict ends the row; another chase refusal keeps it until MAX_HITS or the deadline.
   A source sell after the signal ends it without a decision.

The blind retry lane never touches a dip row (due/reap exclude `dip_json`), so no ENTRY-GAVE-UP
page fires for a dip that never came.

EVM (2026-09-30): chains come from MEMECOIN_REAL_DIP_RETRY_CHAINS (default solana). On EVM only
the execution premium's source-basis refusal is queued ("route quote is Rx the source price (cap
Cx)"); its reference is exact from the refusal. Since 2026-10-08 its post-trade form (E11 on-chain,
"... the post-trade price (cap Cx)") is queued too, with the cap restated on the source basis. A chain in MEMECOIN_REAL_DIP_RETRY_SHADOW_CHAINS
logs `shadow_hit` and never decides. Every row ends in one log row with the watch's min premium,
quotes, unknown quotes and its tick series.

THE EVM WATCH PRICES AS IF WE HELD THE POSITION (operator order 86d053ce, spec "Rebuild" and
"Winner-rung ticks"). No display feed and no research host: the funded chain context's own quote
functions, one thread per chain.
- Tick 0 is the refusal's own quote (`dip_route`: rung, sell_atomic, buy_atomic, legs_path).
- Every later tick re-quotes the WINNING RUNG at the refusal's input: the pinned pool for a V4
  single hop, the pinned path for a V4 multihop, Kyber's routes call for a Kyber route, the ladder
  with `only_rung` for the rest.
- NO PROVIDER POOL LIST MAY BE READ FROM A TICK, and two rules keep it so:
  - The two V4 rungs discover pools through the display providers' pool lists, so the watch never
    runs them: it quotes pools whose key we already hold (`evm_v4_quote.pinned_buy_out`), and its
    ladder tick excludes both rungs.
  - On a chain in MEMECOIN_EVM_V2_PAIR_DISCOVERY_CHAINS or MEMECOIN_EVM_MIXED_HOP_PROBE_CHAINS
    (both default 56) the v2, v3, v3_bridge and mixed rungs read the coin's pair list from
    DexScreener (`evm_v2_exit_quote.pair_quote_tokens`). Since 2026-10-08 (operator: dip "for top
    traders on all chains"; design 92f34c9b7) the dip retry runs there too, but the watch never runs
    those rungs: it re-quotes the refused route's own pin (`dip_route.pin`: the v2 path and cut, the
    v3 path with paths_only, or the mixed hop's v3 leg and its one v2 pair), and its ladder tick
    excludes them (DISCOVERY_RUNGS). The HIT is the normal decision, as every 56 entry.
  Runtime-tested on the real ladder for 4663 and 8453, with BSC as the control that does read.
- An aggregator quote can be stale (HOOKERS: Kyber 1.82x while the pool stood at 1.46x) and never
  misses. So an aggregator winner, and every ladder tick, is also priced on the held V4 pools of
  the mint, and the better output counts.
- Two winner misses lead to one ladder tick, which adopts its winner.
- The premium is `(1 + refused) * buy_atomic_0 / buy_atomic_t - 1`: token outputs at one input,
  so the watch thread reads no quote-token USD price. The full decision re-prices live.
- A hit wakes the runner's dip hit lane, its own thread (the main loop's period is 5 s, and a
  hit on the on-chain lane would hold that lane's detections for a whole decision). A typed rate
  limit pauses that chain's watch for 60 s.
"""
from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from datetime import datetime

from sniper.persistence import db_memecoin as m

logger = logging.getLogger(__name__)

_CAP_RE = re.compile(r"\(cap ([0-9]+(?:\.[0-9]+)?)%\)")
# The EVM execution premium, source basis only (evm_swap_executor._premium_refusal). The chart-mid
# basis is not queued: its reference is the live chart, which a stored number cannot follow.
_EVM_PREMIUM_RE = re.compile(
    r"^route quote is [0-9]+(?:\.[0-9]+)?x the source price \(cap ([0-9]+(?:\.[0-9]+)?)x\)")
# The same premium on the source's POST-TRADE price (E11 on-chain, 2026-10-08). Its cap is restated on
# the watch's quote-over-source basis: quote/post <= C  <=>  quote/source <= C x post/source, with the
# post-trade price from the refusal's `premium_reference_price` (Alpha: "don't switch a working
# feature off").
_EVM_PREMIUM_POST_RE = re.compile(
    r"^route quote is [0-9]+(?:\.[0-9]+)?x the post-trade price \(cap ([0-9]+(?:\.[0-9]+)?)x\)")
# The EVM copy-premium hard cap on the SIGNED minimum (entry_attribution.copy_minimum_refusal; 2026-10-08,
# SOCKET 8346cfef). Its reference is the source average fill, as the premium's. "minimum unavailable" and
# Relay's "unproved" measured nothing and do not match.
_EVM_COPY_FLOOR_RE = re.compile(
    r"^copy price minimum is [+-]?[0-9]+(?:\.[0-9]+)?% above source fill \(cap ([0-9]+(?:\.[0-9]+)?)%\)")
PAUSE_AFTER_COOLING_S = 60.0     # the funded host cooldown (solana_real_seams.JUP_HOST_COOLDOWN_S)
SERIES_MAX = 120                 # ticks kept per row for the terminal log row
LADDER_AFTER_MISSES = 2          # consecutive winner misses before one full-ladder tick
HELD_RESCAN_S = 5.0              # after a held-pool scan that priced nothing, before the next scan
WATCH_EXCLUDED_RUNGS = ("v4", "v4_multihop")  # never run by the watch: see the docstring
# On a display-feed chain (56) these rungs read the coin's pair list (DexScreener): the watch quotes
# them only on the refused route's own pin, and never runs them from a ladder tick (design 92f34c9b7).
DISCOVERY_RUNGS = ("v2", "v3", "v3_bridge", "mixed_v3_v2")
_PIN_TAKER = "0x000000000000000000000000000000000000dEaD"   # quote calldata only; never signed
# Raised by the watch's own code or wiring, never by a provider: a key the context lacks, a seam
# that is None, a wrong call shape.
WATCH_FAULTS = (LookupError, AttributeError, TypeError, NameError, ImportError)
AGGREGATOR_RUNGS = ("kyber", "relay")         # quotes that carry no block and can be stale


def _cfg():
    from sniper.settings import memecoin as cfg
    return cfg


def _epoch(iso):
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def enabled():
    cfg = _cfg()
    return float(cfg.MEMECOIN_REAL_DIP_RETRY_S or 0) > 0 and bool(cfg.MEMECOIN_REAL_DIP_RETRY_WALLETS)


def _chain_set(values):
    from sniper.services.memecoin_chains import canonical_chain_id
    out = set()
    for value in values or ():
        chain = "solana" if str(value).lower() in {"solana", "sol"} else canonical_chain_id(value)
        if chain:
            out.add(str(chain))
    return out


def display_feed_chains():
    """EVM chains whose entry ladder reads a display provider's pool list outside the V4 rungs:
    the pair discovery of the v2/v3 rungs and the mixed-hop probe (Gamma MEDIUM on 4a4a4773c)."""
    cfg = _cfg()
    return _chain_set(tuple(getattr(cfg, "MEMECOIN_EVM_V2_PAIR_DISCOVERY_CHAINS", ()) or ())
                      + tuple(getattr(cfg, "MEMECOIN_EVM_MIXED_HOP_PROBE_CHAINS", ()) or ()))


def chains():
    """Network ids the dip retry runs on, read at call time. A display-feed chain (56) runs too since
    2026-10-08: its watch re-quotes only the refused route's pin and never discovers (operator order
    86d053ce still holds for the watch; design 92f34c9b7)."""
    return _chain_set(_cfg().MEMECOIN_REAL_DIP_RETRY_CHAINS)


def evm_chains():
    return chains() - {"solana"}


def shadow_chains():
    return _chain_set(getattr(_cfg(), "MEMECOIN_REAL_DIP_RETRY_SHADOW_CHAINS", ()))


def _network(obj):
    value = obj.get("network_id") if isinstance(obj, dict) else getattr(obj, "network_id", None)
    return str(value or "solana")


def _source_tx(fomo_event_id):
    """The source transaction one signal is about, whichever lane delivered it: J7
    `swap:raybot:<tx>:<wallet>:<mint>` or the on-chain lane `onchain:<chain>:<tx>:...`. None for
    any other id shape (no cross-lane dedupe is possible). EVM hashes lowercased."""
    parts = str(fomo_event_id or "").split(":")
    if len(parts) >= 3 and ((parts[0] == "swap" and parts[1] == "raybot") or parts[0] == "onchain"):
        tx = parts[2]
        return tx.lower() if tx.lower().startswith("0x") else tx or None
    return None


def _terminal_not_queued(conn, ev, network, status, reason, dip):
    """A terminal log row for an in-scope refusal that is NOT queued, so the denominator stays
    whole: every scoped chase refusal ends in exactly one log row."""
    m.record_dip_retry_hit(
        conn, fomo_event_id=ev.fomo_event_id, source_user_id=getattr(ev, "source_user_id", None),
        mint=str(ev.mint) if network == "solana" else str(ev.mint).lower(),
        hit_at=dip.get("queued_at"), watch_premium_pct=None, cap_pct=dip.get("cap_pct"),
        status=status, reason=reason, position_id=None, network_id=network, stats=None)


def _min_ratio(route):
    """k = buy_atomic / min_atomic of the refused quote, or None: the signed minimum's worst price is
    the quote price x k."""
    try:
        buy, low = int((route or {})["buy_atomic"]), int((route or {})["min_atomic"])
    except (KeyError, TypeError, ValueError):
        return None
    return buy / low if 0 < low <= buy else None


def _reference_and_cap(network, res, source_user_id=None):
    """(reference USD per token, cap %, refused premium %) of the gate that refused, or None. On EVM a
    fourth element names the gate and its own numbers for the row (dip_json)."""
    if getattr(_cfg(), "MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY", False):
        return None  # Legacy refusal fields describe quote averages, not marginal market movement.
    reason = str(res.get("reason") or "")
    if network == "solana":
        named = [res.get(k) for k in ("gate_reference_price", "gate_cap_pct", "gate_premium_pct")]
        if all(v is not None for v in named):
            # A signed-floor refusal names its own gate (SINS 54M6qVo9): the hard copy cap's
            # reference is the source's average fill, not the chase reference. A floor that failed
            # for another reason (a malformed quote) is not over its cap: nothing to watch (Gamma).
            reference, cap_pct, pct = (float(v) for v in named)
            return (reference, cap_pct, pct) if pct > cap_pct else None
        cap = _CAP_RE.search(reason)
        if not cap or res.get("quote_chase_pct") is None or res.get("quote_price") is None:
            return None
        pct = float(res["quote_chase_pct"])
        return float(res["quote_price"]) / (1.0 + pct / 100.0), float(cap.group(1)), pct
    premium, floor = _EVM_PREMIUM_RE.match(reason), _EVM_COPY_FLOOR_RE.match(reason)
    post = _EVM_PREMIUM_POST_RE.match(reason)
    if (not (premium or floor or post) or res.get("source_drift_pct") is None
            or res.get("quote_price") is None):
        return None
    # Both gates compared against pending_source_price, and source_drift_pct is
    # quote_price / pending_source_price - 1 from the same number: exact, not the printed ratio.
    # THE WATCH TRACKS THIS QUOTE PREMIUM q; every cap below is put on that one basis.
    pct = float(res["source_drift_pct"])
    k = _min_ratio(res.get("dip_route"))
    if floor:
        if k is None:
            return None                     # the signed cap cannot be put on the quote basis
        copy_cap = float(floor.group(1))
        gate = {"gate": "copy_signed_floor", "gate_cap_pct": copy_cap, "min_ratio": k}
        cap = _evm_watch_cap(None, network, copy_cap_pct=copy_cap, min_ratio=k)
    elif post:
        source = float(res["quote_price"]) / (1.0 + pct / 100.0)
        try:
            ratio = float(res.get("premium_reference_price")) / source
        except (TypeError, ValueError, ZeroDivisionError):
            return None                     # no post-trade price: the cap cannot be restated
        if not (math.isfinite(ratio) and ratio > 0):
            return None
        try:
            copy_cap = float(_cfg().copy_premium_cap(source_user_id, network)[0])
        except Exception:
            logger.exception("dip retry: copy cap unreadable on %s; no copy bound", network)
            copy_cap = None
        gate = {"gate": "execution_premium_post_trade", "gate_cap_pct": float(post.group(1)),
                "post_over_source": ratio, "min_ratio": k}
        cap = _evm_watch_cap((float(post.group(1)) * ratio - 1.0) * 100.0, network,
                             copy_cap_pct=copy_cap, min_ratio=k)
    else:
        try:
            copy_cap = float(_cfg().copy_premium_cap(source_user_id, network)[0])
        except Exception:
            logger.exception("dip retry: copy cap unreadable on %s; no copy bound", network)
            copy_cap = None
        gate = {"gate": "execution_premium", "gate_cap_pct": float(premium.group(1)),
                "min_ratio": k}
        cap = _evm_watch_cap((float(premium.group(1)) - 1.0) * 100.0, network,
                             copy_cap_pct=copy_cap, min_ratio=k)
    if cap is None:
        return None
    return float(res["quote_price"]) / (1.0 + pct / 100.0), cap, pct, gate


def _evm_watch_cap(premium_cap_pct, network, *, copy_cap_pct=None, min_ratio=None):
    """The watch fires only where the rerun decision can fill (Gamma, DegenCapitalLLC 4663,
    2026-09-30 20:35Z, dip log 8/9). The queued refusal was the execution premium (1.50x), but the
    decision also caps OUR QUOTE over the source price (`_quote_over_source_refusal`, 30% on
    4663, chain-wide). A hit in (30%, 50%] burned a full decision and a funded quote, was refused
    `our quote is +49% above source price (cap 30%)`, and used a MAX_HITS slot a real dip needed.

    The cap is read from the decision's own settings function, never a copy. Its reference is the
    chase reference (the source's post-trade price when known, else the average fill), which is
    within 2% of the source price or above it (post_trade RATIO_MIN 0.98, `memecoin_evm_post_trade`;
    Gamma: 15 of 55 live 4663 ratios are under 1). So a hit in about (27.4%, 30%] can still be
    refused by that gate and use a MAX_HITS slot; Alpha kept it (2026-09-30): the band is a tenth as
    wide, and a stricter watch would skip hits that fill when the ratio is 1 or more. The market
    chase gate (chart vs reference) is not predicted: the watch sees no chart.

    THE COPY FLOOR TOO (2026-10-08, SOCKET 8346cfef). The hard copy cap judges the SIGNED minimum,
    whose worst price is the quote price x k (k = buy/min of the refused quote, 1/(1 - slippage); the
    watch re-quotes the same rung at the same slippage). It passes iff (1 + q)k <= 1 + cap, so on the
    watch's quote basis its bound is (1 + cap)/k - 1. On 4663 (attempt 0 at the 8% retry slippage) that
    is 24.2% at the 35% cap, under the 30% over-source cap; on a 3% chain it is 31%. Off (cap 0) or with
    k unknown: no bound. None when no cap applies."""
    try:
        over_source = _cfg().quote_over_source_max_pct_for(network)
    except Exception:
        logger.exception("dip retry: quote-over-source cap unreadable on %s; premium cap kept",
                         network)
        over_source = None
    caps = [c for c in (premium_cap_pct, over_source) if c is not None]
    if copy_cap_pct and copy_cap_pct > 0 and min_ratio:
        caps.append(((1.0 + float(copy_cap_pct) / 100.0) / float(min_ratio) - 1.0) * 100.0)
    return min(float(c) for c in caps) if caps else None


def dip_watch_cap(dip, network):
    """The premium a watch tick must be at or under to hit: the gate's own cap, capped by
    MEMECOIN_REAL_DIP_RETRY_MAX_PREMIUM_PCT when set (operator 2026-10-09). On EVM the decision binds
    the SIGNED minimum (worst = quote x k), so the knob is restated on the watch's quote basis as
    (1 + knob)/k - 1; with k unknown the knob itself (Alpha, A)."""
    if getattr(_cfg(), "MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY", False):
        return None
    cap = float(dip["cap_pct"])
    knob = getattr(_cfg(), "MEMECOIN_REAL_DIP_RETRY_MAX_PREMIUM_PCT", None)
    if knob is None:
        return cap
    knob = float(knob)
    k = dip.get("min_ratio")
    if network != "solana" and isinstance(k, (int, float)) and math.isfinite(k) and k > 0:
        knob = ((1.0 + knob / 100.0) / float(k) - 1.0) * 100.0
    return min(cap, knob)


def dip_decision_cap(dip):
    """ev.dip_cap for the decision a hit runs, or None (knob unset, or no recorded reference): the
    reference the row recorded and min(gate cap, knob), which both executors enforce on the fresh
    quote and on the signed minimum.

    BASES ON EVM (Gamma LOW, 2026-10-09): the row's cap_pct is on the QUOTE basis (restated through
    k), while the knob and the executor's copy_minimum_refusal judge the SIGNED minimum. With the knob
    below the gate cap (10 against a typical 24-50) the knob binds, as intended; otherwise the gate
    cap is applied on the signed basis, which is only ever STRICTER than the gate itself. The
    reference is the source's (Solana: the chase reference or the source average fill; EVM: the
    source price), so the knob is a premium over the SOURCE's fill, not over our refused quote."""
    if getattr(_cfg(), "MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY", False):
        return None
    knob = getattr(_cfg(), "MEMECOIN_REAL_DIP_RETRY_MAX_PREMIUM_PCT", None)
    try:
        reference, cap = float(dip["reference_price"]), float(dip["cap_pct"])
    except (KeyError, TypeError, ValueError):
        return None
    if knob is None or not (math.isfinite(reference) and reference > 0):
        return None
    return {"reference_price": reference, "max_pct": min(cap, float(knob)), "knob_pct": float(knob),
            "gate_cap_pct": cap}


def in_scope(ev):
    wallets = _cfg().MEMECOIN_REAL_DIP_RETRY_WALLETS
    ids = {str(getattr(ev, "source_user_id", "") or "").lower(),
           str(getattr(ev, "source_handle", "") or "").lower()} - {""}
    return bool(ids & set(wallets))


def maybe_enqueue(conn, ev, res, now_iso, *, event_json):
    """Queue a scoped chase refusal as a dip row. True when queued. Never raises."""
    try:
        res = res or {}
        network = _network(ev)
        if (res.get("status") != "skipped_chase" or not enabled() or network not in chains()
                or getattr(ev, "side", "buy") != "buy" or not in_scope(ev) or not event_json):
            return False
        gate = _reference_and_cap(network, res, getattr(ev, "source_user_id", None))
        clip = m.entry_attempt_notional(conn, res.get("attempt_id"))
        signal = _epoch(getattr(ev, "created_at", None))
        if not (gate and gate[0] > 0 and gate[2] > -100 and clip and float(clip) > 0 and signal):
            logger.warning("dip retry: %s not queued (gate=%s clip=%s signal=%s)",
                           getattr(ev, "fomo_event_id", None), bool(gate), clip, signal)
            return False
        reference, cap_pct, pct = gate[:3]
        dip = {"deadline": signal + float(_cfg().MEMECOIN_REAL_DIP_RETRY_S), "signal_at": signal,
               "reference_price": reference, "cap_pct": cap_pct, **(gate[3] if len(gate) > 3 else {}),
               "clip_usd": float(clip), "source_user_id": getattr(ev, "source_user_id", None),
               "refused_premium_pct": pct}
        dip["queued_at"] = now_iso
        source_tx = _source_tx(getattr(ev, "fomo_event_id", None))
        if source_tx and m.dip_retry_source_seen(conn, network, source_tx):
            _terminal_not_queued(conn, ev, network, "duplicate_source_tx", None, dip)
            return False
        dip["source_tx"] = source_tx
        if network != "solana" and isinstance(res.get("dip_route"), dict):
            dip["dip_route"] = res["dip_route"]     # the refusal's own quote: the watch's tick 0
        mint = ev.mint if network == "solana" else str(ev.mint).lower()
        return m.enqueue_dip_retry(conn, ev.fomo_event_id, mint, network, event_json, now_iso,
                                   json.dumps(dip, sort_keys=True))
    except Exception:
        logger.exception("dip retry: enqueue failed for %s", getattr(ev, "fomo_event_id", None))
        return False


def _funded_host_cooling():
    from sniper.execution.solana_real_seams import _jup_host_cooldown, _monotonic
    now = _monotonic()
    return any(until > now for until in list(_jup_host_cooldown.values()))


def _default_quote(mint, usd):
    from sniper.execution.solana_real_seams import jupiter_entry_quote
    return jupiter_entry_quote(mint, usd)


def _default_decimals(mint):
    from sniper.execution.solana_real_seams import token_supply
    return token_supply(mint, _cfg().MEMECOIN_SOLANA_RPC_URL)[1]


class DipWatch(threading.Thread):
    """Quote-only watch. Never reserves, signs or writes; `take_due` hands hits to the hit host.
    This one quotes Solana rows on the funded Jupiter seam; `EvmDipWatch` quotes one EVM chain."""

    thread_name = "memecoin-dip-watch"

    def __init__(self, db_path, *, quote_fn=None, decimals_fn=None, cooling_fn=None, wake_fn=None,
                 clock=time.time, sleep=time.sleep):
        super().__init__(name=self.thread_name, daemon=True)
        self.db_path = db_path
        self.quote_fn = quote_fn or _default_quote
        self.decimals_fn = decimals_fn or _default_decimals
        self.cooling_fn = cooling_fn or _funded_host_cooling
        self.wake_fn = wake_fn                    # wakes the hit host the moment a row is due
        self.clock, self.sleep = clock, sleep
        self._due, self._lock = set(), threading.Lock()
        self._next = 0
        self._paused_until = 0.0
        self._conn = None
        self.premium = {}                         # fomo_event_id -> last watched premium (%)
        # ponytail: per-row state is never pruned; a few rows a day. Prune on take_due if it grows.
        self.stats = {}                           # fomo_event_id -> {min_premium_pct, quotes, unknown, series}

    def _rows(self):
        if self._conn is None:
            from sniper.persistence.db_core import get_readonly_connection
            self._conn = get_readonly_connection(self.db_path)
        return m.dip_retry_rows(self._conn)

    def take_due(self):
        with self._lock:
            due, self._due = self._due, set()
        return due

    def stats_for(self, fomo_event_id):
        with self._lock:
            got = self.stats.get(fomo_event_id)
            return dict(got, series=list(got["series"])) if got else None

    def _mine(self, row):
        return _network(row) == "solana" and "solana" in chains()

    def _idle(self):
        return self.cooling_fn()

    def _poll_s(self):
        return float(_cfg().MEMECOIN_REAL_DIP_RETRY_POLL_S or 1.0)

    def _price(self, row, dip, now):
        """USD per token for our clip, or None (unknown: never a hit)."""
        quote = self.quote_fn(row["mint"], dip["clip_usd"])
        if quote is None:
            if self.cooling_fn():                 # our own request drew it: step back
                self._paused_until = now + PAUSE_AFTER_COOLING_S
            return None
        decimals = self.decimals_fn(row["mint"])
        try:
            return float(dip["clip_usd"]) / (int(quote["outAmount"]) / (10 ** int(decimals)))
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            return None

    def _observe(self, row, dip, now):
        """(premium % over the gate's reference or None, this tick's series fields)."""
        price = self._price(row, dip, now)
        try:
            return (price / float(dip["reference_price"]) - 1.0) * 100.0, {}
        except (TypeError, ValueError, ZeroDivisionError):
            return None, {}

    @staticmethod
    def _first_stats(dip):
        """A row's stats on first sight. The series opens with tick 0, the refusal's own quote,
        so the watch has a price from the moment of the refusal. Tick 0 is not a watch quote."""
        route = dip.get("dip_route") or {}
        queued = _epoch(dip.get("queued_at"))
        tick0 = {"t": round(queued - float(dip["signal_at"]), 3) if queued is not None else None,
                 "premium_pct": dip.get("refused_premium_pct"), "rung": route.get("rung"),
                 "buy_atomic": route.get("buy_atomic"), "miss": None}
        if getattr(_cfg(), "MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY", False):
            tick0.update(premium_pct=None, basis="intervening_market", state="unknown")
        return {"min_premium_pct": None, "quotes": 0, "unknown": 0, "faults": 0, "series": [tick0]}

    def tick(self):
        now = self.clock()
        if not enabled() or now < self._paused_until or self._idle():
            return
        live = []
        for row in self._rows():
            if not self._mine(row):
                continue
            dip = json.loads(row["dip_json"])
            if now < dip["deadline"]:
                live.append((row, dip))
        if not live:
            return
        # At most MAX_WATCHED rows per watch, the soonest deadlines first: a bound on quote load.
        live.sort(key=lambda item: item[1]["deadline"])
        live = live[:max(1, int(getattr(_cfg(), "MEMECOIN_REAL_DIP_RETRY_MAX_WATCHED", 2) or 2))]
        row, dip = live[self._next % len(live)]
        self._next += 1
        fid = row["fomo_event_id"]
        market_only = getattr(_cfg(), "MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY", False)
        if market_only:
            from sniper.execution.entry_attribution import market_movement_check
            premium, fields = None, market_movement_check(cap_pct=_cfg().copy_premium_cap(
                dip.get("source_user_id"), _network(row))[0])
        else:
            premium, fields = self._observe(row, dip, now)
        hit = False
        with self._lock:
            st = self.stats.get(fid)
            if st is None:
                st = self.stats[fid] = self._first_stats(dip)
            st["quotes"] += not market_only
            if premium is None:
                st["unknown"] += 1
                st["faults"] += fields.get("miss") == "watch_fault"
            elif st["min_premium_pct"] is None or premium < st["min_premium_pct"]:
                st["min_premium_pct"] = premium
            if len(st["series"]) < SERIES_MAX:
                st["series"].append({"t": round(now - float(dip["signal_at"]), 3),
                                     "premium_pct": premium, **fields})
            if market_only or (premium is not None and premium <= dip_watch_cap(
                    dip, str(getattr(self, "chain", "solana")))):
                self._due.add(fid)
                hit = True
        if premium is not None:
            self.premium[fid] = premium
        if hit and self.wake_fn is not None:
            try:
                self.wake_fn()
            except Exception:
                logger.exception("dip watch: wake failed (the host's poll still runs the hit)")

    def run(self):
        while True:
            try:
                self.tick()
            except Exception:
                logger.exception("dip watch: tick failed")
            self.sleep(self._poll_s())


class EvmDipWatch(DipWatch):
    """One EVM chain's watch, on its own thread, so one chain's slow quote never delays another's
    tick. It quotes through the funded chain context, read-only, on the winning rung."""

    def __init__(self, db_path, chain, *, contexts_fn, wake_fn=None, clock=time.time,
                 sleep=time.sleep):
        super().__init__(db_path, quote_fn=lambda *a: None, decimals_fn=lambda *a: None,
                         cooling_fn=lambda: False, wake_fn=wake_fn, clock=clock, sleep=sleep)
        self.chain = str(chain)
        self.name = "%s-%s" % (self.thread_name, self.chain)
        self.contexts_fn = contexts_fn
        self._route = {}                          # fomo_event_id -> the winner adopted from a ladder tick
        self._misses = {}                         # fomo_event_id -> consecutive winner misses
        self._held = {}                           # fomo_event_id -> [the held V4 pool that answered]
        self._held_scan_at = {}                   # fomo_event_id -> when an empty scan may repeat
        self._fault_logged = set()                # fomo_event_ids whose watch fault is logged

    def _mine(self, row):
        return _network(row) == self.chain and self.chain in evm_chains()

    def _idle(self):
        return self.chain not in evm_chains()

    def _poll_s(self):
        return float(_cfg().dip_retry_evm_poll_s_for(self.chain))

    def _held_v4_out(self, ctx, fid, mint, sell, now):
        """Tokens out on the V4 pools of this mint whose key we already hold, or None. Asked
        beside an aggregator and on a ladder tick. The first pool that answers is pinned."""
        from sniper.execution import evm_v4_quote as v4
        pinned = self._held.get(fid)
        if pinned is None and now < self._held_scan_at.get(fid, 0.0):
            return None                           # the last scan priced nothing: not every tick
        try:
            out, pool = v4.pinned_buy_out(self.chain, mint, sell, ctx["rpc_url"],
                                          pool_ids=pinned, http=ctx.get("http"))
        except v4.PinnedQuoteBlind as exc:
            if "rate_limit" in str(exc.failure_type):            # the read pool exits share
                self._paused_until = now + PAUSE_AFTER_COOLING_S
            return None
        except Exception:
            return None                           # it may never cost the tick's own quote
        if out and pool:
            self._held[fid] = [pool]
        elif pinned is None:
            # Up to 8 quoter calls and a ledger open found nothing: a key warmed off the entry
            # path lands in seconds, so the scan waits HELD_RESCAN_S instead of running each tick.
            self._held_scan_at[fid] = now + HELD_RESCAN_S
        return out or None

    def _pinned_out(self, ctx, route, mint, sell):
        """(tokens out or None, typed miss or None) on a display-feed chain: the refused v2 path, v3
        path or mixed hop again, in the SAME basis as tick 0 (the rung's own function and cut), with
        no pair discovery. A missing or malformed pin is a typed miss."""
        rung, pin, http = route.get("rung"), route.get("pin") or {}, ctx.get("http")
        quote_token, mint = str(ctx["quote_token"]).lower(), str(mint).lower()
        try:
            if rung == "v2":
                from sniper.execution import evm_v2_exit_quote as v2
                path = [str(t).lower() for t in pin["path"]]
                if len(path) < 2 or path[0] != quote_token or path[-1] != mint:
                    return None, "pin_mismatch"
                amount_in = int(sell) * (10_000 - int(pin["cut_bps"])) // 10_000
                return v2._quote_path(v2._make_call_fn(self.chain, http), pin["router"],
                                      amount_in, path), None
            from sniper.execution import evm_dex_quote as v3
            if rung in ("v3", "v3_bridge"):
                quote = v3.dex_v3_quote(self.chain, quote_token, mint, int(sell), _PIN_TAKER,
                                        http=http, extra_paths=[(pin["tokens"], pin["fees"])],
                                        paths_only=True)
                return (int(quote["buyAmount"]) if quote else None), None if quote else "pinned_path_no_quote"
            if rung == "mixed_v3_v2":
                from sniper.execution.evm_mixed_hop_router import _v2_direct
                from sniper.execution.evm_v2_exit_quote import dex_v2_exit_quote
                base = str(pin["base_token"]).lower()
                leg1 = v3.dex_v3_quote(self.chain, quote_token, base, int(sell), _PIN_TAKER, http=http,
                                       extra_paths=[(pin["v3_tokens"], pin["v3_fees"])],
                                       paths_only=True)
                bridged = int((leg1 or {}).get("buyAmount") or 0)
                if bridged <= 0:
                    return None, "pinned_path_no_quote"
                tax_bps = int(round(float(getattr(
                    _cfg(), "MEMECOIN_EVM_ROUNDTRIP_TAX_ALLOWANCE_PCT", 4.0)) * 100))
                leg2 = _v2_direct(dex_v2_exit_quote, self.chain, base, mint, bridged, _PIN_TAKER,
                                  http=http, slippage_bps=300, tax_buffer_bps=tax_bps)
                return (int(leg2["buyAmount"]) if leg2 else None), None if leg2 else "pinned_path_no_quote"
        except (KeyError, TypeError, ValueError):
            return None, "pin_malformed"
        return None, "no_pin"

    def _winner_out(self, ctx, route, mint, sell):
        """(tokens out or None, typed miss or None) from the winning rung alone."""
        if self.chain in display_feed_chains() and route.get("rung") in DISCOVERY_RUNGS:
            return self._pinned_out(ctx, route, mint, sell)
        rung = route["rung"]
        if rung == "v4":
            from sniper.execution import evm_v4_quote as v4
            out, _pool = v4.pinned_buy_out(self.chain, mint, sell, ctx["rpc_url"],
                                           pool_ids=[route["pool_id"]], http=ctx.get("http"))
            return out, None if out else "pinned_pool_no_quote"
        if rung == "v4_multihop":
            from sniper.execution.evm_v4_multihop import pinned_path_out
            out = pinned_path_out(self.chain, route["legs_path"], sell, ctx["rpc_url"],
                                  http=ctx.get("http"))
            return out, None if out else "pinned_path_no_quote"
        if rung == "kyber":
            from sniper.execution import evm_kyber_quote as kyber
            quote_token = ctx["quote_token"]      # outside the try: a missing key is OUR fault
            try:
                summary = kyber._routes(quote_token, mint, sell, timeout=4, chain=self.chain)
            except Exception as exc:
                return None, "provider_" + kyber._failure_type(exc)
            return int((summary or {}).get("amountOut") or 0), None if summary else "no_route"
        quote, miss = ctx["venue_quote_with_miss_fn"](ctx["quote_token"], mint, sell, only_rung=rung)
        return (int(quote["buyAmount"]) if quote else None), miss

    def _observe(self, row, dip, now):
        fid = row["fomo_event_id"]
        ctx = (self.contexts_fn() or {}).get(self.chain)
        if not ctx or ctx.get("recovery_only"):
            return None, {"rung": None, "buy_atomic": None, "miss": "no_entry_context"}
        base = dip.get("dip_route") or {}
        try:
            sell, buy0 = int(base["sell_atomic"]), int(base["buy_atomic"])
            refused = float(dip["refused_premium_pct"])
        except (KeyError, TypeError, ValueError):
            return None, {"rung": None, "buy_atomic": None, "miss": "no_refusal_quote"}
        route = self._route.get(fid) or base
        rung = route.get("rung")
        feed_chain = self.chain in display_feed_chains()
        if ((rung == "v4" and not route.get("pool_id"))
                or (rung == "v4_multihop" and not route.get("legs_path"))
                or (feed_chain and rung in DISCOVERY_RUNGS and not route.get("pin"))):
            rung = None                           # nothing pinned: the rung itself is never run
        ladder = not rung or self._misses.get(fid, 0) >= LADDER_AFTER_MISSES
        out, miss, used, fault = None, None, "ladder" if ladder else rung, None
        try:
            if ladder:
                self._misses[fid] = 0
                quote, miss = ctx["venue_quote_with_miss_fn"](
                    ctx["quote_token"], row["mint"], sell,
                    exclude_rungs=WATCH_EXCLUDED_RUNGS + (DISCOVERY_RUNGS if feed_chain else ()))
                if quote:
                    out = int(quote["buyAmount"])
                    self._route[fid] = {"rung": quote.get("_sniper_winner")}
                    used = "ladder:%s" % quote.get("_sniper_winner")
            else:
                out, miss = self._winner_out(ctx, route, row["mint"], sell)
        except WATCH_FAULTS as exc:
            # A FAULT IN THE WATCH ITSELF, not a market answer (2026-09-30): the production
            # context lacked two keys and every such tick read as one more unknown quote. Named on
            # the tick, counted apart, and logged with its traceback ONCE per row.
            out, miss = None, "watch_fault"
            fault = "%s:%s" % (type(exc).__name__, exc)
            if fid not in self._fault_logged:
                self._fault_logged.add(fid)
                logger.exception("dip watch: chain %s row %s cannot be priced, a fault in the "
                                 "watch itself: %s", self.chain, fid, fault)
        except Exception as exc:
            out = None
            # A pinned quoter READ that failed is a TYPED miss. Only a rate limit pauses the chain
            # (below): one failed read of any kind used to pause it for 60 s, longer than the dip
            # window, so every live row expired unwatched (Gamma LOW 2 on c618a17ac).
            miss = ("provider_%s" % getattr(exc, "failure_type", "rpc_transport")
                    if type(exc).__name__ == "PinnedQuoteBlind" else type(exc).__name__)
        out = out if out and out > 0 else None
        if out is None and not ladder:
            self._misses[fid] = self._misses.get(fid, 0) + 1
        if out is None and "rate_limit" in str(miss):
            self._paused_until = now + PAUSE_AFTER_COOLING_S   # exits share this pool and Kyber
        elif ladder or rung in AGGREGATOR_RUNGS:
            held = self._held_v4_out(ctx, fid, row["mint"], sell, now)
            if held and (out is None or held > out):
                out, used = held, "v4_held"
        if out is None or buy0 <= 0:
            fields = {"rung": used, "buy_atomic": None, "miss": miss or "no_quote"}
            return None, dict(fields, fault=fault) if fault else fields
        if not ladder and used == rung:
            self._misses[fid] = 0
        return (((1.0 + refused / 100.0) * buy0 / out - 1.0) * 100.0,
                {"rung": used, "buy_atomic": str(out), "miss": None})


_WATCH = None
_EVM_WATCHES = {}
_WATCH_LOCK = threading.Lock()
# ONE PASS OF `run_hits` AT A TIME, AND A HOST THAT CANNOT DIE SILENTLY. Two concurrent passes
# would end (and log) one row twice, so every host takes HITS_LOCK without blocking. The runner's
# dip hit lane is the host; the main loop runs the hits only while that lane has not passed for
# LANE_STALE_S (it never started, or its thread died). A lane busy inside a long decision looks
# stale too, and then the lock makes the main loop skip.
HITS_LOCK = threading.Lock()
LANE_STALE_S = 15.0               # no pass for this long: the main loop tries to host the hits
LANE_PAGE_S = 120.0               # no pass for this long with rows queued: a hang, page it
STALE_PAGE_EVERY_S = 600.0
_LANE_SEEN = None                 # _monotonic() of the dip hit lane's last pass (or its expected start)
_LANE_EXPECTED = False            # the runner started (or is about to start) the lane thread
_LAST_STALE_PAGE = None
_monotonic = time.monotonic       # this module's own name, so a test never patches the global clock


def expect_lane():
    """The runner calls this right before it starts the lane thread. From here on a lane that
    never passes is as stale as one that stopped."""
    global _LANE_EXPECTED
    _LANE_EXPECTED = True
    lane_seen()


def lane_seen():
    global _LANE_SEEN
    _LANE_SEEN = _monotonic()


def lane_alive():
    return _LANE_SEEN is not None and _monotonic() - _LANE_SEEN < LANE_STALE_S


def lane_status():
    """The runner heartbeat's `dip_lane` field: is a lane expected, how old is its last pass."""
    age = None if _LANE_SEEN is None else max(0.0, _monotonic() - _LANE_SEEN)
    return {"expected": _LANE_EXPECTED,
            "last_pass_age_s": None if age is None else round(age, 1), "alive": lane_alive()}


def page_stale_lane(conn):
    """One page when the lane has not passed for LANE_PAGE_S while dip rows are queued, repeated
    every STALE_PAGE_EVERY_S. Called by the main loop while it covers for the lane. A lane hung
    INSIDE a decision holds HITS_LOCK, so nobody can host: this page is then the only signal.
    Never raises."""
    global _LAST_STALE_PAGE
    try:
        now = _monotonic()
        if not _LANE_EXPECTED or _LANE_SEEN is None or now - _LANE_SEEN < LANE_PAGE_S:
            return False
        if _LAST_STALE_PAGE is not None and now - _LAST_STALE_PAGE < STALE_PAGE_EVERY_S:
            return False
        rows = len(m.dip_retry_rows(conn))
        if not rows:
            return False
        _LAST_STALE_PAGE = now
        from sniper.services import memecoin_alerts
        memecoin_alerts.send_alert(
            "DIP-LANE-STALE",
            "the dip hit lane has not passed for %.0f s (limit %.0f s) with %d dip row(s) queued. "
            "Hits are %s. Restart the runner."
            % (now - _LANE_SEEN, LANE_PAGE_S, rows,
               "blocked: a pass is stuck inside a decision" if HITS_LOCK.locked()
               else "running on the main loop, up to 5 s late"))
        return True
    except Exception:
        logger.exception("dip retry: stale-lane page failed")
        return False


def ensure_watch(db_path, *, contexts=None, wake=None):
    """Start the watch threads for this process (idempotent). The Solana watch idles while the
    knobs are off. One EVM watch per chain in the chain knob, and only with the funded contexts."""
    global _WATCH
    with _WATCH_LOCK:
        if _WATCH is None:
            _WATCH = DipWatch(db_path, wake_fn=wake)
            _WATCH.start()
        if contexts is not None:
            for chain in sorted(evm_chains() - set(_EVM_WATCHES)):
                watch = EvmDipWatch(db_path, chain, contexts_fn=lambda: contexts, wake_fn=wake)
                _EVM_WATCHES[chain] = watch
                watch.start()
        return _WATCH


def run_hits(conn, attempt_fn, now_iso, *, event_from_json, watch=None, onchain_skip_fn=None,
             network_id=None, excluded_networks=()):
    """One pass over dip rows on the caller's (main) thread. Returns a tally. Never raises.

    Every row ends in exactly one log row (Gamma MEDIUM 2): a hit's verdict when it runs, else its
    terminal outcome (expired, knob_off, source_sold, onchain_skip, shadow_hit, error), each with
    the watch's view of the row, so the forward log has a denominator."""
    tally = {"hits": 0, "filled": 0, "dropped": 0}
    try:
        rows = m.dip_retry_rows(conn)
    except Exception:
        logger.exception("dip retry: rows unreadable")
        return tally
    rows = [row for row in rows if _network(row) not in excluded_networks
            and (network_id is None or _network(row) == str(network_id))]
    if not rows:
        return tally
    watches = ([watch] if watch is not None
               else [w for w in (_WATCH, *_EVM_WATCHES.values()) if w is not None])
    watches = [w for w in watches if str(getattr(w, "chain", "solana")) not in excluded_networks
               and (network_id is None or str(getattr(w, "chain", "solana")) == str(network_id))]
    due, premium = set(), {}
    for w in watches:
        due |= w.take_due()
        premium.update(getattr(w, "premium", {}) or {})

    def stats(fid):
        for w in watches:
            got = w.stats_for(fid) if hasattr(w, "stats_for") else (getattr(w, "stats", {}) or {}).get(fid)
            if got:
                return got
        return None

    now = _epoch(now_iso)
    cfg = _cfg()
    market_only = getattr(cfg, "MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY", False)
    if market_only:
        premium = {}
    live_chains, shadow = chains(), shadow_chains()
    for row in rows:
        fid, network = row["fomo_event_id"], _network(row)
        dip = {}

        def log(status, reason=None, res=None):
            # Which cap the row ran under (2026-10-09): cap_pct is the one used, gate_cap_pct the
            # gate's own, cap_source 'dip_knob' when the max-premium knob was the lower.
            try:
                used = dip_watch_cap(dip, network) if dip.get("cap_pct") is not None else None
            except (TypeError, ValueError):
                used = dip.get("cap_pct")
            gate_cap = None if market_only else dip.get("cap_pct")
            m.record_dip_retry_hit(
                conn, fomo_event_id=fid, source_user_id=dip.get("source_user_id"),
                mint=row["mint"], hit_at=now_iso, watch_premium_pct=premium.get(fid),
                cap_pct=used, status=status,
                reason=str(reason or "")[:200] or None,
                position_id=(res or {}).get("position_id"), network_id=network, stats=stats(fid),
                gate_cap_pct=gate_cap,
                cap_source=("intervening_market_unknown" if market_only else None if used is None else
                            "dip_knob" if gate_cap is not None and used < float(gate_cap) else "gate"))

        def end(status, reason=None):
            m.dequeue_entry_retry(conn, fid)
            log(status, reason)
            tally["dropped"] += 1

        try:
            dip = json.loads(row["dip_json"])
            if not enabled() or network not in live_chains:
                end("knob_off")
                continue
            if now is None or now >= dip["deadline"]:
                end("expired")
                continue
            if fid not in due:
                continue
            ev = event_from_json(row["event_json"])
            if m.source_sold_since(conn, dip.get("source_user_id"), row["mint"], network,
                                   dip["signal_at"]):
                logger.warning("dip retry: %s dropped, the source sold since the signal", fid)
                end("source_sold")
                continue
            skip = onchain_skip_fn(conn, ev, now_iso) if onchain_skip_fn is not None else None
            if skip is not None:
                end("onchain_skip", skip)
                continue
            if network in shadow:
                # SHADOW (Gamma MEDIUM 1): the watch says the price came back within the cap; no
                # funded decision runs. The first would-be hit ends the row.
                logger.warning("dip retry: %s shadow hit on %s (watch premium %s)", fid, network,
                               premium.get(fid))
                m.dequeue_entry_retry(conn, fid)
                log("shadow_hit")
                continue
            cap = dip_decision_cap(dip)
            if cap is not None:
                ev.dip_cap = cap               # this decision only; the first attempt never carries it
            res = attempt_fn(conn, ev) or {}
            if res.get("status") == "duplicate_delivery":
                # A funded owner is still deciding this source. Keep the row for another
                # observed price hit; this collision spent no hit and decided no gate.
                continue
            tally["hits"] += 1
            log(res.get("status"), res.get("reason"), res)
            logger.warning("dip retry: %s hit -> %s %s", fid, res.get("status"),
                           res.get("reason") or "")
            if res.get("status") == "filled":
                tally["filled"] += 1
                m.dequeue_entry_retry(conn, fid)
            elif (res.get("status") == "skipped_chase"
                  and int(row["attempts"]) + 1 < int(cfg.MEMECOIN_REAL_DIP_RETRY_MAX_HITS)):
                m.bump_dip_retry(conn, fid)
            else:
                m.dequeue_entry_retry(conn, fid)
        except Exception:
            logger.exception("dip retry: hit failed for %s; dropping the row", fid)
            try:
                end("error")
            except Exception:
                logger.exception("dip retry: could not drop %s", fid)
    return tally
