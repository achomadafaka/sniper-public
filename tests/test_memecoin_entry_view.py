"""The operator view follows durable execution evidence."""
import importlib
import json
import sqlite3

import pytest
from flask import Flask

from sniper.persistence import db_memecoin as m
from sniper.routes import api_memecoin as api
from sniper.services import memecoin_manual_intents as mi

NOW = "2026-09-11T12:00:00+00:00"


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    yield c
    c.close()


def view(c, **kwargs):
    return importlib.import_module("sniper.services.memecoin_entry_view").entry_view(c, **kwargs)


def attempt(c, aid="a1", origin="event1", status=None, chain="solana", **fields):
    row = dict(attempt_id=aid, origin_event_id=origin, network_id=chain, mint="MINT",
               source_handle="alice", decision_started_at=NOW, observed_at=NOW)
    if status:
        row.update(terminal_status=status, terminal_reason="recorded reason", completed_at=NOW)
    row.update(fields)
    c.execute(f"INSERT INTO memecoin_entry_attempts({','.join(row)}) VALUES "
              f"({','.join('?' for _ in row)})", tuple(row.values()))
    c.commit()


def pending(c, *, aid="a1", sig=None, broadcast=None, status="reserved", **fields):
    row = dict(mint="MINT", network_id="solana", attempt_id=aid, origin_event_id="event1",
               reserved_notional_usd=175, reserved_at=NOW, status=status,
               tx_sig=sig, broadcast_at=broadcast)
    row.update(fields)
    cur = c.execute(f"INSERT INTO memecoin_pending_entries({','.join(row)}) VALUES "
                    f"({','.join('?' for _ in row)})", tuple(row.values()))
    c.commit()
    return cur.lastrowid


def fill(c, *, aid="a1", mode="real", origin="event1", chain="solana"):
    pid = m.open_position(c, source_user_id="u", source_handle="alice", mint="MINT",
                          ticker="COIN", entry_price=1, entry_notional=175,
                          entry_fill_source="chain", entry_lag_s=0, their_size_usd=700,
                          opened_at=NOW, execution_mode=mode, network_id=chain,
                          attempt_id=aid, origin_event_id=origin)
    m.insert_fill(c, position_id=pid, kind="entry", price=1, qty=175, notional=175,
                  fill_source="chain", fraction=1, created_at=NOW, attempt_id=aid,
                  origin_event_id=origin, network_id=chain, tx_sig="signature")
    return pid


def hint_event(c, *, source="follow_activity", decision="hint_only"):
    c.execute("INSERT INTO memecoin_events(fomo_event_id,mint,network_id,source_user_id,"
              "source_handle,side,signal_source,delivery_lane,created_at,real_decision,real_decision_reason) "
              "VALUES ('event1','MINT','solana','u1','loganlim_x','buy',?,'j7_wallets',?,?,?)",
              (source, NOW, decision, "yellowstone_source_required"))
    m.add_wallet(c, "u1", "loganlim_x")
    c.execute("UPDATE memecoin_wallets SET real_approved=1,real_chains='1,5042' WHERE source_user_id='u1'")
    c.commit()


@pytest.mark.parametrize("source", ["follow_activity", "feed", "global_feed"])
def test_unsigned_feed_hint_is_not_a_review_or_an_unarmed_wallet_alert(conn, source):
    from sniper.services.memecoin_telegram import _NotificationWorker
    hint_event(conn, source=source)
    row = view(conn, origin_event_id="event1")
    assert row["status"] == "not_evaluated"
    assert row["reason"] == "yellowstone_source_required"
    assert row["attempt_id"] is None and row["tx_sig"] is None
    worker = object.__new__(_NotificationWorker)
    worker.read_conn = conn
    assert worker._eligible(row) is False


@pytest.mark.parametrize("state,expected", [
    ("unsigned", "evaluating"), ("signed", "transaction_unresolved"),
    ("submitted", "submitted_unknown"), ("filled", "filled"),
    ("conflict", "needs_review"), ("unknown", "transaction_unresolved"),
])
def test_feed_hint_cannot_hide_execution_evidence(conn, state, expected):
    hint_event(conn)
    if state == "filled":
        attempt(conn, status="filled")
        fill(conn)
    elif state == "conflict":
        attempt(conn, chain="8453")
        pending(conn, sig="signature")
    elif state == "unknown":
        attempt(conn, status="unknown")
    else:
        attempt(conn)
        pending(conn, sig="signature" if state != "unsigned" else None,
                broadcast=NOW if state == "submitted" else None)
    assert view(conn, origin_event_id="event1")["status"] == expected


def test_unrecognized_source_decision_still_requires_review(conn):
    hint_event(conn, decision="unrecognized_decision")
    assert view(conn, origin_event_id="event1")["status"] == "needs_review"


def test_corrected_feed_hint_does_not_repage_a_prior_review(conn, monkeypatch):
    from test_memecoin_notification_lifecycle import worker, rows
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_TG_SOURCE_CARDS", False)
    monkeypatch.setattr(cfg, "MEMECOIN_TG_UNRESOLVED_GRACE_S", 0.0)
    hint_event(conn)
    life, notice_conn, current, sent = worker()
    current["event1"] = dict(view(conn, origin_event_id="event1"), status="needs_review")
    life.observe(current["event1"])
    life.deliver()
    assert sent == ["needs_review"]
    current["event1"] = view(conn, origin_event_id="event1")
    assert current["event1"]["status"] == "not_evaluated"
    life.observe(current["event1"])
    life.deliver()
    assert sent == ["needs_review"]
    assert rows(notice_conn)[-1]["message_kind"] == "source"


def test_recovered_fill_overrides_old_unknown(conn):
    attempt(conn, status="submitted_unknown")
    pending(conn, sig="signature", status="confirmed")
    pid = fill(conn)
    row = view(conn, origin_event_id="event1")
    assert row["status"] == "filled"
    assert row["position_id"] == pid
    assert row["fill_id"]
    assert row["attempts"][0]["terminal_status"] == "submitted_unknown"
    assert row["requested_usd"] == 175


@pytest.mark.parametrize("broadcast,expected", [(None, "transaction_unresolved"), (NOW, "submitted_unknown")])
def test_signed_work_cannot_be_refused(conn, broadcast, expected):
    attempt(conn, status="refused")
    pending(conn, sig="signature", broadcast=broadcast,
            signed_payload_b64="PRIVATE_BYTES", signed_payload_sha256="digest")
    row = view(conn, attempt_id="a1")
    assert row["status"] == expected
    assert "PRIVATE_BYTES" not in json.dumps(row)
    assert "signed_payload_b64" not in json.dumps(row)


