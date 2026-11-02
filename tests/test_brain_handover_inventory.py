"""Offline inventory proofs. No real brain, credentials or installation."""
import importlib
import json

import pytest

from brain_graph_fake_store import FakeStore
from bot.brain_graph import api
from bot.brain_graph.store import BrainUnavailable


def module():return importlib.import_module('bot.brain_graph.handover_inventory')


class Store(FakeStore):
    server_cap=500

    def rows_page(self,table,*,filters=(),limit=500,**kwargs):
        contains=[f for f in filters if f[1]=='cs']
        if contains:
            view=FakeStore();rows=self.rows[table]
            for column,_,value in contains:
                required=json.loads('['+value[1:-1]+']')
                rows=[r for r in rows if set(required)<=set(r.get(column) or [])]
            view.rows=dict(self.rows,**{table:rows})
            return view.rows_page(table,filters=[f for f in filters if f[1]!='cs'],limit=min(limit,self.server_cap),**kwargs)
        return super().rows_page(table,filters=filters,limit=min(limit,self.server_cap),**kwargs)


@pytest.fixture
def seeded(tmp_path):
    s=Store();s.insert('brain_sections',dict(slug='crew',title='Crew'))
    (tmp_path/'proof.txt').write_text('proof')
    def write(kind,actor,author='alpha'):
        r=api.write_fact(s,dict(kind=kind,statement=f'Planted {kind} work for {actor}',author=author,section='crew',
            entities=[dict(type='actor',name=actor)],evidence=[dict(type='path',ref='proof.txt')]),repo=tmp_path,interactive=False)
        return r['fact']
    task=write('task','codex');order=write('settled','operator')
    return s,task,order,write


def test_complete_canonical_brain_inventory_keeps_unresolved_work(seeded):
    s,t,o,write=seeded
    s.patch_fact(t['id'],t['version'],dict(status='disputed',judge_pending=True))
    result=module().build_inventory(s,'codex')
    assert result['complete'] is True and result['pagination_complete'] is True
    assert result['tasks']==[dict(id=t['id'],version=2,owner='codex')]
    assert result['operator_order_ids']==[o['id']]
    assert result['operator_orders'][0]['owner']=='operator'
    assert len(result['brain_content_digest'])==64 and result['cut_id']
    assert 'pending_message_ids' not in result  # host ledger must supply its own proof
    again=module().build_inventory(s,'codex')
    assert result['brain_content_digest']==again['brain_content_digest'] and result['cut_id']!=again['cut_id']


def test_short_server_pages_do_not_mean_complete(seeded):
    s,t,o,write=seeded;s.server_cap=1
    assert module().build_inventory(s,'codex',page_size=500)['tasks'][0]['id']==t['id']


@pytest.mark.parametrize('mutation',['detail','version','status','provenance'])
def test_same_id_edits_change_content_digest(seeded,mutation):
    s,t,o,write=seeded;before=module().build_inventory(s,'codex')
    row=next(r for r in s.rows['brain_facts'] if r['id']==(o['id'] if mutation=='provenance' else t['id']))
    if mutation=='detail':row['detail']='same ID changed detail, version deliberately unchanged'
    if mutation=='version':row['version']+=1
    if mutation=='status':row['status']='evidence_broken'
    if mutation=='provenance':row['detail']=json.dumps({'order_provenance':{'relayed_by':'gamma','authenticated':False}})
    after=module().build_inventory(s,'codex')
    assert before['brain_content_digest']!=after['brain_content_digest']


def test_closed_tasks_are_excluded_but_verified_and_pending_are_open(seeded):
    s,t,o,write=seeded
    api.verify_fact(s,t['id'],'verified',role='beta',output='Offline proof')
    assert module().build_inventory(s,'codex')['tasks'][0]['id']==t['id']
    task=s.get('brain_facts',t['id']);s.patch_fact(t['id'],task['version'],{'status':'retracted'})
    assert module().build_inventory(s,'codex')['tasks']==[]


@pytest.mark.parametrize('mode',['cap','read_error','missing_actor','missing_reference','bad_version','office','bytes'])
def test_unknown_or_partial_inventory_refuses_instead_of_empty(seeded,monkeypatch,mode):
    s,t,o,write=seeded;kwargs={}
    if mode=='cap':kwargs['max_pages']=1
    if mode=='read_error':
        def down(*a,**kw):raise BrainUnavailable('backend secret detail')
        monkeypatch.setattr(s,'rows_page',down)
    if mode=='missing_actor':s.rows['brain_entities']=[e for e in s.rows['brain_entities'] if e['name']!='operator']
    if mode=='missing_reference':s.rows['brain_facts'][0]['entity_ids'].append('00000000-0000-0000-0000-000000000000')
    if mode=='bad_version':s.rows['brain_facts'][0]['version']=True
    if mode=='office':s.rows['brain_sections'][0]['slug']='office/head'
    if mode=='bytes':kwargs['max_bytes']=128
    with pytest.raises(module().InventoryRefused):module().build_inventory(s,'codex',**kwargs)


