"""Retrospective receipt work shares the existing owner and never changes forward state."""
import copy
import json
import sqlite3
import pytest
from sniper.office.chain_data import ChainData
from .test_wallet_launch_proof import receipt


def api():
    from sniper.office import wallet_receipt_jobs
    return wallet_receipt_jobs


def target(tx,creator):
    ix=tx['transaction']['message']['instructions'][0]
    return dict(expected_creator=creator,source_path='/retained/launches/day.db',launch=dict(
        signature=tx['transaction']['signatures'][0],mint=ix['accounts'][0],curve=ix['accounts'][2],
        slot=tx['slot'],creator=creator))


@pytest.fixture
def setup(tmp_path):
    tx,dev,mint,creator=receipt();clock=[1000.];calls=[]
    def rpc(url,method,params):calls.append((method,params));return 200,dict(result=copy.deepcopy(tx))
    chain=ChainData(tmp_path/'chain.db',lambda:'https://unused.invalid',clock=lambda:clock[0],rpc=rpc)
    return tmp_path/'jobs.db',chain,tx,dev,mint,creator,clock,calls


class Regular:
    def __init__(self):self.budgets=[]
    def tick(self,*,max_calls):self.budgets.append(max_calls);return dict(state='partial',reason='polling_coverage_only',calls_attempted=1,wallets=[dict(address='existing')])


def test_no_jobs_does_not_create_store_or_change_regular_budget(setup):
    path,chain,*_=setup;regular=Regular()
    result,jobs=api().poll(chain,regular,path,max_calls=3)
    assert not path.exists() and regular.budgets==[3]
    assert result['wallets']==[dict(address='existing')] and jobs['pending'] is False


def test_offline_enqueue_is_atomic_deduplicated_and_never_dials(setup):
    path,chain,tx,dev,mint,creator,clock,calls=setup;T=target(tx,creator)
    assert api().enqueue_targets(path,[T,T],now=clock[0])==dict(state='known',added=1,existing=1)
    assert not calls
    conflict=copy.deepcopy(T);conflict['expected_creator']=dev;conflict['launch']['creator']=dev
    assert api().enqueue_targets(path,[conflict],now=clock[0])['state']=='refused'
    with sqlite3.connect(path) as c:assert c.execute('SELECT count(*) FROM receipt_jobs').fetchone()[0]==1


def test_known_receipt_uses_full_standard_cache_and_separates_fee_creator_from_signer(setup):
    path,chain,tx,dev,mint,creator,clock,calls=setup;api().enqueue_targets(path,[target(tx,creator)],now=clock[0])
    regular=Regular();result,status=api().poll(chain,regular,path,max_calls=1)
    assert result is None and regular.budgets==[] and len(calls)==1
    assert status['last_job']['state']=='known'
    with sqlite3.connect(path) as c:
        state,known,body=c.execute('SELECT state,known_at,profile_json FROM receipt_jobs').fetchone()
    profile=json.loads(body)
    assert state=='known' and known==clock[0]>tx['blockTime']
    assert profile['developer']==dev and profile['creator_argument']==creator and dev!=creator
    assert profile['mint']==mint and profile['commitment']=='finalized' and profile['ownership_proven'] is False
    with chain._conn() as c:
        cached=json.loads(c.execute('SELECT doc FROM cache').fetchone()[0])
        assert cached==tx and cached['transaction']['message']['instructions']
    api().poll(chain,regular,path,max_calls=1)
    assert regular.budgets==[1] and len(calls)==1


def test_larger_budget_spends_one_job_slot_and_gives_regular_the_rest(setup):
    path,chain,tx,dev,mint,creator,clock,calls=setup;api().enqueue_targets(path,[target(tx,creator)],now=clock[0]);regular=Regular()
    result,status=api().poll(chain,regular,path,max_calls=4)
    assert regular.budgets==[3] and result['calls_attempted']==1 and status['calls_attempted']==1


