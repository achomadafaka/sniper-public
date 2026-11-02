#!/usr/bin/env python3
"""Smoke simulation for the stink-bid tennis backtest snapshot.

This is intentionally smaller than the Phase 3/4 replay engine in the spec. It
uses cached DuckDB tables only, locks the entry favorite from the first available
pre-match price, models a down-only resting bid, and exits at aligned match
resolution. Use it to verify that the data snapshot can produce trades before
running the full scientific sweep.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import duckdb

from data.sackmann import normalize_name, player_sides_from_title, side_name_candidates
from sniper.services.paper_limit_engine import compute_bid_price

SPORTS_TAKER_FEE_RATE = 0.03


@dataclass
class SimConfig:
    dip_pct: float = 0.30
    min_favorite: float = 0.55
    cycle_seconds: int = 300
    position_size_usdc: float = 25.0
    min_bid: float = 0.05
    reprice_threshold: float = 0.02
    modeled_half_spread: float = 0.005
    fill_model: str = "price_cross"  # price_cross | trade_confirmed | runner_parity_trade | filtered_runner_parity_trade
    max_position_minutes: Optional[int] = 240
    min_fill_size: float = 50.0
    favorite_flip_cancel: bool = True
    min_prior_trades: int = 0
    min_prior_price_range: Optional[float] = None
    prior_window_minutes: int = 60


@dataclass
class SimTrade:
    condition_id: str
    title: str
    tour_prefix: str
    sack_year: str
    favorite_side: str
    winner_side: str
    entry_ts: int
    fill_price: float
    exit_price: float
    pnl_usdc: float
    return_pct: float


@dataclass
class SimAttempt:
    condition_id: str
    title: str
    sack_year: str
    start_ts: Optional[int]
    ref_yes: Optional[float]
    ref_no: Optional[float]
    favorite_side: Optional[str]
    favorite_token: Optional[str]
    favorite_ref: Optional[float]
    winner_side: Optional[str]
    initial_bid: Optional[float]
    final_bid: Optional[float]
    cutoff_ts: Optional[int]
    fill_model: str
    ticks_seen: int
    reprices: int
    loose_fills_seen: int
    prior_trade_count: Optional[int]
    prior_price_range: Optional[float]
    min_observed_price: Optional[float]
    fill_ts: Optional[int]
    fill_price: Optional[float]
    fill_trade_size: Optional[float]
    fill_trade_side: Optional[str]
    fill_trade_outcome: Optional[str]
    fill_transaction_hash: Optional[str]
    exit_price: Optional[float]
    pnl_usdc: Optional[float]
    return_pct: Optional[float]
    status: str
    reason: str


def _to_ts(value) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _candidate_hit(player_side: str, full_name: str) -> bool:
    side_candidates = side_name_candidates(player_side)
    full_candidates = side_name_candidates(full_name)
    full_compact = normalize_name(full_name).replace(" ", "")
    return bool(side_candidates & full_candidates) or any(c in full_compact for c in side_candidates)


def _winner_side(title: str, winner_name: str) -> Optional[str]:
    side1, side2 = player_sides_from_title(title)
    if not side1 or not side2:
        return None
    if _candidate_hit(side1, winner_name):
        return "yes"
    if _candidate_hit(side2, winner_name):
        return "no"
    return None


def _bucket_price_points(points: list[tuple[int, float]], cycle_seconds: int) -> list[tuple[int, float]]:
    if cycle_seconds <= 60:
        return points
    buckets = {}
    for ts, price in points:
        bucket = ts - (ts % cycle_seconds)
        buckets[bucket] = (ts, price)
    return [buckets[k] for k in sorted(buckets)]


def _entry_reference_price(points: list[tuple[int, float]], start_ts: Optional[int]) -> Optional[float]:
    if not points:
        return None
    if start_ts is None:
        return points[0][1]
    before = [price for ts, price in points if ts <= start_ts]
    return before[-1] if before else points[0][1]


def _sports_fee(notional: float, price: float) -> float:
    return notional * SPORTS_TAKER_FEE_RATE * price * (1.0 - price)


def _trade_confirmed_trades(
    con, condition_id: str, token_id: str, before_ts: Optional[int]
) -> list[tuple[int, float]]:
    time_clause = "AND timestamp_sec <= ?" if before_ts is not None else ""
    params = [condition_id, token_id]
    if before_ts is not None:
        params.append(before_ts)
    return [
        (int(ts), float(price))
        for ts, price in con.execute(
            f"""
            SELECT timestamp_sec, price
            FROM trades
            WHERE condition_id = ?
              AND asset = ?
              AND UPPER(COALESCE(side, '')) = 'SELL'
              {time_clause}
            ORDER BY timestamp_sec
            """,
            params,
        ).fetchall()
    ]


def _trade_confirmed_fill(
    trades: list[tuple[int, float]], start_idx: int, bid: float, after_ts: int
) -> tuple[Optional[tuple[int, float]], int]:
    idx = start_idx
    while idx < len(trades) and trades[idx][0] < after_ts:
        idx += 1
    while idx < len(trades):
        ts, price = trades[idx]
        idx += 1
        if price <= bid:
            return (ts, bid), idx
    return None, idx


def _runner_parity_trades(
    con, condition_id: str, token_id: str, before_ts: Optional[int]
) -> list[dict]:
    time_clause = "AND timestamp_sec <= ?" if before_ts is not None else ""
    params = [condition_id, token_id]
    if before_ts is not None:
        params.append(before_ts)
    rows = con.execute(
        f"""
        SELECT timestamp_sec, price, size, side, outcome, transaction_hash
        FROM trades
        WHERE condition_id = ?
          AND asset = ?
          {time_clause}
        ORDER BY timestamp_sec, transaction_hash
        """,
        params,
    ).fetchall()
    return [
        {
            "timestamp_sec": int(ts),
            "price": float(price),
            "size": float(size or 0.0),
            "side": side,
            "outcome": outcome,
            "transaction_hash": transaction_hash,
        }
        for ts, price, size, side, outcome, transaction_hash in rows
        if price is not None
    ]


def _load_runner_parity_trade_cache(con) -> dict[tuple[str, str], list[dict]]:
    rows = con.execute(
        """
        SELECT condition_id, asset, timestamp_sec, price, size, side, outcome, transaction_hash
        FROM trades
        WHERE asset IS NOT NULL
          AND price IS NOT NULL
        ORDER BY condition_id, asset, timestamp_sec, transaction_hash
        """
    ).fetchall()
    cache: dict[tuple[str, str], list[dict]] = {}
    for condition_id, asset, ts, price, size, side, outcome, transaction_hash in rows:
        cache.setdefault((condition_id, asset), []).append(
            {
                "timestamp_sec": int(ts),
                "price": float(price),
                "size": float(size or 0.0),
                "side": side,
                "outcome": outcome,
                "transaction_hash": transaction_hash,
            }
        )
    return cache


def _runner_parity_scan_trades(
    trades: list[dict],
    start_idx: int,
    bid: float,
    after_ts: int,
    through_ts: Optional[int],
    min_fill_size: float,
) -> tuple[Optional[dict], int, int]:
    idx = start_idx
    loose = 0
    while idx < len(trades) and trades[idx]["timestamp_sec"] < after_ts:
        idx += 1
    while idx < len(trades):
        trade = trades[idx]
        ts = trade["timestamp_sec"]
        if through_ts is not None and ts > through_ts:
            break
        idx += 1
        if trade["price"] > bid:
            continue
        if trade["size"] >= min_fill_size:
            return trade, idx, loose
        loose += 1
    return None, idx, loose


def _simulate_event(con, row, cfg: SimConfig) -> Optional[SimTrade]:
    trade, _attempt = _simulate_event_with_audit(con, row, cfg)
    return trade


def _simulate_event_with_audit(
    con,
    row,
    cfg: SimConfig,
    runner_trade_cache: Optional[dict[tuple[str, str], list[dict]]] = None,
    price_series_cache: Optional[dict[str, list[tuple[int, float]]]] = None,
) -> tuple[Optional[SimTrade], SimAttempt]:
    condition_id, title, tour_prefix, start_date, token_yes, token_no, sack_winner, sack_date = row
    start_ts = _to_ts(start_date)
    sack_year = str(sack_date or "")[:4]

    def audit(
        status: str,
        reason: str,
        *,
        ref_yes: Optional[float] = None,
        ref_no: Optional[float] = None,
        favorite_side: Optional[str] = None,
        favorite_token: Optional[str] = None,
        favorite_ref: Optional[float] = None,
        winner_side: Optional[str] = None,
        initial_bid: Optional[float] = None,
        final_bid: Optional[float] = None,
        cutoff_ts: Optional[int] = None,
        ticks_seen: int = 0,
        reprices: int = 0,
        loose_fills_seen: int = 0,
        prior_trade_count: Optional[int] = None,
        prior_price_range: Optional[float] = None,
        min_observed_price: Optional[float] = None,
        fill_ts: Optional[int] = None,
        fill_price: Optional[float] = None,
        fill_trade_size: Optional[float] = None,
        fill_trade_side: Optional[str] = None,
        fill_trade_outcome: Optional[str] = None,
        fill_transaction_hash: Optional[str] = None,
        exit_price: Optional[float] = None,
        pnl_usdc: Optional[float] = None,
        return_pct: Optional[float] = None,
    ) -> SimAttempt:
        return SimAttempt(
            condition_id=condition_id,
            title=title,
            sack_year=sack_year,
            start_ts=start_ts,
            ref_yes=ref_yes,
            ref_no=ref_no,
            favorite_side=favorite_side,
            favorite_token=favorite_token,
            favorite_ref=favorite_ref,
            winner_side=winner_side,
            initial_bid=initial_bid,
            final_bid=final_bid,
            cutoff_ts=cutoff_ts,
            fill_model=cfg.fill_model,
            ticks_seen=ticks_seen,
            reprices=reprices,
            loose_fills_seen=loose_fills_seen,
            prior_trade_count=prior_trade_count,
            prior_price_range=prior_price_range,
            min_observed_price=min_observed_price,
            fill_ts=fill_ts,
            fill_price=fill_price,
            fill_trade_size=fill_trade_size,
            fill_trade_side=fill_trade_side,
            fill_trade_outcome=fill_trade_outcome,
            fill_transaction_hash=fill_transaction_hash,
            exit_price=exit_price,
            pnl_usdc=pnl_usdc,
            return_pct=return_pct,
            status=status,
            reason=reason,
        )

    series = {}
    for side, token in (("yes", token_yes), ("no", token_no)):
        if price_series_cache is not None:
            series[side] = price_series_cache.get(token, [])
        else:
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
        return None, audit("skipped", "missing_price_history")

    ref_yes = _entry_reference_price(series["yes"], start_ts)
    ref_no = _entry_reference_price(series["no"], start_ts)
    if ref_yes is None or ref_no is None:
        return None, audit("skipped", "missing_reference_price", ref_yes=ref_yes, ref_no=ref_no)
    favorite_side, favorite_token, favorite_ref = (
        ("yes", token_yes, ref_yes) if ref_yes >= ref_no else ("no", token_no, ref_no)
    )
    if favorite_ref < cfg.min_favorite:
        return None, audit(
            "skipped",
            "favorite_below_min",
            ref_yes=ref_yes,
            ref_no=ref_no,
            favorite_side=favorite_side,
            favorite_token=favorite_token,
            favorite_ref=favorite_ref,
        )

    winner_side = _winner_side(title, sack_winner)
    if winner_side is None:
        return None, audit(
            "skipped",
            "winner_side_unmatched",
            ref_yes=ref_yes,
            ref_no=ref_no,
            favorite_side=favorite_side,
            favorite_token=favorite_token,
            favorite_ref=favorite_ref,
        )

    is_runner_parity = cfg.fill_model in {"runner_parity_trade", "filtered_runner_parity_trade"}
    if is_runner_parity:
        bid = compute_bid_price(favorite_ref, dip_pct=cfg.dip_pct, min_bid=cfg.min_bid)
    else:
        bid = max(favorite_ref * (1.0 - cfg.dip_pct), cfg.min_bid)
    initial_bid = bid
    points = _bucket_price_points(series[favorite_side], cfg.cycle_seconds)
    points = [(ts, price) for ts, price in points if start_ts is None or ts >= start_ts]
    if not points:
        return None, audit(
            "skipped",
            "no_price_points_after_start",
            ref_yes=ref_yes,
            ref_no=ref_no,
            favorite_side=favorite_side,
            favorite_token=favorite_token,
            favorite_ref=favorite_ref,
            winner_side=winner_side,
            initial_bid=initial_bid,
            final_bid=bid,
        )
    cutoff_ts = start_ts + cfg.max_position_minutes * 60 if start_ts is not None and cfg.max_position_minutes else None
    confirmed_trades = (
        _trade_confirmed_trades(con, condition_id, favorite_token, cutoff_ts)
        if cfg.fill_model == "trade_confirmed"
        else []
    )
    parity_trades = []
    if is_runner_parity:
        if runner_trade_cache is None:
            parity_trades = _runner_parity_trades(con, condition_id, favorite_token, cutoff_ts)
        else:
            parity_trades = [
                trade
                for trade in runner_trade_cache.get((condition_id, favorite_token), [])
                if cutoff_ts is None or trade["timestamp_sec"] <= cutoff_ts
            ]
        if start_ts is not None:
            prior_start_ts = start_ts - cfg.prior_window_minutes * 60
            prior_trades = [
                trade
                for trade in parity_trades
                if prior_start_ts <= trade["timestamp_sec"] <= start_ts
            ]
            prior_prices = [
                price
                for ts, price in series[favorite_side]
                if prior_start_ts <= ts <= start_ts
            ]
        else:
            prior_trades = []
            prior_prices = []
        prior_trade_count = len(prior_trades)
        prior_price_range = max(prior_prices) - min(prior_prices) if prior_prices else None
        if cfg.min_prior_trades and prior_trade_count < cfg.min_prior_trades:
            return None, audit(
                "skipped",
                "prior_trade_count_below_min",
                ref_yes=ref_yes,
                ref_no=ref_no,
                favorite_side=favorite_side,
                favorite_token=favorite_token,
                favorite_ref=favorite_ref,
                winner_side=winner_side,
                initial_bid=initial_bid,
                final_bid=bid,
                cutoff_ts=cutoff_ts,
                prior_trade_count=prior_trade_count,
                prior_price_range=prior_price_range,
            )
        if (
            cfg.min_prior_price_range is not None
            and (prior_price_range is None or prior_price_range < cfg.min_prior_price_range)
        ):
            return None, audit(
                "skipped",
                "prior_price_range_below_min",
                ref_yes=ref_yes,
                ref_no=ref_no,
                favorite_side=favorite_side,
                favorite_token=favorite_token,
                favorite_ref=favorite_ref,
                winner_side=winner_side,
                initial_bid=initial_bid,
                final_bid=bid,
                cutoff_ts=cutoff_ts,
                prior_trade_count=prior_trade_count,
                prior_price_range=prior_price_range,
            )
    else:
        prior_trade_count = None
        prior_price_range = None
    confirmed_trade_idx = 0
    parity_trade_idx = 0

    fill: Optional[tuple[int, float]] = None
    fill_trade: Optional[dict] = None
    ticks_seen = 0
    reprices = 0
    loose_fills_seen = 0
    min_observed_price: Optional[float] = None
    no_fill_reason = "no_fill_before_cutoff" if cutoff_ts is not None else "no_fill"
    order_active_ts = start_ts if start_ts is not None else points[0][0]
    last_trade_scan_ts = order_active_ts
    for ts, price in points:
        if cutoff_ts is not None and ts > cutoff_ts:
            no_fill_reason = "cutoff_reached"
            break
        ticks_seen += 1
        min_observed_price = price if min_observed_price is None else min(min_observed_price, price)
        if is_runner_parity:
            fill_trade, parity_trade_idx, loose = _runner_parity_scan_trades(
                parity_trades,
                parity_trade_idx,
                bid,
                last_trade_scan_ts,
                ts,
                cfg.min_fill_size,
            )
            loose_fills_seen += loose
            last_trade_scan_ts = ts + 1
            if fill_trade is not None:
                fill = (fill_trade["timestamp_sec"], fill_trade["price"])
                break
            if cfg.favorite_flip_cancel and price < 0.50:
                no_fill_reason = "favorite_flipped"
                break
            new_bid = compute_bid_price(price, dip_pct=cfg.dip_pct, min_bid=cfg.min_bid)
            effective_threshold = min(cfg.reprice_threshold, bid * 0.05)
            if abs(new_bid - bid) >= effective_threshold:
                bid = new_bid
                reprices += 1
            continue
        if cfg.fill_model == "trade_confirmed":
            fill, confirmed_trade_idx = _trade_confirmed_fill(
                confirmed_trades, confirmed_trade_idx, bid, ts
            )
            if fill is not None:
                break
        elif price <= bid:
            fill = (ts, min(price + cfg.modeled_half_spread, 0.999))
            break
        elif cfg.fill_model != "price_cross":
            raise ValueError(f"unsupported fill model: {cfg.fill_model}")
        new_bid = max(price * (1.0 - cfg.dip_pct), cfg.min_bid)
        if new_bid < bid and abs(new_bid - bid) >= cfg.reprice_threshold:
            bid = new_bid
            reprices += 1
    if fill is None:
        if is_runner_parity and no_fill_reason.startswith("no_fill"):
            fill_trade, parity_trade_idx, loose = _runner_parity_scan_trades(
                parity_trades,
                parity_trade_idx,
                bid,
                last_trade_scan_ts,
                cutoff_ts,
                cfg.min_fill_size,
            )
            loose_fills_seen += loose
            if fill_trade is not None:
                fill = (fill_trade["timestamp_sec"], fill_trade["price"])
        if fill is None:
            if loose_fills_seen and no_fill_reason.startswith("no_fill"):
                no_fill_reason = "only_loose_fills"
            return None, audit(
                "no_fill",
                no_fill_reason,
                ref_yes=ref_yes,
                ref_no=ref_no,
                favorite_side=favorite_side,
                favorite_token=favorite_token,
                favorite_ref=favorite_ref,
                winner_side=winner_side,
                initial_bid=initial_bid,
                final_bid=bid,
                cutoff_ts=cutoff_ts,
                ticks_seen=ticks_seen,
                reprices=reprices,
                loose_fills_seen=loose_fills_seen,
                prior_trade_count=prior_trade_count,
                prior_price_range=prior_price_range,
                min_observed_price=min_observed_price,
            )
    fill_ts, fill_price = fill
    fill_price = min(max(fill_price, 0.001), 0.999)
    exit_price = 1.0 if favorite_side == winner_side else 0.0
    pnl = (cfg.position_size_usdc / fill_price) * exit_price - cfg.position_size_usdc
    pnl -= _sports_fee(cfg.position_size_usdc, fill_price)
    side1, side2 = player_sides_from_title(title)
    favorite_label = side1 if favorite_side == "yes" else side2
    winner_label = side1 if winner_side == "yes" else side2
    trade = SimTrade(
        condition_id=condition_id,
        title=title,
        tour_prefix=tour_prefix or "",
        sack_year=sack_year,
        favorite_side=favorite_label or favorite_side,
        winner_side=winner_label or winner_side,
        entry_ts=fill_ts,
        fill_price=fill_price,
        exit_price=exit_price,
        pnl_usdc=pnl,
        return_pct=pnl / cfg.position_size_usdc,
    )
    return trade, audit(
        "filled",
        {
            "price_cross": "price_cross_fill",
            "trade_confirmed": "trade_confirmed_fill",
            "runner_parity_trade": "runner_parity_strict_fill",
            "filtered_runner_parity_trade": "runner_parity_strict_fill",
        }[cfg.fill_model],
        ref_yes=ref_yes,
        ref_no=ref_no,
        favorite_side=favorite_side,
        favorite_token=favorite_token,
        favorite_ref=favorite_ref,
        winner_side=winner_side,
        initial_bid=initial_bid,
        final_bid=bid,
        cutoff_ts=cutoff_ts,
        ticks_seen=ticks_seen,
        reprices=reprices,
        loose_fills_seen=loose_fills_seen,
        prior_trade_count=prior_trade_count,
        prior_price_range=prior_price_range,
        min_observed_price=min_observed_price,
        fill_ts=fill_ts,
        fill_price=fill_price,
        fill_trade_size=fill_trade["size"] if fill_trade else None,
        fill_trade_side=fill_trade["side"] if fill_trade else None,
        fill_trade_outcome=fill_trade["outcome"] if fill_trade else None,
        fill_transaction_hash=fill_trade["transaction_hash"] if fill_trade else None,
        exit_price=exit_price,
        pnl_usdc=pnl,
        return_pct=pnl / cfg.position_size_usdc,
    )


def _summarize_trades(trades: list[SimTrade]) -> dict:
    pnl = [trade.pnl_usdc for trade in trades]
    returns = [trade.return_pct for trade in trades]
    wins = sum(1 for trade in trades if trade.pnl_usdc > 0)
    gross_profit = sum(value for value in pnl if value > 0)
    gross_loss = -sum(value for value in pnl if value < 0)
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for trade in sorted(trades, key=lambda t: (t.entry_ts, t.condition_id)):
        equity += trade.pnl_usdc
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    out = {
        "fills": len(trades),
        "wins": wins,
        "losses": len(trades) - wins,
        "win_rate": wins / len(trades) if trades else 0.0,
        "total_pnl_usdc": sum(pnl),
        "avg_pnl_usdc": statistics.mean(pnl) if pnl else 0.0,
        "median_return_pct": statistics.median(returns) if returns else 0.0,
        "gross_profit_usdc": gross_profit,
        "gross_loss_usdc": gross_loss,
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "max_drawdown_usdc": max_drawdown,
    }
    return_stdev = statistics.stdev(returns) if len(returns) > 1 else 0.0
    if len(returns) >= 30 and return_stdev > 1e-9:
        out["return_stdev_pct"] = return_stdev
        out["return_t_stat"] = statistics.mean(returns) / (return_stdev / math.sqrt(len(returns)))
    else:
        out["return_stdev_pct"] = return_stdev
        out["return_t_stat"] = None
    return out


def run_sim(
    db_path: str | Path,
    cfg: SimConfig,
    limit: Optional[int] = None,
    since_year: Optional[int] = None,
    until_year: Optional[int] = None,
    tour: Optional[str] = None,
    include_audit: bool = False,
) -> dict:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        year_filters = []
        params = []
        if since_year is not None:
            year_filters.append("CAST(substr(a.sack_date, 1, 4) AS INTEGER) >= ?")
            params.append(since_year)
        if until_year is not None:
            year_filters.append("CAST(substr(a.sack_date, 1, 4) AS INTEGER) <= ?")
            params.append(until_year)
        if tour is not None:
            tour_key = tour.lower()
            if tour_key not in {"atp", "wta"}:
                raise ValueError(f"unsupported tour filter: {tour!r}")
            year_filters.append("LOWER(a.sack_tour) LIKE ?")
            params.append(f"{tour_key}%")
        year_clause = f"AND {' AND '.join(year_filters)}" if year_filters else ""
        rows = con.execute(
            f"""
            SELECT g.condition_id, g.title, g.tour_prefix, g.start_date,
                   g.token_id_yes, g.token_id_no, a.sack_winner, a.sack_date
            FROM gamma_events g
            JOIN event_alignment a USING(condition_id)
            WHERE EXISTS (SELECT 1 FROM prices_history p WHERE p.token_id = g.token_id_yes AND p.fidelity = 1)
              AND EXISTS (SELECT 1 FROM prices_history p WHERE p.token_id = g.token_id_no AND p.fidelity = 1)
              {year_clause}
            ORDER BY g.end_date, g.condition_id
            """,
            params,
        ).fetchall()
        if limit:
            rows = rows[:limit]
        trades = []
        attempts = []
        runner_trade_cache = (
            _load_runner_parity_trade_cache(con)
            if cfg.fill_model in {"runner_parity_trade", "filtered_runner_parity_trade"}
            else None
        )
        for row in rows:
            trade, attempt = _simulate_event_with_audit(con, row, cfg, runner_trade_cache=runner_trade_cache)
            if trade is not None:
                trades.append(trade)
            if include_audit:
                attempts.append(attempt)
        by_year = {}
        for year in sorted({trade.sack_year for trade in trades}):
            by_year[year] = _summarize_trades([trade for trade in trades if trade.sack_year == year])
        summary = {
            "config": asdict(cfg),
            "eligible_priced_events": len(rows),
            "since_year": since_year,
            "until_year": until_year,
            "tour": tour,
            **_summarize_trades(trades),
            "by_year": by_year,
            "sample_trades": [asdict(trade) for trade in trades[:10]],
        }
        if include_audit:
            summary["audit_events"] = [asdict(attempt) for attempt in attempts]
        return summary
    finally:
        con.close()


def parse_args(argv: Optional[list[str]] = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/backtest/stink_bid/backtest.db")
    parser.add_argument("--dip-pct", type=float, default=0.30)
    parser.add_argument("--min-favorite", type=float, default=0.55)
    parser.add_argument("--cycle-seconds", type=int, default=300)
    parser.add_argument("--position-size-usdc", type=float, default=25.0)
    parser.add_argument("--modeled-half-spread", type=float, default=0.005)
    parser.add_argument(
        "--fill-model",
        choices=["price_cross", "trade_confirmed", "runner_parity_trade", "filtered_runner_parity_trade"],
        default="price_cross",
    )
    parser.add_argument("--max-position-minutes", type=int, default=240)
    parser.add_argument("--min-fill-size", type=float, default=50.0)
    parser.add_argument("--no-favorite-flip-cancel", action="store_true")
    parser.add_argument("--min-prior-trades", type=int, default=0)
    parser.add_argument("--min-prior-price-range", type=float)
    parser.add_argument("--prior-window-minutes", type=int, default=60)
    parser.add_argument("--since-year", type=int)
    parser.add_argument("--until-year", type=int)
    parser.add_argument("--tour", choices=["atp", "wta"])
    parser.add_argument("--limit", type=int)
    parser.add_argument("--include-audit", action="store_true")
    parser.add_argument("--audit-output", help="Write the per-market audit ledger as JSONL.")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    args = parse_args(argv)
    cfg = SimConfig(
        dip_pct=args.dip_pct,
        min_favorite=args.min_favorite,
        cycle_seconds=args.cycle_seconds,
        position_size_usdc=args.position_size_usdc,
        modeled_half_spread=args.modeled_half_spread,
        fill_model=args.fill_model,
        max_position_minutes=args.max_position_minutes,
        min_fill_size=args.min_fill_size,
        favorite_flip_cancel=not args.no_favorite_flip_cancel,
        min_prior_trades=args.min_prior_trades,
        min_prior_price_range=args.min_prior_price_range,
        prior_window_minutes=args.prior_window_minutes,
    )
    summary = run_sim(
        args.db,
        cfg,
        limit=args.limit,
        since_year=args.since_year,
        until_year=args.until_year,
        tour=args.tour,
        include_audit=args.include_audit or bool(args.audit_output),
    )
    if args.audit_output:
        audit_events = summary.pop("audit_events", [])
        with open(args.audit_output, "w") as f:
            for row in audit_events:
                f.write(json.dumps(row, sort_keys=True) + "\n")
        summary["audit_output"] = args.audit_output
        summary["audit_events_written"] = len(audit_events)
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
