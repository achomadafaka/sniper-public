"""Host-side model proxy for one GPT seat. The sandbox holds a fake token; the real one exists only here, and only on chatgpt.com allowed routes.

TLS ends here, the upstream TLS verifies chatgpt.com, and the client's credential headers are always replaced. Every model request is written to
quota_model_requests before it is sent: a request that cannot be recorded is not sent. A unit is one POST with one response stream. Client and CLI
retries are separate POSTs, so each counts. Refused requests, websocket upgrades included, get a row with counted=0. Nothing decrypted is logged."""
import base64
import json
import re
import sqlite3
import time
import uuid

from sniper.office import egress_proxy as P,netguard,quota_measurement as Q

HOST='chatgpt.com'
MODEL_ROUTES=frozenset({'/backend-api/codex/responses','/backend-api/codex/responses/compact'})
OTHER_ROUTES=frozenset({'/backend-api/codex/models','/backend-api/wham/accounts/check','/backend-api/wham/settings/user','/backend-api/wham/usage'})
# Dropped from the request whatever their value; the real pair is then added once.
CREDENTIAL_HEADERS=(b'authorization',b'cookie',b'chatgpt-account-id',b'openai-organization',b'x-api-key',b'expect',b'accept-encoding')
DROPPED_RESPONSE=(b'set-cookie',b'location',b'connection')
UPSTREAM_IDLE_S=300


def read_token(path):
    """(access_token, account_id) from the real auth file, read per request so a CLI refresh applies at once. Raises on anything unusable."""
    tokens=json.loads(open(path,'rb').read())['tokens']
    token,account=tokens['access_token'],tokens['account_id']
    if not re.fullmatch(r'[A-Za-z0-9._-]+',token) or not re.fullmatch(r'[A-Za-z0-9._-]+',account):raise ValueError('token_format')
    return token,account


def claims(token):
    part=token.split('.')[1]
    return json.loads(base64.urlsafe_b64decode(part+'='*(-len(part)%4)))


def dechunk(source):
    """Strict chunked decoding, so the scan reads the body and not its framing. The client gets a close-delimited body. No terminal chunk raises."""
    buffer=b''
    def more():
        nonlocal buffer
        part=next(source,None)
        if part is None:raise netguard.Denied('upstream_framing')
        buffer+=part
    while True:
        while b'\r\n' not in buffer:
            if len(buffer)>1024:raise netguard.Denied('upstream_framing')
            more()
        line,_,buffer=buffer.partition(b'\r\n')
        if not re.fullmatch(rb'[0-9a-fA-F]{1,8}(;[^\r\n]*)?',line):raise netguard.Denied('upstream_framing')
        left=int(line.split(b';')[0],16)
        if not left:   # trailers are refused, and the close must follow the final empty line
            while len(buffer)<2:more()
            if buffer!=b'\r\n':raise netguard.Denied('upstream_framing')
            return
        while left:
            if not buffer:more()
            part,buffer=buffer[:left],buffer[left:];left-=len(part)
            yield part
        while len(buffer)<2:more()
        if buffer[:2]!=b'\r\n':raise netguard.Denied('upstream_framing')
        buffer=buffer[2:]


