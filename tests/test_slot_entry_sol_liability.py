"""Current-message SOL liabilities use the reservation's existing coherent cash snapshot."""
import base64
from types import SimpleNamespace
import pytest
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from sniper.execution import solana_real_seams as seams, solana_sender_ab as tips
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_risk as risk
from test_memecoin_solana_slot_executor import conn, _reserve

RENT = 6_117_840
NOW = '2026-10-18T10:00:00Z'


def _run(conn, monkeypatch, *, budget=8_117_840, rent=RENT, native=0,
         tip=0, rewrite=False, prior_signature=False, malformed=None, price=50_000_000,
         extra_ixs=(), populate_error=False):
    kp = Keypair()
    ixs = [set_compute_unit_limit(200_000)]
    if price is not None:
        ixs.append(set_compute_unit_price(price))
    if tip:
        ixs += tips.tip_instructions(str(kp.pubkey()), (tips.NOZOMI_TIP_ACCOUNTS[0], tip))
    ixs += list(extra_ixs)
    message = MessageV0.try_compile(kp.pubkey(), ixs, [], Hash.default())
    if native:
        pid = m.reserve_pending(conn, mint='native', source_user_id='wallet', strategy='real',
                                notional=2., reserved_at=NOW, network_id='solana',
                                funding_asset='sol', funding_amount_atomic=native,
                                funding_sol_usd=100.)
        pending = m.pending_row(conn, pid)
    else:
        pending = _reserve(conn)
    if prior_signature:
        m.set_pending_sig(conn, pending['id'], 'already-sent', NOW)
        # Exercise a stale unsigned caller snapshot against a now-owned durable row.
    validity = {'entry_sol_budget': {'pending_id': pending['id'], 'available_lamports': budget},
                'entry_rent_lamports': rent, 'nozomi_tip_lamports': tip,
                'crowded_priority_lamports': 10_000_000}
    if native:
        validity.update(funding_asset='sol', funding_amount_atomic=native)
    if malformed:
        malformed(validity)
    executed, signed, queries = [], [], []
    original = VersionedTransaction

    class SigningSpy:
        populate = staticmethod(original.populate)
        from_bytes = staticmethod(original.from_bytes)

        def __new__(cls, *args):
            signed.append(True)
            return original(*args)

    if populate_error:
        def unavailable(*_args):
            raise ValueError('unsigned serialization unavailable')
        SigningSpy.populate = staticmethod(unavailable)

    monkeypatch.setattr('solders.transaction.VersionedTransaction', SigningSpy)
    from sniper.execution import solana_slot_executor
    monkeypatch.setattr(solana_slot_executor, 'execute_prebuilt_entry',
                        lambda _conn, **kw: executed.append(kw) or {'status': 'unknown', 'sig': kw['sig']})
    monkeypatch.setattr(risk.cfg, 'MEMECOIN_SLOT_LANE_CROWDED_MAX_SEND_LAMPORTS', 16_000_000)
    executor = seams.build_real_solana_executor(
        keypair=kp, rpc_url='http://unused.invalid', entry_bids=object() if rewrite else None,
        http=lambda *_a, **_kw: pytest.fail('liability check added provider I/O'))
    conn.set_trace_callback(queries.append)
    try:
        result = executor['slot_execute_fn'](conn, pending, message, validity=validity, now_iso=NOW)
    finally:
        conn.set_trace_callback(None)
    return result, pending, executed, signed, queries


def test_underfunded_actual_bid_aborts_before_signing_or_transport(conn, monkeypatch):
    result, pending, executed, signed, _ = _run(conn, monkeypatch)
    assert result['status'] == 'aborted'
    assert result['reason'] == 'slot_lane_entry_sol_insufficient'
    assert not signed and not executed
    assert m.pending_row(conn, pending['id'])['status'] == 'failed'


def test_exact_rewritten_bid_budget_passes_without_any_ledger_or_provider_read(conn, monkeypatch):
    # The unsigned message originally asks 20M; the existing crowded cap rewrites it to 10M.
    total = 10_000_000 + 5_000 + 1_000_000 + RENT
    result, _, executed, signed, queries = _run(
        conn, monkeypatch, budget=total, tip=1_000_000, price=100_000_000, rewrite=True)
    assert result['status'] == 'unknown'
    assert len(executed) == len(signed) == 1
    assert queries == []
    tx = VersionedTransaction.from_bytes(base64.b64decode(executed[0]['signed_b64']))
    assert tx.verify_with_results() == [True]


@pytest.mark.parametrize('budget_delta,allowed', [(0, True), (-1, False)])
def test_native_principal_is_in_the_same_budget(conn, monkeypatch, budget_delta, allowed):
    native = 20_000_000
    result, _, executed, signed, _ = _run(
        conn, monkeypatch, budget=native + 10_000_000 + 5_000 + RENT + budget_delta,
        native=native)
    assert (result['status'] == 'unknown') == allowed
    assert len(executed) == len(signed) == int(allowed)


