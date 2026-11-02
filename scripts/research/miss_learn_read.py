#!/usr/bin/env python3
"""Every slot-lane miss-learn line since --since, and what each retry became.

Alpha, 2026-09-27 (overnight board). 29b6789d (Lambda's MissLearner) takes a slot-lane buy whose
PumpSwap pool the lane did not know, reads the pool at once, and puts the buy back on the lane
ONCE when the read lands inside the budget. memecoin_slot_lane.MissLearner._line writes

    [memecoin] slot lane miss-learn <handle> <pool8> S=<source slot> learn_ms=.. read_ms=.. -> <outcome>

outcome: retry, or handoff:budget (the read outran the budget), handoff:late (learned, too late),
handoff:unlearnable (the read found no pool), handoff:queue_full. The line carries no signature,
so a retry is joined to the lane's NEXT decision line for the same handle and source slot:

    [memecoin] slot lane <handle> <mint8> S=<slot> -> <status> ... ms=.. head-S=.. recv-S=.. land-S=..

land-S is the landing slot minus the source slot (S+0 = the source's own slot). One source buy
must still give at most one confirmed reservation: routed_pump_conversion.py checks that across
every lane, retries included (a retry reuses the 'slotlane:<sig>' origin).

    python3 scripts/research/miss_learn_read.py --since 2026-09-27T23:02:04.794Z
    python3 scripts/research/miss_learn_read.py --selftest
"""
import argparse
import collections
import re
import statistics
from pathlib import Path

LOG = Path(__file__).resolve().parents[2] / "logs" / "memecoin_runner.log"
MISS = re.compile(r"^(\S+) \[memecoin\] slot lane miss-learn (\S+) (\S+) S=(\S+) learn_ms=([\d.]+) "
                  r"read_ms=(\S+) -> (\S+)")
DECISION = re.compile(r"^(\S+) \[memecoin\] slot lane (\S+) \S+ S=(\d+) -> (\S+).*? ms=\S+ head-S=\S+"
                      r"(?: recv-S=\S+)?(?: land-S=(\S+))?")


def read(lines, since):
    """[(stamp, handle, pool8, slot, learn_ms, read_ms or None, outcome, retry decision or None)]."""
    misses, decisions = [], []
    for line in lines:
        if "slot lane" not in line or line[:32] < since:
            continue
        m = MISS.match(line)
        if m:
            misses.append(m.groups())
            continue
        d = DECISION.match(line)
        if d and d.group(2) not in ("routed", "miss-learn", "pool", "handoff"):
            decisions.append(d.groups())
    out = []
    for stamp, handle, pool, slot, learn_ms, read_ms, outcome in misses:
        after = None
        if outcome == "retry":
            after = next(((d[3], d[4]) for d in decisions
                          if d[0] >= stamp and d[1] == handle and d[2] == slot), None)
        out.append((stamp, handle, pool, slot, float(learn_ms),
                    None if read_ms == "-" else float(read_ms), outcome, after))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    since = a.since.replace("Z", "+00:00")
    with open(LOG, errors="replace") as fh:
        rows = read(fh, since)
    print("miss-learn lines since %s: %d, outcomes %s" % (
        a.since, len(rows), dict(collections.Counter(r[6] for r in rows))))
    learn = [r[4] for r in rows]
    reads = [r[5] for r in rows if r[5] is not None]
    if rows:
        print("  learn_ms p50 %.1f max %.1f | read_ms p50 %s max %s" % (
            statistics.median(learn), max(learn),
            "%.1f" % statistics.median(reads) if reads else "-", "%.1f" % max(reads) if reads else "-"))
    for r in rows:
        stamp, handle, pool, slot, learn_ms, read_ms, outcome, after = r
        tail = ""
        if outcome == "retry":
            tail = (" -> lane %s land-S=%s" % after) if after else " -> no lane decision line found"
        print("  %s %-16s pool %s S=%s learn_ms %.1f read_ms %s %s%s" % (
            stamp[11:23], handle, pool, slot, learn_ms,
            "%.1f" % read_ms if read_ms is not None else "-", outcome, tail))


def selftest():
    lines = [
        "2026-09-27T23:01:00.000000+00:00 [memecoin] slot lane miss-learn old Pool1111 S=1 learn_ms=5.0 read_ms=4.0 -> retry\n",
        "2026-09-27T23:05:00.000000+00:00 [memecoin] slot lane miss-learn bob AbCdEfGh S=451130000 learn_ms=210.4 read_ms=205.1 -> retry\n",
        "2026-09-27T23:05:00.300000+00:00 [memecoin] slot lane bob Mint1234 S=451130000 -> filled  ms=640.2 head-S=0 recv-S=0 land-S=2\n",
        "2026-09-27T23:06:00.000000+00:00 [memecoin] slot lane miss-learn al XyZxYzXy S=451130100 learn_ms=900.0 read_ms=- -> handoff:budget\n",
        "2026-09-27T23:07:00.000000+00:00 [memecoin] slot lane miss-learn cy QqQqQqQq S=451130200 learn_ms=120.0 read_ms=110.0 -> retry\n",
        "2026-09-27T23:07:00.100000+00:00 [memecoin] slot lane cy Mint9999 S=451130999 -> filled  ms=600.0 head-S=0 recv-S=0 land-S=1\n",
    ]
    rows = read(lines, "2026-09-27T23:02:04.794+00:00")
    assert len(rows) == 3, rows                                       # the 23:01 line is before --since
    assert rows[0][6] == "retry" and rows[0][7] == ("filled", "2"), rows[0]
    assert rows[1][5] is None and rows[1][6] == "handoff:budget" and rows[1][7] is None
    assert rows[2][7] is None                                         # a different slot is not its decision
    print("selftest ok")


if __name__ == "__main__":
    main()
