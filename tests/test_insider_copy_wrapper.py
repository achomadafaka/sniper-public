"""Tests for insider_copy_agent.py wrapper lifecycle: start-run → query → finish-run."""
import json
import pytest
import importlib.util
import types
from unittest.mock import patch, AsyncMock, MagicMock

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(autouse=True)
def _scrub_claude_agent_sdk_stubs():
    """Keep wrapper tests importable whether or not claude_agent_sdk is installed.

    tests/test_bot_relay.py can leave a stub behind. If the real SDK is not
    installed in this environment, provide a minimal local stub instead.
    """
    real_sdk_installed = importlib.util.find_spec("claude_agent_sdk") is not None
    for key in [k for k in list(sys.modules) if k.startswith("claude_agent_sdk")]:
        mod = sys.modules[key]
        if not getattr(mod, "__file__", None):
            sys.modules.pop(key, None)

    created_stub = False
    if not real_sdk_installed:
        stub = types.ModuleType("claude_agent_sdk")

        class ClaudeAgentOptions:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        class SystemMessage:
            def __init__(self, subtype=None, data=None, **kwargs):
                self.subtype = subtype
                self.data = data or {}

        class ResultMessage:
            def __init__(self, subtype=None, is_error=False, result=None, session_id=None,
                         stop_reason=None, total_cost_usd=None, num_turns=None, **kwargs):
                self.subtype = subtype
                self.is_error = is_error
                self.result = result
                self.session_id = session_id
                self.stop_reason = stop_reason
                self.total_cost_usd = total_cost_usd
                self.num_turns = num_turns

        async def query(*args, **kwargs):
            if False:
                yield None

        stub.ClaudeAgentOptions = ClaudeAgentOptions
        stub.SystemMessage = SystemMessage
        stub.ResultMessage = ResultMessage
        stub.query = query
        sys.modules["claude_agent_sdk"] = stub
        created_stub = True

    # Also force re-import of insider_copy_agent itself so it picks up the real SDK.
    for key in [k for k in list(sys.modules) if k.startswith("agents.") and "insider_copy" in k]:
        sys.modules.pop(key, None)
    yield
    if created_stub:
        sys.modules.pop("claude_agent_sdk", None)


def test_wrapper_invokes_start_run_finish_run(monkeypatch):
    """Wrapper must: call start-run → prepend block to prompt → query → call finish-run."""
    from agents import insider_copy_agent as mod

    called = {"start": False, "finish": False, "prompt_with_block": None}

    def fake_run_predict_tool(args: list[str]) -> dict:
        if "start-run" in args:
            called["start"] = True
            return {
                "ok": True,
                "run_id": "run-uuid-xyz",
                "calibration_block": "## CALIBRATION STATE (last 30 days)\n...\nRED FLAGS: none",
                "calibration_stats": {"graded": 20, "accuracy_pct": 55.0},
            }
        if "finish-run" in args:
            called["finish"] = True
            called["finish_args"] = args
            return {"ok": True, "run_id": "run-uuid-xyz", "status": "success"}
        raise AssertionError(f"unexpected tool call: {args}")

    async def fake_query(prompt, options):
        # Capture prompt so we can assert the calibration block is prepended.
        called["prompt_with_block"] = prompt
        # Yield a ResultMessage-like object.
        # Importing real classes to make isinstance work:
        from claude_agent_sdk import SystemMessage, ResultMessage
        yield SystemMessage(subtype="init", data={"session_id": "s-1"})
        yield ResultMessage(subtype="result", is_error=False,
                            result="done", session_id="s-1", stop_reason="end_turn",
                            total_cost_usd=0.50, num_turns=10,
                            duration_ms=1000, duration_api_ms=800)

    monkeypatch.setattr(mod, "_run_predict_tool", fake_run_predict_tool)
    monkeypatch.setattr(mod, "ClaudeAgentOptions", lambda **kwargs: object())
    monkeypatch.setattr(mod, "query", fake_query)

    import anyio
    anyio.run(mod.run)

    assert called["start"] is True
    assert called["finish"] is True
    # Block must appear at the top of the prompt shown to the SDK
    assert called["prompt_with_block"].startswith("## CALIBRATION STATE")
    # Run id must be passed through to finish-run
    finish = called["finish_args"]
    assert "--run-id" in finish and "run-uuid-xyz" in finish
    assert "--status" in finish and "success" in finish


def test_wrapper_finish_with_error_on_exception(monkeypatch):
    """If query raises, wrapper must still call finish-run with status=error."""
    from agents import insider_copy_agent as mod

    called = {"finish_status": None}

    def fake_run_predict_tool(args: list[str]) -> dict:
        if "start-run" in args:
            return {
                "ok": True, "run_id": "r",
                "calibration_block": "## CALIBRATION STATE\n",
                "calibration_stats": {},
            }
        if "finish-run" in args:
            # Locate --status value
            i = args.index("--status")
            called["finish_status"] = args[i + 1]
            return {"ok": True}
        raise AssertionError(args)

    async def failing_query(prompt, options):
        raise RuntimeError("SDK crashed")
        yield  # pragma: no cover  (satisfies async generator contract)

    monkeypatch.setattr(mod, "_run_predict_tool", fake_run_predict_tool)
    monkeypatch.setattr(mod, "ClaudeAgentOptions", lambda **kwargs: object())
    monkeypatch.setattr(mod, "query", failing_query)

    import anyio
    with pytest.raises(RuntimeError):
        anyio.run(mod.run)

    assert called["finish_status"] == "error"


def test_wrapper_inlines_run_id_into_prompt(monkeypatch):
    from agents import insider_copy_agent as mod

    captured = {}

    def fake_run_predict_tool(args):
        if "start-run" in args:
            return {"ok": True, "run_id": "RID-XYZ", "calibration_block": "## CALIBRATION STATE\n", "calibration_stats": {}}
        return {"ok": True}

    async def fake_query(prompt, options):
        captured["prompt"] = prompt
        from claude_agent_sdk import ResultMessage
        yield ResultMessage(subtype="result", is_error=False, result="done", session_id="s",
                            stop_reason="end_turn", total_cost_usd=0.0, num_turns=1,
                            duration_ms=1, duration_api_ms=1)

    monkeypatch.setattr(mod, "_run_predict_tool", fake_run_predict_tool)
    monkeypatch.setattr(mod, "ClaudeAgentOptions", lambda **kwargs: object())
    monkeypatch.setattr(mod, "query", fake_query)

    import anyio
    anyio.run(mod.run)

    # The prompt the SDK receives must have the literal run_id, not the $RUN_ID placeholder.
    assert "$RUN_ID" not in captured["prompt"]
    assert "RID-XYZ" in captured["prompt"]
