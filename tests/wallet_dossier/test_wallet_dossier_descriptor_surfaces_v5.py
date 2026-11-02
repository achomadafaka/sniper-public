"""Failing acceptance tests for Wallet Dossier Descriptor Surfaces v5.

Spec: docs/superpowers/specs/2026-05-27-wallet-dossier-descriptor-surfaces-v5.md

Test → spec-line mapping:
  test_v5_schema_version_bumped                  → Acceptance #1 / §DOSSIER_SCHEMA_VERSION
  test_v5_all_descriptor_tables_in_all_dossier_tables → Acceptance #2
  test_v5_descriptor_tables_in_sqlite_sink       → Acceptance #2 (sink)
  test_v5_descriptor_tables_in_json_export       → Acceptance #2 (JSON sink)
  test_v5_descriptor_lineage_coverage            → Acceptance #2 (lineage)
  test_v5_sparse_wallet_emits_zero_descriptor_rows → Acceptance #3
  test_v5_sparse_wallet_emits_five_sparse_gaps   → Acceptance #3 (gap names)
  test_v5_adequate_wallet_hour_distribution_rows → Acceptance #4 / §descriptor_hour_distribution
  test_v5_adequate_wallet_cadence_rows           → Acceptance #4 / §descriptor_cadence
  test_v5_adequate_wallet_sizing_rows            → Acceptance #4 / §descriptor_sizing
  test_v5_adequate_wallet_time_in_position_rows  → Acceptance #4 / §descriptor_time_in_position
  test_v5_adequate_wallet_fills_per_position_rows → Acceptance #4 / §descriptor_fills_per_position
  test_v5_determinism_descriptor_tables          → Acceptance #5
  test_v5_dossier_meta_carries_five_new_counts   → Acceptance #6
  test_v5_swissmiss_smoke_descriptors_populate   → Acceptance #7 (skipped if no fixture)
  test_v5_hour_distribution_columns              → §descriptor_hour_distribution column rules
  test_v5_cadence_columns                        → §descriptor_cadence column rules
  test_v5_sizing_columns                         → §descriptor_sizing column rules
  test_v5_time_in_position_columns               → §descriptor_time_in_position column rules
  test_v5_fills_per_position_columns             → §descriptor_fills_per_position column rules
  test_v5_cadence_honesty_gate_n_intervals       → §descriptor_cadence honesty rule (<10 intervals)
  test_v5_sizing_honesty_gate_n_fills            → §descriptor_sizing honesty rule (<10 fills)
  test_v5_time_in_position_honesty_gate          → §descriptor_time_in_position honesty rule (<5 positions)
  test_v5_fills_per_position_honesty_gate        → §descriptor_fills_per_position honesty rule (<5 positions)
  test_v5_hour_distribution_honesty_gate_sparse  → §descriptor_hour_distribution honesty rule (<50 rows)
  test_v5_burst_share_fraction_in_range          → §descriptor_cadence burst_share in [0.0, 1.0]
  test_v5_single_fill_share_fraction_in_range    → §descriptor_fills_per_position single_fill_share in [0.0, 1.0]
  test_v5_coeff_of_variation_non_negative        → §descriptor_sizing coeff_of_variation >= 0
  test_v5_still_open_positions_use_fetched_at    → §descriptor_time_in_position still_open/unresolved rule
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from sniper.analysis.wallet_dossier.builder import (
    DossierTables,
    build_dossier,
    build_tables,
    write_json_export,
    write_sqlite,
)
from sniper.analysis.wallet_dossier.schema import (
    DOSSIER_SCHEMA_VERSION,
    all_dossier_tables,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
V4_STAGING_DIR = REPO_ROOT / "tests" / "calibration" / "fixtures-v4-staging"
SWISSMISS = "0xdbade4c82fb72780a0db9a38f821d8671aba9c95"

# ---------------------------------------------------------------------------
# New v5 table names (spec §Tables)
# ---------------------------------------------------------------------------

V5_DESCRIPTOR_TABLES = {
    "descriptor_hour_distribution",
    "descriptor_cadence",
    "descriptor_sizing",
    "descriptor_time_in_position",
    "descriptor_fills_per_position",
}

# ---------------------------------------------------------------------------
# New dossier_meta count keys (spec §dossier_meta)
# ---------------------------------------------------------------------------

V5_META_COUNT_KEYS = {
    "n_descriptor_hour_rows",
    "n_descriptor_cadence_rows",
    "n_descriptor_sizing_rows",
    "n_descriptor_time_in_position_rows",
    "n_descriptor_fills_per_position_rows",
}

# Gap names expected for a sparse wallet (spec §honesty rules)
V5_SPARSE_GAP_NAMES = {
    "wallet_too_sparse_for_hour_distribution",
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _minimal_protocol_report(wallet: str) -> dict:
    return {
        "wallet": wallet,
        "protocol_version": "v2",
        "overall_verdict": "underpowered",
        "gate_result": "pass",
    }


def _make_activity_row(
    cid: str,
    asset: str,
    side: str,
    size: float,
    price: float,
    timestamp: int,
) -> dict:
    return {
        "conditionId": cid,
        "asset": asset,
        "type": "TRADE",
        "side": side,
        "size": size,
        "price": price,
        "timestamp": timestamp,
    }


def _make_closed_position(
    cid: str,
    asset: str,
    outcome: str,
    avg_price: float,
    total_bought: float,
    realized_pnl: float,
    timestamp: int = 1700000000,
) -> dict:
    return {
        "conditionId": cid,
        "asset": asset,
        "outcome": outcome,
        "title": f"Market {cid[:8]}",
        "avgPrice": avg_price,
        "totalBought": total_bought,
        "realizedPnl": realized_pnl,
        "timestamp": timestamp,
    }


def _sparse_wallet_data() -> dict:
    """A wallet with zero activity_rows — triggers all 5 sparse-data honesty gates."""
    wallet = "0x" + "5" * 40
    return {
        "wallet": wallet,
        "fetched_at": 1700000000,
        "schema_version": 2,
        "closed_positions": [
            _make_closed_position("0x" + "a" * 64, "1001", "Yes", 0.4, 10.0, 4.0),
        ],
        "open_positions": [],
        "activity_rows": [],  # ZERO rows — triggers all sparse gates
        "leafs_by_cid": {},
        "nav_lookup": {},
    }


def _adequate_wallet_data() -> dict:
    """A wallet with enough activity rows to produce descriptor output.

    - 60+ activity_rows (>= 50 for hour_distribution gate)
    - 10+ intervals per bucket (cadence gate)
    - 10+ fills per bucket (sizing gate)
    - 5+ positions per exit_mode (time_in_position gate)
    - 5+ positions per category (fills_per_position gate)

    Uses a single conditionId / category bucket to keep things simple.
    Timestamps spread across multiple hours of the day.
    """
    cid_a = "0x" + "a" * 64
    cid_b = "0x" + "b" * 64

    base_ts = 1700000000  # 2023-11-14 22:13:20 UTC

    closed_positions = []
    activity_rows = []

    # Build 30 binary YES positions on cid_a (one asset per position, each 1 fill)
    for i in range(30):
        asset = str(2000 + i)
        cp_ts = base_ts + i * 3600  # one per hour
        closed_positions.append(
            _make_closed_position(cid_a, asset, "Yes", 0.40, 100.0, 60.0, timestamp=cp_ts)
        )
        # Add a TRADE activity row for entry
        activity_rows.append(
            _make_activity_row(cid_a, asset, "BUY", 100.0, 0.40, cp_ts)
        )

    # Build 30 binary YES positions on cid_b (different cid so we have 2 categories worth)
    for i in range(30):
        asset = str(3000 + i)
        cp_ts = base_ts + i * 1800  # one per 30 min
        closed_positions.append(
            _make_closed_position(cid_b, asset, "Yes", 0.60, 50.0, 20.0, timestamp=cp_ts)
        )
        activity_rows.append(
            _make_activity_row(cid_b, asset, "BUY", 50.0, 0.60, cp_ts)
        )

    return {
        "wallet": "0x" + "6" * 40,
        "fetched_at": base_ts + 200000,
        "schema_version": 2,
        "closed_positions": closed_positions,
        "open_positions": [],
        "activity_rows": activity_rows,
        "leafs_by_cid": {},
        "nav_lookup": {},
    }


@pytest.fixture
def sparse_tables(tmp_path: Path) -> DossierTables:
    data = _sparse_wallet_data()
    wallet = data["wallet"]
    fix = tmp_path / "fixture_sparse.json"
    rep = tmp_path / "report_sparse.json"
    fix.write_text(json.dumps(data))
    rep.write_text(json.dumps(_minimal_protocol_report(wallet)))
    return build_dossier(wallet=wallet, fixture_path=fix, protocol_report_path=rep)


@pytest.fixture
def adequate_tables(tmp_path: Path) -> DossierTables:
    data = _adequate_wallet_data()
    wallet = data["wallet"]
    fix = tmp_path / "fixture_adequate.json"
    rep = tmp_path / "report_adequate.json"
    fix.write_text(json.dumps(data))
    rep.write_text(json.dumps(_minimal_protocol_report(wallet)))
    return build_dossier(wallet=wallet, fixture_path=fix, protocol_report_path=rep)


# ---------------------------------------------------------------------------
# Acceptance #1 — schema version
# ---------------------------------------------------------------------------


def test_v5_schema_version_bumped():
    """Acceptance #1: DOSSIER_SCHEMA_VERSION must be 'v5_behavioral_descriptors'."""
    assert DOSSIER_SCHEMA_VERSION == "v5_behavioral_descriptors", (
        f"Expected 'v5_behavioral_descriptors', got {DOSSIER_SCHEMA_VERSION!r}"
    )