@pytest.mark.parametrize('bad',['signature','mint','creator','slot','curve','failed','empty','none'])
def test_wrong_or_missing_evidence_stays_typed_unknown_not_empty_success(setup,bad):
    path,chain,tx,dev,mint,creator,clock,calls=setup;api().enqueue_targets(path,[target(tx,creator)],now=clock[0])
    if bad=='signature':tx['transaction']['signatures'][0]=tx['transaction']['signatures'][1]
    elif bad=='mint':tx['transaction']['message']['instructions'][0]['accounts'][0]=dev
    elif bad=='creator':tx['transaction']['message']['instructions'][0]['data']='1'
    elif bad=='slot':tx['slot']+=1
    elif bad=='curve':tx['transaction']['message']['instructions'][0]['accounts'][2]=dev
    elif bad=='failed':tx['meta']['err']={}
    elif bad=='empty':tx['transaction']['message']['instructions']=[]
    else:chain.rpc=lambda *a:(200,dict(result=None))
    regular=Regular();_,status=api().poll(chain,regular,path,max_calls=1)
    assert status['last_job']['state']=='unknown' and status['last_job']['reason']
    with sqlite3.connect(path) as c:
        state,known,profile=c.execute('SELECT state,known_at,profile_json FROM receipt_jobs').fetchone()
    assert state=='pending' and known is None and profile is None


def test_schedule_failure_after_provider_call_cannot_spend_budget_twice(setup,monkeypatch):
    path,chain,tx,dev,mint,creator,clock,calls=setup;J=api();J.enqueue_targets(path,[target(tx,creator)],now=clock[0]);original=J._connect;opened=[]
    def connect(p):
        opened.append(1)
        if len(opened)==4:raise sqlite3.OperationalError('database locked at schedule write')
        return original(p)
    monkeypatch.setattr(J,'_connect',connect);regular=Regular()
    result,status=J.poll(chain,regular,path,max_calls=1)
    assert len(calls)==1 and regular.budgets==[] and result is None
    assert status['state']=='unknown' and status['calls_attempted']==1


@pytest.mark.parametrize('bad',['transaction_null','signatures_empty'])
def test_malformed_receipt_is_typed_and_retryable(setup,bad):
    path,chain,tx,dev,mint,creator,clock,calls=setup;api().enqueue_targets(path,[target(tx,creator)],now=clock[0])
    if bad=='transaction_null':tx['transaction']=None
    else:tx['transaction']['signatures']=[]
    result,status=api().poll(chain,Regular(),path,max_calls=1)
    assert result is None and status['last_job']['state']=='unknown'


def test_cache_success_job_store_failure_resumes_idempotently_known_now(setup,monkeypatch):
    path,chain,tx,dev,mint,creator,clock,calls=setup;J=api();J.enqueue_targets(path,[target(tx,creator)],now=clock[0]);original=J._store_result;attempts=[]
    def store(*args,**kw):
        attempts.append(1)
        if len(attempts)==1:raise sqlite3.OperationalError('job store busy')
        return original(*args,**kw)
    monkeypatch.setattr(J,'_store_result',store);regular=Regular()
    _,failed=J.poll(chain,regular,path,max_calls=1)
    assert failed['state']=='unknown' and len(calls)==1
    J.poll(chain,regular,path,max_calls=1);clock[0]+=2
    _,recovered=J.poll(chain,regular,path,max_calls=1)
    assert recovered['last_job']['state']=='known' and len(calls)==1
    with sqlite3.connect(path) as c:
        known,body=c.execute('SELECT known_at,profile_json FROM receipt_jobs').fetchone();profile=json.loads(body)
        assert known==1002. and profile['fetched_at']==1000. and profile['known_at']==1002.
    assert J.enqueue_targets(path,[target(tx,creator)],now=clock[0])['existing']==1
    with sqlite3.connect(path) as c:assert c.execute('SELECT known_at FROM receipt_jobs').fetchone()[0]==known


