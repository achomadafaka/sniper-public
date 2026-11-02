"""J7Tracker's wallet-tracker relay as the funded signal source.

fomo refused our third account on 2026-09-09. J7 relays the tracked fomo roster over its own
Socket.IO service; the payload below is a real relay recorded that day by scripts/j7_socket_tap.py
(0xdedrater buying MOO on chain 4663, 243 ms after fomo stamped it). These tests pin the mapping
into the fomo /feed item shape, so the copy path downstream of the fetch is the one that has run
funded since July, and pin the seam: with the source set to j7, fomo is never called.
"""
import json

import pytest

from sniper.feeds import j7_feed as J
from sniper.feeds.fomo_feed import normalize_feed_item, normalize_feed_observation

RELAY = json.loads(r'''{"type": "FOMO_EVENT", "kind": "trade", "received_at": 1788967645586,
 "data": {"id": "swap:raybot:0x6efd7b85:0xa94d2eb3:0xc103ac00a25173870c909223c5676d50bf5728b2",
  "tradeId": null, "side": "buy", "userId": "8b335839-be76-5946-8d11-885f628774df",
  "userHandle": "0xdedrater", "source": "trading_activity_ws", "displayName": "Dedrater",
  "usdAmount": 496.745239, "price": 0.001014831164917182, "marketCap": 968114.9482440377,
  "timestamp": "2026-09-09T15:27:25.343Z",
  "token": {"address": "0xc103ac00a25173870c909223c5676d50bf5728b2", "networkId": 4663,
            "network": "robinhood", "symbol": "MOO"}}}''')


def test_a_relayed_buy_becomes_the_copy_event_the_runner_already_knows():
    item = J.fomo_event_to_feed_item(RELAY)
    ev = normalize_feed_item(item, "2026-09-09T15:27:25.647000+00:00", 1788967645.586)
    assert ev is not None
    assert ev.side == "buy"
    assert ev.source_user_id == "8b335839-be76-5946-8d11-885f628774df"   # == memecoin_wallets.source_user_id
    assert ev.source_handle == "0xdedrater"
    assert ev.mint == "0xc103ac00a25173870c909223c5676d50bf5728b2"
    assert ev.network_id == "4663", "the chain id must route the real leg, as a string"
    assert ev.ticker == "MOO"
    assert ev.their_size_usd == pytest.approx(496.745239)
    assert ev.their_price == pytest.approx(0.001014831164917182)
    assert ev.their_market_cap == pytest.approx(968114.948, rel=1e-6)
    assert ev.fomo_event_id.startswith("swap:raybot:"), "J7's swap id is the dedup key"
    assert ev.created_at == "2026-09-09T15:27:25.343Z"
    assert 0.0 < ev.lag_s < 1.0, "fomo -> J7 server lag was 243 ms on the recorded relay"
    assert ev.is_first_buy is False, "not relayed: must not be guessed True"
    # The funded gate refuses 'global_feed' outright; the first two live relays died on it.
    assert ev.signal_source == "follow_activity"
    assert ev.delivery_lane == "j7_wallets"


def test_a_fomo_feed_item_without_lane_keys_keeps_the_global_feed_default():
    raw = {"id": "x1", "type": "large_buy", "userId": "u", "tokenAddress": "m", "createdAt": "2026-09-09T00:00:00Z",
           "body": {"userId": "u", "userHandle": "h", "ticker": "T", "price": 1, "marketCap": 1, "currentSizeUsd": 1}}
    ev = normalize_feed_item(raw, "2026-09-09T00:00:01+00:00", None)
    assert ev.signal_source == "global_feed" and ev.delivery_lane == "global_feed"


def test_the_observation_row_carries_the_same_identity():
    obs = normalize_feed_observation(J.fomo_event_to_feed_item(RELAY),
                                     "2026-09-09T15:27:25.647000+00:00", 1788967645.586)
    assert obs.normalized_side == "buy" and obs.network_id == "4663"
    assert obs.upstream_event_id == RELAY["data"]["id"]
    assert obs.event is not None


def test_a_sell_is_a_sell():
    relay = json.loads(json.dumps(RELAY)); relay["data"]["side"] = "sell"
    ev = normalize_feed_item(J.fomo_event_to_feed_item(relay), "2026-09-09T15:27:25+00:00", None)
    assert ev.side == "sell"


