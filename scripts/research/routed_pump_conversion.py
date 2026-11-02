#!/usr/bin/env python3
"""After 46fe73f5: how many routed PumpSwap / curve buys went to the slot lane, and did any source
buy get two funded attempts?

Alpha, 2026-09-27 (overnight board). 46fe73f5 (MEMECOIN_SLOT_LANE_ROUTED_PUMP default 1) sends a
routed buy whose own frame carries exactly one pump buy into its coin onto the slot lane's pump
builders instead of the on-chain hand-off. Per routed buy after --since, from the runner log
(memecoin_slot_lane.route_routed_buy):

  converted   "slot lane routed lane <handle> <mint8> S=.. sig=.."; the lane then decides it
              ("slot lane <handle> <mint8> S=.. -> <status>", attempt 'slotlane:<sig>')
  handed off  "slot lane routed live ... -> <inserted|duplicate|write_failed>", and when a pump
              event was present but did not qualify, "slot lane routed pump <reason> <handle>
              sig=<sig>" (pool_unknown, no_pump_event_into_coin, multi_pump_event,
              pump_quote_not_sol, pump_pool_not_migrated, pump_conversion_error)

ONE SOURCE BUY, ONE FUNDED PATH. From the ledger: every attempt since --since whose origin carries
a transaction signature (swap:raybot:<sig>:.., pump:<sig>, onchain:<sig>, slotlane:<sig>), grouped
by signature. A signature with MORE THAN ONE CONFIRMED reservation (memecoin_pending_entries
status 'confirmed', i.e. two fills) is a DOUBLE FUNDED attempt, the defect this checks for. More
than one reservation in total is reported beside it and is normally benign: the pre-deploy control
(2026-09-26..27) had 20, every one a released first reservation followed by the retry lane or the
slot lane's fallback after a confirmed failure, both by design.

    python3 scripts/research/routed_pump_conversion.py --since 2026-09-27T22:48:00Z
    python3 scripts/research/routed_pump_conversion.py --selftest
"""
import argparse
import collections
import contextlib
import re
import sqlite3
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LOG = REPO / "logs" / "memecoin_runner.log"
SIG = r"[1-9A-HJ-NP-Za-km-z]{80,90}"
LANE = re.compile(r"^(\S+) \[memecoin\] slot lane routed lane (\S+) \S+ S=\d+ sig=(%s) " % SIG)
LIVE = re.compile(r"^(\S+) \[memecoin\] slot lane routed live (\S+) \S+ S=\d+ sig=(%s) .* -> (\S+)" % SIG)
PUMP = re.compile(r"^(\S+) \[memecoin\] slot lane routed pump (\S+) (\S+) sig=(%s)" % SIG)
TX_RE = re.compile(r"^%s$" % SIG)


def origin_signature(origin):
    """The transaction signature an attempt origin carries, or None (uuid J7 events carry none)."""
    parts = str(origin or "").split(":")
    cand = None
    if len(parts) >= 3 and parts[0] == "swap" and parts[1] == "raybot":
        cand = parts[2]
    elif len(parts) >= 2 and parts[0] in ("onchain", "pump", "slotlane"):
        cand = parts[1]
    return cand if cand and TX_RE.match(cand) else None


def parse_log(lines, since):
    """(converted sigs, {sig: handoff outcome}, {sig: pump reason})."""
    lane, live, pump = set(), {}, {}
    for line in lines:
        if "slot lane routed" not in line or line[:32] < since:
            continue
        m = LANE.match(line)
        if m:
            lane.add(m.group(3))
            continue
        m = LIVE.match(line)
        if m:
            live[m.group(3)] = m.group(4)
            continue
        m = PUMP.match(line)
        if m:
            pump[m.group(4)] = m.group(2)
    return lane, live, pump


