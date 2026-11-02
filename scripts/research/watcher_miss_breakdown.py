#!/usr/bin/env python3
"""Why did a Solana on-chain detection get no attempt? Every watcher and routed detection, one class.

Alpha, 2026-09-27 (overnight board): the scoreboard's watcher line read 63.8% 'missed' (1,853
opportunities, 17 fills) -- a detection nobody acted on is the scariest number on the board. This
splits every detection into exactly one class, from the ledger and the runner log, read-only:

  ACTED ON BY THE ON-CHAIN LANE (an 'onchain:<sig>' attempt exists)
    filled            a position, or a filled attempt
    refused:<family>  the last attempt's reason, classified by refusal_infra_triage.classify
    failed / unknown  failed, or no terminal status
  NO ON-CHAIN ATTEMPT, in the order memecoin_onchain_lane.sweep_onchain_detections decides:
    not_armed         the wallet was not armed on Solana at the detection instant (approvals
                      ledger, solana_onchain_lane_funnel.armed_at): the lane skips it silently,
                      by design (the watcher also watches unarmed registry owners)
    arm_unknown       the approvals ledger cannot say
    j7_acted          a feed attempt on the same signature within +-120 s: any origin carrying the
                      signature from a lane other than onchain_watch / slot_lane, i.e. J7 wallets
                      ('swap:...:<sig>:...') AND J7 pump ('pump:<sig>'), the rule
                      already_attempted_from_another_lane applies: duplicate_of_feed. (A 'swap:'-only
                      match left 28 J7-pump duplicates looking unexplained on 2026-09-27.)
    slot_lane_acted   a 'slotlane:<sig>' attempt: slot_lane_filled / duplicate_of_slot_lane
    lane_down         the detection falls outside every lane-up window from the runner log
                      ('onchain lane started' until the next 'received SIGTERM' or restart),
                      allowing the lane's 45 s startup freshness before each start
    UNEXPLAINED       armed, lane up, no attempt on any lane: the residual that needs a reason

    python3 scripts/research/watcher_miss_breakdown.py [--days 7] [--list 20]
    python3 scripts/research/watcher_miss_breakdown.py --selftest
"""
import argparse
import bisect
import collections
import contextlib
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

REPO = "/home/bigpapi/Desktop/Sniper"
LOG = os.path.join(REPO, "logs", "memecoin_runner.log")
MAX_AGE_S = 45.0          # the lane's freshness (onchain lane started ... max_age=45s)
LOOKBACK_S = 120.0        # the lane's own duplicate window


def ts(text):
    return datetime.fromisoformat(str(text).replace("Z", "+00:00"))


def up_windows(lines):
    """[(start, end)] lane-up windows from stamped runner-log lines; end None = still up."""
    out, start = [], None
    for line in lines:
        stamp = line[:32]
        if "onchain lane started" in line:
            if start is not None:
                out.append((start, stamp))          # restarted without a SIGTERM line
            start = stamp
        elif "runner received SIGTERM" in line and start is not None:
            out.append((start, stamp))
            start = None
    if start is not None:
        out.append((start, None))
    return [(ts(a), ts(b) if b else None) for a, b in out]


def lane_was_up(windows, when):
    """True when `when` lies in an up window, or within MAX_AGE_S before one starts (the startup
    cursor still sweeps a detection that is fresh at the start)."""
    for start, end in windows:
        if start - timedelta(seconds=MAX_AGE_S) <= when and (end is None or when <= end):
            return True
    return False


