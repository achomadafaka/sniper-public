"""Offline nightly checks: evidence truth, concurrent lifecycle changes, resumable queues."""
from copy import deepcopy
import importlib.util
from pathlib import Path

import pytest
from brain_graph_fake_store import FakeStore
from bot.brain_graph import nightly
from bot.brain_graph.store import BrainUnavailable, VersionConflict

NOW = '2026-09-29T08:40:00+00:00'
LATER = '2026-09-30T08:40:00+00:00'


def fact(s, statement='Never run `git stash` in this shared checkout.', **fields):
    row = dict(kind='rule', statement=statement, entity_ids=['e1'], author='beta',
               evidence=[dict(type='path', ref='gone.py')])
    row.update(fields)
    return s.insert('brain_facts', row)


def checker(result):
    return lambda items: [dict(e, check=result, check_reason=result) for e in items]


def test_broken_debounce_unknown_and_recovery_never_verify():
    s = FakeStore(); f = fact(s)
    nightly.evidence_pass(s, [f], checker('broken'), NOW)
    first = s.get('brain_facts', f['id'])
    assert first['status'] == 'unverified' and first['evidence'][0]['broken_runs'] == 1
    nightly.evidence_pass(s, [first], checker('unchecked'), LATER)
    unknown = s.get('brain_facts', f['id'])
    assert unknown['status'] == 'unverified' and unknown['evidence'][0]['broken_runs'] == 1
    out = nightly.evidence_pass(s, [unknown], checker('broken'), '2026-10-01T00:00:00Z')
    broken = s.get('brain_facts', f['id'])
    assert broken['status'] == 'evidence_broken' and out['flips'][0]['id'] == f['id']
    out = nightly.evidence_pass(s, [broken], checker('ok'), '2026-10-02T00:00:00Z')
    assert out['recovered_candidates'] == [f['id']]
    assert s.get('brain_facts', f['id'])['status'] == 'evidence_broken'


def test_same_run_retry_does_not_count_as_second_broken_night():
    s = FakeStore(); f = fact(s)
    nightly.evidence_pass(s, [f], checker('broken'), NOW)
    nightly.evidence_pass(s, [s.get('brain_facts', f['id'])], checker('broken'), NOW)
    assert s.get('brain_facts', f['id'])['evidence'][0]['broken_runs'] == 1


def test_query_is_never_passed_to_checker_and_partial_response_is_unknown():
    s = FakeStore(); f = fact(s, evidence=[dict(type='query', ref='DELETE FROM money'),
                                         dict(type='path', ref='lost.py')])
    def partial(items):
        assert all(e['type'] != 'query' for e in items)
        return []
    nightly.evidence_pass(s, [f], partial, NOW)
    current = s.get('brain_facts', f['id'])
    assert [e['check'] for e in current['evidence']] == ['unchecked', 'unchecked']
    assert current['status'] == 'unverified'


def test_closed_fact_and_conflicting_snapshot_are_not_overwritten():
    s = FakeStore(); f = fact(s)
    s.patch_fact(f['id'], f['version'], dict(status='superseded', valid_to=LATER))
    out = nightly.evidence_pass(s, [f], checker('broken'), NOW)
    assert out['version_conflict'] == 1
    closed = s.get('brain_facts', f['id'])
    assert closed['status'] == 'superseded' and 'broken_runs' not in closed['evidence'][0]


def test_restore_refuses_a_fact_changed_after_the_flip():
    s = FakeStore(); f = fact(s)
    nightly.evidence_pass(s, [f], checker('broken'), NOW)
    out = nightly.evidence_pass(s, [s.get('brain_facts', f['id'])], checker('broken'), LATER)
    broken = s.get('brain_facts', f['id'])
    s.patch_fact(f['id'], broken['version'], {'verify_note': 'newer review'})
    assert nightly.restore_flips(s, out['flips'])['skipped'] == 1


def test_sweep_repairs_link_inserted_before_crash_and_is_idempotent():
    s = FakeStore(); old = fact(s); new = fact(s, 'Use `git stash` before changing the checkout.')
    s.insert('brain_links', dict(from_id=new['id'], from_kind='fact', to_id=old['id'],
                                to_kind='fact', rel='candidate', author='cron'))
    out = nightly.sweep_candidates(s, nightly.all_live_facts(s))
    assert out['pairs_new'] == 0 and out['facts_flagged'] == 1
    assert s.get('brain_facts', new['id'])['judge_pending']
    assert nightly.sweep_candidates(s, nightly.all_live_facts(s))['facts_flagged'] == 0
    assert len(s.rows['brain_links']) == 1


