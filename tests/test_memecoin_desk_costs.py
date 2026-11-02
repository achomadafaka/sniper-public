"""Off-path pricing uses prior FX, bounded queues, and real expense accounting."""
import importlib
import hashlib
import sys
import subprocess
from pathlib import Path

from solders.pubkey import Pubkey
from solders.signature import Signature
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

from sniper.persistence import db_memecoin as ledger
from test_memecoin_desk_cohort import NOW, ledger as memory_ledger, position, register

BLOCK = int(datetime.fromisoformat(NOW).timestamp())
CLOSE = BLOCK // 60 * 60


def costs():
    return importlib.import_module('sniper.services.memecoin_desk_costs')


def iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def test_fx_uses_last_completed_minute_not_current_or_older_price():
    calls = []
    rows = [[CLOSE, 900, 901, 900, 900, 1],
            [CLOSE - 60, 99, 103, 100, 101, 10],
            [CLOSE - 120, 80, 90, 85, 86, 10]]
    def http(url, **kw):
        calls.append((url, kw))
        return rows
    result = costs().read_sol_usd_quote(BLOCK + 20, http_json=http, now_iso=iso(BLOCK + 30))
    assert result == {'price': 101.0, 'observed_at': iso(CLOSE),
                      'known_at': iso(BLOCK + 30), 'source': 'coinbase:SOL-USD:60s'}
    parsed = urlsplit(calls[0][0])
    assert parsed.netloc == 'api.exchange.coinbase.com'
    assert parsed.path == '/products/SOL-USD/candles'
    assert parse_qs(parsed.query) == {'granularity': ['60'], 'start': [iso(CLOSE - 60)],
                                     'end': [iso(CLOSE)]}


@pytest.mark.parametrize('rows', [[], {}, [[CLOSE, 99, 103, 100, 101, 1]],
    [[CLOSE - 120, 99, 103, 100, 101, 1]], [[CLOSE - 60, 99, 103, 100, None, 1]],
    [[CLOSE - 60, 99, 103, 100, True, 1]], [[CLOSE - 60, 99, 103, 100, float('nan'), 1]],
    [[CLOSE - 60, 99, 103, 100, 101, 1], [CLOSE - 60, 99, 103, 100, 102, 1]]])
def test_bad_or_missing_fx_never_becomes_zero(rows):
    with pytest.raises(ValueError):
        costs().read_sol_usd_quote(BLOCK + 20, http_json=lambda *a, **kw: rows,
                                  now_iso=iso(BLOCK + 30))


def test_receipt_rpc_is_confirmed_parsed_and_exact_request():
    calls = []
    def http(url, **kw):
        calls.append((url, kw))
        return {'jsonrpc': '2.0', 'id': 1, 'result': {'slot': 5}}
    assert costs().read_solana_receipt('sig', rpc_url='https://read.example/rpc', http_json=http) == {'slot': 5}
    payload = calls[0][1]['payload']
    assert payload == {'jsonrpc': '2.0', 'id': 1, 'method': 'getTransaction',
        'params': ['sig', {'encoding': 'jsonParsed', 'commitment': 'confirmed',
                           'maxSupportedTransactionVersion': 1}]}


@pytest.mark.parametrize('reply', [None, {}, {'result': None},
    {'jsonrpc': '2.0', 'id': 2, 'result': {}},
    {'jsonrpc': '2.0', 'id': 1, 'error': {'code': -32005, 'message': 'rate limit'}}])
def test_rpc_unavailable_or_foreign_reply_is_not_an_empty_receipt(reply):
    with pytest.raises(ValueError):
        costs().read_solana_receipt('sig', rpc_url='https://read.example/rpc',
                                    http_json=lambda *a, **kw: reply)


def sig(label):
    return str(Signature.from_bytes(hashlib.sha256(label.encode()).digest() * 2))


def failed_request(conn, cohort, signature='failed-sig', mint='failed-mint'):
    pending = conn.execute(
        "INSERT INTO memecoin_pending_entries "
        "(mint,network_id,reserved_at,reserved_notional_usd,signal_source,desk_cohort_id,"
        "desk_producer,desk_opportunity_id,tx_sig,status) VALUES (?,'solana',?,25,'desk',?,'H',?,?,'failed')",
        (str(Pubkey.from_bytes(hashlib.sha256(mint.encode()).digest())), NOW, cohort, 'test:'+mint, sig(signature))).lastrowid
    conn.commit()
    return pending


