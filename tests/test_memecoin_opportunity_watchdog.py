from pathlib import Path


WATCHDOG = Path("scripts/memecoin_opportunity_watchdog.sh").read_text()
ALERT_WATCHDOG = Path("scripts/radar_alert_lane_watchdog.sh").read_text()


def test_watchdog_uses_worker_env_loader_instead_of_parsing_dotenv_in_shell():
    assert "grep -E '^MEMECOIN_OPPORTUNITY_CAPTURE='" not in WATCHDOG
    assert '"$PY" -u "$WORKER" --enabled-check' in WATCHDOG


def test_watchdog_executes_the_worker_from_its_pinned_release_but_keeps_shared_state():
    """The LAUNCH stays pinned to the release; only DISCOVERY became release-independent.

    `MATCH` used to carry `$RUNTIME_ROOT` and served both jobs. That is the double-start hazard in
    the 2026-08-07 deploy note: repointing `current` while the worker runs makes the next cron tick
    fail to match the old process and start a SECOND worker on the same database. Discovery now
    matches any release; the worker it starts is still the pinned one.
    """
    assert 'RUNTIME_ROOT=$(cd -P "$(dirname "$0")/.." && pwd)' in WATCHDOG
    assert 'STATE_ROOT=/home/bigpapi/Desktop/Sniper' in WATCHDOG
    assert 'export PYTHONPATH="$RUNTIME_ROOT${PYTHONPATH:+:$PYTHONPATH}"' in WATCHDOG
    assert 'export SNIPER_STATE_ROOT="$STATE_ROOT"' in WATCHDOG
    assert 'WORKER="$RUNTIME_ROOT/scripts/memecoin_opportunity_worker.py"' in WATCHDOG
    assert 'MATCH="scripts/memecoin_opportunity_worker.py"' in WATCHDOG
    assert 'setsid nohup "$PY" -u "$WORKER"' in WATCHDOG


def test_alert_watchdog_uses_direct_quote_status_not_alert_counts():
    assert "op_runtime_status" in ALERT_WATCHDOG
    assert "opportunity:quote:" in ALERT_WATCHDOG
    assert "op_insight_alerts" not in ALERT_WATCHDOG
    assert "op_insight_samples" not in ALERT_WATCHDOG


def test_alert_watchdog_reads_the_hot_db_from_the_shared_state_root():
    assert 'DB="$ROOT/data/memecoin_opportunity.db"' in ALERT_WATCHDOG
    assert 'ROOT="${SNIPER_STATE_ROOT:-$CODE_ROOT}"' in ALERT_WATCHDOG
    assert "QUOTE_PATH_FAILURE" in ALERT_WATCHDOG
    assert "QUOTE_PATH_STALE" in ALERT_WATCHDOG
