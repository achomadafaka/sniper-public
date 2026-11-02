import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import urllib.error

import pytest


def script():
    path=Path(__file__).resolve().parents[2]/'scripts/office_quota_snapshot.py'
    spec=importlib.util.spec_from_file_location('quota_snapshot_under_test',path)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    return mod


def test_claude_poll_uses_only_usage_get_and_never_emits_secret(tmp_path):
    mod=script();p=tmp_path/'credentials';p.write_text(json.dumps({'claudeAiOauth':{'accessToken':'SECRET'}}))
    class Response:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def read(self,n):return b'{"five_hour":{"utilization":9,"resets_at":"2026-10-30T13:00:00Z"}}'
    def get(req,timeout):
        assert req.full_url=='https://api.anthropic.com/api/oauth/usage'
        assert req.get_method()=='GET' and req.get_header('Authorization')=='Bearer SECRET'
        return Response()
    raw,reason=mod.poll_claude(p,open_url=get)
    assert reason is None and 'SECRET' not in str(raw)
    def rejected(*a,**k):raise urllib.error.HTTPError('url',429,'denied',{},None)
    assert mod.poll_claude(p,open_url=rejected)==(None,'rate_limited')


def test_codex_poll_never_starts_model_and_cleans_process():
    mod=script();processes=[]
    program='''import sys,json
for line in sys.stdin:
 d=json.loads(line)
 if d.get('id')==1:print(json.dumps({'id':1,'result':{}}),flush=True)
 if d.get('id')==2:
  assert d['method']=='account/rateLimits/read'
  print(json.dumps({'id':2,'result':{'rateLimits':{'primary':{'usedPercent':37}}}}),flush=True)
'''
    def popen(args,**kwargs):
        assert args==['codex','app-server','--listen','stdio://']
        child=subprocess.Popen([sys.executable,'-c',program],**kwargs);processes.append(child);return child
    raw,reason=mod.poll_codex(popen=popen,timeout_s=2)
    assert reason is None and raw['rateLimits']['primary']['usedPercent']==37
    assert processes[0].poll() is not None


def test_codex_timeout_is_unknown_and_child_is_reaped():
    mod=script();children=[]
    def popen(args,**kwargs):
        p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],**kwargs);children.append(p);return p
    assert mod.poll_codex(popen=popen,timeout_s=.05)==(None,'timeout')
    assert children[0].poll() is not None


def test_snapshot_cli_retains_unknown_poll_and_refuses_foreign_database(tmp_path,monkeypatch,capsys):
    mod=script();monkeypatch.setattr(mod,'poll_codex',lambda:(None,'timeout'))
    monkeypatch.setattr(mod,'now',lambda:1000.)
    p=tmp_path/'quota.db'
    assert mod.main(['--db',str(p),'--provider','codex','--account-alias','codex-main'])==0
    out=json.loads(capsys.readouterr().out);assert out['state']=='unknown'
    with sqlite3.connect(p) as c:assert c.execute('SELECT count(*) FROM quota_account_snapshots').fetchone()[0]==1
    foreign=tmp_path/'foreign.db'
    with sqlite3.connect(foreign) as c:c.execute('CREATE TABLE funded(id INTEGER)')
    with pytest.raises(ValueError,match='dedicated'):
        mod.main(['--db',str(foreign),'--provider','codex','--account-alias','codex-main'])
    with sqlite3.connect(foreign) as c:assert c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()==[('funded',)]


def test_seat_park_clears_is_part_of_the_dedicated_quota_db(tmp_path):
    db=tmp_path/'quota.db'
    conn=sqlite3.connect(db)
    conn.execute('CREATE TABLE seat_park_clears(actor_id TEXT)')
    conn.commit();conn.close()
    proc=subprocess.run([sys.executable,str(Path(__file__).resolve().parents[2]/'scripts/office_quota_snapshot.py'),
                         '--db',str(db),'--provider','claude','--account-alias','t',
                         '--claude-credentials',str(tmp_path/'absent.json')],capture_output=True,text=True,timeout=30)
    assert 'dedicated_quota_database_required' not in proc.stderr


def test_a_foreign_table_beside_the_allowed_three_still_refuses(tmp_path):
    db=tmp_path/'quota.db'
    conn=sqlite3.connect(db)
    for t in ('quota_wakes','quota_account_snapshots','seat_park_clears','orders'):conn.execute(f'CREATE TABLE {t}(x)')
    conn.commit();conn.close()
    proc=subprocess.run([sys.executable,str(Path(__file__).resolve().parents[2]/'scripts/office_quota_snapshot.py'),
                         '--db',str(db),'--provider','claude','--account-alias','t',
                         '--claude-credentials',str(tmp_path/'absent.json')],capture_output=True,text=True,timeout=30)
    assert 'dedicated_quota_database_required' in proc.stderr


def test_now_uses_the_real_clock_helper_without_a_fake():
    assert isinstance(script().now(),float)
