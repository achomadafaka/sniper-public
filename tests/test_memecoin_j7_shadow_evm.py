"""J7 shadow on EVM buys, per chain (2026-09-29, spec 2026-09-29-memecoin-j7-shadow-evm-buys-design.md).

MEMECOIN_J7_SHADOW_EVM_CHAINS names the EVM chains where a J7 BUY by a covered wallet records its
decision and stops before the leg. Covered means EVERY verified address of the wallet on that chain
is in the transfer filter the EVM watcher's socket ACKED, published in its push beat. The watcher
must be healthy: a fresh push-mode beat, coverage known and not low, no suspect silence. Anything
unknown keeps the buy live, today's behaviour. Sells are never shadowed.
"""
import importlib.util
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import axiom_address_verify as axv
from sniper.services import memecoin_onchain_push as P
from sniper.services import memecoin_real_runner as R
from sniper.settings import memecoin as cfg

ROOT = Path(__file__).resolve().parents[1]
UID = "u1"
ADDR = "0x" + "aa" * 20
ADDR2 = "0x" + "bb" * 20
_REAL_HEALTH = R.evm_watcher_healthy


def _beat(ts=None, **over):
    beat = {"ts": (ts or datetime.now(timezone.utc)).isoformat(), "wallets": 1, "mode": "push",
            "coverage": 0.99, "coverage_low_since": None, "log_silence": "ok",
            "subscriptions": {"transfers": {"silence_s": 50.0, "verdict": "no_reference"},
                              "initialize": {"silence_s": 1.0, "verdict": "ok"}},
            "transfer_addresses": [ADDR]}
    beat.update(over)
    return beat


def _write(tmp_path, beat):
    p = tmp_path / "onchain_evm_watch.heartbeat"
    p.write_text(beat if isinstance(beat, str) else json.dumps(beat))
    return p


def _j7(uid=UID, *, network="4663", side="buy", source="follow_activity"):
    return SimpleNamespace(side=side, signal_source=source, network_id=network,
                           mint="0x" + "cc" * 20 if network != "solana" else "MintA",
                           source_user_id=uid, delivery_lane="j7_wallets", fomo_event_id="e1")


@pytest.fixture
def armed(monkeypatch, tmp_path):
    """4663 in the knob; u1 covered by ADDR; the real health gate reads a tmp beat."""
    monkeypatch.setattr(cfg, "MEMECOIN_J7_SHADOW_EVM_CHAINS", "4663", raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_J7_SHADOW_EVM_MAX_AGE_S", 15.0, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_J7_SOLANA_MODE", "live", raising=False)
    owners = {ADDR: (UID, "alice")}
    monkeypatch.setattr(axv, "evm_watched_owners", lambda conn, chain: dict(owners) if chain == "4663" else {})
    beat_path = _write(tmp_path, _beat())
    monkeypatch.setattr(R, "_evm_beat_path", lambda chain: beat_path)
    R._EVM_WATCHER_HEALTH.clear()
    yield owners, beat_path
    R._EVM_WATCHER_HEALTH.clear()


# ---------------------------------------------------------------------------- the tagger

def test_a_covered_4663_buy_with_a_healthy_watcher_is_tagged(armed):
    ev = _j7()
    tagged = R.j7_shadow_event(None, ev)
    assert tagged is not None and tagged.delivery_lane == "j7_shadow"
    assert ev.delivery_lane == "j7_wallets", "the original event is untouched"


