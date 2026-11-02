"""Binance 5-minute kline data adapter for the BTC 5m algo.

Pure parse/merge helpers (no network — unit-testable) plus thin network/cache I/O. Binance is the
single time source: bars are stamped with Binance's own open/close ms, never the local (+7h-skewed)
clock. The cache table lets the backtest replay the same OOS bars deterministically.
"""
from __future__ import annotations

import sqlite3

BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
BINANCE_TIME_URL = "https://api.binance.com/api/v3/time"
INTERVAL = "5m"
INTERVAL_MS = 5 * 60 * 1000
MAX_LIMIT = 1000


def parse_klines(rows, symbol: str = "BTCUSDT") -> list[dict]:
    """Binance kline rows -> OHLCV bar dicts. Pure. Malformed rows are skipped (never raise on one
    bad row — the feed occasionally returns short rows). Row format:
    [open_ms, open, high, low, close, volume, close_ms, ...]."""
    out = []
    for r in rows or []:
        if not isinstance(r, (list, tuple)) or len(r) < 7:
            continue
        try:
            out.append({
                "symbol": symbol,
                "open_ms": int(r[0]),
                "open": float(r[1]), "high": float(r[2]), "low": float(r[3]),
                "close": float(r[4]), "volume": float(r[5]),
                "close_ms": int(r[6]),
            })
        except (TypeError, ValueError):
            continue
    return out


def merge_bars(*bar_lists) -> list[dict]:
    """Union of bar lists, deduped by open_ms (later list wins on collision), sorted ascending."""
    by_open: dict[int, dict] = {}
    for bars in bar_lists:
        for b in bars or []:
            by_open[int(b["open_ms"])] = b
    return [by_open[k] for k in sorted(by_open)]


def closes(bars: list[dict]) -> list[float]:
    """Close series in time order."""
    return [float(b["close"]) for b in merge_bars(bars)]


# --- network / cache (thin; not unit-tested) ---------------------------------

def server_time_ms(session=None) -> int:
    """Binance server time in ms (authoritative clock — never trust the local clock)."""
    import requests
    s = session or requests
    return int(s.get(BINANCE_TIME_URL, timeout=15).json()["serverTime"])


def fetch_klines(symbol: str = "BTCUSDT", *, start_ms: int | None = None,
                 end_ms: int | None = None, limit: int = MAX_LIMIT, session=None) -> list[dict]:
    """Fetch one page (<=1000) of 5m klines from Binance. Returns parsed bars."""
    import requests
    s = session or requests
    params = {"symbol": symbol, "interval": INTERVAL, "limit": min(MAX_LIMIT, int(limit))}
    if start_ms is not None:
        params["startTime"] = int(start_ms)
    if end_ms is not None:
        params["endTime"] = int(end_ms)
    resp = s.get(BINANCE_KLINES_URL, params=params, timeout=30)
    resp.raise_for_status()
    return parse_klines(resp.json(), symbol=symbol)


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS btc5m_bars (
            symbol    TEXT NOT NULL,
            open_ms   INTEGER NOT NULL,
            open      REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
            volume    REAL NOT NULL DEFAULT 0,
            close_ms  INTEGER NOT NULL,
            PRIMARY KEY (symbol, open_ms)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS btc5m_paper (
            symbol    TEXT NOT NULL,
            entry_ms  INTEGER NOT NULL,
            exit_ms   INTEGER NOT NULL,
            side      INTEGER NOT NULL,
            entry_px  REAL NOT NULL, exit_px REAL NOT NULL,
            fee_pct   REAL NOT NULL DEFAULT 0,
            pnl_pct   REAL NOT NULL,
            PRIMARY KEY (symbol, entry_ms, exit_ms)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS btc5m_state (
            symbol    TEXT PRIMARY KEY,
            position  INTEGER NOT NULL DEFAULT 0,
            entry_px  REAL,
            entry_ms  INTEGER,
            last_bar_ms INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.commit()


def save_paper_trade(conn: sqlite3.Connection, symbol: str, t: dict) -> None:
    ensure_tables(conn)
    conn.execute("""INSERT OR IGNORE INTO btc5m_paper
                    (symbol, entry_ms, exit_ms, side, entry_px, exit_px, fee_pct, pnl_pct)
                    VALUES (?,?,?,?,?,?,?,?)""",
                 (symbol, int(t["entry_ms"] or 0), int(t["exit_ms"] or 0), int(t["side"]),
                  float(t["entry_px"]), float(t["exit_px"]), float(t.get("fee_pct", 0.0)),
                  float(t["pnl_pct"])))
    conn.commit()


def load_paper_trades(conn: sqlite3.Connection, symbol: str = "BTCUSDT") -> list[dict]:
    ensure_tables(conn)
    rows = conn.execute("""SELECT entry_ms, exit_ms, side, entry_px, exit_px, fee_pct, pnl_pct
                           FROM btc5m_paper WHERE symbol=? ORDER BY exit_ms""", (symbol,)).fetchall()
    return [{"entry_ms": r[0], "exit_ms": r[1], "side": r[2], "entry_px": r[3], "exit_px": r[4],
             "fee_pct": r[5], "pnl_pct": r[6]} for r in rows]


def load_state(conn: sqlite3.Connection, symbol: str = "BTCUSDT") -> dict:
    ensure_tables(conn)
    r = conn.execute("SELECT position, entry_px, entry_ms, last_bar_ms FROM btc5m_state WHERE symbol=?",
                     (symbol,)).fetchone()
    if not r:
        return {"position": 0, "entry_px": None, "entry_ms": None, "last_bar_ms": 0}
    return {"position": r[0], "entry_px": r[1], "entry_ms": r[2], "last_bar_ms": r[3]}


def state_exists(conn: sqlite3.Connection, symbol: str = "BTCUSDT") -> bool:
    ensure_tables(conn)
    return conn.execute("SELECT 1 FROM btc5m_state WHERE symbol=? LIMIT 1", (symbol,)).fetchone() is not None


def save_state(conn: sqlite3.Connection, symbol: str, state: dict, last_bar_ms: int) -> None:
    ensure_tables(conn)
    conn.execute("""INSERT OR REPLACE INTO btc5m_state (symbol, position, entry_px, entry_ms, last_bar_ms)
                    VALUES (?,?,?,?,?)""",
                 (symbol, int(state.get("position", 0)), state.get("entry_px"),
                  state.get("entry_ms"), int(last_bar_ms)))
    conn.commit()


def save_bars(conn: sqlite3.Connection, bars: list[dict]) -> int:
    ensure_tables(conn)
    n = 0
    for b in merge_bars(bars):
        conn.execute(
            """INSERT OR REPLACE INTO btc5m_bars
               (symbol, open_ms, open, high, low, close, volume, close_ms)
               VALUES (?,?,?,?,?,?,?,?)""",
            (b["symbol"], int(b["open_ms"]), b["open"], b["high"], b["low"], b["close"],
             b.get("volume", 0.0), int(b["close_ms"])))
        n += 1
    conn.commit()
    return n


def load_bars(conn: sqlite3.Connection, symbol: str = "BTCUSDT", *, limit: int | None = None) -> list[dict]:
    ensure_tables(conn)
    q = "SELECT symbol, open_ms, open, high, low, close, volume, close_ms FROM btc5m_bars WHERE symbol=? ORDER BY open_ms"
    rows = conn.execute(q, (symbol,)).fetchall()
    bars = [{"symbol": r[0], "open_ms": r[1], "open": r[2], "high": r[3], "low": r[4],
             "close": r[5], "volume": r[6], "close_ms": r[7]} for r in rows]
    return bars[-limit:] if limit else bars