def test_active_retry_owns_old_refusal(conn):
    attempt(conn, status="aborted")
    m.enqueue_entry_retry(conn, "event1", "MINT", "solana", "{}", NOW, retry_after_s=3)
    row = view(conn, origin_event_id="event1")
    assert row["status"] == "retry_pending"
    assert row["retry_next_at"]


def test_paper_fill_does_not_establish_funded_fill(conn):
    conn.execute("INSERT INTO memecoin_events(fomo_event_id,mint,network_id,action,created_at) "
                 "VALUES ('event1','MINT','solana','opened',?)", (NOW,))
    fill(conn, aid=None, mode="paper")
    row = view(conn, origin_event_id="event1")
    assert row["status"] == "source_received"
    assert row["paper_action"] == "opened"
    assert row["position_id"] is None


def test_missing_event_preserves_attempt_and_exact_chain(conn):
    attempt(conn, status="refused", chain="56")
    attempt(conn, aid="a2", origin="event2", status="refused", chain="8453")
    mod = importlib.import_module("sniper.services.memecoin_entry_view")
    rows = mod.recent_entry_views(conn, chain="bsc")
    assert len(rows) == 1
    assert rows[0]["attempt_id"] == "a1"
    assert rows[0]["network_id"] == "56"
    assert view(conn, origin_event_id="absent") is None


def test_failed_chain_transaction_remains_distinct(conn):
    attempt(conn, status="aborted")
    pending(conn, sig="signature", broadcast=NOW, status="failed",
            resolution_reason="tx_failed_recovery")
    assert view(conn, attempt_id="a1")["status"] == "transaction_failed"


@pytest.mark.parametrize('reason', ['trial_unmarked: slot lane carries a trial only SOL-funded',
                                  'slot_lane_exotic_leg_pending'])
def test_unsigned_slot_funding_handoff_is_not_a_final_refusal(conn, reason):
    attempt(conn, status='rejected', signal_source='slot_lane', terminal_reason=reason)
    assert view(conn, attempt_id='a1')['status'] == 'funding_handoff'
    pending(conn, sig='signed', broadcast=NOW)
    assert view(conn, attempt_id='a1')['status'] == 'submitted_unknown'


def test_processed_solana_buy_awaiting_receipt_is_a_handoff(conn):
    # AUTON e665ea25: confirmed watcher proved and filled this buy 1.8s after this lane result.
    attempt(conn, status="source_initiation_unknown", signal_source="slot_lane",
            delivery_lane="slot_lane_processed", terminal_reason="missing_receipt_proof")
    row = view(conn, attempt_id="a1")
    assert row["status"] == "funding_handoff"
    assert "receipt" in row["reason"].lower()
    assert row["position_id"] is None and row["tx_sig"] is None
    pending(conn, sig="signed", broadcast=NOW)
    assert view(conn, attempt_id="a1")["status"] == "submitted_unknown"


@pytest.mark.parametrize("failed_sig", [None, "0xfailedswap"])
def test_mined_failure_survives_submission_reset_and_retry_refusal(conn, failed_sig):
    confirmation = dict(stage="chain_confirmation", outcome="failed", attempt_no=1,
                        completed_at=NOW, block=125465366)
    if failed_sig:
        confirmation["tx_sig"] = failed_sig
    attempt(conn, status="refused", transaction_submitted_at=NOW,
            terminal_reason="route quote is 1.90x the chart mid (cap 1.50x)",
            trace_json=json.dumps({"submission_events": [confirmation]}))
    pending(conn, status="failed", resolution_reason="execution_premium")
    row = view(conn, attempt_id="a1")
    assert row["status"] == "transaction_failed"
    assert "retry refused" in row["reason"].lower()
    assert "1.90x" in row["reason"]
    assert row["tx_sig"] == failed_sig
    from sniper.services.memecoin_telegram import render_entry_notification
    text = render_entry_notification(row)
    assert text.startswith("🔴 TRANSACTION FAILED")
    assert "retry refused" in text.lower() and "1.90x" in text


@pytest.mark.parametrize("outcome", ["unknown", "confirmed", "simulation_failed"])
def test_retry_refusal_requires_a_recorded_mined_failure(conn, outcome):
    attempt(conn, status="refused", trace_json=json.dumps({"submission_events": [
        dict(stage="chain_confirmation", outcome=outcome, tx_sig="old", attempt_no=1)]}))
    pending(conn, status="failed", resolution_reason="execution_premium")
    row = view(conn, attempt_id="a1")
    assert row["status"] == "refused"
    assert row["tx_sig"] is None


@pytest.mark.parametrize("state", ["signed", "retry", "filled"])
def test_old_failed_swap_cannot_override_current_work_or_fill(conn, state):
    attempt(conn, status="refused", trace_json=json.dumps({"submission_events": [
        dict(stage="chain_confirmation", outcome="failed", tx_sig="old", attempt_no=1)]}))
    if state == "signed":
        pending(conn, sig="new", broadcast=NOW)
        expected = "submitted_unknown"
    elif state == "retry":
        pending(conn, status="failed", resolution_reason="execution_premium")
        m.enqueue_entry_retry(conn, "event1", "MINT", "solana", "{}", NOW, retry_after_s=3)
        expected = "retry_pending"
    else:
        fill(conn)
        expected = "filled"
    row = view(conn, attempt_id="a1")
    assert row["status"] == expected
    assert row["tx_sig"] != "old"


def test_manual_unknown_preserves_ownership_and_recovers(conn):
    iid = mi.enqueue(conn, mint="MINT", now_iso=NOW, size_usd=175,
                     origin_event_id="event1")
    attempt(conn, status="submitted_unknown")
    pending(conn, sig="signature", broadcast=NOW)
    mi.drain(conn, buy_fn=lambda *a, **k: {"status": "submitted_unknown", "attempt_id": "a1"},
             now_fn=lambda: NOW)
    row = mi.read_status(conn, iid)
    assert row["state"] != "refused"
    assert row["status"] == "submitted_unknown"
    assert row["attempt_id"] == "a1"
    assert mi.claim_next(conn, now_iso=NOW) is None
    fill(conn)
    assert mi.read_status(conn, iid)["status"] == "filled"


