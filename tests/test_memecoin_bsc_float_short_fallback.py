"""An exact unsigned native-float refusal must leave the ordinary WBNB entry available."""
import json
import sqlite3
import threading
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

import pytest

from scripts.research.onchain_wallet_watch import SCHEMA
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_onchain_lane as lane

NOW = datetime(2026, 10, 11, 4, 14, 16, 500000, tzinfo=timezone.utc)
ARRIVAL = (NOW - timedelta(seconds=0.1)).isoformat()
TX = "0x" + "46" * 32
MINT = "0x" + "12" * 20
WALLET = "0x" + "34" * 20
UID = "badabeepp"
SIGNATURE = f"56:{TX}:{MINT}"
ORIGIN = "bscfast:" + SIGNATURE


@pytest.fixture
def ledger():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    m.ensure_memecoin_tables(conn)
    yield conn
    conn.close()


def attempt(conn, *, attempt_id="native", pending=True, release=True, link=True, **fields):
    row = dict(attempt_id=attempt_id, origin_event_id=ORIGIN, network_id="56", mint=MINT,
               source_user_id=UID, signal_source="bsc_fast_lane", delivery_lane="bsc_fast_lane",
               decision_started_at=ARRIVAL, terminal_status="aborted", terminal_reason="float_short",
               completed_at=NOW.isoformat(), trace_json=json.dumps(
                   {"stages": {}, "substage_events": [], "submission_events": []}))
    row.update(fields)
    conn.execute("INSERT INTO memecoin_entry_attempts(" + ",".join(row) + ") VALUES(" +
                 ",".join("?" for _ in row) + ")", tuple(row.values()))
    if pending:
        pid = m.reserve_pending(conn, mint=MINT, source_user_id=UID, strategy="alpha_b",
            notional=106.32, reserved_at=ARRIVAL, network_id="56", origin_event_id=ORIGIN,
            attempt_id=attempt_id, signal_source="bsc_fast_lane")
        assert pid is not None
        if link:
            conn.execute("UPDATE memecoin_entry_attempts SET pending_id=? WHERE attempt_id=?",
                         (pid, attempt_id))
        if release:
            assert m.release_pending(conn, pid, "bsc_fast_float_short")
        return pid


def unfinished(conn, *, trace=None, pending=True, **fields):
    return attempt(conn, pending=pending, release=False, link=False, terminal_status=None,
                   terminal_reason=None, completed_at=None, trace_json=trace, **fields)


def finish_float_short(conn, pid, *, now=NOW):
    conn.execute("UPDATE memecoin_entry_attempts SET pending_id=?,terminal_status='aborted',"
                 "terminal_reason='float_short',completed_at=?,trace_json=? WHERE attempt_id='native'",
                 (pid, now.isoformat(), '{"stages":{},"submission_events":[]}'))
    return m.release_pending(conn, pid, "bsc_fast_float_short")


def detection(conn, *, sent=True, arrival=ARRIVAL, uid=UID):
    from test_memecoin_bsc_source_initiation import expected_proof
    valuation = dict(bsc_fast_lane_sent=sent, source_initiation=expected_proof(
        mint=MINT, tx_hash=TX, wallet=WALLET, uid=uid, block_number=16),
        evm_post={"reason": "no_venue_leg"})
    conn.execute("INSERT INTO onchain_wallet_detections(signature,source_user_id,handle,address,"
                 "network_id,mint,source_slot,source_size_usd,source_price,detected_at,created_at,"
                 "source_valuation_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                 (SIGNATURE, uid, "badabeepp", WALLET, "56", MINT, 16, 2000.0, 0.0001,
                  arrival, arrival, json.dumps(valuation)))


def sweep(conn, hook, *, cursor=0, deferred=None, now=NOW):
    return lane.sweep_onchain_detections(conn, hook, cursor=cursor,
        now_fn=lambda: now.isoformat(), max_age_s=45, network_id="56", deferred=deferred,
        approved_ids_fn=lambda _conn, chain: {UID} if chain == "56" else set())


