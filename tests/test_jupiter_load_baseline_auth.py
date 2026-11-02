"""The load series must be able to show a key being REJECTED, not only one being configured.

On 2026-09-24 the operator's new Jupiter key answered 401 from the 16:01:06Z restart, the funded
lane fell back to keyless `lite-api`, and 119 quotes were rate-limited before the old key was
restored at 18:29:50Z. Across all 148 samples of that window this file recorded `keyed: true` with
the right `key_role`, because both describe what was CONFIGURED and neither describes whether it
worked — so the incident was invisible in the series except as a jump in the rate-limit counter,
and a reader (me) computed "auth_failed 0" from a field that had never been written.

ABSENT IS NOT ZERO. A runner that predates the field leaves it None. Zero is a claim that the key
was accepted, and an old row is entitled to make no claim at all.
"""
import importlib.util
import json
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "research" / "jupiter_load_baseline.py"


def _module():
    spec = importlib.util.spec_from_file_location("jupiter_load_baseline", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _heartbeat(tmp_path, jupiter_load):
    path = tmp_path / "hb.json"
    path.write_text(json.dumps({
        "ts": 1790000000.0, "started_at": "2026-09-24T18:29:50.779339+00:00",
        "events": 3, "jupiter_load": jupiter_load}), encoding="utf-8")
    return path


def _sample(monkeypatch, tmp_path, jupiter_load):
    module = _module()
    monkeypatch.setattr(module, "HEARTBEAT", str(_heartbeat(tmp_path, jupiter_load)))
    monkeypatch.setattr(module, "open_funded_positions", lambda: 0)
    return module.sample()


def test_a_rejected_key_is_visible_in_the_row(monkeypatch, tmp_path):
    """The 401 case: configured AND failing, which `keyed` alone cannot distinguish."""
    row = _sample(monkeypatch, tmp_path, {
        "quotes": 900, "rate_limited": 119, "errors": 0, "auth_failed": 42,
        "keyed": True, "key_role": "MEMECOIN_JUPITER_API_KEY", "by_host": {"lite-api": 900}})
    assert row["auth_failed"] == 42
    # The two fields that were TRUE throughout the incident and said nothing about it.
    assert row["keyed"] is True and row["key_role"] == "MEMECOIN_JUPITER_API_KEY"


def test_a_healthy_key_records_zero_not_absence(monkeypatch, tmp_path):
    row = _sample(monkeypatch, tmp_path, {
        "quotes": 10, "rate_limited": 0, "errors": 0, "auth_failed": 0, "keyed": True})
    assert row["auth_failed"] == 0


def test_an_older_runner_reads_as_ABSENT_never_as_zero(monkeypatch, tmp_path):
    """The whole point. A zero here would assert the key was accepted on 148 rows that cannot say."""
    row = _sample(monkeypatch, tmp_path, {
        "quotes": 10, "rate_limited": 0, "errors": 0, "keyed": True})
    assert "auth_failed" in row, "the key must be present so a reader sees the field exists"
    assert row["auth_failed"] is None, "absent, not 0 — 0 is a claim this row cannot make"


def test_the_existing_columns_are_untouched(monkeypatch, tmp_path):
    """Additive: an older reader of this file keeps every field it already parsed."""
    row = _sample(monkeypatch, tmp_path, {
        "quotes": 7, "rate_limited": 1, "errors": 2, "keyed": False, "key_role": None,
        "by_host": {"api": 7}})
    for key in ("wall", "hb_ts", "started_at", "quotes", "rate_limited", "errors",
                "by_host", "keyed", "key_role", "open_positions", "events"):
        assert key in row, key
    assert row["quotes"] == 7 and row["rate_limited"] == 1 and row["by_host"] == {"api": 7}