def test_sweep_skips_judged_and_closed_facts_and_honors_budget():
    s = FakeStore(); a = fact(s); b = fact(s, 'Use `git stash` on this checkout.')
    s.record_judgement(a['id'], b['id'], 'unrelated', 'gamma')
    assert nightly.sweep_candidates(s, nightly.all_live_facts(s))['pairs_new'] == 0
    c = fact(s, 'Always use `git stash` for a new checkout.')
    out = nightly.sweep_candidates(s, nightly.all_live_facts(s), pair_budget=1)
    assert out['pairs_new'] == 1
    assert nightly.sweep_candidates(s, nightly.all_live_facts(s), pair_budget=1)['pairs_new'] == 1


def test_queue_timezones_and_unknown_health_denominators():
    s = FakeStore(); f = fact(s)
    f.update(status='disputed', recorded_at='2026-09-28T10:00:00+03:00', verify_by='2026-09-29T06:00:00Z')
    queue = nightly.queue_report([f], NOW)
    assert queue['disputed_over_24h'] == [f['id']]
    m = nightly.health_metrics([f], {'checks': {'path:unchecked': 2}}, {}, queue, NOW)
    assert m['evidence_failure_rate']['path'] is None and m['past_verify_by'] == 1
    assert nightly.health_metrics([], {}, {}, {}, NOW)['share_verified'] is None


def test_summaries_load_entities_once_and_clear_empty_summaries():
    s = FakeStore(); sec = s.insert('brain_sections', dict(slug='x', title='x', summary='stale'))
    ent = s.insert('brain_entities', dict(type='tool', name='tool', section_id=sec['id'], summary='stale'))
    f = fact(s, entity_ids=[ent['id']])
    out = nightly.summaries(s, [f], NOW)
    assert out['entities_patched'] == 1
    assert s.get('brain_entities', ent['id'])['summary'].startswith('1 facts')
    nightly.summaries(s, [], NOW)
    assert s.get('brain_entities', ent['id'])['summary'] is None
    assert s.get('brain_sections', sec['id'])['summary'] is None


def test_changed_summary_invalidates_embedding_but_unchanged_summary_preserves_it():
    s = FakeStore()
    sec = s.insert('brain_sections', dict(slug='x', title='x'))
    ent = s.insert('brain_entities', dict(type='tool', name='tool', section_id=sec['id'],
                                        summary='old summary', embedding=[1.0] * 384))
    f = fact(s, entity_ids=[ent['id']])
    nightly.summaries(s, [f], NOW)
    assert s.get('brain_entities', ent['id'])['embedding'] is None
    vector = [0.5] * 384
    s.patch_row('brain_entities', ent['id'], {'embedding': vector})
    assert nightly.summaries(s, [f], NOW)['entities_patched'] == 0
    assert s.get('brain_entities', ent['id'])['embedding'] == vector


def test_doctrine_drift_never_supersedes_by_lexical_similarity(monkeypatch):
    from bot.brain_graph import api
    s = FakeStore(); old = fact(s, 'Never use git stash in the shared checkout.', author='operator',
                              evidence=[dict(type='path', ref='CLAUDE.md:10')])
    writes=[]
    monkeypatch.setattr(api, 'write_fact', lambda store, row, **kw: writes.append((row, kw)) or {'status':'written'})
    rules=[dict(statement='Always use git stash in the shared checkout.', source='AGENTS.md', line=10)]
    out = nightly.doctrine_drift(s, [old], rules, repo='.', secret_values=set())
    assert out['superseded'] == 0 and out['doctrine_removed'] == [old['id']]
    assert s.get('brain_facts', old['id'])['status'] == 'unverified'
    assert writes[0][1]['interactive'] is False


def job():
    spec=importlib.util.spec_from_file_location('brain_nightly_job', Path(__file__).parents[1]/'scripts/brain_nightly.py')
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def test_run_dry_run_is_non_mutating_and_success_writes_health():
    s=FakeStore(); fact(s); before=deepcopy(s.rows)
    bn=job()
    metrics=bn.run(s, '.', check_fn=checker('broken'), now_iso=NOW, dry_run=True)
    assert metrics['facts_live'] == 1 and s.rows == before
    bn.run(s, '.', check_fn=checker('broken'), now_iso=NOW)
    assert len(s.rows['brain_health']) == 1


def test_cli_store_initialization_failure_still_writes_local_health(tmp_path):
    bn=job()
    def unavailable():
        raise BrainUnavailable('do not leak provider secrets')
    assert bn.main([], store_factory=unavailable, state=tmp_path) == 4
    import json
    payload=json.loads((tmp_path/'data/brain_health_latest.json').read_text())
    assert payload['error'] == 'BrainUnavailable' and 'provider secrets' not in str(payload)


