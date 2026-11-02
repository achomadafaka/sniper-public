"""Wallet Dossier v0_complete tests.

Spec: docs/superpowers/specs/2026-05-25-wallet-dossier-semantics-v0.md §11
The filename predates the complete checkpoint; these tests now cover the full
v0 table set shipped by PR #57.

Tests in this suite:
  - Synthetic fixture: end-to-end with hand-crafted minimal inputs.
  - Schema shape: all v0 tables present, columns match spec.
  - Lineage completeness: every non-null, non-key cell has a lineage row.
  - Derivation rules §7.1-§7.8.
  - Diagnostic accounting hard separation: §6.4b's accounting_bucket
    column does NOT appear in market_semantics.
  - Canonical in-repo fixture and optional SwissMiss v4 real fixture.
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
from sniper.analysis.wallet_dossier.derivers import (
    RESOLVE_BY_CURPRICE_ENABLED,
    ExitEpisode,
    ResolutionResult,
    _classify_counterfactual,
    classify_accounting_bucket,
    derive_dominant_counterfactual_class,
    derive_entry_exit_ts,
    derive_entry_price_bucket,
    derive_exit_mode,
    derive_market_type,
    derive_outcome_role,
    derive_selected_token_resolution,
    extract_episodes_for_position,
)
from sniper.analysis.wallet_dossier.grain import (
    GrainAggregate,
    LEAKY_BROAD_VALUES,
    build_grain_key,
    build_mechanism_evidence,
    evaluate_honesty_gates,
    grain_id,
)
from sniper.analysis.wallet_dossier.schema import (
    DOSSIER_SCHEMA_VERSION,
    SCHEMA_DDL,
    all_dossier_tables,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
V4_STAGING_DIR = REPO_ROOT / "tests" / "calibration" / "fixtures-v4-staging"
DOSSIER_FIXTURE_DIR = REPO_ROOT / "tests" / "calibration" / "dossier-fixtures"
SWISSMISS = "0xdbade4c82fb72780a0db9a38f821d8671aba9c95"
# Canonical in-repo fixture (trimmed via scripts/trim_dossier_fixture.py).
# Smaller wallet than SwissMiss but exercises the same v4 redemption shape.
CANONICAL_WALLET = "0x8493374739b45eb032382857feeb7ed025215366"
CANONICAL_FIXTURE = DOSSIER_FIXTURE_DIR / f"{CANONICAL_WALLET}_2026-05-25.json"


# ---------------------------------------------------------------------------
# Synthetic fixture (deterministic, in-repo)
# ---------------------------------------------------------------------------


def _synthetic_wallet_data() -> dict:
    """Hand-crafted minimal WalletData covering all 3 market_types and all
    4 outcome_role classifications."""
    return {
        "wallet": "0xdeadbeef" + "0" * 32,
        "fetched_at": 1700000000,
        "schema_version": 2,
        "closed_positions": [
            # Binary YES — held to resolution, won
            {
                "conditionId": "0x" + "a" * 64,
                "asset": "1001",
                "outcome": "Yes",
                "title": "Will it rain on Tuesday?",
                "avgPrice": 0.40,
                "totalBought": 100.0,
                "realizedPnl": 60.0,
                "cashPnl": 0.0,
            },
            # Binary NO — same condition, different outcome (makes market binary)
            {
                "conditionId": "0x" + "a" * 64,
                "asset": "1002",
                "outcome": "No",
                "title": "Will it rain on Tuesday?",
                "avgPrice": 0.55,
                "totalBought": 50.0,
                "realizedPnl": -22.5,
                "cashPnl": 0.0,
            },
            # Multi-outcome — 3 outcomes (favorite-ish at 0.5)
            {
                "conditionId": "0x" + "b" * 64,
                "asset": "2001",
                "outcome": "Team A",
                "title": "Champions League winner 2026",
                "avgPrice": 0.50,
                "totalBought": 200.0,
                "realizedPnl": 100.0,
                "cashPnl": 0.0,
            },
            # Multi-outcome — same condition, longshot
            {
                "conditionId": "0x" + "b" * 64,
                "asset": "2002",
                "outcome": "Team B",
                "title": "Champions League winner 2026",
                "avgPrice": 0.10,
                "totalBought": 1000.0,
                "realizedPnl": -100.0,
                "cashPnl": 0.0,
            },
            # Multi-outcome — third sibling
            {
                "conditionId": "0x" + "b" * 64,
                "asset": "2003",
                "outcome": "Team C",
                "title": "Champions League winner 2026",
                "avgPrice": 0.02,
                "totalBought": 5000.0,
                "realizedPnl": -100.0,
                "cashPnl": 0.0,
            },
            # Unknown market_type — only one outcome ever traded
            {
                "conditionId": "0x" + "c" * 64,
                "asset": "3001",
                "outcome": "Maybe",
                "title": "Ambiguous market",
                "avgPrice": 0.90,
                "totalBought": 10.0,
                "realizedPnl": 1.0,
                "cashPnl": 0.0,
            },
        ],
        "open_positions": [],
        "activity_rows": [
            # Mirror the YES/NO presence so derive_market_type sees both
            {"conditionId": "0x" + "a" * 64, "asset": "1001", "type": "TRADE"},
            {"conditionId": "0x" + "a" * 64, "asset": "1002", "type": "TRADE"},
            {"conditionId": "0x" + "b" * 64, "asset": "2001", "type": "TRADE"},
            {"conditionId": "0x" + "b" * 64, "asset": "2002", "type": "TRADE"},
            {"conditionId": "0x" + "b" * 64, "asset": "2003", "type": "TRADE"},
            {"conditionId": "0x" + "c" * 64, "asset": "3001", "type": "TRADE"},
        ],
        "leafs_by_cid": {},
        "nav_lookup": {},
    }


def _synthetic_protocol_report(wallet: str) -> dict:
    """Minimal valid ProtocolReport dict."""
    return {
        "wallet": wallet,
        "protocol_version": "v2",
        "overall_verdict": "underpowered",
        "gate_result": "pass",
    }


@pytest.fixture
def synthetic_tables(tmp_path: Path) -> DossierTables:
    wallet = "0xdeadbeef" + "0" * 32
    fix = tmp_path / "fixture.json"
    rep = tmp_path / "report.json"
    fix.write_text(json.dumps(_synthetic_wallet_data()))
    rep.write_text(json.dumps(_synthetic_protocol_report(wallet)))
    return build_dossier(
        wallet=wallet,
        fixture_path=fix,
        protocol_report_path=rep,
    )


# ---------------------------------------------------------------------------
# §7.1 — market_type derivation
# ---------------------------------------------------------------------------


def test_derive_market_type_binary():
    closed = [
        {"conditionId": "0xabc", "asset": "1001"},
        {"conditionId": "0xabc", "asset": "1002"},
    ]
    market_type, source, n = derive_market_type("0xabc", closed_positions=closed, activity_rows=[])
    assert market_type == "binary"
    assert source == "outcome_token_count"
    assert n == 2


def test_derive_market_type_multi_outcome():
    closed = [
        {"conditionId": "0xabc", "asset": str(i)} for i in range(5)
    ]
    market_type, _, n = derive_market_type("0xabc", closed_positions=closed, activity_rows=[])
    assert market_type == "multi_outcome"
    assert n == 5


def test_derive_market_type_unknown_when_single():
    closed = [{"conditionId": "0xabc", "asset": "1001"}]
    market_type, source, n = derive_market_type("0xabc", closed_positions=closed, activity_rows=[])
    assert market_type == "unknown"
    assert source == "outcome_token_count_ambiguous"
    assert n == 1


def test_derive_market_type_binary_from_opposite_asset_when_one_side_observed():
    closed = [{"conditionId": "0xabc", "asset": "1001", "oppositeAsset": "1002"}]
    market_type, source, n = derive_market_type("0xabc", closed_positions=closed, activity_rows=[])
    assert market_type == "binary"
    assert source == "closed_position_opposite_asset"
    assert n == 2


def test_derive_market_type_unknown_when_empty():
    market_type, source, n = derive_market_type("0xabc", closed_positions=[], activity_rows=[])
    assert market_type == "unknown"
    assert source == "no_observations"
    assert n is None


def test_derive_market_type_case_insensitive_condition():
    closed = [
        {"conditionId": "0xABC", "asset": "1001"},
        {"conditionId": "0xabc", "asset": "1002"},
    ]
    market_type, _, n = derive_market_type("0xAbC", closed_positions=closed, activity_rows=[])
    assert market_type == "binary"
    assert n == 2


# ---------------------------------------------------------------------------
# §7.2 — entry_price_bucket
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "price,expected",
    [
        (0.00, "p_lt_05"),
        (0.04999, "p_lt_05"),
        (0.05, "p_05_25"),
        (0.249999, "p_05_25"),
        (0.25, "p_25_50"),
        (0.49999, "p_25_50"),
        (0.50, "p_50_75"),
        (0.74999, "p_50_75"),
        (0.75, "p_gte_75"),
        (1.00, "p_gte_75"),
    ],
)
def test_entry_price_bucket_boundaries(price, expected):
    assert derive_entry_price_bucket(price) == expected


def test_entry_price_bucket_clamps_out_of_range():
    assert derive_entry_price_bucket(-0.5) == "p_lt_05"
    assert derive_entry_price_bucket(1.5) == "p_gte_75"


def test_entry_price_bucket_none_raises():
    with pytest.raises(ValueError):
        derive_entry_price_bucket(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# §7.3 — outcome_role
# ---------------------------------------------------------------------------


def test_outcome_role_binary_yes_case_insensitive():
    assert derive_outcome_role(market_type="binary", outcome_label="Yes") == "binary_yes"
    assert derive_outcome_role(market_type="binary", outcome_label="YES") == "binary_yes"
    assert derive_outcome_role(market_type="binary", outcome_label="true") == "binary_yes"


def test_outcome_role_binary_no():
    assert derive_outcome_role(market_type="binary", outcome_label="No") == "binary_no"
    assert derive_outcome_role(market_type="binary", outcome_label="False") == "binary_no"


def test_outcome_role_binary_named_outcome():
    # v3: binary market with any non-empty non-yes/no label → binary_named_outcome
    # (was "unknown" before v3; the rename is the spec's deliberate change).
    # Still NOT routed to OTHER (accounting bucket); binary_named_outcome is structural.
    assert derive_outcome_role(market_type="binary", outcome_label="Maybe") == "binary_named_outcome"
    assert derive_outcome_role(market_type="binary", outcome_label="Trump") == "binary_named_outcome"
    assert derive_outcome_role(market_type="binary", outcome_label="Canucks") == "binary_named_outcome"
    # Empty label → still unknown
    assert derive_outcome_role(market_type="binary", outcome_label="") == "unknown"
    assert derive_outcome_role(market_type="binary", outcome_label=None) == "unknown"


def test_outcome_role_multi_outcome_always_selected():
    assert (
        derive_outcome_role(market_type="multi_outcome", outcome_label="Team A")
        == "multi_outcome_selected"
    )
    assert (
        derive_outcome_role(market_type="multi_outcome", outcome_label="Yes")
        == "multi_outcome_selected"
    ), "multi_outcome dominates; even if label is 'Yes', it's multi_outcome_selected"


def test_outcome_role_unknown_market_type():
    assert derive_outcome_role(market_type="unknown", outcome_label="Yes") == "unknown"


# ---------------------------------------------------------------------------
# §7.3 v3 — favorite_status / outcome_role_rank derivation
# ---------------------------------------------------------------------------


from sniper.analysis.wallet_dossier.derivers import derive_favorite_status_and_rank


def test_v3_favorite_status_binary_favorite():
    """spec §2.2: binary mkt + entry_price >= 0.50 → favorite, rank=1."""
    status, rank, source = derive_favorite_status_and_rank(market_type="binary", entry_price=0.75)
    assert status == "favorite"
    assert rank == 1
    assert source == "binary_entry_price_complement"
    # boundary at exactly 0.50 → favorite (deterministic tie-break per spec)
    status, rank, source = derive_favorite_status_and_rank(market_type="binary", entry_price=0.50)
    assert (status, rank, source) == ("favorite", 1, "binary_entry_price_complement")


def test_v3_favorite_status_binary_underdog():
    """spec §2.2: binary mkt + 0.25 <= entry < 0.50 → underdog, rank=2."""
    status, rank, source = derive_favorite_status_and_rank(market_type="binary", entry_price=0.40)
    assert (status, rank, source) == ("underdog", 2, "binary_entry_price_complement")
    # boundary at 0.25 → underdog
    status, rank, source = derive_favorite_status_and_rank(market_type="binary", entry_price=0.25)
    assert (status, rank, source) == ("underdog", 2, "binary_entry_price_complement")


def test_v3_favorite_status_binary_longshot():
    """spec §2.2: binary mkt + entry < 0.25 → longshot, rank=2."""
    status, rank, source = derive_favorite_status_and_rank(market_type="binary", entry_price=0.10)
    assert (status, rank, source) == ("longshot", 2, "binary_entry_price_complement")


def test_v3_favorite_status_entry_price_missing():
    """spec §2.2: entry_price=None → unknown / entry_price_missing reason."""
    status, rank, source = derive_favorite_status_and_rank(market_type="binary", entry_price=None)
    assert (status, rank, source) == ("unknown", None, "entry_price_missing")


def test_v3_favorite_status_non_binary_refuses():
    """spec §2.2: multi-outcome / unknown markets → refuse via
    sibling_token_prices_at_entry_not_fetched (no binary complement available)."""
    status, rank, source = derive_favorite_status_and_rank(market_type="multi_outcome", entry_price=0.30)
    assert (status, rank, source) == ("unknown", None, "sibling_token_prices_at_entry_not_fetched")
    status, rank, source = derive_favorite_status_and_rank(market_type="unknown", entry_price=0.30)
    assert (status, rank, source) == ("unknown", None, "sibling_token_prices_at_entry_not_fetched")


def test_v3_sibling_gap_count_based(synthetic_tables: DossierTables):
    """spec §3: sibling_token_prices_at_entry_not_fetched emitted only when
    at least one position has outcome_role_rank=NULL (binary→rank derived;
    multi_outcome stays NULL)."""
    # Synthetic fixture has multi-outcome rows → at least one NULL rank → gap is emitted
    gap_names = {g["gap_name"] for g in synthetic_tables.data_gaps}
    n_null_rank = sum(1 for r in synthetic_tables.positions if r["outcome_role_rank"] is None)
    if n_null_rank > 0:
        assert "sibling_token_prices_at_entry_not_fetched" in gap_names
        # affected_grain_count matches actual null count
        for g in synthetic_tables.data_gaps:
            if g["gap_name"] == "sibling_token_prices_at_entry_not_fetched":
                assert g["affected_grain_count"] == n_null_rank
                break
    else:
        # Gap NOT emitted when all positions have a rank
        assert "sibling_token_prices_at_entry_not_fetched" not in gap_names


# ---------------------------------------------------------------------------
# v4_thesis_lifecycle — thesis_candidates table
# Spec: docs/superpowers/specs/2026-05-27-wallet-dossier-thesis-lifecycle-v4.md
# ---------------------------------------------------------------------------


def test_v4_schema_version_bumped():
    assert DOSSIER_SCHEMA_VERSION == "v5_behavioral_descriptors"


def test_v4_thesis_candidates_in_all_tables():
    assert "thesis_candidates" in all_dossier_tables()


def test_v4_no_promotion_emits_zero_candidates(synthetic_tables: DossierTables):
    # Synthetic fixture has no promoted mechanism rows → no thesis candidates.
    promoted = [r for r in synthetic_tables.mechanism_evidence if r.get("candidate_thesis_evidence")]
    assert promoted == []
    assert synthetic_tables.thesis_candidates == []


def test_v4_meta_carries_n_thesis_candidates(synthetic_tables: DossierTables):
    keys = {row["key"] for row in synthetic_tables.dossier_meta}
    assert "n_thesis_candidates" in keys
    for row in synthetic_tables.dossier_meta:
        if row["key"] == "n_thesis_candidates":
            assert int(row["value"]) == 0
            break


def test_v4_promoted_row_produces_one_deterministic_candidate(synthetic_tables: DossierTables):
    """Inject a promoted mechanism row and re-run the v4 builder helpers."""
    from sniper.analysis.wallet_dossier.builder import (
        _build_thesis_candidates,
        _thesis_id,
    )
    promoted = {
        "grain_id": "abc123",
        "mechanism": "prediction_driven",
        "candidate_thesis_evidence": "calls events early; needs resolution dataset",
        "supporting_metrics": json.dumps({"foo": 1.0}),
    }
    rows1 = _build_thesis_candidates([promoted])
    rows2 = _build_thesis_candidates([promoted])
    assert len(rows1) == 1
    row = rows1[0]
    assert row["grain_id"] == "abc123"
    assert row["mechanism"] == "prediction_driven"
    assert row["status"] == "candidate"
    assert row["thesis_text"] == "calls events early; needs resolution dataset"
    assert row["source_schema_version"] == DOSSIER_SCHEMA_VERSION
    # required_test_data is JSON
    rtd = json.loads(row["required_test_data"])
    assert "resolution_outcomes" in rtd
    # simulation_plan is JSON with replay_constraints
    sp = json.loads(row["simulation_plan"])
    assert sp["status"] == "simulation_ready_after_dataset_build"
    assert isinstance(sp["replay_constraints"], list)
    # deterministic across runs
    assert rows1 == rows2
    assert row["thesis_id"] == _thesis_id("abc123", "prediction_driven")


def test_v4_unpromoted_row_produces_no_candidate():
    from sniper.analysis.wallet_dossier.builder import _build_thesis_candidates
    unpromoted = {
        "grain_id": "abc123",
        "mechanism": "underpowered",
        "candidate_thesis_evidence": None,
        "supporting_metrics": json.dumps({}),
    }
    assert _build_thesis_candidates([unpromoted]) == []


def test_v4_required_test_data_by_mechanism():
    from sniper.analysis.wallet_dossier.builder import _build_thesis_candidates
    cases = [
        ("prediction_driven", "resolution_outcomes"),
        ("trading_driven", "fill_sequence"),
        ("risk_management_driven", "risk_cut_policy"),
        ("mixed", "mechanism_specific_dataset"),
    ]
    for mech, expected_field in cases:
        row = _build_thesis_candidates([{
            "grain_id": "g",
            "mechanism": mech,
            "candidate_thesis_evidence": "x",
            "supporting_metrics": "{}",
        }])[0]
        rtd = json.loads(row["required_test_data"])
        assert expected_field in rtd, f"{mech} missing {expected_field}: {rtd}"


def test_v4_thesis_candidates_written_to_sqlite(tmp_path: Path, synthetic_tables: DossierTables):
    db = tmp_path / "v4.sqlite"
    write_sqlite(synthetic_tables, db)
    conn = sqlite3.connect(db)
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='thesis_candidates'")
    assert cur.fetchone() is not None
    n = conn.execute("SELECT COUNT(*) FROM thesis_candidates").fetchone()[0]
    assert n == 0  # synthetic has no promoted rows
    conn.close()


def test_v4_thesis_candidates_in_json_export(tmp_path: Path, synthetic_tables: DossierTables):
    out_dir = tmp_path / "v4_json"
    write_json_export(synthetic_tables, out_dir)
    assert (out_dir / "thesis_candidates.json").exists()


def test_v4_thesis_candidates_lineage_when_promoted():
    """All 8 non-key columns of a thesis_candidates row must have lineage entries."""
    from sniper.analysis.wallet_dossier.builder import (
        _build_thesis_candidates,
        _record_thesis_candidates_lineage,
    )
    from sniper.analysis.wallet_dossier.lineage import LineageTracker
    promoted = {
        "grain_id": "g1",
        "mechanism": "prediction_driven",
        "candidate_thesis_evidence": "edge in event-resolution timing",
        "supporting_metrics": "{}",
    }
    rows = _build_thesis_candidates([promoted])
    tracker = LineageTracker()
    _record_thesis_candidates_lineage(tracker, rows)
    lineage_rows = list(tracker.rows())
    # Expect one lineage row per non-key column (8) for the single thesis_candidate.
    table_rows = [r for r in lineage_rows if r.table == "thesis_candidates"]
    cols = {r.column for r in table_rows}
    assert {
        "grain_id",
        "mechanism",
        "status",
        "thesis_text",
        "supporting_metrics",
        "required_test_data",
        "simulation_plan",
        "source_schema_version",
    } <= cols


# ---------------------------------------------------------------------------
# §6.4b — diagnostic_accounting classifier
# ---------------------------------------------------------------------------


def test_classify_accounting_bucket():
    assert classify_accounting_bucket("Yes") == "YES"
    assert classify_accounting_bucket("yes") == "YES"
    assert classify_accounting_bucket("No") == "NO"
    assert classify_accounting_bucket("Team A") == "OTHER"
    assert classify_accounting_bucket(None) == "OTHER"
    assert classify_accounting_bucket("") == "OTHER"


# ---------------------------------------------------------------------------
# End-to-end synthetic build
# ---------------------------------------------------------------------------


def test_synthetic_all_tables_populated(synthetic_tables: DossierTables):
    assert len(synthetic_tables.positions) == 6
    assert len(synthetic_tables.market_semantics) == 6
    assert len(synthetic_tables.diagnostic_accounting) == 6
    # episodes only emitted for positions with avg_entry_price set;
    # synthetic fixture has SELL+held inferred per position only when
    # there's a resolution path. For the synthetic data (no redemptions,
    # no SELL fills), episode count is 0.
    assert len(synthetic_tables.position_exit_episodes) >= 0
    # Grain table aggregates positions; expect at most n_positions grains.
    assert 1 <= len(synthetic_tables.result_semantics) <= 6
    # Mechanism evidence: 6 rows per grain (5 mechanisms + 'mixed').
    assert len(synthetic_tables.mechanism_evidence) == 6 * len(synthetic_tables.result_semantics)
    # §6.7-§6.9 read-only mirror trio: synthetic ProtocolReport carries
    # neither branches nor surfaces, so hypothesis_results + surfaces
    # are empty; crosstabs are computed from positions/episodes regardless.
    assert synthetic_tables.hypothesis_results == []
    assert synthetic_tables.surfaces == []
    assert len(synthetic_tables.crosstabs) > 0
    assert len(synthetic_tables.data_lineage) > 0
    # 5 v0 gaps + 1 v5 descriptor gap (synthetic fixture has 6 activity_rows < 50):
    #   market_category_unavailable
    #   sibling_token_prices_at_entry_not_fetched
    #   resolution_rule_2_curprice_disabled_pending_verification
    #   multi_outcome_undercounted_when_wallet_one_sided
    #   market_selection_universe_baseline_missing
    #   wallet_too_sparse_for_hour_distribution (v5: < 50 activity_rows)
    assert len(synthetic_tables.data_gaps) == 6
    # dossier_meta: schema_version, wallet, fixture_fetched_at,
    # protocol_report_version, n_positions, n_episodes, n_grains,
    # n_hypotheses, n_surfaces, n_crosstab_rows, n_thesis_candidates (v4),
    # + 5 v5 descriptor counts
    assert len(synthetic_tables.dossier_meta) == 16


def test_synthetic_market_type_distribution(synthetic_tables: DossierTables):
    # 2 binary positions, 3 multi-outcome positions, 1 unknown
    dist = {}
    for r in synthetic_tables.market_semantics:
        dist[r["market_type"]] = dist.get(r["market_type"], 0) + 1
    assert dist == {"binary": 2, "multi_outcome": 3, "unknown": 1}


def test_synthetic_outcome_role_distribution(synthetic_tables: DossierTables):
    dist = {}
    for r in synthetic_tables.market_semantics:
        dist[r["outcome_role"]] = dist.get(r["outcome_role"], 0) + 1
    # 1 binary_yes, 1 binary_no, 3 multi_outcome_selected, 1 unknown
    assert dist == {
        "binary_yes": 1,
        "binary_no": 1,
        "multi_outcome_selected": 3,
        "unknown": 1,
    }


def test_synthetic_diagnostic_accounting_distribution(synthetic_tables: DossierTables):
    dist = {}
    for r in synthetic_tables.diagnostic_accounting:
        dist[r["accounting_bucket"]] = dist.get(r["accounting_bucket"], 0) + 1
    # 1 YES, 1 NO, 4 OTHER (Team A/B/C are OTHER; "Maybe" is OTHER)
    assert dist == {"YES": 1, "NO": 1, "OTHER": 4}


def test_synthetic_category_always_data_unavailable(synthetic_tables: DossierTables):
    # Synthetic fixture has no leafs_by_cid → category stays data_unavailable.
    # event_family stays data_unavailable in v3 (event-family is v4 work).
    # favorite_status: v3 derives it from entry_price for binary markets; the
    # synthetic fixture has 4 multi-outcome positions + 2 binary positions, so
    # we expect a mix of derived favorite/underdog/longshot AND "unknown" for
    # the multi-outcome rows (no sibling-price fetch yet).
    for r in synthetic_tables.market_semantics:
        assert r["category"] == "data_unavailable"
        assert r["category_source"] == "data_unavailable"
        assert r["category_unavailable_reason"] is not None
        assert r["event_family"] == "data_unavailable"
        assert r["favorite_status"] in {"favorite", "underdog", "longshot", "unknown"}


def test_synthetic_size_and_cost_basis_from_totalBought(synthetic_tables: DossierTables):
    """v0 closed-position sizing rule (§6.1): size sourced from `totalBought`,
    cost_basis_usdc = avgPrice * totalBought. Synthetic fixture mirrors live
    /closed-positions shape (Polymarket returns size=0 + totalBought for
    fully-exited rows). See review 2026-05-25 P1 #1."""
    by_asset = {r["asset_id"]: r for r in synthetic_tables.positions}
    # Binary YES: avgPrice=0.40, totalBought=100.0 → cost_basis=40.0
    assert by_asset["1001"]["size"] == 100.0
    assert by_asset["1001"]["cost_basis_usdc"] == pytest.approx(40.0)
    # Multi-outcome longshot: avgPrice=0.10, totalBought=1000.0 → cost_basis=100.0
    assert by_asset["2002"]["size"] == 1000.0
    assert by_asset["2002"]["cost_basis_usdc"] == pytest.approx(100.0)
    # Sibling longshot: avgPrice=0.02, totalBought=5000.0 → cost_basis=100.0
    assert by_asset["2003"]["size"] == 5000.0
    assert by_asset["2003"]["cost_basis_usdc"] == pytest.approx(100.0)


