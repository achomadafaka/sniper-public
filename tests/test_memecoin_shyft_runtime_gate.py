"""Startup/cutover checks use actual producer status, never a configured endpoint alone."""
import copy
import hashlib
from urllib.parse import urlsplit

import pytest
import json
import sqlite3
import time
from types import SimpleNamespace

from scripts import memecoin_runtime_verify as verify
from test_shyft_migration_preflight import candidate_env


def graph():
    env = candidate_env()
    def identity(url):
        p = urlsplit(url)
        return dict(hostname=p.hostname, url_fingerprint=hashlib.sha256(
            ('https://' + p.hostname + (p.path or '/') + '?' + p.query).encode()).hexdigest())
    ack = dict(provider='shyft', hostname='grpc.fra.shyft.to', commitment='processed',
        connected=True, generation=1, acknowledged_generation=1, requested_count=1,
        acknowledged_count=1, requested_filter_fingerprint='a'*64,
        acknowledged_filter_fingerprint='a'*64, head_slot=42, head_monotonic=99.9,
        head_age_s=.1)
    processed = dict(connected=True, requested_owners=['owner'], acknowledged_owners=['owner'],
        connection_generation=1, watch_generation=1, requested_watch_generation=1,
        acknowledged_watch_generation=1, requested_filter_fingerprint='c'*64,
        acknowledged_filter_fingerprint='c'*64,
        head_slot=42, head_age_s=.1, migration_filter_requested=True,
        migration_filter_acknowledged=True, migration_filter_generation='migrations:c1:r1',
        damm_filter_requested=True, damm_filter_acknowledged=True, damm_filter_generation='damm:c1:r1')
    hb = dict(ts=100, auth_ok=True, solana_rpc_pool=dict(provider_roles=dict(provider='shyft',
        funded=[identity(env['MEMECOIN_SOLANA_RPC_POOL_URLS'])],
        read=identity(env['MEMECOIN_SOLANA_READ_RPC_URL']),
        tpu=identity(env['MEMECOIN_TPU_RPC_URL']) | dict(constructed=True))),
        tpu=dict(configured=True, running=True, ready=True, last_health_age_s=.1),
        slot_lane=dict(signal_feeds={f'shyft:{host}': copy.deepcopy(processed)
            for host in ('grpc.ams.shyft.to', 'grpc.fra.shyft.to')},
            account_stream=dict(endpoint='grpc.ams.shyft.to', commitment='confirmed', connected=True,
                subscription_acknowledged=True, connection_generation=1, watch_generation=1,
                requested_account_count=0, acknowledged_account_count=0, coverage='idle'),
            execution_observer=ack | dict(state='ready'),
            preparation=dict(feed=dict(endpoint_hostname='rabbitstream.fra.shyft.to',
                connected=True, connection_generation=1, requested_owners=['owner'],
                acknowledged_owners=['owner'], last_frame_age_s=.1))),
        fastlane=dict(source_sell_mirror=dict(account_socket=dict(provider='shyft',
            hostname='grpc.ams.shyft.to', commitment='processed', connected=True,
            subscription_acknowledged=True, connection_generation=1, watch_generation=1,
            requested_account_count=0, coverage='idle', requested_filter_fingerprint='b'*64,
            acknowledged_filter_fingerprint='b'*64))))
    rh = dict(at=100, stream=ack | dict(watch_generation=1, requested_watch_generation=1,
        acknowledged_watch_generation=1))
    return env, hb, rh


def errors(hb=None, rh=None):
    env, good, good_rh = graph()
    return verify.shyft_runtime_graph_errors(good if hb is None else hb,
        good_rh if rh is None else rh, env, now=100, monotonic=100, rh_now=100)


def test_all_current_roles_and_idle_account_sets_are_ready():
    assert errors() == []


