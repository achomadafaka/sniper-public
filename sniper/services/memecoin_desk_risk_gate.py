"""Pure evidence predicates for model proposals. Source coverage is supplied by the caller, never inferred here.

Prices share one unit; n5 is a log range; flows are SOL excluding verified own fills.
The live validator retains mechanical rail checks. This module grants no execution authority.
"""
from __future__ import annotations

import math

DEFAULTS = {
    "flow_sell_multiple": 1.5, "min_sellers": 3,
    "failed_high_drop_n5": 1.0, "run_horizon_s": 300.0,
    "collapse_drop_n5": 3.0, "collapse_sell_share": 0.6,
    "dying_activity": 0.25, "lock_gain_n5": 1.0,
    "weak_sell_share": 0.55, "target_near_n5": 1.0, "buy_fade_ratio": 0.5,
    "material_move_n5": 1.0, "rail_near_n5": 1.0,
}
PREFIX = "MEMECOIN_DESK_RISK_GATE_"
TIGHTEN_RAIL = {6: "trail_pct", 7: "sl_pct", 8: "tp_pct", 9: "trail_pct"}


def _number(value, *, positive=False):
    return (isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
            and (value > 0 if positive else value >= 0))


def _params(params):
    if params is not None and (not isinstance(params, dict) or set(params) - DEFAULTS.keys()):
        raise ValueError("unknown RISK gate parameter")
    out = {**DEFAULTS, **(params or {})}
    for key, value in out.items():
        if not _number(value, positive=True):
            raise ValueError("invalid RISK gate parameter: " + key)
        if key in ("collapse_sell_share", "weak_sell_share", "dying_activity", "buy_fade_ratio") and value > 1:
            raise ValueError("RISK gate fraction exceeds one: " + key)
    if out["min_sellers"] != int(out["min_sellers"]):
        raise ValueError("RISK gate min_sellers must be an integer")
    return out


def settings(environ):
    """Resolve and validate numeric knobs after the caller loads its state environment."""
    values = {}
    for key in DEFAULTS:
        name = PREFIX + key.upper()
        if name in environ:
            try:
                values[key] = float(environ[name])
            except (TypeError, ValueError):
                raise ValueError("invalid RISK gate parameter: " + name) from None
    return _params(values)


def _share(ev):
    buy, sell = ev.get("buy_sol_30s"), ev.get("sell_sol_30s")
    if not _number(buy) or not _number(sell) or max(buy, sell) <= 0:
        return None
    if math.isfinite(buy + sell):
        return sell / (buy + sell)
    scale = max(buy, sell)
    return (sell / scale) / (buy / scale + sell / scale)


def _log_ratio(value, base):
    ratio = value / base
    return math.log(ratio) if ratio > 0 and math.isfinite(ratio) else math.log(value) - math.log(base)


def _failed_high(ev, p):
    price, high, noise = ev.get("last_price"), ev.get("high_120s_post_fill"), ev.get("n5")
    return (ev.get("new_high_60s") is False and all(_number(x, positive=True) for x in (price, high, noise))
            and price <= high * math.exp(-p["failed_high_drop_n5"] * noise))


