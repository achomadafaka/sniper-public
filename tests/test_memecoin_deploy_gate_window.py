"""Nothing that trades nothing may sit inside a deploy's entry-gated window.

Every deploy trips the entry kill switch before stopping the old runner and clears it once the new
runner is verified. On the night of 2026-09-26/27, 15 deploys held entries ~350 s in total, and at
00:47Z untaxxable's aqhpFfoG buy was refused kill_switch 0.1 s before the CLEAR. The Solana tape
worker's stop (a kill plus up to 5 s of polling, for an observation-only lane) ran inside that
window, between the verification and the CLEAR.
"""
from pathlib import Path

SCRIPT = (Path(__file__).resolve().parents[1] / "scripts" / "deploy_memecoin_runtime.sh").read_text()


def _order(script):
    verified = script.index("new runner failed PID/path/heartbeat verification")
    clear = script.index('clear_deploy_entry_gate || fail "could not restore entry admission"')
    trap_off = script.index("trap - EXIT", clear)
    tape = script.index('stop_stale_worker "sol tape worker"')
    return verified, clear, trap_off, tape


def test_the_tape_worker_stop_runs_after_the_clear_and_the_rollback_trap():
    verified, clear, trap_off, tape = _order(SCRIPT)
    assert verified < clear < trap_off < tape


def test_nothing_waits_between_the_verification_and_the_clear():
    verified, clear, _trap_off, _tape = _order(SCRIPT)
    window = SCRIPT[verified:clear]
    assert "sleep" not in window and "kill " not in window


STEPS = ("entry_gate_trip", "entries_drained", "old_runner_stopped", "switched",
         "lifi_activate_start", "lifi_activate_end", "new_runner_launch", "new_runner_verified",
         "entry_gate_clear")


def test_every_cutover_step_is_stamped_in_order():
    """The deploy log had no timestamps, so the ~6.5 s with no runner after the switch could not
    be split between the LI.FI activation and the second verify."""
    positions = [SCRIPT.index('step "%s' % name) for name in STEPS]
    assert positions == sorted(positions)
    assert SCRIPT.count('step "verify_start') == 1 and SCRIPT.count('step "verify_end"') == 1


def test_a_step_line_carries_a_millisecond_utc_stamp():
    import re
    import subprocess
    start = SCRIPT.index("step() {")
    define = SCRIPT[start:SCRIPT.index("\n}\n", start) + 2]
    out = subprocess.run(["bash", "-euo", "pipefail", "-c", define + "\nstep switched"],
                         capture_output=True, text=True, check=True).stdout
    assert re.fullmatch(r"deploy step \d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z switched\n", out), out
    # A closed stdout must not abort the cutover under set -e.
    closed = subprocess.run(["bash", "-euo", "pipefail", "-c",
                             define + "\nexec 1>&-\nstep switched\necho ALIVE >&2"],
                            capture_output=True, text=True)
    assert closed.returncode == 0 and "ALIVE" in closed.stderr



def test_only_the_post_switch_launch_tells_the_watchdog_the_release_is_verified():
    """The watchdog skips its own verify only for a release the deploy named, so the flag may
    appear on exactly one call: the launch of the release this deploy just verified. A rollback
    relaunch of the old release must verify in full."""
    assert SCRIPT.count("MEMECOIN_WATCHDOG_RELEASE_VERIFIED") == 1
    flagged = SCRIPT.index('MEMECOIN_WATCHDOG_RELEASE_VERIFIED="$SHA"')
    call = SCRIPT[flagged:SCRIPT.index("\n", SCRIPT.index("memecoin_watchdog.sh", flagged))]
    assert '"$CURRENT/scripts/memecoin_watchdog.sh"' in call
    assert SCRIPT.index('step "new_runner_launch"') < flagged
    assert SCRIPT.index("\nverify_candidate_release\n") < flagged    # the deploy verified first
    for line in SCRIPT.splitlines():
        if '"$OLD_RELEASE/scripts/memecoin_watchdog.sh"' in line:
            assert "RELEASE_VERIFIED" not in line


def test_a_failing_verify_stops_the_deploy_before_any_watchdog_call():
    import subprocess
    start = SCRIPT.index("fail() {")
    fail = SCRIPT[start:SCRIPT.index("\n", start) + 1]
    step_start = SCRIPT.index("step() {")
    step = SCRIPT[step_start:SCRIPT.index("\n}\n", step_start) + 3]
    v_start = SCRIPT.index("verify_candidate_release() {")
    verify = SCRIPT[v_start:SCRIPT.index("\n}\n", v_start) + 3]
    harness = "\n".join([
        "set -euo pipefail", "LIFI_REACTIVATED=0", "LIFI_REACTIVATION_CANARY=", "RELEASE=r",
        "STATE_ROOT=s", "CANDIDATE_MANIFEST=c", "MANIFEST=m", "PY=python3", "DEFAULT_SOURCE=.",
        "export SNIPER_DEPLOY_VERIFY_COMMAND='exit 1'", fail, step, verify,
        "verify_candidate_release", "echo REACHED_THE_WATCHDOG_CALL"])
    result = subprocess.run(["bash", "-c", harness], capture_output=True, text=True)
    assert result.returncode != 0 and "REACHED_THE_WATCHDOG_CALL" not in result.stdout
    assert "release verifier hook failed" in result.stderr