def test_entry_revision_changes_on_submission_without_new_reservation(conn, monkeypatch):
    monkeypatch.setattr("sniper.services.memecoin_arming.is_kill_tripped", lambda: False)
    attempt(conn)
    pid = pending(conn)
    before = api._cockpit_change_signature(conn)
    conn.execute("UPDATE memecoin_pending_entries SET tx_sig='signature' WHERE id=?", (pid,))
    assert api._cockpit_change_signature(conn) != before


def test_entries_route_has_revision_and_identity(conn, monkeypatch):
    monkeypatch.setattr(api, "_now_iso", lambda: NOW)
    attempt(conn, status="refused")
    app = Flask(__name__)
    app.register_blueprint(api.create_api_memecoin_blueprint({"conn": conn}))
    response = app.test_client().get("/api/memecoin/entries?origin_event_id=event1")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["as_of"] == NOW
    assert payload["revision"]
    assert payload["entries"][0]["origin_event_id"] == "event1"


def test_gate_abort_is_a_refusal_without_submission(conn):
    attempt(conn, status="aborted")
    pending(conn, status="failed", resolution_reason="roundtrip_cap")
    assert view(conn, attempt_id="a1")["status"] == "refused"


def test_fill_with_legacy_null_attempt_uses_exact_position_identity(conn):
    attempt(conn, status="unknown")
    fill(conn)
    conn.execute("UPDATE memecoin_fills SET attempt_id=NULL, origin_event_id=NULL")
    assert view(conn, attempt_id="a1")["status"] == "filled"


def test_manual_exception_after_signature_is_not_a_refusal(conn):
    iid = mi.enqueue(conn, mint="MINT", now_iso=NOW, size_usd=175, origin_event_id="event1")
    def buy(*args, **kwargs):
        attempt(conn)
        pending(conn, sig="signature")
        raise RuntimeError("callback failed after signing")
    mi.drain(conn, buy_fn=buy, now_fn=lambda: NOW)
    row = conn.execute("SELECT state, attempt_id FROM memecoin_manual_intents WHERE intent_id=?", (iid,)).fetchone()
    assert row["state"] == "unresolved"
    assert row["attempt_id"] == "a1"


def test_read_projection_ignores_untrusted_trace_nested_payload(conn):
    trace = {"substage_events": [{"name": "gate.volume", "allowed": False,
             "required_usd": 27008, "tested_volume_m5": 33757, "units": "USD",
             "private_key": "SECRET", "error": {"signed_payload": "SECRET"}}]}
    attempt(conn, status="refused", trace_json=json.dumps(trace))
    row = view(conn, attempt_id="a1")
    assert row["gate_evidence"][0]["required_usd"] == 27008
    assert "SECRET" not in json.dumps(row)


def test_source_buy_floor_evidence_shows_effective_requirement(conn):
    trace = {"substage_events": [{"name": "source_buy_floor", "floor_usd": 1600,
             "effective_floor_usd": 1568, "floor_tolerance_pct": 2,
             "private_key": "SECRET"}]}
    attempt(conn, status="refused", trace_json=json.dumps(trace))
    row = view(conn, attempt_id="a1")
    assert row["gate_evidence"][0]["effective_floor_usd"] == 1568
    assert row["gate_evidence"][0]["floor_tolerance_pct"] == 2
    assert "SECRET" not in json.dumps(row)


def test_manual_projection_error_cannot_requeue_an_executed_intent(conn, monkeypatch):
    module = importlib.import_module("sniper.services.memecoin_entry_view")
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("read unavailable")
    monkeypatch.setattr(module, "entry_view", unavailable)
    iid = mi.enqueue(conn, mint="MINT", now_iso=NOW)
    mi.drain(conn, lambda *a, **k: {"status": "submitted_unknown", "attempt_id": "a1"},
             now_fn=lambda: NOW)
    row = conn.execute("SELECT state FROM memecoin_manual_intents WHERE intent_id=?", (iid,)).fetchone()
    assert row["state"] == "unresolved"
    assert mi.claim_next(conn, now_iso=NOW) is None


def test_roundtrip_evidence_keeps_used_measurement_basis(conn):
    trace = {"substage_events": [
        {"name": "roundtrip_cap", "basis": "independent_pre_entry_quotes", "cost_pct": 12, "cap_pct": 10},
        {"name": "roundtrip_sequential", "used": True, "cost_pct": 11, "cap_pct": 10,
         "evidence": {"status": "measured", "version": "solana_sequential_roundtrip_v1",
                      "buy_spent_atomic": "175000000", "sell_received_atomic": "155750000",
                      "signed_payload": "SECRET"}},
    ]}
    attempt(conn, status="aborted", trace_json=json.dumps(trace))
    evidence = view(conn, attempt_id="a1")["gate_evidence"]
    assert evidence[0]["basis"] == "independent_pre_entry_quotes"
    assert evidence[1]["used"] is True
    assert evidence[1]["evidence"]["buy_spent_atomic"] == "175000000"
    assert "SECRET" not in json.dumps(evidence)


def test_projection_is_read_only(conn):
    attempt(conn, status="refused")
    before = conn.total_changes
    conn.execute("PRAGMA query_only=1")
    assert view(conn, attempt_id="a1")["status"] == "refused"
    assert conn.total_changes == before


def test_observer_connection_is_short_and_read_only(tmp_path):
    c = sqlite3.connect(tmp_path / "operator.db")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    with m.operator_read_connection(c) as observed:
        assert observed is not c
        with pytest.raises(sqlite3.OperationalError):
            observed.execute("DELETE FROM memecoin_events")
    with pytest.raises(sqlite3.ProgrammingError):
        observed.execute("SELECT 1")
    c.close()


def test_sse_entry_carries_exact_identity_and_projected_label(conn, monkeypatch):
    monkeypatch.setattr("sniper.services.memecoin_arming.is_kill_tripped", lambda: False)
    before = api._cockpit_change_signature(conn)[0]
    attempt(conn, status="unknown")
    pid = fill(conn)
    after = api._cockpit_change_signature(conn)[0]
    row = api._fill_events(conn, before, after)[0]
    assert row["status"] == "filled"
    assert row["label"] == "Bought"
    assert row["attempt_id"] == "a1"
    assert row["origin_event_id"] == "event1"
    assert row["network_id"] == "solana"
    assert row["position_id"] == pid


