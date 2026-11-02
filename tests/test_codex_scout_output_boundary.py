"""Actual SDK parser/adapter to scout label persistence, with only transport faked."""
import json

import claude_agent_sdk as sdk
from claude_agent_sdk._internal.message_parser import parse_message
import pytest

import test_memecoin_desk_source_scout as F
from test_memecoin_desk_judge_worker import W as JW, assistant


@pytest.mark.parametrize("bad_result", [["kol"], {"label": "kol"}, 42])
def test_non_string_sdk_result_is_failed_label_and_next_handle_progresses(tmp_path, monkeypatch, bad_result):
    desk = F.world(tmp_path, posts=[("a_bad", "bad-evidence", F.T), ("z_good", "good-evidence", F.T)])
    _, db = F._scout_db(tmp_path, pool=[("a_bad", "S", "2026-10-21T00:00:00+00:00"),
                                      ("z_good", "R", "2026-10-21T00:00:00+00:00")])
    monkeypatch.setattr(JW, "AUTH_PAGE_STATE", tmp_path / "auth_page.json")
    monkeypatch.setattr("sniper.services.memecoin_alerts.send_alert", lambda *a, **kw: pytest.fail("no external alerts"))
    seen = []

    async def query(prompt, options):
        seen.append(prompt)
        assert options.model == F.SC.TYPE_MODEL
        assert options.tools == [] and options.mcp_servers == {} and options.setting_sources == []
        yield assistant(model=F.SC.TYPE_MODEL)
        # This is a JSON wire result through the installed SDK's production parser. It does not
        # enforce ResultMessage.result's annotation; the real desk adapter forwards it unchanged.
        wire = {"type": "result", "subtype": "success", "duration_ms": 1, "duration_api_ms": 1,
                "is_error": False, "num_turns": 1, "session_id": "offline", "stop_reason": "end_turn",
                "usage": {}, "result": bad_result if "bad-evidence" in prompt else "news"}
        yield parse_message(json.loads(json.dumps(wire)))

    monkeypatch.setattr(sdk, "query", query)
    def call(system, user):
        return JW.sdk_call_model(system, user, model=F.SC.TYPE_MODEL, deadline_s=F.SC.TYPE_DEADLINE_S)
    try:
        assert F.SC.label_types(db, call, desk / "x", [F.DAY]) == 2
        rows = {h: (label, error, model) for h, label, error, model in
                db.execute("SELECT handle,label,error,model FROM types")}
        assert rows["a_bad"][0] == "unknown" and rows["a_bad"][1] is not None
        assert rows["z_good"] == ("news", None, F.SC.TYPE_MODEL)
        assert len(seen) == 2
        assert F.SC.label_types(db, lambda s, u: {"text": "kol", "model_echo": F.SC.TYPE_MODEL},
                               desk / "x", [F.DAY]) == 1  # only the failed label remains retryable
    finally:
        db.close()
