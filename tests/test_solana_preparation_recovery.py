"""Offline regressions for funded pool-only readers and transient account seed loss."""
import asyncio
import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from sniper.execution import pump_curve_buy as curve
from sniper.services import memecoin_slot_lane as lane
from sniper.services import memecoin_solana_swap_marks as marks
from sniper.settings import memecoin as cfg
from test_memecoin_shyft_accounts import ACCOUNT, OTHER, price_book, vault_row

POOL = 'https://rpc.ams.shyft.to/?api_key=offline-pool'
SELECTED = 'https://rpc.fra.shyft.to/?api_key=offline-context'
USER = '11111111111111111111111111111111'


@pytest.fixture
def readers(monkeypatch):
    from sniper.execution import fast_http, solana_real_seams
    monkeypatch.setattr(cfg, 'MEMECOIN_SOLANA_INFRA_PROVIDER', 'shyft')
    monkeypatch.setattr(cfg, 'MEMECOIN_SOLANA_RPC_URL', '')
    monkeypatch.setattr(cfg, 'MEMECOIN_SOLANA_RPC_POOL_URLS', (POOL,))
    calls = []

    def ordinary(url, method, params, http):
        calls.append((url, method))
        return 200, {'result': 'offline-read'}

    class Client:
        def __init__(self, **kwargs):
            pass

        def request(self, method, url, **kwargs):
            request = json.loads(kwargs['body'])
            calls.append((url, request['method']))
            result = {'context': {'slot': 1}, 'value': None} if request['params'] else 'offline-read'
            return 200, {'result': result}, None

    monkeypatch.setattr(solana_real_seams, '_rpc_call', ordinary)
    monkeypatch.setattr(fast_http, 'PooledHttpClient', Client)
    return calls


def test_default_curve_and_pool_readers_accept_shyft_pool_without_primary(readers):
    assert cfg.solana_funded_rpc_urls() == (POOL,)
    assert curve.SlotLaneCurveBuilder(user=USER).rpc('getAccountInfo', []) == 'offline-read'
    assert curve.keepalive_custody_rpc()('getMultipleAccounts', []) == 'offline-read'
    assert readers == [(POOL, 'getAccountInfo'), (POOL, 'getMultipleAccounts')]


def test_production_builders_and_graduation_use_context_selected_custody(monkeypatch, tmp_path, readers):
    from sniper.execution import meteora_damm_v2_buy, meteora_dbc_buy, raydium_cpmm_buy, raydium_launchlab_buy
    from sniper.services import memecoin_solana_provider, memecoin_slot_signal, memecoin_shyft_signal
    from sniper.services import memecoin_shyft_prepare, memecoin_research_rpc, memecoin_slot_graduation
    from sniper.services import memecoin_real_runner
    monkeypatch.setenv('SNIPER_STATE_ROOT', str(tmp_path))
    monkeypatch.setattr(memecoin_solana_provider, 'infra_provider', lambda: 'shyft')
    monkeypatch.setattr(memecoin_solana_provider, 'provider_urls', lambda role: (POOL,))
    monkeypatch.setattr(memecoin_shyft_signal, 'live_config', lambda: None)
    monkeypatch.setattr(memecoin_shyft_prepare, 'prepare_config', lambda: None)
    monkeypatch.setattr(memecoin_research_rpc, 'funded_dependency_url', lambda *a: None)
    monkeypatch.setattr(memecoin_slot_signal, '_shares_funded_key', lambda _: False)
    for module in (raydium_cpmm_buy, raydium_launchlab_buy, meteora_damm_v2_buy):
        monkeypatch.setattr(module, 'lane_mode', lambda: 'off')
    monkeypatch.setattr(meteora_dbc_buy, 'dbc_live', lambda: False)
    monkeypatch.setattr(lane, 'routed_mode', lambda: 'off')
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_MIGRATION_PREWARM', False)
    capture = {}
    original_start = lane.start_slot_lane
    monkeypatch.setattr(lane, 'start_slot_lane', lambda rc, **kwargs: capture.update(kwargs))
    rc = dict(pubkey=USER, withdraw_rpc_url=SELECTED, custody_ready_fn=lambda: True,
        slot_lock_key='offline-sol', usdc_fn=lambda: 100, sol_fn=lambda: 1,
        latest_blockhash_fn=lambda: ('hash', 1))
    lane.start_from_context(rc, log=lambda _: None)
    builders = capture['builders']
    for builder in builders.values():
        assert builder.rpc('getAccountInfo', []) == 'offline-read'
    assert builders['pump_amm'].pools.rpc('getMultipleAccounts', []) == 'offline-read'
    assert readers == [(SELECTED, 'getAccountInfo')] * 2 + [(SELECTED, 'getMultipleAccounts')]

    # Construct the actual startup closure; no receiver/prefetch/learner thread runs.
    monkeypatch.setattr(threading.Thread, 'start', lambda _: None)
    monkeypatch.setattr(lane, 'load_cu_demotions', lambda *a, **k: set())
    monkeypatch.setattr(lane, 'cu_demotion_writer', lambda *a, **k: None)
    monkeypatch.setattr(memecoin_real_runner, '_momentum_read_pool',
                        lambda: SimpleNamespace(submit=lambda *a: None))
    monkeypatch.setattr(memecoin_slot_graduation, 'mode', lambda: 'shadow')
    captured = {}
    original_watcher = memecoin_slot_graduation.GraduationWatcher
    def watcher(**kwargs):
        captured.update(kwargs)
        return original_watcher(**kwargs)
    monkeypatch.setattr(memecoin_slot_graduation, 'GraduationWatcher', watcher)
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_MISS_LEARN', False)
    monkeypatch.setattr(raydium_launchlab_buy, 'warm_legs_on', lambda: False)
    conn = sqlite3.connect(':memory:')
    try:
        original_start(rc, builders=builders, signal_start_fn=lambda *a, **k: None,
            sol_usd_fn=lambda: 150, log=lambda _: None, roster_path=str(tmp_path / 'owners'),
            conn_factory=lambda: conn, readonly_conn_factory=lambda: conn)
        captured['read_fn'](USER)
    finally:
        conn.close()
    assert readers[-1] == (SELECTED, 'getMultipleAccounts')


