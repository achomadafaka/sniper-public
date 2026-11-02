"""Cold watcher metadata shares an owned pooled transport without changing proof or failures."""
import json
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest
import httpx

import scripts.research.onchain_evm_wallet_watch as W
from sniper.util.receipt_clock import ReceiptClock


class Client:
    def __init__(self, status=200, answer=None):
        self.status, self.answer = status, answer if answer is not None else {"result": "0x12"}
        self.calls, self.closed = [], 0
    def request_with_headers(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.status, self.answer, {"date": "today"}
    def close(self):
        self.closed += 1


def reads(monkeypatch, client=None):
    factory = getattr(W, "_WatcherReads", None)
    assert factory is not None, "watcher cold reads still bypass a reusable owned transport"
    return factory(client=client or Client())


def test_rpc_and_metadata_bind_one_private_http_seam(monkeypatch):
    seen = []
    def rpc(url, method, params, http=None):
        seen.append(("rpc", http))
        return {"result": http("POST", url, body="{}")[1]["result"]}
    def decimals(token, url, http=None):
        seen.append(("decimals", http))
        return int(http("POST", url, body="{}")[1]["result"], 16)
    monkeypatch.setattr(W, "_rpc", rpc)
    monkeypatch.setattr(W, "erc20_decimals", decimals)
    client = Client()
    with reads(monkeypatch, client) as transport:
        assert transport.rpc("https://read", "eth_chainId", []) == {"result": "0x12"}
        assert transport.decimals("coin", "https://read") == 18
    assert len(client.calls) == 2 and client.closed == 1
    assert seen[0][1] == seen[1][1]


@pytest.mark.parametrize("status,body,error", [(403, "denied", True), (429, "limited", True),
    (500, "unavailable", True), (400, {"jsonrpc": "2.0", "error": {"code": -32600}}, False),
    (200, "malformed", True)])
def test_pooled_adapter_preserves_legacy_http_failure_shape(status, body, error, monkeypatch):
    client = Client(status, body)
    with reads(monkeypatch, client) as transport:
        if status != 200 and error:
            with pytest.raises(urllib.error.HTTPError) as exc:
                transport.http("POST", "https://read", body="{}")
            assert exc.value.code == status
        elif error:
            with pytest.raises(json.JSONDecodeError):
                transport.http("POST", "https://read", body="{}")
        else:
            assert transport.http("POST", "https://read", body="{}") == (status, body, {"date": "today"})
    assert len(client.calls) == 1 and client.closed == 1


def test_client_creation_keeps_original_network_timeout(monkeypatch):
    created = []
    def factory(**kw):
        created.append(kw)
        return Client()
    monkeypatch.setattr("sniper.execution.fast_http.PooledHttpClient", factory)
    with reads(monkeypatch):
        pass
    # Explicit construction of a default transport, rather than the injected client helper.
    with W._WatcherReads():
        pass
    assert len(created) == 1
    assert created[0]["connect_timeout_s"] == created[0]["read_timeout_s"] == 10.
    assert created[0]["write_timeout_s"] == 10.


@pytest.mark.parametrize('chain,ttl,minimum,expiry', [('56', 600., 30., 220.),
    ('56', 60., 30., 220.), ('56', 30., 30., 220.), ('4663', 600., 200., 560.),
    ('8453', 600., 200., 560.), ('1', 600., 200., 560.)])
def test_owned_pool_outlives_the_existing_relay_refresh_interval(monkeypatch, chain, ttl, minimum, expiry):
    from sniper.settings import memecoin as cfg
    created = []
    monkeypatch.setattr(W, 'CHAIN', chain)
    monkeypatch.setattr(cfg, 'MEMECOIN_ONCHAIN_RELAY_CODE_TTL_S', ttl)
    monkeypatch.setattr('sniper.execution.fast_http.PooledHttpClient',
                        lambda **kw: created.append(kw) or Client())
    with W._WatcherReads():
        pass
    assert created[0].get('keepalive_expiry_s', 30.) > minimum
    assert created[0]['keepalive_expiry_s'] == expiry
    assert created[0]['connect_timeout_s'] == created[0]['read_timeout_s'] == 10.
    assert created[0]['write_timeout_s'] == 10. and created[0]['pool_timeout_s'] == 0.


@pytest.mark.parametrize('chain,ttl', [('5042', 600.), ('57073', 600.), ('56', None),
    ('56', 0.), ('56', -1.), ('56', float('nan')), ('56', float('inf')), ('56', 'unknown')])
def test_inactive_or_invalid_relay_warming_keeps_bounded_default_retention(monkeypatch, chain, ttl):
    from sniper.settings import memecoin as cfg
    created = []
    monkeypatch.setattr(W, 'CHAIN', chain)
    monkeypatch.setattr(cfg, 'MEMECOIN_ONCHAIN_RELAY_CODE_TTL_S', ttl)
    monkeypatch.setattr('sniper.execution.fast_http.PooledHttpClient',
                        lambda **kw: created.append(kw) or Client())
    with W._WatcherReads():
        pass
    assert created[0].get('keepalive_expiry_s', 30.) == 30.


def test_external_transport_is_reused_by_nested_startup_without_double_close(monkeypatch):
    events = []
    owned = reads(monkeypatch)
    @W._pooled_watcher_reads
    def inner(*, read_transport=None):
        events.append(read_transport)
        return "ok"
    @W._pooled_watcher_reads
    def outer(*, read_transport=None):
        return inner(read_transport=read_transport)
    assert outer(read_transport=owned) == "ok" and events == [owned]
    assert owned.client.closed == 0
    owned.close()
    assert owned.client.closed == 1


def test_owned_transport_closes_after_startup_exception(monkeypatch):
    client = Client()
    monkeypatch.setattr("sniper.execution.fast_http.PooledHttpClient", lambda **kw: client)
    assert hasattr(W, "_pooled_watcher_reads"), "startup must own and close the client"
    @W._pooled_watcher_reads
    def launch(*, read_transport=None):
        assert read_transport is not None
        raise RuntimeError("startup fails")
    with pytest.raises(RuntimeError, match="startup fails"):
        launch()
    assert client.closed == 1


def test_cold_metadata_has_separate_read_and_join_stamps(monkeypatch):
    value = 18
    def rpc(url, method, params, http=None):
        return {"result": {"status": "0x1", "logs": []} if "Receipt" in method else {"from": "owner"}}
    def decimals(token, url, http=None):
        assert http is not None
        time.sleep(.01)
        return value
    monkeypatch.setattr(W, "_rpc", rpc)
    monkeypatch.setattr(W, "erc20_decimals", decimals)
    monkeypatch.setattr(W, "direct_buy_from_receipt", lambda *a, **kw: None)
    W._configure("56")
    producer = ReceiptClock(producer_id="metadata-test")
    outcome = {"_receipt_clock": producer.capture()}
    cache = {}
    with reads(monkeypatch) as transport:
        W.push_detection("https://read", {"transactionHash": "0x"+"ab"*32, "address": "coin"},
            {}, decimals_cache=cache, quote_usd=600., now="original", outcome=outcome,
            read_transport=transport)
    stages = {x["name"]: x for x in outcome["receipt_stages"]}
    assert {"metadata_read_start", "metadata_read_end", "metadata_wait_start", "metadata_wait_end"} <= stages.keys()
    assert stages["metadata_read_end"]["receipt_elapsed_ms"] >= stages["metadata_read_start"]["receipt_elapsed_ms"]
    assert stages["metadata_wait_end"]["receipt_elapsed_ms"] >= stages["metadata_wait_start"]["receipt_elapsed_ms"]
    assert cache == {"coin": 18}


def test_tx_read_host_has_one_exact_curl_agent_with_real_httpx_client(monkeypatch):
    from sniper.execution.fast_http import PooledHttpClient
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"result": {"hash": "TX"}})
    client = PooledHttpClient(transport=httpx.MockTransport(respond))
    with reads(monkeypatch, client) as transport:
        assert transport.post("https://read", {"method": "eth_getTransactionByHash"}, 1.5) == (
            200, {"result": {"hash": "TX"}})
    assert requests[0].headers.get_list('user-agent') == ["curl/8.5.0"]
    assert requests[0].headers.get_list('content-type') == ["application/json"]
    assert requests[0].extensions['timeout']['read'] == 1.5
    assert requests[0].extensions['timeout']['pool'] == 0.


