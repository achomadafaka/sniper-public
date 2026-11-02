#!/usr/bin/env python3
"""Read-only Hyperliquid squeeze-state tape.

One public `metaAndAssetCtxs` POST per tick captures, for the whole perp universe, the state a
short-side exhaustion rule needs and that Hyperliquid serves NO history for:

  * open interest                -> cascade fuel; a squeeze is OI collapsing INTO an adverse move
  * mark / oracle / premium      -> perp-vs-spot basis
  * impact bid/ask (fixed size)  -> HL's native depth probe; its spread is the replenishment proxy
  * mark/mid + funding           -> the <=300s execution clock the forward prereg requires

`candleSnapshot` only serves currently-listed coins, so delisted names vanish from history — the
exact tail (JELLY-style squeezes) a short strategy dies on. `is_delisted` is recorded per tick so
the survivorship boundary is dated rather than inferred later.

There is no public liquidation feed. This tape records the inputs a cascade is inferable from and
never fabricates a `liquidation_volume` series.

No keys, no orders, no writes outside its own database.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sqlite3
import sys
import time
from pathlib import Path

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from sniper.markets.hyperliquid_api import _post_info  # noqa: E402
from sniper.services.memecoin_runner import acquire_run_lock

logger = logging.getLogger("hl_squeeze_tape")

DEFAULT_DB = "data/hl_squeeze_tape.db"
DEFAULT_HEARTBEAT = "data/hl_squeeze_tape.heartbeat"
TAPE_VERSION = "hl-squeeze-tape-v1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS hl_squeeze_marks (
    ts_ms          INTEGER NOT NULL,
    coin           TEXT    NOT NULL,
    mark_px        REAL,
    mid_px         REAL,
    oracle_px      REAL,
    premium        REAL,
    funding        REAL,
    open_interest  REAL,
    day_ntl_vlm    REAL,
    day_base_vlm   REAL,
    prev_day_px    REAL,
    impact_bid     REAL,
    impact_ask     REAL,
    is_delisted    INTEGER NOT NULL CHECK (is_delisted IN (0,1)),
    PRIMARY KEY (ts_ms, coin)
);

CREATE INDEX IF NOT EXISTS idx_hl_squeeze_coin_ts ON hl_squeeze_marks(coin, ts_ms);

CREATE TABLE IF NOT EXISTS hl_squeeze_ticks (
    ts_ms          INTEGER PRIMARY KEY,
    tape_version   TEXT    NOT NULL,
    coins          INTEGER NOT NULL CHECK (coins >= 0),
    delisted       INTEGER NOT NULL CHECK (delisted >= 0),
    fetch_ms       INTEGER NOT NULL CHECK (fetch_ms >= 0),
    gap_s          REAL,
    status         TEXT    NOT NULL CHECK (status IN ('ok','error')),
    detail         TEXT
);

PRAGMA user_version=1;
"""


def connect(path: str = DEFAULT_DB) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def _f(value):
    """Float or None. A missing/garbage provider field stays missing — never a fabricated zero."""
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and parsed not in (float("inf"), float("-inf")) else None


def normalize(meta, ctxs, ts_ms: int) -> list[tuple]:
    """(meta, assetCtxs) -> one row per coin. Length mismatch truncates to the paired prefix;
    HL indexes ctxs positionally against meta['universe'], so an unpaired tail has no identity."""
    universe = (meta or {}).get("universe") or []
    rows = []
    for asset, ctx in zip(universe, ctxs or []):
        coin = str((asset or {}).get("name") or "").strip()
        if not coin:
            continue
        ctx = ctx if isinstance(ctx, dict) else {}
        impact = ctx.get("impactPxs")
        impact = impact if isinstance(impact, (list, tuple)) and len(impact) == 2 else (None, None)
        rows.append((
            ts_ms, coin,
            _f(ctx.get("markPx")), _f(ctx.get("midPx")), _f(ctx.get("oraclePx")),
            _f(ctx.get("premium")), _f(ctx.get("funding")), _f(ctx.get("openInterest")),
            _f(ctx.get("dayNtlVlm")), _f(ctx.get("dayBaseVlm")), _f(ctx.get("prevDayPx")),
            _f(impact[0]), _f(impact[1]),
            1 if (asset or {}).get("isDelisted") else 0,
        ))
    return rows


