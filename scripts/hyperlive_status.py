#!/usr/bin/env python3
"""Hyperlive monitor — one status line per call (cron-friendly): process health, per-track
state, and per-track realized paper PnL. Read-only."""
from __future__ import annotations

import json
import os
import sqlite3
import time

ENGINE_DB = "/home/bigpapi/Desktop/Sniper/data/hyperlive_engine.sqlite"
PIDS = {
    "engine": "/home/bigpapi/Desktop/Sniper/data/hyperlive_engine.pid",
    "cluster": "/home/bigpapi/Desktop/Sniper/data/cluster_map.pid",
    "binance": "/home/bigpapi/Desktop/Sniper/data/leadlag_capture.pid",
}


def _alive(pidfile: str) -> bool:
    try:
        pid = int(open(pidfile).read().strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError, FileNotFoundError):
        return False


def main() -> int:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    procs = " ".join(f"{k}={'up' if _alive(v) else 'DOWN'}" for k, v in PIDS.items())
    parts = [f"[{now}]", procs]
    if os.path.exists(ENGINE_DB):
        c = sqlite3.connect(f"file:{ENGINE_DB}?mode=ro", uri=True)
        row = c.execute("SELECT state_json, updated_ts_ms FROM hyperlive_state WHERE id=1").fetchone()
        if row:
            st = json.loads(row[0])
            age = (time.time() * 1000 - (row[1] or 0)) / 1000
            parts.append(f"state_age={age:.0f}s")
            for tname, t in (st.get("tracks") or {}).items():
                inpos = "IN-POS" if t.get("position") else t.get("state")
                parts.append(f"{tname}:{inpos}/cyc{t.get('cycle_id')}")
        try:
            for tname, n, wins, bps in c.execute(
                    "SELECT track, COUNT(*), COALESCE(SUM(won),0), COALESCE(SUM(realized_bps),0) "
                    "FROM hyperlive_cycles GROUP BY track"):
                wr = (wins / n) if n else 0.0
                parts.append(f"{tname}_pnl:n{n}/wr{wr:.0%}/{bps:+.0f}bps")
        except sqlite3.Error:
            pass
        c.close()
    else:
        parts.append("engine_db=missing")
    print(" ".join(parts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