def test_runner_stop_before_signing_settles_unfinished_attempt(conn):
    attempt(conn)
    pending(conn, status="failed", resolution_reason="runner_stopped_before_signing")
    row = view(conn, attempt_id="a1")
    assert row["status"] == "failed_before_submission"
    assert row["reason"] == "runner_stopped_before_signing"


def test_fill_cannot_hide_another_unresolved_attempt_for_same_origin(conn):
    attempt(conn, aid="a1", status="unknown")
    pending(conn, aid="a1", sig="unknown-signature")
    attempt(conn, aid="a2", status="filled")
    fill(conn, aid="a2")
    row = view(conn, origin_event_id="event1")
    assert row["status"] == "needs_review"
    assert row["fill_id"] is not None


def test_manual_view_preserves_source_kind_without_a_source_event(conn):
    attempt(conn, origin=None, signal_source="manual")
    row = view(conn, attempt_id="a1")
    assert row["signal_source"] == "manual"
    assert row["origin_event_id"] is None


def test_missing_notification_schema_is_explicit(conn):
    attempt(conn, status="refused")
    delivery = view(conn, attempt_id="a1")["notification_delivery"]
    assert delivery["available"] is False
    assert delivery["result"] is None


def test_delivery_result_changes_entry_revision(conn):
    from sniper.persistence.db_memecoin_notifications import NotificationLifecycle
    attempt(conn, status="refused")
    life = NotificationLifecycle(conn, read_view=lambda **kw: view(conn, **kw),
        send_fn=lambda text: (True, 1), render_fn=lambda row: row["label"],
        eligible_fn=lambda row: True, now_fn=lambda: NOW)
    life.activate("2026-09-11T11:59:59+00:00")
    life.observe(view(conn, attempt_id="a1"))
    before = m.entry_view_revision(conn)
    life.deliver()
    assert m.entry_view_revision(conn) != before
    assert view(conn, attempt_id="a1")["notification_delivery"]["result"]["delivery_state"] == "delivered"


def test_old_signed_owner_outranks_later_failed_attempt(conn):
    attempt(conn, aid="a1", status="unknown")
    pending(conn, aid="a1", sig="still-live")
    attempt(conn, aid="a2", status="refused")
    pending(conn, aid="a2", status="failed", resolution_reason="roundtrip_cap")
    row = view(conn, origin_event_id="event1")
    assert row["status"] == "transaction_unresolved"
    assert row["attempt_id"] == "a1"
    assert row["tx_sig"] == "still-live"


def test_originless_manual_error_cannot_prove_refusal(conn):
    iid = mi.enqueue(conn, mint="MINT", now_iso=NOW)
    def buy(*args, **kwargs):
        attempt(conn, origin=None)
        pending(conn, sig="still-live", origin_event_id=None)
        raise RuntimeError("unknown result")
    mi.drain(conn, buy, now_fn=lambda: NOW)
    row = mi.read_status(conn, iid)
    assert row["state"] == "unresolved"
    assert row["status"] == "needs_review"


def test_entry_projection_holds_one_short_snapshot(conn, monkeypatch):
    attempt(conn, status="refused")
    real_evidence = m.entry_view_evidence
    def evidence(c, **kwargs):
        assert c.in_transaction, "all execution evidence requires one snapshot"
        return real_evidence(c, **kwargs)
    monkeypatch.setattr(m, "entry_view_evidence", evidence)
    assert view(conn, attempt_id="a1")["status"] == "refused"
    assert not conn.in_transaction


def test_snapshot_preserves_caller_transaction(conn):
    conn.execute("BEGIN")
    with m.operator_read_snapshot(conn):
        conn.execute("SELECT 1")
    assert conn.in_transaction
    conn.rollback()


@pytest.mark.parametrize("resolution", [
    "expired_unfound_proven", "dropped_blockhash_expired_proven", "evm_nonces_superseded",
    "evm_confirmed_pretransaction_no_token_proven",
    "dropped_blockhash_expired_operator_verified",
    "manual_reconcile_tx_absent_nonce_unconsumed",
    "evm_native_unwrap_rewrapped_proven_onchain",
    "native_cleanup_confirmed_after_binding_conflict",
    "operator_recovery: blockhash expired (417937911 < 417939150), sig absent from full history — crash orphaned the row before settle",
    "evm_entry_never_admitted: latest == pending == nonce 9, wallet holds none of the coin, unmined for 900s",
])
def test_terminal_recovery_without_fill_is_not_an_unresolved_transaction(conn, resolution):
    attempt(conn, status="unknown")
    pending(conn, sig="signed", broadcast=NOW, status="failed", resolution_reason=resolution)
    row = view(conn, attempt_id="a1")
    assert row["status"] == "no_fill"
    assert row["label"] == "No fill"
    assert row["reason"] == resolution
    assert row["completed_at"] is None


def test_confirmed_native_cleanup_overrides_old_orphan_marker(conn):
    attempt(conn, status="unknown")
    pending(conn, sig="unwrap", status="failed", resolution_reason="native_cleanup_recovered",
            recovery_status="orphaned", cleanup_tx_sig="rewrap", cleanup_status="confirmed")
    assert view(conn, attempt_id="a1")["status"] == "no_fill"


def test_recovery_reason_does_not_release_active_signed_reservation(conn):
    attempt(conn, status="unknown")
    pending(conn, sig="signed", resolution_reason="expired_unfound_proven")
    assert view(conn, attempt_id="a1")["status"] == "transaction_unresolved"


def test_runner_stop_without_terminal_time_does_not_use_decision_time(conn):
    attempt(conn)
    pending(conn, status="failed", resolution_reason="runner_stopped_before_signing")
    row = view(conn, attempt_id="a1")
    assert row["status"] == "failed_before_submission"
    assert row["completed_at"] is None
    assert row["status_updated_at"] is None


def test_proven_expiry_uses_recovery_check_time(conn):
    attempt(conn, status="unknown")
    pending(conn, sig="signed", status="failed", resolution_reason="expired_unfound_proven",
            last_recovery_check_at="2026-09-11T12:30:00+00:00")
    row = view(conn, attempt_id="a1")
    assert row["completed_at"] == "2026-09-11T12:30:00+00:00"
    assert row["status_updated_at"] == row["completed_at"]


