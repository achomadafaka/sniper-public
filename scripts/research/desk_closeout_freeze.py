#!/usr/bin/env python3
"""Desk close-out freeze (Gamma; docs/audits/2026-10-29-desk-closeout-and-office-carryover-note.md). READ-ONLY.
Computes, for the stop second, one JSON record: per cohort the ticket count/time span and a sha256 over the ordered ticket rows; sha256 of every
desk-related preregistration file; git HEAD; the release directories the desk processes run from (ps); the d-strata boundary table. Writes the JSON to --out
(default data/research/desk_closeout/freeze-<UTC>.json) and prints the sha256 of that file plus a one-line text for the brain episode. Deletes nothing.

  nice -n 19 python3 scripts/research/desk_closeout_freeze.py [--out PATH] [--db data/memecoin_desk.db]
  python3 scripts/research/desk_closeout_freeze.py --self-check
"""
import argparse
import hashlib
import json
import re
import sqlite3
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PREREG = re.compile(r"desk|cohort|intake|dump-group|dump_group|tape-reader|ps-decoder|v28|three-feed|exit-first|exit-observer|forecast|band-drift|sympathy|dex", re.I)
BOUNDARIES = [("d0_broken_cluster", None, "2026-10-29T09:30:54Z"), ("d1_cut", "2026-10-29T09:30:54Z", "2026-10-29T09:53:02Z"), ("d2_decision", "2026-10-29T09:53:02Z", "2026-10-29T10:00:02Z"),
              ("d3_curve_tips", "2026-10-29T10:00:02Z", "2026-10-29T10:02:01Z"), ("d4_chart_fixed", "2026-10-29T10:02:01Z", "2026-10-29T10:05:02Z"),
              ("d5_holders", "2026-10-29T10:05:02Z", "2026-10-29T10:24:02Z"), ("d6_dump_group_ps", "2026-10-29T10:24:02Z", None)]


def sha(b):
    return hashlib.sha256(b).hexdigest()


def cohort_rows(conn):
    out = {}
    for cid, in conn.execute("SELECT DISTINCT cohort_id FROM desk_tickets ORDER BY cohort_id"):
        h, n, lo, hi, stages = hashlib.sha256(), 0, None, None, {}
        for r in conn.execute("SELECT ticket_id,stage,verdict,reason,inputs_hash,ts FROM desk_tickets WHERE cohort_id=? ORDER BY ts,ticket_id", (cid,)):
            h.update(json.dumps(r, sort_keys=True, default=str).encode())
            n += 1
            lo, hi = lo or r[5], r[5]
            k = "%s:%s" % (r[1], r[2])
            stages[k] = stages.get(k, 0) + 1
        out[cid] = {"rows": n, "first_ts": lo, "last_ts": hi, "sha256_rows": h.hexdigest(), "by_stage_verdict": stages}
    return out


def prereg_hashes():
    res = {}
    for p in sorted((ROOT / "docs/audits").glob("*.md")):
        if PREREG.search(p.name) and "office" not in p.name.lower():
            res[str(p.relative_to(ROOT))] = sha(p.read_bytes())
    return res


def running_releases():
    try:
        ps = subprocess.run(["ps", "-eo", "pid,lstart,args"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return {"error": "ps_unreadable"}
    out = []
    for line in ps.splitlines():
        m = re.search(r"(memecoin_desk_\w+\.py|desk_paper\w*\.py)", line)
        r = re.search(r"(\.runtime/[\w./-]+?/(?:releases/)?[0-9a-f]{9,40})", line)
        if m and r:
            out.append({"process": m.group(1), "release_dir": r.group(1), "ps_line": " ".join(line.split()[:6])})
    return out


def build(db):
    conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=5)
    try:
        cohorts = cohort_rows(conn)
    finally:
        conn.close()
    head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    return {"schema": "desk-closeout-freeze-v1", "frozen_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "git_head": head,
            "reason": "stopped-by-operator (operator reprioritisation to the Office, 2026-10-29); UNTESTED, not failed; no look, no verdict",
            "cohorts": cohorts, "strata_boundaries": BOUNDARIES, "running_desk_processes": running_releases(), "prereg_sha256": prereg_hashes()}


def self_check():
    c = sqlite3.connect(":memory:")
    c.execute("create table desk_tickets(ticket_id,stage,verdict,reason,inputs_hash,ts,cohort_id)")
    c.executemany("insert into desk_tickets values(?,?,?,?,?,?,?)", [("t1", "hard_filter", "SKIP", "r", "h", "2026-10-29T10:00:00", "c1"), ("t2", "hard_filter", "PASS", "pass", "h", "2026-10-29T10:01:00", "c1")])
    a = cohort_rows(c)
    c.execute("update desk_tickets set verdict='PASS' where ticket_id='t1'")
    b = cohort_rows(c)
    assert a["c1"]["rows"] == 2 and a["c1"]["sha256_rows"] != b["c1"]["sha256_rows"], "row hash must change when a row changes"
    assert PREREG.search("2026-10-28-desk-cohort-d-all-sheet-gaps-preregistration.md") and not PREREG.search("2026-10-28-memecoin-trading-readiness.md")
    print("self-check ok")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=str(ROOT / "data/memecoin_desk.db"))
    p.add_argument("--out")
    p.add_argument("--self-check", action="store_true")
    a = p.parse_args()
    if a.self_check:
        return self_check()
    rec = build(a.db)
    out = Path(a.out) if a.out else ROOT / "data/research/desk_closeout" / ("freeze-%s.json" % rec["frozen_at_utc"].replace(":", ""))
    out.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(rec, indent=1, sort_keys=True).encode()
    out.write_bytes(raw)
    print("wrote", out, "sha256", sha(raw))
    print("episode: desk close-out freeze %s file_sha256=%s cohorts=%s head=%s" % (rec["frozen_at_utc"], sha(raw), {k[-8:]: v["rows"] for k, v in rec["cohorts"].items()}, rec["git_head"][:9]))


if __name__ == "__main__":
    main()
