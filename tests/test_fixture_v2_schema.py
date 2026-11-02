# tests/test_fixture_v2_schema.py
"""v2 fixture schema: WalletData carries five replay-surface fields,
schema_version bumped to 2, replay_surface_meta required for v2,
v1 fixtures still load with replay surfaces None.

Spec: docs/superpowers/specs/2026-05-01-fixture-v2-schema.md.
"""
from __future__ import annotations

import json

import pytest

from sniper.analysis.wallet_protocol.replay import (
    FIXTURE_SCHEMA_VERSION,
    deserialize_wallet_data,
    load_fetch_snapshot,
    save_fetch_snapshot,
    serialize_wallet_data,
)
from sniper.analysis.wallet_protocol.types import WalletData


def _wallet_data_minimal(**overrides) -> WalletData:
    base = dict(
        wallet="0xabc",
        closed_positions=[],
        open_positions=[],
        activity_rows=[],
        activity_cap_hit=False,
        archetype_layer1=None,
        execution_profile=None,
        fetched_at=1700000000,
    )
    base.update(overrides)
    return WalletData(**base)


def test_fixture_schema_version_is_2():
    assert FIXTURE_SCHEMA_VERSION == 2


def test_v2_round_trip_with_all_replay_surfaces():
    data = _wallet_data_minimal(
        archetype_layer1="speculator_bot",
        execution_profile={"status": "populated", "fills_with_drift": []},
        price_history_lookups={"asset_a|1700000000": 0.42, "asset_a|1700086400": None},
        leafs_by_cid={"0xcid1": ["politics"]},
        nav_lookup={"politics": "politics"},
        peer_baselines={"leaf|politics": [0.6, 50]},
        replay_surface_meta={
            "archetype_layer1": {"status": "captured", "captured_at": 1700000000,
                                 "source": "offline-classifier", "producer": "test"},
            "execution_profile": {"status": "captured", "captured_at": 1700000000,
                                  "source": "compute_execution_profile", "producer": "test"},
            "price_history_lookups": {"status": "captured", "captured_at": 1700000000,
                                      "source": "clob /prices-history", "producer": "test",
                                      "n_entries": 2, "n_missing": 1},
            "leafs_by_cid": {"status": "captured", "captured_at": 1700000000,
                             "source": "db.fetch_market_tags_for_cids", "producer": "test"},
            "nav_lookup": {"status": "captured", "captured_at": 1700000000,
                           "source": "db.fetch_tag_nav_category_map", "producer": "test"},
            "peer_baselines": {"status": "captured", "captured_at": 1700000000,
                               "source": "db.peer_baseline_for_grain", "producer": "test"},
        },
    )
    payload = serialize_wallet_data(data)
    assert payload["schema_version"] == 2
    restored = deserialize_wallet_data(payload)
    assert restored.archetype_layer1 == "speculator_bot"
    assert restored.execution_profile == {"status": "populated", "fills_with_drift": []}
    assert restored.price_history_lookups == {
        "asset_a|1700000000": 0.42, "asset_a|1700086400": None,
    }
    assert restored.leafs_by_cid == {"0xcid1": ["politics"]}
    assert restored.nav_lookup == {"politics": "politics"}
    assert restored.peer_baselines == {"leaf|politics": [0.6, 50]}
    assert restored.replay_surface_meta["price_history_lookups"]["n_entries"] == 2


def test_v2_round_trip_with_skipped_surfaces():
    """Skipped surfaces serialize as None and round-trip as None."""
    data = _wallet_data_minimal(
        replay_surface_meta={
            k: {"status": "skipped", "captured_at": 1700000000,
                "source": "skip", "producer": "test"}
            for k in (
                "archetype_layer1", "execution_profile", "price_history_lookups",
                "leafs_by_cid", "nav_lookup", "peer_baselines",
            )
        },
    )
    restored = deserialize_wallet_data(serialize_wallet_data(data))
    for f in (
        "price_history_lookups", "leafs_by_cid", "nav_lookup", "peer_baselines",
    ):
        assert getattr(restored, f) is None
    assert restored.replay_surface_meta is not None


def test_v2_round_trip_with_empty_captured_surfaces():
    """Empty {} captured surfaces round-trip as {} (not None)."""
    data = _wallet_data_minimal(
        price_history_lookups={},
        leafs_by_cid={},
        nav_lookup={},
        peer_baselines={},
        replay_surface_meta={
            k: {"status": "captured", "captured_at": 1700000000,
                "source": "test", "producer": "test"}
            for k in (
                "archetype_layer1", "execution_profile", "price_history_lookups",
                "leafs_by_cid", "nav_lookup", "peer_baselines",
            )
        },
    )
    restored = deserialize_wallet_data(serialize_wallet_data(data))
    assert restored.price_history_lookups == {}
    assert restored.leafs_by_cid == {}
    assert restored.nav_lookup == {}
    assert restored.peer_baselines == {}


