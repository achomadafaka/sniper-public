"""Fail-closed OFFLINE VM definition and untrusted file export checks; no downloader."""
import hashlib
from dataclasses import dataclass
from contextlib import closing,contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import uuid

IMAGE_MAX=1024**3
INPUT_MAX=64*1024**2
EXPORT_MAX=1024**2
QUARANTINED=('evaluation','grade','outcome','label','verdict','trial_registry')


def load_plan(path,expected_sha256):
    from sniper.office.child_launcher import read_regular
    raw=read_regular(path)
    if hashlib.sha256(raw).hexdigest()!=expected_sha256:raise ValueError('vm_plan_unapproved')
    return json.loads(raw)


def _open_verified(path,expected,*,algorithm,maximum,size=None):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        before=os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_size>maximum or before.st_mode&0o222
                or (size is not None and before.st_size!=size)):
            raise ValueError('vm_input_not_sealed')
        digest=hashlib.new(algorithm)
        with os.fdopen(os.dup(fd),'rb') as f:
            for part in iter(lambda:f.read(1024**2),b''):digest.update(part)
        after=os.fstat(fd)
        if (before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns):
            raise ValueError('vm_input_changed')
        if digest.hexdigest()!=expected:raise ValueError('vm_input_hash')
        return fd,before
    except BaseException:
        os.close(fd)
        raise


def verified_file(path,expected,*,algorithm,maximum,size=None):
    fd,before=_open_verified(path,expected,algorithm=algorithm,maximum=maximum,size=size)
    try:return before.st_size
    finally:os.close(fd)


def _quarantine(path,read_path=None):
    if any(word in path.name.lower() for word in QUARANTINED):raise ValueError('quarantined_input')
    if path.suffix in ('.db','.sqlite'):
        with closing(sqlite3.connect((Path(read_path).absolute() if read_path else path.resolve()).as_uri()+'?mode=ro',uri=True,timeout=0)) as conn:
            conn.execute('PRAGMA query_only=ON');conn.execute('PRAGMA trusted_schema=OFF')
            rows=conn.execute("SELECT name FROM sqlite_schema WHERE type IN ('table','view') LIMIT 257").fetchall()
            if len(rows)>256 or any(any(word in n.lower() for word in QUARANTINED) for (n,) in rows):
                raise ValueError('quarantined_table')
    # The externally pinned, Gamma-reviewed manifest is still required: a name scan cannot prove absence of labels.


def _command(plan,seal):
    required={'version','enabled','image','snapshots','quarantine_review_commit','network','exports_root','memory_mib','vcpus'}
    if (type(plan) is not dict or set(plan)!=required or plan['version']!='office-research-vm-offline-v1'
            or plan['enabled'] is not True or plan['memory_mib']!=1024 or type(plan['memory_mib']) is not int
            or type(plan['vcpus']) is not int or plan['vcpus']!=1):raise ValueError('vm_definition_invalid')
    if not re.fullmatch('[a-f0-9]{40}',str(plan['quarantine_review_commit'])):
        raise ValueError('vm_quarantine_unapproved')
    # Provider/RPC egress remains refused until an independently reviewed host proxy exists.
    if plan['network']!={'mode':'offline','allowlist':[]}:raise ValueError('vm_network_unapproved')
    snapshots=plan['snapshots']
    if type(snapshots) is not list or not 1<=len(snapshots)<=16:raise ValueError('vm_snapshots_missing')
    exports=Path(plan['exports_root'])
    if exports.is_symlink() or not exports.is_dir():raise ValueError('vm_exports_invalid')
    fs=os.statvfs(exports)
    if not 0<fs.f_blocks*fs.f_frsize<=32*1024**2 or fs.f_flag&os.ST_RDONLY:
        raise ValueError('vm_exports_unbounded')
    if next(exports.iterdir(),None) is not None:raise ValueError('vm_exports_not_empty')
    image=plan['image']
    image_source,_=seal(image['path'],image['sha512'],algorithm='sha512',maximum=IMAGE_MAX,size=image['bytes'])
    args=['/usr/bin/bwrap','--unshare-user','--unshare-pid','--unshare-net','--die-with-parent','--new-session',
          '--ro-bind','/','/','--dev','/dev','--dev-bind','/dev/kvm','/dev/kvm',
          '--tmpfs','/home','--tmpfs','/root','--tmpfs','/run','--tmpfs','/tmp','--tmpfs','/opt',
          '--size','268435456','--tmpfs','/opt/scratch','--dir','/opt/inputs',
          '--ro-bind',image_source,'/opt/base.qcow2','--bind',str(exports),'/opt/exports']
    names=set();total=0
    for item in snapshots:
        name=item['name'];source=Path(item['path'])
        if (not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,95}',name) or name in names
                or item['kind'] not in ('archive','daily_tape')
                or any(word in name.lower() for word in QUARANTINED)):
            raise ValueError('vm_snapshot_invalid')
        binding,size=seal(source,item['sha256'],algorithm='sha256',maximum=INPUT_MAX)
        total+=size
        _quarantine(source,read_path=binding)
        if total>256*1024**2:raise ValueError('vm_snapshot_budget')
        names.add(name);args+=['--ro-bind',binding,'/opt/inputs/'+name]
    return args+['--proc','/proc','--chdir','/opt/scratch','--cap-drop','ALL','--clearenv','--setenv','TMPDIR','/opt/scratch',
          '/usr/bin/qemu-system-x86_64','-enable-kvm','-m','1024','-smp','1','-nodefaults',
          '-display','none','-serial','stdio','-monitor','none','-nic','none','-snapshot',
          '-drive','file=/opt/base.qcow2,if=virtio,format=qcow2',
          '-virtfs','local,path=/opt/inputs,mount_tag=research,security_model=none,readonly=on',
          '-virtfs','local,path=/opt/exports,mount_tag=exports,security_model=none']