@pytest.mark.parametrize("resolution", [
    "evm_nonces_superseded_cleanup_unresolved", "unknown_operator_proven",
    "operator_recovery: blockhash expired, provider unavailable",
])
def test_unknown_recovery_reasons_remain_reviewable(conn, resolution):
    attempt(conn, status="unknown")
    pending(conn, sig="signed", status="failed", resolution_reason=resolution)
    assert view(conn, attempt_id="a1")["status"] == "needs_review"


def test_recorded_operator_cleanup_overrides_old_cleanup_status(conn):
    attempt(conn, status="unknown")
    pending(conn, sig="unwrap", status="failed",
            resolution_reason="evm_native_unwrap_rewrapped_proven_onchain",
            recovery_status="orphaned", cleanup_tx_sig="rewrap", cleanup_status="not_found")
    row = view(conn, attempt_id="a1")
    assert row["status"] == "no_fill"
    assert row["reason"] == "evm_native_unwrap_rewrapped_proven_onchain"


@pytest.mark.parametrize("resolution", [
    'chain_error:{"InstructionError":[3,{"Custom":6001}]}', "chain_error:custom program error: 0x1771",
    "chain_error:ConstraintMintTokenProgram", "reverted_unknown", "approve_failed",
    "tx_failed",
])
def test_recorded_chain_failure_is_a_failed_transaction_not_a_review(conn, resolution):
    """36 resolved reservations read "Needs review" before 2026-09-11; that status is UNRESOLVED,
    so the next live on-chain failure would have paged the operator as NEEDS REVIEW."""
    attempt(conn, status="unknown")
    pending(conn, sig="signed", broadcast=NOW, status="failed", resolution_reason=resolution)
    row = view(conn, attempt_id="a1")
    assert row["status"] == "transaction_failed"
    assert row["reason"] == resolution


def test_bsc_exhausted_min_output_retry_explains_no_purchase_and_keeps_signed_identity(conn):
    from sniper.services.memecoin_telegram import render_entry_notification

    tx = "0x" + "ab" * 32
    attempt(conn, status="failed", chain="56", ticker="COIN", tx_sig=tx,
            terminal_reason="reverted_min_out", trace_json=json.dumps({"substage_events": [
                {"name": "evm.revert_reason", "attempt_no": 1, "kind": "min_out",
                 "detail": "SlippageTooHigh(uint256,uint256): actual=9 minimum=10 0x76baadda"},
                {"name": "evm.revert_reason", "attempt_no": 2, "kind": "min_out",
                 "detail": "SlippageTooHigh(uint256,uint256): actual=8 minimum=10 0x76baadda"},
            ]}))
    pending(conn, sig=tx, broadcast=NOW, status="failed", network_id="56",
            reserved_notional_usd=50, resolution_reason="reverted_min_out")

    row = view(conn, attempt_id="a1")
    assert row["status"] == "transaction_failed"
    assert row["reason"] == "Price moved beyond the signed minimum output; no purchase"
    assert row["reservation_resolution_reason"] == "reverted_min_out"
    assert row["tx_sig"] == tx
    assert row["links"]["transaction"] == "https://bscscan.com/tx/" + tx
    assert row["requested_usd"] == 50
    assert row["position_id"] is None and row["fill_id"] is None
    text = render_entry_notification(row)
    assert text.startswith("🔴 TRANSACTION FAILED · COIN · BSC")
    assert "Price moved beyond the signed minimum output; no purchase" in text
    assert 'href="https://bscscan.com/tx/' + tx + '"' in text


def test_bsc_unknown_failure_stays_unknown_despite_prior_min_output_evidence(conn):
    attempt(conn, status="failed", chain="56", terminal_reason="reverted_unknown",
            trace_json=json.dumps({"substage_events": [
                {"name": "evm.revert_reason", "attempt_no": 1, "kind": "min_out",
                 "detail": "SlippageTooHigh(uint256,uint256): actual=9 minimum=10 0x76baadda"},
                {"name": "evm.revert_reason", "attempt_no": 2, "kind": "unknown",
                 "detail": "execution reverted"},
            ]}))
    pending(conn, sig="signed", broadcast=NOW, status="failed", network_id="56",
            resolution_reason="reverted_unknown")
    row = view(conn, attempt_id="a1")
    assert row["status"] == "transaction_failed"
    assert row["reason"] == "reverted_unknown"
    assert row["reservation_resolution_reason"] == "reverted_unknown"


def test_bsc_fill_after_min_output_failure_keeps_successful_purchase(conn):
    attempt(conn, status="filled", chain="56", terminal_reason="filled",
            trace_json=json.dumps({"substage_events": [
                {"name": "evm.revert_reason", "attempt_no": 1, "kind": "min_out",
                 "detail": "SlippageTooHigh(uint256,uint256): actual=9 minimum=10 0x76baadda"},
            ]}))
    pending(conn, sig="signature", broadcast=NOW, status="confirmed", network_id="56")
    pid = fill(conn, chain="56")
    row = view(conn, attempt_id="a1")
    assert row["status"] == "filled"
    assert row["reason"] == "Confirmed entry fill recorded."
    assert row["position_id"] == pid and row["fill_id"] is not None
    assert row["tx_sig"] == "signature" and row["filled_notional"] == 175


def test_recorded_chain_failure_overrides_an_old_orphan_marker(conn):
    attempt(conn, status="unknown")
    pending(conn, sig="signed", broadcast=NOW, status="failed", recovery_status="orphaned",
            resolution_reason="chain_error:custom program error: 0x1771")
    assert view(conn, attempt_id="a1")["status"] == "transaction_failed"


@pytest.mark.parametrize("chain,lane,chain_label,explorer", [
    ("4663", "rh", "Robinhood", "https://robinhoodchain.blockscout.com/tx/"),
    ("56", "bsc", "BSC", "https://bscscan.com/tx/"),
])
def test_evm_fast_revert_is_a_failed_transaction_and_preserves_its_phone_link(
        conn, chain, lane, chain_label, explorer):
    from sniper.services.memecoin_telegram import render_entry_notification

    tx = "0x" + "ab" * 32
    mint = "0x" + "cd" * 20
    origin = lane + "fast:" + chain + ":0x" + "12" * 32 + ":" + mint
    resolution = lane + "_fast_reverted"
    attempt(conn, origin=origin, status="failed", chain=chain, mint=mint,
            ticker="ANR", signal_source=lane + "_fast_lane", delivery_lane=lane + "_fast_lane",
            terminal_reason="reverted_min_out", tx_sig=tx)
    pending(conn, sig=None, broadcast=None, status="failed", network_id=chain,
            mint=mint, origin_event_id=origin, resolution_reason=resolution)

    row = view(conn, attempt_id="a1")
    assert row["status"] == "transaction_failed"
    assert row["label"] == "Transaction failed"
    assert row["reason"] == resolution
    assert row["tx_sig"] == tx
    assert row["position_id"] is None and row["fill_id"] is None
    assert row["links"]["transaction"] == explorer + tx
    text = render_entry_notification(row)
    assert text.startswith("🔴 TRANSACTION FAILED · ANR · " + chain_label)
    assert 'href="' + explorer + tx + '"' in text


