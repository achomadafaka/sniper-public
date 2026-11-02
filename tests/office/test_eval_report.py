"""Read-only eval-1 report contract. No efficacy output before an external look seal."""
import hashlib
import importlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from sniper.office import store as S, ledger as L, judge_decisions as J, eval_orders as EO, eval_transition as X
from sniper.office.supervisor import _put
from sniper.office.tools import sheet as SH
from office.test_pilot_seats import policy

E=X.NEW
T=datetime(2026,10,1,tzinfo=timezone.utc).timestamp()
sha=lambda v:hashlib.sha256(S.canonical_json(v)).hexdigest()


def health_quarantine(value,path=()):
    """Labels/provenance are allowed; money/results never pass under a renamed key."""
    forbidden=re.compile(r'pnl|net_usd|equity_usd|realized|profit|win|median|worst|top3|per_coin|closed.*(?:up|down|flat)',re.I)
    amount=re.compile(r'\$\s*[+-]?\d|[+-]?\d+(?:\.\d+)?\s*USD',re.I)
    metadata={'network_id','equity_snapshots','ledger.equity_snapshots'}
    valuation_keys={'state','reason','halt_kind','halted_at','unpriced_count','unpriced_order_ids','demoted_count',
        'demoted_order_ids','demoted_seconds','demoted_never_priced_order_ids','repromotions','last_at'}
    if isinstance(value,dict):
        for key,item in value.items():
            if path==('valuation',):assert key in valuation_keys
            if key not in metadata:assert not forbidden.search(key)
            if isinstance(item,(int,float)) and not isinstance(item,bool) and key.endswith('_usd'):
                assert key in ('clip_usd','capital_usd')
            health_quarantine(item,path+(key,))
    elif isinstance(value,list):
        for item in value:health_quarantine(item,path+('[]',))
    elif isinstance(value,str):assert not amount.search(value)


@pytest.fixture
def world(tmp_path):
    office=tmp_path/'office.db';ledger=tmp_path/'ledger.db';desk=tmp_path/'desk.db'
    o=S.open_store(office);l=L.open_ledger(ledger);d=sqlite3.connect(desk)
    hard={'require_collected':['chart','holders']};p={'hard_filter':hard}
    cohort={'id':'core-eval','policy_hash':sha(p),'hard_policy_hash':sha(hard),'require_collected':['chart','holders']}
    _put(o,'epochs',E,{'epoch_id':E,'kind':'paper_eval','opened_at':T,'first_order_at':None,'closed_at':None,
        'book_id':E,'ledger_epoch_id':E,'operator_waived_gates':True,'record':{'near_pass_cohort':cohort}})
    o.execute('CREATE TABLE office_epoch_control(id INTEGER PRIMARY KEY,payload_json TEXT NOT NULL)')
    o.execute('INSERT INTO office_epoch_control VALUES(1,?)',(S.canonical_json({'active_epoch':E}).decode(),))
    L.open_epoch(l,epoch_id=E,clip_usd=25.,rule_version=EO.PODS_RULE['version'],pods_rule=EO.PODS_RULE,
                 actor='crew',note=S.canonical_json(X.LEDGER_NOTE).decode(),now=T)
    L.open_book(l,book_id=E,epoch_id=E,kind='evaluated',actor='crew',note='test',now=T)
    L.fund(l,book_id=E,usd=1000.,kind='initial',actor='crew',note='test',now=T)
    d.executescript('CREATE TABLE desk_cohorts(cohort_id TEXT PRIMARY KEY,policy_json TEXT);'
        'CREATE TABLE desk_tickets(id INTEGER PRIMARY KEY,ticket_id TEXT,cohort_id TEXT,network_id TEXT,mint TEXT,pool TEXT,'
        'stage TEXT,verdict TEXT,reason TEXT,inputs_json TEXT,inputs_hash TEXT,ts TEXT);'
        'CREATE INDEX idx_desk_tickets_stage_ts ON desk_tickets(stage,ts);'
        'CREATE INDEX idx_desk_tickets_coin ON desk_tickets(network_id,mint,ts);')
    d.execute('INSERT INTO desk_cohorts VALUES(?,?)',(cohort['id'],S.canonical_json(p).decode()));d.commit()
    yield SimpleNamespace(o=o,l=l,d=d,office=office,ledger=ledger,desk=desk,root=tmp_path)
    o.close();l.close();d.close()


def report(w,now=T+100,**kw):
    w.o.commit()  # A separate RO snapshot sees only published fixture inputs.
    R=importlib.import_module('sniper.office.eval_report')
    return R.build_report(w.office,w.ledger,now=now,desk_path=w.desk,epoch_id=E,**kw)


def candidate(w,i,*,unknown=(),price='tape',at=T+10,feed='pass'):
    checks={k:{'status':'unknown' if k in unknown else 'pass','reason':k+'_unknown' if k in unknown else 'ok'}
            for k in ('market_data','token_safety','sell_probe','dump_group','cluster','chart','holders')}
    emission={'network_id':'solana','mint':'mint-'+str(i),'pool':'pool-'+str(i),
              'snapshot':{'hash':sha(['snapshot',i]),'emission_at':at}}
    inputs={'evidence':{'checks':checks},'emission_candidate':emission}
    w.d.execute('INSERT INTO desk_tickets VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
        (i+1,'ticket-'+str(i),'core-eval','solana',emission['mint'],emission['pool'],'hard_filter','SKIP' if feed=='near_pass' else 'PASS','unknown:chart' if feed=='near_pass' else 'pass',
         S.canonical_json(inputs).decode(),sha(inputs),datetime.fromtimestamp(at,timezone.utc).isoformat()));w.d.commit()
    request={'network_id':'solana','mint':emission['mint'],'pool':emission['pool'],'strategy_epoch':'core-eval',
             'judged_snapshot_hash':emission['snapshot']['hash'],'feed':feed}
    body={'pool_state':{'virtual_quote_source':'pumpswap_rpc:pool_state' if price=='exact' else 'tape_tip'},
          'creator_history':{'state':'unknown','reason':'not_observed'}}
    rec={'kind':'sheet','state':'known','contract':SH.OFFICE_SHEET_CONTRACT['version'],'request':request,
         'cache_key':sha(request),'definition_bundle_hash':'d'*64,'strategy_epoch':'core-eval',
         'clock_sample':{'corrected_utc':at},'sheet':body,'sheet_hash':sha(body),'lenses':{},'lenses_hash':sha({})}
    rid=S.append(w.o,'receipts',rec)
    return rid,rec


def publish(w,rid,*,decisions=('ENTER','ENTER'),at=T+20):
    args={'decision':decisions[0],'candidate':rid,'order_id':None,'public_reason':'observed'}
    did=J.decision_id(E,args)
    for slot,decision in zip(J.JUDGE_SLOTS,decisions):
        ctx=SimpleNamespace(epoch_id=E,actor_id=slot,generation=1,decision_id=slot+did,cut={'knowledge_cut':at-1})
        J.record(w.o,context=ctx,slot=slot,args={**args,'decision':decision},now=at,
                 equity_snapshot_id='snapshot',lesson_ids=[])
    return did