def test_solana_relays_keep_the_canonical_network_label():
    relay = json.loads(json.dumps(RELAY)); relay["data"]["token"]["networkId"] = 1399811149
    ev = normalize_feed_item(J.fomo_event_to_feed_item(relay), "2026-09-09T15:27:25+00:00", None)
    assert ev.network_id == "solana"


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(kind="thesis"),
    lambda r: r["data"].update(side="transfer"),
    lambda r: r["data"].__setitem__("token", {}),
    lambda r: r["data"].__setitem__("userId", ""),
    lambda r: r.clear(),
])
def test_anything_that_is_not_a_trade_with_an_owner_and_a_mint_is_dropped(mutate):
    relay = json.loads(json.dumps(RELAY)); mutate(relay)
    assert J.fomo_event_to_feed_item(relay) is None


def test_with_the_j7_source_the_runner_never_calls_fomo(monkeypatch):
    """The seam. run_once with MEMECOIN_SIGNAL_SOURCE=j7 must drain the feed object and must not
    touch fomo_get. Everything past the fetch is driven with the real normalizers on an
    in-memory ledger; the copy fan-out is allowed to do nothing with an unknown wallet."""
    import sqlite3
    from sniper.services import memecoin_runner as R
    from sniper.persistence import db_memecoin as m

    class Feed:
        drained = 0
        def drain(self):
            Feed.drained += 1
            return [J.fomo_event_to_feed_item(RELAY)], 1788967645.586
        def status(self):
            return {}

    monkeypatch.setattr("sniper.feeds.j7_feed.j7_feed_or_none", lambda log_fn=None: Feed())

    def boom(*a, **k):
        raise AssertionError("fomo_get must not be called when the source is j7")
    monkeypatch.setattr(R, "fomo_get", boom)

    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    res = R.run_once(conn, auth=None, size_usd=10.0, http=None,
                     now_fn=lambda: "2026-09-09T15:27:26+00:00",
                     stats_fn=lambda *a, **k: {})
    assert Feed.drained == 1
    assert res.get("feed_status") == 200
    assert res.get("skipped_feed_reason") is None


def test_the_loop_has_no_fomo_call_left_on_the_j7_path():
    """Source scan on BEHAVIOUR, not spelling: every fomo network call in the loop must sit under
    a branch the J7 flag disables. The three call families are named by their functions."""
    import pathlib
    src = pathlib.Path("scripts/memecoin_runner_loop.py").read_text()
    assert "if not _J7_SOURCE:          # J7 source: fomo is never called\n" \
           "                    report_follow_sync(" in src, "follow_sync must be fomo-only"
    assert 'if _J7_SOURCE:\n        _p("[memecoin] follow-activity lanes' in src, \
        "the fomo REST/WS lanes must not start under J7"
    assert "_J7_SOURCE or _GLOBAL_FEED_PERIOD_S <= 0" in src, "J7 drains every cycle"


def test_the_clock_is_learned_at_receipt_and_never_from_a_stale_drain(monkeypatch):
    """21 s behind at 20:54Z on 2026-09-09: the drain handed record_real_utc the LAST relay's
    stamp on every cycle. The feed learns at receipt; the runner learns nothing from a J7 drain."""
    learned = []
    import sniper.util.clock as C
    monkeypatch.setattr(C, "record_real_utc", lambda epoch, path=None: learned.append(epoch))
    feed = J.J7WalletsFeed("tok")
    # drive the handler the socket would call, without a socket
    handlers = {}

    class _Sio:
        def __init__(self, *a, **k): pass
        def event(self, fn): return fn
        def on(self, name):
            def reg(fn): handlers[name] = fn; return fn
            return reg
        def connect(self, *a, **k): raise RuntimeError("no network in tests")
        def wait(self): pass
    import types, sys
    fake = types.SimpleNamespace(Client=_Sio, exceptions=types.SimpleNamespace(ConnectionError=RuntimeError))
    monkeypatch.setitem(sys.modules, "socketio", fake)
    monkeypatch.setattr("threading.Thread.start", lambda self: None)
    feed.start()
    handlers["fomo_event"](RELAY)
    assert learned == [1788967645.586], "learned once, at receipt, in seconds"
    items, epoch = feed.drain()
    assert len(items) == 1 and epoch == 1788967645.586
    assert feed.drain() == ([], 1788967645.586), "an empty drain still reports the stale epoch -- the runner must not teach with it"


