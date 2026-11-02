"""Unreadable stored input is a retryable failure; absent input remains distinct."""
import json
import sqlite3

import pytest

import test_memecoin_desk_source_scout as F


def add_raw(desk, handle, payload, at=F.T):
    db = sqlite3.connect(desk / "x" / (F.DAY + ".db"))
    db.execute("INSERT INTO posts(event,tweet_id,received_at,handle,payload_json) VALUES('tweet',NULL,?,?,?)",
               (at, handle, payload))
    db.commit()
    db.close()


@pytest.mark.parametrize("payload", ["{", json.dumps({"text": ["UNREADABLE_MARKER"]})])
def test_unreadable_only_is_no_call_error_then_readable_input_enables_success(tmp_path, capsys, payload):
    desk = F.world(tmp_path, posts=[("z_good", "GOOD_CONTROL_MARKER", F.T)])
    add_raw(desk, "a_bad", payload)
    _, db = F._scout_db(tmp_path, pool=[("a_bad", "S", "2026-10-21T00:00:00+00:00"),
                                      ("z_good", "R", "2026-10-21T00:00:00+00:00")])
    calls = []
    def model(system, user):
        calls.append(user)
        return {"text": "kol", "model_echo": F.SC.TYPE_MODEL}
    try:
        F.SC.label_types(db, model, desk / "x", [F.DAY])
        rows = dict(db.execute("SELECT handle,label || ':' || COALESCE(error,'') FROM types"))
        assert len(calls) == 1 and "GOOD_CONTROL_MARKER" in calls[0], calls
        assert rows == {"a_bad": "unknown:evidence_unreadable", "z_good": "kol:"}
        assert "1 unreadable post" in capsys.readouterr().err
        add_raw(desk, "a_bad", json.dumps({"text": "RECOVERED_READABLE_MARKER"}), F.T + 10)
        F.SC.label_types(db, model, desk / "x", [F.DAY])
        assert len(calls) == 2 and "RECOVERED_READABLE_MARKER" in calls[1]
        rows = db.execute("SELECT label,error FROM types WHERE handle='a_bad' ORDER BY rowid").fetchall()
        assert rows == [("unknown", "evidence_unreadable"), ("kol", None)]
        assert db.execute("SELECT COUNT(*) FROM types WHERE handle='z_good'").fetchone()[0] == 1
        F.SC.label_types(db, lambda *_: pytest.fail("successful labels must stay done"), desk / "x", [F.DAY])
    finally:
        db.close()


def test_legitimate_no_posts_stays_distinct_from_observed_unreadable_posts(tmp_path):
    desk = F.world(tmp_path)
    _, db = F._scout_db(tmp_path, pool=[("mentioned_only", "R", "2026-10-21T00:00:00+00:00")])
    calls = []
    try:
        def model(system, user):
            calls.append(user)
            return {"text": "unknown", "model_echo": F.SC.TYPE_MODEL}
        F.SC.label_types(db, model, desk / "x", [F.DAY])
        assert len(calls) == 1 and "(no posts stored)" in calls[0]
        assert db.execute("SELECT label,error FROM types").fetchall() == [("unknown", None)]
        F.SC.label_types(db, lambda *_: pytest.fail("successful unknown remains a model answer"), desk / "x", [F.DAY])
    finally:
        db.close()


def test_partial_bad_and_good_posts_label_readable_input_and_count_read_failure(tmp_path, capsys):
    desk = F.world(tmp_path, posts=[("kol", "READABLE_PARTIAL_MARKER", F.T)])
    add_raw(desk, "kol", json.dumps(["UNREADABLE_MARKER"]), F.T + 1)
    _, db = F._scout_db(tmp_path, pool=[("kol", "S", "2026-10-21T00:00:00+00:00")])
    calls = []
    try:
        def model(system, user):
            calls.append(user)
            return {"text": "kol", "model_echo": F.SC.TYPE_MODEL}
        F.SC.label_types(db, model, desk / "x", [F.DAY])
        assert len(calls) == 1 and "READABLE_PARTIAL_MARKER" in calls[0]
        assert "UNREADABLE_MARKER" not in calls[0]
        assert db.execute("SELECT label,error FROM types").fetchall() == [("kol", None)]
        assert "1 unreadable post" in capsys.readouterr().err
    finally:
        db.close()
