"""data/desk/attention/tape.db: the tape-side attention writer's own file (one file per writer; the runner has its own,
Beta's runner.db). The attention table is IDENTICAL to Beta's (memecoin_desk_attention.DDL) because his auto-tighten reads
both files with one query. Append-only, INSERT OR IGNORE by a deterministic id, so a restart or a re-scan can never write the
same event twice. fired: 1 = a fire under a frozen thresholds version, NULL = measured but dry (thresholds unset) or an
'unknown' row (value_json then names the reason). Also holds the tape price table for Beta's exact-vs-tape quote form."""
from __future__ import annotations

import json
import sqlite3

DDL = (
    "CREATE TABLE IF NOT EXISTS attention(id TEXT PRIMARY KEY, position_id INTEGER NOT NULL, mint TEXT, "
    "signal TEXT NOT NULL, value_json TEXT, observed_at REAL NOT NULL, source TEXT NOT NULL, fired INTEGER, "
    "thresholds_version TEXT)",
    "CREATE INDEX IF NOT EXISTS attention_position ON attention(position_id, observed_at)",
    "CREATE TABLE IF NOT EXISTS health(id INTEGER PRIMARY KEY CHECK(id=1), ts REAL NOT NULL, tape_ok INTEGER NOT NULL, "
    "reason TEXT, receipt_cut REAL, positions INTEGER NOT NULL, inserted INTEGER NOT NULL, dropped INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS tape_price(mint TEXT PRIMARY KEY, price_sol REAL NOT NULL, price_usd REAL NOT NULL, "
    "slot INTEGER NOT NULL, signature TEXT NOT NULL, received_at REAL NOT NULL, receipt_cut REAL NOT NULL, "
    "updated_at REAL NOT NULL)",
)


def connect(path):
    conn = sqlite3.connect(str(path), timeout=2)
    conn.execute("PRAGMA journal_mode=WAL")
    for stmt in DDL:
        conn.execute(stmt)
    return conn


def insert(conn, *, event_id, position_id, mint, signal, value, observed_at, source, fired, thresholds_version):
    """True when a new row was written; False when the id already existed. NaN in `value` is refused (ValueError)."""
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    cur = conn.execute("INSERT OR IGNORE INTO attention VALUES(?,?,?,?,?,?,?,?,?)",
                       (event_id, position_id, mint, signal, body, observed_at, source, fired, thresholds_version))
    return cur.rowcount == 1


def set_price(conn, *, mint, price_sol, price_usd, slot, signature, received_at, receipt_cut, updated_at):
    conn.execute("INSERT INTO tape_price VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(mint) DO UPDATE SET price_sol=excluded.price_sol, "
                 "price_usd=excluded.price_usd, slot=excluded.slot, signature=excluded.signature, "
                 "received_at=excluded.received_at, receipt_cut=excluded.receipt_cut, updated_at=excluded.updated_at",
                 (mint, price_sol, price_usd, slot, signature, received_at, receipt_cut, updated_at))


def set_health(conn, *, ts, tape_ok, reason, receipt_cut, positions, inserted, dropped):
    conn.execute("INSERT INTO health VALUES(1,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET ts=excluded.ts, "
                 "tape_ok=excluded.tape_ok, reason=excluded.reason, receipt_cut=excluded.receipt_cut, "
                 "positions=excluded.positions, inserted=excluded.inserted, dropped=excluded.dropped",
                 (ts, int(tape_ok), reason, receipt_cut, positions, inserted, dropped))
