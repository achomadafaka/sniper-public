# tests/test_archetype_offline_parity.py
"""Parity check: extract_wallet_archetype (live) and
classify_wallet_archetype_from_snapshot (offline) must produce the same
features dict when given the same activity + positions inputs.

This locks the 'thin wrapper' claim from drifting. If the live entry
point computes anything that the offline classifier does not, this test
fires.

Spec: docs/superpowers/specs/2026-05-01-fixture-v2-schema.md, Archetype capture.
"""
from __future__ import annotations

import unittest.mock as mock

import pytest

from scripts.wallet_archetype_extractor import (
    classify_wallet_archetype_from_snapshot,
    extract_wallet_archetype,
)


@pytest.fixture
def synthetic_activity_and_positions():
    """Synthetic but realistic fixture covering at least: TRADE BUY/SELL,
    REDEEM, MERGE, SPLIT — enough surface for the extractor to compute
    classification features."""
    activity = []
    base_ts = 1_700_000_000
    for i in range(40):
        activity.append({
            "type": "TRADE",
            "side": "BUY",
            "conditionId": f"0xcid{i % 5}",
            "asset": f"asset{i % 5}_yes",
            "timestamp": base_ts + i * 600,
            "price": 0.45,
            "size": 100,
            "outcome": "Yes",
            "usdc_size": 45.0,
        })
    for i in range(15):
        activity.append({
            "type": "TRADE",
            "side": "SELL",
            "conditionId": f"0xcid{i % 5}",
            "asset": f"asset{i % 5}_yes",
            "timestamp": base_ts + 50 * 600 + i * 600,
            "price": 0.52,
            "size": 50,
            "outcome": "Yes",
            "usdc_size": 26.0,
        })
    for i in range(8):
        activity.append({
            "type": "REDEEM",
            "conditionId": f"0xcid{i}",
            "timestamp": base_ts + 100 * 600 + i * 3600,
            "size": 50,
            "outcome": "Yes",
        })

    positions = [
        {
            "conditionId": f"0xcid{i}",
            "outcome": "Yes",
            "asset": f"asset{i}_yes",
            "avgPrice": 0.45 + 0.01 * i,
            "size": 50,
            "currentValue": 50 * (0.50 + 0.01 * i),
            "redeemable": False,
        }
        for i in range(5)
    ]
    return activity, positions


SYNTHETIC_TENURE = {
    "wallet_first_activity_ts": 1_700_000_000,
    "wallet_last_activity_ts": 1_700_000_000 + 100 * 600,
    "wallet_lifetime_days": 0.69,
    "pagination_complete": True,
    "pages_fetched": 1,
    "profile_joined_ts": 1_690_000_000,
    "profile_lifetime_days": 365.0,
    "profile_tenure_source": "test",
    "wallet_lifetime_source": "profile_joined",
}


def test_offline_classifier_matches_live_for_same_inputs(
    synthetic_activity_and_positions,
):
    activity, positions = synthetic_activity_and_positions
    # Empty tag_map for both paths so neither touches SQLite. The parity
    # test only proves "same inputs -> same outputs"; the tag_map AND
    # tenure_metadata are inputs and must be identical on both sides.
    tag_map: dict[str, frozenset[str]] = {}
    tenure = dict(SYNTHETIC_TENURE)

    # Run the offline classifier directly on the synthetic data with an
    # explicit empty tag_map and explicit tenure_metadata.
    offline_features = classify_wallet_archetype_from_snapshot(
        activity, positions, tag_map=tag_map, tenure_metadata=tenure,
        wallet="0xabc", skip_gate0=True, with_execution=False,
    )

    # Run the live extractor with monkeypatched fetchers AND stubbed
    # _fetch_tag_map and _fetch_wallet_tenure_metadata so it sees the
    # SAME activity, positions, tag_map, and tenure block. Anything else
    # would diverge for trivial reasons (network state, DB contents,
    # profile age recomputed at call time) and defeat the parity test.
    with mock.patch(
        "scripts.wallet_archetype_extractor._fetch_activity_dicts",
        return_value=activity,
    ), mock.patch(
        "scripts.wallet_archetype_extractor._fetch_positions_dicts",
        return_value=positions,
    ), mock.patch(
        "scripts.wallet_archetype_extractor._fetch_tag_map",
        return_value=tag_map,
    ), mock.patch(
        "scripts.wallet_archetype_extractor.fetch_wallet_lifetime_stats",
        return_value={},
    ), mock.patch(
        "scripts.wallet_archetype_extractor._fetch_wallet_tenure_metadata",
        return_value=dict(SYNTHETIC_TENURE),
    ):
        live_features = extract_wallet_archetype(
            "0xabc", skip_gate0=True, with_execution=False,
        )

    # Drop fields that legitimately differ between the two paths
    # (timestamps captured at function-call time).
    for f in ("as_of_ts", "extracted_at"):
        offline_features.pop(f, None)
        live_features.pop(f, None)

    assert offline_features == live_features, (
        "offline classifier output diverged from live extractor output for "
        "the same activity+positions+tag_map+tenure_metadata. "
        "The thin-wrapper claim is broken."
    )


