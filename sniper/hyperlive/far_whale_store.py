"""SQLite persistence for the fwlm-v0 far-whale magnet paper study (spec §10).
Forward-accruing, hyperlive_engine.sqlite. Episodes (§3.1) are the unit of observation."""
from __future__ import annotations

import json
import sqlite3

from sniper.hyperlive.far_whale import EPISODE_GAP_MS, EPISODE_LIQ_TOL_BPS, FEE_BPS, STUDY_VERSION
from sniper.hyperlive.persistence import connect  # noqa: F401  (re-export)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS hl_far_whale_liq_market_state (
            asset TEXT NOT NULL, ts_ms INTEGER NOT NULL,
            mid REAL NOT NULL, oi REAL, day_ntl_vlm REAL, spread_bps REAL,
            UNIQUE(asset, ts_ms)
        );
        CREATE INDEX IF NOT EXISTS idx_fwlm_ms_asset_ts
            ON hl_far_whale_liq_market_state(asset, ts_ms);

        CREATE TABLE IF NOT EXISTS hl_far_whale_liq_rounds (
            ts_ms INTEGER PRIMARY KEY,
            addresses_polled INTEGER, whales_seen INTEGER,
            anomalies_dropped INTEGER, candidates_new INTEGER, sweep_mode INTEGER
        );

        CREATE TABLE IF NOT EXISTS hl_far_whale_liq_position_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_ms INTEGER NOT NULL, address TEXT NOT NULL, asset TEXT NOT NULL,
            liquidation_price REAL NOT NULL, position_notional REAL NOT NULL,
            szi REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_fwlm_snap_addr_ts
            ON hl_far_whale_liq_position_snapshots(address, asset, ts_ms);

        CREATE TABLE IF NOT EXISTS hl_far_whale_liq_episodes (
            episode_id TEXT PRIMARY KEY,
            asset TEXT NOT NULL, whale_address TEXT NOT NULL,
            ref_liquidation_price REAL NOT NULL,
            first_ts_ms INTEGER NOT NULL, last_seen_ts_ms INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'open', n_candidates INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_fwlm_ep_addr
            ON hl_far_whale_liq_episodes(asset, whale_address, status);

        CREATE TABLE IF NOT EXISTS hl_far_whale_liq_candidates (
            candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
            episode_id TEXT NOT NULL, study_version TEXT NOT NULL,
            asset TEXT NOT NULL, signal_ts_ms INTEGER NOT NULL,
            mid_price REAL NOT NULL, liquidation_price REAL NOT NULL,
            distance_bps REAL NOT NULL, direction_to_liquidation TEXT NOT NULL,
            position_side TEXT NOT NULL, liquidation_type TEXT NOT NULL,
            whale_address TEXT NOT NULL, position_notional REAL NOT NULL,
            position_size REAL NOT NULL, spread_bps REAL,
            source TEXT NOT NULL DEFAULT 'clearinghouseState',
            source_snapshot_hash TEXT, is_primary INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_fwlm_cand_episode
            ON hl_far_whale_liq_candidates(episode_id);

        CREATE TABLE IF NOT EXISTS hl_far_whale_liq_paper_entries (
            entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER NOT NULL, episode_id TEXT NOT NULL,
            study_version TEXT NOT NULL, asset TEXT NOT NULL,
            direction_to_liquidation TEXT NOT NULL,
            entry_ts_ms INTEGER NOT NULL, entry_price REAL NOT NULL,
            paper_notional REAL NOT NULL, d_bps REAL NOT NULL,
            target_price REAL NOT NULL, adverse_stop_price REAL NOT NULL,
            liquidation_price REAL NOT NULL, spread_bps REAL,
            is_primary INTEGER NOT NULL DEFAULT 0,
            mfe_bps REAL NOT NULL DEFAULT 0, mae_bps REAL NOT NULL DEFAULT 0,
            whale_event TEXT, status TEXT NOT NULL DEFAULT 'open',
            fee_assumption REAL
        );
        CREATE INDEX IF NOT EXISTS idx_fwlm_entry_status
            ON hl_far_whale_liq_paper_entries(status);

        CREATE TABLE IF NOT EXISTS hl_far_whale_liq_outcomes (
            entry_id INTEGER PRIMARY KEY,
            exit_ts_ms INTEGER NOT NULL, exit_price REAL NOT NULL,
            exit_reason TEXT NOT NULL, paper_pnl REAL NOT NULL,
            mfe_bps REAL NOT NULL, mae_bps REAL NOT NULL,
            migrated_toward_liq INTEGER NOT NULL, hit_liq_level INTEGER NOT NULL,
            liq_fired INTEGER, position_persisted INTEGER, horizon_ms INTEGER NOT NULL,
            fee_assumption REAL
        );

        CREATE TABLE IF NOT EXISTS hl_far_whale_liq_controls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id INTEGER NOT NULL, control_type TEXT NOT NULL,
            matched_asset TEXT NOT NULL, matched_ts_ms INTEGER NOT NULL,
            direction_to_liquidation TEXT NOT NULL, d_bps REAL NOT NULL,
            paper_pnl REAL, migrated_toward_liq INTEGER, exit_reason TEXT,
            UNIQUE(entry_id, control_type)
        );

        CREATE TABLE IF NOT EXISTS hl_far_whale_liq_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_ms INTEGER NOT NULL, study_version TEXT NOT NULL,
            kind TEXT NOT NULL, payload_json TEXT NOT NULL
        );
    """)
    conn.commit()


def save_market_state(conn, rows: dict, ts_ms: int) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO hl_far_whale_liq_market_state"
        "(asset, ts_ms, mid, oi, day_ntl_vlm, spread_bps) VALUES (?,?,?,?,?,?)",
        [(a, int(ts_ms), v[0], v[1], v[2], v[3]) for a, v in rows.items()])
    conn.commit()


def save_round(conn, ts_ms: int, stats: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO hl_far_whale_liq_rounds"
        "(ts_ms, addresses_polled, whales_seen, anomalies_dropped, candidates_new, sweep_mode)"
        " VALUES (?,?,?,?,?,?)",
        (int(ts_ms), stats.get("addresses_polled", 0), stats.get("whales_seen", 0),
         stats.get("anomalies_dropped", 0), stats.get("candidates_new", 0),
         1 if stats.get("sweep_mode") else 0))
    conn.commit()


def save_snapshots(conn, ts_ms: int, rows: list) -> None:
    conn.executemany(
        "INSERT INTO hl_far_whale_liq_position_snapshots"
        "(ts_ms, address, asset, liquidation_price, position_notional, szi)"
        " VALUES (?,?,?,?,?,?)",
        [(int(ts_ms), r["address"], r["asset"], r["liquidation_price"],
          r["position_notional"], r["szi"]) for r in rows])
    conn.commit()


def assign_episode(conn, cand: dict, signal_ts_ms: int) -> tuple:
    """§3.1: same (asset, whale, liq ±25bps) within 60min = same episode. A >25bps liq
    move closes the old episode as liq_price_moved and opens a new one."""
    row = conn.execute(
        "SELECT episode_id, ref_liquidation_price, last_seen_ts_ms FROM hl_far_whale_liq_episodes"
        " WHERE asset=? AND whale_address=? AND status='open'"
        " ORDER BY last_seen_ts_ms DESC LIMIT 1",
        (cand["asset"], cand["whale_address"])).fetchone()
    if row is not None:
        drift_bps = abs(cand["liquidation_price"] - row["ref_liquidation_price"]) \
            / row["ref_liquidation_price"] * 1e4
        gap = signal_ts_ms - row["last_seen_ts_ms"]
        # §3.1: a level that exits the gate for >= 60 min starts a NEW episode, so an
        # exactly-60-min gap must NOT reuse — strict less-than only.
        if drift_bps <= EPISODE_LIQ_TOL_BPS and gap < EPISODE_GAP_MS:
            conn.execute(
                "UPDATE hl_far_whale_liq_episodes SET last_seen_ts_ms=?,"
                " n_candidates=n_candidates+1 WHERE episode_id=?",
                (int(signal_ts_ms), row["episode_id"]))
            conn.commit()
            return row["episode_id"], False
        conn.execute("UPDATE hl_far_whale_liq_episodes SET status=? WHERE episode_id=?",
                     ("liq_price_moved" if drift_bps > EPISODE_LIQ_TOL_BPS else "closed",
                      row["episode_id"]))
    episode_id = f"{cand['asset']}:{cand['whale_address']}:{int(signal_ts_ms)}"
    conn.execute(
        "INSERT INTO hl_far_whale_liq_episodes"
        "(episode_id, asset, whale_address, ref_liquidation_price, first_ts_ms,"
        " last_seen_ts_ms, status, n_candidates) VALUES (?,?,?,?,?,?,'open',1)",
        (episode_id, cand["asset"], cand["whale_address"], cand["liquidation_price"],
         int(signal_ts_ms), int(signal_ts_ms)))
    conn.commit()
    return episode_id, True


def insert_candidate(conn, cand: dict, *, episode_id: str, signal_ts_ms: int,
                     is_primary: int, source_snapshot_hash: str) -> int:
    cur = conn.execute(
        "INSERT INTO hl_far_whale_liq_candidates"
        "(episode_id, study_version, asset, signal_ts_ms, mid_price, liquidation_price,"
        " distance_bps, direction_to_liquidation, position_side, liquidation_type,"
        " whale_address, position_notional, position_size, spread_bps,"
        " source_snapshot_hash, is_primary)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (episode_id, STUDY_VERSION, cand["asset"], int(signal_ts_ms), cand["mid_price"],
         cand["liquidation_price"], cand["distance_bps"], cand["direction_to_liquidation"],
         cand["position_side"], cand["liquidation_type"], cand["whale_address"],
         cand["position_notional"], cand["position_size"], cand.get("spread_bps"),
         source_snapshot_hash, int(is_primary)))
    conn.commit()
    return cur.lastrowid


def open_episodes(conn) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM hl_far_whale_liq_episodes WHERE status='open'").fetchall()]


def sweep_snapshot_rows(conn, since_ms: int, until_ms: int) -> list:
    """Ordered position-snapshot rows for the §4 threshold sweep (asset, address, ts_ms,
    position_notional). Keeps the sweep reading the snapshot pool through the store rather
    than duplicating schema knowledge in raw SQL in the sweep script."""
    return conn.execute(
        "SELECT asset, address, ts_ms, position_notional"
        " FROM hl_far_whale_liq_position_snapshots"
        " WHERE ts_ms>=? AND ts_ms<=? ORDER BY asset, address, ts_ms",
        (int(since_ms), int(until_ms))).fetchall()


def first_candidate_notional(conn, episode_id: str) -> float | None:
    """First (by candidate_id) position_notional recorded for an episode — the Control 4
    shrink baseline the runner compares each round's polled notional against."""
    row = conn.execute(
        "SELECT position_notional FROM hl_far_whale_liq_candidates"
        " WHERE episode_id=? ORDER BY candidate_id LIMIT 1", (episode_id,)).fetchone()
    return float(row[0]) if row is not None else None


