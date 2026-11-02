#!/usr/bin/env python3
"""Read-only measurement-honesty report for the memecoin paper ledger."""
from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path

import sys as _sys, pathlib as _pathlib   # a bare `python3 scripts/memecoin_measurement_audit.py` puts this file's
_R = str(_pathlib.Path(__file__).resolve().parents[1])  # own dir on sys.path,
if _R not in _sys.path:                    # never the repo root
    _sys.path.insert(0, _R)
from sniper.persistence import db_memecoin as m


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / os.getenv("DB_PATH", "sniper_tracker.db")


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params)]


def _safe_sell_coverage(conn: sqlite3.Connection) -> dict:
    try:
        return m.sell_coverage_summary(conn)
    except sqlite3.Error:
        return {"total": 0, "seen": 0, "missed": 0}


def build_report(conn: sqlite3.Connection) -> dict:
    """Return raw evidence only. No verdicts, no DB writes."""
    open_partial = _rows(conn, """
        SELECT p.id AS position_id, p.strategy, p.source_user_id, p.source_handle, p.mint,
               p.ticker, p.entry_price, p.entry_notional,
               COALESCE(SUM(f.qty), 0) AS partial_qty,
               COALESCE(SUM((f.price - p.entry_price) * f.qty), 0) AS booked_partial_pnl
        FROM memecoin_positions p
        JOIN memecoin_fills f ON f.position_id = p.id AND f.kind='partial_exit'
        WHERE p.is_open=1
        GROUP BY p.id
        ORDER BY ABS(booked_partial_pnl) DESC, p.id
    """)
    open_unpriced = _rows(conn, """
        SELECT id AS position_id, strategy, source_user_id, source_handle, mint, ticker,
               entry_price, entry_notional, opened_at
        FROM memecoin_positions
        WHERE is_open=1 AND (last_quote_price IS NULL OR last_quote_price <= 0)
        ORDER BY opened_at DESC, id DESC
    """)
    arm_summary = {r["strategy"]: r for r in m.strategy_summary(conn)}
    return {
        "open_partial_exit_positions": open_partial,
        "open_unpriced_positions": open_unpriced,
        "sell_coverage": _safe_sell_coverage(conn),
        "arm_summary": arm_summary,
    }


def _money(v) -> str:
    return f"${float(v or 0.0):+.2f}"


def render_report(report: dict) -> str:
    partial = report.get("open_partial_exit_positions") or []
    unpriced = report.get("open_unpriced_positions") or []
    cov = report.get("sell_coverage") or {}
    arms = report.get("arm_summary") or {}
    lines = [
        "Memecoin measurement audit",
        f"open partial-exit positions: {len(partial)}",
        f"open unpriced positions: {len(unpriced)}",
        "sell coverage: "
        f"{int(cov.get('seen') or 0)} seen / {int(cov.get('missed') or 0)} missed / "
        f"{int(cov.get('total') or 0)} total",
        "",
        "Arms:",
    ]
    for name in sorted(arms):
        r = arms[name]
        lines.append(
            f"- {name}: total {_money(r.get('total_pnl'))}, "
            f"conservative {_money(r.get('conservative_total_pnl'))}, "
            f"open {_money(r.get('unrealized_pnl'))}, "
            f"open-realized {_money(r.get('open_realized_pnl'))}, "
            f"partial marks {int(r.get('unrealized_partial') or 0)}, "
            f"noquote {int(r.get('noquote_closes') or 0)}, "
            f"stale {int(r.get('stale_closes') or 0)}"
        )
    return "\n".join(lines)


def _connect_readonly(path: Path) -> sqlite3.Connection:
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite DB path")
    args = parser.parse_args(argv)
    with _connect_readonly(Path(args.db)) as conn:
        print(render_report(build_report(conn)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
