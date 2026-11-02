"""Cold funding legs batch uncached ALTs without publishing an incomplete cache."""
import base64

import pytest
from solders.pubkey import Pubkey

from sniper.execution import pump_curve_buy as P
from test_slot_lane_pump_alt import USER, _table_account

TABLES = [str(Pubkey.from_bytes(bytes([n]) * 32)) for n in range(1, 104)]
ACCOUNT = _table_account([USER])


def _prefetch(keys, rpc, cache):
    def jup(method, path, body=None):
        if method == 'GET':
            return 200, {'otherAmountThreshold': '90', 'outAmount': '100'}
        return 200, {'swapInstruction': {'programId': P.SYSTEM_PROGRAM, 'accounts': [], 'data': ''},
                     'addressLookupTableAddresses': keys}
    return P.prefetch_usdc_sol_leg(10_000_000, 50, user=USER, jup_call=jup, rpc=rpc,
                                   alt_cache=cache)


@pytest.mark.parametrize('envelope', [False, True])
def test_missing_tables_are_deduplicated_and_cached_without_changing_table_order(envelope):
    a, b, cached = TABLES[:3]
    cache = {cached: P.lookup_table_from_account(cached, ACCOUNT['data'][0])}
    calls = []

    def rpc(method, params):
        calls.append((method, params))
        value = ACCOUNT if method == 'getAccountInfo' else [ACCOUNT] * len(params[0])
        result = {'context': {'slot': 123}, 'value': value}
        return {'jsonrpc': '2.0', 'id': 1, 'result': result} if envelope else result

    keys = [a, cached, b, a]
    leg = _prefetch(keys, rpc, cache)
    assert [str(t.key) for t in leg['tables']] == [a, cached, b, a]
    assert leg['tables'][1] is cache[cached] and leg['tables'][0] is leg['tables'][3]
    assert calls == [('getMultipleAccounts', [[a, b], {'encoding': 'base64'}])]
    second = _prefetch(keys, rpc, cache)
    assert all(x is y for x, y in zip(leg['tables'], second['tables']))
    assert len(calls) == 1


def test_empty_table_list_never_reads_rpc():
    assert _prefetch([], lambda *_: pytest.fail('no table requires no RPC'), {})['tables'] == []


@pytest.mark.parametrize('accounts', [
    None, [], [ACCOUNT], [ACCOUNT, ACCOUNT, ACCOUNT], [ACCOUNT, None],
    'not_accounts', {'value': [ACCOUNT, ACCOUNT]}, [ACCOUNT, {}],
    [ACCOUNT, {'data': []}], [ACCOUNT, {'data': ['not base64!']}],
    [ACCOUNT, {'data': [base64.b64encode(b'short').decode(), 'base64']}],
], ids=['null_list', 'empty', 'short', 'long', 'null_account', 'string', 'object',
        'missing_data', 'empty_data', 'bad_base64', 'bad_table'])
def test_invalid_batch_never_returns_a_leg_or_partially_populates_cache(accounts):
    a, b, cached = TABLES[:3]
    sentinel = object()
    cache = {cached: sentinel}

    def rpc(method, params):
        return {'value': ACCOUNT if method == 'getAccountInfo' else accounts}

    assert _prefetch([a, cached, b], rpc, cache) is None
    assert cache == {cached: sentinel}


def test_large_batches_stay_within_rpc_account_limit_and_preserve_order():
    calls = []

    def rpc(method, params):
        keys = [params[0]] if method == 'getAccountInfo' else params[0]
        calls.append((method, keys))
        return {'value': ACCOUNT if method == 'getAccountInfo' else [ACCOUNT] * len(keys)}

    leg = _prefetch(TABLES, rpc, {})
    assert [str(t.key) for t in leg['tables']] == TABLES
    assert calls == [('getMultipleAccounts', TABLES[:100]), ('getMultipleAccounts', TABLES[100:])]


def test_a_failed_later_batch_does_not_publish_the_first_batch():
    calls, cache = [], {}

    def rpc(method, params):
        calls.append(method)
        if method == 'getAccountInfo':
            return {'value': ACCOUNT}
        return {'value': [ACCOUNT] * len(params[0]) if len(calls) == 1 else [ACCOUNT, None, ACCOUNT]}

    assert _prefetch(TABLES, rpc, cache) is None
    assert cache == {}


def _warm_with_tables(monkeypatch, accounts):
    from test_slot_lane_exotic_warm_leg import _real_warm
    warm, builder, clock, _, _ = _real_warm(monkeypatch)
    original_rpc, original_jup = builder.read_rpc, P._funded_jup_call
    keys, reads = TABLES[:2], []

    def rpc(method, params):
        if params[1]['encoding'] == 'jsonParsed':
            return original_rpc(method, params)
        reads.append((method, params))
        value = accounts[keys.index(params[0])] if method == 'getAccountInfo' else accounts
        return {'result': {'context': {'slot': 123}, 'value': value}}

    def jup(method, path, body=None, *, http=None):
        status, value = original_jup(method, path, body, http=http)
        if method == 'POST':
            value['addressLookupTableAddresses'] = keys
        return status, value

    builder.read_rpc = rpc
    monkeypatch.setattr(P, '_funded_jup_call', jup)
    return warm, clock, reads


def test_warmer_validates_and_reuses_every_table_in_the_batch(monkeypatch):
    from test_slot_lane_pump_nonsol import SPCX
    warm, clock, reads = _warm_with_tables(monkeypatch, [ACCOUNT, ACCOUNT])
    warm.refresh_once()
    leg = warm.get('pump_amm', SPCX['quote_mint'], 10_000_000)
    assert leg is not None and [str(t.key) for t in leg['tables']] == TABLES[:2]
    assert reads == [('getMultipleAccounts', [TABLES[:2], {'encoding': 'base64'}])]
    clock.t += 1.5
    warm.refresh_once()
    assert warm.stats['refreshed'] == 2 and len(reads) == 1


@pytest.mark.parametrize('account, category', [
    (_table_account([USER], owner='foreign'), 'warm_alt_owner'),
    (_table_account([USER], deactivation=123), 'warm_alt_deactivated'),
    (None, 'missing:account'),
], ids=['foreign_owner', 'deactivated', 'missing'])
def test_warmer_rejects_a_bad_second_table_without_caching_the_good_first(monkeypatch, account, category):
    from test_slot_lane_pump_nonsol import SPCX
    warm, _, _ = _warm_with_tables(monkeypatch, [ACCOUNT, account])
    warm.refresh_once()
    assert warm.get('pump_amm', SPCX['quote_mint'], 10_000_000) is None
    assert warm.stats['last_failure']['stage'] == 'alt_validation'
    assert warm.stats['last_failure']['category'] == category
    assert not any(warm._alt_cache.values())
