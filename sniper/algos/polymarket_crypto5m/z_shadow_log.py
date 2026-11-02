"""Per-tick shadow log of the brain-v2 stabilized probability (design sec 6.4).

One row per tick: the current z, our jump-excluded z_ours, the market-implied z_market, the blended z_v2 and
its weight, plus the regime-vol context. Resolved at WINDOW CLOSE with the 0/1 settlement and the independent
sharp-window label. This single table is the graph source AND the offline-validation substrate. Restart-safe:
pending rows persist; unresolved windows are swept to 'incomplete' and excluded from the gate.

Standalone paper DB (data/crypto5m/active_trader_v0.sqlite) -- inline SQL, not governed by db.py."""
from __future__ import annotations

_COLS = (
    "engine_version", "window_start_ts", "tick_ts", "condition_id",
    "btc_now", "btc_start", "seconds_left",
    "vol_slow", "vol_fast", "jump_flag", "divergence",
    "z", "z_ours", "z_market", "market_mid", "z_v2", "weight",
)


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS crypto5m_z_shadow (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        engine_version TEXT, window_start_ts INTEGER, tick_ts INTEGER, condition_id TEXT,
        btc_now REAL, btc_start REAL, seconds_left INTEGER,
        vol_slow REAL, vol_fast REAL, jump_flag INTEGER, divergence REAL,
        z REAL, z_ours REAL, z_market REAL, market_mid REAL, z_v2 REAL, weight REAL,
        status TEXT DEFAULT 'pending', settlement INTEGER, sharp_label TEXT, resolved_ts INTEGER)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_z_shadow_pending ON crypto5m_z_shadow "
                 "(status, window_start_ts)")
    conn.commit()


def record_tick(conn, row: dict) -> int:
    cols = ", ".join(_COLS)
    ph = ", ".join("?" for _ in _COLS)
    cur = conn.execute(
        f"INSERT INTO crypto5m_z_shadow ({cols}, status) VALUES ({ph}, 'pending')",
        tuple(row.get(c) for c in _COLS))
    conn.commit()
    return int(cur.lastrowid)


def resolve_window(conn, window_start_ts, settlement, sharp_label, now_ts) -> int:
    cur = conn.execute(
        "UPDATE crypto5m_z_shadow SET status='resolved', settlement=?, sharp_label=?, resolved_ts=? "
        "WHERE status='pending' AND window_start_ts=?",
        (int(settlement), sharp_label, now_ts, window_start_ts))
    conn.commit()
    return cur.rowcount


def sweep_incomplete(conn, now_ts) -> int:
    cur = conn.execute(
        "UPDATE crypto5m_z_shadow SET status='incomplete' "
        "WHERE status='pending' AND (window_start_ts + 300) <= ?", (now_ts,))
    conn.commit()
    return cur.rowcount
