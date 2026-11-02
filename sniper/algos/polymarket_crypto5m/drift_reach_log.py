"""Drift-reachability SHADOW log for the crypto5m taker redesign.

For every confirmed-direction tick we record the drift model's prediction (mu_hat, touch_prob, the SIGNED
expected favorable move, and the driftless baseline move side-by-side) plus the book at the decision, then
resolve the realized MID move at tick_ts + t_hold. The paired (drift, driftless, realized) row is what the
validation uses to ask: does a MEASURED-drift reachability model predict realized moves where the driftless
gate scored rho~0? Mirrors ev_observation_log; kept in a SEPARATE table so this research experiment can never
entangle the load-bearing ev-observation data.

Standalone paper DB (data/crypto5m/active_trader_v0.sqlite) — inline SQL matches the active-engine tables.
"""
from __future__ import annotations

_COLS = (
    "engine_version", "window_start_ts", "tick_ts", "side", "condition_id",
    "z", "vol_per_s", "seconds_left", "mu_hat", "b_barrier",
    "touch_prob", "exp_fav_move", "exp_fav_move_driftless", "max_hold_s", "jump_flag",
    "side_ask", "side_bid", "side_mid", "mid_at_entry", "t_hold", "resolve_at_ts",
)


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS crypto5m_drift_reach (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        engine_version TEXT, window_start_ts INTEGER, tick_ts INTEGER, side TEXT, condition_id TEXT,
        z REAL, vol_per_s REAL, seconds_left INTEGER, mu_hat REAL, b_barrier REAL,
        touch_prob REAL, exp_fav_move REAL, exp_fav_move_driftless REAL, max_hold_s REAL, jump_flag INTEGER,
        side_ask REAL, side_bid REAL, side_mid REAL, mid_at_entry REAL, t_hold REAL,
        resolve_at_ts INTEGER, status TEXT DEFAULT 'pending',
        mid_at_thold REAL, realized_mid_move REAL, realized_ts INTEGER)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_drift_reach_pending ON crypto5m_drift_reach "
                 "(status, window_start_ts, resolve_at_ts)")
    conn.commit()


def record_pending(conn, obs: dict) -> int:
    cols = ", ".join(_COLS)
    ph = ", ".join("?" for _ in _COLS)
    cur = conn.execute(
        f"INSERT INTO crypto5m_drift_reach ({cols}, status) VALUES ({ph}, 'pending')",
        tuple(obs.get(c) for c in _COLS))
    conn.commit()
    return int(cur.lastrowid)


def due_pending(conn, window_start_ts, now_ts):
    return conn.execute(
        "SELECT * FROM crypto5m_drift_reach WHERE status='pending' AND window_start_ts=? "
        "AND resolve_at_ts <= ? ORDER BY id", (window_start_ts, now_ts)).fetchall()


def complete_observation(conn, obs_id, *, mid, realized_ts, status="complete"):
    row = conn.execute("SELECT mid_at_entry FROM crypto5m_drift_reach WHERE id=?", (obs_id,)).fetchone()
    entry_mid = row["mid_at_entry"] if row is not None else None
    move = (mid - entry_mid) if (mid is not None and entry_mid is not None) else None
    conn.execute(
        "UPDATE crypto5m_drift_reach SET mid_at_thold=?, realized_mid_move=?, realized_ts=?, status=? "
        "WHERE id=?", (mid, move, realized_ts, status, obs_id))
    conn.commit()


def sweep_incomplete(conn, now_ts) -> int:
    cur = conn.execute(
        "UPDATE crypto5m_drift_reach SET status='incomplete' "
        "WHERE status='pending' AND (window_start_ts + 300) <= ?", (now_ts,))
    conn.commit()
    return cur.rowcount
