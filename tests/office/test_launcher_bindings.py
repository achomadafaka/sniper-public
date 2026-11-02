import json
from uuid import uuid4
import pytest
from sniper.office.launcher_bindings import Completion


def fixture(tmp_path):
    rid=str(uuid4());state=tmp_path/'state';state.mkdir()
    (state/(rid+'.result.json')).write_text(json.dumps(dict(request_id=rid,outcome='success',output_file=rid+'/result.json',exit_code=0)))
    unit='office-model-'+rid+'.service'
    properties=dict(Id=unit,ActiveState='inactive',SubState='dead',MainPID='0',ControlPID='0',ControlGroup='')
    calls=[]
    def probe(u):calls.append(u);return properties
    return rid, state, properties, calls, Completion(state, cgroup_root=tmp_path/'cgroup', probe=probe)


def test_completed_requires_protected_receipt_and_exact_stopped_unit(tmp_path):
    rid,state,props,calls,check=fixture(tmp_path)
    assert check(rid) is True and calls==['office-model-'+rid+'.service']
    props['ActiveState']='active'
    assert check(rid) is False
    props['ActiveState']='inactive';props['MainPID']='1'
    assert check(rid) is False


def test_missing_or_mismatched_receipt_never_checks_unit(tmp_path):
    rid,state,props,calls,check=fixture(tmp_path)
    (state/(rid+'.result.json')).unlink()
    assert check(rid) is False and not calls
    (state/(rid+'.result.json')).write_text(json.dumps(dict(request_id=str(uuid4()),outcome='success')))
    assert check(rid) is False and not calls


def test_nonempty_cgroup_requires_observed_zero_population(tmp_path):
    rid,state,props,calls,check=fixture(tmp_path)
    props['ControlGroup']='/user.slice/office.slice/office-model-'+rid+'.service'
    group=tmp_path/'cgroup'/props['ControlGroup'].lstrip('/');group.mkdir(parents=True)
    events=group/'cgroup.events';events.write_text('populated 1\nfrozen 0\n')
    assert check(rid) is False
    events.write_text('populated 0\nfrozen 0\n')
    assert check(rid) is True
    events.unlink()
    assert check(rid) is False


@pytest.mark.parametrize('field,value',[('Id','other.service'),('ControlGroup','/other'),('SubState','running'),('ControlPID','1')])
def test_wrong_identity_or_live_state_refuses(tmp_path,field,value):
    rid,state,props,calls,check=fixture(tmp_path);props[field]=value
    assert check(rid) is False


def test_probe_exception_and_invalid_request_refuse(tmp_path):
    rid,state,props,calls,check=fixture(tmp_path)
    check.probe=lambda _:(_ for _ in ()).throw(OSError())
    assert check(rid) is False and check('../escape') is False


def test_real_probe_uses_only_fixed_show_properties(monkeypatch):
    from sniper.office import launcher_bindings as B
    seen=[]
    class Result:
        stdout='Id=office-model-fixture.service\nActiveState=inactive\n'
    def run(argv, **kwargs):seen.append((argv,kwargs));return Result()
    monkeypatch.setattr(B.subprocess,'run',run)
    assert B.unit_properties('office-model-fixture.service')['ActiveState']=='inactive'
    assert seen==[(['systemctl','--user','show','office-model-fixture.service',
                   '--property=Id,ActiveState,SubState,MainPID,ControlPID,ControlGroup'],
                  dict(capture_output=True,text=True,timeout=2,check=True))]


def test_producer_child_error_is_terminal_when_exact_unit_is_stopped(tmp_path):
    rid,state,props,calls,check=fixture(tmp_path)
    (state/(rid+'.result.json')).write_text(json.dumps(dict(request_id=rid,outcome='child_error',output_file=None,exit_code=1)))
    props['ActiveState']='failed';props['SubState']='failed'
    assert check(rid) is True and len(calls)==1