def test_an_empty_knob_changes_nothing_and_reads_nothing(armed, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_J7_SHADOW_EVM_CHAINS", "")
    monkeypatch.setattr(axv, "evm_watched_owners", lambda *a: pytest.fail("read the definition"))
    monkeypatch.setattr(R, "evm_watcher_healthy", lambda *a, **k: pytest.fail("read the beat"))
    assert R.j7_shadow_event(None, _j7()) is None


@pytest.mark.parametrize("why, ev", [
    ("a sell is the only EVM sell signal", _j7(side="sell")),
    ("not J7", _j7(source="onchain_watch")),
    ("a chain outside the knob", _j7(network="56")),
    ("an unknown chain", _j7(network="evm-unknown")),
    ("an uncovered wallet", _j7("uid-other")),
])
def test_everything_else_stays_live(armed, why, ev):
    assert R.j7_shadow_event(None, ev) is None, why


def test_a_covered_healthy_wallet_on_a_chain_outside_the_knob_stays_live(armed, monkeypatch):
    """Covered and healthy everywhere; only the knob decides: 56 is not listed."""
    monkeypatch.setattr(axv, "evm_watched_owners", lambda conn, chain: {ADDR: (UID, "alice")})
    assert R.j7_shadow_event(None, _j7(network="4663")) is not None
    assert R.j7_shadow_event(None, _j7(network="56")) is None


def test_a_wallet_is_covered_only_when_every_address_is_acked(armed):
    owners, beat_path = armed
    owners[ADDR2] = (UID, "alice")                     # a second verified address, not in the filter
    assert R.j7_shadow_event(None, _j7()) is None
    beat_path.write_text(json.dumps(_beat(transfer_addresses=[ADDR, ADDR2.upper().replace("0X", "0x")])))
    R._EVM_WATCHER_HEALTH.clear()
    assert R.j7_shadow_event(None, _j7()) is not None, "addresses compare lowercased"


def test_an_unreadable_definition_keeps_the_buy_live(armed, monkeypatch):
    def boom(*a):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(axv, "evm_watched_owners", boom)
    assert R.j7_shadow_event(None, _j7()) is None


def test_the_half_applied_state_keeps_j7_live(armed):
    """Alpha (c): the knob is set but the watcher is the previous release, whose push beat carries
    no transfer_addresses. Nobody is covered; J7 stays live."""
    _owners, beat_path = armed
    old = _beat()
    del old["transfer_addresses"]
    beat_path.write_text(json.dumps(old))
    R._EVM_WATCHER_HEALTH.clear()
    assert R.j7_shadow_event(None, _j7()) is None


def test_the_solana_path_is_unchanged_by_the_evm_knob(armed):
    assert R.j7_shadow_event(None, _j7(network="solana")) is None     # Solana mode is live here


# ---------------------------------------------------------------------------- the health gate

@pytest.mark.parametrize("why, over, age_s, healthy", [
    ("fresh push beat", {}, 1, True),
    ("stale past the socket's own reconnect", {}, 16, False),
    ("from the future", {}, -30, False),
    ("poll fallback", {"mode": "poll"}, 1, False),
    ("a poll-mode beat carries no mode", {"mode": None}, 1, False),
    ("coverage unknown", {"coverage": None}, 1, False),
    ("coverage low", {"coverage_low_since": 1790000000.0}, 1, False),
    ("log silence suspect", {"log_silence": "suspect"}, 1, False),
    ("a subscription suspect", {"subscriptions": {"initialize": {"verdict": "suspect"}}}, 1, False),
    ("no reference is not suspect (56)", {"log_silence": "no_reference",
                                          "subscriptions": {"transfers": {"verdict": "no_reference"}}}, 1, True),
    ("no acked set (the previous release)", {"transfer_addresses": None}, 1, False),
])
def test_evm_watcher_health(monkeypatch, tmp_path, why, over, age_s, healthy):
    monkeypatch.setattr(cfg, "MEMECOIN_J7_SHADOW_EVM_MAX_AGE_S", 15.0, raising=False)
    now = datetime(2026, 9, 29, 3, 0, tzinfo=timezone.utc)
    beat = _beat(ts=now)
    beat.update(over)
    got = _REAL_HEALTH("4663", now_wall=now + timedelta(seconds=age_s), path=_write(tmp_path, beat))
    assert (got is not None) is healthy, why


def test_the_gate_returns_the_acked_set_lowercased(tmp_path):
    now = datetime.now(timezone.utc)
    got = _REAL_HEALTH("4663", now_wall=now, path=_write(tmp_path, _beat(
        ts=now, transfer_addresses=["0x" + "AB" * 20])))
    assert got == frozenset({"0x" + "ab" * 20})


def test_an_unreadable_beat_is_unhealthy(tmp_path):
    assert _REAL_HEALTH("4663", path=tmp_path / "missing") is None
    assert _REAL_HEALTH("4663", path=_write(tmp_path, "{not json")) is None


def test_each_chain_reads_its_own_heartbeat():
    assert R._evm_beat_path("4663").name == "onchain_evm_watch.heartbeat"
    assert R._evm_beat_path("56").name == "onchain_evm_watch.56.heartbeat"


# ---------------------------------------------------------------------------- the knob

def test_the_knob_parses_chain_names_and_drops_what_it_cannot_use():
    assert R._j7_shadow_evm_chains(" 4663, bsc ,solana,nope,,8453") == frozenset({"4663", "56", "8453"})
    assert R._j7_shadow_evm_chains("") == frozenset()
    assert R._j7_shadow_evm_chains(None) == frozenset()


# ---------------------------------------------------------------------------- the decision

def test_a_shadow_evm_decision_reads_nothing_it_will_not_use_and_never_reaches_the_leg(monkeypatch):
    from test_memecoin_entry_identity import FakeClock, _approve, _arm, _conn, _event
    conn = _conn()
    _arm(monkeypatch)
    _approve(conn, chains=("4663",))
    monkeypatch.setattr(cfg, "MEMECOIN_J7_SHADOW_EVM_CHAINS", "4663", raising=False)
    monkeypatch.setattr(R, "j7_shadow_event", _tag_everything)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_EARLY_GATE_READS_EVM", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEY_PREWARM_ENABLED", True, raising=False)
    from sniper.services import memecoin_evm_key_prewarm as kp
    prewarmed = []
    monkeypatch.setattr(kp, "enqueue", lambda *a, **k: prewarmed.append(a))

    def leg(*a, **k):
        raise AssertionError("a shadow decision reached the leg")
    monkeypatch.setattr(R, "real_entry", leg)
    monkeypatch.setattr(R, "_notify_entry_projection", lambda *a, **k: None)
    monkeypatch.setattr(R, "_maybe_enqueue_retry", lambda *a, **k: None)
    native = []
    ctx = {"usdc_fn": lambda: 300.0, "native_fn": lambda: native.append(1) or 1.0,
           "token_safety_fn": lambda mint, **kw: (True, "ok"), "execute_fn": leg, "kill_path": None}
    hook = R.make_real_hook(None, {"4663": ctx}, now_fn=lambda: datetime.now(timezone.utc).isoformat(),
                            trade_size=10.0, max_age_s=120,
                            stats_fn=lambda mint, network_id=None: {"price": 0.25, "market_cap": 50_000.0},
                            trace_clock=FakeClock())
    ev = _event(network_id="4663", mint="0x" + "cc" * 20, source="follow_activity")
    ev.created_at = ev.received_at_normalized = datetime.now(timezone.utc).isoformat()
    res = hook(conn, ev)
    assert res["j7_shadow"] is True and res["status"] == "shadow", res
    assert native == [], "the EVM native balance is read only by the leg"
    assert prewarmed == [], "the lane prewarms its own coin"
    row = conn.execute("SELECT delivery_lane, terminal_status FROM memecoin_entry_attempts").fetchone()
    assert tuple(row) == ("j7_shadow", "shadow")


