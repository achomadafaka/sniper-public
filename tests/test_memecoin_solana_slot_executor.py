"""The slot lane's prebuilt-entry executor books only what the transaction's own receipt proves.

A false `filled` opens a funded position that does not exist; a false release frees capacity while
a transaction may still land. Each case drives the real ledger schema.
"""
import contextlib
import sqlite3
import time

import pytest

from sniper.execution.solana_slot_executor import execute_prebuilt_entry
from sniper.persistence import db_memecoin as m


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "ledger.db")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    c.commit()
    with contextlib.closing(c):
        yield c


def _reserve(conn, mint="MintSlot1", notional=20.0):
    pid = m.reserve_pending(conn, mint=mint, source_user_id="wallet", strategy="alpha_b",
                            notional=notional, reserved_at="2026-09-26T20:00:00+00:00",
                            network_id="solana", origin_event_id="slot:sig:1",
                            signal_source="slot_lane", source_price=0.0001)
    return m.pending_row(conn, pid)


class _Status(str):
    pass


def _run(conn, pending, *, send=True, status="confirmed", fill=(19.9, 150000.0, 6)):
    def send_fn(_b64):
        return send
    return execute_prebuilt_entry(
        conn, pending=pending, signed_b64="AA==", sig="SigSlot1",
        validity=({"funding_asset": "sol", "funding_amount_atomic": int(
            pending["funding_amount_atomic"])} if pending.get("funding_asset") == "sol" else None),
        send_fn=send_fn, confirm_fn=lambda _sig: _Status(status),
        receipt_fill_fn=lambda _sig, _mint, _max: fill,
        now_iso="2026-09-26T20:00:01+00:00", sleep_fn=lambda _s: None)


def _pending_status(conn, pid):
    return conn.execute("SELECT status FROM memecoin_pending_entries WHERE id=?",
                        (pid,)).fetchone()[0]


def test_confirmed_with_receipt_opens_the_position_at_the_receipt_fill(conn):
    pending = _reserve(conn)
    result = _run(conn, pending)
    assert result["status"] == "filled"
    pos = conn.execute("SELECT entry_notional, entry_price, execution_mode, network_id, "
                       "quote_route_target FROM memecoin_positions WHERE id=?",
                       (result["position_id"],)).fetchone()
    assert pos["entry_notional"] == pytest.approx(19.9)
    assert pos["entry_price"] == pytest.approx(19.9 / 150000.0)
    assert (pos["execution_mode"], pos["network_id"]) == ("real", "solana")
    assert pos["quote_route_target"] == "slot_lane"
    assert _pending_status(conn, pending["id"]) == "confirmed"


def test_native_funded_fill_uses_its_receipt_spend_and_locked_sol_price(conn):
    pid = m.reserve_pending(conn, mint="native-coin", source_user_id="wallet", strategy="alpha_b",
                            notional=50.0, reserved_at="2026-09-27T15:00:00Z",
                            network_id="solana", signal_source="slot_lane", funding_asset="sol",
                            funding_amount_atomic=500_000_000, funding_sol_usd=100.0)
    pending = m.pending_row(conn, pid)
    fill = {"sol_spent_lamports": 499_000_000, "got": 10.0, "decimals": 6,
            "fee_lamports": 100_000, "rent_lamports": 2_039_280}
    result = _run(conn, pending, fill=fill)
    assert result["status"] == "filled"
    assert result["notional"] == pytest.approx(49.9)
    assert result["funding_asset"] == "sol"
    assert result["network_fee_lamports"] == 100_000
    assert result["rent_lamports"] == 2_039_280
    assert conn.execute("SELECT entry_notional FROM memecoin_positions").fetchone()[0] \
        == pytest.approx(49.9)


def test_native_builder_mismatch_refuses_before_any_broadcast(conn):
    pid = m.reserve_pending(conn, mint="native-coin", source_user_id="wallet", strategy="alpha_b",
                            notional=50.0, reserved_at="2026-09-27T15:00:00Z",
                            network_id="solana", signal_source="slot_lane", funding_asset="sol",
                            funding_amount_atomic=500_000_000, funding_sol_usd=100.0)
    sent = []
    result = execute_prebuilt_entry(
        conn, pending=m.pending_row(conn, pid), signed_b64="AA==", sig="SigSlot1",
        validity={"funding_asset": "sol", "funding_amount_atomic": 499_000_000},
        send_fn=lambda _b64: sent.append(True), confirm_fn=lambda _sig: "unknown",
        receipt_fill_fn=lambda *_a: None, now_iso="2026-09-27T15:00:01Z")
    assert result["status"] == "aborted" and not sent
    assert m.pending_row(conn, pid)["status"] == "failed"


