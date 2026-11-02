"""A native fast lane that let go UNSIGNED never blocks the ordinary lane (Alpha 2026-10-14, after the
Base lane's first live minutes): a pre-send abort released its reservation with the lane's own reason
and spent nothing, so the on-chain lane must route the same transaction to the hook, never
duplicate_of_feed. On 8453 and 4663 (rh_fast_lane rows) and on 56 next to float_short. Anything that
may have reached a node stays "attempted"."""
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

from scripts.research.onchain_wallet_watch import SCHEMA
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_base_fast_lane as base
from sniper.services import memecoin_onchain_lane as lane
from sniper.services import memecoin_rh_fast_lane as rh

NOW = datetime(2026, 10, 14, 11, 7, 0, tzinfo=timezone.utc)
ARRIVAL = (NOW - timedelta(seconds=0.1)).isoformat()
TX = "0x" + "5a" * 32
MINT = "0x" + "6b" * 20
WALLET = "0x" + "7c" * 20
UID = "degen_ak"


@pytest.fixture
def ledger():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    m.ensure_memecoin_tables(conn)
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def no_hold(monkeypatch):
    monkeypatch.setattr(rh, "LANE_HOLDS", rh.LaneHolds(bound_s=1.0, clock=lambda: 0.0, wake_fn=lambda: None))


def _lane_abort(conn, chain, *, signal_source, delivery_lane, origin, release, terminal="aborted",
                reason="nonce_in_flight", **pending_fields):
    """The lane's attempt row as its trace writes it, and its reservation released as the lane does."""
    pid = m.reserve_pending(conn, mint=MINT, source_user_id=UID, strategy="alpha_b", notional=50.0,
                            reserved_at=ARRIVAL, network_id=chain, origin_event_id=origin,
                            attempt_id="lane", signal_source=signal_source)
    conn.execute("INSERT INTO memecoin_entry_attempts(attempt_id, origin_event_id, network_id, mint, "
                 "source_user_id, signal_source, delivery_lane, decision_started_at, terminal_status, "
                 "terminal_reason, completed_at, pending_id, trace_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("lane", origin, chain, MINT, UID, signal_source, delivery_lane, ARRIVAL, terminal, reason,
                  NOW.isoformat(), pid, json.dumps({"stages": {}, "substage_events": [],
                                                    "submission_events": []})))
    for field, value in pending_fields.items():
        conn.execute("UPDATE memecoin_pending_entries SET %s=? WHERE id=?" % field, (value, pid))
    if release:
        assert m.release_pending(conn, pid, release)
    return pid


def _detection(conn, chain):
    conn.execute("INSERT INTO onchain_wallet_detections(signature,source_user_id,handle,address,network_id,"
                 "mint,source_slot,source_size_usd,source_price,detected_at,created_at,source_valuation_json) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("%s:%s:%s" % (chain, TX, MINT), UID, UID, WALLET, chain, MINT, 16, 100.0, 1e-6,
                  ARRIVAL, ARRIVAL, json.dumps({})))


def _sweep(conn, chain):
    calls = []
    _cursor, results = lane.sweep_onchain_detections(
        conn, lambda c, ev: calls.append(ev) or {"status": "rejected", "reason": "ordinary verdict"},
        cursor=0, now_fn=lambda: NOW.isoformat(), max_age_s=45, network_id=chain, deferred={},
        approved_ids_fn=lambda _conn, net: {UID} if net == chain else set())
    return calls, results


NATIVE = [("8453", "base_fast_lane", "base_fast_"), ("4663", "rh_fast_lane", "rh_fast_")]


@pytest.mark.parametrize("chain,delivery,prefix", NATIVE)
def test_a_nonce_in_flight_abort_hands_the_same_buy_to_the_ordinary_lane(ledger, chain, delivery, prefix):
    origin = "rhfast:%s:%s:%s" % (chain, TX, MINT)
    _lane_abort(ledger, chain, signal_source="rh_fast_lane", delivery_lane=delivery, origin=origin,
                release=prefix + "nonce_in_flight")
    _detection(ledger, chain)
    calls, results = _sweep(ledger, chain)
    assert len(calls) == 1 and calls[0].fomo_event_id == "onchain:%s:%s:%s" % (chain, TX, MINT)
    assert results[0]["result"]["reason"] == "ordinary verdict"


