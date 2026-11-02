"""Training is discovery-only and has no production/evaluation ledger handles."""
import ast
import hashlib
import inspect
import json

import pytest

from sniper.office import training as T, training_store as TS, memory as M, memory_store as MS, store as S
from sniper.office.models import Envelope
from sniper.office.scheduler import Scheduler


SYSTEM = 'Trusted fixture scout persona and policy.'
ACTOR = dict(agent_id='scout', role='scout', generation=1, provider='codex', model='fixture-model',
             system=SYSTEM, prompt_hash=hashlib.sha256(SYSTEM.encode()).hexdigest())


def frame(at=100., known_at=99.):
    return dict(at=at, cut=dict(basis='corrected_utc', receipt_cut=at, knowledge_cut=at, slot_cut=None, clock_sample=dict(corrected_utc=at, offset_s=0., mode='replay')), rows=[dict(known_at=known_at, source='archive:fixture', text='past flow')])


@pytest.fixture
def env(tmp_path):
    shadow = TS.ReplayShadow(tmp_path/'training_shadow.db')
    calls, counters = [], []
    backlog = {'live': False, 'd_judge': False}

    def dispatch(provider, **kw):
        calls.append(dict(kw, provider=provider))
        return Envelope(requested_provider='codex', requested_model='fixture-model', outcome='success', text=json.dumps({'lesson':'Lesson from the archived flow.'}))

    floor = T.ReplayFloor(shadow, clock=lambda: 1000., dispatch=dispatch,
                          backlog=lambda: dict(backlog), capacity=lambda: True,
                          record_counter=lambda name: counters.append(name) or True,
                          scheduler=Scheduler({'codex':1}, 1))
    yield floor, shadow, calls, counters, backlog
    shadow.close()


def run(env, frames=None):
    return env[0].run([frame()] if frames is None else frames, actors=[ACTOR], run_id='fixture-run', archive_before=900.)


def test_future_row_refused_without_changing_earlier_sheet(env):
    earlier = S.canonical_json(frame())
    result = run(env, [frame(), frame(110., 111.)])
    assert result['state'] == 'refused' and result['reason'] == 'future_knowledge'
    assert not env[2]
    assert S.canonical_json(frame()) == earlier
    assert not env[1].entries()


def test_lessons_stay_shadow_and_cannot_enter_existing_epoch_snapshot(env, tmp_path):
    live = S.open_store(tmp_path/'office.db')
    memory = MS.OfficeMemory(live)
    before = M.seal_snapshot(memory, epoch_id='live', cut=1000.)
    result = run(env)
    assert result['state'] == 'completed'
    assert not M.retrieve(memory, '', decision_cut=2000., candidate={}, viewer_role='Scout')
    assert M.seal_snapshot(memory, epoch_id='live', cut=1000.) == before
    lesson = env[1].entries()[0]
    assert lesson['known_at'] == 1000. and lesson['replay_at'] == 100.
    assert lesson['epoch_kind'] == 'discovery' and lesson['counts_as_evidence'] is False
    live.close()


def test_protected_stores_byte_identical_and_grader_not_called(env, tmp_path, monkeypatch):
    from sniper.office import grader, ledger
    protected = [S.open_store(tmp_path/'office.db'), S.open_store(tmp_path/'evaluation.db', evaluation=True)]
    for name in ('evaluated_ledger.db', 'null_ledger.db'):
        conn = ledger.open_ledger(tmp_path/name)
        ledger.open_epoch(conn, epoch_id='fixture', clip_usd=25., rule_version='fixture', actor='fixture', note='fixture', now=99.)
        ledger.open_book(conn, book_id='fixture', epoch_id='fixture', kind='evaluated', actor='fixture', note='fixture', now=99.)
        protected.append(conn)
    def hashes():
        return {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.iterdir()
                if p.is_file() and not p.name.startswith('training_shadow')}
    before = hashes()
    monkeypatch.setattr(grader, 'grade_epoch', lambda *a, **k: pytest.fail('replay reached grader'))
    run(env)
    assert hashes() == before
    for conn in protected:
        conn.close()


@pytest.mark.parametrize('lane', ['live','d_judge'])
def test_replay_yields_without_dispatch_when_live_or_judge_backlogged(env, lane):
    env[4][lane] = True
    result = run(env)
    assert result['state'] == 'paused' and not env[2] and not env[3]