def doubles(attempt_rows):
    """attempt_rows: (origin, attempt_id, reservation status or None). Returns
    ({sig: [origins]} with >1 CONFIRMED reservation, {sig: [origins]} with >1 reservation of any
    status, number of signatures attempted on more than one origin kind)."""
    by_sig = collections.defaultdict(list)
    for origin, attempt_id, status in attempt_rows:
        sig = origin_signature(origin)
        if sig:
            by_sig[sig].append((origin, attempt_id, status))
    double = {s: [o for o, _a, st in rows if st == "confirmed"] for s, rows in by_sig.items()
              if sum(1 for _o, _a, st in rows if st == "confirmed") > 1}
    reserved = {s: [o for o, _a, st in rows if st] for s, rows in by_sig.items()
                if sum(1 for _o, _a, st in rows if st) > 1}
    multi = sum(1 for rows in by_sig.values() if len({o.split(":")[0] for o, _a, _r in rows}) > 1)
    return double, reserved, multi


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    since = a.since.replace("Z", "+00:00")
    with open(LOG, errors="replace") as fh:
        lane, live, pump = parse_log(fh, since)
    with contextlib.closing(sqlite3.connect("file:%s?mode=ro" % (REPO / "sniper_tracker.db"),
                                            uri=True, timeout=10)) as c:
        rows = c.execute(
            "SELECT a.origin_event_id, a.attempt_id, "
            "(SELECT p.status FROM memecoin_pending_entries p WHERE p.attempt_id=a.attempt_id) "
            "FROM memecoin_entry_attempts a WHERE a.decision_started_at >= ? AND a.network_id='solana'",
            (since,)).fetchall()
        lane_out = collections.Counter()
        for sig in lane:
            r = c.execute("SELECT terminal_status FROM memecoin_entry_attempts WHERE origin_event_id=?",
                          ("slotlane:" + sig,)).fetchone()
            lane_out[(r[0] if r else "no_attempt_row")] += 1
    handed = {s: o for s, o in live.items() if s not in lane}
    print("since %s: routed buys converted to the slot lane %d, handed off %d" % (
        a.since, len(lane), len(handed)))
    print("  converted -> lane outcome: %s" % dict(lane_out))
    print("  handed off, by pump reason: %s" % dict(collections.Counter(
        pump.get(s, "no_pump_event") for s in handed)))
    print("  handed off, hand-off write: %s" % dict(collections.Counter(handed.values())))
    both = sorted(set(lane) & set(live))
    print("  signatures both converted AND written to the hand-off (should be 0): %d %s" % (
        len(both), [s[:12] for s in both][:5]))
    dbl, reserved, multi = doubles(rows)
    print("ONE SOURCE BUY, ONE FUNDED PATH: signatures with >1 CONFIRMED reservation (double fill): %d"
          % len(dbl))
    print("  context: >1 reservation of any status (retry / slot fallback, normally benign): %d; "
          "attempted on >1 origin kind: %d" % (len(reserved), multi))
    for sig, origins in list(dbl.items())[:10]:
        print("  DOUBLE FILL %s: %s" % (sig[:12], [o.split(":")[0] for o in origins]))


def selftest():
    s1 = "3ydSSHuKSo5hUvfBX3ceyBDzrpF7TC9AkS9Cg5TXNDm96q6DjdfnhzXt1WPCL6ShEgFCYKxzLkyVfrK4Baz4pWm"
    s2 = "5c9bQPQBq32RwhBA4d7xDN9ZpqHZEsLL9DFdk4LSNhEZUxz1aRJvaC1oTyECtC8nmfAQogDbXRGLDjA7e2NzwmJa"
    lines = [
        "2026-09-27T22:40:00.000000+00:00 [memecoin] slot lane routed lane bob Cgi54CNp S=5 sig=%s usdc=1.00 tokens=1 recv=x pair_ms=0.5\n" % s2,
        "2026-09-27T22:50:00.000000+00:00 [memecoin] slot lane routed lane bob Cgi54CNp S=5 sig=%s usdc=1.00 tokens=1 recv=x pair_ms=0.5\n" % s1,
        "2026-09-27T22:51:00.000000+00:00 [memecoin] slot lane routed live al F9SAHZVQ S=6 sig=%s usdc=2.00 tokens=1 recv=x pair_ms=0.4 -> inserted\n" % s2,
        "2026-09-27T22:51:00.000100+00:00 [memecoin] slot lane routed pump pool_unknown al sig=%s\n" % s2,
    ]
    lane, live, pump = parse_log(lines, "2026-09-27T22:48:00+00:00")
    assert lane == {s1} and live == {s2: "inserted"} and pump == {s2: "pool_unknown"}, (lane, live, pump)
    assert origin_signature("swap:raybot:%s:owner:mint" % s1) == s1
    assert origin_signature("pump:%s" % s1) == s1 and origin_signature("slotlane:%s" % s1) == s1
    assert origin_signature("swap:7c2f0000-uuid") is None
    s3 = "4aokkWKcNjiAcVi7gnycEMaBcnSWp1EN9DALrCwpMPRQXqUyq4u3d2ojrRu5RkWsnqPRHRhWkrm39jy4mzC9KuFU"
    rows = [("slotlane:" + s1, "a1", "confirmed"), ("onchain:" + s1, "a2", "confirmed"),  # double fill
            ("slotlane:" + s2, "a3", "confirmed"), ("pump:" + s2, "a4", None),          # one reservation
            ("slotlane:" + s3, "a5", "failed"), ("onchain:" + s3, "a6", "confirmed")]   # fallback: benign
    dbl, reserved, multi = doubles(rows)
    assert list(dbl) == [s1] and sorted(reserved) == sorted([s1, s3]) and multi == 3, (dbl, reserved, multi)
    print("selftest ok")


if __name__ == "__main__":
    main()
