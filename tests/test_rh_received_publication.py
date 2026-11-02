"""Received-signal publication retains both funded EVM chain denominators."""
from types import SimpleNamespace

from sniper.services import memecoin_runner as runner
from sniper.util.receipt_clock import ReceiptClock


class Sink:
    def __init__(self, fail=False):
        self.events, self.gaps, self.fail = [], [], fail

    def record_signal(self, event):
        if self.fail:
            raise RuntimeError("journal unavailable")
        self.events.append(event)

    def record_gap(self, event):
        self.gaps.append(event)


def observation(chain, side="buy"):
    return SimpleNamespace(network_id=chain, normalized_side=side, upstream_event_id="j7:"+chain,
        source_user_id="wallet", mint="mint", raw_payload={"receipt_clock": ReceiptClock("test").capture()})


def test_received_publication_retains_each_chain_before_policy():
    bsc, rh = Sink(), Sink()
    rows = [observation("56"), observation("4663"), observation("4663", "sell"), observation("1")]
    assert runner.publish_received_evm(rows, {"56": bsc, "4663": rh}) == 0
    assert [event["signal_id"] for event in bsc.events] == ["j7:56"]
    assert [event["signal_id"] for event in rh.events] == ["j7:4663"]
    assert rh.events[0]["eligibility"] is None
    assert rh.events[0]["receipt_clock"] == rows[1].raw_payload["receipt_clock"]


def test_lost_rh_publication_keeps_chain_specific_gap():
    bsc, rh = Sink(), Sink(fail=True)
    assert runner.publish_received_evm([observation("56"), observation("4663")],
                                       {"56": bsc, "4663": rh}) == 1
    assert len(bsc.events) == 1 and bsc.gaps == []
    assert rh.gaps == [{"reason": "received_publication_failed", "count": 1}]


def test_legacy_bsc_helper_stays_bsc_only():
    sink = Sink()
    assert runner.publish_received_bsc([observation("56"), observation("4663")], sink) == 0
    assert [event["signal_id"] for event in sink.events] == ["j7:56"]
