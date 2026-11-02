"""Offline receipts qualify model inputs, never authenticate an on-chain edge."""
from copy import deepcopy
from datetime import datetime, timezone
import json

import pytest

from sniper.persistence import db_wallet_execution as store
from sniper.research.wallet_execution.evidence import digest
from sniper.services.wallet_execution_research import read_dossier
from test_wallet_execution_evidence import episode

NOW = '2026-10-05T12:00:00+00:00'


def admission(e, baseline, key, *, decision=None, cap=800000):
    source = deepcopy(e['source'])
    received = source['received_at']
    decision = received if decision is None else decision
    tx = {'transaction': source['transaction'], 'address': source['address'], 'chain': e['chain'],
          'mint': e['mint'], 'paid_atomic': source['paid_atomic'], 'acquired_atomic': source['acquired_atomic']}
    identity = {'wallet': e['wallet'], 'chain': e['chain'], 'address': source['address']}
    source['raw'] = tx; source['identity_raw'] = identity
    source['proof'] = {'transaction_digest': digest(tx), 'identity_digest': digest(identity)}
    body = {'origin_id': e['origin_id'], 'wallet': e['wallet'], 'chain': e['chain'], 'mint': e['mint'],
        'baseline_id': key, 'defaults': deepcopy(baseline['defaults']), 'source': source,
        'decision_at': decision, 'exemptions': [], 'wallet_overrides': {},
        'market_cap': {'value_usd': cap, 'available_at': received, 'raw': {'cap': cap}},
        'opening': {'scope': 'wallet_mint_prestate', 'complete': True,
            'address': source['address'], 'chain': e['chain'], 'mint': e['mint'],
            'transaction': source['transaction'], 'before_order': source['order'],
            'available_at': received, 'accounts': []},
        'gates': {k: {'passed': True, 'default': v, 'available_at': received,
                       'inputs': {'fixture': 'observed_gate_input'}} for k, v in baseline['defaults'].items()}}
    return {'protocol': 'wallet-fast-move-admission-v2', 'evidence': body, 'admission_digest': digest(body)}


def setup_receipt(tmp_path):
    path = tmp_path / 'research.db'; db = store.open_store(path)
    baseline = {'chain': '56', 'defaults': {k: True for k in store.dossier_gate_inventory()},
                'required_gates': sorted(store.dossier_gate_inventory()),
                'policy': {'policy_id': 'default', 'entry': 'immediate', 'clip_usd': 100, 'max_hold_s': 60}}
    key = store.freeze_dossier_baseline(db, baseline, now='2026-10-04T00:00:00+00:00')
    at = datetime.fromisoformat(NOW).timestamp() - 3600
    origin = store.record_origin(db, {'upstream_event_id': 'source', 'source_user_id': 'alice',
        'network_id': '56', 'mint': 'mint', 'observed_at': datetime.fromtimestamp(at, timezone.utc).isoformat()})
    e = episode(); e.update(origin_id=origin, mint='mint')
    for f in e['frames']:
        f['at'] += at; f['available_at'] += at
    for field in ('source_at', 'available_at', 'received_at'): e['source'][field] += at
    for field in ('start', 'end'): e['window'][field] += at
    e['execution']['calibration_before'] += at
    return path, db, baseline, key, e


def test_standalone_admission_counts_without_fabricating_a_path(tmp_path):
    path, db, baseline, key, e = setup_receipt(tmp_path)
    receipt = admission(e, baseline, key, decision=e['source']['received_at'] + .25)
    store.record_dossier_admission(db, receipt, now=NOW)
    db.commit(); db.close()
    view = read_dossier(path, wallet='alice', chain='56', now=NOW)
    assert view['funnel']['valid_entries'] == 1
    assert view['funnel']['modeled_outcomes'] == 0 and view['funnel']['pending_or_censored'] == 1
    assert view['entries'][0]['paper'] is None and view['strategy_supported'] is False
    assert view['entries'][0]['admission']['decision_at'] == e['source']['received_at'] + .25


