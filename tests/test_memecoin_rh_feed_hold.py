"""D-b (2026-10-16, Alpha; MEMECOIN_RH_FAST_FEED_HOLD): the 4663 feed detector holds a signal for the
runner's live rh lane at the feed's arrival, before its receipt wait.

CLAWDMKT 0x9f69a7ea…3a69 (11:21:25Z): the on-chain watcher decoded the source's Transfer in 118 ms and
its lane began the attempt 61 ms before the rh lane's datagram arrived (the detector waited 164 ms for
the receipt), so LANE_HOLDS was not yet set; the fast lane then lost the one-position lock. Since
10-09 the on-chain lane decided before the datagram on 99 of 125 shared transactions (p50 -33 ms).
Pins: the hold goes out before the queue and the receipt wait; a decision that is not the lane's
datagram releases it; a tx-only hold covers every coin of the transaction; live and the knob only;
bounded by LANE_HOLDS' own bound; counted in the lane's heartbeat."""
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from sniper.services import memecoin_rh_fast_decide as decide  # noqa: E402
from sniper.services import memecoin_rh_fast_lane as rh  # noqa: E402
from test_memecoin_onchain_rh_lane_hold import MINT, TX, holds, ledger, sweep  # noqa: E402,F401

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def knob(monkeypatch):
    monkeypatch.setenv("MEMECOIN_RH_FAST_FEED_HOLD", "1")


# --- the detector's datagrams ----------------------------------------------------------------------

def _sender(mode="live"):
    sent = []
    return rh.make_feed_hold_sender(mode, "\0x", send_fn=lambda a, p: sent.append(p) or True), sent


def test_the_detector_holds_then_settles_only_when_no_datagram_followed():
    holds_, sent = _sender()
    rec = {"tx_hash": TX.upper().replace("0X", "0x"), "mint": MINT}
    assert holds_.hold(rec) is True
    assert sent[0]["kind"] == rh.FEED_HOLD_KIND and sent[0]["tx_hash"] == TX and sent[0]["mint"] == MINT
    assert holds_.settle(rec, {"reason": "would_send"}) is False and len(sent) == 1
    assert holds_.settle(rec, {"reason": "chase"}) is True and sent[1]["kind"] == rh.FEED_RELEASE_KIND
    assert holds_.settle(rec, None) is True                       # an exception or an expiry


def test_no_mint_in_the_calldata_holds_the_whole_transaction():
    holds_, sent = _sender()
    holds_.hold({"tx_hash": TX})
    assert sent[0]["mint"] == "*"


def test_the_detector_sends_nothing_unless_live_and_listed(monkeypatch):
    holds_, sent = _sender("runner_shadow")
    assert holds_.hold({"tx_hash": TX, "mint": MINT}) is False and sent == []
    monkeypatch.setenv("MEMECOIN_RH_FAST_FEED_HOLD", "0")
    holds_, sent = _sender()
    assert holds_.hold({"tx_hash": TX, "mint": MINT}) is False and sent == []


def test_a_failing_send_never_raises_into_the_feed_thread():
    def boom(_a, _p):
        raise OSError("gone")
    holds_ = rh.make_feed_hold_sender("live", "\0x", send_fn=boom)
    assert holds_.hold({"tx_hash": TX, "mint": MINT}) is False


def test_the_hold_precedes_the_decide_queue_and_a_dropped_signal_releases_it():
    src = (REPO / "sniper" / "services" / "memecoin_rh_fast_decide.py").read_text()
    body = src.split("    def offer(rec):", 1)[1].split("\n    def ", 1)[0]
    assert body.index("feed_holds.hold(rec)") < body.index("work.put_nowait")
    assert "feed_holds.release(rec)" in body.split("except queue.Full:", 1)[1]


class _Pending:
    def __init__(self, ready=(), expired=()):
        self._ready, self._expired = list(ready), list(expired)

    def ready(self, tape, *, now):
        out, self._ready = self._ready, []
        return out

    def pop_expired(self):
        out, self._expired = self._expired, []
        return out


