"""Desk tab feed panels: read-only views of the desk's data feeds (collect/report only).

Every read opens SQLite with mode=ro or stats a file; nothing writes, nothing calls a provider, nothing is on
the money path. A feed that is off or has no data yet says so instead of failing. Token rows always carry
distinct creators next to launches: one wallet mass-launching a name is a farm, not a narrative (Gamma).
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

COLLECT_ONLY = ("Collect/report only: none of these numbers gates a trade or reaches the JUDGE sheet until a "
                "pre-registered read says so.")


def _ro(path):
    path = Path(path)
    if not path.is_file():
        return None
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    return conn


def _latest_day_file(directory):
    files = sorted(Path(directory).glob("????-??-??.db")) if Path(directory).is_dir() else []
    return files[-1] if files else None


def _age(path, now):
    try:
        return round(now - Path(path).stat().st_mtime, 1)
    except OSError:
        return None


def trending(desk_dir, limit=10):
    path = _latest_day_file(Path(desk_dir) / "trending")
    conn = _ro(path) if path else None
    if conn is None:
        return {"available": False, "reason": "no_trending_data"}
    try:
        board = conn.execute("SELECT * FROM boards ORDER BY at DESC LIMIT 1").fetchone()
        if board is None:
            return {"available": False, "reason": "no_board_yet"}
        cols = {r[1] for r in conn.execute("PRAGMA table_info(ranks)")}
        kind = "curve_kind" if "curve_kind" in cols else "NULL AS curve_kind"
        ranks = [dict(r) for r in conn.execute(
            "SELECT rank, mint, buy_usd_15m, makers_15m, swaps_15m, change_15m_pct, mcap_usd, %s FROM ranks "
            "WHERE at=? ORDER BY rank LIMIT ?" % kind, (board["at"], limit))]
        b = dict(board)
        return {"available": True, "at": b["at"], "rule_version": b.get("rule_version"), "coverage": b.get("coverage"),
                "koth": {"mint": b.get("koth_mint"), "mcap_usd": b.get("koth_mcap_usd"),
                         "curve_kind": b.get("koth_curve_kind")}, "ranks": ranks}
    except sqlite3.Error as exc:
        return {"available": False, "reason": "unreadable", "detail": str(exc)[:120]}
    finally:
        conn.close()


def narrative(desk_dir, limit=15):
    live = _latest_day_file(Path(desk_dir) / "narrative/live-v2")
    path = live or _latest_day_file(Path(desk_dir) / "narrative")
    conn = _ro(path) if path else None
    if conn is None:
        return {"available": False, "reason": "no_narrative_data"}
    try:
        hour = conn.execute("SELECT MAX(hour_start) FROM token_hourly").fetchone()[0]
        tokens = {}
        for kind in ("name_token", "rule_word"):
            tokens[kind] = [dict(r) for r in conn.execute(
                "SELECT rank, token, launches, creators, prev_hour_launches FROM token_hourly "
                "WHERE hour_start=? AND kind=? ORDER BY rank LIMIT ?", (hour, kind, limit))] if hour else []
        at = conn.execute("SELECT MAX(at) FROM theme_agg").fetchone()[0]
        themes = [dict(r) for r in conn.execute(
            "SELECT theme, launches, band_entries, buy_usd, buyers FROM theme_agg WHERE at=? AND window_s=3600 "
            "ORDER BY launches DESC", (at,))] if at else []
        out = {"available": True, "hour_start": hour, "tokens": tokens, "themes_at": at, "themes_60m": themes}
        if live:
            row = conn.execute("SELECT * FROM source_state ORDER BY at DESC LIMIT 1").fetchone()
            out.update(version="narrative-live-v2", source_state=dict(row) if row else None,
                       scope="Observed Solana first sightings; bounded bootstrap and retained WSOL PumpSwap flow are lower bounds. Creator identity and band entries are unknown.")
        return out
    except sqlite3.Error as exc:
        return {"available": False, "reason": "unreadable", "detail": str(exc)[:120]}
    finally:
        conn.close()


def heartbeats(desk_dir, now=None):
    """Freshness of each feed: its heartbeat or newest data file, plus the X producer's own heartbeat body."""
    now = time.time() if now is None else now
    d = Path(desk_dir)
    out = []
    try:
        x = json.loads((d / "x" / "producer.heartbeat").read_text())
        out.append({"feed": "X producer", "age_s": round(now - float(x["ts"]), 1), "connected": x.get("connected"),
                    "last_error": x.get("last_error")})
    except (OSError, ValueError, KeyError, TypeError):
        out.append({"feed": "X producer", "age_s": None, "connected": None, "last_error": None})
    for label, path in (("crawler", d / "crawler" / "crawls.db"),
                        ("DexScreener", _latest_day_file(d / "dexscreener")),
                        ("trending", _latest_day_file(d / "trending")),
                        ("narrative", _latest_day_file(d / "narrative/live-v2") or _latest_day_file(d / "narrative"))):
        out.append({"feed": label, "age_s": _age(path, now) if path else None, "connected": None, "last_error": None})
    conn = _ro(_latest_day_file(d / "dexscreener") or d / "none")
    if conn is not None:
        try:
            row = conn.execute("SELECT error, received_at FROM polls ORDER BY id DESC LIMIT 1").fetchone()
            if row:
                out[2]["last_error"] = row["error"]
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    return out


def feeds(desk_dir, now=None):
    now = time.time() if now is None else now
    return {"display_only": True, "collect_only": COLLECT_ONLY,
            "as_of": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            "heartbeats": heartbeats(desk_dir, now), "trending": trending(desk_dir), "narrative": narrative(desk_dir)}
