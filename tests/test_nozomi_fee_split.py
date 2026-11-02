"""Organic fee allocation changes signed fees, never provider membership or policy caps."""
import base64
import hashlib
import sqlite3
import struct

import pytest
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.transaction import VersionedTransaction

from sniper.execution import solana_sender_ab as A
from sniper.execution.solana_entry_bids import EntryBidCache, _Sample


RULE = 'nozomi-fee-split-v1'
ACTIVATION = {
    'MEMECOIN_NOZOMI_FEE_SPLIT_AB': '1',
    'MEMECOIN_SLOT_LANE_NOZOMI_AB': '1',
    'MEMECOIN_NOZOMI_ALL_B': '1',
    'MEMECOIN_NOZOMI_ALL_LANES': '1',
    'MEMECOIN_SOLANA_ENTRY_BIDS_ENABLED': '1',
    'MEMECOIN_NOZOMI_API_KEY': 'offline-unit-fixture',
}


@pytest.fixture(autouse=True)
def activation(monkeypatch):
    for key, value in ACTIVATION.items():
        monkeypatch.setenv(key, value)


def expected_arm(attempt_id):
    digest = hashlib.sha256((RULE + ':' + attempt_id).encode()).digest()
    return ('T' if int.from_bytes(digest[:4], 'big') % 2 else 'C'), digest[:4].hex()


def attempt_for(arm):
    return next(str(i) for i in range(100) if expected_arm(str(i))[0] == arm)


def cache():
    def forbidden(*a, **k):
        pytest.fail('fee selection performed provider I/O')
    return EntryBidCache(forbidden, forbidden, clock=lambda: 100)


def message(*, tip=6_000_000, limit=152_000, price=40_000_000):
    payer = Keypair().pubkey()
    ixs = ([set_compute_unit_limit(limit)] if limit is not None else [])
    ixs += [set_compute_unit_price(price)]
    ixs += A.tip_instructions(str(payer), (A.NOZOMI_TIP_ACCOUNTS[0], tip))
    return MessageV0.try_compile(payer, ixs, [], Hash.default())


def test_fee_split_is_domain_separated_and_keeps_every_provider_arm_b():
    seen = set()
    for i in range(128):
        aid = str(i)
        arm = A.nozomi_arm_for(aid, tip_lamports_fn=lambda: 1_250_000)
        expected, digest = expected_arm(aid)
        seen.add(arm.get('fee_arm'))
        assert arm['arm'] == 'B'
        assert arm['rule'] == RULE
        assert arm['fee_arm'] == expected
        assert arm['fee_assignment_hash'] == digest
        assert arm['tip_account'] == A.assign_nozomi_all(aid)['tip_account']
        assert arm['tip_lamports'] == (6_000_000 if expected == 'T' else 1_250_000)
        assert arm['priority_budget_lamports'] == 6_520_000 - arm['tip_lamports']
    assert seen == {'C', 'T'}


@pytest.mark.parametrize('fee_arm', ['C', 'T'])
def test_gas_holdback_and_lane_selection_share_frozen_assignment(monkeypatch, fee_arm):
    monkeypatch.setattr(A, 'nozomi_wallet_enabled', lambda _: True)
    aid = attempt_for(fee_arm)
    calls = []
    arm = A.nozomi_entry_arm(aid, 'wallet', tip_lamports_fn=lambda: calls.append(1) or 2_000_000)
    assert arm['fee_arm'] == fee_arm
    assert arm['tip_lamports'] == (6_000_000 if fee_arm == 'T' else 2_000_000)
    assert calls == ([] if fee_arm == 'T' else [1])
    assert A.nozomi_gas_extra_sol(aid, 'wallet') == (.006 if fee_arm == 'T' else .005)


def test_disabled_experiment_preserves_dynamic_control_and_old_rule(monkeypatch):
    monkeypatch.setenv('MEMECOIN_NOZOMI_FEE_SPLIT_AB', '0')
    arm = A.nozomi_arm_for(attempt_for('T'), tip_lamports_fn=lambda: 1_250_000)
    assert arm['rule'] == A.NOZOMI_CAPPED_RULE
    assert arm['tip_lamports'] == 1_250_000
    assert 'fee_arm' not in arm
    with pytest.raises(ValueError):
        cache().select(pool=None, cu_limit=152_000, tip_lamports=6_000_000)
    with pytest.raises(ValueError):
        A.cap_entry_priority_fee(message(), tip_lamports=6_000_000)
    assert A.guard_refusal(message(), 0, 6_000_000, dynamic_nozomi=True)


@pytest.mark.parametrize('key', [key for key in ACTIVATION if key != 'MEMECOIN_NOZOMI_FEE_SPLIT_AB'])
def test_partial_activation_cannot_select_or_sign_treatment(monkeypatch, key):
    monkeypatch.delenv(key)
    with pytest.raises(ValueError, match='fee_split'):
        A.nozomi_arm_for(attempt_for('T'))


