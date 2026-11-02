"""Trusted launcher-only composition. No credentials or completion callbacks enter the supervisor."""
import hashlib
import json
import os
from pathlib import Path
import shlex

from sniper.office.child_launcher import read_regular
from sniper.office.host_auth import Auth,approved_path
from sniper.office.launcher_bindings import Completion
from sniper.office import library_audit


def build_auth(config_path,*,expected_sha256,clock,alert):
    """One externally pinned config owns auth, completion and actual unit/child mount identities."""
    raw=read_regular(approved_path(str(config_path)))
    if hashlib.sha256(raw).hexdigest()!=expected_sha256:raise ValueError('launcher_config_unapproved')
    config=json.loads(raw)
    if type(config)is not dict:raise ValueError('launcher_config_shape')
    try:
        if approved_path(config['claude_credentials'])!=Path.home()/'.claude/.credentials.json':
            raise ValueError('launcher_claude_source')
        if config['state_root']!=config['launcher_state']:raise ValueError('launcher_state_conflict')
        control=Path.home()/'Desktop/Sniper/data/office-control'
        for key,name in [('private_state','auth-private'),('child_root','children'),('launcher_state','launcher-state')]:
            if approved_path(config[key])!=control/name:raise ValueError('launcher_private_location')
        if approved_path(config['guard_file'])!=control/'guard/guard.json':raise ValueError('launcher_guard_location')
        if set(config['adapters'])!={'claude_sdk','codex_exec'}:raise ValueError('launcher_adapters')
        model_roots={str(control/'guard')}|{str(approved_path(src))for adapter in config['adapters'].values()for src,_ in adapter['ro_binds']}
        if type(config['model_roots'])is not list or set(config['model_roots'])!=model_roots:
            raise ValueError('launcher_model_roots')
        unit=Path(__file__).resolve().parents[2]/'deploy/office/office-supervisor.service'
        text=read_regular(unit).decode().replace('%h',str(Path.home())).replace('%U',str(os.getuid()))
        commands=[line.split('=',1)[1]for line in text.splitlines()if line.startswith('ExecStart=')]
        if len(commands)!=1:raise ValueError('launcher_supervisor_unit')
        args=shlex.split(commands[0])
        roots={args[i+1]for i,arg in enumerate(args)if arg=='--bind'}
        if type(config['supervisor_roots'])is not list or set(config['supervisor_roots'])!=roots:
            raise ValueError('launcher_supervisor_roots')
        if not any(args[i:i+2]==['--tmpfs',str(control)]for i in range(len(args))):
            raise ValueError('launcher_supervisor_private_mount')
        if approved_path(config['socket_path'])!=control/'launcher-sock/launcher.sock':
            raise ValueError('launcher_socket_location')
        for path in (control/'guard',control/'launcher-sock'):
            if not any(args[i:i+3]==['--ro-bind',str(path),str(path)]for i in range(len(args))):
                raise ValueError('launcher_supervisor_control_mount')
        for at,arg in enumerate(args):
            if arg in ('--bind','--ro-bind') and any(p==str(control) or p.startswith(str(control)+'/') for p in args[at+1:at+3]):
                if args[at:at+3]not in (['--ro-bind',str(control/'guard'),str(control/'guard')],
                                           ['--ro-bind',str(control/'launcher-sock'),str(control/'launcher-sock')],
                                           ['--ro-bind',str(control/'transport.json'),str(control/'transport.json')]):
                    raise ValueError('launcher_supervisor_private_mount')
    except (KeyError,TypeError,IndexError) as exc:raise ValueError('launcher_config_shape') from exc
    site=Path.home()/'.local/lib/python3.13/site-packages'
    for adapter in config['adapters'].values():
        sources=[src for src,dst in adapter['ro_binds']if dst=='/opt/site']
        if sources!=[str(site)]:raise ValueError('launcher_library_mount')
    report=library_audit.audit(site)
    if type(report)is not dict or report.get('state')!='passed' or report.get('root')!=str(site):
        raise ValueError('launcher_library_audit_unknown')
    # Auth rechecks the same raw-byte pin before touching private state; Completion reads only this protected root.
    return Auth(config_path,expected_sha256=expected_sha256,clock=clock,alert=alert,
                completed=Completion(config['launcher_state']))