def command(plan):
    """Inspection only. An executor must use pinned_command, not this path-based preview."""
    def seal(path,expected,**kwargs):
        return str(path),verified_file(path,expected,**kwargs)
    return _command(plan,seal)


def _fingerprint(value):
    return (value.st_dev,value.st_ino,value.st_size,value.st_mode,value.st_mtime_ns,value.st_ctime_ns,value.st_nlink)


@dataclass(frozen=True)
class PinnedCommand:
    argv:list
    pass_fds:tuple
    sources:tuple

    def verify(self):
        for path,fd,before in self.sources:
            try:
                current=os.stat(path,follow_symlinks=False)
                held=os.fstat(fd)
            except OSError as exc:raise ValueError('vm_input_replaced') from exc
            if (current.st_dev,current.st_ino)!=(before.st_dev,before.st_ino):
                raise ValueError('vm_input_replaced')
            if _fingerprint(current)!=_fingerprint(before) or _fingerprint(held)!=_fingerprint(before):
                raise ValueError('vm_input_changed')


@contextmanager
def pinned_command(plan):
    """Bind held verified inodes; refuse replaced/changed originals instead of reopening paths."""
    handles=[];sources=[]
    def seal(path,expected,**kwargs):
        path=Path(path)
        if not path.is_absolute():raise ValueError('vm_input_path')
        fd,before=_open_verified(path,expected,**kwargs)
        handles.append(fd);sources.append((path,fd,before))
        return '/proc/self/fd/%d'%fd,before.st_size
    try:
        args=_command(plan,seal)
        for i in range(len(args)-2):
            if args[i]=='--ro-bind' and args[i+1].startswith('/proc/self/fd/'):
                args[i]='--ro-bind-fd';args[i+1]=args[i+1].rsplit('/',1)[1]
        exports=Path(plan['exports_root'])
        if not exports.is_absolute():raise ValueError('vm_input_path')
        fd=os.open(exports,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_NONBLOCK)
        handles.append(fd);before=os.fstat(fd);fs=os.fstatvfs(fd)
        if not 0<fs.f_blocks*fs.f_frsize<=32*1024**2 or fs.f_flag&os.ST_RDONLY:
            raise ValueError('vm_exports_unbounded')
        if os.listdir(fd):raise ValueError('vm_exports_not_empty')
        sources.append((exports,fd,before))
        at=args.index('/opt/exports');args[at-2:at]=['--bind-fd',str(fd)]
        launch=PinnedCommand(args,tuple(handles),tuple(sources));launch.verify()
        yield launch
    finally:
        for fd in handles:os.close(fd)


def export_file(source_root,name,destination_root,*,guest_stopped):
    if guest_stopped is not True or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,80}\.(txt|json)',name):
        raise ValueError('vm_export_refused')
    root=os.open(source_root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=root)
    finally:os.close(root)
    try:
        before=os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_mode&0o111
                or before.st_size>EXPORT_MAX):raise ValueError('vm_export_invalid')
        with os.fdopen(os.dup(fd),'rb') as f:raw=f.read(EXPORT_MAX+1)
        after=os.fstat(fd)
        if (len(raw)>EXPORT_MAX or (before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=
                (after.st_size,after.st_mtime_ns,after.st_ctime_ns)):raise ValueError('vm_export_changed')
        text=raw.decode('utf-8')
        if name.endswith('.json'):json.dumps(json.loads(text),allow_nan=False)
    finally:os.close(fd)
    target_name=str(uuid.uuid4())+Path(name).suffix
    directory=os.open(destination_root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:out=os.open(target_name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o400,dir_fd=directory)
    finally:os.close(directory)
    with os.fdopen(out,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
    return {'type':'untrusted_vm_export','name':target_name,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