def test_exact_unsigned_float_short_runs_original_ordinary_event_before_two_second_hold(ledger):
    pid = attempt(ledger)
    detection(ledger)
    calls, deferred = [], {}
    statements = []
    ledger.set_trace_callback(statements.append)
    cursor, results = sweep(ledger, lambda c, ev: calls.append(ev) or {"status": "rejected",
        "reason": "ordinary safety refused"}, deferred=deferred)
    assert len(calls) == 1
    assert results[0]["result"] == {"status": "rejected", "reason": "ordinary safety refused"}
    ev = calls[0]
    assert (ev.fomo_event_id, ev.signal_source, ev.network_id, ev.source_user_id) == (
        "onchain:" + SIGNATURE, "onchain_watch", "56", UID)
    assert ev.created_at == ev.received_at == ARRIVAL
    assert ev.their_size_usd == 2000.0 and ev.their_price == 0.0001
    assert ev.source_initiation["tx_hash"] == TX
    assert deferred == {} and cursor == 1
    assert m.pending_row(ledger, pid)["status"] == "failed"
    lookups = [sql for sql in statements if "FROM memecoin_entry_attempts" in sql]
    assert len(lookups) == 1, "handoff must reuse the existing indexed lookup"
    sweep(ledger, lambda c, ev: calls.append(ev) or {}, cursor=cursor, deferred=deferred)
    assert len(calls) == 1


def test_already_deferred_handoff_releases_at_unsigned_terminal_without_waiting(ledger):
    pid = attempt(ledger, terminal_status=None, terminal_reason=None, completed_at=None)
    ledger.execute("UPDATE memecoin_pending_entries SET status='reserved',resolution_reason=NULL "
                   "WHERE id=?", (pid,))
    detection(ledger)
    row = dict(ledger.execute("SELECT * FROM onchain_wallet_detections").fetchone())
    key = (SIGNATURE, UID, "56", MINT)
    deferred = {key: dict(row, _held_for="bsc_fast_lane")}
    calls = []
    # The native owner atomically finished its unsigned refusal before this sweep.
    ledger.execute("UPDATE memecoin_entry_attempts SET terminal_status='aborted',"
                   "terminal_reason='float_short',completed_at=?", (NOW.isoformat(),))
    assert m.release_pending(ledger, pid, "bsc_fast_float_short")
    cursor, results = sweep(ledger, lambda c, ev: calls.append(ev) or {"status": "filled"},
                            cursor=1, deferred=deferred)
    assert len(calls) == 1 and results[0]["result"]["status"] == "filled"
    assert cursor == 1 and not deferred


@pytest.mark.parametrize("trace", [None, '{"source_evidence":{"provider":"watcher"}}',
                                  '{"stages":{},"submission_events":[]}'])
@pytest.mark.parametrize("sent", [True, False])
def test_unfinished_native_is_held_then_original_event_handoffs_after_float_short(ledger, trace, sent):
    pid = unfinished(ledger, trace=trace)
    detection(ledger, sent=sent)
    calls, deferred = [], {}
    cursor, results = sweep(ledger, lambda c, ev: calls.append(ev) or {"status": "filled"},
                            deferred=deferred)
    assert calls == [] and results[0]["result"]["reason"] == "bsc_fast_lane_handoff"
    assert cursor == 1 and len(deferred) == 1
    ev = lane.OnchainEv(dict(ledger.execute("SELECT * FROM onchain_wallet_detections").fetchone()))
    assert lane.already_attempted_from_another_lane(ledger, SIGNATURE, ARRIVAL, source_user_id=UID)
    assert lane.retry_skip_reason(ledger, ev, NOW.isoformat(), max_age_s=45) == "duplicate_of_feed"
    assert finish_float_short(ledger, pid)
    cursor, results = sweep(ledger, lambda c, ev: calls.append(ev) or {"status": "filled"},
                            cursor=cursor, deferred=deferred, now=NOW + timedelta(milliseconds=20))
    assert len(calls) == 1 and results[0]["result"]["status"] == "filled"
    assert calls[0].created_at == calls[0].received_at == ARRIVAL
    assert calls[0].fomo_event_id == "onchain:" + SIGNATURE
    assert cursor == 1 and deferred == {}