def test_local_pool_capacity_is_not_a_provider_failure_or_retry(monkeypatch):
    from sniper.execution.fast_http import PooledHttpClient
    from sniper.execution.evm_rpc_pool import RequestNotSent
    calls = []
    def full(request):
        calls.append(request)
        raise httpx.PoolTimeout('local pool full')
    client = PooledHttpClient(transport=httpx.MockTransport(full))
    with reads(monkeypatch, client) as transport:
        with pytest.raises(RequestNotSent):
            transport.http('POST', 'https://read', body='{}')
        host = W.TxReadHost('https://read', cooldown_s=60., timeout_s=1.5, post_fn=transport.post)
        assert host.read('eth_getTransactionByHash', ['TX']) == (None, 'transport')
        assert host._cool_until is None
    assert len(calls) == 2   # one caller request each; transport never retries


def test_concurrent_pooled_reads_do_not_mix_request_headers_or_bodies(monkeypatch):
    from sniper.execution.fast_http import PooledHttpClient
    barrier, observed = Barrier(2), []
    def respond(request):
        barrier.wait(timeout=1.)
        observed.append((request.headers['x-request'], request.content.decode()))
        return httpx.Response(200, json={"result": request.headers['x-request']})
    client = PooledHttpClient(transport=httpx.MockTransport(respond))
    with reads(monkeypatch, client) as transport, ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(transport.http, 'POST', 'https://read', headers={'X-Request': 'A'}, body='body-A')
        b = pool.submit(transport.http, 'POST', 'https://read', headers={'X-Request': 'B'}, body='body-B')
        assert a.result()[1] == {"result": "A"} and b.result()[1] == {"result": "B"}
    assert sorted(observed) == [('A', 'body-A'), ('B', 'body-B')]


