"""Office v2 explore-1 transition on synthetic stores: seal the proof epoch, open office-v2-explore-1, change log and segments, ledger book. Never live."""
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from sniper.office import ledger as L, seat_events as SE, store as S, v2_explore as X, v2_transition as V
from .test_eval_transition import disc2, go  # noqa: F401
from .test_epoch_transition import prepared  # noqa: F401
from .test_pilot_seed import env  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
POLICY = json.loads((ROOT / 'config/office/book_policy_pilot.json').read_text())['policy']
SEATS = {'head': {'family': 'claude', 'model': 'claude-opus-5-5'}, 'judge': {'family': 'claude', 'model': 'claude-opus-5-5'},
         'scout': {'family': 'claude', 'model': 'claude-sonnet-5-5'}}
SHA = 'a' * 40
H1, H2 = 'b' * 64, 'c' * 64
NOW = V.STOP_AT + 3600.


@pytest.fixture
def proof(disc2):
    assert go(disc2, apply=True)['state'] == 'transitioned'
    conn = disc2[0]
    assert V.transition(conn, now=V.STOP_AT + 600., apply=True)['state'] == 'transitioned'
    return conn


def run(conn, **kw): return X.transition(conn, **{'now': NOW, 'seats': SEATS, 'book_policy': POLICY, 'release_sha': SHA, **kw})


def _epoch(conn, eid):
    row = conn.execute('SELECT payload_json FROM epochs WHERE id=?', (eid,)).fetchone()
    return None if row is None else json.loads(row[0])


def _control(conn): return json.loads(conn.execute('SELECT payload_json FROM office_epoch_control WHERE id=1').fetchone()[0])
def _org(conn): return [json.loads(r[0]) for r in conn.execute('SELECT payload_json FROM org_changes ORDER BY seq')]
def _tables(conn): return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'office_v2%'")}


@pytest.fixture
def ledger(tmp_path):
    conn = L.open_ledger(tmp_path / 'ledger.db')
    yield conn
    conn.close()


def test_record_hash_is_the_epoch_record_file():
    path = ROOT / X.RECORD
    if not path.exists(): pytest.skip('record not on this branch')
    assert hashlib.sha256(path.read_bytes()).hexdigest() == X.RECORD_SHA256


def test_dry_run_writes_nothing(proof):
    before, seq = proof.total_changes, len(_org(proof))
    assert run(proof)['state'] == 'dry_run'
    assert proof.total_changes == before and len(_org(proof)) == seq and 'office_v2_changes' not in _tables(proof)
    assert _epoch(proof, X.PRED)['closed_at'] is None and _control(proof)['active_epoch'] == X.PRED


def test_apply_seals_proof_then_opens_explore_with_three_seats_and_segment_one(proof):
    assert run(proof, apply=True)['state'] == 'transitioned'
    old = _epoch(proof, X.PRED)
    assert old['closed_at'] == NOW and old['status'] == 'SEALED'
    new = _epoch(proof, X.NEW)
    assert (new['kind'], new['exploratory'], new['order_capable'], new['evaluation_allowed'], new['book_id'], new['ledger_epoch_id']) == \
        ('v2_explore', True, True, False, X.NEW, X.NEW)
    assert new['record_sha256'] == X.RECORD_SHA256 and new['book'] == X._book_terms()
    assert _control(proof)['active_epoch'] == X.NEW
    roster = {r[0]: tuple(r[1:]) for r in proof.execute('SELECT actor_id,epoch_id,role,generation,incarnation,sealed FROM office_v2_roster')}
    assert roster[V.PROOF_ACTOR_ID] == (X.PRED, 'head', 1, 1, 1)                                  # proof Head sealed
    assert {k: v for k, v in roster.items() if v[0] == X.NEW} == {X.actor_id(r): (X.NEW, r, 1, 1, 0) for r in X.ROLES}
    assert len({X.actor_id(r) for r in X.ROLES}) == 3 and V.PROOF_ACTOR_ID not in {X.actor_id(r) for r in X.ROLES}
    assert X.segment_id(proof, X.NEW) == 1
    assert {'office_v2_candidates', 'office_v2_halts', 'office_v2_order_labels', 'office_v2_changes'} <= _tables(proof)
    kinds = [e['kind'] for e in _org(proof)]
    assert kinds.index(X.SEAL_KIND) < kinds.index(X.OPEN_KIND) and S.verify_chain(proof, 'org_changes')[0]


def test_seal_commits_before_the_first_explore_write(proof):
    seen = []
    proof.set_trace_callback(seen.append)
    run(proof, apply=True)
    proof.set_trace_callback(None)
    seal = next(i for i, s in enumerate(seen) if s.startswith('UPDATE epochs') and 'SEALED' in s)
    writes = [i for i, s in enumerate(seen) if s.startswith(('INSERT INTO office_v2', 'INSERT OR IGNORE INTO office_v2', 'CREATE TABLE IF NOT EXISTS office_v2'))]
    assert writes and any(seal < i < min(writes) for i, s in enumerate(seen) if s == 'COMMIT')


