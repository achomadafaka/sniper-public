"""Pure feature functions for the memecoin real exit brain.

No DB, no network. Reused verbatim by the live materializer and by deterministic replay (the
07-13 "one code path" refusal). Every time-based function takes an explicit series of
`(block_ts, price)` on TRUE event time — never ingest time, never an assumed block-time constant.

Doctrine baked in here:
- Causal only: `confirmed_pivots` reports a swing point only after `confirm` real subsequent bars
  exist in the series it is given (which represents up-to-now). No lookahead.
- Router != buyer: flow is POOL-LEVEL signed net flow with a DUST-only wash filter. The maker
  both-sides exclusion is intentionally gone (`wash_filter="disabled_router_ambiguity"`).
- V3 has no decoded mid: the price path is the per-swap EXECUTION price |quote/base| from the
  already-stored signed amounts (flagged as exec-price in provenance), never the executable exit.

Spec: docs/superpowers/specs/2026-07-23-memecoin-real-exit-brain-design.md.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from sniper.analysis.exit_state_types import Pivot

WASH_FILTER_LABEL = "disabled_router_ambiguity"


# EVM chains whose V2/V3 log layout the decoder is verified against (spec §3.1 rows: 4663, 8453).
# Any other chain string is NOT a proxy for "EVM" — it is fail-closed to forced_legacy so an
# unknown/typo'd chain can never be trusted purely on its pool_kind.
_KNOWN_EVM_CHAINS = frozenset({"4663", "8453"})


@dataclass(frozen=True)
class Flow:
    buy_n: int
    sell_n: int
    buy_vol_quote: str
    sell_vol_quote: str
    net_flow_quote: str
    rejected_dust_n: int
    wash_filter: str = WASH_FILTER_LABEL


@dataclass(frozen=True)
class Capability:
    flow_trusted: bool
    vol_trusted: bool
    structure_trusted: bool
    depth_trusted: bool
    forced_legacy: bool
    required: frozenset = frozenset()   # provenance families that MUST be trusted to act (spec §3.1)


def profile_capability(chain_id, pool_kind, *, v3_depth_verified=None):
    """What a (chain, pool-kind) profile may honestly support (spec §3.1). Fail-closed on anything
    unrecognized: unknown chain OR unknown pool_kind -> forced_legacy with nothing trusted.

    `required` names the provenance families that must be provenance-`trusted` before the state is
    candidate-capable. V2 requires depth (Sync reserves); Solana/unknown require nothing because they
    are forced to legacy.

    WO-10: V3 depth is available ONLY on a chain whose V3 decoder has been live-verified
    (`v3_depth_verified`, default = MEMECOIN_V3_DEPTH_VERIFIED_CHAINS). On a verified chain V3 gains
    depth (supported + required); everywhere else V3 stays flow/vol/structure-only exactly as before,
    so a v3_burn can never CUT until its decoder is proven against real logs.
    """
    if v3_depth_verified is None:
        try:
            from sniper.settings.memecoin import MEMECOIN_V3_DEPTH_VERIFIED_CHAINS as v3_depth_verified
        except Exception:
            v3_depth_verified = frozenset()
    chain = str(chain_id or "").lower()
    if chain in ("solana", "sol"):
        # v2 (prereg 2026-07-24): the vault-keyed pull tape carries amounts, reserves AND slot
        # times, so a pool with a decoded sol tape supports every family — depth included, since
        # sol_sync carries post-balance reserves and sol_burn is a live-verified LP-pull signal.
        # Any other/unknown kind on Solana stays forced legacy (no tape -> nothing to trust).
        if str(pool_kind or "").lower() == "sol":
            return Capability(True, True, True, True, forced_legacy=False,
                              required=frozenset({"price", "flow", "vol", "depth"}))
        return Capability(False, False, False, False, forced_legacy=True)
    if chain == "" or chain not in _KNOWN_EVM_CHAINS:
        # unknown chain identity -> never trust a decode we have not verified for it
        return Capability(False, False, False, False, forced_legacy=True)
    kind = str(pool_kind or "").lower()
    if kind in ("v2", "v2_swap"):
        return Capability(True, True, True, True, forced_legacy=False,
                          required=frozenset({"price", "flow", "vol", "depth"}))
    if kind in ("v3", "v3_swap"):
        # V3 flow/vol/structure from signed amounts. Depth ONLY on a verified chain (WO-10): the
        # in-range-liquidity series comes from v3_swap and the LP-pull signal from v3_burn.
        if chain in v3_depth_verified:
            return Capability(True, True, True, True, forced_legacy=False,
                              required=frozenset({"price", "flow", "vol", "depth"}))
        return Capability(True, True, True, False, forced_legacy=False,
                          required=frozenset({"price", "flow", "vol"}))
    return Capability(False, False, False, False, forced_legacy=True)


def _q(swap, quote_side):
    try:
        return int(swap[f"amount{quote_side}"])
    except (KeyError, TypeError, ValueError):
        return None


def in_window(swaps, *, window_s, window_end_ts):
    """Events inside the closed window `(end - window_s, end]`. Returns all events when the window
    is unspecified. P1 (Codex 2026-07-24): flow and depth previously scanned the ENTIRE buffer while
    arrival used a 60s window, so one old swap could keep flow positive and one old burn could keep
    depth_pull true indefinitely. Every windowed feature must share one definition of 'now'."""
    if window_s is None or window_end_ts is None or window_s <= 0:
        return list(swaps)
    lo = window_end_ts - window_s
    out = []
    for s in swaps:
        t = s.get("block_ts")
        if t is not None and lo < t <= window_end_ts:
            out.append(s)
    return out


def pool_flow(swaps, *, quote_side, dust_atomic=0):
    """Pool-level signed net flow in QUOTE units. Positive quote = INTO the pool = a buy of the coin.

    NO actor/maker wash filter (maker is the router, not the buyer — filtering same-maker both-sides
    would delete valid flow). Only |quote| <= dust_atomic is excluded; rejected_dust_n reports it.

    Counts/volumes only — NO arrival rate here. Arrival rate is a *fixed-window* quantity (swaps per
    second over a pre-registered window), not swaps over the elastic first-to-last span, so it is a
    separate function (`arrival_rate_window`) the caller drives with explicit closed-window bounds.
    """
    clean, dust = [], 0
    for s in swaps:
        q = _q(s, quote_side)
        if q is None:
            continue
        if abs(q) <= dust_atomic:
            dust += 1
            continue
        clean.append(q)
    buys = [q for q in clean if q > 0]
    sells = [q for q in clean if q < 0]
    return Flow(
        buy_n=len(buys), sell_n=len(sells),
        buy_vol_quote=str(sum(buys)), sell_vol_quote=str(sum(sells)),
        net_flow_quote=str(sum(clean)), rejected_dust_n=dust)


def arrival_rate_window(swaps, *, quote_side, window_s, window_end_ts, dust_atomic=0):
    """Clean-swap arrival rate over the FIXED closed window `(window_end_ts - window_s, window_end_ts]`,
    in swaps per second of event time.

    This is the spec's fixed-window rate, not `count / (last - first)`: two swaps in the final second
    of a 300s window read as 2/300 per second, never 2/1. Returns None on a non-positive window or a
    missing window end. Swaps without a `block_ts`, dust, or unreadable quote are excluded.
    """
    if window_end_ts is None or window_s is None or window_s <= 0:
        return None
    lo = window_end_ts - window_s
    n = 0
    for s in swaps:
        q = _q(s, quote_side)
        if q is None or abs(q) <= dust_atomic:
            continue
        t = s.get("block_ts")
        if t is None or t <= lo or t > window_end_ts:
            continue
        n += 1
    return n / window_s


def arrival_acceleration(rate_now, rate_prev):
    """Change in arrival rate between two adjacent windows. None if either window lacks a rate."""
    if rate_now is None or rate_prev is None:
        return None
    a = rate_now - rate_prev
    return a if math.isfinite(a) else None


def exec_price_path_per_block(swaps, *, quote_side):
    """Size-weighted execution price per BLOCK (spec §3.3), the structure/vol input.

    Per-swap `|quote|/|base|` prints carry individual impact and, when several land in one block,
    equal event timestamps that make Δt-based features degenerate. So prints are aggregated to ONE
    price per block: the base-weighted VWAP `sum(|quote|) / sum(|base|)`. Returns
    `[(block_ts, price, n_prints)]` sorted by block_ts. Swaps with zero base/quote or no block_ts
    are skipped.

    Price is the atomic quote/base ratio — its scale is irrelevant to the only consumers (pivot
    comparisons and log-return vol are scale-invariant), and the executable-route price is a separate
    lane (§3.4). Decimal→human normalization belongs with the Phase-5 display surface that needs it.
    """
    base_side = 1 - int(quote_side)
    # Key on BLOCK IDENTITY (block_no), never on block_ts. block_times stores second-resolution
    # timestamps and 4663 produces ~10 blocks/second (measured 2026-07-24: 970,581 blocks over 27h),
    # so keying on the timestamp silently merged distinct blocks into one bucket — locally 933,586
    # blocks collapsed to 14,555 timestamps. That made "per-block VWAP" a per-second bucket and
    # corrupted warmup counts, pivot spacing, vol and next-block replay fills. Fall back to block_ts
    # only when no block_no exists (callers that key both identically).
    by_block = {}   # key -> [sum|quote|, sum|base|, n_prints, block_ts]
    for s in swaps:
        q = _q(s, quote_side)
        b = _q(s, base_side)
        if not q or not b:
            continue
        ts = s.get("block_ts")
        if ts is None:
            continue
        key = s.get("block_no")
        if key is None:
            key = ts
        agg = by_block.setdefault(key, [0, 0, 0, ts])
        agg[0] += abs(q)
        agg[1] += abs(b)
        agg[2] += 1
        if ts < agg[3]:
            agg[3] = ts
    out = []
    for key, (sq, sb, n, ts) in by_block.items():
        if sb <= 0:
            continue
        price = sq / sb
        if math.isfinite(price) and price > 0:
            out.append((ts, price, n, key))
    # Sort by (block_ts, block_identity). Timestamps are second-resolution and 4663 makes ~10
    # blocks/second, so ties are the COMMON case, not an edge case; sorting on ts alone left tied
    # blocks in whatever order the caller supplied — and the production reader hands rows back
    # NEWEST-first, so ties came out reverse-chronological and every downstream consumer (pivots,
    # log-return vol, replay fills) saw the series run backwards inside each second.
    out.sort(key=lambda x: (x[0], x[3]))
    return out          # (block_ts, price, n_prints, block_key)


def realized_vol(series, *, window_s):
    """Realized volatility = zero-mean RMS of consecutive log returns in the last `window_s` of
    event time: `sqrt(sum(r_i^2) / n)`.

    `series` = [(t_seconds, price)]. Points older than `window_s` before the last point are excluded.
    Returns None when fewer than two in-window points (no return to measure). A flat in-window path
    returns 0.0 (known zero, not unknown).

    FROZEN (pre-registration 2026-07-23). This is the standard realized-volatility estimator and it
    is deliberately NOT the sample std-dev, which was discontinuous: std-dev measures dispersion about
    the sample MEAN, so one +10% return read 0.0953 while two identical +10% returns read ~0 — pure
    drift cancelled to "no volatility". RMS never subtracts a mean, so the estimate is continuous in n
    and n=1 reduces exactly to |r| (the old single-return branch), collapsing both branches into one.
    """
    if len(series) < 2:
        return None
    last_t = series[-1][0]
    pts = [(t, p) for (t, p) in series if t >= last_t - window_s]
    if len(pts) < 2:
        return None
    rets = []
    for (t0, p0), (t1, p1) in zip(pts, pts[1:]):
        if p0 and p0 > 0 and p1 and p1 > 0:
            rets.append(math.log(p1 / p0))
    if not rets:
        return None
    v = math.sqrt(sum(r * r for r in rets) / len(rets))
    return v if math.isfinite(v) else None


def confirmed_pivots(series, *, confirm):
    """Causal swing-high/low detector. `series` = [(block_ts, price)] in event-time order.

    Index i is a confirmed LOW iff it turns down from its predecessor (price[i] < price[i-1]) and
    the next `confirm` bars are all strictly higher (a real trough); symmetric for HIGH. A pivot is
    reported ONLY when `confirm` real subsequent bars already exist in `series` — so growing the
    series can add pivots but never retroactively invents one from future data.
    """
    n = len(series)
    if n < 2 or confirm < 1:
        return []
    out = []
    for i in range(1, n):
        if i + confirm > n - 1:
            break                                   # not enough forward bars yet -> causal stop
        px = series[i][1]
        prev = series[i - 1][1]
        fwd = [series[i + k][1] for k in range(1, confirm + 1)]
        if px < prev and all(f > px for f in fwd):
            out.append(Pivot(kind="low", block_ts=series[i][0], price=px))
        elif px > prev and all(f < px for f in fwd):
            out.append(Pivot(kind="high", block_ts=series[i][0], price=px))
    return out


def liquidity_removed_fraction(quote_removed, post_quote_reserve):
    """WO-3: fraction of the quote reserve pulled by an LP removal — |removed| / (|removed| + post),
    since pre = post + removed. `quote_removed` is the burn's quote-leg delta (sign-agnostic here) and
    `post_quote_reserve` is the same-tx sync's post balance. Returns None when either input is
    missing/invalid or the pool is empty — an unmeasurable removal is treated as a rug by the caller."""
    try:
        removed = abs(int(quote_removed))
        post = int(post_quote_reserve)
    except (TypeError, ValueError):
        return None
    denom = removed + post
    if denom <= 0:
        return None
    return removed / denom


def largest_lp_removal_fraction(events, *, quote_side):
    """Largest decoded LP-removal fraction, pairing each burn with its same-transaction sync.

    Returns None when any burn is provisional, lacks a same-transaction post reserve, or cannot be
    measured. Callers treat unmeasurable removal as catastrophic; silently borrowing a later sync as
    the denominator would understate or inflate the rug.
    """
    rows = list(events or ())
    burns = [r for r in rows if r.get("kind") in ("v2_burn", "sol_burn")]
    if not burns:
        return None
    syncs = {}
    for row in rows:
        if row.get("kind") not in ("v2_sync", "sol_sync"):
            continue
        identity = (row.get("block_no"), row.get("tx_sig"))
        if identity[0] is None or not identity[1]:
            continue
        syncs[identity] = row
    fractions = []
    for burn in burns:
        identity = (burn.get("block_no"), burn.get("tx_sig"))
        if identity[0] is None or not identity[1]:
            return None
        sync = syncs.get(identity)
        if sync is None:
            return None
        fraction = liquidity_removed_fraction(
            burn.get(f"amount{int(quote_side)}"), sync.get(f"amount{int(quote_side)}"))
        if fraction is None:
            return None
        fractions.append(fraction)
    return max(fractions) if fractions else None


def v3_largest_lp_removal_fraction(events):
    """Largest in-range V3 burn as a fraction of pre-burn active liquidity.

      - Only a `v3_burn` counts; a `v3_collect` is a fee claim and can NEVER contribute.
      - Events are replayed chronologically. Swap snapshots reset active in-range liquidity; an
        in-range Mint/Burn after that snapshot updates it causally.
      - An out-of-range burn contributes zero because it does not change current executable depth.
      - A burn before a usable swap snapshot, malformed ranges/liquidity, or removal larger than
        known active liquidity is unmeasurable and returns None (fail closed at the caller).
    """
    indexed = list(enumerate(events or ()))

    def _order(item):
        idx, row = item
        ts = row.get("block_ts")
        block = row.get("block_no")
        log_index = row.get("log_index")
        try:
            ts_order = float(ts) if ts is not None else float("inf")
        except (TypeError, ValueError):
            ts_order = float("inf")
        try:
            block_order = int(block) if block is not None else idx
        except (TypeError, ValueError):
            block_order = idx
        try:
            log_order = int(log_index) if log_index is not None else idx
        except (TypeError, ValueError):
            log_order = idx
        return (ts_order, block_order, log_order, idx)

    rows = [row for _idx, row in sorted(indexed, key=_order)]
    current_tick = current_liq = None
    saw_burn = False
    fractions = []
    for row in rows:
        kind = row.get("kind")
        if kind == "v3_swap":
            try:
                current_tick = int(row["tick"])
                current_liq = int(row["liquidity"])
            except (KeyError, TypeError, ValueError):
                current_tick = current_liq = None
            if current_liq is not None and current_liq <= 0:
                current_tick = current_liq = None
            continue
        if kind not in ("v3_mint", "v3_burn"):
            continue
        if kind == "v3_burn":
            saw_burn = True
        if current_tick is None or current_liq is None:
            if kind == "v3_burn":
                return None
            continue
        lo, hi, liq = row.get("tick_lower"), row.get("tick_upper"), row.get("liquidity")
        if lo is None or hi is None or liq is None:
            if kind == "v3_burn":
                return None
            continue
        try:
            lo, hi, amount = int(lo), int(hi), int(liq)
        except (TypeError, ValueError):
            if kind == "v3_burn":
                return None
            continue
        if lo >= hi or amount < 0:
            if kind == "v3_burn":
                return None
            continue
        if not (lo <= current_tick < hi):
            continue
        if kind == "v3_mint":
            current_liq += amount
            continue
        if current_liq <= 0 or amount > current_liq:
            return None
        fractions.append(amount / current_liq)
        current_liq -= amount
    if not saw_burn:
        return None
    return max(fractions) if fractions else 0.0
