"""Root host-boundary primitives, inactive until a reviewed service binds trusted probes.

No public signing endpoint, job ingress, service installation or key generation. Tests
may select a fixture UID/anchor; the host service must use root and the filesystem root.
All imports in an installed root artifact must be root-owned/hash-pinned before exec.
"""
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import stat

from .planned_recovery import canonical, read_regular
from uuid import uuid4


def atomic(path, value):
    """Mode is set before rename; writes are relative to a held directory FD."""
    path=Path(path);name='.host-'+uuid4().hex
    directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory)
        with os.fdopen(fd,'wb') as stream:
            os.fchmod(stream.fileno(),0o644 if path.name=='guard.json' else 0o600)
            stream.write(canonical(value));stream.flush();os.fsync(stream.fileno())
        os.rename(name,path.name,src_dir_fd=directory,dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:os.unlink(name,dir_fd=directory)
        except FileNotFoundError:pass
        os.close(directory)


def protected_directory(path, *, owner_uid=0, anchor=Path('/')):
    """Hold an FD after validating every ancestor in the protected tree, no symlinks.

    Production anchor is /; a writable /tmp ancestor is deliberately not acceptable.
    Fixture anchors are an explicit test dependency, never an untrusted policy field.
    """
    path, anchor = Path(path), Path(anchor)
    if (not path.is_absolute() or not anchor.is_absolute() or '..' in path.parts
            or '..' in anchor.parts or path != anchor and anchor not in path.parents):
        raise ValueError('host_path')
    current = anchor
    fd = os.open(anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = path.relative_to(anchor).parts
        for component in (None, *parts):
            if component is not None:
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd);fd=child;current=current/component
            info = os.fstat(fd)
            if info.st_uid != owner_uid or info.st_mode & 0o022:
                raise ValueError('host_directory_owner_or_mode:'+str(current))
        return fd
    except BaseException:
        os.close(fd)
        raise


class GuardPublisher:
    """Guard-only writer; no replay path dependency. Shared external lock serializes all writers.

    Lock conflicts are explicit: a failed pause must never be acknowledged as committed.
    Service bindings retain/retry a pause request or fail closed. This is not CAS.
    """
    def __init__(self,public,lock_dir,*,owner_uid=0,anchor=Path('/')):
        self.public,self.lock_dir=Path(public),Path(lock_dir)
        self.owner_uid,self.anchor=owner_uid,Path(anchor)
        for path in (self.public,self.lock_dir):
            fd=protected_directory(path,owner_uid=owner_uid,anchor=anchor);os.close(fd)
        self.guard=self.public/'guard.json'

    @contextmanager
    def locked(self):
        # Root-owned ancestors exclude non-root replacement, while the common lock
        # excludes every cooperating privileged writer. Recheck+rename is NOT CAS.
        dirs = []
        lock = None
        try:
            for p in (self.lock_dir,self.public):
                dirs.append(protected_directory(p,owner_uid=self.owner_uid,anchor=self.anchor))
            fd = os.open('authority.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK,
                         0o600,dir_fd=dirs[0])
            lock = os.fdopen(fd,'w')
            info=os.fstat(lock.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=self.owner_uid or info.st_mode & 0o077:
                raise ValueError('host_lock_identity')
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            yield
        finally:
            if lock is not None:lock.close()
            for fd in dirs:os.close(fd)

    def read(self, path):
        value=json.loads(read_regular(path,expected_uid=self.owner_uid))
        if type(value) is not dict:
            raise ValueError('host_state_unknown')
        return value

    def pause(self, reason, *, now):
        if type(reason)is not str or not reason or type(now)not in (int,float) or not math.isfinite(now) or now<0:
            raise ValueError('host_pause_invalid')
        with self.locked():
            guard=self.read(self.guard)
            revision=guard.get('revision')
            if type(revision)is not int or revision<0:
                raise ValueError('host_revision_unknown')
            if reason!='funded_heartbeat' and 'unrelated_fault_seq'in guard:
                sequence=guard['unrelated_fault_seq']
                guard['unrelated_fault_seq']=sequence+1 if type(sequence)is int and sequence>=0 else None
                guard['last_unrelated_fault']=dict(reason=reason,at=now)
            atomic(self.guard,dict(guard,state='paused',reason=reason,at=now,revision=revision+1,
                                   healthy_since=None,resume_eligible=False,recovery_basis=None))



class Authority(GuardPublisher):
    """Recovery adapter ONLY: guardian must never instantiate or mount this private state."""
    def __init__(self,private,public,*,lock_dir,owner_uid=0,anchor=Path('/')):
        self.private=Path(private)
        public,lock_dir=Path(public),Path(lock_dir)
        if any(self.private==p or self.private in p.parents or p in self.private.parents
               for p in (public,lock_dir)):
            raise ValueError('host_private_public_overlap')
        fd=protected_directory(self.private,owner_uid=owner_uid,anchor=anchor)
        try:
            if os.fstat(fd).st_mode & 0o077:raise ValueError('host_private_mode')
        finally:os.close(fd)
        super().__init__(public,lock_dir,owner_uid=owner_uid,anchor=anchor)
        self.state=self.private/'state.json'

    def initialize(self, state, guard):
        """Explicit root installation only; never a daemon's missing-state fallback."""
        with self.locked():
            if os.path.lexists(self.state) or os.path.lexists(self.guard):
                raise ValueError('host_already_initialized')
            if guard.get('revision')!=0:
                raise ValueError('host_initial_revision')
            atomic(self.state,state)
            atomic(self.guard,guard)

    def recover(self, expected_revision, transition):
        """Trusted root transition only, never caller-provided code or success JSON.

        The service re-probes under this lock; durable consumption precedes ready.
        On failure after consumption, the shortcut is lost rather than replayable.
        """
        with self.locked():
            guard=self.read(self.guard);state=self.read(self.state)
            if (type(expected_revision)is not int or type(guard.get('revision'))is not int
                    or expected_revision<0 or guard['revision']!=expected_revision):
                raise ValueError('host_revision_changed')
            if (guard.get('state')=='paused' and guard.get('reason')!='funded_heartbeat'
                    or guard.get('state') not in ('paused','ready')
                    or guard.get('state')=='ready' and guard.get('reason')is not None):
                raise ValueError('host_unrelated_pause')
            termination=guard.get('termination')
            if (guard.get('auto_resume_blocked')is True
                    or 'auto_resume_blocked'in guard and type(guard['auto_resume_blocked'])is not bool
                    or 'termination'in guard and (type(termination)is not dict or termination.get('state')!='sent')):
                raise ValueError('host_blocked_authority')
            if state.get('status')=='consumed':
                raise ValueError('host_already_consumed')
            next_state,ready=transition(state,guard)
            if (type(next_state)is not dict or next_state.get('status')!='consumed'
                    or type(ready)is not dict or ready.get('state')!='ready' or ready.get('reason')is not None
                    or ready.get('recovery_basis')!='planned_stop_verified'):
                raise ValueError('host_recovery_not_verified')
            ready=dict(ready,revision=expected_revision+1)
            if 'unrelated_fault_seq'in guard:
                if type(guard['unrelated_fault_seq'])is not int or guard['unrelated_fault_seq']<0:
                    raise ValueError('host_fault_sequence_unknown')
                ready['unrelated_fault_seq']=guard['unrelated_fault_seq']
                ready['last_unrelated_fault']=guard.get('last_unrelated_fault')
            atomic(self.state,next_state)
            atomic(self.guard,ready)
            return ready

