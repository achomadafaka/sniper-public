"""Builder-layer tests for wallet-dossier market-semantics v1 — failing until implemented.

Spec: docs/superpowers/specs/2026-05-26-wallet-dossier-market-semantics-v1.md
Tests T8-T12 (§9 hard-rule regression, lineage, data_gaps rename, schema bump, SwissMiss smoke).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sniper.analysis.wallet_dossier.builder import (
    DossierTables,
    build_dossier,
    build_tables,
)
from sniper.analysis.wallet_dossier.schema import DOSSIER_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Shared helpers — minimal fixture factories
# ---------------------------------------------------------------------------

WALLET = "0xdeadbeef" + "0" * 32


def _make_fixture(
    positions: list[dict],
    *,
    leafs_by_cid: dict | None = None,
    nav_lookup: dict | None = None,
    extra_activity: list | None = None,
) -> dict:
    """Minimal WalletData fixture for the builder."""
    return {
        "wallet": WALLET,
        "fetched_at": 1700000000,
        "schema_version": 2,
        "closed_positions": positions,
        "open_positions": [],
        "activity_rows": extra_activity or [],
        "leafs_by_cid": leafs_by_cid or {},
        "nav_lookup": nav_lookup or {},
    }


def _make_report(wallet: str = WALLET) -> dict:
    return {
        "wallet": wallet,
        "protocol_version": "v2",
        "overall_verdict": "underpowered",
        "gate_result": "pass",
    }


def _build(tmp_path: Path, wallet_data: dict, report: dict | None = None) -> DossierTables:
    fix = tmp_path / "fixture.json"
    rep = tmp_path / "report.json"
    fix.write_text(json.dumps(wallet_data))
    rep.write_text(json.dumps(report or _make_report()))
    return build_dossier(wallet=WALLET, fixture_path=fix, protocol_report_path=rep)


def _one_binary_position(cid: str = "0x" + "a" * 64) -> dict:
    return {
        "conditionId": cid,
        "asset": "1001",
        "outcome": "Yes",
        "title": "Test market",
        "avgPrice": 0.40,
        "totalBought": 100.0,
        "realizedPnl": 60.0,
        "cashPnl": 0.0,
    }


# ---------------------------------------------------------------------------
# T8 — §9 hard-rule regression: accounting_bucket never in grain keys
# (with a TAGGED fixture so v1's new category dimension is present in grain keys)
# ---------------------------------------------------------------------------


def test_T8_accounting_bucket_not_in_result_semantics_grain_keys(tmp_path: Path):
    """§9 hard rule: 'accounting_bucket' must NEVER appear as a dimension in any
    grain key in result_semantics, even in v1 where grain_key gains a real 'category'.

    This test uses a TAGGED fixture so the v1 code path (category='crypto') is
    exercised. The grain_key must contain 'category' but never 'accounting_bucket'.
    It fails until v1 makes category resolve AND confirms accounting_bucket absent."""
    cid = "0x" + "a" * 64
    positions = [
        {"conditionId": cid, "asset": "1001", "outcome": "Yes",
         "title": "T", "avgPrice": 0.40, "totalBought": 100.0, "realizedPnl": 60.0},
        {"conditionId": cid, "asset": "1002", "outcome": "No",
         "title": "T", "avgPrice": 0.55, "totalBought": 50.0, "realizedPnl": -22.5},
    ]
    tables = _build(
        tmp_path,
        _make_fixture(positions, leafs_by_cid={cid: ["crypto"]}, nav_lookup={"crypto": "crypto"}),
    )

    # v1 must resolve the category — confirm at least one grain has category='crypto'
    categories_in_grains = set()
    for rs_row in tables.result_semantics:
        grain_key = json.loads(rs_row["grain_key"])
        if "category" in grain_key:
            categories_in_grains.add(grain_key["category"])
        assert "accounting_bucket" not in grain_key, (
            f"§9 violation: accounting_bucket found in grain_key={grain_key}"
        )

    # Assert v1 actually populated a real category in grain (not just data_unavailable)
    assert "crypto" in categories_in_grains, (
        f"Expected at least one grain with category='crypto' after v1 implementation; "
        f"found categories_in_grains={categories_in_grains}. "
        "v1 must wire derive_category result into grain_key."
    )


def test_T8_accounting_bucket_not_in_mechanism_evidence_grain_keys(tmp_path: Path):
    """§9 also covers mechanism_evidence. Uses tagged fixture to exercise v1 code path."""
    cid = "0x" + "a" * 64
    positions = [
        {"conditionId": cid, "asset": "1001", "outcome": "Yes",
         "title": "T", "avgPrice": 0.40, "totalBought": 100.0, "realizedPnl": 60.0},
    ]
    tables = _build(
        tmp_path,
        _make_fixture(positions, leafs_by_cid={cid: ["sports"]}, nav_lookup={"sports": "sports"}),
    )

    # v1 must resolve the category
    ms_rows = tables.market_semantics
    assert len(ms_rows) == 1
    assert ms_rows[0]["category"] == "sports", (
        f"Expected category='sports' after v1 implementation, got {ms_rows[0]['category']!r}"
    )

    for me_row in tables.mechanism_evidence:
        supporting = json.loads(me_row.get("supporting_metrics") or "{}")
        assert "accounting_bucket" not in supporting, (
            f"§9 violation: accounting_bucket in supporting_metrics={supporting}"
        )


# ---------------------------------------------------------------------------
# T9 — lineage covers leaf_tags_json when non-NULL
# ---------------------------------------------------------------------------


def test_T9_lineage_covers_leaf_tags_json_when_populated(tmp_path: Path):
    """For every market_semantics row where leaf_tags_json IS NOT NULL,
    at least one data_lineage row exists with table='market_semantics',
    column='leaf_tags_json', matching row_key, source='fixture_field'."""
    cid = "0x" + "a" * 64
    tables = _build(
        tmp_path,
        _make_fixture(
            [_one_binary_position(cid)],
            leafs_by_cid={cid: ["microstrategy"]},
            nav_lookup={"microstrategy": "crypto"},
        ),
    )

    # Find market_semantics rows that carry leaf_tags_json
    tagged_pids = {
        r["position_id"]
        for r in tables.market_semantics
        if r.get("leaf_tags_json") is not None
    }

    # Must have at least one (spec requires implementation to populate it)
    assert tagged_pids, (
        "Expected at least one market_semantics row with leaf_tags_json populated "
        "when leafs_by_cid provides surviving tags"
    )

    # For each such row, lineage must cover it
    lineage_keys = {
        (l["table"], l["column"], l["row_key"])
        for l in tables.data_lineage
        if l["table"] == "market_semantics" and l["column"] == "leaf_tags_json"
    }

    for pid in tagged_pids:
        assert ("market_semantics", "leaf_tags_json", pid) in lineage_keys, (
            f"Missing lineage for market_semantics.leaf_tags_json row_key={pid}"
        )


def test_T9_lineage_source_is_fixture_field_for_leaf_tags_json(tmp_path: Path):
    """Per spec §4: when leaf_tags_json IS NOT NULL, lineage source='fixture_field'."""
    cid = "0x" + "a" * 64
    tables = _build(
        tmp_path,
        _make_fixture(
            [_one_binary_position(cid)],
            leafs_by_cid={cid: ["sports"]},
            nav_lookup={"sports": "sports"},
        ),
    )
    leaf_lineage = [
        l for l in tables.data_lineage
        if l["table"] == "market_semantics" and l["column"] == "leaf_tags_json"
    ]
    assert leaf_lineage, "No lineage rows found for market_semantics.leaf_tags_json"
    for row in leaf_lineage:
        assert row["source"] == "fixture_field", (
            f"Expected source='fixture_field', got {row['source']!r}"
        )


def test_T9_event_family_lineage_does_not_use_old_market_tags_gap_vocab(tmp_path: Path):
    """event_family is still deferred in v1, but lineage must not use the old
    market_tags_not_provisioned reason after the gap rename."""
    tables = _build(tmp_path, _make_fixture([_one_binary_position()]))
    event_family_lineage = [
        l for l in tables.data_lineage
        if l["table"] == "market_semantics"
        and l["column"] in {"event_family", "event_family_source"}
    ]
    assert event_family_lineage
    for row in event_family_lineage:
        assert row["unavailable_reason"] == "event_family_not_derived_in_v1_market_semantics"


# ---------------------------------------------------------------------------
# T10 — data_gap renamed + count-based
# ---------------------------------------------------------------------------


def _make_mixed_fixture(tmp_path: Path, n_resolved: int, n_unavailable: int):
    """Build a fixture where `n_resolved` positions resolve category and
    `n_unavailable` are data_unavailable."""
    positions = []
    leafs_by_cid = {}
    nav_lookup = {}

    # Resolved positions: have tags and nav mapping
    for i in range(n_resolved):
        cid = f"0x{'a' * 60}{i:04d}"
        positions.append({
            "conditionId": cid,
            "asset": str(1000 + i),
            "outcome": "Yes",
            "title": f"Resolved market {i}",
            "avgPrice": 0.40,
            "totalBought": 100.0,
            "realizedPnl": 60.0,
        })
        leafs_by_cid[cid] = ["crypto"]
        nav_lookup["crypto"] = "crypto"

    # Unavailable positions: no tags
    for i in range(n_unavailable):
        cid = f"0x{'b' * 60}{i:04d}"
        positions.append({
            "conditionId": cid,
            "asset": str(2000 + i),
            "outcome": "Yes",
            "title": f"Unavailable market {i}",
            "avgPrice": 0.35,
            "totalBought": 50.0,
            "realizedPnl": 10.0,
        })
        # No entry in leafs_by_cid → tag_not_found_in_leafs

    return _make_fixture(positions, leafs_by_cid=leafs_by_cid, nav_lookup=nav_lookup)


def test_T10_data_gap_renamed_from_market_tags_not_provisioned(tmp_path: Path):
    """Spec §2.3: gap_name is renamed to 'market_category_unavailable'.
    'market_tags_not_provisioned' must NOT appear."""
    tables = _build(tmp_path, _make_fixture([_one_binary_position()]))
    gap_names = {g["gap_name"] for g in tables.data_gaps}
    assert "market_tags_not_provisioned" not in gap_names, (
        "Old gap name 'market_tags_not_provisioned' must not appear after v1 rename"
    )


def test_T10_data_gap_market_category_unavailable_present_when_any_unavailable(tmp_path: Path):
    """When any positions have category==data_unavailable, gap IS emitted."""
    # 3 resolved (30%), 7 unavailable (70%)
    fixture = _make_mixed_fixture(tmp_path, n_resolved=3, n_unavailable=7)
    tables = _build(tmp_path, fixture)

    unavailable_count = sum(
        1 for r in tables.market_semantics if r["category"] == "data_unavailable"
    )
    gap = next(
        (g for g in tables.data_gaps if g["gap_name"] == "market_category_unavailable"),
        None,
    )
    assert gap is not None, "Expected 'market_category_unavailable' gap when some rows unavailable"
    assert gap["affected_grain_count"] == unavailable_count, (
        f"affected_grain_count must equal count of data_unavailable market_semantics rows; "
        f"expected {unavailable_count}, got {gap['affected_grain_count']}"
    )


def test_T10_data_gap_severity_high_when_70pct_unavailable(tmp_path: Path):
    """70% unavailable ≥ 50% → severity must be 'high'."""
    fixture = _make_mixed_fixture(tmp_path, n_resolved=3, n_unavailable=7)
    tables = _build(tmp_path, fixture)
    gap = next(g for g in tables.data_gaps if g["gap_name"] == "market_category_unavailable")
    assert gap["severity"] == "high", f"Expected severity=high, got {gap['severity']!r}"


def test_T10_data_gap_not_emitted_when_0_unavailable(tmp_path: Path):
    """When 100% positions resolve category, gap must NOT be emitted.
    Requires v1 to actually resolve categories (otherwise all rows remain
    data_unavailable and the gap would still appear)."""
    # All 5 positions have tags that resolve to crypto
    n = 5
    positions = []
    leafs_by_cid = {}
    nav_lookup = {"crypto": "crypto"}
    for i in range(n):
        cid = f"0x{'a' * 60}{i:04d}"
        positions.append({
            "conditionId": cid,
            "asset": str(1000 + i),
            "outcome": "Yes",
            "title": f"Market {i}",
            "avgPrice": 0.40,
            "totalBought": 100.0,
            "realizedPnl": 60.0,
        })
        leafs_by_cid[cid] = ["crypto"]

    tables = _build(
        tmp_path,
        _make_fixture(positions, leafs_by_cid=leafs_by_cid, nav_lookup=nav_lookup),
    )

    # Verify v1 actually resolved all rows (prerequisite for the gap-absent check)
    n_unavailable = sum(
        1 for r in tables.market_semantics if r["category"] == "data_unavailable"
    )
    assert n_unavailable == 0, (
        f"Expected 0 data_unavailable rows when all cids have tags+nav; got {n_unavailable}. "
        "v1 derive_category must resolve these."
    )

    # When all resolve successfully, gap must not be present
    gap = next(
        (g for g in tables.data_gaps if g["gap_name"] == "market_category_unavailable"),
        None,
    )
    assert gap is None, "Gap must not be emitted when 0 positions are data_unavailable"


# ---------------------------------------------------------------------------
# T11 — schema version bumped
# ---------------------------------------------------------------------------


def test_T11_schema_version_constant_bumped():
    """Current dossier schema carries the latest semantic layer version."""
    assert DOSSIER_SCHEMA_VERSION == "v5_behavioral_descriptors", (
        f"Expected 'v5_behavioral_descriptors', got {DOSSIER_SCHEMA_VERSION!r}"
    )


def test_T11_dossier_meta_carries_v1_schema_version(tmp_path: Path):
    """dossier_meta row with key='schema_version' must carry the new value."""
    tables = _build(tmp_path, _make_fixture([_one_binary_position()]))
    schema_row = next(
        (r for r in tables.dossier_meta if r["key"] == "schema_version"), None
    )
    assert schema_row is not None, "dossier_meta must contain key='schema_version'"
    assert schema_row["value"] == "v5_behavioral_descriptors", (
        f"Expected 'v5_behavioral_descriptors', got {schema_row['value']!r}"
    )


# ---------------------------------------------------------------------------
# T1 integration path — build_tables resolves category via tagged fixture
# ---------------------------------------------------------------------------


def test_T1_integration_tagged_build_resolves_category(tmp_path: Path):
    """End-to-end: fixture carries leafs_by_cid+nav_lookup → market_semantics.category resolved."""
    cid = "0x" + "a" * 64
    tables = _build(
        tmp_path,
        _make_fixture(
            [_one_binary_position(cid)],
            leafs_by_cid={cid: ["microstrategy"]},
            nav_lookup={"microstrategy": "crypto"},
        ),
    )
    ms_rows = tables.market_semantics
    assert len(ms_rows) == 1
    row = ms_rows[0]
    assert row["category"] == "crypto"
    assert row["category_source"] == "tag_nav_category"
    assert row["category_unavailable_reason"] is None
    assert row.get("leaf_tags_json") == '["microstrategy"]'


def test_T1_integration_leaf_tags_json_schema_column_present(tmp_path: Path):
    """Spec §2.2: leaf_tags_json column must exist in market_semantics rows."""
    cid = "0x" + "a" * 64
    tables = _build(
        tmp_path,
        _make_fixture(
            [_one_binary_position(cid)],
            leafs_by_cid={cid: ["sports"]},
            nav_lookup={"sports": "sports"},
        ),
    )
    row = tables.market_semantics[0]
    assert "leaf_tags_json" in row, (
        "leaf_tags_json column must exist on market_semantics rows (spec §2.2)"
    )


def test_T1_integration_leaf_tags_json_null_when_unavailable(tmp_path: Path):
    """leaf_tags_json IS NULL when category unavailable due to empty leafs_by_cid.
    Additionally, the column MUST exist on the row (spec §2.2 adds it as a new column)."""
    tables = _build(tmp_path, _make_fixture([_one_binary_position()]))
    for row in tables.market_semantics:
        # Column must exist (v1 adds it; v0 rows don't have it)
        assert "leaf_tags_json" in row, (
            "leaf_tags_json column must be present on market_semantics rows (spec §2.2); "
            "currently absent — requires v1 implementation"
        )
        assert row["leaf_tags_json"] is None, (
            f"leaf_tags_json must be NULL when category unavailable, got {row['leaf_tags_json']!r}"
        )


# ---------------------------------------------------------------------------
# T9 — leaf_tags_json preserved when no_nav_mapping_for_any_leaf
# ---------------------------------------------------------------------------


def test_T9_leaf_tags_json_preserved_when_no_nav_mapping(tmp_path: Path):
    """Spec §3.3: leaf_tags_json populated when ≥1 leaf survives skip+ambiguous filters,
    even when no nav mapping resolves category."""
    cid = "0x" + "a" * 64
    tables = _build(
        tmp_path,
        _make_fixture(
            [_one_binary_position(cid)],
            leafs_by_cid={cid: ["unknown-new-tag"]},
            nav_lookup={"other-known-tag": "sports"},  # our leaf not in here
        ),
    )
    row = tables.market_semantics[0]
    assert row["category"] == "data_unavailable"
    assert row["category_unavailable_reason"] == "no_nav_mapping_for_any_leaf"
    # leaf_tags_json still populated
    assert row.get("leaf_tags_json") == '["unknown-new-tag"]'