@pytest.mark.parametrize("answer,expected", [(dict(result=None), None), (dict(result=0), 0),
    ({"jsonrpc": "2.0", "error": {"code": -32000}}, "error"), ({"jsonrpc": "2.0"}, "error")])
def test_real_rpc_result_keeps_null_zero_and_error_distinct(answer, expected, monkeypatch):
    from sniper.execution.fast_http import PooledHttpClient
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, json=answer)
    client = PooledHttpClient(transport=httpx.MockTransport(respond))
    with reads(monkeypatch, client) as transport:
        if expected == "error":
            with pytest.raises(W.RpcErrorBody):
                W._rpc_result('https://read.invalid', 'eth_getTransactionReceipt', ['TX'], read_transport=transport)
        else:
            assert W._rpc_result('https://read.invalid', 'eth_getTransactionReceipt', ['TX'],
                                 read_transport=transport) == expected
    assert len(calls) == 1


def test_warm_metadata_never_starts_another_read(monkeypatch):
    monkeypatch.setattr(W, "erc20_decimals", lambda *a, **kw: pytest.fail("warm metadata re-read"))
    monkeypatch.setattr(W, "direct_buy_from_receipt", lambda *a, **kw: None)
    outcome = {}
    with reads(monkeypatch) as transport:
        W.push_detection("https://read", {"transactionHash": "0x"+"ab"*32, "address": "coin"},
            {}, decimals_cache={"coin": 18}, quote_usd=600., now="original",
            read_fn=lambda m,p: {"logs": []} if "Receipt" in m else {"from": "owner"},
            read_transport=transport, outcome=outcome)
    assert 'metadata_prefetch_rpc_attempts' not in outcome


