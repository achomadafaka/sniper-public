"""Log silence on a live head stream acts (Alpha 2026-10-16): resubscribe, then reconnect.

2026-10-16: after the 16:37:54Z socket-silent reconnect the 4663 socket kept delivering heads while
its log subscriptions went silent (Initialize last at ~16:47Z, transfers at 16:34:45Z) until a manual
restart at 17:53Z; three deposit-matched fast-lane buys (16:45, 17:39 x2) never got a watcher receipt
and expired proof_absent. The `log_silence: suspect` verdict was observe-only. Pins: the push stream
re-sends its log filters on the live socket when the watcher asks (`resubscribe_logs`), ignores late
deliveries on the dropped ids, and counts it as a reconnect cause so the watcher's gap backfill runs;
the watcher asks once per silent episode, forces a reconnect if no log returns within another
window, then waits a cooldown; any log resets it; a chain without the Initialize reference, or with
the knob empty, never acts.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from test_memecoin_onchain_push_lanes import ARMED, MANAGER, _push, _sub_reply  # noqa: E402
from sniper.execution import evm_v4_quote as V  # noqa: E402
from sniper.services import memecoin_onchain_push as P  # noqa: E402


class _Clk:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class _Sock:
    """Records sends; after the script, a head every second (or the injected message at its time)."""

    def __init__(self, script, clock, inject=None):
        self.script, self.clock, self.inject, self.sent, self.n = list(script), clock, dict(inject or {}), [], 0

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def recv(self, timeout=None):
        if self.script:
            return self.script.pop(0)
        self.clock.t += 1.0
        self.n += 1
        for at in sorted(self.inject):
            if self.clock.t >= at:
                return self.inject.pop(at)
        return _push("0xheads", {"number": hex(100 + self.n)})

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_the_stream_resubscribes_its_log_filters_on_the_live_socket():
    clock, opened, socks, transfers = _Clk(), [], [], []
    old_log = _push("0xtr_old", {"transactionHash": "0xold", "topics": []})
    new_log = _push("0xtr_new", {"transactionHash": "0xnew", "topics": []})

    def connect_fn(_u, **_k):
        opened.append(clock.t)
        # subscribe replies: 1 heads, 2 transfers, 3 initialize; after the resubscribe 4, 5
        sock = _Sock([_sub_reply(1, "0xheads"), _sub_reply(2, "0xtr_old"), _sub_reply(3, "0xin_old")],
                     clock, inject={7.0: _sub_reply(4, "0xtr_new"), 8.0: _sub_reply(5, "0xin_new"),
                                    9.0: old_log, 10.0: new_log})
        socks.append(sock)
        return sock
    asked = []

    def resubscribe():
        asked.append(clock.t)
        return P.ALL_LOGS if 5.0 <= clock.t and len([a for a in asked if a >= 5.0]) == 1 else ()
    state = P.PushState()
    P.run_push_stream("wss://x", on_transfer=transfers.append, on_initialize=lambda _l: None,
                      addresses_fn=lambda: [ARMED], pool_manager=MANAGER, init_topic=V._INIT_TOPIC,
                      state=state, connect_fn=connect_fn,
                      sleep_fn=lambda s: setattr(clock, "t", clock.t + s), now_fn=clock,
                      stale_after_s=15.0, resubscribe_logs=resubscribe, should_stop=lambda: clock.t >= 12)
    assert opened == [0.0], "a resubscribe keeps the socket"
    subs = [m for m in socks[0].sent if m["method"] == "eth_subscribe"]
    assert [m["id"] for m in subs] == [1, 2, 3, 4, 5]
    assert subs[3]["params"] == subs[1]["params"] and subs[4]["params"] == subs[2]["params"]
    assert subs[1]["params"][0] == "logs" and subs[2]["params"][0] == "logs"
    assert not any(m["method"] == "eth_unsubscribe" for m in socks[0].sent)
    assert [l["transactionHash"] for l in transfers] == ["0xnew"], "the dropped id's late delivery is ignored"
    assert state.reconnect_causes == {"log_silence_resubscribe": 1} and state.reconnects == 1


def test_a_transfer_only_resubscribe_keeps_the_initialize_subscription():
    """Trigger B (Alpha): transfers silent behind a live Initialize re-sends the transfer filter only."""
    clock, socks, inits, transfers = _Clk(), [], [], []

    def connect_fn(_u, **_k):
        socks.append(_Sock([_sub_reply(1, "0xheads"), _sub_reply(2, "0xtr_old"), _sub_reply(3, "0xin")],
                           clock, inject={7.0: _sub_reply(4, "0xtr_new"),
                                          9.0: _push("0xin", {"transactionHash": "0xinit", "topics": []}),
                                          10.0: _push("0xtr_new", {"transactionHash": "0xnew", "topics": []})}))
        return socks[-1]
    taken = []

    def resubscribe():
        if clock.t >= 5.0 and not taken:
            taken.append(clock.t)
            return ("transfers",)
        return ()
    state = P.PushState()
    P.run_push_stream("wss://x", on_transfer=transfers.append, on_initialize=inits.append,
                      addresses_fn=lambda: [ARMED], pool_manager=MANAGER, init_topic=V._INIT_TOPIC,
                      state=state, connect_fn=connect_fn,
                      sleep_fn=lambda s: setattr(clock, "t", clock.t + s), now_fn=clock,
                      stale_after_s=15.0, resubscribe_logs=resubscribe, should_stop=lambda: clock.t >= 12)
    subs = [m for m in socks[0].sent if m["method"] == "eth_subscribe"]
    assert [m["id"] for m in subs] == [1, 2, 3, 4] and subs[3]["params"] == subs[1]["params"]
    assert [l["transactionHash"] for l in inits] == ["0xinit"], "the live Initialize id still delivers"
    assert [l["transactionHash"] for l in transfers] == ["0xnew"]
    assert state.reconnect_causes == {"transfer_silence_resubscribe": 1}


def test_without_the_lever_nothing_is_resent():
    clock, socks = _Clk(), []

    def connect_fn(_u, **_k):
        socks.append(_Sock([_sub_reply(1, "0xheads")], clock))
        return socks[-1]
    state = P.PushState()
    P.run_push_stream("wss://x", on_transfer=lambda _l: None, on_initialize=lambda _l: None,
                      addresses_fn=lambda: [ARMED], pool_manager=MANAGER, init_topic=V._INIT_TOPIC,
                      state=state, connect_fn=connect_fn,
                      sleep_fn=lambda s: setattr(clock, "t", clock.t + s), now_fn=clock,
                      stale_after_s=15.0, resubscribe_logs=lambda: (), should_stop=lambda: clock.t >= 12)
    assert len([m for m in socks[0].sent if m["method"] == "eth_subscribe"]) == 3 and state.reconnects == 0


# --- the watcher's action on its own verdict ---------------------------------------------------------

def _action(**kw):
    return P.LogSilenceAction(limit_s=300.0, cooldown_s=900.0, **kw)


def test_suspect_resubscribes_once_then_reconnects_then_waits_the_cooldown():
    act = _action()
    assert act.note("ok", now=100.0, marker=5) is None
    assert act.note("suspect", now=400.0, marker=5) == "resubscribe"
    assert act.take_resubscribe() is True and act.take_resubscribe() is False      # taken once
    for t in (401.0, 500.0, 699.0):
        assert act.note("suspect", now=t, marker=5) is None                          # inside the window
    assert act.note("suspect", now=700.0, marker=5) == "reconnect"
    assert act.take_reconnect() is True and act.take_reconnect() is False
    for t in (701.0, 1599.0):
        assert act.note("suspect", now=t, marker=5) is None                          # the cooldown
    assert act.note("suspect", now=1600.0, marker=5) == "resubscribe"
    assert act.stats == {"resubscribes": 2, "reconnects": 1, "stage": "resubscribed"}


def test_a_delivered_log_resets_the_episode():
    act = _action()
    act.note("ok", now=0.0, marker=1)
    assert act.note("suspect", now=400.0, marker=1) == "resubscribe"
    assert act.note("ok", now=410.0, marker=2) is None and act.stats["stage"] is None
    assert act.note("suspect", now=900.0, marker=2) == "resubscribe"                 # a new episode


@pytest.mark.parametrize("verdict", ["ok", "no_reference"])
def test_only_suspect_acts(verdict):
    act = _action()
    for t in range(0, 5000, 100):
        assert act.note(verdict, now=float(t), marker=0) is None
    assert act.stats == {"resubscribes": 0, "reconnects": 0, "stage": None}


# --- wired into the watcher (its own verdicts, the knob, the two triggers) ------------------------------

from test_onchain_evm_watch_log_silence import _heads, _log, _run, watcher  # noqa: E402,F401
from sniper.settings import memecoin as cfg  # noqa: E402


def _probe(asked, kinds=("resubscribe_logs", "force_reconnect")):
    """A step that asks the stream's levers the way the push loop does after each head."""
    def step(state, kw):
        for name in kinds:
            lever = kw.get(name)
            got = lever() if lever is not None else None
            if got:
                asked.append((name, got))
    return step


