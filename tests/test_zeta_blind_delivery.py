import json
import sys
from types import SimpleNamespace
from test_watch_sweep import sweep

def test_actual_page_failure_keeps_blind_label_retryable(sweep,monkeypatch,tmp_path):
 hb=tmp_path/'runner.hb';hb.write_text(json.dumps({'slot_lane':{'exotic_cu_blind':{'NewAmm':3}}}))
 monkeypatch.setattr(sweep,'RUNNER_HEARTBEAT',str(hb))
 for name in ('check_sweep_gap','check_runner','check_web','check_db','check_rpc_cold','check_gas'):
  monkeypatch.setattr(sweep,name,lambda problems:None)
 result=[False];calls=[]
 def send(text):calls.append(text);return result[0],0.0
 monkeypatch.setitem(sys.modules,'sniper.services.memecoin_telegram',SimpleNamespace(send_telegram=send))
 sweep.main()
 assert json.loads(open(sweep.STATE).read())['blind_labels_paged']==[]
 result[0]=True;sweep.main()
 assert json.loads(open(sweep.STATE).read())['blind_labels_paged']==['NewAmm']
 sweep.main();assert len(calls)==2


import pytest

@pytest.mark.parametrize('result,expected',[
 ((True,1.0),True),((False,1.0),False),((1,1.0),False),
 (None,False),(True,False),((True,),False),
])
def test_page_requires_explicit_primary_success(sweep,monkeypatch,result,expected):
 monkeypatch.setitem(sys.modules,'sniper.services.memecoin_telegram',SimpleNamespace(send_telegram=lambda text:result))
 assert sweep.page('synthetic') is expected


def test_page_exception_is_not_success(sweep,monkeypatch):
 def fail(text):raise RuntimeError('synthetic send failure')
 monkeypatch.setitem(sys.modules,'sniper.services.memecoin_telegram',SimpleNamespace(send_telegram=fail))
 assert sweep.page('synthetic') is False
