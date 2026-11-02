#!/usr/bin/env python3
"""Cohort D Amendment 22 lag report (read-only): decision_as_of - cut_as_of per UTC day, p50/p90/max, split by
age_clock stratum. decision_as_of = the hard_filter row time, which the assembler persists as the one decision sample taken after all
collection (Am25); cut_as_of = the emission snapshot time (the market cut).
usage: nice -n 19 python3 scripts/desk_decision_lag_report.py [--db data/memecoin_desk.db] [--cohort ID] [--days 7]"""
import argparse
import json
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone


def stratum(inputs_json):
    """age_clock from the PERSISTED provenance: the chart check reason inside the stage's evidence (a real PASS row's
    own reason is just 'pass'). Unreadable provenance is its own stratum, never 'cut'."""
    try:
        reason = json.loads(inputs_json)["evidence"]["checks"]["chart"]["reason"]
        if not isinstance(reason, str):
            raise TypeError
    except (ValueError, TypeError, KeyError):
        return "provenance_unreadable"
    return "decision" if "_ageclock_decision" in reason else "cut"


def report(conn, *, cohort=None, days=7, now=None):
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).isoformat()
    sql = ("SELECT t.ts, t.inputs_json, e.candidate_json FROM desk_tickets t JOIN desk_candidate_emissions e "
           "ON e.opportunity_id=t.opportunity_id WHERE t.stage='hard_filter' AND t.ts>=?" + (" AND t.cohort_id=?" if cohort else ""))
    lags = {}
    for ts, inputs_json, cand in conn.execute(sql, (since, cohort) if cohort else (since,)):
        cut = json.loads(cand)["snapshot"]["emission_at"]
        lag = datetime.fromisoformat(ts).timestamp() - cut
        lags.setdefault((ts[:10], stratum(inputs_json)), []).append(lag)
    out = []
    for (day, s), xs in sorted(lags.items()):
        xs.sort()
        out.append({"day": day, "age_clock": s, "n": len(xs), "p50_s": round(statistics.median(xs), 3),
                    "p90_s": round(xs[min(len(xs) - 1, int(0.9 * len(xs)))], 3), "max_s": round(xs[-1], 3)})
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default="data/memecoin_desk.db")
    p.add_argument("--cohort")
    p.add_argument("--days", type=int, default=7)
    a = p.parse_args(argv)
    conn = sqlite3.connect("file:%s?mode=ro" % a.db, uri=True, timeout=1.)
    try:
        for row in report(conn, cohort=a.cohort, days=a.days):
            print(json.dumps(row))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
