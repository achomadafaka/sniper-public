"""Host-side egress proxy for one Office v2 seat, on a Unix socket. Nothing it decrypts is logged.

Tool sandbox (`ca` given): TLS ends here. One HTTP/1.1 request per connection, Host and SNI must equal the CONNECT host,
and every request counts against the cap. A seat picks its own SNI on an opaque tunnel, so a tunnel cannot enforce a host wall.
Harness (`allow` given, no `ca`): opaque tunnel to model hosts only, ClientHello SNI must equal the CONNECT host."""
from collections import defaultdict,deque
from dataclasses import replace
import hashlib
import ipaddress
import json
import os
import re
import socket
import ssl
import tempfile
import threading
import time

from sniper.office import netguard,search_broker as S

MAX_HEADER=8192
IDLE_S=120
MAX_CONNECTIONS=64
MAX_BODY=16*1024**2
HOP=(b'connection',b'proxy-connection',b'keep-alive',b'te',b'upgrade',b'proxy-authorization')


def load_denylist(path):
    raw=open(path,'rb').read()
    return frozenset(json.loads(raw)['domains']),hashlib.sha256(raw).hexdigest()


def load_read_only_hosts(path):
    """Exact hosts a domain on the denylist still allows: GET and HEAD only, no credentials, tool sandbox only. Never a wildcard."""
    hosts=json.loads(open(path,'rb').read()).get('read_only_host_exceptions',[])
    if not isinstance(hosts,list) or not all(isinstance(h,str) and h==h.lower() and not h.startswith(('.','*')) and h.count('.')>=2 for h in hosts):raise ValueError('read_only_hosts_invalid')
    return frozenset(hosts)


