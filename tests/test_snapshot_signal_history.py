from __future__ import annotations

import sqlite3

import pandas as pd
import pytest

from db import init_db, insert_signal_history
from scripts.snapshot_signal_history import (
    REQUIRED_COLUMNS,
    default_snapshot_path,
    export_signal_history,
    sha256_file,
)


@pytest.fixture
def seeded_db(tmp_path):
    db_path = tmp_path / "history.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    init_db(conn)
    insert_signal_history(
        conn,
        signals=[
            {
                "wallet": "0xaaa",
                "condition_id": "cond1",
                "outcome": "yes",
                "title": "Will A happen?",
                "is_open": True,
                "entry_price": 0.35,
                "entry_notional": 500.0,
                "current_price": 0.42,
                "position_insider_score": 85.0,
                "position_speculator_score": 15.0,
                "position_classification": "position_insider_candidate",
                "activity_state": "accumulating",
                "hold_hours": 12.5,
                "entry_anchor_ts": 1_699_990_000,
            },
            {
                "wallet": "0xbbb",
                "condition_id": "cond2",
                "outcome": "no",
                "title": "Will B happen?",
                "is_open": False,
                "entry_price": 0.55,
                "entry_notional": 1200.0,
                "current_price": 0.21,
                "position_insider_score": 62.0,
                "position_speculator_score": 72.0,
                "position_classification": "position_speculator_ladder",
                "activity_state": "distributing",
                "hold_hours": 48.0,
                "entry_anchor_ts": 1_699_991_000,
            },
        ],
        analyzed_ts=1_700_000_000,
        analysis_version=42,
    )
    conn.commit()
    conn.close()
    return db_path


def test_snapshot_round_trip_count_matches_signal_history(seeded_db, tmp_path):
    out = tmp_path / "signal-history.parquet"
    export_signal_history(db_path=seeded_db, output_path=out)

    df = pd.read_parquet(out)
    conn = sqlite3.connect(str(seeded_db))
    try:
        expected = conn.execute("SELECT COUNT(*) FROM signal_history").fetchone()[0]
    finally:
        conn.close()
    assert df.shape[0] == expected


def test_snapshot_preserves_required_scoring_columns(seeded_db, tmp_path):
    out = tmp_path / "signal-history.parquet"
    export_signal_history(db_path=seeded_db, output_path=out)

    df = pd.read_parquet(out)
    for col in REQUIRED_COLUMNS:
        assert col in df.columns

    first = df.sort_values("id").iloc[0]
    assert first["position_insider_score"] == 85.0
    assert first["position_speculator_score"] == 15.0
    assert first["position_classification"] == "position_insider_candidate"
    assert first["entry_anchor_ts"] == 1_699_990_000


def test_snapshot_rerun_is_idempotent_by_sha256(seeded_db, tmp_path):
    out = tmp_path / "signal-history.parquet"
    export_signal_history(db_path=seeded_db, output_path=out)
    first_sha = sha256_file(out)

    export_signal_history(db_path=seeded_db, output_path=out)
    second_sha = sha256_file(out)

    assert second_sha == first_sha


def test_default_snapshot_path_uses_archive_snapshots():
    assert (
        default_snapshot_path().parent.as_posix() == "archive/snapshots"
    )