def order(w,i,rid,rec,did,*,closed=True,unpriced=False,exit_by='rails'):
    bh=SH.freeze_bundle(w.o,did,rec['cache_key'],definition_bundle_hash='d'*64,strategy_epoch='core-eval')
    oid='order-'+str(i);requested=T+10+i;filled=requested+2;ended=filled+5 if closed else None
    w.l.execute('INSERT INTO orders(order_id,book_id,epoch_id,book_kind,network_id,mint,pool,decision_bundle_hash,policy_json,'
        'policy_hash,fill_model_version,state,requested_at,filled_at,spend_usd,tokens_atomic,closed_at,exit_by,costs_usd) '
        'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (oid,E,E,'evaluated','solana',rec['request']['mint'],rec['request']['pool'],bh,policy().frozen(),'p',
         'office-ps-paper-v1','closed' if closed else 'open',requested,filled,25.,'1000000',ended,exit_by,0.))
    for kind,value,at in [('entry',-25.,filled)]+([('exit',26.,ended)] if closed else []):
        w.l.execute('INSERT INTO cashflows(order_id,kind,at,lamports,cost_lamports,cost_basis,cost_evidence,fx_usd,fx_at,usd) '
            'VALUES(?,?,?,?,?,?,?,?,?,?)',(oid,kind,at,'1','0','fixture','fixture',150.,at,value))
    return oid


def filled_cohort(w,n=30):
    for i in range(n):
        rid,rec=candidate(w,i,unknown=('dump_group',) if i%2 else (),price='exact' if i%2 else 'tape')
        did=publish(w,rid);order(w,i,rid,rec,did,exit_by=None if i==0 else 'rails')


def seal(w,now,**kw):
    R=importlib.import_module('sniper.office.eval_report')
    seed_safety(w,now)
    prep_mode='look' if any(kw.get('limits',{}).get(k,v)>v for k,v in R.DEFAULT_LIMITS.items()) else 'health'
    h=report(w,now=now,mode=prep_mode,**kw)
    rec={'version':'office-eval-look-v2','epoch_id':E,'look_at':now,'report_script_sha256':R.script_sha256(),
         'input_heads':h['input_heads'],'eligible_counts':h['eligibility'],'limits':h['limits'],
         'fx':{'sol_usd':kw.get('sol_usd'),'sol_at':kw.get('sol_at')},
         'receipt_seq_cut':h['receipt_seq_cut'],'safety_snapshot':h['safety_snapshot'],
         'descriptive_snapshot':h['descriptive_snapshot'],'descriptive_snapshot_sha256':h['descriptive_snapshot_sha256']}
    raw=S.canonical_json(rec)
    return raw,hashlib.sha256(raw).hexdigest()


def test_health_has_counts_but_never_quarantined_numbers_even_when_eligible(world):
    filled_cohort(world)
    out=report(world,now=T+14*86400+100)
    assert out['eligibility']['eligible_as_of'] is True and out['orders']['filled_coins']==30
    text=json.dumps(out)
    assert all(k not in text for k in ('net_usd','equity_usd','trading_net_usd','win_count','daily_realized_usd'))
    assert out['economics']['state']=='withheld' and out['operator_waived_gates'] is True
    health_quarantine(out)


def test_no_seal_or_bad_seal_never_prints_net(world):
    filled_cohort(world);now=T+14*86400+100
    absent=report(world,now=now,mode='look')
    assert absent['economics']=={'state':'withheld','reason':'look_record_required'}
    raw,digest=seal(world,now)
    assert report(world,now=now,mode='look',look_record=raw,look_sha256='a'*64)['economics']['state']=='withheld'


def test_sealed_eligible_look_is_deterministic_and_changed_inputs_refuse(world):
    filled_cohort(world);now=T+14*86400+100
    raw,digest=seal(world,now)
    a=report(world,now=now,mode='look',look_record=raw,look_sha256=digest)
    assert a==report(world,now=now,mode='look',look_record=raw,look_sha256=digest)
    assert a['economics']['state']=='known' and a['economics']['trading_net_usd']==pytest.approx(30.)
    assert a['economics']['net_usd']==pytest.approx(30.-15*800/31)
    world.l.execute("UPDATE cashflows SET usd=27 WHERE order_id='order-0' AND kind='exit'")
    out=report(world,now=now,mode='look',look_record=raw,look_sha256=digest)
    assert out['economics']['state']=='withheld' and 'net_usd' not in out['economics']


def test_prelook_and_failed_epoch_withhold_even_with_valid_sha(world):
    filled_cohort(world);now=T+100
    raw,digest=seal(world,now)
    assert report(world,now=now,mode='look',look_record=raw,look_sha256=digest)['economics']['state']=='withheld'
    S.append(world.o,'receipts',{'kind':'epoch.end','cache_key':'epoch.end:'+E,'epoch_id':E,'status':'FAILED',
                              'known_at':T+80,'halted_at':T+80,'halt_kind':'manual'})
    now=T+14*86400+100;raw,digest=seal(world,now)
    assert report(world,now=now,mode='look',look_record=raw,look_sha256=digest)['economics']['state']=='withheld'


def test_core_strata_seen_coverage_and_enter_never_ordered_reason_are_explicit(world):
    rid,rec=candidate(world,0,unknown=('dump_group',),price='exact');did=publish(world,rid)
    S.append(world.o,'receipts',{'kind':'judge.settle','cache_key':'judge.settle:'+did,'decision_id':did,'label':'ENTER',
        'known_at':T+30,'result':{'state':'refused','reason':'max_concurrent','order_id':None}})
    out=report(world)
    assert out['admissions']['denominator']=='admitted by the near-pass cohort hard filter (desk tickets)'
    assert out['strata']['core_list']['unknown_dump_group']['admitted_coins']==1
    assert out['strata']['price_source']['exact']['ENTER']==1
    assert out['enter_not_ordered']['by_reason']['max_concurrent']==1


def test_exit_split_uses_asker_rows_and_null_cause_stays_unknown(world):
    rid,rec=candidate(world,0);did=publish(world,rid);oid=order(world,0,rid,rec,did,exit_by=None)
    world.l.execute('INSERT INTO exit_askers VALUES(?,?,?,?,?,?)',(oid,'judge_claude','exit-id','reason',
        json.dumps({'judge_claude':'EXIT','judge_gpt':'EXIT'}),T+30))
    out=report(world)
    assert out['exits']['by_cause']['unknown_cause']==1
    assert out['exits']['asker_count_split']['one_judge']==1 and out['exits']['asker_count_split']['two_judges']==0


def test_limits_and_missing_sources_are_unknown_not_false_zero(world):
    for i in range(3):candidate(world,i)
    out=report(world,limits={'max_rows':1})
    assert out['state'] in ('partial','unknown') and out['economics']['state']=='withheld'
    assert out['coverage']['complete'] is False
    missing=world.root/'missing.db'
    R=importlib.import_module('sniper.office.eval_report')
    out=R.build_report(world.office,world.ledger,now=T+100,desk_path=missing,epoch_id=E)
    assert out['admissions']['state']=='unknown' and not missing.exists()


def test_reads_do_not_mutate_or_checkpoint_any_input(world):
    world.o.commit()
    before=[c.total_changes for c in (world.o,world.l,world.d)]
    data=['\n'.join(c.iterdump()) for c in (world.o,world.l,world.d)]
    report(world)
    assert before==[c.total_changes for c in (world.o,world.l,world.d)]
    assert data==['\n'.join(c.iterdump()) for c in (world.o,world.l,world.d)]


