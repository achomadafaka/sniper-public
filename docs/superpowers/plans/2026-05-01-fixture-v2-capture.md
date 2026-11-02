# Fixture v2 Capture Implementation Plan (Phase 2B-capture)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the v2 fixture capability per `docs/superpowers/specs/2026-05-01-fixture-v2-schema.md` — extend `WalletData` with five optional replay-surface fields, bump `FIXTURE_SCHEMA_VERSION` 1 → 2, refactor the archetype extractor for offline same-snapshot classification, capture all five surfaces in `record_fetch_snapshot.py`, and make `--from-fixture` strictly fixture-only (no live fallback).

**Architecture:** Eight TDD tasks. Each adds a failing regression test before any implementation, follows the spec's locked decisions verbatim, and ends in a verified-green commit. v1 fixtures continue to load. The 10 calibration fixtures recorded under v1 stay valid; their re-record is Phase 2C work, not part of this plan.

**Tech Stack:** Python 3, pytest, `unittest.mock` for closure stubs and live-call traps, `requests.Session` mocking for the price-history adapter, sqlite (via `db.peer_baseline_for_grain` and `db.fetch_market_tags_for_cids`).

---

## Task 1: WalletData v2 fields + serializer round-trip

**Files:**
- Test: `tests/test_fixture_v2_schema.py` (create)
- Modify: `sniper/analysis/wallet_protocol/types.py:186-205` (`WalletData`)
- Modify: `sniper/analysis/wallet_protocol/replay.py:34` (`FIXTURE_SCHEMA_VERSION` constant + `serialize_wallet_data` + `deserialize_wallet_data`)

Add the five optional replay-surface fields to `WalletData`, bump `FIXTURE_SCHEMA_VERSION` to 2, update the serialize/deserialize round-trip to include them, enforce that v2 fixtures carry `replay_surface_meta`.

- [ ] **Step 1: Write failing tests**

```python
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


def test_v2_rejects_missing_replay_surface_meta():
    payload = serialize_wallet_data(_wallet_data_minimal())
    payload.pop("replay_surface_meta", None)
    payload["schema_version"] = 2
    with pytest.raises(ValueError, match=r"replay_surface_meta"):
        deserialize_wallet_data(payload)


def test_v2_rejects_null_replay_surface_meta():
    """REQUIRED means non-null, not just key-present. A v2 fixture with
    "replay_surface_meta": null is malformed and must be rejected."""
    payload = serialize_wallet_data(_wallet_data_minimal())
    payload["schema_version"] = 2
    payload["replay_surface_meta"] = None
    with pytest.raises(ValueError, match=r"replay_surface_meta"):
        deserialize_wallet_data(payload)


def test_v1_fixture_still_loads_with_replay_surfaces_none():
    payload = serialize_wallet_data(_wallet_data_minimal())
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
    payload = serialize_wallet_data(_wallet_data_minimal())
    payload["schema_version"] = 99
    with pytest.raises(ValueError, match=r"schema_version"):
        deserialize_wallet_data(payload)


def test_existing_calibration_v1_fixtures_still_load(tmp_path):
    """The 10 calibration fixtures recorded under v1 must continue to load
    without error after the v1→v2 bump."""
    from pathlib import Path
    fixtures = sorted(Path("tests/calibration/fixtures").glob("*_2026-04-30.json"))
    assert fixtures, "calibration fixtures missing — re-record before running this test"
    for fx in fixtures:
        data = load_fetch_snapshot(fx)
        assert data.wallet
        assert data.replay_surface_meta is None  # v1 fixture
        assert data.price_history_lookups is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_fixture_v2_schema.py -q`

Expected: most tests FAIL — `WalletData` does not have the new fields, `FIXTURE_SCHEMA_VERSION` is still 1, serializer doesn't emit replay-surface fields, deserializer doesn't enforce `replay_surface_meta`.

- [ ] **Step 3: Add v2 fields to `WalletData`**

In `sniper/analysis/wallet_protocol/types.py:186-205`, append the five optional fields after `subgraph_fallback_used`:

```python
    # --- v2 replay surfaces (spec 2026-05-01-fixture-v2-schema.md) ---
    price_history_lookups: dict[str, float | None] | None = None
    leafs_by_cid: dict[str, list[str]] | None = None
    nav_lookup: dict[str, str] | None = None
    peer_baselines: dict[str, list] | None = None  # values are [mean_wr, peer_count] lists
    replay_surface_meta: dict[str, dict] | None = None
```

- [ ] **Step 4: Update `FIXTURE_SCHEMA_VERSION` and the round-trip in `replay.py`**

In `sniper/analysis/wallet_protocol/replay.py`:

```python
FIXTURE_SCHEMA_VERSION = 2
```

In `serialize_wallet_data` (around line 42), include the new fields. The full body becomes:

```python
def serialize_wallet_data(data: WalletData) -> dict[str, Any]:
    return {
        "schema_version": FIXTURE_SCHEMA_VERSION,
        "wallet": data.wallet,
        "closed_positions": list(data.closed_positions),
        "open_positions": list(data.open_positions),
        "activity_rows": list(data.activity_rows),
        "activity_cap_hit": data.activity_cap_hit,
        "archetype_layer1": data.archetype_layer1,
        "execution_profile": data.execution_profile,
        "fetched_at": data.fetched_at,
        "capital_ledger": _serialize_capital_ledger(data.capital_ledger),
        "portfolio_value_usdc": data.portfolio_value_usdc,
        "fetch_quality_flags": list(data.fetch_quality_flags),
        "subgraph_fallback_used": data.subgraph_fallback_used,
        # v2 replay surfaces:
        "price_history_lookups": data.price_history_lookups,
        "leafs_by_cid": data.leafs_by_cid,
        "nav_lookup": data.nav_lookup,
        "peer_baselines": data.peer_baselines,
        "replay_surface_meta": data.replay_surface_meta,
    }
```

(Use whatever `_serialize_capital_ledger` helper already exists; do not change capital ledger handling.)

In `deserialize_wallet_data` (around line 64), update the schema_version check and add the new fields:

```python
def deserialize_wallet_data(payload: dict[str, Any]) -> WalletData:
    schema_version = payload.get("schema_version")
    if not isinstance(schema_version, int) or schema_version < 1 or schema_version > FIXTURE_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported fixture schema_version {schema_version!r}; "
            f"this build supports 1..{FIXTURE_SCHEMA_VERSION}"
        )
    if "wallet" not in payload:
        raise ValueError("fixture payload missing 'wallet' field")

    is_v2 = schema_version >= 2
    if is_v2 and not isinstance(payload.get("replay_surface_meta"), dict):
        # REQUIRED means non-null, not just key-present. A missing key OR
        # an explicit null both fail closed.
        raise ValueError(
            "v2 fixture missing or null 'replay_surface_meta' field "
            "(must be a non-null dict)"
        )

    return WalletData(
        wallet=payload["wallet"],
        closed_positions=list(payload.get("closed_positions") or []),
        open_positions=list(payload.get("open_positions") or []),
        activity_rows=list(payload.get("activity_rows") or []),
        activity_cap_hit=bool(payload.get("activity_cap_hit", False)),
        archetype_layer1=payload.get("archetype_layer1"),
        execution_profile=payload.get("execution_profile"),
        fetched_at=int(payload.get("fetched_at") or 0),
        capital_ledger=_deserialize_capital_ledger(payload.get("capital_ledger")),
        portfolio_value_usdc=payload.get("portfolio_value_usdc"),
        fetch_quality_flags=list(payload.get("fetch_quality_flags") or []),
        subgraph_fallback_used=bool(payload.get("subgraph_fallback_used", False)),
        # v2 replay surfaces — None when v1 (key absent) or when explicitly skipped.
        price_history_lookups=payload.get("price_history_lookups"),
        leafs_by_cid=payload.get("leafs_by_cid"),
        nav_lookup=payload.get("nav_lookup"),
        peer_baselines=payload.get("peer_baselines"),
        replay_surface_meta=payload.get("replay_surface_meta"),
    )
```

