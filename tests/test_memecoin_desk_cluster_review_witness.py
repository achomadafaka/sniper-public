"""Codex's cluster acceptance witnesses on 71edac009, ported (Beta 2026-10-22). Offline: fake transport,
no provider or production I/O. Two residuals they pinned: a self-consistent rehashed INVALID candidate
must never bind a cluster row (the shared U-reader boundary applies the full candidate contract), and a
missing or malformed holder response is dependency-unknown, never kill:cluster_no_holders; an explicit
result:{value:[]} stays a verified-empty kill."""
import copy
import inspect
import io
import json
from pathlib import Path
from urllib.error import HTTPError

import pytest

from sniper.services import memecoin_desk_cluster as dc
from sniper.services import memecoin_desk_contract as contract
from sniper.services.memecoin_cluster_admission import ClusterDecision
from sniper.services.memecoin_cluster_rpc import ResearchClusterRpc, SYSTEM_PROGRAM_ID
from sniper.services.memecoin_cluster_service import ClusterAdmissionService
from sniper.services.memecoin_cluster_store import ClusterStore

FIXTURES = Path(__file__).parent / "fixtures"
VALID=json.loads((FIXTURES/'desk_cluster_valid_u_emission.json').read_text())
EPOCH=VALID['snapshot']['emission_at']+5
NOW='2026-10-21T13:00:05+00:00'

def candidate(i=0):
 c=copy.deepcopy(json.loads((FIXTURES/'desk_cluster_canonical_candidates.json').read_text())[i])
 contract.validate_candidate(c)
 return c

