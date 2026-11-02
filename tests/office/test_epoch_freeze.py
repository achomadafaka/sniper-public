import copy
import hashlib
import json
import time

import pytest

from sniper.office import roster as R, store as S
from .epoch_fixture import build


@pytest.fixture
def env(tmp_path, monkeypatch):
    conn = S.open_store(tmp_path/'office.db')
    nominee, refs = build(conn, tmp_path/'config')
    from .launcher_fixture import LauncherFixture
    from sniper.office import launcher_policy
    monkeypatch.setattr(launcher_policy, 'readonly_mount', lambda fd:True)
    launcher = LauncherFixture(tmp_path/'config', lambda:1000.)
    args = dict(launcher_policy=launcher.policy, probe_write=launcher.probe, clock=lambda: 1000., hypothesis_id='same', by='crew', reason='freeze',
                nominee=nominee, nominee_refs=refs, config_root=tmp_path/'config')
    yield conn, args
    launcher.close()
    conn.close()


def trial(conn, k, status):
    S.append(conn, 'trial_registry', dict(k=k, status=status))


def test_empty_registry_derives_first_alpha_and_stores_nominee(env):
    conn, args = env
    start = time.monotonic()
    eid = R._open_evaluated(conn, {}, **args)
    assert time.monotonic()-start < 1.
    row = json.loads(conn.execute('SELECT payload_json FROM epochs WHERE id=?', (eid,)).fetchone()[0])
    assert row['k'] == 1 and row['alpha_k'] == .025
    assert row['nominee'] == args['nominee']
    assert row['nominee_config_hash'] == hashlib.sha256(S.canonical_json(row['nominee'])).hexdigest()


@pytest.mark.parametrize('status', ['BROKEN','FAILED','COMPLETED','NOMINATED','REPLICATION','RUNNING','aborted','retry'])
def test_all_trial_statuses_spend_alpha(env, status):
    conn, args = env
    trial(conn, 1, status)
    args['nominee'], args['nominee_refs'] = build(conn, args['config_root'], k=2)
    eid = R._open_evaluated(conn, {}, **args)
    assert json.loads(conn.execute('SELECT payload_json FROM epochs WHERE id=?', (eid,)).fetchone()[0])['k'] == 2


@pytest.mark.parametrize('over,reason', [({'k': 2}, 'k_mismatch'), ({'alpha_k': .01}, 'alpha_mismatch')])
def test_caller_cannot_choose_alpha(env, over, reason):
    conn, args = env
    with pytest.raises(R.OrgRefused, match=reason):
        R._open_evaluated(conn, {}, **dict(args, **over))
    assert conn.execute('SELECT count(*) FROM trial_registry').fetchone()[0] == 0


def test_family_exhausted_and_broken_chain_refuse(env):
    conn, args = env
    trial(conn, 10, 'FAILED')
    with pytest.raises(R.OrgRefused, match='family_exhausted'):
        R._open_evaluated(conn, {}, **args)
    conn.execute('DROP TRIGGER trial_registry_no_update')
    conn.execute("UPDATE trial_registry SET payload_json='{}'")
    conn.commit()
    with pytest.raises(R.OrgRefused, match='trial_registry_corrupt'):
        R._open_evaluated(conn, {}, **args)


@pytest.mark.parametrize('key', ['memory_snapshot_id','learning_rule','prompt_hashes','model_ids','pods_rule_sha256','cost_rule_id','metric_registry_hash','calibration_proof_id','delta0','delta1','paper_drawdown','clip_usd','min_epoch_days','l_null_file_hash','exec_bound_evidence_id','host_guard_manifest_hash','stopping_rule','tool_contract_hash'])
def test_missing_nominee_fields_refuse(env, key):
    conn, args = env
    args['nominee'] = copy.deepcopy(args['nominee'])
    args['nominee'].pop(key)
    with pytest.raises(R.OrgRefused):
        R._open_evaluated(conn, {}, **args)
    assert conn.execute('SELECT count(*) FROM trial_registry').fetchone()[0] == 0