def test_context_preparation_retains_custody_identity_gate_without_provider_reads(readers):
    ready = [False]
    rpc = lane._context_custody_rpc(dict(withdraw_rpc_url=SELECTED, custody_ready_fn=lambda: ready[0]))
    with pytest.raises(RuntimeError, match='custody_identity_unverified'):
        rpc.ping()
    assert readers == []
    ready[0] = True
    assert rpc('getAccountInfo', []) == 'offline-read'
    assert readers == [(SELECTED, 'getAccountInfo')]


@pytest.mark.parametrize('failure', ['null', 'exception', 'invalid_decimals'])
def test_failed_seed_retries_off_consumer_and_restores_raw_prices(monkeypatch, failure):
    now = [100.0]
    monkeypatch.setattr(marks, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    stream = marks.AccountPriceStream('https://grpc.ams.shyft.to', 'offline')
    vaults, bindings, mint, quote = price_book()
    calls, threads, rows = {}, [], []
    cache = marks.SolanaSwapMarkCache()
    consumer_thread = threading.get_ident()
    def seed(address):
        threads.append(threading.get_ident())
        calls[address] = calls.get(address, 0) + 1
        if address == ACCOUNT and calls[address] == 1:
            if failure == 'exception':
                raise RuntimeError('offline unavailable')
            return (100, 100, True) if failure == 'invalid_decimals' else None
        return 100, 100 if address == ACCOUNT else 1000, 6 if address == ACCOUNT else 9
    def consume(row):
        rows.append(row)
        cache.record(row, next(iter(bindings.values())), now_ts=now[0], observed_at='t')
    async def settle():
        await asyncio.sleep(.03)
    async def exercise():
        task = asyncio.create_task(stream.stream(vaults, consume, bindings=bindings, vault_seed_fn=seed))
        try:
            await asyncio.sleep(.005)
            stream.status.update(connected=True, subscription_acknowledged=True,
                requested_accounts=stream.watched(), connection_generation=1, watch_generation=1)
            await settle()
            assert calls == {ACCOUNT: 1, OTHER: 1}
            for address, token, amount in ((ACCOUNT, mint, 100), (OTHER, quote, 1000)):
                row = vault_row(address, token, amount, slot=100, baseline=True)
                row['recv_monotonic'] = now[0]
                stream.queue.offer(row)
            await settle()
            assert cache.fresh_price_map({quote: 1}, now_ts=now[0], max_age_s=2) == {}
            now[0] += .49
            await settle()
            assert calls[ACCOUNT] == 1, 'retry cannot run on every consumer iteration'
            now[0] += .02
            await settle()
            assert calls == {ACCOUNT: 2, OTHER: 1}
            for address, token, amount in ((ACCOUNT, mint, 90), (OTHER, quote, 1100)):
                row = vault_row(address, token, amount, slot=101)
                row['recv_monotonic'] = now[0]
                stream.queue.offer(row)
            await settle()
            assert cache.fresh_price_map({quote: 1}, now_ts=now[0], max_age_s=2)
            assert all(tid != consumer_thread for tid in threads)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    asyncio.run(exercise())


def test_seed_outage_has_three_attempt_bound_per_account_generation(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(marks, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    stream = marks.AccountPriceStream('https://grpc.ams.shyft.to', 'offline')
    vaults, bindings, _, _ = price_book()
    calls = []
    def seed(address):
        calls.append(address)
        return None
    async def exercise():
        task = asyncio.create_task(stream.stream(vaults, lambda _: None, bindings=bindings, vault_seed_fn=seed))
        try:
            await asyncio.sleep(.005)
            stream.status.update(connected=True, subscription_acknowledged=True,
                requested_accounts=stream.watched(), connection_generation=1, watch_generation=1)
            for jump in (0, .51, 2.01, 10, 10):
                now[0] += jump
                await asyncio.sleep(.03)
            assert calls.count(ACCOUNT) == calls.count(OTHER) == 3
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    asyncio.run(exercise())