@pytest.mark.parametrize('component,field,value', [
    ('account_stream', 'subscription_acknowledged', False),
    ('account_stream', 'connection_generation', 0),
    ('account_stream', 'coverage', 'unknown'),
    ('execution_observer', 'acknowledged_generation', 0),
    ('execution_observer', 'acknowledged_filter_fingerprint', None),
    ('execution_observer', 'hostname', 'grpc.ny.shyft.to'),
    ('execution_observer', 'head_monotonic', 97),
])
def test_missing_unknown_partial_or_stale_role_refuses_cutover(component, field, value):
    _, hb, _ = graph()
    hb['slot_lane'][component][field] = value
    assert errors(hb)


@pytest.mark.parametrize('field,value', [('migration_filter_acknowledged', False),
    ('damm_filter_acknowledged', False), ('requested_owners', []),
    ('acknowledged_owners', ['other']), ('head_age_s', 3)])
def test_processed_ack_and_fresh_heads_are_required_in_both_regions(field, value):
    _, hb, _ = graph()
    hb['slot_lane']['signal_feeds']['shyft:grpc.fra.shyft.to'][field] = value
    assert errors(hb)


@pytest.mark.parametrize('field,value', [('connection_generation', 0),
    ('acknowledged_watch_generation', 0), ('acknowledged_filter_fingerprint', None)])
def test_processed_current_generation_and_exact_filter_fingerprint_are_required(field, value):
    _, hb, _ = graph()
    hb['slot_lane']['signal_feeds']['shyft:grpc.ams.shyft.to'][field] = value
    assert errors(hb)


def test_historical_rh_head_and_wrong_watch_generation_refuse():
    _, _, rh = graph()
    rh['stream']['head_monotonic'] = 97
    assert errors(rh=rh)
    _, _, rh = graph()
    rh['stream']['acknowledged_watch_generation'] = 0
    assert errors(rh=rh)


def test_configured_tpu_without_constructed_actual_role_refuses():
    _, hb, _ = graph()
    hb['solana_rpc_pool']['provider_roles']['tpu']['constructed'] = False
    assert errors(hb)


def test_operator_kill_does_not_falsify_provider_readiness():
    _, hb, _ = graph()
    hb['kill_switch'] = True
    assert errors(hb) == []


def test_nonempty_account_ack_does_not_require_organic_account_observations():
    _, hb, _ = graph()
    hb['slot_lane']['account_stream'].update(requested_account_count=3,
        acknowledged_account_count=0, coverage='pending')
    assert errors(hb) == []


def test_published_head_age_includes_time_since_heartbeat():
    env, hb, rh = graph()
    hb['ts'] = 97
    assert verify.shyft_runtime_graph_errors(hb, rh, env, now=100, monotonic=100, rh_now=100)


def test_runtime_wrapper_binds_external_rh_pid_without_sql(tmp_path, monkeypatch):
    data = tmp_path / 'data'; data.mkdir()
    env, hb, rh = graph()
    hb['ts'] = time.time()
    (data / 'memecoin_runner.heartbeat').write_text(json.dumps(hb))
    (data / 'memecoin_rh_feed_shadow.heartbeat').write_text(json.dumps(dict(ts=time.time(), pid=22,
        deposit=dict(stream=rh['stream']))))
    from sniper.services.memecoin_real_runner import _provider_role_identity
    (data / 'onchain_watch.heartbeat').write_text(json.dumps(dict(shyft_confirmed=dict(
        http_role=_provider_role_identity(env['ONCHAIN_WATCH_SOLANA_RPC_URL'])))))
    (tmp_path / '.env').write_text('\n'.join(k+'='+v for k,v in env.items()))
    monkeypatch.setattr(sqlite3, 'connect', lambda *a, **kw: (_ for _ in ()).throw(AssertionError('SQL forbidden')))
    def rows(state, suffix):
        return [(11 if suffix.endswith('memecoin_runner_loop.py') else 22, str(tmp_path / suffix))]
    monkeypatch.setattr(verify, '_owned_script_rows', rows)
    monkeypatch.setattr(verify, 'verify_shyft_watcher', lambda *a, **kw: [])
    monkeypatch.setattr(verify, 'shyft_runtime_graph_errors', lambda *a, **kw: [])
    assert verify.verify_shyft_runtime_graph(tmp_path, tmp_path) == []
    (data / 'memecoin_rh_feed_shadow.heartbeat').write_text(json.dumps(dict(ts=time.time(), pid=23)))
    assert 'shyft_runtime_rh_process_invalid' in verify.verify_shyft_runtime_graph(tmp_path, tmp_path)


