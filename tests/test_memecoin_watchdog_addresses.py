"""Run the real watchdog control path with local process and probe substitutes."""
import json
from pathlib import Path
import shlex
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def run_watchdog(tmp_path, codes):
    source = (ROOT / 'scripts/memecoin_watchdog.sh').read_text()
    start = source.index('\n_ELIGIBLE_EVM_WATCHERS=""')
    # The upkeep is a function since 2026-09-27 (run after the runner launch); its body is
    # the control path under test, sliced without the wrapper.
    functions = source[source.index('log() {'):source.index('\nperipheral_upkeep() {')]
    control = source[start:source.index('\n}\n# END peripheral-upkeep')]
    state = tmp_path / 'data'
    state.mkdir()
    lines = [
        'set -u', f'ROOT={shlex.quote(str(ROOT))}', f'STATE_ROOT={shlex.quote(str(tmp_path))}',
        'PY=probe; DB_PATH=unused; FOMO_TOKEN_STORE=unused',
        'WATCHDOG_LOG="$STATE_ROOT/watchdog.log"', functions,
        '''probe() {
          [ "$2" = "--chain" ] || return 0
          if [ "$CODE" = 0 ]; then
            printf '%s\\n' '{"status":"eligible","eligible_addresses":1}';
          elif [ "$CODE" = 3 ]; then
            printf '%s\\n' '{"status":"blocked","reason":"no verified addresses","eligible_addresses":0}';
          fi
          return "$CODE"
        }''',
        # The eligibility gate is the same code for every chain; 8453 stands in for all of them.
        # 4663's launch and kill paths are pinned in test_memecoin_watchdog_4663_revival.py.
        'onchain_evm_chains() { printf "8453\\n"; }',
        'onchain_evm_observe_chains() { :; }',
        'onchain_evm_observe_chains_missing_ws() { :; }',
        'stop_unpinned_onchain_watcher() { echo duplicate_check; }',
        'upgrade_onchain_evm_watcher() { echo upgrade_check; }',
        'kill_degraded_onchain_watcher() { echo health_check; }',
        'ensure_onchain_evm_watcher() { echo launch_check; }',
        'tick() {', control, '}',
    ]
    for code in codes:
        lines += [f'CODE={code}', 'tick']
    result = subprocess.run(['bash', '-c', '\n'.join(lines)], text=True, capture_output=True,
                            env={'PATH': '/usr/bin:/bin'}, timeout=5)
    assert result.returncode == 0, result.stderr
    return (result.stdout, (tmp_path/'watchdog.log').read_text(),
            json.loads((state/'onchain_evm_watch.8453.watchdog.json').read_text()))


def test_blocked_ticks_do_not_launch_or_restart_and_log_once(tmp_path):
    output, log, state = run_watchdog(tmp_path, [3, 3, 3])
    assert output == ''
    assert log.count('blocked: no verified addresses') == 1
    assert state['eligible_addresses'] == 0


def test_next_tick_resumes_when_a_verified_address_becomes_eligible(tmp_path):
    output, log, state = run_watchdog(tmp_path, [3, 0])
    assert output.splitlines() == ['duplicate_check', 'upgrade_check', 'health_check', 'launch_check']
    assert 'eligible' in log
    assert state['status'] == 'eligible'


def test_database_probe_failure_blocks_launch_without_claiming_an_empty_book(tmp_path):
    output, log, state = run_watchdog(tmp_path, [2, 2])
    assert output == ''
    assert log.count('blocked: address eligibility unavailable') == 1
    assert state['reason'] == 'address eligibility unavailable'
