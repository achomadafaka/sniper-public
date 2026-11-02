#!/usr/bin/env bash
# Preparation only. No start, canary, seed, DB initialization or funded changes.
set -euo pipefail
exec nice -n 19 ionice -c 3 python3 -I -B - "$@" <<'PY'
import argparse
import ast
import base64
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import shlex
import stat
import sqlite3
import subprocess
import sys
import tempfile
import time

UNITS=('office.slice','office-child-launcher.service','office-monitor.service','office-paper-watcher.service','office-supervisor.service','office-watchdog.service','office-supervisor-wake.service','office-supervisor-wake.timer')
# The wake service is static: its timer owns scheduling, never installer start.
ENABLE_UNITS=tuple(u for u in UNITS[1:] if u!='office-supervisor-wake.service')
CONFIGS={'runtime.json':'config/office/runtime.json','runtime.sha256':'config/office/runtime.sha256','transport.json':'data/office-control/transport.json','launcher.json':'data/office-control/launcher.json',
         'launcher.sha256':'data/office-control/launcher.sha256','monitor.json':'data/office-control/monitor.json'}
STATE_DIRS=('data/office','data/office/ledger','data/office/watcher','data/office/launcher',
            'data/office-control','data/office-control/auth-private','data/office-control/children',
            'data/office-control/launcher-state','data/office-control/guard','data/office-control/launcher-sock')
MAX_BYTES=1048576


class Refused(ValueError):pass


def require(condition,reason):
    if not condition:raise Refused(reason)


def digest(raw):return hashlib.sha256(raw).hexdigest()


def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def safe(path):
    path=Path(os.path.abspath(path))
    require(not any(p.is_symlink() for p in (*path.parents,path)),'symlink_path:'+str(path))
    return path


def regular(path):
    safe(path)
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_size<=MAX_BYTES,'nonregular_or_oversized:'+str(path))
        raw=stream.read(MAX_BYTES+1)
    require(len(raw)<=MAX_BYTES,'file_read_budget')
    return raw


def run(argv,*,check=True,text=True):
    process=subprocess.Popen(argv,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=text,start_new_session=True)
    try:out,err=process.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid,signal.SIGKILL);process.communicate();raise
    result=subprocess.CompletedProcess(argv,process.returncode,out,err)
    if check:require(result.returncode==0,'command_failed:'+Path(argv[0]).name+':'+(argv[2] if len(argv)>2 else ''))
    return result


def git(repo,*args,check=True):return run(['git','-c','core.hooksPath=/dev/null','-C',str(repo),*args],check=check)


def blob(repo,sha,name):
    spec=sha+':'+name
    require(int(git(repo,'cat-file','-s',spec).stdout)<=MAX_BYTES,'git_blob_budget')
    result=run(['git','-C',str(repo),'show',spec],text=False)
    require(result.returncode==0 and len(result.stdout)<=MAX_BYTES,'git_blob_read_failed')
    return result.stdout


def ctl(action,*units,check=True):return run(['systemctl','--user',action,*units],check=check)


def enabled(unit):
    result=ctl('is-enabled',unit,check=False)
    state=result.stdout.strip()
    require(state in ('enabled','disabled','not-found'),'unit_enable_state_unknown:'+unit)
    return state=='enabled'


def inactive(keep=()):
    for unit in UNITS[1:]:
        if unit in keep:continue      # same-epoch repin: byte-identical monitor/watchdog keep running (Alpha 2026-10-29)
        result=ctl('is-active',unit,check=False)
        require(result.stdout.strip() in ('inactive','failed','unknown'),'active_or_unknown_unit:'+unit)


KEEPABLE={'office-monitor.service':('scripts/office_host_monitor.py',),
          'office-watchdog.service':('scripts/office_watchdog.py','config/office/host_guard_pilot.json')}


def _exists(repo,sha,name):
    return git(repo,'cat-file','-e',sha+':'+name,check=False).returncode==0


def _closure(repo,sha,roots):
    """Repo files a long-running unit loads: its entry scripts and their imports of repo modules (scripts/<m>.py bare,
    scripts.<m>, sniper.<pkg...>), transitively. Third-party and stdlib come from the unchanged interpreter."""
    seen,todo=set(),list(roots)
    while todo:
        name=todo.pop()
        if name in seen:continue
        seen.add(name)
        if not name.endswith('.py') or not _exists(repo,sha,name):continue   # a missing root just keeps nothing below
        for node in ast.walk(ast.parse(blob(repo,sha,name))):
            if (isinstance(node,ast.Name) and node.id=='__import__') or (isinstance(node,ast.alias) and node.name.split('.')[0]=='importlib') or (isinstance(node,ast.ImportFrom) and (node.module or '').split('.')[0]=='importlib'):
                seen.add('<dynamic-import>')     # unresolvable statically: the unit is never kept (fail closed)
            mods=[a.name for a in node.names] if isinstance(node,ast.Import) else ([node.module]+[node.module+'.'+a.name for a in node.names]) if isinstance(node,ast.ImportFrom) and node.module and not node.level else []   # from pkg import submodule
            for mod in mods:
                parts=mod.split('.')
                options=(['scripts/'+parts[0]+'.py'] if len(parts)==1 else [])+(['/'.join(parts)+'.py','/'.join(parts)+'/__init__.py'] if parts[0] in ('sniper','scripts') else [])
                for i in range(1,len(parts)):
                    if parts[0]=='sniper':options.append('/'.join(parts[:i])+'/__init__.py')
                todo+= [o for o in options if _exists(repo,sha,o)]
    return seen


def keep_units(repo,old_sha,new_sha):
    """Units that may stay running through a same-epoch repin: unit file and every loaded repo file byte-identical."""
    keep=set()
    for unit,roots in KEEPABLE.items():
        names=_closure(repo,old_sha,roots)|_closure(repo,new_sha,roots)|{'deploy/office/'+unit}
        if '<dynamic-import>' not in names and all(_exists(repo,old_sha,n) and _exists(repo,new_sha,n) and blob(repo,old_sha,n)==blob(repo,new_sha,n) for n in sorted(names)):
            keep.add(unit)
    return frozenset(keep)


def manager_identity(unit,path,dropins,*,loaded=False):
    result=ctl('show',unit,'--property=FragmentPath,DropInPaths,MainPID,ControlPID',check=False)
    values=dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
    required={'FragmentPath','DropInPaths'}|({'MainPID','ControlPID'} if unit.endswith('.service') else set())
    require(required<=set(values)<= {'FragmentPath','DropInPaths','MainPID','ControlPID'},'unit_manager_identity_unknown:'+unit)
    expected={str(p) for p in dropins} if unit=='office.slice' else set()
    require(set(values['DropInPaths'].split())==expected,'unreviewed_unit_dropins:'+unit)
    for item,pin in dropins.items():
        info=item.stat();require(info.st_uid==os.getuid() and stat.S_IMODE(info.st_mode)==0o644 and digest(regular(item))==pin,'slice_dropin_mismatch')
    require(values['FragmentPath'] in (str(path),''),'foreign_unit_fragment:'+unit)
    if loaded:require(values['FragmentPath']==str(path),'unit_not_loaded:'+unit)
    return values


