import json
import os
from pathlib import Path
import re
import socket
import ssl
import threading
import time

import pytest

from sniper.office import egress_proxy as E,netguard

DENY=Path('config/office/v2_egress_denylist.json')
# Docs and explorers that appear in comments, and research-only hosts no funded lane calls.
NOT_LANES={'developers.uniswap.org','github.com','solscan.io','etherscan.io','bscscan.com','basescan.org','explorer.inkonchain.com','financialmodelingprep.com','api.x.com',
           'api.openai.com','api.x.ai'}   # model providers: the Codex harness needs OpenAI, and neither is a funded read lane


def _proxy(tmp_path,**kw):
    p=E.Proxy(str(tmp_path/'p.sock'),peer_uid=kw.pop('peer_uid',os.getuid()),deny=kw.pop('deny',frozenset({'jup.ag'})),allow=kw.pop('allow',frozenset({'test','com','org','127.0.0.1','169.254.169.254','10.0.0.1','::1','jup.ag'})) if 'ca' not in kw else None,log=str(tmp_path/'log'),**kw)
    p.start();return p


def _connect(p,host):
    c=socket.socket(socket.AF_UNIX);c.settimeout(5);c.connect(p.path)
    c.sendall(b'CONNECT %s:443 HTTP/1.1\r\nHost: x\r\n\r\n'%host.encode())
    return c,c.recv(4096).decode()


@pytest.mark.parametrize('host,why',[('api.jup.ag','denied_funded_lane'),('JUP.AG','denied_funded_lane'),('lite-api.jup.ag.','denied_funded_lane'),
                                     ('127.0.0.1','address_denied'),('169.254.169.254','address_denied'),('10.0.0.1','address_denied'),('[::1]','address_denied')])
def test_refused(tmp_path,host,why):
    p=_proxy(tmp_path)
    try:
        c,reply=_connect(p,host);c.close()
        assert '403' in reply and why in reply
    finally:p.stop()


def test_suffix_is_not_substring():
    assert not E.on_list('notjup.ag',{'jup.ag'}) and E.on_list('a.jup.ag',{'jup.ag'})


@pytest.mark.parametrize('head',[b'GET http://a/ HTTP/1.1',b'CONNECT a.com:80 HTTP/1.1',b'CONNECT a.com:22 HTTP/1.1',b'CONNECT :443 HTTP/1.1'])
def test_only_connect_443(head):
    with pytest.raises(netguard.Denied):E.parse_connect(head+b'\r\n\r\n')


@pytest.mark.parametrize('authority',[
    'jup.ag/path:443','jup.ag?query:443','jup.ag#fragment:443',
    'user@jup.ag:443','jup.ag\\path:443','[jup.ag]:443',
    'jup.ag\t:443','jup.ag%2e:443',
])
def test_connect_rejects_non_authority_before_denylist(authority):
    with pytest.raises(netguard.Denied,match='request_denied'):
        E.parse_connect(('CONNECT '+authority+' HTTP/1.1\r\n\r\n').encode())


def test_equivalent_host_spellings_share_rate_budget(tmp_path):
    p=E.Proxy('x',peer_uid=os.getuid(),deny=frozenset(),allow=frozenset({'example.com'}),rate_per_min=1,clock=lambda:100.)
    assert p.decide('example.com') is None
    assert p.decide('EXAMPLE.COM.')=='denied_rate'


def test_wrong_peer_refused_before_any_read(tmp_path):
    p=_proxy(tmp_path,peer_uid=os.getuid()+1)
    try:
        try:reply=_connect(p,'example.com')[1]
        except ConnectionError:reply=''   # closed with our request unread: a reset is the same refusal
        assert reply==''
    finally:p.stop()


def test_rate_cap_and_harness_allowlist(tmp_path):
    p=E.Proxy('x',peer_uid=0,deny=frozenset(),allow=frozenset({'com'}),rate_per_min=2,clock=lambda:100.)
    assert [p.decide('a.com') for _ in range(3)]==[None,None,'denied_rate']
    assert p.decide('b.com') is None
    m=E.Proxy('x',peer_uid=0,deny=frozenset(),allow=frozenset({'anthropic.com'}))
    assert m.decide('api.anthropic.com') is None and m.decide('evil.com')=='denied_not_allowed'


