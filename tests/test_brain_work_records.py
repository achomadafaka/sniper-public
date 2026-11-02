import importlib
import json
import subprocess
from pathlib import Path

import pytest

from brain_graph_fake_store import FakeStore
from bot.brain_graph import api
from bot.brain_graph.store import BrainRefused


def work():
    return importlib.import_module('bot.brain_graph.work_records')


@pytest.fixture
def env(tmp_path):
    def git(*args):
        return subprocess.check_output(['git', '-C', str(tmp_path), *args], text=True).strip()
    git('init', '-b', 'main');git('config', 'user.email', 'test@example.org');git('config', 'user.name', 'Test')
    (tmp_path/'proof.txt').write_text('delivered')
    git('add', 'proof.txt');git('commit', '-m', 'fix: planted proof')
    class WindowStore(FakeStore):
        def rows_page(self, table, *, filters=(), **kwargs):
            ranges = [(col, op, val) for col, op, val in filters if op in ('gte', 'lt')]
            if not ranges:
                return super().rows_page(table, filters=filters, **kwargs)
            filtered = FakeStore()
            selected = self.rows[table]
            for col, op, value in ranges:
                selected = [r for r in selected if r.get(col) is not None
                            and (r[col] >= value if op == 'gte' else r[col] < value)]
            filtered.rows = dict(self.rows, **{table: selected})
            return filtered.rows_page(table, filters=[f for f in filters if f[1] not in ('gte', 'lt')], **kwargs)
    store = WindowStore();store.insert('brain_sections', {'slug':'crew','title':'Crew'})
    task = api.write_fact(store, dict(kind='task', statement='Deliver planted review result', author='codex',
        section='crew', entities=[dict(type='actor',name='codex')],
        evidence=[dict(type='path',ref='proof.txt')]), repo=tmp_path, interactive=False)['fact']
    return store, tmp_path, task, git


def record(env, **kwargs):
    s,p,t,g=env
    return work().record_work(s, role='codex', statement='Delivered the planted review result',
        validation='offline focused proof passed', task_id=t['id'], repo=p, fetched=False, **kwargs)


def test_integration_completion_leaves_live_finding_and_versioned_link(env):
    s,p,t,g=env;r=record(env, merge=g('rev-parse','HEAD'))
    assert r['status']=='written'
    fact=r['fact'];assert s.find_entity('commit',g('rev-parse','HEAD'))['id'] in fact['entity_ids']
    closed=work().complete_work(s,t['id'],role='codex',completion_fact_id=fact['id'],reason='delivered',repo=p)
    assert closed['status']=='written'
    task=s.get('brain_facts',t['id']);link=json.loads(task['detail'])['completion']
    assert link['completion_fact_id']==fact['id'] and link['completion_fact_version']==fact['version']
    assert s.get('brain_facts',fact['id'])['status']=='unverified'
    assert work().complete_work(s,t['id'],role='codex',completion_fact_id=fact['id'],reason='retry',repo=p)['status']=='written'


def test_episode_alone_cannot_close_and_no_integration_requires_durable_finding(env):
    s,p,t,g=env;ep=api.add_episode(s,'crew_report','Planted review delivered',author='codex')[0]
    with pytest.raises(BrainRefused):
        work().complete_work(s,t['id'],role='codex',completion_fact_id=ep['id'],reason='done',repo=p)
    r=record(env,no_integration=True,artifact='episode:'+ep['id'])
    assert json.loads(r['fact']['detail'])['work']['completion_kind']=='no_integration'
    assert work().complete_work(s,t['id'],role='codex',completion_fact_id=r['fact']['id'],reason='done',repo=p)['status']=='written'


@pytest.mark.parametrize('artifact',['proof.txt','proof.txt@deadbee','absent.txt@HEAD'])
def test_unversioned_or_nonexistent_artifact_refuses(env,artifact):
    with pytest.raises(BrainRefused):record(env,no_integration=True,artifact=artifact)


def test_committed_artifact_and_idempotent_record_with_payload_conflict(env):
    s,p,t,g=env;sha=g('rev-parse','HEAD')
    r=record(env,no_integration=True,artifact='proof.txt@'+sha)
    assert record(env,no_integration=True,artifact='proof.txt@'+sha)['fact']['id']==r['fact']['id']
    with pytest.raises(BrainRefused):
        work().record_work(s,role='codex',statement='Different completion under same identity',validation='changed',
            task_id=t['id'],repo=p,no_integration=True,artifact='proof.txt@'+sha,fetched=False)