def test_run_once_does_not_teach_the_clock_from_a_j7_drain(monkeypatch):
    import sqlite3
    from sniper.services import memecoin_runner as R
    from sniper.persistence import db_memecoin as m
    import sniper.util.clock as C
    taught = []
    monkeypatch.setattr(C, "record_real_utc", lambda epoch, path=None: taught.append(epoch))

    class Feed:
        def drain(self): return [], 1788967645.586      # stale epoch, nothing new
        def status(self): return {}
    monkeypatch.setattr("sniper.feeds.j7_feed.j7_feed_or_none", lambda log_fn=None: Feed())
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    R.run_once(conn, auth=None, size_usd=10.0, http=None, now_fn=lambda: "2026-09-09T20:54:00+00:00",
               stats_fn=lambda *a, **k: {})
    assert taught == []


def test_a_relay_wakes_the_runner_instead_of_waiting_out_the_cycle(monkeypatch):
    """2026-09-14: J7 delivers in ~0.3 s but relays sat 0-5 s in the queue for the next 5 s cycle
    (3.0 s median, 12.7 s p99). A queued relay must wake the loop; an idle loop keeps its cadence."""
    import time, types, sys
    feed = J.J7WalletsFeed("tok")
    handlers = {}

    class _Sio:
        def __init__(self, *a, **k): pass
        def event(self, fn): return fn
        def on(self, name):
            def reg(fn): handlers[name] = fn; return fn
            return reg
        def connect(self, *a, **k): raise RuntimeError("no network in tests")
        def wait(self): pass
    monkeypatch.setitem(sys.modules, "socketio", types.SimpleNamespace(
        Client=_Sio, exceptions=types.SimpleNamespace(ConnectionError=RuntimeError)))
    monkeypatch.setattr("threading.Thread.start", lambda self: None)
    monkeypatch.setattr("sniper.util.clock.record_real_utc", lambda *a, **k: None)
    feed.start()
    t0 = time.monotonic()
    assert feed.wait(0.05) is False and time.monotonic() - t0 >= 0.04    # idle: full wait
    handlers["fomo_event"](RELAY)                  # lands between the last drain and the wait
    t0 = time.monotonic()
    assert feed.wait(5.0) is True and time.monotonic() - t0 < 0.5         # woken, not 5 s later
    assert len(feed.drain()[0]) == 1
    assert feed.wait(0.01) is False                                        # the wake was consumed


def test_the_loop_sleeps_on_the_j7_wake_not_a_fixed_sleep():
    import pathlib
    src = pathlib.Path("scripts/memecoin_runner_loop.py").read_text()
    # Since 10-12 the rest goes through wait_for_idle_heartbeat (Shyft funded-only idle
    # heartbeat); the J7 wake is its wait_fn, so a relay still ends the rest at once.
    assert "wait_for_idle_heartbeat(_rest," in src
    assert "wait_fn=_j7_feed.wait if _j7_feed is not None" in src


def test_two_j7_accounts_share_one_wake_and_merge_disjoint_events(monkeypatch):
    monkeypatch.setattr("sniper.util.clock.record_real_utc", lambda *a, **k: None)
    feed = J.J7AccountsFeed(["first", "second"])
    first, second = feed._feeds
    first._set(status="connected", tracked_count=50, pump_tracked_count=6)
    second._set(status="connected", tracked_count=10, pump_tracked_count=0)
    first.observe(RELAY)
    other = json.loads(json.dumps(RELAY))
    other["data"]["id"] = "second-swap"
    other["data"]["userId"] = "second-user"
    second.observe(other)
    assert feed.wait(0.01) is True
    items, _ = feed.drain()
    assert [item["id"] for item in items] == [RELAY["data"]["id"], "second-swap"]
    status = feed.status()
    assert status["status"] == "connected"
    assert status["tracked_count"] == 60
    assert status["pump_tracked_count"] == 6
    second._set(status="auth_error")
    assert feed.status()["status"] == "degraded"