def test_tunnel_pins_the_resolved_address_and_logs_host_only(tmp_path,monkeypatch):
    srv=socket.socket();srv.bind(('127.0.0.1',0));srv.listen(1)
    def echo():
        s,_=srv.accept();s.sendall(s.recv(100));s.close()
    threading.Thread(target=echo,daemon=True).start()
    target=netguard.Target('x.test',443,True,'/',socket.AF_INET,srv.getsockname())
    calls=[]
    monkeypatch.setattr(netguard,'resolve',lambda url:(calls.append(url),target)[1])
    p=_proxy(tmp_path)
    try:
        c,reply=_connect(p,'x.test')
        assert '200' in reply
        c.sendall(_hello('x.test'));assert c.recv(4096)[:1]==b'\x16';c.close()
    finally:p.stop()
    import time
    for _ in range(50):
        if (tmp_path/'log').exists() and (tmp_path/'log').read_text().endswith('\n'):break   # the file exists before its line lands
        time.sleep(.05)
    log=(tmp_path/'log').read_text()
    assert len(calls)==1 and json.loads(log)['host']=='x.test'


def test_every_funded_code_host_is_on_the_denylist():
    deny=E.load_denylist(DENY)[0]
    pattern=re.compile(r'https?://([a-zA-Z0-9.-]+\.[a-z]{2,})')
    roots=[Path('sniper/execution'),*Path('sniper/services').glob('memecoin*')]
    hosts={m for r in roots for f in ([r] if r.is_file() else r.rglob('*.py')) for m in pattern.findall(f.read_text(errors='ignore'))}
    assert hosts and not {h for h in hosts-NOT_LANES if not E.on_list(h,deny)}


def _ca(tmp_path):return E.load_or_create_ca(str(tmp_path/'ca'),'t')


def _fake_upstream(monkeypatch,reply):
    """A plain local server stands in for the origin; only netguard.resolve and connect are replaced."""
    srv=socket.socket();srv.bind(('127.0.0.1',0));srv.listen(4)
    seen=[]
    def serve():
        s,_=srv.accept();data=b''
        s.settimeout(1)
        try:
            while True:
                part=s.recv(4096)
                if not part:break
                data+=part
                if b'\r\n\r\n' in data:break
        except OSError:pass
        seen.append(data);s.sendall(reply);s.close()
    threading.Thread(target=serve,daemon=True).start()
    target=netguard.Target('site.test',443,True,'/',socket.AF_INET,srv.getsockname())
    monkeypatch.setattr(netguard,'resolve',lambda url:target)
    monkeypatch.setattr(netguard,'connect',lambda t,d:socket.create_connection(srv.getsockname()))
    return seen


def _tls(p,tmp_path,host,sni):
    c,reply=_connect(p,host)
    assert '200' in reply,reply
    ctx=ssl.create_default_context(cafile=str(tmp_path/'ca/ca.pem'));ctx.check_hostname=False
    return ctx.wrap_socket(c,server_hostname=sni)


def _read(t):
    out=b''
    try:
        while True:
            part=t.recv(4096)
            if not part:return out
            out+=part
    except (ssl.SSLError,OSError):return out