def _minimal_v2_payload() -> dict:
    """Build a well-formed v2 payload without going through serialize_wallet_data.

    Used by tests that need to patch the payload after construction — the
    serializer now fails fast on null replay_surface_meta, so these tests
    must construct the base dict directly.
    """
    return {
        "schema_version": 2,
        "wallet": "0xabc",
        "fetched_at": 1700000000,
        "closed_positions": [],
        "open_positions": [],
        "activity_rows": [],
        "activity_cap_hit": False,
        "archetype_layer1": None,
        "execution_profile": None,
        "capital_ledger": None,
        "portfolio_value_usdc": None,
        "fetch_quality_flags": [],
        "subgraph_fallback_used": False,
        "price_history_lookups": None,
        "leafs_by_cid": None,
        "nav_lookup": None,
        "peer_baselines": None,
        "replay_surface_meta": {},
    }


def test_v2_rejects_missing_replay_surface_meta():
    payload = _minimal_v2_payload()
    payload.pop("replay_surface_meta", None)
    with pytest.raises(ValueError, match=r"replay_surface_meta"):
        deserialize_wallet_data(payload)


def test_v2_rejects_null_replay_surface_meta():
    """REQUIRED means non-null, not just key-present. A v2 fixture with
    "replay_surface_meta": null is malformed and must be rejected."""
    payload = _minimal_v2_payload()
    payload["replay_surface_meta"] = None
    with pytest.raises(ValueError, match=r"replay_surface_meta"):
        deserialize_wallet_data(payload)


def test_save_rejects_walletdata_with_null_replay_surface_meta(tmp_path):
    """Symmetric with deserializer: a v2 fixture must carry a non-null
    replay_surface_meta. The writer must fail fast on save instead of
    producing a JSON file that load_fetch_snapshot will reject. This
    closes the save→load asymmetry codex flagged on commit a15d80b."""
    data = _wallet_data_minimal()  # replay_surface_meta defaults to None
    out = tmp_path / "0xabc_test.json"
    with pytest.raises(ValueError, match=r"replay_surface_meta"):
        save_fetch_snapshot(data, out)
    assert not out.exists(), (
        "save_fetch_snapshot must NOT create a file when input is invalid"
    )


def test_serialize_rejects_walletdata_with_null_replay_surface_meta():
    data = _wallet_data_minimal()
    with pytest.raises(ValueError, match=r"replay_surface_meta"):
        serialize_wallet_data(data)


def test_v1_fixture_still_loads_with_replay_surfaces_none():
    # Build the payload directly — serializer now rejects null replay_surface_meta,
    # so we construct a v2 base dict and then downgrade it to v1 shape.
    payload = _minimal_v2_payload()
    # Force v1 shape: drop v2-only keys so the payload looks like a real v1 file.
    payload["schema_version"] = 1
    for key in ("price_history_lookups", "leafs_by_cid", "nav_lookup",
                "peer_baselines", "replay_surface_meta"):
        payload.pop(key, None)
    restored = deserialize_wallet_data(payload)
    assert restored.archetype_layer1 is None
    assert restored.price_history_lookups is None
    assert restored.peer_baselines is None
    assert restored.replay_surface_meta is None


def test_unsupported_schema_version_rejected():
    # Build the payload directly — serializer now rejects null replay_surface_meta.
    payload = _minimal_v2_payload()
    payload["schema_version"] = 99
    with pytest.raises(ValueError, match=r"schema_version"):
        deserialize_wallet_data(payload)


def test_existing_calibration_v3_fixtures_still_load(tmp_path):
    """The 10 calibration fixtures recorded under v3 (post-Phase-A) must
    continue to load without error.

    v3 fixtures were captured against the rebuilt fetcher (DESC sort, no
    client caps, walk_full_history=True default). They are schema_version=2
    with replay_surface_meta populated and price_history_lookups present.
    Previously this test checked `*_2026-04-30.json` (v1/v2 mix) — those
    fixtures were retired during the 2026-05-13 v3 re-baseline.
    """
    from pathlib import Path
    fixtures = sorted(Path("tests/calibration/fixtures").glob("*_2026-05-13.json"))
    assert fixtures, "v3 calibration fixtures missing — re-record before running this test"
    for fx in fixtures:
        data = load_fetch_snapshot(fx)
        assert data.wallet
        assert data.replay_surface_meta is not None  # v3 = v2 schema with surfaces


