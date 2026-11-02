"""Offline planned-stop protocol. Not wired into watchdog or deploy; never grants admission.

advance/Store.run take an ALREADY authenticated record from read_record. Host process,
health and containment observations are trusted inputs, not marker-provided claims.
"""
import copy
import fcntl
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
from uuid import UUID

MAX_BYTES = 4096
FIELDS = frozenset(('version', 'kind', 'nonce', 'boot_id', 'state_root', 'issued_boot',
                    'expires_boot', 'phase', 'old_sha', 'target_sha', 'issuer', 'pre_guard',
                    'completed_boot', 'result'))


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def uuid(value):
    return type(value) is str and str(UUID(value)) == value


def process(value):
    return (type(value) is dict and set(value) == {'pid', 'birth_ticks', 'release_sha'}
            and all(type(value[k]) is int and value[k] > 0 for k in ('pid', 'birth_ticks'))
            and type(value['release_sha']) is str and re.fullmatch('[0-9a-f]{40}', value['release_sha']))


def valid_record(r, *, boot_id, state_root, now_boot):
    try:
        return (type(r) is dict and set(r) == FIELDS and type(r['version']) is int and r['version'] == 1
            and r['kind'] in ('funded_deploy', 'office_stop') and uuid(r['nonce']) and uuid(r['boot_id'])
            and r['boot_id'] == boot_id and r['state_root'] == state_root
            and Path(state_root).is_absolute() and '..' not in Path(state_root).parts
            and number(now_boot) and number(r['issued_boot']) and number(r['expires_boot'])
            and r['issued_boot'] <= now_boot < r['expires_boot']
            and 0 < r['expires_boot'] - r['issued_boot'] <= 300
            and all(type(r[k]) is str and re.fullmatch('[0-9a-f]{40}', r[k]) for k in ('old_sha', 'target_sha'))
            and type(r['issuer']) is dict and set(r['issuer']) == {'pid', 'birth_ticks'}
            and all(type(v) is int and v > 0 for v in r['issuer'].values())
            and type(r['pre_guard']) is dict and r['pre_guard'].get('state') == 'ready'
            and r['pre_guard'].get('reason') is None and number(r['pre_guard'].get('at'))
            and r['pre_guard'].get('guard_policy_version') == 'office-host-guard-pilot-v3'
            and ('unrelated_fault_seq' not in r['pre_guard']
                 or type(r['pre_guard']['unrelated_fault_seq'])is int
                 and 0 <= r['pre_guard']['unrelated_fault_seq'] < 2**63-1)
            and r['phase'] in ('armed', 'completed', 'aborted')
            and ((r['phase'] in ('armed', 'aborted') and r['completed_boot'] is None and r['result'] is None)
                 or (r['phase'] == 'completed' and number(r['completed_boot'])
                     and r['issued_boot'] <= r['completed_boot'] <= now_boot and process(r['result'])
                     and r['result']['release_sha'] == r['target_sha'])))
    except (ValueError, TypeError, KeyError):
        return False


def verify_signature(public_key, message, signature):
    """Only public bytes reach OpenSSL; no dependency on a Python crypto package."""
    if type(public_key) is not bytes or len(public_key) != 32 or type(signature) is not bytes or len(signature) != 64:
        return False
    try:
        with tempfile.TemporaryDirectory(prefix='office-public-verify-') as directory:
            p = Path(directory)
            # RFC8410 Ed25519 SubjectPublicKeyInfo prefix, then the 32-byte public key.
            (p/'public.der').write_bytes(bytes.fromhex('302a300506032b6570032100') + public_key)
            (p/'message').write_bytes(message)
            (p/'signature').write_bytes(signature)
            result = subprocess.run(['/usr/bin/openssl', 'pkeyutl', '-verify', '-pubin', '-keyform', 'DER',
                '-inkey', str(p/'public.der'), '-rawin', '-in', str(p/'message'), '-sigfile', str(p/'signature')],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=1, close_fds=True, env={'PATH': '/usr/bin:/bin'})
            return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def read_regular(path, *, maximum=MAX_BYTES, expected_uid=None):
    path = Path(path)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('recovery_symlink')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_size > maximum or info.st_mode & 0o022
                or expected_uid is not None and info.st_uid != expected_uid):
            raise ValueError('recovery_file_identity')
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError('recovery_file_budget')
    return raw