def test_a_re_ask_settles_its_hold_by_its_own_outcome_and_an_expiry_releases(monkeypatch):
    settled = []
    outs = iter([{"reason": "would_send"}, {"reason": "chase"}])
    monkeypatch.setattr(decide, "handle_signal", lambda *a, **k: next(outs))
    monkeypatch.setattr(decide, "record_pending_expired", lambda *a, **k: None)
    a, b, c = {"tx_hash": "0xa"}, {"tx_hash": "0xb"}, {"tx_hash": "0xc"}
    pending = _Pending(ready=[(a, 0.0), (b, 0.0)], expired=[(c, 0.0, "tape_behind")])
    decide.service_pending_sources(pending, tape=None, live={}, db_path=None, now_fn=None,
                                   on_decided=None, now=0.0,
                                   settle=lambda rec, out: settled.append((rec["tx_hash"], out)))
    assert settled == [("0xa", {"reason": "would_send"}), ("0xb", {"reason": "chase"}), ("0xc", None)]


# --- the runner's dispatch -------------------------------------------------------------------------

@pytest.mark.parametrize("payload, ok", [
    ({"kind": rh.FEED_HOLD_KIND, "tx_hash": TX, "mint": MINT}, True),
    ({"kind": rh.FEED_RELEASE_KIND, "tx_hash": TX, "mint": "*"}, True),
    ({"kind": rh.FEED_HOLD_KIND, "tx_hash": "0x12", "mint": MINT}, False),
    ({"kind": rh.FEED_HOLD_KIND, "tx_hash": TX, "mint": "0xnothex"}, False),
])
def test_a_hold_datagram_is_validated_like_a_signal(payload, ok):
    got = rh.check_sender(payload, os.getpid(), os.getuid(), detector_pid=os.getpid())
    assert (got is None) == ok


class _Dispatch(rh.LaneDispatch):
    def __init__(self, live):
        self.live, self.drops, self.counts = live, rh.Drops(), __import__("collections").Counter()


def test_the_runner_holds_and_releases_for_the_live_lane_only(holds, monkeypatch):
    held, clock, wakes = holds
    d = _Dispatch(live=object())
    assert d._on_feed_hold({"kind": rh.FEED_HOLD_KIND, "tx_hash": TX, "mint": "*"}) == "feed_hold"
    assert held.holds(TX, MINT) and held.counts["hold_feed"] == 1 and held.recent[0][:2] == (TX, "*")
    assert d._on_feed_hold({"kind": rh.FEED_RELEASE_KIND, "tx_hash": TX, "mint": "*"}) == "feed_release"
    assert not held.holds(TX, MINT)
    assert _Dispatch(live=None)._on_feed_hold({"kind": rh.FEED_HOLD_KIND, "tx_hash": TX, "mint": MINT}) is None
    monkeypatch.setenv("MEMECOIN_RH_FAST_FEED_HOLD", "")
    d2 = _Dispatch(live=object())
    assert d2._on_feed_hold({"kind": rh.FEED_HOLD_KIND, "tx_hash": TX, "mint": MINT}) is None
    assert d2.drops.counts["feed_hold_off"] == 1 and not held.holds(TX, MINT)


# --- the on-chain lane waits on a feed hold --------------------------------------------------------

def test_the_onchain_lane_waits_on_a_tx_only_feed_hold_then_decides_after_its_release(ledger, holds):
    held, clock, wakes = holds
    calls, deferred = [], {}
    hook = lambda c, ev: calls.append(ev) or {"status": "rejected", "reason": "ordinary refusal"}
    held.hold(TX, "*", source="feed")                       # at the feed's arrival, before decode
    cursor, results = sweep(ledger, hook, deferred=deferred)
    assert calls == [] and results[0]["result"] == {"status": "deferred", "reason": "rh_fast_lane_in_flight"}
    held.release(TX, "*")                                   # the detector sent no signal
    assert wakes == [100.0]
    cursor, results = sweep(ledger, hook, cursor=cursor, deferred=deferred)
    assert len(calls) == 1


