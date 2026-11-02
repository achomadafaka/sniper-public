"""Tests for sniper.analysis.wallet_parameter_extractor."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from sniper.analysis.wallet_parameter_extractor import (  # noqa: E402
    EXTRACTOR_VERSION,
    _cadence_fingerprint,
    _exit_fingerprint,
    _fragmentation_fingerprint,
    _price_range_from_bucket,
    _sizing_fingerprint,
    extract_wallet_rule,
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_price_range_from_bucket_known():
    assert _price_range_from_bucket("p_50_75") == [0.50, 0.75]
    assert _price_range_from_bucket("p_gte_75") == [0.75, 1.00]
    assert _price_range_from_bucket("p_lt_05") == [0.00, 0.05]


def test_price_range_from_bucket_unknown_returns_none():
    assert _price_range_from_bucket(None) is None
    assert _price_range_from_bucket("garbage") is None


def test_sizing_fingerprint_buckets():
    assert _sizing_fingerprint(None) == "unknown"
    assert _sizing_fingerprint(0.05) == "uniform"
    assert _sizing_fingerprint(0.3) == "consistent"
    assert _sizing_fingerprint(0.7) == "variable"
    assert _sizing_fingerprint(1.5) == "highly_variable"


def test_cadence_fingerprint_buckets():
    assert _cadence_fingerprint(None, 100) == "unknown"
    assert _cadence_fingerprint(0.5, 100) == "bot_like_bursts"
    assert _cadence_fingerprint(0.05, 30) == "fast_reactive"
    assert _cadence_fingerprint(0.05, 100_000) == "slow_deliberate"
    assert _cadence_fingerprint(0.05, 3600) == "deliberate"


def test_exit_fingerprint():
    assert _exit_fingerprint("held") == "hold_to_resolution"
    assert _exit_fingerprint("pre_resolution") == "sell_pre_resolution"
    assert _exit_fingerprint("still_open") == "still_holding"
    assert _exit_fingerprint("garbage") == "unknown"


def test_fragmentation_fingerprint():
    assert _fragmentation_fingerprint(None, 0.5) == "unknown"
    assert _fragmentation_fingerprint(1, 0.9) == "one_shot"
    assert _fragmentation_fingerprint(2, 0.5) == "lightly_scaled"
    assert _fragmentation_fingerprint(8, 0.1) == "heavily_scaled"


# ---------------------------------------------------------------------------
# Full extract_wallet_rule()
# ---------------------------------------------------------------------------


def _make_view(
    *,
    wallet: str = "0xtest",
    grain_key: dict | None = None,
    grain_quality: str = "interpretable",
    n_positions: int = 100,
    n_episodes: int = 180,
    has_thesis: bool = True,
    sizing_cv: float = 0.7,
    sizing_median: float = 250.0,
    hour_rows: list[dict] | None = None,
    cadence_burst: float = 0.05,
    cadence_median_s: int = 3600,
    exit_mode: str = "held",
    exit_median_s: int = 86400 * 30,
    fills_median: int = 1,
    fills_single_share: float = 0.65,
) -> dict:
    grain_key = grain_key or {
        "category": "sports",
        "category_source": "tag_nav_category",
        "market_type": "binary",
        "outcome_role": "binary_named_outcome",
        "entry_price_bucket": "p_50_75",
        "exit_mode": "held",
        "position_pattern": "fragmented_or_ambiguous",
    }
    return {
        "dossier_meta": [
            {"key": "wallet", "value": wallet},
            {"key": "schema_version", "value": "v5_behavioral_descriptors"},
        ],
        "result_semantics": [
            {
                "grain_id": "g1",
                "grain_key": json.dumps(grain_key, sort_keys=True),
                "n_positions": n_positions,
                "n_episodes": n_episodes,
                "grain_quality": grain_quality,
            }
        ],
        "mechanism_evidence": [
            {
                "grain_id": "g1",
                "mechanism": "prediction_driven",
                "evidence_strength": "metric_supported",
            }
        ],
        "thesis_candidates": [
            {"thesis_id": "t1", "grain_id": "g1", "mechanism": "prediction_driven"}
        ] if has_thesis else [],
        "data_gaps": [
            {"gap_name": "market_selection_universe_baseline_missing", "severity": "high"},
        ],
        "descriptor_hour_distribution": hour_rows or [
            {"hour_utc": 14, "side": "BUY", "market_category": "sports", "n_trades": 30, "total_size_usdc": 6000.0},
            {"hour_utc": 15, "side": "BUY", "market_category": "sports", "n_trades": 50, "total_size_usdc": 12000.0},
            {"hour_utc": 16, "side": "BUY", "market_category": "sports", "n_trades": 25, "total_size_usdc": 5000.0},
            {"hour_utc":  3, "side": "BUY", "market_category": "sports", "n_trades":  5, "total_size_usdc":  500.0},
        ],
        "descriptor_cadence": [
            {
                "side": "BUY", "market_category": "sports",
                "n_intervals": 100,
                "median_seconds_between_trades": cadence_median_s,
                "p10_seconds_between_trades": 60,
                "p90_seconds_between_trades": 86400,
                "burst_share": cadence_burst,
            }
        ],
        "descriptor_sizing": [
            {
                "side": "BUY", "market_category": "sports",
                "n_fills": 180,
                "median_size_usdc": sizing_median,
                "p10_size_usdc": 50.0,
                "p90_size_usdc": 1500.0,
                "max_size_usdc": 5000.0,
                "coeff_of_variation": sizing_cv,
            }
        ],
        "descriptor_time_in_position": [
            {
                "exit_mode": exit_mode,
                "n_positions": 180,
                "median_seconds_in_position": exit_median_s,
                "p10_seconds_in_position": 86400,
                "p90_seconds_in_position": 86400 * 90,
            }
        ],
        "descriptor_fills_per_position": [
            {
                "market_category": "sports",
                "n_positions": 180,
                "median_fills": fills_median,
                "p10_fills": 1,
                "p90_fills": 5,
                "single_fill_share": fills_single_share,
            }
        ],
    }


def test_extract_returns_v1_envelope():
    view = _make_view()
    rule = extract_wallet_rule(view)
    assert rule["extractor_version"] == EXTRACTOR_VERSION
    assert rule["wallet"] == "0xtest"
    assert rule["source_schema_version"] == "v5_behavioral_descriptors"
    assert rule["source_grain"]["grain_id"] == "g1"


def test_extract_market_filter_mirrors_grain_key():
    view = _make_view()
    mf = extract_wallet_rule(view)["market_filter"]
    assert mf["categories"] == ["sports"]
    assert mf["market_types"] == ["binary"]
    assert mf["outcome_roles"] == ["binary_named_outcome"]
    assert mf["entry_price_bucket"] == "p_50_75"
    assert mf["entry_price_range"] == [0.50, 0.75]


def test_extract_sizing_populates_from_descriptor():
    view = _make_view(sizing_cv=0.05, sizing_median=300.0)
    s = extract_wallet_rule(view)["sizing"]
    assert s["median_usdc"] == 300.0
    assert s["fingerprint"] == "uniform"  # cv < 0.1 → algorithmic-looking
    assert s["n_fills_sourced"] == 180


def test_extract_timing_picks_top_hours():
    view = _make_view()
    t = extract_wallet_rule(view)["timing"]
    assert set(t["preferred_hours_utc"]) >= {14, 15, 16}
    assert t["cadence_fingerprint"] == "deliberate"


def test_extract_timing_flags_bot_when_burst_high():
    view = _make_view(cadence_burst=0.5)
    t = extract_wallet_rule(view)["timing"]
    assert t["cadence_fingerprint"] == "bot_like_bursts"


def test_extract_exit_rule_picks_expected_mode():
    view = _make_view(exit_mode="held")
    e = extract_wallet_rule(view)["exit_rule"]
    assert e["mode"] == "held"
    assert e["fingerprint"] == "hold_to_resolution"


def test_extract_fragmentation_one_shot():
    view = _make_view(fills_median=1, fills_single_share=0.7)
    f = extract_wallet_rule(view)["fragmentation"]
    assert f["fingerprint"] == "one_shot"


def test_extract_evidence_thesis_supported():
    view = _make_view(has_thesis=True)
    ev = extract_wallet_rule(view)["evidence"]
    assert ev["thesis_supported"] is True
    assert ev["n_thesis_candidates_for_grain"] == 1
    assert "market_selection_universe_baseline_missing" in ev["data_gaps_high_severity"]


def test_extract_no_interpretable_grain_returns_empty_rule():
    view = _make_view(grain_quality="underpowered", has_thesis=False)
    rule = extract_wallet_rule(view)
    assert rule["source_grain"] is None
    assert rule["market_filter"] is None
    assert rule["rejection_reason"] == "no_interpretable_grain"


def test_extract_accepts_interpretable_evidence_only():
    """Regression (2026-05-29): the extractor must accept
    `interpretable_evidence_only` grains — the dossier's own promotion gates
    (wallet_dossier/grain.py) treat them as interpretable/promotable. The
    extractor previously matched only the exact string "interpretable"."""
    view = _make_view(grain_quality="interpretable_evidence_only", has_thesis=False)
    rule = extract_wallet_rule(view)
    assert rule.get("rejection_reason") is None
    assert rule["source_grain"] is not None
    assert rule["source_grain"]["grain_quality"] == "interpretable_evidence_only"


def test_market_filter_drops_data_unavailable_dimensions():
    """An evidence-only grain has category=data_unavailable; the rule must not
    filter on the unknown dimension (empty list = no constraint) or it matches
    zero positions in the backtest."""
    from sniper.analysis.wallet_parameter_extractor import _build_market_filter
    mf = _build_market_filter({
        "category": "data_unavailable", "market_type": "binary",
        "outcome_role": "data_unavailable", "entry_price_bucket": "p_50_75",
    })
    assert mf["categories"] == []          # unknown → no constraint
    assert mf["market_types"] == ["binary"]
    assert mf["outcome_roles"] == []
    assert mf["entry_price_range"] == [0.50, 0.75]


def test_extract_prefers_fully_interpretable_over_evidence_only():
    """When both qualities are present, a true `interpretable` grain wins even
    if an `interpretable_evidence_only` grain has more episodes."""
    view = _make_view(grain_quality="interpretable", n_episodes=50, has_thesis=False)
    view["result_semantics"].append({
        "grain_id": "g_evid",
        "grain_key": json.dumps({"category": "data_unavailable", "market_type": "binary"}, sort_keys=True),
        "n_positions": 200,
        "n_episodes": 500,   # more episodes, but evidence-only
        "grain_quality": "interpretable_evidence_only",
    })
    rule = extract_wallet_rule(view)
    assert rule["source_grain"]["grain_quality"] == "interpretable"
    assert rule["source_grain"]["grain_id"] == "g1"


def test_extract_target_grain_id_overrides_selection():
    view = _make_view()
    view["result_semantics"].append({
        "grain_id": "g2",
        "grain_key": json.dumps({"category": "elections", "market_type": "binary"}, sort_keys=True),
        "n_positions": 5,
        "n_episodes": 5,
        "grain_quality": "interpretable",
    })
    rule = extract_wallet_rule(view, target_grain_id="g2")
    assert rule["source_grain"]["grain_id"] == "g2"
    assert rule["market_filter"]["categories"] == ["elections"]


def test_extract_target_grain_not_present_returns_empty():
    view = _make_view()
    rule = extract_wallet_rule(view, target_grain_id="nonexistent")
    assert rule["source_grain"] is None


def test_extract_accepts_dataclass_view():
    """The extractor should accept a DossierTables-like attribute-bearing object."""
    view_dict = _make_view()
    ns = SimpleNamespace(**view_dict)
    rule = extract_wallet_rule(ns)
    assert rule["source_grain"]["grain_id"] == "g1"


def test_extract_chooses_grain_with_thesis_over_interpretable_only():
    view = _make_view()
    view["result_semantics"].append({
        "grain_id": "g_big",
        "grain_key": json.dumps({"category": "finance"}, sort_keys=True),
        "n_positions": 5000,
        "n_episodes": 5000,
        "grain_quality": "interpretable",
    })
    rule = extract_wallet_rule(view)
    assert rule["source_grain"]["grain_id"] == "g1"


# ---------------------------------------------------------------------------
# CLI smoke
# ---------------------------------------------------------------------------


def test_cli_from_export_smoke(tmp_path: Path):
    from scripts.extract_wallet_rule import main as cli_main

    export_dir = tmp_path / "export"
    export_dir.mkdir()
    view = _make_view()
    for name, rows in view.items():
        (export_dir / f"{name}.json").write_text(json.dumps(rows))

    out = tmp_path / "rule.json"
    rc = cli_main(["--from-export", str(export_dir), "--out", str(out)])
    assert rc == 0
    rule = json.loads(out.read_text(encoding="utf-8"))
    assert rule["wallet"] == "0xtest"
    assert rule["market_filter"]["entry_price_range"] == [0.50, 0.75]


def test_cli_rejects_missing_export(tmp_path: Path):
    from scripts.extract_wallet_rule import main as cli_main

    rc = cli_main([
        "--from-export", str(tmp_path / "missing"),
        "--out", str(tmp_path / "rule.json"),
    ])
    assert rc == 2


def test_cli_wallet_path_requires_fixture_and_report(tmp_path: Path):
    from scripts.extract_wallet_rule import main as cli_main

    rc = cli_main([
        "--wallet", "0xtest",
        "--out", str(tmp_path / "rule.json"),
    ])
    assert rc == 2