@pytest.mark.parametrize('why', ['lease_live', 'not_active', 'bad_seats', 'judge_not_claude', 'bad_release', 'foreign_close'])
def test_refusals_write_nothing(proof, why):
    kw = {}
    if why == 'lease_live': proof.execute('UPDATE supervisor_lease SET expires_at=?', (NOW + 100,)); proof.commit()
    elif why == 'not_active': proof.execute('UPDATE office_epoch_control SET payload_json=json_set(payload_json,"$.active_epoch","other")'); proof.commit()
    elif why == 'bad_seats': kw['seats'] = {'head': SEATS['head']}
    elif why == 'judge_not_claude': kw['seats'] = dict(SEATS, judge={'family': 'gpt', 'model': 'gpt-6'})
    elif why == 'bad_release': kw['release_sha'] = 'xyz'
    elif why == 'foreign_close':
        proof.execute('UPDATE epochs SET payload_json=json_set(payload_json,"$.closed_at",1500.) WHERE id=?', (X.PRED,)); proof.commit()
    seen = []
    proof.set_trace_callback(seen.append)
    with pytest.raises(ValueError, match='^(explore_|v2_|epoch_)'):
        run(proof, apply=True, **kw)
    proof.set_trace_callback(None)
    assert _epoch(proof, X.NEW) is None and not any(s.startswith(('INSERT', 'UPDATE', 'CREATE')) for s in seen)


def test_repeat_is_idempotent_and_a_changed_approval_is_refused(proof):
    assert run(proof, apply=True)['state'] == 'transitioned'
    seq, changes = len(_org(proof)), proof.total_changes
    assert run(proof, apply=True, now=NOW + 5)['state'] == 'already_applied'
    assert len(_org(proof)) == seq and proof.total_changes == changes
    with pytest.raises(ValueError, match='explore_approval_changed'):
        run(proof, apply=True, release_sha='d' * 40)
    with pytest.raises(ValueError, match='explore_approval_changed'):
        run(proof, apply=True, seats=dict(SEATS, scout={'family': 'claude', 'model': 'claude-opus-5-5'}))


def test_crash_after_seal_is_completed_by_a_rerun(proof, monkeypatch):
    real = SE.ensure_schema
    def boom(conn): raise sqlite3.OperationalError('disk I/O error')
    monkeypatch.setattr(SE, 'ensure_schema', boom)
    with pytest.raises(sqlite3.OperationalError): run(proof, apply=True)
    assert _epoch(proof, X.PRED)['status'] == 'SEALED' and _control(proof)['active_epoch'] == X.PRED and _epoch(proof, X.NEW) is None
    monkeypatch.setattr(SE, 'ensure_schema', real)
    assert run(proof, apply=True, now=NOW + 9)['state'] == 'transitioned'
    assert run(proof, apply=True, now=NOW + 9)['state'] == 'already_applied'
    assert [e['kind'] for e in _org(proof)].count(X.SEAL_KIND) == 1 and _epoch(proof, X.PRED)['closed_at'] == NOW


# ---- change log ---------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def opened(proof):
    run(proof, apply=True)
    return proof


def test_a_change_starts_a_new_segment_and_a_retry_does_not(opened):
    assert X.record_change(opened, epoch_id=X.NEW, at=NOW + 10, release_sha=SHA, content_hash=H1, kind='tool_set') == 2
    assert X.record_change(opened, epoch_id=X.NEW, at=NOW + 11, release_sha=SHA, content_hash=H1, kind='tool_set') == 2
    assert X.record_change(opened, epoch_id=X.NEW, at=NOW + 12, release_sha=SHA, content_hash=H2, kind='prompt') == 3
    assert X.segment_id(opened, X.NEW) == 3


def test_an_unlogged_hash_is_refused_until_entered(opened):
    with pytest.raises(ValueError, match='change_unlogged'): X.require_logged(opened, epoch_id=X.NEW, kind='tool_set', content_hash=H1)
    X.record_change(opened, epoch_id=X.NEW, at=NOW + 10, release_sha=SHA, content_hash=H1, kind='tool_set')
    X.require_logged(opened, epoch_id=X.NEW, kind='tool_set', content_hash=H1)
    with pytest.raises(ValueError, match='change_unlogged'): X.require_logged(opened, epoch_id=X.NEW, kind='tool_set', content_hash=H2)


def test_change_log_is_append_only(opened):
    for sql in ('UPDATE office_v2_changes SET kind="prompt"', 'DELETE FROM office_v2_changes'):
        with pytest.raises(sqlite3.DatabaseError, match='change_log_append_only'): opened.execute(sql)


@pytest.mark.parametrize('kw', [dict(kind='epoch_open'), dict(kind='bogus'), dict(release_sha='abc'), dict(content_hash='zz'), dict(at=float('nan')),
                                dict(at=NOW - 1.), dict(note='x' * 501)])
def test_invalid_changes_are_refused(opened, kw):
    with pytest.raises(ValueError, match='change_'):
        X.record_change(opened, **{'epoch_id': X.NEW, 'at': NOW + 10, 'release_sha': SHA, 'content_hash': H1, 'kind': 'tool_set', **kw})
    assert X.segment_id(opened, X.NEW) == 1