def test_already_held_unsigned_native_survives_inflight_sweep(ledger):
    pid = unfinished(ledger)
    detection(ledger)
    row = dict(ledger.execute("SELECT * FROM onchain_wallet_detections").fetchone())
    key = (SIGNATURE, UID, "56", MINT)
    deferred = {key: dict(row, _held_for="bsc_fast_lane")}
    calls = []
    cursor, results = sweep(ledger, lambda c, ev: calls.append(ev) or {"status": "filled"},
                            cursor=1, deferred=deferred)
    assert calls == [] and results == [] and key in deferred
    assert finish_float_short(ledger, pid)
    _, results = sweep(ledger, lambda c, ev: calls.append(ev) or {"status": "filled"},
                       cursor=cursor, deferred=deferred)
    assert len(calls) == 1 and results[0]["result"]["status"] == "filled" and not deferred


def test_known_unsigned_native_stays_held_after_hint_timeout_until_original_freshness(ledger):
    pid = unfinished(ledger)
    detection(ledger)
    calls, deferred = [], {}
    cursor, _ = sweep(ledger, lambda c, ev: calls.append(ev) or {"status": "filled"}, deferred=deferred)
    assert len(deferred) == 1
    cursor, results = sweep(ledger, lambda c, ev: calls.append(ev) or {"status": "filled"},
                            cursor=cursor, deferred=deferred, now=NOW + timedelta(seconds=3))
    assert calls == [] and results == [] and len(deferred) == 1
    assert finish_float_short(ledger, pid, now=NOW + timedelta(seconds=3))
    _, results = sweep(ledger, lambda c, ev: calls.append(ev) or {"status": "filled"},
                       cursor=cursor, deferred=deferred, now=NOW + timedelta(seconds=3, milliseconds=20))
    assert len(calls) == 1 and results[0]["result"]["status"] == "filled" and not deferred
    assert calls[0].created_at == ARRIVAL


def test_known_unsigned_native_expires_with_original_source_without_hook(ledger):
    unfinished(ledger)
    detection(ledger)
    calls, deferred = [], {}
    cursor, _ = sweep(ledger, lambda c, ev: calls.append(ev) or {}, deferred=deferred)
    assert len(deferred) == 1
    _, results = sweep(ledger, lambda c, ev: calls.append(ev) or {}, cursor=cursor,
                       deferred=deferred, now=NOW + timedelta(seconds=46))
    assert calls == [] and results[0]["result"]["reason"] == "slot_fallback_expired" and not deferred


def test_native_release_before_trace_finish_stays_held_until_exact_terminal_proof(ledger):
    pid = unfinished(ledger)
    detection(ledger)
    calls, deferred = [], {}
    cursor, _ = sweep(ledger, lambda c, ev: calls.append(ev) or {"status": "filled"}, deferred=deferred)
    assert len(deferred) == 1
    # Native abort() commits the failed reservation before its caller finishes the trace.
    assert m.release_pending(ledger, pid, "bsc_fast_float_short")
    cursor, results = sweep(ledger, lambda c, ev: calls.append(ev) or {"status": "filled"},
                            cursor=cursor, deferred=deferred)
    assert calls == [] and results == [] and len(deferred) == 1
    finish_float_short(ledger, pid)
    _, results = sweep(ledger, lambda c, ev: calls.append(ev) or {"status": "filled"},
                       cursor=cursor, deferred=deferred)
    assert len(calls) == 1 and results[0]["result"]["status"] == "filled" and not deferred


