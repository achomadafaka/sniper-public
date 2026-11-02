import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / 'scripts/deploy_memecoin_runtime.sh'


def function(name):
    text = DEPLOY.read_text()
    body = text[text.index('\n' + name + '() {')+1:]
    return body[:body.index('\n}\n')+3]


@pytest.mark.parametrize('via_cron', [False, True])
@pytest.mark.parametrize('cron_ready', [False, True])
def test_rh_start_honors_deploy_cgroup_handoff(tmp_path, via_cron, cron_ready):
    scripts = tmp_path / 'scripts'; scripts.mkdir()
    events = tmp_path / 'events'
    (scripts / 'memecoin_rh_feed_shadow_watchdog.sh').write_text(
        'printf "direct\\n" >> "$TEST_CALLS"\n')
    harness = '\n'.join(['set -u', f'STATE_ROOT={shlex.quote(str(tmp_path))}', 'PY=unused',
        f'LAUNCH_VIA_CRON={int(via_cron)}',
        'wait_for_provider_rh_cron_launch() { printf "cron\\n" >> "$TEST_CALLS"; return '
            + str(0 if cron_ready else 1) + '; }',
        function('start_provider_rh'), f'start_provider_rh {shlex.quote(str(tmp_path))}'])
    result = subprocess.run(['bash', '-c', harness], capture_output=True, text=True, timeout=10,
        env=os.environ | dict(TEST_CALLS=str(events)))
    assert result.returncode == (1 if via_cron and not cron_ready else 0), result.stderr
    assert events.read_text().splitlines() == ['cron' if via_cron else 'direct']


@pytest.mark.parametrize('ready', [False, True])
@pytest.mark.parametrize('retake_ok', [False, True])
def test_rh_cron_wait_releases_and_retakes_lock_on_every_path(tmp_path, ready, retake_ok):
    assert 'wait_for_provider_rh_cron_launch() {' in DEPLOY.read_text(), 'RH cron handoff missing'
    (tmp_path / 'data').mkdir()
    lock = tmp_path / 'data/memecoin_rh_feed_shadow.watchdog.lock'
    events = tmp_path / 'events'
    fake = tmp_path / 'python'
    fake.write_text('''#!/usr/bin/env python3
import fcntl, os, pathlib, sys
root=pathlib.Path(os.environ['TEST_STATE']); events=root/'events'
with (root/'data/memecoin_rh_feed_shadow.watchdog.lock').open('a') as lock:
    try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError: raise SystemExit('cron lock remained held')
    with events.open('a') as log: log.write('lock_released\\n')
for name in ('runner.lock', 'deploy.lock'):
    with (root/name).open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: continue
        raise SystemExit(name+' was released during RH handoff')
if os.environ['READY']=='1': print('42 7000')
else: sys.exit(1)
''')
    fake.chmod(0o755)
    harness = '\n'.join(['set -u', f'PY={fake}', f'STATE_ROOT={tmp_path}',
        f'DEFAULT_SOURCE={tmp_path}', 'MEMECOIN_DEPLOY_CRON_WAIT_S=1; PROVIDER_RECOVERY_REQUIRED=0',
        f'exec 7>{lock}; command flock -n 7',
        f'exec 8>{tmp_path / "runner.lock"}; command flock -n 8',
        f'exec 9>{tmp_path / "deploy.lock"}; command flock -n 9',
        'step() { printf "%s\\n" "$*" >> "$TEST_STATE/events"; }',
        f'date() {{ if [ ! -e {tmp_path / "clock"} ]; then touch {tmp_path / "clock"}; echo 100; '
            f'elif [ ! -e {tmp_path / "tick"} ]; then touch {tmp_path / "tick"}; echo 100; else echo 200; fi; }}',
        'sleep() { :; }',
        'flock() { printf "retake\\n" >> "$TEST_STATE/events"; '
            + ('command flock -n 7;' if retake_ok else 'return 1;') + ' }',
        function('retake_provider_rh_watchdog_lock'),
        function('wait_for_provider_rh_cron_launch'),
        f'wait_for_provider_rh_cron_launch {tmp_path / "release"}; rc=$?',
        'printf "rc=%s recovery=%s\\n" "$rc" "$PROVIDER_RECOVERY_REQUIRED"',
        f'if (exec 6>{lock}; command flock -n 6); then echo unlocked; else echo held; fi'])
    result = subprocess.run(['bash', '-c', harness], capture_output=True, text=True, timeout=10,
        env=os.environ | dict(TEST_STATE=str(tmp_path), READY=str(int(ready))))
    assert result.returncode == 0, result.stderr
    assert f'rc={0 if ready and retake_ok else 1} recovery={0 if retake_ok else 1}' in result.stdout
    assert ('held' in result.stdout) is retake_ok
    lines = events.read_text().splitlines()
    assert lines[0].startswith('rh_launch_handed_to_cron release=')
    assert lines[1:3] == ['lock_released', 'retake']
    assert any(line.startswith('rh_launched_by_cron pid=42') for line in lines) is (ready and retake_ok)