def test_sizing_handles_size_zero_with_totalBought(tmp_path: Path):
    """Real /closed-positions shape: size=0 (no current holdings) and
    totalBought populated. Builder MUST NOT read .size — must read .totalBought."""
    wallet = "0x" + "1" * 40
    fix = tmp_path / "fixture.json"
    rep = tmp_path / "report.json"
    fix.write_text(json.dumps({
        "wallet": wallet,
        "fetched_at": 1700000000,
        "closed_positions": [
            {
                "conditionId": "0x" + "a" * 64,
                "asset": "1001",
                "outcome": "Yes",
                "title": "Real-shape closed position",
                "avgPrice": 0.997859,
                "size": 0,
                "totalBought": 22752.55,
                "realizedPnl": 48.55,
            },
        ],
        "open_positions": [], "activity_rows": [], "leafs_by_cid": {},
    }))
    rep.write_text(json.dumps({"wallet": wallet, "protocol_version": "v2"}))
    tables = build_dossier(wallet=wallet, fixture_path=fix, protocol_report_path=rep)
    assert len(tables.positions) == 1
    row = tables.positions[0]
    assert row["size"] == 22752.55
    assert row["cost_basis_usdc"] == pytest.approx(0.997859 * 22752.55)


def test_sizing_null_when_totalBought_missing(tmp_path: Path):
    """Guardrail: totalBought missing → size=NULL, cost_basis=NULL.
    Builder must not silently fall back to .size (which would also be 0)."""
    wallet = "0x" + "2" * 40
    fix = tmp_path / "fixture.json"
    rep = tmp_path / "report.json"
    fix.write_text(json.dumps({
        "wallet": wallet,
        "fetched_at": 1700000000,
        "closed_positions": [
            {
                "conditionId": "0x" + "b" * 64,
                "asset": "2001",
                "outcome": "Yes",
                "title": "Missing totalBought",
                "avgPrice": 0.50,
                # totalBought intentionally absent
                "realizedPnl": 0.0,
            },
        ],
        "open_positions": [], "activity_rows": [], "leafs_by_cid": {},
    }))
    rep.write_text(json.dumps({"wallet": wallet, "protocol_version": "v2"}))
    tables = build_dossier(wallet=wallet, fixture_path=fix, protocol_report_path=rep)
    row = tables.positions[0]
    assert row["size"] is None
    assert row["cost_basis_usdc"] is None


def test_sizing_cost_basis_null_when_avgPrice_missing(tmp_path: Path):
    """Guardrail: avgPrice missing → size still populated from totalBought,
    cost_basis_usdc=NULL because multiplication is not well-defined."""
    wallet = "0x" + "3" * 40
    fix = tmp_path / "fixture.json"
    rep = tmp_path / "report.json"
    fix.write_text(json.dumps({
        "wallet": wallet,
        "fetched_at": 1700000000,
        "closed_positions": [
            {
                "conditionId": "0x" + "c" * 64,
                "asset": "3001",
                "outcome": "Yes",
                "title": "Missing avgPrice",
                # avgPrice intentionally absent
                "totalBought": 250.0,
                "realizedPnl": 0.0,
            },
        ],
        "open_positions": [], "activity_rows": [], "leafs_by_cid": {},
    }))
    rep.write_text(json.dumps({"wallet": wallet, "protocol_version": "v2"}))
    tables = build_dossier(wallet=wallet, fixture_path=fix, protocol_report_path=rep)
    row = tables.positions[0]
    assert row["size"] == 250.0
    assert row["cost_basis_usdc"] is None


def test_sizing_lineage_records_totalBought_source(synthetic_tables: DossierTables):
    """Lineage for positions.size must point to totalBought (not .size)."""
    size_lineage = [
        r for r in synthetic_tables.data_lineage
        if r["table"] == "positions" and r["column"] == "size"
    ]
    assert len(size_lineage) >= 1
    for r in size_lineage:
        assert "totalBought" in r["source_detail"]
        assert ".size" not in r["source_detail"]
    cost_lineage = [
        r for r in synthetic_tables.data_lineage
        if r["table"] == "positions" and r["column"] == "cost_basis_usdc"
    ]
    assert len(cost_lineage) >= 1
    for r in cost_lineage:
        assert "avgPrice * totalBought" in r["source_detail"]


def test_synthetic_entry_price_buckets_assigned(synthetic_tables: DossierTables):
    # Every position has a price → every entry_price_bucket should be set
    for r in synthetic_tables.positions:
        assert r["entry_price_bucket"] is not None
        assert r["entry_price_bucket"].startswith("p_")
    for r in synthetic_tables.market_semantics:
        assert r["entry_price_bucket"] is not None


