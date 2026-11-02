"""Offline startup/first-request witnesses; no funded network or custody."""
import asyncio
import json
import threading
from types import SimpleNamespace
from urllib.parse import urlsplit

import base58
import pytest

from sniper.feeds.yellowstone import geyser_pb2 as pb

READ = 'https://rpc.ams.shyft.to/?api_key=offline'
ENV = {'MEMECOIN_SOLANA_INFRA_PROVIDER': 'shyft', 'MEMECOIN_FUNDED_ONLY': '1',
       'MEMECOIN_SOLANA_READ_RPC_URL': READ, 'MEMECOIN_SOLANA_RPC_URL': READ,
       'ONCHAIN_WATCH_SOLANA_RPC_URL': READ}


def test_explicit_funded_dependency_first_request_uses_neutral_eu_read():
    from sniper.services import memecoin_research_rpc as r
    requests = []
    def http(method, url, **kw):
        assert urlsplit(url).hostname in {'rpc.ams.shyft.to', 'rpc.fra.shyft.to'}
        requests.append(json.loads(kw['body']))
        return 200, {'result': 125}, None
    call, pool = r.build_dependency_read_call('signal_pool_resolver', env=ENV, http=http, min_call_s=.001)
    assert call('getBlockTime', [10]) == 125
    assert requests[0]['method'] == 'getBlockTime'
    assert r.funded_overlap([READ], ENV)
    with pytest.raises(ValueError, match='research_funded_overlap'):
        r.build_research_call('research', env={**ENV, 'MEMECOIN_RESEARCH_SOLANA_RPC_URLS': READ})


def test_funded_dependency_never_turns_json_rpc_error_into_empty_success():
    from sniper.services import memecoin_research_rpc as r
    call, _ = r.build_dependency_read_call('signal_pool_resolver', env=ENV,
        http=lambda *a, **k: (200, {'error': {'code': -1, 'message': 'secret'}}, None), min_call_s=.001)
    with pytest.raises(RuntimeError, match='rpc_read_failed'):
        call('getMultipleAccounts', [[]])


def test_research_lane_cannot_claim_funded_purpose():
    from sniper.services import memecoin_research_rpc as r
    with pytest.raises(ValueError, match='funded_purpose_invalid'):
        r.build_dependency_read_call('cohort', env=ENV)


def test_processed_commitment_zero_is_not_defaulted_to_confirmed():
    from sniper.services.memecoin_shyft_accounts import subscription
    address = base58.b58encode(bytes([3]) * 32).decode()
    assert subscription([address], 'source', commitment=pb.PROCESSED).commitment == pb.PROCESSED
    assert subscription([], 'source', commitment=pb.PROCESSED).accounts == {}


def test_damm_program_filter_is_explicit_on_existing_processed_subscription():
    from sniper.services.memecoin_shyft_signal import subscription
    from sniper.execution.meteora_damm_v2_buy import PROGRAM
    request = subscription({}, damm=True, damm_label='damm:c1:r1')
    assert request.commitment == pb.PROCESSED
    assert list(request.transactions) == ['damm:c1:r1']
    assert list(request.transactions['damm:c1:r1'].account_include) == [PROGRAM]


def test_owner_account_shyft_deletion_is_unknown_not_zero():
    from sniper.services.memecoin_source_sell_mirror import OwnerAccountWatch
    watch = OwnerAccountWatch(ws_url=None, rpc_url=READ, sink=lambda *a: None,
        shyft_config=('offline', ('https://grpc.ams.shyft.to',)),
        post=lambda body: {'result': {'context': {'slot': 10}, 'value': [None]}})
    assert watch.read_account('account') is None


def test_owner_account_shyft_account_identity_is_checked():
    from sniper.services.memecoin_source_sell_mirror import OwnerAccountWatch, TOKEN_PROGRAM
    mint = base58.b58encode(bytes([4]) * 32).decode()
    owner = base58.b58encode(bytes([5]) * 32).decode()
    from sniper.execution.pump_native_exit import ata
    account = str(ata(owner, mint, TOKEN_PROGRAM))
    events, gaps = [], []
    watch = OwnerAccountWatch(ws_url=None, rpc_url=READ, sink=lambda *a: events.append(a),
        on_gap=lambda *a: gaps.append(a), shyft_config=('offline', ('https://grpc.ams.shyft.to',)))
    watch.set_want({account: {'mint': mint, 'owner': owner}})
    data = bytearray(165)
    data[:32] = base58.b58decode(mint)
    data[32:64] = base58.b58decode(owner)
    data[64:72] = (100).to_bytes(8, 'little')
    data[108] = 1
    row = dict(kind='account', pubkey=account, owner=TOKEN_PROGRAM, data=bytes(data),
        deleted=False, slot=10, write_version=1, connection_generation=1, watch_generation=1,
        baseline_only=True, recv_monotonic=__import__('time').monotonic())
    watch.status.update(connection_generation=1, watch_generation=1, subscription_acknowledged=True)
    watch._consume_yellowstone(row)
    assert not events  # first fresh account state is a baseline, not a source sell
    row.update(slot=11, write_version=2, baseline_only=False)
    data[64:72] = (80).to_bytes(8, 'little')
    row['data'] = bytes(data)
    watch._consume_yellowstone(row)
    assert events[-1][:3] == (account, 11, 80)
    from sniper.services.memecoin_source_sell_mirror import TOKEN_2022_PROGRAM
    row.update(slot=12, owner=TOKEN_2022_PROGRAM)
    watch._consume_yellowstone(row)
    assert len(events) == 1 and watch.status['last_gap'] == 'account_identity'
    row['owner'] = TOKEN_PROGRAM
    row.update(slot=13, write_version=3, deleted=True, data=b'')
    watch._consume_yellowstone(row)
    assert len(events) == 1 and gaps


