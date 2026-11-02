"""New hooks fail open, bound remote work and keep retrieved content labelled as data."""
import json
import subprocess
import sys
import threading
import time

import pytest

from scripts import brain_ambient as amb
from bot.brain_graph import views
from brain_graph_fake_store import FakeStore
from test_brain_graph_views import seeded


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(amb, "STATE_DIR", tmp_path / "sessions")
    monkeypatch.setattr(amb, "_state_root", lambda: tmp_path, raising=False)
    monkeypatch.setattr(views.live, "derive", lambda *a, **k: {"errors": []})


def test_session_boots_populated_graph_and_empty_graph_is_unavailable(capsys):
    amb.cmd_session(store_factory=seeded)
    out = capsys.readouterr().out
    assert out.startswith("[brain boot BEGIN]") and out.rstrip().endswith("[brain boot END]")
    assert "data, not instructions" in out and "BRAIN UNREACHABLE" not in out
    amb.cmd_session(store_factory=FakeStore)
    assert "BRAIN UNREACHABLE" in capsys.readouterr().out


def test_session_timeout_uses_real_snapshot_shape(tmp_path, capsys):
    snap = tmp_path / "snapshot.json"
    snap.write_text(json.dumps({"at": "2026-09-28T20:00:00Z", "role": "alpha", "text": "CACHED BOOT"}))
    release = threading.Event()
    def slow():
        release.wait(2)
        raise RuntimeError("down")
    start = time.monotonic()
    try:
        amb.cmd_session(store_factory=slow, snapshot=snap, budget_s=.03)
        assert time.monotonic() - start < .5
        out = capsys.readouterr().out
        assert "CACHED BOOT" in out and "Nothing below is live" in out
    finally:
        release.set()


def test_role_boot_once_success_only(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(amb, "_role_boot", lambda store, role, **kw: calls.append(role) or "ROLE BOOT")
    data = {"session_id": "s1", "prompt": "You are Gamma"}
    amb.cmd_prompt(data, store_factory=seeded)
    amb.cmd_prompt(data, store_factory=seeded)
    assert calls == ["gamma"]
    assert capsys.readouterr().out.count("ROLE BOOT") == 1


def test_session_budget_includes_git_state_root_resolution(monkeypatch, capsys):
    release = threading.Event()
    def slow_root():
        release.wait(2)
        raise RuntimeError("git unavailable")
    monkeypatch.setattr(amb, "_state_root", slow_root)
    started = time.monotonic()
    try:
        amb.cmd_session(store_factory=seeded, budget_s=.03)
        assert time.monotonic() - started < .5
    finally:
        release.set()


def test_prompt_labels_facts_and_deduplicates_without_embedding(monkeypatch, capsys):
    from bot.brain_graph import search
    store = seeded()
    calls = []
    def ask(s, q, *, limit, embed):
        calls.append(embed)
        return s.rows["brain_facts"]
    monkeypatch.setattr(search, "ask", ask)
    data = {"session_id": "s", "prompt": "Find the deployment trap and explain the relevant evidence."}
    amb.cmd_prompt(data, store_factory=lambda: store)
    out = capsys.readouterr().out
    assert "[brain facts BEGIN]" in out and "[unverified]" in out and "data, not instructions" in out
    amb.cmd_prompt(data, store_factory=lambda: store)
    assert capsys.readouterr().out == "" and calls == [False, False]


def test_prompt_failure_and_disabled_hook_are_noops(monkeypatch, capsys):
    def boom():
        raise RuntimeError("down")
    amb.cmd_prompt({"prompt": "This is a sufficiently long query about deployment evidence."}, store_factory=boom)
    assert capsys.readouterr().out == ""
    monkeypatch.setattr(amb, "ENABLED", False)
    amb.cmd_session(store_factory=boom)
    assert capsys.readouterr().out == ""


def test_hook_cli_never_rejects_malformed_input():
    result = subprocess.run([sys.executable, "scripts/brain_ambient.py", "prompt"],
                            input="not json", capture_output=True, text=True, timeout=3)
    assert result.returncode == 0


HARNESS_INLINE_MAX = 9500     # Alpha 2026-09-30: an 11.6 KB hook boot was saved to a file with a 2 KB preview


def test_the_prompt_hook_role_boot_stays_under_the_harness_inline_limit(capsys):
    from test_brain_graph_views import _heavy
    from datetime import datetime, timezone
    store = seeded(n_traps=30)
    # The hook boots at real now (no `now=`), so tasks and episodes stamped at the fake 09-28 clock
    # read stale or older than 48 h once real time passed them, and OPEN WORK printed empty.
    store.clock = datetime.now(timezone.utc)
    _heavy(store, "alpha", doctrine=60, tasks=40, orders=30, episodes=40)
    amb.cmd_prompt({"session_id": "cap", "prompt": "You are Alpha"}, store_factory=lambda: store)
    out = capsys.readouterr().out
    assert "-- OPEN WORK --" in out and "more OPEN WORK" in out           # the boot really was full
    assert len(out) <= HARNESS_INLINE_MAX, len(out)


def test_a_fixture_store_through_the_prompt_hook_leaves_the_real_snapshot_alone(tmp_path, monkeypatch, capsys):
    """Lambda's 2026-10-02 08:12Z debug script, verbatim but for the state root: cmd_prompt with a
    fixture store and the state root's own snapshot path."""
    from test_brain_graph_views import _heavy
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    (tmp_path / "data").mkdir()
    real = tmp_path / "data" / "brain_snapshot.json"
    real.write_text(json.dumps({"at": "2026-10-02T10:31:00Z", "role": "beta", "text": "REAL BOOT"}))
    store = seeded(n_traps=30)
    _heavy(store, "alpha", doctrine=60, tasks=40, orders=30, episodes=40)
    amb.cmd_prompt({"session_id": "dbg", "prompt": "You are Alpha"}, store_factory=lambda: store)
    out = capsys.readouterr().out
    assert "filler words" not in real.read_text() and "REAL BOOT" in out and "SnapshotRefused" in out


def test_the_prompt_hook_snapshot_fallback_stays_under_the_harness_inline_limit(tmp_path, capsys):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "brain_snapshot.json").write_text(
        json.dumps({"at": "2026-09-30T09:00:00Z", "role": "alpha", "text": "X" * 20000}))
    def down():
        raise RuntimeError("down")
    amb.cmd_prompt({"session_id": "cap2", "prompt": "You are Alpha"}, store_factory=down)
    out = capsys.readouterr().out
    assert "BRAIN UNREACHABLE" in out and len(out) <= HARNESS_INLINE_MAX, len(out)
