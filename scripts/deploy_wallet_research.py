#!/usr/bin/env python3
"""Pinned, local-only research deployment with automatic rollback. No DB backups.

Creates only its own user service, research release and research state. The funded
runner/web are not stopped by this helper. Code SHA must be on origin/main.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ENTRY='scripts/research/wallet_execution_worker.py'
UNIT='sniper-wallet-execution-research.service'
MARKER='# Managed by Sniper wallet execution research deploy.'


def validate_request(sha, repo, source):
    if not re.fullmatch('[0-9a-f]{40}',sha): raise ValueError('full lowercase SHA required')
    if not source.is_file(): raise ValueError('source ledger missing')
    subprocess.run(['git','-C',str(repo),'merge-base','--is-ancestor',sha,'origin/main'],check=True,capture_output=True)


def owned_command(argv):
    if not argv or not re.fullmatch(r'python(?:\d+(?:\.\d+)*)?',Path(argv[0]).name): return False
    rest=argv[1:]
    if rest and rest[0]=='-u': rest=rest[1:]
    return bool(rest and '/.runtime/wallet-research/' in rest[0] and rest[0].endswith('/'+ENTRY) and '--loop' in rest)


def ready_artifact(body, pid, sha):
    return (body.get('worker_pid')==pid and body.get('release_sha')==sha
            and body.get('source_status')=='ok'
            and body.get('source_state',{}).get('tape_status')=='sampled_state_not_executable_path')


def run(*args, **kwargs):
    return subprocess.run(list(args),check=True,capture_output=True,text=True,timeout=30,**kwargs)


def quoted(value):
    return '"'+str(value).replace('\\','\\\\').replace('"','\\"').replace('%','%%')+'"'


def deploy(sha, *, repo, state, unit_dir=None):
    source=state/'sniper_tracker.db'; validate_request(sha,repo,source)
    runtime=state/'.runtime/wallet-research'; runtime.mkdir(parents=True,exist_ok=True)
    unit_dir=unit_dir or Path.home()/'.config/systemd/user'; unit_dir.mkdir(parents=True,exist_ok=True)
    unit_path=unit_dir/UNIT
    prior_unit=unit_path.read_text() if unit_path.exists() else None
    if prior_unit is not None and MARKER not in prior_unit: raise ValueError('existing service is not owned by this deployment')
    with (runtime/'deploy.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        release=runtime/'releases'/sha; release.parent.mkdir(parents=True,exist_ok=True)
        current=runtime/'current'; previous=current.resolve() if current.is_symlink() else None
        if not release.exists(): run('git','-C',str(repo),'worktree','add','--detach',str(release),sha)
        if run('git','-C',str(release),'rev-parse','HEAD').stdout.strip()!=sha: raise ValueError('release SHA mismatch')
        if run('git','-C',str(release),'status','--porcelain','--untracked-files=no').stdout.strip(): raise ValueError('dirty pinned release')
        if not (release/ENTRY).is_file(): raise ValueError('release worker absent')
        run(sys.executable,'-c','import sys;sys.path.insert(0,sys.argv[1]);import sniper.services.wallet_execution_research, sniper.research.wallet_execution.study',str(release),cwd=release)
        mainpid=run('systemctl','--user','show',UNIT,'--property=MainPID','--value').stdout.strip()
        if mainpid and mainpid!='0':
            argv=Path('/proc',mainpid,'cmdline').read_bytes().split(b'\0')
            if not owned_command([a.decode() for a in argv if a]): raise ValueError('service PID identity is not research')
        template=MARKER+f'''
[Unit]
Description=Sniper wallet execution research (local archive only)
[Service]
Type=simple
WorkingDirectory={str(state).replace("%", "%%")}
Environment={quoted("SNIPER_STATE_ROOT="+str(state))}
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart={quoted(sys.executable)} -u {quoted(current/ENTRY)} --loop --release-sha {sha}
Restart=on-failure
RestartSec=5
Nice=10
CPUWeight=10
IOWeight=10
MemoryMax=256M
TasksMax=16
NoNewPrivileges=true
RestrictAddressFamilies=AF_UNIX
UMask=0077
[Install]
WantedBy=default.target
'''
        # Validate actual systemd syntax before stopping an existing worker.
        candidate_dir=runtime/'unit-validation';candidate_dir.mkdir(exist_ok=True)
        candidate=candidate_dir/UNIT;candidate.write_text(template)
        try: run('systemd-analyze','--user','verify',str(candidate))
        finally: candidate.unlink(missing_ok=True);candidate_dir.rmdir()
        switched=False; stopped=False
        try:
            if prior_unit is not None:
                stopped=True   # touched BEFORE the stop: a stop that stops it and then fails must still roll back (Codex W9)
                run('systemctl','--user','stop',UNIT)
            nxt=runtime/'current.next'; nxt.unlink(missing_ok=True); nxt.symlink_to(release); os.replace(nxt,current)
            switched=True
            tmp=unit_path.with_suffix('.tmp'); tmp.write_text(template); os.replace(tmp,unit_path)
            run('systemctl','--user','daemon-reload')
            run('systemctl','--user','enable','--now',UNIT)
            artifact=state/'data/wallet_execution_coverage.json'
            deadline=time.monotonic()+25; ready=False
            while time.monotonic()<deadline:
                pid=int(run('systemctl','--user','show',UNIT,'--property=MainPID','--value').stdout.strip() or 0)
                try: body=json.loads(artifact.read_text())
                except (OSError,ValueError): body={}
                if pid and ready_artifact(body,pid,sha): ready=True; break
                time.sleep(.5)
            if not ready: raise ValueError('new research worker did not publish healthy same-PID coverage')
            (runtime/'manifest.json').write_text(json.dumps({'sha':sha,'entry':ENTRY,'state_root':str(state),
                                    'pid':pid,'provider_requests':0,'previous':str(previous) if previous else None}))
            return {'sha':sha,'pid':pid,'source_status':body['source_status'],'cells':len(body.get('cells',[]))}
        except Exception:
            if switched or stopped:
                subprocess.run(['systemctl','--user','stop',UNIT],capture_output=True,timeout=30)
                if switched and previous:
                    nxt=runtime/'current.rollback'; nxt.unlink(missing_ok=True); nxt.symlink_to(previous); os.replace(nxt,current)
                elif switched: current.unlink(missing_ok=True)
                if prior_unit: unit_path.write_text(prior_unit)
                else:
                    enabled=unit_dir/'default.target.wants'/UNIT
                    if enabled.is_symlink() and enabled.resolve()==unit_path.resolve():enabled.unlink()
                    unit_path.unlink(missing_ok=True)
                subprocess.run(['systemctl','--user','daemon-reload'],capture_output=True,timeout=30)
                if prior_unit and mainpid and mainpid!='0':
                    restored=subprocess.run(['systemctl','--user','start',UNIT],capture_output=True,timeout=30)
                    if restored.returncode!=0:   # the old worker is down; say so, never silently
                        print(json.dumps({'rollback':'previous_worker_not_restarted','unit':UNIT,
                                          'stderr':(restored.stderr or '')[-400:]}),file=sys.stderr)
            raise


def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('sha'); p.add_argument('--state-root',type=Path,required=True)
    p.add_argument('--source-root',type=Path,default=Path(__file__).resolve().parents[1]); p.add_argument('--dry-run',action='store_true')
    a=p.parse_args(); validate_request(a.sha,a.source_root,a.state_root/'sniper_tracker.db')
    if a.dry_run: print(json.dumps({'sha':a.sha,'state_root':str(a.state_root),'mutated':False})); return 0
    print(json.dumps(deploy(a.sha,repo=a.source_root,state=a.state_root))); return 0

if __name__=='__main__': raise SystemExit(main())