@pytest.mark.parametrize('startup_slot,startup_amount', [(104, 110), (105, 90), (106, 95)])
def test_owner_stream_respects_unordered_http_seed_bank_and_preserves_newer_deltas(startup_slot, startup_amount):
    import time
    from sniper.services import memecoin_source_sell_mirror as m
    from sniper.execution.pump_native_exit import ata
    mint, owner = [base58.b58encode(bytes([x]) * 32).decode() for x in (4, 5)]
    address = str(ata(owner, mint, m.TOKEN_PROGRAM))
    mirror = m.SourceSellMirror(enabled=True, shadow=False, min_fraction=.1,
        clock=lambda: 10., wall=lambda: 1000., real_now_fn=lambda: '2026-10-12T12:00:00+00:00')
    state = dict(account=address, mint=mint, owner=owner, slot=105, amount=100,
        baseline_slot=105, baseline_amount=100, uid='offline', handle='offline',
        position_id=1, resolved_mono=9.)
    mirror._acct_by_account[address] = state
    sales = []
    def sink(*row):
        sale = mirror._from_account(row[:3], row[3])
        if sale is not None: sales.append(sale)
    watch = m.OwnerAccountWatch(ws_url=None, rpc_url=READ, sink=sink,
        on_baseline=lambda a,s,q,t: mirror._from_account((a,s,q),t,baseline_only=True),
        shyft_config=('offline', ('https://grpc.ams.shyft.to',)))
    watch.set_want({address: state})
    watch.status.update(connection_generation=1, watch_generation=1, subscription_acknowledged=True)
    data = bytearray(165)
    data[:32], data[32:64], data[108] = base58.b58decode(mint), base58.b58decode(owner), 1
    def receive(slot, amount, baseline, version):
        data[64:72] = amount.to_bytes(8, 'little')
        watch._consume_yellowstone(dict(kind='account', pubkey=address, owner=m.TOKEN_PROGRAM,
            slot=slot, data=bytes(data), write_version=version, baseline_only=baseline,
            connection_generation=watch.status['connection_generation'], watch_generation=1,
            recv_monotonic=time.monotonic()))
    receive(startup_slot, startup_amount, True, 1)
    if startup_slot <= 105:
        receive(105, 90, False, 2)
        receive(105, 100, False, 3)
        assert not sales and state['amount'] == 100 and state['slot'] == 105
        receive(106, 95, False, 4)  # first ordered stream baseline beyond the HTTP bank
    assert not sales and state['amount'] == 95 and state['slot'] == 106
    receive(106, 85, False, 5)
    assert [(s.slot, s.sold_atomic, s.pre_atomic) for s in sales] == [(106, 10, 95)]
    watch.status['connection_generation'] = 2
    receive(106, 80, True, 1)  # reconnect/reset may not order the existing bank
    receive(106, 70, False, 2)
    assert len(sales) == 1 and state['amount'] == 85
    receive(107, 75, False, 3)
    assert len(sales) == 1 and state['amount'] == 75
    receive(107, 65, False, 4)
    assert [(s.slot, s.sold_atomic, s.pre_atomic) for s in sales] == [(106, 10, 95), (107, 10, 75)]
    assert 'write_version' not in state  # HTTP never acquires a fabricated write version


def test_owner_shyft_http_reread_resets_unordered_bank_boundary_without_a_sale():
    from sniper.services import memecoin_source_sell_mirror as m
    mirror = m.SourceSellMirror(enabled=True, shadow=False, min_fraction=.1,
        clock=lambda: 10., wall=lambda: 1000., real_now_fn=lambda: '2026-10-12T12:00:00+00:00')
    state = dict(account='account', mint='mint', owner='owner', slot=105, amount=100,
        baseline_slot=105, baseline_amount=100, uid='offline', handle='offline',
        position_id=1, resolved_mono=9.)
    mirror._acct_by_account['account'] = state
    mirror.account_watch = SimpleNamespace(_shyft_config=('offline', ('https://grpc.ams.shyft.to',)))
    mirror._from_account(('account', 106, 95), 9., baseline_only=True)
    assert mirror._from_reread(('account', (107, 80), 9.5, 1., state.get('generation', 0)), 9.6) is None
    assert state['slot'] == 107 and state['amount'] == 80
    assert mirror._from_account(('account', 107, 70), 9.7) is None
    assert mirror._from_account(('account', 107, 80), 9.8) is None
    assert mirror._from_account(('account', 108, 75), 9.9) is None
    sale = mirror._from_account(('account', 108, 65), 10.)
    assert (sale.slot, sale.sold_atomic, sale.pre_atomic) == (108, 10, 75)

from test_memecoin_j7_verified_backup import setup, _native_attempt


def test_shyft_confirmed_proof_backup_does_not_need_external_hint(setup, monkeypatch):
    from sniper.services import memecoin_onchain_lane as lane
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, 'MEMECOIN_SOLANA_INFRA_PROVIDER', 'shyft')
    conn, _, _ = setup
    seen = []
    row = conn.execute('SELECT * FROM onchain_wallet_detections').fetchone()
    lane._run_hook(conn, lambda c, ev: seen.append(ev) or {'status': 'test'}, row)
    assert seen[0].j7_verified_backup and seen[0].delivery_lane == 'j7_verified_backup'


@pytest.mark.parametrize('source,lane_name', [('follow_activity', 'websocket'), ('feed', 'global')])
def test_shyft_external_solana_hint_never_admits_even_with_cached_receipt(setup, monkeypatch, source, lane_name):
    from sniper.services import memecoin_real_runner as runner
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, 'MEMECOIN_SOLANA_INFRA_PROVIDER', 'shyft')
    conn, ev, _ = setup
    ev.signal_source, ev.delivery_lane = source, lane_name
    _native_attempt(conn)
    hook = runner.make_real_hook(None, {'solana': {}}, now_fn=lambda: ev.created_at,
                                trade_size=25, max_age_s=60)
    result = hook(conn, ev)
    assert result['status'] == 'hint_only' and result['reason'] == 'yellowstone_source_required'


