import json
from sniper.office import store
from sniper.office.report import read_state


def setup(tmp_path):
    c=store.open_store(tmp_path/'office.db')
    c.execute('INSERT INTO agents VALUES(?,?)',('scout',json.dumps(dict(name='Scout',generation=2))))
    c.execute('INSERT INTO epochs VALUES(?,?)',('public',json.dumps(dict(epoch_id='public',kind='discovery',opened_at=1))))
    return c


def turn(c,key,admission=None,reason='mutation_budget',**kw):
    r=dict(actor_id='scout',generation=2,epoch_id='public',created_at=100,ended_at=110,state='completed',value=dict(reason=reason))
    if admission is not None:r['value']['request_admission']=admission
    r.update(kw)
    c.execute('INSERT OR REPLACE INTO turn_runs VALUES(?,?)',(key,json.dumps(r)))


def counts(requested=3,refused=1,truncated=0):
    return dict(state='known',requested_n=requested,refused_n=refused,truncated=truncated>0,truncated_n=truncated)


def receipt(c,tool='desk.near_pass_list',**kw):
    r=dict(actor_id='scout',generation=2,epoch_id='public',tool=tool,outcome='success',cut=dict(receipt_cut=105),result=dict(state='known',limited=False,items=[{'ticket_id':'x'},{'ticket_id':'y'}]),units={'items':2})
    r.update(kw);return store.append(c,'receipts',r)


def test_cumulative_turn_once_per_reason_unknown_not_zero_and_pending_reason(tmp_path):
    c=setup(tmp_path)
    turn(c,'t',counts(1,0));turn(c,'t',counts(7,1))
    turn(c,'zero',counts(0,0),reason='desk_judge_work_unknown')
    turn(c,'missing',reason='provider_timeout')
    turn(c,'pending',counts(2,2),reason=None,pending_step={'value':{'reason':'mixed_final_requests','request_admission':counts(2,2)}})
    c.commit();c.close();s=read_state(tmp_path,now=200.,host_now=200.)
    rows={r['reason']:r for r in s['request_admission']['items']}
    assert rows['mutation_budget']['requested_n']==7 and rows['mutation_budget']['refused_n']==1
    assert rows['mutation_budget']['turns_n']==1
    assert rows['desk_judge_work_unknown']['requested_n']==0
    assert rows['provider_timeout']['requested_n'] is None and rows['provider_timeout']['refused_n'] is None
    assert rows['mixed_final_requests']['refused_n']==2


def test_near_pass_receipts_count_list_items_separate_from_sheet_requests(tmp_path):
    c=setup(tmp_path);ref=receipt(c)
    receipt(c,'desk.near_pass_sheet',units={},result={'state':'known'})
    receipt(c,'desk.near_pass_sheet',units={},result={'reason':'tool_denied'},outcome='refused')
    c.commit();c.close();s=read_state(tmp_path,now=200.,host_now=200.)
    rows=s['near_pass_receipts']['items']
    listed=next(r for r in rows if r['tool']=='desk.near_pass_list')
    assert listed['requests_n']==1 and listed['list_items_n']==2 and ref in listed['receipt_ids']
    refused=next(r for r in rows if r['reason']=='tool_denied')
    assert refused['requests_n']==1 and refused['outcome']=='refused' and refused['list_items_n'] is None
    assert sum(r['requests_n'] for r in rows if r['tool']=='desk.near_pass_sheet')==2
    assert all(r['result_limited'] is None for r in rows if r['tool']=='desk.near_pass_sheet')


def test_private_future_missing_index_and_unknown_clock_do_not_claim_zero(tmp_path):
    c=setup(tmp_path)
    turn(c,'private',counts(),evaluation_derived=True)
    turn(c,'future',counts(),ended_at=300)
    receipt(c,cut={'receipt_cut':300});receipt(c,result={'private':True})
    c.commit();c.close();s=read_state(tmp_path,now=200.,host_now=200.)
    assert not s['request_admission']['items'] and not s['near_pass_receipts']['items']
    c=store.open_store(tmp_path/'office.db');c.execute('DROP INDEX receipts_near_pass_tool');c.commit();c.close()
    assert read_state(tmp_path,now=200.,host_now=200.)['near_pass_receipts']['state']=='unknown'
    assert read_state(tmp_path,now=None,host_now=200.)['request_admission']['state']=='unknown'


def test_legacy_dashboard_keeps_typed_counts_and_unknown():
    import subprocess
    from pathlib import Path
    code="""
const assert=require('node:assert/strict'),F=require('./static/office-floor.js');
const s={request_admission:{state:'partial',limited:true,items:[{actor_id:'scout',generation:2,reason:'mutation_budget',requested_n:7,refused_n:1,truncated_n:0}]},near_pass_receipts:{state:'known',items:[{actor_id:'scout',generation:2,tool:'desk.near_pass_list',outcome:'success',requests_n:1,list_items_n:0}]}};
assert.ok(F.requestRows(s).some(r=>r.detail.includes('refused 1')));
assert.ok(F.requestRows({}).every(r=>r.detail.includes('Unknown')));
"""
    result=subprocess.run(['node','-e',code],cwd=Path(__file__).resolve().parents[2],capture_output=True,text=True)
    assert result.returncode==0,result.stderr


def test_legacy_unknown_does_not_hide_first_new_typed_refusal(tmp_path):
    c=setup(tmp_path);turn(c,'legacy',reason='mutation_budget');turn(c,'new',counts(3,1))
    c.commit();c.close();rows=read_state(tmp_path,now=200.,host_now=200.)['request_admission']['items']
    known=next(r for r in rows if r.get('accounting_state')=='known')
    unknown=next(r for r in rows if r.get('accounting_state')=='unknown')
    assert known['refused_n']==1 and unknown['refused_n'] is None


def test_wrapper_turn_reason_is_not_fabricated_request_refusal_reason(tmp_path):
    c=setup(tmp_path);turn(c,'t',counts(3,1),reason=None)
    r=json.loads(c.execute("SELECT payload_json FROM turn_runs WHERE id='t'").fetchone()[0]);r['reason']='deadline'
    c.execute("UPDATE turn_runs SET payload_json=? WHERE id='t'",(json.dumps(r),));c.commit();c.close()
    row=read_state(tmp_path,now=200.,host_now=200.)['request_admission']['items'][0]
    assert row['refused_n']==1 and row['reason'] is None


def test_receipt_tail_is_bounded_and_preserves_explicit_list_truncation(tmp_path):
    c=setup(tmp_path)
    for _ in range(35):receipt(c,result=dict(state='known',limited=True,items=[{'ticket_id':'x'}]),units={'items':1})
    c.commit();c.close();s=read_state(tmp_path,now=200.,host_now=200.)
    assert s['near_pass_receipts']['limited'] is True
    row=s['near_pass_receipts']['items'][0]
    assert row['requests_n']==32 and row['list_items_n']==32 and row['result_limited'] is True
    assert s['read_budget']['queries']<=512 and s['read_budget']['bytes']<=1048576
