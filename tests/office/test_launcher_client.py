from contextlib import contextmanager
import json
import socket
import threading
from uuid import uuid4
import pytest
from sniper.office import launcher_client as C


@contextmanager
def auth(adapter, root):
    yield


def fixture_client(tmp_path, response=None):
    config=dict(socket_path=str(tmp_path/'launcher.sock'),request_root=str(tmp_path/'requests'),child_root=str(tmp_path/'children'))
    client=C.Client(config,clock=lambda:1000.,auth=auth,verify_server=lambda sock:True)
    return client,config


def test_missing_auth_or_peer_verification_refuses_before_dispatch(tmp_path):
    with pytest.raises(ValueError):C.Client({},clock=lambda:1000.,auth=None,verify_server=None)


def test_unix_roundtrip_fixed_request_and_result_identity(tmp_path):
    client,cfg=fixture_client(tmp_path);ready=threading.Event();seen=[]
    def server():
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:
            s.bind(cfg['socket_path']);s.listen(1);ready.set()
            conn,_=s.accept()
            with conn:
                msg=json.loads(conn.makefile('rb').readline());seen.append(msg)
                root=tmp_path/'children'/msg['request_id'];root.mkdir(parents=True)
                (root/'result.json').write_text(json.dumps({'text':'{}','model_echo':None}))
                conn.sendall(json.dumps(dict(request_id=msg['request_id'],outcome='success',output_file=msg['request_id']+'/result.json',exit_code=0,model_result={'text':'{}','model_echo':None})).encode()+b'\n')
    thread=threading.Thread(target=server);thread.start();assert ready.wait(2)
    result=client.call('codex_exec',{'system':'s'},deadline_at=1002.)
    thread.join(2)
    assert result['text']=='{}' and result['model_echo'] is None
    assert set(seen[0])=={'adapter_id','request_id','request_file','deadline_at'}
    assert seen[0]['request_file']==seen[0]['request_id']+'/request.json'


def test_expired_call_never_creates_scratch(tmp_path):
    client,cfg=fixture_client(tmp_path)
    assert client.call('codex_exec',{},deadline_at=999.)['call_error']=='timeout'
    assert not (tmp_path/'requests').exists()


def test_result_path_cannot_escape_child_root(tmp_path):
    client,cfg=fixture_client(tmp_path)
    with pytest.raises(ValueError):client.read_result(str(uuid4()),{'output_file':'../../secret','outcome':'success'})


def test_read_regular_fifo_is_nonblocking(tmp_path):
    import os
    path=tmp_path/'fifo';os.mkfifo(path)
    with pytest.raises(ValueError):C.read_regular(path)


@pytest.mark.parametrize('rid',['bad','../escape',True])
def test_invalid_request_id_is_typed_refusal(tmp_path,rid):
    client,cfg=fixture_client(tmp_path)
    assert client.call('codex_exec',{},deadline_at=1002.,request_id=rid)['call_error']=='invalid_request'


def test_client_timeout_does_not_redispatch(tmp_path):
    client,cfg=fixture_client(tmp_path);ready=threading.Event();calls=[]
    def server():
        import time
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:
            s.bind(cfg['socket_path']);s.listen(1);ready.set()
            conn,_=s.accept()
            with conn:
                calls.append(conn.recv(4096));time.sleep(.15)
    t=threading.Thread(target=server);t.start();assert ready.wait(2)
    rid=str(uuid4())
    assert client.call('codex_exec',{},deadline_at=1000.05,request_id=rid)['call_error']=='timeout'
    assert client.call('codex_exec',{},deadline_at=1001.,request_id=rid)['call_error']=='existing_unknown'
    t.join(2);assert len(calls)==1


def test_launcher_transport_binds_provider_clock_and_raw_request():
    calls=[]
    class Fake:
        def call(self,*a,**kw):calls.append((a,kw));return {'text':'{}'}
    adapter=C.Transport('codex',Fake(),clock=lambda:1000.)
    assert adapter('s','u',model='gpt-6-luna',deadline_s=2,schema_obj={'type':'object'})=={'text':'{}'}
    assert calls==[(('codex_exec',dict(system='s',user='u',model='gpt-6-luna',deadline_s=2,schema={'type':'object'})),{'deadline_at':1002.})]


@pytest.mark.parametrize('reason', ['codex_busy', 'no_codex_login', 'codex_cleanup_unproven',
                                  'codex_auth_writeback_refused', 'codex_auth_snapshot_missing'])
def test_codex_auth_refusal_preserves_typed_pause_reason(tmp_path, reason):
    from sniper.office.model_auth import AuthRefused
    client, _ = fixture_client(tmp_path)
    @contextmanager
    def refused(adapter, parent):
        raise AuthRefused(reason)
        yield
    client.auth = refused
    assert client.call('codex_exec', {}, deadline_at=1002.)['call_error'] == reason


def test_inline_result_does_not_need_any_child_root_access(tmp_path):
    client,_=fixture_client(tmp_path)
    client.config.pop('child_root')
    rid=str(uuid4())
    assert client.read_result(rid,dict(request_id=rid,outcome='success',exit_code=0,
        output_file=rid+'/result.json',model_result={'text':'{}'}))=={'text':'{}'}


@pytest.mark.parametrize('reason',['codex_busy','codex_cleanup_unproven','claude_auth_near_expiry'])
def test_authenticated_error_receipt_keeps_type_and_requires_request_identity(tmp_path,reason):
    client,_=fixture_client(tmp_path);rid=str(uuid4())
    receipt=dict(request_id=rid,outcome='refused',reason=reason)
    assert client.read_result(rid,receipt)=={'call_error':reason,'request_id':rid}
    receipt['request_id']=str(uuid4())
    with pytest.raises(ValueError):client.read_result(rid,receipt)


def test_daemon_existing_unknown_is_not_a_generic_refusal(tmp_path):
    client,_=fixture_client(tmp_path);rid=str(uuid4())
    assert client.read_result(rid,dict(request_id=rid,outcome='existing_unknown'))['call_error']=='existing_unknown'
