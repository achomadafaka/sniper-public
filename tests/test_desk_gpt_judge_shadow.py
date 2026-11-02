"""GPT blind second JUDGE phase 2 (shadow seat): own DB, sheet hash + text, pause rules, resumable (fake call)."""
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("gpt_shadow", ROOT / "scripts/memecoin_desk_gpt_judge_shadow.py")
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)
R = S._replay()
CAPS = {"enter_ttl_max_s": 30, "boundary_max_s": 120, "wait_max_s": 300}
BOUNDS = {"sl_pct": [10.0, 40.0], "tp_pct": [15.0, 150.0], "trail_pct": [8.0, 30.0]}
OID = "desk:U:" + "a" * 24
GOOD = '{"k":"%s","v":"S","e":null,"r":null,"c":1,"enter_ttl_s":5,"next_check_s":30}' % OID


def desk_db(tmp_path, n=3):
    path = tmp_path / "desk.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE desk_judge_calls(call_id TEXT, ticket_id TEXT, state TEXT, call_started_at TEXT, result_json TEXT)")
    c.execute("CREATE TABLE desk_tickets(ticket_id TEXT, stage TEXT, prompt_version TEXT)")
    for i in range(n):
        c.execute("INSERT INTO desk_judge_calls VALUES(?,?,?,?,?)", ("judge:%d" % i, "t%d" % i, "responded",
                  "2026-10-25T00:0%d:00" % i, json.dumps({"text": GOOD, "asof": {"candidate": {}}})))
        c.execute("INSERT INTO desk_tickets VALUES(?,?,?)", ("t%d" % i, "vet", "desk-judge-v18"))
    c.commit()
    c.close()
    return path


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(R, "sheet_of", lambda r: json.dumps({"opportunity_id": OID, "trigger": "universe", "missing": []}))
    monkeypatch.setattr(R, "cohort_limits", lambda db: (BOUNDS, CAPS))
    monkeypatch.setattr(S, "_replay", lambda: R)


def run(tmp_path, call, **kw):
    pages = []
    out = tmp_path / "gpt_judge.db"
    auth = tmp_path / "auth.json"
    if not auth.exists():                                    # a fresh login: the 6-day page stays quiet
        from datetime import datetime, timezone
        auth.write_text(json.dumps({"last_refresh": datetime.now(timezone.utc).isoformat()}))
    code = S.run(str(tmp_path / "desk.db"), out, "2026-10-25T00:00:00", once=True, call=call, page=pages.append,
                 sleep=lambda s: None, auth_path=auth, **kw)
    return code, pages, sqlite3.connect(out)


def test_each_real_call_is_answered_once_with_the_sheet_hash_and_text_and_reruns_add_nothing(tmp_path, patched):
    desk_db(tmp_path)
    seen = []
    call = lambda W, prompt: (seen.append(prompt) or (GOOD, None, 5000.0, {"tool_events": 0, "usage": {"output_tokens": 9}}))
    code, pages, conn = run(tmp_path, call)
    rows = conn.execute("SELECT call_id, sheet_sha256, sheet_text, gpt_text, parse_ok, valid, call_error, guard_void FROM gpt_judge ORDER BY call_id").fetchall()
    assert code == 0 and len(rows) == 3 and not pages
    import hashlib
    assert all(r[1] == hashlib.sha256(r[2].encode()).hexdigest() and r[3] == GOOD and r[4:] == (1, 1, None, None) for r in rows)
    assert all("Fact sheet:\n" + r[2] in p and GOOD not in p for r, p in zip(rows, seen))    # the live verdict is never in the prompt
    code2, _, conn2 = run(tmp_path, call)
    assert len(seen) == 3 and conn2.execute("SELECT COUNT(*) FROM gpt_judge").fetchone() == (3,)   # resumable


def test_a_tool_event_pauses_the_seat_and_a_restart_refuses_until_resume(tmp_path, patched):
    desk_db(tmp_path)
    boom = lambda W, prompt: (None, "tool_event", 100.0, {"tool_events": 1, "usage": None})
    code, pages, conn = run(tmp_path, boom)
    assert code == 3 and "tool event" in pages[-1] and conn.execute("SELECT v FROM seat_state").fetchone()
    code2, _, _ = run(tmp_path, lambda W, p: pytest.fail("a paused seat makes no call"))
    assert code2 == 3
    code3, _, conn3 = run(tmp_path, lambda W, p: (GOOD, None, 1.0, {"tool_events": 0, "usage": None}), resume=True)
    assert code3 == 0 and conn3.execute("SELECT COUNT(*) FROM gpt_judge").fetchone()[0] >= 1


