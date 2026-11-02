import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
import pytest
from sniper.services import memecoin_large_buyer as B, memecoin_manual_intents as I
from sniper.persistence import db_memecoin as M
import sniper.settings.memecoin as cfg

NOW=1793500000.
# Observed-tape fixture intentionally keeps full-history capture unknown.
T=dict(pool='P',wallet='W',mint='So11111111111111111111111111111111111111112',signature='S',trigger_at=NOW-10,entry_due=NOW,pool_quote_before=str(50*10**9),post_quote=str(100*10**9),post_base=str(10**15),history_state='unknown',others_60s=1,crowd_others_60s=1,crowd_state="known",source_slot=123,source_tx_index=1,source_ordinal=0,source_signature="S",detect_lag_s=0.1,detected_at=NOW-9.9)

@pytest.fixture
def enabled(monkeypatch):
    for key,value in dict(MEMECOIN_LARGE_BUYER_ENABLED=True,MEMECOIN_REAL_TOKEN_SAFETY_ENABLED=True,MEMECOIN_DESK_ENABLED=False,MEMECOIN_DESK_CLIP_USD=25.,MEMECOIN_DESK_CHAINS=frozenset({'solana'}),MEMECOIN_DESK_RAIL_BOUNDS={'tp_pct':(15,150),'sl_pct':(10,40),'trail_pct':(8,30)},MEMECOIN_LARGE_BUYER_MAX_FILLS=30).items():monkeypatch.setattr(cfg,key,value)

def db(path=':memory:'):
    c=sqlite3.connect(path,timeout=3);c.row_factory=sqlite3.Row;M.ensure_memecoin_tables(c);c.commit();return c

def request(wallet='W'):
    return B.request_for(dict(T,wallet=wallet),cohort_id=B.CROWD_COHORT_ID,decimals=6,sol_usd=100.,now=NOW,mint_metadata=dict(decimals=6,metadata_slot=123,metadata_known_at=NOW-1,metadata_source='helius_research:get_account_info:json_parsed',token_program='spl-token'))

def test_suffix_is_never_decimal_proof():
    assert B.pump_decimals('11111111111111111111111111111pump') is None

def test_decimal_cache_requires_actual_receipt_program_and_source(tmp_path):
    from sniper.services import memecoin_desk_ps_metadata as MD
    c=MD.connect(tmp_path/'mint.db')
    c.execute('INSERT INTO mints VALUES(?,?,?,?,?,?,?,?,?,?)',(T['mint'],9,'1000000','spl-token',None,None,NOW-1,123,MD.SOURCE,'{}'));c.commit()
    proof=B.mint_proof(c,T['mint'],now=NOW)
    assert proof['decimals']==9 and proof['metadata_slot']==123 and proof['metadata_source']==MD.SOURCE
    assert B.mint_proof(c,T['mint'],now=NOW-2) is None
    c.execute("UPDATE mints SET source='suffix_guess'");c.commit()
    assert B.mint_proof(c,T['mint'],now=NOW) is None

@pytest.mark.parametrize('change',[{'ts':NOW+1},{'sol_age_s':-1},{'sol_age_s':float('nan')},{'ts':NOW-121},{'sol_usd':float('inf')}])
def test_fx_causal_finite_and_fresh(tmp_path,change):
    path=tmp_path/'universe.heartbeat';path.write_text(json.dumps(dict(sol_usd=100.,sol_age_s=0.,ts=NOW,**{} )|change))
    assert B.read_sol_usd(path,now=NOW) is None


def test_queue_time_is_corrected_and_due_window_not_refreshed(monkeypatch):
    monkeypatch.setattr(B,'real_utc_now',lambda:NOW-60)
    r=B.queue_request(request(),host_now=NOW+3)
    assert r['now_iso']==B.iso(NOW-60)
    assert r['ttl_s']==2
    assert B.queue_request(request(),host_now=NOW+6) is None


def test_atomic_cap_across_producers_and_restart(tmp_path,enabled,monkeypatch):
    path=tmp_path/'ledger.db';c=db(path)
    for n in range(29):
        r=request(str(n));r.pop('ttl_s');I.enqueue(c,now_iso=B.iso(NOW),**r)
    c.close();barrier=threading.Barrier(2)
    def producer(n):
        c=sqlite3.connect(path,timeout=3);c.row_factory=sqlite3.Row;r=request(str(n));r.pop('ttl_s');barrier.wait(timeout=2)
        try:I.enqueue(c,now_iso=B.iso(NOW),**r);return 'queued'
        except ValueError as exc:return str(exc)
        finally:c.close()
    with ThreadPoolExecutor(max_workers=2) as pool:result=list(pool.map(producer,[29,30]))
    assert result.count('queued')==1 and any('cap' in x for x in result)
    c=sqlite3.connect(path);assert c.execute('SELECT count(*) FROM memecoin_manual_intents').fetchone()[0]==30
    # Even an unresolved/signed attempt consumes its durable admission after restart.
    c.execute("UPDATE memecoin_manual_intents SET state='unresolved'");c.commit();r=request('restart');r.pop('ttl_s')
    with pytest.raises(ValueError,match='cap'):I.enqueue(c,now_iso=B.iso(NOW),**r)


