"""Run the real observe-only watchdog loop with local function substitutes.

Mirrors tests/test_memecoin_watchdog_addresses.py's technique: source the actual shell text
(not a rewritten copy) so a change to the real control flow is what this test exercises.
"""
from pathlib import Path
import shlex
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def run_watchdog(tmp_path, *, observe_chains=(), missing_ws=(), funded_chains=(), eligible_chains=()):
    source = (ROOT / 'scripts/memecoin_watchdog.sh').read_text()
    start = source.index('\n_ELIGIBLE_EVM_WATCHERS=""')
    # The upkeep is a function since 2026-09-27 (run after the runner launch); its body is
    # the control path under test, sliced without the wrapper.
    functions_src = source[source.index('log() {'):source.index('\nperipheral_upkeep() {')]
    control = source[start:source.index('\n}\n# END peripheral-upkeep')]
    state = tmp_path / 'data'
    state.mkdir()
    (tmp_path / 'watchdog.log').touch()
    observe_list = ' '.join(observe_chains)
    missing_list = ' '.join(missing_ws)
    funded_list = ' '.join(funded_chains)
    lines = [
        'set -u', f'ROOT={shlex.quote(str(ROOT))}', f'STATE_ROOT={shlex.quote(str(tmp_path))}',
        'PY=probe; DB_PATH=unused; FOMO_TOKEN_STORE=unused',
        'WATCHDOG_LOG="$STATE_ROOT/watchdog.log"', functions_src,
        # Override the real (Python-shelling) discovery functions with fixed test lists.
        f'onchain_evm_chains() {{ printf "%s\\n" {shlex.quote(funded_list)}; }}',
        f'onchain_evm_observe_chains() {{ printf "%s\\n" {shlex.quote(observe_list)}; }}',
        f'onchain_evm_observe_chains_missing_ws() {{ printf "%s\\n" {shlex.quote(missing_list)}; }}',
        'onchain_addresses_eligible() { case " '+ ' '.join(eligible_chains) + ' " in *" $1 "*) return 0;; *) return 1;; esac; }',
        'stop_unpinned_onchain_watcher() { echo "duplicate_check $*"; }',
        'upgrade_onchain_evm_watcher() { echo "upgrade_check $*"; }',
        'kill_degraded_onchain_watcher() { echo "health_check $*"; }',
        'ensure_onchain_evm_watcher() { echo "launch_check $*"; }',
        'tick() {', control, '}', 'tick',
    ]
    result = subprocess.run(['bash', '-c', '\n'.join(lines)], text=True, capture_output=True,
                            env={'PATH': '/usr/bin:/bin'}, timeout=5)
    assert result.returncode == 0, result.stderr
    return result.stdout, (tmp_path / 'watchdog.log').read_text()


def test_an_observe_chain_launches_with_the_observe_only_flag(tmp_path):
    output, log = run_watchdog(tmp_path, observe_chains=['5042'])
    lines = output.splitlines()
    assert lines == [
        'duplicate_check 5042 --observe-only',
        'upgrade_check 5042 --observe-only',
        'health_check 5042 --observe-only',
        'launch_check 5042 --observe-only',
    ]


def test_an_observe_chain_with_no_ws_url_refuses_loudly_and_does_not_launch(tmp_path):
    output, log = run_watchdog(tmp_path, missing_ws=['5042'])
    assert output == ''
    assert 'on-chain watcher chain=5042 observe-only: refusing to start' in log
    assert 'MEMECOIN_EVM_WS_URL_5042 is unset' in log


def test_a_chain_already_in_the_funded_loop_is_never_double_launched(tmp_path):
    """One chain gets exactly one supervised watcher -- funded status wins once it applies."""
    output, log = run_watchdog(tmp_path, observe_chains=['5042'], funded_chains=['5042'], eligible_chains=['5042'])
    assert output.count('launch_check 5042') == 1
    assert '--observe-only' not in output


def test_the_observe_only_gate_needs_no_wallet_eligibility(tmp_path):
    """onchain_addresses_eligible always fails in this harness; the observe loop must not call
    it at all -- there is no wallet lane on this path to be eligible for."""
    output, log = run_watchdog(tmp_path, observe_chains=['5042', '99999'])
    assert output.count('launch_check') == 2


def test_allowlisted_chain_without_verified_addresses_keeps_pool_capture(tmp_path):
    output, _ = run_watchdog(tmp_path, observe_chains=['5042'], funded_chains=['5042'])
    assert output.count('launch_check 5042 --observe-only') == 1
