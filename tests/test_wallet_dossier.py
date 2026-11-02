from datetime import datetime, timezone
from copy import deepcopy
import json
import pytest
from sniper.persistence import db_wallet_execution as store
from sniper.services.wallet_execution_research import read_dossier
from sniper.research.wallet_execution.evidence import digest
from test_wallet_execution_evidence import episode
from test_wallet_dossier_admission import admission

NOW = '2026-10-05T12:00:00+00:00'


def test_missing_store_is_unknown_and_never_created(tmp_path):
    path = tmp_path / 'missing.db'
    view = read_dossier(path, wallet='alice', chain='56', now=NOW)
    assert view['state'] == 'unavailable' and view['funnel'] is None
    assert not path.exists()


def test_clean_view_keeps_denominator_chain_window_and_frozen_defaults(tmp_path):
    path = tmp_path / 'research.db'
    db = store.open_store(path)
    baseline = {'chain': '56', 'defaults': {k: True for k in store.dossier_gate_inventory()},
                'required_gates': sorted(store.dossier_gate_inventory()),
                'policy': {'policy_id': 'default-v1', 'entry': 'immediate', 'clip_usd': 100,
                           'max_hold_s': 1799, 'tp_pct': 25, 'sl_pct': 35}}
    key = store.freeze_dossier_baseline(db, baseline, now='2026-10-04T00:00:00+00:00')
    stamp = datetime.fromisoformat(NOW).timestamp() - 3600
    for uid, chain, at in [('good', '56', stamp), ('unknown', '56', stamp),
                           ('legacy', '56', stamp), ('pending', '56', stamp), ('wrong', 'solana', stamp),
                           ('old', '56', stamp - 31 * 86400)]:
        source = {'upstream_event_id': uid, 'source_user_id': 'alice', 'network_id': chain,
                  'mint': uid, 'observed_at': datetime.fromtimestamp(at, timezone.utc).isoformat()}
        origin = store.record_origin(db, source)
        if uid in ('good', 'unknown', 'pending'):
            e = episode(); e.update(origin_id=origin, mint=uid)
            for f in e['frames']:
                f['at'] += at; f['available_at'] += at
            for field in ('source_at', 'available_at', 'received_at'): e['source'][field] += at
            for field in ('start', 'end'): e['window'][field] += at
            e['execution']['calibration_before'] += at
            e['dossier'] = admission(e, baseline, key)
            if uid == 'unknown': e['dossier']['evidence']['opening']['complete'] = False
            e['dossier']['admission_digest'] = digest(e['dossier']['evidence'])
            e['source'] = deepcopy(e['dossier']['evidence']['source'])
            if uid == 'pending': e['frames'] = e['frames'][:-100]
            store.ingest_episode(db, e)
    store.append_dossier_note(db, wallet='alice', chain='56', text='<script>research</script>', now=NOW)
    db.commit(); db.close()
    view = read_dossier(path, wallet='alice', chain='56', now=NOW)
    assert view['funnel'] == {'observed_buys': 4, 'valid_entries': 2, 'excluded_or_unknown': 2, 'distinct_coins': 4, 'modeled_outcomes': 1, 'pending_or_censored': 1}
    assert view['strategy_supported'] is False and view['coverage']['days_observed'] < 30
    assert view['notes'][0]['text'] == '<script>research</script>'
    valid = next(r for r in view['entries'] if r['mint'] == 'good')
    assert valid['fast_move']['5']['endpoint_pct'] == pytest.approx(8 / 101 * 100)
    assert valid['paper']['state'] == 'closed'
    db = store.open_store(path)
    with pytest.raises(ValueError):
        store.freeze_dossier_baseline(db, baseline, now=NOW)  # immutable active baseline
    db.close()


def test_notes_endpoint_is_scoped_and_requires_csrf(monkeypatch, tmp_path):
    from test_memecoin_promotion_endpoint import _client
    from sniper.util import state_paths
    monkeypatch.setattr(state_paths, 'state_path', lambda *a: tmp_path.joinpath(*a))
    client, _ = _client()
    url = '/api/memecoin/wallet-map/dossier?chain=bsc&wallet=alice'
    assert client.post(url, json={'text': 'test'}).status_code == 403
    token = client.get('/api/memecoin/csrf-token').get_json()['csrf_token']
    headers = {'X-CSRF-Token': token}
    assert client.post(url, json={'text': 'Research question'}, headers=headers).status_code == 201
    view = client.get(url).get_json()
    assert view['notes'][0]['text'] == 'Research question'
    assert client.get(url.replace('bsc', 'solana')).get_json()['notes'] == []
    assert client.post(url, json={'text': 'x' * 4001}, headers=headers).status_code == 400
    assert client.get(url.replace('bsc', 'bad')).status_code == 400


