"""SDK and wall durations are recorded independently; API duration is not an exclusive wall-clock phase."""
import importlib.util
from pathlib import Path

import claude_agent_sdk
from claude_agent_sdk import ResultMessage

spec = importlib.util.spec_from_file_location("jw", Path(__file__).resolve().parents[1] / "scripts/memecoin_desk_judge_worker.py")
JW = importlib.util.module_from_spec(spec)
spec.loader.exec_module(JW)


def test_the_result_carries_cli_and_wall_timings(monkeypatch):
    async def fake_query(*, prompt, options):
        yield ResultMessage(subtype="success", duration_ms=5200, duration_api_ms=3900, is_error=False,
                            num_turns=1, session_id="s", result='{"v":"E"}', usage={"output_tokens": 9},
                            total_cost_usd=0.03)
    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    monkeypatch.setattr(JW, "auth_page", lambda *a, **k: None)
    out = JW.sdk_call_model("SYS", "{}", deadline_s=5)
    t = out["timing"]
    assert (t["duration_ms"], t["duration_api_ms"], t["num_turns"]) == (5200, 3900, 1)
    assert t["wall_ms"] >= 0 and out["text"] == '{"v":"E"}'
    assert t["spare_miss_reason"] == "disabled" and t["spare_connect_ms"] is None
    assert out["session_id"] == "s" and out["usage"] == {"output_tokens": 9} and out["notional_usd"] == 0.03


def test_no_result_still_records_the_wall_clock(monkeypatch):
    async def fake_query(*, prompt, options):
        raise RuntimeError("cli died")
        yield
    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    monkeypatch.setattr(JW, "auth_page", lambda *a, **k: None)
    out = JW.sdk_call_model("SYS", "{}", deadline_s=5)
    assert out["timing"]["duration_api_ms"] is None and out["timing"]["wall_ms"] >= 0
    assert out["timing"]["spare_miss_reason"] == "disabled" and out["timing"]["spare_connect_ms"] is None
