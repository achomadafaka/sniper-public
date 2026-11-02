import json

from sniper.services.memecoin_onchain_lane import OnchainEv, event_from_retry_payload, retry_payload
from sniper.util.receipt_clock import ReceiptClock


def event(clock):
    return OnchainEv({"mint": "0x" + "11" * 20, "source_user_id": "wallet",
                      "handle": "handle", "network_id": "56", "source_size_usd": 100,
                      "source_price": .01, "signature": "0x" + "22" * 32,
                      "detected_at": "2026-10-11T15:00:00+00:00",
                      "source_valuation_json": json.dumps({"receipt_clock": clock,
                          "receipt_stages": [{"name": "decode", "monotonic_ns": 200}]})})


def test_db_fallback_and_retry_keep_original_clock_without_changing_source_age():
    clock = ReceiptClock("test").capture(monotonic_ns=100)
    ev = event(clock)
    assert ev.receipt_clock == clock
    rebuilt = event_from_retry_payload(retry_payload(ev))
    assert rebuilt.receipt_clock == clock
    assert rebuilt.receipt_stages == ev.receipt_stages
    assert rebuilt.created_at == ev.created_at


def test_malformed_clock_is_unknown_and_does_not_refuse_existing_event():
    ev = event({"monotonic_ns": 100})
    assert ev.receipt_clock is None
    assert ev.their_size_usd == 100
