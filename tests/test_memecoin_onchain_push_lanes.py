"""Push-fed on-chain lanes (2026-09-05): armed-wallet transfers and new V4 pools by subscription.

Spec: docs/superpowers/specs/2026-09-05-memecoin-4663-push-fed-onchain-lanes-design.md.
The watcher saw 6 of 435 armed buys because it polled range `eth_getLogs`; a subscription delivers
at block time and costs no range read. Nothing here opens a socket.
"""
import json

import pytest

from sniper.execution import evm_v4_quote as V
from sniper.services import memecoin_onchain_push as P
from thread_local_time import patch_local

CHAIN = "4663"
ARMED = "0x1111111111111111111111111111111111111111"
OTHER = "0x2222222222222222222222222222222222222222"
MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
FOREIGN = "0x" + "ee" * 20
NATIVE = "0x" + "00" * 20
HOOK = "0xe5e702641ea86f4ae6cc3cdaed2b886f976be044"
COIN = "0x" + "33" * 20


def _w(v):
    if isinstance(v, str):
        return v.lower().removeprefix("0x").rjust(64, "0")
    return format(int(v) % (1 << 256), "064x")


def _transfer_log(to_address):
    return {"topics": [P.TRANSFER_TOPIC, _w(OTHER), "0x" + _w(to_address)],
            "transactionHash": "0x" + "ab" * 32, "blockNumber": "0x10"}


def _key(fee=0, ts=200):
    return {"currency0": NATIVE, "currency1": COIN, "fee": fee,
            "tick_spacing": ts, "hooks": HOOK}


def _init_log(key=None, address=MANAGER):
    key = key or _key()
    from sniper.execution.evm_v4_launchpad import pool_id_for_key
    data = (_w(key["fee"]) + format(key["tick_spacing"] % (1 << 256), "064x")
            + _w(key["hooks"]) + _w(0) + _w(0))
    return {"address": address,
            "topics": [V._INIT_TOPIC, pool_id_for_key(key),
                       "0x" + _w(key["currency0"]), "0x" + _w(key["currency1"])],
            "data": "0x" + data}


class _FakeSocket:
    """Replays a scripted server. `messages` are raw strings; iteration ends the connection."""

    def __init__(self, messages, sent):
        self._messages, self._sent = list(messages), sent

    def send(self, raw):
        self._sent.append(json.loads(raw))

    def __iter__(self):
        return iter(self._messages)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _connect(scripts, sent):
    """One scripted socket per connection attempt, then stop."""
    calls = {"n": 0}

    def connect_fn(_url, **_kw):
        i = calls["n"]
        calls["n"] += 1
        return _FakeSocket(scripts[i] if i < len(scripts) else [], sent)
    connect_fn.calls = calls
    return connect_fn


def _clock(step=0.1, start=0.0):
    """A monotonic clock that never exhausts, so a loop cannot fail on StopIteration."""
    t = {"v": start}

    def now():
        t["v"] += step
        return t["v"]
    return now


def _stop_after(connect_fn, connections):
    """Stop once the loop has opened `connections` sockets.

    Counting CONNECTIONS rather than calls matters: `should_stop` is polled once per delivered
    message too, so a fixed list of False values runs out mid-socket and the loop returns before
    it ever reconnects -- which is exactly the behaviour a reconnect test needs to observe.
    """
    return lambda: connect_fn.calls["n"] >= connections


def _sub_reply(request_id, sub_id):
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "result": sub_id})


def _push(sub_id, payload):
    return json.dumps({"method": "eth_subscription",
                       "params": {"subscription": sub_id, "result": payload}})


# --- 1. transfers -----------------------------------------------------------------------------

def test_a_transfer_to_an_armed_address_is_delivered_and_one_to_another_is_not():
    assert P.transfer_recipient(_transfer_log(ARMED)) == ARMED
    assert P.transfer_recipient(_transfer_log(OTHER)) == OTHER
    assert P.transfer_recipient({"topics": ["0xdead"]}) is None