@pytest.mark.parametrize('tip', [5_000_001, 5_999_999, 6_000_001, True])
def test_six_million_is_one_versioned_treatment_not_a_larger_dynamic_range(tip):
    with pytest.raises(ValueError):
        cache().select(pool=None, cu_limit=152_000, tip_lamports=tip)
    with pytest.raises(ValueError):
        A.cap_entry_priority_fee(message(), tip_lamports=tip)


@pytest.mark.parametrize('limit', [1, 152_000, 333_333, 1_400_000])
def test_treatment_cached_selection_and_signed_integer_rounding_keep_combined_cap(limit):
    c = cache()
    bid = c.select(pool=None, cu_limit=limit, tip_lamports=6_000_000)
    assert bid['priority_fee_lamports'] <= 520_000
    assert bid['combined_fee_lamports'] <= 6_520_000
    msg = message(limit=limit)
    out, signed = A.cap_entry_priority_fee(msg, tip_lamports=6_000_000, entry_bids=c)
    actual_price = struct.unpack('<Q', bytes(out.instructions[1].data)[1:])[0]
    assert signed['priority_fee_lamports'] == (actual_price * limit + 999_999) // 1_000_000
    assert signed['combined_fee_lamports'] <= 6_520_000
    assert out.account_keys == msg.account_keys and out.header == msg.header
    assert out.instructions[0] == msg.instructions[0] and out.instructions[2:] == msg.instructions[2:]
    assert A.guard_refusal(out, 0, 6_000_000, dynamic_nozomi=True) is None
    assert A.guard_refusal(out, 0, 5_000_000, dynamic_nozomi=True)


def test_zero_priority_sample_stays_known_and_stale_sample_stays_fallback():
    c = cache()
    c._pools = {'pool': _Sample(value=0, observed_at=99, count=3, source_slot=4)}
    bid = c.select(pool='pool', cu_limit=152_000, tip_lamports=6_000_000)
    assert bid['priority_sample_micro_lamports_per_cu'] == 0
    assert bid['priority_sample_count'] == 3 and bid['priority_source'] == 'account_filtered_p95'
    c._pools = {'pool': _Sample(value=90_000_000, observed_at=1, count=3, reason='timeout')}
    stale = c.select(pool='pool', cu_limit=152_000, tip_lamports=6_000_000)
    assert stale['priority_source'] == 'fallback_stale' and stale['priority_failure'] == 'timeout'
    assert stale['combined_fee_lamports'] <= 6_520_000


def test_unknown_cu_limit_keeps_honest_upper_bound_under_treatment_cap():
    msg = message(limit=None)
    class NoSelect:
        def select(self, **kw): pytest.fail('unknown CU limit cannot select a precise fee')
    out, bid = A.cap_entry_priority_fee(msg, tip_lamports=6_000_000, entry_bids=NoSelect())
    assert bid['cu_limit'] is None and bid['priority_fee_lamports'] is None
    assert bid['priority_fee_upper_bound_lamports'] <= 520_000
    assert bid['combined_fee_upper_bound_lamports'] <= 6_520_000
    assert A.guard_refusal(out, 0, 6_000_000, dynamic_nozomi=True) is None


def test_heartbeat_reports_activation_and_budget_without_fabricating_a_fee_sample(monkeypatch):
    c = cache()
    status = c.status()
    assert status['fee_split_enabled'] is True
    assert status['fee_split_rule'] == RULE
    assert status['fee_split_control_tip_max_lamports'] == 5_000_000
    assert status['fee_split_treatment_tip_lamports'] == 6_000_000
    assert status['fee_split_treatment_priority_max_lamports'] == 520_000
    assert status['max_total_lamports'] == 6_520_000
    assert status['tip_sample_lamports'] is None
    assert status['tip_sample_age_s'] is None and status['pools'] == {}
    monkeypatch.setenv('MEMECOIN_NOZOMI_FEE_SPLIT_AB', '0')
    rolled_back = c.status()
    assert rolled_back['fee_split_enabled'] is False
    assert rolled_back['fee_split_rule'] is None
    assert rolled_back['fee_split_treatment_tip_lamports'] is None


def test_nozomi_posts_the_treatment_bytes_once_only_when_experiment_enabled(monkeypatch):
    from sniper.execution.submission_transport import build_request, AttemptState
    raw = base64.b64encode(bytes(VersionedTransaction.populate(message(), []))).decode()
    seen = []
    http = lambda *a, **k: seen.append(k) or (200, {'result': 'sig'}, None)
    request = build_request(chain_id='solana', action='entry', tx_identity='sig', signed_payload=raw)
    provider = A.nozomi_provider(http)
    assert provider.submit(request).state is AttemptState.ACCEPTED
    assert len(seen) == 1
    assert provider.submit(request).state is AttemptState.NOT_SENT
    assert len(seen) == 1
    monkeypatch.setenv('MEMECOIN_NOZOMI_FEE_SPLIT_AB', '0')
    assert A.nozomi_provider(http).submit(request).state is AttemptState.NOT_SENT
    assert len(seen) == 1