@pytest.mark.parametrize("trace", [None, '{"source_evidence":{"provider":"watcher"}}'])
def test_native_pre_reservation_shell_is_held_then_actual_unlinked_pending_can_finish(ledger, trace):
    unfinished(ledger, pending=False, trace=trace)
    detection(ledger)
    calls, deferred = [], {}
    cursor, results = sweep(ledger, lambda c, ev: calls.append(ev) or {"status": "filled"},
                            deferred=deferred)
    assert calls == [] and results[0]["result"]["reason"] == "bsc_fast_lane_handoff"
    assert len(deferred) == 1
    # reserve_pending records attempt_id; native a.pending_id is tagged only by finish().
    pid = m.reserve_pending(ledger, mint=MINT, source_user_id=UID, strategy="alpha_b",
        notional=106.32, reserved_at=ARRIVAL, network_id="56", origin_event_id=ORIGIN,
        attempt_id="native", signal_source="bsc_fast_lane")
    assert pid is not None
    assert ledger.execute("SELECT pending_id FROM memecoin_entry_attempts WHERE attempt_id='native'").fetchone()[0] is None
    cursor, results = sweep(ledger, lambda c, ev: calls.append(ev) or {"status": "filled"},
                            cursor=cursor, deferred=deferred)
    assert calls == [] and results == [] and len(deferred) == 1
    assert finish_float_short(ledger, pid)
    _, results = sweep(ledger, lambda c, ev: calls.append(ev) or {"status": "filled"},
                       cursor=cursor, deferred=deferred)
    assert len(calls) == 1 and results[0]["result"]["status"] == "filled" and not deferred


def test_native_terminal_float_short_without_direct_pending_link_cannot_handoff(ledger):
    unfinished(ledger)
    ledger.execute("UPDATE memecoin_entry_attempts SET terminal_status='aborted',"
                   "terminal_reason='float_short',completed_at=?,trace_json=?",
                   (NOW.isoformat(), '{"stages":{},"submission_events":[]}'))
    detection(ledger)
    calls, deferred = [], {}
    _, results = sweep(ledger, lambda c, ev: calls.append(ev) or {}, deferred=deferred)
    assert calls == [] and results[0]["result"]["reason"] == "duplicate_of_feed" and not deferred


@pytest.mark.parametrize("changes", [{"tx_sig": "0xsigned"}, {"signing_ms": 0},
    {"trace_json": "[]"}, {"trace_json": "not-json"},
    {"trace_json": '{"source_evidence":{"stage":"rh_fast_lane_send"}}'},
    {"trace_json": '{"submission_events":[{"status":"accepted"}]}'},
    {"trace_json": '{"submission_events":null}'}])
def test_unsigned_native_pending_with_submission_or_malformed_trace_blocks(ledger, changes):
    fields = dict(changes)
    trace = fields.pop("trace_json", None)
    unfinished(ledger, trace=trace, **fields)
    detection(ledger)
    calls, deferred = [], {}
    _, results = sweep(ledger, lambda c, ev: calls.append(ev) or {}, deferred=deferred)
    assert calls == [] and results[0]["result"]["reason"] == "duplicate_of_feed" and not deferred


@pytest.mark.parametrize("changes", [{"nonce": 0}, {"submission_bound_at": ARRIVAL},
    {"status": "submitted_unknown"}, {"source_user_id": "someone_else"},
    {"status": "failed", "resolution_reason": "bsc_fast_token_safety"}])
def test_unfinished_native_pending_metadata_cannot_authorize_fallback(ledger, changes):
    pid = unfinished(ledger)
    for field, value in changes.items():
        ledger.execute(f"UPDATE memecoin_pending_entries SET {field}=? WHERE id=?", (value, pid))
    detection(ledger)
    calls, deferred = [], {}
    _, results = sweep(ledger, lambda c, ev: calls.append(ev) or {}, deferred=deferred)
    assert calls == [] and results[0]["result"]["reason"] == "bsc_fast_lane_handoff"
    assert len(deferred) == 1