def file_state(path,expected,mode):
    safe(path)
    if not path.exists():return False
    info=path.stat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid() and stat.S_IMODE(info.st_mode)==mode
            and digest(regular(path))==digest(expected),'preexisting_file_mismatch:'+str(path))
    return True


def atomic(path,raw,mode):
    fd,name=tempfile.mkstemp(prefix='.office-install-',dir=path.parent)
    try:
        os.fchmod(fd,mode)
        with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
        os.replace(name,path)
        fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:
        if os.path.lexists(name):os.unlink(name)


def save(path,value):
    value=dict(value);value.pop('journal_sha256',None)
    value['journal_sha256']=digest(canonical(value))
    atomic(path,canonical(value),0o600)
    return value


def journal_load(path):
    info=path.stat();require(info.st_uid==os.getuid() and stat.S_IMODE(info.st_mode)==0o600,'journal_mode_or_owner')
    value=json.loads(regular(path));checksum=value.pop('journal_sha256',None)
    require(checksum==digest(canonical(value)),'journal_tampered')
    return value


def no_null(value):
    if value is None:return False
    if isinstance(value,dict):return all(no_null(v) for v in value.values())
    if isinstance(value,list):return all(no_null(v) for v in value)
    return True


def release_valid(repo,release,sha):
    safe(release)
    if not release.exists():return False
    require(git(release,'rev-parse','HEAD').stdout.strip()==sha,'release_head_mismatch')
    require(not git(release,'status','--porcelain','--untracked-files=all').stdout,'release_dirty')
    require(Path(git(release,'rev-parse','--show-toplevel').stdout.strip())==release,'release_root_mismatch')
    return True


def supervisor_alias(repo,release,*,installed=False,check_current=True):
    current=repo/'.runtime/office/current'
    safe(current.parent);safe(release/'scripts/office_supervisor.py')
    if not check_current:return str(current/'scripts/office_supervisor.py')
    if os.path.lexists(current):
        require(current.is_symlink() and os.readlink(current)==str(release) and current.resolve()==release,'current_release_mismatch')
    else:require(not installed,'current_release_missing')
    return str(current/'scripts/office_supervisor.py')


def runtime_contract(repo,release,sha,runtime,source,launcher,*,repin=False):
    fields={'version','enabled','discovery_only','evaluated_epoch_allowed','research_enabled','content_addressed_release_root',
        'state_root','config_root','store_path','ledger_path','clock_file','guard_file','office_cgroup','supervisor_identity',
        'transport','cadence','model_manifest','pilot_seed_launcher_config','provider_caps','host_children_max',
        'turn_deadline_s','max_steps','lease_ttl_s','poll_s','desk_db'}
    require(set(runtime)-{'claude_sdk','claude_debug_stderr'}==fields and runtime['version']=='office-runtime-pilot-v1' and runtime['enabled'] is True,'runtime_schema')
    if 'claude_debug_stderr' in runtime:require(type(runtime['claude_debug_stderr'])is bool,'runtime_debug_flag')
    # discovery pilot, or the office-paper-eval-1 paper runtime (both flags flipped; bound to its 5-slot prompt profile below)
    evaluated=(runtime['discovery_only'],runtime['evaluated_epoch_allowed'])==(False,True)
    require((evaluated or (runtime['discovery_only'] is True and runtime['evaluated_epoch_allowed'] is False)) and runtime['research_enabled'] is False,'runtime_pilot_contract')
    for key in ('turn_deadline_s','lease_ttl_s','poll_s'):
        value=runtime[key];require(type(value) in (int,float) and math.isfinite(value) and value>0,'runtime_limit:'+key)
    require(runtime['turn_deadline_s']<=120 and runtime['poll_s']<=60 and runtime['lease_ttl_s']>runtime['turn_deadline_s']+runtime['poll_s'],'runtime_timing_bounds')
    for key,maximum in (('max_steps',16),('host_children_max',4)):
        require(type(runtime[key]) is int and 1<=runtime[key]<=maximum,'runtime_limit:'+key)
    caps=runtime['provider_caps']
    require(type(caps) is dict and set(caps)=={'claude','codex'} and all(type(v) is int and 1<=v<=4 for v in caps.values()),'runtime_provider_caps')
    expected={'content_addressed_release_root':release,'state_root':repo,'config_root':release/'config/office',
        'store_path':repo/'data/office/office.db','ledger_path':repo/'data/office/ledger/ledger.db',
        'clock_file':repo/'data/clock_offset.json','guard_file':repo/'data/office-control/guard/guard.json','desk_db':repo/'data/memecoin_desk.db'}
    for key,path in expected.items():require(runtime[key]==str(path),'runtime_path:'+key);safe(path)
    result=ctl('show','--property=ControlGroup')
    require(result.stdout.startswith('ControlGroup=/') and len(result.stdout.splitlines())==1,'user_manager_cgroup_unknown')
    group=result.stdout.strip().split('=',1)[1]+'/office.slice'
    require(runtime['office_cgroup']=='/sys/fs/cgroup'+group,'runtime_office_cgroup')
    script=supervisor_alias(repo,release,check_current=not repin)
    require(runtime['supervisor_identity']==dict(crew_uid=os.getuid(),expected_cgroup=group+'/office-supervisor.service',supervisor_script=script),'runtime_supervisor_identity')
    require(launcher.get('supervisor_script')==script and launcher.get('supervisor_cgroup')==group+'/office-supervisor.service' and launcher.get('office_cgroup')==runtime['office_cgroup'],'launcher_runtime_identity_mismatch')
    unit=blob(repo,sha,'deploy/office/office-supervisor.service').decode().replace('%h/Desktop/Sniper',str(repo)).replace('%h',str(Path.home()))
    commands=[line.split('=',1)[1] for line in unit.splitlines() if line.startswith('ExecStart=')]
    require(len(commands)==1 and shlex.split(commands[0]).count(script)==1,'supervisor_unit_argv_mismatch')
    blob(repo,sha,'scripts/office_supervisor.py')
    config=release/'config/office'
    def reference(key,path,raw):
        require(runtime[key]==dict(path=str(path),sha256=digest(raw)),'runtime_reference:'+key)
    reference('transport',repo/'data/office-control/transport.json',source['transport.json'])
    transport=json.loads(source['transport.json'])
    require(transport.get('crew_uid')==os.getuid() and transport.get('request_root')==str(repo/'data/office/launcher/requests') and transport.get('socket_path')==str(repo/'data/office-control/launcher-sock/launcher.sock'),'runtime_transport_bindings')
    models_name='pilot_models_eval1.json' if evaluated else 'pilot_models.json'   # eval-1 has its own model manifest file
    for key,name in (('cadence','cadence.json'),('model_manifest',models_name)):
        reference(key,config/name,blob(repo,sha,'config/office/'+name))
    names=('pilot_org.json','limits_pilot.json',models_name,'book_policy_pilot.json')
    pins={name:digest(blob(repo,sha,'config/office/'+name)) for name in names}
    matched=False
    for profile,slots in (('pilot',('head','scout','judge_claude','judge_gpt')),('discovery2',('head','scout','judge_claude','judge_gpt')),
                          ('office-paper-eval-1',('head','scout','judge_claude','judge_gpt','postmortem'))):
        if evaluated!=(profile=='office-paper-eval-1'):continue
        prefix='config/office/prompts/'+profile+'/'
        if git(repo,'cat-file','-e',sha+':'+prefix+'sha256.json',check=False).returncode:continue
        approvals=json.loads(blob(repo,sha,prefix+'sha256.json'))
        prompts={slot:dict(text=blob(repo,sha,prefix+slot+'.md').decode(),sha256=approvals['files'][slot+'.md']) for slot in slots}
        if (approvals.get('approval_status')=='approved_alpha' and approvals.get('source_pins')==pins
                and all(digest(p['text'].encode())==p['sha256'] for p in prompts.values())
                and runtime['pilot_seed_launcher_config']==dict(schema='office-pilot-seed-launcher-v1',files=pins,prompts=prompts)):
            matched=True;break
    require(matched,'runtime_seed_bindings')