def test_the_filter_asks_the_server_for_armed_recipients_only():
    """Server-side filtering is the point: the alternative is every transfer on the chain."""
    params = P.armed_transfer_filter([ARMED, OTHER])

    assert params[0] == "logs"
    assert params[1]["topics"][0] == P.TRANSFER_TOPIC
    assert params[1]["topics"][1] is None                      # any sender
    assert set(params[1]["topics"][2]) == {P.address_topic(ARMED), P.address_topic(OTHER)}


def test_an_empty_or_malformed_armed_set_subscribes_to_nothing():
    assert P.armed_transfer_filter([]) is None
    assert P.armed_transfer_filter(["not-an-address", None]) is None


# --- 2. Initialize ----------------------------------------------------------------------------

def _capture_writes(monkeypatch):
    """Intercept at the DB writer, which is where `store_pool_keys_from_logs` actually writes."""
    stored = []
    monkeypatch.setattr("sniper.persistence.db_core.get_connection", lambda *a, **k: object())
    monkeypatch.setattr("sniper.persistence.db_memecoin.put_evm_v4_pool_key",
                        lambda conn, *, chain_id, pool_id, pool_manager, key, now_iso=None:
                            stored.append((pool_id, key)))
    return stored


def test_an_initialize_log_stores_the_key_it_decodes(monkeypatch):
    stored = _capture_writes(monkeypatch)

    assert P.store_initialize(CHAIN, _init_log()) is True
    assert stored and stored[0][1] == _key()


def test_an_initialize_from_a_foreign_pool_manager_stores_nothing(monkeypatch):
    """An Initialize on someone else's PoolManager describes a pool we cannot trade."""
    stored = _capture_writes(monkeypatch)

    assert P.store_initialize(CHAIN, _init_log(address=FOREIGN)) is False
    assert stored == []


def test_the_initialize_filter_binds_our_pool_manager_only():
    params = P.initialize_filter(MANAGER, V._INIT_TOPIC)

    assert params[1]["address"] == MANAGER
    assert params[1]["topics"] == [V._INIT_TOPIC]
    assert P.initialize_filter("nonsense", V._INIT_TOPIC) is None


# --- 3. liveness ------------------------------------------------------------------------------

def test_a_subscription_reply_is_not_delivery():
    """publicnode accepted a subscription and delivered nothing (2026-08-07)."""
    state = P.PushState()
    sent = []
    clock = iter([0.0, 1.0, 2.0, 3.0, 4.0] + [100.0] * 40)
    connect_fn = _connect([[_sub_reply(1, "0xheads")]], sent)
    stops = iter([False, True])

    P.run_push_stream("wss://x", on_transfer=lambda _l: None, on_initialize=lambda _l: None,
                      addresses_fn=lambda: [ARMED], pool_manager=MANAGER,
                      init_topic=V._INIT_TOPIC, state=state, connect_fn=connect_fn,
                      sleep_fn=lambda _s: None, now_fn=lambda: next(clock),
                      should_stop=lambda: next(stops, True))

    assert state.heads == 0 and state.last_head_at is None


def test_a_delivered_head_marks_liveness():
    state = P.PushState()
    sent = []
    clock = _clock(step=0.0)
    connect_fn = _connect([[_sub_reply(1, "0xheads"), _push("0xheads", {"number": "0x1"})]], sent)

    P.run_push_stream("wss://x", on_transfer=lambda _l: None, on_initialize=lambda _l: None,
                      addresses_fn=lambda: [ARMED], pool_manager=MANAGER,
                      init_topic=V._INIT_TOPIC, state=state, connect_fn=connect_fn,
                      sleep_fn=lambda _s: None, now_fn=clock,
                      should_stop=_stop_after(connect_fn, 2))

    assert state.heads == 1 and state.last_head_at is not None


