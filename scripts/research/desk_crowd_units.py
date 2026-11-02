#!/usr/bin/env python3
"""Crowd-burst units (pre-reg docs/audits/2026-10-23-crowd-burst-abstain-preregistration.md). usage: DAY OUT.jsonl"""
import bisect, statistics, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import desk_qual_units as Q
import desk_tape_events as E


def w60(c, s):
    a, b = bisect.bisect_right(c.t, s - 60), bisect.bisect_right(c.t, s)
    return len({c.actor[k] for k in range(a, b)})


def feats(c, i):
    t, age = c.t[i], c.t[i] - c.t[0]
    cur = [w60(c, t - 10 * j) for j in range(0, 31)]               # s = t, t-10, ... t-300
    out = {"age": age, "w60": cur[0], "wmax60": max(cur[:7]), "wmax180": max(cur[:19]), "wmax300": max(cur)}
    hist = [w60(c, t - 60 - 60 * m) for m in range(1, 6) if t - 60 - 60 * m - 60 >= c.t[0]]
    out["hist_med"] = statistics.median(hist) if len(hist) >= 2 else None
    return out


Q.feats = feats
if __name__ == "__main__":
    Q.run(sys.argv[1], sys.argv[2])
