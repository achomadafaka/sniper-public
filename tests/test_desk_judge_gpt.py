"""Offline GPT caller boundaries; transport is injected, never invoked."""
import pytest

from sniper.services import memecoin_desk_judge as J
from test_desk_judge_model import _worker

GPT={'judge_model':'gpt-6-luna','judge_provider':'codex_cli','judge_effort':'low','model_billing_basis':'subscription_flat'}


def test_gpt_frozen_identity_is_explicit_and_cannot_alias_claude():
    assert J.judge_model({'policy':GPT},environ={})=='gpt-6-luna'
    for frozen,override in ((GPT,'claude-opus-5-5'),({'judge_model':'claude-opus-5-5'},'gpt-6-luna')):
        with pytest.raises(ValueError):
            J.judge_model({'policy':frozen},environ={'MEMECOIN_DESK_JUDGE_MODEL_OVERRIDE':override})
    for change in ({'judge_provider':'claude_sdk'},{'judge_effort':'none'}):
        with pytest.raises(ValueError): J.judge_model({'policy':{**GPT,**change}},environ={})


def test_gpt_proof_never_claims_a_claude_receipt_or_invents_ids_echo():
    proof=J._proof('t',{'model_requested':'gpt-6-luna','provider':'codex_cli','model_echo':None},'2026-10-27T14:00:00+00:00')
    assert proof['provider']=='codex_cli' and proof['model']=='gpt-6-luna'
    assert proof['session_id'] is None and proof['result_id'] is None


def test_lost_gpt_call_names_the_frozen_provider_without_an_sdk_receipt():
    call={'ticket_id':'t','call_id':'c','worker':'judge-123','claimed_at':'2026-10-27T14:00:00+00:00'}
    assert J._lost_flat_proof({'policy':GPT},call).startswith('model:codex_cli:subscription_flat:')
    assert J._lost_flat_proof({'policy':{'model_billing_basis':'subscription_flat'}},call).startswith('model:claude_sdk:subscription_flat:')


def test_gpt_dispatch_bypasses_claude_and_preserves_actual_missing_echo(monkeypatch):
    W=_worker();seen={}
    def forbidden(*a,**kw): raise AssertionError('Claude dispatch/spare forbidden')
    def adapter(system,user,**kw):
        seen.update(kw)
        return {'text':'{}','model_echo':None,'session_id':'thread-real','result_id':'item-real','provider':'codex_cli','billing_basis':'subscription_flat'}
    monkeypatch.setattr(W,'sdk_call_model',forbidden)
    monkeypatch.setattr(W,'gpt_call_model',adapter,raising=False)
    result=W.dispatch_judge_call('s','{}',cohort={'policy':GPT},budget=None,spares={'spare':None},make_spare=forbidden)
    assert seen['model']=='gpt-6-luna' and seen['effort']=='low' and seen['deadline_s']>0
    schema=seen['schema_obj']
    assert schema['additionalProperties'] is False and set(schema['required'])=={'k','v','e','r','c','enter_ttl_s','next_check_s'}
    assert result['model_echo'] is None and result['session_id']=='thread-real' and result['result_id']=='item-real'


def test_provision_freezes_gpt_provider_effort_and_preserves_old_default():
    import scripts.memecoin_desk_provision_cohort as P
    kw=dict(judge_p_admit=1,round_trip_loss_pct_max=8,hook_allowlist=[],rail_bounds={'tp_pct':[15,150],'sl_pct':[10,40],'trail_pct':[8,30]})
    old=P.frozen_policy(**kw)
    gpt=P.frozen_policy(**kw,judge_model='gpt-6-luna')
    assert 'judge_provider' not in old and 'judge_effort' not in old
    assert {k:gpt[k] for k in ('judge_model','judge_provider','judge_effort')}=={k:GPT[k] for k in ('judge_model','judge_provider','judge_effort')}


