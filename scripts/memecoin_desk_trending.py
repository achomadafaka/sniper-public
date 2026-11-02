#!/usr/bin/env python3
"""Desk trending / king-of-the-hill board computed from our own U tape (rule trend-v1).

Pump.fun's frontend API is off limits (its terms bar automated access without express permission;
operator decision pending), so the board is built only from the Pump.fun curve trades producer U
already decodes. No provider call, no money path. Every minute, from swaps with received_at <= now
(causal; the host clock U stamps):
  - per coin over 5, 15 and 60 min: buy_usd, sell_usd, swaps, makers, first/last curve price, mcap now;
  - trending = top 50 coins by 15-min buy_usd, ties by makers then mint;
  - king of the hill = the highest-mcap coin still on its curve with a trade in the last 5 min.
Rows go to data/desk/trending/<UTC day>.db (append-only). mcap needs a fresh SOL/USD from U's heartbeat
(<= 120 s old); without one, USD fields are NULL, never 0. A U gap inside a window marks the board
coverage 'gap' instead of hiding it.

    MEMECOIN_DESK_TRENDING_ENABLED=1 python3 scripts/memecoin_desk_trending.py --loop
Cron line (operator installs): see scripts/memecoin_desk_trending_watchdog.sh.
"""
from __future__ import annotations

import argparse
import collections
import fcntl
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sniper.util.envfiles import load_env_files                     # noqa: E402
from sniper.util.state_paths import state_root                      # noqa: E402

RULE_VERSION = "trend-v1"
WINDOWS = (300, 900, 3600)
TOP_N = 50
PASS_S = 60.0
SOL_MAX_AGE_S = 120.0
LATE_SLOTS = 200
# curve-k-v1: a standard Pump.fun curve keeps virtual SOL x virtual tokens at its launch product, 30 SOL x
# 1,073,000,000 tokens (lamports x 6-decimal atomic units). Within 1 % of it = "standard", otherwise
# "nonstandard" (e.g. mayhem-type curves, ~1,858 virtual SOL seen 2026-10-21). Kept in the v1 ranking and
# labelled so a reader can split (Gamma/Alpha); a split ranking is trend-v2.
STANDARD_K = 30 * 10**9 * 1_073_000_000 * 10**6
CURVE_RULE = "curve-k-v1"


def curve_kind(vq, vt):
    if not vq or not vt:
        return None
    return "standard" if abs(vq * vt / STANDARD_K - 1) <= 0.01 else "nonstandard"                                # re-read the last slots each pass: late rows land there

DDL = """
CREATE TABLE IF NOT EXISTS boards (at REAL NOT NULL, rule_version TEXT NOT NULL, coverage TEXT NOT NULL,
    sol_usd REAL, coins INTEGER NOT NULL, koth_mint TEXT, koth_mcap_usd REAL, koth_curve_kind TEXT,
    curve_rule TEXT, PRIMARY KEY(at));
CREATE TABLE IF NOT EXISTS ranks (at REAL NOT NULL, rank INTEGER NOT NULL, mint TEXT NOT NULL,
    buy_usd_5m REAL, buy_usd_15m REAL, buy_usd_60m REAL, sell_usd_15m REAL, swaps_15m INTEGER,
    makers_15m INTEGER, change_15m_pct REAL, mcap_usd REAL, curve_kind TEXT, PRIMARY KEY(at, rank));
"""


def utc_day(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def ro(path):
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=30) if Path(path).exists() else None