def test_the_backoff_doubles_and_is_capped():
    assert [P.next_backoff(d) for d in (1, 2, 4, 8, 16, 32)] == [2.0, 4.0, 8.0, 16.0, 30.0, 30.0]


# --- 4. re-subscription -----------------------------------------------------------------------

def test_an_armed_set_change_rebuilds_the_transfer_filter():
    """The old subscription cannot match a newly armed wallet, so the socket is rebuilt."""
    sent = []
    sets = iter([[ARMED], [ARMED], [ARMED, OTHER], [ARMED, OTHER], [ARMED, OTHER]])
    current = {"v": [ARMED]}

    def addresses_fn():
        current["v"] = next(sets, current["v"])
        return current["v"]
    script = [_sub_reply(1, "0xheads"), _push("0xheads", {"number": "0x1"})]
    connect_fn = _connect([script, script, []], sent)
    clock = _clock(step=0.01)

    P.run_push_stream("wss://x", on_transfer=lambda _l: None, on_initialize=lambda _l: None,
                      addresses_fn=addresses_fn, pool_manager=MANAGER,
                      init_topic=V._INIT_TOPIC, connect_fn=connect_fn,
                      sleep_fn=lambda _s: None, now_fn=clock,
                      should_stop=_stop_after(connect_fn, 3))

    subs = [m for m in sent if m.get("method") == "eth_subscribe"]
    transfer_subs = [m for m in subs if m["params"][0] == "logs"
                     and m["params"][1].get("topics", [None])[0] == P.TRANSFER_TOPIC]
    assert len(transfer_subs) >= 2                       # rebuilt after the set changed
    assert len(transfer_subs[-1]["params"][1]["topics"][2]) == 2


# --- 5. fallback ------------------------------------------------------------------------------

def test_a_socket_that_never_delivers_a_head_falls_back_to_polling():
    fell_back = []
    connect_fn = _connect([[], []], [])
    clock = iter([0.0, 200.0, 200.0, 400.0, 400.0] + [500.0] * 20)
    stops = iter([False, False, True])

    P.run_push_stream("wss://x", on_transfer=lambda _l: None, on_initialize=lambda _l: None,
                      addresses_fn=lambda: [ARMED], pool_manager=MANAGER,
                      init_topic=V._INIT_TOPIC, connect_fn=connect_fn,
                      sleep_fn=lambda _s: None, now_fn=lambda: next(clock),
                      fallback_after_s=120.0, on_fallback=lambda: fell_back.append(1),
                      should_stop=lambda: next(stops, True))

    assert fell_back, "a dark socket must hand coverage to the poll loop"


def test_a_socket_error_does_not_end_the_lane():
    """Reconnect and keep going: every other seam in this loop already fails that way."""
    def connect_fn(_url, **_kw):
        raise OSError("connection refused")
    stops = iter([False, False, True])
    clock = iter([float(i) for i in range(20)])

    state = P.run_push_stream(
        "wss://x", on_transfer=lambda _l: None, on_initialize=lambda _l: None,
        addresses_fn=lambda: [ARMED], pool_manager=MANAGER, init_topic=V._INIT_TOPIC,
        connect_fn=connect_fn, sleep_fn=lambda _s: None, now_fn=lambda: next(clock),
        should_stop=lambda: next(stops, True))

    assert state.heads == 0                              # returned cleanly rather than raising


# --- 6. no network ----------------------------------------------------------------------------

def test_the_ws_url_is_derived_from_the_http_endpoint():
    assert P.ws_url_for("https://x.example/v2/k") == "wss://x.example/v2/k"
    assert P.ws_url_for("wss://already") == "wss://already"
    assert P.ws_url_for("") is None