def _tag_everything(conn, ev):
    import copy
    tagged = copy.copy(ev)
    tagged.delivery_lane = R.J7_SHADOW_LANE
    return tagged


def test_the_lane_does_not_treat_an_evm_shadow_row_as_the_feeds():
    from sniper.services.memecoin_onchain_lane import already_attempted_from_another_lane
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    tx, mint = "0x" + "ab" * 32, "0x" + "cc" * 20
    now = datetime.now(timezone.utc)
    conn.execute("INSERT INTO memecoin_entry_attempts (attempt_id, origin_event_id, signal_source, "
                 "decision_started_at, delivery_lane, network_id, mint) VALUES (?,?,?,?,?,?,?)",
                 ("j7", "swap:raybot:%s:owner:%s" % (tx, mint), "follow_activity",
                  (now - timedelta(seconds=3)).isoformat(), "j7_shadow", "4663", mint))
    key = "4663:%s:%s" % (tx, mint)
    assert not already_attempted_from_another_lane(conn, key, now.isoformat())
    conn.execute("UPDATE memecoin_entry_attempts SET delivery_lane='j7_wallets'")
    assert already_attempted_from_another_lane(conn, key, now.isoformat()), "a live J7 row still wins"


# ---------------------------------------------------------------------------- the watcher side

