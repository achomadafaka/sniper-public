#!/usr/bin/env python3
"""Summarize the live tennis inverse paper strategy state."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _json_loads(raw: str | None) -> dict:
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _load_state_summary(state_path: str | Path) -> dict:
    path = Path(state_path)
    if not path.exists():
        return {"path": str(path), "tracked_markets": 0}
    try:
        state = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {"path": str(path), "tracked_markets": 0, "error": "invalid_json"}
    if not isinstance(state, dict):
        return {"path": str(path), "tracked_markets": 0, "error": "invalid_shape"}
    return {
        "path": str(path),
        "tracked_markets": len(state),
        "sample_condition_ids": sorted(state)[:5],
    }


def summarize_inverse_paper(
    conn: sqlite3.Connection,
    *,
    strategy_id: int,
    state_path: str | Path,
) -> dict:
    conn.row_factory = sqlite3.Row
    strategy = conn.execute(
        """
        SELECT id, label, strategy_type, status, lifecycle_stage, parameters,
               pnl_usdc, trades_triggered, instance_tag
        FROM strategies
        WHERE id = ?
        """,
        (strategy_id,),
    ).fetchone()
    if strategy is None:
        raise ValueError(f"strategy_id={strategy_id} not found")

    rows = conn.execute(
        """
        SELECT position_id, condition_id, outcome, state, entry_price, current_price,
               size_usdc, pnl_usdc, opened_ts, updated_ts, instance_tag
        FROM strategy_positions
        WHERE strategy_id = ?
          AND COALESCE(instance_tag, '') = 'tennis-inverse-paper'
        ORDER BY updated_ts DESC, position_id DESC
        """,
        (str(strategy_id),),
    ).fetchall()
    open_rows = [row for row in rows if row["state"] in {"pending_entry", "open", "pending_exit"}]
    closed_rows = [row for row in rows if row["state"] == "closed"]
    open_pnl = sum(float(row["pnl_usdc"] or 0.0) for row in open_rows)
    closed_pnl = sum(float(row["pnl_usdc"] or 0.0) for row in closed_rows)
    return {
        "strategy": {
            "id": int(strategy["id"]),
            "label": strategy["label"],
            "strategy_type": strategy["strategy_type"],
            "status": strategy["status"],
            "lifecycle_stage": strategy["lifecycle_stage"],
            "instance_tag": strategy["instance_tag"],
            "parameters": _json_loads(strategy["parameters"]),
            "pnl_usdc": float(strategy["pnl_usdc"] or 0.0),
            "trades_triggered": int(strategy["trades_triggered"] or 0),
        },
        "positions": {
            "total_count": len(rows),
            "open_count": len(open_rows),
            "closed_count": len(closed_rows),
            "open_pnl_usdc": open_pnl,
            "closed_pnl_usdc": closed_pnl,
            "recent": [dict(row) for row in rows[:10]],
        },
        "state": _load_state_summary(state_path),
    }


def parse_args(argv: Optional[list[str]] = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="sniper_tracker.db")
    parser.add_argument("--strategy-id", type=int, default=44)
    parser.add_argument("--state", default="data/live_shadow/tennis_inverse_state_44.json")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    args = parse_args(argv)
    conn = sqlite3.connect(args.db, timeout=30)
    try:
        out = summarize_inverse_paper(conn, strategy_id=args.strategy_id, state_path=args.state)
    finally:
        conn.close()
    print(json.dumps(out, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
