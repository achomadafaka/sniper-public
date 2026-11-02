#!/usr/bin/env python3
"""Score passive tennis strategy candidates before live paper deployment."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.simulate_post_dip_recovery import PostDipConfig, run_grid, run_sim


@dataclass
class ScoreboardConfig:
    min_entries: int = 10
    current_since_year: int = 2026


@dataclass
class ScoreboardRow:
    strategy_id: str
    status: str
    entries: int
    wins: int
    losses: int
    total_pnl_usdc: float
    profit_factor: Optional[float]
    current_entries: int
    robust_pnl_usdc: float
    command: str


def classify_status(
    *,
    entries: int,
    total_pnl: float,
    current_entries: int,
    min_entries: int,
) -> str:
    if entries < min_entries or total_pnl <= 0:
        return "reject"
    if current_entries >= 5 and entries >= min_entries * 3:
        return "paper"
    return "watch"


def rank_rows(rows: list[ScoreboardRow]) -> list[ScoreboardRow]:
    status_rank = {"paper": 2, "watch": 1, "reject": 0}
    return sorted(
        rows,
        key=lambda row: (
            status_rank.get(row.status, 0),
            row.robust_pnl_usdc,
            row.total_pnl_usdc,
            row.entries,
        ),
        reverse=True,
    )


def _best_or_empty(result: dict) -> Optional[dict]:
    return result.get("best") if result else None


def _row_from_best(
    *,
    strategy_id: str,
    best: dict,
    current_entries: int,
    robust_pnl_usdc: float,
    min_entries: int,
    command: str,
) -> ScoreboardRow:
    entries = int(best["entries"])
    total_pnl = float(best["total_pnl_usdc"])
    return ScoreboardRow(
        strategy_id=strategy_id,
        status=classify_status(
            entries=entries,
            total_pnl=total_pnl,
            current_entries=current_entries,
            min_entries=min_entries,
        ),
        entries=entries,
        wins=int(best["wins"]),
        losses=int(best["losses"]),
        total_pnl_usdc=total_pnl,
        profit_factor=best.get("profit_factor"),
        current_entries=current_entries,
        robust_pnl_usdc=robust_pnl_usdc,
        command=command,
    )


def run_scoreboard(db_path: str | Path, cfg: ScoreboardConfig) -> dict:
    candidates = [
        {
            "strategy_id": "post_dip_all",
            "grid": {
                "modes": ["post_dip"],
                "drop_pcts": [0.15, 0.20, 0.25, 0.30],
                "reclaim_pcts": [0.05],
                "reclaim_prices": [0.52],
                "reclaim_window_minutes": [30],
                "max_trigger_minutes": [60, 120, 240],
                "slippages": [0.03],
                "min_ref": 0.60,
                "max_ref": 0.90,
                "min_entries": cfg.min_entries,
            },
            "current_cfg": PostDipConfig(mode="post_dip", drop_pct=0.25, max_trigger_minutes=60, slippage=0.03),
            "command": "python3 scripts/simulate_post_dip_recovery.py --modes post_dip --slippage 0.03",
        },
        {
            "strategy_id": "favorite_flip_reclaim_atp",
            "grid": {
                "modes": ["favorite_flip_reclaim"],
                "drop_pcts": [0.20],
                "reclaim_pcts": [0.05],
                "reclaim_prices": [0.50],
                "reclaim_window_minutes": [120],
                "max_trigger_minutes": [240],
                "slippages": [0.03],
                "min_ref": 0.65,
                "max_ref": 0.90,
                "min_entries": 3,
                "tour": "atp",
            },
            "current_cfg": PostDipConfig(
                mode="favorite_flip_reclaim",
                drop_pct=0.20,
                reclaim_price=0.50,
                reclaim_window_minutes=120,
                max_trigger_minutes=240,
                min_ref=0.65,
                max_ref=0.90,
                slippage=0.03,
            ),
            "current_tour": "atp",
            "command": "python3 scripts/simulate_post_dip_recovery.py --modes favorite_flip_reclaim --tour atp --slippage 0.03",
        },
        {
            "strategy_id": "favorite_drop_fade_all",
            "grid": {
                "modes": ["favorite_drop_fade"],
                "drop_pcts": [0.10, 0.15, 0.20, 0.25, 0.30],
                "reclaim_pcts": [0.05],
                "reclaim_prices": [0.52],
                "reclaim_window_minutes": [30],
                "max_trigger_minutes": [60, 120, 240],
                "slippages": [0.03],
                "min_ref": 0.60,
                "max_ref": 0.90,
                "min_entries": cfg.min_entries,
            },
            "current_cfg": PostDipConfig(mode="favorite_drop_fade", drop_pct=0.20, max_trigger_minutes=120, slippage=0.03),
            "command": "python3 scripts/simulate_post_dip_recovery.py --modes favorite_drop_fade --slippage 0.03",
        },
        {
            "strategy_id": "favorite_flip_fade_all",
            "grid": {
                "modes": ["favorite_flip_fade"],
                "drop_pcts": [0.20],
                "reclaim_pcts": [0.05],
                "reclaim_prices": [0.52],
                "reclaim_window_minutes": [30],
                "max_trigger_minutes": [60, 120, 240],
                "slippages": [0.03],
                "min_ref": 0.60,
                "max_ref": 0.90,
                "min_entries": cfg.min_entries,
            },
            "current_cfg": PostDipConfig(mode="favorite_flip_fade", max_trigger_minutes=120, slippage=0.03),
            "command": "python3 scripts/simulate_post_dip_recovery.py --modes favorite_flip_fade --slippage 0.03",
        },
    ]
    rows: list[ScoreboardRow] = []
    for candidate in candidates:
        grid_args = dict(candidate["grid"])
        result = run_grid(db_path, **grid_args)
        best = _best_or_empty(result)
        if best is None:
            rows.append(
                ScoreboardRow(
                    strategy_id=candidate["strategy_id"],
                    status="reject",
                    entries=0,
                    wins=0,
                    losses=0,
                    total_pnl_usdc=0.0,
                    profit_factor=None,
                    current_entries=0,
                    robust_pnl_usdc=0.0,
                    command=candidate["command"],
                )
            )
            continue
        current = run_sim(
            db_path,
            candidate["current_cfg"],
            since_year=cfg.current_since_year,
            tour=candidate.get("current_tour"),
        )
        rows.append(
            _row_from_best(
                strategy_id=candidate["strategy_id"],
                best=best,
                current_entries=int(current["entries"]),
                robust_pnl_usdc=float(best["total_pnl_usdc"]),
                min_entries=cfg.min_entries,
                command=candidate["command"],
            )
        )
    ranked = rank_rows(rows)
    return {
        "config": asdict(cfg),
        "rows": [asdict(row) for row in ranked],
    }


def parse_args(argv: Optional[list[str]] = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/backtest/stink_bid/backtest.db")
    parser.add_argument("--min-entries", type=int, default=10)
    parser.add_argument("--current-since-year", type=int, default=2026)
    parser.add_argument("--output")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    args = parse_args(argv)
    out = run_scoreboard(
        args.db,
        ScoreboardConfig(
            min_entries=args.min_entries,
            current_since_year=args.current_since_year,
        ),
    )
    text = json.dumps(out, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