def test_shyft_coin_age_uses_standard_confirmed_signature_lower_bound(monkeypatch):
    from sniper.services import memecoin_coin_age as age
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, 'MEMECOIN_SOLANA_INFRA_PROVIDER', 'shyft')
    monkeypatch.setattr(cfg, 'MEMECOIN_SOLANA_READ_RPC_URL', READ)
    calls = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return json.dumps({'result': [
            {'err': None, 'blockTime': 130}, {'err': 'failed', 'blockTime': 1},
            {'err': None, 'blockTime': 100}]}).encode()
    def open_request(req, **kw):
        assert urlsplit(req.full_url).hostname == 'rpc.ams.shyft.to'
        calls.append(json.loads(req.data))
        return Response()
    monkeypatch.setattr(age.urllib.request, 'urlopen', open_request)
    assert age._helius_first_ts('mint') == 100
    assert calls[0]['method'] == 'getSignaturesForAddress'
    assert calls[0]['params'][1] == {'limit': 5, 'commitment': 'confirmed'}


def test_rh_deposit_processed_filter_keeps_exact_roster_and_depository():
    from sniper.services.memecoin_rh_deposit_proof import shyft_subscription, SOLANA_DEPOSITORY
    request = shyft_subscription(('owner',), 'deposits:c1:r1')
    assert request.commitment == pb.PROCESSED
    assert list(request.transactions['deposits:c1:r1'].account_include) == ['owner']
    assert list(request.transactions['deposits:c1:r1'].account_required) == [SOLANA_DEPOSITORY]
    assert not shyft_subscription((), 'idle').transactions


def test_recorded_rh_deposit_wire_proof_preserves_immediate_map(monkeypatch):
    from test_memecoin_rh_deposit_proof import ORIGIN, OWNERS, ARRIVED, ORDER_ID
    from test_memecoin_shyft_confirmed import wire_receipt
    from sniper.services import memecoin_rh_deposit_proof as dep
    update = wire_receipt(ORIGIN)
    proof, why = dep.shyft_deposit(update, owners=OWNERS, arrived=ARRIVED,
        recv_monotonic=1, head_slot=update.transaction.slot, head_monotonic=1)
    assert why is None and proof['order_id'] == ORDER_ID
    assert proof['amount_atomic'] == 600000000 and proof['uids'] == tuple(next(iter(OWNERS.values())))
    assert proof['arrived'] == ARRIVED


def test_funded_only_evidence_only_h1_probe_opens_zero_io(monkeypatch):
    from sniper.services import memecoin_solana_address_activity as a
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, 'MEMECOIN_FUNDED_ONLY', True)
    monkeypatch.setattr(cfg, 'MEMECOIN_SOL_H1_ACTIVE_SIGS', '')
    monkeypatch.setattr(cfg, 'MEMECOIN_SOL_H1_ACTIVITY_RPC_URL', 'https://legacy.invalid')
    monkeypatch.setattr(a, '_PROBE', {'built': False, 'probe': None, 'disabled_reason': None})
    monkeypatch.setattr(a, 'ActivityProbe', lambda *a, **k: pytest.fail('retired evidence probe constructed'))
    probe, reason = a.runtime_probe()
    assert probe is None and reason == 'funded_only_evidence_disabled'


def test_sol_tape_shyft_first_request_has_no_legacy_override(monkeypatch):
    from scripts import memecoin_sol_tape_worker as w
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, 'MEMECOIN_SOLANA_INFRA_PROVIDER', 'shyft')
    monkeypatch.setattr(cfg, 'MEMECOIN_SOLANA_READ_RPC_URL', READ)
    monkeypatch.setenv('MEMECOIN_SOL_TAPE_RPC_URL', 'https://legacy.invalid')
    with pytest.raises(ValueError, match='read_url_invalid'):
        w._build_rpc_call()


def test_funded_only_sol_tape_retains_funded_pass_and_ignores_stale_research_flags(monkeypatch):
    from scripts import memecoin_sol_tape_worker as w
    from sniper.settings import memecoin as cfg
    from sniper.settings import memecoin_v6_paper as v6
    monkeypatch.setattr(cfg, 'MEMECOIN_FUNDED_ONLY', True)
    monkeypatch.setattr(v6, 'MEMECOIN_V6_PAPER_ENABLED', True)
    monkeypatch.setenv('MEMECOIN_SOL_TAPE_ENABLED', '1')
    monkeypatch.setattr('dotenv.load_dotenv', lambda *a, **k: None)
    monkeypatch.setattr(w, '_connect_ledger', lambda: 'funded-ledger')
    monkeypatch.setattr(w, '_connect_sol_tape', lambda: 'tape')
    monkeypatch.setattr(w, '_build_rpc_call', lambda: object())
    monkeypatch.setattr(w, 'get_signatures_fn', lambda **k: 'signatures')
    monkeypatch.setattr(w, 'get_transaction_fn', lambda **k: 'transactions')
    passes = []
    monkeypatch.setattr(w, 'solana_tape_pass', lambda *a, **k: passes.append((a, k)) or 0)
    assert w.main(iterations=1) == 1
    assert len(passes) == 1 and passes[0][0][0] == 'funded-ledger'
    assert passes[0][1]['include_paper'] is False and not w._v6_capture_enabled()
    assert w.v6_capture_targets() == []


def test_evm_relay_origin_constructs_shyft_sol_reader(monkeypatch):
    from scripts.research import onchain_evm_wallet_watch as watch
    from sniper.services import memecoin_relay_initiation as relay
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, 'MEMECOIN_SOLANA_INFRA_PROVIDER', 'shyft')
    monkeypatch.setenv('MEMECOIN_SOLANA_READ_RPC_URL', READ)
    monkeypatch.delenv('MEMECOIN_RESEARCH_HELIUS_RPC_URL', raising=False)
    monkeypatch.setattr(watch, '_deposit_store', lambda: None)
    seen, calls = [], []
    def first_read(url, **kw):
        assert urlsplit(url).hostname == 'rpc.ams.shyft.to'
        calls.append((url, kw['body']['method']))
        return {'result': None}
    monkeypatch.setattr(relay, 'read_json', first_read)
    class Constructed(Exception): pass
    def verifier(*a, **kw):
        seen.append(kw['solana_rpc'])
        assert urlsplit(kw['solana_rpc']).hostname == 'rpc.ams.shyft.to'
        assert kw['fetch'](kw['solana_rpc'],
            body={'method': 'getTransaction', 'params': ['sig']}) == {'result': None}
        raise Constructed
    monkeypatch.setattr(relay, 'WatcherVerifier', verifier)
    with pytest.raises(Constructed): watch._relay_origin_worker(lambda *a: None)
    assert seen == [READ]
    assert calls == [(READ, 'getTransaction')]


