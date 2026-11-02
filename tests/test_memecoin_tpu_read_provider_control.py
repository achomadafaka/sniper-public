import json
import pytest
from scripts.research import memecoin_tpu_read_provider_control as c
from sniper.execution.free_endpoint_registry import SOLANA_MAINNET_GENESIS


def test_control_identity_uses_one_isolated_read():
    calls = []
    def http(method, url, *, body=None, timeout=None):
        request = json.loads(body)
        calls.append((method, url, request, timeout))
        return 200, {'jsonrpc': '2.0', 'id': request['id'], 'result': SOLANA_MAINNET_GENESIS}, None
    assert c.verify_control_identity('https://isolated.invalid', http=http) is None
    assert calls == [('POST', 'https://isolated.invalid', {'jsonrpc': '2.0', 'id': 'tpu-control-genesis', 'method': 'getGenesisHash', 'params': []}, 2.0)]


@pytest.mark.parametrize('status,payload', [(429, {}), (302, {}), (200, []), (200, {'id':'wrong','result':SOLANA_MAINNET_GENESIS}), (200, {'id':'tpu-control-genesis','result':'wrong'}), (200, {'id':'tpu-control-genesis','error':{}})])
def test_bad_identity_is_sanitized(status, payload):
    with pytest.raises(RuntimeError, match='identity unavailable') as exc:
        c.verify_control_identity('https://secret.invalid/key', http=lambda *a, **k: (status,payload,None))
    assert 'secret' not in str(exc.value)


def test_transport_failure_is_sanitized():
    def http(*a, **k):
        raise TimeoutError('secret URL')
    with pytest.raises(RuntimeError, match='identity unavailable') as exc:
        c.verify_control_identity('https://secret.invalid', http=http)
    assert 'secret' not in str(exc.value)


@pytest.fixture
def control(tmp_path, monkeypatch):
    root = tmp_path/'release'; root.mkdir()
    manifest = {'sha':'a'*40, 'release_path':str(root), 'sidecar':{'path':'/immutable/artifact', 'sha256':'b'*64,'cargo_lock_sha256':'c'*64}}
    path = tmp_path/'manifest'; path.write_text(json.dumps(manifest))
    monkeypatch.setattr(c, '_release_head', lambda *a: 'a'*40)
    monkeypatch.setattr(c, 'verify_sidecar', lambda *a: [])
    monkeypatch.setenv('CONTROL_RPC', 'https://secret.invalid/key')
    class Clock:
        now=0
        def monotonic(self): return self.now
        def sleep(self, n): self.now += n
    class Child:
        pid=12345
        stopped=False
        def poll(self): return None if not self.stopped else 0
        def terminate(self): self.stopped=True
        def wait(self, timeout): return 0
    child=Child(); launches=[]; frames=[]; slots=iter(range(100,200))
    def spawn(argv, **kw): launches.append((argv,kw)); return child
    class Socket:
        def settimeout(self,n): pass
        def send(self,b):
            self.req=json.loads(b[4:]); frames.append(self.req)
            self.buf=c.encode_frame({'request_id':self.req['request_id'], 'ready':True,'leader_count':4,'leader_slot':next(slots)})
            return len(b)
        def recv(self,n): out=self.buf[:n]; self.buf=self.buf[n:]; return out
        def close(self): pass
    kw=dict(code_root=root, manifest_path=path,rpc_env_name='CONTROL_RPC',duration_s=2,output_path=tmp_path/'out',spawn=spawn,connect=lambda *a:Socket(),http=lambda *a,**k:(200,{'id':'tpu-control-genesis','result':SOLANA_MAINNET_GENESIS},None),clock=Clock())
    return kw,child,launches,frames


def test_private_health_only_cleanup(control):
    kw,child,launches,frames=control
    result=c.run_control(**kw)
    assert result['submitted_count']==0 and result['independent_schedule_verified'] is False
    assert child.stopped and frames and all(set(f)=={'request_id','health'} and f['health'] is True for f in frames)
    argv, options=launches[0]
    assert argv==['/immutable/artifact']
    assert set(options['env'])=={'SNIPER_TPU_RPC_URL','SNIPER_TPU_SOCKET_PATH','SNIPER_TPU_PARENT_PID'}
    assert not __import__('pathlib').Path(options['env']['SNIPER_TPU_SOCKET_PATH']).parent.exists()
    assert 'secret' not in json.dumps(result)


def test_health_alignment_uses_fresh_native_head_and_never_signed_bytes(control):
    kw, child, launches, frames = control
    heads = iter(range(100, 200))
    kw['native_head_fn'] = lambda: (next(heads), kw['clock'].monotonic())
    result = c.run_control(**kw)
    assert result['native_head_alignment_verified'] is True
    assert result['alignment_basis'] == 'health_native_minimum_within_two_slots'
    assert all(set(f) == {'request_id', 'health', 'min_context_slot'} for f in frames)
    assert all(f['health'] is True for f in frames)
    assert all(s['native_head_age_ms'] <= 1000 and s['routing_slot_lag'] <= 2 for s in result['samples'])
    assert result['submitted_count'] == 0 and child.stopped