def _ticks(clock, n, dt, asked):
    return [s for _ in range(n) for s in (_heads(clock, 1, dt), _probe(asked))]


def test_the_watcher_resubscribes_all_logs_then_reconnects_on_aggregate_silence(watcher, monkeypatch):
    clock, hb = watcher
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_WS_LOG_SILENCE_ACT_CHAINS", "4663", raising=False)
    asked = []
    _run(monkeypatch, "4663", [_log(clock), _log(clock, "transfers")] + _ticks(clock, 70, 10.0, asked), hb)
    assert asked == [("resubscribe_logs", P.ALL_LOGS), ("force_reconnect", True)], asked
    beat = json.loads(hb.read_text())
    assert beat["log_silence_actions"]["all"] == {"resubscribes": 1, "reconnects": 1, "stage": "reconnected"}


def test_transfers_silent_behind_live_initialize_resubscribes_the_transfer_filter_once(watcher, monkeypatch):
    clock, hb = watcher
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_WS_LOG_SILENCE_ACT_CHAINS", "4663", raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_WS_TRANSFER_SILENCE_S", 900.0, raising=False)
    asked, steps = [], [_log(clock, "transfers")]
    for _ in range(19):                                   # 950 s: Initialize every 50 s, no transfer
        steps += _ticks(clock, 5, 10.0, asked) + [_log(clock, "initialize")]
    _run(monkeypatch, "4663", steps, hb)
    assert asked == [("resubscribe_logs", ("transfers",))], asked