def test_one_definition_of_watched_per_chain():
    """The J7 gate and the watcher read the same function, so they cannot drift."""
    from test_onchain_evm_verified_addresses import _conn as _addr_conn
    legacy = axv.LEGACY_CONFIRMED
    conn = _addr_conn(
        legacy_rows=[("u1", "alice", "4663", ADDR, "m", legacy),
                     ("u2", "bob", "4663", ADDR2, "m", legacy),       # armed on 56 only
                     ("u3", "carol", "4663", "0x" + "dd" * 20, "m", legacy),     # not armed
                     ("u4", "dave", "56", "0x" + "ee" * 20, "m", legacy)],
        armed=[("u1", "alice", "4663"), ("u2", "bob", "56"), ("u4", "dave", "56,4663")])
    assert axv.evm_watched_owners(conn, "4663") == {ADDR: ("u1", "alice")}
    assert axv.evm_watched_owners(conn, "56") == {"0x" + "ee" * 20: ("u4", "dave")}
    spec = importlib.util.spec_from_file_location(
        "oeww_def", ROOT / "scripts/research/onchain_evm_wallet_watch.py")
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)
    w._configure("4663")
    assert w._verified_addresses(conn) == axv.evm_watched_owners(conn, "4663")


def _sub_reply(request_id, sub_id):
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "result": sub_id})


def _push(sub_id, payload):
    return json.dumps({"method": "eth_subscription", "params": {"subscription": sub_id, "result": payload}})


class _Socket:
    def __init__(self, messages):
        self._messages = list(messages)

    def send(self, raw):
        pass

    def __iter__(self):
        return iter(self._messages)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_the_acked_set_and_the_delivery_counts_are_per_connection():
    """transfer_addresses exists only once the transfers subscribe is ACKED, and a reconnect clears
    it; per-subscription delivery counts restart with each connection (Alpha b)."""
    armed_upper = "0x" + "AA" * 20
    tr_log = {"topics": [P.TRANSFER_TOPIC, "0x" + "0" * 64, P.address_topic(ADDR)],
              "transactionHash": "0x" + "ab" * 32, "blockNumber": "0x10"}
    scripts = [[_sub_reply(1, "0xh"), _push("0xh", {"number": "0x1"}),
                _sub_reply(2, "0xtr"), _push("0xh", {"number": "0x2"}),
                _push("0xtr", tr_log), _push("0xh", {"number": "0x3"})],
               [_sub_reply(1, "0xh2"), _push("0xh2", {"number": "0x4"})]]
    calls = {"n": 0}

    def connect_fn(_url, **_kw):
        i = calls["n"]
        calls["n"] += 1
        return _Socket(scripts[i] if i < len(scripts) else [])
    state = P.PushState()
    seen = []
    clock = iter([float(i) * 0.1 for i in range(200)])
    P.run_push_stream(
        "wss://x", on_transfer=lambda _l: None, on_initialize=lambda _l: None,
        addresses_fn=lambda: [armed_upper, "not-an-address"], pool_manager=None, init_topic=None,
        state=state, connect_fn=connect_fn, sleep_fn=lambda _s: None, now_fn=lambda: next(clock),
        on_head=lambda: seen.append((state.transfer_addresses, dict(state.delivered),
                                     state.connected_at is not None)),
        should_stop=lambda: calls["n"] >= 3)
    assert seen[0] == (None, {"heads": 1}, True), "not acked yet"
    assert seen[1] == ((ADDR,), {"heads": 2}, True), "acked: the filter's own valid addresses, lowercased"
    assert seen[2] == ((ADDR,), {"heads": 3, "transfers": 1}, True)
    assert seen[3] == (None, {"heads": 1}, True), "a reconnect clears the set and the counts"


# ---------------------------------------------------------------------------- the read