def episode_status_map(conn) -> dict[str, str]:
    """episode_id -> status for ALL episodes (open and closed) — the runner uses this to
    resolve each open paper entry's episode status when stepping/settling this round."""
    return {r["episode_id"]: r["status"] for r in
            conn.execute("SELECT episode_id, status FROM hl_far_whale_liq_episodes")}


def update_episode(conn, episode_id: str, *, last_seen_ts_ms=None, status=None) -> None:
    if last_seen_ts_ms is not None:
        conn.execute("UPDATE hl_far_whale_liq_episodes SET last_seen_ts_ms=?"
                     " WHERE episode_id=?", (int(last_seen_ts_ms), episode_id))
    if status is not None:
        conn.execute("UPDATE hl_far_whale_liq_episodes SET status=? WHERE episode_id=?",
                     (status, episode_id))
    conn.commit()


def insert_entry(conn, entry: dict) -> int:
    """Stamps fee_assumption from the frozen FEE_BPS constant (spec §16: fee "reported
    explicitly") so the fee used is an auditable data fact, not just recoverable via
    study_version -> FEE_BPS."""
    cur = conn.execute(
        "INSERT INTO hl_far_whale_liq_paper_entries"
        "(candidate_id, episode_id, study_version, asset, direction_to_liquidation,"
        " entry_ts_ms, entry_price, paper_notional, d_bps, target_price,"
        " adverse_stop_price, liquidation_price, spread_bps, is_primary, fee_assumption)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (entry["candidate_id"], entry["episode_id"], STUDY_VERSION, entry["asset"],
         entry["direction_to_liquidation"], int(entry["entry_ts_ms"]),
         entry["entry_price"], entry["paper_notional"], entry["d_bps"],
         entry["target_price"], entry["adverse_stop_price"], entry["liquidation_price"],
         entry.get("spread_bps"), int(entry.get("is_primary", 0)), FEE_BPS))
    conn.commit()
    return cur.lastrowid


