"""Fomo trading-activity WebSocket feed into the existing funded follow path."""
import json
import queue
import sqlite3
import threading
import time

from scripts import memecoin_runner_loop
from sniper.feeds import fomo_account
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_follow_activity as follow


NOW = "2026-08-30T22:50:31Z"
PAYLOAD = {
    "createdAt": "2026-08-30T22:50:30.199Z",
    "fdv": 97_900.0,
    "id": "activity-id",
    "marketCap": 97_900.0,
    "networkId": 4663,
    "price": 0.004,
    "ticker": "FAST",
    "tokenAddress": "0xf219757afc29389dac63858f6e4bcdf7f1d07bfd",
    "tradeId": "swap-id",
    "type": "swap_buy",
    "usdAmount": 2_000.0,
    "userHandle": "fmpumpguy",
    "userId": "u1",
}


class _Auth:
    access = "jwt"


class _Socket:
    def __init__(self):
        self.sent = []
        self.messages = iter([
            {"type": "challenge"},
            {"type": "challengeAccepted"},
            {"type": "subscribed", "topicType": "trading_activity", "topicId": "me"},
            {"type": "data", "topicType": "trading_activity", "topicId": "me",
             "payload": PAYLOAD},
        ])

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def recv(self, timeout=None):
        return json.dumps(next(self.messages))

    def send(self, payload):
        self.sent.append(json.loads(payload))


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    m.add_wallet(conn, "u1", "fmpumpguy", source="follow")
    conn.execute(
        "UPDATE memecoin_wallets SET real_approved=1, real_chains='4663' "
        "WHERE source_user_id='u1'"
    )
    conn.commit()
    return conn


def test_stream_authenticates_subscribes_and_yields_the_payload():
    stream_fn = getattr(fomo_account, "stream_trading_activity", None)
    assert callable(stream_fn), "Fomo WebSocket stream is missing"
    socket = _Socket()

    subscribed = []
    stream = stream_fn(
        _Auth(), "me", connect_fn=lambda *_a, **_k: socket,
        on_subscribed=lambda: subscribed.append(True),
    )
    assert next(stream) == PAYLOAD
    stream.close()

    assert socket.sent == [
        {"type": "challengeResponse", "jwt": "jwt"},
        {"type": "subscribe", "topicType": "trading_activity", "topicId": "me"},
    ]
    assert subscribed == [True]


def test_websocket_handoff_does_not_wait_for_a_slow_dispatch():
    work = [queue.Queue(maxsize=2)]
    started = threading.Event()
    release = threading.Event()

    def slow_dispatch():
        work[0].get(timeout=1.0)
        started.set()
        release.wait(1.0)
        work[0].task_done()

    threading.Thread(target=slow_dispatch, daemon=True).start()
    memecoin_runner_loop.enqueue_fomo_activity(work, PAYLOAD)
    assert started.wait(1.0)

    then = time.monotonic()
    memecoin_runner_loop.enqueue_fomo_activity(
        work, {**PAYLOAD, "tradeId": "second-swap"},
    )
    elapsed = time.monotonic() - then
    release.set()

    assert elapsed < 0.1


def test_websocket_handoff_keeps_the_socket_receipt_time():
    work = [queue.Queue()]

    memecoin_runner_loop.enqueue_fomo_activity(work, PAYLOAD, received_at=NOW)

    assert work[0].get_nowait() == (PAYLOAD, NOW)


def test_websocket_handoff_keeps_one_wallet_on_one_worker():
    queues = [queue.Queue(), queue.Queue()]
    first = {**PAYLOAD, "tradeId": "first-swap"}
    second = {**PAYLOAD, "tradeId": "second-swap"}

    first_index = memecoin_runner_loop.enqueue_fomo_activity(queues, first)
    second_index = memecoin_runner_loop.enqueue_fomo_activity(queues, second)

    assert first_index == second_index