@pytest.mark.parametrize("field,value", [("tx_sig", "0xsigned"), ("submission_bound_at", ARRIVAL)])
def test_native_signed_or_bound_pending_after_hold_cannot_authorize_terminal_float_short(ledger, field, value):
    pid = unfinished(ledger)
    detection(ledger)
    calls, deferred = [], {}
    cursor, _ = sweep(ledger, lambda c, ev: calls.append(ev) or {}, deferred=deferred)
    assert len(deferred) == 1
    ledger.execute(f"UPDATE memecoin_pending_entries SET {field}=? WHERE id=?", (value, pid))
    _, results = sweep(ledger, lambda c, ev: calls.append(ev) or {}, cursor=cursor, deferred=deferred)
    assert calls == [] and results == [] and len(deferred) == 1
    # The incomplete trace can only retain a row; binding prevents the final exemption.
    finish_float_short(ledger, pid)
    _, results = sweep(ledger, lambda c, ev: calls.append(ev) or {}, cursor=cursor, deferred=deferred)
    assert calls == [] and results[0]["result"]["reason"] == "duplicate_of_feed" and not deferred


@pytest.mark.parametrize("blocker_first", [True, False])
def test_competing_feed_attempt_wins_over_unsigned_native_pending(ledger, blocker_first):
    if blocker_first:
        attempt(ledger, attempt_id="feed", pending=False, signal_source="follow_activity")
    unfinished(ledger)
    if not blocker_first:
        attempt(ledger, attempt_id="feed", pending=False, signal_source="follow_activity")
    detection(ledger)
    calls, deferred = [], {}
    _, results = sweep(ledger, lambda c, ev: calls.append(ev) or {}, deferred=deferred)
    assert calls == [] and results[0]["result"]["reason"] == "duplicate_of_feed" and not deferred


def test_legacy_bool_and_retry_guard_allow_only_proved_unsigned_float_short(ledger):
    attempt(ledger)
    detection(ledger)
    ev = lane.OnchainEv(dict(ledger.execute("SELECT * FROM onchain_wallet_detections").fetchone()))
    assert not lane.already_attempted_from_another_lane(
        ledger, SIGNATURE, ARRIVAL, source_user_id=UID)
    assert lane.retry_skip_reason(ledger, ev, NOW.isoformat(), max_age_s=45) is None
    assert lane.retry_skip_reason(ledger, ev, (NOW + timedelta(seconds=46)).isoformat(),
                                  max_age_s=45) == "stale_for_lane"


# Any other UNSIGNED lane abort released with the lane's own reason is released too since
# 2026-10-14 (test_memecoin_lane_unsigned_abort_release.py); a non-abort terminal still blocks.
@pytest.mark.parametrize("changes", [
    {"terminal_status": "unsafe"}, {"terminal_status": "rejected"},
    {"signal_source": "j7"}, {"delivery_lane": "bsc_v3_fast_lane"},
    {"origin_event_id": "swap:bsc:" + TX + ":" + MINT},
    {"tx_sig": "0xsigned"}, {"tx_sig": ""}, {"position_id": 1}, {"fill_id": 1},
    {"signing_completed_at": ARRIVAL}, {"signing_ms": 0},
    {"transaction_submitted_at": ARRIVAL}, {"transaction_submitted_ms": 0},
    {"trace_json": '{"marks": [{"stage":"bsc_fast_lane_send"}]}'},
    {"trace_json": '{"marks": [{"stage":"rh_fast_lane_send"}]}'},
    {"trace_json": None}, {"trace_json": "not-json"},
])
def test_other_refusals_or_attempt_submission_evidence_remain_blocked(ledger, changes):
    attempt(ledger, **changes)
    detection(ledger)
    calls = []
    _, results = sweep(ledger, lambda c, ev: calls.append(ev) or {}, deferred={})
    assert calls == [] and results[0]["result"]["reason"] == "duplicate_of_feed"
    assert lane.already_attempted_from_another_lane(ledger, SIGNATURE, ARRIVAL, source_user_id=UID)