def test_crowd_selection_frozen():
    r=request()
    assert r['desk_metadata']['cohort_id']=='large-buyer-crowd-v1'
    assert r['desk_metadata']['min_others_60s']==1 and B.POLICY['min_others']==1


@pytest.mark.parametrize('change',[dict(crowd_state='unknown'),dict(crowd_others_60s=0),dict(crowd_others_60s=None),dict(source_tx_index=0),dict(source_slot=None),dict(detect_lag_s=6),dict(detect_lag_s=float('nan')),dict(entry_due=NOW+1)])
def test_crowd_never_uses_legacy_count_or_unknown_order(change):
    c=db();t=dict(T,**change)
    why=B._skip(t,c,B.CROWD_COHORT_ID,lambda _:6,lambda _:100.,NOW,30,1)
    assert why in ('crowd_proof_unknown','crowd_order_unknown','late')


@pytest.mark.parametrize('cohort',[B.COHORT_ID,B.CROWD_COHORT_ID])
def test_full_runner_desk_path_refuses_unsafe_token_before_executor(enabled,monkeypatch,cohort):
    from sniper.services import memecoin_arming,memecoin_real_runner as runner
    from sniper.util.clock import real_utc_now_iso
    monkeypatch.setattr(cfg,'MEMECOIN_REAL_ENABLED',True)
    monkeypatch.setattr(memecoin_arming.cfg,'MEMECOIN_REAL_ENABLED',True)
    monkeypatch.setattr(cfg,'MEMECOIN_DESK_MAX_CONCURRENT',2)
    monkeypatch.setattr(cfg,'MEMECOIN_DESK_DAILY_LOSS_USD',50.)
    monkeypatch.setattr(runner,'_alert_entry_result',lambda *a,**k:None)
    c=db();mint=T['mint'][:-1]+'3';r=B.request_for(dict(T,mint=mint),cohort_id=cohort,decimals=6,sol_usd=100.,now=NOW,mint_metadata=dict(decimals=6,metadata_slot=123,metadata_known_at=NOW-1,metadata_source='helius_research:get_account_info:json_parsed',token_program='spl-token'))
    M.register_desk_cohort(c,producer=B.PRODUCER,policy=B.POLICY,prompt_version=B.PROMPT_VERSION,created_at=real_utc_now_iso(),cohort_id=cohort);c.commit()
    hit=[]
    def unsafe(*a,**k):hit.append('safety');return False,'live freeze authority'
    def execute(*a,**k):pytest.fail('unsafe token reached executor')
    contexts={'solana':dict(usdc_fn=lambda:300.,sol_fn=lambda:1.,token_safety_fn=unsafe,execute_fn=execute,kill_path=None)}
    stats=lambda *a,**k:dict(market_cap=60000.,price=.0001,liquidity_usd=20000.)
    r.pop('ttl_s');iid=I.enqueue(c,now_iso=real_utc_now_iso(),**r)
    done=I.drain(c,runner.make_real_buy_fn(contexts,stats_fn=stats),now_fn=real_utc_now_iso)
    assert hit==['safety'] and done==[(iid,I.REFUSED)], (I.read_status(c,iid).get('reason'),I.read_status(c,iid).get('result_status'))
    assert c.execute('SELECT count(*) FROM memecoin_positions').fetchone()[0]==0


@pytest.mark.parametrize('usd_back',[None,4.60],ids=['unsellable','expensive_roundtrip'])
def test_shared_executor_reverse_route_and_roundtrip_fail_before_signing(usd_back):
    from sniper.execution import solana_swap_executor as ex
    from test_memecoin_entry_roundtrip_cap import _c,_Deps,_run
    c,pid=_c();d=_Deps(quote_impact=.05,usd_back=usd_back)
    out=_run(c,pid,d)
    assert out['status'] in ('aborted','route_failed') and 'quote' in d.calls and 'roundtrip' in d.calls
    assert 'sign' not in d.calls and 'send' not in d.calls