def test_no_segment_without_an_open_explore_epoch(proof):
    with pytest.raises(ValueError, match='segment_unknown'): X.segment_id(proof, X.NEW)
    with pytest.raises(ValueError, match='change_epoch_not_open'):
        X.record_change(proof, epoch_id=X.NEW, at=NOW, release_sha=SHA, content_hash=H1, kind='tool_set')


# ---- ledger stage -------------------------------------------------------------------------------------------------------------------

def test_ledger_stage_is_idempotent_and_binds_one_initial_fund(ledger):
    assert X.book_bound(ledger) == 'explore_book_unbound'
    assert X.provision_ledger(ledger, now=NOW) is None
    assert X.provision_ledger(ledger, now=NOW + 5) is None
    assert [tuple(r) for r in ledger.execute("SELECT kind,usd FROM capital_flows WHERE book_id=?", (X.BOOK,))] == [('initial', 1000.)]
    assert tuple(ledger.execute('SELECT clip_usd FROM ledger_epochs WHERE epoch_id=?', (X.NEW,)).fetchone()) == (25.,)


def test_half_applied_ledger_stage_refuses_to_trade_then_completes(opened, ledger, monkeypatch):
    def boom(*a, **k): raise sqlite3.OperationalError('disk I/O error')
    monkeypatch.setattr(L, 'fund', boom)
    with pytest.raises(sqlite3.OperationalError): X.provision_ledger(ledger, now=NOW)
    assert X.ready(opened, ledger) == 'explore_book_unbound'
    monkeypatch.undo()
    assert X.provision_ledger(ledger, now=NOW + 1) is None and X.ready(opened, ledger) is None
    assert [tuple(r)[1:5] for r in ledger.execute('SELECT * FROM capital_flows WHERE book_id=?', (X.BOOK,))] == [(X.BOOK, NOW + 1, 'initial', 1000.)]


def test_ready_needs_the_epoch_and_segment_one(proof, ledger):
    X.provision_ledger(ledger, now=NOW)
    assert X.ready(proof, ledger) == 'explore_epoch_not_active'


def test_a_second_or_wrong_fund_is_a_mismatch_not_ready(opened, ledger):
    X.provision_ledger(ledger, now=NOW)
    L.fund(ledger, book_id=X.BOOK, usd=500., kind='deposit', actor='x', note='x', now=NOW + 1)
    assert X.ready(opened, ledger) == 'explore_book_mismatch'


def test_kill_rule_is_enforced_for_the_explore_epoch_not_silently_off(ledger):
    """evaluation_safety must manage this epoch (state != not_configured): otherwise the 200 USD drawdown kill never runs."""
    X.provision_ledger(ledger, now=NOW)
    assert L.evaluation_safety(ledger, epoch_id=X.NEW, now=NOW + 1)['state'] != 'not_configured'
    assert L._evaluation_config(ledger, X.NEW)[1]['drawdown_usd'] == 200.


def test_cli_missing_seat_config_is_an_error_main_turns_into_a_refusal(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location('cli', ROOT / 'scripts/office_v2_explore_transition.py')
    cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)
    with pytest.raises(OSError): cli._seats(tmp_path)     # main catches OSError and prints explore_transition_input_unknown


def test_override_without_a_reference_is_refused():
    import pytest as P
    from sniper.office import v2_explore as X
    with P.raises(ValueError,match='override_needs_reference'):
        X.record_change(None,epoch_id=X.NEW,at=1.,release_sha='a'*40,content_hash='b'*64,kind='override',note=' ')


def _start(conn,seat,*,prompt=None,release=SHA):
    """What a seat host logs at start, in its order."""
    X.record_change(conn,epoch_id=X.NEW,at=NOW+50,release_sha=release,content_hash=hashlib.sha256(release.encode()).hexdigest(),kind='release')
    for kind,value in (('tool_set','tools'),('prompt',prompt or seat+'-prompt'),('model','m'),('config','c')):
        X.record_change(conn,epoch_id=X.NEW,at=NOW+50,release_sha=release,content_hash=hashlib.sha256(value.encode()).hexdigest(),kind=kind,note=seat)


def _count(conn):return conn.execute('SELECT COUNT(*) FROM office_v2_changes').fetchone()[0]


def test_restarting_seats_with_nothing_changed_opens_no_segment(opened):
    for seat in ('head','judge','scout'):_start(opened,seat)
    before=_count(opened)
    for seat in ('head','head','judge','scout'):_start(opened,seat)
    assert _count(opened)==before


def test_one_prompt_change_is_one_segment_and_a_new_release_is_one_segment(opened):
    for seat in ('head','judge','scout'):_start(opened,seat)
    before=_count(opened)
    _start(opened,'judge',prompt='new prompt')
    assert _count(opened)==before+1
    other='c'*40
    for seat in ('head','judge','scout'):_start(opened,seat,prompt='new prompt' if seat=='judge' else None,release=other)
    assert _count(opened)==before+2
    assert opened.execute("SELECT kind FROM office_v2_changes ORDER BY segment DESC LIMIT 1").fetchone()[0]=='release'
