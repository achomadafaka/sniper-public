"""Pure exit-observer-v1 flags; no actions, quotes, persistence or outcome reads.

Value is native SOL from selling the current residual atoms into each raw post-swap
state. Ratios equal unit-cost-normalized V; fees round up independently. Reserve
states already include our buy. Noise uses own-excluded marginal reserve prices.
Amendment 3 freezes ONE_SWAP_DROP to sell destinations; buy fee-tier falls remain
visible to STRUCT_BREAK, never a single-sell flag.
"""
from __future__ import annotations

import math
import statistics
from bisect import bisect_left
from collections import defaultdict

VERSION = "exit-observer-v1"
GRID = (2, 3, 4, 6)
INDICATORS = ("SELL_BURST", "EXODUS", "STRUCT_BREAK", "ONE_SWAP_DROP",
              "TOP_DUMP_TAPE_PROXY", "CREATOR_SELL_TAPE_PROXY")
FEES = ("fee_bps", "creator_fee_bps", "cashback_fee_bps")


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _atoms(value):
    if type(value) is int:
        return value if 0 <= value < 2**64 else None
    if (isinstance(value, str) and len(value) <= 20 and value.isascii()
            and value.isdecimal() and str(int(value)) == value):
        return _atoms(int(value))
    return None


def _order(row):
    key = tuple(row.get(k) for k in ("slot", "tx_index", "ordinal"))
    return key if all(type(x) is int and x >= 0 for x in key) else None


