"""The restart blackout (Alpha GO 2026-10-07; operator-flagged loss EELDREW, ResellCalendar, ref
67b8832d). The deploy stopped the old runner at 20:15:48.8Z, the source bought at 20:15:58Z (the
watcher, a separate process, detected it at 20:15:58.20Z) and the new runner came up at 20:16:18Z.
The on-chain lane took the queued detection at 20:16:18.570 and refused native_fast_lane_unavailable:
fast-lane-only admits only a MECHANICAL slot-lane failure, and the slot lane never saw a buy that
happened while it was down.

A detection whose arrival falls inside the runner's own downtime gets the fallback class `lane_down`:
START is the previous runner's last heartbeat (another `started_at`), END is the moment the new slot
lane subscribed to THAT owner (its own logsSubscribe ack; Gamma MED: an owner acked earlier is already
traded, so a later buy of its own is the slot lane's), capped at process start + 120 s. The detection's
watcher time is AFTER the processed frame by at most 2.65 s (n=1089), so it must sit 3 s past START.
Only with no slot attempt at all (verdict (None, None)); every ordinary gate still runs."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from sniper.services import memecoin_onchain_lane as O, memecoin_slot_lane as L
from sniper.settings import memecoin as cfg
from test_memecoin_onchain_lane import ARMED_UID, NOW, approved, conn_with, now_iso


def _epoch(iso):
    return datetime.fromisoformat(iso).timestamp()


EELDREW = dict(heartbeat_ts=_epoch("2026-10-07T20:15:46.512000+00:00"),
               old_started_at="2026-10-07T20:12:20.104000+00:00",
               stop="2026-10-07T20:15:48.800000+00:00",
               new_started_at="2026-10-07T20:16:17.900000+00:00",
               detected_at="2026-10-07T20:15:58.200182+00:00",
               ready="2026-10-07T20:16:19.200000+00:00")
OWNER = "ResellCalendarOwner1111111111111111111111111"
OTHER = "AnotherOwner11111111111111111111111111111111"


def _window(*, heartbeat_ts=EELDREW["heartbeat_ts"], old=EELDREW["old_started_at"],
            new=EELDREW["new_started_at"], ready=None, owner=OWNER):
    w = O.LaneDownWindow()
    beat = None if heartbeat_ts is None else {"ts": heartbeat_ts, "started_at": old}
    w.open(beat, started_at=new, now=_epoch(new))
    if ready is not None:
        w.owner_acked(owner, _epoch(ready))
    return w


def _row(detected_at, network_id="solana", address=OWNER):
    return {"signature": "ntDenkpz", "network_id": network_id, "detected_at": detected_at,
            "address": address}


# --- the window ------------------------------------------------------------------------------------

@pytest.mark.parametrize("ready", [None, EELDREW["ready"]], ids=["lane_not_ready_yet", "lane_ready_after"])
def test_eeldrew_falls_inside_the_restart_blackout(ready):
    assert _window(ready=ready).covers(_row(EELDREW["detected_at"]))


def test_an_arrival_too_close_to_the_last_heartbeat_is_not_covered():
    start = EELDREW["heartbeat_ts"]
    w = _window()
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()      # noqa: E731
    assert not w.covers(_row(iso(start + 2.9)))          # the frame may predate the old lane's end
    assert w.covers(_row(iso(start + 3.1)))


def test_an_arrival_after_its_owner_was_subscribed_is_not_covered():
    w = _window(ready=EELDREW["ready"])
    ready = _epoch(EELDREW["ready"])
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()      # noqa: E731
    assert w.covers(_row(iso(ready - 0.1)))
    assert not w.covers(_row(iso(ready)))
    assert not w.covers(_row(iso(ready + 0.5)))


def test_a_lane_that_never_comes_up_is_covered_only_for_120_seconds():
    w = _window()
    new = _epoch(EELDREW["new_started_at"])
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()      # noqa: E731
    assert w.covers(_row(iso(new + 119.0)))
    assert not w.covers(_row(iso(new + O.LANE_DOWN_MAX_S)))
    late = _window(ready=datetime.fromtimestamp(new + 500.0, timezone.utc).isoformat())
    assert not late.covers(_row(iso(new + 200.0)))       # a late ack never stretches the cap


def test_an_owner_acked_before_the_buy_is_not_covered_while_others_are_still_unacked():
    """Gamma MED (Alpha: per-owner END). The slot lane trades OWNER's frames from OWNER's own ack, so
    OWNER's later buy is the slot lane's (seen, or refused before any row), never lane_down; OTHER,
    not yet acked, is still blind."""
    w = _window(ready=EELDREW["ready"], owner=OWNER)
    later = datetime.fromtimestamp(_epoch(EELDREW["ready"]) + 0.4, timezone.utc).isoformat()
    assert not w.covers(_row(later, address=OWNER))
    assert w.covers(_row(later, address=OTHER))
    w.owner_acked(OTHER, _epoch(EELDREW["ready"]) + 1.0)
    assert w.covers(_row(later, address=OTHER))          # its own ack came after this buy
    w.owner_acked(OTHER, _epoch(EELDREW["ready"]) - 5.0)   # a later re-ack never moves the first
    assert w.covers(_row(later, address=OTHER))


@pytest.mark.parametrize("case", ["no_heartbeat", "own_process", "no_ts", "future_ts", "bad_ts"])
def test_no_known_start_covers_nothing(case):
    beat = {"ts": EELDREW["heartbeat_ts"], "started_at": EELDREW["old_started_at"]}
    if case == "no_heartbeat":
        beat = None
    elif case == "own_process":
        beat["started_at"] = EELDREW["new_started_at"]
    elif case == "no_ts":
        beat.pop("ts")
    elif case == "future_ts":
        beat["ts"] = _epoch(EELDREW["new_started_at"]) + 60.0
    else:
        beat["ts"] = "soon"
    w = O.LaneDownWindow()
    line = w.open(beat, started_at=EELDREW["new_started_at"], now=_epoch(EELDREW["new_started_at"]))
    assert "lane_down window: none" in line
    assert not w.covers(_row(EELDREW["detected_at"]))


def test_the_startup_and_ready_lines_name_the_window():
    w = O.LaneDownWindow()
    line = w.open({"ts": EELDREW["heartbeat_ts"], "started_at": EELDREW["old_started_at"]},
                  started_at=EELDREW["new_started_at"], now=_epoch(EELDREW["new_started_at"]))
    assert "START=2026-10-07T20:15:46.512" in line and "cap=2026-10-07T20:18:17.9" in line
    w.owner_acked(OWNER, _epoch(EELDREW["ready"]) - 0.7)
    ready = w.slot_lane_ready(_epoch(EELDREW["ready"]), owners=97)
    assert "END=2026-10-07T20:16:19.2" in ready and "owners=97" in ready
    assert "first ack 2026-10-07T20:16:18.5" in ready
    assert w.slot_lane_ready(_epoch(EELDREW["ready"]) + 30.0, owners=97) is None   # first time only
    assert w.ready == _epoch(EELDREW["ready"])


@pytest.mark.parametrize("row", [_row(EELDREW["detected_at"], network_id="4663"),
                                 _row("not a time"), _row(None)], ids=["evm", "garbage", "none"])
def test_other_chains_and_unreadable_times_are_never_covered(row):
    assert not _window().covers(row)


def test_the_class_is_on_by_default():
    assert "lane_down" in cfg.SOLANA_FALLBACK_CLASS_NAMES
    assert "lane_down" in cfg.MEMECOIN_SOLANA_FALLBACK_CLASSES


# --- the sweep: only an unseen buy, every gate still runs -----------------------------------------------

SIG = "sigDown"


@pytest.fixture
def fast_only(monkeypatch):
    for name in ("EXOTIC_PENDING", "CONFIRMED_PENDING", "ROUTED_HANDOFFS"):
        monkeypatch.setattr(L, name, L.PendingLegs(), raising=False)
    monkeypatch.setattr(O, "_OUTAGE", {"down": False}, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_FAST_LANE_ONLY", True)
    # The runner stopped 30 s before NOW and the detection (5 s old) arrived while it was down.
    w = O.LaneDownWindow()
    w.open({"ts": (NOW - timedelta(seconds=30)).timestamp(), "started_at": "old"},
           started_at=NOW.isoformat(), now=NOW.timestamp())
    monkeypatch.setattr(O, "LANE_DOWN", w)
    return w


def _sweep(conn):
    seen = []

    def hook(_c, ev):
        seen.append(ev)
        return {"status": "refused"}
    O.sweep_onchain_detections(conn, hook, cursor=0, now_fn=now_iso, max_age_s=45,
                               approved_ids_fn=approved, deferred={})
    return seen


def test_an_unseen_buy_in_the_blackout_gets_one_ordinary_attempt(fast_only):
    (ev,) = _sweep(conn_with([(SIG, ARMED_UID, 5)]))
    assert ev.native_failure_fallback is True and ev.slot_fallback_class == "lane_down"
    assert ev.delivery_lane == "onchain_lane_down"


def test_the_class_knob_can_drop_it(fast_only, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_FALLBACK_CLASSES", frozenset({"landed_fail"}))
    (ev,) = _sweep(conn_with([(SIG, ARMED_UID, 5)]))
    assert ev.native_failure_fallback is False and ev.slot_fallback_class == "lane_down"


def test_outside_the_blackout_fast_lane_only_stands(fast_only):
    fast_only.owner_acked("addr", (NOW - timedelta(seconds=10)).timestamp())   # conn_with's address
    (ev,) = _sweep(conn_with([(SIG, ARMED_UID, 5)]))       # arrived after its owner was subscribed
    assert ev.native_failure_fallback is False and ev.slot_fallback_class is None
    assert getattr(ev, "delivery_lane", None) != "onchain_lane_down"


@pytest.mark.parametrize("state", ["decided", "source_initiation_unknown", "routed_handoff"])
def test_any_slot_lane_trace_of_the_buy_wins_over_the_blackout(fast_only, state):
    """No double entry and no override: an attempt row (a decision, a proof refusal) or an in-process
    marker means the slot lane saw this transaction, so its own verdict and class stand."""
    conn = conn_with([(SIG, ARMED_UID, 5)])
    started = NOW.replace(second=0).isoformat()
    if state == "routed_handoff":
        import time
        L.ROUTED_HANDOFFS.mark(SIG, time.monotonic())
    else:
        terminal, reason = (("rejected", "mcap $1 over ceiling $0") if state == "decided"
                            else ("source_initiation_unknown", "missing_receipt_proof"))
        conn.execute("INSERT INTO memecoin_entry_attempts (attempt_id, origin_event_id, signal_source, "
                     "decision_started_at, terminal_status, terminal_reason) VALUES (?,?,?,?,?,?)",
                     ("slot-" + SIG, "slotlane:" + SIG, "slot_lane", started, terminal, reason))
    seen = _sweep(conn)
    assert all(getattr(ev, "slot_fallback_class", None) != "lane_down" for ev in seen)
    if state == "decided":
        assert seen == []                                    # duplicate_of_slot_lane, as today


def test_an_in_flight_slot_send_still_holds_the_detection(fast_only):
    L.CONFIRMED_PENDING.add(SimpleNamespace(signature=SIG), deadline=float("inf"))
    assert _sweep(conn_with([(SIG, ARMED_UID, 5)])) == []


# --- END comes from the slot signal socket: every owner's subscription answered ---------------------

def test_the_signal_socket_marks_the_lane_ready_once_every_owner_is_answered(monkeypatch):
    import threading
    from sniper.services import memecoin_slot_signal as s
    from test_memecoin_slot_signal import CURVE_BUYER, CURVE_SELLER, _FakeWS
    w = O.LaneDownWindow()
    w.open({"ts": 1.0, "started_at": "old"}, started_at="new", now=2.0)
    monkeypatch.setattr(O, "LANE_DOWN", w)
    monkeypatch.setattr(s, "ROSTER_REFRESH_S", 0.0)
    stop = threading.Event()
    ws = _FakeWS(stop)
    before = __import__("time").time()
    thread = s.start_signal_thread(lambda ev: None, status={}, ws_url="wss://x", stop=stop,
                                   connect=lambda url: ws,
                                   owners_fn=lambda: {CURVE_BUYER: ("uid-b", "b"), CURVE_SELLER: ("uid-s", "s")})
    thread.join(timeout=10)
    assert w.ready is not None and w.ready >= before
    assert set(w.acks) == {CURVE_BUYER, CURVE_SELLER} and all(before <= t <= w.ready for t in w.acks.values())
    assert w.slot_lane_ready(w.ready + 5.0) is None            # logged once
