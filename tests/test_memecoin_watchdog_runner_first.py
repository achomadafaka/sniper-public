"""The watchdog launches the funded runner BEFORE its peripheral upkeep, and still does the upkeep.

Deploy 95e5e189 (2026-09-27): the watchdog was called at 01:30:53 and launched the runner at
01:30:58. The seconds in between went to EVM watcher kills (`sleep 1` each), eligibility probes and
launches, with no runner up at all, so exits were down as well as entries. The upkeep now runs
after the launch, while the runner starts; a tick that launches nothing still runs it on the way
out, as it always did.

The whole script runs here against a fake interpreter. The upkeep's first act is to list the funded
EVM chains, and the fake records whether the runner's pidfile existed at that moment: the launch
writes the pidfile synchronously, so "yes" on a no-runner tick means the launch came first.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

FAKE = r'''#!/usr/bin/env bash
case "${1:-}" in
  */memecoin_runtime_verify.py)
    if [[ "$*" == *--watcher-only* ]]; then
      if [ -s "$TEST_PIDFILE" ]; then echo 'watcher pidfile=yes' >> "$TEST_EVENTS";
      else echo 'watcher pidfile=no' >> "$TEST_EVENTS"; fi
      [ "${TEST_WATCHER_FAIL:-0}" = 0 ]; exit $?;
    fi
    echo verify >> "$TEST_EVENTS.verify"; exit 0 ;;
  */memecoin_runtime_processes.py)
    [ -f "$TEST_LAUNCHED" ] && echo "scan" >> "$TEST_EVENTS"
    if [ -n "${TEST_EXISTING_PID:-}" ]; then
      printf '%s\tpinned\t%s\n' "$TEST_EXISTING_PID" "${3:-}"
    elif [ -s "$TEST_PIDFILE" ] && [ -f "$TEST_LAUNCHED" ]; then
      printf '%s\tpinned\t%s\n' "$(cat "$TEST_PIDFILE")" "${3:-}"
    fi
    exit 0 ;;
  */memecoin_watchdog_escalate.py) exit 0 ;;
  -)  cat >/dev/null; printf '0\n'; exit 0 ;;
  -u) touch "$TEST_LAUNCHED"
      env | grep -q '^MEMECOIN_WATCHDOG_RELEASE_VERIFIED=' && echo leaked >> "$TEST_EVENTS.env"
      exec sleep 30 ;;
  -c)
    case "${2:-}" in
      *MEMECOIN_SOLANA_INFRA_PROVIDER*) printf '%s\n' "${TEST_PROFILE:-legacy}" ;;
      *MEMECOIN_EVM_ALLOWLIST*)
        if [ -s "$TEST_PIDFILE" ]; then echo "chains pidfile=yes" >> "$TEST_EVENTS";
        else echo "chains pidfile=no" >> "$TEST_EVENTS"; fi ;;
      *runner_launch_health*)
        echo "launch check" >> "$TEST_EVENTS"
        date +%s.%N >> "$TEST_EVENTS.checks"
        if [ -n "${TEST_HEALTH_FAILS:-}" ] && [ "$(wc -l < "$TEST_EVENTS.checks")" -le "$TEST_HEALTH_FAILS" ]; then
          exit 1
        fi
        exit 0 ;;
      *send_telegram*)
        [ -n "${TEST_SLOW_ALERT_S:-}" ] && sleep "$TEST_SLOW_ALERT_S"
        echo "$3" >> "$TEST_EVENTS.alerts" ;;
      *runner_health*) printf 'ok\n' ;;
      *read_heartbeat*) printf -- '-1\n' ;;
    esac
    exit 0 ;;
esac
exit 0
'''


def _run(tmp_path, *, existing_pid=None, health_fails=None, slow_alert_s=None,
         verified_sha=None, wrap=None, shyft=False, watcher_fail=False):
    release = tmp_path / "release"
    scripts = release / "scripts"
    scripts.mkdir(parents=True)
    watchdog = scripts / "memecoin_watchdog.sh"
    shutil.copy2(Path(__file__).resolve().parents[1] / "scripts" / "memecoin_watchdog.sh", watchdog)
    (scripts / "memecoin_runner_loop.py").write_text("# launched through the fake interpreter\n")
    (scripts / "memecoin_onchain_volume_watchdog.sh").write_text("exit 0\n")
    for name in ("memecoin_runtime_verify.py", "memecoin_runtime_processes.py",
                 "memecoin_watchdog_escalate.py"):
        (scripts / name).write_text("# handled by the fake interpreter\n")
    subprocess.run(["git", "init", "-q", str(release)], check=True)
    subprocess.run(["git", "-C", str(release), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "--allow-empty", "-m", "release"], check=True)
    state = tmp_path / "state"
    (state / "data").mkdir(parents=True)
    (state / "logs").mkdir()
    (state / ".fomo.env").write_text("MEMECOIN_REAL_ENABLED=0\n")
    (state / "data" / "memecoin_deploy.json").write_text("{}\n")
    fake = tmp_path / "python"
    fake.write_text(FAKE)
    fake.chmod(0o755)
    pidfile = state / "data" / "memecoin_runner.pid"
    events = tmp_path / "events"
    # Nothing from the calling process's MEMECOIN_/SNIPER_ settings: another test in the same
    # worker can leave a log, lock or state path there, and the script would follow it.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MEMECOIN_", "SNIPER_"))} | {
        "SNIPER_STATE_ROOT": str(state), "SNIPER_PYTHON": str(fake),
        "MEMECOIN_PIDFILE": str(pidfile), "MEMECOIN_WATCHDOG_STARTUP_TIMEOUT_S": "10",
        "TEST_PIDFILE": str(pidfile), "TEST_EVENTS": str(events),
        "TEST_LAUNCHED": str(tmp_path / "launched"),
    }
    if existing_pid is not None:
        env["TEST_EXISTING_PID"] = str(existing_pid)
    if health_fails is not None:
        env["TEST_HEALTH_FAILS"] = str(health_fails)
    if slow_alert_s is not None:
        env["TEST_SLOW_ALERT_S"] = str(slow_alert_s)
    if verified_sha is not None:
        env["MEMECOIN_WATCHDOG_RELEASE_VERIFIED"] = (
            verified_sha if verified_sha != "HEAD" else subprocess.run(
                ["git", "-C", str(release), "rev-parse", "HEAD"], capture_output=True,
                text=True, check=True).stdout.strip())
    if shyft:
        env["TEST_PROFILE"] = "shyft"
        env["ONCHAIN_SOL_HANDOVER_MAX_S"] = "1"
    if watcher_fail:
        env["TEST_WATCHER_FAIL"] = "1"
    cmd = ["bash", str(watchdog)] if wrap is None else wrap(watchdog, env)
    try:
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=30)
    finally:
        launched = pidfile.read_text().strip() if pidfile.exists() else ""
        # A wrapped run is in its own pid namespace: its pids mean nothing out here, and its
        # processes died with the namespace.
        if launched.isdigit() and existing_pid is None and wrap is None:
            subprocess.run(["kill", launched], check=False)
    log = (state / "logs" / "memecoin_watchdog.log").read_text()
    return result, events.read_text() if events.exists() else "", log


def test_a_tick_with_no_runner_launches_it_before_the_upkeep(tmp_path):
    result, events, log = _run(tmp_path)
    assert result.returncode == 0, (result.stdout, result.stderr, log)
    assert "pinned runner not running; launching" in log and "runner launch verified" in log
    # Launched first, then the upkeep once, while the runner starts: before the launch check,
    # not after it, where it would hold up a deploy waiting on this script.
    assert events == "chains pidfile=yes\nlaunch check\nscan\n"


def test_shyft_runner_launches_before_watcher_handover_upkeep(tmp_path):
    result, events, log = _run(tmp_path, shyft=True)
    assert result.returncode == 0, (result.stdout, result.stderr, log)
    assert "watcher pidfile=no" not in events
    assert "chains pidfile=yes" in events and "launch check" in events


def test_shyft_failed_handover_still_launches_funded_exit_recovery_owner(tmp_path):
    result, events, log = _run(tmp_path, shyft=True, watcher_fail=True)
    assert result.returncode == 0, (result.stdout, result.stderr, log)
    assert (tmp_path / "launched").exists()
    assert "watcher pidfile=no" not in events and "launch check" in events
    assert "runner launch verified" in log
    assert "Shyft Solana entries and auxiliary streams staged" in log


def test_a_tick_with_a_healthy_runner_still_runs_the_upkeep(tmp_path):
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        result, events, log = _run(tmp_path, existing_pid=process.pid)
    finally:
        process.terminate()
        process.wait(timeout=5)
    assert result.returncode == 0, (result.stdout, result.stderr, log)
    assert "launching" not in log
    assert events == "chains pidfile=yes\n"          # the repaired pidfile; upkeep ran once


def test_the_launch_check_polls_every_quarter_second_heartbeat_first(tmp_path):
    """The old loop slept 1 s between checks and scanned processes first, so a runner that had
    passed waited up to ~1.6 s to be noticed, and a deploy's entry gate with it."""
    result, events, log = _run(tmp_path, health_fails=3)
    assert result.returncode == 0, (result.stdout, result.stderr, log)
    assert events.splitlines()[1:] == ["launch check"] * 4 + ["scan"]   # no scan until it passes
    stamps = [float(x) for x in (tmp_path / "events.checks").read_text().split()]
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert len(gaps) == 3 and max(gaps) < 0.6, gaps