@pytest.mark.parametrize("chain,delivery,prefix", NATIVE)
@pytest.mark.parametrize("case", ["bound", "signed", "nonce", "hook_refusal", "foreign_release", "unreleased"])
def test_anything_that_may_have_reached_a_node_or_was_a_verdict_stays_attempted(ledger, chain, delivery,
                                                                                 prefix, case):
    origin = "rhfast:%s:%s:%s" % (chain, TX, MINT)
    kw = dict(signal_source="rh_fast_lane", delivery_lane=delivery, origin=origin,
              release=prefix + "nonce_in_flight")
    if case == "bound":
        kw["submission_bound_at"] = ARRIVAL
    elif case == "signed":
        kw["tx_sig"] = "0x" + "11" * 32
    elif case == "nonce":
        kw["nonce"] = 7
    elif case == "hook_refusal":
        kw.update(terminal="rejected", reason="mcap over ceiling")
    elif case == "foreign_release":
        kw["release"] = "stale_reservation"
    elif case == "unreleased":
        kw["release"] = None
    _lane_abort(ledger, chain, **kw)
    _detection(ledger, chain)
    calls, results = _sweep(ledger, chain)
    assert calls == [] and results[0]["result"]["reason"] == "duplicate_of_feed", case


def test_56_releases_any_unsigned_lane_abort_next_to_float_short(ledger):
    signature = "56:%s:%s" % (TX, MINT)
    _lane_abort(ledger, "56", signal_source="bsc_fast_lane", delivery_lane="bsc_fast_lane",
                origin="bscfast:" + signature, release="bsc_fast_lock_busy",
                reason="wallet execution lock busy: today's path covers it")
    assert lane._other_lane_attempt_state(ledger, signature, ARRIVAL, source_user_id=UID,
                                          include_onchain=True) == "lane_released_unsigned"
    assert not lane.already_attempted_from_another_lane(ledger, signature, ARRIVAL, source_user_id=UID)


def test_the_real_base_lane_abort_leaves_exactly_the_row_the_ordinary_lane_releases(ledger, tmp_path,
                                                                                     monkeypatch):
    """The reservation release comes from the lane's own executor, not a hand-made row: no nonce from
    the floor -> nonce_in_flight, released 'base_fast_nonce_in_flight', nothing signed or bound."""
    from sniper.settings import memecoin as cfg
    from test_memecoin_base_fast_lane_datagram import SNAP, Chain, _frame, _hook, _live
    monkeypatch.setenv("MEMECOIN_BASE_FAST_LANE", "live")
    monkeypatch.setattr(cfg, "MEMECOIN_BASE_FAST_ROUTES", ("v4_native",))
    monkeypatch.setattr(cfg, "MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS", ("8453",))
    sent = []
    row, tx, receipt = _frame(preconf_read=False)
    _hook(sent)(row, tx, receipt, 2488.29, 1.0, "cache")
    payload = dict(sent[0], snapshot=SNAP, eth_usd_at=1000.0, tx_hash=TX, mint=MINT)
    live = _live(ledger, None, Chain(), tmp_path)
    live.ctx["lane_claim_nonce_fn"] = lambda pending: None     # today's 8453 seam answered None
    origin = "rhfast:8453:%s:%s" % (TX, MINT)
    pid = m.reserve_pending(ledger, mint=MINT, source_user_id=UID, strategy="alpha_b", notional=50.0,
                            reserved_at=ARRIVAL, network_id="8453", origin_event_id=origin,
                            attempt_id="lane", signal_source="rh_fast_lane")
    result = live.execute_reserved(payload, {"_perf0": time.perf_counter()}, ledger, pid, 50.0, decimals=18)
    assert result["status"] == "aborted" and result["reason"] == "nonce_in_flight"
    ledger.execute("INSERT INTO memecoin_entry_attempts(attempt_id, origin_event_id, network_id, mint, "
                   "source_user_id, signal_source, delivery_lane, decision_started_at, terminal_status, "
                   "terminal_reason, completed_at, pending_id, trace_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("lane", origin, "8453", MINT, UID, "rh_fast_lane", "base_fast_lane", ARRIVAL,
                    result["status"], result["reason"], NOW.isoformat(), pid,
                    json.dumps({"stages": {}, "substage_events": [], "submission_events": []})))
    assert m.pending_row(ledger, pid)["resolution_reason"] == "base_fast_nonce_in_flight"
    _detection(ledger, "8453")
    calls, _results = _sweep(ledger, "8453")
    assert len(calls) == 1


def _sweep_at(conn, chain, calls, *, cursor, deferred):
    return lane.sweep_onchain_detections(
        conn, lambda c, ev: calls.append(ev) or {"status": "rejected", "reason": "ordinary verdict"},
        cursor=cursor, now_fn=lambda: NOW.isoformat(), max_age_s=45, network_id=chain, deferred=deferred,
        approved_ids_fn=lambda _conn, net: {UID} if net == chain else set())