def test_missing_entry_price_records_honest_provenance(tmp_path: Path):
    """When closed_positions[i].avgPrice is missing, the dossier must NOT
    claim 'derived from bin(entry_price)' — that would be false provenance.

    Expected behavior (per codex review):
      - positions.entry_price_bucket = NULL
      - market_semantics.entry_price_bucket = literal "data_unavailable"
      - lineage source = data_unavailable, reason = entry_price_missing
    """
    fix = tmp_path / "fixture.json"
    rep = tmp_path / "report.json"
    fix.write_text(json.dumps({
        "wallet": "0x" + "f" * 40,
        "fetched_at": 1700000000,
        "closed_positions": [
            {
                "conditionId": "0x" + "d" * 64,
                "asset": "9001",
                "outcome": "Yes",
                "title": "Missing price market",
                # avgPrice intentionally absent
                "totalBought": 100.0,
                "realizedPnl": 0.0,
            },
            {
                "conditionId": "0x" + "d" * 64,
                "asset": "9002",
                "outcome": "No",
                "title": "Missing price market",
                # avgPrice intentionally absent
                "totalBought": 100.0,
                "realizedPnl": 0.0,
            },
        ],
        "open_positions": [],
        "activity_rows": [],
        "leafs_by_cid": {},
    }))
    rep.write_text(json.dumps({"wallet": "0x" + "f" * 40, "protocol_version": "v2"}))

    tables = build_dossier(
        wallet="0x" + "f" * 40,
        fixture_path=fix,
        protocol_report_path=rep,
    )

    assert len(tables.positions) == 2
    for pos in tables.positions:
        assert pos["entry_price"] is None
        assert pos["entry_price_bucket"] is None, (
            "positions.entry_price_bucket should be NULL when entry_price is missing"
        )

    for ms in tables.market_semantics:
        assert ms["entry_price_bucket"] == "data_unavailable", (
            "market_semantics.entry_price_bucket must be the literal 'data_unavailable'"
            " sentinel so grain-quality gate 7 can detect it later"
        )

    # Lineage must record the honest source, not the false 'bin(entry_price)' claim
    bucket_lineage = [
        l for l in tables.data_lineage
        if l["table"] == "market_semantics" and l["column"] == "entry_price_bucket"
    ]
    assert len(bucket_lineage) == 2
    for l in bucket_lineage:
        assert l["source"] == "data_unavailable", (
            f"expected source=data_unavailable, got {l['source']!r}"
        )
        assert l["unavailable_reason"] == "entry_price_missing"
        # Crucially: no source_detail like 'bin(entry_price)' that would lie
        assert l.get("source_detail") is None or "bin(" not in (l.get("source_detail") or "")


def test_synthetic_v0_complete_columns_populated(synthetic_tables: DossierTables):
    """v0_complete populates everything except outcome_role_rank (still
    `data_unavailable` per §7.3). data_quality_flags is NULL only when
    the producer surface emits no caveats for the position (clean row)."""
    for r in synthetic_tables.positions:
        assert r["n_exit_episodes"] is not None
        assert r["dominant_counterfactual_class"] is not None
        assert r["position_pattern"] is not None
        # data_quality_flags is None here because the synthetic
        # ProtocolReport has no position_building_surface — the column
        # is wired but legitimately absent. Real-fixture coverage in
        # the canonical/SwissMiss tests exercises the populated path.
        assert r["data_quality_flags"] is None


def test_position_pattern_reads_accumulation_pattern_key(tmp_path: Path):
    """P1 #3 regression (review 2026-05-25): position_building producer
    emits the key 'accumulation_pattern' (position_building.py:600), but
    builder.py read 'pattern' — so every real dossier row defaulted to
    position_pattern='unknown', triggering gate 7 and blocking interpretable
    evidence. Provide a position_building_surface entry with the real key
    and assert it flows through to positions[i].position_pattern."""
    wallet = "0x" + "4" * 40
    cid = "0x" + "e" * 64
    fix = tmp_path / "fixture.json"
    rep = tmp_path / "report.json"
    fix.write_text(json.dumps({
        "wallet": wallet,
        "fetched_at": 1700000000,
        "closed_positions": [
            {
                "conditionId": cid,
                "asset": "4001",
                "outcome": "Yes",
                "title": "Pattern lookup test",
                "avgPrice": 0.50,
                "totalBought": 100.0,
                "realizedPnl": 0.0,
            },
        ],
        "open_positions": [], "activity_rows": [], "leafs_by_cid": {},
    }))
    rep.write_text(json.dumps({
        "wallet": wallet,
        "protocol_version": "v2",
        "position_building_surface": {
            "positions": [
                {
                    "position_key": {"condition_id": cid, "asset": "4001"},
                    # Real producer field name (NOT "pattern"):
                    "accumulation_pattern": "single_fill",
                },
            ],
        },
    }))
    tables = build_dossier(wallet=wallet, fixture_path=fix, protocol_report_path=rep)
    assert len(tables.positions) == 1
    assert tables.positions[0]["position_pattern"] == "single_fill"


def test_data_quality_flags_mirror_position_building_caveats(tmp_path: Path):
    """positions.data_quality_flags mirrors the producer's per-position
    caveats list from position_building_surface. Multiple positions, one
    with caveats, one clean — assert per-row state matches input."""
    wallet = "0x" + "b" * 40
    cid_a = "0x" + "1" * 64
    cid_b = "0x" + "2" * 64
    fix = tmp_path / "fixture.json"
    rep = tmp_path / "report.json"
    fix.write_text(json.dumps({
        "wallet": wallet, "fetched_at": 1700000000,
        "closed_positions": [
            {
                "conditionId": cid_a, "asset": "5001",
                "outcome": "Yes", "title": "Caveat-laden position",
                "avgPrice": 0.50, "totalBought": 100.0, "realizedPnl": 0.0,
            },
            {
                "conditionId": cid_b, "asset": "5002",
                "outcome": "Yes", "title": "Clean position",
                "avgPrice": 0.60, "totalBought": 200.0, "realizedPnl": 0.0,
            },
        ],
        "open_positions": [], "activity_rows": [], "leafs_by_cid": {},
    }))
    rep.write_text(json.dumps({
        "wallet": wallet, "protocol_version": "v2",
        "position_building_surface": {
            "positions": [
                {
                    "position_key": {"condition_id": cid_a, "asset": "5001"},
                    "accumulation_pattern": "single_fill",
                    "caveats": ["ACTIVITY_TRUNCATED_POSITION_PATH", "LABEL_FALLBACK_IDENTITY"],
                },
                {
                    "position_key": {"condition_id": cid_b, "asset": "5002"},
                    "accumulation_pattern": "single_fill",
                    "caveats": [],  # clean
                },
            ],
        },
    }))
    tables = build_dossier(wallet=wallet, fixture_path=fix, protocol_report_path=rep)
    by_asset = {r["asset_id"]: r for r in tables.positions}
    flagged = by_asset["5001"]
    clean = by_asset["5002"]
    assert json.loads(flagged["data_quality_flags"]) == [
        "ACTIVITY_TRUNCATED_POSITION_PATH", "LABEL_FALLBACK_IDENTITY",
    ]
    assert clean["data_quality_flags"] is None
    # Lineage check: flagged row gets a data_quality_flags lineage; clean does not.
    flag_lineage = [
        r for r in tables.data_lineage
        if r["table"] == "positions" and r["column"] == "data_quality_flags"
    ]
    assert len(flag_lineage) == 1
    assert flag_lineage[0]["row_key"] == flagged["position_id"]
    assert flag_lineage[0]["source"] == "protocol_report_surface"


def test_data_quality_flags_include_exit_attribution_window_caveats(tmp_path: Path):
    """Spec §6.1 sources data_quality_flags from position_building AND
    exit_attribution. Exit attribution exposes only coverage, so the dossier
    derives per-position outside-window flags from coverage.activity_min_ts."""
    wallet = "0x" + "c" * 40
    old_cid = "0x" + "3" * 64
    fresh_cid = "0x" + "4" * 64
    fix = tmp_path / "fixture.json"
    rep = tmp_path / "report.json"
    fix.write_text(json.dumps({
        "wallet": wallet, "fetched_at": 1700000000,
        "closed_positions": [
            {
                "conditionId": old_cid, "asset": "6001",
                "outcome": "Yes", "title": "Outside activity window",
                "avgPrice": 0.40, "totalBought": 100.0, "realizedPnl": 0.0,
                "timestamp": 1000,
            },
            {
                "conditionId": fresh_cid, "asset": "6002",
                "outcome": "Yes", "title": "Inside activity window",
                "avgPrice": 0.55, "totalBought": 100.0, "realizedPnl": 0.0,
                "timestamp": 3000,
            },
        ],
        "open_positions": [],
        "activity_rows": [{"timestamp": 2000, "conditionId": fresh_cid, "asset": "6002"}],
        "leafs_by_cid": {},
    }))
    rep.write_text(json.dumps({
        "wallet": wallet,
        "protocol_version": "v2",
        "exit_attribution": {
            "summary": {},
            "coverage": {
                "activity_min_ts": 2000,
                "n_outside_window": 1,
                "history_assumed_full": True,
            },
        },
    }))
    tables = build_dossier(wallet=wallet, fixture_path=fix, protocol_report_path=rep)
    by_asset = {r["asset_id"]: r for r in tables.positions}
    assert json.loads(by_asset["6001"]["data_quality_flags"]) == [
        "EXIT_ATTRIBUTION_POSITION_OUTSIDE_ACTIVITY_WINDOW"
    ]
    assert by_asset["6002"]["data_quality_flags"] is None
    flag_lineage = [
        r for r in tables.data_lineage
        if r["table"] == "positions" and r["column"] == "data_quality_flags"
    ]
    assert len(flag_lineage) == 1
    assert "ProtocolReport.exit_attribution.coverage" in flag_lineage[0]["source_detail"]


def test_position_pattern_unknown_when_legacy_pattern_key_only(tmp_path: Path):
    """Negative test: an entry that only carries the OLD 'pattern' key must
    NOT match (the lookup is by accumulation_pattern). Guards against
    silent re-introduction of the dual-key reader."""
    wallet = "0x" + "5" * 40
    cid = "0x" + "f" * 64
    fix = tmp_path / "fixture.json"
    rep = tmp_path / "report.json"
    fix.write_text(json.dumps({
        "wallet": wallet,
        "fetched_at": 1700000000,
        "closed_positions": [
            {
                "conditionId": cid,
                "asset": "5001",
                "outcome": "Yes",
                "title": "Pattern lookup negative test",
                "avgPrice": 0.50,
                "totalBought": 100.0,
                "realizedPnl": 0.0,
            },
        ],
        "open_positions": [], "activity_rows": [], "leafs_by_cid": {},
    }))
    rep.write_text(json.dumps({
        "wallet": wallet,
        "protocol_version": "v2",
        "position_building_surface": {
            "positions": [
                {
                    "position_key": {"condition_id": cid, "asset": "5001"},
                    # OLD/wrong key — must NOT be read:
                    "pattern": "gradual_accumulation",
                },
            ],
        },
    }))
    tables = build_dossier(wallet=wallet, fixture_path=fix, protocol_report_path=rep)
    assert tables.positions[0]["position_pattern"] == "unknown"


def test_synthetic_resolution_columns_populated(synthetic_tables: DossierTables):
    """§7.5 (this slice): every row has resolution + exit_mode set."""
    for r in synthetic_tables.positions:
        assert r["selected_token_resolution"] in {
            "won", "lost", "still_open", "unknown"
        }, f"resolution must be from the 4-value vocab: got {r['selected_token_resolution']!r}"
        assert r["resolution_source"] is not None
        assert r["resolution_confidence"] in {"high", "medium", "low"}
        assert r["exit_mode"] in {
            "held", "pre_resolution", "still_open", "unresolved", "unknown"
        }


# ---------------------------------------------------------------------------
# Hard rules
# ---------------------------------------------------------------------------


def test_diagnostic_accounting_table_separation(synthetic_tables: DossierTables):
    """§9 hard rule: accounting_bucket must NOT appear in market_semantics."""
    for r in synthetic_tables.market_semantics:
        assert "accounting_bucket" not in r, (
            "market_semantics must not contain accounting_bucket; "
            "diagnostic_accounting is a separate table"
        )


def test_schema_has_no_accounting_bucket_in_market_semantics():
    """§9: schema-level enforcement, not just data-level."""
    ms_ddl = [d for d in SCHEMA_DDL if "CREATE TABLE IF NOT EXISTS market_semantics" in d]
    assert len(ms_ddl) == 1
    assert "accounting_bucket" not in ms_ddl[0]


# ---------------------------------------------------------------------------
# Lineage completeness (§6.10)
# ---------------------------------------------------------------------------


def test_lineage_covers_every_non_null_position_cell(synthetic_tables: DossierTables):
    """Every non-null, non-key cell in positions must have a lineage row."""
    KEY_OR_INTERNAL = {
        "position_id",  # PK
        "outcome_role_rank",  # always None in v0
    }
    lineage_keys: set[tuple[str, str, str]] = {
        (l["table"], l["column"], l["row_key"]) for l in synthetic_tables.data_lineage
    }
    for pos in synthetic_tables.positions:
        pid = pos["position_id"]
        for col, val in pos.items():
            if col in KEY_OR_INTERNAL or val is None:
                continue
            assert ("positions", col, pid) in lineage_keys, (
                f"missing lineage for positions.{col} row_key={pid}"
            )


def test_lineage_covers_every_non_null_market_semantics_cell(synthetic_tables: DossierTables):
    KEY = {"position_id"}
    lineage_keys: set[tuple[str, str, str]] = {
        (l["table"], l["column"], l["row_key"]) for l in synthetic_tables.data_lineage
    }
    for ms in synthetic_tables.market_semantics:
        pid = ms["position_id"]
        for col, val in ms.items():
            if col in KEY or val is None:
                continue
            assert ("market_semantics", col, pid) in lineage_keys, (
                f"missing lineage for market_semantics.{col} row_key={pid}"
            )


def test_lineage_covers_every_non_null_diagnostic_accounting_cell(
    synthetic_tables: DossierTables,
):
    KEY = {"position_id"}
    lineage_keys: set[tuple[str, str, str]] = {
        (l["table"], l["column"], l["row_key"]) for l in synthetic_tables.data_lineage
    }
    for da in synthetic_tables.diagnostic_accounting:
        pid = da["position_id"]
        for col, val in da.items():
            if col in KEY or val is None:
                continue
            assert ("diagnostic_accounting", col, pid) in lineage_keys


def test_lineage_covers_every_non_null_data_gaps_cell(
    synthetic_tables: DossierTables,
):
    """§6.10: data_gaps is a semantic table — every non-key cell needs lineage."""
    KEY = {"gap_name"}  # PK
    lineage_keys: set[tuple[str, str, str]] = {
        (l["table"], l["column"], l["row_key"]) for l in synthetic_tables.data_lineage
    }
    for gap in synthetic_tables.data_gaps:
        gap_name = gap["gap_name"]
        for col, val in gap.items():
            if col in KEY or val is None:
                continue
            assert ("data_gaps", col, gap_name) in lineage_keys, (
                f"missing lineage for data_gaps.{col} row_key={gap_name}"
            )


def test_dossier_meta_explicitly_exempt_from_lineage(
    synthetic_tables: DossierTables,
):
    """dossier_meta is the ONE explicitly-exempt table per
    SPEC_EXEMPT_TABLES_FROM_LINEAGE. No lineage rows should reference it."""
    meta_lineage_rows = [
        l for l in synthetic_tables.data_lineage if l["table"] == "dossier_meta"
    ]
    assert meta_lineage_rows == [], (
        "dossier_meta is metadata, not a semantic claim — should not appear in data_lineage"
    )


def test_lineage_data_unavailable_rows_have_reason(synthetic_tables: DossierTables):
    """§6.10: source=data_unavailable requires unavailable_reason."""
    for l in synthetic_tables.data_lineage:
        if l["source"] == "data_unavailable":
            assert l["unavailable_reason"], (
                f"{l['table']}.{l['column']} has source=data_unavailable but no reason"
            )