@pytest.mark.parametrize("trace", [
    "null", "[]", "1", "true", '"text"', "{}",
    '{"submission_events": null}', '{"submission_events": {}}',
    '{"submission_events": true}', '{"submission_events": 0}',
    '{"submission_events": "[]"}',
    '{"submission_events": [{"status":"not_sent"}]}',
    '{"submission_events": [{"status":"accepted"}]}',
])
def test_unproven_trace_cannot_release_handoff(ledger, trace):
    attempt(ledger, trace_json=trace)
    detection(ledger)
    calls = []
    _, results = sweep(ledger, lambda c, ev: calls.append(ev) or {}, deferred={})
    assert calls == [] and results[0]["result"]["reason"] == "duplicate_of_feed"


@pytest.mark.parametrize("field,value", [
    ("status", "reserved"), ("status", "confirmed"),
    ("resolution_reason", "stale_reservation"),          # not the lane's own release
    ("network_id", "4663"), ("mint", "0x" + "99" * 20),
    ("source_user_id", "someone_else"), ("origin_event_id", "onchain:" + SIGNATURE),
    ("signal_source", "onchain_watch"), ("attempt_id", "someone_else"),
    ("tx_sig", "0xsigned"), ("tx_sig", ""), ("broadcast_at", ARRIVAL), ("nonce", 0),
    ("signed_payload_b64", "bytes"), ("signed_payload_sha256", "hash"),
    ("submission_blockhash", "0xblock"), ("submission_last_valid_height", 0),
    ("submission_backend", "bsc_fast_lane"), ("submission_bound_at", ARRIVAL),
    ("cleanup_tx_sig", "0xcleanup"), ("cleanup_nonce", 0), ("cleanup_amount_atomic", "0"),
    ("cleanup_status", "failed"), ("cleanup_signed_at", ARRIVAL),
    ("cleanup_signed_payload_sha256", "hash"),
])
def test_conflicting_or_ever_bound_pending_cannot_release_handoff(ledger, field, value):
    pid = attempt(ledger)
    ledger.execute(f"UPDATE memecoin_pending_entries SET {field}=? WHERE id=?", (value, pid))
    detection(ledger)
    calls = []
    _, results = sweep(ledger, lambda c, ev: calls.append(ev) or {}, deferred={})
    assert calls == [] and results[0]["result"]["reason"] == "duplicate_of_feed"


@pytest.mark.parametrize("missing", ["pending_row", "attempt_pending_id"])
def test_missing_pending_link_remains_blocked(ledger, missing):
    pid = attempt(ledger)
    if missing == "pending_row":
        ledger.execute("DELETE FROM memecoin_pending_entries WHERE id=?", (pid,))
    else:
        ledger.execute("UPDATE memecoin_entry_attempts SET pending_id=NULL")
    detection(ledger)
    calls = []
    _, results = sweep(ledger, lambda c, ev: calls.append(ev) or {}, deferred={})
    assert calls == [] and results[0]["result"]["reason"] == "duplicate_of_feed"


@pytest.mark.parametrize("blocker_first", [True, False])
@pytest.mark.parametrize("source", ["bsc_fast_lane", "follow_activity"])
def test_any_other_matching_attempt_wins_over_unsigned_float_refusal(ledger, blocker_first, source):
    if blocker_first:
        attempt(ledger, attempt_id="blocking", pending=False, terminal_reason="token_safety",
                signal_source=source)
    attempt(ledger)
    if not blocker_first:
        attempt(ledger, attempt_id="blocking", pending=False, terminal_reason="token_safety",
                signal_source=source)
    detection(ledger)
    calls = []
    _, results = sweep(ledger, lambda c, ev: calls.append(ev) or {}, deferred={})
    assert calls == [] and results[0]["result"]["reason"] == "duplicate_of_feed"


