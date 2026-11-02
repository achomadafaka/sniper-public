"""Real structured AgentStep -> Broker delivery, with offline typed handler fixtures."""
from dataclasses import replace
import json
from sniper.office import broker as B,store as S
from .test_runtime_steps import actor,context,runner,Client
import pytest


@pytest.mark.parametrize('tool,args',[('desk.near_pass_list',{}),('desk.near_pass_sheet',{'ticket_id':'near-ticket'})])
@pytest.mark.parametrize('role',['scout','head','judge'])
def test_model_turn_near_pass_request_reaches_scout_broker_only(tmp_path,tool,args,role):
    db=S.open_store(tmp_path/'office.db');called=[]
    def handler(actual,ctx):
        called.append((actual,ctx.role))
        return B.ToolResult({'feed':'near_pass','blockers':[{'status':'unknown'}]}, {},'solana',[],'observed')
    broker=B.Broker(db,None,{tool:B.Tool(handler,{} if not args else {'ticket_id':str},'fixture-near-pass-v1')},generation_check=lambda _:True)
    client=Client({'text':json.dumps(dict(done=False,task='read',memo='',refs=None,requests=[{'tool':tool,'args':args}])),
                   'model_echo':'claude-opus-5-5'})
    step=runner(client);slot='judge_claude' if role=='judge' else role
    step.manifest['seats'][slot]=step.manifest['seats']['head'];step.prompts[slot]=step.prompts['head']
    step.actor=lambda _:dict(actor(),role={'scout':'Scout','head':'Head','judge':'Judge'}[role])
    step.read_tool=lambda ctx,name,actual:broker.execute(B.ToolRequest(ctx.actor_id,name,actual,ctx.decision_id,ctx.cut),context=ctx)
    result=step(replace(context(),role=role),{},10.)
    receipt=result.value['receipts'][0]
    if role=='scout':
        assert called==[(args,'scout')]
        assert receipt['outcome']=='success' and receipt['result']['feed']=='near_pass'
    else:
        assert called==[] and receipt['outcome']=='refused' and receipt['result']['reason']=='tool_denied'
    assert db.execute('SELECT count(*) FROM receipts').fetchone()[0]==1
    db.close()


def test_ineligible_sheet_broker_refusal_reaches_model_context(tmp_path):
    db=S.open_store(tmp_path/'office.db')
    tool='desk.near_pass_sheet'
    broker=B.Broker(db,None,{tool:B.Tool(lambda a,c:B.ToolResult({'state':'refused','reason':'not_near_pass_eligible'}, {},'solana',[],'unknown',outcome='refused'),{'ticket_id':str},'fixture-near-pass-v1')},generation_check=lambda _:True)
    client=Client({'text':json.dumps(dict(done=False,task='read',memo='',refs=None,requests=[{'tool':tool,'args':{'ticket_id':'ineligible'}}])), 'model_echo':'claude-opus-5-5'})
    step=runner(client);step.actor=lambda _:dict(actor(),role='Scout')
    step.manifest['seats']['scout']=step.manifest['seats']['head'];step.prompts['scout']=step.prompts['head']
    step.read_tool=lambda ctx,name,args:broker.execute(B.ToolRequest(ctx.actor_id,name,args,ctx.decision_id,ctx.cut),context=ctx)
    result=step(replace(context(),role='scout'),{},10.)
    assert result.value['receipts'][0]['result']['reason']=='not_near_pass_eligible'
    db.close()


@pytest.mark.parametrize('role',['risk','trader'])
@pytest.mark.parametrize('tool,args',[('desk.near_pass_list',{}),('desk.near_pass_sheet',{'ticket_id':'near-ticket'})])
def test_deterministic_seats_cannot_call_near_pass_tools(tmp_path,role,tool,args):
    db=S.open_store(tmp_path/'office.db');called=[]
    broker=B.Broker(db,None,{tool:B.Tool(lambda *a:called.append(a),{} if not args else {'ticket_id':str},'fixture')},generation_check=lambda _:True)
    ctx=replace(context(),role=role)
    receipt=broker.execute(B.ToolRequest(ctx.actor_id,tool,args,ctx.decision_id,ctx.cut),context=ctx)
    assert receipt.outcome=='refused' and receipt.result['reason']=='tool_denied' and called==[]
    db.close()


@pytest.mark.parametrize('args',[{'ticket_id':''},{'ticket_id':'x'*161},{'ticket_id':1},{'ticket_id':'ok','extra':True}])
def test_near_pass_schema_rejects_invalid_ticket_shape(args):
    from jsonschema import Draft202012Validator
    from sniper.office.runtime_steps import SCHEMA
    doc=dict(done=False,task='',memo='',refs=None,requests=[{'tool':'desk.near_pass_sheet','args':args}])
    assert not Draft202012Validator(SCHEMA).is_valid(doc)
