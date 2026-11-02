"""Production GPT transport: bounded calls and truthful receipts, using owned fake children."""
import contextlib
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from sniper.services import memecoin_desk_gpt as G
from scripts.research import judge_gpt_replay as R


SCHEMA = {"type": "object", "additionalProperties": False, "required": ["ok"],
          "properties": {"ok": {"type": "boolean"}}}


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """No login or provider access: only the fixture executable runs."""
    exe = tmp_path / "codex"
    auth = []
    slots = []

    @contextlib.contextmanager
    def slot(*, timeout_s):
        slots.append(timeout_s)
        yield

    checker = SimpleNamespace(
        codex_slot=slot, CodexBusy=type("CodexBusy", (Exception,), {}),
        prepare_codex_home=lambda tmp: (Path(tmp), Path(tmp) / "fake-auth"),
        isolation=lambda *a: [], minimal_env=lambda home: {"PATH": "/usr/bin:/bin"},
        sync_auth_back=lambda copy: auth.append("checked") or "unchanged")
    monkeypatch.setattr(R, "_checker", lambda: checker)
    monkeypatch.setattr(R.shutil, "which", lambda name: str(exe) if name == "codex" else "/usr/bin/bwrap")
    monkeypatch.setattr(G, "_preferred_cli", lambda: None, raising=False)

    def write(body):
        exe.write_text("#!/bin/sh\nout=''; prev=''; for a in \"$@\"; do [ \"$prev\" = -o ] && out=$a; prev=$a; done\n" + body)
        exe.chmod(0o700)
    return SimpleNamespace(write=write, checker=checker, auth=auth, slots=slots, path=tmp_path)


def call(**kw):
    return G.call_model("Return JSON only.", "A fixed transport check.",
                        deadline_s=kw.pop("deadline_s", 2), schema_obj=SCHEMA, **kw)


def test_success_forwards_schema_effort_and_real_stream_receipts(fake):
    argv = fake.path / "argv"
    fake.write("printf '%s\\n' \"$@\" > '" + str(argv) + "'\n"
               "echo '{\"type\":\"thread.started\",\"thread_id\":\"thread-real\"}'\n"
               "echo '{\"type\":\"item.completed\",\"item\":{\"id\":\"item-real\",\"type\":\"agent_message\",\"text\":\"{\\\"ok\\\":true}\"}}'\n"
               "echo '{\"type\":\"turn.completed\",\"usage\":{\"input_tokens\":12,\"cached_input_tokens\":4,\"output_tokens\":2}}'\n"
               "printf '{\"ok\":true}' > \"$out\"\n")
    got = call()
    assert got["call_error"] is None
    assert got["provider"] == "codex_cli" and got["billing_basis"] == "subscription_flat"
    assert got["session_id"] == "thread-real" and got["result_id"] == "item-real"
    assert got["model_requested"] == "gpt-6-luna" and got["model_echo"] is None
    assert got["usage"] == {"input_tokens": 12, "output_tokens": 2, "cache_read_input_tokens": 4}
    args = argv.read_text().splitlines()
    assert args[args.index("-m") + 1] == "gpt-6-luna"
    assert 'model_reasoning_effort="low"' in args
    assert "--output-schema" in args and fake.slots == [0]
    assert fake.auth == ["checked"]


def test_missing_ids_are_not_fabricated_and_observed_echo_is_kept(fake):
    fake.write("echo '{\"type\":\"turn.completed\",\"model\":\"gpt-6-luna-20261001\",\"usage\":{}}'\n"
               "printf '{\"ok\":true}' > \"$out\"\n")
    got = call()
    assert got["session_id"] is None and got["result_id"] is None
    assert got["model_echo"] == "gpt-6-luna-20261001"


def test_historical_subscription_model_is_an_explicit_request(fake):
    args = fake.path / "old-model-argv"
    fake.write("printf '%s\\n' \"$@\" > '" + str(args) + "'\n"
               "printf '{\"ok\":true}' > \"$out\"\n")
    got = call(model="gpt-5.6-luna")
    assert got["call_error"] is None and got["model_requested"] == "gpt-5.6-luna"
    assert args.read_text().splitlines()[args.read_text().splitlines().index("-m") + 1] == "gpt-5.6-luna"