def _read_mod():
    spec = importlib.util.spec_from_file_location("j7sr", ROOT / "scripts/research/j7_shadow_read.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_a_miss_on_an_evm_chain_is_a_stop():
    """Alpha (a): shadow is tagged only while the watcher is healthy, so an EVM MISS is a real miss
    with the watcher up: revert that chain to live."""
    sr = _read_mod()
    assert sr.stop_flags(fills_window=1, would_window=1, would_total=1, refusal_families={},
                         chain="4663", miss_window=1) == [
        "STOP(3): 1 MISS on 4663 in 90 min with the watcher healthy: revert 4663 to live"]
    assert sr.stop_flags(fills_window=1, would_window=1, would_total=1, refusal_families={},
                         chain="4663", miss_window=0) == []
    assert sr.stop_flags(fills_window=0, would_window=4, would_total=4, refusal_families={},
                         chain="4663", miss_window=0) == [
        "STOP(1): 0 4663 fills in 90 min with 4 shadow would-enters"]
    assert sr.stop_flags(fills_window=0, would_window=4, would_total=4, refusal_families={},
                         miss_window=2) == ["STOP(1): 0 Solana fills in 90 min with 4 shadow would-enters"], \
        "Solana keeps its own rule: STOP(1) there is read against the MISS count by hand"


def test_the_push_beat_carries_the_acked_set_and_the_per_connection_counts(monkeypatch, tmp_path):
    import scripts.research.onchain_evm_wallet_watch as w
    from test_onchain_evm_watch_log_silence import _Conn
    monkeypatch.setattr(w, "get_connection", lambda path: _Conn())
    monkeypatch.setattr(w.m, "ensure_memecoin_tables", lambda conn: None)
    monkeypatch.setattr(w, "_require_addresses", lambda conn: {ADDR: ("u1", "h1")})
    monkeypatch.setattr(w, "_verified_addresses", lambda conn: {ADDR: ("u1", "h1")})
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_RELAY_CODE_TTL_S", 0.0, raising=False)
    w._configure("4663")
    hb = tmp_path / "hb.json"
    monkeypatch.setattr(w, "HEARTBEAT", hb)

    def fake_stream(ws_url, **kw):
        state = kw["state"]
        state.new_connection(1000.0)
        state.transfer_addresses = (ADDR,)
        state.log("initialize", 1000.5)
        state.head(1001.0)
        kw["on_head"]()
        return state
    monkeypatch.setattr(P, "run_push_stream", fake_stream)
    price = w.NativePrice(lambda: 2650.0, log_fn=lambda line: None)
    price.refresh()
    try:
        w.run_push("wss://test", price=price)
    finally:
        w._configure("4663")
    beat = json.loads(hb.read_text())
    assert beat["mode"] == "push" and beat["transfer_addresses"] == [ADDR]
    assert beat["delivered_since_connect"] == {"initialize": 1, "heads": 1}
    assert beat["connected_at"] == 1000.0


def test_every_connection_attempt_calls_back_after_the_reset():
    """Gamma LOW (2026-09-29): the caller unpublishes the old socket's acked set at each attempt."""
    state = P.PushState()
    state.transfer_addresses = (ADDR,)
    seen = []

    def connect_fn(_url, **_kw):
        raise OSError("connection refused")
    stops = iter([False, False, True])
    P.run_push_stream(
        "wss://x", on_transfer=lambda _l: None, on_initialize=lambda _l: None,
        addresses_fn=lambda: [ADDR], pool_manager=None, init_topic=None, state=state,
        connect_fn=connect_fn, sleep_fn=lambda _s: None, now_fn=lambda: 1.0,
        on_connect_attempt=lambda: seen.append(state.transfer_addresses),
        should_stop=lambda: next(stops, True))
    assert seen and all(s is None for s in seen), seen


def test_a_connection_attempt_clears_the_published_set_without_refreshing_the_beat(monkeypatch, tmp_path):
    """A fresh ts on a reconnect would make a watcher stuck reconnecting look alive to the
    watchdog's beat_stale rule, so only the acked set and the per-connection counts change."""
    import scripts.research.onchain_evm_wallet_watch as w
    from test_onchain_evm_watch_log_silence import _Conn
    monkeypatch.setattr(w, "get_connection", lambda path: _Conn())
    monkeypatch.setattr(w.m, "ensure_memecoin_tables", lambda conn: None)
    monkeypatch.setattr(w, "_require_addresses", lambda conn: {ADDR: ("u1", "h1")})
    monkeypatch.setattr(w, "_verified_addresses", lambda conn: {ADDR: ("u1", "h1")})
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_RELAY_CODE_TTL_S", 0.0, raising=False)
    w._configure("4663")
    hb = tmp_path / "hb.json"
    monkeypatch.setattr(w, "HEARTBEAT", hb)
    old = _beat(ts=datetime(2026, 9, 29, 3, 0, tzinfo=timezone.utc),
                delivered_since_connect={"heads": 9}, connected_at=5.0)
    hb.write_text(json.dumps(old))

    def fake_stream(ws_url, **kw):
        kw["on_connect_attempt"]()
        return kw["state"]
    monkeypatch.setattr(P, "run_push_stream", fake_stream)
    price = w.NativePrice(lambda: 2650.0, log_fn=lambda line: None)
    price.refresh()
    try:
        w.run_push("wss://test", price=price)
    finally:
        w._configure("4663")
    beat = json.loads(hb.read_text())
    assert beat["transfer_addresses"] is None
    assert beat["delivered_since_connect"] == {} and beat["connected_at"] is None
    assert beat["ts"] == old["ts"], "liveness is a delivered head's to write, never a reconnect's"
    assert _REAL_HEALTH("4663", path=hb, now_wall=datetime(2026, 9, 29, 3, 0, 1, tzinfo=timezone.utc)) is None


def test_a_raising_connect_attempt_callback_never_ends_the_lane():
    """Gamma LOW: _drop_acked's write can raise (a full disk); the lane must still reconnect."""
    attempts = []

    def connect_fn(_url, **_kw):
        attempts.append(1)
        raise OSError("connection refused")

    def boom():
        raise OSError(28, "No space left on device")
    stops = iter([False, False, False, True])
    P.run_push_stream(
        "wss://x", on_transfer=lambda _l: None, on_initialize=lambda _l: None,
        addresses_fn=lambda: [ADDR], pool_manager=None, init_topic=None,
        connect_fn=connect_fn, sleep_fn=lambda _s: None, now_fn=lambda: 1.0,
        on_connect_attempt=boom, should_stop=lambda: next(stops, True))
    assert len(attempts) >= 2, "the lane stopped at the first failed callback"


# ---------------------------------------------------------------------------- BLIND (2026-09-29)
# 05:43-05:49Z: three J7 4663 shadow decisions by 0xdetweiler, whose only verified address was in the
# watcher's acked filter, were refused over_ceiling by J7's own gate; the lane never saw any of them.
# STOP(3) counts only would-enters, so a blind lane hid behind the gate. BLIND counts every covered
# shadow buy with no lane sighting within 30 s, whatever J7's gate said, and is a STOP on EVM.

def _blind_book():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def _shadow_row(conn, aid, at, *, uid="u1", mint="0xabc", status="refused",
                reason="mcap $394,600 over ceiling $250,000 (over_ceiling)"):
    conn.execute("INSERT INTO memecoin_entry_attempts (attempt_id, origin_event_id, signal_source, "
                 "decision_started_at, delivery_lane, network_id, mint, source_user_id, source_handle, "
                 "terminal_status, terminal_reason, completed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (aid, "swap:raybot:0x" + "ab" * 32 + ":0xtrader:" + mint, "follow_activity", at,
                  "j7_shadow", "4663", mint, uid, "0xdetweiler", status, reason, at))


