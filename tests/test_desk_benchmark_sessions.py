"""Benchmark calls must not become saved chats; ordinary SDK callers keep their defaults."""
import asyncio
import importlib.util
import json
import re
from pathlib import Path
from types import SimpleNamespace

import claude_agent_sdk as sdk
import pytest
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

from test_memecoin_desk_judge_worker import W, assistant, result
from test_memecoin_desk_narrative import G

ROOT = Path(__file__).resolve().parents[1]
NO_SAVE = {"no-session-persistence": None, "print": None}


def check_options(prompt, options, expected):
    assert options.extra_args == expected
    assert options.resume is None and options.continue_conversation is False
    transport = SubprocessCLITransport(prompt=prompt, options=options)
    transport._cli_path = "claude"  # Build argv only; never start a subprocess.
    argv = transport._build_command()
    assert ("--no-session-persistence" in argv) == bool(expected)
    assert ("--print" in argv) == bool(expected)


@pytest.mark.parametrize("kwargs,expected", [({}, {}), ({"persist_session": True}, {}),
                                             ({"persist_session": False}, NO_SAVE)])
@pytest.mark.parametrize("limited", [False, True], ids=["success", "rate-limit"])
def test_adapter_opt_out_keeps_receipts_and_normal_session_defaults(monkeypatch, kwargs, expected, limited):
    async def query(prompt, options):
        check_options(prompt, options, expected)
        yield assistant()
        yield result(is_error=limited, api_error_status=429 if limited else None)

    monkeypatch.setattr(sdk, "query", query)
    out = W.sdk_call_model("SYS", "{}", **kwargs)
    assert out["call_error"] == ("rate_limited" if limited else None)
    assert out["session_id"] == "s1" and out["result_id"] == "r-1"
    assert out["usage"]["output_tokens"] == 60 and out["notional_usd"] == 0.0004
    assert out["model_echo"] == "claude-haiku-5-5" and out["billing_basis"] == "subscription_flat"


def test_theme_gate_default_reference_batches_disable_session_persistence(tmp_path, monkeypatch):
    calls = []

    async def query(prompt, options):
        check_options(prompt, options, NO_SAVE)
        ids = re.findall(r"^([gi]\d+):", prompt, re.M)
        calls.append(ids)
        yield assistant()
        yield result(result=json.dumps(dict.fromkeys(ids, "other")))

    monkeypatch.setattr(sdk, "query", query)
    monkeypatch.setattr(G, "draw", lambda *a: [("mint", "PEPE", "Pepe")])
    monkeypatch.setattr(G, "Watcher", lambda *a: SimpleNamespace(
        start=lambda: None, join=lambda **kw: None, stop=False, rows=[], abort=None))
    assert G.main(["--day", "2026-10-21", "--out", str(tmp_path)],
                  local=lambda *a: {"text": "other"}, sleep=lambda *a: None) == 0
    assert [len(c) for c in calls] == [25, 1]


def test_direct_judge_benchmark_disables_session_persistence(tmp_path, monkeypatch):
    # This old harness reads its playbook and creates its output directory at import.
    playbook = tmp_path / "docs/superpowers/specs/2026-10-21-desk-judge-playbook.md"
    playbook.parent.mkdir(parents=True)
    playbook.write_text("prompt_version: `test`\n---\nSYS")
    monkeypatch.chdir(tmp_path)
    spec = importlib.util.spec_from_file_location("bench_no_sessions", ROOT / "scripts/research/desk_judge_bench.py")
    bench = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bench)

    async def query(prompt, options):
        check_options(prompt, options, NO_SAVE)
        yield result()

    monkeypatch.setattr(sdk, "query", query)
    text, _, meta = asyncio.run(bench.haiku("{}"))
    assert text and meta["out_tok"] == 60 and meta["notional_usd"] == 0.0004