def test_a_slow_telegram_alert_does_not_hold_the_deploy_after_the_launch_is_verified(tmp_path):
    """Deploy e9781d89 (2026-09-27): the watchdog logged 'runner launch verified' at 02:16:07Z and
    the deploy, which waits on this script before it clears the entry gate, saw it at 02:16:15.8.
    The 8.6 s in between was the post-verification recovery page and Telegram alert."""
    import time
    result, events, log = _run(tmp_path, slow_alert_s=4)
    returned = time.time()
    assert result.returncode == 0, (result.stdout, result.stderr, log)
    last_check = float((tmp_path / "events.checks").read_text().split()[-1])
    assert returned - last_check < 1.5, returned - last_check
    # ...and the page is still sent, after the script has returned.
    alerts = tmp_path / "events.alerts"
    while not alerts.exists() and time.time() - returned < 8:
        time.sleep(0.1)
    assert "memecoin pinned runner launched" in alerts.read_text()


def _verified(tmp_path):
    return (tmp_path / "events.verify").exists()


def test_a_deploy_that_verified_this_exact_release_is_not_verified_again(tmp_path):
    """Deploy 67534745 spent ~2 s in the watchdog's own pre-launch verify of a release the deploy
    had verified 0.1 s earlier; the deploy now names the SHA it verified."""
    result, events, log = _run(tmp_path, verified_sha="HEAD")
    assert result.returncode == 0, (result.stdout, result.stderr, log)
    assert not _verified(tmp_path) and "not verifying again" in log
    assert "runner launch verified" in log
    assert not (tmp_path / "events.env").exists()     # the runner never inherits the flag