def test_a_lane_sighting_is_a_detection_or_a_fast_lane_attempt_within_thirty_seconds():
    sr = _read_mod()
    conn = _blind_book()
    at = "2026-09-29T05:43:43.409383+00:00"
    assert not sr.lane_sighted(conn, "4663", "u1", "0xABC", at)
    conn.execute("INSERT INTO onchain_wallet_detections (signature, source_user_id, handle, address, "
                 "network_id, mint, detected_at, created_at) VALUES (?,?,?,?,?,?,?,?)",
                 ("4663:0xtx:0xabc", "u1", "h", "0xaddr", "4663", "0xabc",
                  "2026-09-29T05:43:20.000000+00:00", "2026-09-29T05:43:20.000000+00:00"))
    assert sr.lane_sighted(conn, "4663", "u1", "0xABC", at), "23 s earlier, mint case-insensitive"
    assert not sr.lane_sighted(conn, "4663", "u2", "0xabc", at), "another wallet"
    assert not sr.lane_sighted(conn, "4663", "u1", "0xabc", "2026-09-29T05:44:30+00:00"), "70 s later"
    conn2 = _blind_book()
    conn2.execute("INSERT INTO memecoin_entry_attempts (attempt_id, signal_source, decision_started_at, "
                  "network_id, mint, source_user_id) VALUES (?,?,?,?,?,?)",
                  ("lane", "onchain_watch", "2026-09-29T05:43:44+00:00", "4663", "0xabc", "u1"))
    assert sr.lane_sighted(conn2, "4663", "u1", "0xabc", at), "a lane attempt counts"
    conn2.execute("UPDATE memecoin_entry_attempts SET signal_source='follow_activity'")
    assert not sr.lane_sighted(conn2, "4663", "u1", "0xabc", at), "J7's own attempt is not a sighting"