def backing(block, action, proposed_rails, code, params=None):
    """(backed, missing-or-failed predicates) for a mechanically validated proposal.

    ev.fresh must mean independently proven current coverage, including venue/gaps. A
    heartbeat or old swap alone cannot establish it. Window facts are None until complete.
    """
    p = _params(params)
    if not isinstance(block, dict) or type(code) is not int:
        return False, ["reason_code"]
    if action == "TIGHTEN" and code in TIGHTEN_RAIL:
        if not isinstance(proposed_rails, dict) or set(proposed_rails) != {TIGHTEN_RAIL[code]}:
            return False, ["reason_rail"]
        if code == 9:
            rails = block.get("rails")
            if not isinstance(rails, dict) or "trail_pct" not in rails:
                return False, ["current_trail"]
            trail = rails["trail_pct"]
            ok = trail is None or (_number(trail) and trail == 0)
            return (True, []) if ok else (False, ["current_trail_off"])
    elif action != "EXIT" or code not in range(10, 16):
        return False, ["action_reason"]
    ev = block.get("ev")
    if not isinstance(ev, dict) or ev.get("fresh") is not True:
        return False, ["fresh_coverage"]
    if action == "EXIT":
        if code == 10:
            ok = type(block.get("sb")) is int and block["sb"] == 1
            why = "confirmed_structure_break"
        elif code == 11:
            buy, sell, sellers = (ev.get(k) for k in ("buy_sol_30s", "sell_sol_30s", "distinct_sellers_30s"))
            ok = (all(_number(x) for x in (buy, sell, sellers)) and sellers == int(sellers)
                  and sell > 0 and sell >= p["flow_sell_multiple"] * buy and sellers >= p["min_sellers"])
            why = "sell_flow_and_distinct_sellers"
        elif code == 12:
            ok, why = _failed_high(ev, p), "failed_high_post_fill_window"
        elif code == 13:
            held, horizon = ev.get("held_s"), ev.get("plan_horizon_s", p["run_horizon_s"])
            ok = (_number(held) and _number(horizon, positive=True) and held >= horizon
                  and ev.get("new_high_120s") is False)
            why = "horizon_and_no_new_high"
        elif code == 14:
            price, high, noise = (ev.get(k) for k in ("last_price", "high_30s_post_fill", "n5"))
            share = _share(ev)
            ok = (all(_number(x, positive=True) for x in (price, high, noise)) and share is not None
                  and price <= high * math.exp(-p["collapse_drop_n5"] * noise) and share >= p["collapse_sell_share"])
            why = "collapse_post_fill_window_and_sell_share"
        else:
            count, prior = ev.get("swaps_60s"), ev.get("swaps_median_60s_prior_300s")
            ok = (_number(count) and count == int(count) and _number(prior, positive=True)
                  and count <= p["dying_activity"] * prior)
            why = "activity_and_positive_prior_baseline"
    elif code == 6:
        price, entry, noise = (ev.get(k) for k in ("last_price", "entry_price", "n5"))
        ok = (all(_number(x, positive=True) for x in (price, entry, noise))
              and _log_ratio(price, entry) >= p["lock_gain_n5"] * noise)
        why = "gain_from_entry"
    elif code == 7:
        share = _share(ev)
        ok = (share is not None and share >= p["weak_sell_share"]) or _failed_high(ev, p)
        why = "sell_share_or_failed_high"
    else:
        price, entry, noise = (ev.get(k) for k in ("last_price", "entry_price", "n5"))
        tp = (block.get("rails") or {}).get("tp_pct")
        near = False
        if all(_number(x, positive=True) for x in (price, entry, noise, tp)):
            target = entry * (1 + tp / 100)
            near = (math.isfinite(target) and price <= target
                    and price >= target * math.exp(-p["target_near_n5"] * noise))
        buy, prior = ev.get("buy_sol_30s"), ev.get("buy_sol_prev_30s")
        fade = (_number(buy) and _number(prior, positive=True) and buy < p["buy_fade_ratio"] * prior)
        ok, why = near or fade, "near_target_or_fading_buys"
    return (True, []) if ok else (False, [why])


def material(block, previous, params=None):
    """Compare fresh evidence with the last call/reference, without changing either snapshot."""
    p = _params(params)
    if previous is None:
        return False, "baseline"
    for key, value in (block.get("rail_proximity") or {}).items():
        old = (previous.get("rail_proximity") or {}).get(key)
        if type(value) is bool and type(old) is bool and value != old:
            return True, "rail_proximity"
    ev, before = block.get("ev") or {}, previous.get("ev") or {}
    noise = ev.get("n5")
    if ev.get("fresh") is not True or not _number(noise, positive=True):
        return False, "unknown"
    margin = p["material_move_n5"] * noise
    price, old = ev.get("last_price"), before.get("last_price")
    if not all(_number(x, positive=True) for x in (price, old)):
        return False, "unknown"
    if abs(_log_ratio(price, old)) >= margin:
        return True, "move"
    for key, direction in (("high_since_fill", 1), ("low_since_fill", -1)):
        value, old = ev.get(key), before.get(key)
        if (all(_number(x, positive=True) for x in (value, old))
                and direction * _log_ratio(value, old) >= margin):
            return True, "move"
    return False, "unchanged"