def open_entries(conn) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM hl_far_whale_liq_paper_entries WHERE status='open'").fetchall()]


def update_entry_path(conn, entry_id: int, *, mfe_bps: float, mae_bps: float,
                      whale_event=None) -> None:
    conn.execute(
        "UPDATE hl_far_whale_liq_paper_entries SET mfe_bps=?, mae_bps=?,"
        " whale_event=COALESCE(?, whale_event) WHERE entry_id=?",
        (mfe_bps, mae_bps, whale_event, entry_id))
    conn.commit()


def settle_entry(conn, entry_id: int, outcome: dict) -> None:
    """Persists fee_assumption on the outcome row, carried from the entry it settles
    (falls back to the entry's stored value so outcome fee_assumption always matches
    the entry that produced it; outcome dict may override explicitly if needed)."""
    conn.execute("UPDATE hl_far_whale_liq_paper_entries SET status='closed' WHERE entry_id=?",
                 (entry_id,))
    fee_assumption = outcome.get("fee_assumption")
    if fee_assumption is None:
        row = conn.execute(
            "SELECT fee_assumption FROM hl_far_whale_liq_paper_entries WHERE entry_id=?",
            (entry_id,)).fetchone()
        fee_assumption = row["fee_assumption"] if row is not None else None
    conn.execute(
        "INSERT OR REPLACE INTO hl_far_whale_liq_outcomes"
        "(entry_id, exit_ts_ms, exit_price, exit_reason, paper_pnl, mfe_bps, mae_bps,"
        " migrated_toward_liq, hit_liq_level, liq_fired, position_persisted, horizon_ms,"
        " fee_assumption)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (entry_id, int(outcome["exit_ts_ms"]), outcome["exit_price"],
         outcome["exit_reason"], outcome["paper_pnl"], outcome["mfe_bps"],
         outcome["mae_bps"], int(outcome["migrated_toward_liq"]),
         int(outcome["hit_liq_level"]), outcome.get("liq_fired"),
         outcome.get("position_persisted"), int(outcome["horizon_ms"]), fee_assumption))
    conn.commit()


