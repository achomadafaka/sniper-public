"""Offline cold-start recovery keeps exits separate from Solana signal readiness."""
import threading
import pytest
from types import SimpleNamespace


def test_funded_sol_buy_is_held_before_source_work_and_ungates_after_recovery(monkeypatch):
    from sniper.services import memecoin_real_runner as rr
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(rr, '_start_trace_best_effort', lambda *a, **kw: None)
    decisions = []
    monkeypatch.setattr(rr, '_record_real_decision', lambda conn, ev, result, decision_at=None: decisions.append(result))
    ready, calls = threading.Event(), []
    contexts = {'solana': {'entry_ready_fn': ready.is_set}, '56': {}}
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_CLUSTER_GATE_ENABLED', False)
    monkeypatch.setattr(cfg, 'MEMECOIN_J7_SOLANA_VERIFIED_BACKUP', False)
    monkeypatch.setattr(rr, 'resolve_entry_identity', lambda *a, **kw:
        (calls.append('resolve') or rr.ResolvedEntryIdentity('solana', 'unresolved', 'fixture')))
    hook = rr.make_real_hook(None, contexts, now_fn=lambda: '2026-10-14T12:00:00Z',
        trade_size=10, max_age_s=120, blind_mcap_fn=lambda *a: None)
    event = SimpleNamespace(side='buy', network_id='solana', signal_source='onchain',
        fomo_event_id='fixture', source_user_id='u', mint='mint', their_market_cap=1)
    result = hook(None, event)
    assert result == {'status': 'deferred', 'chain': 'solana', 'reason': 'shyft_handover_pending'}
    assert calls == []  # no source proof, signing, reservation or provider read while held
    assert decisions == [result]
    ready.set()
    hook(None, event)
    assert calls == ['resolve']


def test_funded_sells_and_evm_buys_do_not_wait_on_solana_startup(monkeypatch):
    from sniper.services import memecoin_real_runner as rr
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(rr, '_start_trace_best_effort', lambda *a, **kw: None)
    monkeypatch.setattr(rr, '_record_real_decision', lambda *a, **kw: None)
    calls = []
    contexts = {'solana': {'entry_ready_fn': lambda: False}, '56': {}}
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_CLUSTER_GATE_ENABLED', False)
    monkeypatch.setattr(cfg, 'MEMECOIN_J7_SOLANA_VERIFIED_BACKUP', False)
    monkeypatch.setattr(rr, 'real_mirror_on_sell', lambda *a, **kw: calls.append('exit') or {'status': 'sold'})
    monkeypatch.setattr(rr, 'resolve_entry_identity', lambda *a, **kw:
        calls.append('evm') or rr.ResolvedEntryIdentity('56', 'unresolved', 'fixture'))
    hook = rr.make_real_hook(None, contexts, now_fn=lambda: '2026-10-14T12:00:00Z',
        trade_size=10, max_age_s=120, blind_mcap_fn=lambda *a: None)
    sale = SimpleNamespace(side='sell', network_id='solana', signal_source='onchain')
    buy = SimpleNamespace(side='buy', network_id='56', signal_source='onchain',
        fomo_event_id='fixture', source_user_id='u', mint='0x1', their_market_cap=1)
    assert hook(None, sale)['status'] == 'sold'
    hook(None, buy)
    assert calls == ['exit', 'evm']


def test_auxiliary_streams_stay_staged_until_full_local_watcher_proof(monkeypatch):
    import importlib.util
    assert importlib.util.find_spec('sniper.services.memecoin_shyft_startup') is not None
    from sniper.services.memecoin_shyft_startup import ShyftStartupGate
    from scripts import memecoin_runtime_verify as verify
    from sniper.services import memecoin_solana_provider as provider
    checked, release, started, calls = threading.Event(), threading.Event(), threading.Event(), []
    monkeypatch.setattr(provider, 'infra_provider', lambda *a: 'shyft')
    def check(code_root, state_root):
        checked.set()
        return [] if release.is_set() else ['shyft_watcher_not_sole', 'shyft_watcher_head_stale']
    monkeypatch.setattr(verify, 'verify_shyft_watcher', check)
    gate = ShyftStartupGate()
    worker = gate.defer(lambda: (calls.append('auxiliary'), started.set()), name='offline-staged-stream')
    try:
        assert checked.wait(2)
        assert not gate.ready.is_set() and calls == []
        release.set()
        assert started.wait(2)
        assert gate.ready.is_set() and calls == ['auxiliary']
        assert gate.status['state'] == 'ready'
        worker.join(2)
        assert not worker.is_alive()
    finally:
        gate.stop.set()
        gate.ready.set()
        worker.join(2)