def test_mediated_request_forwarded_and_path_not_logged(tmp_path,monkeypatch):
    seen=_fake_upstream(monkeypatch,b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi')
    p=_proxy(tmp_path,ca=_ca(tmp_path))
    try:
        t=_tls(p,tmp_path,'site.test','site.test')
        t.sendall(b'GET /secret HTTP/1.1\r\nHost: site.test\r\nConnection: keep-alive\r\n\r\n')
        assert _read(t).endswith(b'hi');t.close()
    finally:p.stop()
    assert seen[0].startswith(b'GET /secret HTTP/1.1') and b'Connection: close' in seen[0] and b'keep-alive' not in seen[0]
    for _ in range(50):
        if (tmp_path/'log').exists():break
        time.sleep(.05)
    assert 'secret' not in (tmp_path/'log').read_text()


def test_origin_response_has_a_byte_bound(tmp_path,monkeypatch):
    monkeypatch.setattr(E,'MAX_BODY',128)
    _fake_upstream(monkeypatch,b'HTTP/1.1 200 OK\r\nContent-Length: 1024\r\n\r\n'+b'x'*1024)
    p=_proxy(tmp_path,ca=_ca(tmp_path))
    try:
        t=_tls(p,tmp_path,'site.test','site.test')
        t.sendall(b'GET / HTTP/1.1\r\nHost: site.test\r\n\r\n')
        out=_read(t);t.close()
    finally:p.stop()
    assert len(out)<=128
    assert 'response_too_large' in (tmp_path/'log').read_text()


def test_funded_sni_on_an_allowed_connect_is_refused(tmp_path,monkeypatch):
    """Zeta's witness: an allowed alias must not carry a funded SNI."""
    dials=[]
    monkeypatch.setattr(netguard,'resolve',lambda url:netguard.Target('site.test',443,True,'/',socket.AF_INET,('127.0.0.1',1)))
    monkeypatch.setattr(netguard,'connect',lambda t,d:dials.append(t))
    p=_proxy(tmp_path,ca=_ca(tmp_path))
    try:
        c,reply=_connect(p,'site.test');assert '200' in reply
        ctx=ssl.create_default_context(cafile=str(tmp_path/'ca/ca.pem'));ctx.check_hostname=False
        try:
            t=ctx.wrap_socket(c,server_hostname='api.jup.ag')
            t.sendall(b'GET / HTTP/1.1\r\nHost: site.test\r\n\r\n');_read(t)
        except (ssl.SSLError,OSError):pass
    finally:p.stop()
    assert not dials


@pytest.mark.parametrize('request_bytes',[
    b'GET / HTTP/1.1\r\nHost: api.jup.ag\r\n\r\n',
    b'GET / HTTP/1.1\r\nHost: site.test\r\nHost: api.jup.ag\r\n\r\n',
    b'GET http://api.jup.ag/ HTTP/1.1\r\nHost: site.test\r\n\r\n',
    b'POST / HTTP/1.1\r\nHost: site.test\r\nTransfer-Encoding: chunked\r\n\r\n',
    b'GET / HTTP/1.1\r\nHost: site.test\r\nUpgrade: websocket\r\n\r\n',
    b'POST / HTTP/1.1\r\nHost: site.test\r\nContent-Length: 1\r\nContent-Length: 2\r\n\r\n',
    b'GET / HTTP/1.1\nHost: site.test\r\n\r\n',
    b'SSH-2.0-x\r\n\r\n',
    b'GET / HTTP/1.1\r\nHost: site.test\r\n\r\nGET / HTTP/1.1\r\nHost: api.jup.ag\r\n\r\n'])
def test_request_refused_before_any_dial(tmp_path,monkeypatch,request_bytes):
    target=netguard.Target('site.test',443,True,'/',socket.AF_INET,('127.0.0.1',1))
    monkeypatch.setattr(netguard,'resolve',lambda url:target)
    dials=[];monkeypatch.setattr(netguard,'connect',lambda t,d:dials.append(t))
    p=_proxy(tmp_path,ca=_ca(tmp_path))
    try:
        t=_tls(p,tmp_path,'site.test','site.test')
        t.sendall(request_bytes);assert b'403' in _read(t);t.close()
    finally:p.stop()
    assert not dials


def test_ip_literal_refused_in_tool_mode(tmp_path):
    p=_proxy(tmp_path,ca=_ca(tmp_path))
    try:
        c,reply=_connect(p,'8.8.8.8');c.close();assert 'denied_ip_literal' in reply
    finally:p.stop()


def test_each_request_counts_against_the_cap():
    p=E.Proxy('x',peer_uid=0,deny=frozenset(),ca=object(),rate_per_min=1,clock=lambda:1.)
    assert p.decide('a.test',count=False) is None and p.decide('a.test') is None and p.decide('a.test')=='denied_rate'


def test_ca_key_is_private_and_reloaded(tmp_path):
    first=_ca(tmp_path);second=_ca(tmp_path)
    assert (tmp_path/'ca/ca.key').stat().st_mode&0o077==0 and first[0].serial_number==second[0].serial_number


def _hello(sni):
    a,b=ssl.MemoryBIO(),ssl.MemoryBIO()
    obj=ssl.create_default_context().wrap_bio(a,b,server_hostname=sni)
    try:obj.do_handshake()
    except ssl.SSLWantReadError:pass
    return b.read()


def test_harness_sni_parser_on_a_real_client_hello():
    assert E.client_hello_sni(_hello('api.anthropic.com'))=='api.anthropic.com'
    assert E.client_hello_sni(b'\x16\x03\x01') is None and E.client_hello_sni(b'GET / HTTP/1.1\r\n\r\n') is None


def test_harness_refuses_wrong_sni(tmp_path,monkeypatch):
    seen=_fake_upstream(monkeypatch,b'x')
    p=_proxy(tmp_path,allow=frozenset({'test'}))
    try:
        c,reply=_connect(p,'site.test');assert '200' in reply
        c.sendall(_hello('api.jup.ag'));assert b'denied_sni_mismatch' in c.recv(4096);c.close()
    finally:p.stop()
    assert seen==[]


def test_effective_hosts_keep_only_the_hostname(tmp_path):
    env=tmp_path/'e';env.write_text('A=https://user:pw@key123.rpc.example/v2/secret?x=1\nB="wss://ws.example.org/k"\nC=grpcs://g.example.net:443\nD=notaurl\nE=https://api.anthropic.com/x\n')
    assert E.effective_hosts([env],keep={'anthropic.com'})=={'key123.rpc.example','ws.example.org','g.example.net'}


def test_effective_hosts_fail_closed_when_unreadable(tmp_path):
    with pytest.raises(ValueError,match='funded_config_unreadable'):E.effective_hosts([tmp_path/'none'])


DOCS=frozenset({'docs.pump.fun'})


def _docs_proxy(tmp_path,monkeypatch,reply=b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi',deny=frozenset({'pump.fun','jup.ag'})):
    seen=_fake_upstream(monkeypatch,reply)
    return None,seen,_proxy(tmp_path,ca=_ca(tmp_path),deny=deny,read_only_hosts=DOCS)


def test_the_config_excepts_only_the_docs_host_and_keeps_the_parent_denied():
    assert E.load_read_only_hosts(DENY)==DOCS and 'pump.fun' in E.load_denylist(DENY)[0]


@pytest.mark.parametrize('bad',[['pump.fun'],['*.pump.fun'],['.pump.fun'],['Docs.pump.fun'],'docs.pump.fun'])
def test_a_wide_exception_refuses_to_load(tmp_path,bad):
    path=tmp_path/'d.json';path.write_text(json.dumps(dict(domains=[],read_only_host_exceptions=bad)))
    with pytest.raises(ValueError,match='read_only_hosts_invalid'):E.load_read_only_hosts(path)


@pytest.mark.parametrize('method',[b'GET',b'HEAD'])
def test_docs_get_and_head_pass(tmp_path,monkeypatch,method):
    _,seen,p=_docs_proxy(tmp_path,monkeypatch)
    try:
        t=_tls(p,tmp_path,'docs.pump.fun','docs.pump.fun')
        t.sendall(method+b' /guide HTTP/1.1\r\nHost: docs.pump.fun\r\n\r\n')
        assert b'200 OK' in _read(t);t.close()
    finally:p.stop()
    assert seen[0].startswith(method+b' /guide')


@pytest.mark.parametrize('request_bytes',[
    b'POST /x HTTP/1.1\r\nHost: docs.pump.fun\r\nContent-Length: 0\r\n\r\n',
    b'PUT /x HTTP/1.1\r\nHost: docs.pump.fun\r\n\r\n',
    b'GET /x HTTP/1.1\r\nHost: docs.pump.fun\r\nContent-Length: 1\r\n\r\nx',
    b'GET /x HTTP/1.1\r\nHost: docs.pump.fun\r\nAuthorization: Bearer k\r\n\r\n',
    b'GET /x HTTP/1.1\r\nHost: docs.pump.fun\r\nCookie: a=b\r\n\r\n'])
def test_docs_writes_and_credentials_never_dial(tmp_path,monkeypatch,request_bytes):
    target=netguard.Target('docs.pump.fun',443,True,'/',socket.AF_INET,('127.0.0.1',1))
    monkeypatch.setattr(netguard,'resolve',lambda url:target)
    dials=[];monkeypatch.setattr(netguard,'connect',lambda t,d:dials.append(t))
    p=_proxy(tmp_path,ca=_ca(tmp_path),deny=frozenset({'pump.fun'}),read_only_hosts=DOCS)
    try:
        t=_tls(p,tmp_path,'docs.pump.fun','docs.pump.fun')
        t.sendall(request_bytes);assert b'403' in _read(t);t.close()
    finally:p.stop()
    assert not dials


@pytest.mark.parametrize('host',['pump.fun','api.pump.fun','a.docs.pump.fun','docs.pump.fun.evil.pump.fun','frontend-api.pump.fun','api.jup.ag','dexscreener.com','api.geckoterminal.com'])
def test_every_other_host_stays_denied(tmp_path,host):
    p=_proxy(tmp_path,ca=_ca(tmp_path),deny=E.load_denylist(DENY)[0],read_only_hosts=E.load_read_only_hosts(DENY))
    try:assert p.decide(host)=='denied_funded_lane'   # a redirect is a new CONNECT, so it lands here too
    finally:p.stop()


def test_a_funded_docs_host_and_the_harness_proxy_get_no_exception(tmp_path):
    p=_proxy(tmp_path,ca=_ca(tmp_path),deny=frozenset({'pump.fun','docs.pump.fun'}),read_only_hosts=DOCS)
    try:assert p.decide('docs.pump.fun')=='denied_funded_lane'
    finally:p.stop()
    m=_proxy(tmp_path,deny=frozenset({'pump.fun'}),allow=frozenset({'docs.pump.fun'}),read_only_hosts=DOCS)
    try:assert m.decide('docs.pump.fun')=='denied_funded_lane'
    finally:m.stop()