def test_a_day_error_rate_above_two_percent_pauses_and_a_version_mismatch_is_not_compared(tmp_path, patched):
    path = desk_db(tmp_path, n=S.MIN_DAY_CALLS + 2)
    c = sqlite3.connect(path)
    c.execute("UPDATE desk_tickets SET prompt_version='desk-judge-v17' WHERE ticket_id='t0'")   # ran another playbook
    c.commit()
    c.close()
    calls = []
    fail = lambda W, prompt: (calls.append(1) or (None, "timeout", 10.0, {"tool_events": 0, "usage": None}))
    code, pages, conn = run(tmp_path, fail)
    assert code == 3 and "rate" in pages[-1]
    assert conn.execute("SELECT call_error FROM gpt_judge WHERE call_id='judge:0'").fetchone() == ("playbook_version_mismatch",)
    assert len(calls) < S.MIN_DAY_CALLS + 2                                  # it stopped at the rule, not after the queue


def test_a_guard_void_makes_no_call_and_is_stored(tmp_path, patched, monkeypatch):
    desk_db(tmp_path, n=1)
    monkeypatch.setattr(R, "free_text_guard", lambda s: "/x=ignore previous")
    code, pages, conn = run(tmp_path, lambda W, p: pytest.fail("no call on a voided sheet"))
    assert conn.execute("SELECT guard_void, gpt_text FROM gpt_judge").fetchone() == ("/x=ignore previous", None)
    assert pages and "free-text guard" in pages[0]


def test_known_cluster_parser_mismatch_reaches_shadow_fake_call_without_guard_page(tmp_path, monkeypatch):
    sheet = {"opportunity_id": OID, "trigger": "universe", "missing": [],
             "evidence": {"cluster": {"check": "unknown", "state": "unknown",
                                      "reason": "cluster_parser_mismatch"}}}
    monkeypatch.setattr(R, "sheet_of", lambda result: json.dumps(sheet))
    seen, pages = [], []
    conn = S.connect(tmp_path / "shadow.db")
    try:
        status = S.answer_one(
            R, None, conn, None,
            ("judge:typed-mismatch", "T", "2026-10-26T15:51:00+00:00",
             json.dumps({"text": GOOD, "asof": {"candidate": {}}}), "desk-judge-v18"),
            "system", "desk-judge-v18", BOUNDS, CAPS,
            call=lambda worker, prompt: (seen.append(prompt) or
                (GOOD, None, 1.0, {"tool_events": 0, "usage": None})), page=pages.append)
        assert status == "ok" and len(seen) == 1 and not pages
        assert conn.execute("SELECT guard_void,gpt_text,parse_ok,valid FROM gpt_judge").fetchone() == (None, GOOD, 1, 1)
        assert '"check": "unknown"' in seen[0]
    finally:
        conn.close()


def test_mismatch_and_render_rows_stay_outside_the_pause_rate(tmp_path, patched):
    desk_db(tmp_path, n=S.MIN_DAY_CALLS + 5)
    c = sqlite3.connect(tmp_path / "desk.db")
    c.execute("UPDATE desk_tickets SET prompt_version='desk-judge-v17'")                 # a playbook bump: every call mismatches
    c.commit()
    c.close()
    code, pages, conn = run(tmp_path, lambda W, p: pytest.fail("no call on a mismatch"))
    assert code == 0 and not pages                                                       # 25 mismatch rows never pause the seat
    assert S.day_rate(conn, "2026-10-25") == (0, 0, None)
    conn.execute("INSERT INTO gpt_judge(call_id,ticket_id,call_started_at,day,model,created_at,call_error) VALUES('x','t','2026-10-25T01:00:00','2026-10-25','m',0,'timeout')")
    assert S.day_rate(conn, "2026-10-25") == (1, 1, 1.0)