def test_busy_slot_does_not_start_or_wait(fake):
    @contextlib.contextmanager
    def busy(*, timeout_s):
        assert timeout_s == 0
        raise fake.checker.CodexBusy()
        yield
    fake.checker.codex_slot = busy
    started = time.monotonic()
    got = call()
    assert got["call_error"] == "codex_busy" and got["text"] is None
    assert got["no_model_call"] is True
    assert time.monotonic() - started < .2 and not fake.auth


@pytest.mark.parametrize("body,want", [
    ("printf nope > \"$out\"\n", "invalid_json"),
    ("printf '[]' > \"$out\"\n", "invalid_json"),
    ("printf '{\"ok\":NaN}' > \"$out\"\n", "invalid_json"),
    ("printf '{\"ok\":true,\"ok\":false}' > \"$out\"\n", "invalid_json"),
    ("exit 7\n", "cli_failed"),
    ("echo '{\"type\":\"error\",\"message\":\"Rate limit exceeded\"}'\nexit 1\n", "rate_limit"),
])
def test_failures_are_typed_without_a_fallback(fake, body, want):
    fake.write(body)
    got = call()
    assert got["call_error"] == want and got["text"] is None and got["is_error"]
    assert fake.slots == [0]


def test_partial_stdout_cannot_block_the_deadline_and_children_are_stopped(fake):
    child = fake.path / "child"
    fake.write("sleep 30 &\nprintf '%s' $! > '" + str(child) + "'\nprintf '{'\nwait\n")
    started = time.monotonic()
    got = call(deadline_s=.15)
    assert got["call_error"] == "timeout" and time.monotonic() - started < 1
    assert got["no_model_call"] is False
    pid = int(child.read_text())
    status = Path("/proc/%d/status" % pid)

    def stopped():
        try:
            return "State:\tZ" in status.read_text()
        except (FileNotFoundError, ProcessLookupError):   # the child may exit between the listdir and the read (ESRCH)
            return True

    until = time.monotonic() + .2
    while not stopped() and time.monotonic() < until:
        time.sleep(.005)
    assert stopped()


@pytest.mark.parametrize("status_result", [FileNotFoundError(), "State:\tR (running)\n", PermissionError()])
def test_child_cleanup_check_handles_only_vanished_children(fake, monkeypatch, status_result):
    read_text, exists = Path.read_text, Path.exists

    def status_read(path, *args, **kwargs):
        if str(path).startswith("/proc/") and path.name == "status":
            if isinstance(status_result, Exception):
                raise status_result
            return status_result
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", status_read)
    monkeypatch.setattr(Path, "exists", lambda p: True if str(p).startswith("/proc/") and p.name == "status" else exists(p))
    if isinstance(status_result, FileNotFoundError):
        test_partial_stdout_cannot_block_the_deadline_and_children_are_stopped(fake)
    else:
        with pytest.raises(PermissionError if isinstance(status_result, PermissionError) else AssertionError):
            test_partial_stdout_cannot_block_the_deadline_and_children_are_stopped(fake)


def test_tool_event_discards_answer_and_never_syncs_auth(fake):
    fake.write("echo '{\"type\":\"item.started\",\"item\":{\"type\":\"command_execution\"}}'\nsleep 30\n")
    got = call()
    assert got["call_error"] == "tool_event" and got["text"] is None
    assert not fake.auth


def test_setup_consumes_deadline_and_never_starts_late(fake):
    marker = fake.path / "started"
    fake.write("touch '" + str(marker) + "'\n")
    def slow(tmp):
        time.sleep(.04)
        return Path(tmp), Path(tmp) / "fake-auth"
    fake.checker.prepare_codex_home = slow
    got = call(deadline_s=.01)
    assert got["call_error"] == "timeout" and not marker.exists()


def test_bad_input_refuses_before_credentials_or_process(fake):
    with pytest.raises(ValueError, match="gpt_model"):
        call(model="claude-opus-5-5")
    with pytest.raises(ValueError, match="gpt_effort"):
        call(effort="none")


def test_frozen_output_budget_is_never_silently_ignored(fake):
    got = call(max_output_tokens=240)
    assert got["call_error"] == "output_budget_unsupported"
    assert got["no_model_call"] is True
    assert fake.slots == [] and not fake.auth and got["text"] is None


