#!/usr/bin/env python3
"""Deploy only the pinned Wallet universe observer; no backups or trading actions.

Readiness proves a new worker read its source census, not completed measurements.
Owned prior files and enabled/active service state are restored on activation failure.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

ENTRY = 'scripts/research/wallet_universe_metrics.py'
UNIT = 'sniper-wallet-universe-metrics.service'
MARKER = '# Managed by Sniper wallet universe metrics deploy.'


def run(*args, **kwargs):
    kwargs.setdefault('timeout', 30)
    return subprocess.run(list(args), check=True, capture_output=True, text=True, **kwargs)


def validate_request(sha, repo, source):
    if not re.fullmatch('[0-9a-f]{40}', sha): raise ValueError('full lowercase SHA required')
    if not source.is_file(): raise ValueError('source ledger missing')
    run('git', '-C', str(repo), 'merge-base', '--is-ancestor', sha, 'origin/main')


def process_command(pid):
    return [s.decode() for s in Path('/proc', str(pid), 'cmdline').read_bytes().split(b'\0') if s]


def owned_command(argv, state):
    if not argv or not re.fullmatch(r'python(?:\d+(?:\.\d+)*)?', Path(argv[0]).name): return False
    rest = argv[1:]
    if rest and rest[0] == '-u': rest = rest[1:]
    if not rest: return False
    try:
        parts = Path(rest[0]).relative_to(state / '.runtime/wallet-universe-metrics').parts
        valid = parts == ('current', *Path(ENTRY).parts) or (
            len(parts) == 5 and parts[0] == 'releases' and re.fullmatch('[0-9a-f]{40}', parts[1])
            and parts[2:] == Path(ENTRY).parts)
        args = rest[1:]
        return bool(valid and args.count('--loop') == 1 and args.count('--state-root') == 1
                    and args.count('--release-sha') == 1
                    and Path(args[args.index('--state-root') + 1]).resolve() == state.resolve()
                    and re.fullmatch('[0-9a-f]{40}', args[args.index('--release-sha') + 1]))
    except (ValueError, IndexError): return False


def check_observer_lock(state, *, allowed_pid=0):
    path = state / 'data/wallet_universe_metrics.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            stat = os.fstat(lock.fileno())
            for line in Path('/proc/locks').read_text().splitlines():
                fields = line.split()
                if len(fields) < 6 or fields[1] != 'FLOCK': continue
                try:
                    major, minor, inode = fields[5].split(':')
                    matches = (int(major, 16), int(minor, 16), int(inode)) == (
                        os.major(stat.st_dev), os.minor(stat.st_dev), stat.st_ino)
                    if matches and allowed_pid > 0 and int(fields[4]) == allowed_pid: return
                except (ValueError, IndexError): continue
            raise ValueError('manual or unidentified observer holds measurement lock') from None


def _instant(value):
    dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if dt.tzinfo is None: raise ValueError('naive clock')
    return dt.timestamp()


def utc_now(state):
    # No environment mutation: corrected-clock state is explicit even in a worktree.
    try:
        offset = float(json.loads((state / 'data/clock_offset.json').read_text()).get('offset_s', 0))
        if not math.isfinite(offset): raise ValueError('invalid clock offset')
    except (OSError, ValueError, TypeError, AttributeError): offset = 0
    return datetime.fromtimestamp(time.time() - offset, timezone.utc).isoformat()


def ready_artifact(body, pid, sha, *, not_before, now):
    try:
        c = body['collection']
        return bool(body['version'] == 1 and pid > 0 and c['worker_pid'] == pid
                    and c['release_sha'] == sha and c['state'] in ('collecting', 'ok')
                    and c['source']['census_valid'] is True
                    and _instant(not_before) <= _instant(body['generated_at']) <= _instant(now) + 5
                    and _instant(not_before) <= _instant(c['started_at']) <= _instant(now) + 5)
    except (KeyError, TypeError, ValueError): return False


def _atomic(path, content):
    if content is None:
        path.unlink(missing_ok=True); return
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(content); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _link(path, target):
    if target is None:
        path.unlink(missing_ok=True); return
    pending = path.with_name(path.name + '.next')
    pending.unlink(missing_ok=True)
    try:
        pending.symlink_to(target); os.replace(pending, path)
    finally: pending.unlink(missing_ok=True)


def _service(**kwargs):
    raw = run('systemctl', '--user', 'show', '--property=MainPID,ActiveState,UnitFileState,FragmentPath', UNIT, **kwargs).stdout
    values = dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)
    return dict(pid=int(values.get('MainPID') or 0), active=values.get('ActiveState') == 'active',
                active_state=values.get('ActiveState'), enabled=values.get('UnitFileState'),
                fragment=values.get('FragmentPath'))


def _quoted(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def deploy(sha, *, repo, state, unit_dir=None):
    state = Path(state).resolve(); repo = Path(repo).resolve()
    if any(c in str(state) for c in '\n\r'): raise ValueError('invalid state path')
    validate_request(sha, repo, state / 'sniper_tracker.db')
    runtime = state / '.runtime/wallet-universe-metrics'; runtime.mkdir(parents=True, exist_ok=True)
    unit_dir = Path(unit_dir) if unit_dir else Path.home() / '.config/systemd/user'
    unit_dir.mkdir(parents=True, exist_ok=True)
    with (runtime / 'deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        current = runtime / 'current'; unit = unit_dir / UNIT; manifest = runtime / 'manifest.json'
        artifact = state / 'data/wallet_universe_metrics.json'
        if current.exists() and not current.is_symlink(): raise ValueError('current is not an owned symlink')
        previous = os.readlink(current) if current.is_symlink() else None
        old = {p: p.read_bytes() if p.exists() else None for p in (unit, manifest, artifact)}
        if old[unit] is not None and not old[unit].startswith(MARKER.encode() + b'\n'):
            raise ValueError('existing unit is not owned')
        before = _service()
        if before['enabled'] not in ('enabled', 'disabled', 'not-found', '', None):
            raise ValueError('unsupported prior unit enablement state')
        if before['active_state'] not in ('active', 'inactive', 'failed'):
            raise ValueError('service state is transitional or unavailable')
        if before['fragment'] and Path(before['fragment']).resolve() != unit.resolve():
            raise ValueError('service fragment is not owned')
        if before['pid'] and (old[unit] is None or not owned_command(process_command(before['pid']), state)):
            raise ValueError('service PID identity mismatch')
        if before['active'] and not before['pid']: raise ValueError('active service has no worker PID')
        check_observer_lock(state, allowed_pid=before['pid'])
        release = runtime / 'releases' / sha; release.parent.mkdir(exist_ok=True)
        if not release.exists(): run('git', '-C', str(repo), 'worktree', 'add', '--detach', str(release), sha)
        if run('git', '-C', str(release), 'rev-parse', 'HEAD').stdout.strip() != sha:
            raise ValueError('release SHA mismatch')
        if run('git', '-C', str(release), 'status', '--porcelain', '--untracked-files=all').stdout.strip():
            raise ValueError('dirty pinned release')
        if not (release / ENTRY).is_file(): raise ValueError('release worker absent')
        run(sys.executable, '-B', str(release / ENTRY), '--help', cwd=release)
        text = MARKER + f'''
[Unit]
Description=Sniper Wallet universe measurements
[Service]
Type=simple
WorkingDirectory={str(state).replace('%', '%%')}
Environment={_quoted('SNIPER_STATE_ROOT=' + str(state))}
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart={_quoted(sys.executable)} -u {_quoted(current / ENTRY)} --loop --release-sha {sha} --state-root {_quoted(state)}
Restart=on-failure
RestartSec=5
Nice=19
CPUWeight=10
IOWeight=10
MemoryMax=512M
TasksMax=16
NoNewPrivileges=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
UMask=0077
[Install]
WantedBy=default.target
'''
        with tempfile.TemporaryDirectory(prefix='unit-validation-', dir=runtime) as folder:
            candidate = Path(folder) / UNIT; candidate.write_text(text)
            run('systemd-analyze', '--user', 'verify', str(candidate))
        touched = False
        try:
            # Set before stop: even a failed/timeout stop may have stopped the worker.
            touched = True
            if old[unit] is not None: run('systemctl', '--user', 'stop', UNIT)
            check_observer_lock(state)
            activated_at = utc_now(state)
            _link(current, release); _atomic(unit, text.encode())
            run('systemctl', '--user', 'daemon-reload')
            run('systemctl', '--user', 'enable', UNIT)
            run('systemctl', '--user', 'start', UNIT)
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                live = _service(timeout=max(.1, deadline - time.monotonic()))
                try:
                    body = json.loads(artifact.read_text()) if artifact.stat().st_size <= 4 * 1024 * 1024 else {}
                except (OSError, ValueError): body = {}
                if live['active'] and ready_artifact(body, live['pid'], sha, not_before=activated_at, now=utc_now(state)):
                    command = process_command(live['pid'])
                    if not owned_command(command, state) or command[command.index('--release-sha') + 1] != sha:
                        raise ValueError('new service PID identity mismatch')
                    break
                time.sleep(min(.5, max(0, deadline - time.monotonic())))
            else: raise ValueError('new worker did not publish same-PID census readiness')
            _atomic(manifest, json.dumps(dict(sha=sha, pid=live['pid'], entry=ENTRY,
                state_root=str(state), previous=previous, collection_state=body['collection']['state'])).encode())
            return dict(sha=sha, pid=live['pid'], collection_state=body['collection']['state'])
        except Exception as exc:
            if touched:
                errors = []
                def restore(fn, *args):
                    try: fn(*args)
                    except Exception as failure: errors.append(str(failure))
                restore(run, 'systemctl', '--user', 'stop', UNIT)
                restore(run, 'systemctl', '--user', 'disable', UNIT)
                restore(_link, current, previous)
                for path, content in old.items(): restore(_atomic, path, content)
                restore(run, 'systemctl', '--user', 'daemon-reload')
                if before['enabled'] == 'enabled': restore(run, 'systemctl', '--user', 'enable', UNIT)
                if before['active']: restore(run, 'systemctl', '--user', 'start', UNIT)
                if errors: raise RuntimeError('deployment failed; rollback incomplete: ' + '; '.join(errors)) from exc
            raise


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('sha'); p.add_argument('--state-root', type=Path, required=True)
    p.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument('--dry-run', action='store_true'); args = p.parse_args()
    if args.dry_run:
        validate_request(args.sha, args.source_root, args.state_root / 'sniper_tracker.db')
        print(json.dumps(dict(sha=args.sha, mutated=False))); return 0
    print(json.dumps(deploy(args.sha, repo=args.source_root, state=args.state_root))); return 0


if __name__ == '__main__': raise SystemExit(main())
