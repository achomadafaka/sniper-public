"""Office desk funnel (Epsilon's contract): bounded tail, producer reason as the first failing check, absent checks are
not observed, unknown never empty."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime,timezone

from sniper.office import desk_funnel as F


@contextmanager
def ro(path):
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def desk(tmp_path, rows):
    (tmp_path / "office").mkdir()
    db = sqlite3.connect(tmp_path / "memecoin_desk.db")
    db.execute("CREATE TABLE desk_tickets(id INTEGER PRIMARY KEY, ticket_id TEXT, stage TEXT, verdict TEXT, reason TEXT, "
               "inputs_json TEXT, ts TEXT)")
    for i, (verdict, reason, checks) in enumerate(rows):
        db.execute("INSERT INTO desk_tickets(ticket_id,stage,verdict,reason,inputs_json,ts) VALUES(?,?,?,?,?,?)",
                   ("t%d" % i, "hard_filter", verdict, reason, json.dumps({"evidence": {"checks": checks}}),
                    "2026-10-29T10:%02d:00+00:00" % (i % 60)))
    db.execute("INSERT INTO desk_tickets(ticket_id,stage,verdict,reason,inputs_json,ts) VALUES('v','vet','SKIP','x','{}','2026-10-29T10:00:00+00:00')")
    db.commit(); db.close()
    return tmp_path / "office"


def read(root):
    return F.read_funnel(root, now=datetime(2026,10,29,12,tzinfo=timezone.utc).timestamp(), host_now=0, open_ro=ro, charge=lambda n=0: None)


def test_first_failing_check_is_the_producer_reason_and_absent_checks_are_not_observed(tmp_path):
    root = desk(tmp_path, [("SKIP", "unknown:dump_group:dump_group_unknown",
                            {"chart": {"status": "pass", "reason": "chart_complete"}}),
                           ("KILL", "kill:mcap_safety", {}), ("PASS", "pass", {})])
    f = read(root)
    assert f["state"] == "known" and f["limited"] is False
    by = {t["ticket_id"]: t for t in f["tickets"]}
    assert by["t0"]["first_failing_check"] == "dump_group" and by["t1"]["first_failing_check"] == "hard_kill"
    assert by["t2"]["first_failing_check"] is None
    assert by["t0"]["checks"]["chart"] == {"status": "pass", "reason": "chart_complete"}
    assert by["t0"]["checks"]["cluster"] == {"status": "not_observed", "reason": None}
    assert {b["check"]: b["count"] for b in f["blockers"]} == {"dump_group": 1, "hard_kill": 1}
    assert all("inputs_json" not in t for t in f["tickets"]) and len(f["tickets"]) == 3   # vet rows never counted


def test_the_tail_is_capped_and_says_so(tmp_path):
    root = desk(tmp_path, [("SKIP", "unknown:chart:x", {})] * (F.LIMIT + 5))
    f = read(root)
    assert f["limited"] is True and sum(i["count"] for i in f["items"]) == F.LIMIT - 1 and len(f["tickets"]) == F.TICKETS
    assert f["sources"][0]["limited"] is True and f["sources"][0]["covered_through"] is not None


def test_a_missing_store_is_unknown_never_an_empty_funnel(tmp_path):
    (tmp_path / "office").mkdir()
    f = F.read_funnel(tmp_path / "office", now=0, host_now=0, open_ro=lambda p: (_ for _ in ()).throw(FileNotFoundError()),
                      charge=lambda n=0: None)
    assert f["state"] == "unknown" and f["items"][0]["count"] is None and f["sources"][0]["state"] == "unknown"
