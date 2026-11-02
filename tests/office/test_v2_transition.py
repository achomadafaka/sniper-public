"""Office v2 transition on synthetic stores: close eval-1 as STOPPED_BY_OPERATOR, then open office-v2-proof-1. Never live."""
import json
import sqlite3

import pytest

from sniper.office import seat_events as SE, store as S, v2_transition as V
from .test_eval_transition import disc2, go, BP, CO  # noqa: F401
from .test_epoch_transition import prepared  # noqa: F401
from .test_pilot_seed import env  # noqa: F401

NOW = V.STOP_AT + 600.


@pytest.fixture
def eval1(disc2):
    assert go(disc2, apply=True)['state'] == 'transitioned'
    return disc2[0]


def _epoch(conn, eid):
    row = conn.execute('SELECT payload_json FROM epochs WHERE id=?', (eid,)).fetchone()
    return None if row is None else json.loads(row[0])


def _control(conn):
    return json.loads(conn.execute('SELECT payload_json FROM office_epoch_control WHERE id=1').fetchone()[0])


def _v2_tables(conn):
    return [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'office_v2%'")]


def _org(conn): return [json.loads(r[0]) for r in conn.execute('SELECT payload_json FROM org_changes ORDER BY seq')]


def test_stop_time_is_the_recorded_operator_stop():
    assert V.STOP_AT == 1793353688.0       # 2026-10-30T09:48:08Z


def test_dry_run_writes_nothing_even_the_v2_tables(eval1):
    changes, seq = eval1.total_changes, len(_org(eval1))
    assert V.transition(eval1, now=NOW)['state'] == 'dry_run'
    assert eval1.total_changes == changes and len(_org(eval1)) == seq and _v2_tables(eval1) == []
    assert _epoch(eval1, V.PRED)['closed_at'] is None and _control(eval1)['active_epoch'] == V.PRED


def test_apply_closes_eval1_then_opens_proof_epoch_with_one_headless_actor(eval1):
    assert V.transition(eval1, now=NOW, apply=True)['state'] == 'transitioned'
    old = _epoch(eval1, V.PRED)
    assert old['closed_at'] == V.STOP_AT and old['status'] == 'STOPPED_BY_OPERATOR' and old['first_order_at'] is None
    new = _epoch(eval1, V.NEW)
    assert new['kind'] == 'v2_proof' and new['book_id'] is None and new['ledger_epoch_id'] is None
    assert new['evaluation_allowed'] is False and new['order_capable'] is False and new['closed_at'] is None
    assert new['actor'] == {'actor_id': V.PROOF_ACTOR_ID, 'role': 'head', 'family': 'claude', 'model': 'claude-opus-5-5'}
    assert _control(eval1)['active_epoch'] == V.NEW
    roster = [tuple(r) for r in eval1.execute('SELECT actor_id,epoch_id,role,generation,incarnation,sealed FROM office_v2_roster')]
    assert roster == [(V.PROOF_ACTOR_ID, V.NEW, 'head', 1, 1, 0)]
    kinds = [e['kind'] for e in _org(eval1)]
    assert kinds.count('office_eval1_stopped_by_operator') == 1 and kinds.count('office_v2_epoch_open') == 1
    assert kinds.index('office_eval1_stopped_by_operator') < kinds.index('office_v2_epoch_open')
    assert S.verify_chain(eval1, 'org_changes')[0]
    assert V.PROOF_ACTOR_ID not in {r[0] for r in eval1.execute('SELECT id FROM agents')}      # v1 roster untouched


def test_no_v2_write_lands_before_eval1_is_closed(eval1):
    seen = []
    eval1.set_trace_callback(seen.append)
    V.transition(eval1, now=NOW, apply=True)
    eval1.set_trace_callback(None)
    close = next(i for i, s in enumerate(seen) if s.startswith('UPDATE epochs') and 'STOPPED_BY_OPERATOR' in s.replace("'", ''))
    v2 = [i for i, s in enumerate(seen) if 'office_v2' in s and not s.startswith(('SELECT', 'PRAGMA'))]
    assert v2 and min(v2) > close
    # the stop commits on its own: the close is durable before the first v2 statement runs
    commits = [i for i, s in enumerate(seen) if s == 'COMMIT']
    assert any(close < c < min(v2) for c in commits)


@pytest.mark.parametrize('why', ['has_orders', 'not_active', 'lease_live', 'stop_in_future', 'empty_model'])
def test_refusals_leave_no_v2_write_and_eval1_open(eval1, why):
    kw = {}
    if why == 'has_orders':
        eval1.execute('UPDATE epochs SET payload_json=json_set(payload_json,"$.first_order_at",1300.) WHERE id=?', (V.PRED,)); eval1.commit()
    elif why == 'not_active':
        eval1.execute('UPDATE office_epoch_control SET payload_json=json_set(payload_json,"$.active_epoch","office-pilot-discovery-2")'); eval1.commit()
    elif why == 'lease_live':
        eval1.execute('UPDATE supervisor_lease SET expires_at=?', (NOW + 100,)); eval1.commit()
    elif why == 'stop_in_future':
        kw['now'] = V.STOP_AT - 1
    elif why == 'empty_model':
        kw['model'] = ' '
    seen = []
    eval1.set_trace_callback(seen.append)
    with pytest.raises(ValueError, match='^(v2_|epoch_)'):
        V.transition(eval1, **{'now': NOW, 'apply': True, **kw})
    eval1.set_trace_callback(None)
    assert _v2_tables(eval1) == [] and _epoch(eval1, V.NEW) is None
    assert not any(s.startswith(('INSERT', 'UPDATE', 'CREATE')) for s in seen)
    if why != 'not_active': assert _epoch(eval1, V.PRED)['closed_at'] is None


def test_repeat_is_idempotent_and_changed_approval_is_refused(eval1):
    assert V.transition(eval1, now=NOW, apply=True)['state'] == 'transitioned'
    seq, changes = len(_org(eval1)), eval1.total_changes
    assert V.transition(eval1, now=NOW + 5, apply=True)['state'] == 'already_applied'
    assert len(_org(eval1)) == seq and eval1.total_changes == changes
    with pytest.raises(ValueError, match='v2_approval_changed'):
        V.transition(eval1, now=NOW + 6, apply=True, model='claude-sonnet-5-5')


def test_half_applied_stage_two_crash_is_completed_by_a_rerun(eval1, monkeypatch):
    real = SE.ensure_schema
    def boom(conn): raise sqlite3.OperationalError('disk I/O error')
    monkeypatch.setattr(SE, 'ensure_schema', boom)
    with pytest.raises(sqlite3.OperationalError):
        V.transition(eval1, now=NOW, apply=True)
    assert _epoch(eval1, V.PRED)['closed_at'] == V.STOP_AT and _control(eval1)['active_epoch'] == V.PRED
    assert _v2_tables(eval1) == [] and _epoch(eval1, V.NEW) is None
    monkeypatch.setattr(SE, 'ensure_schema', real)
    assert V.transition(eval1, now=NOW + 9, apply=True)['state'] == 'transitioned'
    assert V.transition(eval1, now=NOW + 9, apply=True)['state'] == 'already_applied'
    assert [e['kind'] for e in _org(eval1)].count('office_eval1_stopped_by_operator') == 1
    assert _epoch(eval1, V.PRED)['closed_at'] == V.STOP_AT


def test_closed_eval1_without_our_stop_receipt_is_refused(eval1):
    eval1.execute('UPDATE epochs SET payload_json=json_set(payload_json,"$.closed_at",1500.) WHERE id=?', (V.PRED,)); eval1.commit()
    with pytest.raises(ValueError, match='v2_eval1_closed_unexpectedly'):
        V.transition(eval1, now=NOW, apply=True)
    assert _v2_tables(eval1) == []


def test_roster_conflict_is_refused_not_overwritten(eval1):
    SE.ensure_schema(eval1)
    eval1.execute("INSERT INTO office_v2_roster VALUES(?,?,?,?,?,?,?)", (V.PROOF_ACTOR_ID, 'other-epoch', 'head', 3, 7, 0, 1.)); eval1.commit()
    with pytest.raises(ValueError, match='v2_roster_conflict'):
        V.transition(eval1, now=NOW, apply=True)
    assert [tuple(r) for r in eval1.execute('SELECT epoch_id,generation,incarnation FROM office_v2_roster')] == [('other-epoch', 3, 7)]
    assert _epoch(eval1, V.NEW) is None


def test_cli_dry_run_is_readonly_then_apply_then_idempotent(eval1, tmp_path, capsys, monkeypatch):
    import importlib.util
    from pathlib import Path
    path = Path(eval1.execute('PRAGMA database_list').fetchone()[2])
    spec = importlib.util.spec_from_file_location('fixture_v2_cli', Path(__file__).resolve().parents[2] / 'scripts/office_v2_transition.py')
    cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)
    monkeypatch.setattr(cli, 'approved_path', lambda p: p)
    argv = ['--store', str(path), '--clock-file', str(tmp_path / 'unused')]
    before = path.read_bytes()
    assert cli.main(argv, clock=lambda: NOW) == 0 and json.loads(capsys.readouterr().out)['state'] == 'dry_run'
    assert path.read_bytes() == before
    assert cli.main(argv + ['--apply'], clock=lambda: NOW) == 0 and json.loads(capsys.readouterr().out)['state'] == 'transitioned'
    assert cli.main(argv + ['--apply'], clock=lambda: NOW + 1) == 0 and json.loads(capsys.readouterr().out)['state'] == 'already_applied'
    assert cli.main(argv + ['--apply', '--model', 'claude-sonnet-5-5'], clock=lambda: NOW + 2) == 2
    assert json.loads(capsys.readouterr().err)['reason'] == 'v2_approval_changed'