def sdk_plan(repo,sha,release,bundle,manifest,runtime,*,check_existing=True):
    name='sniper/office/claude_compat.py'
    exists=git(repo,'cat-file','-e',sha+':'+name,check=False).returncode==0
    if not exists:
        require('claude_sdk' not in manifest and 'claude_sdk' not in runtime,'sdk_source_missing')
        return None
    # The helper is source from the exact approved main SHA, and imports stdlib only.
    module={'__file__':str(release/name),'__name__':'office_pinned_sdk_artifact'}
    exec(compile(blob(repo,sha,name),str(release/name),'exec'),module)
    require(manifest.get('claude_sdk')==module['artifact_manifest'](),'sdk_manifest_mismatch')
    require(runtime.get('claude_sdk')==module['runtime_pins'](release),'sdk_runtime_pin_mismatch')
    root=safe(release/module['VENDOR_REL']);wheel=safe(bundle/module['WHEEL_FILE'])
    require(module['wheel_refusal'](wheel) is None,'sdk_wheel_mismatch')
    if check_existing and os.path.lexists(root):require(module['verify_vendor'](root) is None,'sdk_existing_tree_mismatch')
    return dict(module=module,root=root,wheel=wheel)


def plan(args,*,repin=False,keep=frozenset()):
    require(re.fullmatch('[0-9a-f]{40}',args.sha) is not None,'exact_main_sha_required')
    require(re.fullmatch('[0-9a-f]{64}',args.bundle_sha256) is not None,'external_bundle_sha_required')
    repo=safe(args.repo);bundle=safe(args.bundle)
    require(repo.stat().st_uid==os.getuid(),'run_as_repository_owner')
    require(Path(git(repo,'rev-parse','--show-toplevel').stdout.strip())==repo,'repository_root_mismatch')
    require(git(repo,'rev-parse',args.sha+'^{commit}').stdout.strip()==args.sha,'commit_identity_mismatch')
    require(git(repo,'merge-base','--is-ancestor',args.sha,'main',check=False).returncode==0,'release_not_on_main')
    raw=regular(bundle/'manifest.json');require(digest(raw)==args.bundle_sha256,'bundle_hash_mismatch')
    manifest=json.loads(raw)
    release=repo/'.runtime/office/releases'/args.sha
    required={'schema','approved_by','main_sha','repo_root','release_root','guard_manifest_sha256','files'}
    require(required<=set(manifest)<=required|{'office_slice_dropin_sha256','claude_sdk'},'bundle_schema')
    require(manifest['schema']=='office-pilot-activation-v1' and manifest['approved_by']=='alpha'
            and manifest['main_sha']==args.sha and manifest['repo_root']==str(repo) and manifest['release_root']==str(release),'bundle_identity_mismatch')
    require(set(manifest['files'])==set(CONFIGS),'bundle_config_set')
    source={}
    for name in CONFIGS:
        raw=regular(bundle/name);require(digest(raw)==manifest['files'][name],'config_hash_mismatch:'+name);source[name]=raw
    require(source['runtime.sha256'].decode().strip()==digest(source['runtime.json']),'runtime_external_pin_mismatch')
    require(source['launcher.sha256'].decode().strip()==digest(source['launcher.json']),'launcher_external_pin_mismatch')
    docs={name:json.loads(raw) for name,raw in source.items() if name.endswith('.json')}
    for name,value in docs.items():
        require(isinstance(value,dict) and no_null(value) and 'draft' not in str(value.get('version','')).lower(),'incomplete_draft:'+name)
    runtime=docs['runtime.json'];monitor=docs['monitor.json'];launcher=docs['launcher.json']
    runtime_contract(repo,release,args.sha,runtime,source,launcher,repin=repin)
    sdk=sdk_plan(repo,args.sha,release,bundle,manifest,runtime,check_existing=not (repin and args.rollback))
    require((runtime.get('discovery_only'),runtime.get('evaluated_epoch_allowed')) in ((True,False),(False,True))
            and runtime.get('research_enabled') is False and runtime.get('content_addressed_release_root')==str(release),'runtime_pilot_contract')
    require(monitor.get('research_enabled') is False and monitor.get('sample_out')==str(repo/'data/office-control/health.json'),'monitor_contract')
    require(isinstance(launcher.get('model_roots'),list) and str(release) in launcher['model_roots'] and launcher.get('guard_file')==str(repo/'data/office-control/guard/guard.json'),'launcher_release_or_guard_mismatch')
    for value in launcher['model_roots']:
        require(isinstance(value,str) and Path(value).is_absolute(),'model_root_identity')
        require(repo/'.runtime/office/current' not in (Path(value),*Path(value).parents),'model_root_uses_current_alias')
        safe(value)
    for adapter in launcher.get('adapters',{}).values():
        for mount_source,target in adapter.get('ro_binds',[]):
            if target=='/opt/office':require(mount_source==str(release),'adapter_release_root_mismatch')
    policy=blob(repo,args.sha,'config/office/host_guard_pilot.json')
    require(digest(policy)==manifest['guard_manifest_sha256'],'guard_manifest_hash_mismatch')
    guard=json.loads(policy)
    require(guard.get('mode')=='pilot' and guard.get('discovery_only') is True and guard.get('evaluation_allowed') is False,'guard_not_discovery_pilot')
    git(repo,'cat-file','-e',args.sha+':sniper/office/runtime.py')
    git(repo,'cat-file','-e',args.sha+':scripts/office_supervisor_wake.py')
    files={repo/target:(source[name],0o600) for name,target in CONFIGS.items()}
    unitdir=safe(Path(os.environ.get('XDG_CONFIG_HOME',str(Path.home()/'.config')))/'systemd/user')
    for unit in UNITS:
        raw=blob(repo,args.sha,'deploy/office/'+unit).decode('utf-8')
        raw=raw.replace('%h/Desktop/Sniper',str(repo)).replace('%h',str(Path.home())).replace('%U',str(os.getuid()))
        files[unitdir/unit]=(raw.encode(),0o644)
    directories={repo/name:0o700 for name in STATE_DIRS}
    for path in directories:
        safe(path)
        if path.exists():
            info=path.stat();require(stat.S_ISDIR(info.st_mode) and info.st_uid==os.getuid() and stat.S_IMODE(info.st_mode)==0o700,'state_directory_mismatch:'+str(path))
    require(not any(c.isspace() for c in str(repo)+str(unitdir)),'whitespace_install_paths_unsupported')
    if not repin:
        for path,(raw,mode) in files.items():file_state(path,raw,mode)
    dropins={}
    pin=manifest.get('office_slice_dropin_sha256')
    require(pin=='3db4896d7cb9fbd6e74c5f3b342d0c8209626f66f74b227f7af8caa34542444e','slice_dropin_pin_invalid')
    dropins[unitdir/'office.slice.d/10-office-pilot.conf']=pin
    for unit in UNITS:
        identity=manager_identity(unit,unitdir/unit,dropins)
        if repin and unit.endswith('.service') and unit not in keep:
            require(identity['MainPID']=='0' and identity['ControlPID']=='0','repin_unit_has_process:'+unit)
    current=repo/'.runtime/office/current'
    if not repin and os.path.lexists(current):require(current.is_symlink() and os.readlink(current)==str(release),'current_release_mismatch')
    if not (repin and args.rollback):release_valid(repo,release,args.sha)
    with tempfile.TemporaryDirectory(prefix='office-unit-verify-') as folder:
        paths=[]
        for unit in UNITS:
            path=Path(folder)/unit;path.write_bytes(files[unitdir/unit][0]);paths.append(str(path))
        run(['systemd-analyze','--user','verify',*paths])
    return repo,release,files,directories,current,dropins,sdk