def test_offline_classifier_with_explicit_inputs_does_not_hit_network(
    monkeypatch,
):
    """The offline classifier must not open a SQLite connection or make
    any HTTP call when both tag_map AND tenure_metadata are provided
    explicitly. Trap every live producer the legacy fallback paths could
    reach (activity/positions fetchers, tag_map fetcher, gate-0 stats,
    tenure metadata + its two underlying live fetchers + their HTTP
    primitives)."""
    import scripts.wallet_archetype_extractor as mod

    def _trap(*a, **kw):
        raise AssertionError("offline classifier hit a live producer")

    for name in (
        "_fetch_activity_dicts",
        "_fetch_positions_dicts",
        "_fetch_tag_map",
        "fetch_wallet_lifetime_stats",
        "_fetch_wallet_tenure_metadata",
        "_fetch_wallet_lifetime_span",
        "_fetch_wallet_profile_tenure",
        "get_activity_page",
        "build_session",
        "fetch_wallet_profile_enrichment",
    ):
        monkeypatch.setattr(mod, name, _trap, raising=False)

    classify_wallet_archetype_from_snapshot(
        activity=[], positions=[],
        tag_map={},
        tenure_metadata=dict(SYNTHETIC_TENURE),
        skip_gate0=True, with_execution=False,
    )  # no live calls = no AssertionError raised


def test_offline_classifier_with_tag_map_none_falls_back_to_db():
    """When tag_map is None, the snapshot classifier MUST fall back to
    _fetch_tag_map (legacy behavior, documented as not pure). Verify the
    fallback fires so the contract can't drift."""
    import scripts.wallet_archetype_extractor as mod
    called = {"n": 0}

    def fake_fetch_tag_map(condition_ids, *, db_path=None):
        called["n"] += 1
        return {}

    # Stub tenure too so this test isolates the tag_map fallback.
    with mock.patch.object(mod, "_fetch_tag_map", side_effect=fake_fetch_tag_map), \
         mock.patch.object(mod, "_fetch_wallet_tenure_metadata",
                           return_value=dict(SYNTHETIC_TENURE)):
        classify_wallet_archetype_from_snapshot(
            activity=[
                {"type": "TRADE", "conditionId": "0xcid0",
                 "side": "BUY", "timestamp": 1700000000,
                 "price": 0.5, "size": 10, "outcome": "Yes"},
            ],
            positions=[],
            tag_map=None,
            tenure_metadata=dict(SYNTHETIC_TENURE),
            skip_gate0=True, with_execution=False,
        )
    assert called["n"] == 1, (
        "tag_map=None must trigger _fetch_tag_map fallback"
    )


def test_offline_classifier_with_tenure_metadata_none_falls_back_to_live():
    """When tenure_metadata is None, the snapshot classifier MUST fall
    back to _fetch_wallet_tenure_metadata. Symmetric with the tag_map
    fallback contract: the snapshot variant is offline-by-default ONLY
    when the caller passes both inputs explicitly. tenure_metadata=None
    means 'go fetch it from the network/profile API,' identical to what
    the legacy extract_wallet_archetype wrapper does."""
    import scripts.wallet_archetype_extractor as mod
    called = {"n": 0}

    def fake_tenure(wallet):
        called["n"] += 1
        return dict(SYNTHETIC_TENURE)

    with mock.patch.object(mod, "_fetch_wallet_tenure_metadata", side_effect=fake_tenure):
        classify_wallet_archetype_from_snapshot(
            activity=[], positions=[],
            tag_map={},
            tenure_metadata=None,
            wallet="0xabc", skip_gate0=True, with_execution=False,
        )
    assert called["n"] == 1, (
        "tenure_metadata=None must trigger _fetch_wallet_tenure_metadata fallback"
    )