def test_changed_content_between_complete_scans_refuses(seeded,monkeypatch):
    s,t,o,write=seeded;original=s.rows_page;changed=False
    def read(table,**kwargs):
        nonlocal changed
        rows=original(table,**kwargs)
        if table=='brain_facts' and ('kind','eq','task') in kwargs.get('filters',()) and rows and not changed:
            changed=True;s.rows['brain_facts'][0]['detail']='changed after first page'
        return rows
    monkeypatch.setattr(s,'rows_page',read)
    with pytest.raises(module().InventoryRefused,match='changed'):module().build_inventory(s,'codex')


def test_repeated_nonadvancing_page_refuses(seeded,monkeypatch):
    s,t,o,write=seeded;original=s.rows_page
    def read(table,**kwargs):
        return original(table,**dict(kwargs,after_id=None,limit=1))
    monkeypatch.setattr(s,'rows_page',read)
    with pytest.raises(module().InventoryRefused):module().build_inventory(s,'codex')


def test_cli_consumer_matches_disputed_tasks_and_settled_orders(seeded):
    s,t,o,write=seeded
    s.patch_fact(t['id'],t['version'],dict(status='disputed',judge_pending=True))
    inventory=module().build_inventory(s,'codex')
    from bot.brain_graph.work_records import check_fact_inventory
    result=check_fact_inventory(s,'codex',inventory)
    assert result['task_ids']==[t['id']] and result['order_facts'][0]['id']==o['id']


def test_brain_digest_cannot_masquerade_as_combined_host_digest(seeded):
    s,t,o,write=seeded;result=module().build_inventory(s,'codex')
    assert len(result['brain_content_digest'])==64
    assert 'content_digest' not in result


def test_role_is_canonical_and_prose_is_not_returned(seeded):
    s,t,o,write=seeded;result=module().build_inventory(s,'Codex')
    assert result['role']=='codex' and result['tasks'][0]['owner']=='codex'
    assert 'Planted' not in json.dumps(result)


def test_known_empty_work_is_distinct_from_missing_actor_and_zero_version_is_valid(seeded):
    s,t,o,write=seeded
    task=next(r for r in s.rows['brain_facts'] if r['id']==t['id']);task['version']=0
    assert module().build_inventory(s,'codex')['tasks'][0]['version']==0
    for fact in s.rows['brain_facts']:fact['status']='retracted'
    result=module().build_inventory(s,'codex')
    assert result['complete'] and result['tasks']==[] and result['operator_order_ids']==[]


def test_mixed_office_reference_refuses_instead_of_silently_dropping_task(seeded):
    s,t,o,write=seeded
    section=s.insert('brain_sections',dict(slug='office/head',title='Office'))
    entity=s.insert('brain_entities',dict(type='actor',name='office:head',section_id=section['id']))
    next(r for r in s.rows['brain_facts'] if r['id']==t['id'])['entity_ids'].append(entity['id'])
    with pytest.raises(module().InventoryRefused,match='provenance'):module().build_inventory(s,'codex')


def test_wrong_server_filter_result_is_refused(seeded,monkeypatch):
    s,t,o,write=seeded;original=s.rows_page
    def read(table,**kwargs):
        rows=original(table,**kwargs)
        if table=='brain_facts' and rows and ('kind','eq','task') in kwargs.get('filters',()):
            rows[0]['kind']='finding'
        return rows
    monkeypatch.setattr(s,'rows_page',read)
    with pytest.raises(module().InventoryRefused):module().build_inventory(s,'codex')


@pytest.fixture
def pointer_target(seeded,monkeypatch):
    s,t,pointer,write=seeded
    target=write('decision','alpha')
    monkeypatch.setattr(module(),'LEGACY_ORDER_REFERENCES',((pointer['id'],target['id']),),raising=False)
    return s,pointer,target


def test_pinned_target_is_required_beside_pointer_and_consumer_accepts(pointer_target):
    s,pointer,target=pointer_target
    result=module().build_inventory(s,'codex')
    assert set(result['operator_order_ids'])=={pointer['id'],target['id']}
    ref=result['referenced_order_targets'][0]
    assert ref==dict(pointer_id=pointer['id'],id=target['id'],version=target['version'],status=target['status'],state='live')
    from bot.brain_graph.work_records import check_fact_inventory
    assert {r['id'] for r in check_fact_inventory(s,'codex',result)['order_facts']}=={pointer['id'],target['id']}
    assert target['statement'] not in json.dumps(result)


@pytest.mark.parametrize('field',['statement','detail','version','status','author_session'])
def test_target_same_id_content_and_provenance_move_digest(pointer_target,field):
    s,pointer,target=pointer_target;before=module().build_inventory(s,'codex')
    row=next(r for r in s.rows['brain_facts'] if r['id']==target['id'])
    row[field]=row[field]+1 if field=='version' else 'disputed' if field=='status' else 'changed full target value'
    assert module().build_inventory(s,'codex')['brain_content_digest']!=before['brain_content_digest']