def test_on_chain_failure_releases_the_reservation(conn):
    pending = _reserve(conn)
    result = _run(conn, pending, status="failed")
    assert result["status"] == "failed"
    assert _pending_status(conn, pending["id"]) == "failed"
    assert conn.execute("SELECT COUNT(*) FROM memecoin_positions").fetchone()[0] == 0


def test_broadcast_refusal_releases_the_reservation(conn):
    pending = _reserve(conn)
    assert _run(conn, pending, send=False)["status"] == "failed"
    assert _pending_status(conn, pending["id"]) == "failed"


def test_unknown_outcome_keeps_the_reservation_and_opens_nothing(conn):
    pending = _reserve(conn)
    assert _run(conn, pending, status="pending")["status"] == "unknown"
    assert _pending_status(conn, pending["id"]) == "reserved"
    assert conn.execute("SELECT COUNT(*) FROM memecoin_positions").fetchone()[0] == 0


@pytest.mark.parametrize("fill", [None, (0.0, 1.0, 6), (25.0, 1.0, 6), (float("nan"), 1.0, 6)])
def test_unprovable_receipt_is_an_anomaly_that_stays_reserved(conn, fill):
    # 25.0 exceeds the 20.0 reservation: a receipt that spent more than reserved is not our fill.
    pending = _reserve(conn)
    result = _run(conn, pending, fill=fill)
    assert result["status"] == "anomaly"
    assert _pending_status(conn, pending["id"]) == "reserved"
    assert conn.execute("SELECT COUNT(*) FROM memecoin_positions").fetchone()[0] == 0


def _run_credit(conn, pending, *, surplus, sol_usd=150.0, fill=(20.0, 150000.0, 6)):
    return execute_prebuilt_entry(
        conn, pending=pending, signed_b64="AA==", sig="SigSlot1", validity=None,
        send_fn=lambda _b64: True, confirm_fn=lambda _sig: _Status("confirmed"),
        receipt_fill_fn=lambda _sig, _mint, _max: fill, sol_surplus_fn=lambda _sig: surplus,
        sol_usd=sol_usd, now_iso="2026-09-26T20:00:01+00:00", sleep_fn=lambda _s: None)


def test_the_unspent_sol_surplus_is_credited_back_to_the_token_cost(conn):
    """Operator order 2026-09-26: 0.002 SOL left in the wallet at $150/SOL is $0.30 that did not
    buy tokens, so entry_notional is $20.00 - $0.30."""
    result = _run_credit(conn, _reserve(conn), surplus=2_000_000)
    assert result["status"] == "filled"
    assert result["usdc_spent"] == pytest.approx(20.0)
    assert result["sol_surplus_credit_usd"] == pytest.approx(0.30)
    pos = conn.execute("SELECT entry_notional, entry_price FROM memecoin_positions WHERE id=?",
                       (result["position_id"],)).fetchone()
    assert pos["entry_notional"] == pytest.approx(19.70)
    assert pos["entry_price"] == pytest.approx(19.70 / 150000.0)


@pytest.mark.parametrize("surplus,sol_usd", [(0, 150.0), (-5, 150.0), (2_000_000, None),
                                             (10**12, 150.0)])
def test_no_credit_when_there_is_nothing_sane_to_credit(conn, surplus, sol_usd):
    # zero, negative, unknown SOL price, or a credit larger than the whole spend
    result = _run_credit(conn, _reserve(conn), surplus=surplus, sol_usd=sol_usd)
    assert result["status"] == "filled" and result["sol_surplus_credit_usd"] == 0.0
    assert conn.execute("SELECT entry_notional FROM memecoin_positions").fetchone()[0] \
        == pytest.approx(20.0)