def test_blind_covered_buys_are_a_stop_whatever_j7s_gate_said(tmp_path, monkeypatch):
    sr = _read_mod()
    monkeypatch.setattr(sr, "STATE", tmp_path)
    conn = _blind_book()
    now = datetime(2026, 9, 29, 5, 51, tzinfo=timezone.utc)
    for i, at in enumerate(("05:43:43", "05:47:06", "05:49:48")):
        _shadow_row(conn, "a%d" % i, "2026-09-29T%s.000000+00:00" % at)
    out = sr.read(conn, "2026-09-29T02:18:03+00:00", now, chain="4663")
    assert out["would"] == 0 and out["blind"] == 3 and out["blind_window"] == 3
    assert out["per_wallet"] == {"0xdetweiler": (3, 0)}, "the per-wallet match table"
    assert "STOP(4): 3 covered 4663 J7 buys the lane never sighted in 90 min: " \
           "the lane is blind to a covered wallet; revert 4663 to live" in out["stop"]


def test_blind_is_not_a_stop_on_solana():
    sr = _read_mod()
    assert sr.stop_flags(fills_window=1, would_window=1, would_total=1, refusal_families={},
                         chain="solana", blind_window=5) == []


def test_a_source_tx_match_is_a_sighting_however_late_j7_was(monkeypatch):
    """Gamma (b): DipWheeler's J7 event arrived ~10 min late; the lane saw the same tx 578 s before."""
    sr = _read_mod()
    conn = _blind_book()
    tx = "0x" + "8b" * 32
    conn.execute("INSERT INTO onchain_wallet_detections (signature, source_user_id, handle, address, "
                 "network_id, mint, detected_at, created_at) VALUES (?,?,?,?,?,?,?,?)",
                 ("4663:%s:0xabc" % tx, "u1", "h", "0xaddr", "4663", "0xabc",
                  "2026-09-29T05:34:05.000000+00:00", "2026-09-29T05:34:05.000000+00:00"))
    at = "2026-09-29T05:43:43.000000+00:00"
    assert sr.lane_sighted(conn, "4663", "u1", "0xabc", at, origin="swap:raybot:%s:0xt:0xabc" % tx)
    other = "swap:raybot:0x%s:0xt:0xabc" % ("cd" * 32)
    conn.execute("UPDATE onchain_wallet_detections SET detected_at=?", ("2026-09-29T05:43:40.000000+00:00",))
    assert not sr.lane_sighted(conn, "4663", "u1", "0xabc", at, origin=other), \
        "a tx the lane never saw is blind, even beside the same coin's other tx"


def test_a_lane_attempt_carrying_the_tx_is_a_sighting():
    sr = _read_mod()
    conn = _blind_book()
    sig = "5WybaE2favbrveot6oAZ3XBzV591FNfyLwEeRyfujZVf7zH7t6XyRm8US3id9H21wjB8EP9c7bsv4KcVFJAfpqtW"
    conn.execute("INSERT INTO memecoin_entry_attempts (attempt_id, origin_event_id, signal_source, "
                 "decision_started_at, network_id, mint, source_user_id) VALUES (?,?,?,?,?,?,?)",
                 ("lane", "onchain:" + sig, "onchain_watch", "2026-09-29T05:30:00+00:00", "solana",
                  "MintX", "u1"))
    assert sr.lane_sighted(conn, "solana", "u1", "MintX", "2026-09-29T05:40:00+00:00",
                           origin="pump:%s:MintXprefix" % sig), "10 min apart, the same tx"


def test_without_a_tx_the_window_is_anchored_on_the_source_time():
    sr = _read_mod()
    conn = _blind_book()
    conn.execute("INSERT INTO onchain_wallet_detections (signature, source_user_id, handle, address, "
                 "network_id, mint, detected_at, created_at) VALUES (?,?,?,?,?,?,?,?)",
                 ("SigZ", "u1", "h", "a", "solana", "MintZ", "2026-09-29T05:00:05+00:00",
                  "2026-09-29T05:00:05+00:00"))
    assert sr.lane_sighted(conn, "solana", "u1", "MintZ", "2026-09-29T05:00:00+00:00",
                           origin="swap:9f1c2e2a-uuid")


