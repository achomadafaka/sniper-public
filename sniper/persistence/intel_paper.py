"""Paper book for intel-driven candidates — both venues, one table pair.

Deliberately NOT `strategy_positions` (its `condition_id NOT NULL` / `outcome NOT NULL`
cannot represent a perp without fabricating a market identity — the audit F-01 error
class) and NOT `hl_paper_positions` (welded to the alt-short study's arm/obs schema;
intel rows would fill 6 of 35 columns and contaminate a live experiment).

Spec: docs/superpowers/specs/2026-07-29-intel-paper-testing-design.md
"""
from __future__ import annotations

import sqlite3
from typing import Any, Optional

_POSITIONS_DDL = """
CREATE TABLE IF NOT EXISTS intel_paper_positions (
    position_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    idea_id          INTEGER NOT NULL,
    venue            TEXT    NOT NULL,
    instrument       TEXT    NOT NULL,
    token_id         TEXT,
    event_title      TEXT,
    outcome_label    TEXT,
    direction        TEXT    NOT NULL,
    entry_price      REAL    NOT NULL,
    entry_ts         TEXT    NOT NULL,
    size_usdc        REAL    NOT NULL,
    status           TEXT    NOT NULL DEFAULT 'open',
    close_price      REAL,
    close_ts         TEXT,
    close_reason     TEXT,
    reviewer_verdict TEXT,
    reviewer_score   INTEGER,
    holder_snapshot  TEXT,
    neg_risk         INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_intel_paper_idea ON intel_paper_positions(idea_id);
CREATE INDEX IF NOT EXISTS idx_intel_paper_open ON intel_paper_positions(status, venue);

-- One open position per instrument, enforced by the DB rather than trusted to a
-- prompt. A second position on a market we already hold is never a new
-- observation: same direction is silent double-sizing, opposite direction is a
-- self-excluding pair that pays the spread twice to learn nothing. Partial index,
-- so the instrument frees up once the position closes.
CREATE UNIQUE INDEX IF NOT EXISTS idx_intel_paper_one_open_per_instrument
    ON intel_paper_positions(instrument) WHERE status = 'open';

CREATE TABLE IF NOT EXISTS intel_paper_marks (
    position_id INTEGER NOT NULL,
    ts          TEXT    NOT NULL,
    price       REAL    NOT NULL,
    pnl_pct     REAL    NOT NULL,
    PRIMARY KEY (position_id, ts)
);

-- Model opinion about an open position, recorded and displayed but NEVER executed.
-- An auto-close on model opinion would make the exit endogenous to the signal this
-- book exists to measure, so exits stay manual-or-resolution. Kept as a time series
-- so a recommendation can later be scored against what the price actually did.
CREATE TABLE IF NOT EXISTS intel_paper_recommendations (
    position_id INTEGER NOT NULL,
    ts          TEXT    NOT NULL,
    action      TEXT    NOT NULL,
    urgency     TEXT,
    reason      TEXT,
    source      TEXT,
    PRIMARY KEY (position_id, ts)
);
"""

_FIELDS = ("idea_id", "venue", "instrument", "token_id", "event_title", "outcome_label",
           "direction", "entry_price", "entry_ts", "size_usdc", "reviewer_verdict",
           "reviewer_score", "holder_snapshot", "neg_risk")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_POSITIONS_DDL)
    # Additive migrations for DBs created before a column existed.
    for col, decl in (("neg_risk", "INTEGER"),):
        try:
            conn.execute(f"ALTER TABLE intel_paper_positions ADD COLUMN {col} {decl}")
        except Exception:
            pass  # already present
    conn.commit()


def open_position(conn: sqlite3.Connection, **fields: Any) -> Optional[int]:
    """Insert a position. Returns position_id, or None if this idea already has one."""
    ensure_schema(conn)
    cols = ", ".join(_FIELDS)
    marks = ", ".join("?" for _ in _FIELDS)
    cur = conn.execute(
        f"INSERT OR IGNORE INTO intel_paper_positions ({cols}) VALUES ({marks})",
        tuple(fields.get(f) for f in _FIELDS),
    )
    conn.commit()
    return cur.lastrowid if cur.rowcount else None