@pytest.mark.parametrize('unpriced_count',[1,6,7])
def test_ceiling_look_lower_bound_is_partial_and_censored_share_can_refuse(world,unpriced_count):
    for i in range(30):
        rid,rec=candidate(world,i);did=publish(world,rid)
        order(world,i,rid,rec,did,closed=i>=unpriced_count)
    now=T+28*86400+100
    # Fresh typed no-route observations are a conservative bound, not stale points.
    for i in range(unpriced_count):
        world.l.execute('INSERT INTO marks(order_id,at,lamports,source,reason) VALUES(?,?,?,?,?)',
                        ('order-'+str(i),now,None,'ps_paper.sell','no_route'))
    raw,digest=seal(world,now,sol_usd=150.,sol_at=now)
    out=report(world,now=now,mode='look',look_record=raw,look_sha256=digest,sol_usd=150.,sol_at=now)
    if unpriced_count==7:
        assert out['economics']['state']=='withheld' and out['economics']['reason']=='censored_share_exceeds_20pct'
    else:
        assert out['economics']['state']=='partial' and out['economics']['unpriced_count']==unpriced_count
        assert set(out['economics']['unpriced_order_ids'])=={'order-'+str(i) for i in range(unpriced_count)}
        assert out['economics']['net_usd'] is not None and out['economics']['value_kind']=='lower_bound'
        assert out['economics']['last_mark_reading']['state']=='unknown'
        assert out['economics']['last_mark_reading']['label']=='unverified'


def test_unpublished_future_and_foreign_epoch_judgements_never_enter_counts(world):
    rid,rec=candidate(world,0)
    args={'decision':'ENTER','candidate':rid,'order_id':None,'public_reason':'PRIVATE ONE SEAT'}
    ctx=SimpleNamespace(epoch_id=E,actor_id='judge',generation=1,decision_id='one',cut={'knowledge_cut':T+10})
    J.record(world.o,context=ctx,slot='judge_claude',args=args,now=T+20,equity_snapshot_id='s',lesson_ids=[])
    out=report(world)
    assert out['decisions']['published_targets']==0 and 'PRIVATE ONE SEAT' not in json.dumps(out)
    ctx.epoch_id='foreign'
    J.record(world.o,context=ctx,slot='judge_gpt',args=args,now=T+21,equity_snapshot_id='s',lesson_ids=[])
    assert report(world)['decisions']['published_targets']==0
    ctx.epoch_id=E
    J.record(world.o,context=ctx,slot='judge_gpt',args=args,now=T+1000,equity_snapshot_id='s',lesson_ids=[])
    assert report(world)['decisions']['published_targets']==0


def test_look_record_pins_script_cut_limits_and_eligible_counts(world):
    filled_cohort(world);now=T+14*86400+100
    raw,_=seal(world,now)
    for field,value in [('report_script_sha256','a'*64),('look_at',now-1),('eligible_counts',{}),('limits',{})]:
        record=json.loads(raw);record[field]=value;changed=S.canonical_json(record)
        out=report(world,now=now,mode='look',look_record=changed,look_sha256=hashlib.sha256(changed).hexdigest())
        assert out['economics']['state']=='withheld' and 'net_usd' not in out['economics']


@pytest.mark.parametrize('n,open_one,days,eligible',[(30,True,14,False),(29,False,14,False),(2,False,28,True)])
def test_look_sample_terminal_rule_and_underpowered_ceiling(world,n,open_one,days,eligible):
    for i in range(n):
        rid,rec=candidate(world,i);did=publish(world,rid)
        order(world,i,rid,rec,did,closed=not(open_one and i==0))
    now=T+days*86400+100
    h=report(world,now=now)
    assert h['eligibility']['eligible_as_of'] is eligible
    if eligible:
        raw,digest=seal(world,now)
        out=report(world,now=now,mode='look',look_record=raw,look_sha256=digest)
        assert out['economics']['underpowered'] is True


def test_broken_epoch_does_not_release_efficacy(world):
    filled_cohort(world)
    S.append(world.o,'receipts',{'kind':'epoch.end','cache_key':'epoch.end:'+E,'epoch_id':E,'status':'BROKEN',
        'halt_kind':'config_drift','halted_at':T+80,'known_at':T+80})
    now=T+14*86400+100;raw,digest=seal(world,now)
    assert report(world,now=now,mode='look',look_record=raw,look_sha256=digest)['economics']['state']=='withheld'


def test_every_core_stratum_and_unpriced_attempt_reasons_are_separate(world):
    for i,unknown in enumerate(((),('dump_group',),('cluster',),('dump_group','cluster'))):
        rid,rec=candidate(world,i,unknown=unknown);publish(world,rid)
    S.append(world.o,'receipts',{'tool':'desk.near_pass_sheet','cut':{'receipt_cut':T+20},
        'outcome':'unknown','result':{'kind':'sheet','state':'unknown','reason':'rpc_cap'}})
    out=report(world)
    core=out['strata']['core_list']
    assert all(core[k]['admitted_coins']==1 for k in ('all_known','unknown_dump_group','unknown_cluster','both_unknown'))
    attempt=out['strata']['price_source']['unpriced_attempts']
    assert attempt['by_reason']=={'rpc_cap':1} and attempt['coin_state']=='coins_unknown'
    assert attempt['distinct_coins'] is None
    assert out['admissions']['admitted_tickets']==4 and out['seen']['sheet_coins']==4
    assert out['seen']['seen_tickets']==4
    assert out['coverage']['by_hour'][0]['admitted_tickets']==4


@pytest.mark.parametrize('blocker',['missing_input','policy','merit',None])
def test_split_and_wait_blockers_use_only_published_pairs(world,blocker):
    rid,_=candidate(world,0);did=publish(world,rid,decisions=('ENTER','WAIT'))
    rid,_=candidate(world,1);wait_id=publish(world,rid,decisions=('WAIT','WAIT'))
    for slot in J.JUDGE_SLOTS:
        memo='WAIT reason' if blocker is None else 'WAIT blocker='+blocker
        _put(world.o,'messages',__import__('sniper.office.supervisor',fromlist=['action_key']).action_key(slot+wait_id,'memo.commit'),
             {'actor_id':slot,'request':{'memo':memo,'served_model':'model'}})
    out=report(world)
    assert out['decisions']['by_label']['split']==1 and out['decisions']['by_label']['WAIT']==1
    assert out['decisions']['wait_by_blocker'][blocker or 'unknown']==1
    assert 'WAIT reason' not in json.dumps(out)


def test_safety_is_state_only_with_typed_demotion_and_halt_provenance(world):
    L.evaluation_safety(world.l,epoch_id=E,now=T+1)
    world.l.execute("UPDATE epoch_entry_safety SET state='halted',halt_kind='manual',halted_at=?,"
        "unpriced_count=1,unpriced_order_ids_json='[\"o\"]',demoted_order_ids_json='[\"o\"]',"
        "demoted_never_priced_order_ids_json='[\"o\"]',demoted_seconds_json='{\"o\":100}',repromotions=2",(T+2,))
    out=report(world)
    assert out['valuation']['halt_kind']=='manual' and out['valuation']['halted_at']==T+2
    assert out['valuation']['demoted_order_ids']==['o'] and out['valuation']['repromotions']==2
    assert out['valuation']['demoted_never_priced_order_ids']==['o']
    assert out['valuation']['demoted_seconds']=={'o':100}
    world.l.execute('DROP TABLE epoch_entry_safety')
    missing=report(world)
    assert missing['valuation']['state']=='unknown'
    assert all(row['valuation']['state']=='unknown' and row['valuation']['demoted_count'] is None for row in missing['feeds'].values())