# ---------------------------------------------------------------------------
# data_gaps presence
# ---------------------------------------------------------------------------


def test_data_gaps_lists_the_v0_complete_gaps(synthetic_tables: DossierTables):
    """v1_market_semantics renamed market_tags_not_provisioned to
    market_category_unavailable (spec §2.3) and made it count-based.
    For the synthetic fixture (no leafs_by_cid, no nav_lookup), every
    market_semantics row remains data_unavailable so the gap IS emitted."""
    names = {g["gap_name"] for g in synthetic_tables.data_gaps}
    assert names == {
        "market_category_unavailable",
        "sibling_token_prices_at_entry_not_fetched",
        "resolution_rule_2_curprice_disabled_pending_verification",
        "multi_outcome_undercounted_when_wallet_one_sided",
        "market_selection_universe_baseline_missing",
        # v5: synthetic fixture has 6 activity_rows < 50 threshold
        "wallet_too_sparse_for_hour_distribution",
    }


def test_data_gaps_names_market_selection_baseline_blocker(
    synthetic_tables: DossierTables,
):
    gap = next(
        g for g in synthetic_tables.data_gaps
        if g["gap_name"] == "market_selection_universe_baseline_missing"
    )
    assert gap["affected_grain_count"] == len(synthetic_tables.result_semantics)
    assert gap["severity"] == "high"
    assert "mechanism_evidence.market_selection_driven" in json.loads(
        gap["affected_columns"]
    )


def test_data_gaps_severity_in_vocab(synthetic_tables: DossierTables):
    for g in synthetic_tables.data_gaps:
        assert g["severity"] in {"high", "medium", "low"}


# ---------------------------------------------------------------------------
# SQLite sink determinism (acceptance #1)
# ---------------------------------------------------------------------------


def _dump_sqlite_tables(p: Path) -> dict[str, list[tuple]]:
    conn = sqlite3.connect(str(p))
    out = {}
    for tbl in all_dossier_tables():
        rows = conn.execute(f"SELECT * FROM {tbl} ORDER BY 1").fetchall()
        out[tbl] = rows
    conn.close()
    return out


def test_sqlite_sink_row_content_deterministic(synthetic_tables: DossierTables, tmp_path: Path):
    db_a = tmp_path / "a.sqlite"
    db_b = tmp_path / "b.sqlite"
    write_sqlite(synthetic_tables, db_a)
    write_sqlite(synthetic_tables, db_b)

    assert _dump_sqlite_tables(db_a) == _dump_sqlite_tables(db_b), (
        "spec acceptance #1: same inputs must produce byte-identical "
        "table content"
    )


def test_json_export_writes_all_tables(synthetic_tables: DossierTables, tmp_path: Path):
    out = tmp_path / "exports"
    write_json_export(synthetic_tables, out)
    expected = {f"{t}.json" for t in all_dossier_tables()}
    actual = {p.name for p in out.iterdir()}
    assert actual == expected


# ---------------------------------------------------------------------------
# Real v4 fixture (SwissMiss) — skipped if not present
# ---------------------------------------------------------------------------


@pytest.fixture
def swissmiss_v4_fixture() -> Path:
    path = V4_STAGING_DIR / f"{SWISSMISS}_2026-05-25.json"
    if not path.exists():
        pytest.skip(f"v4 fixture not present at {path} (run recapture first)")
    return path


@pytest.fixture
def swissmiss_v4_report(tmp_path: Path, swissmiss_v4_fixture: Path) -> Path:
    """Use a minimal stub ProtocolReport."""
    rep = tmp_path / "report.json"
    rep.write_text(json.dumps({
        "wallet": SWISSMISS,
        "protocol_version": "v2",
        "overall_verdict": "underpowered",
        "gate_result": "pass",
    }))
    return rep


def test_swissmiss_v4_real_build_smoke(
    swissmiss_v4_fixture: Path,
    swissmiss_v4_report: Path,
    tmp_path: Path,
):
    tables = build_dossier(
        wallet=SWISSMISS,
        fixture_path=swissmiss_v4_fixture,
        protocol_report_path=swissmiss_v4_report,
        out_db=tmp_path / "swissmiss.sqlite",
        out_json_dir=tmp_path / "json",
    )
    # SwissMiss has 1011 closed positions per v4 capture
    assert len(tables.positions) == 1011
    assert len(tables.market_semantics) == 1011
    assert len(tables.diagnostic_accounting) == 1011
    # v4_thesis_lifecycle uses closed_positions.oppositeAsset to repair
    # one-sided binary rows. If the residual undercount gap is emitted, it
    # must represent real unknown rows, not a stale zero-count placeholder.
    undercount_gaps = [
        g for g in tables.data_gaps
        if g["gap_name"] == "multi_outcome_undercounted_when_wallet_one_sided"
    ]
    if undercount_gaps:
        assert undercount_gaps[0]["affected_grain_count"] > 0
    # Every position has a market_type, entry_price_bucket, outcome_role
    for r in tables.positions:
        assert r["market_type"] in {"binary", "multi_outcome", "unknown"}
        assert r["entry_price_bucket"] is not None
        assert r["outcome_role"] in {
            "binary_yes", "binary_no", "multi_outcome_selected", "unknown",
        }
    # T12 (spec §3): resolution requires BOTH a populated leafs_by_cid AND a
    # populated nav_lookup. Either one missing → all rows must be data_unavailable.
    # If both are present, at least some rows must resolve a real category.
    fixture_data = json.loads(swissmiss_v4_fixture.read_text())
    leafs = fixture_data.get("leafs_by_cid") or {}
    navs = fixture_data.get("nav_lookup") or {}
    if not leafs or not navs:
        # Either tags table or nav lookup absent → no category resolution possible.
        # (Current v4 capture state: leafs_by_cid={} → all data_unavailable.)
        for r in tables.market_semantics:
            assert r["category"] == "data_unavailable", (
                f"expected data_unavailable (missing leafs or nav_lookup), "
                f"got {r['category']!r} for position {r.get('position_id')}"
            )
    else:
        # Both leafs_by_cid AND nav_lookup are populated → some rows must resolve.
        resolved = sum(1 for r in tables.market_semantics if r["category"] != "data_unavailable")
        assert resolved > 0, (
            "fixture carries both leafs_by_cid and nav_lookup but no market_semantics "
            "rows resolved category"
        )
    # Diagnostic accounting buckets sum to total positions
    bucket_counts = {}
    for r in tables.diagnostic_accounting:
        bucket_counts[r["accounting_bucket"]] = bucket_counts.get(r["accounting_bucket"], 0) + 1
    assert sum(bucket_counts.values()) == 1011


def test_swissmiss_v4_lineage_covers_all_cells(
    swissmiss_v4_fixture: Path,
    swissmiss_v4_report: Path,
):
    tables = build_dossier(
        wallet=SWISSMISS,
        fixture_path=swissmiss_v4_fixture,
        protocol_report_path=swissmiss_v4_report,
    )
    lineage_keys: set[tuple[str, str, str]] = {
        (l["table"], l["column"], l["row_key"]) for l in tables.data_lineage
    }
    KEY_OR_INTERNAL = {"position_id", "outcome_role_rank"}
    missing: list[tuple[str, str, str]] = []
    for pos in tables.positions:
        for col, val in pos.items():
            if col in KEY_OR_INTERNAL or val is None:
                continue
            key = ("positions", col, pos["position_id"])
            if key not in lineage_keys:
                missing.append(key)
    assert not missing, (
        f"missing lineage entries (first 5): {missing[:5]}"
    )


def test_schema_version_constant():
    assert DOSSIER_SCHEMA_VERSION == "v5_behavioral_descriptors"


# ---------------------------------------------------------------------------
# Canonical in-repo fixture (trimmed, committed) — CI-enforced baseline
# ---------------------------------------------------------------------------


@pytest.fixture
def canonical_fixture() -> Path:
    """Trimmed v4 capture of CANONICAL_WALLET. Committed at
    `tests/calibration/dossier-fixtures/`. Regenerate via
    `scripts/trim_dossier_fixture.py`. This fixture MUST exist — unlike
    the larger v4-staging set it ships in the repo so CI exercises real
    Polymarket /closed-positions shape on every run."""
    assert CANONICAL_FIXTURE.exists(), (
        f"canonical fixture missing at {CANONICAL_FIXTURE}; "
        "regenerate with scripts/trim_dossier_fixture.py"
    )
    return CANONICAL_FIXTURE


@pytest.fixture
def canonical_report(tmp_path: Path) -> Path:
    rep = tmp_path / "report.json"
    rep.write_text(json.dumps({
        "wallet": CANONICAL_WALLET,
        "protocol_version": "v2",
        "overall_verdict": "underpowered",
        "gate_result": "pass",
    }))
    return rep


def test_canonical_fixture_real_build_smoke(
    canonical_fixture: Path,
    canonical_report: Path,
    tmp_path: Path,
):
    """Real-shape regression: builds the dossier from a real Polymarket
    fixture (trimmed but otherwise unchanged). Catches field-name drift
    in /closed-positions, /activity, /redemptions responses."""
    tables = build_dossier(
        wallet=CANONICAL_WALLET,
        fixture_path=canonical_fixture,
        protocol_report_path=canonical_report,
        out_db=tmp_path / "canonical.sqlite",
        out_json_dir=tmp_path / "json",
    )
    # 275 closed positions baseline (matches trimmed fixture probe).
    assert len(tables.positions) == 275
    # All 275 have non-NULL size + cost_basis_usdc thanks to P1 #1 fix.
    null_size = [r for r in tables.positions if r["size"] is None]
    null_cost = [r for r in tables.positions if r["cost_basis_usdc"] is None]
    assert null_size == [], (
        f"P1 #1 regression risk: {len(null_size)} positions with NULL size "
        f"(should be 0 — all rows have avgPrice+totalBought)"
    )
    assert null_cost == [], (
        f"P1 #1 regression risk: {len(null_cost)} positions with NULL "
        f"cost_basis_usdc (should be 0)"
    )
    # Every position has a market_type, entry_price_bucket, outcome_role.
    # v3 added binary_named_outcome to the valid set.
    for r in tables.positions:
        assert r["market_type"] in {"binary", "multi_outcome", "unknown"}
        assert r["entry_price_bucket"] is not None
        assert r["outcome_role"] in {
            "binary_yes", "binary_no", "binary_named_outcome",
            "multi_outcome_selected", "unknown",
        }


def test_canonical_fixture_market_structure_repair_clears_zero_count_gap(
    canonical_fixture: Path,
    canonical_report: Path,
):
    tables = build_dossier(
        wallet=CANONICAL_WALLET,
        fixture_path=canonical_fixture,
        protocol_report_path=canonical_report,
    )
    assert {r["market_type"] for r in tables.market_semantics} == {"binary"}
    assert not [
        g for g in tables.data_gaps
        if g["gap_name"] == "multi_outcome_undercounted_when_wallet_one_sided"
    ]


def test_canonical_fixture_paired_redemptions_exit_held(
    canonical_fixture: Path,
    canonical_report: Path,
):
    """P1 #2 regression: the trimmed fixture's 249 redemptions are 100%
    paired (indexSets=['1','2']). Most are single-side holders, so they
    must resolve via §7.5 rule 1 paired branch and land on exit_mode='held'
    — NOT 'unknown' as before the fix."""
    tables = build_dossier(
        wallet=CANONICAL_WALLET,
        fixture_path=canonical_fixture,
        protocol_report_path=canonical_report,
    )
    paired_single = [
        r for r in tables.positions
        if r["resolution_source"] == "redemption_event_paired_single_side_holder"
    ]
    assert len(paired_single) > 0, (
        "expected paired-single-side rows in a wallet with 249 paired redemptions"
    )
    bad = [r for r in paired_single if r["exit_mode"] != "held"]
    assert bad == [], (
        f"P1 #2 regression: {len(bad)}/{len(paired_single)} paired-single rows "
        f"have exit_mode != 'held'. Sample: "
        f"{[(r['asset_id'], r['exit_mode']) for r in bad[:5]]}"
    )


def test_canonical_fixture_held_redemptions_have_exit_ts(
    canonical_fixture: Path,
    canonical_report: Path,
):
    """Spec §6.1: exit_ts is last SELL or redemption timestamp."""
    tables = build_dossier(
        wallet=CANONICAL_WALLET,
        fixture_path=canonical_fixture,
        protocol_report_path=canonical_report,
    )
    held_redemptions = [
        r for r in tables.positions
        if r["exit_mode"] == "held"
        and str(r["resolution_source"]).startswith("redemption_event")
    ]
    assert held_redemptions, "fixture should exercise held redemption rows"
    missing = [r for r in held_redemptions if r["exit_ts"] is None]
    assert missing == [], (
        f"{len(missing)}/{len(held_redemptions)} held redemption rows have NULL "
        f"exit_ts; sample: {[(r['asset_id'], r['resolution_source']) for r in missing[:5]]}"
    )


def test_canonical_fixture_no_curprice_resolution_while_flag_off(
    canonical_fixture: Path,
    canonical_report: Path,
):
    """While RESOLVE_BY_CURPRICE_ENABLED=False, no row may carry
    resolution_source='closed_position_curPrice'. (Same constraint as
    Acceptance #10, but on the in-repo trimmed fixture so CI enforces it.)"""
    assert RESOLVE_BY_CURPRICE_ENABLED is False, (
        "this test assumes flag OFF; flip the assertion if flag flips"
    )
    tables = build_dossier(
        wallet=CANONICAL_WALLET,
        fixture_path=canonical_fixture,
        protocol_report_path=canonical_report,
    )
    bad = [r for r in tables.positions if r["resolution_source"] == "closed_position_curPrice"]
    assert bad == [], (
        f"Found {len(bad)} rows with curPrice resolution while flag is OFF — "
        "violates spec §7.5 + Acceptance #10"
    )


def test_canonical_fixture_outputs_byte_stable(
    canonical_fixture: Path,
    canonical_report: Path,
    tmp_path: Path,
):
    """Acceptance #1 on the in-repo real-shape fixture: same input produces
    identical markdown and identical SQLite row content."""
    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    out_a.mkdir()
    out_b.mkdir()
    build_dossier(
        wallet=CANONICAL_WALLET,
        fixture_path=canonical_fixture,
        protocol_report_path=canonical_report,
        out_db=out_a / "dossier.sqlite",
        out_md=out_a / "dossier.md",
        include_llm_summary=True,
    )
    build_dossier(
        wallet=CANONICAL_WALLET,
        fixture_path=canonical_fixture,
        protocol_report_path=canonical_report,
        out_db=out_b / "dossier.sqlite",
        out_md=out_b / "dossier.md",
        include_llm_summary=True,
    )
    assert (out_a / "dossier.md").read_text() == (out_b / "dossier.md").read_text()
    assert _dump_sqlite_tables(out_a / "dossier.sqlite") == _dump_sqlite_tables(
        out_b / "dossier.sqlite"
    )


# ---------------------------------------------------------------------------
# §10 markdown report
# ---------------------------------------------------------------------------


def _md_with_synthetic(tmp_path: Path, *, include_llm: bool = False) -> str:
    tmp_path.mkdir(parents=True, exist_ok=True)
    wallet = "0xdeadbeef" + "0" * 32
    fix = tmp_path / "fixture.json"
    rep = tmp_path / "report.json"
    fix.write_text(json.dumps(_synthetic_wallet_data()))
    rep.write_text(json.dumps(_synthetic_protocol_report(wallet)))
    out_md = tmp_path / "report.md"
    build_dossier(
        wallet=wallet, fixture_path=fix, protocol_report_path=rep,
        out_md=out_md, include_llm_summary=include_llm,
    )
    return out_md.read_text()