@pytest.mark.parametrize('change',[dict(source_signature='different'),dict(entry_due=NOW-1),dict(detected_at=NOW)])
def test_crowd_order_proof_is_bound_to_trigger_identity_and_receipt_clocks(change):
    t=dict(T,detected_at=T['trigger_at']+T['detect_lag_s']);t.update(change)
    assert B._skip(t,db(),B.CROWD_COHORT_ID,lambda _:6,lambda _:100.,NOW,30,1) in ('crowd_order_unknown','crowd_proof_unknown')


def test_crowd_price_provenance_is_corrected_utc(monkeypatch):
    monkeypatch.setattr(B,'real_utc_now',lambda:NOW-60)
    assert request()['desk_metadata']['verdict_price']['snapshot_at']==NOW-60


def test_failed_enqueue_cannot_leave_admission_claim(tmp_path,enabled,monkeypatch):
    c=db(tmp_path/'ledger.db')
    original=I._enqueue
    def fail(*a,**k):raise ValueError('definitely not queued')
    monkeypatch.setattr(I,'_enqueue',fail);r=request();r.pop('ttl_s')
    with pytest.raises(ValueError,match='not queued'):I.enqueue(c,now_iso=B.iso(NOW),**r)
    monkeypatch.setattr(I,'_enqueue',original)
    assert c.execute('SELECT count(*) FROM memecoin_manual_intents').fetchone()[0]==0
    # Retry must still have every slot available after transaction rollback.
    I.enqueue(c,now_iso=B.iso(NOW),**r)
    assert I._pilot_slots(c,B.CROWD_COHORT_ID)==1
    assert I.enqueue(c,now_iso=B.iso(NOW),**r)
    assert I._pilot_slots(c,B.CROWD_COHORT_ID)==1


@pytest.mark.parametrize('missing',['decimals','fx'])
@pytest.mark.parametrize('cohort',[B.COHORT_ID,B.CROWD_COHORT_ID])
def test_transient_proof_can_arrive_inside_original_window_only(missing,cohort):
    from test_memecoin_large_buyer import Arm
    c=db();seen=set();counts={};sent=[];ready=[False]
    def tick(now):
        B.cycle(Arm([T]),c,cohort_id=cohort,decimals_fn=lambda _:6 if ready[0] or missing!='decimals' else None,
                sol_fn=lambda _:100. if ready[0] or missing!='fx' else None,enqueue=lambda conn,**r:sent.append(r),
                now=now,max_fills=30,seen=seen,counts=counts,min_others=1,
                proof_fn=lambda _:dict(decimals=6,metadata_slot=123,metadata_known_at=NOW-1,metadata_source='helius_research:get_account_info:json_parsed',token_program='spl-token') if ready[0] or missing!='decimals' else None)
    tick(NOW);assert not sent
    ready[0]=True;tick(NOW+1)
    assert len(sent)==1
    tick(NOW+2);assert len(sent)==1
    # A separate still-unready ticket cannot refresh its original due after expiry.
    seen.clear();sent.clear();ready[0]=False;tick(NOW)
    ready[0]=True;tick(NOW+6)
    assert not sent


def test_running_producer_reports_bounded_counts_without_provider_poll(tmp_path,enabled,monkeypatch):
    from types import SimpleNamespace
    c=db(tmp_path/'ledger.db');c.close();sqlite3.connect(tmp_path/'arm.db').close()
    tick=[NOW];logs=[]
    monkeypatch.setattr(B,'time',SimpleNamespace(time=lambda:tick[0],sleep=lambda _:None))
    monkeypatch.setattr(B,'monotonic',lambda:tick[0],raising=False)
    def cycle(*a,counts,**kw):counts['sol_unknown']=counts.get('sol_unknown',0)+1;tick[0]+=31
    monkeypatch.setattr(B,'cycle',cycle)
    B.run(arm_path=tmp_path/'arm.db',ledger_path=tmp_path/'ledger.db',desk_dir=tmp_path,until=NOW+32,log=logs.append)
    assert any('started cohort=large-buyer-crowd-v1' in x for x in logs)
    beats=[json.loads(x.split('heartbeat ',1)[1]) for x in logs if 'heartbeat ' in x]
    assert beats and beats[-1]['cohort_id']==B.CROWD_COHORT_ID
    assert beats[-1]['counts']['sol_unknown']==2 and beats[-1]['host_now']==NOW+62


@pytest.mark.parametrize('state',[None,'unknown','observed_only',''])
def test_observed_crowd_labels_incomplete_history_without_an_extra_gate(state):
    t=dict(T,history_state=state)
    assert B._skip(t,db(),B.CROWD_COHORT_ID,lambda _:6,lambda _:100.,NOW,30,1) is None
    assert B.POLICY['history_basis']=='observed_tape' and B.POLICY['capture_complete'] is False
