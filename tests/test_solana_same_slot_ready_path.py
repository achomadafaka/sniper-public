"""Ready native entry keeps authority and signed identity durable before dispatch.

This proves local readiness through the first provider invocation, not a network write or fill.
"""
import base64
import dataclasses
import json
import sqlite3
import time
from pathlib import Path

import pytest
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from sniper.execution import exec_lock, pump_curve_buy as pcb, solana_real_seams as seams
from sniper.execution.submission_transport import AttemptState, SubmissionAttempt, SubmissionTransport
from sniper.execution.submission_wiring import flush_async_audit
from sniper.persistence import db_memecoin as db
from sniper.persistence.db_core import get_connection
from sniper.services import memecoin_real_runner as runner, memecoin_slot_lane as lane
from sniper.services.memecoin_entry_trace import EntryAttemptTrace
from sniper.services.memecoin_slot_signal import SlotBuy

SOURCE = json.loads((Path(__file__).parent / "fixtures/memecoin/slot_price_limit_a47a6c14.json").read_text())[0]
EVENT = pcb.parse_trade_event(SOURCE["trade_event_log"])
NOW = "2026-10-11T20:00:00+00:00"
BLOCKHASH = "11111111111111111111111111111111"


@pytest.fixture
def ready(tmp_path, monkeypatch):
    path = tmp_path / "ready.db"
    conn = get_connection(str(path))
    db.ensure_memecoin_tables(conn)
    conn.commit()
    sql, network, sent, errors = [], [], [], []
    conn.set_trace_callback(sql.append)
    kp = Keypair()
    monkeypatch.setattr(exec_lock, "_lock_path", lambda key, lock_dir=None: str(tmp_path / "wallet.lock"))
    for name, value in {
        "MEMECOIN_REAL_ENABLED": True,
        "MEMECOIN_SOLANA_SLOT_LANE_MODE": "live",
        "MEMECOIN_REAL_SIZING": "fixed",
        "MEMECOIN_TRADE_SIZE_USD": 10.0,
        "MEMECOIN_FUNDED_REGIME_RISK_SCALE": 1.0,
        "MEMECOIN_REAL_MAX_MCAP_USD": 250_000.0,
        "MEMECOIN_REAL_MIN_SOURCE_BUY_USD": 0.0,
        "MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO": 0.0,
        "MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO_BY_BAND": {},
        "MEMECOIN_REAL_CHASE_MAX_PCT": 30.0,
        "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT": 35.0,
        "MEMECOIN_SOLANA_MAX_CLIP_USD_ABOVE_NANO": 0.0,
    }.items():
        monkeypatch.setattr(lane.cfg, name, value)
    monkeypatch.setattr(lane.cfg, "memecoin_slot_lane_resend_offsets_ms", lambda: ())
    # The observational mark worker is outside admission and owns another database/thread.
    monkeypatch.setattr(runner, "_start_momentum_read", lambda *_: None)

    def forbidden(*args, **kwargs):
        network.append((args, kwargs))
        raise AssertionError("ready path made network request")

    monkeypatch.setattr(seams, "_rpc_call", forbidden)
    monkeypatch.setattr(seams, "_jup_call", forbidden)
    monkeypatch.setattr(seams, "rpc_confirm", forbidden)
    monkeypatch.setattr(pcb, "_funded_jup_call", forbidden)
    builder = pcb.SlotLaneCurveBuilder(user=str(kp.pubkey()), rpc=forbidden, jup_call=forbidden)
    builder._buyback = EVENT["fee_recipient"]
    timings = {}
    mark_substage = EntryAttemptTrace.mark_substage

    def observe_substage(trace, name, **fields):
        if name == "timing_local_sign":
            timings["local_sign_ms"] = fields["duration_ms"]
        return mark_substage(trace, name, **fields)

    monkeypatch.setattr(EntryAttemptTrace, "mark_substage", observe_substage)

    class Provider:
        provider_id = "solana:tpu"

        def submit(self, request):
            timings["dispatch_at"] = time.perf_counter()
            sent.append(request)
            try:
                # Another connection can see only committed reservation and signed bytes.
                with sqlite3.connect(path) as witness:
                    witness.row_factory = sqlite3.Row
                    pending = db.pending_row(witness, int(request.owner_id))
                    assert pending is not None, "reservation was not committed before dispatch"
                    assert pending["status"] == "reserved"
                    assert pending["funded_contract_json"] and pending["funded_contract_hash"]
                    assert pending["origin_event_id"] == "slotlane:" + SOURCE["signature"]
                    assert pending["signed_payload_b64"] == request.signed_payload, "signed bytes were not committed before dispatch"
                    assert pending["tx_sig"] == request.tx_identity
                    assert pending["submission_bound_at"] is not None
                    signed = VersionedTransaction.from_bytes(base64.b64decode(request.signed_payload))
                    assert signed.verify_with_results() == [True]
                    assert str(signed.message.account_keys[0]) == str(kp.pubkey()) != EVENT["user"]
                    assert str(signed.signatures[0]) == request.tx_identity
                    assert request.skip_preflight is True
            except Exception as exc:
                errors.append(exc)
            return SubmissionAttempt(self.provider_id, AttemptState.ACCEPTED,
                                     request.tx_identity, 0.0, "fixture", None)

    transport = SubmissionTransport([Provider()], deadline_s=1.0)
    executor = seams.build_real_solana_executor(
        keypair=kp, rpc_url="https://offline.invalid", http=forbidden, usdc_fn=forbidden,
        submission_transport=transport, now_fn=lambda: NOW)
    confirmations = lane.SlotConfirmations(
        conn_factory=lambda: get_connection(str(path)), put=lambda _: None, start_worker=lambda _: None)

    def snapshot():
        observed = time.monotonic()
        return lane.PrefetchSnapshot(sol_usd=150.0, usdc_balance=400.0, sol_balance=1.0,
            blockhash=BLOCKHASH, last_valid_height=1,
            observed_mono={name: observed for name in ("sol_usd", "balances", "blockhash")})

    live = lane.SlotLane(conn=conn, executor=executor,
        build_fn=lane.dispatch_build({"pump_curve": builder}), snapshot_fn=snapshot,
        armed_ids_fn=lambda: {"w1"}, kill_tripped_fn=lambda: False, now_iso_fn=lambda: NOW,
        head_slot_fn=lambda: (SOURCE["slot"], time.monotonic()),
        lock_key="ready-fixture", mode="live", shadow_clip_fn=lambda: 10.0,
        sol_buffer_cap_usd=50.0, confirmations=confirmations)

    def buy(**changes):
        fields = dict(venue="pump_curve", source_user_id="w1", handle="fixture", owner=EVENT["user"],
            signature=SOURCE["signature"], slot=SOURCE["slot"], mint=EVENT["mint"], pool=None,
            sol_amount=EVENT["sol_amount"], token_amount=EVENT["token_amount"],
            virtual_sol_reserves=EVENT["virtual_sol_reserves"], virtual_token_reserves=EVENT["virtual_token_reserves"],
            real_sol_reserves=EVENT["real_sol_reserves"], real_token_reserves=EVENT["real_token_reserves"],
            token_program=pcb.TOKEN_PROGRAM, trade_event_log=SOURCE["trade_event_log"],
            recv_monotonic=time.monotonic(), recv_utc=NOW)
        fields.update(changes)
        return SlotBuy(**fields)

    yield dict(conn=conn, lane=live, buy=buy, builder=builder, sent=sent, errors=errors,
               network=network, sql=sql, timings=timings)
    transport.close()
    assert flush_async_audit(timeout_s=2.0)
    conn.close()


