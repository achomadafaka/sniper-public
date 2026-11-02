"""SQLite persistence for the Hyperlive engine — durable closed-cycle history + the latest
public state blob the UI polls."""
from __future__ import annotations

import json
import os
import sqlite3
import time


def connect(db_path: str) -> sqlite3.Connection:
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS hyperlive_state (
            id INTEGER PRIMARY KEY CHECK(id=1),
            updated_ts_ms INTEGER NOT NULL,
            state_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS hyperlive_cycles (
            cycle_id INTEGER,
            asset TEXT, setup TEXT, regime TEXT, reason TEXT,
            realized_bps REAL, won INTEGER, thesis TEXT,
            closed_ts_ms INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS hyperlive_clusters (
            asset TEXT PRIMARY KEY,
            side TEXT, distance_bps REAL, notional REAL,
            updated_ts_ms INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS hyperlive_cluster_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset TEXT NOT NULL, mid REAL,
            side TEXT, distance_bps REAL, notional REAL,
            ts_ms INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_cluster_hist_asset_ts
            ON hyperlive_cluster_history(asset, ts_ms);

        CREATE TABLE IF NOT EXISTS hl_cascade_obs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset TEXT NOT NULL, ts_ms INTEGER NOT NULL,
            mid REAL, oi REAL, n_trades INTEGER, notional REAL,
            UNIQUE(asset, ts_ms)
        );
        CREATE INDEX IF NOT EXISTS idx_cascade_obs_asset_ts ON hl_cascade_obs(asset, ts_ms);

        CREATE TABLE IF NOT EXISTS hl_cascade_events (
            event_id TEXT PRIMARY KEY,
            asset TEXT NOT NULL, ts_ms INTEGER NOT NULL, direction INTEGER,
            ret_w REAL, sigma_w REAL, oi_delta_w REAL,
            trade_count_w INTEGER, traded_notional_w REAL,
            magnitude_bucket TEXT, detector_version TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_cascade_events_ver_ts ON hl_cascade_events(detector_version, ts_ms);

        CREATE TABLE IF NOT EXISTS hl_cascade_audit_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_ms INTEGER NOT NULL, verdict_json TEXT NOT NULL
        );
    """)
    conn.commit()


def save_cluster_history(conn: sqlite3.Connection, sigs: dict, mids: dict, *, ts_ms: int) -> int:
    """Append-only time series of (asset, mid, cluster side/distance/notional) per poll — the
    spine of the cascade audit. Unlike hyperlive_clusters (latest-snapshot upsert), every poll
    is kept so forward moves can be measured against the cluster state at signal time."""
    payload = [(asset, mids.get(asset), side, dist, notional, int(ts_ms))
               for asset, (side, dist, notional) in sigs.items()]
    conn.executemany(
        "INSERT INTO hyperlive_cluster_history(asset, mid, side, distance_bps, notional, ts_ms)"
        " VALUES (?,?,?,?,?,?)", payload)
    conn.commit()
    return len(payload)


def save_clusters(conn: sqlite3.Connection, sigs: dict, *, ts_ms: int) -> None:
    for asset, (side, dist, notional) in sigs.items():
        conn.execute(
            "INSERT INTO hyperlive_clusters(asset, side, distance_bps, notional, updated_ts_ms)"
            " VALUES (?,?,?,?,?) ON CONFLICT(asset) DO UPDATE SET side=excluded.side,"
            " distance_bps=excluded.distance_bps, notional=excluded.notional, updated_ts_ms=excluded.updated_ts_ms",
            (asset, side, dist, notional, ts_ms))
    conn.commit()


def load_clusters(conn: sqlite3.Connection, *, now_ms: int, max_age_ms: int = 600_000) -> dict:
    try:
        rows = conn.execute(
            "SELECT asset, side, distance_bps FROM hyperlive_clusters WHERE updated_ts_ms >= ?",
            (now_ms - max_age_ms,)).fetchall()
    except sqlite3.Error:
        return {}
    return {r["asset"]: (r["side"], r["distance_bps"]) for r in rows}


def save_state(conn: sqlite3.Connection, state: dict, *, ts_ms: int | None = None) -> None:
    ts = ts_ms if ts_ms is not None else int(time.time() * 1000)
    conn.execute(
        "INSERT INTO hyperlive_state(id, updated_ts_ms, state_json) VALUES (1,?,?) "
        "ON CONFLICT(id) DO UPDATE SET updated_ts_ms=excluded.updated_ts_ms, state_json=excluded.state_json",
        (ts, json.dumps(state)))
    conn.commit()


def _ensure_track_column(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(hyperlive_cycles)").fetchall()}
    if "track" not in cols:
        conn.execute("ALTER TABLE hyperlive_cycles ADD COLUMN track TEXT DEFAULT 'all'")
        conn.commit()


def record_cycle(conn: sqlite3.Connection, close: dict, *, cycle_id: int, ts_ms: int | None = None) -> None:
    _ensure_track_column(conn)
    ts = ts_ms if ts_ms is not None else int(time.time() * 1000)
    conn.execute(
        "INSERT INTO hyperlive_cycles(cycle_id, track, asset, setup, regime, reason, realized_bps, won, thesis, closed_ts_ms)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (cycle_id, close.get("track", "all"), close.get("asset"), close.get("setup"), close.get("regime"), close.get("reason"),
         close.get("realized_bps"), 1 if close.get("won") else 0, close.get("thesis"), ts))
    conn.commit()


def load_state(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT state_json FROM hyperlive_state WHERE id=1").fetchone()
    return json.loads(row["state_json"]) if row else None


# --- cascade detector persistence (forward-only; see cascade detector design/plan) ---

def save_cascade_obs(conn: sqlite3.Connection, rows: dict, ts_ms: int) -> None:
    """rows: {asset: (mid, oi, n_trades, notional)}. Append-only raw per-bucket observations at a
    single ts. Idempotent on (asset, ts_ms) so a capture/watchdog restart can't double-count a
    bucket. The detector derives ret/sigma/oi-delta downstream."""
    conn.executemany(
        "INSERT OR IGNORE INTO hl_cascade_obs(asset, ts_ms, mid, oi, n_trades, notional) "
        "VALUES (?,?,?,?,?,?)",
        [(a, int(ts_ms), v[0], v[1], v[2], v[3]) for a, v in rows.items()])
    conn.commit()


def read_cascade_obs(conn: sqlite3.Connection, asset: str | None = None,
                     since_ms: int | None = None) -> list[dict]:
    q = "SELECT asset, ts_ms, mid, oi, n_trades, notional FROM hl_cascade_obs"
    cond, args = [], []
    if asset is not None:
        cond.append("asset=?"); args.append(asset)
    if since_ms is not None:
        cond.append("ts_ms>=?"); args.append(int(since_ms))
    if cond:
        q += " WHERE " + " AND ".join(cond)
    q += " ORDER BY ts_ms ASC, id ASC"
    return [dict(r) for r in conn.execute(q, args).fetchall()]


def save_cascade_event(conn: sqlite3.Connection, event: dict) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO hl_cascade_events
           (event_id, asset, ts_ms, direction, ret_w, sigma_w, oi_delta_w,
            trade_count_w, traded_notional_w, magnitude_bucket, detector_version)
           VALUES (:event_id,:asset,:ts_ms,:direction,:ret_w,:sigma_w,:oi_delta_w,
                   :trade_count_w,:traded_notional_w,:magnitude_bucket,:detector_version)""",
        event)
    conn.commit()


def read_cascade_events(conn: sqlite3.Connection, detector_version: str | None = None) -> list[dict]:
    q = ("SELECT event_id, asset, ts_ms, direction, ret_w, sigma_w, oi_delta_w, "
         "trade_count_w, traded_notional_w, magnitude_bucket, detector_version FROM hl_cascade_events")
    args = []
    if detector_version is not None:
        q += " WHERE detector_version=?"; args.append(detector_version)
    q += " ORDER BY ts_ms ASC"
    return [dict(r) for r in conn.execute(q, args).fetchall()]


def save_cascade_audit(conn: sqlite3.Connection, verdict: dict, ts_ms: int) -> None:
    conn.execute("INSERT INTO hl_cascade_audit_runs(ts_ms, verdict_json) VALUES (?,?)",
                 (int(ts_ms), json.dumps(verdict)))
    conn.commit()


def read_latest_cascade_audit(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT ts_ms, verdict_json FROM hl_cascade_audit_runs ORDER BY ts_ms DESC LIMIT 1").fetchone()
    if not row:
        return None
    return {"ts_ms": row["ts_ms"], "verdict": json.loads(row["verdict_json"])}
