from types import SimpleNamespace

from sniper.services.memecoin_runner import publish_received_bsc
from sniper.util.receipt_clock import ReceiptClock


class Publisher:
    def __init__(self):
        self.records = []

    def record_signal(self, record):
        self.records.append(record)


def observation(network="56", side="buy", ev=None):
    return SimpleNamespace(network_id=network, normalized_side=side, event=ev,
        raw_payload={"receipt_clock": ReceiptClock("test").capture(monotonic_ns=100)},
        upstream_event_id="opaque", source_user_id="wallet", mint="mint")


def test_bsc_received_publication_keeps_no_attempt_signal_and_unknown_eligibility():
    publisher = Publisher()
    first = observation()
    publish_received_bsc([first, observation("1"), observation(side="sell")], publisher)
    assert len(publisher.records) == 1
    record = publisher.records[0]
    assert record["signal_id"] == "opaque"
    assert record["receipt_clock"] == first.raw_payload["receipt_clock"]
    assert record["eligibility"] is None


def test_measurement_failure_cannot_break_funded_fanout():
    class Broken:
        def record_signal(self, record):
            raise RuntimeError("observer unavailable")
    publish_received_bsc([observation()], Broken())