def test_missing_rh_database_stays_missing(tmp_path, monkeypatch):
    (tmp_path / 'data').mkdir()
    monkeypatch.setattr(verify, '_owned_script_rows', lambda *a: [])
    monkeypatch.setattr(verify, 'verify_shyft_watcher', lambda *a, **kw: [])
    assert verify.verify_shyft_runtime_graph(tmp_path, tmp_path)
    assert not (tmp_path / 'data/memecoin_rh_feed_shadow.db').exists()


def test_actual_observer_heartbeat_preserves_receive_clock_for_runtime_gate(tmp_path, monkeypatch):
    from sniper.services import memecoin_runner as runner
    env, hb, rh = graph()
    observer = hb['slot_lane']['execution_observer'] | {'extra_secret': 'offline-secret'}
    monkeypatch.setattr(runner, 'time', SimpleNamespace(time=lambda: 100., monotonic=lambda: 100.))
    path = tmp_path / 'heartbeat.json'
    runner.write_heartbeat({}, True, path=path, ts=100.,
        slot_lane_status={'execution_observer': observer})
    serialized = json.loads(path.read_text())['slot_lane']['execution_observer']
    hb['slot_lane']['execution_observer'] = serialized
    assert verify.shyft_runtime_graph_errors(hb, rh, env, now=100., monotonic=100., rh_now=100.) == []
    assert serialized['head_monotonic'] == observer['head_monotonic']
    assert 'offline-secret' not in path.read_text()
    for bad in (None, 97., 100.1):
        observer['head_monotonic'] = bad
        runner.write_heartbeat({}, True, path=path, ts=100.,
            slot_lane_status={'execution_observer': observer})
        hb['slot_lane']['execution_observer'] = json.loads(path.read_text())['slot_lane']['execution_observer']
        assert 'shyft_runtime_observer_unready' in verify.shyft_runtime_graph_errors(
            hb, rh, env, now=100., monotonic=100., rh_now=100.)


@pytest.mark.parametrize('stamp', [None, float('nan'), float('inf'), -1., True, '99.9'])
def test_actual_observer_heartbeat_cannot_invent_invalid_receive_clock(tmp_path, monkeypatch, stamp):
    from sniper.services import memecoin_runner as runner
    env, hb, rh = graph()
    observer = hb['slot_lane']['execution_observer'] | {'head_monotonic': stamp}
    monkeypatch.setattr(runner, 'time', SimpleNamespace(time=lambda: 100., monotonic=lambda: 100.))
    path = tmp_path / 'heartbeat.json'
    runner.write_heartbeat({}, True, path=path, ts=100.,
        slot_lane_status={'execution_observer': observer})
    serialized = json.loads(path.read_text())['slot_lane']['execution_observer']
    assert serialized['head_monotonic'] is None
    hb['slot_lane']['execution_observer'] = serialized
    assert 'shyft_runtime_observer_unready' in verify.shyft_runtime_graph_errors(
        hb, rh, env, now=100., monotonic=100., rh_now=100.)