def test_inflight_backlog_counts_once_pauses_and_discards_output(env):
    floor, shadow, calls, counters, backlog = env
    def dispatch(provider, **kw):
        calls.append(dict(kw, provider=provider))
        backlog['live'] = True
        return Envelope(requested_provider='codex', requested_model='fixture-model', outcome='success', text=json.dumps({'lesson':'must not persist'}))
    floor.dispatch = dispatch
    result = run(env)
    assert result['state'] == 'paused'
    assert counters == ['replay_while_live_backlogged']
    assert not shadow.entries()


def test_all_outputs_and_dispatch_are_discovery_lowest_priority(env):
    result = run(env)
    assert result['epoch_kind'] == 'discovery' and result['counts_as_evidence'] is False
    assert env[2][0]['priority'] == 'replay'
    request = json.loads(env[2][0]['user'])
    assert request['context']['book_id'] == 'training-shadow'
    assert request['inputs'][0]['type'] == 'quoted_data'
    assert all(row['epoch_kind'] == 'discovery' and row['counts_as_evidence'] is False for row in result['outputs'])


def test_driver_has_no_evaluated_or_ledger_authority():
    tree = ast.parse(inspect.getsource(T))
    forbidden = {'roster','ledger','grader','open_evaluated_epoch','_open_evaluated','trial_registry','fund'}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not any(part in forbidden or part == 'training_promote' for alias in node.names for part in alias.name.split('.'))
        if isinstance(node, ast.ImportFrom):
            assert not forbidden.intersection(alias.name for alias in node.names)
            assert not any(alias.name == 'training_promote' for alias in node.names)
        if isinstance(node, ast.Attribute):
            assert node.attr not in forbidden
    assert 'training_promote' not in inspect.getsource(TS)


def test_retry_is_cached_without_another_model_call(env):
    first = run(env)
    assert run(env) == first
    assert len(env[2]) == 1 and len(env[1].entries()) == 1


def test_shadow_refuses_existing_office_store_without_mutation(tmp_path):
    path = tmp_path/'training_shadow.db'
    conn = S.open_store(path)
    conn.close()
    before = path.read_bytes()
    with pytest.raises(TS.ShadowRefused):
        TS.ReplayShadow(path)
    assert path.read_bytes() == before


def test_archive_bounds_and_unknown_capacity_refuse_before_dispatch(env):
    assert run(env, [frame()]*65)['state'] == 'refused'
    env[0].capacity = lambda: None
    assert run(env)['state'] == 'paused'
    assert not env[2]


def test_dispatch_failure_still_records_inflight_backlog(env):
    def dispatch(provider, **kw):
        env[4]['d_judge'] = True
        raise TimeoutError()
    env[0].dispatch = dispatch
    assert run(env)['state'] == 'paused'
    assert env[3] == ['replay_while_live_backlogged']
    assert not env[1].entries()


def test_lesson_clock_cannot_backdate_into_archived_time(env):
    clock = [1000.]
    env[0].clock = lambda: clock[0]
    original = env[0].dispatch
    def dispatch(provider, **kw):
        answer = original(provider, **kw)
        clock[0] = 50.
        return answer
    env[0].dispatch = dispatch
    assert run(env)['state'] != 'completed'
    assert not env[1].entries()


def test_promotion_is_explicit_atomic_and_opens_new_discovery_epoch(env, tmp_path):
    from sniper.office.training_promote import promote_lessons
    run(env)
    live = S.open_store(tmp_path/'office.db')
    memory = MS.OfficeMemory(live)
    prior = M.seal_snapshot(memory, epoch_id='old', cut=1000.)
    clock = [1999.]
    def now():
        clock[0] += 1.
        return clock[0]
    eid = promote_lessons(env[1], live, entry_ids=[env[1].entries()[0]['entry_id']],
                          clock=now, limits={'material_changes':['memory_promotion']},
                          by='crew', launcher_check=lambda who: who == 'crew')
    epoch = json.loads(live.execute('SELECT payload_json FROM epochs WHERE id=?', (eid,)).fetchone()[0])
    assert epoch['kind'] == 'discovery'
    assert memory.entries()[0]['known_at'] < epoch['opened_at']
    assert M.retrieve(memory, '', decision_cut=3000., candidate={}, viewer_role='Scout')
    assert M.seal_snapshot(memory, epoch_id='old', cut=1000.) == prior
    assert live.execute('SELECT count(*) FROM trial_registry').fetchone()[0] == 0
    live.close()


def test_actor_prompt_identity_cannot_drift(env):
    actor = dict(ACTOR, system='changed unpinned prompt')
    result = env[0].run([frame()], actors=[actor], run_id='wrong-prompt', archive_before=900.)
    assert result['state'] == 'refused' and not env[2]