@pytest.mark.parametrize("ordinary", [
    {"terminal_status": "rejected", "terminal_reason": "price minimum over cap"},
    {"terminal_status": "unsafe", "terminal_reason": "token_safety"},
    {"terminal_reason": "provider_timeout"},
    {"terminal_status": "filled", "terminal_reason": "filled", "tx_sig": "0xsigned", "position_id": 1},
    {"terminal_status": None, "terminal_reason": None, "completed_at": None},
    {"terminal_status": None, "terminal_reason": None, "completed_at": None, "tx_sig": "0xsigned"},
])
def test_persisted_ordinary_decision_blocks_replayed_handoff_after_restart(ledger, ordinary):
    attempt(ledger)
    attempt(ledger, attempt_id="ordinary", pending=False, signal_source="onchain_watch",
            delivery_lane="onchain_watch", origin_event_id="onchain:" + SIGNATURE, **ordinary)
    detection(ledger)
    calls = []
    _, results = sweep(ledger, lambda c, ev: calls.append(ev) or {}, deferred={})
    assert calls == [] and results[0]["result"]["reason"] == "duplicate_of_feed"
    ev = lane.OnchainEv(dict(ledger.execute("SELECT * FROM onchain_wallet_detections").fetchone()))
    # Explicit queued retries still bypass completed ordinary deliveries under their old guard.
    assert lane.retry_skip_reason(ledger, ev, NOW.isoformat(), max_age_s=45) is None


@pytest.mark.parametrize("changes", [
    {"network_id": "4663"}, {"mint": "0x" + "98" * 20},
    {"source_user_id": "someone_else"},
    {"origin_event_id": "onchain:56:0x" + "97" * 32 + ":" + MINT},
])
def test_persisted_ordinary_blocker_only_owns_its_exact_source_identity(ledger, changes):
    attempt(ledger)
    fields = dict(signal_source="onchain_watch", delivery_lane="onchain_watch",
                  origin_event_id="onchain:" + SIGNATURE, terminal_reason="provider_timeout")
    fields.update(changes)
    attempt(ledger, attempt_id="ordinary", pending=False, **fields)
    detection(ledger)
    calls = []
    _, results = sweep(ledger, lambda c, ev: calls.append(ev) or {"status": "rejected"}, deferred={})
    assert len(calls) == 1 and results[0]["result"]["status"] == "rejected"


@pytest.mark.parametrize("chain,coin,wallet,transaction", [
    ("4663", MINT, UID, TX), ("56", "0x" + "99" * 20, UID, TX),
    ("56", MINT, "someone_else", TX), ("56", MINT, UID, "0x" + "98" * 32),
])
def test_unsigned_exemption_is_scoped_to_exact_chain_coin_wallet_transaction(
        ledger, chain, coin, wallet, transaction):
    attempt(ledger)
    # An unrelated unsigned row may not override the ordinary two-second handoff hold.
    signature = f"{chain}:{transaction}:{coin}"
    assert not lane.already_attempted_from_another_lane(ledger, signature, ARRIVAL,
                                                       source_user_id=wallet)
    detection(ledger, uid=wallet)
    ledger.execute("UPDATE onchain_wallet_detections SET signature=?, network_id=?,mint=?",
                   (signature, chain, coin))
    row = ledger.execute("SELECT * FROM onchain_wallet_detections").fetchone()
    calls = []
    result = lane._route(ledger, lambda c, ev: calls.append(ev) or {}, row, {}, now=NOW)
    if chain == "56":
        assert calls == [] and result["reason"] == "bsc_fast_lane_handoff"
    else:
        assert len(calls) == 1


def test_unsigned_refusal_cannot_override_source_freshness_or_arming(ledger):
    attempt(ledger)
    detection(ledger, arrival=(NOW - timedelta(seconds=46)).isoformat())
    calls = []
    assert sweep(ledger, lambda c, ev: calls.append(ev) or {}, deferred={}) == (1, [])
    assert calls == []
    ledger.execute("UPDATE onchain_wallet_detections SET detected_at=?,source_user_id='watch_only'",
                   (ARRIVAL,))
    assert sweep(ledger, lambda c, ev: calls.append(ev) or {}, deferred={}) == (1, [])
    assert calls == []