def classify_detection(onchain_atts, has_position, armed, j7_atts, slot_atts, lane_up, family_fn):
    if onchain_atts:
        if has_position or any(a[0] == "filled" for a in onchain_atts):
            return "filled"
        status, reason = onchain_atts[-1]
        if status in ("refused", "aborted", "safety_failed"):
            return "refused:" + family_fn(reason)[1]
        return "failed" if status == "failed" else "unknown"
    if armed is False:
        return "not_armed"
    if armed is None:
        return "arm_unknown"
    if j7_atts:
        return "j7_acted"
    if slot_atts:
        return "slot_lane_acted"
    if not lane_up:
        return "lane_down"
    return "UNEXPLAINED"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--list", type=int, default=15, help="print up to N UNEXPLAINED detections")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    sys.path.insert(0, REPO)
    sys.path.insert(0, os.path.join(REPO, "scripts", "research"))
    from solana_onchain_lane_funnel import armed_at
    from refusal_infra_triage import classify
    hi = datetime.now(timezone.utc)
    lo = hi - timedelta(days=a.days)
    with open(LOG, errors="replace") as fh:
        lines = [l for l in fh if "onchain lane started" in l or "runner received SIGTERM" in l
                 or "onchain lane: " in l]
    windows = up_windows([l for l in lines if "onchain lane: " not in l])
    lane_lines = [l for l in lines if "onchain lane: " in l]
    lane_stamps = [l[:32] for l in lane_lines]
    classes = {"watcher": collections.Counter(), "routed": collections.Counter()}
    j7_outcomes = collections.Counter()
    unexplained = []
    with contextlib.closing(sqlite3.connect("file:%s/sniper_tracker.db?mode=ro" % REPO, uri=True,
                                            timeout=30)) as c:
        c.row_factory = sqlite3.Row
        dets = c.execute(
            "SELECT signature, source_user_id, handle, mint, detected_at, "
            "json_extract(source_valuation_json, '$.basis') AS basis FROM onchain_wallet_detections "
            "WHERE network_id='solana' AND detected_at >= ? AND detected_at <= ? ORDER BY id",
            (lo.isoformat(), hi.isoformat())).fetchall()
        for d in dets:
            sig, when = d["signature"], ts(d["detected_at"])
            lo_s = (when - timedelta(seconds=LOOKBACK_S)).isoformat()
            hi_s = (when + timedelta(seconds=LOOKBACK_S)).isoformat()
            onchain = c.execute(
                "SELECT terminal_status, terminal_reason, position_id FROM memecoin_entry_attempts "
                "WHERE origin_event_id=? ORDER BY decision_started_at", ("onchain:" + sig,)).fetchall()
            has_pos = any(r[2] for r in onchain) or c.execute(
                "SELECT 1 FROM memecoin_positions WHERE origin_event_id=?", ("onchain:" + sig,)).fetchone() is not None
            j7 = c.execute(
                "SELECT terminal_status FROM memecoin_entry_attempts WHERE decision_started_at >= ? "
                "AND decision_started_at <= ? AND signal_source NOT IN ('onchain_watch', 'slot_lane') "
                "AND origin_event_id LIKE ?",
                (lo_s, hi_s, "%" + sig + "%")).fetchall()
            slot = c.execute("SELECT 1 FROM memecoin_entry_attempts WHERE origin_event_id=?",
                             ("slotlane:" + sig,)).fetchall()
            cls = classify_detection(
                [(r[0], r[1]) for r in onchain], has_pos,
                armed_at(c, d["source_user_id"], d["detected_at"]) if not onchain else True,
                j7, slot, lane_was_up(windows, when), classify)
            group = "routed" if d["basis"] == "slot_lane_routed" else "watcher"
            classes[group][cls] += 1
            if cls == "j7_acted":
                j7_outcomes[j7[-1][0] or "no_status"] += 1
            if cls == "UNEXPLAINED":
                # The lane's own log line for this coin near the detection, if it wrote one.
                i = bisect.bisect_left(lane_stamps, d["detected_at"][:32])
                near = [l.split("-> ", 1)[-1].strip()[:60] for l in lane_lines[max(0, i - 3):i + 6]
                        if (d["handle"] or "") in l and d["mint"][:8] in l]
                unexplained.append((d["detected_at"][:19], d["handle"], d["mint"][:8], sig[:12],
                                    near[:1]))
    print("window %s .. %s | lane-up windows in log: %d" % (lo.isoformat(timespec="seconds"),
                                                              hi.isoformat(timespec="seconds"), len(windows)))
    for group, cnt in classes.items():
        n = sum(cnt.values())
        print("\n%s detections: %d" % (group, n))
        for cls, k in sorted(cnt.items(), key=lambda kv: -kv[1]):
            print("  %-34s %5d  (%.1f%%)" % (cls, k, 100.0 * k / n if n else 0))
    print("\nj7_acted outcomes (J7's last attempt status): %s" % dict(j7_outcomes))
    print("\nUNEXPLAINED (armed, lane up, no attempt on any lane): %d" % len(unexplained))
    for row in unexplained[:a.list]:
        print("  %s %-16s %s sig %s  lane log: %s" % row)


def selftest():
    fam = lambda reason: ("POLICY", "mcap_ceiling") if "mcap" in reason else ("X", "other")
    assert classify_detection([("filled", "")], False, True, [], [], True, fam) == "filled"
    assert classify_detection([("refused", "mcap $1 over ceiling")], False, True, [], [], True, fam) == "refused:mcap_ceiling"
    assert classify_detection([("refused", "x")], True, True, [], [], True, fam) == "filled"   # position wins
    assert classify_detection([], False, False, [("filled",)], [], True, fam) == "not_armed"
    assert classify_detection([], False, True, [("refused",)], [(1,)], True, fam) == "j7_acted"
    assert classify_detection([], False, True, [], [(1,)], True, fam) == "slot_lane_acted"
    assert classify_detection([], False, True, [], [], False, fam) == "lane_down"
    assert classify_detection([], False, True, [], [], True, fam) == "UNEXPLAINED"
    assert classify_detection([], False, None, [], [], True, fam) == "arm_unknown"
    lines = ["2026-09-27T10:00:00.000000+00:00 [memecoin] onchain lane started (cursor=1, max_age=45s)",
             "2026-09-27T11:00:00.000000+00:00 [memecoin] runner received SIGTERM — exiting",
             "2026-09-27T11:01:00.000000+00:00 [memecoin] onchain lane started (cursor=2, max_age=45s)",
             "2026-09-27T12:00:00.000000+00:00 [memecoin] onchain lane started (cursor=3, max_age=45s)"]
    w = up_windows(lines)
    assert len(w) == 3 and w[0][1] == ts("2026-09-27T11:00:00+00:00") and w[1][1] == ts("2026-09-27T12:00:00+00:00")
    assert w[2][1] is None
    assert lane_was_up(w, ts("2026-09-27T10:30:00+00:00"))
    assert not lane_was_up(w, ts("2026-09-27T11:00:10+00:00"))          # down between SIGTERM and start
    assert lane_was_up(w, ts("2026-09-27T11:00:30+00:00"))              # fresh at the 11:01 start
    assert lane_was_up(w, ts("2026-09-28T00:00:00+00:00"))              # still up
    print("selftest ok")


if __name__ == "__main__":
    main()