def test_actual_rh_heartbeat_stays_fresh_between_publishes_and_rejects_lost_generation(tmp_path, monkeypatch):
    from scripts import memecoin_rh_feed_shadow_loop as loop
    from sniper.services import memecoin_rh_feed_shadow as feed, memecoin_runner as runner
    from sniper.settings import memecoin as cfg
    env, _, rh = graph()
    now, sleeps = [100.], []
    status = rh['stream'] | {'extra_secret': 'offline-secret'}
    shadow = SimpleNamespace(threads=[], counters={}, stats={}, deposit_stream_status=status)
    monkeypatch.setattr(cfg, 'memecoin_rh_fast_lane_mode', lambda: 'live')
    monkeypatch.setattr(runner, 'acquire_run_lock', lambda *a: object())
    monkeypatch.setattr(feed, 'start_shadow', lambda **kw: shadow)
    monkeypatch.setattr(loop, 'state_path', lambda *a: tmp_path / a[-1])
    monkeypatch.setattr(loop, 'time', SimpleNamespace(time=lambda: now[0], monotonic=lambda: now[0]))
    class Done(Exception):
        pass
    def sleep(duration):
        payload = json.loads((tmp_path / 'memecoin_rh_feed_shadow.heartbeat').read_text())
        probe = now[0] + duration - .01
        _, hb, _ = graph()
        hb['ts'] = probe
        hb['slot_lane']['execution_observer']['head_monotonic'] = probe - .1
        gate = verify.shyft_runtime_graph_errors(hb,
            {'at': payload['ts'], 'stream': payload['deposit']['stream']}, env,
            now=probe, monotonic=probe, rh_now=probe)
        assert payload['pid'] > 0 and 'offline-secret' not in json.dumps(payload)
        if len(sleeps) < 2:
            assert 'shyft_runtime_rh_deposit_unready' not in gate
        else:
            assert 'shyft_runtime_rh_deposit_unready' in gate
            raise Done
        sleeps.append(duration)
        now[0] += duration
        status.update(head_monotonic=now[0] - .1, last_frame_monotonic=now[0] - .1)
        if len(sleeps) == 2:
            status.update(connected=False, generation=2, acknowledged_generation=None,
                acknowledged_filter_fingerprint=None)
    with pytest.raises(Done):
        loop.main(sleep_fn=sleep)
    assert sleeps == [1., 1.]


def idle_graph():
    """Copy trading stopped (2026-10-23): no Solana wallet armed, every owner roster empty and acknowledged."""
    _, hb, _ = graph()
    lane = hb['slot_lane']
    for feed in [*lane['signal_feeds'].values(), lane['preparation']['feed']]:
        feed['requested_owners'], feed['acknowledged_owners'] = [], []
    lane['armed'], lane['owners'] = 0, 0
    hb['address_coverage'] = dict(armed=0, labels={}, j7_only=[])
    return hb


def test_empty_acknowledged_rosters_are_ready_only_when_nothing_is_armed():
    assert errors(idle_graph()) == []


@pytest.mark.parametrize('path,value', [
    (('slot_lane', 'armed'), 1),                 # lane says armed but subscribed nobody
    (('slot_lane', 'owners'), 2),
    (('address_coverage', 'armed'), 3),          # the registry's independent read sees armed wallets
    (('address_coverage',), None),               # coverage read failed: unknown is not zero
    (('slot_lane', 'armed'), None),
    (('slot_lane', 'armed'), False),
])
def test_empty_roster_still_refuses_when_armed_or_unknown(path, value):
    hb = idle_graph()
    target = hb
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert 'shyft_runtime_processed_ack_incomplete' in errors(hb)


def test_idle_roster_still_needs_exact_ack_generation_and_fingerprint():
    for field, value in [('acknowledged_owners', ['x']), ('acknowledged_watch_generation', 0),
                         ('acknowledged_filter_fingerprint', None), ('connected', False)]:
        hb = idle_graph()
        hb['slot_lane']['signal_feeds']['shyft:grpc.ams.shyft.to'][field] = value
        assert 'shyft_runtime_processed_ack_incomplete' in errors(hb), field
    hb = idle_graph()
    hb['slot_lane']['preparation']['feed']['connected'] = False
    assert errors(hb) == ['shyft_runtime_prepare_unready']