@pytest.mark.parametrize('bad', [None, True, 0, 5_000_001])
def test_control_uses_original_fallback_when_cached_tip_is_invalid(bad):
    arm = A.nozomi_arm_for(attempt_for('C'), tip_lamports_fn=lambda: bad)
    assert arm['rule'] == RULE and arm['fee_arm'] == 'C'
    assert arm['tip_lamports'] == 5_000_000 and arm['tip_failure'] == 'invalid_tip'


def test_ordinary_entry_records_assignment_and_caps_the_actual_treatment_bytes(monkeypatch):
    from sniper.execution import solana_real_seams as S, solana_swap_executor as E
    from sniper.persistence import db_memecoin as m
    kp = Keypair()
    ixs = [set_compute_unit_limit(152_000), set_compute_unit_price(40_000_000)]
    ixs += A.tip_instructions(str(kp.pubkey()), (str(Keypair().pubkey()), 0))
    msg = MessageV0.try_compile(kp.pubkey(), ixs, [], Hash.default())
    raw = base64.b64encode(bytes(VersionedTransaction.populate(msg, []))).decode()
    monkeypatch.setattr(S, 'jupiter_swap_tx', lambda *a, **k: raw)
    monkeypatch.setattr(A, 'nozomi_wallet_enabled', lambda _: True)
    captured, events = [], []
    class Trace:
        attempt_id = attempt_for('T')
        def mark_substage(self, name, **values): events.append((name, values))
    def execute(conn, *, sign_fn, **kw):
        captured.append(sign_fn({'routePlan': [], 'outputMint': 'fixture'}))
        return {'status': 'aborted', 'reason': 'offline fixture'}
    class Transport:
        def provider(self, label):
            return object() if label == 'solana:nozomi' else None
    monkeypatch.setattr(E, 'execute_entry', execute)
    c = sqlite3.connect(':memory:'); c.row_factory = sqlite3.Row
    try:
        m.ensure_memecoin_tables(c)
        pid = m.reserve_pending(c, mint='fixture', source_user_id='wallet', strategy='mirror',
            notional=50, reserved_at='2026-10-13T00:00:00+00:00', network_id='solana')
        seam = S.build_real_solana_executor(keypair=kp, rpc_url='http://unused', entry_bids=cache(),
                                            submission_transport=Transport())
        seam['execute_fn'](c, pid, 'fixture', 50, '2026-10-13T00:00:01+00:00', trace=Trace())
        signed = captured[0][0]
        tx = VersionedTransaction.from_bytes(base64.b64decode(signed))
        assert tx.verify_with_results() == [True]
        assert A.payload_tip(signed, A._NOZOMI_SET, 6_000_000) == 6_000_000
        bid = [v for name, v in events if name == 'entry_bid_signed'][-1]
        assert bid['priority_fee_lamports'] == 520_000 and bid['combined_fee_lamports'] == 6_520_000
        arm = [v for name, v in events if name == 'nozomi_ab'][-1]
        assert arm['fee_arm'] == 'T' and arm['rule'] == RULE and arm['tip_lamports'] == 6_000_000
    finally:
        c.close()


def test_slot_entry_caps_treatment_before_one_signature_and_one_transport(conn, monkeypatch):
    from sniper.execution import solana_real_seams as S, solana_slot_executor as E
    from test_memecoin_solana_slot_executor import _reserve
    kp = Keypair()
    ixs = [set_compute_unit_limit(152_000), set_compute_unit_price(40_000_000)]
    ixs += A.tip_instructions(str(kp.pubkey()), (A.NOZOMI_TIP_ACCOUNTS[0], 6_000_000))
    msg = MessageV0.try_compile(kp.pubkey(), ixs, [], Hash.default())
    captured, events = [], []
    monkeypatch.setattr(E, 'execute_prebuilt_entry', lambda _conn, **kw: captured.append(kw) or
                        {'status': 'unknown', 'sig': kw['sig']})
    class Transport:
        def provider(self, label):
            return object() if label == 'solana:nozomi' else None
    class Trace:
        def mark_substage(self, name, **values): events.append((name, values))
        def mark(self, name, **values): events.append((name, values))
    seam = S.build_real_solana_executor(keypair=kp, rpc_url='http://unused',
                                        submission_transport=Transport(), entry_bids=cache())
    result = seam['slot_execute_fn'](conn, _reserve(conn), msg,
        validity={'nozomi_tip_lamports': 6_000_000}, now_iso='2026-10-13T00:00:00Z', trace=Trace())
    assert result['status'] == 'unknown' and len(captured) == 1
    signed = captured[0]['signed_b64']
    tx = VersionedTransaction.from_bytes(base64.b64decode(signed))
    assert tx.verify_with_results() == [True]
    assert A.payload_tip(signed, A._NOZOMI_SET, 6_000_000) == 6_000_000
    bid = [v for name, v in events if name == 'entry_bid_signed'][-1]
    assert bid['priority_fee_lamports'] == 520_000 and bid['combined_fee_lamports'] == 6_520_000


from test_memecoin_solana_slot_executor import conn  # noqa: E402,F401