@pytest.mark.parametrize('mode,count,outcomes,resolved,value', [
    ('single', 1, ['ok'], True, 18), ('zero', 1, ['ok'], True, 0),
    ('retry', 2, ['http_503', 'ok'], True, 18),
    ('unknown', 2, ['ok', 'ok'], False, None),
    ('exception', 2, ['raise'] * 2, False, None),
    ('timeout', 2, ['timeout'] * 2, False, None),
    ('provider_text', 2, ['rpc_error'] * 2, False, None),
])
def test_prefetch_rpc_evidence_counts_real_attempts_and_preserves_unknown_and_clock(
        monkeypatch, mode, count, outcomes, resolved, value):
    from sniper.execution.fast_http import PooledHttpClient
    calls = []
    mint = '0x' + '11' * 20
    monkeypatch.setattr(W, 'CHAIN', '56')

    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        assert body['method'] == 'eth_call' and body['params'][0] == {'to': mint, 'data': '0x313ce567'}
        if mode == 'exception':
            raise httpx.ReadTimeout('synthetic-secret')
        if mode == 'timeout':
            raise TimeoutError('synthetic-secret')
        answer = {'jsonrpc': '2.0', 'id': 1, 'result': '0x0' if mode == 'zero' else '0x12'}
        if mode == 'unknown':
            answer['result'] = None
        if mode == 'provider_text':
            answer = {'jsonrpc': '2.0', 'id': 1, 'error': {'code': 'synthetic-secret'}}
        if mode == 'retry' and len(calls) == 1:
            return httpx.Response(503, json={'jsonrpc': '2.0', 'id': 1, 'error': {'code': -32000}})
        return httpx.Response(200, json=answer)

    client = PooledHttpClient(transport=httpx.MockTransport(respond))
    producer = ReceiptClock(producer_id='prefetch-test')
    anchor = producer.capture()
    outcome, cache = {'_receipt_clock': anchor}, {}
    with reads(monkeypatch, client) as transport:
        W.push_detection('https://read.invalid/?key=synthetic-secret',
            {'transactionHash': '0x' + 'ab' * 32, 'address': mint}, {},
            decimals_cache=cache, quote_usd=600., now='original', outcome=outcome,
            read_fn=lambda m, p: {'logs': []} if 'Receipt' in m else {'from': 'owner'},
            read_transport=transport)
    evidence = outcome['metadata_prefetch_rpc_attempts']
    assert evidence['count'] == len(calls) == count and evidence['outcomes'] == outcomes
    assert evidence['resolved'] is resolved and evidence['wall_ms'] >= evidence['rpc_ms'] >= 0.
    if mode in ('retry', 'unknown', 'exception', 'provider_text'):
        assert evidence['wall_ms'] - evidence['rpc_ms'] >= 140.  # Existing 150ms helper retry.
    assert cache == ({mint: value} if resolved else {})
    assert outcome['_receipt_clock'] is anchor
    assert 'synthetic-secret' not in json.dumps(evidence)
    assert set(evidence) == {'count', 'outcomes', 'resolved', 'rpc_ms', 'wall_ms'}


def test_prefetch_rpc_evidence_is_thread_owned_and_not_total_fallback_reads(monkeypatch):
    from sniper.execution.fast_http import PooledHttpClient
    from sniper.execution.evm_wallet import take_rpc_tap
    mint_a, mint_b = '0x' + '11' * 20, '0x' + '22' * 20
    barrier, counts = Barrier(2), {mint_a: 0, mint_b: 0}
    monkeypatch.setattr(W, 'CHAIN', '56')

    def respond(request):
        mint = json.loads(request.content)['params'][0]['to']
        counts[mint] += 1
        if counts[mint] == 1:
            barrier.wait(timeout=1.)
        # B fails both prefetch attempts, then its ordinary decode fallback succeeds.
        result = None if mint == mint_b and counts[mint] <= 2 else '0x12'
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': 1, 'result': result})

    def decode(receipt, tx, owner, *, decimals_fn, **kwargs):
        decimals_fn(tx['mint'])
        return None

    monkeypatch.setattr(W, 'direct_buy_from_receipt', decode)
    client = PooledHttpClient(transport=httpx.MockTransport(respond))
    with reads(monkeypatch, client) as transport:
        def run(mint):
            outcome, cache = {}, {}
            W.push_detection('https://read.invalid', {'transactionHash': mint, 'address': mint},
                {'owner': ('u', 'h')}, decimals_cache=cache, quote_usd=600., now='original',
                outcome=outcome, read_transport=transport,
                read_fn=lambda m, p: {'logs': []} if 'Receipt' in m else {'from': 'owner', 'mint': mint})
            assert take_rpc_tap() is None
            return outcome['metadata_prefetch_rpc_attempts'], cache

        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(run, mint_a), pool.submit(run, mint_b)
            a_evidence, a_cache = a.result(timeout=2.)
            b_evidence, b_cache = b.result(timeout=2.)
    assert a_evidence['count'] == counts[mint_a] == 1 and a_evidence['resolved']
    assert b_evidence['count'] == 2 and counts[mint_b] == 3 and not b_evidence['resolved']
    assert a_cache == {mint_a: 18} and b_cache == {mint_b: 18}