@pytest.mark.parametrize('case', ['ready', 'app_scope', 'wrong_path', 'duplicate', 'wrong_pid',
    'stale', 'before_start', 'birth_missing', 'birth_changed', 'membership_changed',
    'heartbeat_replaced', 'heartbeat_future'])
def test_rh_cron_identity_probe_binds_process_birth_scope_and_fresh_heartbeat(tmp_path, monkeypatch, capsys, case):
    import json
    from scripts import memecoin_runtime_verify as verify
    assert 'wait_for_provider_rh_cron_launch() {' in DEPLOY.read_text(), 'RH cron handoff missing'
    body = function('wait_for_provider_rh_cron_launch')
    probe = body.split("<<'PY'\n", 1)[1].split('\nPY', 1)[0]
    state, code = tmp_path / 'state', tmp_path / 'release'
    (state / 'data').mkdir(parents=True)
    expected = str(code / 'scripts/memecoin_rh_feed_shadow_loop.py')
    now = time.time()
    stamp = now - (30 if case == 'stale' else 2 if case == 'before_start' else 0)
    (state / 'data/memecoin_rh_feed_shadow.heartbeat').write_text(json.dumps(
        dict(ts=stamp, pid=43 if case == 'wrong_pid' else 42)))
    rows = [(42, expected if case != 'wrong_path' else '/other/scripts/memecoin_rh_feed_shadow_loop.py')]
    if case == 'duplicate': rows.append((43, expected))
    calls = []
    def owned(*args):
        calls.append(args)
        return [] if case == 'membership_changed' and len(calls) > 2 else rows
    monkeypatch.setattr(verify, '_owned_script_rows', owned)
    births = iter([None if case == 'birth_missing' else '7000', '7001' if case == 'birth_changed' else '7000'])
    monkeypatch.setattr(verify, '_process_start_time', lambda *_: next(births))
    read = Path.read_text
    beat_reads = []
    def read_text(path, *args, **kwargs):
        if str(path) == '/proc/42/cgroup':
            return '0::/user.slice/app-chromium.scope\n' if case == 'app_scope' else '0::/system.slice/cron.service\n'
        if path.name == 'memecoin_rh_feed_shadow.heartbeat' and case in ('heartbeat_replaced', 'heartbeat_future'):
            beat_reads.append(path)
            value = json.loads(read(path, *args, **kwargs))
            if len(beat_reads) > 1:
                value.update(pid=43) if case == 'heartbeat_replaced' else value.update(ts=now + 60)
            return json.dumps(value)
        return read(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', read_text)
    monkeypatch.setattr(sys, 'argv', ['probe', str(DEPLOY.parents[1]), str(code), str(state), str(now - 1)])
    if case == 'ready':
        exec(compile(probe, str(DEPLOY), 'exec'), {})
        assert capsys.readouterr().out.strip() == '42 7000'
    else:
        with pytest.raises(SystemExit):
            exec(compile(probe, str(DEPLOY), 'exec'), {})


@pytest.mark.parametrize('retake_ok', [False, True])
def test_interrupted_rh_handoff_relocks_before_rollback_mutation(tmp_path, retake_ok):
    events = tmp_path / 'events'
    intent = tmp_path / 'intent'; intent.write_text('unfinished deploy\n')
    operator_kill = tmp_path / 'kill'; operator_kill.write_text('operator owns this pause\n')
    harness = f'''set -euo pipefail
SUCCESS=0; SWITCHED=0; SHYFT_RUNTIME=1; PROVIDER_ENV=stage; PROVIDER_RECOVERY_REQUIRED=0
PROVIDER_CANDIDATE_LAUNCHED=1; PROVIDER_RH_CRON_HANDOFF=1; SNIPER_DEPLOY_NO_PROCESS=0
ROLLOVER_OLD_RUNNER_STOPPED=0; CANDIDATE_MANIFEST=''; OLD_MANIFEST=''; CURRENT=''
ROLLOVER_ENV_BACKUP=''; LIFI_ENV_ROLLBACK=''; DEPLOY_INTENT={intent}
retake_provider_rh_watchdog_lock() {{ echo retake >> {events}; return {0 if retake_ok else 1}; }}
stop_provider_processes() {{ echo stop >> {events}; }}
restore_lifi_env() {{ :; }}
restore_rollover_env() {{ :; }}
restore_provider_env() {{ echo restore >> {events}; }}
clear_deploy_entry_gate() {{ echo clear >> {events}; }}
cleanup_provider_stage() {{ :; }}
{function('rollback')}
false || rollback
'''
    result = subprocess.run(['bash', '-c', harness], capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert events.read_text().splitlines() == (['retake', 'stop', 'restore', 'clear'] if retake_ok else ['retake'])
    assert operator_kill.read_text() == 'operator owns this pause\n'
    assert intent.exists() is not retake_ok


@pytest.mark.parametrize('stop_ok', [True, False])
def test_rh_cutover_captures_expected_old_identity_before_stop(tmp_path, stop_ok):
    (tmp_path / 'data').mkdir()
    fake = tmp_path / 'python'
    fake.write_text('''#!/usr/bin/env python3
import os, pathlib, sys
p=pathlib.Path(os.environ['TEST_CALLS']); a=sys.argv[1:]
with p.open('a') as f: f.write(' '.join(a)+'\\n')
if '--process-record-script' in a: print('{}')
if '--stop-process-record' in a: sys.exit(0 if os.environ['STOP_OK']=='1' else 1)
''')
    fake.chmod(0o755)
    harness = '\n'.join(['set -u', f'PY={fake}', f'STATE_ROOT={tmp_path}',
        f'RELEASE={tmp_path / "new"}', f'OLD_RELEASE={tmp_path / "old"}',
        f'SOURCE_ROOT={tmp_path / "source"}', 'PROVIDER_RH_RECORD=""; PROVIDER_RH_STOPPED=0',
        function('prepare_provider_rh_cutover'), 'prepare_provider_rh_cutover'])
    r = subprocess.run(['bash', '-c', harness], capture_output=True, text=True, timeout=10,
        env=os.environ | dict(TEST_CALLS=str(tmp_path / 'calls'), STOP_OK=str(int(stop_ok))))
    assert r.returncode == (0 if stop_ok else 1), r.stderr
    calls = (tmp_path / 'calls').read_text().splitlines()
    assert '--process-record-script scripts/memecoin_rh_feed_shadow_loop.py' in calls[0]
    assert '--record-expected-root ' + str(tmp_path / 'old') in calls[0]
    assert '--stop-process-record' in calls[1]


def test_actual_graph_gate_failure_is_not_runner_ready(tmp_path):
    fake = tmp_path / 'python'
    fake.write_text('#!/bin/bash\nexit 1\n'); fake.chmod(0o755)
    harness = '\n'.join(['set -u', f'PY={fake}', f'STATE_ROOT={tmp_path}',
        f'RELEASE={tmp_path}', 'SHYFT_RUNTIME=1', function('shyft_runtime_gate'),
        function('provider_runtime_ready'), 'provider_runtime_ready'])
    r = subprocess.run(['bash', '-c', harness], capture_output=True, text=True, timeout=10)
    assert r.returncode == 1


@pytest.mark.parametrize('old_shyft', [False, True])
@pytest.mark.parametrize('graph_ready', [False, True])
def test_rollback_to_shyft_requires_all_roles_before_clearing_hold(tmp_path, old_shyft, graph_ready):
    (tmp_path / '.env').write_text('MEMECOIN_SOLANA_INFRA_PROVIDER=' + ('shyft' if old_shyft else 'legacy') + '\n')
    fake = tmp_path / 'python'
    fake.write_text('''#!/usr/bin/env python3
import os, pathlib, sys
a=sys.argv[1:]
with pathlib.Path(os.environ['TEST_CALLS']).open('a') as f: f.write(' '.join(a)+'\\n')
if '--shyft-runtime-only' in a: sys.exit(0 if os.environ['GRAPH_READY']=='1' else 1)
''')
    fake.chmod(0o755)
    harness = '\n'.join(['set -u', f'PY={fake}', f'STATE_ROOT={tmp_path}',
        f'RELEASE={tmp_path / "new"}', f'OLD_RELEASE={tmp_path / "old"}', f'DEFAULT_SOURCE={tmp_path}',
        'PROVIDER_RH_STOPPED=1; PROVIDER_ROLLBACK_STARTED=100',
        f'date() {{ if [ -e {tmp_path / "clock"} ]; then echo 200; else touch {tmp_path / "clock"}; echo 100; fi; }}',
        function('shyft_runtime_gate'), function('verify_provider_rollback'), 'verify_provider_rollback'])
    # The first date sets the deadline; the next admits one iteration, then the fake clock expires.
    harness = harness.replace('then echo 200;', f'then if [ -e {tmp_path / "tick"} ]; then echo 200; else touch {tmp_path / "tick"}; echo 100; fi;')
    r = subprocess.run(['bash', '-c', harness], capture_output=True, text=True, timeout=10,
        env=os.environ | dict(TEST_CALLS=str(tmp_path / 'calls'), GRAPH_READY=str(int(graph_ready))))
    assert r.returncode == (1 if old_shyft and not graph_ready else 0), r.stderr
    calls = (tmp_path / 'calls').read_text()
    assert ('--shyft-runtime-only' in calls) is old_shyft