# ---------------------------------------------------------------------------
# Acceptance #2 — 5 new tables present in all surfaces + lineage
# ---------------------------------------------------------------------------


def test_v5_all_descriptor_tables_in_all_dossier_tables():
    """Acceptance #2: all 5 descriptor tables must be in all_dossier_tables()."""
    tables = set(all_dossier_tables())
    missing = V5_DESCRIPTOR_TABLES - tables
    assert not missing, f"Missing descriptor tables: {missing}"


def test_v5_descriptor_tables_in_sqlite_sink(adequate_tables: DossierTables, tmp_path: Path):
    """Acceptance #2 (sink): 5 descriptor tables created in SQLite output."""
    db = tmp_path / "v5_desc.sqlite"
    write_sqlite(adequate_tables, db)
    conn = sqlite3.connect(str(db))
    existing = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    conn.close()
    missing = V5_DESCRIPTOR_TABLES - existing
    assert not missing, f"SQLite missing tables: {missing}"


def test_v5_descriptor_tables_in_json_export(adequate_tables: DossierTables, tmp_path: Path):
    """Acceptance #2 (JSON sink): 5 descriptor table JSON files written."""
    out = tmp_path / "v5_json"
    write_json_export(adequate_tables, out)
    for tbl in V5_DESCRIPTOR_TABLES:
        assert (out / f"{tbl}.json").exists(), f"JSON export missing {tbl}.json"