def test_receipt_surplus_counts_native_and_wrapped_sol_for_the_owner_only():
    from sniper.execution.solana_slot_executor import receipt_owner_sol_surplus
    wsol = "So11111111111111111111111111111111111111112"
    tx = {"transaction": {"message": {"accountKeys": [{"pubkey": "Owner"}, {"pubkey": "Other"}]}},
          "meta": {"preBalances": [1_000, 5], "postBalances": [1_700, 9],
                   "preTokenBalances": [{"mint": wsol, "owner": "Owner",
                                         "uiTokenAmount": {"amount": "100"}}],
                   "postTokenBalances": [{"mint": wsol, "owner": "Owner",
                                          "uiTokenAmount": {"amount": "400"}},
                                         {"mint": wsol, "owner": "Other",
                                          "uiTokenAmount": {"amount": "999"}}]}}
    assert receipt_owner_sol_surplus(tx, "Owner") == 700 + 300
    tx["meta"]["postBalances"][0] = 0          # a net outflow is never a credit
    tx["meta"]["postTokenBalances"][0]["uiTokenAmount"]["amount"] = "100"
    assert receipt_owner_sol_surplus(tx, "Owner") == 0


def test_the_signing_seam_refuses_a_late_automatic_copy_before_signing(conn, monkeypatch):
    from solders.hash import Hash
    from solders.keypair import Keypair
    from solders.message import MessageV0
    from types import SimpleNamespace
    from sniper.execution import solana_real_seams as seams, solana_slot_executor as executor_module
    kp = Keypair()
    events, called = [], []
    trace = SimpleNamespace(mark_substage=lambda name, **kw: events.append((name, kw)),
                            mark=lambda name, **kw: events.append((name, kw)))
    def execute(*args, **kwargs):
        called.append(kwargs['sig'])
        return {'status': 'unknown', 'sig': kwargs['sig']}
    monkeypatch.setattr(executor_module, 'execute_prebuilt_entry', execute)
    executor = seams.build_real_solana_executor(keypair=kp, rpc_url='http://unused',
        submission_transport=SimpleNamespace(submit=lambda *_a: None))
    late = {'source_slot': 12, 'head_slot': 20, 'head_age_ms': 5.0, 'head_fresh': True,
            'signal_age_ms': 900.0}
    result = executor['slot_execute_fn'](
        conn, _reserve(conn), MessageV0.try_compile(kp.pubkey(), [], [], Hash.default()),
        now_iso='2026-09-26T20:00:01+00:00', trace=trace, boundary_fn=lambda: dict(late),
        entry_min_slot=12, entry_max_slot=13, entry_head_fn=lambda: (20, time.monotonic()))
    assert not called and not events
    assert result['status'] == 'aborted' and result['reason'] == 'entry_slot_expired'


def test_signing_seam_reads_native_receipt_for_native_reservation(conn, monkeypatch):
    from types import SimpleNamespace
    from solders.hash import Hash
    from solders.keypair import Keypair
    from solders.message import MessageV0
    from sniper.execution import solana_real_seams as seams, solana_slot_executor as slot
    kp = Keypair()
    owner = str(kp.pubkey())
    pid = m.reserve_pending(conn, mint="native-coin", source_user_id="wallet", strategy="alpha_b",
                            notional=50.0, reserved_at="2026-09-27T15:00:00Z",
                            network_id="solana", funding_asset="sol",
                            funding_amount_atomic=500_000_000, funding_sol_usd=100.0)

    def cached(_read):
        def receipt(sig):
            return {"transaction": {"signatures": [sig], "message": {"accountKeys": [
                {"pubkey": owner, "signer": True}, {"pubkey": "token", "signer": False}]}},
                    "meta": {"err": None, "fee": 100_000,
                             "preBalances": [1_000_000_000, 0],
                             "postBalances": [497_960_720, 2_039_280],
                             "preTokenBalances": [],
                             "postTokenBalances": [{"accountIndex": 1, "mint": "native-coin",
                                                    "owner": owner, "uiTokenAmount": {
                                                        "amount": "1000000", "decimals": 6}}]}}
        return receipt
    monkeypatch.setattr(seams, "successful_receipt_cache", cached)

    def inspect(_conn, **kw):
        proof = kw["receipt_fill_fn"](kw["sig"], "native-coin", 50.0)
        return {"status": "probe", "proof": proof}
    monkeypatch.setattr(slot, "execute_prebuilt_entry", inspect)
    seam = seams.build_real_solana_executor(keypair=kp, rpc_url="http://unused",
        submission_transport=SimpleNamespace(submit=lambda *_a: None))
    result = seam["slot_execute_fn"](
        conn, m.pending_row(conn, pid),
        MessageV0.try_compile(kp.pubkey(), [], [], Hash.default()),
        validity={"funding_asset": "sol", "funding_amount_atomic": 500_000_000},
        now_iso="2026-09-27T15:00:01Z", entry_min_slot=12, entry_max_slot=13,
        entry_head_fn=lambda: (13, time.monotonic()))
    assert result["proof"]["sol_spent_lamports"] == 499_900_000


