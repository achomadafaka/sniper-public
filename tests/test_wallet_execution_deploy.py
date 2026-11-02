import importlib.util
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]


def mod():
    spec=importlib.util.spec_from_file_location('deploy_wallet_research',ROOT/'scripts/deploy_wallet_research.py')
    m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def test_deploy_requires_merged_full_sha_and_existing_source(tmp_path):
    m=mod()
    with pytest.raises(ValueError,match='full'): m.validate_request('main',tmp_path,tmp_path)
    with pytest.raises(ValueError,match='source'): m.validate_request('a'*40,tmp_path,tmp_path/'none.db')


def test_pid_identity_never_matches_an_unrelated_python_process():
    m=mod()
    command=['/usr/bin/python3','-u','/repo/.runtime/wallet-research/releases/'+ 'a'*40 +'/scripts/research/wallet_execution_worker.py','--loop']
    assert m.owned_command(command)
    assert not m.owned_command(['/usr/bin/python3','sniper/services/memecoin_real_runner.py'])
    assert not m.owned_command(['/bin/bash','-c','python3 wallet_execution_worker.py'])
    assert not m.owned_command(['/usr/bin/python3','/tmp/wallet_execution_worker.py','--loop'])


def test_coverage_activation_requires_this_pid_this_release_and_healthy_source():
    m=mod(); sha='a'*40
    assert m.ready_artifact({'worker_pid':12,'release_sha':sha,'source_status':'ok','source_state':{'tape_status':'sampled_state_not_executable_path'}},12,sha)
    assert not m.ready_artifact({'worker_pid':13,'release_sha':sha,'source_status':'ok','source_state':{'tape_status':'sampled_state_not_executable_path'}},12,sha)
    assert not m.ready_artifact({'worker_pid':12,'release_sha':sha,'source_status':'unavailable'},12,sha)


def test_first_activation_does_not_stop_nonexistent_service(monkeypatch,tmp_path):
    import json
    import subprocess
    m=mod(); sha='a'*40; state=tmp_path/'state'; repo=tmp_path/'repo'; units=tmp_path/'units'
    release=state/'.runtime/wallet-research/releases'/sha; (release/'scripts/research').mkdir(parents=True)
    (release/m.ENTRY).write_text('# fixture')
    monkeypatch.setattr(m,'validate_request',lambda *a: None)
    def run(*args,**kwargs):
        out=''
        if args[0]=='git' and 'rev-parse' in args: out=sha
        if args[0]=='systemctl' and 'stop' in args: raise subprocess.CalledProcessError(5,args)
        if args[0]=='systemctl' and 'show' in args:
            out='123' if (state/'data/wallet_execution_coverage.json').exists() else '0'
        if args[0]=='systemctl' and 'enable' in args:
            (state/'data').mkdir(exist_ok=True)
            (state/'data/wallet_execution_coverage.json').write_text(json.dumps({'worker_pid':123,'release_sha':sha,'source_status':'ok','source_state':{'tape_status':'sampled_state_not_executable_path'}}))
        return subprocess.CompletedProcess(args,0,out,'')
    monkeypatch.setattr(m,'run',run)
    assert m.deploy(sha,repo=repo,state=state,unit_dir=units)['pid']==123


def test_failed_activation_restores_symlink_and_prior_unit(monkeypatch,tmp_path):
    import subprocess
    m=mod(); sha='a'*40; state=tmp_path/'state'; repo=tmp_path/'repo'; units=tmp_path/'units'
    runtime=state/'.runtime/wallet-research'; release=runtime/'releases'/sha
    (release/'scripts/research').mkdir(parents=True); (release/m.ENTRY).write_text('# fixture')
    previous=runtime/'releases'/('b'*40); previous.mkdir(); (runtime/'current').symlink_to(previous)
    units.mkdir(); original=m.MARKER+'\n# prior unit'; (units/m.UNIT).write_text(original)
    monkeypatch.setattr(m,'validate_request',lambda *a: None)
    def run(*args,**kwargs):
        if args[0]=='systemctl' and 'enable' in args: raise ValueError('simulated start failure')
        out=sha if args[0]=='git' and 'rev-parse' in args else '0' if 'show' in args else ''
        return subprocess.CompletedProcess(args,0,out,'')
    monkeypatch.setattr(m,'run',run)
    monkeypatch.setattr(m.subprocess,'run',lambda *a,**k: subprocess.CompletedProcess(a,0,'',''))
    with pytest.raises(ValueError,match='simulated'): m.deploy(sha,repo=repo,state=state,unit_dir=units)
    assert (runtime/'current').resolve()==previous and (units/m.UNIT).read_text()==original


def test_working_directory_is_an_absolute_systemd_value(monkeypatch,tmp_path):
    import json,subprocess
    m=mod();sha='a'*40;state=tmp_path/'state';units=tmp_path/'units';release=state/'.runtime/wallet-research/releases'/sha
    (release/'scripts/research').mkdir(parents=True);(release/m.ENTRY).write_text('# fixture')
    monkeypatch.setattr(m,'validate_request',lambda *a:None)
    def run(*args,**kwargs):
        out=sha if args[0]=='git' and 'rev-parse' in args else ''
        if 'show' in args:out='123' if (state/'data/wallet_execution_coverage.json').exists() else '0'
        if 'enable' in args:
            template=(units/m.UNIT).read_text();value=next(line.split('=',1)[1] for line in template.splitlines() if line.startswith('WorkingDirectory='))
            assert Path(value).is_absolute(),value
            (state/'data').mkdir(exist_ok=True);(state/'data/wallet_execution_coverage.json').write_text(json.dumps({'worker_pid':123,'release_sha':sha,'source_status':'ok','source_state':{'tape_status':'sampled_state_not_executable_path'}}))
        return subprocess.CompletedProcess(args,0,out,'')
    monkeypatch.setattr(m,'run',run)
    assert m.deploy(sha,repo=tmp_path,state=state,unit_dir=units)['pid']==123
