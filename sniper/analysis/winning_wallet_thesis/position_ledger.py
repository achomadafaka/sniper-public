"""Avg-cost per-(condition,outcome,episode) leg accountant (Track 1a). Folds chronological
buy/split/sell/merge events + resolution into a realized PnL with correct cost basis.

SPLIT/MERGE are CONDITION-PAIR events on Polymarket (a complete set = 1 share of every
outcome = exactly $1; the raw rows carry no outcome index and `usdcSize == size`). The
pair-level cash is therefore real and known; only its ALLOCATION across the outcome legs is
synthesized, and that allocation is done by `position_builder` (which can see both legs).
This module consumes the already-allocated per-leg cash:

  - split LegEvent.usdc = the cost basis allocated to THIS leg (the pair's per-leg allocs
    sum to the split row's actual cash).
  - merge LegEvent.usdc = the cash proceeds allocated to THIS leg (sums to the merge row's
    actual cash). Merge realizes real PnL (cash received − avg cost of the burned shares):
    buying both legs cheap then merging for face value is a genuine gain, NOT cost-neutral.

This SUPERSEDES the wallet-level inventory_net_cash band-aid: per-leg attribution makes the
episode realized_pnl sum to the cash-flow truth."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LegEvent:
    kind: str          # buy | split | sell | merge
    shares: float
    usdc: float        # buy: paid · sell: received · split: allocated cost · merge: allocated proceeds


@dataclass
class LegResult:
    realized_pnl: float
    held_shares: float
    total_in_shares: float
    total_cost: float
    split_shares: float
    split_cost: float
    merge_shares: float
    merge_cash: float


def allocate_split_pair(cash: float, vwap0: float | None, vwap1: float | None) -> tuple[float, float]:
    """Allocate a SPLIT's actual cash across the binary pair so the two legs sum to `cash`.
    Use the wallet's own revealed price where a leg has a buy VWAP (leg0 at p, leg1 at 1-p);
    else 50/50. Guarantees pair-sum == cash regardless of which leg revealed a price."""
    if vwap0 is not None:
        p = vwap0
    elif vwap1 is not None:
        p = 1.0 - vwap1
    else:
        p = 0.5
    p = min(1.0, max(0.0, p))
    return round(p * cash, 6), round((1.0 - p) * cash, 6)


def allocate_merge_pair(cash: float) -> tuple[float, float]:
    """A complete set redeems $1 with no canonical per-leg face value; split the MERGE cash
    50/50. Per-leg merge ROI is approximate; the pair total is exact (== cash)."""
    return round(cash / 2.0, 6), round(cash / 2.0, 6)


def account_leg(events: list[LegEvent], *, resolved: bool, won: int | None,
                is_push: bool) -> LegResult:
    shares = cost = realized = 0.0
    split_shares = split_cost = merge_shares = merge_cash = 0.0
    in_shares = in_cost = 0.0
    for e in events:
        if e.kind == "buy":
            shares += e.shares; cost += e.usdc
            in_shares += e.shares; in_cost += e.usdc
        elif e.kind == "split":
            # usdc = pre-allocated cost basis for THIS leg (pair allocs sum to the split's cash)
            shares += e.shares; cost += e.usdc
            in_shares += e.shares; in_cost += e.usdc
            split_shares += e.shares; split_cost += e.usdc
        elif e.kind == "sell":
            avg = cost / shares if shares > 0 else 0.0
            realized += e.usdc - avg * e.shares
            shares -= e.shares; cost -= avg * e.shares
        elif e.kind == "merge":
            # usdc = pre-allocated cash proceeds for THIS leg; realize cash vs avg cost burned
            avg = cost / shares if shares > 0 else 0.0
            realized += e.usdc - avg * e.shares
            merge_shares += e.shares; merge_cash += e.usdc
            shares -= e.shares; cost -= avg * e.shares
    held = max(shares, 0.0)
    if resolved:
        payout = held * (0.5 if is_push else (1.0 if won else 0.0))
        realized += payout - cost                                   # redeem remainder, write off its cost
    return LegResult(realized_pnl=round(realized, 6), held_shares=round(held, 6),
                     total_in_shares=round(in_shares, 6), total_cost=round(in_cost, 6),
                     split_shares=round(split_shares, 6), split_cost=round(split_cost, 6),
                     merge_shares=round(merge_shares, 6), merge_cash=round(merge_cash, 6))