def save_report(conn, *, kind: str, payload: dict, ts_ms: int) -> None:
    conn.execute(
        "INSERT INTO hl_far_whale_liq_reports(ts_ms, study_version, kind, payload_json)"
        " VALUES (?,?,?,?)", (int(ts_ms), STUDY_VERSION, kind, json.dumps(payload)))
    conn.commit()


def latest_report(conn, kind: str) -> dict | None:
    row = conn.execute(
        "SELECT payload_json FROM hl_far_whale_liq_reports WHERE kind=? AND study_version=?"
        " ORDER BY ts_ms DESC, id DESC LIMIT 1", (kind, STUDY_VERSION)).fetchone()
    return json.loads(row["payload_json"]) if row else None


def load_freeze(conn) -> dict | None:
    return latest_report(conn, "freeze")


def read_market_series(conn, asset: str, since_ms: int, until_ms: int) -> list:
    return [(int(r["ts_ms"]), float(r["mid"])) for r in conn.execute(
        "SELECT ts_ms, mid FROM hl_far_whale_liq_market_state"
        " WHERE asset=? AND ts_ms>=? AND ts_ms<=? ORDER BY ts_ms",
        (asset, int(since_ms), int(until_ms))).fetchall()]


def market_state_window(conn, asset: str, since_ms: int, until_ms: int) -> list:
    """Full market-state rows (ts_ms, mid, oi, spread_bps) over a window — for feature
    computation (read_market_series returns only the price path)."""
    return [dict(r) for r in conn.execute(
        "SELECT ts_ms, mid, oi, spread_bps FROM hl_far_whale_liq_market_state"
        " WHERE asset=? AND ts_ms>=? AND ts_ms<=? ORDER BY ts_ms",
        (asset, int(since_ms), int(until_ms))).fetchall()]