def test_relay_deposit_stream_waits_without_stopping_its_existing_proof_workers(monkeypatch):
    import collections
    from sniper.services import memecoin_rh_deposit_proof as dep
    from sniper.services import memecoin_solana_provider as provider
    from scripts import memecoin_runtime_verify as verify
    checked, recovered, called, stop = (threading.Event() for _ in range(4))
    monkeypatch.setattr(provider, 'infra_provider', lambda *a: 'shyft')
    def check(*args, **kwargs):
        checked.set()
        return [] if recovered.is_set() else ['shyft_watcher_not_sole']
    monkeypatch.setattr(verify, 'verify_shyft_watcher', check)
    async def socket(*args):
        called.set()
    monkeypatch.setattr(dep, '_shyft_deposit_socket', socket)
    counters = collections.Counter()
    workers = dep.start_shyft_deposit_stream(('test-token', ('https://grpc.fra.shyft.to',)),
        dep.Roster(), dep.DepositProofs(), counters, lambda *a: None, stop=stop)
    try:
        assert checked.wait(2)
        assert not called.is_set()
        assert workers[0].is_alive()  # bounded proof consumer remains available
        recovered.set()
        assert called.wait(2)
    finally:
        stop.set()
        for worker in workers:
            worker.join(2)


def test_deferred_runner_startup_preserves_account_status_and_independent_owners(monkeypatch):
    """Execute the production startup callback with inert stream constructors."""
    import ast
    import copy
    import time
    from pathlib import Path
    from sniper.services import memecoin_slot_lane as slot
    from sniper.services import memecoin_slot_skips as skips
    from sniper.services import memecoin_source_sell_mirror as mirror
    from sniper.services import memecoin_solana_provider as provider
    from sniper.services import memecoin_shyft_signal as signal
    from sniper.services.memecoin_shyft_startup import ShyftStartupGate
    from scripts import memecoin_runtime_verify as verify
    pending, calls, completed = threading.Event(), [], threading.Event()
    monkeypatch.setattr(provider, 'infra_provider', lambda *a: 'shyft')
    monkeypatch.setattr(verify, 'verify_shyft_watcher',
        lambda *a, **kw: [] if pending.is_set() else ['shyft_watcher_not_sole'])
    monkeypatch.setattr(signal, 'live_config', lambda: ('test-token', ('https://grpc.ams.shyft.to',)))
    class AccountWatch:
        def __init__(self, **kwargs): pass
        def start(self): calls.append('source_accounts')
    monkeypatch.setattr(mirror, 'OwnerAccountWatch', AccountWatch)
    monkeypatch.setattr(skips, 'run_drain', lambda *a, **kw: None)
    monkeypatch.setattr(slot, 'start_from_context', lambda *a, **kw: calls.append('slot') or {'mode': 'live'})
    gate = ShyftStartupGate()
    mark_status = {'connected': True, 'head_slot': 100}
    namespace = {
        'threading': threading, 'time': time, '_source_mirror': SimpleNamespace(**{
            name: lambda *a, **kw: None for name in ('offer_account', 'offer_ack', 'offer_account_gap',
                                                   'offer_account_baseline')}),
        '_SSM_ACCOUNTS': True, '_ssm_url': 'https://rpc.ams.shyft.to/?api_key=test',
        '_slot_lane_enabled': lambda: True, 'real_hook': lambda *a, **kw: None,
        'contexts': {'solana': {'exit_fn': lambda: calls.append('exit')},
                     '56': {'reconcile_fn': lambda: calls.append('reconcile')}},
        '_onchain_wake': threading.Event(), '_p': lambda *a: None, '_BUFFER_CAP': 0,
        '_rent_keypair': None, '_account_price_stream': SimpleNamespace(status=mark_status),
        '_slot_lane_status': {'mode': 'staged', 'account_stream': mark_status},
        '_shyft_startup': gate,
        'start_execution_measurement': lambda *a, **kw: calls.append('observer'),
    }
    tree = ast.parse((Path(__file__).resolve().parents[1] / 'scripts/memecoin_runner_loop.py').read_text())
    main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'main')
    callback = copy.deepcopy(next(node for node in main.body
        if isinstance(node, ast.FunctionDef) and node.name == '_start_solana_streams'))
    callback.body[0] = ast.Global(names=['_slot_lane_status'])
    exec(compile(ast.fix_missing_locations(ast.Module(body=[callback], type_ignores=[])),
                 '<production-deferred-startup>', 'exec'), namespace)
    def start():
        namespace['_start_solana_streams']()
        completed.set()
    worker = gate.defer(start, name='offline-runner-startup')
    try:
        namespace['contexts']['solana']['exit_fn']()
        namespace['contexts']['56']['reconcile_fn']()
        assert calls == ['exit', 'reconcile']
        pending.set()
        assert completed.wait(2)
        worker.join(2)
        assert calls == ['exit', 'reconcile', 'source_accounts', 'slot', 'observer']
        status = namespace['_slot_lane_status']
        assert status['account_stream'] is mark_status
        assert status['startup_gate']['state'] == 'ready'
        # With slot/observer disabled, a marks-only status must still survive publication.
        namespace['_slot_lane_enabled'] = lambda: False
        namespace['_start_solana_streams']()
        assert namespace['_slot_lane_status']['account_stream'] is mark_status
    finally:
        gate.stop.set()
        gate.ready.set()
        worker.join(2)


