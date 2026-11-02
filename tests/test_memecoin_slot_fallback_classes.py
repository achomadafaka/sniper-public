"""Slot-lane fallback classes (operator order 2026-10-06, via Alpha): fill with the slot lane; an
ordinary on-chain attempt only when the slot lane failed MECHANICALLY (landed fail, routed
handoff, exotic leg expired or released, build refused). Its own policy and sizing refusals and
its proof refusals are never overridden; absence alone never admits; an in-flight or ambiguous
slot send never reaches reservation; an unreadable slot state is a refusal with one page."""
import sqlite3
import time
import collections
from types import SimpleNamespace

import pytest

from sniper.services import memecoin_onchain_lane as O, memecoin_slot_lane as L
from sniper.settings import memecoin as cfg
from test_memecoin_onchain_lane import (ARMED_UID, NOW, SLOT_SCHEMA, approved, conn_with,
                                        now_iso)

SIG = "sigS"
DETECTED = NOW.isoformat()


@pytest.fixture(autouse=True)
def fresh_registries(monkeypatch):
    for name in ("EXOTIC_PENDING", "CONFIRMED_PENDING", "ROUTED_HANDOFFS"):
        monkeypatch.setattr(L, name, L.PendingLegs(), raising=False)
    monkeypatch.setattr(O, "_OUTAGE", {"down": False}, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_FAST_LANE_ONLY", True)


def _conn():
    return conn_with([(SIG, ARMED_UID, 5)])


def _row(conn, *, terminal=None, pending=None, reason=None, tx=None):
    started = NOW.replace(second=0).isoformat()
    conn.execute("INSERT INTO memecoin_entry_attempts (attempt_id, origin_event_id, signal_source, "
                 "decision_started_at, terminal_status, terminal_reason) VALUES (?,?,?,?,?,?)",
                 ("slot-" + SIG, "slotlane:" + SIG, "slot_lane", started, terminal, reason))
    if pending is not None:
        conn.execute("INSERT INTO memecoin_pending_entries (attempt_id, status, resolution_reason,"
                     " tx_sig) VALUES (?,?,?,?)", ("slot-" + SIG, pending, reason, tx))


@pytest.mark.parametrize("terminal,pending,reason,tx,expected", [
    ("failed", "failed", "chain_error:BuySlippageBelowMinTokensOut", "T", ("decided", None)),
    ("failed", "failed", O.PUMP_GRADUATION_REASON, "T", ("graduated", "landed_fail")),
    ("aborted", "failed", "slot_lane_build:leg_over_clip", None, ("failed", "build_refused")),
    ("failed", "failed", "chain_error:unsent", None, ("failed", None)),
    ("rejected", None, L.EXOTIC_PENDING_REASON, None, ("failed", "exotic_released")),
    ("rejected", None, "trial_unmarked: slot lane carries a trial only SOL", None, ("failed", None)),
    ("rejected", None, L.SCALED_CLIP_REASON, None, ("failed", None)),
    ("source_initiation_unknown", None, "missing_receipt_proof", None, ("failed", None)),
    ("rejected", None, "slot_lane_signal_too_old", None, ("decided", None)),
    (None, "reserved", None, None, ("in_flight", None)),
    ("filled", "confirmed", None, "T", ("filled", None)),
])
def test_each_slot_state_maps_to_one_outcome_and_class(terminal, pending, reason, tx, expected):
    conn = _conn()
    _row(conn, terminal=terminal, pending=pending, reason=reason, tx=tx)
    assert O.slot_lane_verdict(conn, SIG, DETECTED) == expected
    assert O.slot_lane_outcome(conn, SIG, DETECTED) == expected[0]


def test_no_row_classes_come_only_from_positive_in_process_markers():
    conn = _conn()
    assert O.slot_lane_verdict(conn, SIG, DETECTED) == (None, None)          # absence: no class
    L.ROUTED_HANDOFFS.mark(SIG, time.monotonic())
    assert O.slot_lane_verdict(conn, SIG, DETECTED) == (None, "routed_handoff")
    L.EXOTIC_PENDING.add(SimpleNamespace(signature=SIG), deadline=time.monotonic() + 30)
    assert O.slot_lane_verdict(conn, SIG, DETECTED) == ("in_flight", None)    # a hold wins
    L.EXOTIC_PENDING.claim(SIG)
    assert O.slot_lane_verdict(conn, SIG, DETECTED) == ("failed", "exotic_released")


def test_an_unreadable_slot_state_is_never_none():
    conn = sqlite3.connect(":memory:")                                       # no attempt tables
    assert O.slot_lane_verdict(conn, SIG, DETECTED) == ("unreadable", None)
    assert O.slot_lane_outcome(conn, SIG, DETECTED) == "unreadable"


def test_the_code_default_is_the_approved_class_list():
    # graduation_released (2026-10-07, Alpha GO on the graduation direct design): a pool watch that
    # ended with no attempt row gets today's one graduation fallback.
    # lane_down (2026-10-07, Alpha GO): the runner's own restart blackout (test_onchain_lane_down.py).
    # slot_shape_unsupported (2026-10-14, Alpha GO): a multi_own_buy frame the lane rejected unsent.
    assert cfg.MEMECOIN_SOLANA_FALLBACK_CLASSES == frozenset(
        {"landed_fail", "routed_handoff", "exotic_released", "build_refused", "graduation_released",
         "lane_down", "slot_shape_unsupported"})
    assert cfg.parse_solana_fallback_classes("landed_fail, bogus") == frozenset({"landed_fail"})


def test_a_routed_buy_with_no_live_slot_venue_is_marked_handed_off():
    from test_slot_lane_exotic_live import PEPE_EV, _Routed, _routed
    routed = _routed(PEPE_EV)
    L.route_routed_buy(routed, handoff=_Routed(), log=lambda line: None, put=[].append,
                       amm_pools=None, cpmm_on=False, pump_on=False, lane_live=True)
    assert L.ROUTED_HANDOFFS.released(routed.signature, time.monotonic())


def test_a_shadow_lane_handoff_is_not_marked():
    from test_slot_lane_exotic_live import PEPE_EV, _Routed, _routed
    routed = _routed(PEPE_EV)
    L.route_routed_buy(routed, handoff=None, log=lambda line: None, put=[].append,
                       amm_pools=None, cpmm_on=False, pump_on=False, lane_live=False)
    assert not L.ROUTED_HANDOFFS.released(routed.signature, time.monotonic())


def _sweep(conn, seen, deferred=None, cursor=0):
    def hook(_c, ev):
        seen.append(ev)
        return {"status": "refused"}
    return O.sweep_onchain_detections(conn, hook, cursor=cursor, now_fn=now_iso, max_age_s=45,
                                      approved_ids_fn=approved,
                                      deferred={} if deferred is None else deferred)


@pytest.mark.parametrize("setup,admitted", [
    (lambda c: _row(c, terminal="failed", pending="failed", reason="chain_error:x", tx="T"), True),
    (lambda c: _row(c, terminal="aborted", pending="failed", reason="slot_lane_build:leg_over_clip"), True),
    (lambda c: _row(c, terminal="rejected", reason=L.EXOTIC_PENDING_REASON), True),
    (lambda c: L.ROUTED_HANDOFFS.mark(SIG, time.monotonic()), True),
    (lambda c: _row(c, terminal="rejected", reason="trial_unmarked: x"), False),
    (lambda c: _row(c, terminal="rejected", reason=L.SCALED_CLIP_REASON), False),
    (lambda c: _row(c, terminal="source_initiation_unknown", reason="missing_receipt_proof"), False),
    (lambda c: None, False),                                                  # never saw it
])
def test_only_a_mechanical_slot_failure_admits_the_ordinary_attempt(setup, admitted):
    conn = _conn()
    setup(conn)
    seen = []
    _sweep(conn, seen)
    assert len(seen) == 1
    assert seen[0].native_failure_fallback is admitted


def test_the_class_knob_narrows_admission(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_FALLBACK_CLASSES", frozenset({"landed_fail"}))
    conn = _conn()
    L.ROUTED_HANDOFFS.mark(SIG, time.monotonic())
    seen = []
    _sweep(conn, seen)
    assert seen[0].native_failure_fallback is False
    assert seen[0].slot_fallback_class == "routed_handoff"


@pytest.mark.parametrize("fast_only", [True, False])
@pytest.mark.parametrize("in_flight", ["reserved", "no_terminal", "confirmed_pending", "exotic_hold"])
def test_an_in_flight_or_ambiguous_slot_send_never_reaches_the_hook(monkeypatch, fast_only, in_flight):
    """THE DOUBLE-BUY GUARD. Even with a routed-handoff marker (the most permissive class), a slot
    send that may still land holds the detection: the ordinary hook, and so its reservation,
    never runs."""
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_FAST_LANE_ONLY", fast_only)
    conn = _conn()
    L.ROUTED_HANDOFFS.mark(SIG, time.monotonic())
    if in_flight == "reserved":
        _row(conn, pending="reserved")
    elif in_flight == "no_terminal":
        _row(conn)
    elif in_flight == "confirmed_pending":
        L.CONFIRMED_PENDING.add(SimpleNamespace(signature=SIG), deadline=float("inf"))
    else:
        L.EXOTIC_PENDING.add(SimpleNamespace(signature=SIG), deadline=time.monotonic() + 30)
    seen, deferred = [], {}
    _cursor, results = _sweep(conn, seen, deferred)
    assert seen == []
    assert results[0]["result"]["status"] == "deferred" and len(deferred) == 1


def test_an_unreadable_slot_state_refuses_and_pages_once_per_outage(monkeypatch):
    pages = []
    monkeypatch.setattr(O, "_page_slot_lane_outage", pages.append)
    conn = conn_with([(SIG, ARMED_UID, 5), ("sig2", ARMED_UID, 4)], slot_tables=False)
    seen, deferred = [], {}
    cursor, results = _sweep(conn, seen, deferred)
    assert seen == []
    assert [(r["result"]["status"], r["result"]["reason"]) for r in results] == [
        ("deferred", "slot_lane_state_unreadable")] * 2
    assert len(pages) == 1 and len(deferred) == 2        # refused, NOT consumed (Gamma LOW-2)
    _sweep(conn, seen, deferred, cursor)                  # still unreadable: held, no new page
    assert seen == [] and len(pages) == 1
    conn.executescript(SLOT_SCHEMA)                       # readable again: the held rows decide
    _sweep(conn, seen, deferred, cursor)
    assert len(seen) == 2 and not any(e.native_failure_fallback for e in seen)
    assert len(pages) == 1 and O._OUTAGE["down"] is False


def test_the_routed_tail_never_puts_a_live_buy_it_hands_off(monkeypatch):
    """Gamma LOW-1: a pool learned between route_routed_buy's two conversions made the tail put a
    LIVE buy on a buy it also marked handed off: a native send plus an ordinary fallback."""
    from test_slot_lane_exotic_live import PEPE_EV, _Routed, _routed
    live = SimpleNamespace(signature="live-tail", venue="raydium_cpmm")
    monkeypatch.setattr(L, "shadow_only", lambda buy: buy is not live)
    calls = []
    monkeypatch.setattr(L, "cpmm_slot_buy", lambda r: calls.append(1) or (None if len(calls) == 1 else live))
    put = []
    L.route_routed_buy(_routed(PEPE_EV), handoff=_Routed(), log=lambda line: None, put=put.append,
                       amm_pools=None, cpmm_on=True, pump_on=False, lane_live=True)
    assert live not in put


@pytest.fixture
def skip_memory(monkeypatch):
    from sniper.services import memecoin_slot_skips as skips
    monkeypatch.setattr(skips, "_LAST", collections.OrderedDict())
    return skips


@pytest.mark.parametrize("notes,expected", [
    ([("rejected", "multi_own_buy:pump_amm+pump_curve")], ("failed", "slot_shape_unsupported")),
    ([("rejected", "multi_own_buy:2xpump_amm")], ("failed", "slot_shape_unsupported")),
    ([("rejected", "mcap $636705 over ceiling $250000")], (None, None)),      # policy: no class
    ([("rejected", "kill_switch")], (None, None)),
    # The lane's latest word replaces the shape refusal (a later routed hand-off or send).
    ([("rejected", "multi_own_buy:2xpump_amm"), ("submitted", None)], (None, None)),
])
def test_a_multi_own_buy_rejection_is_the_shape_class_only_while_it_is_the_lanes_latest_word(
        skip_memory, notes, expected):
    conn = _conn()
    for outcome, reason in notes:
        skip_memory.note(SIG, outcome, reason)
    assert O.slot_lane_verdict(conn, SIG, DETECTED) == expected


def test_a_multi_own_buy_with_a_slot_attempt_row_is_judged_by_that_row(skip_memory):
    """Never sent is the premise: a slot attempt that may still land holds the detection."""
    conn = _conn()
    skip_memory.note(SIG, "rejected", "multi_own_buy:2xpump_amm")
    _row(conn, pending="reserved")
    assert O.slot_lane_verdict(conn, SIG, DETECTED) == ("in_flight", None)


def test_a_multi_own_buy_row_now_reaches_the_hook_as_the_one_fallback(skip_memory):
    """Gamma's current-code losses, 10-11..10-14: 7 of 14 were multi_own_buy refused blind by
    fast-lane-only (six a bigwarzeth Xsc9-curve burst). The ordinary path now judges it once."""
    conn = _conn()
    skip_memory.note(SIG, "rejected", "multi_own_buy:pump_amm+pump_curve")
    seen, deferred = [], {}
    cursor, results = _sweep(conn, seen, deferred)
    assert len(seen) == 1 and seen[0].native_failure_fallback is True
    assert seen[0].slot_fallback_class == "slot_shape_unsupported"
    assert seen[0].delivery_lane == O.FALLBACK_LANE