def test_markdown_report_contains_all_required_sections(tmp_path: Path):
    """Sections A-J must always be present (K only with --llm-summary)."""
    md = _md_with_synthetic(tmp_path)
    for heading in (
        "## A. Top-level",
        "## B. Hypothesis tally",
        "## C. Surfaces",
        "## D. Diagnostic accounting",
        "## E. Market semantics",
        "## F. Result semantics",
        "## G. Mechanism evidence",
        "## H. Diagnostic-only grains",
        "## I. Data lineage summary",
        "## J. Data gaps",
    ):
        assert heading in md, f"missing required section: {heading!r}"
    # Section K only when opted in.
    assert "## K. Operator-requested LLM Summary" not in md


def test_markdown_section_k_present_with_llm_summary(tmp_path: Path):
    md = _md_with_synthetic(tmp_path, include_llm=True)
    assert "## K. Operator-requested LLM Summary" in md
    # Template must include the 6 numbered slots per spec §10.K.
    for tag in ("[1]", "[2]", "[3]", "[4]", "[5]", "[6]"):
        assert tag in md, f"section K missing template tag {tag!r}"


def test_markdown_byte_stable_for_same_input(tmp_path: Path):
    """Acceptance #1: same input → byte-identical markdown."""
    a = _md_with_synthetic(tmp_path / "a", include_llm=True)
    b = _md_with_synthetic(tmp_path / "b", include_llm=True)
    assert a == b


def test_markdown_vocab_blocklist_clean(tmp_path: Path):
    """§11.7 — dossier-generated markdown must contain NONE of the
    blocklisted phrases (case-insensitive)."""
    md = _md_with_synthetic(tmp_path, include_llm=True).lower()
    from sniper.analysis.wallet_dossier.report_markdown import VOCAB_BLOCKLIST
    for phrase in VOCAB_BLOCKLIST:
        assert phrase not in md, (
            f"§11.7 hard-rule violation: blocklisted phrase {phrase!r} appears in markdown"
        )


def test_markdown_canonical_fixture_vocab_clean(
    canonical_fixture: Path,
    canonical_report: Path,
    tmp_path: Path,
):
    """Same blocklist check, but against a real-shape fixture (275
    positions). Belt and braces — catches any conditional code paths
    that wouldn't fire on the synthetic input."""
    out_md = tmp_path / "canonical.md"
    build_dossier(
        wallet=CANONICAL_WALLET,
        fixture_path=canonical_fixture,
        protocol_report_path=canonical_report,
        out_md=out_md,
        include_llm_summary=True,
    )
    md = out_md.read_text().lower()
    from sniper.analysis.wallet_dossier.report_markdown import VOCAB_BLOCKLIST
    for phrase in VOCAB_BLOCKLIST:
        assert phrase not in md, (
            f"§11.7 violation on canonical fixture: phrase {phrase!r} in markdown"
        )


def _assert_vocab_blocklist_clean_in_text_cells(tables: DossierTables) -> None:
    """§11.7 covers generated DB text cells, not only rendered markdown."""
    from sniper.analysis.wallet_dossier.report_markdown import VOCAB_BLOCKLIST

    text_cells: list[tuple[str, str, str]] = []
    for r in tables.mechanism_evidence:
        row_key = f"{r['grain_id']}|{r['mechanism']}"
        for col in (
            "mechanism",
            "evidence_strength",
            "supporting_metrics",
            "caveats",
            "missing_data",
            "candidate_thesis_evidence",
            "not_promoted_reason",
        ):
            if r.get(col) is not None:
                text_cells.append(("mechanism_evidence", row_key, str(r[col]).lower()))
    for r in tables.result_semantics:
        row_key = r["grain_id"]
        for col in (
            "grain_key",
            "episodes_by_class",
            "grain_quality",
            "grain_quality_reasons",
        ):
            if r.get(col) is not None:
                text_cells.append(("result_semantics", row_key, str(r[col]).lower()))

    violations = [
        (table, row_key, phrase)
        for table, row_key, cell in text_cells
        for phrase in VOCAB_BLOCKLIST
        if phrase in cell
    ]
    assert violations == []


def test_vocab_blocklist_clean_in_dossier_text_cells(synthetic_tables: DossierTables):
    _assert_vocab_blocklist_clean_in_text_cells(synthetic_tables)


def test_vocab_blocklist_clean_in_canonical_text_cells(
    canonical_fixture: Path,
    canonical_report: Path,
):
    tables = build_dossier(
        wallet=CANONICAL_WALLET,
        fixture_path=canonical_fixture,
        protocol_report_path=canonical_report,
    )
    _assert_vocab_blocklist_clean_in_text_cells(tables)


def test_markdown_header_mirrors_wallet_and_schema(tmp_path: Path):
    md = _md_with_synthetic(tmp_path)
    # Title line should carry the lowercased wallet.
    first_line = md.split("\n", 1)[0]
    assert "wallet dossier" in first_line.lower()
    assert "0xdeadbeef" in first_line
    # Schema marker must appear (independent of which version is current).
    assert "_Schema:" in md


def test_markdown_diagnostic_section_carries_warning(tmp_path: Path):
    md = _md_with_synthetic(tmp_path)
    # Find section D and check the warning text appears in it.
    d_section, *_rest = md.split("## E. Market semantics")
    assert "DIAGNOSTIC ONLY" in d_section


def test_markdown_does_not_emit_generic_win_rate_column(tmp_path: Path):
    """Acceptance #11: no generic `win_rate` column — denominator must
    always be named (e.g. outcome_win_rate_held, trade_win_rate_preres)."""
    md = _md_with_synthetic(tmp_path, include_llm=True)
    # The string "| win_rate |" would indicate a generic column header.
    # Allow the named variants by checking the literal column header form,
    # case-insensitively.
    assert "| win_rate |" not in md.lower()


# ---------------------------------------------------------------------------
# §6.7 hypothesis_results — branch hypothesis mirror
# ---------------------------------------------------------------------------


def test_hypothesis_results_empty_when_no_branches(synthetic_tables: DossierTables):
    """Synthetic ProtocolReport has no branches; the table must be empty
    rather than raising or fabricating rows."""
    assert synthetic_tables.hypothesis_results == []


def test_hypothesis_results_mirrors_branch_hypotheses(tmp_path: Path):
    """Both branches contribute one row per hypothesis. Mirror only —
    no derivation. p_value prefers BH-adjusted; notes are joined."""
    wallet = "0x" + "7" * 40
    fix = tmp_path / "fixture.json"
    rep = tmp_path / "report.json"
    fix.write_text(json.dumps({
        "wallet": wallet, "fetched_at": 1700000000,
        "closed_positions": [], "open_positions": [],
        "activity_rows": [], "leafs_by_cid": {},
    }))
    rep.write_text(json.dumps({
        "wallet": wallet,
        "protocol_version": "v2",
        "held_analyst_branch": {
            "branch": "held_analyst",
            "is_active": True,
            "hypotheses": [
                {
                    "name": "H1", "verdict": "supported",
                    "n": 120,
                    "p_value_raw": 0.01,
                    "p_value_bh_adjusted": 0.04,
                    "details": {"effect_size_excess_pp": 8.5},
                    "notes": ["sample concentrated in 2024", "BH adjusted within branch"],
                },
                {
                    "name": "H2", "verdict": "underpowered",
                    "n": 12,
                    "test_statistic": 0.123,
                    "p_value_raw": None,
                    "p_value_bh_adjusted": None,
                    "details": {},
                    "notes": [],
                },
            ],
        },
        "flipper_branch": {
            "branch": "flipper",
            "is_active": True,
            "hypotheses": [
                {
                    "name": "FH3", "verdict": "refuted",
                    "n": 40,
                    "p_value_bh_adjusted": 0.91,
                    "details": {"roi_excess_pp": -1.2},
                    "notes": None,  # tolerate None for notes
                },
            ],
        },
    }))
    tables = build_dossier(wallet=wallet, fixture_path=fix, protocol_report_path=rep)
    # Sort by the (branch, name) PK pair. "flipper" < "held_analyst" lexically.
    rows = sorted(tables.hypothesis_results, key=lambda r: (r["branch"], r["name"]))
    assert [r["name"] for r in rows] == ["FH3", "H1", "H2"]
    assert [r["branch"] for r in rows] == ["flipper", "held_analyst", "held_analyst"]
    h1 = next(r for r in rows if r["name"] == "H1")
    assert h1["branch"] == "held_analyst"
    assert h1["verdict"] == "supported"
    assert h1["n"] == 120
    assert h1["p_value"] == pytest.approx(0.04)  # BH-adjusted, not raw
    assert h1["effect_size"] == pytest.approx(8.5)
    assert h1["note"] == "sample concentrated in 2024; BH adjusted within branch"
    h2 = next(r for r in rows if r["name"] == "H2")
    assert h2["p_value"] is None
    assert h2["effect_size"] == pytest.approx(0.123)
    assert h2["note"] is None
    fh3 = next(r for r in rows if r["name"] == "FH3")
    assert fh3["branch"] == "flipper"
    assert fh3["effect_size"] == pytest.approx(-1.2)
    assert fh3["note"] is None


def test_hypothesis_results_lineage_points_to_protocol_report(tmp_path: Path):
    """Every non-key cell must trace to ProtocolReport via lineage."""
    wallet = "0x" + "8" * 40
    fix = tmp_path / "fixture.json"
    rep = tmp_path / "report.json"
    fix.write_text(json.dumps({
        "wallet": wallet, "fetched_at": 1700000000,
        "closed_positions": [], "open_positions": [],
        "activity_rows": [], "leafs_by_cid": {},
    }))
    rep.write_text(json.dumps({
        "wallet": wallet, "protocol_version": "v2",
        "held_analyst_branch": {
            "branch": "held_analyst", "is_active": True,
            "hypotheses": [{
                "name": "H1", "verdict": "supported",
                "n": 50, "p_value_bh_adjusted": 0.02,
                "details": {"effect_size": 5.0}, "notes": ["note"],
            }],
        },
    }))
    tables = build_dossier(wallet=wallet, fixture_path=fix, protocol_report_path=rep)
    lineage = [r for r in tables.data_lineage if r["table"] == "hypothesis_results"]
    # 5 non-key columns × 1 row = 5 lineage entries.
    assert len(lineage) == 5
    for r in lineage:
        assert r["source"] == "protocol_report_surface"
        assert "ProtocolReport.held_analyst" in r["source_detail"]


# ---------------------------------------------------------------------------
# §6.8 surfaces — read-only mirror of *_surface dicts
# ---------------------------------------------------------------------------


def test_surfaces_empty_when_no_surfaces_present(synthetic_tables: DossierTables):
    assert synthetic_tables.surfaces == []


def test_surfaces_mirrors_protocol_report_surface_dicts(tmp_path: Path):
    """One row per populated surface. data_state + caveats split out;
    rest goes into key_metrics JSON. Missing/empty surfaces contribute 0 rows."""
    wallet = "0x" + "9" * 40
    fix = tmp_path / "fixture.json"
    rep = tmp_path / "report.json"
    fix.write_text(json.dumps({
        "wallet": wallet, "fetched_at": 1700000000,
        "closed_positions": [], "open_positions": [],
        "activity_rows": [], "leafs_by_cid": {},
    }))
    rep.write_text(json.dumps({
        "wallet": wallet, "protocol_version": "v2",
        "timing_surface": {
            "surface_version": "v1",
            "data_state": "populated",
            "classification": "fast_held",
            "n_attributed": 88,
            "caveats": ["partial coverage"],
        },
        "liquidity_surface": {
            "data_state": "data_unavailable",
            "fidelity_tier": "proxy_only",
            # no caveats field
        },
        "data_completeness": {
            "data_state": "partial",
            "attribution_coverage": {"n_total": 2, "n_insufficient_data": 1},
            "caveats": {"attribution_coverage_low": True},
        },
        "time_coverage": {
            "first_position_ts": 100,
            "last_position_ts": 200,
            "duration_days": 1,
        },
        "capital_ledger": {
            "net_capital_funded_usdc": 123.45,
            "events": [],
        },
        # Not a real ProtocolReport field; must not leak into surfaces.
        "completeness_surface": {"data_state": "wrong_name"},
        # Empty dict → must be skipped
        "copy_latency_surface": {},
        # None → must be skipped
        "position_reconciliation_surface": None,
    }))
    tables = build_dossier(wallet=wallet, fixture_path=fix, protocol_report_path=rep)
    by_name = {r["surface_name"]: r for r in tables.surfaces}
    assert set(by_name.keys()) == {
        "capital_ledger",
        "data_completeness",
        "liquidity_surface",
        "time_coverage",
        "timing_surface",
    }
    ts = by_name["timing_surface"]
    assert ts["data_state"] == "populated"
    km = json.loads(ts["key_metrics"])
    assert km["classification"] == "fast_held"
    assert km["n_attributed"] == 88
    # data_state and caveats must NOT appear in key_metrics
    assert "data_state" not in km
    assert "caveats" not in km
    assert json.loads(ts["caveats"]) == ["partial coverage"]
    ls = by_name["liquidity_surface"]
    assert ls["data_state"] == "data_unavailable"
    assert ls["caveats"] is None
    assert json.loads(ls["key_metrics"])["fidelity_tier"] == "proxy_only"
    dc = by_name["data_completeness"]
    assert dc["data_state"] == "partial"
    assert json.loads(dc["caveats"]) == {"attribution_coverage_low": True}
    assert json.loads(dc["key_metrics"])["attribution_coverage"]["n_total"] == 2
    assert json.loads(by_name["time_coverage"]["key_metrics"])["duration_days"] == 1
    assert json.loads(by_name["capital_ledger"]["key_metrics"])["net_capital_funded_usdc"] == 123.45
    assert "completeness_surface" not in by_name


# ---------------------------------------------------------------------------
# §6.9 crosstabs — pre-computed grain aggregates
# ---------------------------------------------------------------------------


def test_crosstabs_three_position_grained_always_emit(synthetic_tables: DossierTables):
    """The three position-grained crosstabs are emitted whenever positions
    exist. The episode-grained crosstab (entry_bucket × counterfactual_class)
    is present only when episodes exist — synthetic has 0 episodes, so it
    is absent here. A separate canonical-fixture test verifies it appears
    when episodes exist."""
    names = {r["crosstab_name"] for r in synthetic_tables.crosstabs}
    assert {
        "market_type_x_entry_bucket_x_exit_mode",
        "market_type_x_accounting_bucket",
        "position_pattern_x_exit_mode",
    }.issubset(names)
    # No episodes in the synthetic fixture, so the episode-grained crosstab
    # must NOT appear (no fabricated rows).
    assert "entry_bucket_x_counterfactual_class" not in names


def test_crosstabs_episode_grained_present_on_canonical_fixture(
    canonical_fixture: Path,
    canonical_report: Path,
):
    """When episodes exist (canonical fixture has many), the
    entry_bucket × counterfactual_class crosstab must emit rows."""
    tables = build_dossier(
        wallet=CANONICAL_WALLET,
        fixture_path=canonical_fixture,
        protocol_report_path=canonical_report,
    )
    names = {r["crosstab_name"] for r in tables.crosstabs}
    assert "entry_bucket_x_counterfactual_class" in names
    episode_rows = [
        r for r in tables.crosstabs
        if r["crosstab_name"] == "entry_bucket_x_counterfactual_class"
    ]
    # n should sum to the total episode count for this wallet.
    assert sum(r["n"] for r in episode_rows) == len(tables.position_exit_episodes)


def test_crosstabs_accounting_bucket_marked_diagnostic_only(synthetic_tables: DossierTables):
    """The market_type × accounting_bucket crosstab is the diagnostic
    one — must carry is_diagnostic_only=1. None of the others may."""
    for r in synthetic_tables.crosstabs:
        if r["crosstab_name"] == "market_type_x_accounting_bucket":
            assert r["is_diagnostic_only"] == 1
        else:
            assert r["is_diagnostic_only"] == 0