@pytest.mark.parametrize('name,argv', [('memecoin_swap_sol_usdc', ['utility', '--amount', '1']),
                                     ('memecoin_reclaim_ata_rent', ['utility'])])
def test_funded_utilities_fail_unread_without_trying_legacy_provider(monkeypatch, name, argv):
    import importlib
    from sniper.settings import memecoin as cfg
    from sniper.execution import solana_wallet as wallet
    from sniper.util import envfiles
    module = importlib.import_module('scripts.' + name)
    monkeypatch.setattr(cfg, 'MEMECOIN_SOLANA_INFRA_PROVIDER', 'shyft')
    monkeypatch.setattr(cfg, 'MEMECOIN_SOLANA_RPC_URL', READ)
    monkeypatch.setattr(cfg, 'solana_funded_rpc_urls', lambda: [READ])
    monkeypatch.setenv('MEMECOIN_RESEARCH_HELIUS_RPC_URL', 'https://legacy.invalid')
    monkeypatch.setattr('sys.argv', argv)
    monkeypatch.setattr(envfiles, 'load_env_files', lambda *a: None)
    if hasattr(module, 'load_env_files'): monkeypatch.setattr(module, 'load_env_files', lambda *a: None)
    monkeypatch.setattr(wallet, 'load_keypair', lambda *a: SimpleNamespace(pubkey=lambda: 'owner'))
    monkeypatch.setattr(wallet, 'pubkey_str', lambda *a: 'owner')
    calls = []
    def unread(first, *a, **k):
        url = a[0] if name.endswith('swap_sol_usdc') else first
        calls.append(url)
        assert urlsplit(url).hostname in {'rpc.ams.shyft.to', 'rpc.fra.shyft.to'}
        return SimpleNamespace(ok=False) if name.endswith('swap_sol_usdc') else {'error': {'code': -1}}
    if name.endswith('swap_sol_usdc'): monkeypatch.setattr(wallet, 'sol_balance_read', unread)
    else: monkeypatch.setattr(module, '_rpc', unread)
    if name.endswith('swap_sol_usdc'):
        with pytest.raises(SystemExit): module.main()
    else:
        assert module.main().startswith('REFUSE:')
    assert calls == [READ]


