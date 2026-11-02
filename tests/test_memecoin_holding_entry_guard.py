"""Unknown manual EVM approvals/swaps fence funded entry nonces until terminal proof."""
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m

NOW = '2026-10-07T22:00:00+00:00'
TOKEN = '0x' + '12' * 20


def _conn():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


@pytest.mark.parametrize('chain', ['1', '56', '8453', '4663', '5042', '57073'])
@pytest.mark.parametrize('phase', ['approval', 'swap'])
def test_unknown_holding_send_blocks_only_its_chain_until_terminal_receipt(chain, phase):
    conn = _conn()
    iid = m.enqueue_holding_sell(conn, chain, TOKEN, now_iso=NOW)
    m.resolve_holding_sell(conn, iid,
                          'approval_submitted' if phase == 'approval' else 'submitted',
                          now_iso=NOW, sig='0xmanual', transaction_phase=phase)

    # A fresh/empty provider pending nonce cannot erase this durable identity.
    assert m.unresolved_signed_pending_count(conn, chain) == 1
    assert m.unresolved_signed_pending_count(conn, 'solana') == 0
    other = '1' if chain != '1' else '56'
    assert m.unresolved_signed_pending_count(conn, other) == 0
    m.resolve_holding_sell(conn, iid, 'failed' if phase == 'approval' else 'sold', now_iso=NOW)
    assert m.unresolved_signed_pending_count(conn, chain) == 0


@pytest.mark.parametrize('signed_field,value', [
    (None, None), ('tx_sig', '0xsigned'), ('nonce', 7),
    ('signed_payload_sha256', 'sha'), ('cleanup_tx_sig', '0xcleanup'),
])
def test_entry_blocker_releases_only_a_completely_unsigned_reservation(signed_field, value):
    conn = _conn()
    pid = m.reserve_pending(conn, mint=TOKEN, source_user_id='u', strategy='alpha_b',
                            notional=35.0, reserved_at=NOW, network_id='56')
    if signed_field:
        conn.execute(f'UPDATE memecoin_pending_entries SET {signed_field}=? WHERE id=?',
                     (value, pid))
        conn.commit()
    changed = m.release_unsigned_blocked_entry(conn, pid, 'holding_unresolved')
    row = m.pending_row(conn, pid)
    assert changed is (signed_field is None)
    assert row['status'] == ('failed' if signed_field is None else 'reserved')
    if changed:
        assert row['resolution_reason'] == 'holding_unresolved'
        assert not m.release_unsigned_blocked_entry(conn, pid, 'later_reason')
        assert m.pending_row(conn, pid)['resolution_reason'] == 'holding_unresolved'


@pytest.mark.parametrize('state', ['submitted', 'submitted_unknown'])
@pytest.mark.parametrize('direction', ['unwrap', 'wrap'])
def test_holding_sell_waits_for_gas_transfer_even_when_rpc_pending_is_blind(state, direction):
    from sniper.services import memecoin_evm_holdings as holdings
    conn = _conn()
    rid = m.record_evm_gas_topup_submission(
        conn, now_iso=NOW, day='2026-10-07', chain_id='56', native_amount_atomic=100,
        native_before_atomic=1000, tx_hash='0xgas', nonce=7, direction=direction)
    if state != 'submitted':
        m.finish_evm_gas_topup(conn, row_id=rid, status=state)
    ctx = dict(chain='56', address='0xwallet', pending_nonce_fn=lambda: 7,
               confirmed_nonce_fn=lambda: 7)
    assert holdings.book_busy(conn, ctx) == 'gas_unresolved'
    assert holdings.book_busy(conn, dict(ctx, chain='1')) is None
    m.finish_evm_gas_topup(conn, row_id=rid, status='confirmed')
    assert holdings.book_busy(conn, ctx) is None