def test_notes_refuse_foreign_store_as_unavailable(monkeypatch, tmp_path):
    import sqlite3
    from test_memecoin_promotion_endpoint import _client
    from sniper.util import state_paths
    monkeypatch.setattr(state_paths, 'state_path', lambda *a: tmp_path.joinpath(*a))
    path = tmp_path / 'data/wallet_execution_research.db'; path.parent.mkdir()
    with sqlite3.connect(path) as db: db.execute('CREATE TABLE foreign_owner(id INTEGER)')
    before = path.read_bytes()
    client, _ = _client()
    token = client.get('/api/memecoin/csrf-token').get_json()['csrf_token']
    response = client.post('/api/memecoin/wallet-map/dossier?chain=bsc&wallet=alice',
                           json={'text': 'Research question'}, headers={'X-CSRF-Token': token})
    assert response.status_code == 503 and response.get_json()['error'] == 'research_store_unavailable'
    assert path.read_bytes() == before


def test_partial_gate_inventory_and_unsupported_exits_cannot_seal(tmp_path):
    db = store.open_store(tmp_path / 'research.db')
    baseline = {'chain': '56', 'defaults': {'route': True}, 'required_gates': ['route'],
                'policy': {'policy_id': 'test', 'entry': 'immediate', 'clip_usd': 25, 'max_hold_s': 60}}
    with pytest.raises(ValueError): store.freeze_dossier_baseline(db, baseline, now=NOW)
    baseline.update(defaults={k: True for k in store.dossier_gate_inventory()},
                    required_gates=sorted(store.dossier_gate_inventory()))
    baseline['policy']['derisk_pct'] = 50
    with pytest.raises(ValueError): store.freeze_dossier_baseline(db, baseline, now=NOW)
    assert db.execute("SELECT count(*) FROM we_raw WHERE source LIKE 'wallet_dossier_baseline%'").fetchone()[0] == 0
    db.close()


def test_baseline_seal_is_independent_of_collector_process_lock(tmp_path):
    import fcntl
    import subprocess
    baseline = {'chain': '56', 'defaults': {k: True for k in store.dossier_gate_inventory()},
                'required_gates': sorted(store.dossier_gate_inventory()),
                'policy': {'policy_id': 'default', 'entry': 'immediate', 'clip_usd': 25, 'max_hold_s': 60}}
    source = tmp_path / 'baseline.json'; source.write_text(json.dumps(baseline))
    with (tmp_path / 'wallet_execution_research.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(['python3', 'scripts/research/wallet_execution_worker.py',
                    '--db', str(tmp_path / 'research.db'), '--seal-dossier-baseline', str(source)],
                    capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)['baseline_id'] == digest(baseline)
    assert not (tmp_path / 'wallet_execution_coverage.json').exists()


def test_dossier_snapshot_never_counts_future_decisions_or_paths(tmp_path):
    path = tmp_path / 'research.db'; db = store.open_store(path)
    baseline = {'chain': '56', 'defaults': {k: True for k in store.dossier_gate_inventory()},
                'required_gates': sorted(store.dossier_gate_inventory()),
                'policy': {'policy_id': 'default', 'entry': 'immediate', 'clip_usd': 100, 'max_hold_s': 60}}
    key = store.freeze_dossier_baseline(db, baseline, now='2026-10-04T00:00:00+00:00')
    now = datetime.fromisoformat(NOW).timestamp()
    for name, received in [('future_decision', now + 3600), ('pending_path', now - 30)]:
        origin = store.record_origin(db, {'upstream_event_id': name, 'source_user_id': 'alice',
            'network_id': '56', 'mint': name, 'observed_at': datetime.fromtimestamp(now - 30, timezone.utc).isoformat()})
        e = episode(); e.update(origin_id=origin, mint=name)
        for frame in e['frames']:
            frame['at'] += received; frame['available_at'] += received
        for field in ('source_at', 'available_at', 'received_at'): e['source'][field] += received
        for field in ('start', 'end'): e['window'][field] += received
        e['execution']['calibration_before'] += received
        e['dossier'] = admission(e, baseline, key, cap=100000)
        e['source'] = deepcopy(e['dossier']['evidence']['source'])
        assert store.ingest_episode(db, e).eligible
    db.commit(); db.close()
    view = read_dossier(path, wallet='alice', chain='56', now=NOW)
    assert view['funnel']['valid_entries'] == 1
    assert view['funnel']['modeled_outcomes'] == 0
    assert view['funnel']['pending_or_censored'] == 1
    future = next(r for r in view['entries'] if r['mint'] == 'future_decision')
    assert future['state'] == 'excluded_or_unknown'
    assert all(r['fast_move'] is None for r in view['entries'])


def test_fast_move_uses_exact_replay_entry_when_publication_times_tie():
    from sniper.services.wallet_execution_research import dossier_fast_move
    from sniper.research.wallet_execution.replay import replay
    e = episode()
    for frame in e['frames'][:122]: frame['available_at'] = 1.0
    e['frames'][0]['buy'].update(spend_usd=200., fee_usd=2.)
    result = replay(e, {'policy_id': 'default', 'entry': 'immediate', 'clip_usd': 100., 'max_hold_s': 60.})
    assert result['state'] == 'closed'
    fast = dossier_fast_move(e, result)
    assert fast['5']['endpoint_pct'] == pytest.approx(8 / 101 * 100)
    assert fast['5']['worst_pct'] == pytest.approx(8 / 101 * 100)