@pytest.mark.parametrize('status',['retracted','superseded'])
def test_retired_target_is_hashed_and_reported_but_not_open(pointer_target,status):
    s,pointer,target=pointer_target;before=module().build_inventory(s,'codex')
    row=next(r for r in s.rows['brain_facts'] if r['id']==target['id']);row.update(status=status,version=row['version']+1,valid_to='2026-10-30T12:00:00Z')
    after=module().build_inventory(s,'codex')
    assert after['operator_order_ids']==[pointer['id']]
    assert after['referenced_order_targets'][0]['state']=='retired'
    assert after['referenced_order_targets'][0]['status']==status
    assert after['brain_content_digest']!=before['brain_content_digest']


@pytest.mark.parametrize('mode',['missing','wrong_kind','cycle','cap','office'])
def test_pinned_reference_failures_refuse_without_partial_inventory(pointer_target,monkeypatch,mode):
    s,pointer,target=pointer_target
    row=next(r for r in s.rows['brain_facts'] if r['id']==target['id'])
    if mode=='missing':s.rows['brain_facts'].remove(row)
    if mode=='wrong_kind':row['kind']='task'
    if mode=='cycle':monkeypatch.setattr(module(),'LEGACY_ORDER_REFERENCES',((pointer['id'],target['id']),(target['id'],pointer['id'])))
    if mode=='cap':monkeypatch.setattr(module(),'LEGACY_ORDER_REFERENCES',tuple((str(__import__('uuid').UUID(int=i+1)),target['id']) for i in range(65)))
    if mode=='office':
        sec=s.insert('brain_sections',dict(slug='office/head',title='Office'))
        ent=s.insert('brain_entities',dict(type='actor',name='office:head',section_id=sec['id']));row['entity_ids'].append(ent['id'])
    with pytest.raises(module().InventoryRefused):module().build_inventory(s,'codex')


def test_prose_uuid_cannot_widen_inventory(seeded):
    s,t,pointer,write=seeded;target=write('decision','alpha')
    row=next(r for r in s.rows['brain_facts'] if r['id']==pointer['id'])
    row['statement']='Full text in brain fact '+target['id']+'; read it by id.'
    result=module().build_inventory(s,'codex')
    assert target['id'] not in result['operator_order_ids']
    assert result.get('referenced_order_targets')==[]


def test_target_edit_between_scans_refuses(pointer_target,monkeypatch):
    s,pointer,target=pointer_target;original=s.rows_page;changed=False
    def read(table,**kwargs):
        nonlocal changed
        rows=original(table,**kwargs)
        if table=='brain_facts' and rows and any(f[0]=='id' and f[1]=='in' for f in kwargs.get('filters',())) and not changed:
            changed=True
            next(r for r in s.rows['brain_facts'] if r['id']==target['id'])['detail']='changed during inventory cut'
        return rows
    monkeypatch.setattr(s,'rows_page',read)
    with pytest.raises(module().InventoryRefused,match='changed'):module().build_inventory(s,'codex')


def test_retired_target_full_content_still_affects_digest(pointer_target):
    s,pointer,target=pointer_target
    row=next(r for r in s.rows['brain_facts'] if r['id']==target['id']);row.update(status='superseded',valid_to='2026-10-30T12:00:00Z')
    before=module().build_inventory(s,'codex')
    row['statement']='retired full content changed without a version bump'
    assert module().build_inventory(s,'codex')['brain_content_digest']!=before['brain_content_digest']


def test_consumer_does_not_accept_arbitrary_unlinked_prose_targets(seeded):
    s,t,pointer,write=seeded;target=write('decision','alpha')
    inventory=module().build_inventory(s,'codex');inventory['operator_order_ids'].append(target['id'])
    from bot.brain_graph.work_records import check_fact_inventory
    with pytest.raises(module().BrainRefused,match='pinned'):check_fact_inventory(s,'codex',inventory)


def test_live_target_with_retired_pointer_refuses(pointer_target):
    s,pointer,target=pointer_target
    row=next(r for r in s.rows['brain_facts'] if r['id']==pointer['id'])
    row.update(status='retracted',valid_to='2026-10-30T14:00:00Z')
    with pytest.raises(module().InventoryRefused,match='inventory_order_pointer_missing'):
        module().build_inventory(s,'codex')


def test_retired_pair_is_still_hashed_without_blocking(pointer_target):
    s,pointer,target=pointer_target
    for row in s.rows['brain_facts']:
        if row['id'] in (pointer['id'],target['id']):row.update(status='superseded',valid_to='2026-10-30T14:00:00Z')
    result=module().build_inventory(s,'codex')
    assert result['operator_order_ids']==[]
    assert result['referenced_order_targets'][0]['state']=='retired'
    next(r for r in s.rows['brain_facts'] if r['id']==target['id'])['detail']='retired target edit after pointer retirement'
    assert module().build_inventory(s,'codex')['brain_content_digest']!=result['brain_content_digest']