def test_backfill_actual_dry_run_read_uses_shyft_without_research_key(monkeypatch):
    from scripts import memecoin_backfill_derisk as b
    from sniper.persistence import db_memecoin as m, db_core
    from sniper.settings import memecoin as cfg
    from sniper.util import envfiles
    monkeypatch.setattr(cfg, 'MEMECOIN_SOLANA_INFRA_PROVIDER', 'shyft')
    monkeypatch.setenv('MEMECOIN_SOLANA_INFRA_PROVIDER', 'shyft')
    monkeypatch.setenv('MEMECOIN_SOLANA_READ_RPC_URL', READ)
    monkeypatch.delenv('MEMECOIN_RESEARCH_HELIUS_RPC_URL', raising=False)
    monkeypatch.setattr(envfiles, 'load_env_files', lambda *a: None)
    monkeypatch.setattr(db_core, 'get_readonly_connection', lambda *a: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(m, 'list_open_positions', lambda *a, **k: [])
    monkeypatch.setattr(m, 'get_position', lambda *a: dict(id=1, mint='mint', execution_mode='real',
        network_id='solana', is_open=0))
    monkeypatch.setattr(m, 'unbooked_derisk_intents', lambda *a: [dict(sig='sig')])
    calls = []
    def fetch(url, sig):
        calls.append((url, sig)); assert urlsplit(url).hostname == 'rpc.ams.shyft.to'
        raise TimeoutError
    assert b.main(['--position', '1'], fetch=fetch, owner='owner') == 0
    assert calls == [(READ, 'sig')]


@pytest.mark.parametrize('aba,timely', [(False, True), (False, False), (True, True)])
def test_rh_receiver_first_roster_epoch_does_not_ack_old_roster_or_restart_deadline(monkeypatch, aba, timely):
    import grpc
    import queue
    from sniper.services import memecoin_rh_deposit_proof as dep
    now, stop, status, jobs = [0.], threading.Event(), {'coverage_gaps': 0, 'queue_overflows': 0}, queue.Queue()
    roster = dep.Roster(); roster.set({'owner': {'uid': 0}})
    monkeypatch.setattr(dep, 'time', SimpleNamespace(monotonic=lambda: now[0], time=lambda: 100.))
    async def no_wait(_): await asyncio.sleep(0)
    monkeypatch.setattr(dep, 'asyncio', SimpleNamespace(wait_for=asyncio.wait_for,
        sleep=no_wait, CancelledError=asyncio.CancelledError))
    class Stream:
        def __init__(self, requests): self.requests = requests
        def __aiter__(self): return self.rows()
        async def rows(self):
            request = await self.requests.__anext__(); ping = await self.requests.__anext__()
            assert request.commitment == pb.PROCESSED
            now[0] = 1; pong = pb.SubscribeUpdate(); pong.pong.id = ping.ping.id; yield pong
            assert status['connected']
            old_ping = await self.requests.__anext__()
            roster.set({'owner': {'uid': 0}, 'other': {'other': 0}})
            now[0] = 20; head = pb.SubscribeUpdate(); head.slot.slot = 200; yield head
            assert not status['connected']
            if aba:
                roster.set({'owner': {'uid': 0}}); now[0] = 21; yield head
            now[0] = 22; pong.pong.id = old_ping.ping.id; yield pong
            assert not status['connected']
            now[0] = 28; changed = await self.requests.__anext__()
            assert len(next(iter(changed.transactions.values())).account_include) == (1 if aba else 2)
            ping = await self.requests.__anext__()
            now[0] = 29 if timely else 31
            pong.pong.id = ping.ping.id; yield pong
            assert status['connected']
            stop.set(); yield head
        def cancel(self): stop.set()
    class Channel:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        def stream_stream(self, *a, **kw):
            def create(requests, **kw):
                assert kw['metadata'] == (('x-token', 'offline'),)
                return Stream(requests)
            return create
    def channel(target, *a, **kw):
        assert target == 'grpc.fra.shyft.to:443'
        return Channel()
    monkeypatch.setattr(grpc.aio, 'secure_channel', channel)
    asyncio.run(dep._shyft_deposit_socket('https://grpc.fra.shyft.to', 'offline', roster, jobs, status, stop))
    assert status.get('last_error') == (None if timely else 'TimeoutError')
    assert status['acknowledged_generation'] == (1 if timely else None)


def test_owner_account_rejects_truncated_token2022_extensions_and_stale_handoff(monkeypatch):
    import time
    from sniper.services import memecoin_source_sell_mirror as mirror
    mint, owner, account = [base58.b58encode(bytes([x]) * 32).decode() for x in (4, 5, 6)]
    data = bytearray(172); data[:32] = base58.b58decode(mint); data[32:64] = base58.b58decode(owner)
    data[108] = 1; data[165] = 2; data[166:170] = bytes([1, 0, 50, 0])
    assert mirror.OwnerAccountWatch._raw_balance(dict(owner=mirror.TOKEN_2022_PROGRAM, data=bytes(data)), mint, owner) is None
    from sniper.execution.pump_native_exit import ata
    account = str(ata(owner, mint, mirror.TOKEN_PROGRAM))
    events, gaps = [], []
    monkeypatch.setattr(mirror.time, 'monotonic', lambda: 10.)
    w = mirror.OwnerAccountWatch(ws_url=None, rpc_url=READ, sink=lambda *a: events.append(a),
        on_gap=lambda *a: gaps.append(a), shyft_config=('offline', ('https://grpc.ams.shyft.to',)))
    w.set_want({account: dict(mint=mint, owner=owner)})
    w.status.update(connection_generation=1, watch_generation=1, subscription_acknowledged=True)
    data = data[:165]
    for slot, mono, baseline in [(1, 9, True), (2, 7, False)]:
        w._consume_yellowstone(dict(kind='account', pubkey=account, owner=mirror.TOKEN_PROGRAM,
            data=bytes(data), slot=slot, recv_monotonic=mono, connection_generation=1, watch_generation=1,
            baseline_only=baseline))
    assert not events and gaps and w.status['last_gap'] == 'consumer_stale'


def test_shyft_runtime_diagnostics_survive_heartbeat_without_credentials(tmp_path):
    from sniper.services import memecoin_runner as runner, memecoin_real_runner as real
    roles = {'provider': 'shyft', 'funded': [{'hostname': 'rpc.ams.shyft.to', 'url_fingerprint': '1' * 64}],
        'read': {'hostname': 'rpc.ams.shyft.to', 'url_fingerprint': '2' * 64},
        'tpu': {'hostname': 'rpc.fra.shyft.to', 'url_fingerprint': '3' * 64, 'constructed': True}}
    status = real.rpc_pool_status_for_contexts({'solana': {'provider_roles': roles}})
    assert status['provider_roles'] == roles
    observer = dict(state='ready', reason='acknowledged', generation=2, provider='shyft',
        hostname='grpc.fra.shyft.to', commitment='processed', connected=True,
        acknowledged_generation=2, requested_count=1, acknowledged_count=1,
        requested_filter_fingerprint='a' * 64, acknowledged_filter_fingerprint='a' * 64,
        head_slot=123, head_monotonic=0, last_frame_monotonic=0, extra_secret='offline-secret')
    path = tmp_path / 'heartbeat.json'
    runner.write_heartbeat({}, True, path=str(path), rpc_pool_status=status,
        slot_lane_status={'execution_observer': observer, 'signal': {'signal_feeds': {'eu': {
            'damm_filter_requested': True, 'damm_filter_acknowledged': True, 'damm_filter_generation': 'damm:c1:r1'}}}})
    heartbeat = json.loads(path.read_text())
    assert heartbeat['solana_rpc_pool']['provider_roles'] == roles
    assert heartbeat['slot_lane']['execution_observer']['hostname'] == 'grpc.fra.shyft.to'
    assert heartbeat['slot_lane']['execution_observer']['acknowledged_generation'] == 2
    assert heartbeat['slot_lane']['signal_feeds']['eu']['damm_filter_acknowledged'] is True
    assert 'offline-secret' not in path.read_text()


@pytest.mark.parametrize('connect_ip', ['', '84.32.186.182'])
def test_pool_tape_heartbeat_keeps_actual_transport_identity(tmp_path, connect_ip):
    from sniper.services.memecoin_runner import write_heartbeat
    from sniper.services.memecoin_rabbit_tape import tape_transport
    transport = tape_transport('https://grpc.ams.shyft.to', connect_ip)
    path = tmp_path / 'heartbeat.json'
    status = dict(transport, connected=True, by_filter={}, token='offline-secret')
    write_heartbeat({}, True, path=str(path), slot_lane_status={'signal': {'rabbit_tape': status}})
    actual = json.loads(path.read_text())['slot_lane']['rabbit_tape']
    assert {key: actual.get(key) for key in transport} == transport
    assert 'offline-secret' not in path.read_text()


def _funded_startup(monkeypatch):
    from sniper.settings import memecoin as cfg
    for name, value in dict(MEMECOIN_REAL_ENABLED=True, MEMECOIN_REAL_MAX_HOLD_S=86400.,
            MEMECOIN_EXIT_SLIPPAGE_LADDER=[3., 10., 25.], MEMECOIN_REAL_STOP_CONFIRM_SWEEPS=4,
            MEMECOIN_REAL_STOP_PCT=50., MEMECOIN_REAL_TP_PCT=60., MEMECOIN_REAL_DERISK_PCT=0.,
            MEMECOIN_REAL_TRAIL_PCT=0., MEMECOIN_SOLANA_SLOT_LANE_ENABLED='1',
            MEMECOIN_SOLANA_SLOT_LANE_MODE='live', MEMECOIN_SLOT_LANE_RABBIT_TAPE=False).items():
        monkeypatch.setattr(cfg, name, value)
    return cfg


@pytest.mark.parametrize('pin,urls', [
    ('not-an-ip', 'https://grpc.ams.shyft.to,https://grpc.fra.shyft.to'),
    ('84.32.186.182', 'https://grpc.fra.shyft.to,https://grpc.ams.shyft.to'),
])
def test_funded_shyft_startup_rejects_invalid_tape_transport(monkeypatch, pin, urls):
    cfg = _funded_startup(monkeypatch)
    monkeypatch.setenv('MEMECOIN_SOLANA_INFRA_PROVIDER', 'shyft')
    monkeypatch.setenv('MEMECOIN_SHYFT_LIVE_ENABLED', '1')
    monkeypatch.setenv('MEMECOIN_SHYFT_GRPC_TOKEN', 'offline')
    monkeypatch.setenv('MEMECOIN_SHYFT_GRPC_URLS', urls)
    monkeypatch.setenv('MEMECOIN_SHYFT_TAPE_CONNECT_IP', pin)
    with pytest.raises(RuntimeError, match='Shyft live feed invalid: ValueError'):
        cfg.validate_real_exit_policy()


@pytest.mark.parametrize('pin', [None, '', '84.32.186.182'])
def test_funded_shyft_startup_valid_tape_transport_does_not_require_tape(monkeypatch, pin):
    import grpc
    cfg = _funded_startup(monkeypatch)
    monkeypatch.setenv('MEMECOIN_SOLANA_INFRA_PROVIDER', 'shyft')
    monkeypatch.setenv('MEMECOIN_SHYFT_LIVE_ENABLED', '1')
    monkeypatch.setenv('MEMECOIN_SHYFT_GRPC_TOKEN', 'offline')
    monkeypatch.setenv('MEMECOIN_SHYFT_GRPC_URLS', 'https://grpc.ams.shyft.to,https://grpc.fra.shyft.to')
    if pin is None:
        monkeypatch.delenv('MEMECOIN_SHYFT_TAPE_CONNECT_IP', raising=False)
    else:
        monkeypatch.setenv('MEMECOIN_SHYFT_TAPE_CONNECT_IP', pin)
    monkeypatch.setattr(grpc.aio, 'secure_channel', lambda *_a, **_k: pytest.fail('startup opened channel'))
    assert cfg.validate_real_exit_policy() is None


def test_funded_legacy_startup_does_not_require_tape_transport(monkeypatch):
    from sniper.services import memecoin_rabbit_tape as tape
    cfg = _funded_startup(monkeypatch)
    monkeypatch.setenv('MEMECOIN_SOLANA_INFRA_PROVIDER', 'legacy')
    monkeypatch.setenv('MEMECOIN_SHYFT_LIVE_ENABLED', '0')
    monkeypatch.setenv('MEMECOIN_SHYFT_TAPE_CONNECT_IP', 'not-an-ip')
    monkeypatch.setattr(tape, 'tape_transport', lambda *_: pytest.fail('legacy required Tape'), raising=False)
    assert cfg.validate_real_exit_policy() is None


@pytest.mark.parametrize('aba,timely', [(False, True), (False, False), (True, True)])
def test_processed_owner_and_program_filter_ack_share_actual_roster_epoch(monkeypatch, aba, timely):
    import grpc
    from sniper.services import memecoin_shyft_signal as signal
    now, stop, status = [0.], threading.Event(), {}
    owners = {'owner': ('uid', 'name')}
    monkeypatch.setattr(signal, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    async def no_wait(_): await asyncio.sleep(0)
    monkeypatch.setattr(signal, 'asyncio', SimpleNamespace(wait_for=asyncio.wait_for,
        sleep=no_wait, CancelledError=asyncio.CancelledError))
    class Stream:
        def __init__(self, requests): self.requests = requests
        def __aiter__(self): return self.rows()
        async def rows(self):
            await self.requests.__anext__(); ping = await self.requests.__anext__()
            now[0] = 1; pong = pb.SubscribeUpdate(); pong.pong.id = ping.ping.id; yield pong
            assert status['connected'] and status['damm_filter_acknowledged']
            old_ping = await self.requests.__anext__()
            owners['other'] = ('other', 'other')
            now[0] = 20; head = pb.SubscribeUpdate(); head.slot.slot = 200; yield head
            assert not status['connected'] and not status['damm_filter_acknowledged']
            if aba:
                del owners['other']; now[0] = 21; yield head
            now[0] = 22; pong.pong.id = old_ping.ping.id; yield pong
            assert not status['connected'] and not status['migration_filter_acknowledged']
            now[0] = 28; changed = await self.requests.__anext__()
            assert len(changed.transactions['owners'].account_include) == (1 if aba else 2)
            ping = await self.requests.__anext__()
            now[0] = 29 if timely else 31; pong.pong.id = ping.ping.id; yield pong
            assert status['connected'] and status['damm_filter_acknowledged'] and status['migration_filter_acknowledged']
            stop.set(); yield head
        def cancel(self): stop.set()
    class Channel:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        def stream_stream(self, *a, **kw): return lambda requests, **kw: Stream(requests)
    monkeypatch.setattr(grpc.aio, 'secure_channel', lambda *a, **kw: Channel())
    asyncio.run(signal.run_feed('https://grpc.ams.shyft.to', 'offline', lambda: dict(owners),
        lambda *a: pytest.fail('test admitted buy'), status, stop,
        on_migration=lambda *a: None, on_damm_creation=lambda *a: None))
    assert status['last_error'] == (None if timely else 'subscription_ack_timeout')


def test_processed_owner_account_startup_uses_one_processed_core_and_consumer_thread(monkeypatch):
    import time
    from sniper.services import memecoin_source_sell_mirror as mirror, memecoin_shyft_accounts as accounts
    mint, owner, address = [base58.b58encode(bytes([x]) * 32).decode() for x in (4, 5, 6)]
    from sniper.execution.pump_native_exit import ata
    address = str(ata(owner, mint, mirror.TOKEN_PROGRAM))
    stop, received, calls, acks = threading.Event(), [], [], []
    raw = bytearray(165); raw[:32] = base58.b58decode(mint); raw[32:64] = base58.b58decode(owner); raw[108] = 1
    async def receiver(url, token, watch, offer, status, owned_stop, *, commitment):
        assert url == 'https://grpc.ams.shyft.to' and token == 'offline' and commitment == pb.PROCESSED
        calls.append(threading.current_thread().name)
        status.update(connection_generation=1, watch_generation=1, subscription_acknowledged=True,
            connected=True, requested_accounts=tuple(sorted(watch())), requested_account_count=1)
        for slot, amount in [(10, 100), (11, 90)]:
            raw[64:72] = amount.to_bytes(8, 'little')
            assert offer(dict(kind='account', pubkey=address, owner=mirror.TOKEN_PROGRAM, data=bytes(raw),
                slot=slot, write_version=slot, connection_generation=1, watch_generation=1,
                recv_monotonic=time.monotonic(), baseline_only=slot == 10))
        while not owned_stop.is_set(): await asyncio.sleep(.005)
        status['connected'] = False
    monkeypatch.setattr(accounts, 'run_accounts', receiver)
    def sink(*args): received.append((args, threading.current_thread().name)); stop.set()
    watch = mirror.OwnerAccountWatch(ws_url=None, rpc_url=READ, sink=sink, stop=stop,
        on_ack=lambda *a: acks.append(a), shyft_config=('offline', ('https://grpc.ams.shyft.to',)))
    watch.set_want({address: dict(mint=mint, owner=owner)})
    producer = watch.start(); producer.join(1)
    assert not producer.is_alive() and calls == ['source-owner-yellowstone']
    assert received == [((address, 11, 90, received[0][0][3]), 'source-owner-account-consumer')]
    assert acks and watch.status['requested_filter_fingerprint'] == watch.status['acknowledged_filter_fingerprint']
    assert watch.status['commitment'] == 'processed'


def test_rh_existing_supervisor_heartbeat_publishes_actual_deposit_status(tmp_path, monkeypatch):
    from scripts import memecoin_rh_feed_shadow_loop as loop
    from sniper.services import memecoin_rh_feed_shadow as feed, memecoin_runner as runner
    from sniper.settings import memecoin as cfg
    status = dict(provider='shyft', hostname='grpc.fra.shyft.to', commitment='processed', connected=True,
        generation=1, watch_generation=1, requested_watch_generation=1, acknowledged_watch_generation=1,
        acknowledged_generation=1, requested_count=2, acknowledged_count=2,
        requested_filter_fingerprint='a' * 64, acknowledged_filter_fingerprint='a' * 64,
        head_slot=200, head_monotonic=100., last_frame_monotonic=100., extra_secret='offline-secret')
    shadow = SimpleNamespace(threads=[], counters={}, stats={}, deposit_stream_status=status)
    monkeypatch.setattr(cfg, 'memecoin_rh_fast_lane_mode', lambda: 'live')
    monkeypatch.setattr(runner, 'acquire_run_lock', lambda *a: object())
    monkeypatch.setattr(feed, 'start_shadow', lambda **kw: shadow)
    monkeypatch.setattr(loop, 'state_path', lambda *a: tmp_path / a[-1])
    monkeypatch.setattr(loop.time, 'monotonic', lambda: 100.2)
    class Done(Exception): pass
    def sleep(_): raise Done
    with pytest.raises(Done): loop.main(sleep_fn=sleep)
    result = json.loads((tmp_path / 'memecoin_rh_feed_shadow.heartbeat').read_text())
    assert result['pid'] > 0 and result['deposit']['stream']['connected']
    assert result['deposit']['stream']['head_age_s'] == pytest.approx(.2)
    assert result['deposit']['stream']['head_monotonic'] == 100.
    assert 'offline-secret' not in json.dumps(result)


@pytest.mark.parametrize('elapsed', [50, 150])
def test_shyft_coin_age_lower_bound_cannot_prove_young_unknown_pair(monkeypatch, elapsed):
    from sniper.services import memecoin_coin_age as age, memecoin_risk as risk
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, 'MEMECOIN_SOLANA_INFRA_PROVIDER', 'shyft')
    monkeypatch.setattr(cfg, 'MEMECOIN_SOLANA_READ_RPC_URL', READ)
    monkeypatch.setenv('MEMECOIN_REAL_MIN_COIN_AGE_S', '120')
    monkeypatch.delenv('MEMECOIN_REAL_MIN_COIN_AGE_S_solana', raising=False)
    monkeypatch.setenv('MEMECOIN_COIN_AGE_UNKNOWN_PAIR_CHECK', '1')
    monkeypatch.setattr(age, '_FIRST_TS', {})
    monkeypatch.setattr(age, '_IN_FLIGHT', {})
    monkeypatch.setattr(age.time, 'time', lambda: 1000.)
    class Response:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return json.dumps({'result': [{'err': None, 'blockTime': 1000 - elapsed}]}).encode()
    monkeypatch.setattr(age.urllib.request, 'urlopen', lambda *a, **k: Response())
    assert age.warm('solana', 'mint', wait_s=1) == elapsed
    assert age.warm('solana', 'mint', wait_s=0) == elapsed  # cached quality is retained
    reason, fields = risk.coin_age_presign_check('solana', 'mint', None)
    assert reason is None
    assert fields['resolved'] is (elapsed >= 120)
    assert fields['mint_age_quality'] == 'lower_bound'


def test_custody_observer_startup_binds_actual_fra_yellowstone_without_legacy_key(monkeypatch):
    from scripts import memecoin_runner_loop as runner
    from sniper.services import memecoin_execution_observer as observer
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, 'MEMECOIN_EXECUTION_OBSERVER_ENABLED', True)
    for key, value in {**ENV, 'MEMECOIN_SHYFT_LIVE_ENABLED': '1', 'MEMECOIN_SHYFT_GRPC_TOKEN': 'offline',
            'MEMECOIN_SHYFT_GRPC_URLS': 'https://grpc.ams.shyft.to,https://grpc.fra.shyft.to'}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv('MEMECOIN_RESEARCH_HELIUS_RPC_URL', raising=False)
    calls = []
    monkeypatch.setattr(observer, 'start_execution_observer', lambda **k: pytest.fail('legacy observer constructed'))
    monkeypatch.setattr(observer, 'start_yellowstone_execution_observer', lambda **k: calls.append(k))
    status, stop = {}, threading.Event()
    journal = runner.start_execution_measurement({'solana': {'pubkey': 'custody'}}, status=status, stop=stop)
    assert len(calls) == 1 and calls[0]['url'] == 'https://grpc.fra.shyft.to'
    assert calls[0]['journal'] is journal and calls[0]['stop'] is stop and status['state'] == 'starting'