def read_record(path, public_key, *, expected_uid=0):
    def pairs(values):
        obj = {}
        for key, value in values:
            if key in obj:
                raise ValueError('recovery_duplicate_key')
            obj[key] = value
        return obj
    try:
        doc = json.loads(read_regular(path, expected_uid=expected_uid), object_pairs_hook=pairs)
        if type(doc) is not dict or set(doc) != {'payload', 'signature'}:
            raise ValueError('recovery_envelope')
        if type(doc['payload']) is not dict or set(doc['payload']) != FIELDS:
            raise ValueError('recovery_payload_schema')
        signature = bytes.fromhex(doc['signature'])
        if not verify_signature(public_key, canonical(doc['payload']), signature):
            raise ValueError('recovery_signature')
        return doc['payload']
    except (TypeError, KeyError, json.JSONDecodeError) as exc:
        raise ValueError('recovery_envelope') from exc


def empty_state(boot_id):
    if not uuid(boot_id):
        raise ValueError('recovery_boot')
    return dict(version=1, boot_id=boot_id, watermark=-1., status='idle', accepted=None,
                healthy_since_boot=None, last_sample=None, last_beat=None, beats=0, recovery_basis=None, completion=None)


def advance(state, record, *, now_boot, now_host, boot_id, state_root, latch, healthy,
            reason, heartbeat_ts, process, issuer_alive, containment, intent_absent):
    """Pure decision; the caller MUST persist returned state before exposing ready=True.

    Signed pre_guard bytes permit an Office watchdog to return from its own stop.
    A paused/mutated latch cannot stand in for that exact pre-stop ready authority.
    Rollback completions intentionally excluded pending separate eligibility approval.
    """
    if (type(state) is not dict or set(state) != set(empty_state(boot_id))
            or state.get('boot_id') != boot_id or type(state.get('version')) is not int or state.get('version') != 1
            or state.get('status') not in ('idle', 'accepted', 'disqualified', 'consumed')
            or type(state.get('watermark')) not in (int, float) or not math.isfinite(state['watermark'])
            or state['watermark'] < -1 or type(state.get('beats')) is not int or not 0 <= state['beats'] <= 2
            or any(state.get(k) is not None and not number(state[k])
                   for k in ('last_sample', 'last_beat', 'healthy_since_boot'))):
        raise ValueError('recovery_state_unknown')
    s = copy.deepcopy(state)
    def disqualify():
        s.update(status='disqualified', healthy_since_boot=None, beats=0, recovery_basis=None)
        return s, False
    if not valid_record(record, boot_id=boot_id, state_root=state_root, now_boot=now_boot):
        return disqualify() if s['status'] == 'accepted' else (s, False)
    identity = {k:v for k,v in record.items() if k not in ('phase', 'completed_boot', 'result')}
    if s['status'] == 'accepted' and identity != s['accepted']:
        return disqualify()
    if s['status'] != 'accepted':
        if record['issued_boot'] <= s['watermark']:
            return s, False
        # No marker can rescue an existing pause or changed authority. The signed old
        # ready latch must still be byte-equivalent, even when intentionally stale.
        if (latch != record['pre_guard'] or reason is not None or healthy is not True
                or containment is not True or record['phase'] == 'aborted'):
            return s, False
        if (record['kind'] != 'office_stop' and record['phase'] != 'armed') or (
                record['phase'] == 'armed' and (not number(now_host) or not 0 <= now_host-latch['at'] <= 5)):
            return s, False
        s.update(status='accepted', accepted=identity, watermark=record['issued_boot'],
                 healthy_since_boot=None, last_sample=None, last_beat=None, beats=0, recovery_basis=None, completion=None)
    if 'unrelated_fault_seq' in record['pre_guard']:
        expected=record['pre_guard']['unrelated_fault_seq']
        if (type(latch)is not dict or type(latch.get('unrelated_fault_seq'))is not int
                or latch['unrelated_fault_seq']!=expected):
            return disqualify()
    allowed = {'funded_heartbeat'} if record['kind'] == 'funded_deploy' else {'guard_stale'}
    if (type(latch) is not dict or latch.get('state') not in ('ready', 'paused')
            or not number(latch.get('at')) or latch.get('state') == 'ready' and latch.get('reason') is not None
            or record['kind'] == 'office_stop' and latch != record['pre_guard']
            or containment is not True or record['phase'] == 'aborted'
            or reason is not None and reason not in allowed
            or (latch.get('state') == 'paused' and latch.get('reason') not in allowed)
            or (record['phase'] == 'armed' and issuer_alive is not True)):
        return disqualify()
    if record['phase'] == 'armed':
        return disqualify() if s['completion'] is not None else (s, False)
    completion = dict(completed_boot=record['completed_boot'], result=record['result'])
    if s['completion'] is not None and s['completion'] != completion:
        return disqualify()
    s['completion'] = completion
    if (process != record['result'] or intent_absent is not True or healthy is not True
            or not number(now_host) or not number(heartbeat_ts) or not 0 <= now_host-heartbeat_ts <= 5):
        return disqualify()
    if s['last_sample'] is not None:
        if now_boot < s['last_sample'] or now_boot-s['last_sample'] > 5:
            return disqualify()
        if now_boot == s['last_sample']:
            return s, False
    if s['last_beat'] is not None and heartbeat_ts < s['last_beat']:
        return disqualify()
    if s['healthy_since_boot'] is None:
        s['healthy_since_boot'] = now_boot
    s['last_sample'] = now_boot
    if s['last_beat'] is None or heartbeat_ts > s['last_beat']:
        s['last_beat'] = heartbeat_ts
        s['beats'] = min(2, s['beats'] + 1)
    if now_boot-s['healthy_since_boot'] >= 5 and s['beats'] >= 2:
        s.update(status='consumed', recovery_basis='planned_stop_verified')
        return s, True
    return s, False


