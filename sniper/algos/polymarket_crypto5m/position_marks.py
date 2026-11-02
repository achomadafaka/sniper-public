"""Per-position intra-trade mid marks for the crypto5m EXIT counterfactual.

Records the held side's mid/bid/ask (and seconds_held) every tick a position is open. This is the data the
viability study (Q2, "CANNOT TEST") lacked: with the full intra-trade mid PATH we can later compute a
theta-aware exit counterfactual — would banking at t* / the reachable ceiling beat the current
book-price/flip/patience exits? Logging only; gates nothing, feeds no decision.

Standalone paper DB (data/crypto5m/active_trader_v0.sqlite) — inline SQL matches the active-engine tables.
"""
from __future__ import annotations

_COLS = ("position_key", "window_start_ts", "tick_ts", "seconds_held", "side", "mid", "bid", "ask")


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS crypto5m_position_marks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        position_key TEXT, window_start_ts INTEGER, tick_ts INTEGER, seconds_held INTEGER,
        side TEXT, mid REAL, bid REAL, ask REAL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pos_marks_key ON crypto5m_position_marks "
                 "(position_key, seconds_held)")
    conn.commit()


def record_mark(conn, mark: dict) -> int:
    cols = ", ".join(_COLS)
    ph = ", ".join("?" for _ in _COLS)
    cur = conn.execute(f"INSERT INTO crypto5m_position_marks ({cols}) VALUES ({ph})",
                       tuple(mark.get(c) for c in _COLS))
    conn.commit()
    return int(cur.lastrowid)


def marks_for_position(conn, position_key):
    return conn.execute(
        "SELECT * FROM crypto5m_position_marks WHERE position_key=? ORDER BY seconds_held",
        (position_key,)).fetchall()
