from pathlib import Path


def test_real_watchdog_restarts_a_live_process_with_stale_heartbeat():
    source = Path("scripts/hl_real_watchdog.sh").read_text(encoding="utf-8")
    assert 'CODE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd -P)"' in source
    assert 'STATE_ROOT="${SNIPER_STATE_ROOT:-/home/bigpapi/Desktop/Sniper}"' in source
    assert '$CODE_ROOT/scripts/run_hl_real.py' in source   # LAUNCH stays pinned
    assert "HEARTBEAT=" in source
    assert "HEARTBEAT_MAX_AGE_SECONDS=" in source
    assert "heartbeat_fresh" in source
    assert "STALE_HEARTBEAT" in source


# ---- duplicate-worker / retirement-sweep audit (2026-08-03) ------------------------------------

def test_real_runner_holds_a_process_lifetime_lock():
    """This leg signs REAL orders, so a second copy is a double-submit. The watchdog cannot
    prevent one alone — it matches a running process by path, and after a release repoint the
    old process stops matching, so the next cron tick launches another."""
    source = Path("scripts/run_hl_real.py").read_text(encoding="utf-8")
    assert "acquire_run_lock" in source
    assert "HL_REAL_RUN_LOCK" in source
    assert "double-submit" in source


def test_retirement_sweep_matches_runners_from_ANY_release():
    """The `HL_REAL_WATCHDOG_ENABLED != 1` branch kills whatever it finds and is the mechanism
    keeping this leg retired. It shares MATCH with find_existing, so a release-scoped match let
    a runner started from a PREVIOUS release survive the sweep — unkillable through the
    watchdog, while HL_REAL_ENABLED=1 sits in .env. Reproduced 2026-08-03 with a stand-in
    process on an old release path.

    MATCH must therefore NOT be release-qualified; LAUNCH must stay pinned to CODE_ROOT.
    """
    source = Path("scripts/hl_real_watchdog.sh").read_text(encoding="utf-8")
    match_line = next(line for line in source.splitlines() if line.startswith("MATCH="))
    launch_line = next(line for line in source.splitlines() if line.startswith("LAUNCH="))

    assert "$CODE_ROOT" not in match_line, "MATCH is release-scoped; the sweep will miss orphans"
    assert "scripts/run_hl_real.py" in match_line
    assert "--db-path $DB" in match_line, "MATCH must stay scoped to this state root's runner"
    assert "$CODE_ROOT/scripts/run_hl_real.py" in launch_line, "LAUNCH must stay release-pinned"


def test_retirement_sweep_is_still_the_default():
    source = Path("scripts/hl_real_watchdog.sh").read_text(encoding="utf-8")
    assert '"${HL_REAL_WATCHDOG_ENABLED:-0}" != "1"' in source
    assert "DISABLED stopped_pid=" in source