@pytest.mark.parametrize('mutate', [
    lambda b: b['opening'].update(scope='transaction_accounts'),
    lambda b: b['opening'].update(complete=False),
    lambda b: b['opening'].update(accounts=[{'address': 'ata', 'owner': b['source']['address'], 'mint': b['mint'], 'amount_atomic': '1'}]),
    lambda b: b['opening'].update(address='somebody_else'),
    lambda b: b['opening'].update(before_order=[10, 1, 0]),
    lambda b: b['market_cap'].update(available_at=b['decision_at'] + 1),
    lambda b: b['gates']['route'].update(passed=None),
    lambda b: b['gates']['route'].update(default=False),
    lambda b: b['gates']['route'].update(inputs={}),
    lambda b: b.update(exemptions=['source_buy_floor']),
    lambda b: b.update(wallet_overrides={'chase': 50}),
    lambda b: b['source']['raw'].update(acquired_atomic='999'),
    lambda b: b['source'].update(decimals=True),
    lambda b: b['source'].update(spent_usd=None),
    lambda b: b['market_cap'].update(raw='unparsed'),
])
def test_mutated_or_partial_evidence_never_passes(tmp_path, mutate):
    path, db, baseline, key, e = setup_receipt(tmp_path)
    receipt = admission(e, baseline, key)
    mutate(receipt['evidence']); receipt['admission_digest'] = digest(receipt['evidence'])
    store.record_dossier_admission(db, receipt, now=NOW)
    db.commit(); db.close()
    view = read_dossier(path, wallet='alice', chain='56', now=NOW)
    assert view['funnel']['valid_entries'] == 0 and view['entries'][0]['reasons']


def test_hash_only_legacy_claims_are_not_clean_receipts(tmp_path):
    path, db, baseline, key, e = setup_receipt(tmp_path)
    e['dossier'] = {'baseline_id': key, 'defaults_digest': digest(baseline['defaults']),
        'pre_balance_atomic': '0', 'pre_balance_digest': 'a' * 64, 'market_cap_usd': 800000,
        'available_at': e['source']['received_at'], 'decision_at': e['source']['received_at'],
        'admission_digest': 'b' * 64, 'gates': {k: True for k in baseline['required_gates']}}
    store.ingest_episode(db, e); db.commit(); db.close()
    assert read_dossier(path, wallet='alice', chain='56', now=NOW)['funnel']['valid_entries'] == 0


def test_explicit_admission_clock_delays_replay():
    from sniper.research.wallet_execution.replay import replay
    e = episode()
    result = replay(e, {'policy_id': 'default', 'entry': 'immediate', 'clip_usd': 100, 'max_hold_s': 60}, decision_at=2.)
    assert result['entry_at'] == 3. and result['decision_at'] == 2.


def test_admission_import_survives_collector_lock_and_is_immutable(tmp_path):
    import fcntl
    import subprocess
    path, db, baseline, key, e = setup_receipt(tmp_path)
    receipt = admission(e, baseline, key)
    db.commit(); db.close()
    raw = tmp_path / 'receipt.json'; raw.write_text(json.dumps(receipt))
    with (tmp_path / 'wallet_execution_research.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        imported = subprocess.run(['python3', 'scripts/research/wallet_execution_worker.py',
            '--db', str(path), '--ingest-admission', str(raw)], capture_output=True, text=True)
    assert imported.returncode == 0, imported.stderr
    assert json.loads(imported.stdout)['eligible'] is True
    db = store.open_store(path)
    assert db.execute("SELECT count(*) FROM we_raw WHERE source='wallet_dossier_admission_v2'").fetchone()[0] == 1
    store.record_dossier_admission(db, receipt, now=NOW)
    changed = deepcopy(receipt); changed['evidence']['market_cap']['value_usd'] = 750000
    changed['admission_digest'] = digest(changed['evidence'])
    with pytest.raises(ValueError, match='immutable'): store.record_dossier_admission(db, changed, now=NOW)
    assert db.execute("SELECT count(*) FROM we_raw WHERE source='wallet_dossier_admission_v2'").fetchone()[0] == 1
    db.close()