def market_state_ts(conn, asset: str) -> list:
    """All market-state timestamps for an asset (the trend-match candidate pool)."""
    return [int(r["ts_ms"]) for r in conn.execute(
        "SELECT ts_ms FROM hl_far_whale_liq_market_state WHERE asset=? ORDER BY ts_ms",
        (asset,)).fetchall()]


def candidate_signal_ts(conn, asset: str) -> list:
    """All candidate signal timestamps for an asset (trend-match exclusion set)."""
    return [int(r["signal_ts_ms"]) for r in conn.execute(
        "SELECT signal_ts_ms FROM hl_far_whale_liq_candidates WHERE asset=?",
        (asset,)).fetchall()]


def primary_entries_with_outcomes(conn) -> list:
    """Settled primary paper entries (those with an outcome row) — the control-generation set."""
    return [dict(r) for r in conn.execute(
        "SELECT e.* FROM hl_far_whale_liq_paper_entries e"
        " JOIN hl_far_whale_liq_outcomes o ON o.entry_id=e.entry_id"
        " WHERE e.is_primary=1").fetchall()]


def existing_control_keys(conn) -> set:
    """(entry_id, control_type) pairs already written — for idempotent control generation."""
    return {(r["entry_id"], r["control_type"]) for r in conn.execute(
        "SELECT entry_id, control_type FROM hl_far_whale_liq_controls").fetchall()}


def insert_control(conn, ctl: dict) -> None:
    """Idempotent control-row write (UNIQUE(entry_id, control_type))."""
    conn.execute(
        "INSERT OR IGNORE INTO hl_far_whale_liq_controls"
        "(entry_id, control_type, matched_asset, matched_ts_ms, direction_to_liquidation,"
        " d_bps, paper_pnl, migrated_toward_liq, exit_reason) VALUES (?,?,?,?,?,?,?,?,?)",
        (ctl["entry_id"], ctl["control_type"], ctl["matched_asset"], ctl["matched_ts_ms"],
         ctl["direction_to_liquidation"], ctl["d_bps"], ctl["paper_pnl"],
         ctl["migrated_toward_liq"], ctl["exit_reason"]))
    conn.commit()


def primary_outcome_rows(conn) -> list:
    """Settled PRIMARY entries joined to outcome + candidate — the ITT verdict set (never
    persistence-filtered). Keys: entry_id, episode_id, asset, distance_bps, whale_address,
    position_notional, paper_pnl, migrated_toward_liq, position_persisted."""
    return [dict(r) for r in conn.execute(
        "SELECT e.entry_id, e.episode_id, e.asset, e.d_bps AS distance_bps,"
        " c.whale_address, c.position_notional,"
        " o.paper_pnl, o.migrated_toward_liq, o.position_persisted"
        " FROM hl_far_whale_liq_paper_entries e"
        " JOIN hl_far_whale_liq_outcomes o ON o.entry_id=e.entry_id"
        " JOIN hl_far_whale_liq_candidates c ON c.candidate_id=e.candidate_id"
        " WHERE e.is_primary=1").fetchall()]


def count_episodes(conn) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM hl_far_whale_liq_episodes").fetchone()[0])


def all_controls(conn) -> list:
    """(entry_id, control_type, paper_pnl, migrated_toward_liq) for every control row."""
    return [dict(r) for r in conn.execute(
        "SELECT entry_id, control_type, paper_pnl, migrated_toward_liq"
        " FROM hl_far_whale_liq_controls").fetchall()]


def episode_status_counts(conn) -> dict:
    """status -> count over all episodes (persistence-attrition diagnostic)."""
    return {r["status"]: int(r["cnt"]) for r in conn.execute(
        "SELECT status, COUNT(*) AS cnt FROM hl_far_whale_liq_episodes GROUP BY status")}