def test_frozen_output_cap_refuses_before_any_adapter_or_spare(monkeypatch):
    W=_worker()
    def forbidden(*a,**kw): raise AssertionError('dispatch with unsupported cap')
    monkeypatch.setattr(W,'gpt_call_model',forbidden,raising=False)
    monkeypatch.setattr(W,'sdk_call_model',forbidden)
    result=W.dispatch_judge_call('s','{}',cohort={'policy':{**GPT,'judge_max_output_tokens':256}},
                                 budget=256,spares={'spare':None},make_spare=forbidden)
    assert result['call_error']=='output_budget_unsupported'
    assert result['no_model_call'] is True


@pytest.mark.parametrize('proved',[False,True])
def test_gpt_busy_is_zero_call_only_with_explicit_pre_dispatch_proof(monkeypatch,proved):
    from contextlib import nullcontext
    from datetime import datetime,timezone
    got={}
    monkeypatch.setattr(J.store,'get_ticket',lambda *a:{'ticket_id':'t','cohort_id':'c'})
    monkeypatch.setattr(J.store,'get_cohort',lambda *a:{'policy':{**GPT,'rail_bounds':{}}})
    result={'call_error':'codex_busy','model_requested':'gpt-6-luna','no_model_call':proved}
    at='2026-10-27T14:00:00+00:00'
    J._record(None,lambda:nullcontext(None),{'ticket_id':'t','call_started_at':at,'response_at':at,'elapsed_ms':0,'result':result},
              lambda:datetime(2026,10,27,14,tzinfo=timezone.utc),
              record_fn=lambda *a,**kw:got.update(kw) or {})
    if proved:
        assert got['model_cost_usd']==0 and got['cost_provenance']=='desk:no_model_call'
    else:
        assert 'model_cost_usd' not in got
        assert got['model_cost_receipt']['provider']=='codex_cli'


def test_gpt_policy_registers_a_distinct_cohort_and_rejects_partial_identity(tmp_path):
    import sqlite3
    from sniper.services import memecoin_desk as desk
    from sniper.persistence import db_memecoin as ledger, db_memecoin_desk as store
    from test_memecoin_desk_pipeline import POLICY,AT
    dc=store.connect(str(tmp_path/'desk.db'))
    lc=sqlite3.connect(tmp_path/'ledger.db');ledger.ensure_memecoin_tables(lc)
    old=desk.provision_cohort(dc,lc,producer='U',policy={**POLICY,'judge_model':'claude-opus-5-5'},
                              prompt_version='desk-judge-v26',created_at=AT)
    new=desk.provision_cohort(dc,lc,producer='U',policy={**POLICY,**GPT},
                              prompt_version='desk-judge-v26',created_at=AT)
    assert old!=new and store.get_cohort(dc,new)['policy']['judge_provider']=='codex_cli'
    for absent in ('judge_model','judge_provider','judge_effort'):
        bad={**POLICY,**GPT};bad.pop(absent)
        with pytest.raises(ValueError):
            desk.provision_cohort(dc,lc,producer='U',policy=bad,prompt_version='desk-judge-v26',created_at=AT)
    dc.close();lc.close()


def test_gpt_retires_an_old_claude_spare_without_building_a_replacement():
    import threading
    W=_worker();closed=threading.Event()
    class Old:
        def close(self): closed.set()
    def forbidden(*a): raise AssertionError('GPT must not create Claude spare')
    slot={'spare':Old()}
    assert W.spare_for(slot,J.GPT_MODEL,forbidden) is None
    assert closed.wait(2) and slot['spare'] is None


def test_unknown_model_cannot_be_accounted_as_a_claude_provider():
    at='2026-10-27T14:00:00+00:00'
    assert J._proof('t',{'model_requested':'unknown-model'},at)['provider'] is None
    assert J._lost_flat_proof({'policy':{**GPT,'judge_model':'unknown-model'}},
        {'ticket_id':'t','call_id':'c','worker':'judge-123','claimed_at':at}) is None
