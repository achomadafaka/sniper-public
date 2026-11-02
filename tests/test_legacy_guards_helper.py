"""Unit tests for the LEGACY_PIPELINE_DISABLED guard helper."""

from __future__ import annotations

import pytest

from sniper.legacy_guards import (
    LegacyPipelineDisabledError,
    assert_legacy_pipeline_enabled,
)


def test_skips_when_env_unset(monkeypatch):
    monkeypatch.delenv("LEGACY_PIPELINE_DISABLED", raising=False)
    monkeypatch.delenv("LEGACY_STREAM_FROZEN", raising=False)
    assert_legacy_pipeline_enabled("test_caller")


def test_skips_when_env_zero(monkeypatch):
    monkeypatch.setenv("LEGACY_PIPELINE_DISABLED", "0")
    monkeypatch.delenv("LEGACY_STREAM_FROZEN", raising=False)
    assert_legacy_pipeline_enabled("test_caller")


def test_raises_when_disabled(monkeypatch):
    monkeypatch.setenv("LEGACY_PIPELINE_DISABLED", "1")
    monkeypatch.delenv("LEGACY_STREAM_FROZEN", raising=False)
    with pytest.raises(LegacyPipelineDisabledError) as excinfo:
        assert_legacy_pipeline_enabled("my_caller_name")
    msg = str(excinfo.value)
    assert "my_caller_name" in msg
    assert "LEGACY_PIPELINE_DISABLED" in msg


def test_raises_when_legacy_stream_frozen_alias(monkeypatch):
    monkeypatch.delenv("LEGACY_PIPELINE_DISABLED", raising=False)
    monkeypatch.setenv("LEGACY_STREAM_FROZEN", "1")
    with pytest.raises(LegacyPipelineDisabledError):
        assert_legacy_pipeline_enabled("test_caller")


def test_either_var_set_to_1_fires(monkeypatch):
    monkeypatch.setenv("LEGACY_PIPELINE_DISABLED", "1")
    monkeypatch.setenv("LEGACY_STREAM_FROZEN", "1")
    with pytest.raises(LegacyPipelineDisabledError):
        assert_legacy_pipeline_enabled("test_caller")


def test_message_includes_pointer_to_spec(monkeypatch):
    monkeypatch.setenv("LEGACY_PIPELINE_DISABLED", "1")
    with pytest.raises(LegacyPipelineDisabledError) as excinfo:
        assert_legacy_pipeline_enabled("test_caller")
    assert "2026-04-30-analyzer-completion-and-archive-design" in str(excinfo.value)