def test_a_mismatched_or_absent_flag_verifies_in_full(tmp_path):
    for i, flag in enumerate(("0" * 40, None)):
        run_dir = tmp_path / str(i)
        run_dir.mkdir()
        result, events, log = _run(run_dir, verified_sha=flag)
        assert result.returncode == 0, (result.stdout, result.stderr, log)
        assert _verified(run_dir) and "not verifying again" not in log


# THE WHOLE WATCHDOG NEVER SIGNALS ANOTHER STATE ROOT'S WATCHER (2026-09-29). This file ran the whole
# script with the REAL ps and kill and a tmp ROOT/STATE_ROOT, and ensure_onchain_solana_watcher took
# the live pinned Solana watcher for an older release of ITS root and killed it (logged only to the
# tmp log). The check runs in its own pid namespace beside a stand-in "live" watcher, so even a
# regression can only kill the stand-in, never production.
NS = ["unshare", "-c", "-p", "-f", "--mount-proc"]


def test_a_live_watcher_under_another_state_root_survives_the_whole_watchdog(tmp_path):
    import pytest
    probe = subprocess.run(NS + ["true"], capture_output=True, text=True)
    if probe.returncode != 0:
        pytest.skip(f"no unprivileged pid namespace: {probe.stderr.strip()[:120]}")
    live = tmp_path / "live/.runtime/memecoin/releases/f00d42/scripts/research/onchain_wallet_watch.py"
    live.parent.mkdir(parents=True)
    live.write_text("import os, time\nopen(os.environ['LIVE_READY'], 'w').close()\ntime.sleep(120)\n")
    ready, status = tmp_path / "live.ready", tmp_path / "live.status"

    def wrap(watchdog, env):
        env.update(LIVE_READY=str(ready))
        script = (
            f'{sys.executable} {live} --duration 86400 & L=$!\n'
            f'for i in $(seq 1 200); do [ -f {ready} ] && break; sleep 0.05; done\n'
            f'bash {watchdog}; rc=$?\n'
            f'if kill -0 $L 2>/dev/null; then echo alive; else wait $L; echo "gone $?"; fi > {status}\n'
            'exit $rc\n')
        return NS + ["bash", "-c", script]

    result, _events, log = _run(tmp_path, wrap=wrap)
    assert ready.exists(), "the stand-in watcher never started"
    assert status.read_text().strip() == "alive", (status.read_text(), log)
    assert "older-release" not in log and "stopping pid(s)" not in log, log
