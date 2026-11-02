import json
from sniper.services import memecoin_runner as runner


def test_whitelisted_string_fields_cannot_carry_urls_or_keys(tmp_path):
    path=str(tmp_path/'heartbeat')
    secret='https://provider.example/SECRETKEY'
    status={'inline_hosts':{'n':1,'active':1,'hosts':[{'label':secret,'state':secret}],'rotations':{secret:1}}}
    runner.write_heartbeat({'events':0},True,path=path,rh_fast_lane=status)
    assert 'SECRETKEY' not in open(path).read()


def test_idle_refresh_keeps_last_inline_value(tmp_path):
    path=str(tmp_path/'heartbeat');status={'inline_hosts':{'n':1,'active':1,'hosts':[{'label':'host 1 of 1','state':'healthy'}],'rotations':{}}}
    runner.write_heartbeat({'events':0},True,path=path,started_at='session',rh_fast_lane=status)
    before=json.load(open(path))['rh_fast_lane']
    assert runner.refresh_idle_heartbeat(started_at='session',slot_lane_status=None,tpu_status=None,rpc_pool_status=None,fastlane_status=None,path=path) is not False
    assert json.load(open(path))['rh_fast_lane']==before


import pytest

@pytest.mark.parametrize('label', ['host 1', 'host 0 of 2 (?)', 'host 1000 of 2 (?)', 'host 1 of 2 (https://x/KEY)', 'host 1 of 2 (KEY.example)', 'host 1 of 2 ('+'a'*64+')', 'host 1 of 2 (x?key=secret)'])
def test_bad_label_is_null(tmp_path,label):
    path=str(tmp_path/'heartbeat')
    runner.write_heartbeat({},True,path=path,rh_fast_lane={'inline_hosts':{'hosts':[{'label':label,'state':'healthy'}]}})
    assert json.load(open(path))['rh_fast_lane']['inline_hosts']['hosts'][0]['label'] is None

@pytest.mark.parametrize('label', ['host 1 of 2 (provider.example)', 'host 2 of 2 (?)'])
def test_production_label_survives(tmp_path,label):
    path=str(tmp_path/'heartbeat')
    runner.write_heartbeat({},True,path=path,rh_fast_lane={'inline_hosts':{'hosts':[{'label':label,'state':'healthy'}]}})
    assert json.load(open(path))['rh_fast_lane']['inline_hosts']['hosts'][0]['label']==label


def test_states_counters_and_host_cap(tmp_path):
    path=str(tmp_path/'heartbeat')
    states=['healthy','quota spent','dead','429 pause','identity pending','wrong chain (refused)','SECRETKEY','https://secret/KEY','healthy']
    counts={k:1 for k in ('rotated','all_unavailable','recovered','refused','identity_expired')}
    counts.update(SECRETKEY=1,other=2,healthy=True)
    runner.write_heartbeat({},True,path=path,rh_fast_lane={'inline_hosts':{'hosts':[{'label':'host 1 of 1 (?)','state':state} for state in states],'rotations':counts}})
    got=json.load(open(path))['rh_fast_lane']['inline_hosts']
    assert len(got['hosts'])==8
    assert [h['state'] for h in got['hosts']]==states[:6]+[None,None]
    assert got['rotations']=={k:1 for k in ('rotated','all_unavailable','recovered','refused','identity_expired')}