class Tape:
    """The last hour of U curve trades in memory, read incrementally by slot."""
    COLS = "slot, signature, ordinal, mint, is_buy, quote_atomic, actor, virtual_quote, virtual_token, real_token, received_at"

    def __init__(self, desk_dir):
        self.desk_dir, self.rows, self.keys, self.cursor = Path(desk_dir), collections.deque(), set(), {}
        self.graduated = set()

    def _day_rows(self, day, since_t):
        c = ro(self.desk_dir / "swaps" / ("%s.db" % day))
        if c is None:
            return []
        try:
            last = self.cursor.get(day)
            if last is None:                            # first read of this file: find the slot bound by time
                lo = c.execute("SELECT MIN(slot) FROM swaps").fetchone()[0]
                hi = c.execute("SELECT MAX(slot) FROM swaps").fetchone()[0]
                if lo is None:
                    return []
                while lo < hi:                          # first slot whose rows were received at/after since_t
                    mid = (lo + hi) // 2
                    t = c.execute("SELECT MAX(received_at) FROM swaps WHERE slot BETWEEN ? AND ?",
                                  (lo, mid)).fetchone()[0]
                    lo, hi = (mid + 1, hi) if t is not None and t < since_t else (lo, mid)
                last = lo - 1 + LATE_SLOTS
            rows = c.execute("SELECT %s FROM swaps WHERE slot > ? ORDER BY slot, ordinal" % self.COLS,
                             (last - LATE_SLOTS,)).fetchall()
        finally:
            c.close()
        if rows:
            self.cursor[day] = max(self.cursor.get(day) or 0, rows[-1][0])
        elif day not in self.cursor:
            self.cursor[day] = last
        return rows

    def refresh(self, now):
        for day in sorted({utc_day(now - WINDOWS[-1]), utc_day(now)}):
            for r in self._day_rows(day, now - WINDOWS[-1]):
                key = (r[1], r[2])
                if key in self.keys or r[10] > now:
                    continue                            # duplicate re-read, or received after this pass's clock
                self.keys.add(key)
                self.rows.append(r)
                if r[9] == 0:
                    self.graduated.add(r[3])
        self.rows = collections.deque(sorted(self.rows, key=lambda r: r[10]))
        while self.rows and self.rows[0][10] <= now - WINDOWS[-1]:
            old = self.rows.popleft()
            self.keys.discard((old[1], old[2]))


def board(rows, graduated, now, sol_usd):
    """(ranks, koth) from rows received in (now - 60 min, now]; pure."""
    usd = (lambda atomic: atomic / 1e9 * sol_usd) if sol_usd else (lambda atomic: None)
    per = {}
    for _, _, _, mint, is_buy, q, actor, vq, vt, _, t in rows:
        if t > now or t <= now - WINDOWS[-1]:
            continue
        p = per.setdefault(mint, {"buy": {w: 0 for w in WINDOWS}, "sell15": 0, "swaps15": 0, "makers15": set(),
                                  "first15": None, "last": None, "last_t": None, "kind": None})
        price = (vq / 1e9) / (vt / 1e6) if vq and vt else None
        age = now - t
        for w in WINDOWS:
            if age < w and is_buy:
                p["buy"][w] += q
        if age < 900:
            p["swaps15"] += 1
            p["makers15"].add(actor)
            if not is_buy:
                p["sell15"] += q
            if price is not None and p["first15"] is None:
                p["first15"] = price
        if price is not None and (p["last_t"] is None or t >= p["last_t"]):
            p["last"], p["last_t"], p["kind"] = price, t, curve_kind(vq, vt)
    out = []
    for mint, p in per.items():
        mcap = p["last"] * 1e9 * sol_usd if p["last"] is not None and sol_usd else None
        change = (p["last"] / p["first15"] - 1) * 100 if p["first15"] and p["last"] is not None else None
        out.append({"mint": mint, "buy_usd_5m": usd(p["buy"][300]), "buy_usd_15m": usd(p["buy"][900]),
                    "buy_usd_60m": usd(p["buy"][3600]), "sell_usd_15m": usd(p["sell15"]), "swaps_15m": p["swaps15"],
                    "makers_15m": len(p["makers15"]), "change_15m_pct": change, "mcap_usd": mcap,
                    "curve_kind": p["kind"],
                    "_buy15_atomic": p["buy"][900], "_last_t": p["last_t"], "_last": p["last"]})
    ranked = sorted((c for c in out if c["swaps_15m"]), key=lambda c: (-c["_buy15_atomic"], -c["makers_15m"], c["mint"]))
    live = [c for c in out if c["mint"] not in graduated and c["_last_t"] is not None and now - c["_last_t"] < 300
            and c["_last"] is not None]
    koth = max(live, key=lambda c: (c["_last"], c["mint"]), default=None)
    return ranked[:TOP_N], koth, len(out)


