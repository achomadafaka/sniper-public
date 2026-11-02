"""scripts/apply_memory_status_updates.py: a zero- or multi-row PATCH must be a loud, non-zero
failure, never a silent success line.

The operator hit this for real, 2026-09-20: passed `session:finding-voldeath-exit-is-roughly-
neutral-2026-09-18` (the identifier's docstring implied), it matched zero rows because "session"
is not a memories.type value (the real type was "project"), and the script printed "Applying 2
memory status update(s)" and exited 0. Nothing changed; the mistake was caught only by querying
the table afterwards.
"""
from __future__ import annotations

import importlib
import json
import sys

import pytest

import bot.supabase_helper as sb

apply_mod = importlib.import_module("scripts.apply_memory_status_updates")


def _report_path(tmp_path, identifier, status="retired"):
    report = {
        "proposals": [
            {"target_kind": "memory", "identifier": identifier, "proposed_status": status,
             "superseded_by": None},
        ],
    }
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report))
    return path


def _run(monkeypatch, tmp_path, identifier, *, apply, fake_request, status="retired"):
    calls = []

    def wrapped(method, path, body=None, extra_headers=None, timeout=10):
        calls.append((method, path, body))
        return fake_request(method, path, body, extra_headers)

    monkeypatch.setattr(apply_mod, "_load_env", lambda: None)
    monkeypatch.setattr(sb, "_request", wrapped)
    argv = ["apply_memory_status_updates.py", str(_report_path(tmp_path, identifier, status))]
    if apply:
        argv.append("--apply")
    monkeypatch.setattr(sys, "argv", argv)
    return apply_mod.main(), calls


def test_a_zero_row_patch_is_a_loud_nonzero_failure(monkeypatch, tmp_path, capsys):
    """The operator's exact failure: a well-formed report whose identifier matches nothing."""
    code, calls = _run(
        monkeypatch, tmp_path, "project:session/does-not-exist-2026-09-20",
        apply=True, fake_request=lambda *a: [],
    )
    out = capsys.readouterr().out
    assert code != 0, "a zero-row PATCH must not exit 0"
    assert "ERROR" in out
    assert "project:session/does-not-exist-2026-09-20" in out
    assert "matched 0 row" in out
    assert calls[0][0] == "PATCH"


def test_a_multi_row_patch_is_also_a_failure(monkeypatch, tmp_path, capsys):
    """A status change fanning out to more than one row is exactly as wrong as hitting none."""
    code, _ = _run(
        monkeypatch, tmp_path, "project:session/ambiguous-name",
        apply=True, fake_request=lambda *a: [{"id": 1}, {"id": 2}],
    )
    out = capsys.readouterr().out
    assert code != 0
    assert "matched 2 row" in out


def test_a_correctly_matched_apply_still_succeeds(monkeypatch, tmp_path, capsys):
    """The fix must not change what a successful apply does."""
    code, calls = _run(
        monkeypatch, tmp_path, "project:session/real-memory-2026-09-20",
        apply=True, fake_request=lambda *a: [{"id": 7}],
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "project:session/real-memory-2026-09-20 -> retired" in out
    assert calls[0][0] == "PATCH"


def test_an_invalid_type_prefix_is_rejected_before_any_request(monkeypatch, tmp_path, capsys):
    """"session" is not a memories.type value -- this must be caught before the network call that
    would otherwise return the same zero rows for a subtler reason."""
    code, calls = _run(
        monkeypatch, tmp_path, "session:finding-voldeath-exit-is-roughly-neutral-2026-09-18",
        apply=True, fake_request=lambda *a: (_ for _ in ()).throw(
            AssertionError("must not reach the network for an invalid type")),
    )
    out = capsys.readouterr().out
    assert code != 0
    assert "not a memory type" in out
    assert calls == [], "an invalid identifier must never reach _request"


def test_a_bad_identifier_stops_the_run_instead_of_reporting_the_rest_as_success(
        monkeypatch, tmp_path, capsys):
    """Two proposals, the first one bad: the second must never be attempted, and the run must
    exit non-zero rather than silently continuing past the failure."""
    report = {
        "proposals": [
            {"target_kind": "memory", "identifier": "project:session/bad", "proposed_status": "retired"},
            {"target_kind": "memory", "identifier": "project:session/good", "proposed_status": "retired"},
        ],
    }
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report))
    calls = []

    def fake_request(method, p, body=None, extra_headers=None, timeout=10):
        calls.append((method, p, body))
        return []   # every PATCH matches nothing in this test

    monkeypatch.setattr(apply_mod, "_load_env", lambda: None)
    monkeypatch.setattr(sb, "_request", fake_request)
    monkeypatch.setattr(sys, "argv", ["apply_memory_status_updates.py", str(path), "--apply"])

    code = apply_mod.main()
    out = capsys.readouterr().out

    assert code != 0
    assert len(calls) == 1, "the second (good-looking) proposal must never be PATCHed"
    assert "session/good" not in out


def test_dry_run_reports_a_would_fail_identifier_without_writing(monkeypatch, tmp_path, capsys):
    """A dry run must do the same lookup a PATCH would and say so -- this is the check that was
    missing and let the bad identifier ship in the first place."""
    code, calls = _run(
        monkeypatch, tmp_path, "project:session/does-not-exist-2026-09-20",
        apply=False, fake_request=lambda *a: [],
    )
    out = capsys.readouterr().out
    assert code != 0, "a dry run that would fail to apply must not exit 0"
    assert "WOULD FAIL" in out
    assert calls[0][0] == "GET", "a dry run must never PATCH"


def test_dry_run_reports_a_correct_identifier_as_matching(monkeypatch, tmp_path, capsys):
    code, calls = _run(
        monkeypatch, tmp_path, "project:session/real-memory-2026-09-20",
        apply=False, fake_request=lambda *a: [{"id": 7}],
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "matches 1 row" in out
    assert calls[0][0] == "GET"


def test_memory_parts_worked_example_from_the_docstring():
    """Pins the exact worked example the docstring gives, so the contract and the doc cannot
    drift apart."""
    assert apply_mod._memory_parts(
        "project:session/finding-voldeath-exit-is-roughly-neutral-2026-09-18",
    ) == ("project", "session/finding-voldeath-exit-is-roughly-neutral-2026-09-18")
    with pytest.raises(ValueError, match="not a memory type"):
        apply_mod._memory_parts("session:finding-voldeath-exit-is-roughly-neutral-2026-09-18")