def test_crosstabs_n_sums_match_positions_count(synthetic_tables: DossierTables):
    """n across the position-grained crosstabs must equal n_positions."""
    n_positions = len(synthetic_tables.positions)
    by_name: dict[str, int] = {}
    for r in synthetic_tables.crosstabs:
        by_name[r["crosstab_name"]] = by_name.get(r["crosstab_name"], 0) + r["n"]
    for name in (
        "market_type_x_entry_bucket_x_exit_mode",
        "market_type_x_accounting_bucket",
        "position_pattern_x_exit_mode",
    ):
        assert by_name[name] == n_positions, (
            f"crosstab {name!r} should sum to {n_positions} positions; got {by_name[name]}"
        )


def test_crosstabs_grain_key_is_valid_json(synthetic_tables: DossierTables):
    for r in synthetic_tables.crosstabs:
        gk = json.loads(r["grain_key"])
        assert isinstance(gk, dict)
        assert all(isinstance(v, str) for v in gk.values())


def test_crosstabs_deterministic_id(tmp_path: Path):
    """crosstab_id is a stable hash of (name, grain_key) — same inputs
    must produce the same id across runs."""
    wallet = "0x" + "a" * 40
    fix = tmp_path / "fixture.json"
    rep = tmp_path / "report.json"
    fix.write_text(json.dumps({
        "wallet": wallet, "fetched_at": 1700000000,
        "closed_positions": [{
            "conditionId": "0x" + "1" * 64, "asset": "1", "outcome": "Yes",
            "title": "T", "avgPrice": 0.5, "totalBought": 100.0, "realizedPnl": 10.0,
        }],
        "open_positions": [], "activity_rows": [],
        "leafs_by_cid": {},
    }))
    rep.write_text(json.dumps({"wallet": wallet, "protocol_version": "v2"}))
    t1 = build_dossier(wallet=wallet, fixture_path=fix, protocol_report_path=rep)
    t2 = build_dossier(wallet=wallet, fixture_path=fix, protocol_report_path=rep)
    ids1 = sorted(r["crosstab_id"] for r in t1.crosstabs)
    ids2 = sorted(r["crosstab_id"] for r in t2.crosstabs)
    assert ids1 == ids2


# ---------------------------------------------------------------------------
# §7.5 synthetic edge cases (no real fixture required)
# ---------------------------------------------------------------------------


def test_resolution_paired_dual_side_holder_is_ambiguous():
    """§7.5 rule 1, paired branch: when the wallet held BOTH sides of a
    binary condition, a paired-indexSet redemption payout combines them
    and CANNOT be attributed to a specific asset_id from the record alone.
    Resolution must be 'unknown' with source 'redemption_event_paired_both_sides_held'."""
    cp = {
        "conditionId": "0x" + "a" * 64,
        "asset": "1001",
        "outcomeIndex": 0,  # → our_index_set = "1"
    }
    redemptions = [
        {
            "condition": "0x" + "a" * 64,
            "indexSets": ["1", "2"],
            "payout": 100_000,  # nonzero, but combines both sides
            "timestamp": 1700000000,
        },
    ]
    out = derive_selected_token_resolution(
        closed_position=cp,
        redemptions=redemptions,
        open_positions=[],
        binary_market_n_outcomes=2,
        wallet_holds_other_outcome_for_condition=True,
        enable_curprice_rule=False,
    )
    assert out.value == "unknown"
    assert out.source == "redemption_event_paired_both_sides_held"
    assert out.confidence == "low"


def test_curprice_flag_off_synthetic_does_not_emit_curprice_source(tmp_path: Path):
    """Synthetic mirror of the real-fixture rule-2 check: a position with
    a curPrice that *would* resolve under rule 2 must not gain
    resolution_source='closed_position_curPrice' while the flag is OFF."""
    assert RESOLVE_BY_CURPRICE_ENABLED is False
    wallet = "0x" + "6" * 40
    fix = tmp_path / "fixture.json"
    rep = tmp_path / "report.json"
    fix.write_text(json.dumps({
        "wallet": wallet,
        "fetched_at": 1700000000,
        "closed_positions": [
            {
                "conditionId": "0x" + "d" * 64,
                "asset": "6001",
                "outcome": "Yes",
                "title": "Resolved by curPrice in some hypothetical future",
                "outcomeIndex": 0,
                "avgPrice": 0.20,
                "totalBought": 1000.0,
                "curPrice": 1.0,    # would resolve as won under rule 2 if enabled
                "realizedPnl": 800.0,
            },
        ],
        "open_positions": [],
        "activity_rows": [],
        "leafs_by_cid": {},
    }))
    rep.write_text(json.dumps({"wallet": wallet, "protocol_version": "v2"}))
    tables = build_dossier(
        wallet=wallet, fixture_path=fix, protocol_report_path=rep,
    )
    bad = [r for r in tables.positions if r["resolution_source"] == "closed_position_curPrice"]
    assert bad == []


# ===========================================================================
# §7.6 — episode extraction + 10-class counterfactual taxonomy
# ===========================================================================


def test_classify_counterfactual_held_won():
    klass, src, conf = _classify_counterfactual(
        episode_type="held_to_resolution", trade_result="win",
        resolution_value="won",
    )
    assert klass == "held_won"
    assert src == "episode_is_resolution"
    assert conf == "high"


def test_classify_counterfactual_held_lost():
    klass, _, _ = _classify_counterfactual(
        episode_type="held_to_resolution", trade_result="loss",
        resolution_value="lost",
    )
    assert klass == "held_lost"


def test_classify_counterfactual_profitable_exit_later_won():
    klass, _, _ = _classify_counterfactual(
        episode_type="partial_sell", trade_result="win", resolution_value="won",
    )
    assert klass == "profitable_exit_later_won"


def test_classify_counterfactual_profitable_exit_later_lost():
    """Trade skill independent of prediction."""
    klass, _, _ = _classify_counterfactual(
        episode_type="full_sell", trade_result="win", resolution_value="lost",
    )
    assert klass == "profitable_exit_later_lost"


def test_classify_counterfactual_loss_exit_later_won():
    """Bad exit — sold a winner at a loss."""
    klass, _, _ = _classify_counterfactual(
        episode_type="partial_sell", trade_result="loss", resolution_value="won",
    )
    assert klass == "loss_exit_later_won"


def test_classify_counterfactual_loss_exit_later_lost():
    """Good risk management — cut a loser that would have stayed a loser."""
    klass, _, _ = _classify_counterfactual(
        episode_type="partial_sell", trade_result="loss", resolution_value="lost",
    )
    assert klass == "loss_exit_later_lost"


def test_classify_counterfactual_market_still_open():
    klass, _, _ = _classify_counterfactual(
        episode_type="partial_sell", trade_result="win",
        resolution_value="still_open",
    )
    assert klass == "market_still_open"


def test_classify_counterfactual_unresolved():
    klass, _, _ = _classify_counterfactual(
        episode_type="partial_sell", trade_result="loss",
        resolution_value="unknown",
    )
    assert klass == "unresolved"


def test_extract_episodes_partial_then_held(tmp_path: Path):
    """Position with: BUY 100, SELL 40 @ 0.55 (profit), held 60 to resolution YES."""
    activity = [
        {"asset": "A", "type": "TRADE", "side": "BUY", "size": 100, "price": 0.40, "timestamp": 100},
        {"asset": "A", "type": "TRADE", "side": "SELL", "size": 40, "price": 0.55, "timestamp": 200},
    ]
    episodes = extract_episodes_for_position(
        asset_id="A", avg_entry_price=0.40, activity_rows=activity,
        selected_token_resolution="won",
        resolution_source="redemption_event_paired_single_side_holder",
        redemption_timestamp=300,
    )
    assert len(episodes) == 2
    # First episode: partial_sell profitable, eventual won → profitable_exit_later_won
    assert episodes[0].episode_type == "partial_sell"
    assert episodes[0].counterfactual_class == "profitable_exit_later_won"
    assert episodes[0].size_exited == 40
    # Second episode: held_to_resolution, won
    assert episodes[1].episode_type == "held_to_resolution"
    assert episodes[1].counterfactual_class == "held_won"
    assert episodes[1].size_exited == 60  # 100 - 40


def test_extract_episodes_loss_cut_correct():
    """SELL at loss, eventual outcome lost → loss_exit_later_lost (good cut)."""
    activity = [
        {"asset": "A", "type": "TRADE", "side": "BUY", "size": 100, "price": 0.60, "timestamp": 100},
        {"asset": "A", "type": "TRADE", "side": "SELL", "size": 100, "price": 0.30, "timestamp": 200},
    ]
    episodes = extract_episodes_for_position(
        asset_id="A", avg_entry_price=0.60, activity_rows=activity,
        selected_token_resolution="lost",
        resolution_source="redemption_event_paired_single_side_holder",
        redemption_timestamp=300,
    )
    # 1 SELL episode (full_sell since cum_sold == bought_total) + NO held episode
    # because there are 0 remaining shares.
    assert len(episodes) == 1
    assert episodes[0].counterfactual_class == "loss_exit_later_lost"
    assert episodes[0].episode_type == "full_sell"


def test_extract_episodes_no_avg_price_returns_empty():
    episodes = extract_episodes_for_position(
        asset_id="A", avg_entry_price=None, activity_rows=[],
        selected_token_resolution="won",
        resolution_source="redemption_event_solo",
        redemption_timestamp=100,
    )
    assert episodes == []


# ===========================================================================
# §6.3 — dominant_counterfactual_class rollup
# ===========================================================================


def _mk_episode(klass: str, size: float) -> ExitEpisode:
    return ExitEpisode(
        episode_index=0, episode_type="partial_sell", timestamp=100,
        size_exited=size, price_exit=0.5, price_entry_for_episode=0.4,
        episode_pnl=10.0, trade_result="win",
        selected_token_resolution_at_episode_close="won",
        counterfactual_class=klass,
        counterfactual_source="x", counterfactual_confidence="high",
    )


def test_dominant_class_majority_by_size():
    eps = [
        _mk_episode("held_won", 100),
        _mk_episode("held_lost", 20),
    ]
    dom, share = derive_dominant_counterfactual_class(eps)
    assert dom == "held_won"
    assert share["held_won"] > 0.5


def test_dominant_class_mixed_when_no_majority():
    eps = [
        _mk_episode("held_won", 50),
        _mk_episode("held_lost", 50),
    ]
    dom, _ = derive_dominant_counterfactual_class(eps)
    assert dom == "mixed_episodes"


def test_dominant_class_empty():
    dom, share = derive_dominant_counterfactual_class([])
    assert dom == "unknown"
    assert share == {}


# ===========================================================================
# §7.7 grain key + §8 honesty gates
# ===========================================================================


def test_grain_key_required_dimensions():
    """All 7 grain key dimensions are mandatory."""
    gk = build_grain_key(
        category_source="data_unavailable", category="data_unavailable",
        market_type="binary", outcome_role="binary_yes",
        entry_price_bucket="p_25_50", exit_mode="held",
        position_pattern="single_fill",
    )
    assert set(gk.keys()) == {
        "category_source", "category", "market_type", "outcome_role",
        "entry_price_bucket", "exit_mode", "position_pattern",
    }
    assert grain_id(gk) == grain_id(gk)  # deterministic


def _mk_grain(market_type="binary", outcome_role="binary_yes",
              entry_price_bucket="p_25_50", exit_mode="held",
              position_pattern="single_fill", category="data_unavailable",
              n_episodes=50, attribution_coverage=0.9) -> GrainAggregate:
    g = GrainAggregate(grain_key=build_grain_key(
        category_source="data_unavailable" if category == "data_unavailable" else "market_tags_provisioned",
        category=category, market_type=market_type, outcome_role=outcome_role,
        entry_price_bucket=entry_price_bucket, exit_mode=exit_mode,
        position_pattern=position_pattern,
    ))
    g.grain_id = grain_id(g.grain_key)
    # 20 positions with roughly even PnL — top-5 share well under 70% gate
    g.pnl_per_position = [10.0] * 20
    g.n_episodes = n_episodes
    g.episodes_by_class = {"held_won": int(n_episodes * 0.6), "held_lost": n_episodes - int(n_episodes * 0.6)}
    g.n_attributable = int(n_episodes * attribution_coverage)
    g.total_size = float(n_episodes)
    g.position_ids = [f"p_{i}" for i in range(len(g.pnl_per_position))]
    return g


def test_gate1_underpowered_when_few_episodes():
    g = _mk_grain(n_episodes=5)
    r = evaluate_honesty_gates(g)
    assert r.quality == "underpowered"


def test_gate1_underpowered_precedes_broad_bucket_leakage():
    """§8.2 maps by first failed gate, so Gate 1 beats Gate 7."""
    g = _mk_grain(n_episodes=0, market_type="unknown", exit_mode="unresolved")
    r = evaluate_honesty_gates(g)
    assert r.quality == "underpowered"
    assert any(reason.startswith("gate1_") for reason in r.reasons)
    assert any(reason.startswith("gate7_") for reason in r.reasons)


def test_gate7_not_interpretable_when_market_type_unknown():
    g = _mk_grain(market_type="unknown")
    r = evaluate_honesty_gates(g)
    assert r.quality == "not_interpretable_at_this_grain"


def test_gate7_not_interpretable_when_outcome_role_unknown():
    g = _mk_grain(outcome_role="unknown")
    r = evaluate_honesty_gates(g)
    assert r.quality == "not_interpretable_at_this_grain"


def test_gate7_not_interpretable_when_entry_bucket_data_unavailable():
    g = _mk_grain(entry_price_bucket="data_unavailable")
    r = evaluate_honesty_gates(g)
    assert r.quality == "not_interpretable_at_this_grain"


def test_category_only_data_unavailable_gives_evidence_only():
    """Codex split: category-only data_unavailable → interpretable_evidence_only."""
    g = _mk_grain(category="data_unavailable")  # all other dims clean
    r = evaluate_honesty_gates(g)
    assert r.quality == "interpretable_evidence_only"


@pytest.mark.parametrize("sentinel", sorted(LEAKY_BROAD_VALUES))
def test_non_category_broad_bucket_blocks_thesis_promotion(sentinel: str):
    """§9/Acceptance #4: every broad-bucket sentinel blocks promotion when it
    appears in a non-category grain dimension."""
    g = _mk_grain(category="sports/baseball", position_pattern=sentinel)
    g.grain_key["category_source"] = "market_tags_provisioned"
    g.grain_id = grain_id(g.grain_key)
    quality = evaluate_honesty_gates(g).quality
    assert quality == "not_interpretable_at_this_grain"
    rows = build_mechanism_evidence(g=g, quality=quality)
    assert all(r.candidate_thesis_evidence is None for r in rows)


def test_all_gates_pass_interpretable():
    """Real category + clean dimensions + adequate stats → interpretable."""
    g = _mk_grain(category="sports/baseball")
    g.grain_key["category"] = "sports/baseball"  # real category
    g.grain_key["category_source"] = "market_tags_provisioned"
    g.grain_id = grain_id(g.grain_key)
    r = evaluate_honesty_gates(g)
    assert r.quality == "interpretable"


# ===========================================================================
# §7.8 mechanism evidence rules
# ===========================================================================


