#!/usr/bin/env python3
"""Recover U block times for a day whose swaps/launches were written with block_time NULL (before
271874619). Read-only on the U day files; writes data/desk/blocks_backfill/<day>.db.

Anchors: a candles_1s row's slot_lo and slot_hi both carry block time t exactly (BarBuilder places a
slot only by its block_meta time). Block time never decreases with slot, so a slot between two
anchors has a time in [t_prev, t_next]; when t_prev == t_next that time is exact. Every swap/launch
slot gets (lo, hi); exact = lo == hi. Readers use exact rows as block time and treat the rest as an
interval, never a point.

    python3 scripts/research/desk_block_time_backfill.py 2026-10-21 [<state root>/data/desk]
"""
import bisect
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def ro(path):
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=30)


def intervals(anchors, slots):
    """anchors: {slot: t}; slots: iterable. {slot: (lo, hi)} with None for an open side."""
    keys = sorted(anchors)
    out = {}
    for s in slots:
        if s in anchors:
            out[s] = (anchors[s], anchors[s])
            continue
        i = bisect.bisect_left(keys, s)
        lo = anchors[keys[i - 1]] if i > 0 else None
        hi = anchors[keys[i]] if i < len(keys) else None
        out[s] = (lo, hi)
    return out


def main(day, base=None):
    desk = Path(base or ROOT / "data" / "desk")
    anchors = {}
    for lo, hi, t in ro(desk / "candles_1s" / ("%s.db" % day)).execute("SELECT slot_lo, slot_hi, t FROM candles_1s"):
        anchors[lo] = anchors[hi] = t
    slots = set()
    for stream in ("swaps", "launches"):
        slots |= {r[0] for r in ro(desk / stream / ("%s.db" % day)).execute(
            "SELECT DISTINCT slot FROM %s WHERE block_time IS NULL" % stream)}
    out = desk / "blocks_backfill"
    out.mkdir(exist_ok=True)
    db = sqlite3.connect(out / ("%s.db" % day))
    db.execute("CREATE TABLE IF NOT EXISTS blocks_backfill(slot INTEGER PRIMARY KEY, block_time_lo INTEGER, "
               "block_time_hi INTEGER, exact INTEGER) WITHOUT ROWID")
    rows = [(s, lo, hi, int(lo is not None and lo == hi)) for s, (lo, hi) in intervals(anchors, slots).items()]
    db.executemany("INSERT OR REPLACE INTO blocks_backfill VALUES(?,?,?,?)", rows)
    db.commit()
    exact = sum(r[3] for r in rows)
    widths = sorted(r[2] - r[1] for r in rows if r[1] is not None and r[2] is not None)
    print("day=%s slots=%d anchors=%d exact=%d (%.1f%%) open=%d width_p50=%s width_max=%s"
          % (day, len(rows), len(anchors), exact, 100.0 * exact / max(1, len(rows)),
             sum(1 for r in rows if r[1] is None or r[2] is None),
             widths[len(widths) // 2] if widths else None, widths[-1] if widths else None))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
