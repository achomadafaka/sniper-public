"""Phase 1B Deliverable #8 — feature registry + availability accounting.

The registry catalogs every feature the snapshot produces with family / required_sources / time_scope /
decision_safe, and must not drift from the real features. Availability accounting reports per-feature and
per-checkpoint coverage + null distribution over captured snapshots.
"""
import os

from sniper.algos.polymarket_crypto5m import feature_registry as FR
from sniper.algos.polymarket_crypto5m import persistence as P
from sniper.algos.polymarket_crypto5m import sim_persistence as SP


def test_registry_covers_every_snapshot_feature():
    names = set(FR.canonical_feature_names())
    registered = {e["feature_name"] for e in FR.build_registry()}
    assert names == registered                              # no feature missing, none invented


def test_classification_examples():
    by = {e["feature_name"]: e for e in FR.build_registry()}
    legacy = by["legacy_spot_signal_diagnostic"]
    assert legacy["time_scope"] == "diagnostic_only" and legacy["decision_safe"] is False
    assert "binance_volume" in by["binance_volume_30s"]["required_sources"]
    assert "no_volume_source" in by["binance_volume_30s"]["null_reason_vocab"]
    assert set(by["basis_change_30s"]["required_sources"]) == {"binance_price", "chainlink_rtds"}
    assert "stale_or_misaligned" in by["basis_change_30s"]["null_reason_vocab"]
    assert "fee" in by["up_fee_inclusive_cost_10_usdc"]["required_sources"]
    assert "fee_unresolved" in by["up_fee_inclusive_cost_10_usdc"]["null_reason_vocab"]
    assert by["chainlink_return_15s"]["required_sources"] == ["chainlink_rtds"]
    assert by["chainlink_return_15s"]["minimum_history"] == "15s"
    # every non-diagnostic feature is known-at-decision (no post-resolution leakage into predictors)
    assert all(e["time_scope"] in ("known_at_decision", "diagnostic_only") for e in by.values())


def test_source_age_fields_classified_as_basis():
    by = {e["feature_name"]: e for e in FR.build_registry()}
    for n in ("binance_source_age_ms", "chainlink_source_age_ms", "basis_observation_skew_ms"):
        assert by[n]["feature_family"] == "D_basis"
        assert set(by[n]["required_sources"]) == {"binance_price", "chainlink_rtds"}


def test_no_feature_falls_to_other_or_empty_sources():
    # non-tautological guard: a NEW feature with no matching _classify branch would land in family
    # "other" with empty required_sources — catch it by intent rather than by the (self-derived) cover set.
    for e in FR.build_registry():
        assert e["feature_family"] != "other", f"unclassified feature: {e['feature_name']}"
        assert e["required_sources"], f"feature has no required_sources: {e['feature_name']}"


def _conn(tmp_path):
    conn = P.connect(os.path.join(str(tmp_path), "fr.sqlite"))
    P.ensure_schema(conn)
    SP.ensure_sim_schema(conn)
    return conn


def test_seed_and_read_registry_roundtrip(tmp_path):
    conn = _conn(tmp_path)
    reg = FR.build_registry()
    SP.seed_feature_registry(conn, reg)
    SP.seed_feature_registry(conn, reg)                     # idempotent (replace)
    rows = SP.read_feature_registry(conn)
    assert len(rows) == len(reg)
    one = next(r for r in rows if r["feature_name"] == "binance_volume_30s")
    assert one["feature_family"] == "C_binance"
    assert "binance_volume" in one["required_sources"]      # JSON decoded back to a list


def test_feature_availability_coverage(tmp_path):
    conn = _conn(tmp_path)
    SP.upsert_feature_snapshot(conn, dict(condition_id="a", checkpoint=60, feature_version="fv0",
                               features={"binance_return_15s": 0.1, "binance_volume_30s": None}), now_ts=1)
    SP.upsert_feature_snapshot(conn, dict(condition_id="b", checkpoint=60, feature_version="fv0",
                               features={"binance_return_15s": None, "binance_volume_30s": None}), now_ts=2)
    rep = FR.feature_availability(conn)
    assert rep["snapshots"] == 2
    assert rep["coverage_by_feature"]["binance_return_15s"] == 0.5     # 1 of 2 non-null
    assert rep["coverage_by_feature"]["binance_volume_30s"] == 0.0     # never populated (price-only)
    assert rep["coverage_by_checkpoint"][60]["snapshots"] == 2