def test_tamper_after_open_breaks_epoch_and_keeps_alpha_spent(env):
    conn, args = env
    eid = R._open_evaluated(conn, {}, **args)
    path = args['config_root']/'pods.json'
    path.chmod(0o644)
    path.write_text('{}')
    path.chmod(0o444)
    with pytest.raises(R.OrgRefused, match='nominee_changed'):
        R.verify_nominee(conn, epoch_id=eid, config_root=args['config_root'], clock=args['clock'], launcher_policy=args['launcher_policy'], probe_write=args['probe_write'])
    epoch = json.loads(conn.execute('SELECT payload_json FROM epochs WHERE id=?', (eid,)).fetchone()[0])
    assert epoch['status'] == 'broken'
    assert [json.loads(r[0])['status'] for r in conn.execute('SELECT payload_json FROM trial_registry ORDER BY seq')] == ['RUNNING','BROKEN']


@pytest.mark.parametrize('mode', ['pilot','writable','symlink','escape','wrong_k','wrong_scale','wrong_days','roster','unmeasured'])
def test_invalid_or_unresolved_pins_refuse_without_spending(env, mode):
    conn, args = env
    args = dict(args, nominee=copy.deepcopy(args['nominee']), nominee_refs=copy.deepcopy(args['nominee_refs']))
    path = args['config_root']/'host_guard.json'
    if mode == 'pilot':
        path.chmod(0o644)
        path.write_text('{"mode":"pilot","provisional":true,"evaluation_allowed":false}')
        path.chmod(0o444)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        args['nominee_refs']['host_guard']['sha256'] = digest
        args['nominee']['host_guard_manifest_hash'] = digest
    elif mode == 'writable':
        path.chmod(0o644)
    elif mode == 'symlink':
        path.rename(args['config_root']/'other.json')
        path.symlink_to('other.json')
    elif mode == 'escape':
        args['nominee_refs']['host_guard']['path'] = '../outside.json'
    elif mode in {'wrong_k','wrong_scale','wrong_days'}:
        path = args['config_root']/'proofs/calibration.json'
        doc = json.loads(path.read_bytes())
        if mode == 'wrong_k':
            doc['k'] = 2
        else:
            doc['params']['alpha_scale' if mode == 'wrong_scale' else 'days'] = .5 if mode == 'wrong_scale' else 31
        doc.pop('proof_id')
        doc['proof_id'] = hashlib.sha256(S.canonical_json(doc)).hexdigest()
        path.chmod(0o644)
        path.write_bytes(S.canonical_json(doc))
        path.chmod(0o444)
        args['nominee_refs']['calibration']['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        args['nominee']['calibration_proof_id'] = doc['proof_id']
    elif mode == 'roster':
        conn.execute("UPDATE agents SET payload_json=json_set(payload_json,'$.model','changed')")
        conn.commit()
    else:
        args['nominee']['learning_rule'] = 'unmeasured'
    with pytest.raises(R.OrgRefused):
        R._open_evaluated(conn, {}, **args)
    assert conn.execute('SELECT count(*) FROM trial_registry').fetchone()[0] == 0


def test_unknown_clock_refuses_even_intact_nominee_verification(env):
    conn, args = env
    eid = R._open_evaluated(conn, {}, **args)
    with pytest.raises(R.OrgRefused, match='clock'):
        R.verify_nominee(conn, epoch_id=eid, config_root=args['config_root'], clock=lambda: None, launcher_policy=args['launcher_policy'], probe_write=args['probe_write'])


def test_memory_snapshot_read_is_bounded_before_loading(env, monkeypatch):
    from sniper.office import memory_store
    conn, args = env
    conn.execute('DROP TRIGGER memory_snapshots_no_update')
    conn.execute("UPDATE memory_snapshots SET payload_json=json_set(payload_json,'$.oversize',?)", ('x'*262145,))
    conn.commit()
    def must_not_load(*args):
        raise AssertionError('oversize_snapshot_loaded')
    monkeypatch.setattr(memory_store.OfficeMemory, 'get_snapshot', must_not_load)
    with pytest.raises(R.OrgRefused, match='snapshot'):
        R._open_evaluated(conn, {}, **args)


def test_roster_tamper_after_open_is_broken_and_idempotent(env):
    conn, args = env
    eid = R._open_evaluated(conn, {}, **args)
    conn.execute("UPDATE agents SET payload_json=json_set(payload_json,'$.model','changed')")
    conn.commit()
    for _ in range(2):
        with pytest.raises(R.OrgRefused, match='nominee_changed'):
            R.verify_nominee(conn, epoch_id=eid, config_root=args['config_root'], clock=args['clock'], launcher_policy=args['launcher_policy'], probe_write=args['probe_write'])
    assert conn.execute('SELECT count(*) FROM trial_registry').fetchone()[0] == 2


def test_concurrent_openers_serialize_alpha_allocation(env, monkeypatch, request):
    import concurrent.futures
    import sqlite3
    import threading
    conn, args = env
    second = dict(args)
    second['config_root'] = args['config_root'].parent/'config-second'
    second['nominee'], second['nominee_refs'] = build(conn, second['config_root'], k=2)
    from .launcher_fixture import LauncherFixture
    secondary = LauncherFixture(second['config_root'], second['clock'])
    request.addfinalizer(secondary.close)
    second.update(launcher_policy=secondary.policy, probe_write=secondary.probe)
    first_holds_lock, second_attempts = threading.Event(), threading.Event()
    original_append = S.append

    def append_and_close_fixture(db, table, payload):
        value = original_append(db, table, payload)
        if table == 'trial_registry' and payload.get('status') == 'RUNNING':
            # Fixture immediately completes each epoch, preserving the one-open-epoch gate.
            db.execute("UPDATE epochs SET payload_json=json_set(payload_json,'$.closed_at',1000.) WHERE id=?", (payload['epoch_id'],))
            if payload['k'] == 1:
                first_holds_lock.set()
                assert second_attempts.wait(2.)
        return value

    monkeypatch.setattr(S, 'append', append_and_close_fixture)
    path = conn.execute('PRAGMA database_list').fetchone()[2]

    def run(kw, is_second):
        db = sqlite3.connect(path, timeout=2.)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        if is_second:
            assert first_holds_lock.wait(2.)
            second_attempts.set()
        try:
            return R._open_evaluated(db, {}, **kw)
        finally:
            db.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(run, args, False), pool.submit(run, second, True)
        assert a.result() != b.result()
    trials = [json.loads(r[0]) for r in conn.execute('SELECT payload_json FROM trial_registry ORDER BY seq')]
    assert [row['k'] for row in trials] == [1, 2]
    assert [row['alpha_k'] for row in trials] == [.025, .05/6]


def test_caller_mutation_cannot_change_nominee_during_resolution(env, monkeypatch):
    from sniper.office import nominee as N
    conn, args = env
    original = N.resolve

    def concurrent_mutation(*pos, **kw):
        args['nominee']['delta1'] = 999.
        return original(*pos, **kw)

    monkeypatch.setattr(N, 'resolve', concurrent_mutation)
    eid = R._open_evaluated(conn, {}, **args)
    row = json.loads(conn.execute('SELECT payload_json FROM epochs WHERE id=?', (eid,)).fetchone()[0])
    assert row['nominee']['delta1'] == 2.5


def test_nominee_input_size_is_bounded_before_write_lock(env):
    conn, args = env
    args['nominee']['oversize'] = 'x'*65537
    with pytest.raises(R.OrgRefused, match='input_size'):
        R._open_evaluated(conn, {}, **args)
    assert not conn.in_transaction


def test_tool_contract_schema_change_refuses_the_nominee(env, monkeypatch):
    conn, args = env
    from sniper.office import runtime_steps
    monkeypatch.setattr(runtime_steps, 'SCHEMA', dict(runtime_steps.SCHEMA, title='changed'))
    with pytest.raises(R.OrgRefused, match='tool_contract_mismatch'):
        R._open_evaluated(conn, {}, **args)
    assert conn.execute('SELECT count(*) FROM trial_registry').fetchone()[0] == 0


@pytest.mark.parametrize('kind', ['pod_reallocation_discretionary', 'pod_reallocation_rule', 'restaff', 'hire', 'fire'])
def test_a_discretionary_pod_change_breaks_an_open_evaluated_epoch(env, kind):
    conn, args = env
    from pathlib import Path
    lim = json.loads((Path(__file__).resolve().parents[2]/'config'/'office'/'limits.json').read_text())
    eid = R._open_evaluated(conn, {}, **args)
    R.change_org(conn, lim, clock=lambda: 1001., change={'kind': kind, 'detail': 'x'}, by='crew', reason='discretionary')
    epoch = json.loads(conn.execute('SELECT payload_json FROM epochs WHERE id=?', (eid,)).fetchone()[0])
    assert epoch['status'] == 'broken'
    assert [json.loads(r[0])['status'] for r in conn.execute('SELECT payload_json FROM trial_registry ORDER BY seq')] == ['RUNNING', 'BROKEN']