def test_disappearing_completion_after_close_reopens_new_task(env,monkeypatch):
    s,p,t,g=env;r=record(env,merge=g('rev-parse','HEAD'));fid=r['fact']['id'];original=s.patch_fact
    def race(id,version,patch):
        result=original(id,version,patch)
        if id==t['id'] and patch.get('status')=='retracted':
            f=s.get('brain_facts',fid);original(fid,f['version'],{'status':'retracted'})
        return result
    monkeypatch.setattr(s,'patch_fact',race)
    result=work().complete_work(s,t['id'],role='codex',completion_fact_id=fid,reason='done',repo=p)
    assert result['status']=='reopened'
    assert s.get('brain_facts',t['id'])['status']=='retracted'
    reopened=s.get('brain_facts',result['task_id']);assert reopened['kind']=='task'
    assert 'reopened: completion fact' in reopened['statement']


def test_pending_fact_write_never_closes_task(env,monkeypatch):
    s,p,t,g=env;monkeypatch.setattr(api,'write_fact',lambda *a,**kw:{'status':'queued'})
    assert record(env,merge=g('rev-parse','HEAD'))['status']=='queued'
    assert s.get('brain_facts',t['id'])['status']=='unverified'


def test_typed_retraction_requires_reason_kind(env):
    s,p,t,g=env
    with pytest.raises(BrainRefused):api.retract_fact(s,t['id'],role='codex',reason='done')
    assert api.retract_fact(s,t['id'],role='codex',reason='duplicate mistaken task',reason_kind='misfiled')['status']=='retracted'


def test_coverage_counts_units_not_episode_or_no_integration_records(env):
    s,p,t,g=env
    base=g('rev-parse','HEAD')
    (p/'second.txt').write_text('second');g('add','second.txt');g('commit','-m','docs: second unit')
    head=g('rev-parse','HEAD')
    result=work().work_coverage(s,repo=p,base=base,head=head)
    assert result['missing']==[head] and result['complete'] is True
    record(env,merge=head)
    result=work().work_coverage(s,repo=p,base=base,head=head)
    assert result['covered']==[head] and result['missing']==[]


def test_capped_lookup_is_unknown_not_absent(env,monkeypatch):
    s,p,t,g=env;base=g('rev-parse','HEAD')
    (p/'second.txt').write_text('second');g('add','second.txt');g('commit','-m','fix: second unit');head=g('rev-parse','HEAD')
    monkeypatch.setattr(s,'facts_by_shas',lambda *a,**kw:{head:{'facts':[],'complete':False}})
    result=work().work_coverage(s,repo=p,base=base,head=head)
    assert result['unknown']==[head] and not result['missing'] and result['complete'] is False


def test_typed_closure_audit_counts_role_week_and_marks_cap(env):
    s,p,t,g=env;sha=g('rev-parse','HEAD')
    api.retract_fact(s,t['id'],role='codex',reason_kind='obsolete',reason='done '+sha)
    closed=s.get('brain_facts',t['id']);s.patch_fact(t['id'],closed['version'],{'verified_at':'2026-10-30T10:00:00+00:00'})
    result=work().closure_audit(s,repo=p,since='2026-10-01T00:00:00+00:00',until='2026-11-01T00:00:00+00:00')
    assert result['suspicious']==[dict(role='codex',week='2026-W44',count=1)]
    assert result['complete'] is True
    assert work().closure_audit(s,repo=p,since='2026-10-01T00:00:00+00:00',until='2026-11-01T00:00:00+00:00',page_size=1,max_pages=1)['complete'] is False


def test_relayed_order_is_labelled_without_authenticating_model_claim(env):
    s,p,t,g=env
    result=api.write_fact(s,dict(kind='decision',statement='Operator requests planted offline review',author='codex',
        section='crew',entities=[dict(type='actor',name='operator')],evidence=[dict(type='path',ref='proof.txt')],
        detail=json.dumps({'order_provenance':{'relayed_by':'operator','authenticated':True}})),repo=p,interactive=False)
    fact=result['fact'];provenance=json.loads(fact['detail'])['order_provenance']
    assert provenance==dict(relayed_by='codex',authenticated=False)
    from bot.brain_graph.views import line
    assert 'relayed_by:codex authenticated:false' in line(fact)


