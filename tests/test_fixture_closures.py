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