def test_unknown_default_cannot_be_sealed_as_known(tmp_path):
    path, db, baseline, key, e = setup_receipt(tmp_path)
    baseline['defaults']['route'] = None
    with pytest.raises(ValueError): store.freeze_dossier_baseline(db, baseline, now=NOW)
    db.close()


@pytest.mark.parametrize('mirror', [None, 'false', 1])
def test_nonboolean_mirror_policy_cannot_be_sealed(tmp_path, mirror):
    path, db, baseline, key, e = setup_receipt(tmp_path)
    baseline['policy']['mirror'] = mirror
    with pytest.raises(ValueError): store.freeze_dossier_baseline(db, baseline, now=NOW)
    db.close()


@pytest.mark.parametrize('mutate', [lambda e: e.update(wallet='bob', mint='different-mint'),
                                   lambda e: e['source'].update(spent_usd=50),
                                   lambda e: e['source'].update(source_at=e['source']['source_at'] - 1)])
def test_mismatched_episode_cannot_supply_another_wallets_outcome(tmp_path, mutate):
    path, db, baseline, key, e = setup_receipt(tmp_path)
    receipt = admission(e, baseline, key)
    store.record_dossier_admission(db, receipt, now=NOW)
    e['source'] = deepcopy(receipt['evidence']['source'])
    mutate(e)
    store.ingest_episode(db, e); db.commit(); db.close()
    view = read_dossier(path, wallet='alice', chain='56', now=NOW)
    assert view['funnel']['valid_entries'] == 1 and view['funnel']['modeled_outcomes'] == 0
    assert 'episode_admission_identity_mismatch' in view['entries'][0]['reasons']


@pytest.mark.parametrize('table', ['foreign_owner', 'sqliteforeign'])
def test_foreign_database_is_refused_before_schema_or_receipt_writes(tmp_path, table):
    import sqlite3
    path = tmp_path / 'foreign.db'
    with sqlite3.connect(path) as db: db.execute(f'CREATE TABLE {table}(id INTEGER)')
    before = path.read_bytes()
    with pytest.raises(ValueError, match='foreign'): store.open_store(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize('field,value', [('address', 123), ('transaction', {'tx': 1})])
def test_consistently_hashed_nonscalar_identity_is_rejected(tmp_path, field, value):
    path, db, baseline, key, e = setup_receipt(tmp_path)
    receipt = admission(e, baseline, key); body = receipt['evidence']; source = body['source']
    source[field] = value; source['raw'][field] = value; body['opening'][field] = value
    if field == 'address': source['identity_raw'][field] = value
    source['proof'] = {'transaction_digest': digest(source['raw']), 'identity_digest': digest(source['identity_raw'])}
    receipt['admission_digest'] = digest(body)
    assert not store.record_dossier_admission(db, receipt, now=NOW).eligible
    db.close()


def test_concurrent_raw_import_never_reports_the_losing_receipt_as_stored(tmp_path):
    path = tmp_path / 'research.db'; first = store.open_store(path); second = store.open_store(path)
    class CompetingInsert:
        def execute(self, sql, parameters):
            if sql.startswith('INSERT OR IGNORE INTO we_raw'):
                store.record_raw(second, 'receipt', 'origin', {'cap': 750000}); second.commit()
            return first.execute(sql, parameters)
    try:
        with pytest.raises(ValueError, match='immutable'):
            store.record_raw(CompetingInsert(), 'receipt', 'origin', {'cap': 800000})
        assert json.loads(first.execute('SELECT payload FROM we_raw').fetchone()[0]) == {'cap': 750000}
    finally:
        first.close(); second.close()