def test_handover_inventory_requires_complete_current_tasks_and_orders(env):
    s,p,t,g=env
    inventory=dict(complete=True,pagination_complete=True,content_digest='d'*64,
        tasks=[dict(id=t['id'],version=t['version'],owner='codex')],operator_order_ids=[])
    assert work().check_fact_inventory(s,'codex',inventory)['task_ids']==[t['id']]
    inventory['tasks']=[]
    with pytest.raises(BrainRefused):work().check_fact_inventory(s,'codex',inventory)


def test_cli_new_commands_and_manual_handover_warning(env,monkeypatch,capsys):
    import importlib.util
    spec=importlib.util.spec_from_file_location('work_cli',Path(__file__).resolve().parents[1]/'scripts/brain_cli.py')
    cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
    s,p,t,g=env;monkeypatch.setattr(cli,'ROOT',p)
    parser=cli.build_parser()
    assert parser.parse_args(['record-work','--role','codex','--merge',g('rev-parse','HEAD'),'--validation','passed','Delivered planted review']).cmd=='record-work'
    assert cli.main(['retract',t['id'],'--role','codex','--reason-kind','obsolete','--reason','mistaken task'],store=s,fetch=False)==0
    monkeypatch.setattr(cli.views.live,'derive',lambda *a,**kw:{'errors':[]})
    assert cli.main(['handover','codex'],store=s,fetch=False)==0
    assert 'fact inventory UNVERIFIED' in capsys.readouterr().out


def test_superseded_completion_follows_qualified_successor(env):
    s,p,t,g=env;fact=record(env,merge=g('rev-parse','HEAD'))['fact']
    successor=s.supersede(fact['id'],fact['version'],dict(fact,id=None,statement='Replacement durable completion statement'))
    result=work().complete_work(s,t['id'],role='codex',completion_fact_id=fact['id'],reason='done',repo=p)
    assert result['completion_fact_id']==successor['id']


def test_wrong_task_and_non_main_merge_cannot_complete(env):
    s,p,t,g=env;g('checkout','-b','branch')
    (p/'branch.txt').write_text('branch');g('add','branch.txt');g('commit','-m','fix: branch only')
    fact=record(env,merge=g('rev-parse','HEAD'))['fact']
    with pytest.raises(BrainRefused):work().complete_work(s,t['id'],role='codex',completion_fact_id=fact['id'],reason='done',repo=p)
    assert s.get('brain_facts',t['id'])['status']=='unverified'


def test_handover_rejects_missing_order_fact_and_paging(env):
    s,p,t,g=env
    inventory=dict(complete=True,pagination_complete=True,content_digest='d'*64,
        tasks=[dict(id=t['id'],version=t['version'],owner='codex')],operator_order_ids=['missing'])
    with pytest.raises(BrainRefused):work().check_fact_inventory(s,'codex',inventory)
    inventory['operator_order_ids']=[];inventory['pagination_complete']=False
    with pytest.raises(BrainRefused):work().check_fact_inventory(s,'codex',inventory)


def test_no_integration_uses_finding_not_decision(env):
    s,p,t,g=env;ep=api.add_episode(s,'crew_report','Delivered review result',author='codex')[0]
    with pytest.raises(BrainRefused):
        work().record_work(s,role='codex',statement='Delivered review result',validation='review PASS',kind='decision',
            no_integration=True,artifact='episode:'+ep['id'],repo=p,fetched=False)


def test_coverage_merge_is_one_unit_and_retracted_finding_is_not_coverage(env):
    s,p,t,g=env;base=g('rev-parse','HEAD');g('checkout','-b','feature')
    for name in ['a.txt','b.txt']:
        (p/name).write_text(name);g('add',name);g('commit','-m','fix: '+name)
    g('checkout','main');g('merge','--no-ff','feature','-m','merge: one delivered unit')
    head=g('rev-parse','HEAD');fact=record(env,merge=head)['fact']
    result=work().work_coverage(s,repo=p,base=base,head=head)
    assert result['total']==1 and result['covered']==[head]
    api.retract_fact(s,fact['id'],role='codex',reason_kind='obsolete',reason='withdraw unproven result')
    assert work().work_coverage(s,repo=p,base=base,head=head)['missing']==[head]