def write_tick(conn, rows, *, ts_ms, fetch_ms, gap_s, status="ok", detail=None) -> int:
    conn.execute("BEGIN IMMEDIATE")
    try:
        if rows:
            conn.executemany(
                "INSERT OR IGNORE INTO hl_squeeze_marks (ts_ms,coin,mark_px,mid_px,oracle_px,"
                "premium,funding,open_interest,day_ntl_vlm,day_base_vlm,prev_day_px,impact_bid,"
                "impact_ask,is_delisted) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        conn.execute(
            "INSERT OR REPLACE INTO hl_squeeze_ticks "
            "(ts_ms,tape_version,coins,delisted,fetch_ms,gap_s,status,detail) VALUES (?,?,?,?,?,?,?,?)",
            (ts_ms, TAPE_VERSION, len(rows), sum(r[13] for r in rows), int(fetch_ms), gap_s,
             status, detail))
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    return len(rows)


def tick_once(conn, *, now_ms=None, fetch=None, last_ts_ms=None) -> dict:
    """One capture. A provider failure is recorded as an error tick, never a silent hole."""
    fetch = fetch or (lambda: _post_info({"type": "metaAndAssetCtxs"}))
    started = time.monotonic()
    ts_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    gap_s = (ts_ms - last_ts_ms) / 1000.0 if last_ts_ms else None
    try:
        payload = fetch()
        meta, ctxs = payload[0], payload[1]
    except Exception as exc:
        fetch_ms = int((time.monotonic() - started) * 1000)
        write_tick(conn, [], ts_ms=ts_ms, fetch_ms=fetch_ms, gap_s=gap_s, status="error",
                   detail=f"{type(exc).__name__}: {exc}"[:200])
        logger.warning("squeeze tape fetch failed: %s: %s", type(exc).__name__, exc)
        return {"ts_ms": ts_ms, "coins": 0, "status": "error"}
    rows = normalize(meta, ctxs, ts_ms)
    fetch_ms = int((time.monotonic() - started) * 1000)
    write_tick(conn, rows, ts_ms=ts_ms, fetch_ms=fetch_ms, gap_s=gap_s)
    return {"ts_ms": ts_ms, "coins": len(rows),
            "delisted": sum(r[13] for r in rows), "fetch_ms": fetch_ms, "status": "ok"}


def write_heartbeat(path: str, payload: dict) -> None:
    tmp = f"{path}.tmp"
    Path(tmp).write_text(json.dumps(payload, sort_keys=True))
    os.replace(tmp, path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Hyperliquid squeeze-state tape (read-only)")
    parser.add_argument("--db-path", default=DEFAULT_DB)
    parser.add_argument("--heartbeat", default=DEFAULT_HEARTBEAT)
    parser.add_argument("--interval-seconds", type=float, default=60.0,
                        help="capture cadence; must stay well under the 300s prereg mark bound")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if args.interval_seconds <= 0 or args.interval_seconds > 300:
        parser.error("--interval-seconds must be in (0, 300]")

    # Single-instance lock held for the PROCESS LIFETIME, not just the watchdog invocation.
    # The watchdog matches a running process by "$CODE_ROOT/scripts/run_hl_squeeze_tape.py $ARGS", and
    # CODE_ROOT resolves through .runtime/hyperliquid/current — so repointing that symlink makes
    # the running process stop matching and the next cron tick starts a SECOND one against the
    # same DB. Two release repoints on 2026-08-03 left three copies of this worker writing one
    # ledger (visible as 2-4 ticks/minute on a 1/minute cadence). Only a lock the runner itself
    # holds can prevent that; a watchdog-side flock serialises watchdogs, not workers.
    _run_lock = acquire_run_lock(os.getenv(
        "HL_SQUEEZE_TAPE_RUN_LOCK", str(Path(args.db_path).resolve().parent / "hl_squeeze_tape.run.lock")))
    if _run_lock is None:
        print("another run_hl_squeeze_tape holds the run lock — refusing to start", file=sys.stderr)
        return 0


    stop = {"stop": False}
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_a: stop.update(stop=True))

    conn = connect(args.db_path)
    row = conn.execute("SELECT MAX(ts_ms) FROM hl_squeeze_ticks").fetchone()
    last_ts_ms = row[0] if row else None
    while True:
        result = tick_once(conn, last_ts_ms=last_ts_ms)
        last_ts_ms = result["ts_ms"]
        write_heartbeat(args.heartbeat, {
            "tape_version": TAPE_VERSION, "interval_s": args.interval_seconds,
            "db": os.path.abspath(args.db_path), **result,
        })
        logger.info("tick coins=%s status=%s", result["coins"], result["status"])
        if args.once or stop["stop"]:
            return 0
        slept = 0.0
        while slept < args.interval_seconds and not stop["stop"]:
            step = min(0.5, args.interval_seconds - slept)
            time.sleep(step)
            slept += step


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(main())