def test_delivered_logs_reach_their_handlers():
    seen = {"t": [], "i": []}
    script = [_sub_reply(1, "0xheads"), _sub_reply(2, "0xtr"), _sub_reply(3, "0xin"),
              _push("0xheads", {"number": "0x1"}),
              _push("0xtr", _transfer_log(ARMED)),
              _push("0xin", _init_log())]
    connect_fn = _connect([script, []], [])
    clock = iter([float(i) * 0.1 for i in range(60)])
    stops = iter([False] * 8 + [True])

    state = P.run_push_stream(
        "wss://x", on_transfer=lambda l: seen["t"].append(l),
        on_initialize=lambda l: seen["i"].append(l), addresses_fn=lambda: [ARMED],
        pool_manager=MANAGER, init_topic=V._INIT_TOPIC, connect_fn=connect_fn,
        sleep_fn=lambda _s: None, now_fn=lambda: next(clock),
        should_stop=lambda: next(stops, True))

    assert len(seen["t"]) == 1 and len(seen["i"]) == 1
    assert state.last_log_at.get("transfers") is not None


# --- 7. the detection path keeps the write-scope rule ------------------------------------------

def test_a_pushed_transfer_decodes_a_buy_without_writing(monkeypatch):
    """The watcher's write-lock rule is why this returns a row instead of inserting one."""
    import scripts.research.onchain_evm_wallet_watch as w
    reads = []

    def read_fn(method, params):
        reads.append(method)
        if method == "eth_getTransactionByHash":
            return {"from": ARMED}
        return {"logs": []}
    monkeypatch.setattr(w, "direct_buy_from_receipt",
                        lambda *a, **k: {"mint": COIN, "token_delta": 1.0,
                                         "source_size_usd": 10.0, "source_price": 0.5})

    row = w.push_detection("http://rpc", _transfer_log(ARMED), {ARMED: ("u1", "handle")},
                           decimals_cache={}, quote_usd=4000.0, now="2026-09-05T00:00:00+00:00",
                           read_fn=read_fn)

    assert row["wallet"] == ARMED and row["source_user_id"] == "u1"
    assert sorted(reads) == ["eth_getTransactionByHash", "eth_getTransactionReceipt"]   # read concurrently


def test_a_transfer_whose_sender_is_not_armed_is_not_a_detection(monkeypatch):
    """An armed wallet RECEIVING a token is not the armed wallet buying it."""
    import scripts.research.onchain_evm_wallet_watch as w

    row = w.push_detection("http://rpc", _transfer_log(ARMED), {ARMED: ("u1", "h")},
                           decimals_cache={}, quote_usd=4000.0, now="x",
                           read_fn=lambda m, p: {"from": OTHER})

    assert row is None


# --- 8. dispatch: push when a custody socket resolves, poll otherwise --------------------------

def _dispatch(monkeypatch, *, ws_url, enabled=True):
    """Record which lane `run()` chose, without either lane doing any work."""
    import scripts.research.onchain_evm_wallet_watch as w
    from sniper.settings import memecoin as cfg
    # THROUGH THE SETTINGS FUNCTION, not the frozen module constant: `run()` reads
    # `memecoin_evm_ws_url(chain)` so one watcher per chain resolves its own socket (2026-09-06).
    if ws_url is None:
        monkeypatch.delenv("MEMECOIN_EVM_WS_URL_4663", raising=False)
        monkeypatch.delenv("MEMECOIN_EVM_RPC_POOL_URLS_4663", raising=False)
    else:
        monkeypatch.setenv("MEMECOIN_EVM_WS_URL_4663", ws_url)
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_PUSH_ENABLED", enabled, raising=False)
    chosen = []
    monkeypatch.setattr(w, "run_poll", lambda **kw: chosen.append("poll"))
    monkeypatch.setattr(w, "run_push", lambda url, **kw: chosen.append(("push", url)))
    w.run(duration_s=1.0, period_s=0.0)
    return chosen


def test_a_resolved_socket_enters_push_and_never_polls(monkeypatch):
    assert _dispatch(monkeypatch, ws_url="wss://custody.example/k") == [
        ("push", "wss://custody.example/k")]


