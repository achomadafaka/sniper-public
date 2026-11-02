import dataclasses
import pytest


def test_typed_provider_diagnostic_is_preserved_and_redacted_before_envelope():
    from sniper.office import models as M
    secret='sensitive request text'
    result=M.call('claude','claude-opus-5-5','system',secret,deadline_s=2,
        clock=lambda:1000.,schema_obj={'type':'object'},transport=lambda *a,**kw:dict(
            call_error='provider_error',provider_error_kind='schema',
            provider_error_detail='schema rejected '+secret+' Bearer secretcredential0123456789'))
    assert result.provider_error_kind=='schema'
    assert 'schema rejected' in result.provider_error_detail
    assert secret not in result.provider_error_detail and 'secretcredential' not in result.provider_error_detail
from sniper.office import models as M

SCHEMA = {'type':'object','required':['answer'], 'properties':{'answer':{'type':'string'}},'additionalProperties':False}


def invoke(monkeypatch, provider='claude', raw=None, **kw):
    seen=[]
    def adapter(*args, **kwargs):
        seen.append(kwargs)
        if isinstance(raw, Exception): raise raw
        return raw if raw is not None else {'text':'{"answer":"ok"}', 'billing_basis':'subscription_flat'}
    monkeypatch.setattr(M, '_claude_adapter', adapter)
    monkeypatch.setattr(M, '_codex_adapter', adapter)
    result=M.call(provider,'claude-opus-5-5' if provider=='claude' else 'gpt-6-luna','s','u',
                  deadline_s=2,schema_obj=SCHEMA,clock=lambda:1000., **kw)
    return result,seen


@pytest.mark.parametrize('provider',['claude','codex'])
@pytest.mark.parametrize('raw,outcome',[
    ({'call_error':'timeout'},'timeout'),({'call_error':'auth_expired'},'auth_revoked'),
    ({'call_error':'rate_limited'},'rate_limited'),({'text':'garbage'},'invalid'),
    ({'text':'{"answer":3}'},'schema_error'),({'text':''},'empty'),
    ({'text':'{"answer":"x","answer":"y"}'},'invalid'),
    ({'text':'{"answer":NaN}'},'invalid'),(TimeoutError(),'timeout'),
    (ValueError('private provider details'),'invalid'),({'text':'{}','is_error':True},'invalid'),
])
def test_failures_are_typed_and_missing_usage_is_unknown(monkeypatch,provider,raw,outcome):
    r,seen=invoke(monkeypatch,provider,raw)
    assert r.outcome==outcome
    assert r.usage is None
    assert len(seen)==1
    assert 'private provider details' not in repr(r)


def test_actual_identity_usage_and_cash_basis_survive(monkeypatch):
    r,_=invoke(monkeypatch,raw={'text':'{"answer":"ok"}','model_echo':'claude-opus-5-5',
        'usage':{'input_tokens':0,'output_tokens':12},'session_id':'session','result_id':'result',
        'billing_basis':'subscription_flat'}, account_id='research-1',price_list_snapshot_id='price-v1')
    assert r.outcome=='success' and r.served_model=='claude-opus-5-5'
    assert r.usage=={'input_tokens':0,'output_tokens':12,'account_id':'research-1'}
    assert r.session_id=='session' and r.result_id=='result'
    assert r.billing_basis=='subscription' and r.price_list_snapshot_id=='price-v1'
    with pytest.raises(dataclasses.FrozenInstanceError): r.text='changed'


def test_unknown_served_identity_is_not_two_family_proof(monkeypatch):
    a,_=invoke(monkeypatch,'claude')
    b,_=invoke(monkeypatch,'codex')
    assert M.independent_families(a,b) is None
    a=dataclasses.replace(a,served_model='claude-opus-5-5')
    b=dataclasses.replace(b,served_model='gpt-6-luna')
    assert M.independent_families(a,b) is True
    assert M.independent_families(a,dataclasses.replace(b,served_model='claude-sonnet-5-5')) is False
    assert M.independent_families(a,dataclasses.replace(b,served_model='unrecognized')) is None


def test_fallback_is_explicit_new_attempt_with_parent(monkeypatch):
    a,_=invoke(monkeypatch,'claude',{'call_error':'rate_limited'})
    b,_=invoke(monkeypatch,'codex',parent_attempt_id=a.attempt_id)
    assert b.attempt_id!=a.attempt_id and b.parent_attempt_id==a.attempt_id
    assert b.requested_provider=='codex' and b.requested_model=='gpt-6-luna'


