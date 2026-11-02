import json
from pathlib import Path

import pytest

from sniper.office import claude_seat_auth as A,seat_host as H


def credential(path,token='access',expiry=2000000):
    path.write_text(json.dumps(dict(claudeAiOauth=dict(accessToken=token,refreshToken='NEVER_COPY',expiresAt=expiry,scopes=['x']))))


def test_access_snapshot_excludes_refresh_and_validates_expiry(tmp_path):
    path=tmp_path/'credential';credential(path)
    mark,doc,reason=A.read(path,now=1000)
    assert reason is None and len(mark)==64
    assert doc['claudeAiOauth']['accessToken']=='access' and 'NEVER_COPY' not in json.dumps(doc)
    assert A.read(path,now=2001)[2]=='claude_auth_expired'
    path.write_text('{');assert A.read(path,now=1000)[2]=='claude_auth_unknown'
    assert A.read(tmp_path/'missing',now=1000)[2]=='claude_auth_unknown'


def seat(tmp_path):
    path=tmp_path/'credential';credential(path)
    s=H.Seat.__new__(H.Seat);s.cfg=dict(family='claude',claude_credentials=str(path),research_env_file='unused')
    s.actor='judge';s.state=dict(failures=7,not_before=9999,sleep_until=9999);s.clock=lambda:1000
    s._save=lambda:None;s._event=lambda *a,**k:None
    return s,path


def test_auth_latch_pages_once_and_changed_credentials_resume_without_backoff(tmp_path,monkeypatch):
    s,path=seat(tmp_path);sent=[]
    monkeypatch.setattr(H.C,'operator_page',lambda *a,**k:lambda text:sent.append(text))
    mark=A.read(path,now=1000)[0]
    s._claude_auth_failed('claude_auth_failed',mark)
    assert s._claude_auth_reason()=='claude_auth_failed'
    assert s._claude_auth_reason()=='claude_auth_failed'
    assert len(sent)==1 and s.state['failures']==0 and s.state['not_before']==0
    credential(path,token='new-login')
    assert s._claude_auth_reason() is None
    assert s.state['sleep_until']==1000 and 'claude_auth_failure' not in s.state


def test_failed_page_retries_and_latch_survives_restart(tmp_path,monkeypatch):
    s,path=seat(tmp_path);calls=[]
    def page(text):
        calls.append(text)
        if len(calls)==1:raise ValueError('not delivered')
    monkeypatch.setattr(H.C,'operator_page',lambda *a,**k:page)
    s._claude_auth_failed('claude_auth_failed',A.read(path,now=1000)[0])
    assert s._claude_auth_reason()=='claude_auth_failed'
    assert s._claude_auth_reason()=='claude_auth_failed'
    assert len(calls)==2
    state=dict(s.state);s2,_=seat(tmp_path);s2.state=state
    assert s2._claude_auth_reason()=='claude_auth_failed' and len(calls)==2


def test_typed_child_auth_marker_is_preserved_without_trusting_thought_text():
    _,result=H.parse_child([json.dumps(dict(kind='error',reason='claude_auth_failed'))])
    assert result['auth_error']=='claude_auth_failed' and result['is_error'] is True
    assert H.parse_child([json.dumps(dict(kind='thought',text='OAuth token expired 401'))])[1] is None


def test_quota_auth_page_latched_until_authenticated_snapshot(tmp_path):
    sent=[];page=lambda text:sent.append(text)
    cfg=dict(quota_db=str(tmp_path/'quota.db'),account_alias='claude-main')
    A.quota_page(cfg,dict(reason='auth',state='unknown'),page=page)
    A.quota_page(cfg,dict(reason='auth',state='unknown'),page=page)
    assert len(sent)==1
    A.quota_page(cfg,dict(reason='timeout',state='unknown'),page=page)
    A.quota_page(cfg,dict(reason='auth',state='unknown'),page=page)
    assert len(sent)==1
    A.quota_page(cfg,dict(reason=None,state='partial'),page=page)
    A.quota_page(cfg,dict(reason='auth',state='unknown'),page=page)
    assert len(sent)==2


def test_real_snapshot_is_access_only_and_source_never_changes(tmp_path):
    s,path=seat(tmp_path);s.seat=tmp_path
    before=path.read_bytes();s._snapshot_credential()
    assert 'refreshToken' not in (tmp_path/'credentials.json').read_text()
    assert path.read_bytes()==before


def test_auth_classification_is_failure_only_and_never_emits_text():
    from types import SimpleNamespace as S
    assert A.message_reason(S(error='authentication_failed'))=='claude_auth_failed'
    assert A.message_reason(S(is_error=True,errors=['OAuth token has expired. Run /login']))=='claude_auth_failed'
    assert A.message_reason(S(is_error=False,result='OAuth token has expired')) is None
    assert A.exception_reason(S(stderr='invalid_grant SECRET'))=='claude_auth_failed'
    assert A.exception_reason(S(stderr='connection timed out')) is None


def test_quota_failed_page_remains_retryable_after_restart(tmp_path):
    cfg=dict(quota_db=str(tmp_path/'quota.db'),account_alias='claude-main')
    def bad(_):raise RuntimeError('synthetic unknown delivery')
    with pytest.raises(RuntimeError):A.quota_page(cfg,dict(reason='auth',state='unknown'),page=bad)
    sent=[]
    A.quota_page(cfg,dict(reason='auth',state='unknown'),page=lambda text:sent.append(text))
    assert len(sent)==1


