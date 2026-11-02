"""The 4663 feed and the runner go to cron in ONE handoff (Alpha go, 2026-10-15).

Since the RH feed moved to its own cron handoff (1135391e6, live 2026-10-14 13:56Z) the deploy handed
the feed to cron, waited for it, and only then handed the runner over. The feed handoff lands just
before a minute tick, so the runner handoff landed just after it and waited ~55 s for the next one:
every deploy since took 83-84 s from the entry-gate trip to the runner launch (22.5-26 s before),
and 92c45c608 took 143.8 s when a slow verify pushed even the feed past its tick.

Now the deploy prepares the runner launch first (drain, stop, unsigned releases), then releases the
feed's watchdog lock together with the runner's, so one cron tick launches both. The rollback paths
keep feed-then-runner: the rollover rollback launches the old runner only after the old feed came up.
The block under test is the deploy's own top-level code between its JOINT_CRON_LAUNCH markers."""
import os
import shlex
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "scripts/deploy_memecoin_runtime.sh"


def _function(name):
    text = DEPLOY.read_text()
    body = text[text.index("\n" + name + "() {") + 1:]
    return body[:body.index("\n}\n") + 3]


def _launch_block():
    text = DEPLOY.read_text()
    begin, end = "# JOINT_CRON_LAUNCH_BEGIN\n", "# JOINT_CRON_LAUNCH_END\n"
    assert begin in text and end in text, "joint cron launch markers missing"
    return text[text.index(begin) + len(begin):text.index(end)]


def _run(tmp_path, *, via_cron, shyft, runner_rc=0, rh_rc=0):
    events = tmp_path / "events"
    release = tmp_path / "release"
    (release / "scripts").mkdir(parents=True)
    (release / "scripts/memecoin_rh_feed_shadow_watchdog.sh").write_text(
        'printf "rh_direct\\n" >> "$TEST_EVENTS"\n')
    (release / "scripts/memecoin_watchdog.sh").write_text(
        'printf "runner_direct\\n" >> "$TEST_EVENTS"\n')
    fd7 = 'if { : >&7; } 2>/dev/null; then s=held; else s=released; fi'
    harness = "\n".join([
        "set -u",
        f"STATE_ROOT={shlex.quote(str(tmp_path))}; RELEASE={shlex.quote(str(release))}",
        "CURRENT=$RELEASE; SOURCE_ROOT=$RELEASE; SHA=abc; PY=true; PROVIDER_ENV=''",
        f"LAUNCH_VIA_CRON={int(via_cron)}; SHYFT_RUNTIME={int(shyft)}",
        "PROVIDER_CANDIDATE_LAUNCHED=0; PROVIDER_RH_CRON_HANDOFF=0",
        f"MEMECOIN_PIDFILE={shlex.quote(str(tmp_path / 'runner.pid'))}",
        f"exec 7>{shlex.quote(str(tmp_path / 'rh.lock'))}",
        'ev() { printf "%s\\n" "$*" >> "$TEST_EVENTS"; }',
        'step() { ev "step ${1%% *}"; }',
        'fail() { ev "fail $*"; exit 1; }',
        'wait_for_exit_drain() { ev drain; }',
        'provider_runner_pids() { :; }',
        'release_unsigned_reservations_after_runner_stop() { ev release_reservations; }',
        'release_unsigned_close_claims_after_runner_stop() { ev release_claims; }',
        'cron_launch_failed_loudly() { ev loud_fallback; }',
        'wait_for_cron_launch() { %s; ev "runner_cron_wait fd7=$s"; return %d; }' % (fd7, runner_rc),
        'wait_for_provider_rh_cron_launch() { %s; ev "rh_cron_wait handoff=$PROVIDER_RH_CRON_HANDOFF '
        'fd7=$s"; return %d; }' % (fd7, rh_rc),
        _function("start_provider_rh"),
        _launch_block(),
        'ev "done candidate=$PROVIDER_CANDIDATE_LAUNCHED"',
    ])
    result = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, timeout=10,
                            env=os.environ | {"TEST_EVENTS": str(events)})
    return result, events.read_text().splitlines()