@pytest.mark.parametrize('fault', ['start', 'take', 'malformed_record', 'malformed_rtt',
    'empty', 'over_cap', 'rtt_nan', 'rtt_inf', 'rtt_negative', 'rtt_bool',
    'arbitrary_outcome', 'bad_http', 'bad_raise', 'aggregate_overflow'])
def test_diagnostic_failure_never_discards_valid_metadata_or_adds_a_fallback_read(monkeypatch, fault):
    from sniper.execution.fast_http import PooledHttpClient
    mint, calls = '0x' + '11' * 20, []
    real_start, real_take = W.start_rpc_tap, W.take_rpc_tap
    monkeypatch.setattr(W, 'CHAIN', '56')

    def start():
        real_start()
        raise RuntimeError('diagnostic unavailable')

    def take():
        real_take()
        if fault == 'take':
            raise RuntimeError('diagnostic unavailable')
        invalid = {
            'malformed_record': [None],
            'malformed_rtt': [{'outcome': 'ok', 'rtt_ms': 'synthetic-secret'}],
            'empty': [], 'over_cap': [{'outcome': 'ok', 'rtt_ms': 1.}] * 3,
            'rtt_nan': [{'outcome': 'ok', 'rtt_ms': float('nan')}],
            'rtt_inf': [{'outcome': 'ok', 'rtt_ms': float('inf')}],
            'rtt_negative': [{'outcome': 'ok', 'rtt_ms': -1.}],
            'rtt_bool': [{'outcome': 'ok', 'rtt_ms': True}],
            'arbitrary_outcome': [{'outcome': 'synthetic-secret', 'rtt_ms': 1.}],
            'bad_http': [{'outcome': 'http_999', 'rtt_ms': 1.}],
            'bad_raise': [{'outcome': 'raise:synthetic-secret', 'rtt_ms': 1.}],
            'aggregate_overflow': [{'outcome': 'ok', 'rtt_ms': 1e308}] * 2,
        }
        return invalid[fault]

    monkeypatch.setattr(W, 'start_rpc_tap' if fault == 'start' else 'take_rpc_tap',
                        start if fault == 'start' else take)

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': 1, 'result': '0x12'})

    def decode(receipt, tx, owner, *, decimals_fn, **kwargs):
        assert decimals_fn(mint) == 18
        return None

    monkeypatch.setattr(W, 'direct_buy_from_receipt', decode)
    client = PooledHttpClient(transport=httpx.MockTransport(respond))
    anchor = ReceiptClock(producer_id='diagnostic-failure').capture()
    outcome, cache = {'_receipt_clock': anchor}, {}
    with reads(monkeypatch, client) as transport:
        W.push_detection('https://read.invalid', {'transactionHash': mint, 'address': mint},
            {'owner': ('u', 'h')}, decimals_cache=cache, quote_usd=600., now='original',
            outcome=outcome, read_transport=transport,
            read_fn=lambda m, p: {'logs': []} if 'Receipt' in m else {'from': 'owner'})
    assert cache == {mint: 18} and len(calls) == 1
    assert outcome.get('metadata_prefetch_rpc_attempts') is None
    assert 'synthetic-secret' not in json.dumps(outcome.get('metadata_prefetch_rpc_attempts'), allow_nan=False)
    assert outcome['_receipt_clock'] is anchor
    stamps = {e['name']: e['receipt_elapsed_ms'] for e in outcome['receipt_stages']}
    assert stamps['metadata_read_end'] >= stamps['metadata_read_start'] >= 0.