def digest(c):
 import hashlib
 c['snapshot']['hash']=hashlib.sha256(json.dumps({k:v for k,v in c['snapshot'].items() if k!='hash'},sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def write_candidates(tmp,cs):
 em,out=tmp/'em',tmp/'out';em.mkdir()
 (em/'2026-10-21.jsonl').write_text(''.join(json.dumps(c)+'\n' for c in cs))
 return em,out

def records(out):
 return [json.loads(l) for p in out.glob('*.jsonl') for l in p.read_text().splitlines()]

def run_pass(em,out,factory,**kw):
 opts=dict(per_min=6,calls_per_candidate=40,calls_per_min=300,calls_per_day=20000,now_fn=lambda:NOW,clock=lambda:60.0)
 if 'epoch_fn' in inspect.signature(dc.run_pass).parameters:
  opts.update(epoch_fn=lambda:EPOCH,max_age_s=60)
 opts.update(kw)
 return dc.run_pass(factory,em,out,**opts)

def factory_n(n,seen):
 def factory(allowance):
  meter,http=dc.metered(lambda:None,allowance)
  class Service:
   def decide(self,mint,**kw):
    seen.append(mint)
    for _ in range(n):http()
    return ClusterDecision('d',True,'pass_thin_owners','p',NOW,NOW,{})
  return meter,Service()
 return factory

@pytest.mark.parametrize('bad',['schema','hash','ticket','stale','rehashed_missing','rehashed_field_value','extra_envelope'])
def test_invalid_emission_never_binds(tmp_path,bad):
 c=candidate()
 if bad=='schema':c['schema']='forged/v1'
 elif bad=='hash':c['snapshot']['fields']['mcap_usd']['value']=42000
 elif bad=='ticket':c['trigger_ordinal']+=1
 elif bad=='stale':
  c['emitted_at']='2000-01-01T00:00:00+00:00';c['snapshot']['emission_at']=946684800.0
  for f in c['snapshot']['fields'].values():f['known_at']=946684800.0
  digest(c)
 elif bad=='rehashed_missing':c['snapshot']['missing']=['invented'];digest(c)
 elif bad=='rehashed_field_value':c['snapshot']['fields']['mcap_usd']['value']='41000';digest(c)
 else:c['unvalidated_extra']=True
 if bad in ('rehashed_missing','rehashed_field_value','extra_envelope'):
  with pytest.raises(ValueError):contract.validate_candidate(c)
 em,out=write_candidates(tmp_path,[c]);seen=[]
 run_pass(em,out,factory_n(0,seen))
 assert not seen and not records(out),{'called':seen,'rows':records(out)}

def test_valid_control_binds_exact_identity(tmp_path):
 c=candidate();em,out=write_candidates(tmp_path,[c]);seen=[]
 run_pass(em,out,factory_n(1,seen))
 got=records(out)
 assert len(got)==1 and seen==[c['mint']]
 assert all(got[0][k]==c[k] for k in ('ticket_id','network_id','mint','pool'))
 assert got[0]['snapshot_hash']==c['snapshot']['hash']
 assert got[0]['check']=={'status':'pass','reason':'pass_thin_owners'}

@pytest.mark.parametrize('cap_kind',['candidates','calls'])
def test_restarts_cannot_reset_minute_caps(tmp_path,cap_kind):
 em,out=write_candidates(tmp_path,[candidate(i) for i in range(12)])
 seen=[];f=factory_n(40,seen)
 opts=dict(per_min=6,calls_per_min=300) if cap_kind=='candidates' else dict(per_min=20,calls_per_min=100)
 run_pass(em,out,f,**opts);run_pass(em,out,f,**opts)
 got=records(out);spent=sum(r['rpc_calls'] for r in got)
 assert len(got)<=opts['per_min'] and spent<=opts['calls_per_min'],{'rows':len(got),'spent':spent,'seen':len(seen)}
 assert len(seen)>0

class Crash(BaseException):pass

def test_interrupted_read_retains_call_allocation(tmp_path):
 em,out=write_candidates(tmp_path,[candidate(0),candidate(1)])
 actual=[];first=True
 def factory(allowance):
  nonlocal first
  meter,http=dc.metered(lambda:actual.append(1),allowance)
  class Service:
   def decide(self,mint,**kw):
    nonlocal first
    http();http()
    if first:first=False;raise Crash()
    return ClusterDecision('d',True,'allowed','p',NOW,NOW,{})
  return meter,Service()
 with pytest.raises(Crash):run_pass(em,out,factory,calls_per_day=40)
 run_pass(em,out,factory,calls_per_day=40)
 assert len(actual)==2,{'actual_calls':len(actual),'rows':records(out)}
 got=records(out)
 assert len(got)==1 and got[0]['check']['status']=='unknown' and got[0]['rpc_calls']==40

FAILURES=['429','timeout','malformed_json','partial_programs','null_program','malformed_program',
          'partial_token_owners','missing_result','null_largest','missing_value','null_value']

def service_with_answer(tmp_path,answer):
 calls=[];paused=[]
 class Pacer:
  def acquire(self):pass
  def pause(self,s):paused.append(s)
 def http(request,timeout):
  q=json.loads(request.data);method,params=q['method'],q['params'];calls.append((method,params))
  if method=='getTokenLargestAccounts':
   if answer=='missing_result':return io.StringIO(json.dumps({'jsonrpc':'2.0','id':1}))
   result= None if answer=='null_largest' else {} if answer=='missing_value' else {'value':None} if answer=='null_value' else {'value':[]} if answer=='verified_empty' else {'value':[{'address':'ATA','amount':'100'}]}
  elif method=='getMultipleAccounts' and params[0]==['ATA']:
   result={'value':[]} if answer=='partial_token_owners' else {'value':[{'data':{'parsed':{'info':{'owner':'Wallet'}}}}]}
  elif method=='getMultipleAccounts' and params[0]==['Wallet']:
   if answer=='429':raise HTTPError(request.full_url,429,'quota',{},None)
   if answer=='timeout':raise TimeoutError('fake provider timeout')
   if answer=='malformed_json':return io.StringIO('{not json')
   values=[] if answer=='partial_programs' else [None] if answer=='null_program' else [{}] if answer=='malformed_program' else [{'owner':SYSTEM_PROGRAM_ID}]
   result={'value':values}
  elif method=='getSignaturesForAddress':result=[{'signature':'oldest'}]
  elif method=='getTransaction':result={'transaction':{'message':{'instructions':[{'parsed':{'type':'transfer','info':{'destination':'Wallet','source':'Funder'}}}]}}}
  else:raise AssertionError(q)
  return io.StringIO(json.dumps({'jsonrpc':'2.0','id':1,'result':result}))
 meter,opener=dc.metered(http,40,pacer=Pacer())
 rpc=ResearchClusterRpc('https://research.invalid/?api-key=FAKE',http=opener)
 opts=dict(now_fn=lambda:NOW,workers=1,deadline_s=5,thin_owner_pass=True)
 if 'holder_failure_reason' in inspect.signature(ClusterAdmissionService).parameters:opts['holder_failure_reason']='cluster_unknown_dependency'
 svc=ClusterAdmissionService(rpc,ClusterStore(tmp_path/'cluster.db'),**opts)
 got=dc.check_from_decision(svc.decide('Mint',source_path='desk',requested_at=NOW))
 return got,calls,paused

@pytest.mark.parametrize('answer',FAILURES)
def test_provider_failures_stay_unknown(tmp_path,answer):
 got,calls,paused=service_with_answer(tmp_path,answer)
 assert got['status']=='unknown',{'check':got,'calls':calls,'paused':paused}
 if answer=='429':assert paused==[60.0]

def test_healthy_thin_book_still_passes(tmp_path):
 got,_calls,_paused=service_with_answer(tmp_path,'healthy')
 assert got=={'status':'pass','reason':'pass_thin_owners'}

def test_verified_empty_book_is_distinct_from_missing_response(tmp_path):
 got,_calls,_paused=service_with_answer(tmp_path,'verified_empty')
 assert got=={'status':'kill','reason':'cluster_no_holders'}
