"""Run the actual watchdog handover with every process and signal operation substituted."""
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest


def run(tmp_path, *, ready=True, stop_ok=True, marker=True, daily=False, age=5,
        candidate_present=True, abort_ok=True, remove_ok=True):
    root = tmp_path / '.runtime/memecoin/releases/new'
    script = str(root / 'scripts/research/onchain_wallet_watch.py')
    data = tmp_path / 'data'
    data.mkdir()
    rows = [[11, script]] if daily or not candidate_present else [[11, script], [22, script]]
    (data / 'rows').write_text('\n'.join(f'{pid} {path}' for pid, path in rows))
    if marker:
        (data / 'onchain_watch.handover.json').write_text(json.dumps({
            'candidate': [22, script, 'new-birth'], 'old': [[11, script, 'old-birth']]}))
    calls = data / 'calls'
    fake = tmp_path / 'python'
    fake.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
a = sys.argv[1:]; state = pathlib.Path(os.environ['TEST_STATE']); calls = state/'data/calls'
if '-c' in a:
    src = a[a.index('-c')+1]
    if 'MEMECOIN_SOLANA_INFRA_PROVIDER' in src: print('shyft')
    elif '_watcher_rows' in src: print((state/'data/rows').read_text())
    else:
        rec = json.loads((state/'data/onchain_watch.handover.json').read_text())
        print(rec['candidate'][0], rec['candidate'][2])
elif '--retire-watcher-record' in a:
    with calls.open('a') as f: f.write('retire captured old\n')
    sys.exit(0 if os.environ['TEST_READY']=='1' and os.environ['TEST_STOP']=='1' else 1)
elif '--abort-watcher-record' in a:
    with calls.open('a') as f: f.write('abort captured candidate\\n')
    sys.exit(0 if os.environ['TEST_ABORT']=='1' else 1)
elif '--watcher-record-pid' in a:
    with calls.open('a') as f: f.write('record replacement\n')
    print(json.dumps({'candidate':[22,'replacement','new-birth'],'old':[]}))
elif '--watcher-only' in a:
    with calls.open('a') as f: f.write('verify candidate '+ ' '.join(a)+'\n')
    sys.exit(0 if '--watcher-primary-only' in a and os.environ['TEST_READY']=='1' else 1)