def _in_flight(conn, chain, delivery, **pending_fields):
    """The lane mid-decision: its trace row written (terminal NULL, pending not yet linked) and its
    reservation held, as between the hook's reserve and the executor's verdict."""
    origin = "rhfast:%s:%s:%s" % (chain, TX, MINT)
    pid = m.reserve_pending(conn, mint=MINT, source_user_id=UID, strategy="alpha_b", notional=50.0,
                            reserved_at=ARRIVAL, network_id=chain, origin_event_id=origin,
                            attempt_id="lane", signal_source="rh_fast_lane")
    conn.execute("INSERT INTO memecoin_entry_attempts(attempt_id, origin_event_id, network_id, mint, "
                 "source_user_id, signal_source, delivery_lane, decision_started_at, trace_json) "
                 "VALUES (?,?,?,?,?,?,?,?,?)", ("lane", origin, chain, MINT, UID, "rh_fast_lane", delivery,
                                                ARRIVAL, json.dumps({"stages": {}, "submission_events": []})))
    for field, value in pending_fields.items():
        conn.execute("UPDATE memecoin_pending_entries SET %s=? WHERE id=?" % field, (value, pid))
    return pid


@pytest.mark.parametrize("chain,delivery,prefix", NATIVE)
@pytest.mark.parametrize("held", [True, False])
def test_a_sweep_during_the_lane_s_flight_waits_then_judges_after_its_unsigned_abort(ledger, chain, delivery,
                                                                                     prefix, held):
    """Gamma's BLOCK on d59e3d2c2: the first sweep met the in-flight row as "attempted", moved its
    cursor past the detection, and after the lane's unsigned abort nobody judged the buy."""
    if held:
        rh.LANE_HOLDS.hold(TX, MINT)
    pid = _in_flight(ledger, chain, delivery)
    _detection(ledger, chain)
    calls, deferred = [], {}
    cursor, results = _sweep_at(ledger, chain, calls, cursor=0, deferred=deferred)
    assert calls == [] and results[0]["result"]["reason"] == "rh_fast_lane_in_flight" and len(deferred) == 1
    cursor, results = _sweep_at(ledger, chain, calls, cursor=cursor, deferred=deferred)
    assert calls == [] and results == [] and len(deferred) == 1          # still deciding: still waiting
    assert m.release_pending(ledger, pid, prefix + "nonce_in_flight")    # the lane let go unsigned
    ledger.execute("UPDATE memecoin_entry_attempts SET terminal_status='aborted', "
                   "terminal_reason='nonce_in_flight', completed_at=?, pending_id=?", (NOW.isoformat(), pid))
    if held:
        rh.LANE_HOLDS.release(TX, MINT)
    cursor, results = _sweep_at(ledger, chain, calls, cursor=cursor, deferred=deferred)
    assert len(calls) == 1 and results[0]["result"]["reason"] == "ordinary verdict" and not deferred


@pytest.mark.parametrize("chain,delivery,prefix", NATIVE)
@pytest.mark.parametrize("field,value", [("submission_bound_at", ARRIVAL), ("tx_sig", "0x" + "22" * 32),
                                         ("nonce", 9)])
def test_an_in_flight_lane_that_bound_or_signed_its_bytes_stays_attempted(ledger, chain, delivery, prefix,
                                                                          field, value):
    _in_flight(ledger, chain, delivery, **{field: value})
    _detection(ledger, chain)
    calls, deferred = [], {}
    _cursor, results = _sweep_at(ledger, chain, calls, cursor=0, deferred=deferred)
    assert calls == [] and results[0]["result"]["reason"] == "duplicate_of_feed" and not deferred


@pytest.mark.parametrize("chain,delivery,prefix", NATIVE)
def test_the_legacy_guard_counts_an_in_flight_lane_as_attempted(ledger, chain, delivery, prefix):
    """Gamma LOW 1: queued on-chain retries rely on the boolean guard; a lane still deciding owns it."""
    _in_flight(ledger, chain, delivery)
    signature = "%s:%s:%s" % (chain, TX, MINT)
    assert lane._other_lane_attempt_state(ledger, signature, ARRIVAL, source_user_id=UID) == "rh_lane_pending"
    assert lane.already_attempted_from_another_lane(ledger, signature, ARRIVAL, source_user_id=UID)


@pytest.mark.parametrize("chain,delivery,prefix", NATIVE)
def test_a_wait_on_the_native_lane_that_outlives_the_detection_expires_by_its_own_name(ledger, chain,
                                                                                    delivery, prefix):
    """Gamma LOW 2: a lane stuck pre-send past the detection's freshness, named apart from the slot
    lane's fallback."""
    _in_flight(ledger, chain, delivery)
    _detection(ledger, chain)
    calls, deferred = [], {}
    cursor, results = _sweep_at(ledger, chain, calls, cursor=0, deferred=deferred)
    assert results[0]["result"]["reason"] == "rh_fast_lane_in_flight"
    _cursor, results = lane.sweep_onchain_detections(
        ledger, lambda c, ev: calls.append(ev) or {}, cursor=cursor,
        now_fn=lambda: (NOW + timedelta(seconds=46)).isoformat(), max_age_s=45, network_id=chain,
        deferred=deferred, approved_ids_fn=lambda _conn, net: {UID} if net == chain else set())
    assert calls == [] and not deferred
    assert results[0]["result"] == {"status": "skipped", "reason": "rh_fast_lane_wait_expired"}