def test_health_wrapper_preserves_nozomi_outcome_and_after_accept_contract():
    from sniper.execution.submission_health import HealthGatedProvider, HealthRegistry
    provider = SimpleNamespace(provider_id='solana:nozomi', send_after_accept=True,
        outcome=lambda signature: {'signature': signature})
    wrapped = HealthGatedProvider(provider, HealthRegistry([provider.provider_id]))
    assert getattr(wrapped, 'send_after_accept', False) is True
    assert wrapped.outcome('fixture') == {'signature': 'fixture'}


def test_shyft_unavailable_custody_constructs_only_gated_delivery_and_recovers_off_path(monkeypatch):
    import inspect
    import json
    from solders.keypair import Keypair
    from sniper.execution import submission_backends as backend
    from sniper.execution import solana_sender_ab as sender
    from sniper.execution.submission_transport import AttemptState, SubmissionAttempt, build_request
    assert 'allow_unavailable_custody' in inspect.signature(backend.build_solana_backend).parameters
    monkeypatch.setattr(backend, '_CUSTODY_PROBE_RETRY_SLEEP_S', 0)
    checks, calls, phase = [], [], {'ready': False, 'wrong': False}
    monkeypatch.setattr(backend, '_start_health_checks',
        lambda endpoints, health, probe_one, **kw: checks.append((tuple(endpoints), probe_one)))
    class Provider:
        provider_id = 'solana:nozomi'
        send_after_accept = True
        def submit(self, request):
            calls.append('nozomi')
            return SubmissionAttempt(self.provider_id, AttemptState.ACCEPTED, request.tx_identity, 0, None, None)
        def outcome(self, sig): return {'signature': sig}
    class Sidecar:
        def provider(self, **kwargs):
            provider = Provider(); provider.provider_id = 'solana:tpu'
            return provider
        def close(self): pass
    monkeypatch.setattr(sender, 'nozomi_provider', lambda *a, **kw: Provider())
    def http(method, url, **kwargs):
        rpc = json.loads(kwargs.get('body') or '{}')['method']
        if rpc == 'getHealth':
            return (200, {'result': 'ok'}, None) if phase['ready'] else (503, {}, None)
        if rpc == 'getGenesisHash':
            from sniper.execution.free_endpoint_registry import SOLANA_MAINNET_GENESIS
            return 200, {'result': 'wrong-chain' if phase['wrong'] else SOLANA_MAINNET_GENESIS}, None
        calls.append(rpc)
        return 200, {'result': 'fixture'}, None
    url = 'https://rpc.fra.shyft.to/?api_key=test'
    env = {'MEMECOIN_SOLANA_INFRA_PROVIDER': 'shyft', 'MEMECOIN_SOLANA_RPC_URL': url}
    backend.close_all()
    transport, _ = backend.build_solana_backend(keypair=Keypair(), custody_rpc_url=url,
        env=env, http=http, tpu_sidecar=Sidecar(), allow_unavailable_custody=True)
    try:
        request = build_request(chain_id='solana', action='entry', tx_identity='fixture', signed_payload='c2lnbmVk')
        assert not transport.custody_ready_fn()
        assert transport.provider('solana:nozomi').outcome('fixture') == {'signature': 'fixture'}
        assert transport.provider('solana:nozomi').send_after_accept is True
        assert all(attempt.state is AttemptState.NOT_SENT for attempt in transport.submit(request).attempts)
        assert transport.submit_one('solana:tpu', request).state is AttemptState.NOT_SENT
        assert calls == []
        phase['ready'] = True
        endpoints, probe = checks[0]
        probe(endpoints[0])
        assert transport.custody_ready_fn()
        assert transport.submit_one('solana:tpu', request).state is AttemptState.ACCEPTED
        assert calls == ['nozomi']
        # An ordinary later outage cannot veto a proved identity's independent TPU/Nozomi path.
        phase['ready'] = False
        for _ in range(5):
            probe(endpoints[0])
        assert transport.custody_ready_fn()
        assert transport.submit_one('solana:tpu', request).state is AttemptState.ACCEPTED
        phase.update(ready=True, wrong=True)
        probe(endpoints[0])
        assert not transport.custody_ready_fn()
        assert transport.submit_one('solana:tpu', request).state is AttemptState.NOT_SENT
        phase['wrong'] = False
        probe(endpoints[0])
        assert not transport.custody_ready_fn()  # mismatch cannot auto-revive
        assert backend.submission_health_snapshots()['solana:tpu']['state'] == 'excluded'
    finally:
        backend.close_all()


