"""Deployment boundaries: real temporary files, mocked service manager only."""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHA = 'a' * 40
OLD_SHA = 'b' * 40
AT = '2026-10-04T16:00:00+00:00'


def module():
    path = ROOT / 'scripts/deploy_wallet_universe_metrics.py'
    assert path.exists(), 'dedicated measurement deployment helper missing'
    spec = importlib.util.spec_from_file_location('metrics_deploy', path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def artifact(pid=222, **changes):
    body = dict(version=1, generated_at=AT, collection=dict(
        state='collecting', release_sha=SHA, worker_pid=pid,
        source=dict(census_valid=True), started_at=AT))
    body.update(changes)
    return body


def test_readiness_requires_new_worker_and_valid_census():
    m = module()
    assert m.ready_artifact(artifact(), 222, SHA, not_before=AT, now=AT)
    for change in ({'worker_pid': 333}, {'release_sha': OLD_SHA},
                   {'source': {'census_valid': False}}, {'state': 'failed'}):
        body = artifact(); body['collection'].update(change)
        assert not m.ready_artifact(body, 222, SHA, not_before=AT, now=AT)
    for stamp in ('2026-10-04T15:59:59+00:00', '2026-10-04T17:00:00+00:00', 'bad'):
        assert not m.ready_artifact(artifact(generated_at=stamp), 222, SHA, not_before=AT, now=AT)
    assert not m.ready_artifact([], 222, SHA, not_before=AT, now=AT)


def test_owned_command_is_bound_to_our_state_and_worker(tmp_path):
    m = module(); state = tmp_path.resolve()
    cmd = ['/usr/bin/python3', '-u', str(state / '.runtime/wallet-universe-metrics/current' / m.ENTRY),
           '--loop', '--release-sha', SHA, '--state-root', str(state)]
    assert m.owned_command(cmd, state)
    assert not m.owned_command(['/usr/bin/python3', '/tmp/wallet_universe_metrics.py', '--loop'], state)
    assert not m.owned_command(cmd[:-1] + ['/different/state'], state)
    assert not m.owned_command(['/bin/bash', '-c', ' '.join(cmd)], state)
    assert not m.owned_command(cmd[:2] + [cmd[2].replace('wallet-universe-metrics', 'memecoin')] + cmd[3:], state)


def test_validation_requires_full_merged_sha_and_real_source(monkeypatch, tmp_path):
    m = module(); source = tmp_path / 'sniper_tracker.db'
    with pytest.raises(ValueError): m.validate_request('main', tmp_path, source)
    with pytest.raises(ValueError): m.validate_request(SHA, tmp_path, source)
    source.touch(); calls = []
    def refuse(*args, **kwargs):
        calls.append(args)
        raise subprocess.CalledProcessError(1, args)
    monkeypatch.setattr(m, 'run', refuse)
    with pytest.raises(subprocess.CalledProcessError): m.validate_request(SHA, tmp_path, source)
    assert calls == [('git', '-C', str(tmp_path), 'merge-base', '--is-ancestor', SHA, 'origin/main')]


def test_manual_observer_lock_is_refused(tmp_path):
    m = module(); (tmp_path / 'data').mkdir()
    with (tmp_path / 'data/wallet_universe_metrics.lock').open('a') as fd:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match='observer'):
            m.check_observer_lock(tmp_path, allowed_pid=0)
        m.check_observer_lock(tmp_path, allowed_pid=os.getpid())


def test_clock_read_does_not_mutate_process_import_path(tmp_path, monkeypatch):
    m = module(); (tmp_path / 'data').mkdir()
    (tmp_path / 'data/clock_offset.json').write_text('{"offset_s":3600}')
    prior = list(sys.path)
    monkeypatch.setattr(sys, 'path', prior.copy())
    stamp = m.utc_now(tmp_path)
    assert sys.path == prior
    assert abs(m._instant(stamp) - (m.time.time() - 3600)) < 2


