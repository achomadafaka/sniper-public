"""Tests for `_persist_flipper_grains_helper` structured outcome marker.

Reviewer finding #3 (2026-05-17): `run_wallet_protocol.py` returned 0
even when `--persist-flipper-grains` was set and the writer never
actually wrote anything (import error, missing Supabase env, writer
raised). The seed runner trusted the subprocess exit code, so
`--execute` could exit 0 while writing zero rows — silent operator
deceit.

Fix: the helper emits a single machine-parseable marker line to stdout
on every code path. Format:

    [PERSIST_FLIPPER_GRAINS_RESULT] {"status": "...", ...}

Status values:
  * wrote   — rows_written > 0, success
  * skipped — writer returned a skipped_reason (idempotent / no rows)
  * failed  — import error, env missing, or writer raised

The seed runner parses these markers to build an honest per-wallet
outcome summary and to propagate failure via its own exit code.
"""
from __future__ import annotations

import json
from argparse import Namespace
from types import SimpleNamespace

import pytest


PERSIST_MARKER_PREFIX = "[PERSIST_FLIPPER_GRAINS_RESULT]"


@pytest.fixture
def fake_report():
    """A minimum report shape that _persist_flipper_grains_helper reads.

    The real ProtocolReport has many more fields, but the persist helper
    only passes `report` through to the writer + optionally reads
    `report.wallet` for the marker payload. SimpleNamespace is enough."""
    return SimpleNamespace(
        wallet="0xabc",
        wallet_fixture_hash="hashbeef",
    )


@pytest.fixture
def fake_args():
    """argparse-ish namespace covering the fields the helper inspects."""
    return Namespace(
        from_fixture=None,
        persist_flipper_grains=True,
    )


def _last_marker_line(captured_stdout: str) -> dict:
    """Find the most recent persist-flipper-grains marker line and
    parse its JSON payload."""
    marker_lines = [
        ln for ln in captured_stdout.splitlines()
        if ln.startswith(PERSIST_MARKER_PREFIX)
    ]
    assert marker_lines, (
        f"no {PERSIST_MARKER_PREFIX} marker in stdout. Got:\n{captured_stdout!r}"
    )
    payload_text = marker_lines[-1][len(PERSIST_MARKER_PREFIX):].strip()
    return json.loads(payload_text)


def test_persist_flipper_grains_emits_wrote_marker_on_success(
    monkeypatch, capsys, fake_args, fake_report,
):
    """Happy path: writer returns rows. Marker must report status=wrote
    with rows_written / rows_superseded counts."""
    from scripts import run_wallet_protocol as rwp
    from sniper.analysis.wallet_protocol.ledger import flipper_tag_grain_writer
    from sniper.analysis.wallet_protocol.ledger import bot_supabase_adapter

    class _StubAdapter:
        pass

    class _WriteResult:
        wallet = "0xabc"
        wallet_fixture_hash = "hashbeef"
        rows_written = 5
        rows_superseded = 2
        skipped_reason = None

    monkeypatch.setattr(
        bot_supabase_adapter.BotSupabaseAdapter,
        "from_env_or_dotenv",
        classmethod(lambda cls: _StubAdapter()),
    )
    monkeypatch.setattr(
        flipper_tag_grain_writer,
        "persist_flipper_tag_grains",
        lambda report, *, supabase, fixture_path, code_version: _WriteResult(),
    )

    rwp._persist_flipper_grains_helper(fake_args, report=fake_report)

    payload = _last_marker_line(capsys.readouterr().out)
    assert payload["status"] == "wrote"
    assert payload["wallet"] == "0xabc"
    assert payload["rows_written"] == 5
    assert payload["rows_superseded"] == 2


def test_persist_flipper_grains_emits_skipped_marker_when_writer_skips(
    monkeypatch, capsys, fake_args, fake_report,
):
    """Writer returned a skipped_reason (legitimate idempotent no-op or
    no-flipper-grains). Marker status=skipped, reason carried through."""
    from scripts import run_wallet_protocol as rwp
    from sniper.analysis.wallet_protocol.ledger import flipper_tag_grain_writer
    from sniper.analysis.wallet_protocol.ledger import bot_supabase_adapter

    class _StubAdapter: pass

    class _Skipped:
        wallet = "0xabc"
        wallet_fixture_hash = "hashbeef"
        rows_written = 0
        rows_superseded = 0
        skipped_reason = "idempotent — 4 rows already exist at this hash"

    monkeypatch.setattr(
        bot_supabase_adapter.BotSupabaseAdapter,
        "from_env_or_dotenv",
        classmethod(lambda cls: _StubAdapter()),
    )
    monkeypatch.setattr(
        flipper_tag_grain_writer,
        "persist_flipper_tag_grains",
        lambda report, *, supabase, fixture_path, code_version: _Skipped(),
    )

    rwp._persist_flipper_grains_helper(fake_args, report=fake_report)

    payload = _last_marker_line(capsys.readouterr().out)
    assert payload["status"] == "skipped"
    assert payload["wallet"] == "0xabc"
    assert "idempotent" in payload["skipped_reason"]