def test_shyft_http_outage_keeps_evm_owner_and_sol_recovery_reads_unavailable(monkeypatch, tmp_path):
    import json
    from solders.keypair import Keypair
    from sniper.services import memecoin_real_runner as rr
    from sniper.services import memecoin_solana_provider as provider
    from sniper.services import memecoin_entry_bids as bids
    from sniper.execution import submission_backends as backend
    from sniper.execution import solana_real_seams as seams
    from sniper.settings import memecoin as cfg
    from sniper.settings import memecoin_submission as submission
    checked, reads, requested, phase = [], [], [], {'ready': False}
    url = 'https://rpc.fra.shyft.to/?api_key=test'
    key_file = tmp_path / 'offline-key.json'
    key_file.write_text(json.dumps(list(bytes(Keypair()))))
    monkeypatch.setenv('SNIPER_STATE_ROOT', str(tmp_path))
    monkeypatch.setenv('MEMECOIN_SOLANA_SUBMIT_MODE', 'auto_free')
    monkeypatch.setenv('MEMECOIN_SLOT_LANE_NOZOMI_AB', '0')
    monkeypatch.setenv('MEMECOIN_SLOT_LANE_SENDER_AB', '0')
    monkeypatch.setenv('MEMECOIN_SLOT_LANE_SENDER_ALL', 'off')
    monkeypatch.setenv('MEMECOIN_NOZOMI_FEE_SPLIT_AB', '0')
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_ENABLED', True)
    monkeypatch.setattr(cfg, 'MEMECOIN_SOLANA_KEYPAIR', str(key_file))
    monkeypatch.setattr(cfg, 'solana_funded_rpc_urls', lambda: (url,))
    monkeypatch.setattr(cfg, 'validate_real_exit_policy', lambda: None)
    monkeypatch.setattr(submission, 'validate_submission_config', lambda **kw: None)
    monkeypatch.setattr(provider, 'infra_provider', lambda *a: 'shyft')
    monkeypatch.setattr(provider, 'provider_urls', lambda *a, **kw: (url,))
    monkeypatch.setattr(bids, 'start_entry_bids', lambda *a: None)
    monkeypatch.setattr(backend, '_CUSTODY_PROBE_RETRY_SLEEP_S', 0)
    monkeypatch.setattr(backend, '_start_health_checks',
        lambda endpoints, health, probe_one, **kw: checked.append((tuple(endpoints), probe_one)))
    monkeypatch.setattr(seams, 'build_real_solana_executor', lambda **kw: {
        name: lambda *a, **kw: None for name in ('execute_fn', 'exit_fn', 'confirm_fn', 'confirm_once_fn')})
    exits = []
    monkeypatch.setattr(rr, 'build_real_evm_contexts', lambda **kw: {
        '56': {'exit_fn': lambda: exits.append('exit'), 'reconcile_fn': lambda: exits.append('reconcile')}})
    def http(method, url, **kwargs):
        rpc = json.loads(kwargs.get('body') or '{}')['method']
        if rpc == 'getHealth':
            return (200, {'result': 'ok'}, None) if phase['ready'] else (503, {}, None)
        if rpc == 'getGenesisHash':
            from sniper.execution.free_endpoint_registry import SOLANA_MAINNET_GENESIS
            return 200, {'result': SOLANA_MAINNET_GENESIS}, None
        reads.append(rpc)
        requested.append(url)
        return 200, {'result': {'value': 1000000000}}, None
    backend.close_all()
    try:
        with pytest.raises(submission.SubmissionConfigError):
            rr.build_all_contexts(http=http, own_sidecar=False)  # direct/deploy callers stay strict
        contexts = rr.build_all_contexts(http=http, own_sidecar=False, allow_shyft_unavailable=True)
        contexts['56']['exit_fn']()
        contexts['56']['reconcile_fn']()
        assert exits == ['exit', 'reconcile']
        sol = contexts['solana']
        assert not sol['custody_ready_fn']()
        assert sol['sol_fn']().ok is False
        assert sol['usdc_fn']() is None
        sol['reoffer_send_fn'](b'c2lnbmVk')
        assert reads == []  # no read proof or stored-byte rebroadcast against unknown identity
        phase['ready'] = True
        endpoints, probe = checked[0]
        probe(endpoints[0])
        assert sol['custody_ready_fn']()
        assert sol['sol_fn']().ok is True
        assert reads == ['getBalance']
        selected = 'https://rpc.ams.shyft.to/?api_key=offline-selected'
        sol['rpc_call_fn'](selected, 'getBalance', ['offline-owner'])
        assert requested[-1] == selected
        sol['withdraw_call_fn'](selected, 'getBalance', ['offline-owner'])
        assert requested[-1] == url
    finally:
        backend.close_all()