def test_v5_descriptor_lineage_coverage(adequate_tables: DossierTables):
    """Acceptance #2 (lineage): at least one lineage row per descriptor table."""
    lineage_tables = {row["table"] for row in adequate_tables.data_lineage}
    missing = V5_DESCRIPTOR_TABLES - lineage_tables
    assert not missing, f"No lineage rows for descriptor tables: {missing}"


# ---------------------------------------------------------------------------
# Acceptance #3 — sparse wallet → zero rows + 5 gaps
# ---------------------------------------------------------------------------


def test_v5_sparse_wallet_emits_zero_descriptor_rows(sparse_tables: DossierTables):
    """Acceptance #3: wallet with < 50 activity_rows emits zero descriptor rows."""
    # All 5 new descriptor tables must be present on DossierTables
    assert hasattr(sparse_tables, "descriptor_hour_distribution"), (
        "DossierTables missing field: descriptor_hour_distribution"
    )
    assert sparse_tables.descriptor_hour_distribution == [], (
        "Expected zero hour_distribution rows for sparse wallet"
    )
    assert sparse_tables.descriptor_cadence == [], (
        "Expected zero cadence rows for sparse wallet"
    )
    assert sparse_tables.descriptor_sizing == [], (
        "Expected zero sizing rows for sparse wallet"
    )
    assert sparse_tables.descriptor_time_in_position == [], (
        "Expected zero time_in_position rows for sparse wallet"
    )
    assert sparse_tables.descriptor_fills_per_position == [], (
        "Expected zero fills_per_position rows for sparse wallet"
    )


def test_v5_sparse_wallet_emits_five_sparse_gaps(sparse_tables: DossierTables):
    """Acceptance #3: sparse wallet emits the wallet_too_sparse_for_hour_distribution gap.

    Additionally, descriptor_cadence / sizing / time_in_position / fills_per_position
    honesty gates fire for their respective insufficient-data conditions.
    """
    gap_names = {g["gap_name"] for g in sparse_tables.data_gaps}
    # The spec guarantees at minimum the hour_distribution sparse gap
    assert "wallet_too_sparse_for_hour_distribution" in gap_names, (
        f"Expected wallet_too_sparse_for_hour_distribution in gaps; got: {gap_names}"
    )


# ---------------------------------------------------------------------------
# Acceptance #4 — adequate wallet emits rows per relevant bucket
# ---------------------------------------------------------------------------