def test_the_lanes_verdict_on_its_datagram_also_ends_the_tx_only_feed_hold(ledger, holds):
    """Gamma B1: the feed held (tx, "*"), the detector's would_send datagram held (tx, M) and settled
    no release; the lane's refusal releases (tx, M). The "*" key must end with it, at once, and wake
    the on-chain lane, not keep it waiting to the bound."""
    held, clock, wakes = holds
    calls, deferred = [], {}
    hook = lambda c, ev: calls.append(ev) or {"status": "rejected", "reason": "ordinary refusal"}
    held.hold(TX, "*", source="feed")                       # feed arrival, calldata names no mint
    held.hold(TX, MINT)                                     # the would_send datagram
    cursor, results = sweep(ledger, hook, deferred=deferred)
    assert calls == [] and results[0]["result"]["reason"] == "rh_fast_lane_in_flight"
    held.release(TX, MINT)                                  # the lane refused (safety, ceiling)
    assert not held.holds(TX, MINT) and wakes == [100.0]
    cursor, results = sweep(ledger, hook, cursor=cursor, deferred=deferred)
    assert len(calls) == 1 and held.counts["expired_waited"] == 0


def test_a_tx_only_feed_hold_that_ran_out_is_counted_apart(ledger, holds):
    held, clock, wakes = holds
    held.hold(TX, "*", source="feed")
    assert held.holds(TX, MINT)
    clock.t += held.bound_s + 0.01
    assert not held.holds(TX, MINT)
    assert held.counts["expired_waited"] == 1 and held.counts["expired_waited_tx_only"] == 1


def test_a_feed_hold_that_nobody_releases_ends_at_the_bound_and_is_counted(ledger, holds):
    held, clock, wakes = holds
    calls, deferred = [], {}
    hook = lambda c, ev: calls.append(ev) or {"status": "rejected", "reason": "x"}
    held.hold(TX, MINT, source="feed")
    cursor, _ = sweep(ledger, hook, deferred=deferred)
    clock.t += held.bound_s + 0.01
    cursor, _ = sweep(ledger, hook, cursor=cursor, deferred=deferred)
    assert len(calls) == 1 and held.counts["expired_waited"] == 1


# --- measured in the heartbeat ---------------------------------------------------------------------

def test_the_heartbeat_carries_the_hold_counts_and_recent_feed_holds(tmp_path, holds):
    held, clock, wakes = holds
    held.hold(TX, MINT, source="feed")

    class Counts:
        counts = {"candidates_msgs": 0, "signals": 0, "verdicts_ok": 0, "verdicts_unsafe": 0,
                  "verdict_errors": 0, "cpu_ms": 0.0, "calls": 0, "http_429": 0, "feed_holds": 1}

        def size(self):
            return 0
    db = str(tmp_path / "lane.db")
    rh.write_heartbeat(db, Counts(), Counts(), Counts())
    (raw,) = sqlite3.connect(db).execute("SELECT lane_holds FROM rh_fast_lane_heartbeat").fetchone()
    got = json.loads(raw)
    assert got["hold_feed"] == 1 and got["feed_holds"] == 1 and got["recent"][0][:2] == [TX, MINT]


def test_the_decide_loop_settles_every_outcome_but_a_pending_re_ask():
    src = (REPO / "sniper" / "services" / "memecoin_rh_fast_decide.py").read_text()
    loop = src.split("    def decide_loop():", 1)[1].split("\n    def ", 1)[0]
    assert 'out = {"reason": "reask_pending"}' in loop
    assert 'if (out or {}).get("reason") != "reask_pending":\n                    feed_holds.settle(rec, out)' in loop
    assert "settle=feed_holds.settle" in loop