def test_job_429_uses_existing_global_stop_and_never_switches_provider(setup):
    path,chain,tx,dev,mint,creator,clock,calls=setup;J=api();J.enqueue_targets(path,[target(tx,creator)],now=clock[0]);urls=[]
    def limited(url,method,params):urls.append(url);return 429,None
    chain.rpc=limited;regular=Regular();_,first=J.poll(chain,regular,path,max_calls=1)
    assert first['last_job']['reason']=='provider_rate_limited'
    for _ in range(8):clock[0]+=.5;J.poll(chain,regular,path,max_calls=1)
    assert urls==['https://unused.invalid']
    with chain._conn() as c:
        assert c.execute('SELECT stop_until FROM gate').fetchone()[0]==1900.
        assert c.execute('SELECT count(*) FROM calls').fetchone()[0]==1


def test_paced_receipt_lane_holds_until_admitted_then_regular_gets_its_turn(setup):
    path,chain,tx,dev,mint,creator,clock,calls=setup;J=api();J.enqueue_targets(path,[target(tx,creator)],now=clock[0])
    chain.call('getSignaturesForAddress',[dev,dict(limit=1)])
    regular=Regular()
    for advance in (0.,.5,.5):
        clock[0]+=advance;result,status=J.poll(chain,regular,path,max_calls=1)
        if result is None:assert status['provider_attempts']==0
        else:assert status['reason']=='receipt_retry_wait' and status['calls_attempted']==0
        assert len(calls)==1
    clock[0]=1002.;_,done=J.poll(chain,regular,path,max_calls=1)
    assert done['last_job']['state']=='known' and len(calls)==2
    J.poll(chain,regular,path,max_calls=1)
    assert regular.budgets and regular.budgets[-1]==1


def test_bad_manifest_refuses_whole_batch_without_store_or_network(setup):
    path,chain,tx,dev,mint,creator,clock,calls=setup;T=target(tx,creator);bad=copy.deepcopy(T);bad['launch']['slot']=True
    assert api().enqueue_targets(path,[T,bad],now=clock[0])['state']=='refused'
    assert not path.exists() and not calls
    assert api().enqueue_targets(path,[T]*101,now=clock[0])['state']=='refused'
    assert not path.exists()


def test_corrupt_job_store_is_unknown_and_regular_retains_full_budget(setup):
    path,chain,*_=setup;path.write_bytes(b'not a sqlite database');regular=Regular()
    result,status=api().poll(chain,regular,path,max_calls=3)
    assert status['state']=='unknown' and status['pending'] is None
    assert regular.budgets==[3] and result['wallets']


def test_regressed_worker_clock_does_not_fetch_or_backdate_a_profile(setup):
    path,chain,tx,dev,mint,creator,clock,calls=setup;api().enqueue_targets(path,[target(tx,creator)],now=clock[0]);clock[0]=999.
    _,status=api().poll(chain,Regular(),path,max_calls=1)
    assert status['state']=='unknown' and status['reason']=='receipt_clock_unknown' and not calls
    with sqlite3.connect(path) as c:assert c.execute('SELECT known_at FROM receipt_jobs').fetchone()[0] is None


@pytest.mark.parametrize('budget',[1,4])
@pytest.mark.parametrize('latency',[0.,2.])
def test_shared_pacer_cannot_phase_lock_regular_or_receipt_out_of_real_dials(setup,budget,latency):
    import base58
    from sniper.office.chain_data import _Refused
    path,chain,tx,dev,mint,creator,clock,calls=setup;J=api();other=copy.deepcopy(tx)
    other['transaction']['signatures'][0]=base58.b58encode(bytes([9])*64).decode()
    J.enqueue_targets(path,[target(tx,creator),target(other,creator)],now=clock[0]);methods=[]
    def rpc(url,method,params):
        methods.append(method);clock[0]+=latency
        if method=='getTransaction':return 200,dict(result=tx if params[0]==tx['transaction']['signatures'][0] else None)
        return 200,dict(result=[])
    chain.rpc=rpc
    class PacedRegular:
        def __init__(self):self.successes=0
        def tick(self,*,max_calls):
            try:
                chain.call('getSignaturesForAddress',[dev,dict(before='cursor'+str(self.successes),limit=1)])
                self.successes+=1
                return dict(state='partial',reason='polling_coverage_only',calls_attempted=1,cached_operations=0)
            except _Refused as exc:return dict(state='unknown',reason='shared_pace' if exc.reason=='paced' else exc.reason,calls_attempted=0)
    regular=PacedRegular()
    for _ in range(25):
        J.poll(chain,regular,path,max_calls=budget);clock[0]+=.25
    assert methods[:4]==['getTransaction','getSignaturesForAddress','getTransaction','getSignaturesForAddress']


