import base64
import json
import struct
import time
from types import SimpleNamespace

import pytest
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.transaction import VersionedTransaction

from sniper.execution import solana_sender_ab as A


def message(price=3_800_000, limit=400_000, tip=1_000_000):
    payer = Keypair().pubkey()
    ixs = [set_compute_unit_limit(limit), set_compute_unit_price(price)]
    ixs += A.tip_instructions(str(payer), (A.NOZOMI_TIP_ACCOUNTS[0], tip))
    return MessageV0.try_compile(payer, ixs, [], Hash.default())


def test_dynamic_tip_keeps_exact_arm_binding_and_old_guard_default():
    msg = message(tip=1_250_000)
    assert A.guard_refusal(msg, 0, 1_250_000, dynamic_nozomi=True) is None
    assert A.guard_refusal(msg, 0, 1_000_000, dynamic_nozomi=True)
    assert A.guard_refusal(msg, 0, 1_250_000)


@pytest.mark.parametrize('tip', [1, 999_999, 5_000_001])
def test_dynamic_tip_never_accepts_under_minimum_or_over_cap(tip):
    assert A.guard_refusal(message(tip=tip), 0, tip, dynamic_nozomi=True)


def test_dynamic_arm_is_frozen_once_before_signing(monkeypatch):
    monkeypatch.setattr(A, 'nozomi_all_b_on', lambda: True)
    calls = []
    arm = A.nozomi_arm_for('an-attempt', tip_lamports_fn=lambda: calls.append(1) or 1_250_000)
    assert arm['tip_lamports'] == 1_250_000
    assert arm['rule'] == A.NOZOMI_CAPPED_RULE
    assert calls == [1]


@pytest.mark.parametrize('bad', [None, True, 0, 5_000_001, 1.5])
def test_bad_tip_callback_preserves_fixed_fallback(monkeypatch, bad):
    monkeypatch.setattr(A, 'nozomi_all_b_on', lambda: True)
    arm = A.nozomi_arm_for('an-attempt', tip_lamports_fn=lambda: bad)
    assert arm['tip_lamports'] == 5_000_000
    assert arm['tip_failure'] == 'invalid_tip'


def test_priority_cap_preserves_swap_keys_and_exact_integer_ceiling():
    msg = message(price=40_000_000, limit=152_000, tip=5_000_000)
    capped, info = A.cap_entry_priority_fee(msg, tip_lamports=5_000_000)
    price = struct.unpack('<Q', bytes(capped.instructions[1].data)[1:])[0]
    assert price == 10_000_000
    assert info['priority_fee_lamports'] == 1_520_000
    assert info['combined_fee_lamports'] == 6_520_000
    assert capped.account_keys == msg.account_keys
    assert capped.instructions[2] == msg.instructions[2]
    assert capped.recent_blockhash == msg.recent_blockhash


def test_priority_within_cap_is_byte_identical():
    msg = message()
    result, info = A.cap_entry_priority_fee(msg, tip_lamports=1_000_000)
    assert bytes(result) == bytes(msg)
    assert info['cap_saturated'] is False


def test_unsigned_payload_priority_is_capped_without_signing():
    msg = message(price=40_000_000, tip=5_000_000)
    raw = base64.b64encode(bytes(VersionedTransaction.populate(msg, []))).decode()
    capped, info = A.cap_unsigned_entry_priority(raw, tip_lamports=5_000_000)
    tx = VersionedTransaction.from_bytes(base64.b64decode(capped))
    assert tx.signatures == []
    assert info['combined_fee_lamports'] <= 6_520_000


