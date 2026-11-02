from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from sniper.analysis.winning_wallet_thesis.target_position_anatomy import (  # noqa: E402
    TargetAnatomyConfig,
    build_target_position_anatomy,
)
from sniper.strategies.source_position_copy_v1 import (  # noqa: E402
    SWISSMISS_WALLET,
    SourceObservation,
)


def _obs(
    activity_key,
    *,
    price,
    usdc_size,
    size=None,
    ts=100,
    tags=None,
    side="BUY",
    condition_id="cond-1",
    won=True,
):
    return SourceObservation(
        activity_key=activity_key,
        wallet=SWISSMISS_WALLET,
        condition_id=condition_id,
        asset_id="asset-yes",
        outcome="YES",
        side=side,
        price=price,
        size=size if size is not None else usdc_size / price,
        usdc_size=usdc_size,
        observed_ts=ts,
        title="UFC fight",
        event_slug="ufc-fight-night-1",
        event_id="event-1",
        tags=tags or ["sports", "ufc"],
        source_endpoint="fixture",
        raw_json={"won": won},
    )


def test_anatomy_filters_target_universe_and_counts_exclusions():
    observations = [
        _obs("sports", price=0.50, usdc_size=25000, tags=["sports", "ufc"]),
        _obs("iran", price=0.50, usdc_size=25000, tags=["geopolitics", "iran"], condition_id="cond-2"),
        _obs("low", price=0.20, usdc_size=25000, tags=["sports"], condition_id="cond-3"),
    ]

    report = build_target_position_anatomy(observations, config=TargetAnatomyConfig())

    assert report["target_universe"]["included_positions"] == 1
    assert report["target_universe"]["excluded_by_reason"]["rejected_tag:iran"] == 1
    assert report["target_universe"]["excluded_by_reason"]["entry_price_below_min:0.2"] == 1


def test_anatomy_threshold_grid_reports_crossing_frequency_and_roi():
    observations = [
        _obs("a", price=0.50, usdc_size=10000, ts=100),
        _obs("b", price=0.60, usdc_size=15000, ts=200),
        _obs("c", price=0.70, usdc_size=10000, ts=300),
    ]

    report = build_target_position_anatomy(observations, config=TargetAnatomyConfig())
    row = report["threshold_grid"]["25000|6h"]

    assert row["crossed_positions"] == 1
    assert row["wins"] == 1
    assert row["roi_pct"] > 0
    assert row["median_time_to_cross_seconds"] == 100


def test_anatomy_uses_default_trigger_when_supported():
    observations = []
    for idx in range(20):
        observations.extend(
            [
                _obs(f"{idx}-a", price=0.50, usdc_size=10000, ts=idx * 1000 + 100, condition_id=f"cond-{idx}"),
                _obs(f"{idx}-b", price=0.60, usdc_size=15000, ts=idx * 1000 + 200, condition_id=f"cond-{idx}"),
            ]
        )

    report = build_target_position_anatomy(observations, config=TargetAnatomyConfig(min_crossings_for_default=20))

    assert report["recommended_trigger"]["source_commitment_threshold_usdc"] == 25000
    assert report["recommended_trigger"]["source_commitment_window_hours"] == 6
    assert report["recommended_trigger"]["selection_reason"] == "default_supported"


def test_anatomy_fallback_selects_lowest_feasible_threshold_not_best_roi():
    observations = [
        _obs("a1", price=0.50, usdc_size=5000, ts=100, condition_id="cond-a", won=False),
        _obs("b1", price=0.50, usdc_size=5000, ts=100, condition_id="cond-b", won=False),
        _obs("c1", price=0.50, usdc_size=5000, ts=100, condition_id="cond-c", won=False),
        _obs("d1", price=0.50, usdc_size=25000, ts=100, condition_id="cond-d", won=True),
    ]

    report = build_target_position_anatomy(
        observations,
        config=TargetAnatomyConfig(min_crossings_for_default=3, min_crossings_for_fallback=3),
    )

    assert report["recommended_trigger"]["source_commitment_threshold_usdc"] == 5000
    assert report["recommended_trigger"]["selection_reason"] == "fallback_lowest_feasible_threshold"