def test_task_mutation_between_reads_refuses_close(env,monkeypatch):
    s,p,t,g=env;fact=record(env,merge=g('rev-parse','HEAD'))['fact'];original=s.get;reads=0
    def get(table,id):
        nonlocal reads
        result=original(table,id)
        if table=='brain_facts' and id==t['id']:
            reads+=1
            if reads==2:
                s.patch_fact(id,result['version'],{'detail':'concurrent task update'})
                result=original(table,id)
        return result
    monkeypatch.setattr(s,'get',get)
    with pytest.raises(BrainRefused):work().complete_work(s,t['id'],role='codex',completion_fact_id=fact['id'],reason='done',repo=p)
    assert original('brain_facts',t['id'])['status']=='unverified'


def test_completion_record_cannot_close_task_edited_since_recording(env):
    s,p,t,g=env;fact=record(env,merge=g('rev-parse','HEAD'))['fact']
    s.patch_fact(t['id'],t['version'],{'detail':'scope changed after delivery record'})
    with pytest.raises(BrainRefused):work().complete_work(s,t['id'],role='codex',completion_fact_id=fact['id'],reason='done',repo=p)
    assert s.get('brain_facts',t['id'])['status']=='unverified'


def test_coverage_limit_is_incomplete_without_any_brain_lookup(env,monkeypatch):
    s,p,t,g=env;original=work()._git
    def git(repo,*args):
        if args[0]=='rev-list':return '\n'.join(f'{n:040x}' for n in range(101))
        return original(repo,*args)
    monkeypatch.setattr(work(),'_git',git)
    monkeypatch.setattr(s,'facts_by_shas',lambda *a,**kw:pytest.fail('overflow must stop before brain reads'))
    sha=g('rev-parse','HEAD');result=work().work_coverage(s,repo=p,base=sha,head=sha)
    assert not result['complete'] and result['reason']=='unit_limit' and not result['missing']


def test_legacy_order_provenance_is_unknown():
    from bot.brain_graph.views import line
    assert 'authenticated:unknown' in line(dict(id='legacy',kind='decision',statement='Old quoted instruction',status='unverified'),operator_order=True)


def test_boot_old_task_nudge_uses_event_age_and_excludes_judgement(env):
    s,p,t,g=env
    row=s.rows['brain_facts'][0];row['valid_from']='2026-09-01T00:00:00+00:00';row['recorded_at']='2026-10-29T00:00:00+00:00'
    from datetime import datetime, timezone
    from bot.brain_graph import views
    now=datetime(2026,10,30,tzinfo=timezone.utc)
    result=views.old_task_nudge(s,'codex',now)
    assert '14+ days' in result and 'codex: 1' in result and t['id'][:8] in result and '59d' in result
    row['judge_pending']=True
    assert 'codex: 0' in views.old_task_nudge(s,'codex',now)


def test_boot_old_task_nudge_labels_capped_and_unknown_dates(env):
    s,p,t,g=env
    row=s.rows['brain_facts'][0];row['valid_from']='broken';row['recorded_at']='2026-09-01T00:00:00+00:00'
    from datetime import datetime, timezone
    from bot.brain_graph import views
    result=views.old_task_nudge(s,'codex',datetime(2026,10,30,tzinfo=timezone.utc),page_size=1,max_pages=1)
    assert 'UNKNOWN: capped' in result and 'age unknown: 1' in result


def test_closure_audit_pushes_time_window_into_every_page_query(env,monkeypatch):
    s,p,t,g=env;seen=[];original=s.rows_page
    def read(table,**kwargs):
        if table=='brain_facts':seen.append(kwargs.get('filters',()))
        return original(table,**kwargs)
    monkeypatch.setattr(s,'rows_page',read)
    since='2026-10-01T00:00:00+00:00';until='2026-11-01T00:00:00+00:00'
    work().closure_audit(s,repo=p,since=since,until=until)
    assert seen and all(('verified_at','gte',since) in f and ('verified_at','lt',until) in f for f in seen)


