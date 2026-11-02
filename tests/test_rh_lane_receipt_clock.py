"""Native and shared RH handoffs must retain the first signal receipt."""
import json

import pytest

from sniper.services import memecoin_rh_fast_lane as lane
from sniper.util.receipt_clock import ReceiptClock
from test_memecoin_rh_fast_lane import INPUTS, OUT, REC


@pytest.mark.parametrize("shared", [False, True])
def test_native_and_shared_handoff_preserve_original_receipt(shared):
    clock = ReceiptClock("rh-test").capture(monotonic_ns=123456789)
    rec = dict(REC, receipt_clock=clock)
    out = dict(OUT)
    if shared:
        out.update(route_kind="pons_shared", post_ratio=1.0)

    payload = lane.lane_payload(rec, out, sent_epoch=2000.0, inputs=INPUTS)
    event = lane.LaneEvent(json.loads(json.dumps(payload)), decimals=18, supply=10**27)

    assert payload.get("receipt_clock") == clock
    assert event.receipt_clock == clock
    assert event.receipt_clock["monotonic_ns"] == 123456789
    with pytest.raises(TypeError, match="immutable"):
        event.receipt_clock["monotonic_ns"] = 999999999


@pytest.mark.parametrize("value", [None, {}, {"monotonic_ns": "123"}])
def test_missing_or_malformed_clock_stays_unknown(value):
    payload = lane.lane_payload(dict(REC, receipt_clock=value), OUT,
                                sent_epoch=2000.0, inputs=INPUTS)
    event = lane.LaneEvent(payload, decimals=18, supply=10**27)
    assert event.receipt_clock is None