def verify(repo,release,files,current,dropins,sdk=None):
    if sdk:require(sdk['module']['verify_vendor'](sdk['root']) is None,'sdk_installed_tree_mismatch')
    supervisor_alias(repo,release,installed=True)
    for path in files:
        if path.name in UNITS:manager_identity(path.name,path,dropins,loaded=True)
    require(all(enabled(unit) for unit in ENABLE_UNITS),'unit_enable_readback_mismatch')
    for path,(raw,mode) in files.items():require(file_state(path,raw,mode),'installed_file_missing:'+str(path))
    require(current.is_symlink() and os.readlink(current)==str(release),'current_release_mismatch')
    run(['systemd-analyze','--user','verify',*[str(path) for path in files if path.name in UNITS]])
    pending=[]
    runtime=json.loads(regular(repo/'config/office/runtime.json'))
    if not Path(runtime['office_cgroup']).is_dir():pending.append('office_cgroup:missing')
    caps=run([sys.executable,'-I','-B',str(release/'scripts/office_pilot_caps.py'),'--manifest',str(release/'config/office/host_guard_pilot.json'),'--check'],check=False)
    try:report=json.loads(caps.stdout)
    except (ValueError,TypeError):report={}
    if caps.returncode or report.get('ready_for_child_preexec') is not True:pending.append('caps:base_caps_not_verified')
    for name,path,clock in (('health',repo/'data/office-control/health.json','ts'),('guard',repo/'data/office-control/guard/guard.json','at')):
        try:
            value=json.loads(regular(path));at=value.get(clock)
            if type(at) not in (int,float) or not 0<=time.time()-at<=5:pending.append(name+':stale_or_unknown')
            elif name=='guard' and (value.get('state')!='ready' or value.get('reason') is not None):pending.append('guard:not_ready')
            elif name=='health' and (value.get('telemetry_error') is True or any(value.get(k) is not True for k in ('base_caps_applied','roster_valid','resources_verified'))):pending.append('health:not_verified')
        except (OSError,ValueError,TypeError):pending.append(name+':missing_or_unreadable')
    if not any(p.startswith('health:') or p.startswith('guard:') for p in pending):
        code='import sys,json,time;sys.path.insert(0,sys.argv[1]);from office_watchdog import PilotGuard,external_guard;h=json.load(open(sys.argv[2]));g=json.load(open(sys.argv[3]));v=json.load(open(sys.argv[4]))["version"];print(json.dumps({"health": "policy_mismatch" if h.get("guard_policy_version")!=v else PilotGuard().check(h,now=time.time()), "guard":"policy_mismatch" if g.get("guard_policy_version")!=v else external_guard(sys.argv[3])}))'
        result=run([sys.executable,'-I','-B','-c',code,str(release/'scripts'),str(repo/'data/office-control/health.json'),str(repo/'data/office-control/guard/guard.json'),str(release/'config/office/host_guard_pilot.json')],check=False)
        try:
            reasons=json.loads(result.stdout);require(result.returncode==0,'health_policy_unknown')
            for name in ('health','guard'):
                if reasons[name] is not None:pending.append(name+':'+str(reasons[name]))
        except (ValueError,KeyError,TypeError):pending.append('health:policy_check_unknown')
    return pending


def ensure_dir(path,mode=0o755):
    safe(path)
    if not path.exists():path.mkdir(parents=True,mode=mode)
    require(path.is_dir(),'directory_required:'+str(path))