def test_provider_adapter_starts_background_only_and_passes_filtered_pool(monkeypatch):
    from sniper.services import memecoin_entry_bids as S
    pool = str(Keypair().pubkey())
    calls = []
    class Cache:
        def __init__(self, priority_fee_fn, tip_floor_fn, **bounds):
            self.priority_fee_fn, self.tip_floor_fn = priority_fee_fn, tip_floor_fn
        def start(self):
            calls.append('start')
            return self
        def close(self):
            pass
    monkeypatch.setattr(S, 'EntryBidCache', Cache)
    monkeypatch.setattr(S, 'register_client', lambda client: client)
    def http(method, url, **kw):
        calls.append((method, url))
        if method == 'GET':
            return 200, [{'time': '2026-10-12T00:00:00Z', 'landed_tips_95th_percentile': .002}], None
        body = json.loads(kw['body'])
        assert body[0]['method'] == 'getRecentPrioritizationFees'
        assert body[0]['params'] == [[pool]]
        assert body[1]['method'] == 'getSlot'
        assert kw['timeout'] <= 1
        return 200, [{'id': 1, 'result': [{'slot': 3, 'prioritizationFee': 30_000_000}]},
                     {'id': 2, 'result': 4}], None
    cache = S.start_entry_bids('https://rpc.ams.shyft.to/?api_key=private',
                              env={'MEMECOIN_SOLANA_ENTRY_BIDS_ENABLED': '1'}, http=http)
    assert calls == ['start']
    assert cache.priority_fee_fn(pool) == [{'slot': 3, 'prioritizationFee': 30_000_000}]
    assert cache.tip_floor_fn()[0]['landed_tips_95th_percentile'] == .002


def test_disabled_adapter_constructs_no_client(monkeypatch):
    from sniper.services import memecoin_entry_bids as S
    monkeypatch.setattr(S, 'PooledHttpClient', lambda **kw: pytest.fail('disabled client'))
    assert S.start_entry_bids('https://unused', env={}) is None


def test_pooled_http_timeout_keeps_its_typed_failure(monkeypatch):
    from sniper.services import memecoin_entry_bids as S
    from sniper.execution.fast_http import FastHttpError
    class Cache:
        def __init__(self, priority_fee_fn, tip_floor_fn, **kw): self.priority_fee_fn = priority_fee_fn
        def start(self): return self
        def close(self): pass
    monkeypatch.setattr(S, 'EntryBidCache', Cache)
    monkeypatch.setattr(S, 'register_client', lambda x: x)
    def timeout(*a, **kw): raise FastHttpError('POST request failed: ReadTimeout')
    cache = S.start_entry_bids('https://unused', env={'MEMECOIN_SOLANA_ENTRY_BIDS_ENABLED': '1'}, http=timeout)
    with pytest.raises(S.EntryBidReadError) as exc: cache.priority_fee_fn(str(Keypair().pubkey()))
    assert exc.value.reason == 'timeout'


@pytest.mark.parametrize('status,payload', [(429, {}), (403, {}), (200, {'error': {'code': -32601}}),
                                          (200, {'result': None}), (200, 'not-json')])
def test_background_read_failure_is_typed_and_secret_free(monkeypatch, status, payload):
    from sniper.services import memecoin_entry_bids as S
    class Cache:
        def __init__(self, priority_fee_fn, tip_floor_fn, **bounds):
            self.priority_fee_fn = priority_fee_fn
        def start(self): return self
        def close(self): pass
    monkeypatch.setattr(S, 'EntryBidCache', Cache)
    monkeypatch.setattr(S, 'register_client', lambda client: client)
    cache = S.start_entry_bids('https://rpc.ams.shyft.to/?api_key=private',
                              env={'MEMECOIN_SOLANA_ENTRY_BIDS_ENABLED': '1'},
                              http=lambda *a, **kw: (status, payload, None))
    with pytest.raises(S.EntryBidReadError) as exc:
        cache.priority_fee_fn(str(Keypair().pubkey()))
    assert 'private' not in str(exc.value)
    assert 'https' not in str(exc.value)


@pytest.mark.parametrize('slot,head,reason', [(2, 20, 'stale_source'), (22, 20, 'future_source'),
                                            (True, 20, 'malformed'), (20, True, 'malformed')])
def test_adapter_refuses_stale_or_future_initial_slot(slot, head, reason):
    from sniper.services import memecoin_entry_bids as S
    response = [{'id': 1, 'result': [{'slot': slot, 'prioritizationFee': 20}]},
                {'id': 2, 'result': head}]
    with pytest.raises(S.EntryBidReadError) as exc:
        S.validate_priority_response(response)
    assert exc.value.reason == reason


def test_slot_arm_uses_its_executor_tip_snapshot(monkeypatch):
    from sniper.services.memecoin_slot_lane import SlotLane
    monkeypatch.setattr(A, 'nozomi_all_b_on', lambda: True)
    monkeypatch.setattr(A, 'nozomi_wallet_enabled', lambda uid: True)
    trace = SimpleNamespace(attempt_id='a', mark_substage=lambda *a, **kw: None)
    instance = SimpleNamespace(nozomi_ab=True, entry_tip_fn=lambda: 1_100_000)
    arm = SlotLane._nozomi_arm(instance, SimpleNamespace(source_user_id='wallet'), trace)
    assert arm['tip_lamports'] == 1_100_000