def test_valid_actual_creator_conflict_never_publishes_claimed_fee_root(setup):
    path,chain,tx,dev,mint,creator,clock,calls=setup;J=api();T=target(tx,dev)
    J.enqueue_targets(path,[T],now=clock[0]);_,status=J.poll(chain,Regular(),path,max_calls=1)
    job=status['last_job']
    assert job['state']=='unknown' and job['reason']=='identity_conflict'
    assert job['actual_identity']['developer']==dev and job['actual_identity']['creator_argument']==creator
    with sqlite3.connect(path) as c:assert c.execute('SELECT state,known_at,profile_json FROM receipt_jobs').fetchone()==('pending',None,None)


def test_work_batch_limit_does_not_limit_lifetime_queue(setup):
    import base58
    path,chain,tx,dev,mint,creator,clock,calls=setup;items=[]
    for i in range(1,102):
        item=target(tx,creator);item['launch']['signature']=base58.b58encode(bytes([i])*64).decode();items.append(item)
    J=api();assert J.enqueue_targets(path,items[:100],now=clock[0])['added']==100
    assert J.enqueue_targets(path,items[100:],now=clock[0])['added']==1
    with sqlite3.connect(path) as c:assert c.execute('SELECT count(*) FROM receipt_jobs').fetchone()[0]==101
    assert not calls


def test_job_removed_while_rpc_runs_cannot_report_a_durably_known_job(setup):
    path,chain,tx,dev,mint,creator,clock,calls=setup;J=api();J.enqueue_targets(path,[target(tx,creator)],now=clock[0])
    def rpc(*args):
        with sqlite3.connect(path) as c:c.execute('DELETE FROM receipt_jobs')
        return 200,dict(result=tx)
    chain.rpc=rpc;regular=Regular();result,status=J.poll(chain,regular,path,max_calls=1)
    assert result is None and status['state']=='unknown' and status['reason']=='receipt_store_unknown'
    assert regular.budgets==[]


@pytest.mark.parametrize('failure',[OSError,sqlite3.OperationalError])
def test_unknown_post_admission_failure_yields_without_claiming_physical_dial(setup,monkeypatch,failure):
    from sniper.office.chain_data import _Refused
    path,chain,tx,dev,mint,creator,clock,calls=setup;J=api();J.enqueue_targets(path,[target(tx,creator)],now=clock[0]);original=chain.call;turns=[]
    def uncertain(method,params,**kw):
        if method!='getTransaction':return original(method,params,**kw)
        c=chain._conn()
        try:admission=chain._admit(c,method)
        finally:c.close()
        if isinstance(admission,str):raise _Refused(admission)
        turns.append('job_unknown');raise failure('local failure after admission')
    monkeypatch.setattr(chain,'call',uncertain)
    class RegularPoll:
        def __init__(self):self.progress=0
        def tick(self,*,max_calls):
            try:
                chain.call('getSignaturesForAddress',[dev,dict(before='cursor'+str(self.progress))])
                self.progress+=1;turns.append('regular')
                return dict(state='partial',reason='polling_coverage_only',calls_attempted=1)
            except _Refused as exc:return dict(state='unknown',reason='shared_pace' if exc.reason=='paced' else exc.reason,calls_attempted=0)
    regular=RegularPoll();result,status=J.poll(chain,regular,path,max_calls=1)
    assert result is None and status['calls_attempted']==1 and status['provider_attempts'] is None
    assert status['last_job']['state']=='unknown'
    with sqlite3.connect(path) as c:assert c.execute('SELECT profile_json,known_at FROM receipt_jobs').fetchone()==(None,None)
    for _ in range(24):clock[0]+=.25;J.poll(chain,regular,path,max_calls=1)
    assert turns[:4]==['job_unknown','regular','job_unknown','regular']
