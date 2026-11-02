"""One wake of one GPT seat, inside the harness sandbox. Holds a fake token only: the host proxy swaps it on allowed routes.

Native tools are on (Alpha's ruling); the walls are the sandbox and the proxies. Reads a JSON request on stdin, prints seat_child's event shape."""
import base64
import json
import os
import subprocess
import sys
import time
import uuid

sys.path.insert(0,'/opt/release')
from sniper.office import seat_child,seat_forward

CLI='/opt/codex'
HOME='/opt/workspace/.codex'
CA='/opt/ca.pem'


def emit(kind,**fields):
    print(json.dumps(dict(kind=kind,**fields)),flush=True)


def fake_token(account_id):
    """Shape only: the CLI reads the account and expiry from it. Unsigned, worthless outside the proxy. The account id is not a secret."""
    def part(doc):return base64.urlsafe_b64encode(json.dumps(doc).encode()).rstrip(b'=').decode()
    return part(dict(alg='none'))+'.'+part({'exp':int(time.time())+30*86400,'sub':'seat','email':'seat@example.invalid','https://api.openai.com/auth':{'chatgpt_account_id':account_id,'chatgpt_plan_type':'pro'}})+'.c2ln'


def write_home(doc,HOME=HOME):
    os.makedirs(HOME,exist_ok=True)
    token=fake_token(doc['account_id'])
    # chatgptAuthTokens with an empty refresh token: the CLI never calls /oauth/token (offline matrix in the auth evidence).
    with open(os.open(HOME+'/auth.json',os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600),'w') as f:
        json.dump(dict(auth_mode='chatgptAuthTokens',OPENAI_API_KEY=None,tokens=dict(id_token=token,access_token=token,account_id=doc['account_id'],refresh_token=''),last_refresh='2026-10-30T00:00:00Z'),f)
    env={k:os.environ[k] for k in ('PYTHONHOME','LD_LIBRARY_PATH') if k in os.environ}
    if 'model' not in doc:return   # the quota read needs only the token
    lines=['model=%s'%json.dumps(doc['model']),'model_reasoning_effort=%s'%json.dumps(doc.get('effort','medium')),'developer_instructions=%s'%json.dumps(doc['system']),
           '[analytics]','enabled=false','[mcp_servers.office]','command=%s'%json.dumps(sys.executable),'args=%s'%json.dumps(['-c',seat_child.BRIDGE]),'tool_timeout_sec=3600','[mcp_servers.office.env]']+['%s=%s'%(k,json.dumps(v)) for k,v in env.items()]
    with open(HOME+'/config.toml','w') as f:f.write('\n'.join(lines)+'\n')


def usage_of(raw):
    """CLI names to the host's. All four are reported by the CLI; none is derived, so a missing one stays unknown."""
    return dict(input_tokens=raw.get('input_tokens'),output_tokens=raw.get('output_tokens'),cache_read_input_tokens=raw.get('cached_input_tokens'),cache_creation_input_tokens=raw.get('cache_write_input_tokens'))


def relay(doc,stream):
    """Prints events as they arrive, so a killed wake can still resume. Returns (session, usage, failed)."""
    session,usage,failed=doc.get('session_id'),None,False
    for raw in stream:
        try:event=json.loads(raw)
        except ValueError:continue
        kind=event.get('type') if isinstance(event,dict) else None
        if kind=='thread.started':
            session=event.get('thread_id');emit('session',session_id=session)
        elif kind=='item.completed' and isinstance(event.get('item'),dict) and event['item'].get('type') in ('agent_message','reasoning') and isinstance(event['item'].get('text'),str):
            emit('thought',text=event['item']['text'][:4000])
        elif kind=='turn.completed' and isinstance(event.get('usage'),dict):usage=usage_of(event['usage'])
        elif kind=='turn.failed':failed=True
    return session,usage,failed


def run(doc):
    write_home(doc)
    version=subprocess.run([CLI,'--version'],capture_output=True,text=True,timeout=30).stdout.split()[-1]
    env=dict(os.environ,**seat_forward.start(),CODEX_HOME=HOME,HOME='/opt/workspace',CODEX_CA_CERTIFICATE=CA,SSL_CERT_FILE=CA)
    head=[CLI,'exec','--json','--skip-git-repo-check','--dangerously-bypass-approvals-and-sandbox']
    argv=head+(['resume',doc['session_id'],'-'] if doc.get('session_id') else ['-'])
    child=subprocess.Popen(argv,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,env=env,cwd='/opt/workspace')
    child.stdin.write(doc['prompt']);child.stdin.close()
    session,usage,failed=relay(doc,child.stdout)
    code=child.wait()
    emit('result',sdk_version=version,session_id=session,result_id=uuid.uuid4().hex,usage=usage,is_error=failed or code!=0)
    return code


def poll(doc):
    """The account's rate limits, on the fake token through the proxy. The CLI's managed-auth refresh cannot reach the real credential from here."""
    home='/opt/workspace/.codex-poll'
    write_home(doc,home)
    from scripts import office_quota_read as R
    env=dict(os.environ,**seat_forward.start(),CODEX_HOME=home,HOME='/opt/workspace',CODEX_CA_CERTIFICATE=CA,SSL_CERT_FILE=CA)
    raw,error=R.poll_codex(popen=lambda argv,**kw:subprocess.Popen([CLI]+argv[1:],env=env,**kw),timeout_s=doc.get('timeout_s',30))
    print(json.dumps(dict(raw=raw,error=error)),flush=True)


if __name__=='__main__':
    if sys.argv[1:]==['--poll']:sys.exit(poll(json.loads(sys.stdin.readline())))
    try:sys.exit(run(json.loads(sys.stdin.readline())))
    except Exception as exc:
        emit('error',reason=type(exc).__name__);sys.exit(4)