def test_failure_receipt_proof_reuses_existing_rpc_read():
    """The 6042 diagnosis comes from the getTransaction the failed verdict already makes."""
    import json
    from pathlib import Path
    from sniper.execution import solana_real_seams as seams
    row = json.loads((Path(__file__).parent / 'fixtures/memecoin/slot_price_limit_a47a6c14.json')
                     .read_text())[4]
    receipt = {'slot': row['slot'], 'meta': {'err': row['err'], 'fee': row['fee_lamports'],
                                             'logMessages': row['failure_logs']}}
    calls = []
    def http(method, url, **kwargs):
        request = json.loads(kwargs['body'])
        calls.append(request['method'])
        if request['method'] == 'getSignatureStatuses':
            return 200, {'result': {'value': [{'err': row['err']}]}}, None
        return 200, {'result': receipt}, None
    status = seams.rpc_confirm('sig', 'http://unused', http=http, max_attempts=1)
    assert status == 'failed' and status.pump_slippage['min_tokens_atomic'] == 21_600_089_757_031
    assert status.landing_slot == 450797211
    assert calls == ['getSignatureStatuses', 'getTransaction']

@pytest.mark.usefixtures("fake_payload_clamp")
def test_signing_mark_covers_the_guarded_sign_call(conn, monkeypatch):
    from solders.keypair import Keypair
    from solders.hash import Hash
    from solders.message import MessageV0
    from types import SimpleNamespace
    from sniper.execution import solana_real_seams as seams, solana_slot_executor as executor_module
    kp = Keypair()
    message = MessageV0.try_compile(kp.pubkey(), [], [], Hash.default())
    events = []
    trace = SimpleNamespace(mark_substage=lambda name, **kw: events.append((name, kw)),
                            mark=lambda name, **kw: events.append((name, kw)))
    def execute(*args, **kwargs):
        assert events[-1][0] == 'signing'
        assert events[-1][1]['duration_ms'] >= 0
        return {'status': 'unknown', 'sig': kwargs['sig']}
    monkeypatch.setattr(executor_module, 'execute_prebuilt_entry', execute)
    executor = seams.build_real_solana_executor(keypair=kp, rpc_url='http://unused',
        submission_transport=SimpleNamespace(submit=lambda *_a: None))
    result = executor['slot_execute_fn'](
        conn, _reserve(conn), message, now_iso='2026-09-26T20:00:01+00:00', trace=trace,
        boundary_fn=lambda: {'source_slot': 12, 'head_slot': 13},
        entry_min_slot=12, entry_max_slot=13, entry_head_fn=lambda: (13, time.monotonic()))
    assert result['status'] == 'unknown' and result['sig'] and result['head_minus_s'] == 1
    # option B (2026-10-22): the entry fee clamp now runs on every slot-lane entry and marks its bid
    assert [e[0] for e in events] == ['slot_pre_sign', 'entry_bid_signed', 'timing_local_sign', 'signing']
    assert events[2][1]['timing_basis'] == 'local_solders_signature'
    assert events[2][1]['duration_ms'] == events[3][1]['duration_ms']


def test_transport_clock_boundaries_follow_binding_and_precede_audit():
    from types import SimpleNamespace
    from sniper.execution.submission_wiring import guarded_submit
    events = []
    def persist(request):
        events.append('bound')
        return True
    def submit(request):
        events.append('submitted')
        return 'result'
    out = guarded_submit(SimpleNamespace(submit=submit), None, persist_fn=persist,
                         record_fn=lambda result: events.append('audit'),
                         observe_fn=lambda phase: events.append(phase))
    assert out == 'result'
    assert events == ['bound', 'dispatch', 'submitted', 'ack', 'audit']