def test_j7_source_uses_both_saved_tokens_without_exposing_them(tmp_path, monkeypatch):
    from sniper.settings import memecoin
    monkeypatch.setattr(memecoin, "MEMECOIN_SIGNAL_SOURCE", "j7")
    path = tmp_path / ".j7.env"
    path.write_text("J7_TOKEN=first-secret\nJ7_TOKEN_2=second-secret\n")
    monkeypatch.setenv("J7_ENV_FILE", str(path))
    monkeypatch.setattr(J.J7WalletsFeed, "start", lambda self: self)
    monkeypatch.setattr(J, "_SINGLETON", None)
    try:
        feed = J.j7_feed_or_none()
        assert len(feed._feeds) == 2
        assert "secret" not in json.dumps(feed.status())
    finally:
        monkeypatch.setattr(J, "_SINGLETON", None)


@pytest.fixture
def j7_connection(monkeypatch):
    """Run the registered Socket.IO callbacks and one real reconnect-loop iteration."""
    import sys
    import types

    handlers, actions, logs = {}, [], []
    token = "private-j7-session-token"

    class StopLoop(BaseException):
        pass

    class Socket:
        sid = "session-id"

        def __init__(self, **kwargs):
            pass

        def event(self, fn):
            handlers[fn.__name__] = fn
            return fn

        def on(self, name):
            def register(fn):
                handlers[name] = fn
                return fn
            return register

        def connect(self, *args, **kwargs):
            actions.pop(0)()

        def wait(self):
            pass

    def end_iteration(_seconds):
        raise StopLoop

    monkeypatch.setitem(sys.modules, "socketio", types.SimpleNamespace(Client=Socket))
    monkeypatch.setattr(J.threading.Thread, "start", lambda self: None)
    monkeypatch.setattr(J.time, "sleep", end_iteration)
    feed = J.J7WalletsFeed(token, log_fn=logs.append).start()

    def run(action):
        actions.append(action)
        with pytest.raises(StopLoop):
            feed._thread._target()

    return feed, handlers, logs, token, run


def test_j7_preserves_invalid_token_instead_of_the_following_namespace_error(j7_connection):
    feed, handlers, logs, token, run = j7_connection

    def rejected():
        handlers["connect_error"]({"message": "Invalid token", "echo": token})
        raise RuntimeError("One or more namespaces failed to connect: / " + token)

    run(rejected)
    status = feed.status()
    assert status["status"] == "auth_error"
    assert "Invalid token" in status["last_error"]
    assert "namespaces" not in status["last_error"]
    assert token not in json.dumps(status) + "\n".join(logs)

    def next_attempt_unavailable():
        raise RuntimeError("transport unavailable")

    run(next_attempt_unavailable)
    assert feed.status()["status"] == "connect_failed"
    assert feed.status()["last_error"] == "transport unavailable"


def test_j7_retains_generic_failure_and_redacts_its_logs_without_auth_rejection(j7_connection):
    feed, handlers, logs, token, run = j7_connection

    def unavailable():
        handlers["connect_error"]({"message": "Service unavailable", "echo": token})
        raise RuntimeError("transport unavailable " + token)

    run(unavailable)
    status = feed.status()
    assert status["status"] == "connect_failed"
    assert status["last_error"] == "transport unavailable <token>"
    assert token not in json.dumps(status) + "\n".join(logs)
    assert "Service unavailable" in "\n".join(logs)


def test_j7_explicit_auth_error_survives_outer_failure_then_clears_on_connect(j7_connection):
    feed, handlers, logs, token, run = j7_connection

    def rejected():
        handlers["auth_error"]({"message": "Session refused", "echo": token})
        raise RuntimeError("One or more namespaces failed to connect: /")

    run(rejected)
    assert feed.status()["status"] == "auth_error"
    assert "Session refused" in feed.status()["last_error"]
    assert token not in json.dumps(feed.status()) + "\n".join(logs)

    run(handlers["connect"])
    status = feed.status()
    assert status["status"] == "connected" and status["connected_at"]
    assert status["last_error"] is None and status["last_error_at"] is None

    def transport_failed():
        raise RuntimeError("transport unavailable")

    run(transport_failed)
    assert feed.status()["status"] == "connect_failed"
    assert feed.status()["last_error"] == "transport unavailable"
