"""Refused-entry evidence: a band refusal must keep the mcap that produced the band, and the
alert must show it (2026-07-27).

The first production floor refusal wrote terminal_reason="coin band is not reservable: 'micro'"
with classification_json NULL and rendered "? — mcap ~?", because:
  1. prepare_funded_contract raised BEFORE persisting the classification, and the callers roll
     back on ValueError, so the resolved effective_mcap was computed then discarded;
  2. the alert's mcap fallbacks read only OUR tables, and a coin seen solely via follow_activity
     has no non-zero reading to borrow;
  3. the follow_activity lane hardcoded ticker='' / mcap=0.0 into every event it wrote.
"""
import json

import pytest

from sniper.services import memecoin_follow_activity as fa
from sniper.services.memecoin_risk import BandRefused, persist_refused_classification


def _cls_payload(mcap):
    return json.dumps({"coin_band": "micro", "facts": {"effective_mcap": mcap}})


# --------------------------------------------------------------- fix 1: evidence survives rollback
def test_band_refused_carries_the_classification():
    exc = BandRefused("micro", classification_json=_cls_payload(120_000.0),
                      classification_hash="h1", classification_version=1)
    assert isinstance(exc, ValueError)                 # callers still catch it as before
    assert "not reservable" in str(exc) and "micro" in str(exc)
    assert json.loads(exc.classification_json)["facts"]["effective_mcap"] == 120_000.0


def test_persist_refused_classification_writes_after_rollback():
    seen = {}

    class _M:
        @staticmethod
        def set_entry_attempt_classification(conn, attempt_id, **kw):
            seen.update({"attempt_id": attempt_id, **kw})
            return True

    import sniper.services.memecoin_risk as risk
    original, risk.m = risk.m, _M
    try:
        exc = BandRefused("nano", classification_json=_cls_payload(9_000.0),
                          classification_hash="h2", classification_version=1)
        assert persist_refused_classification(object(), "att-1", exc) is True
    finally:
        risk.m = original
    assert seen["attempt_id"] == "att-1"
    assert seen["commit"] is True                      # must survive the caller's rollback
    assert json.loads(seen["classification_json"])["facts"]["effective_mcap"] == 9_000.0


def test_persist_refused_classification_ignores_other_errors_and_never_raises():
    assert persist_refused_classification(object(), "att-1", ValueError("something else")) is False
    assert persist_refused_classification(object(), None, BandRefused(
        "micro", classification_json="{}", classification_hash="h", classification_version=1)) is False

    class _Boom:
        @staticmethod
        def set_entry_attempt_classification(*a, **k):
            raise RuntimeError("db down")

    import sniper.services.memecoin_risk as risk
    original, risk.m = risk.m, _Boom
    try:
        exc = BandRefused("micro", classification_json="{}", classification_hash="h",
                          classification_version=1)
        assert persist_refused_classification(object(), "att-1", exc) is False   # logged, not raised
    finally:
        risk.m = original


# ------------------------------------------------------------------ fix 2: the alert shows the mcap
@pytest.mark.parametrize("payload,expected", [
    (_cls_payload(120_000.0), 120_000.0),
    (_cls_payload(0.0), None),                          # 0 is "unknown", never a real cap
    (json.dumps({"facts": {}}), None),
    ("not json", None),
    (None, None),
])
def test_classified_effective_mcap(payload, expected):
    assert fa._classified_effective_mcap({"classification_json": payload}) == expected


def test_classified_effective_mcap_tolerates_a_missing_attempt():
    assert fa._classified_effective_mcap(None) is None
    assert fa._classified_effective_mcap({}) is None
