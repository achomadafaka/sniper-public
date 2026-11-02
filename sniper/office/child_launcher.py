"""Trusted sibling-launch mechanics. Requests carry no argv, environment, or mounts."""
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import uuid

MAX_BYTES=1_000_000


def read_regular(path):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size>MAX_BYTES:
            raise ValueError('request_file_invalid')
        with os.fdopen(os.dup(fd),'rb') as f:
            raw=f.read(MAX_BYTES+1)
        if len(raw)>MAX_BYTES:raise ValueError('request_file_too_large')
        return raw
    finally:os.close(fd)


@dataclass(frozen=True)
class Request:
    adapter_id:str
    request_id:str
    source:Path
    deadline_at:float
    payload:bytes
    fingerprint:str


def validate_request(obj,*,root,now):
    if (type(obj) is not dict or set(obj)!={'adapter_id','request_id','request_file','deadline_at'}
            or obj['adapter_id'] not in ('claude_sdk','codex_exec')):
        raise ValueError('request_shape')
    rid=obj['request_id'];deadline=obj['deadline_at']
    try:
        if str(uuid.UUID(rid))!=rid:raise ValueError('request_id')
    except (ValueError,TypeError,AttributeError):raise ValueError('request_id') from None
    if (type(deadline) not in (int,float) or not math.isfinite(deadline)
            or not 0<deadline-now<=120 or obj['request_file']!=rid+'/request.json'):
        raise ValueError('request_deadline_or_path')
    root=Path(root).resolve(strict=True);parent=root/rid;source=parent/'request.json'
    if parent.is_symlink() or source.is_symlink():raise ValueError('request_symlink')
    try:raw=read_regular(source)
    except OSError:raise ValueError('request_unreadable') from None
    # Validate JSON before any spawn; no text is interpreted as shell or launcher authority.
    try:
        value=json.loads(raw)
        if type(value) is not dict:raise ValueError()
        json.dumps(value,allow_nan=False)
    except (ValueError,TypeError,RecursionError):raise ValueError('request_json') from None
    fp=hashlib.sha256(json.dumps(obj,sort_keys=True).encode()+b'\n'+raw).hexdigest()
    return Request(obj['adapter_id'],rid,source,float(deadline),raw,fp)


def validate_peer(pid,uid,*,proc_root=Path('/proc'),expected_cgroup,supervisor_script,expected_uid):
    if type(pid) is not int or pid<=0 or uid!=expected_uid:raise ValueError('peer_identity')
    p=Path(proc_root)/str(pid)
    try:
        groups=p.joinpath('cgroup').read_text().splitlines()
        args=p.joinpath('cmdline').read_bytes().split(b'\0')
        cookie=p.joinpath('stat').read_text().rsplit(')',1)[1].split()[19]
    except (OSError,IndexError):raise ValueError('peer_unreadable') from None
    if '0::'+expected_cgroup not in groups or os.fsencode(supervisor_script) not in args:
        raise ValueError('peer_not_supervisor')
    return cookie


def reserve(state_root,request,*,guard_reason):
    if guard_reason is not None:raise ValueError('guard_paused')
    root=Path(state_root);root.mkdir(parents=True,exist_ok=True)
    path=root/(request.request_id+'.json')
    body={'fingerprint':request.fingerprint,'state':'prepared'}
    try:fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    except FileExistsError:
        if json.loads(read_regular(path)).get('fingerprint')!=request.fingerprint:
            raise ValueError('request_id_conflict')
        return False  # crash-after-spawn stays unknown; never launch a second billed attempt
    with os.fdopen(fd,'w') as f:
        json.dump(body,f);f.flush();os.fsync(f.fileno())
    directory=os.open(root,os.O_DIRECTORY)
    try:os.fsync(directory)
    finally:os.close(directory)
    return True



def extract_io_proof(stdout,request_id):
    try:
        text=stdout.decode('utf-8') if isinstance(stdout,bytes) else stdout
        for line in text.splitlines():
            if not line.startswith('OFFICE_IO_PREEXEC '):continue
            value=json.loads(line[len('OFFICE_IO_PREEXEC '):])
            if (type(value)is not dict or set(value)!={'request_id','source','io_class','before_provider_exec','observed_at_host'}
                    or value['request_id']!=request_id or value['source']!='ioprio_get'
                    or type(value['io_class'])is not int or value['io_class']!=3
                    or value['before_provider_exec'] is not True
                    or type(value['observed_at_host'])not in (int,float)
                    or not math.isfinite(value['observed_at_host'])):
                return None
            return value
    except (ValueError,TypeError,AttributeError):
        return None
    return None

