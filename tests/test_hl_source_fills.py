import json

from sniper.hyperlive import source_fills as SF


def _event(**overrides):
    base = {
        "wallet": "0xabc", "tid": "7", "coin": "kPEPE", "side": "B",
        "direction": "Open Long", "px": 1.0, "sz": 2.0,
        "source_event_ts_ms": 1000, "received_ts_ms": 1100,
        "is_snapshot": 0, "is_recovery": 0, "connection_epoch": 1,
    }
    return {**base, **overrides}


def test_subscription_messages_are_bounded_and_wallet_exact():
    messages = SF.subscription_messages(["0xB", "0xa"])
    assert [json.loads(value)["subscription"] for value in messages] == [
        {"type": "userFills", "user": "0xa"},
        {"type": "userFills", "user": "0xb"},
    ]


def test_normalize_user_fills_preserves_source_time_symbol_and_snapshot():
    message = {
        "channel": "userFills",
        "data": {"user": "0xAbC", "isSnapshot": True, "fills": [{
            "tid": 9, "coin": "kPEPE", "side": "B", "dir": "Open Long",
            "px": "0.01", "sz": "20", "time": 1000, "oid": 4, "hash": "0xh",
            "startPosition": "0",
        }]},
    }
    rows = SF.normalize_user_fills_message(message, received_ts_ms=1200, connection_epoch=2)
    assert rows == [{
        "wallet": "0xabc", "tid": "9", "coin": "kPEPE", "side": "B",
        "direction": "Open Long", "px": 0.01, "sz": 20.0, "start_position": 0.0,
        "oid": "4", "tx_hash": "0xh", "source_event_ts_ms": 1000,
        "received_ts_ms": 1200, "is_snapshot": 1, "is_recovery": 0,
        "connection_epoch": 2,
    }]


def test_bounded_buffer_counts_overflow_without_blocking():
    buffer = SF.SourceEventBuffer(maxsize=2)
    assert buffer.put_many([_event(tid="1"), _event(tid="2"), _event(tid="3")]) == 2
    assert buffer.status()["overflow_count"] == 1
    assert [row["tid"] for row in buffer.drain()] == ["1", "2"]
    assert buffer.drain_gaps() == [{
        "start_received_ts_ms": 1100, "end_received_ts_ms": 1100,
        "connection_epoch": 1, "dropped_count": 1, "reason": "queue_overflow",
    }]
    assert buffer.drain_gaps() == []


def test_entry_match_aggregates_multiple_causal_fills():
    result = SF.match_entry(
        [_event(tid="1", sz=1.25), _event(tid="2", sz=0.75,
                                         source_event_ts_ms=1050, received_ts_ms=1150)],
        wallet="0xABC", coin="kPEPE", side="long", prior_size=0.0, current_size=2.0,
        after_ts_ms=900, before_ts_ms=1300,
    )
    assert result["status"] == "matched"
    assert result["tids"] == ["1", "2"]
    assert result["source_event_ts_ms"] == 1050
    assert result["source_received_ts_ms"] == 1150


def test_entry_match_fails_closed_on_snapshot_size_mismatch_or_wrong_case():
    assert SF.match_entry(
        [_event(is_snapshot=1)], wallet="0xabc", coin="kPEPE", side="long",
        prior_size=0, current_size=2, after_ts_ms=0, before_ts_ms=2000,
    )["status"] == "unmatched"

    recovered = SF.match_entry(
        [_event(is_recovery=1)], wallet="0xabc", coin="kPEPE", side="long",
        prior_size=0, current_size=2, after_ts_ms=0, before_ts_ms=2000,
    )
    assert recovered["status"] == "matched_recovery"
    assert recovered["tids"] == ["7"]
    assert SF.match_entry(
        [_event(clock_domain="host_wall")], wallet="0xabc", coin="kPEPE", side="long",
        prior_size=0, current_size=2, after_ts_ms=0, before_ts_ms=2000,
    )["status"] == "ambiguous"


def test_reconnect_recovery_marks_rows_and_advances_cursor():
    calls = []

    def recovery(wallet, start_ms, end_ms):
        calls.append((wallet, start_ms, end_ms))
        return [{"tid": 8, "coin": "kPEPE", "side": "A", "dir": "Close Long",
                 "px": "0.01", "sz": "2", "time": 1500, "startPosition": "2"}]

    collector = SF.SourceFillCollector(
        ["0xABC"], now_ms=lambda: 2000, recovery_fetch=recovery,
        cursors={"0xabc": 1000},
    )
    collector._recover(2)

    assert calls == [("0xabc", 1000, 2000)]
    row = collector.drain()[0]
    assert row["is_recovery"] == 1 and row["connection_epoch"] == 2
    assert collector.cursors["0xabc"] == 1500
    assert collector.status()["recovery_count"] == 1
    assert SF.match_entry(
        [_event(sz=1.0)], wallet="0xabc", coin="kPEPE", side="long",
        prior_size=0, current_size=2, after_ts_ms=0, before_ts_ms=2000,
    )["status"] == "ambiguous"
    assert SF.match_entry(
        [_event()], wallet="0xabc", coin="KPEPE", side="long",
        prior_size=0, current_size=2, after_ts_ms=0, before_ts_ms=2000,
    )["status"] == "unmatched"


def test_quiet_websocket_timeout_sends_app_ping_without_reconnect_or_recovery():
    import websocket

    holder = {}
    message = json.dumps({
        "channel": "userFills",
        "data": {"user": "0xabc", "isSnapshot": False, "fills": [{
            "tid": 9, "coin": "kPEPE", "side": "B", "dir": "Open Long",
            "px": "0.01", "sz": "2", "time": 1500, "startPosition": "0",
        }]},
    })

    class QuietThenFill:
        def __init__(self):
            self.recv_count = 0
            self.ping_count = 0
            self.sent = []

        def send(self, payload):
            self.sent.append(payload)

        def recv(self):
            self.recv_count += 1
            if self.recv_count == 1:
                raise websocket.WebSocketTimeoutException("quiet")
            holder["collector"]._stop.set()
            return message

        def ping(self):
            self.ping_count += 1

        def close(self):
            pass

    fake = QuietThenFill()

    def unexpected_recovery(*_args):
        raise AssertionError("quiet socket must not trigger REST recovery")

    collector = SF.SourceFillCollector(
        ["0xabc"], now_ms=lambda: 2000, recovery_fetch=unexpected_recovery,
        connect=lambda *_args, **_kwargs: fake,
    )
    holder["collector"] = collector
    collector._run()

    assert json.loads(fake.sent[-1]) == {"method": "ping"}
    assert fake.ping_count == 0
    assert collector.status()["connected"] is True
    assert collector.status()["connection_epoch"] == 1
    assert collector.status()["reconnect_count"] == 0
    assert [row["tid"] for row in collector.drain()] == ["9"]