'''.replace("old\n'", "old\\n'").replace("replacement\n'", "replacement\\n'").replace("a)+'\n'", "a)+'\\n'"))
    fake.chmod(0o755)
    source = (Path(__file__).resolve().parents[1] / 'scripts/memecoin_watchdog.sh').read_text()
    funcs = source[source.index('ONCHAIN_SOL_SCRIPT="'):source.index('stop_unpinned_onchain_watcher() {')]
    harness = '\n'.join([
        'set -u', f'ROOT={shlex.quote(str(root))}', f'STATE_ROOT={shlex.quote(str(tmp_path))}',
        f'PY={shlex.quote(str(fake))}', 'DB_PATH=/unused', f'CALLS={shlex.quote(str(calls))}',
        'log() { printf "log %s\\n" "$*" >> "$CALLS"; }', funcs,
        'ps() { echo %s; }' % (86300 if daily else '' if age is None else age),
        'rm() { if [ "$*" = "-f $STATE_ROOT/data/onchain_watch.handover.json" ] '
        '&& [ "$TEST_REMOVE" = 0 ]; then return 1; fi; command rm "$@"; }',
        'setsid() { printf "launch %s\\n" "$*" >> "$CALLS"; }',
        'sleep() { :; }', 'ensure_onchain_solana_watcher', 'wait'])
    result = subprocess.run(['bash', '-c', harness], capture_output=True, text=True, timeout=10,
        env=os.environ | {'TEST_STATE': str(tmp_path), 'TEST_READY': str(int(ready)),
                          'TEST_STOP': str(int(stop_ok)), 'TEST_ABORT': str(int(abort_ok)),
                          'TEST_REMOVE': str(int(remove_ok))})
    assert result.returncode == 0, result.stderr
    return calls.read_text() if calls.exists() else ''


def test_daily_replacement_uses_captured_new_pid_when_old_is_first(tmp_path):
    calls = run(tmp_path)
    assert 'retire captured old' in calls
    assert 'launch ' not in calls


def test_primary_ack_failure_or_old_stop_failure_keeps_secondary_held(tmp_path):
    calls = run(tmp_path, stop_ok=False)
    assert 'retire captured old' in calls
    assert 'launch ' not in calls
    assert 'keeping' in calls


def test_near_daily_expiry_launches_and_records_replacement(tmp_path):
    calls = run(tmp_path, marker=False, daily=True)
    assert 'launch ' in calls
    assert 'record replacement' in calls


def test_unready_replacement_at_deadline_is_stopped_without_retiring_old(tmp_path):
    calls = run(tmp_path, ready=False, age=301)
    assert 'abort captured candidate' in calls
    assert 'retire captured old' not in calls
    assert 'launch ' not in calls


def test_dead_candidate_rotates_survivor_through_a_fresh_recorded_launch(tmp_path):
    calls = run(tmp_path, ready=False, age=None, candidate_present=False)
    assert 'abort captured candidate' in calls
    assert calls.index('abort captured candidate') < calls.index('launch ') < calls.index('record replacement')
    assert 'retire captured old' not in calls
    record = json.loads((tmp_path / 'data/onchain_watch.handover.json').read_text())
    assert record['candidate'][1] == 'replacement'


def test_missing_ps_age_does_not_rotate_an_unproven_candidate(tmp_path):
    calls = run(tmp_path, ready=False, age=None, abort_ok=False)
    assert 'abort captured candidate' in calls
    assert 'launch ' not in calls
    assert 'retire captured old' not in calls
    assert (tmp_path / 'data/onchain_watch.handover.json').exists()


def test_dead_candidate_cleanup_failure_cannot_launch_an_unrecorded_replacement(tmp_path):
    calls = run(tmp_path, ready=False, age=None, candidate_present=False, remove_ok=False)
    assert 'abort captured candidate' in calls
    assert 'launch ' not in calls
    assert (tmp_path / 'data/onchain_watch.handover.json').exists()


@pytest.mark.parametrize('discovery', ['healthy', 'empty', 'unavailable'])
@pytest.mark.parametrize('entry_point', ['ensure_onchain_solana_watcher',
                                         'ensure_shyft_solana_watcher'])
def test_watcher_discovery_uses_pinned_release_and_holds_unknown(tmp_path, discovery, entry_point):
    """Execute actual discovery/handover functions with a real isolated interpreter."""
    release = tmp_path / '.runtime/memecoin/releases/new'
    for root in (tmp_path, release):
        (root / 'scripts').mkdir(parents=True)
        (root / 'scripts/__init__.py').write_text('')
    (tmp_path / 'scripts/memecoin_runtime_verify.py').write_text(
        "raise RuntimeError('mutable state checkout imported')\n")
    rel = 'scripts/research/onchain_wallet_watch.py'
    rows = [(123, str(release / rel))] if discovery == 'healthy' else []
    body = ("raise RuntimeError('release discovery unavailable')" if discovery == 'unavailable'
            else f'return {rows!r}')
    (release / 'scripts/memecoin_runtime_verify.py').write_text(
        f'def _watcher_rows(*args):\n    {body}\n')
    source = (Path(__file__).resolve().parents[1] / 'scripts/memecoin_watchdog.sh').read_text()
    names = ('solana_watchers', 'ensure_shyft_solana_watcher', 'ensure_onchain_solana_watcher')
    functions = []
    for name in names:
        start = source.index(name + '() {')
        end = source.index('\n}\n', start) + 3
        functions.append(source[start:end])
    calls = tmp_path / 'calls'
    harness = '\n'.join([
        'set -u', 'SHYFT_WATCHER_FIRST=1',
        f'ROOT={shlex.quote(str(release))}', f'STATE_ROOT={shlex.quote(str(tmp_path))}',
        f'PY={shlex.quote(sys.executable)}', f'ONCHAIN_SOL_SCRIPT={shlex.quote(rel)}',
        'ONCHAIN_SOL_ROTATE_LEAD_S=180', 'ONCHAIN_SOL_HANDOVER_MAX_S=0',
        f'CALLS={shlex.quote(str(calls))}',
        'log() { printf "log %s\\n" "$*" >> "$CALLS"; }',
        'launch_solana_watcher() { echo launch >> "$CALLS"; }',
        'ps() { echo 5; }', *functions, entry_point])
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('SNIPER_', 'MEMECOIN_', 'PYTHON'))}
    result = subprocess.run(['bash', '-c', harness], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=10)
    recorded = calls.read_text() if calls.exists() else ''
    if discovery == 'unavailable':
        assert result.returncode != 0, 'unknown discovery was treated as no owned watcher'
        assert 'launch' not in recorded, 'unknown discovery launched a replacement'
    else:
        assert result.returncode == 0, result.stderr
        assert 'mutable state checkout imported' not in result.stderr
        assert ('launch' in recorded) == (discovery == 'empty')
