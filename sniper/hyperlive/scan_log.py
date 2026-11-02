"""Step-0 perishable recall log — one row per asset per refresh, for ALL ~230 markets.
Append-only, known-at-time (same discipline as hlm_raw_events). The spine of the recall
audit: every refresh not logged is data we can never reconstruct. Shadow only — logging this
does not touch live selection."""
from __future__ import annotations

import json
import os
import sqlite3


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
        CREATE TABLE IF NOT EXISTS hyperlive_scan_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_ts INTEGER NOT NULL,
            asset TEXT NOT NULL,
            selected_rank INTEGER,
            selected_bool INTEGER,
            already_watched_bool INTEGER,
            hand_score_v0 REAL,
            change_score_v0 REAL,
            score_components TEXT,        -- live opportunity components (JSON)
            change_components TEXT,       -- Step-1 shadow rate/accel components (JSON)
            lead_reason TEXT,
            day_ntl_vlm REAL,
            mark_px REAL, oracle_px REAL, mid_px REAL,
            funding REAL, premium REAL, prev_day_px REAL,
            open_interest REAL, prior_open_interest REAL, oi_delta REAL,
            refresh_interval_s INTEGER,
            watchlist_state TEXT,
            binance_basis_bps REAL        -- (binance_mid - hl_mid)/hl_mid in bps, contemporaneous; NULL if unmapped
        );
        CREATE INDEX IF NOT EXISTS idx_scan_log_ts ON hyperlive_scan_log(scan_ts);
        CREATE INDEX IF NOT EXISTS idx_scan_log_asset_ts ON hyperlive_scan_log(asset, scan_ts);
    """)
    # additive migration for logs created before the cross-venue column existed
    cols = {r[1] for r in conn.execute("PRAGMA table_info(hyperlive_scan_log)")}
    if "binance_basis_bps" not in cols:
        conn.execute("ALTER TABLE hyperlive_scan_log ADD COLUMN binance_basis_bps REAL")
    conn.commit()


def log_refresh(conn: sqlite3.Connection, scan_ts: int, rows: list[dict], *,
                refresh_interval_s: int, watchlist_state: str = "") -> int:
    payload = []
    for r in rows:
        payload.append((
            int(scan_ts), r["asset"], r.get("selected_rank"),
            1 if r.get("selected_bool") else 0, 1 if r.get("already_watched_bool") else 0,
            r.get("hand_score_v0"), r.get("change_score_v0"),
            json.dumps(r.get("score_components") or {}), json.dumps(r.get("change_components") or {}),
            r.get("lead_reason"),
            r.get("day_ntl_vlm"), r.get("mark_px"), r.get("oracle_px"), r.get("mid_px"),
            r.get("funding"), r.get("premium"), r.get("prev_day_px"),
            r.get("open_interest"), r.get("prior_open_interest"), r.get("oi_delta"),
            int(refresh_interval_s), watchlist_state, r.get("binance_basis_bps"),
        ))
    conn.executemany(
        """INSERT INTO hyperlive_scan_log
           (scan_ts, asset, selected_rank, selected_bool, already_watched_bool,
            hand_score_v0, change_score_v0, score_components, change_components, lead_reason,
            day_ntl_vlm, mark_px, oracle_px, mid_px, funding, premium, prev_day_px,
            open_interest, prior_open_interest, oi_delta, refresh_interval_s, watchlist_state,
            binance_basis_bps)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        payload)
    conn.commit()
    return len(payload)
