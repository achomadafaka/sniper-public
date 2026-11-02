import importlib.util
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

spec = importlib.util.spec_from_file_location("lag", Path(__file__).resolve().parents[1] / "scripts/desk_decision_lag_report.py")
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)


def test_lag_per_day_and_stratum():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE desk_tickets(ts TEXT, inputs_json TEXT, stage TEXT, opportunity_id TEXT, cohort_id TEXT)")
    c.execute("CREATE TABLE desk_candidate_emissions(opportunity_id TEXT, candidate_json TEXT)")
    base = datetime(2026, 10, 29, 12, tzinfo=timezone.utc).timestamp()
    rows_in = [(1.0, "chart_complete"), (3.0, "chart_complete"), (5.0, "chart_complete_ageclock_decision"), (7.0, None)]
    for i, (lag, chart) in enumerate(rows_in):
        reason = "{}" if chart is None else json.dumps({"evidence": {"checks": {"chart": {"status": "pass", "reason": chart}}}})
        c.execute("INSERT INTO desk_candidate_emissions VALUES(?,?)", ("o%d" % i, json.dumps({"snapshot": {"emission_at": base}})))
        c.execute("INSERT INTO desk_tickets VALUES(?,?,?,?,?)", (datetime.fromtimestamp(base + lag, timezone.utc).isoformat(),
                                                             reason, "hard_filter", "o%d" % i, "D"))
    rows = M.report(c, days=7, now=datetime(2026, 10, 30, tzinfo=timezone.utc))
    assert rows == [{"day": "2026-10-29", "age_clock": "cut", "n": 2, "p50_s": 2.0, "p90_s": 3.0, "max_s": 3.0},
                    {"day": "2026-10-29", "age_clock": "decision", "n": 1, "p50_s": 5.0, "p90_s": 5.0, "max_s": 5.0},
                    {"day": "2026-10-29", "age_clock": "provenance_unreadable", "n": 1, "p50_s": 7.0, "p90_s": 7.0,
                     "max_s": 7.0}]
