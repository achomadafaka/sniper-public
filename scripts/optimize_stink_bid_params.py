#!/usr/bin/env python3
"""Grid-search stink-bid simulator parameters.

This intentionally stays small and deterministic: it reuses the smoke simulator
engine, evaluates a Cartesian product of parameter values, and ranks results by
one summary metric. It is the first optimization harness, not a full research
runner.
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import duckdb

from scripts.simulate_stink_bid_smoke import (
    SimConfig,
    _load_runner_parity_trade_cache,
    _simulate_event_with_audit,
    _summarize_trades,
    run_sim,
)


@dataclass
class CachedSimDataset:
    rows: list[tuple]
    price_series_cache: dict[str, list[tuple[int, float]]]
    runner_trade_cache: dict[tuple[str, str], list[dict]]


def parse_number_grid(raw: str, cast=float) -> list:
    """Parse comma values or inclusive start:stop:step ranges."""
    if ":" in raw:
        parts = [part.strip() for part in raw.split(":")]
        if len(parts) != 3:
            raise ValueError(f"range must be start:stop:step, got {raw!r}")
        start, stop, step = (float(part) for part in parts)
        if step <= 0:
            raise ValueError("range step must be positive")
        values = []
        value = start
        epsilon = step / 1_000_000
        while value <= stop + epsilon:
            values.append(cast(round(value, 10)))
            value += step
        return values
    return [cast(part.strip()) for part in raw.split(",") if part.strip()]


def parse_optional_number_grid(raw: str, cast=float) -> list:
    values = []
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        values.append(None if item.lower() in {"none", "null"} else cast(item))
    return values


def _score_value(result: dict, objective: str) -> float:
    value = result.get(objective)
    if value is None:
        return float("-inf")
    return float(value)


def _runner_parity_model(fill_model: str) -> bool:
    return fill_model in {"runner_parity_trade", "filtered_runner_parity_trade"}


def _load_cached_dataset(
    con,
    *,
    since_year: Optional[int] = None,
    until_year: Optional[int] = None,
    tour: Optional[str] = None,
    limit: Optional[int] = None,
) -> CachedSimDataset:
    filters = []
    params = []
    if since_year is not None:
        filters.append("CAST(substr(a.sack_date, 1, 4) AS INTEGER) >= ?")
        params.append(since_year)
    if until_year is not None:
        filters.append("CAST(substr(a.sack_date, 1, 4) AS INTEGER) <= ?")
        params.append(until_year)
    if tour is not None:
        tour_key = tour.lower()
        if tour_key not in {"atp", "wta"}:
            raise ValueError(f"unsupported tour filter: {tour!r}")
        filters.append("LOWER(a.sack_tour) LIKE ?")
        params.append(f"{tour_key}%")
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
    token_ids = sorted({row[4] for row in rows} | {row[5] for row in rows})
    price_series_cache: dict[str, list[tuple[int, float]]] = {token_id: [] for token_id in token_ids}
    if token_ids:
        placeholders = ", ".join("?" for _ in token_ids)
        price_rows = con.execute(
            f"""
            SELECT token_id, timestamp_sec, price
            FROM prices_history
            WHERE fidelity = 1
              AND token_id IN ({placeholders})
            ORDER BY token_id, timestamp_sec
            """,
            token_ids,
        ).fetchall()
        for token_id, ts, price in price_rows:
            price_series_cache.setdefault(token_id, []).append((int(ts), float(price)))
    return CachedSimDataset(
        rows=rows,
        price_series_cache=price_series_cache,
        runner_trade_cache=_load_runner_parity_trade_cache(con),
    )


def _cached_summary(con, dataset: CachedSimDataset, cfg: SimConfig) -> dict:
    trades = []
    for row in dataset.rows:
        trade, _attempt = _simulate_event_with_audit(
            con,
            row,
            cfg,
            runner_trade_cache=dataset.runner_trade_cache,
            price_series_cache=dataset.price_series_cache,
        )
        if trade is not None:
            trades.append(trade)
    return {"eligible_priced_events": len(dataset.rows), **_summarize_trades(trades)}


def run_grid(
    db_path: str | Path,
    dip_pcts: list[float],
    min_favorites: list[float],
    cycle_seconds: list[int],
    max_position_minutes: list[int],
    fill_model: str,
    objective: str,
    min_fills: int = 0,
    since_year: Optional[int] = None,
    until_year: Optional[int] = None,
    tour: Optional[str] = None,
    limit: Optional[int] = None,
    min_prior_trades: Optional[list[int]] = None,
    min_prior_price_ranges: Optional[list[Optional[float]]] = None,
    prior_window_minutes: Optional[list[int]] = None,
) -> dict:
    runs = []
    total_runs = 0
    min_prior_trades = min_prior_trades or [0]
    min_prior_price_ranges = min_prior_price_ranges or [None]
    prior_window_minutes = prior_window_minutes or [60]
    con = duckdb.connect(str(db_path), read_only=True) if _runner_parity_model(fill_model) else None
    try:
        dataset = (
            _load_cached_dataset(
                con,
                since_year=since_year,
                until_year=until_year,
                tour=tour,
                limit=limit,
            )
            if con is not None
            else None
        )
        for dip_pct, min_favorite, cycle, max_minutes, prior_trades, prior_range, prior_window in itertools.product(
            dip_pcts,
            min_favorites,
            cycle_seconds,
            max_position_minutes,
            min_prior_trades,
            min_prior_price_ranges,
            prior_window_minutes,
        ):
            total_runs += 1
            cfg = SimConfig(
                dip_pct=dip_pct,
                min_favorite=min_favorite,
                cycle_seconds=cycle,
                max_position_minutes=max_minutes,
                fill_model=fill_model,
                min_prior_trades=prior_trades,
                min_prior_price_range=prior_range,
                prior_window_minutes=prior_window,
            )
            summary = (
                _cached_summary(con, dataset, cfg)
                if dataset is not None
                else run_sim(
                    db_path,
                    cfg,
                    limit=limit,
                    since_year=since_year,
                    until_year=until_year,
                    tour=tour,
                )
            )
            row = {
                "config": asdict(cfg),
                "eligible_priced_events": summary["eligible_priced_events"],
                "fills": summary["fills"],
                "wins": summary["wins"],
                "losses": summary["losses"],
                "win_rate": summary["win_rate"],
                "total_pnl_usdc": summary["total_pnl_usdc"],
                "avg_pnl_usdc": summary["avg_pnl_usdc"],
                "profit_factor": summary["profit_factor"],
                "max_drawdown_usdc": summary["max_drawdown_usdc"],
                "return_t_stat": summary["return_t_stat"],
            }
            if row["fills"] >= min_fills:
                row["objective_value"] = _score_value(row, objective)
                runs.append(row)
    finally:
        if con is not None:
            con.close()
    runs.sort(key=lambda row: (row["objective_value"], row["fills"]), reverse=True)
    return {
        "objective": objective,
        "min_fills": min_fills,
        "since_year": since_year,
        "until_year": until_year,
        "tour": tour,
        "runs_evaluated": total_runs,
        "runs_after_min_fills": len(runs),
        "best": runs[0] if runs else None,
        "runs": runs,
    }


def parse_args(argv: Optional[list[str]] = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/backtest/stink_bid/backtest.db")
    parser.add_argument("--dip-pcts", default="0.2,0.3,0.4")
    parser.add_argument("--min-favorites", default="0.55,0.6,0.65")
    parser.add_argument("--cycle-seconds", default="300")
    parser.add_argument("--max-position-minutes", default="240")
    parser.add_argument(
        "--fill-model",
        choices=["price_cross", "trade_confirmed", "runner_parity_trade", "filtered_runner_parity_trade"],
        default="price_cross",
    )
    parser.add_argument(
        "--objective",
        choices=["total_pnl_usdc", "avg_pnl_usdc", "profit_factor", "return_t_stat"],
        default="total_pnl_usdc",
    )
    parser.add_argument("--min-fills", type=int, default=0)
    parser.add_argument("--min-prior-trades", default="0")
    parser.add_argument("--min-prior-price-ranges", default="none")
    parser.add_argument("--prior-window-minutes", default="60")
    parser.add_argument("--since-year", type=int)
    parser.add_argument("--until-year", type=int)
    parser.add_argument("--tour", choices=["atp", "wta"])
    parser.add_argument("--limit", type=int)
    parser.add_argument("--top", type=int, default=10)
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    args = parse_args(argv)
    out = run_grid(
        db_path=args.db,
        dip_pcts=parse_number_grid(args.dip_pcts, float),
        min_favorites=parse_number_grid(args.min_favorites, float),
        cycle_seconds=parse_number_grid(args.cycle_seconds, int),
        max_position_minutes=parse_number_grid(args.max_position_minutes, int),
        fill_model=args.fill_model,
        objective=args.objective,
        min_fills=args.min_fills,
        since_year=args.since_year,
        until_year=args.until_year,
        tour=args.tour,
        limit=args.limit,
        min_prior_trades=parse_number_grid(args.min_prior_trades, int),
        min_prior_price_ranges=parse_optional_number_grid(args.min_prior_price_ranges, float),
        prior_window_minutes=parse_number_grid(args.prior_window_minutes, int),
    )
    out["runs"] = out["runs"][: args.top]
    print(json.dumps(out, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
