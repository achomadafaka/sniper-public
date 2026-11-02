#!/usr/bin/env python3
"""BSC 56 fast lane read (steps 1 and 2). Read-only on the 56 watcher's log and the lane's own SQLite
file (data/memecoin_bsc_fast_lane.db; never the funded ledger).

Reports, over --since:
  decide     every 56 detection's lane outcome: would_send share, the fallback split per reason,
             whether the datagram went out, and the decide time (p50/p90); the tape-priced hops
             re-read 2 s later and how many were late (Gamma's hop_late)
  hand-off   the watcher's own detection hand-off, arrival -> wake_sent and commit -> wake_sent
             (p50/p90). The lane's signal runs AFTER the wake, so these must not move (Alpha).
  runner     runner_shadow: signals, reasons, handoff ms (watcher -> runner), busy and thread CPU ms,
             lock-busy share; the funded hook's verdicts (split, hook ms p50/p90); the DexScreener
             budget; GATE once >= 5 signals

    python3 scripts/research/bsc_fast_lane_read.py --since 2026-10-02T02:00:00+00:00
"""
import argparse
import collections
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

_STAMPS = re.compile(r"(arrival|decoded_at|commit_at|wake_sent_at)=(\S+)")
_LANE = re.compile(r"\[bsc-lane\] (\S+) reason=(\S+) sent=(\S+) decide_ms=([0-9.]+)")
_HOP = re.compile(r"\[bsc-lane\] hop_check \S+ .* late=(\S+)")


def q(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))] if xs else None


def dist(label, xs, fmt="%.2f"):
    if not xs:
        print("  %s: n=0" % label)
        return
    print(("  %s: n=%d p50 " + fmt + " p90 " + fmt + " max " + fmt)
          % (label, len(xs), q(xs, .5), q(xs, .9), max(xs)))


def epoch(iso):
    try:
        return datetime.fromisoformat(str(iso)).timestamp()
    except ValueError:
        return None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True)
    ap.add_argument("--log", default=None)
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)
    from sniper.util.state_paths import state_path
    path = args.log or str(state_path("logs", "onchain_evm_armed.56.log"))
    since = epoch(args.since)
    reasons, sent, decide_ms, arrival_wake, commit_wake = collections.Counter(), 0, [], [], []
    hop_checks = collections.Counter()
    for line in open(path, errors="replace"):
        # [bsc-lane] lines carry the watcher's own timestamp first; a detection line is printed bare,
        # so its arrival stamp dates it.
        stamps = {k: epoch(v) for k, v in _STAMPS.findall(line)}
        at = epoch(line.split(" ", 1)[0]) or stamps.get("arrival")
        if at is None or at < since:
            continue
        m = _HOP.search(line)
        if m:
            hop_checks[m.group(1)] += 1
            continue
        m = _LANE.search(line)
        if m:
            reasons[m.group(2)] += 1
            sent += m.group(3) == "True"
            decide_ms.append(float(m.group(4)))
            continue
        if "[onchain-evm] detection " in line:
            if stamps.get("arrival") and stamps.get("wake_sent_at"):
                arrival_wake.append((stamps["wake_sent_at"] - stamps["arrival"]) * 1000.0)
            if stamps.get("commit_at") and stamps.get("wake_sent_at"):
                commit_wake.append((stamps["wake_sent_at"] - stamps["commit_at"]) * 1000.0)
    total = sum(reasons.values())
    print("56 lane decisions since %s: %d" % (args.since, total))
    if total:
        print("  would_send %d (%.1f%%), datagrams sent %d" % (
            reasons["would_send"], 100.0 * reasons["would_send"] / total, sent))
        print("  fallbacks:", ", ".join("%s %d" % kv for kv in reasons.most_common()
                                        if kv[0] != "would_send") or "none")
    dist("decide + datagram ms", decide_ms, "%.3f")
    print("tape-priced hops re-read 2 s later: %d, hop_late %d, masked %d (a later Sync hid the "
          "window), unknown %d" % (
              sum(hop_checks.values()), hop_checks["1"], hop_checks["masked"],
              sum(n for k, n in hop_checks.items() if k.startswith("unknown"))))
    print("watcher hand-off (must not move):")
    dist("arrival -> wake_sent ms", arrival_wake, "%.1f")
    dist("commit -> wake_sent ms", commit_wake, "%.2f")
    runner(args.db or str(state_path("data", "memecoin_bsc_fast_lane.db")), args.since)
    return 0