@pytest.mark.parametrize('reply', [None, {'check':'broken'}, ['wrong', None]])
def test_malformed_checker_result_stays_unknown(reply):
    s=FakeStore(); f=fact(s)
    nightly.evidence_pass(s, [f], lambda _: reply, NOW)
    result=s.get('brain_facts', f['id'])
    assert result['evidence'][0]['check']=='unchecked' and result['status']=='unverified'


def test_unavailable_mid_run_preserves_partial_evidence_counts():
    s=FakeStore(); fact(s); fact(s, 'Another independent long enough rule.')
    original=s.patch_fact; calls=[]
    def fail_second(*args, **kwargs):
        calls.append(args)
        if len(calls)==2:
            raise BrainUnavailable('offline')
        return original(*args, **kwargs)
    s.patch_fact=fail_second
    progress={}
    with pytest.raises(BrainUnavailable):
        job().run(s, '.', check_fn=checker('broken'), now_iso=NOW, progress=progress)
    assert progress['stage']=='evidence' and progress['evidence']['patched']==1
    assert progress['evidence']['checks']=={'path:broken':2}


def test_candidate_sweep_does_not_reflag_a_concurrently_closed_row():
    s=FakeStore(); old=fact(s); new=fact(s, 'Use `git stash` in the shared checkout.')
    snapshot=nightly.all_live_facts(s)
    s.patch_fact(new['id'],new['version'],dict(status='superseded',valid_to=LATER))
    out=nightly.sweep_candidates(s,snapshot)
    assert out['pairs_new']==0 and not s.get('brain_facts',new['id'])['judge_pending']


def test_long_doctrine_uses_same_statement_and_detail_as_import(monkeypatch):
    from bot.brain_graph import api
    from bot.brain_graph.import_sources import split_statement
    text = 'Never use git stash in the shared checkout. ' + 'Preserve the operator context. ' * 30
    statement, detail = split_statement(text)
    s=FakeStore(); existing=fact(s, statement, author='operator', detail=detail,
                               evidence=[dict(type='path',ref='CLAUDE.md:10')])
    writes=[]
    monkeypatch.setattr(api,'write_fact', lambda store,row,**kw: writes.append(row) or {'status':'written'})
    rule=dict(source='CLAUDE.md',line=10,statement=text)
    out=nightly.doctrine_drift(s,[existing],[rule],repo='.',secret_values=set())
    assert out['added']==0 and out['doctrine_removed']==[] and writes==[]
    nightly.doctrine_drift(FakeStore(),[],[rule],repo='.',secret_values=set())
    assert writes[0]['statement']==statement and writes[0]['detail']==detail


def test_cli_protects_both_env_files_and_process_secrets(monkeypatch,tmp_path):
    from bot.brain_graph import evidence
    bn=job(); monkeypatch.setattr(bn,'ROOT',tmp_path)
    (tmp_path/'.env').write_text('API_KEY=fixture-env-secret-0123456789\n')
    (tmp_path/'.fomo.env').write_text('API_TOKEN=fixture-fomo-secret-0123456789\n')
    monkeypatch.setenv('NIGHTLY_API_SECRET','fixture-process-secret-0123456789')
    monkeypatch.setattr(evidence,'fetch_once',lambda _:False)
    captured={}
    monkeypatch.setattr(bn,'run',lambda *a,**kw: captured.update(kw) or {})
    assert bn.main([],store_factory=FakeStore,state=tmp_path)==0
    assert {'fixture-env-secret-0123456789','fixture-fomo-secret-0123456789',
            'fixture-process-secret-0123456789'} <= captured['secret_values']


@pytest.mark.parametrize('status', ['refuted', 'retracted', 'superseded'])
def test_doctrine_drift_does_not_recreate_closed_unchanged_rule(monkeypatch, status):
    from bot.brain_graph import api
    s = FakeStore()
    old = fact(s, author='operator', evidence=[dict(type='path', ref='CLAUDE.md:10')])
    patch = dict(status=status)
    if status != 'refuted':
        patch['valid_to'] = LATER
    else:
        patch['verified_by'] = 'beta'
    s.patch_fact(old['id'], old['version'], patch)
    writes = []
    monkeypatch.setattr(api, 'write_fact', lambda store, row, **kw: writes.append(row) or {'status': 'written'})
    rule = dict(source='CLAUDE.md', line=20, statement=old['statement'])
    out = nightly.doctrine_drift(s, nightly.all_live_facts(s), [rule], repo='.', secret_values=set())
    assert out['added'] == 0 and writes == []
    assert out['doctrine_removed'] == []
    nightly.doctrine_drift(s, nightly.all_live_facts(s),
        [dict(rule, statement='Never run git reset in this shared checkout.')], repo='.', secret_values=set())
    assert len(writes) == 1
    assert s.get('brain_facts', old['id'])['status'] == status