@pytest.mark.parametrize("status,resolution,expected", [
    ("failed", "rh_fast_receipt_unknown", "needs_review"),
    ("failed", "rh_fast_reverted_unverified", "needs_review"),
    ("reserved", "rh_fast_reverted", "submitted_unknown"),
])
def test_rh_fast_failure_label_requires_an_exact_terminal_ledger_verdict(
        conn, status, resolution, expected):
    tx = "0x" + "ab" * 32
    attempt(conn, status="failed", chain="4663", signal_source="rh_fast_lane",
            delivery_lane="rh_fast_lane", terminal_reason="reverted_min_out", tx_sig=tx)
    pending(conn, sig=tx, broadcast=NOW, status=status, network_id="4663",
            resolution_reason=resolution)
    row = view(conn, attempt_id="a1")
    assert row["status"] == expected
    assert row["tx_sig"] == tx


@pytest.mark.parametrize("resolution", ["nonce_cancel_confirmed", "broadcast_rejected", "execution_premium", "quote_over_source"])
def test_resolved_signed_reservation_without_chain_failure_is_no_fill(conn, resolution):
    attempt(conn, status="unknown")
    pending(conn, sig="signed", broadcast=NOW, status="failed", resolution_reason=resolution)
    assert view(conn, attempt_id="a1")["status"] == "no_fill"


@pytest.mark.parametrize("resolution", ["pre_tx_failed", "pipeline_not_visible"])
def test_a_reconciled_pre_transaction_is_no_fill_not_needs_review(conn, resolution):
    """LINTCHA (4663, ref 9021c685, 2026-09-23T19:06Z). The unwrap confirmed on chain at block
    70750681, the swap never went out, and the cleanup re-wrap confirmed at nonce 2311 -- so the
    native balance is back where it started and nothing is outstanding. The row still read "Needs
    review", which asks an operator to reconcile something already fully reconciled."""
    attempt(conn, status="aborted")
    pending(conn, sig="0xpre", broadcast=NOW, status="failed", resolution_reason=resolution,
            cleanup_status="confirmed", cleanup_tx_sig="0xcleanup")
    assert view(conn, attempt_id="a1")["status"] == "no_fill"


@pytest.mark.parametrize("cleanup,expected", [(None, "no_fill"), ("confirmed", "no_fill"),
                                              ("explicit_rejected", "needs_review"),
                                              ("unknown", "needs_review")])
def test_a_rejected_broadcast_after_an_unwrap_needs_its_rewrap_proven(conn, cleanup, expected):
    """Reservations 1937 and 3350 (4663), 4581 and 4817 (8453, PICNIC): broadcast_rejected after a
    confirmed unwrap whose re-wrap was explicit_rejected or unknown read No fill, hiding native ETH
    that may still be out. A started cleanup (non-NULL status) must be confirmed; NULL means no
    unwrap was made, so the rejection alone settles the row (Gamma's rule, Alpha yes 2026-10-07)."""
    attempt(conn, status="unknown", chain="8453")
    pending(conn, network_id="8453", sig="signed", broadcast=NOW, status="failed",
            resolution_reason="broadcast_rejected", cleanup_status=cleanup,
            cleanup_tx_sig="0xcleanup" if cleanup else None)
    assert view(conn, attempt_id="a1")["status"] == expected


def test_an_unwrap_the_swap_could_not_see_is_no_fill_once_rewrapped(conn):
    """X402ID (8453, 0xsneakky, reservation 4885, 2026-10-07). The unwrap confirmed at block
    52298205, evm.native_visible read visible=false after 3000 ms, the swap was never sent, and the
    re-wrap confirmed at nonce 59. The only signature on the row is the cleanup's. Fully reconciled,
    yet it read "Needs review" because pre_tx_not_visible was in neither reason set."""
    attempt(conn, status="aborted", chain="8453")
    pending(conn, network_id="8453", status="failed", resolution_reason="pre_tx_not_visible",
            cleanup_status="confirmed", cleanup_tx_sig="0xdddae137",
            recovery_status="native_pre_transaction_ambiguous")
    assert view(conn, attempt_id="a1")["status"] == "no_fill"


@pytest.mark.parametrize("cleanup", [None, "unknown", "failed", "explicit_rejected"])
def test_an_unwrap_the_swap_could_not_see_without_a_confirmed_rewrap_needs_review(conn, cleanup):
    attempt(conn, status="aborted", chain="8453")
    pending(conn, network_id="8453", status="failed", resolution_reason="pre_tx_not_visible",
            cleanup_status=cleanup, cleanup_tx_sig="0xdddae137")
    assert view(conn, attempt_id="a1")["status"] == "needs_review"


@pytest.mark.parametrize("cleanup", [None, "unknown", "failed"])
def test_a_pre_transaction_whose_cleanup_is_unproven_still_needs_review(conn, cleanup):
    """The other half: without a CONFIRMED re-wrap the native ETH is still out there, and that is
    exactly the case a human has to look at. The reason string alone must not settle the row."""
    attempt(conn, status="aborted")
    pending(conn, sig="0xpre", broadcast=NOW, status="failed", resolution_reason="pre_tx_failed",
            cleanup_status=cleanup)
    assert view(conn, attempt_id="a1")["status"] != "no_fill"


def test_a_chain_failure_reason_on_a_live_reservation_stays_unresolved(conn):
    """The reason alone releases nothing: only a FAILED reservation carries a verdict."""
    attempt(conn, status="unknown")
    pending(conn, sig="signed", broadcast=NOW, resolution_reason="chain_error:custom program error: 0x1771")
    assert view(conn, attempt_id="a1")["status"] == "submitted_unknown"


@pytest.mark.parametrize("reason", ["no planned-quantity sell route",
                                    "entry_simulation_min_out: exact route minimum output reverted"])
