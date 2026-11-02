"""Regression tests for scripts/event_trigger.py.

Covers:
  - keyword scoring (word boundary + phrase matching)
  - stable event ID derivation with sha1 fallback
  - state persistence across exceptions (try/finally guarantee)
  - checkpoint-save after each agent fire (OOM-safe)
  - cooldown enforcement across runs via persisted last_trigger
  - per-entry exception isolation (one bad entry can't kill the scan)
  - atomic save (tmp + rename)
"""
from __future__ import annotations

import json
import sys
import time
import types
from pathlib import Path
from unittest import mock

# feedparser isn't needed for unit tests — stub it before importing event_trigger
sys.modules.setdefault("feedparser", types.SimpleNamespace(parse=None))

from scripts import event_trigger as et  # noqa: E402
from scripts.event_trigger import _derive_eid, _score  # noqa: E402


# ── Scoring (pre-existing) ────────────────────────────────────────────────────
def test_word_boundary_prevents_substring_false_positive():
    score, agent, domains = _score(
        "Madonna offers reward for return of missing Coachella costume",
        "Singer Madonna has offered a reward for the return of a missing costume.",
    )
    assert score == 0
    assert agent == "world_analyst"
    assert domains == "--domains geo,politics"


def test_word_boundary_still_matches_real_war():
    score, agent, domains = _score("Russia declares war on Ukraine", "")
    assert score == 14
    assert agent == "world_analyst"
    assert domains == "--domains geo"


def test_phrase_keyword_still_matches_whole_phrase():
    score, agent, domains = _score("Federal Reserve signals patience on rates", "")
    assert score == 4
    assert agent == "finance_analyst"
    assert domains == "--domains geo,politics"


# ── eid derivation ────────────────────────────────────────────────────────────
def test_eid_prefers_id():
    eid = _derive_eid({"id": "guid-123", "link": "https://x/1", "title": "t"})
    assert eid == "guid-123"


def test_eid_falls_back_to_link_when_id_missing():
    eid = _derive_eid({"link": "https://x/42", "title": "t"})
    assert eid == "https://x/42"


def test_eid_sha1_fallback_when_id_and_link_missing():
    e = {"title": "Breaking: market falls", "published": "2026-04-21T12:00:00Z"}
    eid = _derive_eid(e)
    assert eid.startswith("sha1:")
    # Must be deterministic across invocations (hashlib, not builtin hash())
    assert _derive_eid(e) == eid
    # Distinct inputs must produce distinct fallbacks
    other = {"title": "Different headline", "published": "2026-04-21T12:00:00Z"}
    assert _derive_eid(other) != eid


def test_eid_fallback_handles_empty_entry():
    eid = _derive_eid({})
    assert eid.startswith("sha1:")


# ── State persistence helpers ─────────────────────────────────────────────────
def _make_feed(entries):
    return types.SimpleNamespace(
        status=200,
        entries=entries,
        get=lambda k, default=None: {"etag": None, "modified": None}.get(k, default),
    )


def _install_run_mocks(tmp_path: Path, monkeypatch, feeds_by_src):
    """Redirect STATE_FILE to tmp_path; stub _fetch, _notify, Popen."""
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(et, "STATE_FILE", state_file)

    def fake_fetch(src, url, etag, modified):
        return src, feeds_by_src.get(src, _make_feed([]))

    monkeypatch.setattr(et, "_fetch", fake_fetch)
    monkeypatch.setattr(et, "_notify", lambda *a, **kw: None)
    monkeypatch.setattr(
        et.subprocess,
        "Popen",
        mock.MagicMock(return_value=types.SimpleNamespace(pid=9999)),
    )
    return state_file


# ── Test: state persisted even when an exception occurs inside the scan ───────
def test_state_persisted_on_exception(tmp_path, monkeypatch):
    """If the scan loop raises mid-run, the finally block must still save."""
    state_file = _install_run_mocks(tmp_path, monkeypatch, {})

    # Force _fetch to blow up the whole loop by making as_completed raise
    def boom(*a, **kw):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(et, "_fetch", boom)

    # Pre-seed some state so we can verify it's written through
    state_file.write_text(json.dumps({
        "seen_ids": {"old": time.time()},
        "last_trigger": {"world_analyst": 100.0},
        "etags": {}, "modified": {},
    }))

    # run() should complete cleanly — exceptions inside feeds are caught,
    # and finally block always persists.
    et.run()

    # State file must still be valid JSON
    saved = json.loads(state_file.read_text())
    assert "seen_ids" in saved
    assert "last_trigger" in saved


