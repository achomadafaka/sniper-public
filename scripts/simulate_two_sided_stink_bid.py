#!/usr/bin/env python3
"""Two-sided strict simulator for tennis stink bids.

This first version tests a simple structure: place one resting bid on each
outcome at the start/reference time, keep both active until cutoff, and only
count fills backed by size-qualified real trade rows.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import duckdb

from sniper.services.paper_limit_engine import compute_bid_price
from scripts.simulate_stink_bid_smoke import (
    _entry_reference_price,
    _load_runner_parity_trade_cache,
    _to_ts,
    _winner_side,
)


@dataclass
class TwoSidedConfig:
    mode: str = "static"  # static | after_first_fill
    dip_pct: float = 0.15
    position_size_usdc: float = 25.0
    min_bid: float = 0.05
    min_fill_size: float = 50.0
    max_position_minutes: Optional[int] = 240
    max_pair_bid_cost: Optional[float] = None
    hedge_ttl_minutes: Optional[int] = 120
    require_full_hedge_size: bool = True


@dataclass
class LegFill:
    side: str
    token_id: str
    bid_price: float
    fill_ts: int
    fill_price: float
    fill_size: float
    trade_side: Optional[str]
    transaction_hash: Optional[str]


@dataclass
class TwoSidedAttempt:
    condition_id: str
    title: str
    tour_prefix: str
    sack_year: str
    start_ts: Optional[int]
    cutoff_ts: Optional[int]
    ref_yes: Optional[float]
    ref_no: Optional[float]
    bid_yes: Optional[float]
    bid_no: Optional[float]
    winner_side: Optional[str]
    yes_fill: Optional[dict]
    no_fill: Optional[dict]
    filled_legs: int
    paired: bool
    first_fill_ts: Optional[int]
    second_fill_ts: Optional[int]
    combined_fill_price: Optional[float]
    total_cost_usdc: float
    payout_usdc: float
    pnl_usdc: float
    return_pct: Optional[float]
    loose_fills_seen: int
    status: str
    reason: str


def _strict_fill(
    trades: list[dict],
    *,
    token_id: str,
    bid: float,
    start_ts: int,
    cutoff_ts: Optional[int],
    min_fill_size: float,
    required_size: Optional[float] = None,
) -> tuple[Optional[LegFill], int]:
    loose = 0
    size_floor = max(min_fill_size, required_size or 0.0)
    for trade in trades:
        ts = trade["timestamp_sec"]
        if ts < start_ts:
            continue
        if cutoff_ts is not None and ts > cutoff_ts:
            break
        if trade["price"] > bid:
            continue
        if trade["size"] < size_floor:
            loose += 1
            continue
        return (
            LegFill(
                side="",
                token_id=token_id,
                bid_price=bid,
                fill_ts=ts,
                fill_price=trade["price"],
                fill_size=trade["size"],
                trade_side=trade["side"],
                transaction_hash=trade["transaction_hash"],
            ),
            loose,
        )
    return None, loose


def _leg_dict(fill: Optional[LegFill]) -> Optional[dict]:
    return asdict(fill) if fill is not None else None


def _attempt(
    *,
    condition_id: str,
    title: str,
    tour_prefix: str,
    sack_year: str,
    start_ts: Optional[int],
    cutoff_ts: Optional[int],
    ref_yes: Optional[float],
    ref_no: Optional[float],
    bid_yes: Optional[float],
    bid_no: Optional[float],
    winner_side: Optional[str],
    yes_fill: Optional[LegFill],
    no_fill: Optional[LegFill],
    loose_fills_seen: int,
    position_size_usdc: float,
    status: str,
    reason: str,
    total_cost_override: Optional[float] = None,
    payout_override: Optional[float] = None,
    pnl_override: Optional[float] = None,
) -> TwoSidedAttempt:
    fills = [fill for fill in (yes_fill, no_fill) if fill is not None]
    fills.sort(key=lambda fill: (fill.fill_ts, fill.side))
    total_cost = total_cost_override if total_cost_override is not None else position_size_usdc * len(fills)
    if payout_override is not None:
        payout = payout_override
    else:
        payout = 0.0
        if winner_side == "yes" and yes_fill is not None:
            payout = position_size_usdc / yes_fill.fill_price
        elif winner_side == "no" and no_fill is not None:
            payout = position_size_usdc / no_fill.fill_price
    pnl = pnl_override if pnl_override is not None else payout - total_cost
    combined = (
        yes_fill.fill_price + no_fill.fill_price
        if yes_fill is not None and no_fill is not None
        else None
    )
    return TwoSidedAttempt(
        condition_id=condition_id,
        title=title,
        tour_prefix=tour_prefix or "",
        sack_year=sack_year,
        start_ts=start_ts,
        cutoff_ts=cutoff_ts,
        ref_yes=ref_yes,
        ref_no=ref_no,
        bid_yes=bid_yes,
        bid_no=bid_no,
        winner_side=winner_side,
        yes_fill=_leg_dict(yes_fill),
        no_fill=_leg_dict(no_fill),
        filled_legs=len(fills),
        paired=len(fills) == 2,
        first_fill_ts=fills[0].fill_ts if fills else None,
        second_fill_ts=fills[1].fill_ts if len(fills) > 1 else None,
        combined_fill_price=combined,
        total_cost_usdc=total_cost,
        payout_usdc=payout,
        pnl_usdc=pnl,
        return_pct=(pnl / total_cost) if total_cost else None,
        loose_fills_seen=loose_fills_seen,
        status=status,
        reason=reason,
    )


def _simulate_event(con, row, cfg: TwoSidedConfig, trade_cache: dict[tuple[str, str], list[dict]]) -> TwoSidedAttempt:
    condition_id, title, tour_prefix, start_date, token_yes, token_no, sack_winner, sack_date = row
    start_ts = _to_ts(start_date)
    sack_year = str(sack_date or "")[:4]
    cutoff_ts = start_ts + cfg.max_position_minutes * 60 if start_ts is not None and cfg.max_position_minutes else None

    series = {}
    for side, token in (("yes", token_yes), ("no", token_no)):
        points = con.execute(
            """
            SELECT timestamp_sec, price
            FROM prices_history
            WHERE token_id = ? AND fidelity = 1
            ORDER BY timestamp_sec
            """,
            [token],
        ).fetchall()
        series[side] = [(int(ts), float(price)) for ts, price in points]
    if not series["yes"] or not series["no"]:
        return _attempt(
            condition_id=condition_id,
            title=title,
            tour_prefix=tour_prefix,
            sack_year=sack_year,
            start_ts=start_ts,
            cutoff_ts=cutoff_ts,
            ref_yes=None,
            ref_no=None,
            bid_yes=None,
            bid_no=None,
            winner_side=None,
            yes_fill=None,
            no_fill=None,
            loose_fills_seen=0,
            position_size_usdc=cfg.position_size_usdc,
            status="skipped",
            reason="missing_price_history",
        )

    ref_yes = _entry_reference_price(series["yes"], start_ts)
    ref_no = _entry_reference_price(series["no"], start_ts)
    winner_side = _winner_side(title, sack_winner)
    if ref_yes is None or ref_no is None:
        reason = "missing_reference_price"
    elif winner_side is None:
        reason = "winner_side_unmatched"
    else:
        reason = ""
    if reason:
        return _attempt(
            condition_id=condition_id,
            title=title,
            tour_prefix=tour_prefix,
            sack_year=sack_year,
            start_ts=start_ts,
            cutoff_ts=cutoff_ts,
            ref_yes=ref_yes,
            ref_no=ref_no,
            bid_yes=None,
            bid_no=None,
            winner_side=winner_side,
            yes_fill=None,
            no_fill=None,
            loose_fills_seen=0,
            position_size_usdc=cfg.position_size_usdc,
            status="skipped",
            reason=reason,
        )

    bid_yes = compute_bid_price(ref_yes, dip_pct=cfg.dip_pct, min_bid=cfg.min_bid)
    bid_no = compute_bid_price(ref_no, dip_pct=cfg.dip_pct, min_bid=cfg.min_bid)
    if cfg.mode == "static" and cfg.max_pair_bid_cost is not None and bid_yes + bid_no > cfg.max_pair_bid_cost:
        return _attempt(
            condition_id=condition_id,
            title=title,
            tour_prefix=tour_prefix,
            sack_year=sack_year,
            start_ts=start_ts,
            cutoff_ts=cutoff_ts,
            ref_yes=ref_yes,
            ref_no=ref_no,
            bid_yes=bid_yes,
            bid_no=bid_no,
            winner_side=winner_side,
            yes_fill=None,
            no_fill=None,
            loose_fills_seen=0,
            position_size_usdc=cfg.position_size_usdc,
            status="skipped",
            reason="pair_bid_cost_above_max",
        )

    if cfg.mode == "after_first_fill":
        favorite_side = "yes" if ref_yes >= ref_no else "no"
        first_token = token_yes if favorite_side == "yes" else token_no
        hedge_side = "no" if favorite_side == "yes" else "yes"
        hedge_token = token_no if hedge_side == "no" else token_yes
        first_bid = bid_yes if favorite_side == "yes" else bid_no
        active_ts = start_ts if start_ts is not None else min(series["yes"][0][0], series["no"][0][0])
        first_fill, first_loose = _strict_fill(
            trade_cache.get((condition_id, first_token), []),
            token_id=first_token,
            bid=first_bid,
            start_ts=active_ts,
            cutoff_ts=cutoff_ts,
            min_fill_size=cfg.min_fill_size,
        )
        if first_fill is None:
            return _attempt(
                condition_id=condition_id,
                title=title,
                tour_prefix=tour_prefix,
                sack_year=sack_year,
                start_ts=start_ts,
                cutoff_ts=cutoff_ts,
                ref_yes=ref_yes,
                ref_no=ref_no,
                bid_yes=bid_yes,
                bid_no=bid_no,
                winner_side=winner_side,
                yes_fill=None,
                no_fill=None,
                loose_fills_seen=first_loose,
                position_size_usdc=cfg.position_size_usdc,
                status="no_fill",
                reason="no_first_leg_fill",
            )
        first_fill.side = favorite_side
        first_shares = cfg.position_size_usdc / first_fill.fill_price
        hedge_ref = ref_no if hedge_side == "no" else ref_yes
        hedge_base_bid = compute_bid_price(hedge_ref, dip_pct=cfg.dip_pct, min_bid=cfg.min_bid)
        hedge_bid = hedge_base_bid
        if cfg.max_pair_bid_cost is not None:
            hedge_bid = min(hedge_base_bid, cfg.max_pair_bid_cost - first_fill.fill_price)
        if hedge_bid < cfg.min_bid:
            yes_fill = first_fill if favorite_side == "yes" else None
            no_fill = first_fill if favorite_side == "no" else None
            payout = first_shares if winner_side == favorite_side else 0.0
            pnl = payout - cfg.position_size_usdc
            return _attempt(
                condition_id=condition_id,
                title=title,
                tour_prefix=tour_prefix,
                sack_year=sack_year,
                start_ts=start_ts,
                cutoff_ts=cutoff_ts,
                ref_yes=ref_yes,
                ref_no=ref_no,
                bid_yes=bid_yes if favorite_side == "yes" else hedge_bid,
                bid_no=bid_no if favorite_side == "no" else hedge_bid,
                winner_side=winner_side,
                yes_fill=yes_fill,
                no_fill=no_fill,
                loose_fills_seen=first_loose,
                position_size_usdc=cfg.position_size_usdc,
                status="unpaired",
                reason="hedge_bid_below_min",
                total_cost_override=cfg.position_size_usdc,
                payout_override=payout,
                pnl_override=pnl,
            )
        hedge_cutoff_ts = cutoff_ts
        if cfg.hedge_ttl_minutes is not None:
            ttl_cutoff = first_fill.fill_ts + cfg.hedge_ttl_minutes * 60
            hedge_cutoff_ts = min(cutoff_ts, ttl_cutoff) if cutoff_ts is not None else ttl_cutoff
        hedge_required_size = first_shares if cfg.require_full_hedge_size else None
        hedge_fill, hedge_loose = _strict_fill(
            trade_cache.get((condition_id, hedge_token), []),
            token_id=hedge_token,
            bid=hedge_bid,
            start_ts=first_fill.fill_ts,
            cutoff_ts=hedge_cutoff_ts,
            min_fill_size=cfg.min_fill_size,
            required_size=hedge_required_size,
        )
        yes_fill = first_fill if favorite_side == "yes" else None
        no_fill = first_fill if favorite_side == "no" else None
        total_cost = cfg.position_size_usdc
        payout = first_shares if winner_side == favorite_side else 0.0
        status = "unpaired"
        reason = "hedge_not_filled"
        if hedge_fill is not None:
            hedge_fill.side = hedge_side
            if hedge_side == "yes":
                yes_fill = hedge_fill
            else:
                no_fill = hedge_fill
            hedge_cost = first_shares * hedge_fill.fill_price
            total_cost = cfg.position_size_usdc + hedge_cost
            payout = first_shares
            status = "paired"
            reason = "hedged_pair_filled"
        pnl = payout - total_cost
        return _attempt(
            condition_id=condition_id,
            title=title,
            tour_prefix=tour_prefix,
            sack_year=sack_year,
            start_ts=start_ts,
            cutoff_ts=cutoff_ts,
            ref_yes=ref_yes,
            ref_no=ref_no,
            bid_yes=first_bid if favorite_side == "yes" else hedge_bid,
            bid_no=first_bid if favorite_side == "no" else hedge_bid,
            winner_side=winner_side,
            yes_fill=yes_fill,
            no_fill=no_fill,
            loose_fills_seen=first_loose + hedge_loose,
            position_size_usdc=cfg.position_size_usdc,
            status=status,
            reason=reason,
            total_cost_override=total_cost,
            payout_override=payout,
            pnl_override=pnl,
        )

    if cfg.mode != "static":
        raise ValueError(f"unsupported mode: {cfg.mode}")

    active_ts = start_ts if start_ts is not None else min(series["yes"][0][0], series["no"][0][0])
    yes_fill, yes_loose = _strict_fill(
        trade_cache.get((condition_id, token_yes), []),
        token_id=token_yes,
        bid=bid_yes,
        start_ts=active_ts,
        cutoff_ts=cutoff_ts,
        min_fill_size=cfg.min_fill_size,
    )
    no_fill, no_loose = _strict_fill(
        trade_cache.get((condition_id, token_no), []),
        token_id=token_no,
        bid=bid_no,
        start_ts=active_ts,
        cutoff_ts=cutoff_ts,
        min_fill_size=cfg.min_fill_size,
    )
    if yes_fill is not None:
        yes_fill.side = "yes"
    if no_fill is not None:
        no_fill.side = "no"
    filled_legs = int(yes_fill is not None) + int(no_fill is not None)
    status = "paired" if filled_legs == 2 else ("unpaired" if filled_legs == 1 else "no_fill")
    reason = {
        2: "both_legs_filled",
        1: "one_leg_filled",
        0: "no_strict_fills",
    }[filled_legs]
    return _attempt(
        condition_id=condition_id,
        title=title,
        tour_prefix=tour_prefix,
        sack_year=sack_year,
        start_ts=start_ts,
        cutoff_ts=cutoff_ts,
        ref_yes=ref_yes,
        ref_no=ref_no,
        bid_yes=bid_yes,
        bid_no=bid_no,
        winner_side=winner_side,
        yes_fill=yes_fill,
        no_fill=no_fill,
        loose_fills_seen=yes_loose + no_loose,
        position_size_usdc=cfg.position_size_usdc,
        status=status,
        reason=reason,
    )


def _summarize(attempts: list[TwoSidedAttempt]) -> dict:
    active = [row for row in attempts if row.status in {"paired", "unpaired", "no_fill"}]
    filled = [row for row in active if row.filled_legs > 0]
    paired = [row for row in active if row.paired]
    unpaired = [row for row in active if row.status == "unpaired"]
    pnl = [row.pnl_usdc for row in filled]
    paired_pnl = [row.pnl_usdc for row in paired]
    gross_profit = sum(value for value in pnl if value > 0)
    gross_loss = -sum(value for value in pnl if value < 0)
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for row in sorted(filled, key=lambda item: (item.first_fill_ts or 0, item.condition_id)):
        equity += row.pnl_usdc
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    combined_costs = [row.combined_fill_price for row in paired if row.combined_fill_price is not None]
    return {
        "active_markets": len(active),
        "markets_with_any_fill": len(filled),
        "paired_fills": len(paired),
        "unpaired_fills": len(unpaired),
        "no_fill_markets": sum(1 for row in active if row.status == "no_fill"),
        "paired_rate_per_active_market": len(paired) / len(active) if active else 0.0,
        "unpaired_rate_per_any_fill": len(unpaired) / len(filled) if filled else 0.0,
        "winning_filled_markets": sum(1 for row in filled if row.pnl_usdc > 0),
        "losing_filled_markets": sum(1 for row in filled if row.pnl_usdc < 0),
        "total_pnl_usdc": sum(pnl),
        "paired_pnl_usdc": sum(paired_pnl),
        "unpaired_pnl_usdc": sum(row.pnl_usdc for row in unpaired),
        "avg_pnl_per_filled_market": statistics.mean(pnl) if pnl else 0.0,
        "avg_paired_pnl": statistics.mean(paired_pnl) if paired_pnl else 0.0,
        "median_combined_fill_price": statistics.median(combined_costs) if combined_costs else None,
        "min_combined_fill_price": min(combined_costs) if combined_costs else None,
        "max_combined_fill_price": max(combined_costs) if combined_costs else None,
        "paired_cost_lte_095": sum(1 for cost in combined_costs if cost <= 0.95),
        "paired_cost_lte_090": sum(1 for cost in combined_costs if cost <= 0.90),
        "paired_cost_lte_085": sum(1 for cost in combined_costs if cost <= 0.85),
        "gross_profit_usdc": gross_profit,
        "gross_loss_usdc": gross_loss,
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "max_drawdown_usdc": max_drawdown,
    }


def run_sim(
    db_path: str | Path,
    cfg: TwoSidedConfig,
    *,
    limit: Optional[int] = None,
    since_year: Optional[int] = None,
    until_year: Optional[int] = None,
    tour: Optional[str] = None,
    include_audit: bool = False,
) -> dict:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        filters = []
        params = []
        if since_year is not None:
            filters.append("CAST(substr(a.sack_date, 1, 4) AS INTEGER) >= ?")
            params.append(since_year)
        if until_year is not None:
            filters.append("CAST(substr(a.sack_date, 1, 4) AS INTEGER) <= ?")
            params.append(until_year)
        if tour is not None:
            if tour not in {"atp", "wta"}:
                raise ValueError(f"unsupported tour: {tour!r}")
            filters.append("LOWER(a.sack_tour) LIKE ?")
            params.append(f"{tour}%")
        filter_clause = f"AND {' AND '.join(filters)}" if filters else ""
        rows = con.execute(
            f"""
            SELECT g.condition_id, g.title, g.tour_prefix, g.start_date,
                   g.token_id_yes, g.token_id_no, a.sack_winner, a.sack_date
            FROM gamma_events g
            JOIN event_alignment a USING(condition_id)
            WHERE EXISTS (SELECT 1 FROM prices_history p WHERE p.token_id = g.token_id_yes AND p.fidelity = 1)
              AND EXISTS (SELECT 1 FROM prices_history p WHERE p.token_id = g.token_id_no AND p.fidelity = 1)
              {filter_clause}
            ORDER BY g.end_date, g.condition_id
            """,
            params,
        ).fetchall()
        if limit:
            rows = rows[:limit]
        trade_cache = _load_runner_parity_trade_cache(con)
        attempts = [_simulate_event(con, row, cfg, trade_cache) for row in rows]
        by_year = {}
        for year in sorted({row.sack_year for row in attempts if row.filled_legs > 0}):
            by_year[year] = _summarize([row for row in attempts if row.sack_year == year])
        out = {
            "config": asdict(cfg),
            "eligible_priced_events": len(rows),
            "since_year": since_year,
            "until_year": until_year,
            "tour": tour,
            **_summarize(attempts),
            "by_year": by_year,
            "sample_attempts": [asdict(row) for row in attempts if row.filled_legs > 0][:10],
        }
        if include_audit:
            out["audit_events"] = [asdict(row) for row in attempts]
        return out
    finally:
        con.close()


def parse_args(argv: Optional[list[str]] = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/backtest/stink_bid/backtest.db")
    parser.add_argument("--mode", choices=["static", "after_first_fill"], default="static")
    parser.add_argument("--dip-pct", type=float, default=0.15)
    parser.add_argument("--position-size-usdc", type=float, default=25.0)
    parser.add_argument("--min-bid", type=float, default=0.05)
    parser.add_argument("--min-fill-size", type=float, default=50.0)
    parser.add_argument("--max-position-minutes", type=int, default=240)
    parser.add_argument("--max-pair-bid-cost", type=float)
    parser.add_argument("--hedge-ttl-minutes", type=int, default=120)
    parser.add_argument("--allow-partial-hedge-size", action="store_true")
    parser.add_argument("--since-year", type=int)
    parser.add_argument("--until-year", type=int)
    parser.add_argument("--tour", choices=["atp", "wta"])
    parser.add_argument("--limit", type=int)
    parser.add_argument("--include-audit", action="store_true")
    parser.add_argument("--audit-output", help="Write per-market attempts as JSONL.")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    args = parse_args(argv)
    cfg = TwoSidedConfig(
        mode=args.mode,
        dip_pct=args.dip_pct,
        position_size_usdc=args.position_size_usdc,
        min_bid=args.min_bid,
        min_fill_size=args.min_fill_size,
        max_position_minutes=args.max_position_minutes,
        max_pair_bid_cost=args.max_pair_bid_cost,
        hedge_ttl_minutes=args.hedge_ttl_minutes,
        require_full_hedge_size=not args.allow_partial_hedge_size,
    )
    out = run_sim(
        args.db,
        cfg,
        limit=args.limit,
        since_year=args.since_year,
        until_year=args.until_year,
        tour=args.tour,
        include_audit=args.include_audit or bool(args.audit_output),
    )
    if args.audit_output:
        audit_events = out.pop("audit_events", [])
        with open(args.audit_output, "w") as f:
            for row in audit_events:
                f.write(json.dumps(row, sort_keys=True) + "\n")
        out["audit_output"] = args.audit_output
        out["audit_events_written"] = len(audit_events)
    print(json.dumps(out, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