def test_an_aged_signal_in_the_same_window_keeps_the_onchain_guard(conn, monkeypatch):
    from solders.keypair import Keypair
    from solders.hash import Hash
    from solders.message import MessageV0
    from types import SimpleNamespace
    from sniper.execution import solana_real_seams as seams
    kp = Keypair()
    message = MessageV0.try_compile(kp.pubkey(), [], [], Hash.default())
    bound = False
    original = m.bind_entry_submission
    def bind(*args, **kwargs):
        nonlocal bound
        result = original(*args, **kwargs)
        bound = True
        return result
    monkeypatch.setattr(m, 'bind_entry_submission', bind)
    def dispatched(*args, **kwargs):
        raise RuntimeError('dispatched')
    def boundary():
        return {'source_slot': 12, 'head_slot': 13, 'signal_age_ms': 600 if bound else 1}
    events = []
    trace = SimpleNamespace(mark_substage=lambda name, **kw: events.append((name, kw)),
                            mark=lambda name, **kw: events.append((name, kw)))
    executor = seams.build_real_solana_executor(keypair=kp, rpc_url='http://unused', http=dispatched,
                                                submission_transport=SimpleNamespace(submit=dispatched))
    pending = _reserve(conn)
    with pytest.raises(RuntimeError, match='dispatched'):
        executor['slot_execute_fn'](conn, pending, message, trace=trace,
            now_iso='2026-09-26T20:00:01+00:00', boundary_fn=boundary,
            entry_min_slot=12, entry_max_slot=13, entry_head_fn=lambda: (13, time.monotonic()))
    assert bound
    names = [e[0] for e in events]
    # recorded after the identity bind, immediately before the transport, with the late age
    assert names[-2:] == ['slot_pre_send', 'slot_transport_dispatch']
    assert events[-2][1]['signal_age_ms'] == 600
    row = m.pending_row(conn, pending['id'])
    assert row['status'] == 'reserved' and row['tx_sig'] and row['signed_payload_b64']


def test_after_successful_settlement_records_same_cached_receipt_atoms(conn, monkeypatch):
    from solders.hash import Hash
    from solders.keypair import Keypair
    from solders.message import MessageV0
    from types import SimpleNamespace
    from sniper.execution import solana_real_seams as seams, solana_slot_executor as executor_module
    from sniper.services import memecoin_execution_observer as obs
    kp = Keypair()
    events, seen, receipts = [], [], []
    full = {'slot': 100, 'meta': {'err': None, 'computeUnitsConsumed': 42}}
    def cached(_read):
        def get(sig):
            receipts.append(sig)
            return full
        return get
    monkeypatch.setattr(seams, 'successful_receipt_cache', cached)
    monkeypatch.setattr(executor_module, 'execute_prebuilt_entry', lambda *_a, **kw:
        {'status': 'filled', 'sig': kw['sig']})
    def extract(value, **kw):
        seen.append((value, kw))
        return {'state': 'unknown', 'reason': 'fixture_incomplete'}
    monkeypatch.setattr(obs, 'receipt_execution_evidence', extract)
    trace = SimpleNamespace(mark_substage=lambda name, **kw: events.append((name, kw)),
                            mark=lambda name, **kw: events.append((name, kw)))
    executor = seams.build_real_solana_executor(keypair=kp, rpc_url='http://unused',
        submission_transport=SimpleNamespace(submit=lambda *_a: None))
    result = executor['slot_execute_fn'](conn, _reserve(conn),
        MessageV0.try_compile(kp.pubkey(), [], [], Hash.default()),
        now_iso='2026-09-26T20:00:01+00:00', trace=trace,
        entry_min_slot=99, entry_max_slot=100, entry_head_fn=lambda: (100, time.monotonic()))
    assert result['status'] == 'filled' and len(receipts) == 1
    assert seen[0][0] is full and seen[0][1]['payment_asset'] == 'usdc'
    assert events[-1][0] == 'receipt_execution_evidence'
    assert events[-1][1]['evidence']['state'] == 'unknown'


from entry_clamp_fake import fake_payload_clamp  # noqa: E402,F401