def validate_journal(repo,release,files,directories,journal,sdk=None):
    require(set(journal)-{'vendor_existed'}=={'phase','repo','release','sha','bundle_sha256','files','directories','enabled','release_existed','current_existed'},'journal_schema')
    if sdk:require(type(journal.get('vendor_existed'))is bool,'journal_vendor_prior_state_unknown')
    elif 'vendor_existed' in journal:require(type(journal['vendor_existed'])is bool,'journal_vendor_prior_state_unknown')
    require(journal['phase'] in ('applying','prepared','rolled_back'),'journal_phase')
    require(all(isinstance(journal[k],dict) for k in ('files','directories','enabled')),'journal_collections')
    require(all(isinstance(v,dict) and set(v)=={'sha256','mode','existed'} for v in journal['files'].values()),'journal_file_schema')
    require(journal['repo']==str(repo) and journal['release']==str(release),'journal_identity')
    require(set(journal['enabled'])==set(ENABLE_UNITS) and all(type(v)is bool for v in journal['enabled'].values()),'journal_unit_set_mismatch')
    require(set(journal['directories'])=={str(p) for p in directories} and all(type(v)is bool for v in journal['directories'].values()),'journal_directory_set_mismatch')
    require(type(journal['release_existed']) is bool and type(journal['current_existed']) is bool,'journal_prior_state_invalid')
    require(all(type(v['existed']) is bool for v in journal['files'].values()),'journal_prior_file_state_invalid')
    expected={str(p):dict(sha256=digest(raw),mode=mode) for p,(raw,mode) in files.items()}
    require({p:{k:v[k] for k in ('sha256','mode')} for p,v in journal['files'].items()}==expected,'journal_file_set_mismatch')


def rollback(repo,release,files,directories,current,journal_path,journal,dropins,sdk=None,*,dry_run=False):
    inactive()
    for path in files:
        if path.name in UNITS:manager_identity(path.name,path,dropins)
    validate_journal(repo,release,files,directories,journal,sdk)
    for path,(raw,mode) in files.items():
        present=file_state(path,raw,mode)
        if journal['files'][str(path)]['existed']:require(present,'prior_file_missing')
    for path in directories:
        if journal['directories'][str(path)]:require(path.is_dir(),'prior_directory_missing')
        if path.exists():require(stat.S_IMODE(path.stat().st_mode)==0o700,'state_directory_mismatch')
    if os.path.lexists(current):require(current.is_symlink() and os.readlink(current)==str(release),'current_tampered')
    if journal['current_existed']:require(current.is_symlink(),'prior_current_missing')
    release_exists=release_valid(repo,release,journal['sha'])
    if journal['release_existed']:require(release_exists,'prior_release_missing')
    if journal['phase']=='rolled_back':
        require(not any(p.exists() for p in files if not journal['files'][str(p)]['existed']),'rollback_state_changed')
        return
    if dry_run:return
    for unit,prior in journal['enabled'].items():
        if not prior and enabled(unit):ctl('disable',unit)
    for path in files:
        if not journal['files'][str(path)]['existed'] and path.exists():path.unlink()
    if not journal['current_existed'] and current.is_symlink():current.unlink()
    if sdk and not journal['vendor_existed']:
        sdk['module']['remove_vendor'](sdk['root'])
    if not journal['release_existed'] and release_exists:git(repo,'worktree','remove',str(release))
    for path in sorted(directories,key=lambda p:len(p.parts),reverse=True):
        if not journal['directories'][str(path)] and path.exists():
            try:path.rmdir()
            except OSError:pass  # Existing data/DBs are never installer-owned and are retained.
    ctl('daemon-reload')
    journal['phase']='rolled_back';save(journal_path,journal)


