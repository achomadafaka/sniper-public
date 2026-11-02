"""Only safety work completed inside its original deadline may be reused."""
from types import SimpleNamespace

import pytest

from sniper.services import memecoin_evm_early_quote as early


@pytest.mark.parametrize("completed,expected", [(111., (True, "safe")), (113., None), (None, None)])
def test_consumer_uses_completion_time_not_current_time(monkeypatch, completed, expected):
    monkeypatch.setattr(early, "time", SimpleNamespace(monotonic=lambda: 120.))
    safety = early.EarlySafety("coin", 25., 112.)
    safety.result = (True, "safe")
    safety.done_at = completed
    safety.done.set()
    assert safety.wait() == expected


@pytest.mark.parametrize("completed,expected", [(111., (True, "safe")), (113., None)])
def test_callback_records_its_completion_before_publishing_verdict(monkeypatch, completed, expected):
    now = [100.]
    monkeypatch.setattr(early, "time", SimpleNamespace(monotonic=lambda: now[0]))
    def safety(*_):
        now[0] = completed
        return True, "safe"
    worker = early.EarlyEvmQuote(
        mint="coin", quote_fn=lambda *_: {}, sizing_fn=lambda: (25., None),
        trace=None, clock=lambda: now[0], safety_fn=safety, safety_wait_s=12.)
    worker.worker.join(1.)
    assert not worker.worker.is_alive()
    assert worker.safety.done.wait(1.)
    assert worker.safety.wait() == expected
    assert worker.safety.done_at == completed