class Host:
    def __init__(self, m, tmp_path, *, prior=False, active=False, enabled=False, fail=None):
        self.m = m; self.state = tmp_path / 'state'; self.units = tmp_path / 'units'
        self.runtime = self.state / '.runtime/wallet-universe-metrics'
        self.release = self.runtime / 'releases' / SHA
        (self.release / m.ENTRY).parent.mkdir(parents=True)
        (self.release / m.ENTRY).write_text('# fixture')
        (self.state / 'data').mkdir(); (self.state / 'sniper_tracker.db').touch(); self.units.mkdir()
        self.active = active; self.enabled = enabled; self.pid = 111 if active else 0
        self.calls = []; self.fail = fail; self.failed = False
        self.old = self.runtime / 'releases' / OLD_SHA; self.old.mkdir()
        self.unit = self.units / m.UNIT
        if prior:
            (self.runtime / 'current').symlink_to(self.old)
            self.unit.write_text(m.MARKER + '\n# prior unit\n')
            (self.runtime / 'manifest.json').write_text('{"old":true}')
            (self.state / 'data/wallet_universe_metrics.json').write_text('{"old":true}')

    def run(self, *args, **kwargs):
        self.calls.append(args)
        out = ''
        if args[0] == 'git':
            if 'rev-parse' in args: out = SHA
            if 'status' in args and self.fail == 'dirty': out = '?? injected.py\n'
        if args[0] == 'systemd-analyze' and self.fail == 'verify': raise ValueError('unit invalid')
        if args[0] == 'systemctl':
            action = args[2]
            assert action == 'daemon-reload' or args[-1] == self.m.UNIT
            if action == 'show':
                out = '\n'.join([f'MainPID={self.pid}', f'ActiveState={"active" if self.active else "inactive"}',
                    f'UnitFileState={"enabled" if self.enabled else "disabled"}',
                    f'FragmentPath={self.unit if self.unit.exists() else ""}'])
            elif action == 'stop': self.active = False; self.pid = 0
            elif action == 'disable': self.enabled = False
            elif action == 'enable': self.enabled = True
            elif action == 'start':
                if self.fail == 'start' and not self.failed:
                    self.failed = True; raise ValueError('start failed')
                self.active = True; self.pid = 222
                body = artifact()
                (self.state / 'data/wallet_universe_metrics.json').write_text(json.dumps(body))
            elif action != 'daemon-reload': raise AssertionError(args)
        return subprocess.CompletedProcess(args, 0, out, '')

    def install(self, monkeypatch):
        monkeypatch.setattr(self.m, 'run', self.run)
        monkeypatch.setattr(self.m, 'utc_now', lambda state: AT)
        monkeypatch.setattr(self.m, 'process_command', lambda pid: ['/usr/bin/python3', '-u',
            str(self.runtime / 'current' / self.m.ENTRY), '--loop', '--release-sha', OLD_SHA if pid == 111 else SHA, '--state-root', str(self.state)])


def test_first_activation_is_separate_pinned_and_prevalidated(monkeypatch, tmp_path):
    m = module(); h = Host(m, tmp_path); h.install(monkeypatch)
    got = m.deploy(SHA, repo=tmp_path, state=h.state, unit_dir=h.units)
    assert got['pid'] == 222 and got['collection_state'] == 'collecting'
    assert (h.runtime / 'current').resolve() == h.release
    assert json.loads((h.runtime / 'manifest.json').read_text())['sha'] == SHA
    text = h.unit.read_text()
    for item in ('Nice=19', 'MemoryMax=512M', 'TasksMax=16', 'CPUWeight=10', 'IOWeight=10',
                 'RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6', '--state-root', '--release-sha'):
        assert item in text
    assert all('memecoin/current' not in str(c) and 'backup' not in str(c) for c in h.calls)
    verify = next(i for i,c in enumerate(h.calls) if c[0] == 'systemd-analyze')
    start = next(i for i,c in enumerate(h.calls) if c[:3] == ('systemctl', '--user', 'start'))
    assert verify < start and h.active and h.enabled


@pytest.mark.parametrize('failure', ['dirty', 'verify'])
def test_invalid_release_or_unit_never_stops_old_service(monkeypatch, tmp_path, failure):
    m = module(); h = Host(m, tmp_path, prior=True, active=True, enabled=True, fail=failure); h.install(monkeypatch)
    with pytest.raises(ValueError): m.deploy(SHA, repo=tmp_path, state=h.state, unit_dir=h.units)
    assert h.active and h.enabled and (h.runtime / 'current').resolve() == h.old
    assert not any(c[:3] == ('systemctl', '--user', 'stop') for c in h.calls)


@pytest.mark.parametrize('active,enabled', [(False,False), (False,True), (True,False), (True,True)])
def test_failure_restores_prior_files_and_service_state(monkeypatch, tmp_path, active, enabled):
    m = module(); h = Host(m, tmp_path, prior=True, active=active, enabled=enabled, fail='start'); h.install(monkeypatch)
    old_unit = h.unit.read_bytes(); old_manifest = (h.runtime / 'manifest.json').read_bytes()
    with pytest.raises(ValueError, match='start failed'): m.deploy(SHA, repo=tmp_path, state=h.state, unit_dir=h.units)
    assert h.active == active and h.enabled == enabled
    assert h.unit.read_bytes() == old_unit and (h.runtime / 'manifest.json').read_bytes() == old_manifest
    assert (h.runtime / 'current').resolve() == h.old
    if not active: assert json.loads((h.state / 'data/wallet_universe_metrics.json').read_text()) == {'old':True}


def test_first_activation_failure_removes_only_owned_state(monkeypatch, tmp_path):
    m = module(); h = Host(m, tmp_path, fail='start'); h.install(monkeypatch)
    with pytest.raises(ValueError): m.deploy(SHA, repo=tmp_path, state=h.state, unit_dir=h.units)
    assert not h.active and not h.enabled and not h.unit.exists()
    assert not (h.runtime / 'current').exists() and not (h.runtime / 'manifest.json').exists()


def test_wrong_running_process_is_not_stopped(monkeypatch, tmp_path):
    m = module(); h = Host(m, tmp_path, prior=True, active=True); h.install(monkeypatch)
    monkeypatch.setattr(m, 'process_command', lambda pid: ['python3', 'unrelated.py'])
    with pytest.raises(ValueError, match='identity'): m.deploy(SHA, repo=tmp_path, state=h.state, unit_dir=h.units)
    assert h.active and not any(c[:3] == ('systemctl', '--user', 'stop') for c in h.calls)
