"""Slice 5 Task 1 — AnalysisWindow dataclass."""
from __future__ import annotations

import pytest

from sniper.analysis.wallet_protocol.ledger.window import AnalysisWindow


def _ok_window(**overrides) -> AnalysisWindow:
    base = dict(
        window_id="2026-05-01_eatpraylove_v1",
        capture_run_ids=("run-1",),
        start_ts=1_700_000_000,
        end_ts=1_700_086_400,
        source_versions=(
            ("data_api", "2026-05-02"),
            ("market_price_points", "batch-prices-history-v1"),
        ),
    )
    base.update(overrides)
    return AnalysisWindow(**base)


def test_frozen_dataclass_refuses_attribute_mutation() -> None:
    window = _ok_window()
    with pytest.raises(Exception):
        window.window_id = "other"  # type: ignore[misc]
    with pytest.raises(Exception):
        window.start_ts = 0  # type: ignore[misc]
    with pytest.raises(Exception):
        window.capture_run_ids = ("other",)  # type: ignore[misc]


def test_start_ts_equal_end_ts_is_accepted() -> None:
    window = _ok_window(start_ts=1_700_000_000, end_ts=1_700_000_000)
    assert window.start_ts == window.end_ts


def test_start_ts_greater_than_end_ts_rejected() -> None:
    with pytest.raises(ValueError):
        _ok_window(start_ts=1_700_000_001, end_ts=1_700_000_000)


def test_empty_capture_run_ids_rejected() -> None:
    with pytest.raises(ValueError):
        _ok_window(capture_run_ids=())


def test_capture_run_ids_must_be_a_tuple_not_a_list() -> None:
    """Lists are mutable and would break frozen-dataclass hashability —
    enforce tuple at construction time."""
    with pytest.raises(TypeError):
        _ok_window(capture_run_ids=["run-1"])  # type: ignore[arg-type]


def test_capture_run_ids_must_contain_non_empty_strings() -> None:
    with pytest.raises(ValueError):
        _ok_window(capture_run_ids=("",))


def test_empty_window_id_rejected() -> None:
    with pytest.raises(ValueError):
        _ok_window(window_id="")


def test_source_versions_must_be_a_tuple_not_a_dict() -> None:
    """The bundle uses tuple-of-tuples for hashability; a dict would defeat
    frozen-dataclass hash equality."""
    with pytest.raises(TypeError):
        _ok_window(source_versions={"data_api": "2026-05-02"})  # type: ignore[arg-type]


def test_source_versions_entries_must_be_str_str_pairs() -> None:
    with pytest.raises(TypeError):
        _ok_window(source_versions=(("data_api",),))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        _ok_window(source_versions=(("data_api", 1),))  # type: ignore[arg-type]


def test_identical_windows_compare_equal_and_hash_equal() -> None:
    a = _ok_window()
    b = _ok_window()
    assert a == b
    assert hash(a) == hash(b)
    # Usable as a dict key:
    {a: "x"}[b] == "x"


def test_differing_window_id_changes_hash_and_equality() -> None:
    a = _ok_window(window_id="window-a")
    b = _ok_window(window_id="window-b")
    assert a != b
    assert hash(a) != hash(b)


def test_differing_capture_run_ids_changes_hash_and_equality() -> None:
    a = _ok_window(capture_run_ids=("run-1",))
    b = _ok_window(capture_run_ids=("run-1", "run-2"))
    assert a != b
    assert hash(a) != hash(b)


def test_differing_start_ts_changes_hash_and_equality() -> None:
    a = _ok_window(start_ts=1_700_000_000)
    b = _ok_window(start_ts=1_700_000_001)
    assert a != b
    assert hash(a) != hash(b)


def test_differing_end_ts_changes_hash_and_equality() -> None:
    a = _ok_window(end_ts=1_700_086_400)
    b = _ok_window(end_ts=1_700_086_401)
    assert a != b
    assert hash(a) != hash(b)


def test_differing_source_versions_changes_hash_and_equality() -> None:
    a = _ok_window(
        source_versions=(("data_api", "2026-05-02"),),
    )
    b = _ok_window(
        source_versions=(("data_api", "2026-05-03"),),
    )
    assert a != b
    assert hash(a) != hash(b)


def test_capture_run_id_order_matters_for_hashing() -> None:
    a = _ok_window(capture_run_ids=("run-a", "run-b"))
    b = _ok_window(capture_run_ids=("run-b", "run-a"))
    assert a != b
    assert hash(a) != hash(b)