def coverage(desk_dir, now):
    """'ok', or 'gap' when U's heartbeat is stale or a U gap row falls inside the hour, or 'curve_retired' when the
    curve tape is retired (operator 2026-10-27): an empty board then means NO DATA, never no activity."""
    try:
        beat = json.loads((Path(desk_dir) / "universe.heartbeat").read_text())
        if now - float(beat["ts"]) > 10:
            return "gap", None
        sol = beat.get("sol_usd") if (beat.get("sol_age_s") or 1e9) <= SOL_MAX_AGE_S else None
        if beat.get("curve_capture") is False:
            return "curve_retired", sol
    except (OSError, ValueError, KeyError, TypeError):
        return "gap", None
    for day in sorted({utc_day(now - WINDOWS[-1]), utc_day(now)}):
        g = ro(Path(desk_dir) / "gaps" / ("%s.db" % day))
        if g is not None:
            try:
                if g.execute("SELECT 1 FROM gaps WHERE at > ? AND at <= ? LIMIT 1", (now - WINDOWS[-1], now)).fetchone():
                    return "gap", sol
            finally:
                g.close()
    return "ok", sol


def write(out_dir, now, ranks, koth, coins, cov, sol):
    db = sqlite3.connect(Path(out_dir) / ("%s.db" % utc_day(now)), timeout=30)
    db.executescript(DDL)
    db.execute("INSERT OR IGNORE INTO boards VALUES (?,?,?,?,?,?,?,?,?)",
               (now, RULE_VERSION, cov, sol, coins, koth["mint"] if koth else None, koth["mcap_usd"] if koth else None,
                koth["curve_kind"] if koth else None, CURVE_RULE))
    db.executemany("INSERT OR IGNORE INTO ranks VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   [(now, i + 1, c["mint"], c["buy_usd_5m"], c["buy_usd_15m"], c["buy_usd_60m"], c["sell_usd_15m"],
                     c["swaps_15m"], c["makers_15m"], c["change_15m_pct"], c["mcap_usd"], c["curve_kind"])
                    for i, c in enumerate(ranks)])
    db.commit()
    db.close()


def main(argv=None):
    from sniper.util.state_paths import require_state_root_outside_release
    require_state_root_outside_release()   # never a state root inside .runtime/ (Gamma 2026-10-21)
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--loop", action="store_true")
    a = p.parse_args(argv)
    load_env_files(str(state_root()))
    if os.getenv("MEMECOIN_DESK_TRENDING_ENABLED", "0").strip() != "1":
        print("[desk-trending] MEMECOIN_DESK_TRENDING_ENABLED is not 1; exiting")
        return 0
    desk = Path(state_root()) / "data" / "desk"
    out = desk / "trending"
    out.mkdir(parents=True, exist_ok=True)
    lock = open(out / "trending.lock", "w")
    try:                                               # one writer: a second start exits
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[desk-trending] another trending worker holds the lock; exiting")
        return 0
    tape = Tape(desk)
    try:
        while True:
            now = time.time()
            tape.refresh(now)
            cov, sol = coverage(desk, now)
            ranks, koth, coins = board(tape.rows, tape.graduated, now, sol)
            write(out, now, ranks, koth, coins, cov, sol)
            print(json.dumps({"at": round(now, 1), "coins": coins, "coverage": cov, "koth": koth and koth["mint"],
                              "pass_ms": round((time.time() - now) * 1000)}), flush=True)
            if not a.loop:
                return 0
            time.sleep(max(1.0, PASS_S - (time.time() - now)))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