def test_cli_checks_paths_with_their_cited_commit_and_isolates_cache_context(monkeypatch, tmp_path):
    """Dropping the cited SHA falsely breaks valid branch evidence after two nights."""
    import subprocess
    from bot.brain_graph import evidence

    def git(*args):
        return subprocess.run(['git', '-C', str(tmp_path), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    git('init', '-q', '-b', 'main')
    (tmp_path / 'base.txt').write_text('main\n')
    git('add', 'base.txt')
    git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '-m', 'base')
    base = git('rev-parse', 'HEAD')
    git('update-ref', 'refs/remotes/origin/main', base)
    git('checkout', '-q', '-b', 'work')
    (tmp_path / 'only-at-commit.py').write_text('x = 1\n')
    git('add', 'only-at-commit.py')
    git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '-m', 'branch proof')
    proof = git('rev-parse', 'HEAD')
    git('checkout', '-q', 'main')
    item = dict(type='path', ref='only-at-commit.py', broken_runs=1)
    good = [item, dict(type='sha', ref=proof)]
    wrong = [item, dict(type='sha', ref=base)]
    assert evidence.check_all(good, tmp_path)[0]['check'] == 'pending_push'
    assert evidence.check_all(wrong, tmp_path)[0]['check'] == 'broken'

    bn = job()
    monkeypatch.setattr(bn, 'ROOT', tmp_path)
    monkeypatch.setattr(evidence, 'fetch_once', lambda _: True)
    original = evidence.check_all
    calls = []
    def record(items, *args, **kwargs):
        calls.append(list(items))
        return original(items, *args, **kwargs)
    monkeypatch.setattr(evidence, 'check_all', record)

    observed = {}
    def run(*args, check_fn, **kwargs):
        observed['first'] = check_fn(good)
        observed['wrong'] = check_fn(wrong)
        observed['alone'] = check_fn([item])
        observed['calls_before_repeat'] = len(calls)
        observed['repeat'] = check_fn(good)
        observed['calls_after_repeat'] = len(calls)
        return {}
    monkeypatch.setattr(bn, 'run', run)
    assert bn.main(['--dry-run'], store_factory=FakeStore, state=tmp_path) == 0
    assert observed['first'][0]['check'] == 'pending_push'
    assert observed['first'][0]['broken_runs'] == 1
    assert observed['wrong'][0]['check'] == observed['alone'][0]['check'] == 'broken'
    assert observed['repeat'][0]['check'] == 'pending_push'
    assert observed['calls_after_repeat'] == observed['calls_before_repeat']


def test_orphan_judge_pending_is_cleared_but_a_real_open_pair_keeps_it():
    """Gamma 2026-09-29: judge_pending with no candidate link could never be verified or judged."""
    s = FakeStore()
    orphan = fact(s, 'Orphan pending fact.', judge_pending=True)
    newer = fact(s, 'Newer fact in an open pair.', judge_pending=True)
    older = fact(s, 'Older fact in an open pair.')
    s.insert('brain_links', {'from_id': newer['id'], 'from_kind': 'fact', 'to_id': older['id'],
                             'to_kind': 'fact', 'rel': 'candidate', 'author': 'cron'})
    facts = nightly.all_live_facts(s)
    assert nightly.clear_orphan_pending(s, facts) == [orphan['id']]
    rows = {r['id']: r for r in s.rows['brain_facts']}
    assert rows[orphan['id']]['judge_pending'] is False
    assert rows[newer['id']]['judge_pending'] is True
    s.record_judgement(newer['id'], older['id'], 'unrelated', 'gamma')
    assert nightly.clear_orphan_pending(s, nightly.all_live_facts(s)) == [newer['id']]


def test_a_dispute_lifts_only_when_every_contradicting_side_is_closed():
    s = FakeStore()
    new = fact(s, 'New rule A.', status='disputed')
    old1 = fact(s, 'Old rule B.', status='disputed')
    old2 = fact(s, 'Old rule C.', status='disputed')
    for other in (old1, old2):
        s.insert('brain_links', {'from_id': new['id'], 'from_kind': 'fact', 'to_id': other['id'],
                                 'to_kind': 'fact', 'rel': 'contradicts', 'author': 'lambda'})
    s.patch_fact(old1['id'], old1['version'], {'status': 'retracted'})
    facts = nightly.all_live_facts(s)
    assert nightly.resolve_closed_disputes(s, facts) == []          # old2 still open
    o2 = next(r for r in s.rows['brain_facts'] if r['id'] == old2['id'])
    s.patch_fact(o2['id'], o2['version'], {'status': 'retracted'})
    assert nightly.resolve_closed_disputes(s, nightly.all_live_facts(s)) == [new['id']]
    assert next(r for r in s.rows['brain_facts'] if r['id'] == new['id'])['status'] == 'unverified'