@pytest.mark.parametrize('bad', [
    lambda v: v.update(entry_sol_budget=None),
    lambda v: v['entry_sol_budget'].update(pending_id=-1),
    lambda v: v['entry_sol_budget'].update(available_lamports=True),
    lambda v: v['entry_sol_budget'].update(available_lamports=-1),
    lambda v: v.pop('entry_rent_lamports'),
    lambda v: v.update(entry_rent_lamports=True),
    lambda v: v.update(entry_rent_lamports=-1),
    lambda v: v.update(nozomi_tip_lamports=-1),
])
def test_malformed_evidence_refuses_unsigned(conn, monkeypatch, bad):
    result, _, executed, signed, _ = _run(conn, monkeypatch, budget=1_000_000_000, malformed=bad)
    assert result['status'] == 'aborted'
    assert result['reason'] == 'slot_lane_entry_sol_budget_invalid'
    assert not executed and not signed


def test_refusal_cannot_release_an_already_signed_unknown_owner(conn, monkeypatch):
    result, pending, executed, signed, _ = _run(conn, monkeypatch, prior_signature=True)
    assert result['status'] == 'unknown'
    assert not executed and not signed
    current = m.pending_row(conn, pending['id'])
    assert current['status'] == 'reserved' and current['tx_sig'] == 'already-sent'


def test_unreadable_fee_refuses_before_signing(conn, monkeypatch):
    result, _, executed, signed, _ = _run(conn, monkeypatch, budget=1_000_000_000, price=None)
    assert result['reason'] == 'slot_lane_entry_sol_fee_unknown'
    assert result['status'] == 'aborted' and not executed and not signed


@pytest.mark.parametrize('program', [
    'Ed25519SigVerify111111111111111111111111111',
    'KeccakSecp256k11111111111111111111111111111',
    'Secp256r1SigVerify1111111111111111111111111',
])
def test_unaccounted_signature_precompile_refuses_unsigned(conn, monkeypatch, program):
    ix = Instruction(Pubkey.from_string(program), b'\x01', [])
    result, _, executed, signed, _ = _run(
        conn, monkeypatch, budget=1_000_000_000, extra_ixs=[ix])
    assert result['reason'] == 'slot_lane_entry_sol_fee_unknown'
    assert result['status'] == 'aborted' and not executed and not signed


def test_unsigned_serialization_error_refuses_unsigned(conn, monkeypatch):
    result, _, executed, signed, _ = _run(
        conn, monkeypatch, budget=1_000_000_000, populate_error=True)
    assert result['reason'] == 'slot_lane_entry_sol_fee_unknown'
    assert result['status'] == 'aborted' and not executed and not signed


def test_cash_rounds_down_and_full_book_exit_reserve_rounds_up():
    ctx = SimpleNamespace(sol_balance=.2000000009, pending_sol_lamports=7,
                          chain_concurrent=2, gas_exit_sol=.0000000001, gas_retry_factor=2.)
    assert risk.entry_sol_spend_budget(ctx) == 199_999_992


def test_reservation_publishes_budget_from_existing_context_and_clears_it_on_refusal(conn, monkeypatch):
    monkeypatch.setattr(risk.cfg, 'MEMECOIN_REAL_ENABLED', True)
    monkeypatch.setattr(risk.cfg, 'MEMECOIN_GAS_EXIT_SOL', .0004)
    monkeypatch.setattr(risk.cfg, 'MEMECOIN_GAS_RETRY_FACTOR', 2.)
    # Both an unresolved native principal and history newer than the balance snapshot are debited.
    m.reserve_pending(conn, mint='prior', source_user_id='wallet', strategy='real', notional=10.,
                      reserved_at=NOW, network_id='solana', funding_asset='sol',
                      funding_amount_atomic=100_000_000, funding_overhead_atomic=5_000_000,
                      funding_sol_usd=100.)
    output = {}
    params = dict(source_user_id='wallet', strategy='real', day=NOW[:10], usdc_balance=200.,
                  sol_balance=.2, now_iso=NOW, size_override=20., entry_sol_budget=output,
                  snapshot_debits_fn=lambda: {'pending_ids': (), 'extra_sol_atomic': 10_005_000})
    decision, pid = risk.reserve_entry(conn, mint='new', **params)
    assert decision.allowed and pid is not None
    assert output == {'pending_id': pid, 'available_lamports': 83_395_000}
    decision, pid = risk.reserve_entry(conn, mint='new', **params)
    assert not decision.allowed and pid is None
    assert output == {}
