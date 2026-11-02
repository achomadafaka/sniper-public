#!/usr/bin/env python3
"""Trusted no-network sibling launcher; only the pinned supervisor may request a child."""
import argparse
from contextlib import closing
import hashlib
import fcntl
import stat
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from sniper.office.child_launcher import MAX_BYTES,child_command,read_regular,reserve,validate_peer,validate_request,extract_io_proof
from sniper.office.clock import corrected_now
from sniper.office.model_auth import AuthRefused
from sniper.office.launcher_client import AUTH_ERRORS
from sniper.office.supervisor import run_child
from sniper.office.launcher_diagnostics import persist_stderr
from office_watchdog import caps_applied,external_guard


def receive(conn):
    data=b''
    deadline=time.monotonic()+2
    while not data.endswith(b'\n'):
        conn.settimeout(max(.001,deadline-time.monotonic()))
        if time.monotonic()>=deadline:raise ValueError('request_read_timeout')
        block=conn.recv(min(65536,MAX_BYTES+1-len(data)))
        if not block:raise ValueError('request_truncated')
        data+=block
        if len(data)>MAX_BYTES:raise ValueError('request_too_large')
    return json.loads(data)


def private_directory(path):
    path=Path(path)
    path.mkdir(parents=True,mode=0o700,exist_ok=True)
    info=path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o700:
        raise ValueError('private_directory')
    return path


def handle(conn,config,*,execute=run_child,auth=None):
    pid,uid,_=struct.unpack('3i',conn.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
    peer=dict(expected_cgroup=config['supervisor_cgroup'],supervisor_script=config['supervisor_script'],
              expected_uid=os.getuid())
    cookie=validate_peer(pid,uid,**peer)
    if not callable(auth):raise ValueError('host_auth_required')
    obj=receive(conn)
    now=corrected_now(config['clock_file'],host_now=time.time())
    if now is None:raise ValueError('clock_unknown')
    request=validate_request(obj,root=config['request_root'],now=now)
    if not caps_applied(config['office_cgroup']):raise ValueError('caps_unapplied')
    guard_reason=external_guard(config['guard_file'])
    if cookie!=validate_peer(pid,uid,**peer):raise ValueError('peer_replaced')
    state=private_directory(config['state_root']);result_path=state/(request.request_id+'.result.json')
    reply_path=state/(request.request_id+'.reply.json')
    if not reserve(state,request,guard_reason=guard_reason):
        if reply_path.exists():return json.loads(read_regular(reply_path))
        return {'request_id':request.request_id,'outcome':'existing_unknown','output_file':None,'exit_code':None}
    scratch=private_directory(config['child_root'])/request.request_id
    scratch.mkdir(mode=0o700,parents=True,exist_ok=False)
    (scratch/'request.json').write_bytes(request.payload)
    try:
        with auth(request.adapter_id,scratch,deadline_at=request.deadline_at):
            now=corrected_now(config['clock_file'],host_now=time.time())
            if now is None or external_guard(config['guard_file']) is not None:raise ValueError('guard_paused')
            argv=child_command(config,request,scratch=scratch,now=now)
            unit='office-model-'+request.request_id+'.service'
            try:
                execution=execute(argv,timeout_s=request.deadline_at-now+3,cwd=str(ROOT),
                                    env={k:v for k,v in os.environ.items() if k in
                                         ('PATH','HOME','XDG_RUNTIME_DIR','DBUS_SESSION_BUS_ADDRESS')},max_output_bytes=MAX_BYTES)
            finally:
                # Killing the systemd-run client does not stop its sibling service. Always reap that exact unit.
                subprocess.run(['systemctl','--user','stop',unit],capture_output=True,timeout=5,check=False)
            try:
                persist_stderr(state,request.request_id,execution.get('stderr',b''),truncated=execution.get('stderr_truncated',False))
            except (OSError,ValueError) as exc:
                print(json.dumps(dict(diagnostic_event='stderr_write_failed',request_id=request.request_id,error_type=type(exc).__name__)),file=sys.stderr,flush=True)
            outcome=execution['outcome']
            io_proof=extract_io_proof(execution.get('stdout',b''),request.request_id)
            if outcome=='success' and io_proof is None:outcome='invalid'
            output=None;model_result=None
            if outcome=='success':
                try:
                    raw=read_regular(scratch/'result.json');model_result=json.loads(raw)
                    if type(model_result) is not dict:raise ValueError('model_result_object')
                    output=request.request_id+'/result.json'
                except (OSError,ValueError):outcome='invalid'
            result={'request_id':request.request_id,'outcome':outcome,'output_file':output,'exit_code':execution.get('returncode'),'io_preexec':io_proof}
            fd=os.open(result_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'w') as f:
                json.dump(result,f);f.flush();os.fsync(f.fileno())
    except AuthRefused as exc:
        reason=exc.reason if exc.reason in AUTH_ERRORS else 'launcher_refused'
        return {'request_id':request.request_id,'outcome':'refused','reason':reason,
                'output_file':None,'exit_code':None}
    # Publish only after auth cleanup/CAS succeeds; a terminal receipt alone never replays a reply.
    result['model_result']=model_result
    fd=os.open(reply_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as f:
        json.dump(result,f,allow_nan=False);f.flush();os.fsync(f.fileno())
    return result


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',type=Path,required=True)
    p.add_argument('--config-sha256-file',type=Path,required=True)
    args=p.parse_args(argv)
    raw=read_regular(args.config)
    if hashlib.sha256(raw).hexdigest()!=args.config_sha256_file.read_text().strip():
        raise SystemExit('launcher_config_unapproved')
    config=json.loads(raw)
    from sniper.office.launcher_runtime import build_auth
    def alert(reason,rid):
        print(json.dumps({'auth_event':reason,'request_id':rid}),file=sys.stderr,flush=True)
    auth=build_auth(args.config,expected_sha256=hashlib.sha256(raw).hexdigest(),
                    clock=lambda:corrected_now(config['clock_file'],host_now=time.time()),alert=alert)
    config=auth.config
    # No adapters may run until T15 delivers reviewed direct entrypoints (no nested bwrap).
    if set(config['adapters'])!={'claude_sdk','codex_exec'}:raise SystemExit('adapters_unapproved')
    path=Path(config['socket_path']);private_directory(path.parent)
    # Single trusted daemon owns socket recovery; never unlink a regular file or another user's socket.
    private_directory(config['state_root'])
    private_directory(config['child_root'])
    lock=Path(config['state_root']).joinpath('launcher.lock').open('a')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    if path.exists():
        info=path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid!=os.getuid():
            raise SystemExit('launcher_socket_not_owned')
        path.unlink()
    with closing(socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)) as server:
        server.bind(str(path));os.chmod(path,0o600);server.listen(4)
        try:
            while True:
                conn,_=server.accept()
                with conn:
                    conn.settimeout(2.)
                    try:result=handle(conn,config,auth=auth)
                    except Exception:
                        result={'outcome':'refused','reason':'launcher_refusal','output_file':None}
                    try:conn.sendall(json.dumps(result,allow_nan=False).encode()+b'\n')
                    except (OSError,ValueError):pass
        finally:path.unlink(missing_ok=True)


if __name__=='__main__':main()