def test_repair_actual_dry_run_chain_read_uses_shyft_without_research_key(monkeypatch):
    from scripts import repair_unbooked_derisk as repair
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, 'MEMECOIN_SOLANA_INFRA_PROVIDER', 'shyft')
    monkeypatch.setenv('MEMECOIN_SOLANA_INFRA_PROVIDER', 'shyft')
    monkeypatch.setenv('MEMECOIN_SOLANA_READ_RPC_URL', READ)
    monkeypatch.delenv('MEMECOIN_RESEARCH_HELIUS_RPC_URL', raising=False)
    class Connection:
        def execute(self, sql, *a):
            if sql.startswith('SELECT * FROM memecoin_positions'):
                return SimpleNamespace(fetchone=lambda: dict(id=1, ticker='offline', network_id='solana',
                    execution_mode='real', is_open=0, derisked_at='date', realized_pnl=0, entry_notional=10, mint='mint'))
            if 'COUNT(*)' in sql: return SimpleNamespace(fetchone=lambda: [0])
            if 'memecoin_exit_intents' in sql:
                return [dict(claim_id='claim', status='confirmed', intent_json=json.dumps(
                    dict(tx_identity='sig', fraction=.1, balance_snapshot=10)))]
            assert sql.startswith('SELECT 1 FROM memecoin_fills')
            return SimpleNamespace(fetchone=lambda: None)
    monkeypatch.setattr(repair.sqlite3, 'connect', lambda *a, **k: Connection())
    calls = []
    def read(sig, mint, url, **kw):
        assert urlsplit(url).hostname == 'rpc.ams.shyft.to'
        calls.append((sig, url)); return 1, 2, 'owner'
    monkeypatch.setattr(repair, 'chain_fill', read)
    assert repair.main(1, 'sig') == 0 and calls == [('sig', READ)]