def test_provider_accepts_only_capped_dynamic_tip_when_enabled():
    msg = message(tip=1_000_000)
    kp = Keypair()
    # Only the tip parser matters; this never sends a transaction.
    raw = base64.b64encode(bytes(VersionedTransaction.populate(msg, []))).decode()
    from sniper.execution.submission_transport import build_request, AttemptState
    requests = []
    provider = A.NozomiProvider(lambda *a, **kw: requests.append(kw) or (200, {'result': 'sig'}, None),
                                url='https://fra2.nozomi.temporal.xyz/?c=test', dynamic_tips=True)
    req = build_request(chain_id='solana', action='entry', tx_identity='sig', signed_payload=raw)
    assert provider.submit(req).state is AttemptState.ACCEPTED
    assert len(requests) == 1


def test_build_measurement_records_market_bid_separately_from_validity():
    from sniper.services.memecoin_slot_lane import record_slot_build
    events = []
    trace = SimpleNamespace(mark_substage=lambda name, **kw: events.append((name, kw)))
    validity = record_slot_build(trace, {'blockhash': 'H', 'priority_bid': {'priority_source': 'fallback_cold'},
                                        'cu_limit': 152_000,
                                        'cu_key': ('pump_amm', 'native', '26_accounts', 'T22')}, duration_ms=.2)
    assert validity == {'blockhash': 'H'}
    assert events[0][1]['priority_bid']['priority_source'] == 'fallback_cold'
    assert events[0][1]['cu_native_variant'] == '26_accounts'
    assert events[0][1]['cu_leg_dex'] is None


def test_cap_with_address_lookup_tables_preserves_swap_identity():
    from test_memecoin_nozomi_all_lanes import _jupiter_like, _ix_identities
    msg = _jupiter_like(Keypair().pubkey())
    capped, _info = A.cap_entry_priority_fee(msg, tip_lamports=5_000_000)
    assert _ix_identities(capped) == _ix_identities(msg)
    assert capped.address_table_lookups == msg.address_table_lookups


def test_signed_guard_caps_before_one_signature_and_one_transport(conn, monkeypatch):
    from sniper.execution import solana_real_seams as S, solana_slot_executor as E
    from test_memecoin_solana_slot_executor import _reserve
    kp = Keypair()
    ixs = [set_compute_unit_limit(152_000), set_compute_unit_price(40_000_000)]
    ixs += A.tip_instructions(str(kp.pubkey()), (A.NOZOMI_TIP_ACCOUNTS[0], 1_000_000))
    msg = MessageV0.try_compile(kp.pubkey(), ixs, [], Hash.default())
    seen = []
    monkeypatch.setattr(E, 'execute_prebuilt_entry', lambda _conn, **kw: seen.append(kw) or
                        {'status': 'unknown', 'sig': kw['sig']})
    class Transport:
        def provider(self, label):
            return object() if label == 'solana:nozomi' else None
    ex = S.build_real_solana_executor(keypair=kp, rpc_url='http://unused',
                                      http=lambda *_a, **_k: pytest.fail('unexpected provider read'),
                                      submission_transport=Transport(), entry_bids=object())
    result = ex['slot_execute_fn'](conn, _reserve(conn), msg, validity={'nozomi_tip_lamports': 1_000_000},
                                  now_iso='2026-10-12T00:00:00Z',
                                  entry_min_slot=100, entry_max_slot=101,
                                  entry_head_fn=lambda: (100, time.monotonic()))
    assert result['status'] == 'unknown' and len(seen) == 1
    signed = VersionedTransaction.from_bytes(base64.b64decode(seen[0]['signed_b64']))
    price = struct.unpack('<Q', bytes(signed.message.instructions[1].data)[1:])[0]
    assert (price * 152_000 + 999_999) // 1_000_000 + 1_000_000 <= 6_520_000
    assert signed.verify_with_results() == [True]


from test_memecoin_solana_slot_executor import conn  # noqa: E402,F401
