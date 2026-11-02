"""A live socket that stops delivering logs must not look like a quiet chain (2026-09-28, Alpha GO).

Measured 2026-09-28: DopamineFeenFr1's 4663 buy 0x2330b335a6 (12:16:11Z) was never delivered while
the watcher ran; its log is silent from 12:10:21Z to 12:24:07Z with no line of any kind. Heads were
not logged, so a dead log subscription and a quiet chain could not be told apart. The heartbeat now
says which, OBSERVE-ONLY: heads keep arriving (the socket is alive) while no log on ANY subscription
has arrived for MEMECOIN_EVM_WS_LOG_SILENCE_S -> `log_silence: suspect`. The reference is the V4
Initialize subscription on the same socket (4663 ~6/min); a chain without it (56) says
`no_reference`, never "ok". No RPC call; the heads are the existing subscription's. The clock is read
in the head callback, after the head is counted. Heads that stop too are a dead socket, which the
existing stale-head reconnect already handles.
"""
import json

import pytest

import scripts.research.onchain_evm_wallet_watch as w
from sniper.services import memecoin_onchain_push as P
from sniper.settings import memecoin as cfg


class _Conn:
    def close(self):
        pass


@pytest.fixture
def watcher(monkeypatch, tmp_path):
    monkeypatch.setattr(w, "get_connection", lambda path: _Conn())
    monkeypatch.setattr(w.m, "ensure_memecoin_tables", lambda conn: None)
    monkeypatch.setattr(w, "_require_addresses", lambda conn: {"0x" + "ab" * 20: ("u1", "h1")})
    monkeypatch.setattr(w, "_verified_addresses", lambda conn: {"0x" + "ab" * 20: ("u1", "h1")})
    monkeypatch.setattr(w, "HEARTBEAT", tmp_path / "hb.json")
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_WS_LOG_SILENCE_S", 300.0, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_RELAY_CODE_TTL_S", 0.0, raising=False)
    clock = [1_000.0]
    monkeypatch.setattr(w, "_wall_clock", lambda: clock[0])
    yield clock, tmp_path / "hb.json"
    w._configure("4663")


def _run(monkeypatch, chain, script, hb):
    w._configure(chain)
    monkeypatch.setattr(w, "HEARTBEAT", hb)            # _configure points it at the state root

    def fake_stream(ws_url, **kw):
        state = kw["state"]
        for step in script:
            step(state, kw)
        return state
    monkeypatch.setattr(P, "run_push_stream", fake_stream)
    price = w.NativePrice(lambda: 2650.0, log_fn=lambda line: None)
    price.refresh()
    w.run_push("wss://test", price=price)


def _log(clock, name="initialize"):
    def step(state, kw):
        state.log(name, clock[0])
    return step


def _heads(clock, n, dt):
    def step(state, kw):
        for _ in range(n):
            clock[0] += dt
            state.head(clock[0])
            kw["on_head"]()
    return step


def test_heads_without_any_log_past_the_window_is_suspect_once(watcher, monkeypatch, capsys):
    clock, hb = watcher
    _run(monkeypatch, "4663", [_log(clock), _heads(clock, 40, 10.0)], hb)      # 400 s of heads, no log
    beat = json.loads(hb.read_text())
    assert beat["log_silence"] == "suspect" and beat["heads_since_log"] == 40
    assert beat["log_silence_s"] == pytest.approx(400.0)
    out = capsys.readouterr().out
    assert out.count("log silence suspect") == 1, "one line per silent episode"


def test_a_log_ends_the_episode(watcher, monkeypatch, capsys):
    clock, hb = watcher
    _run(monkeypatch, "4663", [_log(clock), _heads(clock, 40, 10.0), _log(clock), _heads(clock, 1, 1.0)], hb)
    beat = json.loads(hb.read_text())
    assert beat["log_silence"] == "ok" and beat["heads_since_log"] == 1


def test_inside_the_window_is_ok(watcher, monkeypatch):
    clock, hb = watcher
    _run(monkeypatch, "4663", [_log(clock), _heads(clock, 20, 10.0)], hb)      # 200 s
    assert json.loads(hb.read_text())["log_silence"] == "ok"


def test_a_chain_without_a_busy_reference_says_so(watcher, monkeypatch):
    clock, hb = watcher
    _run(monkeypatch, "56", [_heads(clock, 40, 10.0)], hb)
    assert json.loads(hb.read_text())["log_silence"] == "no_reference"


# --- per subscription (2026-09-28, Alpha): the aggregate counts ANY log, so a dead TRANSFER filter
# behind a live Initialize subscription read "ok". Each subscription now reports its own silence.

def test_a_silent_transfer_filter_behind_live_initialize_is_visible(watcher, monkeypatch):
    clock, hb = watcher
    steps = [_log(clock, "transfers")]
    for _ in range(8):                                    # Initialize keeps flowing, transfers stop
        steps += [_heads(clock, 5, 10.0), _log(clock, "initialize")]
    steps += [_heads(clock, 1, 1.0)]
    _run(monkeypatch, "4663", steps, hb)
    beat = json.loads(hb.read_text())
    assert beat["log_silence"] == "ok", "any log still counts for the whole socket"
    subs = beat["subscriptions"]
    assert subs["transfers"]["silence_s"] == pytest.approx(401.0)
    assert subs["transfers"]["verdict"] == "no_reference", "a sparse filter cannot call itself dead"
    assert subs["initialize"]["verdict"] == "ok" and subs["initialize"]["silence_s"] == pytest.approx(1.0)


def test_a_silent_initialize_subscription_is_suspect_on_its_own(watcher, monkeypatch, capsys):
    clock, hb = watcher
    steps = [_log(clock, "initialize")]
    for _ in range(8):                                    # transfers keep flowing, Initialize stops
        steps += [_heads(clock, 5, 10.0), _log(clock, "transfers")]
    steps += [_heads(clock, 1, 1.0)]
    _run(monkeypatch, "4663", steps, hb)
    beat = json.loads(hb.read_text())
    assert beat["log_silence"] == "ok"
    assert beat["subscriptions"]["initialize"]["verdict"] == "suspect"
    assert capsys.readouterr().out.count("initialize subscription silent") == 1


def test_a_chain_without_initialize_lists_only_transfers(watcher, monkeypatch):
    clock, hb = watcher
    _run(monkeypatch, "56", [_log(clock, "transfers"), _heads(clock, 3, 1.0)], hb)
    subs = json.loads(hb.read_text())["subscriptions"]
    assert set(subs) == {"transfers"} and subs["transfers"]["verdict"] == "no_reference"
