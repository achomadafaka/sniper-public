"""V4 pool-key discovery must resume from the range it actually READ, not from a hardcoded lookback.

2026-09-23 (ce308a85) capped the first `eth_getLogs` read at the chain's served width -- 2,000 blocks
on Base -- but left the paging loop starting at `head - _V4_DISCOVERY_LOOKBACK_BLOCKS` (8,192). So
blocks head-8192..head-2000, about 3.4 hours of Base, were never scanned, and a first read that was
refused or timed out also skipped the newest range, where a fresh pool is most likely to be.

Ported from Codex 30255f9d (the paging half only; its receipt-stamp and relay-forward halves are
rejected as held/settled items).
"""
import pytest

from sniper.execution import evm_v4_quote as V


@pytest.mark.parametrize('provider_limit,event_age,first_failure', [
    (2000, 3000, False), (500, 100, False), (2000, 100, True),
])
def test_base_scan_resumes_from_last_successful_range(monkeypatch, provider_limit,
                                                     event_age, first_failure):
    head = 50_000_000
    pool_id = '0x' + 'ab' * 32
    log = {'topics': [V._INIT_TOPIC, pool_id, '0x' + '00' * 32,
                      '0x' + '00' * 12 + '11' * 20],
           'data': '0x' + format(3000, '064x') + format(60, '064x') + '00' * 32}
    monkeypatch.delenv('MEMECOIN_V4_KEY_SCAN_PAGE_BLOCKS_8453', raising=False)
    monkeypatch.setattr(V, '_V4_KEY_SCAN_PAGE_BLOCKS_BY_CHAIN', {'8453': 2000})
    monkeypatch.setattr(V, '_key_cache', {})
    monkeypatch.setattr(V, '_stored_pool_key', lambda *a, **k: None)
    monkeypatch.setattr(V, '_store_pool_key', lambda *a, **k: None)
    monkeypatch.setattr(V, '_archive_rpc_urls', lambda *a, **k: ())
    spans = []
    def rpc(url, method, params, http=None):
        if method == 'eth_blockNumber':
            return {'result': hex(head)}
        low, high = int(params[0]['fromBlock'], 16), int(params[0]['toBlock'], 16)
        spans.append((low, high))
        if first_failure and len(spans) == 1:
            raise TimeoutError('offline timeout')
        if high - low > provider_limit:
            return {'error': {'code': -32614, 'message':
                    'eth_getLogs is limited to a %d range' % provider_limit}}
        return {'result': [log] if low <= head - event_age <= high else []}
    monkeypatch.setattr(V, '_rpc', rpc)
    assert V._pool_key('8453', pool_id, 'offline://rpc') is not None, spans
    assert len(spans) == 2, 'fix must use existing next page, not add a request'