def test_a_route_refusal_before_signing_reads_refused_not_review(conn, reason):
    """47 route refusals in 30 days read "Needs review" before 2026-09-11 and would have paged."""
    attempt(conn, status="route_failed", terminal_reason=reason)
    pending(conn, status="failed", resolution_reason="route_failed")
    assert view(conn, attempt_id="a1")["status"] == "refused"


def test_a_source_sell_is_not_listed_as_an_entry_result(conn):
    """41 of the last 100 live "entry results" were ignored source sells (2026-09-11)."""
    for eid, side in (("buy1", "buy"), ("sell1", "sell")):
        conn.execute("INSERT INTO memecoin_events(fomo_event_id, source_user_id, mint, network_id, side, "
                     "created_at, action) VALUES (?, 'u1', 'MINT', 'solana', ?, ?, 'ignored')",
                     (eid, side, NOW))
    conn.commit()
    ids = {r["origin_event_id"] for r in m.recent_entry_identities(conn, limit=10)}
    assert "buy1" in ids and "sell1" not in ids



def test_an_unsigned_gate_refusal_keeps_its_own_reason_not_no_fill(conn):
    """OPAI 2026-09-11: pending failed/execution_premium, never signed, attempt refused."""
    attempt(conn, status="refused", terminal_reason="route quote is 1.52x the source price (cap 1.50x)")
    pending(conn, status="failed", resolution_reason="execution_premium")
    row = view(conn, attempt_id="a1")
    assert row["status"] == "refused"
    assert row["reason"] == "route quote is 1.52x the source price (cap 1.50x)"
    assert row["completed_at"] is not None


def test_socket_receipt_remains_separate_from_processing_time(conn):
    from sniper.persistence import db_axiom
    db_axiom.ensure_axiom_tables(conn)
    attempt(conn, status='refused')
    receipt = '2026-09-11T11:59:58+00:00'
    db_axiom.record_j7_receipts(conn, [dict(fomo_event_id='event1',
        received_mono_ns=1, received_at=receipt)])
    row = view(conn, attempt_id='a1')
    assert row['socket_received_at'] == receipt
    assert row['observed_at'] == NOW
    assert row['source_timestamp_trust'] == 'claimed_untrusted'


def test_restart_interrupted_attempt_is_failed_before_submission_not_needs_review(conn):
    # COINU 2026-09-17: the startup sweep closed a restart-killed attempt as aborted/"interrupted:".
    # No reservation and no signature exist, so the dashboard must not page it as Needs review.
    attempt(conn, status="aborted",
            terminal_reason="interrupted: the runner restarted before a decision")
    row = view(conn, attempt_id="a1")
    assert row["status"] == "failed_before_submission"


def test_other_unreserved_aborts_still_need_review(conn):
    attempt(conn, status="aborted", terminal_reason="unexplained abort")
    assert view(conn, attempt_id="a1")["status"] == "needs_review"


@pytest.mark.parametrize("chain", ["solana", "1", "56", "8453", "4663", "5042"])
def test_unsigned_source_initiation_refusal_does_not_need_review(conn, chain):
    # CORNELL d716287e: the slot lane refused before reserving or signing; the
    # confirmed watcher then filled a separate attempt. This is a settled gate result.
    attempt(conn, status="source_initiation_unknown", chain=chain,
            signal_source="slot_lane" if chain == "solana" else "onchain_watch",
            terminal_reason="missing_receipt_proof")
    row = view(conn, attempt_id="a1")
    assert row["status"] == "refused"
    assert row["label"] == "Refused"
    assert row["reason"] == "missing_receipt_proof"
    assert row["position_id"] is None and row["tx_sig"] is None


@pytest.mark.parametrize("broadcast,pending_status,expected", [
    (None, "reserved", "transaction_unresolved"),
    (NOW, "reserved", "submitted_unknown"),
    (NOW, "failed", "needs_review"),
])
def test_source_initiation_refusal_cannot_hide_signed_work(
        conn, broadcast, pending_status, expected):
    attempt(conn, status="source_initiation_unknown", terminal_reason="missing_receipt_proof")
    pending(conn, sig="signature", broadcast=broadcast, status=pending_status,
            resolution_reason="missing_receipt_proof" if pending_status == "failed" else None)
    assert view(conn, attempt_id="a1")["status"] == expected


def test_shadow_is_partial_and_links_only_exact_fast_lane_origin(conn):
    origin = "swap:raybot:signature:owner:mint"
    attempt(conn, status="shadow", origin=origin, delivery_lane="j7_shadow", source_user_id="u",
            terminal_reason="j7_shadow: would enter (gates passed)")
    attempt(conn, aid="fast", origin="onchain:signature", status="refused", source_user_id="u",
            signal_source="onchain_watch",
            terminal_reason="re-entry cooldown: a real position on this coin closed within the window")
    row = view(conn, attempt_id="a1")
    assert row["status"] == "shadow"
    assert "quote checks not run" in row["reason"]
    assert row["fast_lane_results"][0]["attempt_id"] == "fast"
    assert row["fast_lane_results"][0]["status"] == "refused"
    conn.execute("UPDATE memecoin_entry_attempts SET source_user_id='other' WHERE attempt_id='fast'")
    assert view(conn, attempt_id="a1")["fast_lane_results"] == []
    pending(conn, sig="unresolved")
    assert view(conn, attempt_id="a1")["status"] == "transaction_unresolved"


def test_chase_evidence_names_average_vs_post_trade_reference(conn):
    attempt(conn, status="refused", trace_json=json.dumps({"substage_events":[{
        "name":"chase_quote_confirmation", "gap_pct":119.1,"cap_pct":30,
        "source_price":0.00008096,"reference_price":0.00008096,
        "reference_basis":"avg_fill", "quote_price":0.0001774}]}))
    row = view(conn, attempt_id="a1")
    assert row["gate_evidence"][0]["reference_basis"] == "avg_fill"
    assert row["gate_evidence"][0]["gap_pct"] == 119.1


def test_shadow_links_use_resolved_execution_evidence(conn):
    attempt(conn, status="shadow", origin="swap:raybot:signature:owner:mint",
            delivery_lane="j7_shadow", source_user_id="u")
    attempt(conn, aid="fast", origin="onchain:signature", status="refused", source_user_id="u",
            signal_source="onchain_watch")
    pid = pending(conn, aid="fast", sig="signature", broadcast=NOW, origin_event_id="onchain:signature")
    row = view(conn, attempt_id="a1")
    assert row["fast_lane_results"][0]["status"] == "submitted_unknown"
    conn.execute("UPDATE memecoin_pending_entries SET status='confirmed' WHERE id=?", (pid,))
    fill(conn, aid="fast", origin="onchain:signature")
    row = view(conn, attempt_id="a1")
    assert row["fast_lane_results"][0]["status"] == "filled"