def repin_tool(repo,sha):
    tree=ast.parse(blob(repo,sha,'sniper/office/runtime_steps.py'))
    names={'_REFS','_REQUEST_ARGS','SCHEMA'}
    nodes=[n for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id in names for t in n.targets)]
    require(len(nodes)==3,'repin_tool_schema_unknown')
    namespace={}
    exec(compile(ast.Module(body=nodes,type_ignores=[]),'<pinned-tool-contract>','exec'),namespace)
    return digest(json.dumps(namespace['SCHEMA'],sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode())


def runtime_is_eval1(runtime):return (runtime.get('discovery_only'),runtime.get('evaluated_epoch_allowed'))==(False,True)


def repin_state(repo,runtime,tool):
    """Read-only admission witness, not database initialization or a backup."""
    if runtime_is_eval1(runtime):return repin_state_eval1(repo,runtime,tool)
    sample=json.loads(regular(Path(runtime['clock_file'])));host=time.time()
    offset,at=sample['offset_s'],sample['at']
    require(all(type(v) in (int,float) and math.isfinite(v) for v in (offset,at))
            and 0<=host-at<=3600 and abs(offset)<=172800,'repin_clock_unknown')
    now=host-offset
    def connect(name):
        path=safe(Path(runtime[name]));conn=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=1.)
        conn.row_factory=sqlite3.Row;deadline=time.monotonic()+1
        conn.set_progress_handler(lambda:int(time.monotonic()>deadline),1000)
        conn.execute('BEGIN');return conn
    conn=ledger=None
    try:
        conn=connect('store_path');ledger=connect('ledger_path')
        require(conn.execute('SELECT purpose FROM _store_info WHERE id=1').fetchone()[0]=='office','repin_store_unknown')
        lease=conn.execute('SELECT expires_at FROM supervisor_lease WHERE id=1').fetchone()
        require(lease is not None and type(lease[0]) in (int,float) and math.isfinite(lease[0]) and lease[0]<=now,'repin_lease_live_or_unknown')
        control=json.loads(conn.execute('SELECT payload_json FROM office_epoch_control WHERE id=1').fetchone()[0])
        require(control['active_epoch']=='office-pilot-discovery-2','repin_epoch_unknown')
        receipt=conn.execute('SELECT payload_json,prev_hash FROM org_changes WHERE row_hash=?',(control['receipt_id'],)).fetchone()
        event=json.loads(receipt[0])
        def frozen_bytes(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()
        require(digest(frozen_bytes(event)+receipt[1].encode())==control['receipt_id'],'repin_transition_corrupt')
        actors={r[0]:json.loads(r[1]) for r in conn.execute('SELECT id,payload_json FROM agents LIMIT 5')}
        require(len(actors)==4 and actors==event['new_actors'] and all(a.get('generation')==2 and a.get('state')=='active' for a in actors.values()),'repin_roster_changed')
        epochs={r[0]:json.loads(r[1]) for r in conn.execute('SELECT id,payload_json FROM epochs LIMIT 3')}
        require(len(epochs)==2 and epochs[control['active_epoch']]==event['new_epoch'],'repin_epoch_changed')
        require(event['new_epoch']['record']['tool_contract_hash']['new']==tool,'repin_tool_not_adopted')
        for a in actors.values():
            slot={'Head':'head','Scout':'scout'}.get(a['role'])
            if slot is None and a['role']=='Judge':slot='judge_'+{'gpt':'gpt','claude':'claude'}[a['family']]
            prompt=runtime['pilot_seed_launcher_config']['prompts'][slot]
            require(a['prompt_hash']==prompt['sha256']==digest(prompt['text'].encode()),'repin_prompt_not_adopted')
        book=ledger.execute("SELECT * FROM books WHERE book_id='pilot-floor'").fetchone()
        flows=ledger.execute("SELECT * FROM capital_flows WHERE book_id='pilot-floor' ORDER BY id LIMIT 1001").fetchall()
        require(book is not None and book['kind']=='discovery' and 0<len(flows)<=1000,'repin_capital_unknown')
        return dict(epoch=control['active_epoch'],generation=2,
                    office_sha256=digest(frozen_bytes(dict(control=control,actors=actors,epochs=epochs))),
                    capital_sha256=digest(frozen_bytes(dict(book=dict(book),flows=[dict(r) for r in flows]))))
    finally:
        if conn is not None:conn.close()
        if ledger is not None:ledger.close()


def repin_state_eval1(repo,runtime,tool):
    """Same-epoch office-paper-eval-1 witness: the sealed eval-1 open, its 5-seat roster and record, the frozen tool contract,
    the adopted prompts and the evaluated paper book. Read-only."""
    sample=json.loads(regular(Path(runtime['clock_file'])));host=time.time()
    offset,at=sample['offset_s'],sample['at']
    require(all(type(v) in (int,float) and math.isfinite(v) for v in (offset,at))
            and 0<=host-at<=3600 and abs(offset)<=172800,'repin_clock_unknown')
    now=host-offset
    def connect(name):
        path=safe(Path(runtime[name]));conn=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=1.)
        conn.row_factory=sqlite3.Row;deadline=time.monotonic()+1
        conn.set_progress_handler(lambda:int(time.monotonic()>deadline),1000)
        conn.execute('BEGIN');return conn
    def frozen_bytes(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()
    conn=ledger=None
    try:
        conn=connect('store_path');ledger=connect('ledger_path')
        require(conn.execute('SELECT purpose FROM _store_info WHERE id=1').fetchone()[0]=='office','repin_store_unknown')
        lease=conn.execute('SELECT expires_at FROM supervisor_lease WHERE id=1').fetchone()
        require(lease is not None and type(lease[0]) in (int,float) and math.isfinite(lease[0]) and lease[0]<=now,'repin_lease_live_or_unknown')
        control=json.loads(conn.execute('SELECT payload_json FROM office_epoch_control WHERE id=1').fetchone()[0])
        require(control['active_epoch']=='office-paper-eval-1','repin_epoch_unknown')
        receipt=conn.execute('SELECT payload_json,prev_hash FROM org_changes WHERE row_hash=?',(control['receipt_id'],)).fetchone()
        event=json.loads(receipt[0])
        require(digest(frozen_bytes(event)+receipt[1].encode())==control['receipt_id'] and event.get('kind')=='paper_eval_epoch_open','repin_transition_corrupt')
        actors={r[0]:json.loads(r[1]) for r in conn.execute('SELECT id,payload_json FROM agents LIMIT 6')}
        require(len(actors)==5 and actors==event['new_actors'] and all(a.get('state')=='active' for a in actors.values()),'repin_roster_changed')
        row=conn.execute("SELECT payload_json FROM epochs WHERE id='office-paper-eval-1'").fetchone()
        epoch=None if row is None else json.loads(row[0])
        strip=lambda e:{k:v for k,v in e.items() if k!='first_order_at'}
        require(epoch is not None and strip(epoch)==strip(event['new_epoch']),'repin_epoch_changed')
        require(event['new_epoch']['record']['tool_contract_hash']==tool,'repin_tool_not_adopted')
        for a in actors.values():
            slot={'Head':'head','Scout':'scout','Postmortem':'postmortem'}.get(a['role'])
            if slot is None and a['role']=='Judge':slot='judge_'+{'gpt':'gpt','claude':'claude'}[a['family']]
            prompt=runtime['pilot_seed_launcher_config']['prompts'][slot]
            require(a['prompt_hash']==prompt['sha256']==digest(prompt['text'].encode()),'repin_prompt_not_adopted')
        book=ledger.execute("SELECT * FROM books WHERE book_id='office-paper-eval-1'").fetchone()
        flows=ledger.execute("SELECT * FROM capital_flows WHERE book_id='office-paper-eval-1' ORDER BY id LIMIT 1001").fetchall()
        require(book is not None and book['kind']=='evaluated' and book['epoch_id']=='office-paper-eval-1' and 0<len(flows)<=1000,'repin_capital_unknown')
        return dict(epoch=control['active_epoch'],roster=5,
                    office_sha256=digest(frozen_bytes(dict(control=control,actors=actors,epoch=epoch))),
                    capital_sha256=digest(frozen_bytes(dict(book=dict(book),flows=[dict(r) for r in flows]))))
    finally:
        if conn is not None:conn.close()
        if ledger is not None:ledger.close()


def repin_link(current,target):
    temp=current.with_name('.repin-current-'+str(os.getpid()))
    require(not os.path.lexists(temp),'repin_link_temp_exists')
    try:
        temp.symlink_to(target);os.replace(temp,current)
        fd=os.open(current.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:
        if temp.is_symlink():temp.unlink()


EVAL1_SLOTS=('head','scout','judge_claude','judge_gpt','postmortem')
EVAL1_FILES=('pilot_org.json','limits_pilot.json','pilot_models_eval1.json','book_policy_pilot.json')
EVAL1_RUNTIME_FIELDS={'discovery_only','evaluated_epoch_allowed','model_manifest','pilot_seed_launcher_config'}


def eval1_runtime(repo,sha,runtime):
    """--epoch-change eval1: the new runtime is exactly the approved office-paper-eval-1 profile from the new SHA."""
    prefix='config/office/prompts/office-paper-eval-1/'
    approvals=json.loads(blob(repo,sha,prefix+'sha256.json'))
    pins={name:digest(blob(repo,sha,'config/office/'+name)) for name in EVAL1_FILES}
    prompts={slot:dict(text=blob(repo,sha,prefix+slot+'.md').decode(),sha256=approvals['files'][slot+'.md']) for slot in EVAL1_SLOTS}
    require(approvals.get('approval_status')=='approved_alpha' and approvals.get('source_pins')==pins
            and all(digest(p['text'].encode())==p['sha256'] for p in prompts.values())
            and runtime.get('discovery_only') is False and runtime.get('evaluated_epoch_allowed') is True
            and runtime.get('pilot_seed_launcher_config')==dict(schema='office-pilot-seed-launcher-v1',files=pins,prompts=prompts)
            and runtime.get('model_manifest',{}).get('sha256')==pins['pilot_models_eval1.json']
            and str(runtime.get('model_manifest',{}).get('path','')).endswith('/config/office/pilot_models_eval1.json'),
            'repin_eval1_profile_mismatch')


def repin(args):
    manifests=[]
    for bundle,pin in ((args.bundle,args.bundle_sha256),(args.repin_from_bundle,args.repin_from_bundle_sha256)):
        raw=regular(bundle/'manifest.json')
        require(isinstance(pin,str) and re.fullmatch('[0-9a-f]{64}',pin) and digest(raw)==pin,'repin_bundle_pin_mismatch')
        manifests.append(json.loads(raw))
    metadata=lambda doc:{k:v for k,v in doc.items() if k not in ('main_sha','release_root','files')}
    require(metadata(manifests[0])==metadata(manifests[1]),'repin_bundle_policy_changed')
    old_args=argparse.Namespace(**vars(args))
    old_args.rollback=False
    old_args.bundle=args.repin_from_bundle;old_args.bundle_sha256=args.repin_from_bundle_sha256
    old_args.sha=manifests[1]['main_sha']
    require(old_args.sha!=args.sha,'repin_identical_release')
    keep=keep_units(args.repo,old_args.sha,args.sha) if getattr(args,'epoch_change',None) is None else frozenset()
    old=plan(old_args,repin=True,keep=keep);new=plan(args,repin=True,keep=keep)
    repo,release,files,_,current,_,sdk=new
    old_release,old_files=old[1],old[2]
    require(old[0]==repo and old[4]==current and set(old_files)==set(files),'repin_layout_changed')
    require(old_release.is_dir(),'repin_previous_release_missing')
    if old[6]:require(old[6]['root'].is_dir(),'repin_previous_sdk_missing')
    epoch_change=getattr(args,'epoch_change',None)
    require(epoch_change in (None,'eval1'),'repin_epoch_change_unknown')
    tool=repin_tool(repo,args.sha);old_tool=repin_tool(repo,old_args.sha)
    old_runtime=json.loads(old_files[repo/'config/office/runtime.json'][0])
    in_eval1=runtime_is_eval1(old_runtime)
    if epoch_change is None:
        require(tool==old_tool,'repin_epoch_change_required')
        profile,slots=('office-paper-eval-1',EVAL1_SLOTS) if in_eval1 else ('discovery2',('head','scout','judge_claude','judge_gpt'))
        for name in [slot+'.md' for slot in slots]+(['sha256.json'] if in_eval1 else []):
            name='config/office/prompts/'+profile+'/'+name
            require(blob(repo,args.sha,name)==blob(repo,old_args.sha,name),'repin_epoch_change_required')
    else:
        require(not in_eval1,'repin_eval1_already_active')
        # discovery-2 -> office-paper-eval-1 only: the tool contract and prompt profile change, nothing else may
        require(tool!=old_tool,'repin_eval1_tool_unchanged')
        eval1_runtime(repo,args.sha,json.loads(files[repo/'config/office/runtime.json'][0]))
    def normalize(value):
        if isinstance(value,dict):return {k:normalize(v) for k,v in value.items()}
        if isinstance(value,list):return [normalize(v) for v in value]
        if isinstance(value,str) and (value==str(release) or value.startswith(str(release)+'/')):
            return str(old_release)+value[len(str(release)):]
        return value
    changed={repo/name for name in CONFIGS.values()}
    for path,(raw,mode) in files.items():
        prior,prior_mode=old_files[path]
        require(mode==prior_mode,'repin_config_changed')
        if path in changed and path.suffix=='.sha256':continue
        if epoch_change and path==repo/'config/office/runtime.json':
            strip=lambda doc:{k:v for k,v in doc.items() if k not in EVAL1_RUNTIME_FIELDS}
            require(strip(normalize(json.loads(raw)))==strip(json.loads(prior)),'repin_config_changed');continue
        require((normalize(json.loads(raw))==json.loads(prior)) if path in changed else raw==prior,'repin_config_changed')
    runtime=old_runtime
    if in_eval1:eval1_runtime(repo,old_args.sha,runtime)          # same-epoch eval-1 repin: old runtime is the old SHA's eval-1 profile
    else:
        for slot in ('head','scout','judge_claude','judge_gpt'):
            raw=blob(repo,old_args.sha,'config/office/prompts/discovery2/'+slot+'.md')
            require(runtime['pilot_seed_launcher_config']['prompts'][slot]==dict(text=raw.decode(),sha256=digest(raw)),'repin_discovery2_profile_required')
    identity=dict(old_sha=old_args.sha,new_sha=args.sha,old_bundle_sha256=old_args.bundle_sha256,new_bundle_sha256=args.bundle_sha256,
                  old_link=str(old_release),new_link=str(release),
                  files={str(p):dict(old=base64.b64encode(old_files[p][0]).decode(),new=base64.b64encode(files[p][0]).decode(),mode=files[p][1]) for p in sorted(changed)})
    if epoch_change:identity['epoch_change']=epoch_change   # absent for same-epoch repins: older journals stay identical
    journal_path=repo/'.runtime/office/repin.json'
    lock_path=repo/'.runtime/office/activation.lock'
    fd=os.open(safe(lock_path),os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'w') as lock:
        info=os.fstat(lock.fileno());require(stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid() and stat.S_IMODE(info.st_mode)==0o600,'repin_lock_unknown')
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        inactive(keep);witness=repin_state(repo,runtime,old_tool)   # Office DB stays discovery-2 until the eval transition
        require(current.is_symlink() and current.lstat().st_uid==os.getuid() and os.readlink(current) in (str(old_release),str(release)),'repin_current_unknown')
        journal=journal_load(journal_path) if journal_path.exists() else None
        same=journal is not None and all(journal.get(k)==v for k,v in identity.items())
        if journal is not None and not same:
            prior_link=journal.get('new_link') if journal.get('phase')=='repinned' else journal.get('old_link')
            require(journal.get('phase') in ('repinned','rolled_back') and prior_link==str(old_release),'repin_previous_operation_pending')
        if same:require(journal['witness']==witness,'repin_state_changed')
        def check_files(mixed=False,target='new'):
            for p,(raw,mode) in files.items():
                choices=(old_files[p][0],raw) if mixed and p in changed else ((raw,) if target=='new' else (old_files[p][0],))
                require(p.is_file() and any(regular(p)==choice for choice in choices),'repin_foreign_config:'+str(p))
                file_state(p,regular(p),mode)
        def restore():
            check_files(mixed=True)
            require(current.is_symlink() and os.readlink(current) in (str(old_release),str(release)),'repin_current_changed')
            for p in sorted(changed):atomic(p,old_files[p][0],old_files[p][1])
            repin_link(current,old_release);check_files(target='old')
            journal['phase']='rolled_back';save(journal_path,journal)
        if args.rollback:
            require(same,'repin_journal_identity_unknown');check_files(mixed=True)
            if not args.apply:return dict(state='dry_run_rollback',**witness)
            restore();return dict(state='rolled_back',**witness)
        if same and journal['phase']=='repinned':
            check_files();require(os.readlink(current)==str(release),'repin_current_changed')
            return dict(state='repin_verified' if args.verify else 'already_repinned',**witness)
        require(not args.verify,'repin_not_applied')
        require(not same or journal['phase']=='rolled_back','repin_recovery_required')
        check_files(target='old');require(os.readlink(current)==str(old_release),'repin_current_changed')
        if not args.apply:return dict(state='dry_run',old_sha=old_args.sha,new_sha=args.sha,tool_sha256=tool,**witness)
        # Retention: one current and one previous private config journal; never database contents.
        if journal is not None and not same:save(journal_path.with_name('repin.previous.json'),journal)
        journal=dict(identity,witness=witness,phase='applying');save(journal_path,journal)
        try:
            if not release.exists():git(repo,'worktree','add','--detach',str(release),args.sha)
            release_valid(repo,release,args.sha)
            if sdk:sdk['module']['install_wheel'](sdk['wheel'],sdk['root'])
            inactive(keep);require(repin_state(repo,runtime,old_tool)==witness,'repin_state_changed')
            for p in sorted(changed):atomic(p,files[p][0],files[p][1])
            repin_link(current,release)
            inactive(keep);check_files();require(repin_state(repo,runtime,old_tool)==witness,'repin_state_changed')
            journal['phase']='repinned';save(journal_path,journal)
        except BaseException:
            try:restore()
            except Exception as exc:raise Refused('repin_recovery_required:'+str(exc)) from exc
            raise
        return dict(state='repinned',old_sha=old_args.sha,new_sha=args.sha,journal=str(journal_path),**witness)


def main():
    os.umask(0o077)  # This standalone installer and its children only; private new state under every caller umask.
    parser=argparse.ArgumentParser(description='Prepare Office only. Exit 0 plan/verified,2 refused,3 verification_pending. No starts/canary/seed.')
    parser.add_argument('--repo',type=Path,required=True);parser.add_argument('--sha',required=True)
    parser.add_argument('--bundle',type=Path,required=True);parser.add_argument('--bundle-sha256',required=True)
    modes=parser.add_mutually_exclusive_group();modes.add_argument('--dry-run',action='store_true');modes.add_argument('--apply',action='store_true');modes.add_argument('--verify',action='store_true')
    parser.add_argument('--rollback',action='store_true')
    parser.add_argument('--repin-from-bundle',type=Path)
    parser.add_argument('--repin-from-bundle-sha256')
    parser.add_argument('--epoch-change',choices=('eval1',),help='repin only: discovery-2 -> office-paper-eval-1 tool/profile change')
    args=parser.parse_args()
    require(args.epoch_change is None or args.repin_from_bundle is not None,'epoch_change_requires_repin')
    if args.repin_from_bundle is not None or args.repin_from_bundle_sha256 is not None:
        require(args.repin_from_bundle is not None and args.repin_from_bundle_sha256 is not None,'repin_previous_bundle_pin_required')
        print(json.dumps(repin(args)));return 0
    repo,release,files,directories,current,dropins,sdk=plan(args)
    journal_path=repo/'.runtime/office/activation'/args.sha/'journal.json'
    if args.verify:
        require(not args.rollback,'verify_rollback_conflict')
        pending=verify(repo,release,files,current,dropins,sdk)
        print(json.dumps(dict(state='verification_pending' if pending else 'preparation_verified',pending=pending,manual_steps=['canary','seed','supervisor_start'])))
        return 3 if pending else 0
    if not args.apply:
        if args.rollback:
            require(journal_path.is_file(),'rollback_journal_missing')
            rollback(repo,release,files,directories,current,journal_path,journal_load(journal_path),dropins,sdk,dry_run=True)
        print(json.dumps(dict(state='dry_run',operation='rollback' if args.rollback else 'prepare',sha=args.sha,release=str(release),files=[str(p) for p in files],pending=['live_caps','guard','health','manual_canary','manual_seed','manual_supervisor_start'])))
        return 0
    inactive()
    if args.rollback:require(journal_path.is_file(),'rollback_journal_missing')
    ensure_dir(journal_path.parent,0o700)
    lock_path=repo/'.runtime/office/activation.lock'
    lock=os.open(lock_path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
    try:
        info=os.fstat(lock);require(stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid() and stat.S_IMODE(info.st_mode)==0o600,'activation_lock_identity')
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        journal=None
        if journal_path.exists():
            journal=journal_load(journal_path)
            require(journal['sha']==args.sha and journal['bundle_sha256']==args.bundle_sha256,'journal_bundle_mismatch')
            validate_journal(repo,release,files,directories,journal,sdk)
        if journal is not None and journal['phase']=='rolled_back' and not args.rollback:
            rollback(repo,release,files,directories,current,journal_path,journal,dropins,sdk)
            journal=None
        if journal is None:
            require(not args.rollback,'rollback_journal_missing')
            journal=dict(phase='applying',repo=str(repo),release=str(release),sha=args.sha,bundle_sha256=args.bundle_sha256,
                files={str(p):dict(sha256=digest(raw),mode=mode,existed=p.exists()) for p,(raw,mode) in files.items()},
                directories={str(p):p.exists() for p in directories},enabled={u:enabled(u) for u in ENABLE_UNITS},
                release_existed=release.exists(),current_existed=os.path.lexists(current),vendor_existed=bool(sdk and sdk['root'].exists()))
            save(journal_path,journal)
        if args.rollback:
            rollback(repo,release,files,directories,current,journal_path,journal,dropins,sdk)
            print(json.dumps(dict(state='rolled_back',data_preserved=True)));return 0
        try:
            for path,mode in directories.items():ensure_dir(path,mode)
            ensure_dir(release.parent,0o700)
            if not release.exists():git(repo,'worktree','add','--detach',str(release),args.sha)
            release_valid(repo,release,args.sha)
            if sdk:sdk['module']['install_wheel'](sdk['wheel'],sdk['root'])
            for path,(raw,mode) in files.items():
                ensure_dir(path.parent)
                if not file_state(path,raw,mode):atomic(path,raw,mode)
            if not os.path.lexists(current):current.symlink_to(release,target_is_directory=True)
            supervisor_alias(repo,release,installed=True)
            ctl('daemon-reload');ctl('enable',*ENABLE_UNITS)
            require(all(enabled(unit) for unit in ENABLE_UNITS),'unit_enable_readback_mismatch')
            journal['phase']='prepared';save(journal_path,journal)
            pending=verify(repo,release,files,current,dropins,sdk)
        except Exception:
            try:rollback(repo,release,files,directories,current,journal_path,journal,dropins,sdk)
            except Exception as exc:raise Refused('recovery_required:'+str(exc)) from None
            raise
        print(json.dumps(dict(state='verification_pending' if pending else 'preparation_verified',pending=pending,sha=args.sha,manual_steps=['canary','seed','supervisor_start'])))
        return 3 if pending else 0
    finally:os.close(lock)


try:
    raise SystemExit(main())
except Exception as exc:
    if not isinstance(exc,Refused):
        import traceback
        traceback.print_exc(file=sys.stderr)
    print(json.dumps(dict(state='refused',reason=str(exc))));raise SystemExit(2)
PY