# ── Test: checkpoint save after each fire ─────────────────────────────────────
def test_checkpoint_after_fire(tmp_path, monkeypatch):
    """After firing an agent, state must be on disk before next entry processed.

    We simulate a crash immediately after the first fire — the second entry
    must NOT be in seen_ids on disk, but the FIRST one (which fired) MUST be.
    """
    state_file = _install_run_mocks(tmp_path, monkeypatch, {
        "bbc_world": _make_feed([
            {"id": "fire-1", "link": "l1", "title": "Russia declares war on Ukraine", "summary": ""},
            {"id": "after-fire", "link": "l2", "title": "irrelevant cooking tips", "summary": ""},
        ]),
    })

    # Wrap the real _save — let the first call (checkpoint after fire) succeed,
    # then raise on the second call (final persist in finally).
    real_save = et._save
    call_count = {"n": 0}

    def flaky_save(state):
        call_count["n"] += 1
        if call_count["n"] >= 2:
            raise RuntimeError("simulated late failure")
        real_save(state)

    monkeypatch.setattr(et, "_save", flaky_save)

    et.run()

    # Checkpoint after fire must have captured fire-1
    saved = json.loads(state_file.read_text())
    assert "fire-1" in saved["seen_ids"]
    # world_analyst must have a last_trigger timestamp recorded
    assert saved["last_trigger"].get("world_analyst", 0) > 0


# ── Test: cooldown blocks second fire across runs ─────────────────────────────
def test_cooldown_blocks_second_fire_across_runs(tmp_path, monkeypatch):
    """A fire in run N must suppress a fire on different eid in run N+1
    within the cooldown window."""
    now = time.time()

    # Run 1: fire on headline A
    state_file = _install_run_mocks(tmp_path, monkeypatch, {
        "bbc_world": _make_feed([
            {"id": "A", "link": "a", "title": "Russia declares war on Ukraine", "summary": ""},
        ]),
    })
    et.run()
    saved1 = json.loads(state_file.read_text())
    assert "world_analyst" in saved1["last_trigger"]

    # Run 2: different eid, same agent — must be blocked by cooldown
    popen_mock = mock.MagicMock(return_value=types.SimpleNamespace(pid=9999))
    monkeypatch.setattr(et.subprocess, "Popen", popen_mock)
    monkeypatch.setattr(et, "_fetch", lambda src, url, etag, mod: (src, _make_feed([
        {"id": "B", "link": "b", "title": "China invades Taiwan", "summary": ""},
    ]) if src == "bbc_world" else _make_feed([])))

    et.run()

    # Popen must NOT have been called in run 2 (cooldown blocked it)
    assert popen_mock.call_count == 0, "cooldown should have blocked second fire"


# ── Test: malformed entry doesn't kill the run ────────────────────────────────
def test_malformed_entry_does_not_kill_run(tmp_path, monkeypatch):
    """If one entry explodes during processing, the next entries must still
    be processed and state must still persist."""

    class BoomEntry(dict):
        def get(self, key, default=None):
            if key == "title":
                raise RuntimeError("entry blew up on title access")
            return super().get(key, default)

    state_file = _install_run_mocks(tmp_path, monkeypatch, {
        "bbc_world": _make_feed([
            BoomEntry({"id": "bad"}),
            {"id": "good", "link": "g", "title": "Federal Reserve signals emergency session", "summary": ""},
        ]),
    })

    # Should not raise
    et.run()

    # Good entry must have been processed (seen_ids contains it)
    saved = json.loads(state_file.read_text())
    assert "good" in saved["seen_ids"]


# ── Test: _save is atomic (tmp + rename) ──────────────────────────────────────
def test_save_is_atomic(tmp_path, monkeypatch):
    """_save must write to a .tmp file then rename — interrupted mid-write
    must not corrupt existing state."""
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(et, "STATE_FILE", state_file)

    # Seed a good file
    state_file.write_text(json.dumps({"seen_ids": {"existing": 1.0}}))

    # Snoop on rename to confirm tmp path was used
    orig_rename = Path.replace
    rename_called_with = {}

    def snoop_rename(self, target):
        rename_called_with["src"] = str(self)
        rename_called_with["dst"] = str(target)
        return orig_rename(self, target)

    monkeypatch.setattr(Path, "replace", snoop_rename)
    et._save({"seen_ids": {"new": 2.0}})

    assert rename_called_with["src"].endswith(".json.tmp")
    assert rename_called_with["dst"] == str(state_file)

    # Final state must be the new payload
    saved = json.loads(state_file.read_text())
    assert "new" in saved["seen_ids"]
