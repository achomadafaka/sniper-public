import json
from pathlib import Path
import pytest

from scripts import run_hl_copy as R


def test_watchdog_identity_includes_filter_and_manifest():
    source = Path("scripts/hl_copy_watchdog.sh").read_text(encoding="utf-8")
    manifest_line = next(line for line in source.splitlines() if line.startswith("ARM_MANIFEST="))
    args_line = next(line for line in source.splitlines() if line.startswith("ARGS="))
    match_line = next(line for line in source.splitlines() if line.startswith("MATCH="))
    launch_line = next(line for line in source.splitlines() if line.startswith("LAUNCH="))
    # Deliberate hardcode: changing the ACTIVE arm must be an explicit, reviewed edit here.
    # v14 -> v15 on 2026-08-03 because fe9831d8 edited frozen copy code (edge_desk display)
    # without minting a rollover, so v14's pin no longer resolved and the arm would have
    # refused to start on the next HL runtime deploy. Copy semantics carried verbatim; see
    # test_forward_vs_promotion_rollover_preserves_frozen_copy_semantics.
    assert "forward_arm.v16.json" in manifest_line
    assert 'CODE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd -P)"' in source
    assert '$CODE_ROOT/scripts/run_hl_copy.py' in source
    for required in ("--alt-short-only", "--arm-manifest", "$ARM_MANIFEST",
                     "--legacy-interval-seconds 300"):
        assert required in args_line
    # LAUNCH carries the full ARGS; MATCH deliberately does NOT — see
    # test_hl_watchdogs_match_release_independently_so_a_repoint_reattaches.
    assert "$ARGS" in launch_line


def test_paper_watchdogs_launch_from_pinned_code_and_write_shared_state():
    for name, runner in (
        ("hl_altshort_paper_watchdog.sh", "run_hl_altshort_paper.py"),
        ("hl_squeeze_tape_watchdog.sh", "run_hl_squeeze_tape.py"),
    ):
        source = Path("scripts", name).read_text(encoding="utf-8")
        assert 'CODE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd -P)"' in source
        assert 'STATE_ROOT="${SNIPER_STATE_ROOT:-/home/bigpapi/Desktop/Sniper}"' in source
        assert f"$CODE_ROOT/scripts/{runner}" in source
        assert "--heartbeat $HEARTBEAT" in source
        if name == "hl_altshort_paper_watchdog.sh":
            assert "--funding-cache-dir $FUNDING_CACHE" in source
            assert "--funding-tape $FUNDING_TAPE" in source


def test_copy_heartbeat_contains_exact_arm_identity(tmp_path):
    arm = {
        "arm_id": "forward-v1", "activated_ts_ms": 1000, "policy_hash": "p123",
        "code_sha": "abc", "targets_json": json.dumps(["0xabc"]),
        "taxonomy_version": "altshort-core-v1",
    }
    payload = R._heartbeat_payload(
        now_ms=2000, code_sha="abc", dirty=False, arm=arm,
        policy=R.CF.EntryPolicy(alt_short_only=True), summary={"open": 2, "closed": 3},
    )
    assert payload == {
        "ts_ms": 2000, "code_sha": "abc", "dirty": False,
        "alt_short_only": True, "open": 2, "closed": 3,
        "depth_capture_format_version": 2,
        "depth_capture_scope": "provider_returned_snapshot",
        "arm_id": "forward-v1", "activated_ts_ms": 1000, "policy_hash": "p123",
        "evidence_arm_id": "forward-v1", "deployment_arm_id": "forward-v1",
        "targets": ["0xabc"], "taxonomy_version": "altshort-core-v1",
    }
    path = tmp_path / "heartbeat.json"
    R._write_heartbeat(path, payload)
    assert json.loads(path.read_text(encoding="utf-8")) == payload


def test_copy_heartbeat_exposes_source_fill_health_without_recasting_values():
    payload = R._heartbeat_payload(
        now_ms=2000, code_sha="abc", dirty=False, arm=None,
        policy=R.CF.EntryPolicy(), summary={"open": 0, "closed": 0},
        source={"connected": True, "instrumented_wallets": 9, "queue_depth": 2,
                "last_error": None, "connection_epoch": 3},
    )
    assert payload["source_fills"] == {
        "connected": True, "instrumented_wallets": 9, "queue_depth": 2,
        "last_error": None, "connection_epoch": 3,
    }


def test_source_health_includes_unmatched_and_overflow_completeness(tmp_path):
    from sniper.hyperlive import intel_store as IS

    conn = IS.connect(str(tmp_path / "intel.sqlite"))
    IS.ensure_schema(conn)
    IS.record_copy_source_events(conn, [{
        "wallet": "0xabc", "tid": "7", "coin": "ETH", "source_event_ts_ms": 1000,
        "received_ts_ms": 1100, "connection_epoch": 1,
    }])
    IS.record_copy_source_gaps(conn, [{
        "start_received_ts_ms": 1150, "end_received_ts_ms": 1200,
        "connection_epoch": 1, "dropped_count": 1, "reason": "queue_overflow",
    }], persisted_ts_ms=1300)

    class Collector:
        def status(self):
            return {"last_event_ts_ms": 1200, "overflow_count": 1, "connected": True}

    health = R._source_health(conn, Collector(), wallets=["0xABC"], now_ms=2000)
    assert health["last_event_age_ms"] == 800
    assert health["unmatched_count"] == 1
    assert health["gap_count"] == 1
    assert health["interval_complete"] is False


