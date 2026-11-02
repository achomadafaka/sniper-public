"""Copy viability scoring from signal_queue data.

Measures how copyable each wallet is: latency, fill rate, signal volume.
Data source is signal_queue only — no paper_trades bootstrap.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def measure_wallet_viability(conn: sqlite3.Connection, wallet: str) -> dict:
    """Compute copy viability metrics for a wallet from signal_queue data."""
    wallet_lower = wallet.strip().lower()

    rows = conn.execute(
        """
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN status = 'entered' THEN 1 ELSE 0 END) as entered,
            AVG(CASE WHEN status = 'entered' AND latency_ms IS NOT NULL
                THEN latency_ms END) as avg_latency
        FROM signal_queue
        WHERE LOWER(json_extract(metadata_json, '$.wallet')) = ?
        """,
        (wallet_lower,),
    ).fetchone()

    total = int(rows["total"] or 0)
    entered = int(rows["entered"] or 0)
    avg_latency = float(rows["avg_latency"] or 0)

    if total == 0:
        return {
            "wallet": wallet_lower,
            "signal_count": 0,
            "fill_rate": 0,
            "avg_latency_ms": 0,
            "avg_slippage_pct": 0,
            "score": 0,
        }

    fill_rate = entered / total
    if fill_rate == 0:
        return {
            "wallet": wallet_lower,
            "signal_count": total,
            "fill_rate": 0,
            "avg_latency_ms": 0,
            "avg_slippage_pct": 0,
            "score": 0,
        }
    latency_factor = max(0.0, 1.0 - (avg_latency / 5000.0))
    volume_factor = min(1.0, total / 50.0)
    score = round((fill_rate * 40) + (latency_factor * 30) + (volume_factor * 30), 2)

    return {
        "wallet": wallet_lower,
        "signal_count": total,
        "fill_rate": round(fill_rate, 4),
        "avg_latency_ms": round(avg_latency, 1),
        "avg_slippage_pct": 0,
        "score": score,
    }


def update_all_viability(conn: sqlite3.Connection) -> int:
    """Batch update copy_viability table for all wallets with signal_queue data."""
    wallets = conn.execute(
        """
        SELECT DISTINCT LOWER(json_extract(metadata_json, '$.wallet')) as wallet
        FROM signal_queue
        WHERE json_extract(metadata_json, '$.wallet') IS NOT NULL
          AND json_extract(metadata_json, '$.wallet') != ''
        """
    ).fetchall()

    count = 0
    for row in wallets:
        wallet = row["wallet"]
        if not wallet:
            continue
        metrics = measure_wallet_viability(conn, wallet)
        conn.execute(
            """
            INSERT INTO copy_viability (wallet, avg_latency_ms, avg_slippage_pct,
                                        fill_rate, signal_count, score, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(wallet) DO UPDATE SET
                avg_latency_ms = excluded.avg_latency_ms,
                avg_slippage_pct = excluded.avg_slippage_pct,
                fill_rate = excluded.fill_rate,
                signal_count = excluded.signal_count,
                score = excluded.score,
                updated_at = excluded.updated_at
            """,
            (wallet, metrics["avg_latency_ms"], metrics["avg_slippage_pct"],
             metrics["fill_rate"], metrics["signal_count"], metrics["score"]),
        )
        count += 1

    conn.commit()
    return count


def get_viability_leaderboard(
    conn: sqlite3.Connection, limit: int = 20
) -> List[Dict[str, Any]]:
    """Top wallets by viability score, descending."""
    rows = conn.execute(
        """
        SELECT wallet, avg_latency_ms, avg_slippage_pct, fill_rate,
               signal_count, score, updated_at
        FROM copy_viability
        ORDER BY score DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]