def test_holdings_worker_rechecks_custody_at_send_and_confirmation_after_quote(monkeypatch, tmp_path):
    from test_memecoin_other_holdings import _conn, _snapshot, _acct, OWNER, T0, oh, m
    from sniper.execution import solana_real_seams as seams
    ready, calls = threading.Event(), []
    ready.set()
    conn = _conn()
    _snapshot(conn, {oh.TOKEN_PROGRAMS[0]: [_acct('LEFT', 2_000_000)]}, {})
    m.enqueue_holding_sell(conn, 'solana', 'LEFT', now_iso=T0)
    def quote(*a, **kw):
        ready.clear()  # identity is revoked after the earlier maintenance admission check
        return {'inAmount': '2000000', 'outAmount': '20000000', 'otherAmountThreshold': '19000000'}
    monkeypatch.setattr(seams, 'jupiter_swap_tx', lambda *a, **kw: {'swapTransaction': 'TX'})
    monkeypatch.setattr(seams, 'sign_swap_tx', lambda *a, **kw: ('SIGNED', 'SIG'))
    monkeypatch.setattr(seams, 'rpc_send', lambda *a, **kw: calls.append('raw_send'))
    monkeypatch.setattr(seams, 'rpc_confirm', lambda *a, **kw: calls.append('raw_confirm') or 'pending')
    monkeypatch.setattr(oh, 'snapshot_once', lambda *a, **kw: {'state': 'ok'})
    monkeypatch.setitem(oh._WORKER, 'thread', None)
    monkeypatch.setitem(oh._WORKER, 'last_sell_try', 0)
    ctx = {'exit_quote_fn': quote,
           'rpc_send_fn': lambda *a, **kw: seams.rpc_send(*a, **kw) if ready.is_set() else None,
           'rpc_confirm_fn': lambda *a, **kw: seams.rpc_confirm(*a, **kw) if ready.is_set() else 'unavailable'}
    assert oh.start_pass(ctx, keypair=object(), pubkey=OWNER, rpc_url='https://offline',
        rpc_candidates=['https://offline'], snapshot_due=False, sell_due=True, resolve_due=False,
        min_sell_usd=.5, now_fn=lambda: T0, log_fn=lambda *a: None, connect=lambda: conn)
    worker = oh._WORKER['thread']
    worker.join(2)
    assert not worker.is_alive()
    assert calls == []