def record_mark(conn: sqlite3.Connection, position_id: int, ts: str,
                price: float, pnl_pct: float) -> None:
    ensure_schema(conn)
    conn.execute(
        "INSERT OR REPLACE INTO intel_paper_marks (position_id, ts, price, pnl_pct) "
        "VALUES (?,?,?,?)",
        (position_id, ts, float(price), float(pnl_pct)),
    )
    conn.commit()


def apply_reviewer_verdict(conn: sqlite3.Connection, idea_id: int,
                           verdict: str, score: Optional[int] = None) -> bool:
    """Attach a reviewer verdict to this idea's position once the review lands.

    Positions open before the reviewer runs, so the verdict is unknowable at entry and
    every row starts 'pending'. Without this the verdict column stays 'pending' forever
    and the approved-vs-rejected comparison the book exists for can never be made.

    Only overwrites 'pending': the first real verdict for a position is final, so a
    later re-review cannot quietly reclassify a cohort mid-experiment.
    """
    ensure_schema(conn)
    cur = conn.execute(
        "UPDATE intel_paper_positions SET reviewer_verdict=?, reviewer_score=? "
        "WHERE idea_id=? AND (reviewer_verdict IS NULL OR reviewer_verdict='pending')",
        (verdict, score, idea_id),
    )
    conn.commit()
    return bool(cur.rowcount)


def record_recommendation(conn: sqlite3.Connection, position_id: int, ts: str,
                          action: str, reason: Optional[str] = None,
                          urgency: Optional[str] = None,
                          source: Optional[str] = None) -> None:
    """Record a model's hold/close opinion. Advisory only — nothing acts on this."""
    ensure_schema(conn)
    conn.execute(
        "INSERT OR REPLACE INTO intel_paper_recommendations "
        "(position_id, ts, action, urgency, reason, source) VALUES (?,?,?,?,?,?)",
        (position_id, ts, action, urgency, reason, source),
    )
    conn.commit()


def open_positions(conn: sqlite3.Connection, venue: Optional[str] = None) -> list[dict]:
    """Open positions with their most recent mark and recommendation (None when absent)."""
    ensure_schema(conn)
    sql = """
        SELECT p.*, m.price AS last_mark_price, m.ts AS last_mark_ts,
               m.pnl_pct AS last_pnl_pct,
               r.action AS last_recommendation,
               r.reason AS last_recommendation_reason,
               r.urgency AS last_recommendation_urgency,
               r.ts AS last_recommendation_ts,
               CASE WHEN p.event_title IS NULL THEN 1 ELSE (
                   SELECT COUNT(*) FROM intel_paper_positions s
                   WHERE s.status = 'open' AND s.event_title = p.event_title
               ) END AS event_position_count
        FROM intel_paper_positions p
        LEFT JOIN intel_paper_marks m
          ON m.position_id = p.position_id
         AND m.ts = (SELECT MAX(ts) FROM intel_paper_marks WHERE position_id = p.position_id)
        LEFT JOIN intel_paper_recommendations r
          ON r.position_id = p.position_id
         AND r.ts = (SELECT MAX(ts) FROM intel_paper_recommendations
                      WHERE position_id = p.position_id)
        WHERE p.status = 'open'
    """
    params: tuple = ()
    if venue:
        sql += " AND p.venue = ?"
        params = (venue,)
    sql += " ORDER BY p.entry_ts DESC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def closed_positions(conn: sqlite3.Connection, venue: Optional[str] = None,
                     limit: int = 100) -> list[dict]:
    """Closed positions, newest first.

    Returns raw rows — PnL is NOT computed here. `intel_paper_mark.pnl_pct` inverts the
    sign for HL shorts, so doing the arithmetic in SQL would silently report a
    profitable short as a loss.
    """
    ensure_schema(conn)
    sql = "SELECT * FROM intel_paper_positions WHERE status = 'closed'"
    params: tuple = ()
    if venue:
        sql += " AND venue = ?"
        params = (venue,)
    sql += " ORDER BY close_ts DESC LIMIT ?"
    return [dict(r) for r in conn.execute(sql, params + (limit,)).fetchall()]


def close_position(conn: sqlite3.Connection, position_id: int, price: float,
                   ts: str, reason: str) -> None:
    ensure_schema(conn)
    conn.execute(
        "UPDATE intel_paper_positions SET status='closed', close_price=?, close_ts=?, "
        "close_reason=? WHERE position_id=?",
        (float(price), ts, reason, position_id),
    )
    conn.commit()
