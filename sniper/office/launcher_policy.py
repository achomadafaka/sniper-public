"""One-use host launcher authority. No model fields, globals, or uid impersonation."""
from collections.abc import Mapping
from dataclasses import dataclass
import fcntl
import hashlib
import math
import os
from pathlib import Path, PurePosixPath
import sqlite3
import stat
import time

from . import store as S

KEYS = frozenset({'schema','principal','office_uid','office_gids','approved_full_guard_sha256',
    'approval_origin','approval_ref','config_root_fd','config_root_dev_ino','config_root_readonly_proof',
    'issued_at','expires_at','nonce'})
PROBE_BUDGET_S = 30.


class Invalid(ValueError):
    pass


def _finite(v):
    return type(v) in (int,float) and math.isfinite(v) and v >= 0


def _hex(v, length):
    return isinstance(v,str) and len(v) == length and all(c in '0123456789abcdef' for c in v)


def readonly_mount(fd):
    """An outside-namespace RW directory FD can bypass a path's read-only bind."""
    return bool(os.fstatvfs(fd).f_flag & os.ST_RDONLY)


@dataclass
class Authority:
    policy: dict
    root_fd: int
    probes: dict

    def close(self):
        fd, self.root_fd = self.root_fd, None
        if fd is not None:
            os.close(fd)

    def recheck(self, now):
        p = self.policy
        if not _finite(now) or not p['issued_at'] <= now <= p['expires_at']:
            raise Invalid('launcher_policy_expired')
        info = os.fstat(self.root_fd)
        if [info.st_dev,info.st_ino] != p['config_root_dev_ino']:
            raise Invalid('config_root_identity_mismatch')
        if not readonly_mount(self.root_fd):
            raise Invalid('config_root_not_readonly')

    def ensure_unused(self, conn):
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='receipts_launcher_nonce'").fetchone() is None:
            raise Invalid('launcher_nonce_index_missing')
        prior = conn.execute("SELECT 1 FROM receipts WHERE json_extract(payload_json,'$.kind')='launcher_policy_use' AND json_extract(payload_json,'$.launcher_policy_nonce')=? LIMIT 1", (self.policy['nonce'],)).fetchone()
        if prior is not None:
            raise Invalid('launcher_policy_nonce_replayed')

    def consume(self, conn, now):
        self.recheck(now)
        self.ensure_unused(conn)
        try:
            return S.append(conn, 'receipts', dict(kind='launcher_policy_use',
                launcher_policy_nonce=self.policy['nonce'], known_at=now, actor_id='crew',
                policy_sha256=hashlib.sha256(S.canonical_json(self.policy)).hexdigest(),
                approved_full_guard_sha256=self.policy['approved_full_guard_sha256'],
                office_uid=self.policy['office_uid'], config_root_dev_ino=self.policy['config_root_dev_ino']))
        except sqlite3.IntegrityError:
            raise Invalid('launcher_policy_nonce_replayed') from None


def prepare(*, root, refs, by, clock, launcher_policy, probe_write):
    """All host/probe callbacks finish before the caller takes the Office write lock."""
    fd = None
    deadline = time.monotonic()+PROBE_BUDGET_S
    def budget():
        # Cooperative total bound; each host helper separately owns its subprocess timeout.
        if time.monotonic()>deadline:
            raise Invalid('launcher_probe_budget_exceeded')
    try:
        if not callable(launcher_policy) or not callable(probe_write):
            raise Invalid('host_launcher_callbacks_required')
        supplied = launcher_policy(by)
        budget()
        if not isinstance(supplied, Mapping) or set(supplied) != KEYS:
            raise Invalid('launcher_policy_schema')
        import json
        raw = S.canonical_json(dict(supplied))
        if len(raw) > 8192:
            raise Invalid('launcher_policy_size')
        p = json.loads(raw)
        now = clock()
        if p['schema'] != 'office-launcher-policy-v1' or p['principal'] != by or by != 'crew':
            raise Invalid('launcher_principal_unknown')
        if (type(p['office_uid']) is not int or p['office_uid'] < 0
                or not isinstance(p['office_gids'],list) or len(p['office_gids']) > 64
                or any(type(g) is not int or g < 0 for g in p['office_gids'])):
            raise Invalid('launcher_office_identity_unknown')
        approval = p['approval_ref']
        if (p['approval_origin'] != 'launcher_config' or not _hex(p['approved_full_guard_sha256'],64)
                or not isinstance(approval,dict) or set(approval) != {'commit','path','approver','second_approver'}
                or not _hex(approval['commit'],40) or approval['path'] != 'config/office/host_guard.json'
                or approval['approver'] != 'gamma' or approval['second_approver'] != 'alpha'):
            raise Invalid('launcher_guard_approval_unknown')
        if (not _finite(p['issued_at']) or not _finite(p['expires_at']) or not _finite(now)
                or not 0 < p['expires_at']-p['issued_at'] <= 60.
                or not p['issued_at'] <= now <= p['expires_at']
                or not isinstance(p['nonce'],str) or not 1 <= len(p['nonce']) <= 128):
            raise Invalid('launcher_policy_expired_or_nonce_invalid')
        proof = p['config_root_readonly_proof']
        if (not isinstance(proof,dict) or set(proof) != {'mount_ro','source','checked_at'}
                or proof['mount_ro'] is not True or proof['source'] != 'mountinfo'
                or not _finite(proof['checked_at']) or not 0 <= now-proof['checked_at'] <= 60.):
            raise Invalid('config_root_readonly_proof_unknown')
        if (type(p['config_root_fd']) is not int or p['config_root_fd'] < 0
                or not isinstance(p['config_root_dev_ino'],list) or len(p['config_root_dev_ino']) != 2
                or any(type(v) is not int or v < 0 for v in p['config_root_dev_ino'])):
            raise Invalid('config_root_identity_unknown')
        root = Path(root).absolute()
        if root.is_symlink():
            raise Invalid('config_root_symlink')
        fd = os.dup(p['config_root_fd'])
        info = os.fstat(fd)
        actual = root.stat()
        if (not stat.S_ISDIR(info.st_mode) or [info.st_dev,info.st_ino] != p['config_root_dev_ino']
                or (info.st_dev,info.st_ino) != (actual.st_dev,actual.st_ino)
                or fcntl.fcntl(fd,fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY):
            raise Invalid('config_root_identity_mismatch')
        if not readonly_mount(fd):
            raise Invalid('config_root_not_readonly')
        if not isinstance(refs,dict) or len(refs) > 40:
            raise Invalid('launcher_probe_scope_invalid')
        paths = {'.'}
        for ref in refs.values():
            if not isinstance(ref,dict) or not isinstance(ref.get('path'),str):
                raise Invalid('launcher_probe_scope_invalid')
            rel = PurePosixPath(ref['path'])
            if rel.is_absolute() or '..' in rel.parts or not rel.parts:
                raise Invalid('launcher_probe_scope_invalid')
            paths.add(str(rel))
            paths.update(str(parent) for parent in rel.parents)
        probes = {}
        for relative in sorted(paths):
            budget()
            try:
                answer = probe_write(relative)
            except Exception:
                raise Invalid('office_write_probe_unknown') from None
            budget()
            if answer is not False:
                raise Invalid('office_write_probe_not_false')
            probes[relative] = False
        authority = Authority(p,fd,probes)
        authority.recheck(clock())
        fd = None  # ownership transferred
        return authority
    except Invalid:
        raise
    except Exception:
        raise Invalid('launcher_policy_unknown') from None
    finally:
        if fd is not None:
            os.close(fd)