def test_no_socket_url_polls_exactly_as_today(monkeypatch):
    """The settings derivation returns None rather than build a socket on the chain-owner host."""
    assert _dispatch(monkeypatch, ws_url=None) == ["poll"]


def test_push_disabled_polls_even_with_a_socket(monkeypatch):
    assert _dispatch(monkeypatch, ws_url="wss://custody.example/k", enabled=False) == ["poll"]


def test_a_dark_socket_polls_and_returns_to_push_when_a_head_arrives():
    """Fallback is a floor on coverage, not a switch: push resumes the moment a head lands."""
    polls, heads = [], []
    script_dark, script_live = [], [_sub_reply(1, "0xh"), _push("0xh", {"number": "0x1"})]
    connect_fn = _connect([script_dark, script_live, []], [])
    clock = _clock(step=60.0)               # each poll of the clock advances a minute

    state = P.run_push_stream(
        "wss://x", on_transfer=lambda _l: None, on_initialize=lambda _l: None,
        addresses_fn=lambda: [ARMED], pool_manager=MANAGER, init_topic=V._INIT_TOPIC,
        connect_fn=connect_fn, sleep_fn=lambda _s: None, now_fn=clock,
        fallback_after_s=120.0, on_fallback=lambda: polls.append(1),
        on_head=lambda: heads.append(1), should_stop=_stop_after(connect_fn, 3))

    assert polls, "a socket that delivered no head must hand coverage to the poll loop"
    assert heads, "and push must resume once a head arrives"
    assert state.heads == 1


def test_the_head_callback_is_what_writes_the_heartbeat():
    """A subscribe reply must never mark the lane healthy; only a delivered head may."""
    beats = []
    connect_fn = _connect([[_sub_reply(1, "0xh")], []], [])

    P.run_push_stream(
        "wss://x", on_transfer=lambda _l: None, on_initialize=lambda _l: None,
        addresses_fn=lambda: [ARMED], pool_manager=MANAGER, init_topic=V._INIT_TOPIC,
        connect_fn=connect_fn, sleep_fn=lambda _s: None, now_fn=_clock(step=0.0),
        on_head=lambda: beats.append(1), should_stop=_stop_after(connect_fn, 2))

    assert beats == []


def test_the_state_reports_a_delivered_message_rate():
    """The provider bills delivered messages, so the rate has to be readable from our side."""
    state = P.PushState()
    state.head(1.0)
    state.log("initialize", 2.0)
    state.log("transfers", 3.0)

    assert state.heads == 1 and state.logs == 2
    assert set(state.last_log_at) == {"initialize", "transfers"}


# --- 9. a refusal is not an outage (2026-09-05, live) ------------------------------------------
# The QuickNode custody key hit its DAILY request limit at ~15:03 UTC. The socket's first reply was
# {"error":{"code":-32003,"message":"daily request limit reached"}} then a 1001 close, and this
# loop read it as "dark" and reconnected about once a second -- 26 fallbacks in 30 seconds, every
# one spending a request against the quota that was already exhausted.

def _error_reply(code=-32003, message="daily request limit reached"):
    return json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": code, "message": message}})


class _ClosedBeforeHead(Exception):
    def __init__(self, code):
        super().__init__("closed")
        self.code = code


def test_the_refusal_schedule_starts_a_minute_out_and_caps_at_ten():
    assert P.next_refusal_backoff(0) == 60.0
    assert P.next_refusal_backoff(60.0) == 120.0
    assert [P.next_refusal_backoff(d) for d in (120.0, 240.0, 480.0, 600.0)] == [
        240.0, 480.0, 600.0, 600.0]
    # and it never borrows the fast transient schedule
    assert P.next_refusal_backoff(1.0) == 60.0