@pytest.mark.parametrize('head', [None, (100, -10), (100, 100), (True, 0), ('secret', 0)])
def test_absent_stale_or_malformed_native_head_keeps_alignment_unknown(control, head):
    kw, child, launches, frames = control
    result = c.run_control(**kw, native_head_fn=lambda: head)
    assert result['native_head_alignment_verified'] is False
    assert all('min_context_slot' not in f for f in frames)
    assert 'secret' not in json.dumps(result)


def test_lagging_routing_slot_fails_alignment_without_any_send(control):
    kw, child, launches, frames = control
    with pytest.raises(RuntimeError, match='native head alignment'):
        c.run_control(**kw, native_head_fn=lambda: (104, kw['clock'].monotonic()))
    assert child.stopped and not kw['output_path'].exists()
    assert all('signed_payload_base64' not in f for f in frames)


@pytest.mark.parametrize('fault',['identity','manifest','unready','zero','frozen','socket','child'])
def test_control_fails_closed(control,monkeypatch,fault):
    kw,child,launches,frames=control
    if fault=='identity': kw['http']=lambda *a,**k:(429,{},None)
    if fault=='manifest': monkeypatch.setattr(c,'verify_sidecar',lambda *a:['bad'])
    if fault=='child': child.stopped=True
    if fault in ('unready','zero','frozen'):
        original=kw['connect']
        def connect(*a):
            sock=original(*a); send=sock.send
            def altered(b):
                n=send(b); payload=json.loads(sock.buf[4:])
                payload.update({'ready':False} if fault=='unready' else {'leader_count':0} if fault=='zero' else {'leader_slot':100})
                sock.buf=c.encode_frame(payload); return n
            sock.send=altered; return sock
        kw['connect']=connect
    if fault=='socket':
        def connect(*a): raise TimeoutError('secret')
        kw['connect']=connect
    with pytest.raises(RuntimeError) as exc: c.run_control(**kw)
    assert 'secret' not in str(exc.value)
    assert not kw['output_path'].exists()
    if fault in ('identity','manifest'): assert not launches
    else: assert child.stopped


@pytest.mark.parametrize('status,size',[ (302,0),(200,4097)])
def test_default_http_caps_body_and_rejects_redirect(monkeypatch,status,size):
    import httpx
    calls=[]; closed=[]
    class Response:
        status_code=status
        headers={}
        def __enter__(self): return self
        def __exit__(self,*a): closed.append(True)
        def iter_raw(self): yield b'x'*size
    class Client:
        def __init__(self,**kw): assert kw=={'timeout':2.0,'follow_redirects':False,'trust_env':False}
        def __enter__(self): return self
        def __exit__(self,*a): pass
        def stream(self,*a,**kw): calls.append(a); return Response()
    monkeypatch.setattr(httpx,'Client',Client)
    with pytest.raises(RuntimeError): c.verify_control_identity('https://isolated.invalid',http=c._http_read)
    assert len(calls)==1 and closed==[True]


@pytest.mark.parametrize('payload',[[],{'id':'wrong','result':SOLANA_MAINNET_GENESIS},{'id':'tpu-control-genesis','result':'wrong'},{'id':'tpu-control-genesis','error':{'message':'secret'}}])
def test_identity_failure_precedes_spawn(control,payload):
    kw,child,launches,frames=control
    kw['http']=lambda *a,**k:(200,payload,None)
    with pytest.raises(RuntimeError): c.run_control(**kw)
    assert launches==[] and frames==[]


def test_exact_release_head_precedes_spawn(control,monkeypatch):
    kw,child,launches,frames=control
    monkeypatch.setattr(c,'_release_head',lambda *a:'d'*40)
    with pytest.raises(RuntimeError): c.run_control(**kw)
    assert not launches


def test_duration_cap_precedes_spawn(control):
    kw,child,launches,frames=control
    kw['duration_s']=20.001
    with pytest.raises(RuntimeError): c.run_control(**kw)
    assert not launches


def test_kill_only_private_child_when_terminate_times_out(control):
    import subprocess
    kw,child,launches,frames=control
    waits=[]
    def wait(timeout):
        waits.append(timeout)
        if len(waits)==1: raise subprocess.TimeoutExpired('private child',timeout)
        return 0
    killed=[]
    child.terminate=lambda:None
    child.wait=wait
    child.kill=lambda:killed.append(child.pid)
    c.run_control(**kw)
    assert waits==[0.5,0.5] and killed==[child.pid]