def test_cash_asset_buys_are_not_covered_buys(tmp_path, monkeypatch):
    """Gamma: J7 'buys' of wSOL are not memecoin buys; the lane rightly ignores them."""
    sr = _read_mod()
    monkeypatch.setattr(sr, "STATE", tmp_path)
    conn = _blind_book()
    _shadow_row(conn, "w", "2026-09-29T05:43:43.000000+00:00",
                mint="So11111111111111111111111111111111111111112")
    conn.execute("UPDATE memecoin_entry_attempts SET network_id='solana'")
    out = sr.read(conn, "2026-09-29T02:00:00+00:00", datetime(2026, 9, 29, 5, 51, tzinfo=timezone.utc),
                  chain="solana")
    assert out["blind"] == 0 and out["per_wallet"] == {}


def test_the_source_tx_is_found_by_shape_in_every_origin_form():
    sr = _read_mod()
    sig = "4ZAjRir2esx7wxSCrBpf7xNJhgroGAqZ8jyjKVc7ZU37hmayDX8m8JmxaqGfLru4Y9Zmw9qyvZ7H6i1xywHxPzH2"
    tx = "0x" + "8b" * 32
    assert sr._source_sig("pump:%s:E7pskdV9xCCMXtsu" % sig) == sig, "not the mint prefix"
    assert sr._source_sig("swap:raybot:%s:owner:Mint" % sig) == sig
    assert sr._source_sig("swap:raybot:%s:0x838a:0x923d" % tx) == tx
    assert sr._source_sig("onchain:4663:%s:0x923d" % tx) == tx
    assert sr._source_sig("swap:9f1c2e2a-uuid") is None and sr._source_sig(None) is None


def test_a_late_j7_event_without_a_tx_is_matched_at_its_source_time(tmp_path, monkeypatch):
    sr = _read_mod()
    monkeypatch.setattr(sr, "STATE", tmp_path)
    conn = _blind_book()
    _shadow_row(conn, "late", "2026-09-29T05:10:00.000000+00:00")            # J7 decided 10 min late
    conn.execute("UPDATE memecoin_entry_attempts SET origin_event_id='swap:9f1c2e2a-uuid', "
                 "observed_at='2026-09-29T05:00:00.000000+00:00'")
    conn.execute("INSERT INTO onchain_wallet_detections (signature, source_user_id, handle, address, "
                 "network_id, mint, detected_at, created_at) VALUES (?,?,?,?,?,?,?,?)",
                 ("4663:0xt:0xabc", "u1", "h", "a", "4663", "0xabc", "2026-09-29T05:00:05.000000+00:00",
                  "2026-09-29T05:00:05.000000+00:00"))
    out = sr.read(conn, "2026-09-29T04:00:00+00:00", datetime(2026, 9, 29, 5, 20, tzinfo=timezone.utc),
                  chain="4663")
    assert out["blind"] == 0 and out["per_wallet"] == {"0xdetweiler": (1, 1)}


def test_an_evm_cash_asset_buy_is_not_a_covered_buy_either(tmp_path, monkeypatch):
    """Gamma LOW: an EVM J7 'buy' of WETH must not trip STOP(4)."""
    sr = _read_mod()
    monkeypatch.setattr(sr, "STATE", tmp_path)
    conn = _blind_book()
    _shadow_row(conn, "weth", "2026-09-29T05:43:43.000000+00:00",
                mint="0x4200000000000000000000000000000000000006")
    _shadow_row(conn, "coin", "2026-09-29T05:44:00.000000+00:00", mint="0x" + "9d" * 20)
    conn.execute("UPDATE memecoin_entry_attempts SET network_id='8453'")
    out = sr.read(conn, "2026-09-29T02:00:00+00:00", datetime(2026, 9, 29, 5, 51, tzinfo=timezone.utc),
                  chain="8453")
    assert out["blind"] == 1 and out["per_wallet"] == {"0xdetweiler": (1, 0)}, \
        "the memecoin row counts, the WETH row does not"
