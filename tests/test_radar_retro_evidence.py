import json
import sqlite3
import subprocess
import sys
from pathlib import Path


TARGETS = (200, 400, 600, 800, 1_200)
HORIZONS = (5_000, 10_000, 20_000, 60_000, 120_000, 300_000)


def _insert_alert(
    conn,
    *,
    alert_id,
    launch_id,
    observed_at,
    score_status="SCORED",
    evaluator_version="alert-outcome-v4",
    complete_grid=True,
):
    conn.execute(
        "INSERT INTO op_insight_alerts VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            alert_id,
            launch_id,
            "pump-confirmation-truth-v5",
            observed_at,
            evaluator_version,
            score_status,
            100,
            json.dumps({"launch_mode": "standard", "observed_pair_return_bps": -400}),
            "creator-1",
            "token-1",
        ),
    )
    cells = [(target, horizon) for target in TARGETS for horizon in HORIZONS]
    if not complete_grid:
        cells.pop()
    for target, horizon in cells:
        conn.execute(
            "INSERT INTO op_insight_alert_targets VALUES(?,?,?,?,?,?)",
            (alert_id, target, horizon, "target-ladder-v2", "hit", -900),
        )


def test_retro_evidence_uses_the_sealed_cohort_contract(tmp_path):
    """The report excludes invalid, incomplete, wrong-version, and duplicate alerts."""
    db = tmp_path / "insights.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE op_insight_alerts("
        "alert_id TEXT,launch_id TEXT,rule_version TEXT,observed_at TEXT,"
        "evaluator_version TEXT,score_status TEXT,terminal_return_bps INTEGER,"
        "feature_json TEXT,creator_id TEXT,token_id TEXT);"
        "CREATE TABLE op_insight_alert_targets("
        "alert_id TEXT,target_bps INTEGER,horizon_ms INTEGER,evaluator_version TEXT,"
        "result TEXT,worst_return_bps INTEGER);"
    )
    _insert_alert(
        conn,
        alert_id="valid",
        launch_id="launch-1",
        observed_at="2026-08-01T00:00:00Z",
    )
    _insert_alert(
        conn,
        alert_id="same-launch-later",
        launch_id="launch-1",
        observed_at="2026-08-01T00:01:00Z",
    )
    _insert_alert(
        conn,
        alert_id="invalid",
        launch_id="launch-2",
        observed_at="2026-08-01T00:02:00Z",
        score_status="DATA_GAP_INVALID",
    )
    _insert_alert(
        conn,
        alert_id="wrong-version",
        launch_id="launch-3",
        observed_at="2026-08-01T00:03:00Z",
        evaluator_version="alert-outcome-v3",
    )
    _insert_alert(
        conn,
        alert_id="incomplete",
        launch_id="launch-4",
        observed_at="2026-08-01T00:04:00Z",
        complete_grid=False,
    )
    _insert_alert(
        conn,
        alert_id="after-cutoff",
        launch_id="launch-5",
        observed_at="2026-08-08T00:00:00Z",
    )
    conn.commit()
    conn.close()

    script = Path(__file__).parents[1] / "scripts" / "radar_retro_evidence.py"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--db",
            str(db),
            "--since",
            "2026-07-30T15:32:00Z",
            "--until",
            "2026-08-07T06:22:39Z",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert '"retro_v6_cohort": 1' in result.stdout
    assert '"retro_v6_creators": 1' in result.stdout
    assert "TP= 1200: n=   1  mean= +1200 bps" in result.stdout
    assert "standard: n=    5  median=  -400 bps" in result.stdout