def failed_receipt(signature, owner, fee=5000):
    return {'slot': 123, 'blockTime': BLOCK, 'transaction': {'signatures': [signature],
        'message': {'accountKeys': [{'pubkey': owner, 'signer': True}]}},
        'meta': {'err': {'InstructionError': [0, {'Custom': 6001}]}, 'fee': fee,
                 'preBalances': [1_000_000], 'postBalances': [1_000_000-fee],
                 'preTokenBalances': [], 'postTokenBalances': []}}


def test_batch_prices_actual_failed_charge_once_without_transaction_during_io():
    conn = memory_ledger()
    cohort = register(conn)
    failed_request(conn, cohort)
    owner = '11111111111111111111111111111111'
    calls = []
    def receipt(sig):
        assert not conn.in_transaction
        calls.append(sig)
        return failed_receipt(sig, owner)
    def fx(at):
        assert not conn.in_transaction
        assert at == BLOCK
        return {'price': 100.0, 'observed_at': iso(BLOCK), 'known_at': iso(BLOCK+1), 'source': 'fixture:USD'}
    result = costs().price_execution_batch(conn, conn, owner=owner, receipt_fn=receipt,
        usd_quote_fn=fx, now_iso_fn=lambda: iso(BLOCK+2), limit=10)
    assert [row['state'] for row in result['results']] == ['priced']
    assert ledger.desk_cohort_accounting(conn, cohort)['known_cost_usd'] == pytest.approx(.0005)
    assert ledger.desk_cohort_accounting(conn, cohort)['unknown_costs'] == 0
    assert not conn.in_transaction
    second = costs().price_execution_batch(conn, conn, owner=owner, receipt_fn=receipt,
        usd_quote_fn=fx, now_iso_fn=lambda: iso(BLOCK+3), limit=10)
    assert second['results'] == []
    assert calls == [sig('failed-sig')]


def test_zero_receipt_does_not_require_fx():
    conn = memory_ledger()
    cohort = register(conn)
    failed_request(conn, cohort)
    owner = '11111111111111111111111111111111'
    def no_fx(_):
        pytest.fail('A proven zero has no currency conversion')
    result = costs().price_execution_batch(conn, conn, owner=owner,
        receipt_fn=lambda sig: failed_receipt(sig, owner, 0), usd_quote_fn=no_fx,
        now_iso_fn=lambda: iso(BLOCK+2))
    assert result['results'][0]['state'] == 'priced'
    assert ledger.desk_cohort_accounting(conn, cohort)['unknown_costs'] == 0
    assert ledger.desk_cohort_accounting(conn, cohort)['known_cost_usd'] == 0


def test_unavailable_oldest_does_not_starve_later_request():
    conn = memory_ledger()
    cohort = register(conn)
    failed_request(conn, cohort, 'sig-a', 'mint-a')
    failed_request(conn, cohort, 'sig-b', 'mint-b')
    owner = '11111111111111111111111111111111'
    def receipt(sig):
        return None if sig == globals()['sig']('sig-a') else failed_receipt(sig, owner, 0)
    first = costs().price_execution_batch(conn, conn, owner=owner, receipt_fn=receipt,
        usd_quote_fn=lambda _: None, now_iso_fn=lambda: iso(BLOCK+2), limit=1)
    assert first['results'][0]['state'] == 'deferred'
    second = costs().price_execution_batch(conn, conn, owner=owner, receipt_fn=receipt,
        usd_quote_fn=lambda _: None, now_iso_fn=lambda: iso(BLOCK+2), limit=1,
        after=first['next_cursor'])
    assert second['results'][0]['state'] == 'priced'
    assert ledger.desk_cohort_accounting(conn, cohort)['unknown_costs'] == 1


def test_rate_limit_stops_batch_without_erasing_unknowns():
    conn = memory_ledger()
    cohort = register(conn)
    failed_request(conn, cohort, 'sig-a', 'mint-a')
    failed_request(conn, cohort, 'sig-b', 'mint-b')
    calls = []
    def limited(sig):
        calls.append(sig)
        raise costs().CostDependencyError('provider_rate_limited', stop_batch=True)
    result = costs().price_execution_batch(conn, conn, owner='11111111111111111111111111111111',
        receipt_fn=limited, usd_quote_fn=lambda _: None, now_iso_fn=lambda: iso(BLOCK+2))
    assert len(result['results']) == len(calls) == 1
    assert result['results'][0]['reason'] == 'provider_rate_limited'
    assert ledger.desk_cohort_accounting(conn, cohort)['unknown_costs'] == 2