def test_promotion_failure_rolls_back_memory_and_epoch(env, tmp_path):
    from sniper.office.training_promote import promote_lessons
    run(env)
    live = S.open_store(tmp_path/'office.db')
    identity = env[1].entries()[0]['entry_id']
    for launcher, clock in [(lambda who:False, lambda:2000.), (lambda who:True, lambda:2000.)]:
        with pytest.raises(TS.ShadowRefused):
            promote_lessons(env[1], live, entry_ids=[identity], clock=clock,
                limits={'material_changes':['memory_promotion']}, by='crew', launcher_check=launcher)
        assert live.execute('SELECT count(*) FROM memory_entries').fetchone()[0] == 0
        assert live.execute('SELECT count(*) FROM epochs').fetchone()[0] == 0
    live.close()


@pytest.mark.parametrize('change', ['offset','clock','slot'])
def test_shared_cut_wrapper_refuses_invalid_replay_cuts(env, change):
    f = frame()
    if change == 'offset':
        f['cut']['clock_sample']['offset_s'] = None
    elif change == 'clock':
        f['cut']['clock_sample']['corrected_utc'] = 99.
    else:
        f['cut']['slot_cut'] = -1
    assert run(env, [f])['state'] == 'refused'
    assert not env[2]


def test_dispatch_cannot_mutate_shared_schema(env):
    original = env[0].dispatch
    before = json.dumps(T.SCHEMA, sort_keys=True)
    def dispatch(provider, **kw):
        kw['schema']['required'].append('foreign')
        return original(provider, **kw)
    env[0].dispatch = dispatch
    assert run(env)['state'] == 'completed'
    assert json.dumps(T.SCHEMA, sort_keys=True) == before


def test_promotion_rejects_evaluation_or_shadow_target_before_write(env, tmp_path):
    from sniper.office.training_promote import promote_lessons
    run(env)
    evaluation = S.open_store(tmp_path/'evaluation.db', evaluation=True)
    with pytest.raises(TS.ShadowRefused, match='target'):
        promote_lessons(env[1], evaluation, entry_ids=[env[1].entries()[0]['entry_id']],
            clock=lambda:2000., limits={'material_changes':['memory_promotion']},
            by='crew', launcher_check=lambda who:True)
    assert evaluation.execute('SELECT count(*) FROM grades').fetchone()[0] == 0
    evaluation.close()


def test_replay_dispatch_uses_real_scheduler_priority(env, monkeypatch):
    from sniper.office.scheduler import Scheduler
    scheduler = Scheduler({'codex':1}, 1)
    seen = []
    original = scheduler.submit
    def submit(*args, **kw):
        seen.append(kw['priority'])
        return original(*args, **kw)
    monkeypatch.setattr(scheduler, 'submit', submit)
    env[0].scheduler = scheduler
    assert run(env)['state'] == 'completed'
    assert seen == ['replay']


def test_scheduler_live_backlog_seam_prevents_dispatch(env, monkeypatch):
    from sniper.office.scheduler import Scheduler
    scheduler = Scheduler({'codex':1}, 1)
    monkeypatch.setattr(scheduler, 'live_backlogged', lambda: True)
    env[0].scheduler = scheduler
    assert run(env)['state'] == 'paused'
    assert not env[2]


def test_archive_cannot_cross_oldest_open_or_planned_epoch_start(env):
    result = env[0].run([frame(100.,99.)], actors=[ACTOR], run_id='future-epoch', archive_before=100.)
    assert result['state'] == 'refused' and result['reason'] == 'archive_crosses_epoch_start'
    assert not env[2]


def test_real_scheduler_refusal_never_counts_as_replay_ran(env):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor
    floor,_,calls,counters,_=env
    scheduler=Scheduler({'codex':1},1);floor.scheduler=scheduler
    entered=threading.Event();release=threading.Event();queued=[]
    def done():return Envelope(requested_provider='codex',requested_model='fixture-model',outcome='success')
    def busy():
        entered.set();assert release.wait(3)
        return done()
    with ThreadPoolExecutor(max_workers=2) as pool:
        active=pool.submit(scheduler.submit,'codex',busy)
        try:
            assert entered.wait(1)
            def capacity():
                queued.append(pool.submit(scheduler.submit,'codex',done,timeout_s=2))
                deadline=time.monotonic()+1
                while not scheduler.live_backlogged() and time.monotonic()<deadline:time.sleep(.005)
                assert scheduler.live_backlogged()
                return True
            floor.capacity=capacity
            result=run(env)
            assert result['state']=='paused'
            assert calls==[] and counters==[]
        finally:
            release.set();active.result(2)
            for future in queued:future.result(2)
