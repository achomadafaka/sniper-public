"""The watchdog must page a funded blackout before it relaunches the runner.

A host poweroff pages nothing on its own: `escalate_failure` only records a failure that a
watchdog tick observed, and no tick runs while the host is off, so the next boot reads as a
clean first launch. On 2026-08-12 that hid a 56-minute funded blackout (12:40:18-13:36:31Z)
which cost one armed-wallet signal outright — cryptojuggler3 bought BENDER at 13:24:11Z with no
process alive to see it, and the backfilled catch arrived 778s stale against a 120s bound.

These tests drive the real function text out of the script, so an edit to the script runs here.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "memecoin_watchdog.sh"


def _blackout_functions() -> str:
    """The blackout helpers as they exist in the script right now."""
    text = SCRIPT.read_text()
    start = text.index("blackout_gap_s() {")
    end = text.index("\nRUNNER_ROWS=", start)
    return text[start:end]


def _run(tmp_path, *, heartbeat_ts=None, raw=None, min_s=None, slow_s=0):
    """Call page_blackout_before_launch with stubbed log/alert. Returns its stdout: the call's own
    output, then (after `wait`) the pages, which are sent in the background. The call's own time
    is written to elapsed_ms."""
    hb = tmp_path / "runner.heartbeat"
    if raw is not None:
        hb.write_text(raw)
    elif heartbeat_ts is not None:
        hb.write_text(json.dumps({"ts": heartbeat_ts, "auth_ok": True}))

    script = "\n".join([
        "set -u",
        f'ROOT="{ROOT}"',
        f'PY="{sys.executable}"',
        "log() { printf 'LOG %s\\n' \"$*\"; }",
        f"alert() {{ sleep {slow_s}; printf 'ALERT %s\\n' \"$1\" >> '{tmp_path / 'alerts'}'; }}",
        _blackout_functions(),
        "t0=$(date +%s%N); page_blackout_before_launch; t1=$(date +%s%N)",
        f"echo $(( (t1 - t0) / 1000000 )) > '{tmp_path / 'elapsed_ms'}'",
        f"wait; cat '{tmp_path / 'alerts'}' 2>/dev/null; true",
    ])
    env = {
        "PATH": "/usr/bin:/bin",
        "MEMECOIN_HEARTBEAT_PATH": str(hb),
        "SNIPER_STATE_ROOT": str(tmp_path),
    }
    if min_s is not None:
        env["MEMECOIN_BLACKOUT_ALERT_MIN_S"] = min_s
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_long_outage_pages_with_the_duration(tmp_path):
    out = _run(tmp_path, heartbeat_ts=time.time() - 3400)
    assert "ALERT" in out and "BLACKOUT" in out
    assert "56m40s" in out, out
    assert "LOG funded blackout 3400s" in out


def test_short_restart_gap_pages_nothing(tmp_path):
    """A wedged runner restart is ~60s and already pages via the stale path — no duplicate."""
    assert _run(tmp_path, heartbeat_ts=time.time() - 60) == ""


def test_absent_heartbeat_pages_nothing(tmp_path):
    """No heartbeat is unknown coverage, not measured downtime. A first launch must stay quiet."""
    assert _run(tmp_path) == ""


def test_corrupt_heartbeat_pages_nothing_and_never_crashes(tmp_path):
    assert _run(tmp_path, raw="{not json") == ""
    assert _run(tmp_path, raw=json.dumps({"ts": "banana"})) == ""
    assert _run(tmp_path, raw=json.dumps({"auth_ok": True})) == ""


def test_threshold_is_operator_tunable(tmp_path):
    assert "ALERT" in _run(tmp_path, heartbeat_ts=time.time() - 60, min_s="30")


def test_garbage_threshold_falls_back_to_the_default(tmp_path):
    """A malformed knob must not become a 0 threshold that pages on every launch."""
    assert _run(tmp_path, heartbeat_ts=time.time() - 60, min_s="") == ""
    assert _run(tmp_path, heartbeat_ts=time.time() - 60, min_s="banana") == ""
    assert "ALERT" in _run(tmp_path, heartbeat_ts=time.time() - 3400, min_s="banana")


def test_early_watcher_restart_can_call_the_real_alert_function(tmp_path):
    import shlex

    state = tmp_path / "state"
    (state / "data").mkdir(parents=True)
    (state / "logs").mkdir()
    (state / "data" / "onchain_evm_watch.8453.heartbeat").write_text("{}")
    alerts = tmp_path / "alerts"
    kills = tmp_path / "kills"
    text = SCRIPT.read_text()
    # The upkeep is a function since 2026-09-27 (run after the runner launch); define it, run it.
    early_watchers = text[text.index("log() {"):text.index("\n_UPKEEP_DONE=0")] + "\nperipheral_upkeep\n"
    harness = "\n".join([
        "set -u",
        # This root's own release: the degraded kill only touches $STATE_ROOT's releases.
        f"ROOT={shlex.quote(str(state / '.runtime/memecoin/releases/current'))}",
        f"STATE_ROOT={shlex.quote(str(state))}",
        f"ALERT_CAPTURE={shlex.quote(str(alerts))}",
        f"KILL_CAPTURE={shlex.quote(str(kills))}",
        'WATCHDOG_LOG="$STATE_ROOT/logs/watchdog.log"',
        'PY=fake_python; DB_PATH=""; FOMO_TOKEN_STORE=""',
        'fake_python() { if [ "$2" = "--chain" ]; then printf "{\\\"status\\\":\\\"eligible\\\"}\\n"; return; fi; case "$2" in',
        '  *send_telegram*) printf "%s\\n" "$3" >> "$ALERT_CAPTURE" ;;',
        '  *coverage_low_since*) printf "beat_stale:200\\n" ;;',
        # The funded loop runs the same restart path for every chain; 8453 stands in for all.
        '  *MEMECOIN_EVM_ALLOWLIST*) printf "8453\\n" ;;',
        'esac; }',
        'ps() { case "$*" in *pid=*) printf "123 " ;; esac;',
        '  printf "/usr/bin/python %s/scripts/research/onchain_evm_wallet_watch.py --duration 86400 --chain 8453\\n" "$ROOT"; }',
        'kill() { printf "%s\\n" "$*" >> "$KILL_CAPTURE"; }',
        'sleep() { :; }',
        'setsid() { printf "Unexpected service launch\\n" >&2; return 1; }',
        early_watchers,
    ])
    result = subprocess.run(["bash", "-c", harness], capture_output=True, text=True,
                            env={"PATH": "/usr/bin:/bin"}, timeout=5)
    assert result.returncode == 0, result.stderr
    assert "command not found" not in result.stderr
    assert kills.read_text() == "123\n"
    assert alerts.read_text() == (
        "memecoin on-chain watcher chain=8453 restarted by watchdog (beat_stale:200)\n")


def test_a_slow_telegram_never_delays_the_relaunch(tmp_path):
    """Gamma LOW / Alpha 2026-09-29: with Telegram unreachable a send can take ~18 s (the primary's
    8 s and the fallback's 10 s). The blackout page runs before the relaunch, so it is sent in the
    background: a 10 s stubbed send leaves the call's own time unchanged, and the page still goes."""
    out = _run(tmp_path, heartbeat_ts=time.time() - 3600, slow_s=10)
    elapsed = int((tmp_path / "elapsed_ms").read_text())
    assert elapsed < 2000, out
    assert "ALERT" in out and "BLACKOUT" in out