def _value(row, qty):
    if qty is None:
        return None
    q, b, real = (_atoms(row.get(k)) for k in ("virtual_quote", "virtual_token", "real_quote"))
    rates = [row.get(k) for k in FEES]
    if (q is None or b is None or real is None or q == 0 or b == 0
            or any(type(x) is not int or not 0 <= x < 10_000 for x in rates)):
        return None
    gross = q * qty // (b + qty)
    net = gross - sum((gross * rate + 9_999) // 10_000 for rate in rates)
    return net / 1e9 if 0 < net and gross <= real else None


def _price(row):
    q, b = (_atoms(row.get(k)) for k in ("virtual_quote", "virtual_token"))
    return q / b if q and b else None


def _direction(row):
    value = row.get("is_buy")
    return bool(value) if type(value) in (int, bool) and value in (0, 1) else None


def _states(rows):
    """A delayed older swap contributes flow, never a newly observed reserve state."""
    states, latest = [], None
    for row in rows:
        key = _order(row)
        if latest is None or key > latest:
            states.append(row)
            latest = key
    return states


def evaluate(rows, *, cut, coverage_from, opened_at, residual_atomic,
             own_signatures, own_complete, launch_at=None):
    """Evaluate at a closed host-receipt cut; all windows are [lo, hi).

    coverage_from is the adapter's continuous, loss-free source proof. A known
    launch additionally requires its first swap to be present in rows. Every
    unknown remains in the denominator, with a per-indicator reason.
    """
    qty = _atoms(residual_atomic)
    if not qty or qty >= 2**64:
        qty = None
    out = {"rule_version": VERSION, "population": None, "flags": {},
           "unknown": {}, "noise_5": None, "value_sol": None, "peak_value_sol": None,
           "peak_covered_from_fill": False, "own_sig_excluded_count": 0,
           "residual_atomic": str(qty) if qty else None}

    def record(name, values=None, reason=None):
        grid = (0,) if name == "CREATOR_SELL_TAPE_PROXY" else GRID
        out["flags"][name] = {str(k): values(k) if values else None for k in grid}
        out["unknown"][name] = {str(k): reason for k in grid if out["flags"][name][str(k)] is None}

    def unknown_all(reason):
        for name in INDICATORS:
            record(name, reason=reason)
        return out

    if not (_number(cut) and _number(coverage_from) and 0 <= coverage_from <= cut):
        return unknown_all("capture_unproven")
    if not isinstance(rows, (list, tuple)) or any(
            not isinstance(r, dict) or not _number(r.get("received_at")) for r in rows):
        return unknown_all("receipt_time_unknown")
    raw = sorted((r for r in rows if coverage_from <= r["received_at"] < cut),
                 key=lambda r: r["received_at"])
    if not raw:
        return unknown_all("no_state")
    orders = [_order(r) for r in raw]
    order_ok = None not in orders and len(set(orders)) == len(orders)
    ordered = sorted(raw, key=_order) if order_ok else raw
    states = _states(raw) if order_ok else raw
    out["value_sol"] = _value(ordered[-1], qty) if order_ok else None
    if _number(opened_at) and opened_at <= cut:
        anchor = [r for r in states if r["received_at"] <= opened_at]
        since = [r for r in states if r["received_at"] > opened_at]
        peak_rows = (anchor[-1:] if anchor else []) + since
        vals = [_value(r, qty) for r in peak_rows]
        known = [v for v in vals if v is not None]
        out["peak_value_sol"] = max(known) if known else None
        out["peak_covered_from_fill"] = bool(coverage_from <= opened_at and anchor
                                                and vals and None not in vals and order_ok)
    own_ok = (own_complete is True and isinstance(own_signatures, (list, tuple, set, frozenset))
              and bool(own_signatures) and all(isinstance(s, str) and s for s in own_signatures))
    if not own_ok or any(not isinstance(r.get("signature"), str) or not r["signature"] for r in raw):
        return unknown_all("own_signatures_unproven")
    out["population"] = "own_excluded"
    own = set(own_signatures)
    external = [r for r in raw if r["signature"] not in own]
    out["own_sig_excluded_count"] = len(raw) - len(external)

    def window(lo, hi):
        return [r for r in external if lo <= r["received_at"] < hi]

    def flow(lo, hi, buy):
        found = window(lo, hi)
        if coverage_from > lo or any(_direction(r) is None or _atoms(r.get("quote_atomic")) is None for r in found):
            return None
        return sum(_atoms(r["quote_atomic"]) for r in found if _direction(r) == buy) / 1e9

    def buyers(lo, hi):
        found = window(lo, hi)
        if coverage_from > lo or any(_direction(r) is None or not isinstance(r.get("actor"), str)
                                     or not r["actor"] for r in found):
            return None
        return len({r["actor"] for r in found if _direction(r) is True})

    # Quiet closes carry only a preceding, covered, own-excluded state.
    price_states = _states(external) if order_ok else []
    price_times = [r["received_at"] for r in price_states]
    closes = []
    for i in range(61):
        ts = cut - 300 + 5 * i
        j = bisect_left(price_times, ts) - 1
        if j >= 0 and coverage_from <= ts:
            closes.append(_price(price_states[j]))
        else:
            closes.append(None)
    returns = [abs(math.log(b / a)) for a, b in zip(closes, closes[1:]) if a and b]
    noise = statistics.median(returns) if len(returns) >= 12 else None
    out["noise_5"] = noise
    noise_reason = "noise_unknown" if noise is None else "noise_zero" if noise == 0 else None

    sell10 = flow(cut - 10, cut, False)
    prior10 = [flow(cut - 10 * (i + 1), cut - 10 * i, False) for i in range(1, 30)]
    nonempty10 = [x for x in prior10 if x is not None and x > 0]
    if sell10 is None or None in prior10:
        record("SELL_BURST", reason="flow_unknown")
    elif len(nonempty10) < 6:
        record("SELL_BURST", reason="fewer_than_6_sell_windows")
    else:
        med = statistics.median(nonempty10)
        record("SELL_BURST", lambda k: sell10 >= k * med)

    buy30, sell30, buyers30 = flow(cut - 30, cut, True), flow(cut - 30, cut, False), buyers(cut - 30, cut)
    prior_buyers = [buyers(cut - 30 * (i + 1), cut - 30 * i) for i in range(1, 10)]
    nonempty_buyers = [x for x in prior_buyers if x is not None and x > 0]
    if None in (buy30, sell30, buyers30) or None in prior_buyers:
        record("EXODUS", reason="flow_or_actor_unknown")
    elif len(nonempty_buyers) < 4:
        record("EXODUS", reason="fewer_than_4_buyer_windows")
    else:
        med = statistics.median(nonempty_buyers)
        record("EXODUS", lambda k: buyers30 <= med / k and sell30 > buy30)

    low_anchor = [r for r in states if r["received_at"] < cut - 300]
    low_rows = low_anchor[-1:] + [r for r in states if cut - 300 <= r["received_at"] < cut - 5]
    low_values = [_value(r, qty) for r in low_rows]
    sell15, buy15 = flow(cut - 15, cut, False), flow(cut - 15, cut, True)
    current = out["value_sol"]
    if noise_reason:
        record("STRUCT_BREAK", reason=noise_reason)
    elif coverage_from > cut - 300 or not low_values or None in low_values or current is None:
        record("STRUCT_BREAK", reason="value_history_unknown")
    elif None in (sell15, buy15):
        record("STRUCT_BREAK", reason="flow_unknown")
    else:
        low = min(low_values)
        depth = math.log(low / current)
        record("STRUCT_BREAK", lambda k: current < low and depth >= k * noise and sell15 > buy15)

    # Amendment 3: only sell destinations; predecessor state still includes ours.
    worst, pair_unknown = 0.0, False
    for i, r in enumerate(ordered):
        if r["received_at"] < cut - 10 or r["signature"] in own:
            continue
        direction = _direction(r)
        if direction is None:
            pair_unknown = True
        elif direction is False:
            before = _value(ordered[i - 1], qty) if i else None
            after = _value(r, qty)
            if before is None or after is None:
                pair_unknown = True
            else:
                worst = max(worst, math.log(before / after))
    if noise_reason:
        record("ONE_SWAP_DROP", reason=noise_reason)
    elif qty is None:
        record("ONE_SWAP_DROP", reason="quantity_unknown")
    elif not order_ok or coverage_from > cut - 10:
        record("ONE_SWAP_DROP", reason="chain_order_or_coverage_unknown")
    elif pair_unknown:
        record("ONE_SWAP_DROP", reason="swap_value_unknown")
    else:
        record("ONE_SWAP_DROP", lambda k: worst >= k * noise)

    launch_ok = (_number(launch_at) and coverage_from <= launch_at <= cut - 30
                 and raw[0]["received_at"] == launch_at and order_ok)
    if not launch_ok:
        record("TOP_DUMP_TAPE_PROXY", reason="launch_or_inventory_unproven")
        record("CREATOR_SELL_TAPE_PROXY", reason="launch_unproven")
        return out
    identity_bad = any(_direction(r) is None or not isinstance(r.get("actor"), str) or not r["actor"] for r in external)
    if identity_bad or any(_atoms(r.get("token_atomic")) is None for r in external):
        record("TOP_DUMP_TAPE_PROXY", reason="inventory_unknown")
    else:
        positions = defaultdict(int)
        for r in external:
            if r["received_at"] < cut - 30:
                positions[r["actor"]] += _atoms(r["token_atomic"]) * (1 if _direction(r) else -1)
        if not positions:
            record("TOP_DUMP_TAPE_PROXY", reason="no_pre_window_inventory")
        else:
            top = max(positions, key=positions.get)
            sold = sum(_atoms(r["token_atomic"]) for r in window(cut - 30, cut)
                       if _direction(r) is False and r["actor"] == top)
            record("TOP_DUMP_TAPE_PROXY", lambda k: positions[top] > 0 and sold * k >= positions[top])
    creator = ordered[0].get("actor")
    if identity_bad or not isinstance(creator, str) or not creator:
        record("CREATOR_SELL_TAPE_PROXY", reason="creator_identity_unknown")
    else:
        record("CREATOR_SELL_TAPE_PROXY", lambda k: any(_direction(r) is False and r["actor"] == creator
                                                        for r in window(cut - 30, cut)))
    return out