CHILD_GATE = """import json,os,subprocess,sys,time
from pathlib import Path
try:
 resolver=Path('/etc/resolv.conf').resolve(strict=True)
 if not resolver.is_file():raise OSError('resolver_not_regular')
except (OSError,RuntimeError):raise SystemExit('child_resolver_refused')
x=json.loads(Path('/opt/guard/guard.json').read_text())
at=x.get('at')
if not (x.get('state')=='ready' and x.get('reason') is None and x.get('guard_policy_version')=='office-host-guard-pilot-v3' and type(at) in (int,float) and 0<=time.time()-at<=5):
 raise SystemExit('guard_refused')
status=Path('/proc/self/status').read_text()
if 'NoNewPrivs:\t1' not in status or 'CapEff:\t0000000000000000' not in status:
 raise SystemExit('child_sandbox_refused')
try:
 priority=subprocess.run(['/usr/bin/ionice','-p',str(os.getpid())],capture_output=True,text=True,timeout=1,check=True)
except (OSError,subprocess.SubprocessError):raise SystemExit('ioprio_unknown')
if priority.stdout.strip()!='idle':raise SystemExit('ioprio_not_idle')
print('OFFICE_IO_PREEXEC '+json.dumps(dict(request_id=sys.argv[1],source='ioprio_get',io_class=3,
 before_provider_exec=True,observed_at_host=time.time())),flush=True)
os.execv(sys.argv[2],sys.argv[2:])
"""


def child_command(config,request,*,scratch,now):
    descriptor=config['adapters'][request.adapter_id]
    argv=descriptor['argv']
    if (type(argv) is not list or not argv or any(type(a) is not str for a in argv)
            or not 0<request.deadline_at-now<=120):raise ValueError('adapter_descriptor')
    args=['systemd-run','--user','--wait','--pipe','--collect',
          '--unit=office-model-'+request.request_id,'--slice=office.slice',
          '--property=NoNewPrivileges=no','--property=KillMode=control-group',
          '--property=TimeoutStopSec=1','--property=RuntimeMaxSec='+str(request.deadline_at-now),
          '--property=Nice=19','--property=IOSchedulingClass=idle',
          '/usr/bin/bwrap','--unshare-user','--unshare-pid','--die-with-parent','--new-session',
          '--ro-bind','/','/','--proc','/proc','--dev','/dev',
          '--tmpfs','/home','--tmpfs','/root','--tmpfs','/run','--tmpfs','/tmp','--tmpfs','/opt',
          '--ro-bind','/run/systemd/resolve','/run/systemd/resolve']
    for src,dst in descriptor['ro_binds']:
        if (not str(dst).startswith('/opt/') or '..' in Path(dst).parts
                or str(dst).startswith(('/opt/scratch','/opt/guard'))):
            raise ValueError('adapter_mount')
        args+=['--ro-bind',src,dst]
    # Dedicated guard-only directory: atomic guard replacement stays visible without exposing auth siblings.
    args+=['--ro-bind',str(Path(config['guard_file']).parent),'/opt/guard']
    # /run stays masked except the read-only system resolver; user bus and auth remain hidden.
    args+=['--bind',str(scratch),'/opt/scratch','--chdir','/opt/scratch','--cap-drop','ALL','--clearenv',
           '--setenv','HOME','/opt/scratch/home','--setenv','PATH','/opt/python/bin:/usr/bin:/bin',
           '--setenv','PYTHONPATH','/opt/office:/opt/site','--setenv','LD_LIBRARY_PATH','/opt/python/lib']
    if request.adapter_id == 'claude_sdk':
        credential = Path(scratch) / 'home/.claude/.credentials.json'
        args += ['--ro-bind', str(credential), '/opt/scratch/home/.claude/.credentials.json']
    return args+['/usr/bin/python3','-c',CHILD_GATE,request.request_id]+argv