def test_persist_flipper_grains_emits_failed_marker_on_env_missing(
    monkeypatch, capsys, fake_args, fake_report,
):
    """BotSupabaseAdapter.from_env_or_dotenv raises → marker status=failed
    with reason=env_missing. Helper must not swallow this silently."""
    from scripts import run_wallet_protocol as rwp
    from sniper.analysis.wallet_protocol.ledger import bot_supabase_adapter

    def _raise(cls):
        raise RuntimeError(
            "BotSupabaseAdapter: SUPABASE_URL and SUPABASE_(SERVICE|ANON)_KEY "
            "must be set in environment or .env"
        )

    monkeypatch.setattr(
        bot_supabase_adapter.BotSupabaseAdapter,
        "from_env_or_dotenv",
        classmethod(_raise),
    )

    rwp._persist_flipper_grains_helper(fake_args, report=fake_report)

    payload = _last_marker_line(capsys.readouterr().out)
    assert payload["status"] == "failed"
    assert payload["reason"] == "env_missing"
    assert payload["wallet"] == "0xabc"


def test_persist_flipper_grains_emits_failed_marker_on_writer_error(
    monkeypatch, capsys, fake_args, fake_report,
):
    """persist_flipper_tag_grains raises → marker status=failed,
    reason=writer_error, detail carries the exception class + message."""
    from scripts import run_wallet_protocol as rwp
    from sniper.analysis.wallet_protocol.ledger import flipper_tag_grain_writer
    from sniper.analysis.wallet_protocol.ledger import bot_supabase_adapter

    class _StubAdapter: pass

    def _raise_writer(report, *, supabase, fixture_path, code_version):
        raise ValueError("partial unique violation in row group")

    monkeypatch.setattr(
        bot_supabase_adapter.BotSupabaseAdapter,
        "from_env_or_dotenv",
        classmethod(lambda cls: _StubAdapter()),
    )
    monkeypatch.setattr(
        flipper_tag_grain_writer,
        "persist_flipper_tag_grains",
        _raise_writer,
    )

    rwp._persist_flipper_grains_helper(fake_args, report=fake_report)

    payload = _last_marker_line(capsys.readouterr().out)
    assert payload["status"] == "failed"
    assert payload["reason"] == "writer_error"
    assert "ValueError" in payload["detail"]
    assert "partial unique violation" in payload["detail"]


def test_persist_flipper_grains_zero_rows_without_skip_reason_is_failed(
    monkeypatch, capsys, fake_args, fake_report,
):
    """Defensive invariant: if the writer returns `rows_written=0` AND
    `skipped_reason` is None, treat that as a contract violation and
    emit status=failed/reason=zero_rows_written. Otherwise the seed
    runner would count this as a successful write and the same "looks
    successful, wrote zero rows" deceit returns through a different
    code path."""
    from scripts import run_wallet_protocol as rwp
    from sniper.analysis.wallet_protocol.ledger import flipper_tag_grain_writer
    from sniper.analysis.wallet_protocol.ledger import bot_supabase_adapter

    class _StubAdapter: pass

    class _EmptyWriteNoSkipReason:
        wallet = "0xabc"
        wallet_fixture_hash = "hashbeef"
        rows_written = 0
        rows_superseded = 0
        skipped_reason = None  # <- contract violation

    monkeypatch.setattr(
        bot_supabase_adapter.BotSupabaseAdapter,
        "from_env_or_dotenv",
        classmethod(lambda cls: _StubAdapter()),
    )
    monkeypatch.setattr(
        flipper_tag_grain_writer,
        "persist_flipper_tag_grains",
        lambda report, *, supabase, fixture_path, code_version: _EmptyWriteNoSkipReason(),
    )

    rwp._persist_flipper_grains_helper(fake_args, report=fake_report)

    payload = _last_marker_line(capsys.readouterr().out)
    assert payload["status"] == "failed", (
        f"rows_written=0 + skipped_reason=None must yield status=failed, "
        f"got: {payload}"
    )
    assert payload["reason"] == "zero_rows_written"
    assert payload["wallet"] == "0xabc"


def test_persist_flipper_grains_marker_is_single_machine_parseable_line(
    monkeypatch, capsys, fake_args, fake_report,
):
    """The marker must be a single line so seed runner can grep it with
    confidence. JSON payload must parse cleanly (no embedded newlines)."""
    from scripts import run_wallet_protocol as rwp
    from sniper.analysis.wallet_protocol.ledger import flipper_tag_grain_writer
    from sniper.analysis.wallet_protocol.ledger import bot_supabase_adapter

    class _StubAdapter: pass

    class _WriteResult:
        wallet = "0xabc"
        wallet_fixture_hash = "hashbeef"
        rows_written = 1
        rows_superseded = 0
        skipped_reason = None

    monkeypatch.setattr(
        bot_supabase_adapter.BotSupabaseAdapter,
        "from_env_or_dotenv",
        classmethod(lambda cls: _StubAdapter()),
    )
    monkeypatch.setattr(
        flipper_tag_grain_writer,
        "persist_flipper_tag_grains",
        lambda report, *, supabase, fixture_path, code_version: _WriteResult(),
    )

    rwp._persist_flipper_grains_helper(fake_args, report=fake_report)
    captured = capsys.readouterr().out
    marker_lines = [
        ln for ln in captured.splitlines()
        if ln.startswith(PERSIST_MARKER_PREFIX)
    ]
    assert len(marker_lines) == 1, f"expected exactly one marker line, got {marker_lines!r}"
    # Survives round-trip — no truncation or stray newlines.
    parsed = json.loads(marker_lines[0][len(PERSIST_MARKER_PREFIX):].strip())
    assert isinstance(parsed, dict)
