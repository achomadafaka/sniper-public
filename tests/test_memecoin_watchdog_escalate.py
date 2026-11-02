"""Watchdog outage escalation (2026-07-21): during the funded-runner crash-loop the watchdog
paged an identical, reason-less line every minute for 26 minutes. The escalation helper turns
that into: one page on FIRST failure (with the actual error), a repeat page every
--repeat-s while still down (with downtime + attempt count), and one RECOVERY page.
State is a JSON file; the bash watchdog calls `failure`/`recovery` and pages iff stdout
is non-empty."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "memecoin_watchdog_escalate.py"


def run(args, now):
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), *args, "--now", str(now)],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_first_failure_pages_immediately_with_reason(tmp_path):
    state = tmp_path / "wd.json"
    msg = run(["failure", "--state", str(state),
               "--reason", "funded contract reconciliation failed: pending 172"], now=1000.0)
    assert msg, "first failure must page"
    assert "funded contract reconciliation failed: pending 172" in msg
    saved = json.loads(state.read_text())
    assert saved["count"] == 1


def test_repeat_failures_page_only_after_interval(tmp_path):
    state = tmp_path / "wd.json"
    assert run(["failure", "--state", str(state), "--reason", "boom"], now=1000.0)
    # 2nd..Nth failure inside the repeat window -> silent
    for i in range(2, 10):
        out = run(["failure", "--state", str(state), "--reason", "boom",
                   "--repeat-s", "900"], now=1000.0 + 60 * (i - 1))
        assert out == "", f"failure {i} inside window must not page (got {out!r})"
    # past the window -> escalation page with downtime + attempt count
    msg = run(["failure", "--state", str(state), "--reason", "boom",
               "--repeat-s", "900"], now=1000.0 + 901)
    assert msg
    assert "15m" in msg          # ~901s downtime
    assert "10" in msg           # 10 consecutive failures
    assert "boom" in msg


def test_recovery_pages_once_and_clears_state(tmp_path):
    state = tmp_path / "wd.json"
    run(["failure", "--state", str(state), "--reason", "boom"], now=1000.0)
    run(["failure", "--state", str(state), "--reason", "boom"], now=1060.0)
    msg = run(["recovery", "--state", str(state), "--detail", "pid 4242"], now=1200.0)
    assert msg and "pid 4242" in msg
    assert "2" in msg            # after 2 failed attempts
    assert not state.exists(), "recovery must clear the state file"
    # recovery with no prior failure state -> silent (healthy path runs every minute)
    assert run(["recovery", "--state", str(state), "--detail", "pid 4242"], now=1300.0) == ""


def test_corrupt_state_never_blocks_paging(tmp_path):
    state = tmp_path / "wd.json"
    state.write_text("{not json")
    msg = run(["failure", "--state", str(state), "--reason", "boom"], now=1000.0)
    assert msg, "corrupt state must behave like first failure, not crash"
    assert json.loads(state.read_text())["count"] == 1


def test_watchdog_launch_poll_default_is_the_safe_rate():
    """The launch default must be 5000ms, never 1500.

    fomo's own client polls the same REST /feed at 15s. At 1500ms we ran ~10x their app
    (57,600 req/day vs 5,760) and the account was blocked at the APPLICATION layer on
    2026-07-31 — a valid Privy JWT 403'd on every endpoint while a garbage token from the same
    IP still got a clean 401. It cost 4 days of paper collection and the account.

    This default is load-bearing: `load_env_files` is setdefault and the watchdog exports the
    var before the runner reads any env file, so a `.env` entry can NEVER override it. And
    `deploy_memecoin_runtime.sh --restart` invokes this script without the cron env, so the
    default is what a deploy-triggered launch actually gets.
    """
    from pathlib import Path
    script = (Path(__file__).resolve().parents[1] / "scripts" / "memecoin_watchdog.sh").read_text()
    assert 'MEMECOIN_POLL_MS="${MEMECOIN_POLL_MS:-5000}"' in script
    assert "MEMECOIN_POLL_MS:-1500" not in script


def test_watchdog_releases_unsigned_reservations_only_after_runner_stop():
    script = (Path(__file__).resolve().parents[1] / "scripts" / "memecoin_watchdog.sh").read_text()
    stale_kill = script.index('kill $PINNED_PIDS 2>/dev/null || true')
    release = script.index("release_unsigned_after_runner_stop", stale_kill)
    launch = script.index('log "pinned runner not running; launching sha', release)
    assert stale_kill < release
    assert release < launch
    assert 'kill -0 "$PINNED_PIDS" 2>/dev/null && {' in script


def test_an_undelivered_page_is_retried_on_the_next_tick(tmp_path):
    """2026-09-29: the state advanced before delivery, so a page Telegram never took was treated as
    sent and the next one waited the whole repeat window. `unsent` rewinds the alert clock only."""
    state = tmp_path / "wd.json"
    assert run(["failure", "--state", str(state), "--reason", "r"], now=1000.0)
    assert run(["unsent", "--state", str(state)], now=1000.0) == ""
    assert json.loads(state.read_text())["count"] == 1
    assert "STILL DOWN" in run(["failure", "--state", str(state), "--reason", "r"], now=1060.0)
    assert run(["failure", "--state", str(state), "--reason", "r"], now=1120.0) == ""   # delivered
    missing = tmp_path / "none.json"
    assert run(["unsent", "--state", str(missing)], now=1.0) == "" and not missing.exists()