def test_real_context_freezes_constructed_read_role_for_hot_closure(tmp_path, monkeypatch):
    from sniper.services import memecoin_real_runner as runner
    from sniper.settings import memecoin as cfg
    from sniper.execution import submission_backends, solana_wallet, solana_real_seams, evm_wallet
    from solders.keypair import Keypair
    path = tmp_path / 'offline-key'; path.write_text('offline')
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_ENABLED', True)
    monkeypatch.setattr(cfg, 'MEMECOIN_SOLANA_KEYPAIR', str(path))
    monkeypatch.setattr(cfg, 'solana_funded_rpc_urls', lambda: [READ])
    for key, value in {**ENV, 'MEMECOIN_SOLANA_SUBMIT_MODE': 'auto_free'}.items(): monkeypatch.setenv(key, value)
    monkeypatch.delenv('MEMECOIN_SOLANA_RPC_POOL_URLS', raising=False)
    monkeypatch.setattr(solana_wallet, 'load_keypair', lambda *a: Keypair())
    monkeypatch.setattr(submission_backends, 'build_solana_backend', lambda **k: (None, None))
    captured = []
    def executor(**kw):
        captured.append(kw); return dict(execute_fn=lambda *a: None, exit_fn=lambda *a: None,
            confirm_fn=lambda *a: None, confirm_once_fn=lambda *a: None)
    monkeypatch.setattr(solana_real_seams, 'build_real_solana_executor', executor)
    context = runner.build_real_context(http=lambda *a, **k: pytest.fail('unexpected context IO'), own_sidecar=False)
    assert context['read_rpc_url'] == READ and captured[0]['read_rpc_url'] == READ
    monkeypatch.setattr(cfg, 'solana_read_rpc', lambda: pytest.fail('hot closure selected env RPC'))
    monkeypatch.setenv('MEMECOIN_SOLANA_READ_RPC_URL', 'https://legacy.invalid')
    calls = []
    def read(url, method, params, **kw):
        assert urlsplit(url).hostname == 'rpc.ams.shyft.to'
        calls.append(url); return {'result': 125}
    monkeypatch.setattr(evm_wallet, '_rpc', read)
    assert context['block_height_fn']() == 125 and calls == [READ]
    assert context['provider_roles']['read'] == runner._provider_role_identity(READ)
    assert context['provider_roles']['tpu'] is None


@pytest.mark.parametrize('lane_name', ['j7_wallets', 'j7_pump'])
def test_shyft_j7_backup_disabled_keeps_external_hint_only_policy(setup, monkeypatch, lane_name):
    from sniper.services import memecoin_real_runner as runner
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, 'MEMECOIN_SOLANA_INFRA_PROVIDER', 'shyft')
    monkeypatch.setattr(cfg, 'MEMECOIN_J7_SOLANA_VERIFIED_BACKUP', False)
    conn, ev, _ = setup
    ev.delivery_lane = lane_name
    _native_attempt(conn)
    hook = runner.make_real_hook(None, {'solana': {}}, now_fn=lambda: ev.created_at,
                                trade_size=25, max_age_s=60)
    result = hook(conn, ev)
    assert result['status'] == 'hint_only' and result['reason'] == 'yellowstone_source_required'
