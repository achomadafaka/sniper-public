"""Host-only config probe. Invoke inside the actual Office uid/mount namespace.

A root FD from a writable mount is refused before any attempted change. No uid,
namespace or environment mutation occurs in the calling process. No installation.
"""
import errno
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys
import uuid


ACTIONS=('open_for_write','chmod','create','rename','unlink')


def _readonly(fd):
    return bool(os.fstatvfs(fd).f_flag & os.ST_RDONLY)


def _child(request):
    result=dict(answer=None,reason='probe_unknown',pid=os.getpid(),uid=os.geteuid(),
                gids=sorted(set(os.getgroups()+[os.getegid()])),witnesses=[],relative=request.get('relative'))
    owned=[]
    try:
        if result['uid']!=request['office_uid'] or result['gids']!=request['office_gids']:
            result['reason']='office_identity_mismatch';return result
        root=request['root_fd']; info=os.fstat(root)
        result['root_dev_ino']=[info.st_dev,info.st_ino]
        result['mount_namespace']=os.readlink('/proc/self/ns/mnt')
        if result['mount_namespace']!=request['office_mount_ns']:
            result['reason']='office_namespace_mismatch';return result
        if (not stat.S_ISDIR(info.st_mode) or result['root_dev_ino']!=request['root_dev_ino']
                or fcntl.fcntl(root,fcntl.F_GETFL)&os.O_ACCMODE!=os.O_RDONLY):
            result['reason']='root_identity_mismatch';return result
        result['mount_ro']=_readonly(root)
        if not result['mount_ro']:
            result['reason']='root_not_readonly';return result
        caps=next(line.split()[1] for line in Path('/proc/self/status').read_text().splitlines() if line.startswith('CapEff:'))
        if int(caps,16):
            result['reason']='privileged_probe_refused';return result
        relative=request['relative']
        if not isinstance(relative,str) or len(relative)>1024:
            raise ValueError('invalid_path')
        path=PurePosixPath(relative)
        if path.is_absolute() or '..' in path.parts:
            raise ValueError('invalid_path')
        parent=os.dup(root);owned.append(parent)
        for part in path.parts[:-1]:
            parent=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent);owned.append(parent)
        name=path.name or '.'
        target=os.open(name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=parent);owned.append(target)
        target_info=os.fstat(target)
        if not (stat.S_ISREG(target_info.st_mode) or stat.S_ISDIR(target_info.st_mode)):
            raise ValueError('invalid_target')
        if not _readonly(parent) or not _readonly(target):
            result['reason']='target_or_parent_not_readonly';return result
        leaf=name
        mutation_parent=parent
        if stat.S_ISDIR(target_info.st_mode):
            mutation_parent=target;leaf=None
            with os.scandir(target) as entries:
                for index,entry in enumerate(entries):
                    if index>=64:break
                    if entry.is_file(follow_symlinks=False):
                        leaf=entry.name;break
            if leaf is None:
                result['reason']='existing_parent_anchor_missing';return result
            anchor=os.open(leaf,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=mutation_parent);owned.append(anchor)
            if not _readonly(anchor):
                result['reason']='anchor_not_readonly';return result
        temporary='.office-write-probe-'+uuid.uuid4().hex
        def attempt(action,operation):
            try:
                operation()
                result['witnesses'].append(dict(action=action,denied=False,errno=None))
            except OSError as exc:
                denied=exc.errno in (errno.EROFS,errno.EACCES,errno.EPERM)
                if action=='open_for_write' and stat.S_ISDIR(target_info.st_mode) and exc.errno==errno.EISDIR:
                    denied=True
                result['witnesses'].append(dict(action=action,denied=denied,errno=exc.errno))
        def open_write():
            fd=os.open(name,os.O_WRONLY|os.O_NOFOLLOW,dir_fd=parent)
            os.close(fd)
        def create():
            fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=mutation_parent)
            os.close(fd)
        operations=(open_write,lambda:os.fchmod(target,stat.S_IMODE(target_info.st_mode)^stat.S_IWUSR),
            create,lambda:os.rename(leaf,temporary,src_dir_fd=mutation_parent,dst_dir_fd=mutation_parent),
            lambda:os.unlink(leaf,dir_fd=mutation_parent))
        for action,operation in zip(ACTIONS,operations):
            attempt(action,operation)
            if result['witnesses'][-1]['denied'] is not True:
                result['reason']='write_allowed_or_denial_unknown';return result
        if all(w['denied'] is True for w in result['witnesses']):
            result.update(answer=False,reason='all_attempts_denied')
        else:result['reason']='write_allowed_or_denial_unknown'
    except (OSError,ValueError,TypeError,KeyError,StopIteration):
        result['reason']='probe_unknown'
    finally:
        for fd in reversed(owned):os.close(fd)
    return result


def probe_write(relative, *, root_fd, office_uid, office_gids, office_mount_ns, record_witness, timeout_s=2.):
    """Literal False only with five denied operations and a recorded child witness.

    The trusted launcher must invoke this in the Office's existing uid/namespace;
    an identity mismatch is unknown, never impersonated with process-global setuid.
    """
    try:
        if (type(office_uid) is not int or office_uid<0 or not isinstance(office_gids,list) or len(office_gids)>64
                or any(type(g) is not int or g<0 for g in office_gids)
                or not isinstance(relative,str) or len(relative)>1024
                or not isinstance(office_mount_ns,str) or len(office_mount_ns)>128
                or not callable(record_witness) or type(timeout_s) not in (int,float) or not 0<timeout_s<=5):
            return None
        info=os.fstat(root_fd)
        request=dict(root_fd=root_fd,root_dev_ino=[info.st_dev,info.st_ino],relative=relative,
                     office_uid=office_uid,office_gids=sorted(set(office_gids)),office_mount_ns=office_mount_ns)
        child=subprocess.run([sys.executable,'-I','-S','-B',str(Path(__file__).resolve()),'--child'],
            input=json.dumps(request,allow_nan=False),text=True,capture_output=True,
            pass_fds=(root_fd,),env={},timeout=timeout_s,check=True)
        if len(child.stdout)>8192 or child.stderr:return None
        result=json.loads(child.stdout)
        try:record_witness(result)
        except Exception:return None
        if (result.get('answer') is False and result.get('uid')==office_uid
                and result.get('gids')==request['office_gids'] and result.get('root_dev_ino')==request['root_dev_ino']
                and result.get('pid')!=os.getpid() and result.get('reason')=='all_attempts_denied'
                and result.get('mount_namespace')==office_mount_ns and result.get('mount_ro') is True
                and tuple(w.get('action') for w in result.get('witnesses',[]))==ACTIONS
                and all(w.get('denied') is True for w in result['witnesses'])):
            return False
    except (OSError,ValueError,TypeError,KeyError,AttributeError,subprocess.SubprocessError):
        pass
    return None


if __name__=='__main__':
    if sys.argv[1:]!=['--child']:raise SystemExit(2)
    raw=sys.stdin.read(8193)
    if len(raw)>8192:raise SystemExit(2)
    print(json.dumps(_child(json.loads(raw)),sort_keys=True,allow_nan=False))