def test_boot_nudge_includes_verified_old_task(env):
    s,p,t,g=env
    s.rows['brain_facts'][0]['valid_from']='2026-09-01T00:00:00+00:00'
    api.verify_fact(s,t['id'],'verified',role='beta',output='Reviewed offline task evidence')
    from datetime import datetime, timezone
    from bot.brain_graph import views
    assert 'codex: 1' in views.old_task_nudge(s,'codex',datetime(2026,10,30,tzinfo=timezone.utc))


def test_nudge_reader_failure_is_unknown_instead_of_failing_boot(env,monkeypatch):
    s,p,t,g=env
    from datetime import datetime, timezone
    from bot.brain_graph import views
    from bot.brain_graph.store import BrainUnavailable
    def down(*a,**kw):raise BrainUnavailable('private backend detail must not print')
    monkeypatch.setattr(s,'rows_page',down)
    result=views.old_task_nudge(s,'codex',datetime(2026,10,30,tzinfo=timezone.utc))
    assert result=='nudge UNKNOWN: BrainUnavailable'


@pytest.mark.parametrize('kind',['decision','settled'])
def test_dated_operator_order_writes_cannot_omit_operator_anchor(env,kind):
    s,p,t,g=env
    result=api.write_fact(s,dict(kind=kind,statement='Operator 2026-10-30: canary is Lambda only.',author='codex',
        section='crew',entities=[dict(type='actor',name='codex')],evidence=[dict(type='path',ref='proof.txt')]),repo=p,interactive=False)
    fact=result['fact'];operator=s.find_entity('actor','operator')
    assert operator and operator['id'] in fact['entity_ids']
    assert fact['author']=='codex'
    assert json.loads(fact['detail'])['order_provenance']==dict(relayed_by='codex',authenticated=False)


def test_explicit_operator_order_links_before_required_entity_gate(env):
    s,p,t,g=env
    result=api.write_fact(s,dict(kind='decision',statement='Canary goes on Lambda after probes pass.',author='codex',
        operator_order=True,section='crew',entities=[],evidence=[dict(type='path',ref='proof.txt')]),repo=p,interactive=False)
    assert result['status']=='written'
    assert s.find_entity('actor','operator')['id'] in result['fact']['entity_ids']


def test_cli_operator_order_is_explicit_and_ordinary_decision_is_not_promoted(env):
    import importlib.util
    spec=importlib.util.spec_from_file_location('order_cli',Path(__file__).resolve().parents[1]/'scripts/brain_cli.py')
    cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
    args=cli.build_parser().parse_args(['write','--role','codex','--kind','decision','--operator-order',
        '--section','crew','--evidence','path:proof.txt','Canary on Lambda after probes.'])
    assert cli.draft_from_args(args)['operator_order'] is True
    s,p,t,g=env
    result=api.write_fact(s,dict(kind='decision',statement='Review ordinary parser boundary behavior.',author='codex',
        section='crew',entities=[dict(type='actor',name='codex')],evidence=[dict(type='path',ref='proof.txt')]),repo=p,interactive=False)
    assert result['status']=='written' and s.find_entity('actor','operator') is None


def test_record_work_operator_decision_remains_idempotent_with_anchor(env):
    s,p,t,g=env
    args=dict(role='codex',statement='Operator 2026-10-30: record reviewed delivery.',validation='offline proof',
        merge=g('rev-parse','HEAD'),kind='decision',repo=p,fetched=False)
    first=work().record_work(s,**args)
    assert s.find_entity('actor','operator')['id'] in first['fact']['entity_ids']
    assert work().record_work(s,**args)['fact']['id']==first['fact']['id']


def test_operator_provenance_cannot_bypass_anchor_by_omitting_flag(env):
    s,p,t,g=env
    result=api.write_fact(s,dict(kind='decision',statement='Lambda is the sole canary after probes.',author='codex',
        section='crew',entities=[dict(type='actor',name='codex')],evidence=[dict(type='path',ref='proof.txt')],
        detail=json.dumps({'order_provenance':{'relayed_by':'operator','authenticated':True}})),repo=p,interactive=False)
    fact=result['fact'];operator=s.find_entity('actor','operator')
    assert operator and operator['id'] in fact['entity_ids']
    assert json.loads(fact['detail'])['order_provenance']==dict(relayed_by='codex',authenticated=False)