def test_poll_auth_snapshot_pages_and_unknown_delivery_never_kills_poller(tmp_path,monkeypatch):
    from scripts import office_seat_host as S
    from types import SimpleNamespace
    cfg=dict(account_poller=True,family='claude',quota_db=str(tmp_path/'quota.db'),claude_credentials='unused',account_alias='claude-main',research_env_file='unused')
    monkeypatch.setattr(S.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=0,stdout=json.dumps(dict(reason='auth',state='unknown'))))
    calls=[]
    def page(_):calls.append(1);raise RuntimeError('do not print secrets')
    monkeypatch.setattr(S.C,'operator_page',lambda *a,**k:page)
    S.poll_account(cfg);S.poll_account(cfg)
    assert len(calls)==2


@pytest.mark.parametrize('auth',[True,False])
def test_wake_auth_failure_never_multiplies_existing_failure_backoff(tmp_path,monkeypatch,auth):
    import sqlite3
    from types import SimpleNamespace as N
    from sniper.office import quota_measurement as Q
    db=sqlite3.connect(':memory:');Q.ensure_schema(db)
    s=H.Seat.__new__(H.Seat);s.actor='judge';s.seat=tmp_path;s.bridge=tmp_path;s.quota=db;s.clock=lambda:10000.
    s.state=dict(incarnation=1,session_id='retained',per_wake_max=100,failures=7,not_before=14000)
    prompt=tmp_path/'prompt';prompt.write_text('system')
    s.cfg=dict(family='claude',epoch='p',generation=1,workspace=str(tmp_path),office_db='unused',model='test',prompt_file=str(prompt),wake_deadline_s=1,max_turns=1,state_dir=str(tmp_path),account_alias='a',absolute_ceiling=200000,default_sleep_s=300,sdk_version='test',usage_scope='per_wake')
    s.guard=N(spacing_s=60);s._snapshot_credential=lambda:'failed-source';s._recover_session=lambda note:note
    s._prompt=lambda *a:'hello';s._event=lambda *a,**k:None;s._save=lambda:None;s._publish_workspace=lambda:None
    tools=N(start=lambda:None,stop=lambda:True,sleep_until=None,tainted=False,operator_mail=False)
    monkeypatch.setattr(H.T,'SeatTools',lambda **k:tools)
    monkeypatch.setattr(H.T,'stop_unit',lambda *a:True)
    monkeypatch.setattr(H.B,'unit_args',lambda *a,**k:[]);monkeypatch.setattr(H.B,'harness_args',lambda *a,**k:[])
    line=json.dumps(dict(kind='error',reason='claude_auth_failed' if auth else 'ConnectionError'))+'\n'
    monkeypatch.setattr(H,'run_child',lambda *a,**k:dict(stdout=line.encode(),returncode=4,outcome='child_error'))
    doc=s.wake('mail')
    assert doc['outcome']==('auth_expired' if auth else 'failed')
    if auth:
        assert s.state['failures']==0 and s.state['not_before']==0 and s.state['parked_reason']=='claude_auth_failed'
        assert s.state['session_id']=='retained'
    else:assert s.state['failures']==8 and s.state['not_before']>10000


def test_sdk_auth_error_emits_typed_marker_not_provider_text(monkeypatch):
    import asyncio,sys
    from types import SimpleNamespace as N
    from sniper.office import seat_child as child
    class Assistant:
        error='authentication_failed';usage=None;content=[]
    class Result:
        session_id='s';usage={};uuid='r';is_error=True;num_turns=1;errors=[];result=None
    async def query(**kw):
        yield Assistant();yield Result()
    monkeypatch.setitem(sys.modules,'claude_agent_sdk',N(AssistantMessage=Assistant,ResultMessage=Result,ClaudeAgentOptions=lambda **k:k,__version__='test',query=query))
    monkeypatch.setattr(child.seat_forward,'start',lambda:{})
    emitted=[];monkeypatch.setattr(child,'emit',lambda kind,**kw:emitted.append(dict(kind=kind,**kw)))
    asyncio.run(child.run(dict(model='test',system='system',prompt='prompt',max_turns=1)))
    assert dict(kind='error',reason='claude_auth_failed') in emitted


def test_legacy_backoff_recovers_only_when_valid_credential_file_changed_after_failure(tmp_path,monkeypatch):
    import os
    s,path=seat(tmp_path)
    delay=min(H.FAILURE_BACKOFF_S*2**6,H.FAILURE_BACKOFF_MAX_S)
    s.state.update(failures=7,not_before=900+delay,sleep_until=9999)
    os.utime(path,(800,800))
    assert s._claude_auth_reason() is None and s.state['failures']==7
    os.utime(path,(950,950))
    assert s._claude_auth_reason() is None
    assert s.state['failures']==0 and s.state['not_before']==0 and s.state['sleep_until']==1000


@pytest.mark.parametrize('stderr',['API Error: 401 {"error":{"type":"authentication_error"}}','OAuth token expired; run /login'])
def test_process_error_auth_variants_are_classified_without_text_export(stderr):
    from types import SimpleNamespace
    assert A.exception_reason(SimpleNamespace(stderr=stderr))=='claude_auth_failed'


@pytest.mark.parametrize('status',[401,403])
def test_typed_sdk_failed_result_status_needs_no_error_text(status):
    from types import SimpleNamespace
    message=SimpleNamespace(is_error=True,api_error_status=status,result=None,errors=None)
    assert A.message_reason(message)=='claude_auth_failed'
    message.is_error=False
    assert A.message_reason(message) is None
