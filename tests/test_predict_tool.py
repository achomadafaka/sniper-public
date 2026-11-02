from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agents.tools import predict_tool


def test_resolve_verify_after_uses_verify_hours_for_non_calendar_predictions():
    before = datetime.now(timezone.utc)
    args = SimpleNamespace(
        prediction="BTC will stay above 70000 over the next two days",
        condition="Check spot price in 48 hours",
        verify_hours=48,
        verify_at=None,
    )

    verify_after = datetime.fromisoformat(predict_tool._resolve_verify_after(args))
    after = datetime.now(timezone.utc)

    assert before + timedelta(hours=47, minutes=59) <= verify_after <= after + timedelta(hours=48, minutes=1)


def test_resolve_verify_after_requires_explicit_timestamp_for_calendar_deadlines():
    args = SimpleNamespace(
        prediction="Trade idea 0xabc will resolve YES by March 29",
        condition="Check after the March 29 close",
        verify_hours=48,
        verify_at=None,
    )

    with pytest.raises(ValueError, match="--verify-at"):
        predict_tool._resolve_verify_after(args)


def test_resolve_verify_after_normalizes_explicit_iso_timestamp():
    args = SimpleNamespace(
        prediction="Brent will stay below $90 through April",
        condition="Check end of month settlement",
        verify_hours=48,
        verify_at="2026-04-30T23:59:00Z",
    )

    assert predict_tool._resolve_verify_after(args) == "2026-04-30T23:59:00+00:00"
