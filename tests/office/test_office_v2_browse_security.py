"""Real published Chromium inside the tool wall; synthetic upstreams only."""
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time

import pytest

from sniper.office import egress_proxy as P,netguard as N,seat_sandbox as B

ROOT=Path(__file__).resolve().parents[2]
MARK='SYNTHETIC_BROWSER_CANARY'


@pytest.fixture
def probe(monkeypatch):
    assert (B.BRIDGE/'runtime/bin/python3').exists(),'published runtime is required'
    with tempfile.TemporaryDirectory(prefix='zeta-browser-',dir='/dev/shm') as name:
        root=Path(name);root.chmod(0o777)
        work=root/'work';(work/'tmp').mkdir(parents=True)
        (work/'browser-canary.txt').write_text(MARK)
        release=root/'release/sniper/office';release.mkdir(parents=True)
        for script in ('seat_browse.py','seat_forward.py'):shutil.copy2(ROOT/'sniper/office'/script,release/script)
        seat=root/'seats/head';seat.mkdir(parents=True)
        ca=P.load_or_create_ca(str(root/'ca'),'browser-test')
        shutil.copy2(root/'ca/ca.pem',seat/'ca.pem')
        subprocess.run(['chmod','-R','a+rwX',str(work),str(root/'release'),str(root/'seats')],check=True)
        reply=[b''];seen=[];workers=[]
        def connect(target,deadline):
            client,server=socket.socketpair()
            def serve():
                with server:
                    server.settimeout(3);data=b''
                    try:
                        while b'\r\n\r\n' not in data:
                            part=server.recv(4096)
                            if not part:return
                            data+=part
                        seen.append(data);server.sendall(reply[0])
                    except OSError:pass
            thread=threading.Thread(target=serve,daemon=True);thread.start();workers.append(thread)
            return client
        monkeypatch.setattr(N,'resolve',lambda url:N.Target('site.test',443,True,'/',socket.AF_INET,('8.8.8.8',443)))
        monkeypatch.setattr(N,'connect',connect)
        proxy=P.Proxy(str(seat/'tool-proxy.sock'),peer_uid=B.office_uid(),deny=frozenset({'blocked.test'}),ca=ca,log=str(root/'proxy.log'))
        proxy.start();os.chmod(proxy.path,0o666)
        def run(response):
            reply[0]=response
            sandbox=[str(root/'release') if a==str(B.BRIDGE/'release') else a for a in B.tool_args('head',work,bridge=root)]
            argv=['sudo','-n','-u','office']+sandbox+[
                '/opt/runtime/bin/python3','/opt/release/sniper/office/seat_forward.py',
                '/opt/runtime/bin/python3','/opt/release/sniper/office/seat_browse.py','https://site.test/']
            done=subprocess.run(argv,capture_output=True,text=True,timeout=45,cwd='/',stdin=subprocess.DEVNULL)
            assert done.returncode==0,done.stderr[-600:]
            return json.loads(done.stdout)
        try:yield run,root,seen
        finally:
            try:proxy.sock.shutdown(socket.SHUT_RDWR)
            except OSError:pass
            proxy.stop()
            for thread in workers:thread.join(4)
            subprocess.run(['sudo','-n','-u','office','rm','-rf','--',str(work)],check=True)


def test_browser_works_through_mediated_tls_without_native_chromium_sandbox(probe):
    run,_,seen=probe;body=('<html><body>'+MARK+'</body></html>').encode()
    result=run(b'HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\nContent-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body)
    assert result['state']=='known' and MARK in result['text'] and seen


def test_funded_redirect_is_denied_before_a_second_upstream(probe):
    run,root,seen=probe
    result=run(b'HTTP/1.1 302 Found\r\nLocation: https://blocked.test/private\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
    assert result['state']=='unknown'
    rows=[json.loads(line) for line in (root/'proxy.log').read_text().splitlines()]
    assert any(r['host']=='blocked.test' and r['outcome']=='denied_funded_lane' for r in rows)
    assert not any(b'host: blocked.test' in request.lower() for request in seen)


def test_plaintext_funded_redirect_cannot_escape_the_proxy(probe):
    run,root,seen=probe
    result=run(b'HTTP/1.1 302 Found\r\nLocation: http://blocked.test/private\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
    assert result['state']=='unknown' or result.get('status')==403
    rows=[json.loads(line) for line in (root/'proxy.log').read_text().splitlines()]
    assert any(r['outcome']=='request_denied' for r in rows)
    assert not any(b'host: blocked.test' in request.lower() for request in seen)


@pytest.mark.parametrize('target',['file:///opt/workspace/browser-canary.txt','chrome://version/','host-service'])
def test_unsafe_redirects_cannot_read_files_or_host_services(probe,target):
    run,_,_=probe
    with socket.socket() as service:
        if target=='host-service':
            service.bind(('127.0.0.1',0));service.listen(1);service.settimeout(.1)
            target='https://127.0.0.1:%d/'%service.getsockname()[1]
        result=run(b'HTTP/1.1 302 Found\r\nLocation: '+target.encode()+b'\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
        assert result['state']=='unknown' and MARK not in result.get('text','')
        if service.gettimeout()==.1:
            with pytest.raises(socket.timeout):
                client,_=service.accept();client.close()


def test_attachment_is_not_saved_to_the_workspace(probe):
    run,root,_=probe;body=MARK.encode()
    result=run(b'HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\nContent-Disposition: attachment; filename="blocked.bin"\r\nContent-Length: '+str(len(body)).encode()+b'\r\nConnection: close\r\n\r\n'+body)
    assert result['state']=='unknown' and not list((root/'work').rglob('blocked.bin'))


def test_origin_tls_certificate_is_still_verified(tmp_path):
    P.load_or_create_ca(str(tmp_path/'origin'),'untrusted-test-origin')
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(tmp_path/'origin/ca.pem',tmp_path/'origin/ca.key')
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(1);listener.settimeout(3)
    def serve():
        try:
            client,_=listener.accept()
            with client:
                try:context.wrap_socket(client,server_side=True).close()
                except ssl.SSLError:pass
        finally:listener.close()
    thread=threading.Thread(target=serve,daemon=True);thread.start()
    target=N.Target('site.test',443,True,'/',socket.AF_INET,listener.getsockname())
    with pytest.raises(ssl.SSLCertVerificationError):N.connect(target,time.monotonic()+2)
    thread.join(3)
    assert not thread.is_alive()
