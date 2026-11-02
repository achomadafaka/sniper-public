"""Compute the per-trader raw statistics envelope from reconstruct() output.

Pure / network-free. No score, no verdict — descriptive statistics only.
Every profitability figure is emitted next to its coverage fields, and the
window is the most-recent <=100 swaps (fomo has no deeper pagination), so
`coverage_ratio` and `unmatched_sell_ratio` are load-bearing honesty signals.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone

_MIN_N_FOR_DISPERSION = 5


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _mean(vals):
    vals = [v for v in vals if v is not None]
    return (sum(vals) / len(vals)) if vals else None


def _median(vals):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def _std(vals):
    vals = [v for v in vals if v is not None]
    if len(vals) < 2:
        return None
    mu = sum(vals) / len(vals)
    return (sum((v - mu) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5


def _pct(vals, q):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    k = (len(vals) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def _safe_div(a, b):
    return (a / b) if (b and a is not None) else None


def _parse(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _balances_metrics(balances, open_bags):
    """Live mark-to-market from /balances. Returns unrealized fields, or {} when balances
    are absent/unusable so the columns stay NULL rather than a misleading 0.

    Price is `tokenFilterResult.priceUSD`. Each holding's `userToken` carries fomo's own
    server-side cost basis + realized PnL (full-history, NOT bound by the 100-swap cap):
      - currentCostBasisUsd -> accurate unrealized = value - cost (no reconstruction guess)
      - totalRealizedPnlUsd -> realized PnL on still-held tokens (partial view of lifetime)
    When userToken cost basis is present we use it (unrealized_partial=0); otherwise we fall
    back to subtracting the reconstructed open-bag cost and flag partial if a held token has
    no in-window cost basis. Cash tokens (USDC/WSOL/USDT) are excluded.
    """
    from .classify import CASH_TOKENS
    if not isinstance(balances, dict) or not any(
            k in balances for k in ("balances", "livePerpPnl", "otherPnl")):
        return {}
    holdings = balances.get("balances") or []
    value = 0.0
    held_mints = set()
    count = 0
    have_fomo_cost = False
    fomo_unreal = 0.0
    fomo_realized_held = 0.0
    for h in holdings:
        bal = h.get("balance") or {}
        mint = bal.get("tokenAddress")
        if not mint or mint in CASH_TOKENS:
            continue
        amt = _f(bal.get("shiftedBalance"))
        price = _f((h.get("tokenFilterResult") or {}).get("priceUSD"))
        if amt <= 0 or price <= 0:
            continue
        v = amt * price
        value += v
        held_mints.add(mint)
        count += 1
        ut = h.get("userToken") or {}
        cb = ut.get("currentCostBasisUsd")
        if cb is not None:
            have_fomo_cost = True
            fomo_unreal += v - _f(cb)
        fomo_realized_held += _f(ut.get("totalRealizedPnlUsd"))
    if have_fomo_cost:                       # fomo's server-side cost basis (accurate)
        unrealized, partial = fomo_unreal, 0
    else:                                    # fallback: reconstructed open-bag cost (capped)
        open_cost = sum(b["cost_usd"] for b in open_bags)
        open_mints = {b["mint"] for b in open_bags}
        unrealized = value - open_cost
        partial = 1 if (held_mints - open_mints) else 0
    return {
        "live_holdings_value_usd": round(value, 2),
        "live_holdings_count": count,
        "unrealized_pnl_usd": round(unrealized, 2),
        "unrealized_partial": partial,
        "fomo_realized_held_usd": round(fomo_realized_held, 2),
        "live_perp_pnl": _f(balances.get("livePerpPnl")),
        "other_pnl": _f(balances.get("otherPnl")),
    }


def compute_metrics(profile, swaps, recon, *, now_iso, large_buy_usd, swap_count, balances=None):
    rts = recon["roundtrips"]
    bags = recon["open_bags"]
    buys = recon["buys"]
    total = recon["total"]

    pnls = [r["realized_usd"] for r in rts]
    pcts = [r["pnl_pct"] for r in rts]
    holds = [r["hold_s"] for r in rts if r["hold_s"] is not None]
    n_closed = len(rts)
    distinct_exit_events = len({(r["mint"], r["exit_ts"]) for r in rts})
    distinct_closed_mints = len({r["mint"] for r in rts})
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    win_pcts = [r["pnl_pct"] for r in rts if r["realized_usd"] > 0]
    loss_pcts = [r["pnl_pct"] for r in rts if r["realized_usd"] < 0]

    # realized max drawdown on cumulative realized (chronological by exit)
    cum = peak = mdd = 0.0
    for r in sorted(rts, key=lambda x: x["exit_ts"] or ""):
        cum += r["realized_usd"]
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)

    # signed concentration: win side and loss side separately (not abs-mixed)
    top3_win_share = _safe_div(sum(sorted(wins, reverse=True)[:3]), sum(wins)) if wins else None
    loss_mags = sorted((-p for p in losses), reverse=True)
    top3_loss_share = _safe_div(sum(loss_mags[:3]), sum(loss_mags)) if loss_mags else None

    sizes = [b["usd"] for b in buys]
    now = _parse(now_iso) or datetime.now(timezone.utc)

    def _age_h(ts):
        t = _parse(ts)
        return ((now - t).total_seconds() / 3600.0) if t else None

    swap_ts = [t for t in (_parse(s.get("createdAt")) for s in swaps) if t]
    swaps_24h = sum(1 for t in swap_ts if (now - t).total_seconds() <= 86400)
    swaps_7d = sum(1 for t in swap_ts if (now - t).total_seconds() <= 7 * 86400)
    vol_7d = sum(_f(s.get("humanUsdAmountIn")) for s in swaps
                 if _parse(s.get("createdAt"))
                 and (now - _parse(s.get("createdAt"))).total_seconds() <= 7 * 86400)
    large_7d = sum(1 for b in buys if b["usd"] >= large_buy_usd and _parse(b["ts"])
                   and (now - _parse(b["ts"])).total_seconds() <= 7 * 86400)
    active_days = len({t.date() for t in swap_ts})
    ss = sorted(swap_ts)
    gaps = [(b - a).total_seconds() / 3600.0 for a, b in zip(ss, ss[1:])]
    longest_gap_h = max(gaps) if gaps else None

    # re-buy-lower rate per mint (chronological buys)
    by_mint = defaultdict(list)
    for b in sorted(buys, key=lambda x: x["ts"] or ""):
        by_mint[b["mint"]].append(b["price"])
    downs = nb = 0
    for prices in by_mint.values():
        for i in range(1, len(prices)):
            nb += 1
            if prices[i] < prices[i - 1]:
                downs += 1
    distinct_mints = len({b["mint"] for b in buys})

    # fee drag is fees over MEMECOIN-trade volume — exclude cash_route (USDC<->WSOL
    # rebalances) from the denominator or a frequent rebalancer looks artificially cheap.
    from .classify import classify_swap
    fee_sum = 0.0
    vol_sum = 0.0
    for s in swaps:
        if classify_swap(s)[0] == "cash_route":
            continue
        fee_sum += _f(s.get("platformFeeHumanAmount")) + _f(s.get("referralFeeHumanAmount"))
        vol_sum += _f(s.get("humanUsdAmountIn"))

    chain_mix, prov_mix = {}, {}
    for s in swaps:
        c = str(s.get("networkId"))
        p = str(s.get("provider"))
        chain_mix[c] = chain_mix.get(c, 0) + 1
        prov_mix[p] = prov_mix.get(p, 0) + 1

    created = _parse(profile.get("createdAt"))
    acct_age = ((now - created).total_seconds() / 86400.0) if created else None
    followers = _int(profile.get("followers"))
    following = _int(profile.get("following"))

    swap_count = _int(swap_count)
    total_buy_notional = recon["total_buy_notional"]
    open_bag_cost = recon["open_bag_cost"]
    closed_cost = total_buy_notional - open_bag_cost   # cost basis of what was actually sold

    # windowed realized PnL (by round-trip exit time). Only claim a window we can SEE
    # to the bottom of: our newest ≤100 swaps always include recent activity, so the
    # only risk is missing OLD trades -> covered iff the window spans >= N days OR we
    # pulled the trader's full history (nothing older exists). Uncovered -> null.
    full_hist = 1 if (swap_count and total >= swap_count) else 0
    wd = recon["window"]["days"]

    def _win_realized(days):
        if not (full_hist or wd >= days):
            return None, 0
        cutoff = now - timedelta(days=days)
        tot = sum(r["realized_usd"] for r in rts
                  if _parse(r["exit_ts"]) and _parse(r["exit_ts"]) >= cutoff)
        return round(tot, 2), 1

    realized_24h, cov_24h = _win_realized(1)
    realized_7d, cov_7d = _win_realized(7)
    realized_30d, cov_30d = _win_realized(30)
    # count of round-trips closed in the trailing 24h — the forward snapshot tiles this
    # daily; >=100 means the day may exceed our 100-swap window (saturated, undercounts).
    n_closed_24h = sum(1 for r in rts if _parse(r["exit_ts"])
                       and (now - _parse(r["exit_ts"])).total_seconds() <= 86400)

    disp = _std(pcts)
    m = {
        "source_user_id": profile.get("id"),
        "handle": profile.get("userHandle") or profile.get("displayName"),
        "computed_at": now_iso,
        "data_as_of": recon["window"]["newest"],
        # coverage
        "swaps_analyzed": total,
        "coverage_ratio": _safe_div(total, swap_count) if swap_count else None,
        "hit_full_history": 1 if (swap_count and total >= swap_count) else 0,
        "window_days": round(recon["window"]["days"], 3),
        "oldest_swap": recon["window"]["oldest"],
        "newest_swap": recon["window"]["newest"],
        "pct_off_platform": _safe_div(recon["off_platform"], total),
        "pct_route_approx": _safe_div(recon["routes"], total),
        "n_open_bags": len(bags),
        "n_unmatched_sells": recon["unmatched_sells"],
        "unmatched_sell_ratio": (_safe_div(recon["unmatched_sells"],
                                           n_closed + recon["unmatched_sells"])
                                 if (n_closed + recon["unmatched_sells"]) else None),
        "closed_coverage": (_safe_div(n_closed, n_closed + len(bags))
                            if (n_closed + len(bags)) else None),
        # profitability (realized, over the window)
        "n_closed": n_closed,
        "matched_lots": n_closed,
        "distinct_exit_events": distinct_exit_events,
        "distinct_closed_mints": distinct_closed_mints,
        "realized_pnl_usd": round(sum(pnls), 2),
        "realized_pnl_24h": realized_24h, "realized_24h_covered": cov_24h,
        "n_closed_24h": n_closed_24h,
        "realized_pnl_7d": realized_7d, "realized_7d_covered": cov_7d,
        "realized_pnl_30d": realized_30d, "realized_30d_covered": cov_30d,
        "total_buy_notional": round(total_buy_notional, 2),
        "open_bag_cost_usd": round(open_bag_cost, 2),
        "dollar_weighted_return_pct": (round(sum(pnls) / closed_cost * 100.0, 2)
                                       if closed_cost > 0 else None),
        "mean_pnl_pct": _mean(pcts),
        "median_pnl_pct": _median(pcts),
        "win_rate": _safe_div(len(wins), n_closed),
        "profit_factor": (_safe_div(sum(wins), abs(sum(losses))) if losses else None),
        "undefeated": 1 if (n_closed > 0 and not losses) else 0,
        "pnl_pct_p10": _pct(pcts, 0.10), "pnl_pct_p25": _pct(pcts, 0.25),
        "pnl_pct_p50": _pct(pcts, 0.50), "pnl_pct_p75": _pct(pcts, 0.75),
        "pnl_pct_p90": _pct(pcts, 0.90),
        "best_pct": max(pcts) if pcts else None,
        "worst_pct": min(pcts) if pcts else None,
        "return_std": disp,
        "pnl_pct_dispersion_ratio": (_safe_div(_mean(pcts), disp)
                                     if (disp and n_closed >= _MIN_N_FOR_DISPERSION) else None),
        "realized_max_drawdown_usd": round(mdd, 2),
        "avg_win_pct": _mean(win_pcts),
        "avg_loss_pct": _mean(loss_pcts),
        "top3_win_share": top3_win_share,
        "top3_loss_share": top3_loss_share,
        # behavior
        "median_hold_s": _median(holds),
        "mean_hold_s": _mean(holds),
        "hold_p10": _pct(holds, 0.10), "hold_p90": _pct(holds, 0.90),
        "pct_held_under_60s": (_safe_div(sum(1 for h in holds if h < 60), len(holds))
                               if holds else None),
        "pct_held_over_1d": (_safe_div(sum(1 for h in holds if h > 86400), len(holds))
                             if holds else None),
        "avg_buy_usd": _mean(sizes),
        "buy_usd_p10": _pct(sizes, 0.10), "buy_usd_p50": _pct(sizes, 0.50),
        "buy_usd_p90": _pct(sizes, 0.90),
        "max_buy_usd": max(sizes) if sizes else None,
        "size_cv": (_safe_div(_std(sizes), _mean(sizes)) if (sizes and _mean(sizes)) else None),
        # fraction of BUYS that re-enter an already-bought token (in [0,1]). The old formula
        # was total_swaps/distinct_mints — it counted sells+routes as re-entries and wasn't a rate.
        "re_entry_rate": (_safe_div(len(buys) - distinct_mints, len(buys)) if buys else None),
        "avg_buys_per_token": (_safe_div(len(buys), distinct_mints) if distinct_mints else None),
        "rebuy_lower_rate": _safe_div(downs, nb),
        "fee_drag_pct": _safe_div(fee_sum, vol_sum),
        # recency
        "last_swap_age_h": _age_h(recon["window"]["newest"]),
        "swaps_24h": swaps_24h, "swaps_7d": swaps_7d,
        "volume_7d": round(vol_7d, 2), "large_buys_7d": large_7d,
        "active_days": active_days, "longest_gap_h": longest_gap_h,
        # profile
        "num_trades": _int(profile.get("numTrades")),
        "total_volume": _f(profile.get("totalVolume")),
        "account_age_days": round(acct_age, 2) if acct_age is not None else None,
        "followers": followers, "following": following,
        "follower_ratio": _safe_div(followers, following),
        "has_twitter": 1 if profile.get("twitter") else 0,
        "is_restricted": 1 if profile.get("isRestricted") else 0,
        "is_private": 1 if profile.get("private") else 0,
        "profile_swap_count": swap_count,
        # mixes + blob
        "chain_mix": json.dumps(chain_mix),
        "provider_mix": json.dumps(prov_mix),
    }
    m.update(_balances_metrics(balances, bags))
    m["metrics_json"] = json.dumps({"pnl_pct_all": pcts, "hold_s_all": holds,
                                    "buy_usd_all": sizes})
    return m