def test_silent_intervals_and_planned_gap_evidence_are_reported_without_auto_censor(world):
    for i,(start,end) in enumerate(((T+5,T+10),(T+500,T+505))):
        world.o.execute('INSERT INTO model_calls(call_id,actor_id,generation,turn_id,requested_provider,requested_model,'
            'started_at,ended_at,outcome,usage_state,cost_basis,epoch_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            (str(i),'actor',1,'turn-'+str(i),'claude','model',start,end,'success','unknown','unknown',E))
    world.o.commit()
    _put(world.o,'heartbeats','supervisor',{'at':T+505,'frontiers':{'state':'working'}})
    out=report(world,now=T+600)
    assert any(g['from_at']==T+10 and g['to_at']==T+500 for g in out['coverage']['call_silent_intervals'])
    assert out['coverage']['heartbeat_history']['state']=='unknown'
    assert out['coverage']['gap_policy']=='reported_not_censored'
    out=report(world,now=T+90000)
    assert out['coverage']['outage_failure_condition_observed'] is True


def test_required_table_failure_is_not_an_empty_complete_collection(world):
    world.l.execute('DROP TABLE exit_askers')
    out=report(world)
    assert out['coverage']['complete'] is False and out['exits']['state']=='unknown'
    assert 'ledger.exit_askers' in out['coverage']['required_missing']


@pytest.mark.parametrize('status',['ineligible','eligible','FAILED','partial'])
def test_health_quarantine_is_structural_in_every_report_state(world,status):
    if status=='eligible':filled_cohort(world)
    elif status=='FAILED':
        S.append(world.o,'receipts',{'kind':'epoch.end','cache_key':'epoch.end:'+E,'epoch_id':E,'status':'FAILED',
            'halted_at':T+50,'known_at':T+50,'halt_kind':'manual'})
    elif status=='partial':
        for i in range(3):candidate(world,i)
    out=report(world,now=T+14*86400+100,**({'limits':{'max_rows':1}} if status=='partial' else {}))
    health_quarantine(out)
    for key,value in [('closed_calls_up',7),('bag_value_usd',12.),('note','$12.3'),('note','12.3 USD')]:
        changed=dict(out,**{key:value})
        with pytest.raises(AssertionError):health_quarantine(changed)


def test_missing_cashflow_is_not_zero_and_missing_control_withholds(world):
    filled_cohort(world);now=T+14*86400+100
    world.l.execute("DELETE FROM cashflows WHERE order_id='order-0' AND kind='exit'")
    raw,digest=seal(world,now)
    assert report(world,now=now,mode='look',look_record=raw,look_sha256=digest)['economics']['state']=='withheld'


def test_actual_tape_source_is_tape_not_unknown(world):
    rid,rec=candidate(world,0)
    rec['sheet']['pool_state']['virtual_quote_source']='pumpswap_grpc:buy_event_virtual_quote'
    rec['sheet_hash']=sha(rec['sheet'])
    rid=S.append(world.o,'receipts',rec);publish(world,rid)
    assert report(world)['strata']['price_source']['tape']['ENTER']==1


def test_missing_epoch_control_is_required(world):
    world.o.execute('DROP TABLE office_epoch_control')
    out=report(world)
    assert not out['coverage']['complete'] and 'office.office_epoch_control' in out['coverage']['required_missing']


def test_am10_feeds_are_not_pooled_and_unknown_is_not_pass(world):
    for i,feed in enumerate(('pass','near_pass','alien')):
        rid,rec=candidate(world,i,feed=feed);did=publish(world,rid);order(world,i,rid,rec,did)
    h=report(world)
    assert h['feeds']['pass']['decisions']['ENTER']==1
    assert h['feeds']['near_pass']['decisions']['ENTER']==1
    assert h['feeds']['unknown']['decisions']['ENTER']==1
    assert h['feeds']['pass']['filled']==1 and h['feeds']['near_pass']['closes']==1
    assert h['admissions']['admitted_tickets']==2 and h['admissions']['refused_unknown_tickets']==1
    assert h['feeds']['unknown']['source_feed_values']==['alien']
    health_quarantine(h)
    now=T+28*86400+100;raw,digest=seal(world,now)
    look=report(world,now=now,mode='look',look_record=raw,look_sha256=digest)
    perfeed=look['economics']['per_feed']
    assert sum(v['trading_net_usd'] for v in perfeed.values())==pytest.approx(look['economics']['trading_net_usd'])
    assert all(v['label']=='descriptive' for v in perfeed.values())


def test_am10_missing_feed_key_is_the_pass_feed_and_unseen_ticket_is_kept(world):   # SH.sheet writes request.feed only for near_pass
    rid,rec=candidate(world,0);del rec['request']['feed'];rec['cache_key']=sha(rec['request'])
    rid=S.append(world.o,'receipts',rec);publish(world,rid)
    candidate(world,1,feed='near_pass')
    out=report(world)
    assert out['feeds']['pass']['decisions']['ENTER']==1 and not out['feeds']['unknown']['decisions']
    assert 'missing' in out['feeds']['pass']['source_feed_values']
    assert out['admissions']['pass_never_listed']['by_reason']['list_evidence_unavailable']==1
    assert out['coverage']['by_hour'][0]['refused_unknown_tickets']==1


def test_sealed_look_does_not_import_readiness_statistics_inside_read_budget(world,monkeypatch):
    import builtins
    filled_cohort(world);now=T+14*86400+100;raw,digest=seal(world,now)
    real=builtins.__import__
    def bounded_import(name,*args,**kwargs):
        if name=='sniper.office.readiness.verdict' or name.startswith(('scipy','numpy')):
            raise RuntimeError('unexpected_statistics_import_in_report_budget')
        return real(name,*args,**kwargs)
    monkeypatch.setattr(builtins,'__import__',bounded_import)
    assert report(world,now=now,mode='look',look_record=raw,look_sha256=digest)['economics']['state']=='known'


@pytest.mark.parametrize('end',['2026-10-02T00:00:00+00:00','2026-11-01T00:00:01+00:00'])
def test_lightweight_calendar_cost_matches_existing_rule(end):
    from sniper.office import eval_report as R,costs
    at=datetime.fromisoformat(end).timestamp()
    assert R._calendar_cost(T,at)=={k:v for k,v in costs.epoch_cost(opened_at=T,ended_at=at,look_at=at).items()
                                   if k in ('amount_usd','days','allocation_rule_id')}


def test_exit_refusal_suffix_is_retained_and_query_exhaustion_withholds(world):
    rid,rec=candidate(world,0);did=publish(world,rid);oid=order(world,0,rid,rec,did)
    args={'decision':'EXIT','candidate':None,'order_id':oid,'public_reason':'close'}
    ctx=SimpleNamespace(epoch_id=E,actor_id='judge_claude',generation=1,decision_id='exit',cut={'knowledge_cut':T+29})
    xdid=J.decision_id(E,args)
    J.record(world.o,context=ctx,slot='judge_claude',args=args,now=T+30,equity_snapshot_id='s',lesson_ids=[])
    S.append(world.o,'receipts',{'kind':'judge.settle','cache_key':'judge.settle:'+xdid+':judge_claude',
        'decision_id':xdid,'label':'EXIT','known_at':T+31,'result':{'state':'refused','reason':'order_not_open_at_cut'}})
    out=report(world)
    assert out['exits']['refused_by_reason']['order_not_open_at_cut']==1
    assert out['feeds']['pass']['decisions']['EXIT']==1
    bounded=report(world,limits={'max_queries':1})
    assert bounded['economics']['state']=='withheld' and not bounded['coverage']['complete']


def test_am11_rounds_single_commit_and_unassigned_sheets_are_explicit(world):
    rid,rec=candidate(world,0,feed='pass');publish(world,rid)
    rid2,rec2=candidate(world,1,feed='near_pass')
    rid3,_=candidate(world,2,feed='pass')
    for candidate_id,feed,cut in ((rid,'pass',T+15),(rid2,'near_pass',T+16)):
        round_id=sha([E,candidate_id])
        S.append(world.o,'receipts',{'kind':'judge.round','cache_key':'judge.round:latest',
            'round_id':round_id,'epoch_id':E,'candidate':candidate_id,'feed':feed,
            'judges':['j1','j2'],'round_cut':cut,'item':{},'known_at':cut})
        S.append(world.o,'receipts',{'kind':'judge.round.assigned','cache_key':'judge.round.assigned:'+candidate_id,
            'round_id':round_id,'known_at':cut})
        S.append(world.o,'receipts',{'kind':'judge.round.end','cache_key':'judge.round.end:'+round_id,
            'round_id':round_id,'committed_slots':list(J.JUDGE_SLOTS) if feed=='pass' else ['judge_claude'],
            'outcome':'complete' if feed=='pass' else 'round_incomplete','reason':None if feed=='pass' else 'one_judge:judge_claude',
            'known_at':T+40})
    args={'decision':'WAIT','candidate':rid2,'order_id':None,'public_reason':'private'}
    ctx=SimpleNamespace(epoch_id=E,actor_id='judge_claude',generation=1,decision_id='single',cut={'knowledge_cut':T+16})
    J.record(world.o,context=ctx,slot='judge_claude',args=args,now=T+22,equity_snapshot_id='s',lesson_ids=[])
    out=report(world)
    assert out['rounds']['rounds']==2 and out['rounds']['assigned_by_feed']=={'pass':1,'near_pass':1,'unknown':0}
    assert out['rounds']['single_judge_commits']==1
    assert out['rounds']['incomplete_by_reason']=={'one_judge:judge_claude':1}
    assert out['rounds']['sheeted_never_assigned']['reason_unknown']==1
    assert out['rounds']['assignment_latency_s']['min']==5
    assert out['rounds']['assignment_latency_s']['max']==6
    health_quarantine(out)


def test_am10_list_snapshot_omission_causes_are_not_global_counts(world):
    for i in range(3):candidate(world,i)
    S.append(world.o,'receipts',{'tool':'desk.near_pass_list','cut':{'receipt_cut':T+30},'result':{
        'state':'known','items':[{'ticket_id':'ticket-0','feed':'pass'}],
        'pass_unlisted':{'age_bound':1,'item_limit':1},'limited':True,
        'coverage':'last 200 desk tickets, all stages'}})
    out=report(world)
    assert out['admissions']['pass_never_listed']['total']==2
    snapshots=out['admissions']['pass_unlisted_snapshots']
    assert snapshots[0]['item_limit']==1 and snapshots[0]['age_bound']==1
    assert snapshots[0]['scope']=='inside_host_200_row_window; not distinct tickets over epoch'


def test_pruned_or_unreadable_call_history_does_not_invent_outage(world):
    world.o.execute("UPDATE runtime_observation_state SET pruned_n=5 WHERE table_name='model_calls'")
    out=report(world,now=T+90000)
    assert out['coverage']['outage_failure_condition_observed'] is not True
    assert out['coverage']['call_history']['state']=='unknown'
    world.o.execute('DROP TABLE model_calls')
    out=report(world,now=T+90000)
    assert out['coverage']['call_silent_intervals']==[]
    assert out['coverage']['outage_failure_condition_observed'] is not True


def test_listed_without_sheet_is_seen_not_silently_unseen(world):
    world.o.commit()
    world.o.execute('BEGIN')
    candidate(world,0)
    world.o.rollback()  # Remove only the uncommitted sheet, not the already committed epoch.
    S.append(world.o,'receipts',{'tool':'desk.near_pass_list','cut':{'receipt_cut':T+30},'result':{
        'state':'known','items':[{'ticket_id':'ticket-0','feed':'pass'}]}})
    out=report(world)
    assert out['seen']['seen_tickets']==1 and out['seen']['sheet_coins']==0
    assert out['coverage']['by_hour'][0]['seen_tickets']==1


def test_external_look_pins_shared_valuation_dependency_source(world,monkeypatch):
    from sniper.office import eval_report as R
    filled_cohort(world);now=T+14*86400+100;raw,digest=seal(world,now)
    real=R.Path.read_bytes
    def changed(path):
        body=real(path)
        return body+b'\n# changed shared valuation rule\n' if path.name=='ledger.py' else body
    monkeypatch.setattr(R.Path,'read_bytes',changed)
    out=report(world,now=now,mode='look',look_record=raw,look_sha256=digest)
    assert out['economics']['state']=='withheld' and out['economics']['reason']=='look_record_mismatch'


def test_ticket_sheet_join_is_linear_instead_of_cross_product(world,monkeypatch):
    from sniper.office import eval_report as R
    n=40
    for i in range(n):candidate(world,i)
    calls=[];real=R._receipt
    class Counted(dict):
        def get(self,key,default=None):
            if key=='judged_snapshot_hash':calls.append(1)
            return super().get(key,default)
    def observed(row):
        p=real(row)
        if isinstance(p.get('request'),dict):p['request']=Counted(p['request'])
        return p
    monkeypatch.setattr(R,'_receipt',observed)
    report(world)
    assert len(calls)<=n*20


def seed_safety(w,now):
    """Offline watcher observations, not fabricated latch rows; no >24h hole."""
    existing=L.evaluation_safety_state(w.l,epoch_id=E)
    w.l.execute('BEGIN')
    try:
        if 'last_at' not in existing:
            L.evaluation_safety(w.l,epoch_id=E,now=T+1,sol_usd=150.,sol_at=T+1)
            at=T+1
        else:at=existing['last_at']
        while at<now:
            at=min(now,at+21600)
            L.evaluation_safety(w.l,epoch_id=E,now=at,sol_usd=150.,sol_at=at)
        w.l.execute('COMMIT')
    except BaseException:w.l.execute('ROLLBACK');raise


def test_live_future_receipts_do_not_invalidate_logical_seal(world):
    filled_cohort(world);now=T+14*86400+100;seed_safety(world,now)
    raw,digest=seal(world,now)
    before=report(world,now=now,mode='look',look_record=raw,look_sha256=digest)
    rid,_=candidate(world,1000,at=now+1);publish(world,rid,at=now+2)
    after=report(world,now=now,mode='look',look_record=raw,look_sha256=digest)
    assert before==after and after['economics']['state']=='known'


def test_optional_bulk_and_memo_failures_do_not_block_sealed_net(world):
    filled_cohort(world,2)
    for i in range(2,8):candidate(world,i)
    now=T+28*86400+100;seed_safety(world,now)
    raw,digest=seal(world,now,limits={'max_rows':4})
    out=report(world,now=now,mode='look',look_record=raw,look_sha256=digest,limits={'max_rows':4})
    assert out['economics']['state']=='known'
    assert out['descriptive_snapshot_sealed']['not_reverified_at_look'] is True


def test_filled_orders_require_fresh_config_bound_safety_latch(world):
    filled_cohort(world);now=T+14*86400+100;seed_safety(world,now)
    raw,digest=seal(world,now)
    world.l.execute('DELETE FROM epoch_entry_safety')
    assert report(world,now=now,mode='look',look_record=raw,look_sha256=digest)['economics']['reason']=='safety_latch_unreadable'


def test_post_look_halt_is_flagged_without_changing_the_look(world):
    filled_cohort(world);now=T+14*86400+100;seed_safety(world,now)
    raw,digest=seal(world,now)
    before=report(world,now=now,mode='look',look_record=raw,look_sha256=digest)
    L.halt_evaluation_entries(world.l,epoch_id=E,reason='test',actor='operator',now=now+60)
    after=report(world,now=now,mode='look',look_record=raw,look_sha256=digest)
    assert after['economics']==before['economics'] and after['post_look_halt'] is True


@pytest.mark.parametrize('change',['config','regressed_read','stale_sealed','financial_snapshot'])
def test_safety_snapshot_is_nonfinancial_fresh_and_bound_to_current_history(world,change):
    filled_cohort(world);now=T+14*86400+100;raw,digest0=seal(world,now)
    if change=='config':world.l.execute("UPDATE epoch_entry_safety SET config_hash=?",('x'*64,))
    elif change=='regressed_read':world.l.execute('UPDATE epoch_entry_safety SET last_equity_read_at=?',(now-1,))
    else:
        record=json.loads(raw)
        if change=='stale_sealed':record['safety_snapshot']['last_equity_read_at']=now-86401
        else:record['safety_snapshot']['equity_usd']=123.
        record['input_heads']['ledger.safety_projection']['snapshot']=record['safety_snapshot']
        raw=S.canonical_json(record);digest0=hashlib.sha256(raw).hexdigest()
    out=report(world,now=now,mode='look',look_record=raw,look_sha256=digest0)
    assert out['economics']['state']=='withheld'


def test_snapshot_financial_payload_and_stale_preparation_are_refused(world):
    filled_cohort(world);now=T+14*86400+100;raw,_=seal(world,now)
    for mutate in ('amount','cut'):
        record=json.loads(raw);snapshot=record['descriptive_snapshot']
        if mutate=='amount':snapshot['report']['bag_value_usd']=12.
        else:snapshot['as_of']=now-1
        record['descriptive_snapshot_sha256']=sha(snapshot)
        changed=S.canonical_json(record)
        out=report(world,now=now,mode='look',look_record=changed,look_sha256=hashlib.sha256(changed).hexdigest())
        assert out['economics']['state']=='withheld'


def test_wait_memos_are_one_optional_batch_not_queries_per_decision(world,monkeypatch):
    from sniper.office import eval_report as R
    for i in range(70):
        rid,_=candidate(world,i);publish(world,rid,decisions=('WAIT','WAIT'))
    labels=[];read=R.Reader.read
    def count(self,conn,label,*args,**kwargs):
        labels.append(label);return read(self,conn,label,*args,**kwargs)
    monkeypatch.setattr(R.Reader,'read',count)
    out=report(world,limits={'max_queries':40})
    assert out['decisions']['by_label']['WAIT']==70
    assert len([s for s in labels if s.startswith('optional.memos:')])==1
    assert out['coverage']['required_complete'] is True


def test_look_budget_can_be_larger_but_is_explicit_and_sealed(world):
    from sniper.office import eval_report as R
    filled_cohort(world,2);now=T+28*86400+100;seed_safety(world,now)
    limits=dict(R.DEFAULT_LIMITS,deadline_s=2.,max_queries=1024)
    prep=report(world,now=now,mode='look',limits=limits)
    assert prep['economics']['reason']=='look_record_required'
    assert prep['limits']==limits
    raw,digest0=seal(world,now,limits=limits)
    assert report(world,now=now,mode='look',limits=limits,look_record=raw,look_sha256=digest0)['economics']['state']=='known'


def test_post_cut_close_is_reconstructed_as_open_with_same_lower_bound(world):
    filled_cohort(world);now=T+28*86400+100
    world.l.execute("UPDATE orders SET closed_at=NULL,state='open' WHERE order_id='order-0'")
    world.l.execute("DELETE FROM cashflows WHERE order_id='order-0' AND kind='exit'")
    world.l.execute("INSERT INTO marks(order_id,at,lamports,source,reason) VALUES('order-0',?,NULL,'ps_paper.sell','no_route')",(now,))
    raw,digest0=seal(world,now,sol_usd=150.,sol_at=now)
    before=report(world,now=now,mode='look',look_record=raw,look_sha256=digest0,sol_usd=150.,sol_at=now)
    world.l.execute("UPDATE orders SET state='closed',closed_at=?,exit_by='judge' WHERE order_id='order-0'",(now+60,))
    after=report(world,now=now,mode='look',look_record=raw,look_sha256=digest0,sol_usd=150.,sol_at=now)
    assert before==after and after['economics']['state']=='partial'


def test_unrelated_large_sheets_do_not_consume_net_required_byte_budget(world):
    filled_cohort(world,2)
    for i in range(2,222):
        _,rec=candidate(world,i)
        rec['sheet']['irrelevant_padding']='x'*24000
        rec['sheet_hash']=sha(rec['sheet'])
        S.append(world.o,'receipts',rec)
    now=T+28*86400+100;raw,digest0=seal(world,now)
    out=report(world,now=now,mode='look',look_record=raw,look_sha256=digest0)
    assert out['economics']['state']=='known' and out['economics']['trading_net_usd']==pytest.approx(2.)
    assert out['coverage']['required_complete'] is True


def test_missing_order_attribution_is_unknown_and_never_borrowed_from_snapshot(world):
    filled_cohort(world,2);now=T+28*86400+100;raw,digest0=seal(world,now)
    world.o.execute('DROP TABLE decision_bundles');world.o.commit()
    out=report(world,now=now,mode='look',look_record=raw,look_sha256=digest0)
    assert out['economics']['state']=='known'
    assert out['economics']['per_feed']['unknown']['filled']==2
    assert out['economics']['per_feed']['pass']['filled']==0


def test_manifest_reader_refuses_fifo_oversize_and_symlink(tmp_path):
    import os
    from scripts.office_eval_report import read_manifest
    fifo=tmp_path/'record.pipe';os.mkfifo(fifo)
    with pytest.raises(ValueError):read_manifest(fifo)
    large=tmp_path/'large';large.touch();os.truncate(large,4*1024*1024+1)
    with pytest.raises(ValueError):read_manifest(large)
    record=tmp_path/'record';record.write_bytes(b'{}')
    link=tmp_path/'link';link.symlink_to(record)
    with pytest.raises(OSError):read_manifest(link)
    assert read_manifest(record)==b'{}'


def test_optional_round_or_broker_truncation_is_unknown_not_zero(world):
    for i in range(2):
        rid,_=candidate(world,i)
        S.append(world.o,'receipts',{'kind':'judge.round','cache_key':'judge.round:latest','epoch_id':E,
            'round_id':sha([E,rid]),'candidate':rid,'feed':'pass','round_cut':T+15+i,'known_at':T+15+i,'item':{}})
    out=report(world,limits={'max_rows':1})
    assert out['rounds']['state']=='unknown' and out['rounds']['rounds'] is None
    world.o.execute('DROP INDEX receipts_near_pass_tool');world.o.commit()
    out=report(world)
    assert out['strata']['price_source']['unpriced_attempts']['state']=='unknown'
    assert out['strata']['price_source']['unpriced_attempts']['by_reason'] is None


@pytest.mark.parametrize('basis',['echoed','requested_pinned',None,'invented'])
def test_am13_decide_basis_joins_own_turn_memo_and_missing_stays_unknown(world,basis):
    rid,_=candidate(world,0);did=publish(world,rid)
    from sniper.office.supervisor import action_key
    request={'memo':'PRIVATE DO NOT DISPLAY','served_model':'gpt-test'}
    if basis is not None:request['served_model_basis']=basis
    _put(world.o,'messages',action_key('judge_gpt'+did,'memo.commit'),
         {'actor_id':'judge_gpt','decision_id':'judge_gpt'+did,'known_at':T+25,'request':request})
    # Another turn's echoed memo must never satisfy the missing Claude turn.
    _put(world.o,'messages',action_key('foreign-turn','memo.commit'),
         {'actor_id':'judge_claude','known_at':T+25,'request':{'served_model_basis':'echoed','served_model':'claude-test'}})
    out=report(world)
    counts=out['decisions']['by_seat_and_basis']
    expected=basis if basis in ('echoed','requested_pinned') else 'unknown'
    assert counts['judge_gpt'][expected]==1 and counts['judge_claude']['unknown']==1
    assert sum(counts['judge_gpt'].values())==1
    assert out['feeds']['pass']['by_seat_and_basis']['judge_gpt'][expected]==1
    assert 'PRIVATE DO NOT DISPLAY' not in json.dumps(out)
    health_quarantine(out)


def test_am13_future_or_wrong_actor_memo_is_unknown(world):
    from sniper.office.supervisor import action_key
    rid,_=candidate(world,0);did=publish(world,rid)
    for actor,at in (('judge_gpt',T+101),('foreign',T+25)):
        _put(world.o,'messages',action_key('judge_gpt'+did,'memo.commit'),
             {'actor_id':actor,'decision_id':'judge_gpt'+did,'known_at':at,'request':{'served_model_basis':'echoed','served_model':'gpt-test'}})
        out=report(world)
        assert out['decisions']['by_seat_and_basis']['judge_gpt']['unknown']==1


def test_am13_real_pass_sheet_without_feed_is_pass_in_basis_and_sheet_aggregates(world):
    from .eval1_pass_sheet_fixture import RECEIPT_JSON
    from sniper.office.supervisor import action_key
    rec=json.loads(RECEIPT_JSON)
    assert 'feed' not in rec['request'] and S.canonical_json(rec).decode()==RECEIPT_JSON
    epoch=json.loads(world.o.execute('SELECT payload_json FROM epochs WHERE id=?',(E,)).fetchone()[0])
    epoch['record']['near_pass_cohort']['id']=rec['strategy_epoch'];_put(world.o,'epochs',E,epoch)
    world.d.execute('UPDATE desk_cohorts SET cohort_id=?',(rec['strategy_epoch'],));world.d.commit()
    at=rec['clock_sample']['corrected_utc'];rid=S.append(world.o,'receipts',rec);did=publish(world,rid,at=at+20)
    for slot,basis in (('judge_claude','echoed'),('judge_gpt','requested_pinned')):
        _put(world.o,'messages',action_key(slot+did,'memo.commit'),{'actor_id':slot,'decision_id':slot+did,
            'known_at':at+30,'request':{'served_model':'model-test','served_model_basis':basis}})
    # An unknown native-shaped receipt is not a known-sheet feed attribution.
    S.append(world.o,'receipts',{'kind':'sheet','state':'unknown','strategy_epoch':rec['strategy_epoch'],
        'clock_sample':{'corrected_utc':at+1},'reason':'rpc_error'})
    out=report(world,now=at+100)
    assert out['feeds']['pass']['by_seat_and_basis']['judge_gpt']['requested_pinned']==1
    assert out['feeds']['pass']['by_seat_and_basis']['judge_claude']['echoed']==1
    assert sum(sum(c.values()) for c in out['feeds']['unknown']['by_seat_and_basis'].values())==0
    assert sum(row['receipts'] for row in out['feeds']['pass']['sheet_receipt_aggregates'])==1
    assert not out['feeds']['unknown'].get('sheet_receipt_aggregates')


def test_completed_round_pair_matrix_is_per_feed_and_not_a_split_relabel(world):
    for i,(feed,answers,ended) in enumerate((('pass',('SKIP','WAIT'),True),('pass',('ENTER','ENTER'),True),
                                           ('near_pass',('WAIT','SKIP'),True),('pass',('SKIP','WAIT'),False))):
        rid,_=candidate(world,i,feed=feed);publish(world,rid,decisions=answers)
        r=sha([E,rid])
        S.append(world.o,'receipts',{'kind':'judge.round','cache_key':'judge.round:latest','round_id':r,'epoch_id':E,
            'candidate':rid,'feed':feed,'judges':['j1','j2'],'round_cut':T+15,'item':{},'known_at':T+15})
        S.append(world.o,'receipts',{'kind':'judge.round.assigned','cache_key':'judge.round.assigned:'+rid,'round_id':r,'known_at':T+15})
        S.append(world.o,'receipts',{'kind':'judge.round.end','cache_key':'judge.round.end:'+r,'round_id':r,
            'committed_slots':list(J.JUDGE_SLOTS),'outcome':'complete','reason':None,'known_at':T+40 if ended else T+101})
    out=report(world)
    assert out['decisions']['by_label']['split']==3  # the prereg label remains unchanged
    matrix=out['decisions']['pair_matrix']
    assert matrix['SKIP']['WAIT']==1 and matrix['ENTER']['ENTER']==1 and matrix['WAIT']['SKIP']==1
    assert sum(sum(row.values()) for row in matrix.values())==3
    assert out['feeds']['pass']['pair_matrix']['SKIP']['WAIT']==1
    assert out['feeds']['near_pass']['pair_matrix']['WAIT']['SKIP']==1
    health_quarantine(out)


def test_feed_default_requires_known_sheet_evidence():
    from sniper.office import eval_report as R
    assert R._feed(None)=='unknown'
    assert R._feed({'kind':'sheet','state':'unknown','request':{}})=='unknown'
    assert R._feed({'kind':'sheet','state':'known','request':{}})=='pass'


def test_complete_round_missing_pair_remains_unattributed_not_private(world):
    rid,_=candidate(world,0,feed='near_pass');r=sha([E,rid])
    ctx=SimpleNamespace(epoch_id=E,actor_id='judge_claude',generation=1,decision_id='single-private',cut={'knowledge_cut':T+19})
    J.record(world.o,context=ctx,slot='judge_claude',args={'decision':'SKIP','candidate':rid,'order_id':None,'public_reason':'PRIVATE'},
        now=T+20,equity_snapshot_id='s',lesson_ids=[])
    S.append(world.o,'receipts',{'kind':'judge.round','cache_key':'judge.round:latest','round_id':r,'epoch_id':E,
        'candidate':rid,'feed':'near_pass','judges':['j1','j2'],'round_cut':T+15,'item':{},'known_at':T+15})
    S.append(world.o,'receipts',{'kind':'judge.round.assigned','cache_key':'judge.round.assigned:'+rid,'round_id':r,'known_at':T+15})
    S.append(world.o,'receipts',{'kind':'judge.round.end','cache_key':'judge.round.end:'+r,'round_id':r,
        'committed_slots':list(J.JUDGE_SLOTS),'outcome':'complete','reason':None,'known_at':T+40})
    out=report(world)
    assert out['decisions']['pair_matrix_unattributed']=={'complete_round_pair_missing':1}
    assert out['feeds']['near_pass']['pair_matrix_state']=='partial'
    assert out['feeds']['near_pass']['pair_matrix_unattributed']=={'complete_round_pair_missing':1}
    assert sum(sum(c.values()) for c in out['decisions']['pair_matrix'].values())==0
    assert 'PRIVATE' not in json.dumps(out)


def test_sheet_failed_attempts_are_epoch_scoped_feed_split_and_never_coins(world):
    for i,(epoch,feed,reason,at) in enumerate(((E,'pass','sheet_failed:too_old',T+20),
        (E,'near_pass','sheet_failed:unavailable',T+21),(E,None,'sheet_failed:fresh',T+22),
        (None,'pass','sheet_failed:unavailable',T+23),('other','pass','sheet_failed:unavailable',T+24),
        (E,'pass','sheet_failed:unavailable',T+101))):
        p={'kind':'judge.round.sheet_failed','cache_key':'judge.round.sheet_failed:ticket-'+str(i),
           'ticket_id':'ticket-'+str(i),'feed':feed,'reason':reason,'known_at':at}
        if epoch is not None:p['epoch_id']=epoch
        S.append(world.o,'receipts',p)
    out=report(world)
    attempts=out['strata']['price_source']['unpriced_attempts']['round_sheet_failed']
    assert attempts['state']=='known' and attempts['coin_state']=='coins_unknown'
    assert attempts['distinct_coins'] is None and attempts['attributed_attempts']==3
    assert attempts['attribution_unknown_attempts']==1
    assert attempts['by_reason']=={'sheet_failed:too_old':1,'sheet_failed:unavailable':1,'sheet_failed:fresh':1}
    assert out['feeds']['pass']['sheet_failed_attempts']['by_reason']=={'sheet_failed:too_old':1}
    assert out['feeds']['near_pass']['sheet_failed_attempts']['by_reason']=={'sheet_failed:unavailable':1}
    assert out['feeds']['unknown']['sheet_failed_attempts']['by_reason']=={'sheet_failed:fresh':1}
    health_quarantine(out)


def test_sheet_failed_read_failure_is_unknown_not_zero(world,monkeypatch):
    from sniper.office import eval_report as R
    read=R.Reader.read
    def fail(self,conn,label,*args,**kwargs):
        if label=='optional.sheet_failed_aggregates':
            self.optional.append(label);self.optional_heads[label]={'state':'incomplete'};return []
        return read(self,conn,label,*args,**kwargs)
    monkeypatch.setattr(R.Reader,'read',fail)
    out=report(world)
    attempts=out['strata']['price_source']['unpriced_attempts']['round_sheet_failed']
    assert attempts['state']=='unknown' and attempts['attributed_attempts'] is None
    assert attempts['reason_details'] is None
    assert out['feeds']['pass']['sheet_failed_attempts']['attempts'] is None
    assert out['coverage']['required_complete'] is True


def test_round_rules_and_ages_use_judge_view_cut_not_commit_time(world):
    for i,rule in enumerate(('am11','am14',None)):
        rid,_=candidate(world,i,feed='pass' if i!=1 else 'near_pass');r=sha([E,rid])
        row={'kind':'judge.round','cache_key':'judge.round:latest','round_id':r,'epoch_id':E,'candidate':rid,
             'feed':'pass' if i!=1 else 'near_pass','judges':['j1','j2'],'round_cut':T+15,
             'sheet_age_s':5,'item':{},'known_at':T+15}
        if rule is not None:row['rule']=rule
        S.append(world.o,'receipts',row)
        S.append(world.o,'receipts',{'kind':'judge.round.assigned','cache_key':'judge.round.assigned:'+rid,'round_id':r,'known_at':T+15})
        S.append(world.o,'receipts',{'kind':'judge.round.end','cache_key':'judge.round.end:'+r,'round_id':r,
            'committed_slots':list(J.JUDGE_SLOTS),'outcome':'complete','reason':None,'known_at':T+40})
        publish(world,rid,decisions=('SKIP','WAIT'),at=T+35) # view cut T+34, not commit T+35
    out=report(world)
    assert set(out['rounds']['by_rule'])=={'am11','am14','unknown'}
    v=out['rounds']['by_rule']['am14']
    assert v['rounds']==1 and v['published_by_label']=={'split':1}
    assert v['by_feed']['near_pass']['rounds']==1 and v['by_feed']['pass']['rounds']==0
    assert v['assignment_sheet_age_s']['mean']==5
    assert v['judge_sheet_age_s_at_cut']['judge_gpt']['mean']==24
    assert v['judge_sheet_age_s_at_cut']['judge_claude']['mean']==24
    health_quarantine(out)


@pytest.mark.parametrize('view_cut',[None,T+30,T+9])
def test_bad_round_and_judge_age_evidence_stays_unknown(world,view_cut):
    rid,_=candidate(world,0);r=sha([E,rid])
    S.append(world.o,'receipts',{'kind':'judge.round','cache_key':'judge.round:latest','round_id':r,'epoch_id':E,
        'candidate':rid,'feed':'pass','judges':['j1','j2'],'round_cut':T+15,'sheet_age_s':99,'rule':'am14',
        'item':{},'known_at':T+15})
    S.append(world.o,'receipts',{'kind':'judge.round.assigned','cache_key':'judge.round.assigned:'+rid,'round_id':r,'known_at':T+15})
    # Fixture corruption: no claim is made that commit-time can replace missing/future view evidence.
    did=J.decision_id(E,{'decision':'WAIT','candidate':rid})
    for slot in J.JUDGE_SLOTS:
        S.append(world.o,'receipts',{'kind':'judge.decide','cache_key':'judge.decide:'+did,'epoch_id':E,
            'decision_id':did,'slot':slot,'actor_id':slot,'turn_decision_id':slot+did,'candidate':rid,
            'decision':'WAIT','order_id':None,'known_at':T+20,'view_cut':view_cut,'public_reason':'observed'})
    out=report(world)
    v=out['rounds']['by_rule']['am14']
    assert v['assignment_sheet_age_s']['state']=='unknown'
    assert v['assignment_sheet_age_s']['unknown_by_reason']=={'assignment_age_conflict':1}
    assert v['judge_sheet_age_s_at_cut']['judge_gpt']['state']=='unknown'
    assert v['judge_sheet_age_s_at_cut']['judge_gpt']['unknown_by_reason']=={'view_cut_unknown':1}


def test_sheet_failure_reason_detail_is_optional_bounded_and_does_not_change_attempt_denominator(world):
    cause='refresh_failed:StaleSnapshot:latest_swap_not_buy'
    details=[{'state':'unknown','reason':cause},None,{'state':False,'reason':'bad'},
             {'state':'unknown','reason':'x'*201}]
    for i,detail in enumerate(details):
        S.append(world.o,'receipts',{'kind':'judge.round.sheet_failed','cache_key':'judge.round.sheet_failed:detail-'+str(i),
            'epoch_id':E,'ticket_id':'detail-'+str(i),'feed':'pass','reason':'sheet_failed:unavailable',
            'reason_detail':detail,'known_at':T+20+i})
    out=report(world)
    failures=out['strata']['price_source']['unpriced_attempts']['round_sheet_failed']
    assert failures['by_reason']=={'sheet_failed:unavailable':4} and failures['attributed_attempts']==4
    assert failures['reason_details']==[
        {'state':'known','tool_state':'unknown','reason':cause,'attempts':1},
        {'state':'unknown','tool_state':None,'reason':'reason_detail_unavailable','attempts':3}]
    assert out['feeds']['pass']['sheet_failed_attempts']['reason_details']==failures['reason_details']
    assert sum(row['attempts'] for row in failures['reason_details'])==failures['attributed_attempts']
    health_quarantine(out)


def test_partial_and_known_figures_are_withheld_when_mixed_and_kept_when_not():
    from sniper.office import eval_report as ER
    def go(states):
        orders=[dict(order_id=s+str(i),filled_at=1.,closed_at=2.,network_id='solana',mint=s+str(i),sheet_state=s,sells_basis='x') for i,s in enumerate(states)]
        data=dict(orders=orders,cashflows=[dict(order_id=o['order_id'],usd=2.) for o in orders],order_feed={},coin_core={},own_sheets={},order_sheets={})
        return ER._coin_stats(data,[],rng_seed=1,now=3.,check=lambda:None)
    mixed=go(['known','partial'])
    assert mixed['mean_trading_net_per_coin_usd'] is None and mixed['median_coin_usd'] is None and mixed['coin_bootstrap_interval']['mean_trading_net_per_coin_usd'] is None and mixed['blended_mean_withheld'] is True
    assert go(['known','known'])['mean_trading_net_per_coin_usd'] is not None
