"""Desk JUDGE worker SDK adapter: one result per call, typed failures, deadline. No real model call."""
import asyncio
import importlib.util
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("desk_judge_worker", ROOT / "scripts/memecoin_desk_judge_worker.py")
W = importlib.util.module_from_spec(spec)
spec.loader.exec_module(W)

import claude_agent_sdk as sdk  # noqa: E402


def result(**kw):
    base = dict(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                session_id="s1", stop_reason="end_turn", total_cost_usd=0.0004,
                usage={"input_tokens": 4, "output_tokens": 60, "cache_read_input_tokens": 4000,
                       "service_tier": "standard"},
                result='{"k":"op","v":"S","e":null,"r":null,"c":15}', uuid="r-1")
    base.update(kw)
    r = sdk.ResultMessage.__new__(sdk.ResultMessage)
    for k in sdk.ResultMessage.__dataclass_fields__:
        setattr(r, k, base.get(k))
    return r


def assistant(error=None, model="claude-haiku-5-5"):
    a = sdk.AssistantMessage.__new__(sdk.AssistantMessage)
    for k in sdk.AssistantMessage.__dataclass_fields__:
        setattr(a, k, None)
    a.error, a.model, a.content = error, model, []
    return a


@pytest.fixture(autouse=True)
def _no_real_auth_page(tmp_path, monkeypatch):
    """sdk_call_model pages on a dead login: keep its state out of the repo and its alerts off Telegram."""
    pages = []
    monkeypatch.setattr(W, "AUTH_PAGE_STATE", tmp_path / "auth_page.json")
    monkeypatch.setattr("sniper.services.memecoin_alerts.send_alert", lambda kind, text, **kw: pages.append((kind, text)))
    return pages


def fake_query(messages, delay=0.0):
    async def q(prompt, options):
        assert options.tools == [] and options.setting_sources == [] and options.mcp_servers == {}
        await asyncio.sleep(delay)
        for m in messages:
            yield m
    return q


def test_ok_call_returns_one_frozen_result(monkeypatch):
    monkeypatch.setattr(sdk, "query", fake_query([assistant(), result()]))
    out = W.sdk_call_model("SYS", "{}")
    assert out["call_error"] is None and out["result_id"] == "r-1" and out["session_id"] == "s1"
    assert out["is_error"] is False and out["model_echo"] == "claude-haiku-5-5"
    assert out["notional_usd"] == 0.0004 and out["usage"]["output_tokens"] == 60


def test_dead_login_is_auth_expired(monkeypatch):
    msgs = [assistant(error="authentication_failed", model="<synthetic>"),
            result(is_error=True, result="Failed to authenticate: OAuth session expired")]
    monkeypatch.setattr(sdk, "query", fake_query(msgs))
    out = W.sdk_call_model("SYS", "{}")
    assert out["call_error"] == "auth_expired" and out["is_error"] is True


def test_429_is_rate_limited(monkeypatch):
    monkeypatch.setattr(sdk, "query", fake_query([result(is_error=True, api_error_status=429, result="x")]))
    assert W.sdk_call_model("SYS", "{}")["call_error"] == "rate_limited"


def test_refusal_stop_reason(monkeypatch):
    monkeypatch.setattr(sdk, "query", fake_query([result(stop_reason="refusal")]))
    assert W.sdk_call_model("SYS", "{}")["call_error"] == "refusal"


def test_deadline_gives_timeout_and_no_result(monkeypatch):
    monkeypatch.setattr(sdk, "query", fake_query([result()], delay=0.5))
    t0 = time.monotonic()
    out = W.sdk_call_model("SYS", "{}", deadline_s=0.1)
    assert out["call_error"] == "timeout" and out["result_id"] is None and time.monotonic() - t0 < 0.4


def test_cli_missing_is_cli_failed(monkeypatch):
    async def boom(prompt, options):
        raise sdk.CLINotFoundError("no cli")
        yield  # pragma: no cover
    monkeypatch.setattr(sdk, "query", boom)
    assert W.sdk_call_model("SYS", "{}")["call_error"] == "cli_failed"


def test_playbook_needs_version_and_delimiter(tmp_path):
    p = tmp_path / "pb.md"
    p.write_text("prompt_version: `desk-judge-v9`\nhuman\n---\nSYSTEM BODY\n")
    assert W.load_playbook(p) == ("SYSTEM BODY\n", "desk-judge-v9")
    p.write_text("no version\n---\nbody")
    with pytest.raises(ValueError):
        W.load_playbook(p)


