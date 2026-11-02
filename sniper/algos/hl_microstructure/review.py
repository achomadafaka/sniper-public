"""Paper-performance review helpers for the Hyperliquid Market Radar."""
from __future__ import annotations

import json
import sqlite3

from sniper.algos.hl_microstructure import persistence as P


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    ).fetchone() is not None


def _rows(conn: sqlite3.Connection, sql: str, params=()) -> list[dict]:
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.Error:
        return []


def _one(conn: sqlite3.Connection, sql: str, params=(), default=0):
    try:
        row = conn.execute(sql, params).fetchone()
    except sqlite3.Error:
        return default
    return row[0] if row else default


def build_performance_report(conn: sqlite3.Connection) -> dict:
    summary = {
        "fusion_decisions": int(_one(conn, "SELECT COUNT(*) FROM hlm_decisions WHERE policy_id='fusion_radar'", default=0)),
        "fusion_entries": int(_one(conn, "SELECT COUNT(*) FROM hlm_decisions WHERE policy_id='fusion_radar' AND status='entered'", default=0)),
        "fusion_exits": int(_one(conn, "SELECT COUNT(*) FROM hlm_decisions WHERE policy_id='fusion_radar' AND status='exited'", default=0)),
        "fusion_open": int(_one(conn, "SELECT COUNT(*) FROM hlm_positions WHERE policy_id='fusion_radar' AND state='open'", default=0)),
        "fusion_realized_pnl": float(_one(conn, "SELECT COALESCE(SUM(realized_pnl),0) FROM hlm_positions WHERE policy_id='fusion_radar' AND state='closed'", default=0) or 0),
    }
    wins = int(_one(conn, "SELECT COUNT(*) FROM hlm_positions WHERE policy_id='fusion_radar' AND state='closed' AND realized_pnl > 0", default=0))
    closed = int(_one(conn, "SELECT COUNT(*) FROM hlm_positions WHERE policy_id='fusion_radar' AND state='closed' AND realized_pnl IS NOT NULL", default=0))
    summary["fusion_win_rate"] = (wins / closed) if closed else None

    radar_assets = []
    if _table_exists(conn, "hlm_radar_snapshots"):
        latest = _one(conn, "SELECT MAX(decision_ts_ms) FROM hlm_radar_snapshots", default=None)
        if latest:
            for r in _rows(conn, """
                SELECT asset, rank, score, reasons_json, volume_usdc, open_interest, funding, premium
                FROM hlm_radar_snapshots
                WHERE decision_ts_ms=?
                ORDER BY rank ASC
                LIMIT 10
            """, (latest,)):
                try:
                    reasons = json.loads(r.get("reasons_json") or "[]")
                except json.JSONDecodeError:
                    reasons = []
                radar_assets.append({**r, "reasons": reasons})

    by_asset = _rows(conn, """
        SELECT asset, COUNT(*) AS closed, COALESCE(SUM(realized_pnl),0) AS pnl
        FROM hlm_positions
        WHERE policy_id='fusion_radar' AND state='closed'
        GROUP BY asset
        ORDER BY pnl ASC
        LIMIT 10
    """)
    skip_reasons = _rows(conn, """
        SELECT reason, COUNT(*) AS n
        FROM hlm_decisions
        WHERE policy_id='fusion_radar' AND status='skipped'
        GROUP BY reason
        ORDER BY n DESC
        LIMIT 8
    """)
    try:
        expert_health = P.expert_summary(conn, limit=8)
    except sqlite3.Error:
        expert_health = []

    good = []
    bad = []
    improve = []
    if radar_assets:
        non_majors = [r["asset"] for r in radar_assets if r["asset"] not in ("BTC", "ETH", "SOL")]
        if non_majors:
            good.append("Radar is selecting non-major markets instead of hardcoding BTC/ETH/SOL.")
    if summary["fusion_entries"] > 0:
        good.append("Fusion lane is firing paper entries.")
    else:
        bad.append("Fusion lane has not entered any paper trades.")
        improve.append("Lower thresholds only after checking source-family votes; do not let one signal trade alone.")
    if summary["fusion_realized_pnl"] < 0:
        bad.append("Fusion closed PnL is negative.")
        improve.append("Review stop/target behavior by asset; tighten signal-decay exits before widening stops.")
    elif summary["fusion_realized_pnl"] > 0:
        good.append("Fusion closed PnL is positive so far.")
    if skip_reasons and skip_reasons[0]["reason"] == "needs_two_source_families":
        improve.append("Most skips need another agreeing source; prioritize catalyst and liquidation feed coverage.")
    if radar_assets and any("funding_extreme" in (r.get("reasons") or []) for r in radar_assets):
        improve.append("Stress names are still prominent; compare their paper exits against clean-liquidity names before raising the stress cap.")
    if not improve:
        improve.append("Keep collecting; current sample is not enough for a promotion claim.")

    return {
        "summary": summary,
        "radar_assets": radar_assets,
        "asset_pnl": by_asset,
        "skip_reasons": skip_reasons,
        "expert_health": expert_health,
        "good": good,
        "bad": bad,
        "improve": improve,
    }


def _usd(v) -> str:
    n = float(v or 0)
    return ("+" if n > 0 else "") + f"${n:.2f}"


def format_markdown_report(report: dict) -> str:
    s = report["summary"]
    radar = report.get("radar_assets") or []
    skips = report.get("skip_reasons") or []
    experts = report.get("expert_health") or []
    lines = [
        "# Hyperliquid Market Radar Paper Review",
        "",
        f"- Fusion decisions: {s['fusion_decisions']}",
        f"- Entries/exits/open: {s['fusion_entries']} / {s['fusion_exits']} / {s['fusion_open']}",
        f"- Closed PnL: {_usd(s['fusion_realized_pnl'])}",
        f"- Win rate: {'n/a' if s['fusion_win_rate'] is None else f'{100*s['fusion_win_rate']:.1f}%'}",
        "",
        "## Current radar",
    ]
    if radar:
        for r in radar[:8]:
            lines.append(f"- #{r['rank']} {r['asset']} score {float(r['score']):.1f}: {', '.join(r.get('reasons') or [])}")
    else:
        lines.append("- No radar snapshot yet.")
    lines.extend(["", "## What is working"])
    lines.extend([f"- {x}" for x in (report.get("good") or ["No clear positives yet."])])
    lines.extend(["", "## What is bad"])
    lines.extend([f"- {x}" for x in (report.get("bad") or ["No clear negatives yet."])])
    lines.extend(["", "## What to improve"])
    lines.extend([f"- {x}" for x in report.get("improve", [])])
    lines.extend(["", "## Top skip reasons"])
    if skips:
        lines.extend([f"- {r['reason']}: {r['n']}" for r in skips])
    else:
        lines.append("- No skips recorded.")
    lines.extend(["", "## Expert lab"])
    if experts:
        lines.extend([
            f"- {r['expert_id']} ({r['family']}): {int(r['settled'])} settled, "
            f"{100 * float(r['accuracy']):.1f}% hit, {10000 * float(r['avg_signed_return']):.2f} bps avg edge"
            for r in experts
        ])
    else:
        lines.append("- No settled expert predictions yet.")
    return "\n".join(lines) + "\n"
