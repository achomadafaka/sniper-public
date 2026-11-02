#!/usr/bin/env python3
"""After-numbers for the rail wake: from a woken price mark to the exit decision's exact quote.

The rail wake (5bc3ad3f, fixed per-source in 49b3d20a, live 2026-09-27 09:10:11Z) runs a FOCUS exit
pass when a Solana price mark crosses a rail or moves 5%. Each wake prints
    [memecoin] rail wake mint=.. reason=.. ratio=.. slot=S src=.. ids=P mark_to_wake_ms=A wake_to_now_ms=B
at the loop, B ms after the wake; the focus pass then prints `rail wake focus pass ... pass_ms=`.
For every wake record this pairs the NEXT exit-decision exact quote for that position
(memecoin_route_series seam='exit_decision', the only route-space series; its quote carries the
provider's context_slot) and reports:
  wake_to_quote_ms   quote observed_at minus the mark's receipt (print time minus B minus A)
  slots              quote context_slot minus the mark's slot S: how far past the change the
                     deciding quote's state is
Before (HANDOFF 2026-09-27 04:18Z): chain change -> next exact quote p50 665 / p90 1,663 ms on the
1 s tick; WS notify lag at confirmed p50 386 / p90 495 ms (not in this number; it precedes the mark).

TWO CLOCKS. The log line carries the HOST clock (`_p`), and route_series.observed_at is corrected UTC
(`real_utc_now_iso`, host + data/clock_offset.json offset_s), so wake_to_quote_ms is biased by
+offset_s (+47 ms at 2026-09-27 13:49Z). The offset history is not stored, so it is reported, not
removed. `slots` is clock-free. The next quote is taken in id order (the clock stepped backward
562 times on 2026-09-09), inside an observed_at window.
Read-only: the runner log and one indexed ledger read per wake.

    python3 scripts/research/rail_wake_latency.py [--since 2026-09-27T09:10:11+00:00]
"""
import argparse
import contextlib
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from sniper.persistence.db_core import get_readonly_connection  # noqa: E402

LOG = REPO / "logs" / "memecoin_runner.log"
LEDGER = str(REPO / "sniper_tracker.db")
WAKE = re.compile(r"^(\S+) \[memecoin\] rail wake mint=(\S+) reason=(\S+) ratio=(\S+) slot=(\S+) "
                  r"src=(\S+) ids=(\S+) mark_to_wake_ms=(\S+) wake_to_now_ms=(\S+)")
PASS = re.compile(r"^(\S+) \[memecoin\] rail wake focus pass ids=\S+ ok=(\S+) pass_ms=(\d+)")
HORIZON_S = 10.0


def parse(since, path=LOG):
    wakes, passes = [], []
    with open(path, errors="replace") as fh:
        for line in fh:
            if "rail wake" not in line or line[:32] < since[:32]:
                continue
            hit = WAKE.match(line)
            if hit:
                printed = datetime.fromisoformat(hit[1])
                mark_to_wake = float(hit[8]) if float(hit[8]) >= 0 else 0.0
                wakes.append({"at": printed - timedelta(milliseconds=float(hit[9]) + mark_to_wake),
                              "reason": hit[3], "slot": int(hit[5]) if hit[5].isdigit() else None,
                              "src": hit[6], "ids": [int(i) for i in hit[7].split(",") if i]})
                continue
            hit = PASS.match(line)
            if hit:
                passes.append({"ok": hit[2] == "True", "pass_ms": float(hit[3])})
    return wakes, passes


def pct(xs, q):
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(q * len(xs)))], 1) if xs else None


def main(since):
    wakes, passes = parse(since)
    rows = []
    with contextlib.closing(get_readonly_connection(LEDGER)) as c:
        for w in wakes:
            lo = w["at"].isoformat()
            hi = (w["at"] + timedelta(seconds=HORIZON_S)).isoformat()
            for pid in w["ids"]:
                q = c.execute(
                    "SELECT observed_at, quote_evidence_json FROM memecoin_route_series "
                    "WHERE position_id=? AND seam='exit_decision' AND observed_at>=? AND observed_at<? "
                    "ORDER BY id LIMIT 1", (pid, lo, hi)).fetchone()
                if q is None:
                    rows.append({**w, "pid": pid, "ms": None, "slots": None})
                    continue
                slot = (json.loads(q[1] or "{}") or {}).get("context_slot")
                rows.append({**w, "pid": pid,
                             "ms": (datetime.fromisoformat(q[0]) - w["at"]).total_seconds() * 1000,
                             "slots": (slot - w["slot"]) if slot and w["slot"] else None})
    by = defaultdict(list)
    for r in rows:
        by[r["reason"]].append(r)
    out = {"since": since, "wakes": len(wakes), "positions": len({r["pid"] for r in rows}),
           "focus_passes": len(passes), "failed_passes": sum(not p["ok"] for p in passes),
           "pass_ms_p50_p90": (pct([p["pass_ms"] for p in passes], .5),
                               pct([p["pass_ms"] for p in passes], .9))}
    for name, group in [("ALL", rows)] + sorted(by.items()):
        ms = [r["ms"] for r in group if r["ms"] is not None]
        sl = [r["slots"] for r in group if r["slots"] is not None]
        out[name] = {"n": len(group), "no_quote_in_%ds" % HORIZON_S: len(group) - len(ms),
                     "wake_to_quote_ms_p50_p90": (pct(ms, .5), pct(ms, .9)),
                     "slots_p50_p90": (pct(sl, .5), pct(sl, .9)),
                     "stale_quote_slot_lt_mark": sum(1 for s in sl if s < 0)}
    print(json.dumps(out, indent=1))
    print("per position:", dict(sorted(((p, sum(1 for r in rows if r["pid"] == p))
                                        for p in {r["pid"] for r in rows}))))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-09-27T09:10:11+00:00")
    main(ap.parse_args().since)