def test_mechanism_evidence_prediction_driven_supported():
    """Held grain with WR>55% and pnl>0 → prediction_driven metric_supported."""
    g = GrainAggregate(grain_key=build_grain_key(
        category_source="market_tags_provisioned", category="sports/baseball",
        market_type="multi_outcome", outcome_role="multi_outcome_selected",
        entry_price_bucket="p_50_75", exit_mode="held",
        position_pattern="single_fill",
    ))
    g.grain_id = grain_id(g.grain_key)
    g.n_episodes = 50
    g.episodes_by_class = {"held_won": 35, "held_lost": 15}
    g.n_attributable = 50
    g.pnl_per_position = [100.0] * 10
    g.position_ids = ["p"] * 10
    g.total_size = 50
    quality = evaluate_honesty_gates(g).quality
    assert quality == "interpretable"
    rows = build_mechanism_evidence(g=g, quality=quality)
    pd = next(r for r in rows if r.mechanism == "prediction_driven")
    assert pd.evidence_strength == "metric_supported"
    assert pd.candidate_thesis_evidence is not None  # promoted because quality=interpretable
    assert pd.not_promoted_reason is None


def test_mechanism_evidence_blocks_promotion_when_category_unavailable():
    """Same grain but category=data_unavailable → metric_supported but
    NOT promoted; not_promoted_reason='category_unavailable'."""
    g = GrainAggregate(grain_key=build_grain_key(
        category_source="data_unavailable", category="data_unavailable",
        market_type="multi_outcome", outcome_role="multi_outcome_selected",
        entry_price_bucket="p_50_75", exit_mode="held",
        position_pattern="single_fill",
    ))
    g.grain_id = grain_id(g.grain_key)
    g.n_episodes = 50
    g.episodes_by_class = {"held_won": 35, "held_lost": 15}
    g.n_attributable = 50
    g.pnl_per_position = [100.0] * 10
    g.position_ids = ["p"] * 10
    g.total_size = 50
    quality = evaluate_honesty_gates(g).quality
    assert quality == "interpretable_evidence_only"
    rows = build_mechanism_evidence(g=g, quality=quality)
    pd = next(r for r in rows if r.mechanism == "prediction_driven")
    assert pd.evidence_strength == "metric_supported"
    assert pd.candidate_thesis_evidence is None  # NOT promoted
    assert pd.not_promoted_reason == "category_unavailable"


def test_mechanism_evidence_trading_driven_supported():
    """Pre-resolution grain with trade WR > 55% and profitable_exit_later_lost
    share > 30% → trading_driven metric_supported."""
    g = GrainAggregate(grain_key=build_grain_key(
        category_source="market_tags_provisioned", category="politics",
        market_type="binary", outcome_role="binary_no",
        entry_price_bucket="p_25_50", exit_mode="pre_resolution",
        position_pattern="gradual_accumulation",
    ))
    g.grain_id = grain_id(g.grain_key)
    g.n_episodes = 50
    g.episodes_by_class = {
        "profitable_exit_later_won": 15,
        "profitable_exit_later_lost": 15,  # 50% of profitable exits — proves trade skill
        "loss_exit_later_won": 5,
        "loss_exit_later_lost": 15,
    }
    g.n_attributable = 50
    g.pnl_per_position = [100.0] * 10
    g.position_ids = ["p"] * 10
    g.total_size = 50
    quality = evaluate_honesty_gates(g).quality
    rows = build_mechanism_evidence(g=g, quality=quality)
    td = next(r for r in rows if r.mechanism == "trading_driven")
    assert td.evidence_strength == "metric_supported"


def test_mechanism_evidence_market_selection_always_na_in_v0():
    """v0 cannot compute selection baseline → market_selection_driven = n/a."""
    g = _mk_grain()
    quality = evaluate_honesty_gates(g).quality
    rows = build_mechanism_evidence(g=g, quality=quality)
    ms = next(r for r in rows if r.mechanism == "market_selection_driven")
    assert ms.evidence_strength == "n/a"
    assert ms.not_promoted_reason == "market_selection_driven_deferred"


def test_mechanism_evidence_emits_one_row_per_mechanism():
    """6 rows per grain: prediction, trading, risk_management, market_selection, underpowered, mixed."""
    g = _mk_grain()
    rows = build_mechanism_evidence(g=g, quality="interpretable")
    mechanisms = {r.mechanism for r in rows}
    assert mechanisms == {
        "prediction_driven", "trading_driven", "risk_management_driven",
        "market_selection_driven", "mixed", "underpowered",
    }


# ===========================================================================
# §9 hard rules (data-level enforcement on real fixture)
# ===========================================================================


def test_hard_rule_diagnostic_accounting_never_in_grain_key(
    swissmiss_v4_fixture: Path,
    swissmiss_v4_report: Path,
):
    """§9 + codex fix: accounting_bucket must NEVER appear in any grain_key dimension."""
    tables = build_dossier(
        wallet=SWISSMISS,
        fixture_path=swissmiss_v4_fixture,
        protocol_report_path=swissmiss_v4_report,
    )
    for r in tables.result_semantics:
        gk = json.loads(r["grain_key"])
        for v in gk.values():
            assert v not in {"YES", "NO"}, (
                f"YES/NO leaked into grain_key: {gk}"
            )


def test_hard_rule_no_thesis_promotion_with_broad_bucket(
    swissmiss_v4_fixture: Path,
    swissmiss_v4_report: Path,
):
    """§9: candidate_thesis_evidence must be NULL when grain has broad-bucket
    leakage on non-category dimensions."""
    tables = build_dossier(
        wallet=SWISSMISS,
        fixture_path=swissmiss_v4_fixture,
        protocol_report_path=swissmiss_v4_report,
    )
    by_grain = {r["grain_id"]: json.loads(r["grain_key"]) for r in tables.result_semantics}
    for me in tables.mechanism_evidence:
        if me["candidate_thesis_evidence"] is None:
            continue
        gk = by_grain[me["grain_id"]]
        for dim, val in gk.items():
            if dim == "category":
                continue
            assert val not in LEAKY_BROAD_VALUES, (
                f"promoted thesis on grain with leaky {dim}={val}: {me}"
            )


def test_hard_rule_no_curprice_resolution_source(
    swissmiss_v4_fixture: Path,
    swissmiss_v4_report: Path,
):
    """Acceptance #10: rule 2 still OFF."""
    tables = build_dossier(
        wallet=SWISSMISS,
        fixture_path=swissmiss_v4_fixture,
        protocol_report_path=swissmiss_v4_report,
    )
    bad = [r for r in tables.positions if r["resolution_source"] == "closed_position_curPrice"]
    assert bad == []


def test_hard_rule_protocol_report_not_mutated(
    swissmiss_v4_fixture: Path,
    swissmiss_v4_report: Path,
):
    """§9: dossier never mutates input ProtocolReport."""
    before = swissmiss_v4_report.read_text()
    build_dossier(
        wallet=SWISSMISS,
        fixture_path=swissmiss_v4_fixture,
        protocol_report_path=swissmiss_v4_report,
    )
    after = swissmiss_v4_report.read_text()
    assert before == after


# ===========================================================================
# Real fixture empirical sanity (episodes + grains on SwissMiss)
# ===========================================================================


def test_swissmiss_v4_episode_distribution(
    swissmiss_v4_fixture: Path,
    swissmiss_v4_report: Path,
):
    """SwissMiss v4 should produce a meaningful counterfactual_class distribution.
    The key tip-3 metrics: held_won, profitable_exit_later_won (sold winners early),
    loss_exit_later_lost (good risk cuts), etc."""
    tables = build_dossier(
        wallet=SWISSMISS,
        fixture_path=swissmiss_v4_fixture,
        protocol_report_path=swissmiss_v4_report,
    )
    dist: dict[str, int] = {}
    for ep in tables.position_exit_episodes:
        dist[ep["counterfactual_class"]] = dist.get(ep["counterfactual_class"], 0) + 1
    print(f"\nSwissMiss episode counterfactual_class distribution: {dist}")
    # Tip 3 expectations: should see ALL of the held + profitable_exit +
    # loss_exit families represented in non-trivial counts
    for required in ("held_won", "held_lost", "profitable_exit_later_won",
                      "loss_exit_later_lost"):
        assert dist.get(required, 0) > 0, (
            f"empirical sanity: SwissMiss v4 should produce {required} episodes; "
            f"distribution: {dist}"
        )


def test_swissmiss_v4_no_thesis_promoted(
    swissmiss_v4_fixture: Path,
    swissmiss_v4_report: Path,
):
    """v0 ships category=data_unavailable AND market_type=unknown for most
    SwissMiss positions → every grain has broad-bucket leakage → no thesis
    may be promoted. This is the correct honest behavior."""
    tables = build_dossier(
        wallet=SWISSMISS,
        fixture_path=swissmiss_v4_fixture,
        protocol_report_path=swissmiss_v4_report,
    )
    promoted = [r for r in tables.mechanism_evidence if r["candidate_thesis_evidence"]]
    assert promoted == [], (
        f"v0 must not promote any thesis on SwissMiss data; got {len(promoted)} promoted"
    )


def test_swissmiss_v4_grain_quality_distribution(
    swissmiss_v4_fixture: Path,
    swissmiss_v4_report: Path,
):
    tables = build_dossier(
        wallet=SWISSMISS,
        fixture_path=swissmiss_v4_fixture,
        protocol_report_path=swissmiss_v4_report,
    )
    dist: dict[str, int] = {}
    for r in tables.result_semantics:
        dist[r["grain_quality"]] = dist.get(r["grain_quality"], 0) + 1
    print(f"\nSwissMiss grain_quality distribution: {dist}")
    # All grains must have a valid quality value
    assert set(dist.keys()).issubset({
        "interpretable", "interpretable_evidence_only",
        "diagnostic_only", "not_interpretable_at_this_grain", "underpowered",
    })


# ===========================================================================
# §7.5 selected-token resolution (4 rules, rule 2 OFF by default)
# ===========================================================================


def _mk_position(condition_id: str, asset: str, outcome_index: int, **kw) -> dict:
    base = {
        "conditionId": condition_id,
        "asset": asset,
        "outcomeIndex": outcome_index,
        "outcome": kw.pop("outcome", "Yes"),
        "totalBought": 100.0,
        "avgPrice": 0.5,
        "realizedPnl": 0.0,
        "title": "test market",
    }
    base.update(kw)
    return base


def _mk_redemption(condition_id: str, index_sets: list[str], payout: int, ts: int = 1700000000) -> dict:
    return {
        "id": f"red_{condition_id}_{'_'.join(index_sets)}",
        "timestamp": ts,
        "condition": condition_id,
        "indexSets": index_sets,
        "payout": payout,
    }


# ---- Rule 1: redemption match ----


def test_resolution_rule1_solo_payout_positive_won():
    pos = _mk_position("0xabc", "1001", outcome_index=0)
    red = _mk_redemption("0xabc", ["1"], payout=500_000_000)
    out = derive_selected_token_resolution(
        closed_position=pos,
        redemptions=[red],
        open_positions=[],
        binary_market_n_outcomes=2,
    )
    assert out.value == "won"
    assert out.source == "redemption_event_solo"
    assert out.confidence == "high"


def test_resolution_rule1_solo_payout_zero_lost():
    pos = _mk_position("0xabc", "1001", outcome_index=0)
    red = _mk_redemption("0xabc", ["1"], payout=0)
    out = derive_selected_token_resolution(
        closed_position=pos,
        redemptions=[red],
        open_positions=[],
        binary_market_n_outcomes=2,
    )
    assert out.value == "lost"
    assert out.source == "redemption_event_solo"
    assert out.confidence == "high"


def test_resolution_rule1_paired_single_side_holder_won():
    """Polymarket helper emits paired redemptions even for single-side
    holders. When the wallet held only one side, payout > 0 still means
    the held side won (sibling contributed 0)."""
    pos = _mk_position("0xabc", "1001", outcome_index=0)
    red = _mk_redemption("0xabc", ["1", "2"], payout=500_000_000)
    out = derive_selected_token_resolution(
        closed_position=pos,
        redemptions=[red],
        open_positions=[],
        binary_market_n_outcomes=2,
        wallet_holds_other_outcome_for_condition=False,
    )
    assert out.value == "won"
    assert out.source == "redemption_event_paired_single_side_holder"
    assert out.confidence == "high"


def test_resolution_rule1_paired_single_side_holder_lost():
    pos = _mk_position("0xabc", "1001", outcome_index=0)
    red = _mk_redemption("0xabc", ["1", "2"], payout=0)
    out = derive_selected_token_resolution(
        closed_position=pos,
        redemptions=[red],
        open_positions=[],
        binary_market_n_outcomes=2,
        wallet_holds_other_outcome_for_condition=False,
    )
    assert out.value == "lost"
    assert out.source == "redemption_event_paired_single_side_holder"
    assert out.confidence == "high"


def test_resolution_rule1_paired_both_sides_held_unknown():
    """When the wallet held BOTH sides, the paired payout can't be
    attributed to a specific asset_id from a single redemption record."""
    pos = _mk_position("0xabc", "1001", outcome_index=0)
    red = _mk_redemption("0xabc", ["1", "2"], payout=500_000_000)
    out = derive_selected_token_resolution(
        closed_position=pos,
        redemptions=[red],
        open_positions=[],
        binary_market_n_outcomes=2,
        wallet_holds_other_outcome_for_condition=True,
    )
    assert out.value == "unknown"
    assert out.source == "redemption_event_paired_both_sides_held"
    assert out.confidence == "low"


def test_resolution_rule1_outcome_index_2_uses_indexset_4():
    """Multi-outcome: outcomeIndex 2 should match indexSet '4' (2^2)."""
    pos = _mk_position("0xabc", "3001", outcome_index=2)
    red = _mk_redemption("0xabc", ["4"], payout=1_000_000)
    out = derive_selected_token_resolution(
        closed_position=pos,
        redemptions=[red],
        open_positions=[],
        binary_market_n_outcomes=4,
    )
    assert out.value == "won"
    assert out.source == "redemption_event_solo"


def test_resolution_rule1_latest_redemption_wins():
    """If multiple redemptions match, the most recent timestamp wins."""
    pos = _mk_position("0xabc", "1001", outcome_index=0)
    red_old = _mk_redemption("0xabc", ["1"], payout=0, ts=1_000_000)
    red_new = _mk_redemption("0xabc", ["1"], payout=500_000_000, ts=2_000_000)
    out = derive_selected_token_resolution(
        closed_position=pos,
        redemptions=[red_new, red_old],
        open_positions=[],
        binary_market_n_outcomes=2,
    )
    assert out.value == "won"


# ---- Rule 2: feature flag OFF in v0 ----


def test_resolution_rule2_default_flag_is_off():
    """Acceptance #10 + spec §7.5: rule 2 is OFF by default in v0."""
    assert RESOLVE_BY_CURPRICE_ENABLED is False


def test_resolution_rule2_enabling_flag_raises():
    """Explicit safeguard: turning on rule 2 must fail loudly until §11.5
    verification is wired."""
    pos = _mk_position("0xabc", "1001", outcome_index=0)
    with pytest.raises(NotImplementedError):
        derive_selected_token_resolution(
            closed_position=pos,
            redemptions=[],
            open_positions=[],
            binary_market_n_outcomes=2,
            enable_curprice_rule=True,
        )


# ---- Rule 3: sibling-redemption inference (conservative) ----


def test_resolution_rule3_binary_sibling_solo_payout_we_lose():
    """Binary market; sibling redeemed solo with payout>0; we have no
    redemption for our token → we lost."""
    pos = _mk_position("0xabc", "1001", outcome_index=0, oppositeAsset="1002")
    sibling_red = _mk_redemption("0xabc", ["2"], payout=500_000_000)
    out = derive_selected_token_resolution(
        closed_position=pos,
        redemptions=[sibling_red],
        open_positions=[],
        binary_market_n_outcomes=2,
    )
    assert out.value == "lost"
    assert out.source == "sibling_redemption_inferred_exhaustive"
    assert out.confidence == "medium"


