"""A pinned-release verification refusal pages, rate-limited, and names the recovery (Alpha, 2026-09-29).

On 2026-09-24 the funded book was dark for 41 minutes (21:16Z-21:57Z): every tick logged "PINNED
RELEASE VERIFICATION FAILED" (TPU sidecar degraded, every funded RPC transport_error) and refused the
launch, correctly. The page carried a raw JSON blob and no recovery; the escalation state advanced
before delivery, so an undelivered page was never retried; and the pager itself imports the release
that failed. Now: the first error line, the automatic retry and a rollback command are in the page;
an undelivered page is retried on the next tick; a release-independent sender is the fallback; a
broken escalation helper still pages.

The WHOLE watchdog runs here against a fake interpreter, inside its own pid namespace (unshare):
a whole-script watchdog run on the host can kill the live Solana watcher (2026-09-29).
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
NS = ["unshare", "-c", "-p", "-f", "--mount-proc"]
ERR = ('{"code_root": "/r/37ac4dfb", "errors": ["real configuration invalid: memecoin TPU sidecar '
       'degraded reason=initial_health_unavailable\\nmemecoin: solana tpu sidecar restart #1 (crashed)'
       '\\nSolana funded RPC rotated reason=transport_error from_index=0 to_index=1'
       '\\nSolana funded RPC rotated reason=transport_error from_index=1 to_index=2"]}')

FAKE = r'''#!/usr/bin/env bash
case "${1:-}" in
  */memecoin_runtime_verify.py) printf '%s\n' "$TEST_VERIFY_OUT"; exit 1 ;;
  */memecoin_runtime_processes.py)
    [ -n "${TEST_EXISTING_PID:-}" ] && printf '%s\tpinned\t%s\n' "$TEST_EXISTING_PID" "${3:-}"
    exit 0 ;;
  */memecoin_watchdog_escalate.py)
    [ -n "${TEST_ESCALATE_BROKEN:-}" ] && exit 2
    exec "$REAL_PY" "$@" ;;
  -)  body="$(cat)"
      case "$body" in
        *api.telegram.org*) echo "fallback:$3" >> "$TEST_EVENTS.alerts"; exit "${TEST_FALLBACK_RC:-0}" ;;
      esac
      printf '0\n'; exit 0 ;;
  -c)
    case "${2:-}" in
      *send_telegram*) echo "primary:$3" >> "$TEST_EVENTS.alerts"; exit "${TEST_PRIMARY_RC:-0}" ;;
      *runner_health*) printf 'ok\n' ;;
      *read_heartbeat*) printf -- '-1\n' ;;
    esac
    exit 0 ;;
esac
exit 0
'''


def _setup(tmp_path):
    release = tmp_path / "release"
    scripts = release / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(REPO / "scripts" / "memecoin_watchdog.sh", scripts / "memecoin_watchdog.sh")
    shutil.copy2(REPO / "scripts" / "memecoin_watchdog_escalate.py", scripts / "memecoin_watchdog_escalate.py")
    for name in ("memecoin_runner_loop.py", "memecoin_runtime_verify.py", "memecoin_runtime_processes.py"):
        (scripts / name).write_text("# handled by the fake interpreter\n")
    (scripts / "memecoin_onchain_volume_watchdog.sh").write_text("exit 0\n")
    subprocess.run(["git", "init", "-q", str(release)], check=True)
    subprocess.run(["git", "-C", str(release), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "--allow-empty", "-m", "release"], check=True)
    state = tmp_path / "state"
    (state / "data").mkdir(parents=True)
    (state / "logs").mkdir()
    (state / ".fomo.env").write_text("MEMECOIN_REAL_ENABLED=0\n")
    (state / "data" / "memecoin_deploy.json").write_text(
        '{"sha": "37ac4dfb12b0", "previous_sha": "0ld5a0000000000000000000000000000000beef"}\n')
    fake = tmp_path / "python"
    fake.write_text(FAKE)
    fake.chmod(0o755)
    return scripts / "memecoin_watchdog.sh", state, fake


def _tick(tmp_path, watchdog, state, fake, healthy=False, **env_extra):
    import os
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MEMECOIN_", "SNIPER_"))} | {
        "SNIPER_STATE_ROOT": str(state), "SNIPER_PYTHON": str(fake), "REAL_PY": sys.executable,
        "MEMECOIN_PIDFILE": str(state / "data" / "memecoin_runner.pid"),
        "TEST_EVENTS": str(tmp_path / "events"), "TEST_VERIFY_OUT": ERR,
        "MEMECOIN_WATCHDOG_NET_PROBE": "exit 0",
    } | {k: str(v) for k, v in env_extra.items()}
    # A healthy runner is a stand-in process INSIDE the namespace, so the watchdog can see its pid.
    cmd = (["bash", "-c", "sleep 30 & export TEST_EXISTING_PID=$!; bash %s; rc=$?; kill $TEST_EXISTING_PID; "
            "exit $rc" % watchdog] if healthy else ["bash", str(watchdog)])
    result = subprocess.run(NS + cmd, env=env, capture_output=True, text=True, timeout=60)
    alerts = tmp_path / "events.alerts"
    log = (state / "logs" / "memecoin_watchdog.log").read_text()
    return result, (alerts.read_text().splitlines() if alerts.exists() else []), log


@pytest.fixture
def sandbox(tmp_path):
    probe = subprocess.run(NS + ["true"], capture_output=True, text=True)
    if probe.returncode != 0:
        pytest.skip(f"no unprivileged pid namespace: {probe.stderr.strip()[:120]}")
    return (tmp_path,) + _setup(tmp_path)


def test_a_verification_refusal_pages_the_error_and_the_recovery(sandbox):
    tmp_path, watchdog, state, fake = sandbox
    result, alerts, log = _tick(tmp_path, watchdog, state, fake)
    assert result.returncode == 1 and "PINNED RELEASE VERIFICATION FAILED" in log
    (page,) = alerts
    assert page.startswith("primary:memecoin runner DOWN")
    assert "real configuration invalid: memecoin TPU sidecar degraded reason=initial_health_unavailable" in page
    assert "code_root" not in page                                    # the first error, not the JSON blob
    assert "2 funded RPC transport errors" in page                   # 09-24: the real cause was the network
    assert "re-verifies every minute" in page
    assert "deploy_memecoin_runtime.sh 0ld5a0000000000000000000000000000000beef --allow-rollback" in page


def test_a_delivered_page_is_rate_limited(sandbox):
    tmp_path, watchdog, state, fake = sandbox
    _tick(tmp_path, watchdog, state, fake)
    _result, alerts, _log = _tick(tmp_path, watchdog, state, fake)
    assert len(alerts) == 1                                           # the second tick is silent


def test_an_undelivered_page_falls_back_and_is_retried_next_tick(sandbox):
    tmp_path, watchdog, state, fake = sandbox
    _r, alerts, log = _tick(tmp_path, watchdog, state, fake, TEST_PRIMARY_RC=3, TEST_FALLBACK_RC=5)
    assert [a.split(":", 1)[0] for a in alerts] == ["primary", "fallback"]
    assert "page not delivered" in log
    _r, alerts, _log = _tick(tmp_path, watchdog, state, fake)
    assert alerts[-1].startswith("primary:memecoin runner STILL DOWN")   # retried, not rate limited


def test_the_fallback_sender_delivers_when_the_release_python_cannot(sandbox):
    tmp_path, watchdog, state, fake = sandbox
    _r, alerts, log = _tick(tmp_path, watchdog, state, fake, TEST_PRIMARY_RC=3)
    assert [a.split(":", 1)[0] for a in alerts] == ["primary", "fallback"]
    assert "page not delivered" not in log
    _r, alerts, _log = _tick(tmp_path, watchdog, state, fake)
    assert len(alerts) == 2                                           # delivered: rate limited


def test_a_broken_escalation_helper_still_pages_once_per_window(sandbox):
    tmp_path, watchdog, state, fake = sandbox
    _r, alerts, _log = _tick(tmp_path, watchdog, state, fake, TEST_ESCALATE_BROKEN=1)
    assert len(alerts) == 1 and "escalation helper failed" in alerts[0]
    assert "real configuration invalid" in alerts[0]
    _r, alerts, _log = _tick(tmp_path, watchdog, state, fake, TEST_ESCALATE_BROKEN=1)
    assert len(alerts) == 1                                           # stamped: not every minute


def test_a_transport_failure_probes_the_host_network_and_marks_it_down(sandbox):
    """Alpha GO (b), 2026-09-29: >= 3 funded RPC transport errors run a host network probe. A failed
    probe is logged and marked, because no page can leave until the network returns."""
    tmp_path, watchdog, state, fake = sandbox
    _r, _alerts, log = _tick(tmp_path, watchdog, state, fake, TEST_VERIFY_OUT=ERR.replace(
        '"]}', '\\nSolana funded RPC rotated reason=transport_error from_index=2 to_index=3"]}'),
        MEMECOIN_WATCHDOG_NET_PROBE="exit 1")
    assert "host network down" in log
    mark = state / "data" / "memecoin_watchdog_netdown"
    assert mark.exists() and len(mark.read_text().split()) == 1
    _r, _alerts, log = _tick(tmp_path, watchdog, state, fake, TEST_VERIFY_OUT=ERR.replace(
        '"]}', '\\nSolana funded RPC rotated reason=transport_error from_index=2 to_index=3"]}'))
    assert "host network back" in log and len(mark.read_text().split()) == 2   # the back time is kept


def test_two_transport_errors_do_not_probe(sandbox):
    tmp_path, watchdog, state, fake = sandbox
    probed = tmp_path / "probed"
    _tick(tmp_path, watchdog, state, fake, MEMECOIN_WATCHDOG_NET_PROBE="touch %s; exit 1" % probed)
    assert not probed.exists()                                        # ERR carries 2


def test_the_recovery_page_names_how_long_the_host_network_was_dark(sandbox):
    import time
    tmp_path, watchdog, state, fake = sandbox
    _tick(tmp_path, watchdog, state, fake)                            # opens the failure streak
    now = int(time.time())
    (state / "data" / "memecoin_watchdog_netdown").write_text("%d %d\n" % (now - 2400, now - 60))
    _r, alerts, _log = _tick(tmp_path, watchdog, state, fake, healthy=True)
    assert "RECOVERED" in alerts[-1] and "host network was down 39m" in alerts[-1]
    assert not (state / "data" / "memecoin_watchdog_netdown").exists()


def test_a_second_outage_in_the_same_streak_restarts_the_clock(sandbox):
    """Gamma LOW 2026-09-29: down, back, down again inside one refusal streak kept the first back
    time, so the recovery page understated the dark minutes. A failed probe after a back time
    starts a new outage."""
    tmp_path, watchdog, state, fake = sandbox
    mark = state / "data" / "memecoin_watchdog_netdown"
    mark.write_text("1000 2000")
    three = ERR.replace('"]}', '\\nSolana funded RPC rotated reason=transport_error from_index=2 to_index=3"]}')
    _tick(tmp_path, watchdog, state, fake, TEST_VERIFY_OUT=three, MEMECOIN_WATCHDOG_NET_PROBE="exit 1")
    words = mark.read_text().split()
    assert len(words) == 1 and int(words[0]) > 2000