def test_worker_refuses_open_sqlite_transaction_before_provider_io():
    conn = memory_ledger()
    cohort = register(conn)
    failed_request(conn, cohort)
    conn.execute('BEGIN')
    with pytest.raises(ValueError, match='transaction'):
        costs().price_execution_batch(conn, conn, owner='11111111111111111111111111111111',
            receipt_fn=lambda _: pytest.fail('provider under caller transaction'),
            usd_quote_fn=lambda _: None, now_iso_fn=lambda: iso(BLOCK+2))
    conn.rollback()


def test_fx_known_at_is_when_the_response_arrives():
    arrived = []
    def http(*a, **kw):
        arrived.append(True)
        return [[CLOSE-60, 99, 103, 100, 101, 1]]
    def clock():
        assert arrived
        return iso(BLOCK+8)
    result = costs().read_sol_usd_quote(BLOCK, http_json=http, now_iso_fn=clock)
    assert result['known_at'] == iso(BLOCK+8)


def test_worker_cli_help_and_missing_ledger_never_creates_database(tmp_path):
    script = Path(__file__).resolve().parents[1] / 'scripts/memecoin_desk_cost_worker.py'
    help_result = subprocess.run([sys.executable, str(script), '--help'], capture_output=True, text=True)
    assert help_result.returncode == 0
    missing = tmp_path/'missing.db'
    result = subprocess.run([sys.executable, str(script), '--ledger-db', str(missing),
        '--owner', '11111111111111111111111111111111', '--read-rpc-url',
        'https://research.example/rpc'], capture_output=True, text=True)
    assert result.returncode == 2
    assert 'must exist' in result.stderr
    assert not missing.exists()


def test_http_rate_limit_is_single_attempt_and_does_not_leak_url(monkeypatch):
    from urllib.error import HTTPError
    calls = []
    def limited(request, **kw):
        calls.append(request)
        raise HTTPError(request.full_url, 429, 'secret URL text', {}, None)
    monkeypatch.setattr(costs(), 'urlopen', limited)
    with pytest.raises(costs().CostDependencyError, match='^provider_rate_limited$') as error:
        costs()._http_json('https://read.example/secret-key')
    assert error.value.stop_batch is True
    assert len(calls) == 1


def test_http_response_is_bounded_and_partial_json_is_not_zero(monkeypatch):
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self, bound):
            assert bound == 2097153
            return b'{' * bound
    monkeypatch.setattr(costs(), 'urlopen', lambda *a, **kw: Response())
    with pytest.raises(ValueError, match='provider_response_too_large'):
        costs()._http_json('https://read.example/rpc')


def test_truncated_http_response_is_typed_deferred_not_worker_crash(monkeypatch):
    from http.client import IncompleteRead
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self, _):
            raise IncompleteRead(b'{', 100)
    monkeypatch.setattr(costs(), 'urlopen', lambda *a, **kw: Response())
    with pytest.raises(costs().CostDependencyError, match='^provider_partial_response$'):
        costs()._http_json('https://read.example/rpc')


