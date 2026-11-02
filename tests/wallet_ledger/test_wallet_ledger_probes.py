"""Slice 4 Task 2 — execution probe writer."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import duckdb

from scripts.execution_profile import (
    DEFAULT_PROBE_CONFIG,
    FillProbeHorizon,
    FillProbeRecord,
    _probe_config_hash,
)
from sniper.analysis.wallet_protocol.ledger.probes import upsert_execution_probes
from sniper.analysis.wallet_protocol.ledger.schema import ensure_wallet_schema


WALLET = "0xABCDEF1234567890abcdef1234567890ABCDEF12"
NORMALIZED_WALLET = "0xabcdef1234567890abcdef1234567890abcdef12"
PROBE_CONFIG_HASH = _probe_config_hash(DEFAULT_PROBE_CONFIG)
HORIZONS_JSON = json.dumps(
    DEFAULT_PROBE_CONFIG["horizons_seconds"],
    sort_keys=True,
    separators=(",", ":"),
)


def _connect(tmp_path: Path) -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(str(tmp_path / "wallet.duckdb"))
    ensure_wallet_schema(conn)
    return conn


def _rows(
    conn: duckdb.DuckDBPyConnection, query: str, params: list | None = None
) -> list[dict]:
    result = conn.execute(query, params or [])
    columns = [col[0] for col in result.description]
    return [dict(zip(columns, row)) for row in result.fetchall()]


def _success_record(
    *,
    fill_event_uid: str = "polymarket_data_api:0xtx:0:asset_yes",
    fill_ts: int = 1_700_000_000,
    fill_price: float = 0.50,
    reference_price: float | None = 0.48,
    horizons: tuple[FillProbeHorizon, ...] | None = None,
) -> FillProbeRecord:
    if horizons is None:
        horizons = tuple(
            FillProbeHorizon(
                horizon_seconds=hz,
                reference_price=reference_price,
                reference_ts=fill_ts - 60,
                target_ts=fill_ts + hz,
                target_price=0.55,
                drift=0.05,
                return_pct=(0.05 / reference_price) if reference_price else None,
                horizon_status="success" if reference_price else "invalid_reference",
            )
            for hz in DEFAULT_PROBE_CONFIG["horizons_seconds"]
        )
    return FillProbeRecord(
        fill_event_uid=fill_event_uid,
        probe_config_hash=PROBE_CONFIG_HASH,
        probe_version=DEFAULT_PROBE_CONFIG["probe_version"],
        horizons_seconds_json=HORIZONS_JSON,
        reference_rule=DEFAULT_PROBE_CONFIG["reference_rule"],
        condition_id="0xcid",
        token_id="asset_yes",
        fill_ts=fill_ts,
        fill_price=fill_price,
        side="BUY",
        size_tokens=10.0,
        reference_price=reference_price,
        reference_ts=fill_ts - 60,
        probe_status="success" if reference_price else "no_history",
        probe_error_msg=None,
        horizons=horizons,
        ref_tape=0.49,
        tape_n=7,
        in_play=False,
        thin_market=False,
        resolution_ts_unknown=False,
    )


def test_upsert_writes_probe_row_and_horizon_rows(tmp_path: Path) -> None:
    record = _success_record()
    with _connect(tmp_path) as conn:
        n = upsert_execution_probes(
            conn,
            wallet=WALLET,
            capture_run_id="run-1",
            fill_probes=[record],
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=1_700_000_100,
        )
        assert n == 1
        probes = _rows(conn, "SELECT * FROM wallet_execution_probes")
        assert len(probes) == 1
        assert probes[0]["wallet"] == NORMALIZED_WALLET
        assert probes[0]["fill_event_uid"] == record.fill_event_uid
        assert probes[0]["probe_config_hash"] == PROBE_CONFIG_HASH
        assert probes[0]["probe_version"] == "v1"
        assert probes[0]["reference_rule"] == "preceding_bar_30min_lookback"
        assert probes[0]["horizons_seconds_json"] == HORIZONS_JSON
        assert probes[0]["probe_status"] == "success"
        assert probes[0]["capture_run_id"] == "run-1"
        assert probes[0]["captured_at"] == 1_700_000_100

        horizons = _rows(
            conn,
            "SELECT * FROM wallet_execution_probe_horizons ORDER BY horizon_seconds",
        )
        assert [row["horizon_seconds"] for row in horizons] == [3600, 21600, 86400]
        for row in horizons:
            assert row["wallet"] == NORMALIZED_WALLET
            assert row["fill_event_uid"] == record.fill_event_uid
            assert row["probe_config_hash"] == PROBE_CONFIG_HASH
            assert row["horizon_status"] == "success"
            assert row["return_pct"] is not None


def test_probe_uid_is_deterministic_sha256_of_pk(tmp_path: Path) -> None:
    record = _success_record()
    with _connect(tmp_path) as conn:
        upsert_execution_probes(
            conn,
            wallet=WALLET,
            capture_run_id="run-1",
            fill_probes=[record],
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=1_700_000_100,
        )
        rows = _rows(conn, "SELECT probe_uid FROM wallet_execution_probes")
        expected = hashlib.sha256(
            f"{NORMALIZED_WALLET}|{record.fill_event_uid}|{PROBE_CONFIG_HASH}".encode()
        ).hexdigest()
        assert rows[0]["probe_uid"] == expected


def test_upsert_is_idempotent_under_repeat(tmp_path: Path) -> None:
    record = _success_record()
    with _connect(tmp_path) as conn:
        first = upsert_execution_probes(
            conn,
            wallet=WALLET,
            capture_run_id="run-1",
            fill_probes=[record],
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=1_700_000_100,
        )
        second = upsert_execution_probes(
            conn,
            wallet=WALLET,
            capture_run_id="run-1",
            fill_probes=[record],
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=1_700_000_200,
        )
        assert first == 1
        assert second == 0
        assert _rows(conn, "SELECT COUNT(*) AS n FROM wallet_execution_probes")[0]["n"] == 1
        assert _rows(conn, "SELECT COUNT(*) AS n FROM wallet_execution_probe_horizons")[0]["n"] == 3


def test_invalid_reference_horizon_overrides_return_pct_to_null(
    tmp_path: Path,
) -> None:
    """Even when the iterator passes a non-NULL return_pct, the writer must
    enforce return_pct=NULL when reference_price <= 0."""
    horizons = tuple(
        FillProbeHorizon(
            horizon_seconds=hz,
            reference_price=0.0,  # invalid reference
            reference_ts=1_699_999_940,
            target_ts=1_700_000_000 + hz,
            target_price=0.55,
            drift=0.05,
            return_pct=999.0,  # iterator may have passed garbage; writer overrides
            horizon_status="success",
        )
        for hz in DEFAULT_PROBE_CONFIG["horizons_seconds"]
    )
    record = _success_record(reference_price=0.0, horizons=horizons)
    with _connect(tmp_path) as conn:
        upsert_execution_probes(
            conn,
            wallet=WALLET,
            capture_run_id="run-1",
            fill_probes=[record],
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=1_700_000_100,
        )
        rows = _rows(
            conn,
            "SELECT horizon_seconds, return_pct, horizon_status FROM wallet_execution_probe_horizons ORDER BY horizon_seconds",
        )
        for row in rows:
            assert row["return_pct"] is None
            assert row["horizon_status"] == "invalid_reference"


def test_no_history_probe_persists_with_invalid_reference_horizons(
    tmp_path: Path,
) -> None:
    """A probe that hit no preceding bar has reference_price=None — the spec
    rule forces every horizon to `invalid_reference` regardless of what the
    iterator stamped on the records. The probe row itself keeps its
    probe_status='no_history' marker so callers can still tell why it failed.
    """
    horizons = tuple(
        FillProbeHorizon(
            horizon_seconds=hz,
            reference_price=None,
            reference_ts=None,
            target_ts=1_700_000_000 + hz,
            target_price=None,
            drift=None,
            return_pct=None,
            horizon_status="no_history",
        )
        for hz in DEFAULT_PROBE_CONFIG["horizons_seconds"]
    )
    record = FillProbeRecord(
        fill_event_uid="polymarket_data_api:0xtx:0:asset_yes",
        probe_config_hash=PROBE_CONFIG_HASH,
        probe_version=DEFAULT_PROBE_CONFIG["probe_version"],
        horizons_seconds_json=HORIZONS_JSON,
        reference_rule=DEFAULT_PROBE_CONFIG["reference_rule"],
        condition_id="0xcid",
        token_id="asset_yes",
        fill_ts=1_700_000_000,
        fill_price=0.50,
        side="BUY",
        size_tokens=None,
        reference_price=None,
        reference_ts=None,
        probe_status="no_history",
        probe_error_msg=None,
        horizons=horizons,
    )
    with _connect(tmp_path) as conn:
        n = upsert_execution_probes(
            conn,
            wallet=WALLET,
            capture_run_id="run-1",
            fill_probes=[record],
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=1_700_000_100,
        )
        assert n == 1
        rows = _rows(conn, "SELECT probe_status FROM wallet_execution_probes")
        assert rows == [{"probe_status": "no_history"}]
        horizon_rows = _rows(
            conn,
            "SELECT horizon_status FROM wallet_execution_probe_horizons",
        )
        assert all(
            row["horizon_status"] == "invalid_reference" for row in horizon_rows
        )


def test_horizon_no_history_status_retained_when_reference_is_valid(
    tmp_path: Path,
) -> None:
    """Defensive: 'no_history' at horizon level is a real status when ref is
    valid but the target bar is missing (market resolved or fidelity gap).
    """
    horizons = (
        FillProbeHorizon(
            horizon_seconds=3600,
            reference_price=0.48,
            reference_ts=1_699_999_940,
            target_ts=1_700_003_600,
            target_price=None,
            drift=None,
            return_pct=None,
            horizon_status="no_history",
        ),
    )
    record = _success_record(reference_price=0.48, horizons=horizons)
    with _connect(tmp_path) as conn:
        upsert_execution_probes(
            conn,
            wallet=WALLET,
            capture_run_id="run-1",
            fill_probes=[record],
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=1_700_000_100,
        )
        rows = _rows(
            conn,
            "SELECT horizon_status, return_pct FROM wallet_execution_probe_horizons",
        )
        assert rows == [{"horizon_status": "no_history", "return_pct": None}]


def test_wallet_normalization_lowercases_input(tmp_path: Path) -> None:
    record = _success_record()
    with _connect(tmp_path) as conn:
        upsert_execution_probes(
            conn,
            wallet=WALLET,  # mixed case
            capture_run_id="run-1",
            fill_probes=[record],
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=1_700_000_100,
        )
        rows = _rows(conn, "SELECT DISTINCT wallet FROM wallet_execution_probes")
        assert rows == [{"wallet": NORMALIZED_WALLET}]
        horizon_rows = _rows(
            conn, "SELECT DISTINCT wallet FROM wallet_execution_probe_horizons"
        )
        assert horizon_rows == [{"wallet": NORMALIZED_WALLET}]


# ---------------------------------------------------------------------------
# Slice 5 Task 0 — auxiliary gating fields persist + migrate
# ---------------------------------------------------------------------------


def test_probe_writer_persists_auxiliary_gating_fields(tmp_path: Path) -> None:
    """Slice 5 Task 0: ref_tape, tape_n, in_play, thin_market, and
    resolution_ts_unknown must roundtrip through the writer so the
    materializer can losslessly re-aggregate H4 inputs."""
    record = _success_record()
    record_in_play = FillProbeRecord(
        **{**record.__dict__, "in_play": True, "thin_market": True,
           "resolution_ts_unknown": True, "tape_n": 0, "ref_tape": None,
           "fill_event_uid": "polymarket_data_api:0xtx:1:asset_yes"},
    )
    with _connect(tmp_path) as conn:
        upsert_execution_probes(
            conn,
            wallet=WALLET,
            capture_run_id="run-1",
            fill_probes=[record, record_in_play],
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=1_700_000_100,
        )
        rows = _rows(
            conn,
            """
            SELECT fill_event_uid, ref_tape, tape_n, in_play, thin_market,
                   resolution_ts_unknown
            FROM wallet_execution_probes
            ORDER BY fill_event_uid
            """,
        )
        assert rows == [
            {
                "fill_event_uid": "polymarket_data_api:0xtx:0:asset_yes",
                "ref_tape": 0.49,
                "tape_n": 7,
                "in_play": False,
                "thin_market": False,
                "resolution_ts_unknown": False,
            },
            {
                "fill_event_uid": "polymarket_data_api:0xtx:1:asset_yes",
                "ref_tape": None,
                "tape_n": 0,
                "in_play": True,
                "thin_market": True,
                "resolution_ts_unknown": True,
            },
        ]


def test_ensure_wallet_schema_migrates_old_probes_table(tmp_path: Path) -> None:
    """Slice 5 Task 0: an existing warehouse created before the auxiliary
    columns were added must be safely migrated by `ensure_wallet_schema`."""
    db_path = tmp_path / "old.duckdb"
    with duckdb.connect(str(db_path)) as conn:
        # Re-create the Slice 4 wallet_execution_probes table — column set
        # taken from the schema as it existed BEFORE Task 0.
        conn.execute(
            """
            CREATE TABLE wallet_execution_probes (
                wallet VARCHAR NOT NULL,
                fill_event_uid VARCHAR NOT NULL,
                probe_config_hash VARCHAR NOT NULL,
                probe_uid VARCHAR NOT NULL,
                probe_version VARCHAR NOT NULL,
                horizons_seconds_json JSON NOT NULL,
                reference_rule VARCHAR NOT NULL,
                condition_id VARCHAR,
                token_id VARCHAR,
                fill_ts BIGINT NOT NULL,
                fill_price DOUBLE,
                side VARCHAR,
                size_tokens DOUBLE,
                reference_price DOUBLE,
                reference_ts BIGINT,
                probe_status VARCHAR NOT NULL,
                probe_error_msg VARCHAR,
                source VARCHAR NOT NULL,
                source_version VARCHAR NOT NULL,
                capture_run_id VARCHAR NOT NULL,
                captured_at BIGINT NOT NULL,
                CHECK (probe_status IN ('success', 'no_history', 'truncated', 'error')),
                PRIMARY KEY (wallet, fill_event_uid, probe_config_hash)
            )
            """
        )

    def _column_names(conn: duckdb.DuckDBPyConnection) -> list[str]:
        return [
            row[1]
            for row in conn.execute(
                "PRAGMA table_info('wallet_execution_probes')"
            ).fetchall()
        ]

    with duckdb.connect(str(db_path)) as conn:
        before = _column_names(conn)
        for col in (
            "ref_tape",
            "tape_n",
            "in_play",
            "thin_market",
            "resolution_ts_unknown",
        ):
            assert col not in before, f"old schema must not yet have {col}"

        ensure_wallet_schema(conn)
        after_first = _column_names(conn)
        for col in (
            "ref_tape",
            "tape_n",
            "in_play",
            "thin_market",
            "resolution_ts_unknown",
        ):
            assert col in after_first, f"migration did not add {col}"

        # Idempotency: a second call must not raise and must not change columns.
        ensure_wallet_schema(conn)
        after_second = _column_names(conn)
        assert after_first == after_second
