"""GPT seat model proxy: fake-token swap, route wall, request accounting. All values are fabricated."""
import dataclasses
import json
import socket
import sqlite3
import threading
import time

import pytest

from sniper.office import egress_proxy as P,model_proxy as M,netguard,quota_measurement as Q

SECRET='real.SECRET.token'


class Upstream:
    def __init__(self,reply):
        self.reply=reply;self.got=b''
    def __call__(self,target,deadline):
        a,b=socket.socketpair();self.peer=a
        def serve():
            a.settimeout(2)
            try:
                while b'\r\n\r\n' not in self.got:self.got+=a.recv(65536)
                a.sendall(self.reply)
            finally:a.close()
        self.thread=threading.Thread(target=serve);self.thread.start()
        return b


@pytest.fixture
def seat(tmp_path):
    db=tmp_path/'q.db';conn=sqlite3.connect(db,isolation_level=None);Q.ensure_schema(conn);conn.close()
    auth=tmp_path/'auth.json';auth.write_text(json.dumps(dict(tokens=dict(access_token=SECRET,account_id='acct'))))
    def make(reply=b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nSet-Cookie: a=b\r\n\r\ndata: ok\n\n'):
        up=Upstream(reply)
        return M.ModelProxy(str(tmp_path/'s'),peer_uid=0,deny=frozenset(),ca=('c','k'),actor='seat',account_alias='gpt',quota_db=str(db),auth_file=str(auth),connect=up),up
    return make,db,auth


def run(proxy,request,host=M.HOST):
    a,b=socket.socketpair();a.sendall(request);a.shutdown(socket.SHUT_WR);a.settimeout(2)
    target=netguard.Target(**{f.name:None for f in dataclasses.fields(netguard.Target)})
    error=None
    try:proxy._request(b,host,target)
    except netguard.Denied as exc:error=str(exc)
    b.close();out=b''
    try:
        while part:=a.recv(65536):out+=part
    except ConnectionResetError:pass   # refused with the request unread
    return error,out


def rows(db):
    conn=sqlite3.connect(db)
    try:return conn.execute('SELECT route,counted,outcome FROM quota_model_requests').fetchall()
    finally:conn.close()


def post(route='/backend-api/codex/responses',extra=b''):
    return b'POST '+route.encode()+b' HTTP/1.1\r\nHost: chatgpt.com\r\nContent-Length: 2\r\n'+extra+b'\r\n{}'


def test_swap_replaces_fake_malformed_and_duplicate_credentials(seat):
    make,db,_=seat;proxy,up=make()
    error,out=run(proxy,post(extra=b'Authorization: Bearer fake\r\nauthorization: junk\r\nCookie: x=y\r\nChatGPT-Account-Id: fake\r\n'))
    up.thread.join()
    assert error is None and b'data: ok' in out
    sent=up.got.lower()
    assert sent.count(b'authorization:')==1 and up.got.count(b'Bearer '+SECRET.encode())==1 and b'fake' not in sent and b'cookie' not in sent and b'junk' not in sent
    assert SECRET.encode() not in out and b'set-cookie' not in out.lower()
    assert rows(db)==[('/backend-api/codex/responses',1,'status_200')]


@pytest.mark.parametrize('request_bytes',[
    b'POST /backend-api/codex/analytics-events/events HTTP/1.1\r\nHost: chatgpt.com\r\nContent-Length: 0\r\n\r\n',
    b'POST /backend-api/ps/mcp HTTP/1.1\r\nHost: chatgpt.com\r\nContent-Length: 0\r\n\r\n',
    b'GET /backend-api/codex/responses HTTP/1.1\r\nHost: chatgpt.com\r\n\r\n',
    b'GET /backend-api/codex/models/../../me HTTP/1.1\r\nHost: chatgpt.com\r\n\r\n',
])
def test_routes_off_the_list_get_no_token(seat,request_bytes):
    make,db,_=seat;proxy,up=make()
    error,_out=run(proxy,request_bytes)
    assert error=='denied_route' and up.got==b'' and rows(db)[0][1]==0


def test_websocket_refused_uncounted(seat):
    make,db,_=seat;proxy,up=make()
    error,_out=run(proxy,b'GET /backend-api/codex/responses HTTP/1.1\r\nHost: chatgpt.com\r\nUpgrade: websocket\r\n\r\n')
    assert error=='denied_non_http' and up.got==b'' and rows(db)==[('unparsed',0,'denied_non_http')]


def test_other_host_never_gets_the_token(seat):
    make,_db,_=seat;proxy,up=make()
    error,_out=run(proxy,post().replace(b'chatgpt.com',b'evil.example'),host='evil.example')
    assert error=='denied_not_allowed' and up.got==b''
    assert proxy.decide('ab.chatgpt.com',count=False)=='denied_not_allowed'


def test_redirect_stops_before_the_client(seat):
    make,db,_=seat;proxy,up=make(b'HTTP/1.1 302 Found\r\nLocation: https://evil.example/\r\n\r\n')
    error,out=run(proxy,post());up.thread.join()
    assert error=='upstream_redirect_refused' and out==b'' and rows(db)[0][1:]==(1,'upstream_redirect_refused')


def test_echoed_token_never_reaches_the_client(seat):
    make,_db,_=seat;proxy,up=make(b'HTTP/1.1 500 x\r\n\r\nbad '+SECRET.encode())
    error,out=run(proxy,post());up.thread.join()
    assert error=='upstream_echoed_credential' and SECRET.encode() not in out


def test_failed_accounting_write_does_not_forward(seat,tmp_path):
    make,db,_=seat;proxy,up=make()
    conn=sqlite3.connect(db);conn.execute('DROP TABLE quota_model_requests');conn.commit();conn.close()
    error,_out=run(proxy,post())
    assert error=='accounting_unavailable' and up.got==b''


def test_unusable_token_does_not_forward(seat):
    make,db,auth=seat;proxy,up=make()
    auth.write_text(json.dumps(dict(tokens=dict(access_token='a\r\nX: y',account_id='acct'))))
    error,_out=run(proxy,post())
    assert error=='token_unavailable' and up.got==b''


def test_direct_request_is_what_host_accounting_and_guard_read(seat):
    make,db,_=seat;proxy,up=make()
    run(proxy,post());up.thread.join()
    conn=sqlite3.connect(db,isolation_level=None);now=time.time()
    assert Q.model_requests(conn,actor_id='seat',since=now-60,until=now+1)==1
    doc=dict(provider='codex',state='known',snapshot_id='s',windows={'x:primary':dict(used_percent=1,resets_at=now+99)})
    conn.execute('INSERT INTO quota_account_snapshots(snapshot_id,account_alias,observed_at,payload_json) VALUES(?,?,?,?)',('s','gpt',now,json.dumps(doc)))
    assert Q.codex_account_guard(conn,account_alias='gpt',now=now+1,max_age_s=60)['model_requests_1h']==1
    conn.execute('DROP TABLE quota_model_requests')
    guard=Q.codex_account_guard(conn,account_alias='gpt',now=now+1,max_age_s=60)
    assert guard['allowed'] is False and guard['reason']=='account_snapshot_read_failed'


def test_body_after_a_head_that_arrived_alone(seat):
    make,_db,_=seat;proxy,up=make(b'HTTP/1.1 200 OK\r\n\r\n')
    class Split:
        def __init__(self,sock):self.sock=sock;self.n=0
        def __getattr__(self,name):return getattr(self.sock,name)
        def recv(self,size):
            self.n+=1
            return b'HTTP/1.1 200 OK\r\n\r\n' if self.n==1 else b'data: late\n\n' if self.n==2 else b''
    inner=proxy.connect
    proxy.connect=lambda t,d:Split(inner(t,d))
    error,out=run(proxy,post());up.thread.join()
    assert error is None and b'data: late' in out


def test_compressed_response_refused_and_identity_requested(seat):
    make,_db,_=seat;proxy,up=make(b'HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\n\r\nzzz')
    error,out=run(proxy,post(extra=b'Accept-Encoding: gzip, zstd\r\n'));up.thread.join()
    assert error=='upstream_encoding_refused' and out==b''
    assert b'Accept-Encoding: identity' in up.got and b'zstd' not in up.got


def scripted(proxy,chunks):
    class Split:
        def __init__(self,sock):self.sock=sock;self.left=list(chunks)
        def __getattr__(self,name):return getattr(self.sock,name)
        def recv(self,size):return self.left.pop(0) if self.left else b''
    inner=proxy.connect
    proxy.connect=lambda t,d:Split(inner(t,d))


def test_token_in_one_byte_reads_leaks_no_part(seat):
    make,_db,_=seat;proxy,up=make(b'')
    scripted(proxy,[b'HTTP/1.1 200 OK\r\n\r\n']+[bytes([c]) for c in b'x'+SECRET.encode()])
    error,out=run(proxy,post());up.thread.join()
    assert error=='upstream_echoed_credential' and SECRET[:2].encode() not in out


def test_chunked_body_is_decoded_for_the_scan_and_the_client(seat):
    make,_db,_=seat
    secret=SECRET.encode()
    proxy,up=make(b'')
    scripted(proxy,[b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n',b'5\r\n'+secret[:5]+b'\r\n','%x\\r\\n'.encode().decode('unicode_escape').encode()%(len(secret)-5)+secret[5:]+b'\r\n0\r\n\r\n'])
    error,out=run(proxy,post());up.thread.join()
    assert error=='upstream_echoed_credential' and secret[:5] not in out
    proxy,up=make(b'')
    scripted(proxy,[b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n4\r\ndata\r\n',b'0\r\n\r\n'])
    error,out=run(proxy,post());up.thread.join()
    assert error is None and out.endswith(b'\r\n\r\ndata') and b'chunked' not in out


@pytest.mark.parametrize('chunks',[[b'HTTP/1.1 200 OK\r\nTransfer-Encoding: gzip, chunked\r\n\r\n'],[b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Length: 1\r\n\r\n'],[b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nzz\r\n'],[b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n4\r\ndata\r\n'],[b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n'],[b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0\r\nX: y\r\n\r\n']])
def test_bad_response_framing_fails_closed(seat,chunks):
    make,_db,_=seat;proxy,up=make(b'')
    scripted(proxy,chunks)
    error,_out=run(proxy,post());up.thread.join()
    assert error=='upstream_framing'