def worker_module():
    path = Path(__file__).resolve().parents[1] / 'scripts/memecoin_desk_cost_worker.py'
    spec = importlib.util.spec_from_file_location('desk_cost_worker_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cost_worker_requires_explicit_rpc_even_with_legacy_env_default(tmp_path, monkeypatch, capsys):
    worker = worker_module()
    monkeypatch.setenv('MEMECOIN_DESK_COST_READ_RPC_URL', 'https://research.example/rpc')
    monkeypatch.setattr(worker, 'get_readonly_connection', lambda *_: pytest.fail('DB opened before RPC validation'))
    path = tmp_path/'exists.db'
    path.touch()
    with pytest.raises(SystemExit) as exc:
        worker.main(['--ledger-db', str(path), '--owner', '11111111111111111111111111111111'])
    assert exc.value.code == 2
    assert '--read-rpc-url' in capsys.readouterr().err


@pytest.mark.parametrize('url', ['https://api.mainnet-beta.solana.com',
    'https://MAINNET-BETA.SOLANA.COM.:443/rpc', 'https://custody.example/other?api-key=research',
    'https://pool.example/rpc', 'https://critical-read.example/rpc'])
def test_cost_rpc_refuses_exact_funded_hosts_and_shared_public_ip(url):
    env = {'MEMECOIN_SOLANA_RPC_URL': 'https://custody.example/rpc?api-key=funded',
           'MEMECOIN_SOLANA_RPC_POOL_URLS': 'https://pool.example/a,https://pool-2.example/b',
           'MEMECOIN_SOLANA_READ_RPC_URL': 'https://critical-read.example/rpc'}
    with pytest.raises(ValueError, match='funded'):
        worker_module().validate_read_rpc_url(url, env)


def test_cost_rpc_refuses_same_funded_credential_on_another_host():
    env = {'MEMECOIN_SOLANA_RPC_URL': 'https://funded.example/rpc?api-key=SHARED-SECRET'}
    with pytest.raises(ValueError, match='funded') as exc:
        worker_module().validate_read_rpc_url('https://other-region.example/rpc?api-key=SHARED-SECRET', env)
    assert 'SHARED-SECRET' not in str(exc.value)


def test_cost_rpc_refuses_the_funded_tape_lane_host():
    env = {'MEMECOIN_SOLANA_RPC_URL': 'https://custody.example/rpc?api-key=FUNDED',
           'MEMECOIN_SOL_TAPE_RPC_URL': 'https://read-lane.example/rpc?api-key=READ-LANE'}
    with pytest.raises(ValueError, match='funded'):
        worker_module().validate_read_rpc_url('https://read-lane.example/other?api-key=RESEARCH', env)


def test_the_watcher_lane_refuses_its_credential_not_its_shared_host():
    """Alpha 2026-10-22: the watcher's mainnet.helius-rpc.com host also serves separate research keys; the old
    host rule refused every research Helius key (manual cost pass exit 2)."""
    env = {'MEMECOIN_SOLANA_RPC_URL': 'https://custody.example/rpc?api-key=FUNDED',
           'ONCHAIN_WATCH_HELIUS_RPC_URL': 'https://mainnet.helius-rpc.com/?api-key=WATCHER'}
    research = 'https://mainnet.helius-rpc.com/?api-key=RESEARCH'
    assert worker_module().validate_read_rpc_url(research, env) == research
    with pytest.raises(ValueError, match='funded'):
        worker_module().validate_read_rpc_url('https://mainnet.helius-rpc.com/other?api-key=WATCHER', env)


@pytest.mark.parametrize('query', ['api-key=RESEARCH&api_key=FUNDED',
    'api_key=RESEARCH&api_key=FUNDED', 'api-key=&api_key=FUNDED', 'api_key='])
@pytest.mark.parametrize('ambiguous_funded', [False, True])
def test_cost_rpc_rejects_ambiguous_or_empty_credentials(query, ambiguous_funded):
    candidate = 'https://research.example/rpc?api-key=RESEARCH'
    funded = 'https://funded.example/rpc?api_key=FUNDED'
    if ambiguous_funded:
        funded = 'https://funded.example/rpc?' + query
    else:
        candidate = 'https://research.example/rpc?' + query
    with pytest.raises(ValueError, match='RPC configuration') as exc:
        worker_module().validate_read_rpc_url(candidate, {'MEMECOIN_SOLANA_RPC_URL': funded})
    assert 'FUNDED' not in str(exc.value) and 'RESEARCH' not in str(exc.value)


def test_cost_rpc_requires_known_funded_configuration_and_accepts_isolated_exact_host():
    worker = worker_module()
    with pytest.raises(ValueError, match='funded RPC configuration'):
        worker.validate_read_rpc_url('https://research.example/rpc', {})
    env = {'MEMECOIN_SOLANA_RPC_URL': 'https://funded.example/rpc?api-key=FUNDED'}
    before = dict(env)
    # The guard compares canonical hosts/credentials, never arbitrary URL substrings.
    candidate = 'https://research.example/mainnet-beta.solana.com?api-key=RESEARCH'
    assert worker.validate_read_rpc_url(candidate, env) == candidate
    assert env == before
