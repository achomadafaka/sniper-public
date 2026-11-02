"""analyze_roster: deep-analyze a wallet list before grading (a grade must be analyzer-backed)."""
from __future__ import annotations
import importlib
import pytest
AR = importlib.import_module("scripts.analyze_roster")


def test_main_no_args_is_usage_error(monkeypatch):
    monkeypatch.setattr(
        AR,
        "run_extractor",
        lambda *_args, **_kwargs: pytest.fail("an explicit empty argv used pytest arguments"),
    )
    assert AR.main([]) == 2


def test_already_analyzed_returns_a_set():
    assert isinstance(AR._already_analyzed(), set)
