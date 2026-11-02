"""Near-pass Scout delivery: bounded listing with the frozen eligibility, the sheet for one ticket from its own
persisted hard_filter row and market cut, and the scout-only broker tools."""
import json
import sqlite3
from types import SimpleNamespace

from sniper.office import capabilities as C
from sniper.office import desk_near_pass as N
from sniper.office.tools import sheet as SH

PASS = {"status": "pass", "reason": "ok"}


def checks(**extra):
    out = {k: PASS for k in set(SH.NEAR_PASS_REQUIRED) | set(SH.NEAR_PASS_ALLOWED)}
    out.update(extra)
    return out


def desk(tmp_path, rows):
    db = sqlite3.connect(tmp_path / "desk.db")
    db.execute("CREATE TABLE desk_tickets(id INTEGER PRIMARY KEY, ticket_id TEXT, cohort_id TEXT, stage TEXT, verdict TEXT, reason TEXT,"
               " inputs_json TEXT, ts TEXT)")
    for row in rows:
        tid, stage, verdict, reason, ch, ts = row[:6]
        cohort = row[6] if len(row) > 6 else "D"
        db.execute("INSERT INTO desk_tickets(ticket_id,cohort_id,stage,verdict,reason,inputs_json,ts) VALUES(?,?,?,?,?,?,?)",
                   (tid, cohort, stage, verdict, reason, json.dumps({"evidence": {"checks": ch}}), ts))
    db.commit()
    db.row_factory = sqlite3.Row
    return db


NOW = 1_800_000_000.0
AT = "2027-01-15T08:00:00+00:00"          # before NOW
LATER = "2027-02-01T08:00:00+00:00"       # after NOW


def test_listing_returns_only_eligible_causal_hard_filter_tickets_with_their_blocker_set(tmp_path):
    dg = {"status": "unknown", "reason": "dump_group_unknown"}
    conn = desk(tmp_path, [
        ("a", "hard_filter", "SKIP", "unknown:dump_group:dump_group_unknown", checks(dump_group=dg), AT),
        ("b", "hard_filter", "SKIP", "unknown:chart:facts_unavailable", checks(chart={"status": "unknown", "reason": "facts_unavailable"}), AT),
        ("c", "hard_filter", "KILL", "kill:mcap_safety", checks(), AT),
        ("d", "hard_filter", "SKIP", "unknown:dump_group:dump_group_unknown", checks(dump_group=dg), LATER),
        ("e", "vet", "SKIP", "x", checks(), AT)])
    out = N.list_near_pass(conn, now=NOW)
    assert out["state"] == "known" and out["limited"] is False
    assert [i["ticket_id"] for i in out["items"]] == ["a"]                    # b refused, c killed, d future, e not hf
    blockers = [{"check": "dump_group", "status": "unknown", "reason": "dump_group_unknown"}]
    assert out["items"][0]["blockers"] == blockers and out["items"][0]["blocker_set_id"] == SH.blocker_set_id(blockers)
    assert N.list_near_pass(conn, now=float("nan"))["state"] == "unknown"


def test_the_listing_tail_is_bounded(tmp_path):
    dg = {"status": "unknown", "reason": "dump_group_unknown"}
    conn = desk(tmp_path, [("t%d" % i, "hard_filter", "SKIP", "unknown:dump_group:x", checks(dump_group=dg), AT)
                           for i in range(N.LIMIT + 10)])
    out = N.list_near_pass(conn, now=NOW)
    assert out["limited"] is True and len(out["items"]) == N.MAX_ITEMS


def test_the_sheet_uses_the_ticket_own_row_and_market_cut_in_near_pass_mode(monkeypatch):
    seen = {}
    monkeypatch.setattr(SH, "sheet", lambda cand, **kw: seen.update(kw, cand=cand) or {"state": "known"})
    ev = {"checks": checks(dump_group={"status": "unknown", "reason": "dump_group_unknown"}),
          "market_cut": {"receipt_cut": 5.0, "cut_slot": 9, "block_known_at": 4.0}, "known_at": AT}
    ticket = {"candidate": {"mint": "m"}, "cohort_id": "D", "opportunity_id": "o",
              "stages": [{"stage": "hard_filter", "verdict": "SKIP", "reason": "unknown:dump_group:dump_group_unknown",
                          "ts": AT, "inputs": {"evidence": ev}}]}
    store = SimpleNamespace(get_ticket=lambda c, t: ticket if t == "T" else None,
                            get_cohort=lambda c, cid: {"policy": {"hard_filter": {}}},
                            get_intake_source_receipt=lambda *a, **k: {"source_metadata": {"s": 1}})
    r = N.near_pass_sheet(None, "T", store_conn="S", desk_dir="/d", now=NOW, clock_sample={"offset_s": 0, "sampled_at": NOW},
                          buyer_ledger=[], refresh=lambda c, **k: (dict(c, refreshed=True), {}), store=store)
    assert r["state"] == "known" and seen["feed"] == "near_pass" and seen["cand"]["refreshed"]
    row = seen["hard_filter_row"]
    assert row["market_cut"] == {"receipt_cut": 5.0, "cut_slot": 9} and row["verdict"] == "SKIP" and row["evidence"] is ev
    assert N.near_pass_sheet(None, "X", store_conn="S", desk_dir="/d", now=NOW, clock_sample={}, buyer_ledger=[],
                             refresh=None, store=store)["reason"] == "ticket_unknown"