# ---- duplicate-worker regression (2026-08-03) -------------------------------------------------

# run_hl_copy.py is inside the alt-short arm's FROZEN code_paths, so adding its lock requires
# minting a rollover; it lands in the immediately-following change with manifest v16.
HL_RUNNERS = [
    ("scripts/run_hl_altshort_paper.py", "HL_ALTSHORT_PAPER_RUN_LOCK"),
    ("scripts/run_hl_squeeze_tape.py", "HL_SQUEEZE_TAPE_RUN_LOCK"),
]


@pytest.mark.parametrize("script,env", HL_RUNNERS)
def test_hl_runner_holds_a_process_lifetime_lock(script, env):
    """Every cron-scheduled HL runner must take a lock IT holds, not rely on its watchdog.

    The watchdogs match a running process by `$CODE_ROOT/scripts/<runner>.py $ARGS`, and
    CODE_ROOT resolves through `.runtime/hyperliquid/current`. Repointing that symlink makes
    the running process stop matching, so the next cron tick starts a SECOND one against the
    same ledger. Two repoints on 2026-08-03 left three copies of two of these workers writing
    one DB each (2-4 ticks/minute on a 1/minute cadence). A watchdog-side flock serialises
    watchdogs, not workers — only a lock the runner holds for its lifetime closes this.
    """
    source = Path(script).read_text(encoding="utf-8")
    assert "acquire_run_lock" in source, f"{script} takes no run lock"
    assert env in source, f"{script} does not allow overriding its lock path via {env}"
    assert "refusing to start" in source, f"{script} does not refuse on a held lock"


@pytest.mark.parametrize("script,env", HL_RUNNERS)
def test_hl_runner_refuses_to_start_while_another_holds_the_lock(tmp_path, script, env):
    """The real exclusion, exercised through a second OS process (an flock is per-open-file-
    description, so a same-process re-acquire would pass and prove nothing)."""
    import subprocess
    import sys as _sys
    from sniper.services.memecoin_runner import acquire_run_lock

    lock = str(tmp_path / "runner.lock")
    held = acquire_run_lock(lock)
    assert held is not None
    probe = (
        "import sys; sys.path.insert(0, %r)\n"
        "from sniper.services.memecoin_runner import acquire_run_lock\n"
        "print(acquire_run_lock(%r) is not None)\n" % (str(Path.cwd()), lock)
    )
    try:
        blocked = subprocess.run([_sys.executable, "-c", probe],
                                 capture_output=True, text=True, timeout=30).stdout.strip()
    finally:
        held.close()
    assert blocked == "False", f"a second {script} instance acquired the lock"

    freed = subprocess.run([_sys.executable, "-c", probe],
                           capture_output=True, text=True, timeout=30).stdout.strip()
    assert freed == "True", "lock not released when the holder exits"


@pytest.mark.parametrize("name,runner", [
    ("hl_copy_watchdog.sh", "run_hl_copy.py"),
    ("hl_altshort_paper_watchdog.sh", "run_hl_altshort_paper.py"),
    ("hl_squeeze_tape_watchdog.sh", "run_hl_squeeze_tape.py"),
    ("hl_real_watchdog.sh", "run_hl_real.py"),
])
def test_hl_watchdogs_match_release_independently_so_a_repoint_reattaches(name, runner):
    """MATCH must NOT be release-qualified; LAUNCH must stay pinned.

    CODE_ROOT resolves through .runtime/hyperliquid/current. With a release-scoped match, a
    worker started before a repoint stops matching, so the watchdog relaunches it every minute,
    the runner's own lock refuses the duplicate, and it logs FAILED_TO_START forever while the
    deploy silently never takes effect — verified 2026-08-03 by repointing the symlink under a
    live worker. For hl_real it was worse still: the same match drives the retirement sweep, so
    the orphan could not be killed either.

    Keyed on the script path plus the absolute --db-path, so it stays scoped to one state root.
    hl_copy in particular cannot key on $ARGS: that embeds a $CODE_ROOT manifest path.
    """
    source = Path("scripts", name).read_text(encoding="utf-8")
    match_line = next(line for line in source.splitlines() if line.startswith("MATCH="))
    launch_line = next(line for line in source.splitlines() if line.startswith("LAUNCH="))

    assert "$CODE_ROOT" not in match_line, f"{name} MATCH is release-scoped again"
    assert f"scripts/{runner}" in match_line
    assert "--db-path $DB" in match_line, f"{name} MATCH must stay state-root scoped"
    assert f"$CODE_ROOT/scripts/{runner}" in launch_line, f"{name} LAUNCH must stay pinned"