def runner(path, since_iso):
    import sqlite3
    try:
        db = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
        db.row_factory = sqlite3.Row
        events = [dict(r) for r in db.execute("SELECT * FROM bsc_lane_events WHERE written_at >= ?",
                                              (since_iso,))]
        verdicts = [dict(r) for r in db.execute(
            "SELECT * FROM bsc_lane_verdicts WHERE written_at >= ?", (since_iso,))]
        beats = [dict(r) for r in db.execute("SELECT * FROM bsc_lane_beat WHERE at >= ? ORDER BY at",
                                             (since_iso,))]
    except sqlite3.Error:                      # no signal has reached the runner's lane yet
        print("runner lane: no records")
        return
    print("runner lane signals: %d" % len(events))
    print("  reasons:", ", ".join("%s %d" % kv for kv in collections.Counter(
        e["reason"] for e in events).most_common()) or "none")
    dist("handoff ms (watcher -> runner)", [e["handoff_ms"] for e in events])
    dist("busy ms (lane thread)", [e["busy_ms"] for e in events])
    cpu = [e["cpu_ms"] for e in events if e["cpu_ms"] is not None]
    dist("cpu ms (lane thread GIL hold)", cpu)
    if events:
        print("  lock busy: %d of %d" % (sum(e["lock_busy"] for e in events), len(events)))
    split = collections.Counter(
        v["verdict"].split(":")[1] if str(v["verdict"]).startswith("hook:") else v["verdict"]
        for v in verdicts)
    print("  hook verdicts:", ", ".join("%s %d" % kv for kv in split.most_common()) or "none")
    dist("hook ms (background verdict, GIL)", [v["hook_ms"] for v in verdicts
                                               if v["hook_ms"] is not None])
    if beats:
        first, last = beats[0], beats[-1]
        print("  dexscreener: %d calls, %d 429s%s" % (
            max(0, last["dex_calls"] - first["dex_calls"]), max(0, last["dex_429"] - first["dex_429"]),
            "; STOPPED (%s)" % last["dex_stopped"] if last["dex_stopped"] else ""))
    bases = collections.Counter(v.get("mcap_basis") for v in verdicts if v.get("mcap") is not None)
    if bases:
        print("  mcap basis:", ", ".join("%s %d" % kv for kv in bases.most_common()),
              "(funded 56 uses chain totalSupply x source price: compare before trusting mcap gates)")
    live_rows(db, since_iso)
    print("GATE runner_shadow signals: %s. A hook pass here EXCLUDES token safety and the wallet "
          "snapshot (no 56 key in runner_shadow), so it overstates live admission." % (
              "%d of 5, met" % len(events) if len(events) >= 5 else "%d of 5" % len(events)))



def live_rows(db, since_iso):
    """MEMECOIN_BSC_FAST_LANE=live: outcomes, the safety path (a cache hit, or the inline check on
    a miss, timed apart: Alpha 2026-10-02), send and decide-to-send ms, blocks behind the source,
    and the fill checks (receipt against the block-pinned balance delta)."""
    import sqlite3
    try:
        rows = [dict(r) for r in db.execute("SELECT * FROM bsc_lane_live WHERE written_at >= ?",
                                            (since_iso,))]
        checks = [dict(r) for r in db.execute(
            "SELECT agree FROM bsc_lane_fill_checks WHERE written_at >= ?", (since_iso,))]
    except sqlite3.Error:                      # the lane never ran live
        return
    print("live lane signals: %d" % len(rows))
    print("  outcomes:", ", ".join("%s %d" % kv for kv in collections.Counter(
        str(r["reason"]).split(":")[0] for r in rows).most_common()) or "none")
    paths = collections.Counter(r["safety_path"] for r in rows if r["safety_path"])
    print("  safety: cache hits %d, inline checks %d" % (paths["cache"], paths["inline"]))
    dist("inline safety ms (cache miss)", [r["safety_ms"] for r in rows
                                           if r["safety_path"] == "inline" and r["safety_ms"] is not None])
    dist("busy ms, cache hit", [r["busy_ms"] for r in rows
                                if r["safety_path"] == "cache" and r["busy_ms"] is not None])
    dist("send ms", [r["send_ms"] for r in rows if r["send_ms"] is not None])
    dist("decide to send ms", [r["decide_to_send_ms"] for r in rows
                               if r["decide_to_send_ms"] is not None])
    dist("blocks behind the source", [r["blocks_behind"] for r in rows
                                      if r["blocks_behind"] is not None], "%d")
    if checks:
        print("  fill checks: %d, receipt and delta agree %d" % (
            len(checks), sum(c["agree"] for c in checks)))


if __name__ == "__main__":
    sys.exit(main())