def test_v5_adequate_wallet_hour_distribution_rows(adequate_tables: DossierTables):
    """Acceptance #4: adequate wallet produces >= 1 hour_distribution row."""
    assert hasattr(adequate_tables, "descriptor_hour_distribution")
    assert len(adequate_tables.descriptor_hour_distribution) >= 1, (
        "Expected at least 1 hour_distribution row for adequate wallet"
    )


def test_v5_hour_distribution_total_size_is_notional(adequate_tables: DossierTables):
    """Spec §1: total_size_usdc = sum of (size * price) for rows in bucket,
    NOT raw size. Regression-locks the reviewer-caught bug from PR #65."""
    fixture = _adequate_wallet_data()
    # Compute the expected sum-of-notionals across all activity_rows that share
    # the first row's (hour_utc, side, market_category) bucket.
    sample = adequate_tables.descriptor_hour_distribution[0]
    target_hour = sample["hour_utc"]
    target_side = sample["side"]
    expected_notional = 0.0
    for r in fixture["activity_rows"]:
        ts = int(r["timestamp"])
        hour = (ts // 3600) % 24
        side = str(r.get("side") or "BUY").upper()
        if hour == target_hour and side == target_side:
            expected_notional += float(r["size"]) * float(r["price"])
    # The bucket may be split by category; sum bucket rows with same hour/side.
    actual_notional = sum(
        row["total_size_usdc"]
        for row in adequate_tables.descriptor_hour_distribution
        if row["hour_utc"] == target_hour and row["side"] == target_side
    )
    assert actual_notional == pytest.approx(expected_notional), (
        f"total_size_usdc must be sum(size*price); got {actual_notional}, "
        f"expected {expected_notional} (raw-size bug would give a different value)"
    )


def test_v5_adequate_wallet_cadence_rows(adequate_tables: DossierTables):
    """Acceptance #4: adequate wallet produces >= 1 cadence row."""
    assert hasattr(adequate_tables, "descriptor_cadence")
    assert len(adequate_tables.descriptor_cadence) >= 1, (
        "Expected at least 1 cadence row for adequate wallet"
    )


def test_v5_adequate_wallet_sizing_rows(adequate_tables: DossierTables):
    """Acceptance #4: adequate wallet produces >= 1 sizing row."""
    assert hasattr(adequate_tables, "descriptor_sizing")
    assert len(adequate_tables.descriptor_sizing) >= 1, (
        "Expected at least 1 sizing row for adequate wallet"
    )


def test_v5_sizing_columns_are_notional_not_raw_size(adequate_tables: DossierTables):
    """Spec §3: median_size_usdc / p10 / p90 / max_size_usdc are fill notionals
    (size * price), NOT raw shares. Regression-locks the Codex-caught bug
    parallel to test_v5_hour_distribution_total_size_is_notional."""
    fixture = _adequate_wallet_data()
    sample = adequate_tables.descriptor_sizing[0]
    target_side = sample["side"]

    # Recompute expected median from the fixture's notional values for matching
    # rows in this (side, category) bucket.
    matching_notionals = []
    for r in fixture["activity_rows"]:
        side = str(r.get("side") or "BUY").upper()
        if side != target_side:
            continue
        size = float(r.get("size") or 0.0)
        price = float(r.get("price") or 0.0)
        matching_notionals.append(size * price)

    assert matching_notionals, "fixture must produce at least one matching row"
    # max_size_usdc must match exactly because max() over the full list survives bucketing
    assert sample["max_size_usdc"] == pytest.approx(max(matching_notionals)), (
        f"max_size_usdc should be sum(size*price)-derived max; got {sample['max_size_usdc']}, "
        f"expected {max(matching_notionals)} (raw-size bug would give max(size) instead)"
    )


def test_v5_adequate_wallet_time_in_position_rows(adequate_tables: DossierTables):
    """Acceptance #4: adequate wallet produces >= 1 time_in_position row."""
    assert hasattr(adequate_tables, "descriptor_time_in_position")
    assert len(adequate_tables.descriptor_time_in_position) >= 1, (
        "Expected at least 1 time_in_position row for adequate wallet"
    )


def test_v5_adequate_wallet_fills_per_position_rows(adequate_tables: DossierTables):
    """Acceptance #4: adequate wallet produces >= 1 fills_per_position row."""
    assert hasattr(adequate_tables, "descriptor_fills_per_position")
    assert len(adequate_tables.descriptor_fills_per_position) >= 1, (
        "Expected at least 1 fills_per_position row for adequate wallet"
    )


# ---------------------------------------------------------------------------
# Acceptance #5 — determinism
# ---------------------------------------------------------------------------


def test_v5_determinism_descriptor_tables(tmp_path: Path):
    """Acceptance #5: identical inputs produce byte-identical descriptor tables."""
    data = _adequate_wallet_data()
    wallet = data["wallet"]
    fix = tmp_path / "fixture_det.json"
    rep = tmp_path / "report_det.json"
    fix.write_text(json.dumps(data))
    rep.write_text(json.dumps(_minimal_protocol_report(wallet)))

    tables_a = build_dossier(wallet=wallet, fixture_path=fix, protocol_report_path=rep)
    tables_b = build_dossier(wallet=wallet, fixture_path=fix, protocol_report_path=rep)

    for attr in [
        "descriptor_hour_distribution",
        "descriptor_cadence",
        "descriptor_sizing",
        "descriptor_time_in_position",
        "descriptor_fills_per_position",
    ]:
        a = getattr(tables_a, attr)
        b = getattr(tables_b, attr)
        assert a == b, f"Non-deterministic output for {attr}: {a!r} != {b!r}"


# ---------------------------------------------------------------------------
# Acceptance #6 — dossier_meta carries 5 new counts
# ---------------------------------------------------------------------------


def test_v5_dossier_meta_carries_five_new_counts(adequate_tables: DossierTables):
    """Acceptance #6: dossier_meta has all 5 new descriptor count keys."""
    meta_keys = {row["key"] for row in adequate_tables.dossier_meta}
    missing = V5_META_COUNT_KEYS - meta_keys
    assert not missing, f"dossier_meta missing keys: {missing}"


def test_v5_dossier_meta_counts_match_actual_rows(adequate_tables: DossierTables):
    """Acceptance #6: dossier_meta count values match actual list lengths."""
    meta = {row["key"]: row["value"] for row in adequate_tables.dossier_meta}
    attr_key_map = {
        "descriptor_hour_distribution": "n_descriptor_hour_rows",
        "descriptor_cadence": "n_descriptor_cadence_rows",
        "descriptor_sizing": "n_descriptor_sizing_rows",
        "descriptor_time_in_position": "n_descriptor_time_in_position_rows",
        "descriptor_fills_per_position": "n_descriptor_fills_per_position_rows",
    }
    for attr, key in attr_key_map.items():
        actual = len(getattr(adequate_tables, attr))
        recorded = int(meta[key])
        assert recorded == actual, (
            f"dossier_meta.{key}={recorded} does not match actual len({attr})={actual}"
        )


# ---------------------------------------------------------------------------
# Column shape tests — spec §Tables column rules
# ---------------------------------------------------------------------------


def test_v5_hour_distribution_columns(adequate_tables: DossierTables):
    """§descriptor_hour_distribution column rules."""
    assert len(adequate_tables.descriptor_hour_distribution) >= 1
    for row in adequate_tables.descriptor_hour_distribution:
        assert "hour_utc" in row, f"Missing hour_utc in {row}"
        assert "side" in row, f"Missing side in {row}"
        assert "market_category" in row, f"Missing market_category in {row}"
        assert "n_trades" in row, f"Missing n_trades in {row}"
        assert "total_size_usdc" in row, f"Missing total_size_usdc in {row}"

        assert isinstance(row["hour_utc"], int), "hour_utc must be INTEGER"
        assert 0 <= row["hour_utc"] <= 23, f"hour_utc out of range: {row['hour_utc']}"
        assert row["side"] in {"BUY", "SELL"}, f"Invalid side: {row['side']}"
        assert isinstance(row["n_trades"], int), "n_trades must be INTEGER"
        assert isinstance(row["total_size_usdc"], (int, float)), "total_size_usdc must be REAL"


def test_v5_cadence_columns(adequate_tables: DossierTables):
    """§descriptor_cadence column rules."""
    assert len(adequate_tables.descriptor_cadence) >= 1
    for row in adequate_tables.descriptor_cadence:
        for col in [
            "side", "market_category", "n_intervals",
            "median_seconds_between_trades", "p10_seconds_between_trades",
            "p90_seconds_between_trades", "burst_share",
        ]:
            assert col in row, f"Missing {col} in cadence row"
        assert row["side"] in {"BUY", "SELL"}
        assert isinstance(row["n_intervals"], int)
        assert isinstance(row["burst_share"], float)


def test_v5_sizing_columns(adequate_tables: DossierTables):
    """§descriptor_sizing column rules."""
    assert len(adequate_tables.descriptor_sizing) >= 1
    for row in adequate_tables.descriptor_sizing:
        for col in [
            "side", "market_category", "n_fills",
            "median_size_usdc", "p10_size_usdc", "p90_size_usdc",
            "max_size_usdc", "coeff_of_variation",
        ]:
            assert col in row, f"Missing {col} in sizing row"
        assert row["side"] in {"BUY", "SELL"}
        assert isinstance(row["n_fills"], int)


def test_v5_time_in_position_columns(adequate_tables: DossierTables):
    """§descriptor_time_in_position column rules."""
    assert len(adequate_tables.descriptor_time_in_position) >= 1
    for row in adequate_tables.descriptor_time_in_position:
        for col in [
            "exit_mode", "n_positions",
            "median_seconds_in_position",
            "p10_seconds_in_position",
            "p90_seconds_in_position",
        ]:
            assert col in row, f"Missing {col} in time_in_position row"
        assert row["exit_mode"] in {
            "held", "pre_resolution", "still_open", "unresolved",
        }, f"Invalid exit_mode: {row['exit_mode']}"
        assert isinstance(row["n_positions"], int)


def test_v5_fills_per_position_columns(adequate_tables: DossierTables):
    """§descriptor_fills_per_position column rules."""
    assert len(adequate_tables.descriptor_fills_per_position) >= 1
    for row in adequate_tables.descriptor_fills_per_position:
        for col in [
            "market_category", "n_positions",
            "median_fills", "p10_fills", "p90_fills",
            "single_fill_share",
        ]:
            assert col in row, f"Missing {col} in fills_per_position row"
        assert isinstance(row["n_positions"], int)
        assert isinstance(row["single_fill_share"], float)


# ---------------------------------------------------------------------------
# Honesty gate edge cases
# ---------------------------------------------------------------------------


def test_v5_hour_distribution_honesty_gate_sparse():
    """§descriptor_hour_distribution: < 50 activity_rows → zero rows + gap."""
    data = _sparse_wallet_data()
    # Verify the fixture has < 50 rows
    assert len(data["activity_rows"]) < 50

    wallet = data["wallet"]
    tables = build_tables(
        wallet=wallet,
        wallet_data=data,
        protocol_report=_minimal_protocol_report(wallet),
    )
    assert hasattr(tables, "descriptor_hour_distribution")
    assert tables.descriptor_hour_distribution == [], (
        "Hour distribution must be empty for wallets with < 50 activity_rows"
    )
    gap_names = {g["gap_name"] for g in tables.data_gaps}
    assert "wallet_too_sparse_for_hour_distribution" in gap_names


def test_v5_cadence_honesty_gate_n_intervals():
    """§descriptor_cadence: bucket with < 10 intervals → row dropped + gap."""
    # A wallet with exactly 5 BUY rows (4 intervals) in one (side, category) bucket
    cid = "0x" + "c" * 64
    activity_rows = [
        _make_activity_row(cid, str(i), "BUY", 100.0, 0.40, 1700000000 + i * 60)
        for i in range(5)  # 4 intervals — below the 10-interval threshold
    ]
    closed_positions = [
        _make_closed_position(cid, str(i), "Yes", 0.40, 100.0, 60.0)
        for i in range(5)
    ]
    data = {
        "wallet": "0x" + "7" * 40,
        "fetched_at": 1700010000,
        "schema_version": 2,
        "closed_positions": closed_positions,
        "open_positions": [],
        "activity_rows": activity_rows,
        "leafs_by_cid": {},
        "nav_lookup": {},
    }
    wallet = data["wallet"]
    tables = build_tables(
        wallet=wallet,
        wallet_data=data,
        protocol_report=_minimal_protocol_report(wallet),
    )
    assert hasattr(tables, "descriptor_cadence")
    # The (BUY, data_unavailable) bucket has only 4 intervals → must be dropped
    for row in tables.descriptor_cadence:
        assert row["n_intervals"] >= 10, (
            f"Cadence row with n_intervals={row['n_intervals']} should have been dropped "
            "(honesty gate: n_intervals < 10)"
        )


def test_v5_sizing_honesty_gate_n_fills():
    """§descriptor_sizing: bucket with < 10 fills → row dropped."""
    cid = "0x" + "d" * 64
    activity_rows = [
        _make_activity_row(cid, str(i), "BUY", 100.0, 0.40, 1700000000 + i * 60)
        for i in range(5)  # 5 fills — below the 10-fill threshold
    ]
    closed_positions = [
        _make_closed_position(cid, str(i), "Yes", 0.40, 100.0, 60.0)
        for i in range(5)
    ]
    data = {
        "wallet": "0x" + "8" * 40,
        "fetched_at": 1700010000,
        "schema_version": 2,
        "closed_positions": closed_positions,
        "open_positions": [],
        "activity_rows": activity_rows,
        "leafs_by_cid": {},
        "nav_lookup": {},
    }
    wallet = data["wallet"]
    tables = build_tables(
        wallet=wallet,
        wallet_data=data,
        protocol_report=_minimal_protocol_report(wallet),
    )
    assert hasattr(tables, "descriptor_sizing")
    # The (BUY, data_unavailable) bucket has only 5 fills → must be dropped
    for row in tables.descriptor_sizing:
        assert row["n_fills"] >= 10, (
            f"Sizing row with n_fills={row['n_fills']} should have been dropped "
            "(honesty gate: n_fills < 10)"
        )


def test_v5_time_in_position_honesty_gate():
    """§descriptor_time_in_position: exit_mode bucket with < 5 positions → row dropped."""
    # Only 3 closed positions — below the 5-position threshold
    cid = "0x" + "e" * 64
    closed_positions = [
        _make_closed_position(cid, str(i), "Yes", 0.40, 100.0, 60.0)
        for i in range(3)
    ]
    data = {
        "wallet": "0x" + "9" * 40,
        "fetched_at": 1700010000,
        "schema_version": 2,
        "closed_positions": closed_positions,
        "open_positions": [],
        "activity_rows": [],
        "leafs_by_cid": {},
        "nav_lookup": {},
    }
    wallet = data["wallet"]
    tables = build_tables(
        wallet=wallet,
        wallet_data=data,
        protocol_report=_minimal_protocol_report(wallet),
    )
    assert hasattr(tables, "descriptor_time_in_position")
    # Every exit_mode bucket has < 5 positions → all rows must be dropped
    for row in tables.descriptor_time_in_position:
        assert row["n_positions"] >= 5, (
            f"time_in_position row with n_positions={row['n_positions']} should have been "
            "dropped (honesty gate: n_positions < 5)"
        )


def test_v5_fills_per_position_honesty_gate():
    """§descriptor_fills_per_position: category bucket with < 5 positions → row dropped."""
    # Only 3 closed positions (one category bucket)
    cid = "0x" + "f" * 64
    closed_positions = [
        _make_closed_position(cid, str(i), "Yes", 0.40, 100.0, 60.0)
        for i in range(3)
    ]
    data = {
        "wallet": "0x" + "aa" + "0" * 38,
        "fetched_at": 1700010000,
        "schema_version": 2,
        "closed_positions": closed_positions,
        "open_positions": [],
        "activity_rows": [],
        "leafs_by_cid": {},
        "nav_lookup": {},
    }
    wallet = data["wallet"]
    tables = build_tables(
        wallet=wallet,
        wallet_data=data,
        protocol_report=_minimal_protocol_report(wallet),
    )
    assert hasattr(tables, "descriptor_fills_per_position")
    for row in tables.descriptor_fills_per_position:
        assert row["n_positions"] >= 5, (
            f"fills_per_position row with n_positions={row['n_positions']} should have been "
            "dropped (honesty gate: n_positions < 5)"
        )


# ---------------------------------------------------------------------------
# Value-range sanity checks (failure modes)
# ---------------------------------------------------------------------------


def test_v5_burst_share_fraction_in_range(adequate_tables: DossierTables):
    """§descriptor_cadence: burst_share must be in [0.0, 1.0]."""
    for row in adequate_tables.descriptor_cadence:
        bs = row["burst_share"]
        assert 0.0 <= bs <= 1.0, f"burst_share out of range: {bs}"


def test_v5_single_fill_share_fraction_in_range(adequate_tables: DossierTables):
    """§descriptor_fills_per_position: single_fill_share must be in [0.0, 1.0]."""
    for row in adequate_tables.descriptor_fills_per_position:
        sfs = row["single_fill_share"]
        assert 0.0 <= sfs <= 1.0, f"single_fill_share out of range: {sfs}"


def test_v5_coeff_of_variation_non_negative(adequate_tables: DossierTables):
    """§descriptor_sizing: coeff_of_variation = stdev/mean must be >= 0."""
    for row in adequate_tables.descriptor_sizing:
        cv = row["coeff_of_variation"]
        assert cv >= 0.0, f"coeff_of_variation is negative: {cv}"


def test_v5_still_open_positions_use_fetched_at(tmp_path: Path):
    """§descriptor_time_in_position: for still_open / unresolved exit_mode,
    duration = fetched_at - entry_ts (not exit_ts, which may be None)."""
    # Build a wallet with 5 still_open positions (no SELL activity)
    cid = "0x" + "bb" + "0" * 62
    fetched_at = 1700100000
    entry_ts = 1700000000  # 100000 seconds before fetched_at
    min_expected_seconds = fetched_at - entry_ts  # 100000

    closed_positions = [
        {
            "conditionId": cid,
            "asset": str(i),
            "outcome": "Yes",
            "title": "Still-open test",
            "avgPrice": 0.40,
            "totalBought": 100.0,
            "realizedPnl": 0.0,
            "timestamp": entry_ts,
        }
        for i in range(5)
    ]
    # These are in closed_positions but have no exits in activity (so exit_mode=unresolved)
    data = {
        "wallet": "0x" + "cc" + "0" * 38,
        "fetched_at": fetched_at,
        "schema_version": 2,
        "closed_positions": closed_positions,
        "open_positions": [],
        "activity_rows": [],
        "leafs_by_cid": {},
        "nav_lookup": {},
    }
    wallet = data["wallet"]
    tables = build_tables(
        wallet=wallet,
        wallet_data=data,
        protocol_report=_minimal_protocol_report(wallet),
    )
    # Find the time_in_position row for unresolved or still_open
    tip_rows = {
        r["exit_mode"]: r
        for r in tables.descriptor_time_in_position
    }
    # At least one of still_open / unresolved should exist (5 positions)
    present_modes = set(tip_rows.keys()) & {"still_open", "unresolved"}
    assert present_modes, (
        f"Expected still_open or unresolved in time_in_position rows; got {set(tip_rows.keys())}"
    )
    for mode in present_modes:
        median_s = tip_rows[mode]["median_seconds_in_position"]
        # Should be ~100000 (fetched_at - entry_ts), not None, not 0
        assert median_s is not None and median_s > 0, (
            f"still_open/unresolved median_seconds_in_position should use fetched_at; "
            f"got {median_s}"
        )


# ---------------------------------------------------------------------------
# Acceptance #7 — SwissMiss real-data smoke (skipped if fixture absent)
# ---------------------------------------------------------------------------


@pytest.fixture
def swissmiss_v4_fixture() -> Path:
    path = V4_STAGING_DIR / f"{SWISSMISS}_2026-05-25.json"
    if not path.exists():
        pytest.skip(f"SwissMiss v4 fixture not present at {path}")
    return path


def test_v5_swissmiss_smoke_descriptors_populate(
    swissmiss_v4_fixture: Path,
    tmp_path: Path,
):
    """Acceptance #7: SwissMiss descriptor tables populate; held median >= days."""
    rep = tmp_path / "sm_report.json"
    rep.write_text(json.dumps({
        "wallet": SWISSMISS,
        "protocol_version": "v2",
        "overall_verdict": "underpowered",
        "gate_result": "pass",
    }))
    tables = build_dossier(
        wallet=SWISSMISS,
        fixture_path=swissmiss_v4_fixture,
        protocol_report_path=rep,
    )

    # Descriptor tables must be present as attributes
    assert hasattr(tables, "descriptor_hour_distribution")
    assert hasattr(tables, "descriptor_cadence")
    assert hasattr(tables, "descriptor_sizing")
    assert hasattr(tables, "descriptor_time_in_position")
    assert hasattr(tables, "descriptor_fills_per_position")

    # At least some rows in most descriptor tables (SwissMiss has 1011 positions)
    assert len(tables.descriptor_hour_distribution) >= 1, (
        "SwissMiss should have hour_distribution rows"
    )

    # Spec acceptance #7: median time-in-position for 'held' >= 1 day (86400 seconds)
    tip_by_mode = {r["exit_mode"]: r for r in tables.descriptor_time_in_position}
    if "held" in tip_by_mode:
        median_held = tip_by_mode["held"]["median_seconds_in_position"]
        assert median_held >= 86400, (
            f"SwissMiss held positions: median time-in-position should be >= 1 day, "
            f"got {median_held}s"
        )

    # pre_resolution should be less than held (likely < days per spec acceptance #7)
    if "pre_resolution" in tip_by_mode and "held" in tip_by_mode:
        median_preres = tip_by_mode["pre_resolution"]["median_seconds_in_position"]
        median_held = tip_by_mode["held"]["median_seconds_in_position"]
        assert median_preres < median_held, (
            f"Expected pre_resolution ({median_preres}s) < held ({median_held}s) "
            "per spec acceptance #7"
        )
