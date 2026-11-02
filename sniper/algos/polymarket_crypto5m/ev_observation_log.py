"""Counterfactual EV observation log for the crypto5m EV-gate (sec 5 of the design).

For EVERY confirmed-direction tick (whether the gate passes or not) we record the EV inputs/outputs under
BOTH fees plus the side's book at the decision tick, then resolve the realized MID move at tick_ts + t_hold.
That counterfactual is what (a) refits k to its true in-distribution value and (b) makes a near-zero admit
rate DECISIVE (cost-bound proof at realized fee) rather than ambiguous. Restart-safe: pending rows persist;
due rows are resolved from the live desk while the window is still current.

Standalone paper DB (data/crypto5m/active_trader_v0.sqlite) — inline SQL matches the existing active-engine
tables (this DB is not governed by db.py).
"""
from __future__ import annotations

_COLS = (
    "engine_version", "campaign_id", "window_start_ts", "tick_ts", "side", "condition_id", "token_id",
    "z_model", "market_probit_z", "conviction", "vol_per_s", "seconds_left",
    "side_ask", "side_bid", "side_mid", "spread",
    "delta", "theta", "t_star", "t_hold", "gain", "gate_stat",
    "cost_007", "cost_realfee", "ev_007", "ev_realfee", "passed_007", "passed_realfee",
    "mid_at_entry", "bid_at_entry", "ask_at_entry", "resolve_at_ts",
)


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS crypto5m_ev_observations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        engine_version TEXT, campaign_id TEXT, window_start_ts INTEGER, tick_ts INTEGER,
        side TEXT, condition_id TEXT, token_id TEXT,
        z_model REAL, market_probit_z REAL, conviction REAL, vol_per_s REAL, seconds_left INTEGER,
        side_ask REAL, side_bid REAL, side_mid REAL, spread REAL,
        delta REAL, theta REAL, t_star REAL, t_hold REAL, gain REAL, gate_stat REAL,
        cost_007 REAL, cost_realfee REAL, ev_007 REAL, ev_realfee REAL,
        passed_007 INTEGER, passed_realfee INTEGER,
        mid_at_entry REAL, bid_at_entry REAL, ask_at_entry REAL,
        resolve_at_ts INTEGER, status TEXT DEFAULT 'pending',
        mid_at_thold REAL, bid_at_thold REAL, ask_at_thold REAL,
        realized_mid_move REAL, realized_ts INTEGER)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ev_obs_pending ON crypto5m_ev_observations "
                 "(status, window_start_ts, resolve_at_ts)")
    conn.commit()


def record_pending(conn, obs: dict) -> int:
    cols = ", ".join(_COLS)
    ph = ", ".join("?" for _ in _COLS)
    cur = conn.execute(
        f"INSERT INTO crypto5m_ev_observations ({cols}, status) VALUES ({ph}, 'pending')",
        tuple(obs.get(c) for c in _COLS))
    conn.commit()
    return int(cur.lastrowid)


def due_pending(conn, window_start_ts, now_ts):
    return conn.execute(
        "SELECT * FROM crypto5m_ev_observations WHERE status='pending' AND window_start_ts=? "
        "AND resolve_at_ts <= ? ORDER BY id", (window_start_ts, now_ts)).fetchall()


def complete_observation(conn, obs_id, *, mid, bid, ask, realized_ts, status="complete"):
    row = conn.execute("SELECT mid_at_entry FROM crypto5m_ev_observations WHERE id=?", (obs_id,)).fetchone()
    entry_mid = row["mid_at_entry"] if row is not None else None
    move = (mid - entry_mid) if (mid is not None and entry_mid is not None) else None
    conn.execute(
        "UPDATE crypto5m_ev_observations SET mid_at_thold=?, bid_at_thold=?, ask_at_thold=?, "
        "realized_mid_move=?, realized_ts=?, status=? WHERE id=?",
        (mid, bid, ask, move, realized_ts, status, obs_id))
    conn.commit()


def sweep_incomplete(conn, now_ts) -> int:
    cur = conn.execute(
        "UPDATE crypto5m_ev_observations SET status='incomplete' "
        "WHERE status='pending' AND (window_start_ts + 300) <= ?", (now_ts,))
    conn.commit()
    return cur.rowcount


def admit_fraction(conn, engine_version) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(passed_007),0) p7, COALESCE(SUM(passed_realfee),0) pr "
        "FROM crypto5m_ev_observations WHERE engine_version=?", (engine_version,)).fetchone()
    n = row["n"] or 0
    p7 = row["p7"] or 0
    pr = row["pr"] or 0
    return {"n": n, "n_pass_007": p7, "frac_007": (p7 / n if n else 0.0),
            "n_pass_realfee": pr, "frac_realfee": (pr / n if n else 0.0)}