def test_only_scouts_hold_the_near_pass_tools_and_they_need_no_context_slot():
    for tool in ("desk.near_pass_list", "desk.near_pass_sheet"):
        assert tool in C.CAPS["scout"] and tool in C.OFFCHAIN_TOOLS
        assert all(tool not in tools for role, tools in C.CAPS.items() if role != "scout")


def test_runtime_registers_both_tools_with_exact_arguments(tmp_path):
    from sniper.office import runtime as RT
    tools = RT.near_pass_tools(None, tmp_path / "desk.db", tmp_path, tmp_path / "clock.json")
    assert tools["desk.near_pass_list"].argument_types == {}
    assert tools["desk.near_pass_sheet"].argument_types == {"ticket_id": str}
    assert tools["desk.near_pass_sheet"].definition_version == N.SHEET_CODE_VERSION


def test_every_funnel_step_has_an_index(tmp_path):
    from sniper.office import store as S, ledger as L
    office = S.open_store(tmp_path / "office.db")
    plan = lambda c, q: " ".join(str(r[3]) for r in c.execute("EXPLAIN QUERY PLAN " + q))
    assert "receipts_near_pass_tool" in plan(office, "SELECT seq FROM receipts WHERE json_extract(payload_json,'$.tool')="
                                             "'desk.near_pass_list' AND json_extract(payload_json,'$.tool') IN "
                                             "('desk.near_pass_list','desk.near_pass_sheet')")
    assert "receipts_sheet_feed" in plan(office, "SELECT seq FROM receipts WHERE json_extract(payload_json,'$.kind')='sheet' "
                                         "AND json_extract(payload_json,'$.request.feed')='near_pass'")
    ledger = L.open_ledger(tmp_path / "ledger.db")
    assert "orders_bundle" in plan(ledger, "SELECT order_id FROM orders WHERE decision_bundle_hash='x'")


def _iso(s):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(s, timezone.utc).isoformat()


def test_am10_pass_feed_lists_core_cohort_pass_tickets_newest_first_with_counted_remainder(tmp_path, monkeypatch):
    monkeypatch.setattr(N, "MAX_ITEMS", 2)
    dg = {"status": "unknown", "reason": "dump_group_unknown"}
    conn = desk(tmp_path, [
        ("old", "hard_filter", "PASS", "ok", checks(), _iso(NOW - N.PASS_MAX_AGE_S - 1), "C"),
        ("p1", "hard_filter", "PASS", "ok", checks(), _iso(NOW - 30), "C"),
        ("p2", "hard_filter", "PASS", "ok", checks(), _iso(NOW - 20), "C"),
        ("p3", "hard_filter", "PASS", "ok", checks(), _iso(NOW - 10), "C"),
        ("other", "hard_filter", "PASS", "ok", checks(), _iso(NOW - 5), "D"),
        ("np", "hard_filter", "SKIP", "unknown:dump_group:dump_group_unknown", checks(dump_group=dg), _iso(NOW - 5), "C"),
        ("fut", "hard_filter", "PASS", "ok", checks(), _iso(NOW + 5), "C")])
    out = N.list_near_pass(conn, now=NOW, cohort_id="C")
    assert [(i["ticket_id"], i["feed"]) for i in out["items"]] == [("p3", "pass"), ("p2", "pass"), ("np", "near_pass")]
    assert out["pass_unlisted"] == {"item_limit": 1, "age_bound": 1}            # p1 over the limit, old past the age bound
    assert "other" not in {i["ticket_id"] for i in N.list_near_pass(conn, now=NOW, cohort_id="C")["items"]}


def test_a_pass_ticket_gets_the_pass_feed_sheet(monkeypatch):
    seen = {}
    monkeypatch.setattr(SH, "sheet", lambda cand, **kw: seen.update(kw) or {"state": "known"})
    ev = {"checks": checks(), "market_cut": {"receipt_cut": 5.0, "cut_slot": 9}, "known_at": AT}
    ticket = {"candidate": {"mint": "m"}, "cohort_id": "C", "opportunity_id": "o",
              "stages": [{"stage": "hard_filter", "verdict": "PASS", "reason": "ok", "ts": AT, "inputs": {"evidence": ev}}]}
    store = SimpleNamespace(get_ticket=lambda c, t: ticket, get_cohort=lambda c, cid: {"policy": {"hard_filter": {}}},
                            get_intake_source_receipt=lambda *a, **k: {})
    N.near_pass_sheet(None, "T", store_conn="S", desk_dir="/d", now=NOW, clock_sample={"offset_s": 0, "sampled_at": NOW},
                      buyer_ledger=[], refresh=lambda c, **k: (c, {}), store=store)
    assert seen["feed"] == "pass" and seen["hard_filter_row"]["verdict"] == "PASS"


def test_an_unread_context_block_is_typed_and_reads_nothing(monkeypatch):
    from sniper.services import memecoin_desk_d_blocks as DB, memecoin_desk_heat_v2 as H, memecoin_desk_contract as K
    monkeypatch.setattr(H,'heat_for',lambda *a,**k:1/0)
    monkeypatch.setattr(K,'D_CONTEXT_MODULES',{'heat':'sniper.services.memecoin_desk_heat_v2'})
    out=DB.context_blocks(dict(mint='m',pool='p'),desk_dir='x',sheet_as_of=100.,offset=1.,unread=frozenset({'heat'}))
    assert out['heat']['reason']=='read_budget' and out['heat']['state']=='unknown' and out['heat']['as_of']==99.