def _dispatch(ready):
    buy = ready["buy"]()
    start = time.perf_counter()
    result = ready["lane"].handle(buy)
    ready["timings"]["authority_to_dispatch_ms"] = (ready["timings"].get("dispatch_at", start) - start) * 1000
    assert result["status"] == "submitted", result
    assert len(ready["sent"]) == 1
    assert not ready["network"], "ready path made a provider read"
    assert not ready["errors"], ready["errors"]
    return result


def test_ready_authority_reserves_builds_signs_and_commits_before_first_dispatch(ready):
    # Moving persistence after transport or adding a cold read breaks this whole path.
    result = _dispatch(ready)
    assert ready["sql"].count("COMMIT") >= 3  # shell, reservation, signed identity
    assert db.pending_row(ready["conn"], result["pending_id"])["funding_asset"] == "sol"
    assert ready["conn"].execute("SELECT COUNT(*) FROM memecoin_positions").fetchone()[0] == 0
    assert result["_settlement"] is not None  # confirmation has not become fake fill evidence


def test_ready_proof_detects_a_missing_signed_payload_commit(ready, monkeypatch):
    # Runnable negative control: the acceptance must reject send-before-persist.
    monkeypatch.setattr(db, "bind_entry_submission", lambda *_a, **_k: True)
    with pytest.raises(AssertionError, match="signed bytes were not committed before dispatch"):
        _dispatch(ready)


def test_cold_buyback_facts_refuse_without_any_provider_read_or_dispatch(ready):
    ready["builder"]._buyback = None
    result = ready["lane"].handle(ready["buy"]())
    assert result["status"] == "aborted"
    assert result["reason"] == "slot lane build refused: no_buyback_recipient"
    assert not ready["sent"] and not ready["network"]
    pending = db.pending_row(ready["conn"], result["pending_id"])
    assert pending["status"] == "failed" and pending["signed_payload_b64"] is None


def test_preparation_does_not_renew_original_source_receipt_window(ready):
    buy = ready["buy"](recv_monotonic=time.monotonic() - 0.501)
    prepared = dataclasses.replace(buy, token_program=pcb.TOKEN_PROGRAM)
    result = ready["lane"].handle(prepared)
    assert result["status"] == "rejected" and result["reason"] == "slot_lane_signal_too_old"
    assert prepared.recv_monotonic == buy.recv_monotonic
    assert not ready["sent"] and not ready["network"]
    assert ready["conn"].execute("SELECT COUNT(*) FROM memecoin_pending_entries").fetchone()[0] == 0


def test_cold_exotic_quote_hands_off_without_authority_and_keeps_original_deadline(ready, monkeypatch):
    from test_slot_lane_pump_nonsol import SPCX, _facts, _slot_buy
    monkeypatch.setattr(lane.cfg, "MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES", frozenset({"pump_amm"}))
    live, offered = ready["lane"], []
    live.amm_pools = _facts(SPCX)
    live.pending_legs = lane.PendingLegs()
    live.exotic_offer = lambda buy, **fields: offered.append((buy, fields)) or True
    buy = _slot_buy(SPCX, source_user_id="w1", recv_utc=NOW)
    result = live.handle(buy)
    assert result == {"status": "rejected", "reason": "slot_lane_exotic_leg_pending", "exotic_pending": True}
    assert offered[0][0] is buy
    assert live.pending_legs.holds(buy.signature, now=buy.recv_monotonic + 0.499)
    assert not live.pending_legs.holds(buy.signature, now=buy.recv_monotonic + 0.501)
    assert not ready["sent"] and not ready["network"]
    assert ready["conn"].execute("SELECT COUNT(*) FROM memecoin_pending_entries").fetchone()[0] == 0