def test_initialize_silent_too_takes_the_aggregate_path_not_both(watcher, monkeypatch):
    clock, hb = watcher
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_WS_LOG_SILENCE_ACT_CHAINS", "4663", raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_WS_TRANSFER_SILENCE_S", 900.0, raising=False)
    asked = []
    _run(monkeypatch, "4663", [_log(clock, "transfers"), _log(clock)] + _ticks(clock, 95, 10.0, asked), hb)
    assert [a for a in asked if a[0] == "resubscribe_logs"] == [("resubscribe_logs", P.ALL_LOGS)], asked


@pytest.mark.parametrize("chain,knob", [("4663", ""), ("56", "4663,56")])
def test_no_action_with_the_knob_empty_or_without_a_reference(watcher, monkeypatch, chain, knob):
    clock, hb = watcher
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_WS_LOG_SILENCE_ACT_CHAINS", knob, raising=False)
    asked = []
    _run(monkeypatch, chain, [_log(clock, "transfers")] + _ticks(clock, 200, 10.0, asked), hb)
    assert asked == []


# --- trigger B reads no lane window; its escalated reconnect does (Gamma MEDIUM, Alpha 2026-10-16) ---

from test_onchain_evm_watch_backfill import _run as _bf_run, env  # noqa: E402,F401
import scripts.research.onchain_evm_wallet_watch as w  # noqa: E402


def test_a_transfer_resubscribe_reads_no_lane_window_but_its_reconnect_does(monkeypatch, env):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_WS_LOG_SILENCE_ACT_CHAINS", "4663", raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_WS_LOG_SILENCE_S", 300.0, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_WS_TRANSFER_SILENCE_S", 60.0, raising=False)
    clock = [3_000.0]
    monkeypatch.setattr(w, "_wall_clock", lambda: clock[0])
    events, block = [], [3_000]

    def blocks(n):
        """n heads at 1 block/s on the wall clock; an Initialize log every 10 blocks; after each head
        the stream's levers are asked, and a lever that fires is counted the way the stream counts it."""
        def step(state, kw):
            for _ in range(n):
                block[0] += 1
                clock[0] = float(block[0])
                if block[0] % 10 == 0:
                    state.log("initialize", clock[0])
                state.head(clock[0], block[0], block[0])
                kw["on_head"]()
                names = kw["resubscribe_logs"]()
                if names:
                    events.append(("resubscribe", names, len(env["pages"])))
                    state.reconnects += 1
                if kw["force_reconnect"]():
                    events.append(("reconnect", len(env["pages"])))
                    state.reconnects += 1
        return step

    def transfer(state, kw):
        state.log("transfers", clock[0])
    _bf_run(monkeypatch, env, [blocks(5), transfer, blocks(140)])
    # B at 60 s of transfer silence, its reconnect 60 s later; the page count is taken when each fires
    assert events == [("resubscribe", ("transfers",), 0), ("reconnect", 0)], events
    assert env["pages"], "the escalated reconnect read the lane window"