def test_disabled_knob_exits_without_work(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("MEMECOIN_DESK_JUDGE_ENABLED", "0")
    assert W.main(["--desk-db", "x", "--ledger-db", "y", "--playbook", "z"], env_root=tmp_path) == 0
    assert "not 1" in capsys.readouterr().out


def test_second_worker_exits_on_the_lock(monkeypatch, tmp_path, capsys):
    import fcntl
    monkeypatch.setenv("MEMECOIN_DESK_JUDGE_ENABLED", "1")
    (tmp_path / "ledger.db").write_bytes(b"")
    pb = tmp_path / "pb.md"
    pb.write_text("prompt_version: `v`\n---\nbody")
    held = open(tmp_path / "desk_judge_worker.lock", "w")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert W.main(["--desk-db", str(tmp_path / "desk.db"), "--ledger-db", str(tmp_path / "ledger.db"),
                   "--playbook", str(pb), "--calls-per-hour", "5"], env_root=tmp_path) == 0
    assert "holds the lock" in capsys.readouterr().out


def test_cron_env_comes_from_the_state_env_file_and_api_keys_never_reach_the_sdk(monkeypatch, tmp_path, capsys):
    """Gamma FAIL b on 92fbca995: cron has no environment, so the worker reads the state root's .env."""
    import fcntl
    for key in ("MEMECOIN_DESK_JUDGE_ENABLED", "MEMECOIN_DESK_SEAT_CALLS_PER_HOUR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-cron")
    (tmp_path / ".env").write_text("MEMECOIN_DESK_JUDGE_ENABLED=1\nMEMECOIN_DESK_SEAT_CALLS_PER_HOUR=5\n"
                                   "ANTHROPIC_BASE_URL=https://example.invalid\n")
    (tmp_path / "ledger.db").write_bytes(b"")
    pb = tmp_path / "pb.md"
    pb.write_text("prompt_version: `v`\n---\nbody")
    held = open(tmp_path / "desk_judge_worker.lock", "w")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)            # stop at the lock: no DB, no model
    assert W.main(["--desk-db", str(tmp_path / "desk.db"), "--ledger-db", str(tmp_path / "ledger.db"),
                   "--playbook", str(pb)], env_root=tmp_path) == 0
    assert "holds the lock" in capsys.readouterr().out          # knobs read: past "not 1" and the cph check
    import os
    assert "ANTHROPIC_API_KEY" not in os.environ and "ANTHROPIC_BASE_URL" not in os.environ


def test_a_dead_login_that_raises_is_still_auth_expired(monkeypatch):
    # 2026-10-21: the SDK raised after its error result, and the seat read provider_error
    async def raises(prompt, options):
        yield result(is_error=True, result="Failed to authenticate: OAuth session expired and could not be refreshed")
        raise Exception("Command failed with exit code 1")
    monkeypatch.setattr(sdk, "query", raises)
    assert W.sdk_call_model("SYS", "{}")["call_error"] == "auth_expired"
    assert W.classify(None, None, Exception("OAuth session expired")) == "auth_expired"
    assert W.classify(None, None, Exception("boom")) == "provider_error"


def test_auth_pages_once_per_outage_and_once_on_recovery(_no_real_auth_page):
    pages = _no_real_auth_page
    for err in ("auth_expired", "auth_expired", "timeout", "auth_expired"):
        W.auth_page(err)
    assert [k for k, _ in pages] == ["DESK-SDK-AUTH"] and "claude /login" in pages[0][1]
    assert "provider outage" in pages[0][1]                                 # AUTH_TEXT can match an outage (Gamma)
    W.auth_page("rate_limited")                                        # says nothing about the login
    W.auth_page(None)
    W.auth_page(None)
    assert len(pages) == 2 and "works again" in pages[1][1]


def test_an_unwritable_auth_page_state_never_raises(monkeypatch):
    monkeypatch.setattr(W, "AUTH_PAGE_STATE", "/proc/no/such/dir/state.json")
    assert W.auth_page("auth_expired") is None


@pytest.mark.parametrize("text", [["a"], {"k": 1}, 7])
def test_container_text_is_a_typed_failure_with_the_receipt(monkeypatch, text):
    monkeypatch.setattr(sdk, "query", fake_query([assistant(), result(result=text)]))
    out = W.sdk_call_model("SYS", "{}")
    assert out["text"] is None and out["is_error"] is True and out["text_type"] == type(text).__name__
    assert out["call_error"] == "model_result_error"                      # typed at the one boundary
    assert out["notional_usd"] == 0.0004 and out["usage"]["output_tokens"] == 60 and out["result_id"] == "r-1"


@pytest.mark.parametrize("echo", [["claude-haiku-5-5"], {"m": 1}, 5])
def test_container_echo_is_a_marker_never_none(monkeypatch, echo):
    monkeypatch.setattr(sdk, "query", fake_query([assistant(model=echo), result()]))
    out = W.sdk_call_model("SYS", "{}")
    assert out["model_echo"].startswith("nonstr:" + type(echo).__name__) and "model_echo_raw" not in out
    from sniper.services.memecoin_desk_model_echo import echo_ok
    assert echo_ok(out["model_echo"], "claude-haiku-5-5") is False       # None would have passed
    assert echo_ok(None, "claude-haiku-5-5") is True


def test_echo_dated_suffix_is_ascii_digits_only():
    from sniper.services.memecoin_desk_model_echo import echo_ok
    assert echo_ok("claude-haiku-5-5-20261015", "claude-haiku-5-5")
    assert not echo_ok("claude-haiku-5-5-٢٠٢٦١٠١٥", "claude-haiku-5-5")


def test_container_text_keeps_a_classified_cause(monkeypatch):
    monkeypatch.setattr(sdk, "query", fake_query([result(is_error=True, api_error_status=429, result=["x"])]))
    assert W.sdk_call_model("SYS", "{}")["call_error"] == "rate_limited"