def test_non_dict_usage_stays_unknown(fake):
    fake.write("echo '{\"type\":\"turn.completed\",\"usage\":[]}'\n"
               "printf '{\"ok\":true}' > \"$out\"\n")
    got = call()
    assert got["call_error"] is None and got["usage"] == {}


@pytest.mark.parametrize("missing,want", [("codex", "cli_missing"), ("bwrap", "no_isolation")])
def test_missing_runtime_does_not_fall_back(fake, monkeypatch, missing, want):
    which = R.shutil.which
    monkeypatch.setattr(R.shutil, "which", lambda name: None if name == missing else which(name))
    got = call()
    assert got["call_error"] == want and got["no_model_call"] is True
    assert fake.slots == [0] and not fake.auth


def test_missing_login_never_launches(fake):
    fake.checker.prepare_codex_home = lambda tmp: (None, None)
    assert call()["call_error"] == "no_codex_login"
    assert not fake.auth


def test_startup_exception_is_typed_without_claiming_an_answer(fake, monkeypatch):
    def fail(*a, **kw):
        raise OSError("fixture failure")
    monkeypatch.setattr(R.subprocess, "Popen", fail)
    got = call()
    assert got["call_error"] == "transport_error" and got["result_id"] is None
    assert got["transport_meta"]["exception"] == "OSError"


WARNING = "echo '{\"type\":\"item.completed\",\"item\":{\"type\":\"error\",\"message\":\"Model metadata for `gpt-6-luna` not found. Defaulting to fallback metadata; this can degrade performance and cause issues.\"}}'\n"


def test_nonterminal_warning_then_valid_answer_succeeds(fake):
    fake.write(WARNING + "printf '{\"ok\":true}' > \"$out\"\n"
               "echo '{\"type\":\"turn.completed\",\"usage\":{\"output_tokens\":2}}'\n")
    got = call()
    assert got["call_error"] is None and got["text"] == '{"ok":true}'
    assert got["transport_meta"]["warnings"] == ["model_metadata_fallback"]
    assert got["transport_meta"]["tool_events"] == 0


def test_nonterminal_warning_then_hang_hits_the_original_deadline(fake):
    fake.write(WARNING + "sleep 30\n")
    started = time.monotonic()
    got = call(deadline_s=.15)
    assert got["call_error"] == "timeout" and time.monotonic() - started < 1
    assert got["transport_meta"]["warnings"] == ["model_metadata_fallback"]


def test_nonterminal_warning_then_terminal_failure_keeps_actual_failure(fake):
    fake.write(WARNING + "echo '{\"type\":\"turn.failed\",\"error\":{\"message\":\"Rate limit exceeded\"}}'\n")
    got = call()
    assert got["call_error"] == "rate_limit" and got["text"] is None
    assert got["transport_meta"]["warnings"] == ["model_metadata_fallback"]


def test_warning_does_not_hide_a_genuine_tool_event(fake):
    fake.write(WARNING + "echo '{\"type\":\"item.started\",\"item\":{\"type\":\"command_execution\"}}'\nsleep 30\n")
    got = call()
    assert got["call_error"] == "tool_event" and not fake.auth
    assert got["transport_meta"]["warnings"] == ["model_metadata_fallback"]


def test_bundled_cli_is_selected_only_when_it_is_executable(tmp_path, monkeypatch):
    exe = tmp_path / "bundled-codex"
    monkeypatch.setattr(G, "BUNDLED_CLI", exe)
    assert G._preferred_cli() is None
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o600)
    assert G._preferred_cli() is None
    exe.chmod(0o700)
    assert G._preferred_cli() == str(exe)


def test_explicit_bundled_binary_is_used_without_changing_the_environment(fake, monkeypatch):
    exe = fake.path / "codex"
    fake.write("printf '{\"ok\":true}' > \"$out\"\n")
    monkeypatch.setattr(G, "_preferred_cli", lambda: str(exe))
    monkeypatch.setattr(R.shutil, "which", lambda name: None if name == "codex" else "/usr/bin/bwrap")
    before = os.environ.copy()
    got = call()
    assert got["call_error"] is None and got["timing"]["cli_path"] == str(exe)
    assert os.environ == before