def test_unknown_clock_refuses_before_adapter(monkeypatch):
    monkeypatch.setattr(M,'_claude_adapter',lambda *a,**k:pytest.fail('unknown clock dispatched'))
    r=M.call('claude','claude-opus-5-5','s','u',deadline_s=1,clock=lambda:None)
    assert r.outcome=='refused' and r.error_reason=='clock_unknown'
    assert r.started_at is None


def test_regressing_clock_is_stale_even_if_model_answers(monkeypatch):
    monkeypatch.setattr(M,'_claude_adapter',lambda *a,**k:{'text':'{}'})
    samples=iter([10.,9.])
    r=M.call('claude','claude-opus-5-5','s','u',deadline_s=1,clock=lambda:next(samples))
    assert r.outcome=='stale' and r.text is None


def test_codex_adapter_already_owns_its_slot(monkeypatch):
    from sniper.services import memecoin_desk_gpt as G
    seen=[]
    monkeypatch.setattr(G,'call_model',lambda *a,**kw:seen.append(kw) or {'text':'{}'})
    r=M.call('codex','gpt-6-luna','s','u',deadline_s=2,clock=lambda:10.,schema_obj={'type':'object'})
    assert r.outcome=='success' and len(seen)==1
    assert seen[0]['schema_obj']=={'type':'object'}


def test_failed_calls_do_not_establish_independent_decisions(monkeypatch):
    a,_=invoke(monkeypatch,'claude',{'call_error':'timeout','model_echo':'claude-opus-5-5'})
    b,_=invoke(monkeypatch,'codex',{'text':'{"answer":"ok"}','model_echo':'gpt-6-luna'})
    assert M.independent_families(a,b) is None


@pytest.mark.parametrize('deadline',[0,-1,float('nan'),True])
def test_invalid_deadline_never_dispatches(monkeypatch,deadline):
    monkeypatch.setattr(M,'_claude_adapter',lambda *a,**k:pytest.fail('invalid deadline dispatched'))
    assert M.call('claude','m','s','u',deadline_s=deadline,clock=lambda:10).outcome=='refused'


def test_error_preserves_usage_and_subscription_basis(monkeypatch):
    r,_=invoke(monkeypatch,raw={'call_error':'timeout','billing_basis':'subscription_flat',
                              'usage':{'input_tokens':123,'output_tokens':0}})
    assert r.usage=={'input_tokens':123,'output_tokens':0} and r.billing_basis=='subscription'


@pytest.mark.parametrize('echo',['gpt-6-luna',{'model':'claude-opus-5-5'}])
def test_mismatched_or_malformed_echo_refuses_answer(monkeypatch,echo):
    r,_=invoke(monkeypatch,raw={'text':'{"answer":"ok"}','model_echo':echo})
    assert r.outcome=='invalid' and r.text is None and r.error_reason=='model_mismatch'


def test_unresolvable_schema_never_raises_after_call(monkeypatch):
    monkeypatch.setattr(M,'_claude_adapter',lambda *a,**k:{'text':'{}'})
    r=M.call('claude','m','s','u',deadline_s=2,clock=lambda:10,
             schema_obj={'type':'object','$ref':'#/$defs/missing'})
    assert r.outcome=='schema_error'


def test_host_supplied_transport_keeps_envelope_validation_without_global_patch():
    seen=[]
    def transport(*a,**kw):seen.append(kw);return {'text':'{"answer":"ok"}','model_echo':None}
    r=M.call('claude','claude-opus-5-5','s','u',deadline_s=2,schema_obj=SCHEMA,clock=lambda:1000.,transport=transport)
    assert r.outcome=='success' and r.served_model is None and seen[0]['schema_obj']==SCHEMA


@pytest.mark.parametrize('error,outcome',[('rate_limit','rate_limited'),('claude_auth_near_expiry','refused'),('launcher_identity','refused')])
def test_launcher_failures_keep_scheduler_relevant_outcomes(error,outcome):
    r=M.call('codex','gpt-6-luna','s','u',deadline_s=2,clock=lambda:1000.,transport=lambda *a,**k:{'call_error':error})
    assert r.outcome==outcome and r.error_reason==error