def test_operator_exposes_recorded_monotonic_decision_elapsed(conn):
    attempt(conn, status="refused", total_ms=839.2)
    assert view(conn, attempt_id="a1")["decision_elapsed_ms"] == 839.2


def native_secondary(c, *, native_status='failed', damage=None, secondary_status='source_initiation_unknown'):
    from test_memecoin_solana_source_initiation import receipt, proof_for
    tx, owner = receipt()
    proof = dict(proof_for(tx, owner), source_user_id='u1')
    sig, mint = proof['tx_hash'], proof['mint']
    c.execute('INSERT INTO onchain_wallet_detections(signature, network_id, mint, source_user_id, detected_at, address, created_at) VALUES(?,?,?,?,?,?,?)', (sig, 'solana', mint, 'u1', NOW, owner, NOW))
    if damage == 'wallet_address':
        c.execute("UPDATE onchain_wallet_detections SET address='foreign-wallet'")
    facts = dict(signature=sig, network_id='solana', mint=mint, source_user_id='u1',
                 source_slot=tx['slot'], source_initiation=proof)
    native = dict(mint=mint, source_user_id='u1', signal_source='slot_lane',
                  delivery_lane='slot_lane_processed', tx_sig='our-signed-tx',
                  trace_json=json.dumps(dict(source_evidence=facts)))
    if damage == 'unsigned': native['tx_sig'] = None
    elif damage == 'proof': facts['source_initiation'] = None; native['trace_json'] = json.dumps(dict(source_evidence=facts))
    elif damage in ('mint', 'source_user_id', 'network_id'): native[damage] = 'different'
    origin = 'slotlane:' + (sig.swapcase() if damage == 'signature' else sig)
    attempt(c, aid='native', origin=origin, status=native_status, **native)
    if native_status == 'filled': fill(c, aid='native', origin=origin)
    attempt(c, aid='secondary', origin='onchain:'+sig, status=secondary_status,
            terminal_reason='missing_receipt_proof', mint=mint, source_user_id='u1',
            signal_source='onchain_watch')
    return sig


@pytest.mark.parametrize('secondary_status', ['refused', 'source_initiation_unknown'])
@pytest.mark.parametrize('native_status', ['failed', 'filled', 'submitted_unknown'])
def test_exact_native_outcome_owns_unsigned_secondary_refusal(conn, native_status, secondary_status):
    native_secondary(conn, native_status=native_status, secondary_status=secondary_status)
    row = view(conn, attempt_id='secondary')
    assert row['status'] == 'refused'
    assert row.get('native_owned_secondary_refusal') is True
    assert row['duplicate_same_source'] is True
    assert view(conn, attempt_id='native')['status'] != 'refused'


@pytest.mark.parametrize('damage', ['unsigned', 'proof', 'signature', 'mint', 'source_user_id', 'network_id', 'wallet_address'])
def test_secondary_refusal_without_exact_native_proof_stays_independent(conn, damage):
    native_secondary(conn, damage=damage)
    assert not view(conn, attempt_id='secondary').get('native_owned_secondary_refusal')


@pytest.mark.parametrize('same', [True, False])
def test_duplicate_blocker_requires_exact_source_transaction(conn, same):
    sig = native_secondary(conn)
    origin = 'slotlane:' + (sig if same else 'DifferentSignature')
    facts = dict(signature=origin[9:], network_id='solana', mint='MINT', source_user_id='u1', address='wallet-A')
    attempt(conn, aid='blocked', origin=origin, status='refused', source_user_id='u1',
            signal_source='slot_lane', trace_json=json.dumps(dict(source_evidence=facts)),
            gate_inputs_json=json.dumps(dict(duplicate_blocker=dict(attempt_id='native'))))
    conn.execute("UPDATE memecoin_entry_attempts SET mint='MINT' WHERE attempt_id='native'")
    trace = json.loads(conn.execute("SELECT trace_json FROM memecoin_entry_attempts WHERE attempt_id='native'").fetchone()[0])
    trace['source_evidence']['mint'] = 'MINT'
    trace['source_evidence']['address'] = 'wallet-A'
    conn.execute("UPDATE memecoin_entry_attempts SET trace_json=? WHERE attempt_id='native'", (json.dumps(trace),))
    assert view(conn, attempt_id='blocked')['duplicate_same_source'] is same


def test_signed_secondary_fallback_keeps_its_own_outcome(conn):
    native_secondary(conn)
    conn.execute("UPDATE memecoin_entry_attempts SET terminal_status='submitted_unknown', tx_sig='fallback-signed', transaction_submitted_at='2026-09-11T12:00:00+00:00' WHERE attempt_id='secondary'")
    row = view(conn, attempt_id='secondary')
    assert row['status'] == 'submitted_unknown'
    assert not row['native_owned_secondary_refusal']


def test_opaque_feed_blocker_cannot_prove_same_source(conn):
    native_secondary(conn)
    attempt(conn, aid='opaque', origin='feed-uuid', status='refused', source_user_id='u1',
            gate_inputs_json=json.dumps(dict(duplicate_blocker=dict(attempt_id='native'))))
    assert not view(conn, attempt_id='opaque')['duplicate_same_source']


@pytest.mark.parametrize('actor', ['different-wallet', None])
def test_generic_blocker_requires_matching_retained_actor(conn, actor):
    facts = dict(signature='SameSignature', network_id='solana', mint='MINT', source_user_id='u1')
    for aid, address in [('blocker-actor', 'wallet-A'), ('blocked-actor', actor)]:
        retained = dict(facts, address=address) if address else dict(facts)
        attempt(conn, aid=aid, origin='slotlane:SameSignature', status='refused', source_user_id='u1',
                signal_source='slot_lane', trace_json=json.dumps(dict(source_evidence=retained)),
                gate_inputs_json=json.dumps(dict(duplicate_blocker=dict(attempt_id='blocker-actor')))
                    if aid == 'blocked-actor' else None)
    assert not view(conn, attempt_id='blocked-actor')['duplicate_same_source']