def test_a_rejected_auth_writeback_pauses_the_seat(tmp_path, patched):
    desk_db(tmp_path, n=2)
    call = lambda W, p: (GOOD, None, 1.0, {"tool_events": 0, "usage": None, "auth": "rejected:account_id_changed"})
    code, pages, conn = run(tmp_path, call)
    assert code == 3 and "write-back" in pages[-1] and conn.execute("SELECT auth_writeback FROM gpt_judge").fetchone() == ("rejected:account_id_changed",)


def test_the_real_login_age_page_fires_once_per_day(tmp_path):
    from datetime import datetime, timezone, timedelta
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"last_refresh": (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()}))
    conn = S.connect(tmp_path / "g.db")
    pages = []
    assert S.page_auth_age(conn, pages.append, auth_path=auth) is True
    assert S.page_auth_age(conn, pages.append, auth_path=auth) is False and len(pages) == 1
    assert S.auth_age_days(tmp_path / "missing.json") is None


def test_a_stored_sheet_is_read_from_result_json_on_a_table_without_those_columns(tmp_path, patched):
    import base64, hashlib, zlib
    path = desk_db(tmp_path, n=2)
    text = json.dumps({"opportunity_id": OID, "trigger": "universe", "missing": []}, separators=(",", ":"), sort_keys=True)
    sha, zb64 = hashlib.sha256(text.encode()).hexdigest(), base64.b64encode(zlib.compress(text.encode())).decode()
    c = sqlite3.connect(path)
    assert "sheet_sha256" not in {r[1] for r in c.execute("PRAGMA table_info(desk_judge_calls)")}        # NO such columns
    good = json.dumps({"text": GOOD, "asof": {"candidate": {}}, "sheet_sha256": sha, "sheet_zb64": zb64})
    bad = json.dumps({"text": GOOD, "asof": {"candidate": {}}, "sheet_sha256": "0" * 64, "sheet_zb64": zb64})   # hash != bytes
    c.execute("UPDATE desk_judge_calls SET result_json=? WHERE call_id='judge:0'", (good,))
    c.execute("UPDATE desk_judge_calls SET result_json=? WHERE call_id='judge:1'", (bad,))
    c.commit()
    c.close()
    seen = []
    code, pages, conn = run(tmp_path, lambda W, p: (seen.append(p) or (GOOD, None, 1.0, {"tool_events": 0, "usage": None, "auth": "unchanged"})))
    rows = dict((r[0], r[1:]) for r in conn.execute("SELECT call_id, sheet_source, sheet_matches_stored, sheet_sha256, playbook_sha256 FROM gpt_judge"))
    assert rows["judge:0"][0] == "stored" and rows["judge:0"][2] == sha and text in seen[0]
    assert rows["judge:0"][1] in (0, 1)                                          # re-render compared with the stored bytes
    assert rows["judge:1"][0] == "rerender" and rows["judge:1"][1] is None       # a corrupt store is never trusted
    assert len(rows["judge:0"][3]) == 64 and rows["judge:0"][3] == rows["judge:1"][3]    # the playbook bytes hash is recorded
    assert S._stored_sheet(sha, zb64[:-4]) is None and S._stored_sheet(None, zb64) is None


def test_codex_busy_rows_make_no_call_and_never_move_the_error_rate(tmp_path, patched):
    desk_db(tmp_path, n=S.MIN_DAY_CALLS + 5)
    busy = lambda W, p: (None, "codex_busy", None, {"tool_events": 0, "usage": None, "auth": None})
    code, pages, conn = run(tmp_path, busy)                  # every call finds the slot held (the daily report is running)
    assert code == 0 and not pages                           # 25 busy rows: no pause, no page
    assert S.day_rate(conn, "2026-10-25") == (0, 0, None)
    assert conn.execute("SELECT COUNT(*) FROM gpt_judge WHERE call_error='codex_busy'").fetchone()[0] == S.MIN_DAY_CALLS + 5
    conn.execute("INSERT INTO gpt_judge(call_id,ticket_id,call_started_at,day,model,created_at,call_error) VALUES('x','t','2026-10-25T01:00:00','2026-10-25','m',0,'timeout')")
    assert S.day_rate(conn, "2026-10-25") == (1, 1, 1.0)     # a real fault still counts