def test_archetype_layer2_round_trips_when_present():
    """Layer 2 archetype is an additive v2 field; capture + replay must
    preserve it exactly."""
    data = _wallet_data_minimal(
        archetype_layer1="directional_discretionary",
        archetype_layer2="thesis_specialist",
        replay_surface_meta={
            k: {"status": "captured", "captured_at": 1700000000,
                "source": "test", "producer": "test"}
            for k in (
                "archetype_layer1", "execution_profile", "price_history_lookups",
                "leafs_by_cid", "nav_lookup", "peer_baselines",
            )
        },
    )
    payload = serialize_wallet_data(data)
    assert payload["archetype_layer2"] == "thesis_specialist"
    restored = deserialize_wallet_data(payload)
    assert restored.archetype_layer1 == "directional_discretionary"
    assert restored.archetype_layer2 == "thesis_specialist"


def test_archetype_layer2_absent_loads_as_none_for_legacy_payloads():
    """A v2 payload that predates the archetype_layer2 field (key absent)
    must still load, with archetype_layer2=None."""
    payload = _minimal_v2_payload()
    payload.pop("archetype_layer2", None)  # ensure it's absent
    restored = deserialize_wallet_data(payload)
    assert restored.archetype_layer2 is None


def test_archetype_layer2_serializes_as_none_when_unset():
    data = _wallet_data_minimal(
        replay_surface_meta={
            k: {"status": "skipped", "captured_at": 1700000000,
                "source": "skip", "producer": "test"}
            for k in (
                "archetype_layer1", "execution_profile", "price_history_lookups",
                "leafs_by_cid", "nav_lookup", "peer_baselines",
            )
        },
    )
    # data.archetype_layer2 defaults to None
    payload = serialize_wallet_data(data)
    assert "archetype_layer2" in payload
    assert payload["archetype_layer2"] is None


# ---------------------------------------------------------------------------
# Test 12b — None survives replay round-trip for redemptions/splits/merges
# (spec §8.4 — serialize MUST NOT coerce None→[])
# ---------------------------------------------------------------------------
def _minimal_replay_meta() -> dict:
    return {
        k: {"status": "skipped", "captured_at": 1700000000,
            "source": "skip", "producer": "test"}
        for k in (
            "archetype_layer1", "execution_profile", "price_history_lookups",
            "leafs_by_cid", "nav_lookup", "peer_baselines",
        )
    }


def test_redemptions_splits_merges_none_survives_round_trip():
    """None (fetch-failure) round-trips as None; [] (empty fetch) round-trips as [].
    Pins that serialize does NOT coerce None→[] which would collapse the
    RECONCILIATION_INPUTS_MISSING caveat into NO_CLOSED_OR_REDEEMED_CONDITIONS."""
    # None case — fetch failure
    data_none = _wallet_data_minimal(
        redemptions=None,
        splits=None,
        merges=None,
        replay_surface_meta=_minimal_replay_meta(),
    )
    payload_none = serialize_wallet_data(data_none)
    assert payload_none["redemptions"] is None, "None must serialize as null, not []"
    assert payload_none["splits"] is None
    assert payload_none["merges"] is None
    restored_none = deserialize_wallet_data(payload_none)
    assert restored_none.redemptions is None, "None must round-trip as None"
    assert restored_none.splits is None
    assert restored_none.merges is None

    # [] case — fetch succeeded, empty result
    data_empty = _wallet_data_minimal(
        redemptions=[],
        splits=[],
        merges=[],
        replay_surface_meta=_minimal_replay_meta(),
    )
    payload_empty = serialize_wallet_data(data_empty)
    assert payload_empty["redemptions"] == [], "[] must serialize as [], not None"
    assert payload_empty["splits"] == []
    assert payload_empty["merges"] == []
    restored_empty = deserialize_wallet_data(payload_empty)
    assert restored_empty.redemptions == [], "[] must round-trip as []"
    assert restored_empty.splits == []
    assert restored_empty.merges == []


def test_redemptions_splits_merges_absent_key_loads_as_none():
    """Pre-surface fixture (no redemptions/splits/merges keys) loads with all three None."""
    payload = _minimal_v2_payload()
    # ensure keys absent (pre-surface fixture)
    for key in ("redemptions", "splits", "merges"):
        payload.pop(key, None)
    restored = deserialize_wallet_data(payload)
    assert restored.redemptions is None
    assert restored.splits is None
    assert restored.merges is None