def test_any_error_reply_reads_as_a_refusal_not_only_the_quota_code():
    assert P.error_reply_text(json.loads(_error_reply())) == "-32003: daily request limit reached"
    assert P.error_reply_text(json.loads(_error_reply(-32601, "method not found"))) == (
        "-32601: method not found")
    assert P.error_reply_text({"result": "0xabc"}) is None


def test_a_quota_refusal_waits_a_minute_instead_of_reconnecting(monkeypatch):
    """The bug this fixes: an error reply must not be read as a dark socket."""
    slept = []
    connect_fn = _connect([[_error_reply()], [_error_reply()], []], [])
    state = P.PushState()

    P.run_push_stream(
        "wss://x", on_transfer=lambda _l: None, on_initialize=lambda _l: None,
        addresses_fn=lambda: [ARMED], pool_manager=MANAGER, init_topic=V._INIT_TOPIC,
        state=state, connect_fn=connect_fn, sleep_fn=slept.append, now_fn=_clock(step=0.1),
        should_stop=_stop_after(connect_fn, 3))

    assert slept and min(slept) >= 60.0, slept        # never the 1s transient schedule
    assert slept[:2] == [60.0, 120.0]                 # and it doubles
    assert state.limit_backoffs == 2
    assert "daily request limit reached" in str(state.last_error_reply)


def test_a_close_before_any_head_is_a_refusal_too():
    """1001/1012 before a single head is a provider shedding us, not a dropped connection."""
    slept = []

    def connect_fn(_url, **_kw):
        connect_fn.calls["n"] += 1
        raise _ClosedBeforeHead(1001)
    connect_fn.calls = {"n": 0}
    state = P.PushState()

    P.run_push_stream(
        "wss://x", on_transfer=lambda _l: None, on_initialize=lambda _l: None,
        addresses_fn=lambda: [ARMED], pool_manager=MANAGER, init_topic=V._INIT_TOPIC,
        state=state, connect_fn=connect_fn, sleep_fn=slept.append, now_fn=_clock(step=0.1),
        should_stop=_stop_after(connect_fn, 2))

    assert slept and slept[0] == 60.0
    assert state.limit_backoffs >= 1


def test_an_ordinary_socket_error_keeps_the_fast_reconnect():
    """A real outage still deserves 1s: only a refusal earns the slow schedule."""
    slept = []

    def connect_fn(_url, **_kw):
        connect_fn.calls["n"] += 1
        raise OSError("connection reset")
    connect_fn.calls = {"n": 0}
    state = P.PushState()

    P.run_push_stream(
        "wss://x", on_transfer=lambda _l: None, on_initialize=lambda _l: None,
        addresses_fn=lambda: [ARMED], pool_manager=MANAGER, init_topic=V._INIT_TOPIC,
        state=state, connect_fn=connect_fn, sleep_fn=slept.append, now_fn=_clock(step=0.1),
        should_stop=_stop_after(connect_fn, 3))

    assert slept[0] == 1.0 and state.limit_backoffs == 0


def test_a_refusal_still_hands_coverage_to_the_poll_loop():
    """Waiting ten minutes must not mean ten minutes blind."""
    polls = []
    connect_fn = _connect([[_error_reply()], []], [])

    P.run_push_stream(
        "wss://x", on_transfer=lambda _l: None, on_initialize=lambda _l: None,
        addresses_fn=lambda: [ARMED], pool_manager=MANAGER, init_topic=V._INIT_TOPIC,
        connect_fn=connect_fn, sleep_fn=lambda _s: None, now_fn=_clock(step=0.1),
        on_fallback=lambda: polls.append(1), should_stop=_stop_after(connect_fn, 2))

    assert polls


