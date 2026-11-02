"""Reconstruct per-mint round-trips (FIFO) from a fomo swap list.

Pure / network-free. Realized-only: leftover buy lots become open_bags
(censored). Sells with no open lot, or leftover after a partial match, are
unmatched (no cost basis) — the review blocker was that a partial match
silently dropped the leftover; we count it explicitly.
"""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime

from .classify import classify_swap, _f


def _parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def reconstruct(swaps):
    ordered = sorted(swaps, key=lambda s: s.get("createdAt") or "")
    lots = defaultdict(deque)          # mint -> deque of [qty, price, ts]
    roundtrips = []
    buys = []
    counters = {"unmatched": 0, "sells": 0, "buy_notional": 0.0}

    def _buy_leg(mint, usd, qty, ts):
        price = (usd / qty) if qty else 0.0
        if qty > 0 and price > 0:
            lots[mint].append([qty, price, ts])
            buys.append({"mint": mint, "usd": usd, "ts": ts, "price": price})
            counters["buy_notional"] += usd

    def _sell_leg(mint, usd, qty, ts):
        price = (usd / qty) if qty else 0.0
        if qty <= 0 or price <= 0:
            return   # malformed/negative sell -> drop (symmetric with _buy_leg's price>0 guard)
        counters["sells"] += 1
        remaining = qty
        while remaining > 1e-9 and lots[mint]:
            lot = lots[mint][0]
            entry_price = lot[1]
            take = min(remaining, lot[0])
            et = _parse_ts(lot[2])
            xt = _parse_ts(ts)
            roundtrips.append({
                "mint": mint, "qty": take, "entry_price": entry_price,
                "exit_price": price, "entry_ts": lot[2], "exit_ts": ts,
                "realized_usd": (price - entry_price) * take,
                "pnl_pct": ((price / entry_price - 1.0) * 100.0) if entry_price else 0.0,
                "hold_s": (xt - et).total_seconds() if (et and xt) else None,
            })
            lot[0] -= take
            remaining -= take
            if lot[0] <= 1e-9:
                lots[mint].popleft()
        # leftover after any matching (or none) is unmatched — never dropped
        if remaining > 1e-9:
            counters["unmatched"] += 1

    routes = 0
    token_routes = 0
    off_platform = 0

    for s in ordered:
        if str(s.get("isOffPlatform")) == "True":
            off_platform += 1
        kind, mint, usd, qty = classify_swap(s)
        ts = s.get("createdAt")
        if kind == "buy":
            _buy_leg(mint, usd, qty, ts)
        elif kind == "sell":
            _sell_leg(mint, usd, qty, ts)
        elif kind == "cash_route":
            routes += 1
        else:  # token_route: A->B is economically sell(A) + buy(B); split both legs.
            routes += 1
            in_m, out_m = s.get("inTokenAddress"), s.get("outTokenAddress")
            if in_m and in_m == out_m:
                # same-mint in==out (multi-hop router arbing back to the same token) is
                # routing noise, not a trade — splitting it manufactures phantom PnL. Skip.
                continue
            token_routes += 1
            _sell_leg(in_m, _f(s.get("humanUsdAmountIn")), _f(s.get("inHumanAmount")), ts)
            _buy_leg(out_m, _f(s.get("humanUsdAmountOut")), _f(s.get("outHumanAmount")), ts)

    unmatched = counters["unmatched"]
    sells = counters["sells"]
    total_buy_notional = counters["buy_notional"]

    open_bags = []
    open_bag_cost = 0.0
    for mint, dq in lots.items():
        for qty, price, ts in dq:
            if qty > 1e-9:
                open_bags.append({"mint": mint, "qty": qty, "entry_price": price,
                                  "entry_ts": ts, "cost_usd": qty * price})
                open_bag_cost += qty * price

    parsed = [t for t in (_parse_ts(s.get("createdAt")) for s in ordered) if t]
    window = {
        "oldest": ordered[0].get("createdAt") if ordered else None,
        "newest": ordered[-1].get("createdAt") if ordered else None,
        "days": ((parsed[-1] - parsed[0]).total_seconds() / 86400.0) if len(parsed) >= 2 else 0.0,
    }
    return {"roundtrips": roundtrips, "open_bags": open_bags, "open_bag_cost": open_bag_cost,
            "total_buy_notional": total_buy_notional,
            "unmatched_sells": unmatched, "routes": routes, "token_routes": token_routes,
            "off_platform": off_platform, "buys": buys, "sells": sells,
            "total": len(ordered), "window": window}