def test_wrong_chain_excludes_custody_and_tpu_atomically(monkeypatch):
    import json
    from solders.keypair import Keypair
    from test_memecoin_submission_backends import FakeSidecar
    from sniper.execution import submission_backends as backend
    from sniper.execution.submission_health import HealthRegistry
    from sniper.execution.submission_transport import AttemptState, build_request
    from sniper.execution.free_endpoint_registry import SOLANA_MAINNET_GENESIS
    phase, checks, result = {'wrong': False}, [], []
    reached, release, finished = (threading.Event() for _ in range(3))
    monkeypatch.setattr(backend, '_start_health_checks',
        lambda endpoints, health, probe_one, **kw: checks.append((tuple(endpoints), probe_one)))
    def http(method, url, **kwargs):
        rpc = json.loads(kwargs['body'])['method']
        value = 'ok' if rpc == 'getHealth' else ('wrong' if phase['wrong'] else SOLANA_MAINNET_GENESIS)
        return 200, {'result': value}, None
    url = 'https://rpc.fra.shyft.to/?api_key=test'
    backend.close_all()
    transport, _ = backend.build_solana_backend(keypair=Keypair(), custody_rpc_url=url,
        env={'MEMECOIN_SOLANA_INFRA_PROVIDER': 'shyft', 'MEMECOIN_SOLANA_RPC_URL': url},
        http=http, tpu_sidecar=FakeSidecar(), allow_unavailable_custody=True)
    original = HealthRegistry.record_wrong_chain
    def pause_after_custody(self, provider_id, *args, **kwargs):
        original(self, provider_id, *args, **kwargs)
        if provider_id == 'solana:custody_rpc':
            reached.set()
            assert release.wait(2)
    monkeypatch.setattr(HealthRegistry, 'record_wrong_chain', pause_after_custody)
    phase['wrong'] = True
    endpoints, probe = checks[0]
    updater = threading.Thread(target=lambda: probe(endpoints[0]))
    request = build_request(chain_id='solana', action='entry', tx_identity='fixture', signed_payload='c2lnbmVk')
    def submit():
        result.append(transport.submit_one('solana:tpu', request))
        finished.set()
    sender = threading.Thread(target=submit)
    try:
        updater.start()
        assert reached.wait(2)
        sender.start()
        assert not finished.wait(.05)
        release.set()
        updater.join(2); sender.join(2)
        assert not updater.is_alive() and not sender.is_alive()
        assert result[0].state is AttemptState.NOT_SENT
    finally:
        release.set()
        updater.join(2)
        if sender.ident is not None:
            sender.join(2)
        backend.close_all()
