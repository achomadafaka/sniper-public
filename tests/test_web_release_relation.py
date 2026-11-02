"""Real tiny git histories; no runtime, network, or service mutations."""
import json
import subprocess
import pytest


def git(repo,*args):
    return subprocess.check_output(['git','-C',str(repo),*args],text=True).strip()


def commit(repo,path,text):
    p=repo/path;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text)
    git(repo,'add','--',path);git(repo,'commit','-qm','fixture')
    return git(repo,'rev-parse','HEAD')


@pytest.fixture
def history(tmp_path):
    repo=tmp_path/'repo';repo.mkdir();git(repo,'init','-q')
    git(repo,'config','user.name','Fixture');git(repo,'config','user.email','fixture@example.invalid')
    runner=commit(repo,'sniper/execution/fill.py','unchanged funded code')
    return repo,runner


@pytest.mark.parametrize('path,funded',[
    ('sniper/office/seat.py',False),('docs/note.md',False),('tests/test_x.py',False),
    ('static/office-v2.js',False),('sniper/routes/api_office.py',False),
    ('static/app.js',True),('sniper/web/server.py',True),('sniper/execution/fill.py',True),
])
def test_known_ancestor_classifies_only_allowlisted_nonfunded_paths(history,path,funded):
    from sniper.util.web_release_relation import build
    repo,runner=history;web=commit(repo,path,'new source')
    r=build(repo,runner,web)
    assert r['state']=='known' and r['runner_is_ancestor'] is True
    assert r['web_is_ancestor'] is False and r['funded_paths_changed'] is funded


def test_behind_diverged_and_unknown_never_become_safe(history):
    from sniper.util.web_release_relation import build
    repo,base=history;a=commit(repo,'docs/a.md','a')
    behind=build(repo,a,base)
    assert behind['state']=='known' and behind['runner_is_ancestor'] is False and behind['web_is_ancestor'] is True
    git(repo,'checkout','-q','--detach',base);b=commit(repo,'docs/b.md','b')
    diverged=build(repo,a,b)
    assert diverged['runner_is_ancestor'] is False and diverged['web_is_ancestor'] is False
    assert build(repo,'f'*40,b)['state']=='unknown'
    assert build(repo,None,b)['funded_paths_changed'] is None


def test_renamed_funded_file_does_not_hide_behind_docs_prefix(history):
    from sniper.util.web_release_relation import build
    repo,runner=history;(repo/'docs').mkdir()
    git(repo,'mv','sniper/execution/fill.py','docs/fill.py');git(repo,'commit','-qm','rename')
    assert build(repo,runner,git(repo,'rev-parse','HEAD'))['funded_paths_changed'] is True


def test_deploy_writer_uses_actual_heartbeat_and_atomic_release_file(history,tmp_path):
    from sniper.util.web_release_relation import write_for_deploy,read
    repo,runner=history;web=commit(repo,'docs/new.md','web');state=tmp_path/'state';(state/'data').mkdir(parents=True)
    (state/'data/memecoin_runner.heartbeat').write_text(json.dumps({'lifi':{'release_sha':runner}}))
    output=tmp_path/'release/.web_release_relation.json'
    write_for_deploy(repo,state,web,output)
    assert read(output)['runner_sha']==runner and read(output)['web_sha']==web
    assert not list(output.parent.glob('.tmp-*'))
    (state/'data/memecoin_runner.heartbeat').write_text('broken')
    write_for_deploy(repo,state,web,output)
    assert read(output)['state']=='unknown'


def test_real_web_deploy_records_metadata_before_flip_without_cycling(history,tmp_path):
    import os
    import sys
    from pathlib import Path
    repo,runner=history
    paths=('sniper/__init__.py','sniper/routes/__init__.py','sniper/services/__init__.py',
           'sniper/web/server.py','sniper/routes/api_memecoin.py','sniper/services/memecoin_manual_intents.py')
    for path in paths:web=commit(repo,path,'# fixture only\n')
    state=tmp_path/'state';(state/'data').mkdir(parents=True)
    (state/'data/memecoin_runner.heartbeat').write_text(json.dumps({'lifi':{'release_sha':runner}}))
    runtime=tmp_path/'runtime'
    script=Path(__file__).resolve().parents[1]/'scripts/deploy_web_runtime.sh'
    result=subprocess.run(['bash',str(script),web,'--no-cycle','--allow-local-commit'],cwd=repo,
        env=dict(os.environ,SNIPER_SOURCE_ROOT=str(repo),SNIPER_STATE_ROOT=str(state),SNIPER_WEB_RUNTIME=str(runtime),SNIPER_PY=sys.executable),
        capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
    metadata=json.loads((runtime/'current/.web_release_relation.json').read_text())
    assert metadata['state']=='known' and metadata['runner_sha']==runner and metadata['web_sha']==web
    assert (runtime/'current').resolve().name==web


@pytest.mark.parametrize('bad',[None,{'version':1,'state':'known'}, {'version':2,'state':'unknown'}])
def test_invalid_metadata_is_unknown(tmp_path,bad):
    from sniper.util.web_release_relation import read
    path=tmp_path/'metadata.json';path.write_text(json.dumps(bad))
    assert read(path)['state']=='unknown'
    path.write_text('x'*16385)
    assert read(path)['funded_paths_changed'] is None


def test_invalid_heartbeat_identity_is_unknown_not_an_embedded_payload(history):
    from sniper.util.web_release_relation import build
    repo,runner=history
    r=build(repo,{'unexpected':'payload'},runner)
    assert r['state']=='unknown' and r['runner_sha'] is None