(Preserve the existing `_serialize_capital_ledger` / `_deserialize_capital_ledger` calls if they're already in place; do not regress them.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_fixture_v2_schema.py tests/test_replay_determinism.py -q`

Expected: all green. Update any pre-existing replay-determinism test that hard-codes `schema_version: 1` (cite the v2 bump in the comment).

- [ ] **Step 6: Commit**

```bash
git add tests/test_fixture_v2_schema.py \
        sniper/analysis/wallet_protocol/types.py \
        sniper/analysis/wallet_protocol/replay.py
git commit -m "feat: WalletData v2 — five replay-surface fields + schema_version 2"
```

---

## Task 2: `replay.build_fixture_closures` helper

**Files:**
- Test: `tests/test_fixture_closures.py` (create)
- Modify: `sniper/analysis/wallet_protocol/replay.py` (add `build_fixture_closures` function)

The helper constructs `(price_history_fn, peer_baseline_fn)` from a loaded `WalletData`. The closures honor the `None` vs `{}` distinction: `None` field ⇒ closure is `None`; `{}` field ⇒ closure exists, returns `None` for every lookup.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_fixture_closures.py
"""build_fixture_closures honors the spec's None vs {} rule:
None field → closure is None (hypothesis underpowers with 'no_*' reason);
{} field → closure exists, returns None per lookup (hypothesis underpowers
with 'n_below_threshold' or similar data-side reason).

Spec: docs/superpowers/specs/2026-05-01-fixture-v2-schema.md Rule 1 + Loader.
"""
from __future__ import annotations

from sniper.analysis.wallet_protocol.replay import build_fixture_closures
from sniper.analysis.wallet_protocol.types import WalletData


def _wd(**overrides) -> WalletData:
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


def test_price_history_closure_is_none_when_field_is_none():
    data = _wd(price_history_lookups=None)
    price_fn, _ = build_fixture_closures(data)
    assert price_fn is None


def test_price_history_closure_exists_for_empty_dict():
    data = _wd(price_history_lookups={})
    price_fn, _ = build_fixture_closures(data)
    assert price_fn is not None
    assert price_fn("asset_a", 1700000000) is None


def test_price_history_closure_returns_stored_value():
    data = _wd(price_history_lookups={
        "asset_a|1700000000": 0.42,
        "asset_a|1700086400": None,
        "asset_b|1700000000": 0.61,
    })
    price_fn, _ = build_fixture_closures(data)
    assert price_fn("asset_a", 1700000000) == 0.42
    assert price_fn("asset_a", 1700086400) is None  # captured-but-null
    assert price_fn("asset_b", 1700000000) == 0.61
    assert price_fn("asset_unknown", 1700000000) is None


def test_peer_baseline_closure_is_none_when_field_is_none():
    data = _wd(peer_baselines=None)
    _, peer_fn = build_fixture_closures(data)
    assert peer_fn is None


def test_peer_baseline_closure_exists_for_empty_dict():
    data = _wd(peer_baselines={})
    _, peer_fn = build_fixture_closures(data)
    assert peer_fn is not None
    assert peer_fn("leaf", "politics", "0xabc") is None


def test_peer_baseline_closure_returns_stored_tuple():
    data = _wd(peer_baselines={
        "leaf|politics": [0.612, 47],
        "leaf|sports": [0.581, 23],
        "nav|politics": [0.598, 88],
    })
    _, peer_fn = build_fixture_closures(data)
    # The closure returns a (mean_wr, peer_count) tuple per the existing
    # PeerBaselineFn contract.
    assert peer_fn("leaf", "politics", "0xabc") == (0.612, 47)
    assert peer_fn("nav", "politics", "0xabc") == (0.598, 88)
    assert peer_fn("leaf", "unknown_tag", "0xabc") is None


def test_peer_baseline_closure_handles_null_value():
    data = _wd(peer_baselines={"leaf|politics": None})
    _, peer_fn = build_fixture_closures(data)
    assert peer_fn("leaf", "politics", "0xabc") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_fixture_closures.py -q`

Expected: ALL FAIL — `build_fixture_closures` does not exist yet.

- [ ] **Step 3: Implement `build_fixture_closures`**

Append to `sniper/analysis/wallet_protocol/replay.py`:

```python
def build_fixture_closures(
    data: WalletData,
) -> tuple[Callable | None, Callable | None]:
    """Construct fixture-backed (price_history_fn, peer_baseline_fn) closures.

    Rule (spec 2026-05-01-fixture-v2-schema.md, "Empty ≠ skipped"):
    * None field → closure is None.
    * Dict field (including {}) → closure exists, returns stored value or
      None for unknown keys.
    """
    price_fn: Callable | None = None
    peer_fn: Callable | None = None

    if data.price_history_lookups is not None:
        lookups = data.price_history_lookups

        def _price_history_fn(asset_id: str, ts: int) -> float | None:
            return lookups.get(f"{asset_id}|{int(ts)}")

        price_fn = _price_history_fn

    if data.peer_baselines is not None:
        baselines = data.peer_baselines

        def _peer_baseline_fn(grain: str, tag: str, wallet: str):
            value = baselines.get(f"{grain}|{tag}")
            if value is None:
                return None
            mean_wr, peer_count = value[0], value[1]
            return (float(mean_wr), int(peer_count))

        peer_fn = _peer_baseline_fn

    return price_fn, peer_fn


__all__ = list(__all__) + ["build_fixture_closures"]  # adjust to match existing __all__ shape
```

(Adjust the `__all__` extension to match how `replay.py` declares it today — append `"build_fixture_closures"` to the existing tuple/list.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_fixture_closures.py -q`

Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/test_fixture_closures.py sniper/analysis/wallet_protocol/replay.py
git commit -m "feat: replay.build_fixture_closures honors None vs {} semantics"
```

---

## Task 3: Archetype offline classifier + parity test

**Files:**
- Test: `tests/test_archetype_offline_parity.py` (create)
- Modify: `scripts/wallet_archetype_extractor.py` (add `classify_wallet_archetype_from_snapshot` + thin-wrapper refactor of `extract_wallet_archetype`)

Codex's locked implementation note: "narrow parity test for the new archetype refactor — existing extract_wallet_archetype() output vs classify_wallet_archetype_from_snapshot() on the same fetched rows, at least for a mocked fixture. That prevents the 'thin wrapper' claim from drifting."

- [ ] **Step 1: Write failing parity test**

```python
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
    # test only proves "same inputs → same outputs"; the tag_map AND
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_archetype_offline_parity.py -q`

Expected: import error / AttributeError — `classify_wallet_archetype_from_snapshot` does not exist yet.

- [ ] **Step 3: Refactor `extract_wallet_archetype` into snapshot variant + thin wrapper**

In `scripts/wallet_archetype_extractor.py`:

1. Locate the body of `extract_wallet_archetype` at line 1756. Identify the lines that fetch (`_fetch_activity_dicts(wallet, limit=activity_limit)` and `_fetch_positions_dicts(wallet)`) and the lines that compute features (everything after, including the `tag_map` lookup and the return dict assembly).
2. Extract the post-fetch logic into a new module-level function:

```python
def classify_wallet_archetype_from_snapshot(
    activity: list[dict],
    positions: list[dict],
    *,
    tag_map: dict[str, frozenset[str]] | None = None,
    tenure_metadata: dict[str, Any] | None = None,
    wallet: str = "",
    skip_gate0: bool = True,
    with_execution: bool = False,
    force_execution: bool = False,
) -> dict[str, Any]:
    """Offline archetype classifier. Takes already-fetched activity and
    positions; returns the same `features` dict that extract_wallet_archetype
    returns.

    No NETWORK calls and no DB calls when BOTH `tag_map` AND
    `tenure_metadata` are passed explicitly. Each falls back to a live
    producer when None — that fallback exists only for the legacy
    `extract_wallet_archetype` wrapper.

    `tag_map`:
      * `tag_map={...}` (or `tag_map={}` for an explicit no-DB run) →
        no SQLite contact. Replay paths and the parity test pass this.
      * `tag_map=None` → falls back to `_fetch_tag_map(condition_ids)`,
        which opens a SQLite connection against `db.get_db_path()`. NOT pure.

    `tenure_metadata`:
      * `tenure_metadata={...}` → use directly. Required keys mirror
        `_fetch_wallet_tenure_metadata` output: `wallet_first_activity_ts`,
        `wallet_last_activity_ts`, `wallet_lifetime_days`,
        `pagination_complete`, `pages_fetched`, plus optional
        `profile_joined_ts`, `profile_lifetime_days`,
        `profile_tenure_source`, `wallet_lifetime_source`. Replay paths
        and the parity test pass this.
      * `tenure_metadata=None` → falls back to
        `_fetch_wallet_tenure_metadata(wallet)`, which makes live HTTP
        calls (`get_activity_page` + `build_session` +
        `fetch_wallet_profile_enrichment`). NOT pure.

    `wallet` is informational (stamped into the features dict). Default
    empty string is acceptable for callers that don't need the address in
    the output (e.g. the parity test). Note: `tenure_metadata=None` with
    an empty `wallet` will hit network for an empty address, which the
    public producers will likely reject — pass tenure_metadata explicitly
    when wallet is empty.

    Spec: docs/superpowers/specs/2026-05-01-fixture-v2-schema.md.
    """
    now = datetime.now(timezone.utc)
    trades = _trade_events(activity)
    position_rows = _position_events(positions)
    tag_condition_ids = (
        [str(r.get("conditionId") or "") for r in trades]
        + [str(r.get("conditionId") or "") for r in position_rows]
    )
    if tag_map is None:
        tag_map = _fetch_tag_map(tag_condition_ids)
    if tenure_metadata is None:
        tenure_metadata = _fetch_wallet_tenure_metadata(wallet)
    resolved = _resolved_position_summaries(activity, tag_map=tag_map)
    features = {
        "wallet": (wallet or "").lower(),
        "as_of_ts": int(now.timestamp()),
        "extracted_at": now.isoformat(),
        "gate0": {} if skip_gate0 else fetch_wallet_lifetime_stats(wallet),
        "counts": {
            "activity_records": len(activity),
            "trade_records": len(trades),
            "live_positions": len(position_rows),
        },
        # ... (rest of feature dict assembly — copy verbatim from current
        # extract_wallet_archetype body, EXCEPT the timing block, which
        # must use the injected tenure_metadata instead of calling
        # _fetch_wallet_tenure_metadata directly):
        "timing": {
            **_timing_metrics(trades),
            **tenure_metadata,
        },
        # ... rest of the assembly unchanged
        ...
    }
    return features
```

3. Replace the body of `extract_wallet_archetype` with a thin wrapper:

```python
def extract_wallet_archetype(
    wallet: str,
    skip_gate0: bool = False,
    activity_limit: int = 500,
    *,
    with_execution: bool = False,
    force_execution: bool = False,
) -> dict[str, Any]:
    activity = _fetch_activity_dicts(wallet, limit=activity_limit)
    positions = _fetch_positions_dicts(wallet)
    # tag_map=None and tenure_metadata=None let the snapshot classifier
    # fall back to live producers (the legacy behavior). Replay/capture
    # callers MUST pass both explicitly to avoid the DB side channel and
    # the live HTTP path through _fetch_wallet_tenure_metadata.
    return classify_wallet_archetype_from_snapshot(
        activity, positions,
        tag_map=None,
        tenure_metadata=None,
        wallet=wallet,
        skip_gate0=skip_gate0,
        with_execution=with_execution,
        force_execution=force_execution,
    )
```

The crucial constraint: no feature computation lives in the wrapper. Every line that produces a value in the returned features dict must live inside `classify_wallet_archetype_from_snapshot`. The wrapper's job is exclusively (a) fetch activity, (b) fetch positions, (c) delegate. Anything that touches a producer the snapshot variant should also be able to receive as an argument (currently `tag_map` and `tenure_metadata`) belongs as an injectable parameter, not as a deeper wrapper layer.

The snapshot variant has TWO live-producer side channels, both gated on a None argument:
1. `_fetch_tag_map` — fires when `tag_map is None`. Opens a SQLite connection against `db.get_db_path()`.
2. `_fetch_wallet_tenure_metadata` — fires when `tenure_metadata is None`. Internally calls `_fetch_wallet_lifetime_span` (live `get_activity_page` HTTP) AND `_fetch_wallet_profile_tenure` (live `build_session` + `fetch_wallet_profile_enrichment` HTTP). This is why the no-network test traps all six functions.

The capture script and the parity test pass BOTH `tag_map` and `tenure_metadata` explicitly so the snapshot variant stays DB-free AND HTTP-free in those code paths. This is the only way to honor the "no live calls under fixture replay" rule (Rule 1 in the v2 schema spec).

NOTE: `fetch_wallet_lifetime_stats` is conditional on `not skip_gate0`. The parity test passes `skip_gate0=True` so it does not run. Capture script also passes `skip_gate0=True`. Good — keeps the snapshot variant pure even though the function technically can call `fetch_wallet_lifetime_stats` on the gate-0 path.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_archetype_offline_parity.py -q`

Expected: 4 passed (parity, no-network with explicit inputs, tag_map=None fallback, tenure_metadata=None fallback).

- [ ] **Step 5: Run full archetype test set + smoke**

Run: `python3 -m pytest tests/ -q -k archetype`

Expected: green. If a pre-existing test calls `extract_wallet_archetype` directly with mocked fetchers and asserts on internal feature shape, it should still pass because the wrapper now delegates to the snapshot variant.

Smoke (offline-only — no network, no DB):
```bash
python3 -c "
from scripts.wallet_archetype_extractor import classify_wallet_archetype_from_snapshot
print(classify_wallet_archetype_from_snapshot(
    [], [],
    tag_map={},
    tenure_metadata={
        'wallet_first_activity_ts': 0,
        'wallet_last_activity_ts': 0,
        'wallet_lifetime_days': 0.0,
        'pagination_complete': True,
        'pages_fetched': 0,
    },
    skip_gate0=True,
)['classification'])
"
```
Verify: prints a classification dict, no `requests` calls, no SQLite open. The explicit `tag_map={}` and `tenure_metadata={...}` are required — defaults would trip live producers.

- [ ] **Step 6: Commit**

```bash
git add tests/test_archetype_offline_parity.py scripts/wallet_archetype_extractor.py
git commit -m "feat: classify_wallet_archetype_from_snapshot — offline same-snapshot classifier"
```

---

## Task 4: Capture script — archetype + execution_profile + meta

**Files:**
- Test: `tests/test_record_fetch_snapshot_v2.py` (create)
- Modify: `scripts/record_fetch_snapshot.py`

Wire `classify_wallet_archetype_from_snapshot` and `build_execution_profile` into the capture script. Add `--no-archetype` and `--no-execution-profile` flags. Populate `WalletData.replay_surface_meta` for both surfaces. Other surfaces (Tasks 5 & 6) stay None for now.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_record_fetch_snapshot_v2.py
"""record_fetch_snapshot.py captures v2 replay surfaces with proper
provenance metadata. Each task in the 2B-capture plan adds a slice;
this file accumulates assertions across slices.

Spec: docs/superpowers/specs/2026-05-01-fixture-v2-schema.md.
"""
from __future__ import annotations

import json
import unittest.mock as mock

import pytest

import scripts.record_fetch_snapshot as mod
from sniper.analysis.wallet_protocol.types import WalletData


@pytest.fixture
def synthetic_wallet_data():
    return WalletData(
        wallet="0xabc",
        closed_positions=[
            {"conditionId": "0xcid0", "outcome": "Yes", "avgPrice": 0.45,
             "size": 50, "realizedPnl": 25.0, "endDate": "2026-04-01T00:00:00Z"},
        ],
        open_positions=[
            {"conditionId": "0xcid1", "outcome": "Yes", "asset": "asset_b",
             "avgPrice": 0.52, "size": 100},
        ],
        activity_rows=[
            {"type": "TRADE", "side": "BUY", "conditionId": "0xcid0",
             "asset": "asset_a", "timestamp": 1700000000, "price": 0.45,
             "size": 50, "outcome": "Yes"},
            {"type": "TRADE", "side": "SELL", "conditionId": "0xcid0",
             "asset": "asset_a", "timestamp": 1700050000, "price": 0.50,
             "size": 50, "outcome": "Yes"},
        ],
        activity_cap_hit=False,
        archetype_layer1=None,
        execution_profile=None,
        fetched_at=1700000000,
    )


# Stub tenure dict reused across tests. Capture script's archetype
# branch calls _fetch_tag_map AND _fetch_wallet_tenure_metadata BEFORE
# the snapshot classifier; both must be stubbed in every test that
# reaches the archetype branch (otherwise tag_map fetch hits SQLite and
# tenure fetch hits live HTTP). The autouse fixture below applies these
# stubs to EVERY test in this file, so tests don't have to repeat them.
# Tests that explicitly skip the archetype branch (--no-archetype) still
# get the stubs; they're harmless when unused.
STUB_TENURE = {
    "wallet_first_activity_ts": 1_700_000_000,
    "wallet_last_activity_ts": 1_700_050_000,
    "wallet_lifetime_days": 0.58,
    "pagination_complete": True,
    "pages_fetched": 1,
    "profile_joined_ts": 1_690_000_000,
    "profile_lifetime_days": 365.0,
    "profile_tenure_source": "test",
    "wallet_lifetime_source": "profile_joined",
}


@pytest.fixture(autouse=True)
def _stub_archetype_producers(monkeypatch):
    """Block live HTTP / SQLite producers that the capture script's
    archetype branch hits before invoking the snapshot classifier.

    Even though a given test may also `mock.patch(... classify_...)`
    directly, the capture script still calls `_fetch_tag_map(cids)` and
    `_fetch_wallet_tenure_metadata(wallet)` first to materialize the
    inputs it passes into the classifier. Without these stubs those
    calls would attempt SQLite and live Polymarket HTTP, which is
    unacceptable for unit tests.

    Tests asserting on tenure provenance metadata can rely on the
    STUB_TENURE values above (e.g.
    `meta["archetype_layer1"]["tenure_lifetime_source"] == "profile_joined"`).
    """
    import scripts.wallet_archetype_extractor as wae
    monkeypatch.setattr(wae, "_fetch_tag_map", lambda *a, **kw: {})
    monkeypatch.setattr(
        wae, "_fetch_wallet_tenure_metadata",
        lambda *a, **kw: dict(STUB_TENURE),
    )


def test_capture_writes_archetype_with_meta(tmp_path, synthetic_wallet_data):
    out = tmp_path / "0xabc_test.json"
    with mock.patch.object(mod, "fetch_wallet_data",
                           return_value=synthetic_wallet_data), \
         mock.patch(
             "scripts.wallet_archetype_extractor."
             "classify_wallet_archetype_from_snapshot",
             return_value={"classification": {"archetype": "speculator_bot"}},
         ), \
         mock.patch.object(mod, "build_execution_profile",
                           return_value={"status": "populated"}):
        rc = mod.main(["0xabc", "--out", str(out)])
    assert rc == 0
    payload = json.loads(out.read_text())
    assert payload["archetype_layer1"] == "speculator_bot"
    assert payload["execution_profile"] == {"status": "populated"}
    meta = payload["replay_surface_meta"]
    assert meta["archetype_layer1"]["status"] == "captured"
    # Provenance must record that tenure_metadata was pre-fetched (not
    # left as None to fall back to live HTTP). Values come from the
    # autouse STUB_TENURE.
    assert meta["archetype_layer1"]["tenure_lifetime_source"] == "profile_joined"
    assert meta["archetype_layer1"]["tenure_pagination_complete"] is True
    assert meta["execution_profile"]["status"] == "captured"
    assert meta["execution_profile"]["inner_status"] == "populated"


def test_no_archetype_flag_skips_with_meta(tmp_path, synthetic_wallet_data):
    out = tmp_path / "0xabc_test.json"
    with mock.patch.object(mod, "fetch_wallet_data",
                           return_value=synthetic_wallet_data), \
         mock.patch.object(mod, "build_execution_profile",
                           return_value={"status": "populated"}):
        rc = mod.main(["0xabc", "--out", str(out), "--no-archetype"])
    assert rc == 0
    payload = json.loads(out.read_text())
    assert payload["archetype_layer1"] is None
    assert payload["replay_surface_meta"]["archetype_layer1"]["status"] == "skipped"


def test_no_execution_profile_flag_skips_with_meta(
    tmp_path, synthetic_wallet_data,
):
    out = tmp_path / "0xabc_test.json"
    with mock.patch.object(mod, "fetch_wallet_data",
                           return_value=synthetic_wallet_data), \
         mock.patch(
             "scripts.wallet_archetype_extractor."
             "classify_wallet_archetype_from_snapshot",
             return_value={"classification": {"archetype": "low_signal"}},
         ):
        rc = mod.main(["0xabc", "--out", str(out), "--no-execution-profile"])
    assert rc == 0
    payload = json.loads(out.read_text())
    assert payload["execution_profile"] is None
    assert payload["replay_surface_meta"]["execution_profile"]["status"] == "skipped"


def test_archetype_failure_records_failed_status(
    tmp_path, synthetic_wallet_data,
):
    out = tmp_path / "0xabc_test.json"
    # The capture script fetches tag_map and tenure (autouse stubs)
    # BEFORE invoking classify, so the RuntimeError below only fires on
    # the classify call. The script's try/except records that failure
    # in replay_surface_meta.
    with mock.patch.object(mod, "fetch_wallet_data",
                           return_value=synthetic_wallet_data), \
         mock.patch(
             "scripts.wallet_archetype_extractor."
             "classify_wallet_archetype_from_snapshot",
             side_effect=RuntimeError("boom"),
         ), \
         mock.patch.object(mod, "build_execution_profile",
                           return_value={"status": "populated"}):
        rc = mod.main(["0xabc", "--out", str(out)])
    assert rc == 0
    payload = json.loads(out.read_text())
    assert payload["archetype_layer1"] is None
    assert payload["replay_surface_meta"]["archetype_layer1"]["status"] == "failed"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_record_fetch_snapshot_v2.py -q`

Expected: ALL FAIL — capture script doesn't invoke archetype/exec, doesn't write `replay_surface_meta`, doesn't have skip flags.

- [ ] **Step 3: Update `scripts/record_fetch_snapshot.py`**

The script's `main` function expands. Pseudocode for the new flow:

```python
def _build_parser():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("wallet")
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--full-history", action="store_true")
    ap.add_argument("--capital-ledger", action="store_true")
    ap.add_argument("--no-archetype", action="store_true")
    ap.add_argument("--no-execution-profile", action="store_true")
    ap.add_argument("--no-price-history", action="store_true")
    ap.add_argument("--no-tags", action="store_true")
    ap.add_argument("--no-nav", action="store_true")
    ap.add_argument("--no-peers", action="store_true")
    ap.add_argument("--db", default="sniper_tracker.db")
    return ap


def _meta_entry(status, source, producer, **extra):
    import time
    return {
        "status": status,
        "captured_at": int(time.time()),
        "source": source,
        "producer": producer,
        **extra,
    }


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    wallet = args.wallet.lower()
    session = requests.Session()
    data = fetch_wallet_data(
        wallet, session=session, archetype_extractor=None,
        walk_full_history=args.full_history,
        with_capital_ledger=args.capital_ledger,
    )
    meta: dict[str, dict] = {}

    # Surface 1 — archetype (offline classifier)
    if args.no_archetype:
        meta["archetype_layer1"] = _meta_entry(
            "skipped", "cli flag --no-archetype",
            "wallet_archetype_extractor",
        )
    else:
        try:
            from scripts.wallet_archetype_extractor import (
                classify_wallet_archetype_from_snapshot,
                _fetch_tag_map,
                _fetch_wallet_tenure_metadata,
            )
            # Build tag_map ONCE here so the snapshot classifier doesn't
            # silently call _fetch_tag_map (which opens SQLite). Capture
            # script controls all DB access explicitly.
            cids_for_archetype = [
                str(r.get("conditionId") or "")
                for r in data.activity_rows[:500]
            ] + [
                str(p.get("conditionId") or "") for p in data.open_positions
            ]
            archetype_tag_map = _fetch_tag_map(cids_for_archetype)
            # Pre-fetch tenure_metadata too. Internally this hits live
            # HTTP (get_activity_page + Polymarket profile API) — which
            # is FINE here because we're in capture, not replay. The
            # whole point of pre-fetching is to materialize that
            # network-derived state ONCE, so the snapshot classifier
            # never has to call it itself. Without this step the
            # snapshot classifier would fall back to its `tenure_metadata
            # is None` branch and re-do the same network calls on every
            # replay run.
            archetype_tenure = _fetch_wallet_tenure_metadata(wallet)
            features = classify_wallet_archetype_from_snapshot(
                data.activity_rows[:500], data.open_positions,
                tag_map=archetype_tag_map,
                tenure_metadata=archetype_tenure,
                wallet=wallet, skip_gate0=True, with_execution=False,
            )
            data.archetype_layer1 = (
                features.get("classification", {}).get("archetype")
            )
            meta["archetype_layer1"] = _meta_entry(
                "captured",
                "scripts.wallet_archetype_extractor.classify_wallet_archetype_from_snapshot",
                "wallet_archetype_extractor (offline classifier; tag_map + tenure_metadata pre-fetched)",
                input_activity_rows=min(len(data.activity_rows), 500),
                tag_map_size=len(archetype_tag_map),
                tenure_lifetime_source=archetype_tenure.get("wallet_lifetime_source"),
                tenure_pagination_complete=bool(archetype_tenure.get("pagination_complete")),
            )
        except Exception as e:
            meta["archetype_layer1"] = _meta_entry(
                "failed", str(e)[:200],
                "wallet_archetype_extractor (offline classifier)",
            )

    # Surface 2 — execution profile
    if args.no_execution_profile:
        meta["execution_profile"] = _meta_entry(
            "skipped", "cli flag --no-execution-profile",
            "exec_profile_adapter.build_execution_profile",
        )
    else:
        try:
            ep = build_execution_profile(
                wallet, data.activity_rows, data.closed_positions,
                archetype=data.archetype_layer1,
            )
            data.execution_profile = ep
            meta["execution_profile"] = _meta_entry(
                "captured",
                "scripts.execution_profile.compute_execution_profile",
                "exec_profile_adapter.build_execution_profile",
                inner_status=(ep or {}).get("status"),
            )
        except Exception as e:
            meta["execution_profile"] = _meta_entry(
                "failed", str(e)[:200],
                "exec_profile_adapter.build_execution_profile",
            )

    # Tasks 5 + 6 add price_history_lookups, leafs_by_cid, nav_lookup,
    # peer_baselines. For now, leave them None and stamp status: "skipped"
    # placeholders so the meta block is exhaustive once those tasks land.
    for stub in (
        "price_history_lookups", "leafs_by_cid", "nav_lookup", "peer_baselines",
    ):
        meta[stub] = _meta_entry(
            "skipped", "not yet implemented (Task 5/6)", "n/a",
        )

    data.replay_surface_meta = meta
    save_fetch_snapshot(data, args.out)
    print(
        f"# wrote {args.out} (closed={len(data.closed_positions)} "
        f"open={len(data.open_positions)} "
        f"activity={len(data.activity_rows)})",
        file=sys.stderr,
    )
    return 0
```

(Adapt to the existing imports and existing `main` body — preserve any existing logging or error handling. The new structure is what matters; the precise lines depend on what's already there.)

Add the import for `build_execution_profile` at module top:

```python
from sniper.analysis.wallet_protocol.exec_profile_adapter import (
    build_execution_profile,
)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_record_fetch_snapshot_v2.py -q`

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/test_record_fetch_snapshot_v2.py scripts/record_fetch_snapshot.py
git commit -m "feat: capture archetype + execution_profile in record_fetch_snapshot v2"
```

---

## Task 5: Capture script — `price_history_lookups` enumeration

**Files:**
- Modify: `tests/test_record_fetch_snapshot_v2.py` (extend)
- Modify: `scripts/record_fetch_snapshot.py`

Enumerate the required `(asset_id, ts)` pairs from activity SELLs and reconstructed flips, call the post-Bug-4 `build_price_history_fn` once per pair, populate `WalletData.price_history_lookups` and meta. `--no-price-history` skip flag.

- [ ] **Step 1: Append failing tests to `tests/test_record_fetch_snapshot_v2.py`**

```python
def test_capture_price_history_for_sells_and_flips(
    tmp_path, synthetic_wallet_data,
):
    """Capture must request prices at sell_ts+24h for every SELL and at
    raw buy_ts/sell_ts for every reconstructed Flip (post-Bug-4 contract)."""
    requested: list[tuple[str, int]] = []

    def fake_price_history_fn(asset_id: str, ts: int) -> float:
        requested.append((asset_id, int(ts)))
        return 0.50

    def fake_build_price_history_fn(**_):
        return fake_price_history_fn

    out = tmp_path / "0xabc_test.json"
    with mock.patch.object(mod, "fetch_wallet_data",
                           return_value=synthetic_wallet_data), \
         mock.patch(
             "scripts.wallet_archetype_extractor."
             "classify_wallet_archetype_from_snapshot",
             return_value={"classification": {"archetype": "low_signal"}},
         ), \
         mock.patch.object(mod, "build_execution_profile",
                           return_value={"status": "populated"}), \
         mock.patch.object(mod, "build_price_history_fn",
                           side_effect=fake_build_price_history_fn):
        rc = mod.main(["0xabc", "--out", str(out)])
    assert rc == 0
    payload = json.loads(out.read_text())
    assert isinstance(payload["price_history_lookups"], dict)
    # SELL row at sell_ts=1700050000 → expect (asset_a, 1700050000+86400)
    assert ("asset_a", 1700050000 + 86400) in requested
    # Flip reconstructed from BUY 1700000000 / SELL 1700050000 → expect raw
    assert ("asset_a", 1700000000) in requested
    assert ("asset_a", 1700050000) in requested
    assert payload["replay_surface_meta"]["price_history_lookups"]["status"] == "captured"


def test_no_price_history_flag_skips_with_meta(tmp_path, synthetic_wallet_data):
    out = tmp_path / "0xabc_test.json"
    with mock.patch.object(mod, "fetch_wallet_data",
                           return_value=synthetic_wallet_data), \
         mock.patch(
             "scripts.wallet_archetype_extractor."
             "classify_wallet_archetype_from_snapshot",
             return_value={"classification": {"archetype": "low_signal"}},
         ), \
         mock.patch.object(mod, "build_execution_profile",
                           return_value={"status": "populated"}):
        rc = mod.main(["0xabc", "--out", str(out), "--no-price-history"])
    assert rc == 0
    payload = json.loads(out.read_text())
    assert payload["price_history_lookups"] is None
    assert payload["replay_surface_meta"]["price_history_lookups"]["status"] == "skipped"


def test_capture_price_history_empty_for_zero_sell_wallet(tmp_path):
    """Wallet with zero SELLs and zero flips → captured with empty dict."""
    data = WalletData(
        wallet="0xabc", closed_positions=[], open_positions=[],
        activity_rows=[
            {"type": "TRADE", "side": "BUY", "conditionId": "0xcid0",
             "asset": "asset_a", "timestamp": 1700000000, "price": 0.45,
             "size": 50, "outcome": "Yes"},
        ],
        activity_cap_hit=False, archetype_layer1=None, execution_profile=None,
        fetched_at=1700000000,
    )
    out = tmp_path / "0xabc_test.json"
    with mock.patch.object(mod, "fetch_wallet_data", return_value=data), \
         mock.patch(
             "scripts.wallet_archetype_extractor."
             "classify_wallet_archetype_from_snapshot",
             return_value={"classification": {"archetype": "low_signal"}},
         ), \
         mock.patch.object(mod, "build_execution_profile",
                           return_value={"status": "populated"}), \
         mock.patch.object(mod, "build_price_history_fn",
                           return_value=lambda *_a, **_kw: 0.5):
        rc = mod.main(["0xabc", "--out", str(out)])
    assert rc == 0
    payload = json.loads(out.read_text())
    # Empty captured surface, NOT None.
    assert payload["price_history_lookups"] == {}
    assert payload["replay_surface_meta"]["price_history_lookups"]["status"] == "captured"
    assert payload["replay_surface_meta"]["price_history_lookups"]["n_entries"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_record_fetch_snapshot_v2.py -q -k "price_history"`

Expected: 3 fail.

- [ ] **Step 3: Implement price-history capture in `record_fetch_snapshot.py`**

Replace the price_history `_meta_entry` stub from Task 4 with real capture logic. Add the import at module top:

```python
from sniper.analysis.wallet_protocol.flip_reconstruction import (
    reconstruct_flips,
)
from sniper.analysis.wallet_protocol.price_history_adapter import (
    build_price_history_fn,
)
```

Replace the `for stub in (...)` block from Task 4 with the price-history slice:

```python
# Surface 3 — price_history_lookups
H6_OFFSET = 24 * 3600  # mirrors held_analyst.H6_POST_SELL_OFFSET_SECONDS
if args.no_price_history:
    meta["price_history_lookups"] = _meta_entry(
        "skipped", "cli flag --no-price-history",
        "price_history_adapter.build_price_history_fn",
    )
else:
    try:
        price_fn = build_price_history_fn(session=session)
        required: set[tuple[str, int]] = set()
        # H6 inputs: every SELL row's (asset, sell_ts + 24h)
        for row in data.activity_rows:
            if str(row.get("side", "")).upper() != "SELL":
                continue
            asset = row.get("asset")
            ts = int(row.get("timestamp") or 0)
            if asset and ts:
                required.add((asset, ts + H6_OFFSET))
        # FH6 inputs: every Flip's (asset_id, buy_ts) and (asset_id, sell_ts)
        flips = reconstruct_flips(data).flips
        for f in flips:
            if f.asset_id:
                required.add((f.asset_id, int(f.buy_ts)))
                required.add((f.asset_id, int(f.sell_ts)))
        lookups: dict[str, float | None] = {}
        n_missing = 0
        for asset, ts in sorted(required):
            value = price_fn(asset, ts)
            lookups[f"{asset}|{ts}"] = value
            if value is None:
                n_missing += 1
        data.price_history_lookups = lookups
        meta["price_history_lookups"] = _meta_entry(
            "captured" if n_missing == 0 else "partial",
            "polymarket clob /prices-history",
            "price_history_adapter.build_price_history_fn",
            n_entries=len(lookups),
            n_missing=n_missing,
        )
    except Exception as e:
        meta["price_history_lookups"] = _meta_entry(
            "failed", str(e)[:200],
            "price_history_adapter.build_price_history_fn",
        )
```

Drop `"price_history_lookups"` from the `for stub in (...)` placeholder list.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_record_fetch_snapshot_v2.py -q`

Expected: previous 4 + new 3 = 7 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/test_record_fetch_snapshot_v2.py scripts/record_fetch_snapshot.py
git commit -m "feat: capture price_history_lookups (sells+24h, flip raw buy/sell)"
```

---

## Task 6: Capture script — `leafs_by_cid` + `nav_lookup` + `peer_baselines`

**Files:**
- Modify: `tests/test_record_fetch_snapshot_v2.py` (extend)
- Modify: `scripts/record_fetch_snapshot.py`

Three DB-backed surfaces. Tag/peer enumeration spans `closed_positions ∪ open_positions ∪ flip-derived condition_ids`. Three independent skip flags: `--no-tags`, `--no-nav`, `--no-peers`.

- [ ] **Step 1: Append failing tests**

```python
def test_capture_tags_includes_flip_only_condition_ids(tmp_path):
    """Tag enumeration must include condition_ids that appear only in
    reconstructed flips (not in closed_positions or open_positions).
    Spec rule from Codex blocker #3."""
    # Build a synthetic wallet: one closed position, one open, plus an
    # activity sequence that produces a flip on a THIRD condition_id.
    data = WalletData(
        wallet="0xabc",
        closed_positions=[
            {"conditionId": "0xcid_closed", "outcome": "Yes",
             "avgPrice": 0.45, "size": 50, "realizedPnl": 0.0,
             "endDate": "2026-04-01T00:00:00Z"},
        ],
        open_positions=[
            {"conditionId": "0xcid_open", "outcome": "Yes",
             "asset": "asset_open", "avgPrice": 0.5, "size": 100},
        ],
        activity_rows=[
            {"type": "TRADE", "side": "BUY", "conditionId": "0xcid_flip",
             "asset": "asset_flip", "timestamp": 1700000000, "price": 0.45,
             "size": 50, "outcome": "Yes"},
            {"type": "TRADE", "side": "SELL", "conditionId": "0xcid_flip",
             "asset": "asset_flip", "timestamp": 1700050000, "price": 0.50,
             "size": 50, "outcome": "Yes"},
        ],
        activity_cap_hit=False, archetype_layer1=None, execution_profile=None,
        fetched_at=1700000000,
    )

    captured_cids: list[list[str]] = []

    def fake_fetch_tag_lookups(conn, cids):
        captured_cids.append(sorted(cids))
        return ({c: ["politics"] for c in cids}, {"politics": "politics"})

    def fake_peer_baseline(conn, grain, tag, *, exclude_wallet, **kw):
        return (0.6, 50)

    out = tmp_path / "0xabc_test.json"
    with mock.patch.object(mod, "fetch_wallet_data", return_value=data), \
         mock.patch(
             "scripts.wallet_archetype_extractor."
             "classify_wallet_archetype_from_snapshot",
             return_value={"classification": {"archetype": "low_signal"}},
         ), \
         mock.patch.object(mod, "build_execution_profile",
                           return_value={"status": "populated"}), \
         mock.patch.object(mod, "build_price_history_fn",
                           return_value=lambda *_a, **_kw: 0.5), \
         mock.patch.object(mod, "fetch_tag_lookups_from_db",
                           side_effect=fake_fetch_tag_lookups), \
         mock.patch("db.peer_baseline_for_grain",
                    side_effect=fake_peer_baseline):
        rc = mod.main(["0xabc", "--out", str(out)])
    assert rc == 0
    assert captured_cids, "fetch_tag_lookups_from_db not called"
    cids_seen = set(captured_cids[0])
    assert {"0xcid_closed", "0xcid_open", "0xcid_flip"}.issubset(cids_seen)


def test_no_tags_no_nav_no_peers_flags_each_skip_independently(
    tmp_path, synthetic_wallet_data,
):
    out = tmp_path / "0xabc_test.json"
    with mock.patch.object(mod, "fetch_wallet_data",
                           return_value=synthetic_wallet_data), \
         mock.patch(
             "scripts.wallet_archetype_extractor."
             "classify_wallet_archetype_from_snapshot",
             return_value={"classification": {"archetype": "low_signal"}},
         ), \
         mock.patch.object(mod, "build_execution_profile",
                           return_value={"status": "populated"}), \
         mock.patch.object(mod, "build_price_history_fn",
                           return_value=lambda *_a, **_kw: 0.5):
        rc = mod.main([
            "0xabc", "--out", str(out),
            "--no-tags", "--no-nav", "--no-peers",
        ])
    assert rc == 0
    payload = json.loads(out.read_text())
    assert payload["leafs_by_cid"] is None
    assert payload["nav_lookup"] is None
    assert payload["peer_baselines"] is None
    for k in ("leafs_by_cid", "nav_lookup", "peer_baselines"):
        assert payload["replay_surface_meta"][k]["status"] == "skipped"


def test_peers_enabled_when_tags_and_nav_skipped(
    tmp_path, synthetic_wallet_data,
):
    """Regression: --no-tags --no-nav must not leave the peer-baselines
    branch's `cids` undefined. Capture script must compute cids ONCE
    up front."""
    requested: list[tuple[str, str]] = []

    def fake_fetch_tag_lookups(conn, cids):
        # Re-fetched inside the peers branch when leafs/navs were skipped
        return ({c: ["politics"] for c in cids},
                {"politics": "politics"})

    def fake_peer_baseline(conn, grain, tag, *, exclude_wallet, **kw):
        requested.append((grain, tag))
        return (0.6, 50)

    out = tmp_path / "0xabc_test.json"
    with mock.patch.object(mod, "fetch_wallet_data",
                           return_value=synthetic_wallet_data), \
         mock.patch(
             "scripts.wallet_archetype_extractor."
             "classify_wallet_archetype_from_snapshot",
             return_value={"classification": {"archetype": "low_signal"}},
         ), \
         mock.patch.object(mod, "build_execution_profile",
                           return_value={"status": "populated"}), \
         mock.patch.object(mod, "build_price_history_fn",
                           return_value=lambda *_a, **_kw: 0.5), \
         mock.patch.object(mod, "fetch_tag_lookups_from_db",
                           side_effect=fake_fetch_tag_lookups), \
         mock.patch("db.peer_baseline_for_grain",
                    side_effect=fake_peer_baseline):
        rc = mod.main([
            "0xabc", "--out", str(out),
            "--no-tags", "--no-nav",  # peers stays enabled
        ])
    assert rc == 0
    payload = json.loads(out.read_text())
    assert payload["leafs_by_cid"] is None
    assert payload["nav_lookup"] is None
    assert payload["peer_baselines"] is not None  # captured under peers
    assert requested, "peer_baseline_for_grain not invoked"


def test_peer_baselines_excludes_self_in_meta(
    tmp_path, synthetic_wallet_data,
):
    """Provenance must record exclude_wallet so future readers know the
    snapshot is per-wallet self-excluded."""
    def fake_fetch_tag_lookups(conn, cids):
        return (
            {c: ["politics"] for c in cids},
            {"politics": "politics"},
        )

    def fake_peer_baseline(conn, grain, tag, *, exclude_wallet, **kw):
        # Capture the exclude_wallet for later assertion via the meta.
        return (0.6, 50)

    out = tmp_path / "0xabc_test.json"
    with mock.patch.object(mod, "fetch_wallet_data",
                           return_value=synthetic_wallet_data), \
         mock.patch(
             "scripts.wallet_archetype_extractor."
             "classify_wallet_archetype_from_snapshot",
             return_value={"classification": {"archetype": "low_signal"}},
         ), \
         mock.patch.object(mod, "build_execution_profile",
                           return_value={"status": "populated"}), \
         mock.patch.object(mod, "build_price_history_fn",
                           return_value=lambda *_a, **_kw: 0.5), \
         mock.patch.object(mod, "fetch_tag_lookups_from_db",
                           side_effect=fake_fetch_tag_lookups), \
         mock.patch("db.peer_baseline_for_grain",
                    side_effect=fake_peer_baseline):
        rc = mod.main(["0xabc", "--out", str(out)])
    assert rc == 0
    payload = json.loads(out.read_text())
    assert payload["replay_surface_meta"]["peer_baselines"]["exclude_wallet"] == "0xabc"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_record_fetch_snapshot_v2.py -q -k "tags or nav or peer"`

Expected: 3 fail.

- [ ] **Step 3: Implement DB-surface capture**

Add imports to `scripts/record_fetch_snapshot.py`:

```python
import sqlite3

import db
from sniper.analysis.wallet_protocol.tag_inference import (
    enumerate_tag_grains,
    fetch_tag_lookups_from_db,
)
```

Replace the remaining placeholder stubs from Task 4 with real capture:

```python
# Surfaces 4-5-6: leafs_by_cid + nav_lookup + peer_baselines
need_db = not (args.no_tags and args.no_nav and args.no_peers)
conn = sqlite3.connect(args.db) if need_db else None

# Compute the condition-id universe ONCE up front so every DB-surface
# branch can reference it. Universe = closed ∪ open ∪ flip-derived.
# Always-defined; empty when wallet has no positions or activity.
flips = reconstruct_flips(data).flips
cids: set[str] = set()
for p in data.closed_positions:
    cid = p.get("conditionId")
    if cid:
        cids.add(cid)
for p in data.open_positions:
    cid = p.get("conditionId")
    if cid:
        cids.add(cid)
for f in flips:
    if f.condition_id:
        cids.add(f.condition_id)

if args.no_tags:
    meta["leafs_by_cid"] = _meta_entry(
        "skipped", "cli flag --no-tags",
        "tag_inference.fetch_tag_lookups_from_db",
    )
if args.no_nav:
    meta["nav_lookup"] = _meta_entry(
        "skipped", "cli flag --no-nav",
        "tag_inference.fetch_tag_lookups_from_db",
    )

if not args.no_tags or not args.no_nav:
    try:
        leafs_by_cid, nav_lookup = fetch_tag_lookups_from_db(conn, cids)
        if not args.no_tags:
            data.leafs_by_cid = leafs_by_cid
            meta["leafs_by_cid"] = _meta_entry(
                "captured",  # empty dict still 'captured'
                "db.fetch_market_tags_for_cids",
                "tag_inference.fetch_tag_lookups_from_db",
                db_path=args.db, n_entries=len(leafs_by_cid),
            )
        if not args.no_nav:
            data.nav_lookup = nav_lookup
            meta["nav_lookup"] = _meta_entry(
                "captured", "db.fetch_tag_nav_category_map",
                "tag_inference.fetch_tag_lookups_from_db",
                db_path=args.db, n_entries=len(nav_lookup),
            )
    except Exception as e:
        if not args.no_tags:
            meta["leafs_by_cid"] = _meta_entry(
                "failed", str(e)[:200],
                "tag_inference.fetch_tag_lookups_from_db",
            )
        if not args.no_nav:
            meta["nav_lookup"] = _meta_entry(
                "failed", str(e)[:200],
                "tag_inference.fetch_tag_lookups_from_db",
            )

if args.no_peers:
    meta["peer_baselines"] = _meta_entry(
        "skipped", "cli flag --no-peers",
        "db.peer_baseline_for_grain",
    )
else:
    try:
        # Reuse leafs/navs if Tasks 4-5 captured them; otherwise re-fetch.
        # `cids` is always defined (computed above), so this branch works
        # even when --no-tags --no-nav were both set.
        leafs = data.leafs_by_cid
        navs = data.nav_lookup
        if leafs is None or navs is None:
            l_re, n_re = fetch_tag_lookups_from_db(conn, cids)
            leafs = leafs if leafs is not None else l_re
            navs = navs if navs is not None else n_re
        positions_for_enum = list(data.closed_positions) + list(data.open_positions)
        for f in flips:
            positions_for_enum.append(
                {"conditionId": f.condition_id, "outcome": f.outcome},
            )
        enumeration = enumerate_tag_grains(positions_for_enum, leafs, navs)
        baselines: dict[str, list | None] = {}
        for tag in enumeration.leaf_grain_members.keys():
            result = db.peer_baseline_for_grain(
                conn, "leaf", tag, exclude_wallet=wallet,
            )
            baselines[f"leaf|{tag}"] = (
                [float(result[0]), int(result[1])] if result is not None else None
            )
        for nav in enumeration.nav_grain_members.keys():
            result = db.peer_baseline_for_grain(
                conn, "nav", nav, exclude_wallet=wallet,
            )
            baselines[f"nav|{nav}"] = (
                [float(result[0]), int(result[1])] if result is not None else None
            )
        data.peer_baselines = baselines
        meta["peer_baselines"] = _meta_entry(
            "captured", "db.peer_baseline_for_grain (exclude_wallet=<wallet>)",
            "db.peer_baseline_for_grain",
            db_path=args.db, exclude_wallet=wallet, n_entries=len(baselines),
        )
    except Exception as e:
        meta["peer_baselines"] = _meta_entry(
            "failed", str(e)[:200], "db.peer_baseline_for_grain",
        )

if conn is not None:
    conn.close()
```

(Verify the `enumerate_tag_grains` signature against `tag_inference.py`. The pseudocode assumes positions are dicts with `conditionId`; adjust to match the real signature.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_record_fetch_snapshot_v2.py -q`

Expected: all 10+ tests passed.

- [ ] **Step 5: Commit**

```bash
git add tests/test_record_fetch_snapshot_v2.py scripts/record_fetch_snapshot.py
git commit -m "feat: capture leafs_by_cid + nav_lookup + peer_baselines (flip-aware)"
```

---

## Task 7: Runner — `--from-fixture` no-live-fallback

**Files:**
- Test: `tests/test_replay_no_live_fallback.py` (create)
- Modify: `scripts/run_wallet_protocol.py`

When `--from-fixture` is set, the runner must NOT call any live producer. All hypothesis inputs come from the fixture's replay-surface fields via `build_fixture_closures`.

- [ ] **Step 1: Write failing test**

```python
# tests/test_replay_no_live_fallback.py
"""Under --from-fixture, the runner must not call any live producer.
Spec: docs/superpowers/specs/2026-05-01-fixture-v2-schema.md Rule 1.
"""
from __future__ import annotations

import json
import sqlite3
import unittest.mock as mock
from pathlib import Path

import pytest

from sniper.analysis.wallet_protocol.replay import save_fetch_snapshot
from sniper.analysis.wallet_protocol.types import WalletData


@pytest.fixture
def v2_fixture_path(tmp_path):
    data = WalletData(
        wallet="0xabc",
        closed_positions=[
            {"conditionId": "0xcid0", "outcome": "Yes", "avgPrice": 0.45,
             "size": 50, "realizedPnl": 25.0,
             "endDate": "2026-04-01T00:00:00Z"},
        ],
        open_positions=[],
        activity_rows=[],
        activity_cap_hit=False,
        archetype_layer1="speculator_bot",
        execution_profile={"status": "populated", "fills_with_drift": []},
        fetched_at=1700000000,
        price_history_lookups={},
        leafs_by_cid={"0xcid0": ["politics"]},
        nav_lookup={"politics": "politics"},
        peer_baselines={"leaf|politics": [0.6, 50]},
        replay_surface_meta={
            k: {"status": "captured", "captured_at": 1700000000,
                "source": "test", "producer": "test"}
            for k in (
                "archetype_layer1", "execution_profile",
                "price_history_lookups", "leafs_by_cid",
                "nav_lookup", "peer_baselines",
            )
        },
    )
    out = tmp_path / "v2_fixture.json"
    save_fetch_snapshot(data, out)
    return out


def test_from_fixture_does_not_call_live_producers(v2_fixture_path):
    """Trap every live producer; verify none fire under --from-fixture."""
    import scripts.run_wallet_protocol as runner

    def _trap(name):
        def _f(*a, **kw):
            raise AssertionError(
                f"live producer {name!r} was called under --from-fixture"
            )
        return _f

    with mock.patch.object(runner, "build_execution_profile",
                           side_effect=_trap("build_execution_profile")), \
         mock.patch.object(runner, "build_price_history_fn",
                           side_effect=_trap("build_price_history_fn")), \
         mock.patch.object(sqlite3, "connect",
                           side_effect=_trap("sqlite3.connect")), \
         mock.patch(
             "scripts.wallet_archetype_extractor.extract_wallet_archetype",
             side_effect=_trap("extract_wallet_archetype"),
         ):
        rc = runner.main([
            "0xabc",
            "--from-fixture", str(v2_fixture_path),
            "--seed", "1", "--n-iter", "200",
            "--json", "--no-persist",
        ])
    assert rc == 0


def test_from_fixture_uses_fixture_archetype(v2_fixture_path):
    """The runner must use data.archetype_layer1 from the fixture, not
    invoke the live extractor."""
    import scripts.run_wallet_protocol as runner

    with mock.patch(
        "scripts.wallet_archetype_extractor.extract_wallet_archetype",
        side_effect=AssertionError("extractor invoked under --from-fixture"),
    ):
        rc = runner.main([
            "0xabc",
            "--from-fixture", str(v2_fixture_path),
            "--seed", "1", "--n-iter", "200",
            "--json", "--no-persist",
        ])
    assert rc == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_replay_no_live_fallback.py -q`

Expected: both fail — current runner still calls live producers when `--no-*` flags are absent.

- [ ] **Step 3: Update `scripts/run_wallet_protocol.py`**

Around the existing `if args.from_fixture` branch (where `load_fetch_snapshot` is called), short-circuit ALL live producer logic when a fixture is loaded:

```python
if args.from_fixture:
    data = load_fetch_snapshot(args.from_fixture)
    # Optional: warn if --no-* flags also set (deprecated combination)
    if any([args.no_tags, args.no_peers, args.no_exec, args.no_history,
            args.no_archetype]):
        print(
            "# warning: --no-* flags are advisory under --from-fixture; "
            "fixture surface availability governs",
            file=sys.stderr,
        )
    # Print a summary of which surfaces are available so the operator
    # can see at a glance what the run can exercise.
    if data.replay_surface_meta:
        for surface, meta in data.replay_surface_meta.items():
            print(
                f"# replay surface {surface}: status="
                f"{meta.get('status', 'unknown')}",
                file=sys.stderr,
            )
    # Build fixture-backed closures from the loaded data; do NOT call any
    # live producer.
    price_history_fn, peer_baseline_fn = build_fixture_closures(data)
    leafs_by_cid = data.leafs_by_cid
    nav_lookup = data.nav_lookup
    archetype = data.archetype_layer1
    # data.execution_profile is already on data; protocol reads it directly.
else:
    # Existing live-fetch path (preserve untouched).
    data = fetch_wallet_data(...)
    # ... existing archetype, exec_profile, price_history, db lookups ...
```

The implementation must guarantee that the `--from-fixture` branch reaches `analyze_wallet(...)` without invoking ANY of: `build_execution_profile`, `build_price_history_fn`, `sqlite3.connect`, `fetch_tag_lookups_from_db`, `db.peer_baseline_for_grain`, `extract_wallet_archetype`. The cleanest pattern is an early `if args.from_fixture: ... return _run_protocol(data, ...)` followed by the existing live path as `else`.

Add the import for `build_fixture_closures` at module top:

```python
from sniper.analysis.wallet_protocol.replay import (
    build_fixture_closures, load_fetch_snapshot,
)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_replay_no_live_fallback.py tests/test_replay_determinism.py tests/calibration/test_calibration_set.py -q`

Expected: all green. The calibration harness still passes because the v1 fixtures' replay surfaces are `None`, so the closures are `None`, so H4/H5/H6/FH3/FH4/FH6 still suppress as before.

- [ ] **Step 5: Commit**

```bash
git add tests/test_replay_no_live_fallback.py scripts/run_wallet_protocol.py
git commit -m "feat: --from-fixture is strictly fixture-only — no live fallback"
```

---

## Task 8: Final regression

After all of the above land, the v2 calibration baseline must still be 10/10 (v1 fixtures' replay surfaces are `None` ⇒ no behavior change), the full suite must be green, and the schema validator + brain index must be clean.

**Files:** none modified by this task; output is a close-out audit doc.

- [ ] **Step 1: v2 schema + closures + capture + replay tests**

Run:

```bash
python3 -m pytest \
    tests/test_fixture_v2_schema.py \
    tests/test_fixture_closures.py \
    tests/test_archetype_offline_parity.py \
    tests/test_record_fetch_snapshot_v2.py \
    tests/test_replay_no_live_fallback.py \
    -q
```

Expected: all green (28-30 tests passed across the five files).

- [ ] **Step 2: Calibration harness + report**

Run:

```bash
python3 -m pytest tests/calibration/test_calibration_set.py -q
python3 scripts/calibration_report.py \
    --labels tests/calibration/labels.yaml \
    --out docs/audits/2026-04-30-calibration-report.md
echo "exit=$?"
```

Expected: 18 passed; exit=0; `match=10 miss=0 missing=0`. If any wallet's verdict shifts, INVESTIGATE before declaring 2B-capture done — v1 fixtures' replay surfaces should all be `None`, so closures are `None`, so behavior is unchanged from the 2B-fix close-out.

- [ ] **Step 3: Full suite + schema + brain index**

```bash
python3 -m pytest tests/ -q --tb=line
python3 scripts/validate_schema_refs.py
python3 scripts/brain_index.py generate
git status --short  # brain_index regen produces no diff
```

Expected: full suite ≥ previous baseline + the new v2 tests; validator clean; no `git diff` from `brain_index.py generate`.

- [ ] **Step 4: Write close-out audit**

Create `docs/audits/2026-05-01-phase-2b-capture-closeout.md` with:

* Summary of the seven implementation commits (SHAs).
* Confirmation that v1 calibration baseline is unchanged (still 10/10).
* Test counts: per-file new tests + total suite delta.
* Confirmation that Phase 2C (re-record calibration with v2 surfaces)
  is unblocked.

- [ ] **Step 5: Commit close-out**

```bash
git add docs/audits/2026-05-01-phase-2b-capture-closeout.md
git commit -m "audit: phase 2B-capture close-out — v2 fixture capability landed"
```

---

## Acceptance (overall)

- [ ] `WalletData` carries five new optional replay-surface fields.
- [ ] `FIXTURE_SCHEMA_VERSION = 2`. v1 fixtures still load.
- [ ] `replay_surface_meta` REQUIRED for v2 fixtures (loader raises on absence).
- [ ] `replay.build_fixture_closures` honors `None` vs `{}` rule.
- [ ] `wallet_archetype_extractor.classify_wallet_archetype_from_snapshot` exists; `extract_wallet_archetype` is a thin wrapper; parity test passes.
- [ ] `record_fetch_snapshot.py` captures all six surfaces with provenance metadata; six per-surface skip flags work; flip-derived condition_ids included in tag/peer enumeration.
- [ ] `--from-fixture` makes ZERO live calls (live producers + sqlite3.connect all trapped).
- [ ] v1 calibration baseline unchanged (10/10).
- [ ] Full suite green; schema validator clean; brain index clean.
- [ ] Close-out audit committed.

After all of the above, Phase 2C (re-record the 10 calibration fixtures as v2 and rerun the harness) is unblocked.