PREPARE = ["drain", "release_reservations", "release_claims", "step new_runner_launch"]


def test_one_cron_tick_launches_the_feed_and_the_runner(tmp_path):
    result, events = _run(tmp_path, via_cron=True, shyft=True)
    assert result.returncode == 0, result.stderr
    # The feed is not launched before the runner is prepared, and its lock is already released
    # when the runner's cron wait starts (that wait releases the runner's lock before its loop).
    assert events == PREPARE + ["step rh_launch_handed_to_cron",
                                "runner_cron_wait fd7=released",
                                "rh_cron_wait handoff=1 fd7=released",
                                "done candidate=1"], events


def test_a_feed_the_cron_did_not_launch_still_fails_the_deploy(tmp_path):
    result, events = _run(tmp_path, via_cron=True, shyft=True, rh_rc=1)
    assert result.returncode == 1
    assert events[-2:] == ["rh_cron_wait handoff=1 fd7=released",
                           "fail new RH provider process failed"], events
    # The rollback then stops every candidate process, the new runner included.
    assert "runner_cron_wait fd7=released" in events


def test_a_runner_the_cron_did_not_launch_keeps_the_loud_fallback_then_waits_for_the_feed(tmp_path):
    result, events = _run(tmp_path, via_cron=True, shyft=True, runner_rc=1)
    assert result.returncode == 0, result.stderr
    assert events[-4:] == ["loud_fallback", "runner_direct",
                           "rh_cron_wait handoff=1 fd7=released", "done candidate=1"], events


def test_a_system_slice_deploy_keeps_the_direct_feed_then_runner_order(tmp_path):
    result, events = _run(tmp_path, via_cron=False, shyft=True)
    assert result.returncode == 0, result.stderr
    assert events == ["rh_direct"] + PREPARE + ["runner_direct", "done candidate=1"], events


def test_without_the_shyft_runtime_only_the_runner_is_handed_over(tmp_path):
    result, events = _run(tmp_path, via_cron=True, shyft=False)
    assert result.returncode == 0, result.stderr
    assert events == PREPARE + ["runner_cron_wait fd7=held", "done candidate=0"], events


@pytest.mark.parametrize("handed", [False, True])
def test_the_feed_wait_names_its_handoff_once(tmp_path, handed):
    """The joint handoff already logged rh_launch_handed_to_cron; the later feed wait must not log
    a second, misleading one."""
    (tmp_path / "data").mkdir()
    fake = tmp_path / "python"
    fake.write_text("#!/bin/sh\necho 42 7000\n")
    fake.chmod(0o755)
    events = tmp_path / "events"
    harness = "\n".join([
        "set -u", f"PY={fake}", f"STATE_ROOT={tmp_path}", f"DEFAULT_SOURCE={tmp_path}",
        "MEMECOIN_DEPLOY_CRON_WAIT_S=5; PROVIDER_RECOVERY_REQUIRED=0",
        f"PROVIDER_RH_CRON_HANDOFF={int(handed)}",
        'step() { printf "%s\\n" "${1%% *}" >> "$TEST_EVENTS"; }',
        "flock() { :; }", "sleep() { :; }",
        _function("retake_provider_rh_watchdog_lock"),
        _function("wait_for_provider_rh_cron_launch"),
        f"wait_for_provider_rh_cron_launch {tmp_path / 'release'}; echo rc=$?"])
    result = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, timeout=10,
                            env=os.environ | {"TEST_EVENTS": str(events)})
    assert "rc=0" in result.stdout, result.stderr
    assert events.read_text().splitlines() == (
        ([] if handed else ["rh_launch_handed_to_cron"]) + ["rh_launched_by_cron"])