def atomic(path, value):
    raw = canonical(value)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.recovery-', delete=False) as out:
            tmp = Path(out.name)
            os.chmod(tmp, 0o600)
            out.write(raw); out.flush(); os.fsync(out.fileno())
        os.replace(tmp, path)
        fd = os.open(path.parent, os.O_DIRECTORY | os.O_NOFOLLOW)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        if tmp is not None: tmp.unlink(missing_ok=True)


class Store:
    """Offline single-writer durable consumption; lock contention/read/write failure raises.

    No auto-initialization: deleting state never erases a replay watermark implicitly.
    Host activation must explicitly initialize BEFORE any issuer can arm a record.
    """
    def __init__(self, path, boot_id):
        self.path, self.boot_id = Path(path), boot_id

    def initialize(self):
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as out:
            out.write(canonical(empty_state(self.boot_id))); out.flush(); os.fsync(out.fileno())
        fd = os.open(self.path.parent, os.O_DIRECTORY | os.O_NOFOLLOW)
        try: os.fsync(fd)
        finally: os.close(fd)

    def run(self, record, **observed):
        fd = os.open(self.path.with_suffix(self.path.suffix+'.lock'),
                     os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        with os.fdopen(fd, 'w') as lock:
            info = os.fstat(lock.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError('recovery_lock_identity')
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try: state = json.loads(read_regular(self.path))
            except (OSError, ValueError) as exc: raise ValueError('recovery_state_unknown') from exc
            state, ready = advance(state, record, **observed)
            atomic(self.path, state)
            return ready