def test_unsigned_label_without_wallet_scope_cannot_exempt_an_attempt(ledger):
    attempt(ledger)
    assert lane.already_attempted_from_another_lane(ledger, SIGNATURE, ARRIVAL)


def test_no_native_attempt_keeps_existing_handoff_window(ledger):
    detection(ledger)
    calls, deferred = [], {}
    cursor, results = sweep(ledger, lambda c, ev: calls.append(ev) or {}, deferred=deferred)
    assert not calls and results[0]["result"]["reason"] == "bsc_fast_lane_handoff"
    sweep(ledger, lambda c, ev: calls.append(ev) or {}, cursor=cursor, deferred=deferred,
          now=NOW + timedelta(seconds=2))
    assert len(calls) == 1 and not deferred


def test_shared_hook_claim_runs_once_across_workers_and_keeps_explicit_retry(ledger, monkeypatch):
    from sniper.services import memecoin_entry_knobs as knobs, memecoin_real_runner as runner
    from sniper.settings import memecoin as cfg
    from test_memecoin_entry_identity import _arm, _approve, _evm_context
    attempt(ledger)
    detection(ledger)
    _arm(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"56"})
    _approve(ledger, uid=UID, chains=("56",))
    assert UID in m.real_approved_user_ids(ledger, network_id="56")
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_OWN_MCAP_CHAINS", "")
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CLUSTER_GATE_ENABLED", False)
    monkeypatch.setattr(knobs, "_DELIVERED_ORIGINS", OrderedDict())
    monkeypatch.setattr(knobs, "_ACTIVE_ORIGINS", set())
    monkeypatch.setattr(m, "resolve_mint_mcap", lambda *a, **kw: 11000.0)
    started, release = threading.Event(), threading.Event()
    called, queued, results = [], [], []
    def decide(_conn, ev, *args, **kwargs):
        called.append(ev)
        started.set()
        assert release.wait(2)
        return {"status": "aborted", "reason": "provider timeout", "quote_miss_code": "provider_timeout"}
    monkeypatch.setattr(runner, "real_entry_hook", decide)
    monkeypatch.setattr(runner, "_record_real_decision", lambda *a, **kw: None)
    monkeypatch.setattr(runner, "_maybe_enqueue_retry", lambda *a, **kw: queued.append(1))
    def hook():
        return runner.make_real_hook(None, {"56": _evm_context({})},
            now_fn=lambda: NOW.isoformat(), trade_size=106.32, max_age_s=45,
            stats_fn=lambda *a, **kw: {"market_cap": 11000.0, "price": 0.0001})
    first, second = hook(), hook()
    thread = threading.Thread(target=lambda: results.append(sweep(ledger, first, deferred={})))
    thread.start()
    try:
        assert started.wait(2), results
        other_cursor, other_results = sweep(ledger, second, deferred={})
        assert other_cursor == 1
        assert other_results[0]["result"] in (
            {"status": "skipped", "reason": "duplicate_of_feed"},
            {"status": "duplicate_delivery", "reason": "source decision already owned"})
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive() and len(called) == len(queued) == 1
    assert called[0].created_at == ARRIVAL
    assert called[0].fomo_event_id == "onchain:" + SIGNATURE
    assert results[0][0] == 1
    # A replayed cursor may reach the existing completed-delivery check, never another decision.
    sweep(ledger, second, deferred={})
    assert len(called) == len(queued) == 1
    ordinary = lane.OnchainEv(dict(ledger.execute("SELECT * FROM onchain_wallet_detections").fetchone()))
    assert second(ledger, ordinary)["status"] == "duplicate_delivery"
    assert lane.retry_skip_reason(ledger, ordinary, NOW.isoformat(), max_age_s=45) is None
    assert second.attempt(ledger, ordinary)["status"] == "aborted"
    assert len(called) == 2 and len(queued) == 1