def test_slow_drip_body_deadline_precedes_spawn(control, monkeypatch):
    import httpx
    kw, child, launches, frames = control
    kw['duration_s'] = 20
    kw['http'] = c._http_read
    consumed = []
    closed = []
    class Drip(httpx.SyncByteStream):
        def __iter__(self):
            for number in range(10):
                kw['clock'].sleep(1)
                consumed.append(number)
                yield b' '
        def close(self): closed.append(True)
    original_client = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=Drip()))
    monkeypatch.setattr(httpx, 'Client', lambda **options: original_client(transport=transport, **options))
    monkeypatch.setattr(c.time, 'monotonic', kw['clock'].monotonic)
    with pytest.raises(RuntimeError, match='identity unavailable'):
        c.run_control(**kw)
    assert consumed == [0, 1]
    assert closed == [True]
    assert launches == [] and frames == []


def test_unexpected_encoding_rejected_before_body(monkeypatch):
    import httpx
    requests = []
    consumed = []
    closed = []
    class Body(httpx.SyncByteStream):
        def __iter__(self):
            consumed.append(True)
            yield b'not gzip'
        def close(self): closed.append(True)
    def handler(request):
        requests.append(request)
        return httpx.Response(200, headers={'Content-Encoding':'gzip'}, stream=Body())
    original_client = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **options: original_client(transport=httpx.MockTransport(handler), **options))
    with pytest.raises(RuntimeError, match='identity unavailable'):
        c.verify_control_identity('https://isolated.invalid', http=c._http_read)
    assert len(requests) == 1 and requests[0].headers['Accept-Encoding'] == 'identity'
    assert not consumed and closed == [True]


def test_identity_budget_includes_manifest_time(control, monkeypatch):
    kw, child, launches, frames = control
    kw['duration_s'] = 20
    def verify(*args):
        kw['clock'].sleep(18.6)
        return []
    monkeypatch.setattr(c, 'verify_sidecar', verify)
    timeouts = []
    def http(*args, **options):
        timeouts.append(options['timeout'])
        kw['clock'].sleep(options['timeout'])
        raise TimeoutError('secret')
    kw['http'] = http
    with pytest.raises(RuntimeError, match='identity unavailable'):
        c.run_control(**kw)
    assert timeouts == [pytest.approx(0.4)]
    assert kw['clock'].now <= 19.0
    assert not launches and not frames


def test_delayed_first_byte_then_stall_has_operation_deadline(monkeypatch):
    import httpx
    import subprocess
    import sys
    import time
    class Stalled(httpx.SyncByteStream):
        def __iter__(self):
            time.sleep(1.9)
            yield b' '
            time.sleep(2)
            yield b' '
    original_client = httpx.Client
    original_run = subprocess.run
    original_popen = subprocess.Popen
    workers = []
    def popen(*args, **options):
        worker = original_popen(*args, **options)
        workers.append(worker)
        return worker
    monkeypatch.setattr(c.subprocess, 'Popen', popen)
    monkeypatch.setattr(httpx, 'Client', lambda **options: original_client(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, stream=Stalled())), **options))
    # The child uses the real helper entry point and HTTPX stream, with no network.
    child_code = '''import httpx,time
from scripts.research import memecoin_tpu_read_provider_control as c
class Stalled(httpx.SyncByteStream):
    def __iter__(self):
        time.sleep(1.9)
        yield b' '
        time.sleep(2)
        yield b' '
original=httpx.Client
httpx.Client=lambda **options:original(transport=httpx.MockTransport(lambda req:httpx.Response(200,stream=Stalled())),**options)
c._identity_http_main()
'''
    calls = []
    def run(argv, **options):
        calls.append((argv, options))
        return original_run([sys.executable, '-c', child_code], **options)
    monkeypatch.setattr(c.subprocess, 'run', run)
    started = time.monotonic()
    with pytest.raises(RuntimeError):
        c.verify_control_identity('https://secret.invalid/key', http=c._http)
    assert time.monotonic() - started < 3
    assert len(calls) == 1
    argv, options = calls[0]
    assert 'secret' not in str(argv)
    assert options['env'] == {} and options['timeout'] == 2.0
    assert options['stdout'] == subprocess.DEVNULL and options['stderr'] == subprocess.DEVNULL
    assert len(workers) == 1 and workers[0].poll() is not None
    assert workers[0].returncode < 0  # hard deadline killed and reaped this exact worker


@pytest.mark.parametrize('valid',[True,False])
def test_identity_worker_exits_success_only_after_validated_read(monkeypatch,capsys,valid):
    import io
    calls=[]
    monkeypatch.setattr(c.sys, 'stdin', io.StringIO(json.dumps({'url':'https://secret.invalid/key','timeout':0.4})))
    def http(method,url,*,body,timeout):
        calls.append((method,url,json.loads(body),timeout))
        return 200, {'id':'tpu-control-genesis','result':SOLANA_MAINNET_GENESIS if valid else 'wrong'},None
    monkeypatch.setattr(c,'_http_read',http)
    if valid:
        assert c._identity_http_main() is None
    else:
        with pytest.raises(SystemExit) as exc: c._identity_http_main()
        assert exc.value.code == 1
    assert calls == [('POST','https://secret.invalid/key',{'jsonrpc':'2.0','id':'tpu-control-genesis','method':'getGenesisHash','params':[]},0.4)]
    captured=capsys.readouterr()
    assert captured.out == captured.err == ''