@pytest.mark.parametrize("poll_duration", [0.0, 1.0, 30.0, 40.0])
@pytest.mark.parametrize("code,message,cooldowns", [
    (-32005, "Too Many Requests", [60, 120, 240, 480, 600, 600]),
    (-32603, "Internal error", [5, 10, 20, 30, 30, 30]),
])
def test_refusal_cooldowns_keep_polling_without_early_reconnect(poll_duration, code, message,
                                                             cooldowns):
    clock = [0.0]
    attempts, polls, sleeps, heads = [], [], [], []
    connect = _connect([[_error_reply(code, message)]] * 7, [])

    def connect_fn(*args, **kwargs):
        attempts.append(clock[0])
        return connect(*args, **kwargs)

    def sleep_fn(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    def poll():
        polls.append(clock[0])
        clock[0] += poll_duration

    state = P.run_push_stream(
        "wss://x", on_transfer=lambda _l: None, on_initialize=lambda _l: None,
        addresses_fn=lambda: [ARMED], pool_manager=MANAGER, init_topic=V._INIT_TOPIC,
        connect_fn=connect_fn, sleep_fn=sleep_fn, now_fn=lambda: clock[0],
        on_fallback=poll, on_head=lambda: heads.append(clock[0]),
        should_stop=lambda: len(attempts) >= 7)

    waits = [b - a for a, b in zip(attempts, attempts[1:])]
    for actual, cooldown in zip(waits, cooldowns):
        assert cooldown <= actual < cooldown + max(poll_duration, 1.0)
    assert max(b - a for a, b in zip(polls, polls[1:])) <= max(30.0, poll_duration)
    assert not sleeps or max(sleeps) <= 30.0
    assert all(seconds > 0 for seconds in sleeps)
    assert heads == [] and state.heads == 0 and state.last_head_at is None


def test_stop_during_refusal_poll_returns_without_sleeping_or_reconnecting():
    stopped, slept = [], []
    connect = _connect([[_error_reply()]], [])

    P.run_push_stream(
        "wss://x", on_transfer=lambda _l: None, on_initialize=lambda _l: None,
        addresses_fn=lambda: [ARMED], pool_manager=MANAGER, init_topic=V._INIT_TOPIC,
        connect_fn=connect, sleep_fn=slept.append, now_fn=lambda: 0.0,
        on_fallback=lambda: stopped.append(True), should_stop=lambda: bool(stopped))

    assert connect.calls["n"] == 1
    assert slept == []


def test_stop_during_refusal_sleep_prevents_another_poll_or_connect():
    clock, stopped, polls = [0.0], [], []
    connect = _connect([[_error_reply()]], [])

    def sleep_fn(seconds):
        assert 0 < seconds <= 30.0
        clock[0] += seconds
        stopped.append(True)

    P.run_push_stream(
        "wss://x", on_transfer=lambda _l: None, on_initialize=lambda _l: None,
        addresses_fn=lambda: [ARMED], pool_manager=MANAGER, init_topic=V._INIT_TOPIC,
        connect_fn=connect, sleep_fn=sleep_fn, now_fn=lambda: clock[0],
        on_fallback=lambda: polls.append(clock[0]), should_stop=lambda: bool(stopped))

    assert connect.calls["n"] == 1
    assert polls == [0.0]


def test_refusal_poll_cooldown_uses_monotonic_time(monkeypatch):
    clock = [0.0]
    polls = []
    connect = _connect([[_error_reply()], []], [])
    patch_local(monkeypatch, "time", lambda: 10000.0 - clock[0])
    patch_local(monkeypatch, "monotonic", lambda: clock[0])

    def sleep_fn(seconds):
        clock[0] += seconds

    P.run_push_stream(
        "wss://x", on_transfer=lambda _l: None, on_initialize=lambda _l: None,
        addresses_fn=lambda: [ARMED], pool_manager=MANAGER, init_topic=V._INIT_TOPIC,
        connect_fn=connect, sleep_fn=sleep_fn, on_fallback=lambda: polls.append(clock[0]),
        should_stop=lambda: connect.calls["n"] >= 2)

    assert polls == [0.0, 30.0]
    assert clock[0] == 60.0
