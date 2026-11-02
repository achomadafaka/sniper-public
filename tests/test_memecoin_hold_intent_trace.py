"""Per-attempt trace records for the D-b 4663 feed hold and the C1 56 intent warm (2026-10-17, Alpha,
from Gamma's read 4 of bc75221a).

Both mechanisms kept only process counters (LaneHolds.counts, LiveLane.intent_counts), which died in
the 10:31Z reboot, so "on-chain decision minus hold" and "intent claimed vs expired" could not be read
per attempt from the ledger. Now the event carries `pre_trace_substages`, and the runner marks them on
the attempt trace right after the trace starts (no I/O; persisted with the trace as today):

  native_lane_hold: hold_ms, hold_outcome released | expired_waited | expired_waited_tx_only |
                    row_wait (the lane's in-flight row, no LaneHolds hold) |
                    expired | unknown | no_hold   (on-chain lane, 4663 and 8453 rows)
  evm.intent_warm:  intent claimed | expired_unused | open_unclaimed | none, handoff_to_decision_ms
                    (the lane signal's sent_epoch to its hook call, host clock; listed intent chains)

Kill knob: MEMECOIN_PRE_TRACE_SUBSTAGES=0 marks nothing."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from sniper.services import memecoin_rh_fast_lane as rh  # noqa: E402
from sniper.settings import memecoin as cfg  # noqa: E402
from test_memecoin_onchain_rh_lane_hold import MINT, TX, holds, ledger, sweep  # noqa: E402,F401
from test_memecoin_bsc_intent_warm import _counting, _intent, _settle, knob  # noqa: E402,F401
from test_memecoin_bsc_fast_lane_live import _miss, _payload, _signal, live56  # noqa: E402,F401


# --- LaneHolds remembers how each hold ended ---------------------------------------------------------

def _holds():
    clock = [100.0]
    return rh.LaneHolds(bound_s=1.0, clock=lambda: clock[0], wake_fn=lambda: None), clock


def test_a_released_hold_ends_released():
    h, _clock = _holds()
    h.hold("0xA", "0xM")
    assert h.holds("0xa", "0xm")
    h.release("0xa", "0xm")
    assert h.ended("0xa", "0xm") == "released"
    assert h.ended("0xa", "0xm") is None, "read once"


@pytest.mark.parametrize("mint, waited, want", [("0xm", True, "expired_waited"),
                                                ("*", True, "expired_waited_tx_only"),
                                                ("0xm", False, "expired")])
def test_an_expired_hold_names_whether_the_onchain_lane_waited(mint, waited, want):
    h, clock = _holds()
    h.hold("0xa", mint)
    if waited:
        assert h.holds("0xa", "0xm")
    clock[0] += 1.5
    assert not h.holds("0xa", "0xm")
    assert h.ended("0xa", "0xm") == want
    assert h.counts["expired_waited"] == (1 if waited else 0), "the old counters still count"


# --- the on-chain lane stamps the hold on its event --------------------------------------------------

def _hold_stage(ev):
    stages = dict(getattr(ev, "pre_trace_substages", None) or [])
    return stages.get("native_lane_hold")


def test_a_released_hold_reaches_the_onchain_attempt_with_its_wait(ledger, holds):
    held, clock, _wakes = holds
    calls, deferred = [], {}
    hook = lambda c, ev: calls.append(ev) or {"status": "rejected", "reason": "x"}
    held.hold(TX, MINT)
    cursor, _ = sweep(ledger, hook, deferred=deferred)
    held.release(TX, MINT)
    sweep(ledger, hook, cursor=cursor, deferred=deferred)
    stage = _hold_stage(calls[0])
    assert stage["hold_outcome"] == "released"
    assert isinstance(stage["hold_ms"], float) and stage["hold_ms"] >= 0.0


def test_an_expired_hold_reaches_the_onchain_attempt(ledger, holds):
    held, clock, _wakes = holds
    calls, deferred = [], {}
    hook = lambda c, ev: calls.append(ev) or {"status": "rejected", "reason": "x"}
    held.hold(TX, "*")
    cursor, _ = sweep(ledger, hook, deferred=deferred)
    clock.t += 1.5
    sweep(ledger, hook, cursor=cursor, deferred=deferred)
    assert _hold_stage(calls[0])["hold_outcome"] == "expired_waited_tx_only"


def test_an_unheld_4663_row_says_no_hold(ledger, holds):
    calls = []
    sweep(ledger, lambda c, ev: calls.append(ev) or {"status": "rejected", "reason": "x"}, deferred={})
    assert _hold_stage(calls[0]) == {"hold_ms": 0.0, "hold_outcome": "no_hold"}


# --- the runner marks them on the attempt trace ------------------------------------------------------

class _Ev:
    network_id, mint, fomo_event_id = "4663", MINT, "onchain:x"


def test_the_runner_marks_pre_trace_substages_and_the_knob_stops_it(monkeypatch):
    from sniper.services import memecoin_real_runner as runner
    marks = []

    class Trace:
        def mark_substage(self, name, **meta):
            marks.append((name, meta))
    ev = _Ev()
    ev.pre_trace_substages = [("native_lane_hold", {"hold_ms": 12.5, "hold_outcome": "released"})]
    monkeypatch.setattr(cfg, "MEMECOIN_PRE_TRACE_SUBSTAGES", True)
    runner._mark_pre_trace_substages(Trace(), ev)
    assert marks == [("native_lane_hold", {"hold_ms": 12.5, "hold_outcome": "released"})]
    monkeypatch.setattr(cfg, "MEMECOIN_PRE_TRACE_SUBSTAGES", False)
    runner._mark_pre_trace_substages(Trace(), ev)
    assert len(marks) == 1
    monkeypatch.setattr(cfg, "MEMECOIN_PRE_TRACE_SUBSTAGES", True)
    runner._mark_pre_trace_substages(None, ev)                    # no trace: nothing, no error
    runner._mark_pre_trace_substages(Trace(), _Ev())              # nothing to mark
    assert len(marks) == 1


# --- the 56 lane's intent, end to end into trace_json ------------------------------------------------

def _intent_stage(ledger):
    rows = [json.loads(r[0] or "{}") for r in ledger.execute(
        "SELECT trace_json FROM memecoin_entry_attempts ORDER BY rowid")]
    found = [e for t in rows for e in t.get("substage_events", []) if e.get("name") == "evm.intent_warm"]
    assert len(found) == 1, rows
    return found[0]


def test_a_claimed_intent_is_on_the_attempt_trace(live56):
    lane, ledger, _net, sender, _prime = live56
    live, mint = lane["live"], _payload()["mint"]
    _miss(live, mint)
    live.quotes[mint] = str(_payload()["quote"]).lower()
    _counting(lane)
    assert _intent(lane, mint) == "started"
    _settle(live)
    _signal(lane)
    stage = _intent_stage(ledger)
    assert stage["intent"] == "claimed"
    assert isinstance(stage["handoff_to_decision_ms"], float) and stage["handoff_to_decision_ms"] >= 0


def test_an_expired_intent_is_named_on_the_attempt_trace(live56):
    lane, ledger, _net, _sender, _prime = live56
    live, mint = lane["live"], _payload()["mint"]
    _miss(live, mint)
    _counting(lane)
    assert _intent(lane, mint) == "started"
    _settle(live)
    live._intent[1]["at"] -= live.INTENT_TTL_S + 1
    _signal(lane)
    assert _intent_stage(ledger)["intent"] == "expired_unused"


def test_a_signal_without_an_intent_says_none(live56):
    lane, ledger, _net, _sender, _prime = live56
    live, mint = lane["live"], _payload()["mint"]
    _miss(live, mint)
    _counting(lane)
    _signal(lane)
    assert _intent_stage(ledger)["intent"] == "none"


def test_a_wait_on_the_lanes_in_flight_row_ends_as_row_wait(ledger, holds, monkeypatch):
    from sniper.services import memecoin_onchain_lane as lane
    state = ["rh_lane_pending"]
    monkeypatch.setattr(lane, "_other_lane_attempt_state", lambda *a, **k: state[0])
    calls, deferred = [], {}
    hook = lambda c, ev: calls.append(ev) or {"status": "rejected", "reason": "x"}
    cursor, _ = sweep(ledger, hook, deferred=deferred)
    state[0] = "lane_released_unsigned"
    sweep(ledger, hook, cursor=cursor, deferred=deferred)
    stage = _hold_stage(calls[0])
    assert stage["hold_outcome"] == "row_wait" and stage["hold_ms"] >= 0.0