class ModelProxy(P.Proxy):
    def __init__(self,path,*,peer_uid,deny,ca,actor,account_alias,quota_db,auth_file,rate_per_min=120,log=None,connect=netguard.connect):
        super().__init__(path,peer_uid=peer_uid,deny=deny,ca=ca,rate_per_min=rate_per_min,log=log)
        self.actor,self.account_alias,self.quota_db,self.auth_file,self.connect=actor,account_alias,quota_db,auth_file,connect

    def decide(self,host,count=True):
        return 'denied_not_allowed' if host!=HOST else super().decide(host,count)

    def _row(self,route,counted,outcome):
        """Raises when the write fails: the caller must not forward."""
        request_id=uuid.uuid4().hex
        conn=sqlite3.connect(self.quota_db,timeout=2,isolation_level=None)
        try:Q.record_model_request(conn,request_id=request_id,actor_id=self.actor,account_alias=self.account_alias,at=time.time(),route=route,counted=counted,outcome=outcome)
        finally:conn.close()
        return request_id

    def _finish(self,request_id,outcome):
        """The row is already counted, so a failed status write only loses the status."""
        try:
            conn=sqlite3.connect(self.quota_db,timeout=2,isolation_level=None)
            try:conn.execute('UPDATE quota_model_requests SET outcome=? WHERE request_id=?',(outcome,request_id))
            finally:conn.close()
        except sqlite3.Error:self._record(HOST,'accounting_status_lost')

    def _refused(self,route,reason):
        try:self._row(route,0,reason)
        except (OSError,sqlite3.Error,ValueError):self._record(HOST,'accounting_unavailable')
        raise netguard.Denied(reason)

    def _request(self,tls,host,target):
        if host!=HOST:raise netguard.Denied('denied_not_allowed')   # the swap is bound to this host, whatever the caller checked
        raw=b''
        while b'\r\n\r\n' not in raw:
            part=tls.recv(4096)
            if not part or len(raw)+len(part)>P.MAX_HEADER*4:raise netguard.Denied('request_framing')
            raw+=part
        head,_,body=raw.partition(b'\r\n\r\n')
        try:method,target_path,headers,length=P.parse_request(head,host)
        except netguard.Denied as exc:self._refused('unparsed',str(exc))
        if len(body)>length:self._refused('unparsed','request_framing')
        route=target_path.split(b'?',1)[0].decode('latin1')
        model=method==b'POST' and route in MODEL_ROUTES
        if not model and not (method==b'GET' and route in OTHER_ROUTES):self._refused(route if route in MODEL_ROUTES|OTHER_ROUTES else 'other','denied_route')
        refusal=self.decide(host)
        if refusal:self._refused(route,refusal)
        try:token,account=read_token(self.auth_file)
        except (OSError,ValueError,KeyError,TypeError):self._refused(route,'token_unavailable')
        try:request_id=self._row(route,int(model),'forwarded')
        except (OSError,sqlite3.Error,ValueError):raise netguard.Denied('accounting_unavailable') from None
        self._forward(tls,target,method,target_path,headers,body,length,token,account,request_id)

    def _forward(self,tls,target,method,path,headers,body,length,token,account,request_id):
        kept=[(n,v) for n,v in headers if n not in CREDENTIAL_HEADERS]
        lines=[method+b' '+path+b' HTTP/1.1']+[n+b': '+v for n,v in kept]+[b'Authorization: Bearer '+token.encode(),b'ChatGPT-Account-Id: '+account.encode(),b'Accept-Encoding: identity',b'Connection: close',b'',b'']
        upstream=self.connect(P.replace(target,tls=True),time.monotonic()+10)
        try:
            upstream.settimeout(UPSTREAM_IDLE_S)
            upstream.sendall(b'\r\n'.join(lines));upstream.sendall(body);sent=len(body)
            while sent<length:
                part=tls.recv(min(65536,length-sent))
                if not part:raise netguard.Denied('request_framing')
                upstream.sendall(part);sent+=len(part)
            status,rest,chunked=self._response_head(tls,upstream,token.encode())
            self._finish(request_id,'status_%d'%status)
            self._stream(tls,upstream,rest,token.encode(),chunked)
        except netguard.Denied as exc:
            self._finish(request_id,str(exc));raise
        finally:upstream.close()

    def _response_head(self,tls,upstream,secret):
        raw=b''
        while b'\r\n\r\n' not in raw:
            part=upstream.recv(65536)
            if not part or len(raw)>P.MAX_HEADER*4:raise netguard.Denied('upstream_framing')
            raw+=part
        head,_,rest=raw.partition(b'\r\n\r\n')
        lines=head.split(b'\r\n');parts=lines[0].split(b' ')
        if len(parts)<2 or not parts[1].isdigit():raise netguard.Denied('upstream_framing')
        status=int(parts[1])
        if 300<=status<400:raise netguard.Denied('upstream_redirect_refused')
        if secret in head:raise netguard.Denied('upstream_echoed_credential')
        # A compressed body would hide the token from the scan, so the request asks for identity and anything else is refused.
        if any(n.strip().lower()==b'content-encoding' and v.strip().lower() not in (b'',b'identity') for n,_,v in (l.partition(b':') for l in lines[1:])):raise netguard.Denied('upstream_encoding_refused')
        coding=[l.partition(b':')[2].strip().lower() for l in lines[1:] if l.partition(b':')[0].strip().lower()==b'transfer-encoding']
        length=any(l.partition(b':')[0].strip().lower()==b'content-length' for l in lines[1:])
        if coding and (coding!=[b'chunked'] or length):raise netguard.Denied('upstream_framing')
        kept=[l for l in lines[1:] if l.partition(b':')[0].strip().lower() not in DROPPED_RESPONSE+(b'transfer-encoding',)]
        tls.sendall(b'\r\n'.join([lines[0]]+kept+[b'Connection: close',b'',b'']))
        return status,rest,bool(coding)

    def _stream(self,tls,upstream,rest,secret,chunked=False):
        """Holds back one byte less than the token, so no part of it leaves before the scan can see all of it. Ends with an upstream close."""
        def raw():
            yield rest
            while part:=upstream.recv(65536):yield part
        total,held,keep=0,b'',len(secret)-1
        for part in dechunk(raw()) if chunked else raw():
            total+=len(part)
            if total>P.MAX_BODY:raise netguard.Denied('response_too_large')
            held+=part
            if secret in held:raise netguard.Denied('upstream_echoed_credential')
            if len(held)>keep:
                tls.sendall(held[:-keep] if keep else held);held=held[-keep:] if keep else b''
        if held:tls.sendall(held)
        self._record(HOST,'ok',0,total)
        if hasattr(tls,'unwrap'):   # the body ends at close, so end TLS cleanly: a bare close reads as truncation
            try:tls.settimeout(2);tls.unwrap()
            except OSError:pass