def test_resolution_rule3_does_not_fire_on_multi_outcome():
    """Rule 3 only fires when market is provably binary (n=2)."""
    pos = _mk_position("0xabc", "1001", outcome_index=0, oppositeAsset="1002")
    sibling_red = _mk_redemption("0xabc", ["2"], payout=500_000_000)
    out = derive_selected_token_resolution(
        closed_position=pos,
        redemptions=[sibling_red],
        open_positions=[],
        binary_market_n_outcomes=4,  # NOT binary
    )
    # Falls through to rule 4 — no signal, marked unknown
    assert out.value == "unknown"
    assert out.source == "no_resolution_signal"


def test_resolution_rule3_pre_empted_by_rule1_paired_match():
    """When a paired redemption contains BOTH our index AND sibling's,
    rule 1's paired-single-side-holder path fires before rule 3 ever
    runs. Rule 3 only applies when no rule-1 match exists for our token."""
    pos = _mk_position("0xabc", "1001", outcome_index=0, oppositeAsset="1002")
    paired_red = _mk_redemption("0xabc", ["1", "2"], payout=500_000_000)
    out = derive_selected_token_resolution(
        closed_position=pos,
        redemptions=[paired_red],
        open_positions=[],
        binary_market_n_outcomes=2,
        wallet_holds_other_outcome_for_condition=False,
    )
    # Rule 1 paired_single_side_holder fires first (our index is in [1,2])
    assert out.source == "redemption_event_paired_single_side_holder"
    assert out.value == "won"


def test_resolution_rule3_does_not_fire_when_sibling_payout_zero():
    """Sibling solo redemption with payout=0 means sibling lost too —
    can't infer we lost; rule 3 doesn't fire."""
    pos = _mk_position("0xabc", "1001", outcome_index=0, oppositeAsset="1002")
    sibling_red = _mk_redemption("0xabc", ["2"], payout=0)
    out = derive_selected_token_resolution(
        closed_position=pos,
        redemptions=[sibling_red],
        open_positions=[],
        binary_market_n_outcomes=2,
    )
    assert out.value == "unknown"
    assert out.source == "no_resolution_signal"


def test_resolution_rule3_requires_opposite_asset():
    """Rule 3 needs oppositeAsset to identify the sibling token."""
    pos = _mk_position("0xabc", "1001", outcome_index=0)  # no oppositeAsset
    sibling_red = _mk_redemption("0xabc", ["2"], payout=500_000_000)
    out = derive_selected_token_resolution(
        closed_position=pos,
        redemptions=[sibling_red],
        open_positions=[],
        binary_market_n_outcomes=2,
    )
    assert out.value == "unknown"
    assert out.source == "no_resolution_signal"


# ---- Rule 4: fallback ----


def test_resolution_rule4_still_open_when_same_asset_open():
    """Rule 4 token-wide: same (condition_id, asset_id) in open_positions
    → still_open. Codex 2026-05-27 audit hardened the check to require
    asset_id match, not just condition_id."""
    pos = _mk_position("0xabc", "1001", outcome_index=0)
    out = derive_selected_token_resolution(
        closed_position=pos,
        redemptions=[],
        open_positions=[{"conditionId": "0xabc", "asset": "1001"}],
        binary_market_n_outcomes=2,
    )
    assert out.value == "still_open"
    assert out.source == "open_position_exists"


def test_resolution_rule4_sibling_only_open_is_NOT_still_open():
    """Codex 2026-05-27 audit regression: when the wallet pivoted from
    asset A → asset B (closed A, holding B), our A token must NOT be
    marked still_open just because the condition is shared."""
    pos = _mk_position("0xabc", "1001", outcome_index=0)  # closed asset = 1001
    out = derive_selected_token_resolution(
        closed_position=pos,
        redemptions=[],
        open_positions=[{"conditionId": "0xabc", "asset": "2002"}],  # sibling
        binary_market_n_outcomes=2,
    )
    assert out.value != "still_open"
    assert out.value == "unknown"
    assert out.source == "no_resolution_signal"


def test_resolution_rule4_unknown_when_no_signal():
    pos = _mk_position("0xabc", "1001", outcome_index=0)
    out = derive_selected_token_resolution(
        closed_position=pos,
        redemptions=[],
        open_positions=[],
        binary_market_n_outcomes=2,
    )
    assert out.value == "unknown"
    assert out.source == "no_resolution_signal"
    assert out.confidence == "low"


def test_resolution_no_outcome_index_falls_through():
    """If outcomeIndex is missing, rule 1 cannot fire; falls to rule 4."""
    pos = _mk_position("0xabc", "1001", outcome_index=None)
    out = derive_selected_token_resolution(
        closed_position=pos,
        redemptions=[_mk_redemption("0xabc", ["1"], payout=500_000_000)],
        open_positions=[],
        binary_market_n_outcomes=2,
    )
    assert out.value == "unknown"


# ===========================================================================
# exit_mode derivation
# ===========================================================================


def test_exit_mode_held_when_redemption_matched():
    res = ResolutionResult(
        value="won", source="redemption_event_solo",
        confidence="high", derivation_detail="x",
    )
    assert derive_exit_mode(resolution=res, activity_rows=[], asset_id="1001") == "held"


def test_exit_mode_held_when_paired_single_side_redemption_won():
    """P1 #2 regression (review 2026-05-25): paired-single-side-holder
    redemptions resolve via §7.5 rule 1 (paired branch), but the held-branch
    source whitelist in derive_exit_mode previously omitted this source,
    so 831/860 SwissMiss paired-single-side rows fell through to
    'unresolved'/'unknown'. Held branch must accept all four
    redemption-derived sources."""
    res = ResolutionResult(
        value="won", source="redemption_event_paired_single_side_holder",
        confidence="high", derivation_detail="paired payout>0 single-side holder",
    )
    assert derive_exit_mode(resolution=res, activity_rows=[], asset_id="1001") == "held"


def test_exit_mode_held_when_paired_single_side_redemption_lost():
    """Mirror of the won case: paired-single-side payout=0 → exit_mode='held'."""
    res = ResolutionResult(
        value="lost", source="redemption_event_paired_single_side_holder",
        confidence="high", derivation_detail="paired payout=0 single-side holder",
    )
    assert derive_exit_mode(resolution=res, activity_rows=[], asset_id="1001") == "held"


def test_exit_mode_pre_resolution_when_sell_fills_exist():
    res = ResolutionResult(
        value="unknown", source="no_resolution_signal",
        confidence="low", derivation_detail="x",
    )
    sells = [
        {"type": "TRADE", "side": "SELL", "asset": "1001", "timestamp": 1700000000},
    ]
    assert derive_exit_mode(resolution=res, activity_rows=sells, asset_id="1001") == "pre_resolution"


def test_exit_mode_still_open():
    res = ResolutionResult(
        value="still_open", source="open_position_exists",
        confidence="medium", derivation_detail="x",
    )
    assert derive_exit_mode(resolution=res, activity_rows=[], asset_id="1001") == "still_open"


def test_exit_mode_unresolved_when_no_signal_no_sells():
    res = ResolutionResult(
        value="unknown", source="no_resolution_signal",
        confidence="low", derivation_detail="x",
    )
    assert derive_exit_mode(resolution=res, activity_rows=[], asset_id="1001") == "unresolved"


# ===========================================================================
# entry/exit timestamps
# ===========================================================================


def test_entry_exit_ts_basic():
    activity = [
        {"asset": "1001", "type": "TRADE", "side": "BUY", "timestamp": 100},
        {"asset": "1001", "type": "TRADE", "side": "BUY", "timestamp": 200},
        {"asset": "1001", "type": "TRADE", "side": "SELL", "timestamp": 300},
        {"asset": "1001", "type": "TRADE", "side": "SELL", "timestamp": 400},
        {"asset": "9999", "type": "TRADE", "side": "BUY", "timestamp": 50},   # other asset, ignored
    ]
    e, x = derive_entry_exit_ts(activity_rows=activity, asset_id="1001", condition_id="0xabc")
    assert e == 100
    assert x == 400


def test_entry_exit_ts_no_activity():
    e, x = derive_entry_exit_ts(activity_rows=[], asset_id="1001", condition_id="0xabc")
    assert e is None
    assert x is None


# ===========================================================================
# §11.5 — empirical verification on real v4 calibration fixture
# ===========================================================================


def test_v11_5_no_row_uses_curprice_resolution_source(
    swissmiss_v4_fixture: Path,
    swissmiss_v4_report: Path,
):
    """Acceptance #10: while RESOLVE_BY_CURPRICE_ENABLED=False, no row
    in any v0 dossier may carry resolution_source == 'closed_position_curPrice'."""
    tables = build_dossier(
        wallet=SWISSMISS,
        fixture_path=swissmiss_v4_fixture,
        protocol_report_path=swissmiss_v4_report,
    )
    bad = [r for r in tables.positions if r["resolution_source"] == "closed_position_curPrice"]
    assert bad == [], (
        f"Found {len(bad)} rows with curPrice resolution while flag is OFF — "
        "violates spec §7.5 + Acceptance #10"
    )


def test_v11_5_redemption_matched_rows_have_consistent_source(
    swissmiss_v4_fixture: Path,
    swissmiss_v4_report: Path,
):
    """For every position whose conditionId matches a redemption and whose
    indexSet appears in the redemption.indexSets, the dossier's
    resolution_source must be from the documented vocab AND the value
    must follow the payout sign — but ONLY when the wallet held a single
    side of the condition (paired redemptions on dual holds are unknown).

    Empirical note from SwissMiss v4: 100% of her redemptions use
    indexSets=['1','2'] (paired), so the test exercises the
    paired_single_side_holder path predominantly.
    """
    fixture = json.loads(swissmiss_v4_fixture.read_text())
    redemptions = fixture.get("redemptions") or []
    closed_positions = fixture.get("closed_positions") or []

    # Build (cid, indexSet) → (payout, indexSets_tuple) map taking the latest
    # by timestamp for each (cid, indexSet) key.
    latest_by_key: dict[tuple[str, str], dict] = {}
    for r in redemptions:
        cid = (r.get("condition") or "").lower()
        sets = list(r.get("indexSets") or [])
        ts = int(r.get("timestamp") or 0)
        for s in sets:
            key = (cid, s)
            existing = latest_by_key.get(key)
            if existing is None or ts >= int(existing.get("timestamp") or 0):
                latest_by_key[key] = r

    # Count how many distinct asset_ids the wallet holds per condition (for
    # the paired-single-side disambiguation).
    asset_count_by_cid: dict[str, int] = {}
    for cp in closed_positions:
        cid = (cp.get("conditionId") or "").lower()
        if cid:
            asset_count_by_cid[cid] = asset_count_by_cid.get(cid, 0) + 1

    tables = build_dossier(
        wallet=SWISSMISS,
        fixture_path=swissmiss_v4_fixture,
        protocol_report_path=swissmiss_v4_report,
    )

    VALID_REDEMPTION_SOURCES = {
        "redemption_event_solo",
        "redemption_event_paired_single_side_holder",
        "redemption_event_paired_both_sides_held",
    }
    n_checked = 0
    n_definitive = 0
    for pos in tables.positions:
        oi = pos["outcome_index"]
        if oi is None:
            continue
        index_set = str(1 << int(oi))
        key = (pos["condition_id"], index_set)
        if key not in latest_by_key:
            continue
        n_checked += 1
        r = latest_by_key[key]
        payout = int(r.get("payout") or 0)
        sets = list(r.get("indexSets") or [])
        assert pos["resolution_source"] in VALID_REDEMPTION_SOURCES, (
            f"position {pos['position_id']} unexpected source: {pos['resolution_source']!r}"
        )
        # When the dossier flagged "definitive", verify the value matches payout
        if pos["resolution_source"] in {
            "redemption_event_solo",
            "redemption_event_paired_single_side_holder",
        }:
            n_definitive += 1
            expected = "won" if payout > 0 else "lost"
            assert pos["selected_token_resolution"] == expected, (
                f"position {pos['position_id']} expected {expected} "
                f"(payout={payout}, indexSets={sets}); got {pos['selected_token_resolution']!r}"
            )

    assert n_checked > 0, "no redemption matches found — fixture may have changed"
    # Most SwissMiss redemptions are paired single-side → definitive
    assert n_definitive > 0, (
        "expected at least some definitive resolutions; got 0 — "
        "paired-single-side-holder rule may be broken"
    )


def test_v11_5_swissmiss_resolution_source_distribution(
    swissmiss_v4_fixture: Path,
    swissmiss_v4_report: Path,
):
    """Empirical sanity: SwissMiss holds to resolution heavily, so we
    expect the bulk of her resolution sources to be redemption-matched.
    This is a sanity check, not a strict assertion — if the distribution
    looks wildly off, that's a signal something changed."""
    tables = build_dossier(
        wallet=SWISSMISS,
        fixture_path=swissmiss_v4_fixture,
        protocol_report_path=swissmiss_v4_report,
    )
    dist: dict[str, int] = {}
    for r in tables.positions:
        dist[r["resolution_source"]] = dist.get(r["resolution_source"], 0) + 1
    # Print for visibility when running -v
    print(f"\nSwissMiss resolution_source distribution: {dist}")
    # At least one redemption-matched row
    redemption_matched = sum(
        n for k, n in dist.items()
        if k in {
            "redemption_event_solo",
            "redemption_event_paired_single_side_holder",
            "redemption_event_paired_both_sides_held",
            "sibling_redemption_inferred_exhaustive",
        }
    )
    assert redemption_matched > 0
    # Empirical: SwissMiss is heavily held-to-resolution; most resolutions
    # should be the high-confidence paired_single_side_holder path.
    paired_single = dist.get("redemption_event_paired_single_side_holder", 0)
    assert paired_single >= 500, (
        f"expected paired_single_side_holder >= 500 for SwissMiss; got {paired_single}"
    )
    # Total must equal n_positions
    assert sum(dist.values()) == len(tables.positions)


def test_v11_5_swissmiss_exit_mode_distribution(
    swissmiss_v4_fixture: Path,
    swissmiss_v4_report: Path,
):
    tables = build_dossier(
        wallet=SWISSMISS,
        fixture_path=swissmiss_v4_fixture,
        protocol_report_path=swissmiss_v4_report,
    )
    dist: dict[str, int] = {}
    for r in tables.positions:
        dist[r["exit_mode"]] = dist.get(r["exit_mode"], 0) + 1
    print(f"\nSwissMiss exit_mode distribution: {dist}")
    # Every exit_mode value must be from the documented vocab
    assert set(dist.keys()).issubset({
        "held", "pre_resolution", "still_open", "unresolved", "unknown"
    })


# ---------------------------------------------------------------------------
# CLI default path includes fetched_at (codex review fix #3)
# ---------------------------------------------------------------------------


def test_cli_default_out_db_path_includes_fetched_at(tmp_path: Path):
    """CLI help says default is dossiers/v4/<wallet>_<fetched_at>.sqlite.
    Implementation must match — otherwise repeat captures overwrite."""
    import importlib.util

    script_path = REPO_ROOT / "scripts" / "build_wallet_dossier.py"
    spec = importlib.util.spec_from_file_location("build_wallet_dossier", script_path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]

    fix = tmp_path / "fixture.json"
    fix.write_text(json.dumps({"wallet": "0xabc", "fetched_at": 1779728198}))
    out = mod._default_out_db("0xABC", fix)
    assert str(out) == "dossiers/v4/0xabc_1779728198.sqlite"


def test_cli_default_out_db_path_no_fetched_at_falls_back(tmp_path: Path):
    fix = tmp_path / "fixture.json"
    fix.write_text(json.dumps({"wallet": "0xdef"}))
    import importlib.util

    script_path = REPO_ROOT / "scripts" / "build_wallet_dossier.py"
    spec = importlib.util.spec_from_file_location("build_wallet_dossier", script_path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]

    out = mod._default_out_db("0xdef", fix)
    assert str(out) == "dossiers/v4/0xdef_no_fetched_at.sqlite"
