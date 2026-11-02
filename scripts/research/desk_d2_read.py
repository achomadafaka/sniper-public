#!/usr/bin/env python3
"""Cohort D age-clock d2 read (Gamma; cohort D prereg Amendments 24-26). DESCRIPTIVE ONLY: no claim, no outcome, no PnL.
Per stratum (d0 broken cluster < 09:30:54Z, d1_cut, d2_decision >= 2026-10-29T09:52:45Z): hard_filter tickets, PASS share, reason mix,
decision_as_of - cut_as_of lag p50/p90/max, and the metadata_future count. Read-only, nice 19.

  nice -n 19 python3 scripts/research/desk_d2_read.py [--db data/memecoin_desk.db] [--since ISO] [--cohort ID]
  python3 scripts/research/desk_d2_read.py --self-check
"""
import argparse
import json
import sqlite3
import statistics
from collections import Counter, defaultdict

D1_FROM, D2_FROM, D3_FROM = "2026-10-29T09:30:54", "2026-10-29T09:53:02", "2026-10-29T10:00:02"   # d2 = verified process start (ps lstart); Alpha reported 09:52:45Z
D7_FROM = "2026-10-29T11:31:01"   # emitter chain Am36-39, release a8faa5ab4: ps-verified start of universe/cluster/assembler
D6_FROM = "2026-10-29T10:24:02"   # dump_group Am31-32 batch, release 53527be81: ps-verified assembler start (dump_group + cluster 10:24:01Z)
D5_FROM = "2026-10-29T10:05:02"   # holders/creator read 1f6ca3172, release 449809a5e: ps-verified assembler start
D4_FROM = "2026-10-29T10:02:01"   # chart key_error fix 4d20409cc, release f23f6da9e; ps-verified assembler start


def stratum(ts):
    t = ts[:19]
    return "d0_broken_cluster" if t < D1_FROM else ("d1_cut" if t < D2_FROM else ("d2_decision" if t < D3_FROM else ("d3_curve_tips" if t < D4_FROM else ("d4_chart_fixed" if t < D5_FROM else ("d5_holders" if t < D6_FROM else ("d6_dump_group_ps" if t < D7_FROM else "d7_emitter_cut"))))))


def clock(inputs_json):
    """age_clock from persisted provenance (chart reason suffix); unreadable is its own bucket, never 'cut'."""
    try:
        checks = json.loads(inputs_json)["evidence"]["checks"]
    except (ValueError, TypeError, KeyError):
        return "provenance_unreadable"
    chart = checks.get("chart") if isinstance(checks, dict) else None
    if chart is None or (isinstance(chart, dict) and chart.get("reason") == "not_collected"):
        return "chart_not_reached"            # refused before the chart check ran: nothing to read, not a defect
    try:
        r = chart["reason"]
        if not isinstance(r, str):
            raise TypeError
    except (TypeError, KeyError):
        return "provenance_unreadable"
    return "decision" if "_ageclock_decision" in r else "cut"


def walk(o, hits):
    """count every string value containing 'metadata_future' in the persisted evidence."""
    if isinstance(o, dict):
        for v in o.values():
            walk(v, hits)
    elif isinstance(o, list):
        for v in o:
            walk(v, hits)
    elif isinstance(o, str) and "metadata_future" in o:
        hits.append(o)


def read(conn, since, cohort=None):
    from datetime import datetime
    sql = ("SELECT t.ts, t.verdict, t.reason, t.inputs_json, e.candidate_json FROM desk_tickets t LEFT JOIN desk_candidate_emissions e "
           "ON e.opportunity_id=t.opportunity_id WHERE t.stage='hard_filter' AND t.ts>=?" + (" AND t.cohort_id=?" if cohort else "") + " ORDER BY t.ts")
    n, passes, reasons, lags, mf, clk = Counter(), Counter(), defaultdict(Counter), defaultdict(list), Counter(), defaultdict(Counter)
    lag_unreadable = Counter()
    for ts, verdict, reason, inputs, cand in conn.execute(sql, (since, cohort) if cohort else (since,)):
        s = stratum(ts)
        n[s] += 1
        passes[s] += verdict == "PASS"
        reasons[s][reason or "none"] += 1
        clk[s][clock(inputs)] += 1
        try:
            hits = []
            walk(json.loads(inputs), hits)
            mf[s] += len(hits)
        except ValueError:
            mf[s] += 0
        try:
            cut = json.loads(cand)["snapshot"]["emission_at"]
            lags[s].append(datetime.fromisoformat(ts).timestamp() - cut)
        except (TypeError, ValueError, KeyError):
            lag_unreadable[s] += 1
    out = {}
    for s in sorted(n):
        xs = sorted(lags[s])
        out[s] = {"tickets": n[s], "pass": passes[s], "pass_share": round(passes[s] / n[s], 4),
                  "reasons": dict(reasons[s].most_common(12)), "age_clock": dict(clk[s]), "metadata_future_mentions": mf[s],
                  "lag_n": len(xs), "lag_unreadable": lag_unreadable[s],
                  "lag_p50_s": round(statistics.median(xs), 3) if xs else None,
                  "lag_p90_s": round(xs[min(len(xs) - 1, int(0.9 * len(xs)))], 3) if xs else None,
                  "lag_max_s": round(xs[-1], 3) if xs else None}
    return out


