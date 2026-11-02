"""Pinned host auth binding; model children cannot clear durable provider pauses."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
from uuid import UUID

from sniper.office import codex_auth
from sniper.office.launcher_client import read_regular
from sniper.office.model_auth import AuthRefused, claude_snapshot


PATHS = ('private_state', 'request_root', 'child_root', 'launcher_state',
         'codex_home', 'codex_lock', 'claude_credentials')


def approved_path(value):
    if type(value) is not str or not Path(value).is_absolute() or '..' in Path(value).parts:
        raise ValueError('auth_config_path')
    path = Path(value)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('auth_config_symlink')
    return path


class Auth:
    def __init__(self, config_path, *, expected_sha256, completed, clock, alert):
        if not all(callable(fn) for fn in (completed, clock, alert)):
            raise ValueError('trusted_auth_bindings_required')
        raw = read_regular(approved_path(str(config_path)))
        if hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise ValueError('auth_config_unapproved')
        self.config = json.loads(raw)
        paths = {key: approved_path(self.config[key]) for key in PATHS}
        source = paths['claude_credentials']
        if source.name != '.credentials.json' or source.parent.name != '.claude':
            raise ValueError('claude_auth_source')
        self.private_state = private = paths['private_state']
        model_roots = self.config['model_roots']
        if type(model_roots) is not list:
            raise ValueError('auth_model_roots')
        excluded = [paths[key] for key in ('request_root', 'child_root', 'launcher_state')]
        excluded += [approved_path(value) for value in model_roots]
        for other in excluded:
            if private == other or private in other.parents or other in private.parents:
                raise ValueError('auth_private_state_overlap')
        supervisor_roots = self.config['supervisor_roots']
        if type(supervisor_roots) is not list:
            raise ValueError('auth_supervisor_roots')
        for root in supervisor_roots:
            root = approved_path(root)
            for protected in (private, paths['child_root'], paths['launcher_state']):
                if root == protected or root in protected.parents or protected in root.parents:
                    raise ValueError('auth_supervisor_overlap')
        for value in model_roots + supervisor_roots:
            root = approved_path(value)
            for secret in (paths['codex_home'], paths['claude_credentials']):
                if root == secret or root in secret.parents or secret in root.parents:
                    raise ValueError('auth_credential_mount_overlap')
        private.mkdir(parents=True, mode=0o700, exist_ok=True)
        info = private.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError('auth_private_state')
        self.completed, self.clock, self.alert = completed, clock, alert

    def _alert(self, provider, reason, rid):
        if reason in ('codex_cleanup_unproven', 'codex_auth_writeback_refused',
                      'codex_auth_snapshot_missing', 'claude_auth_child_rotated'):
            path = self.private_state / (provider + '.paused.json')
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, 'w') as out:
                    json.dump({'reason': reason, 'request_id': rid}, out)
                    out.flush(); os.fsync(out.fileno())
                directory = os.open(self.private_state, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try: os.fsync(directory)
                finally: os.close(directory)
        self.alert(reason, rid)

    @contextmanager
    def __call__(self, adapter, parent, *, deadline_at):
        providers = {'claude_sdk': 'claude', 'codex_exec': 'codex'}
        if adapter not in providers:
            raise AuthRefused('launcher_refused')
        provider = providers[adapter]
        parent = Path(parent)
        if parent.parent != Path(self.config['child_root']) or str(UUID(parent.name)) != parent.name:
            raise AuthRefused('auth_snapshot_path')
        # Hold across preparation, child dispatch, cleanup and pause publication: no in-process race window.
        lock = os.open(self.private_state / (provider + '.lock'),
                       os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(lock).st_mode):
                raise AuthRefused('auth_private_state')
            try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: raise AuthRefused('codex_busy' if provider == 'codex' else 'provider_busy') from None
            if os.path.lexists(self.private_state / (provider + '.paused.json')):
                raise AuthRefused('provider_paused')
            alert = lambda reason, rid: self._alert(provider, reason, rid)
            if provider == 'claude':
                context = claude_snapshot(self.config['claude_credentials'], parent, now=self.clock(),
                                          deadline_at=deadline_at, child_root=self.config['child_root'], alert=alert)
            else:
                context = codex_auth.snapshot(real_home=self.config['codex_home'], parent=parent,
                    child_root=self.config['child_root'], private_state=self.private_state,
                    lock_path=self.config['codex_lock'], completed=self.completed, alert=alert)
            try:
                with context: yield
            except AuthRefused as exc:
                if exc.reason == 'codex_auth_snapshot_missing':
                    alert(exc.reason, parent.name)
                raise
        finally:
            os.close(lock)
