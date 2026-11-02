"""Counterexamples for hf2 fact boundaries; local synthetic inputs only."""
from contextlib import nullcontext

import pytest

from sniper.services import memecoin_desk_contract as K
from sniper.services import memecoin_desk_safety as S
from sniper.services import memecoin_desk_judge as J
from sniper.persistence import db_memecoin_desk as D
import test_memecoin_desk_contract as T
import test_memecoin_desk_safety as ST
from test_memecoin_desk_judge import ready_ttl, model_returning, enter, Clock
from test_memecoin_desk_pipeline import databases  # noqa: F401


@pytest.mark.parametrize('name,key', [('cluster','top_funder_pct'),('cluster','clustered_pct'),
                                      ('holders','top10_pct'),('holders','creator_pct')])
@pytest.mark.parametrize('bad',[10**1000,float('nan'),True],ids=['huge','nan','bool'])
def test_bad_soft_percent_is_unknown_without_an_exception(name,key,bad):
    checks=T.evidence()['checks'];checks[name][key]=bad
    facts=K.soft_facts(checks)
    public=key+'_of_attempted' if name=='cluster' else key
    assert facts[name][public] is None
    K.validate_soft_facts(facts)


def test_huge_flat_soft_number_is_a_typed_shape_refusal():
    c=T.candidate();ev=T.evidence(c)
    ev['checks']['holders']['top10_pct']=10**1000
    out=K.hard_filter(c,evidence=ev,policy=T.policy())
    assert out['action']=='SKIP' and out['reason']=='unknown:evidence:shape'


def test_huge_forged_public_fact_is_a_validation_error_not_overflow():
    facts=K.soft_facts(T.evidence()['checks'])
    facts['holders']['top10_pct']=10**1000
    with pytest.raises(ValueError,match='soft_facts'):K.validate_soft_facts(facts)


def test_cluster_percent_names_state_the_attempted_supply_denominator():
    facts=K.soft_facts({'cluster':{'status':'kill','reason':'cluster_top_funder_high',
                                 'top_funder_pct':55,'clustered_pct':60}})
    c=facts['cluster']
    assert c['top_funder_pct_of_attempted']==55 and c['clustered_pct_of_attempted']==60
    assert c['attempted_pct_of_supply'] is None
    assert 'top_funder_pct' not in c and 'clustered_pct' not in c


def test_creator_with_many_unseen_accounts_has_no_owner_share_upper_bound():
    # Twenty visible accounts x10; creator can own 100 unseen accounts x9 = 81.8%.
    accounts=[ST.tok(ST.MINT,ST.owner(i+10),10) for i in range(20)]
    meta={}
    row=S._holders(ST.MINT,{'supply':1100},[{}]*20,accounts,S.curve_address(ST.MINT),ST.CREATOR,None,meta)
    assert row['status']=='pass' and row['creator_basis']=='below_page'
    assert row['creator_pct'] is None and row['reason']=='creator_unresolved_below_page'
    assert 'creator_pct_bound' not in meta
    facts=K.soft_facts({'holders':row})['holders']
    assert facts['creator_pct'] is None and facts['top10_pct']>0


def test_giant_stored_fact_cannot_leave_a_bare_judge_claim(databases):
    ticket=ready_ttl(databases);clock=Clock();calls=[]
    checks=T.evidence()['checks'];checks['holders']['top10_pct']=10**1000
    J.run_once(databases[0],lambda:nullcontext(databases[1]),system_prompt='offline',worker='test',
               now_fn=clock,calls_per_hour=100,soft_fn=lambda _:K.soft_facts(checks),
               call_model=model_returning(enter,calls=calls,clock=clock))
    assert len(calls)==1 and calls[0]['evidence']['holders']['top10_pct'] is None
    vet=next(s for s in D.get_ticket(databases[0],ticket)['stages'] if s['stage']=='vet')
    assert vet['inputs']['sheet']==calls[0] and D.list_judge_calls_to_record(databases[0])==[]


def test_unknown_cluster_with_a_flagged_reason_cannot_set_top_funder_high():
    facts = K.soft_facts({'cluster': {'status': 'unknown', 'reason': 'cluster_top_funder_high'}})
    assert facts['cluster']['check'] == 'unknown' and facts['cluster']['top_funder_high'] is False


def test_unknown_cluster_cannot_present_placeholder_zeros_as_evidence():
    f=K.soft_facts({'cluster':{'status':'unknown','reason':'cluster_unknown_budget',
                              'top_funder_pct':0,'clustered_pct':0,'attempted_pct_of_supply':0}})
    c=f['cluster']
    assert c['check']=='unknown'
    assert all(c[k] is None for k in ('top_funder_pct_of_attempted','clustered_pct_of_attempted','attempted_pct_of_supply'))


def test_saved_v13_soft_shape_is_preserved_only_for_explicit_recovery():
    current=K.soft_facts(T.evidence()['checks'])
    c=current['cluster']
    old={'cluster':{'state':c['state'],'check':c['check'],'reason':c['reason'],
                    'top_funder_high':c['top_funder_high'],'top_funder_pct':None,'clustered_pct':None},
         'holders':current['holders']}
    with pytest.raises(ValueError,match='soft_facts'):K.render_judge_sheet(T.candidate(),soft=old)
    sheet=K.render_judge_sheet(T.candidate(),soft=old,allow_legacy_soft=True)
    assert sheet['evidence']==old and 'top_funder_pct_of_attempted' not in sheet['evidence']['cluster']