def test_websocket_receipt_excludes_unknown_and_secret_fields():
    receipt = fomo_account.activity_receipt_observation(
        {**PAYLOAD, "jwt": "secret", "unknown": {"secret": "value"}}, NOW,
    )

    assert receipt["upstream_event_id"] == "ws:swap-id"
    assert receipt["raw_type"] == "ws_receipt"
    assert receipt["normalized_side"] is None
    assert receipt["source_user_id"] == "u1"
    assert receipt["raw_payload"]["parser_status"] == "accepted"
    assert "jwt" not in receipt["raw_payload"]["payload"]
    assert "unknown" not in receipt["raw_payload"]["payload"]

    conn = _conn()
    assert m.insert_feed_observation_v2(conn, receipt, True, True) is True
    saved = json.loads(conn.execute(
        "SELECT raw_json FROM memecoin_feed_observations_v2"
    ).fetchone()[0])
    assert saved == receipt["raw_payload"]


def test_activity_buy_uses_the_existing_follow_path_and_trade_id(monkeypatch):
    dispatch = getattr(follow, "dispatch_trading_activity", None)
    assert callable(dispatch), "Fomo WebSocket dispatch is missing"
    monkeypatch.setattr(
        "sniper.settings.memecoin.MEMECOIN_FOLLOW_ACTIVITY_REAL_ENABLED", True,
    )
    calls = []
    holder_calls = []
    monkeypatch.setattr(
        "sniper.services.memecoin_holder_evidence.reconcile_follow_poll",
        lambda *_args: holder_calls.append(1),
    )
    conn = _conn()

    kwargs = {
        "real_hook": lambda _conn, event: calls.append(event) or {"status": "filled"},
        "now_fn": lambda: NOW,
        "fill_fn": lambda *_a, **_k: (0.004, "route"),
    }
    assert dispatch(conn, PAYLOAD, 100.0, **kwargs)["status"] == "dispatched"
    assert dispatch(conn, PAYLOAD, 100.0, **kwargs)["status"] == "duplicate"

    assert len(calls) == 1
    event = calls[0]
    assert event.fomo_event_id == "act-swap-id"
    assert event.network_id == "4663"
    assert event.ticker == "FAST"
    assert event.their_market_cap == 97_900.0
    assert event.their_price == 0.004
    assert event.delivery_lane == "websocket"
    row = conn.execute(
        "SELECT delivery_lane FROM memecoin_events WHERE fomo_event_id='act-swap-id'"
    ).fetchone()
    assert row["delivery_lane"] == "websocket"
    assert holder_calls == []


def test_activity_sell_reaches_the_existing_real_hook_once():
    dispatch = getattr(follow, "dispatch_trading_activity", None)
    assert callable(dispatch), "Fomo WebSocket dispatch is missing"
    calls = []
    payload = {**PAYLOAD, "type": "swap_sell", "tradeId": "sell-id"}
    conn = _conn()

    kwargs = {
        "real_hook": lambda _conn, event: calls.append(event) or {"status": "closed"},
        "now_fn": lambda: NOW,
        "fill_fn": lambda *_a, **_k: (0.004, "route"),
    }
    assert dispatch(conn, payload, 100.0, **kwargs)["status"] == "dispatched"
    assert dispatch(conn, payload, 100.0, **kwargs)["status"] == "duplicate"

    assert len(calls) == 1
    assert calls[0].side == "sell"
    assert calls[0].fomo_event_id == "act-sell-id"


def test_activity_with_no_price_is_ignored_before_the_real_hook():
    dispatch = getattr(follow, "dispatch_trading_activity", None)
    assert callable(dispatch), "Fomo WebSocket dispatch is missing"
    calls = []

    result = dispatch(
        _conn(),
        {**PAYLOAD, "price": 0},
        100.0,
        real_hook=lambda *_args: calls.append(1),
        now_fn=lambda: NOW,
    )

    assert result["status"] == "ignored"
    assert calls == []