def load_or_create_ca(directory,name):
    """Returns (ca_cert, ca_key). The key file is 0600 on the host and is never bound into a sandbox; only ca.pem is published."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes,serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    import datetime
    key_path,cert_path=os.path.join(directory,'ca.key'),os.path.join(directory,'ca.pem')
    if os.path.exists(key_path):
        return (x509.load_pem_x509_certificate(open(cert_path,'rb').read()),
                serialization.load_pem_private_key(open(key_path,'rb').read(),None))
    os.makedirs(directory,mode=0o700,exist_ok=True)
    key=ec.generate_private_key(ec.SECP256R1())
    subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'office-seat-'+name)])
    now=datetime.datetime.now(datetime.timezone.utc)
    cert=(x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now-datetime.timedelta(days=1))
          .not_valid_after(now+datetime.timedelta(days=365))
          .add_extension(x509.BasicConstraints(ca=True,path_length=0),critical=True)
          .add_extension(x509.KeyUsage(digital_signature=False,content_commitment=False,key_encipherment=False,data_encipherment=False,
                                       key_agreement=False,key_cert_sign=True,crl_sign=True,encipher_only=False,decipher_only=False),critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),critical=False).sign(key,hashes.SHA256()))
    fd=os.open(key_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as f:
        f.write(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    with open(cert_path,'wb') as f:f.write(cert.public_bytes(serialization.Encoding.PEM))
    return cert,key


def leaf_context(ca,host):
    """Server context for one host, signed by the seat CA."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes,serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    import datetime
    ca_cert,ca_key=ca
    key=ec.generate_private_key(ec.SECP256R1())
    now=datetime.datetime.now(datetime.timezone.utc)
    cert=(x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,host[:64])]))
          .issuer_name(ca_cert.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now-datetime.timedelta(days=1)).not_valid_after(now+datetime.timedelta(days=7))
          .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]),critical=False)
          .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),critical=False)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),critical=False).sign(ca_key,hashes.SHA256()))
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.set_alpn_protocols(['http/1.1'])
    with tempfile.NamedTemporaryFile() as f:   # the ssl module only loads keys from a path; a throwaway leaf key, removed at once
        f.write(cert.public_bytes(serialization.Encoding.PEM)+key.private_bytes(
            serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
        f.flush();context.load_cert_chain(f.name)
    return context


def client_hello_sni(data):
    """SNI from one TLS record, None when it is absent or the record is not a complete ClientHello."""
    try:
        if data[0]!=22 or data[5]!=1:return None
        at=43
        at+=1+data[at]
        at+=2+int.from_bytes(data[at:at+2],'big')
        at+=1+data[at]
        end=at+2+int.from_bytes(data[at:at+2],'big');at+=2
        if end>len(data):return None
        while at+4<=end:
            kind=int.from_bytes(data[at:at+2],'big');size=int.from_bytes(data[at+2:at+4],'big');at+=4
            if kind==0:
                if data[at+2]!=0:return None
                n=int.from_bytes(data[at+3:at+5],'big')
                return data[at+5:at+5+n].decode('ascii').lower().rstrip('.')
            at+=size
    except (IndexError,UnicodeDecodeError):pass
    return None


def parse_request(raw,host):
    """Strict request head check: returns (method, target, headers, length). Anything a downstream parser could read differently is refused."""
    if b'\n' in raw.replace(b'\r\n',b'') or b'\r' in raw.replace(b'\r\n',b''):raise netguard.Denied('request_framing')
    lines=raw.split(b'\r\n')
    parts=lines[0].split(b' ')
    if len(parts)!=3 or parts[2]!=b'HTTP/1.1' or not parts[1].startswith(b'/') or parts[0] in (b'CONNECT',b'TRACE'):
        raise netguard.Denied('request_denied')
    headers=[]
    for line in lines[1:]:
        name,sep,value=line.partition(b':')
        if not sep or not re.fullmatch(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+",name):raise netguard.Denied('request_framing')
        headers.append((name.lower(),value.strip()))
    def only(name):
        found=[v for n,v in headers if n==name]
        if len(found)>1:raise netguard.Denied('request_framing')
        return found[0] if found else None
    authority=only(b'host')
    if authority is None or authority.lower().rstrip(b'.') not in (host.encode(),host.encode()+b':443'):raise netguard.Denied('denied_host_mismatch')
    if only(b'transfer-encoding') is not None or only(b'upgrade') is not None:raise netguard.Denied('denied_non_http')
    length=only(b'content-length') or b'0'
    if not length.isdigit() or int(length)>MAX_BODY:raise netguard.Denied('request_framing')
    return parts[0],parts[1],[(n,v) for n,v in headers if n not in HOP],int(length)


def effective_hosts(paths,*,environ_texts=(),keep=frozenset()):
    """Hostnames only, from every URL in the funded config. Credentials, paths and queries are dropped. A keyed subdomain is
    itself a secret, so this set stays in memory: never logged, never published. `keep` is what the seat must still reach."""
    texts=list(environ_texts)
    for path in paths:
        try:texts.append(open(path,errors='ignore').read())
        except OSError as exc:raise ValueError('funded_config_unreadable') from exc
    hosts=set()
    for text in texts:
        for match in re.finditer(r'\b(?:https?|wss?|grpcs?)://(?:[^\s/@"\']*@)?([A-Za-z0-9.-]+\.[A-Za-z]{2,})',text):
            host=match.group(1).lower().rstrip('.')
            if not on_list(host,keep):hosts.add(host)
    return frozenset(hosts)


def on_list(host,domains):
    host=host.lower().rstrip('.')
    return any(host==d or host.endswith('.'+d) for d in domains)


def parse_connect(head):
    """Returns host, or raises Denied. Only port 443 passes netguard's https rule."""
    try:line=head.split(b'\r\n',1)[0].decode('ascii')
    except UnicodeDecodeError:raise netguard.Denied('request_denied') from None
    parts=line.split(' ')
    if len(parts)!=3 or parts[0]!='CONNECT' or not parts[1].endswith(':443'):raise netguard.Denied('request_denied')
    host=parts[1][:-4].lower().rstrip('.')
    if host.startswith('[') and host.endswith(']'):
        try:host=str(ipaddress.IPv6Address(host[1:-1]))
        except ValueError:raise netguard.Denied('request_denied') from None
    elif len(host)>253 or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',label) for label in host.split('.')):
        raise netguard.Denied('request_denied')
    return host


class Proxy:
    """`allow` set means model-harness mode: only those domains. `deny` always applies first."""
    def __init__(self,path,*,peer_uid,deny,allow=None,ca=None,rate_per_min=120,log=None,clock=time.monotonic,search_broker=None,read_only_hosts=frozenset()):
        if (ca is None)==(allow is None):raise ValueError('egress_mode')
        self.path,self.peer_uid,self.deny,self.allow,self.rate=path,peer_uid,deny,allow,rate_per_min
        self.log,self.clock,self.ca=log,clock,ca
        self.search_broker=search_broker
        self.read_only_hosts=read_only_hosts if ca is not None else frozenset()   # a tunnel cannot see the method
        self.hits=defaultdict(deque);self.lock=threading.Lock();self.slots=threading.BoundedSemaphore(MAX_CONNECTIONS)
        self.sock=None

    def decide(self,host,count=True):
        """None means allowed, else the typed refusal."""
        if on_list(host,self.deny) and not self._read_only(host):return 'denied_funded_lane'
        if self.allow is not None and not on_list(host,self.allow):return 'denied_not_allowed'
        if not count:return None
        if S.provider(host):
            refusal=self.search_broker.admit(host) if self.search_broker else 'search_config_unknown'
            if refusal:return refusal
        now=self.clock()
        with self.lock:
            q=self.hits[host.lower().rstrip('.')]
            while q and now-q[0]>=60:q.popleft()
            if len(q)>=self.rate:return 'denied_rate'
            q.append(now)
        return None

    def _read_only(self,host):
        """An exact exception, unless the funded config itself names that host."""
        host=host.lower().rstrip('.')
        return host in self.read_only_hosts and host not in self.deny

    def _record(self,host,outcome,sent=0,received=0):
        if self.log is None:return
        line=json.dumps(dict(at=time.time(),host=host,outcome=outcome,sent=sent,received=received))+'\n'
        try:
            # strftime on the path gives one file per UTC day, so retention can drop whole days.
            with self.lock,open(time.strftime(self.log,time.gmtime()),'a') as f:f.write(line)
        except OSError:
            import logging
            logging.getLogger(__name__).exception('egress_log_failed path=%s',self.log)

    def _handle(self,client):
        host=None;upstream=None
        try:
            client.settimeout(IDLE_S)
            uid=client.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12)
            if int.from_bytes(uid[4:8],'little')!=self.peer_uid:
                self._record(None,'denied_peer');return
            head=b''
            while b'\r\n\r\n' not in head:
                part=client.recv(1024)
                if not part or len(head)+len(part)>MAX_HEADER:raise netguard.Denied('request_denied')
                head+=part
            host=parse_connect(head)
            if self.ca is not None and (re.fullmatch(r'[0-9.]+',host) or ':' in host):
                raise netguard.Denied('denied_ip_literal')   # no name to bind TLS to
            refusal=self.decide(host,count=self.ca is None)   # with TLS ended here, requests count, not tunnels
            if refusal:raise netguard.Denied(refusal)
            # Resolve once here and dial that address: no second lookup, so no rebinding. The seat does its own TLS.
            target=replace(netguard.resolve('https://%s/'%(('[%s]'%host) if ':' in host else host)),tls=False)
            client.sendall(b'HTTP/1.1 200 OK\r\n\r\n')
            if self.ca is not None:
                client=self._terminate(client,host)
                self._request(client,host,target)
                return
            if client_hello_sni(client.recv(16384,socket.MSG_PEEK))!=host:raise netguard.Denied('denied_sni_mismatch')
            upstream=netguard.connect(target,time.monotonic()+10)
            upstream.settimeout(IDLE_S)
            sent,received=self._pump(client,upstream)
            self._record(host,'ok',sent,received)
        except netguard.Denied as exc:
            self._refuse(client,host,str(exc))
        except (OSError,TimeoutError) as exc:
            self._refuse(client,host,'upstream_'+type(exc).__name__)
        finally:
            for s in (client,upstream):
                if s is not None:s.close()
            self.slots.release()

    def _terminate(self,client,host):
        seen=[]
        context=leaf_context(self.ca,host)
        context.sni_callback=lambda _s,name,_c:seen.append(name)
        tls=context.wrap_socket(client,server_side=True)
        if not seen or seen[0] is None or seen[0].lower().rstrip('.')!=host:
            self._record(host,'denied_sni_mismatch');tls.close()
            raise netguard.Denied('denied_sni_mismatch')
        return tls

    def _request(self,tls,host,target):
        """One request. Client bytes stop being read after the declared body, so nothing can be smuggled past the Host check."""
        raw=b''
        while b'\r\n\r\n' not in raw:
            part=tls.recv(4096)
            if not part or len(raw)+len(part)>MAX_HEADER*4:raise netguard.Denied('request_framing')
            raw+=part
        head,_,body=raw.partition(b'\r\n\r\n')
        method,path,headers,length=parse_request(head,host)
        if len(body)>length:raise netguard.Denied('request_framing')
        if self._read_only(host) and (method not in (b'GET',b'HEAD') or length or body or any(n in (b'authorization',b'cookie') for n,_ in headers)):
            raise netguard.Denied('denied_read_only_host')
        refusal=self.decide(host)
        if refusal:raise netguard.Denied(refusal)
        upstream=netguard.connect(replace(target,tls=True),time.monotonic()+10)
        try:
            upstream.settimeout(IDLE_S)
            lines=[method+b' '+path+b' HTTP/1.1']+[n+b': '+v for n,v in headers]+[b'Connection: close',b'',b'']
            upstream.sendall(b'\r\n'.join(lines))
            upstream.sendall(body);sent=len(body)
            while sent<length:
                part=tls.recv(min(65536,length-sent))
                if not part:raise netguard.Denied('request_framing')
                upstream.sendall(part);sent+=len(part)
            received=0
            while True:
                part=upstream.recv(65536)
                if not part:break
                if received+len(part)>MAX_BODY:raise netguard.Denied('response_too_large')
                tls.sendall(part);received+=len(part)
            self._record(host,'ok',sent,received)
        finally:upstream.close()

    def _refuse(self,client,host,reason):
        self._record(host,reason)
        try:client.sendall(('HTTP/1.1 403 Forbidden\r\nX-Office-Refusal: %s\r\n\r\n'%reason).encode())
        except OSError:pass

    def _pump(self,a,b):
        counts=[0,0]
        def copy(src,dst,i):
            try:
                while True:
                    data=src.recv(65536)
                    if not data:break
                    dst.sendall(data);counts[i]+=len(data)
            except OSError:pass   # an idle timeout or reset ends this tunnel; the byte counts are still logged
            finally:
                try:dst.shutdown(socket.SHUT_WR)
                except OSError:pass
        other=threading.Thread(target=copy,args=(b,a,1),daemon=True);other.start()
        copy(a,b,0);other.join()
        return counts

    def start(self):
        if os.path.exists(self.path):os.unlink(self.path)
        self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        self.sock.bind(self.path);os.chmod(self.path,0o660);self.sock.listen(64)
        threading.Thread(target=self._serve,daemon=True).start()

    def _serve(self):
        while True:
            try:client,_=self.sock.accept()
            except OSError:return
            if not self.slots.acquire(blocking=False):
                client.close();self._record(None,'denied_capacity');continue
            threading.Thread(target=self._handle,args=(client,),daemon=True).start()

    def stop(self):
        if self.sock is not None:self.sock.close()