def fx_report(path, since_epoch):
    """Cohort D Amendment 39: FX deferrals per COIN sighting (insert-only fx_deferrals rows), not per print and not per ticket."""
    import os
    if not os.path.exists(path):
        return {"status": "not_computable", "why": "intake_emitter.db missing"}
    c = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=2.0)
    try:
        d = c.execute("SELECT first_fx_deferred_at, deferral_prints, first_ticket_at FROM fx_deferrals WHERE first_fx_deferred_at>=?", (since_epoch,)).fetchall()
        sighted = c.execute("SELECT count(DISTINCT mint) FROM claims WHERE claimed_at_host>=?", (since_epoch,)).fetchone()[0]
    except sqlite3.Error as e:
        return {"status": "not_computable", "why": type(e).__name__}
    finally:
        c.close()
    delays = sorted(t - a for a, _p, t in d if t is not None)
    pct = lambda q: round(delays[min(len(delays) - 1, int(q * len(delays)))], 3) if delays else None
    return {"status": "computable", "coins_sighted_with_claim": sighted, "coins_fx_deferred": len(d), "deferral_prints_diagnostic": sum(p for _a, p, _t in d),
            "deferred_never_ticketed": sum(1 for _a, _p, t in d if t is None), "delay_p50_s": pct(0.5), "delay_p90_s": pct(0.9), "delay_max_s": round(delays[-1], 3) if delays else None,
            "defect": bool(sighted and len(d) / sighted > 0.05) or bool(delays and pct(0.9) > 10.0)}


def self_check():
    c0 = sqlite3.connect(":memory:")
    assert fx_report("/nonexistent/x.db", 0)["status"] == "not_computable"
    c0.close()
    assert stratum("2026-10-29T09:30:53+00:00") == "d0_broken_cluster"
    assert stratum("2026-10-29T09:30:54+00:00") == "d1_cut" and stratum("2026-10-29T09:53:01.9") == "d1_cut"
    assert stratum("2026-10-29T09:53:02.0+00:00") == "d2_decision" and stratum("2026-10-29T10:00:02.0+00:00") == "d3_curve_tips" and stratum("2026-10-29T10:02:01.0+00:00") == "d4_chart_fixed" and stratum("2026-10-29T10:05:02.0+00:00") == "d5_holders" and stratum("2026-10-29T10:24:02.0+00:00") == "d6_dump_group_ps" and stratum("2026-10-29T11:31:01.0+00:00") == "d7_emitter_cut"
    assert clock(json.dumps({"evidence": {"checks": {"chart": {"reason": "x_ageclock_decision"}}}})) == "decision"
    assert clock("{}") == "provenance_unreadable" and clock(json.dumps({"evidence": {"checks": {"chart": {"reason": "not_collected"}}}})) == "chart_not_reached"
    h = []
    walk({"a": [{"b": "unknown:metadata_future:age"}]}, h)
    assert len(h) == 1
    c = sqlite3.connect(":memory:")
    c.executescript("create table desk_tickets(ts,verdict,reason,inputs_json,stage,opportunity_id,cohort_id);create table desk_candidate_emissions(opportunity_id,candidate_json);")
    c.execute("insert into desk_tickets values('2026-10-29T09:54:00+00:00','PASS','pass',?, 'hard_filter','o1','c')",
              (json.dumps({"evidence": {"checks": {"chart": {"reason": "ok_ageclock_decision"}}}}),))
    c.execute("insert into desk_candidate_emissions values('o1',?)", (json.dumps({"snapshot": {"emission_at": 1793267570.0}}),))
    r = read(c, "2026-10-29")
    assert r["d2_decision"]["pass"] == 1 and r["d2_decision"]["age_clock"] == {"decision": 1}, r
    print("self-check ok")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="data/memecoin_desk.db")
    p.add_argument("--since", default=D1_FROM)
    p.add_argument("--cohort")
    p.add_argument("--fx-db", default="data/desk/intake_emitter.db")
    p.add_argument("--self-check", action="store_true")
    a = p.parse_args()
    if a.self_check:
        return self_check()
    conn = sqlite3.connect("file:%s?mode=ro" % a.db, uri=True, timeout=2.0)
    try:
        out = read(conn, a.since, a.cohort)
        from datetime import datetime, timezone
        out["_fx_deferrals_Am39"] = fx_report(a.fx_db, datetime.fromisoformat(D7_FROM).replace(tzinfo=timezone.utc).timestamp())
        print(json.dumps(out, indent=1))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
