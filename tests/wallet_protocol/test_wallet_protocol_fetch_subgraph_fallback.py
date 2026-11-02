"""Tests: fetch.py subgraph fallback when activity_cap_hit=True."""
from __future__ import annotations
from unittest.mock import patch

import pytest

from sniper.analysis.wallet_protocol.fetch import (
    _build_token_lookup,
    _normalize_order_fill,
    _CLOB_USDC_TOKEN_ID,
    _ACTIVITY_CAP,
)

WALLET = "0xwallet"
TOKEN_LOOKUP = {
    "tok_yes": ("cid_a", "Yes"),
    "tok_no": ("cid_a", "No"),
}


def _fill(maker_asset, taker_asset, maker_amt, taker_amt, ts="1700000000", tx="0xabc"):
    return {
        "id": f"{tx}_0x00",
        "timestamp": ts,
        "maker": WALLET,
        "taker": "0xother",
        "makerAssetId": maker_asset,
        "takerAssetId": taker_asset,
        "makerAmountFilled": str(maker_amt),
        "takerAmountFilled": str(taker_amt),
        "fee": "0",
        "transactionHash": tx,
    }


def _fill_taker(maker_asset, taker_asset, maker_amt, taker_amt, tx="0xdef"):
    return {
        "id": f"{tx}_0x00",
        "timestamp": "1700000001",
        "maker": "0xother",
        "taker": WALLET,
        "makerAssetId": maker_asset,
        "takerAssetId": taker_asset,
        "makerAmountFilled": str(maker_amt),
        "takerAmountFilled": str(taker_amt),
        "fee": "0",
        "transactionHash": tx,
    }


# _build_token_lookup tests

def test_build_token_lookup_extracts_asset_conditionid_outcome():
    rows = [
        {"asset": "tok_yes_a", "conditionId": "cid_a", "outcome": "Yes"},
        {"asset": "tok_no_a", "conditionId": "cid_a", "outcome": "No"},
        {"asset": "tok_yes_b", "conditionId": "cid_b", "outcome": "Yes"},
    ]
    lookup = _build_token_lookup(rows)
    assert lookup["tok_yes_a"] == ("cid_a", "Yes")
    assert lookup["tok_no_a"] == ("cid_a", "No")
    assert lookup["tok_yes_b"] == ("cid_b", "Yes")


def test_build_token_lookup_skips_rows_without_asset():
    rows = [
        {"conditionId": "cid_a", "outcome": "Yes"},
        {"asset": "", "conditionId": "cid_a", "outcome": "Yes"},
    ]
    assert _build_token_lookup(rows) == {}


def test_build_token_lookup_skips_usdc_token():
    rows = [
        {"asset": _CLOB_USDC_TOKEN_ID, "conditionId": "cid_a", "outcome": "Yes"},
        {"asset": "tok_yes", "conditionId": "cid_a", "outcome": "Yes"},
    ]
    lookup = _build_token_lookup(rows)
    assert _CLOB_USDC_TOKEN_ID not in lookup
    assert "tok_yes" in lookup


# _normalize_order_fill tests — maker perspective

def test_normalize_maker_buys_yes():
    fill = _fill(_CLOB_USDC_TOKEN_ID, "tok_yes", 600_000, 1_000_000)
    row = _normalize_order_fill(fill, WALLET, TOKEN_LOOKUP)
    assert row is not None
    assert row["side"] == "BUY"
    assert row["conditionId"] == "cid_a"
    assert row["outcome"] == "Yes"
    assert row["asset"] == "tok_yes"
    assert abs(row["price"] - 0.6) < 1e-9
    assert abs(row["size"] - 1.0) < 1e-9
    assert row["timestamp"] == 1_700_000_000
    assert row["transactionHash"] == "0xabc"


def test_normalize_maker_sells_yes():
    fill = _fill("tok_yes", _CLOB_USDC_TOKEN_ID, 2_000_000, 1_400_000)
    row = _normalize_order_fill(fill, WALLET, TOKEN_LOOKUP)
    assert row is not None
    assert row["side"] == "SELL"
    assert row["outcome"] == "Yes"
    assert abs(row["price"] - 0.7) < 1e-9
    assert abs(row["size"] - 2.0) < 1e-9


# _normalize_order_fill tests — taker perspective

def test_normalize_taker_buys_yes():
    fill = _fill_taker("tok_yes", _CLOB_USDC_TOKEN_ID, 1_000_000, 500_000)
    row = _normalize_order_fill(fill, WALLET, TOKEN_LOOKUP)
    assert row is not None
    assert row["side"] == "BUY"
    assert row["outcome"] == "Yes"
    assert abs(row["price"] - 0.5) < 1e-9
    assert abs(row["size"] - 1.0) < 1e-9


def test_normalize_taker_sells_yes():
    fill = _fill_taker(_CLOB_USDC_TOKEN_ID, "tok_yes", 800_000, 1_000_000)
    row = _normalize_order_fill(fill, WALLET, TOKEN_LOOKUP)
    assert row is not None
    assert row["side"] == "SELL"
    assert abs(row["price"] - 0.8) < 1e-9


def test_normalize_unknown_token_returns_none():
    fill = _fill(_CLOB_USDC_TOKEN_ID, "unknown_tok", 100_000, 200_000)
    assert _normalize_order_fill(fill, WALLET, TOKEN_LOOKUP) is None


def test_normalize_both_usdc_returns_none():
    fill = _fill(_CLOB_USDC_TOKEN_ID, _CLOB_USDC_TOKEN_ID, 100_000, 100_000)
    assert _normalize_order_fill(fill, WALLET, TOKEN_LOOKUP) is None


def test_normalize_zero_outcome_amount_returns_none():
    fill = _fill(_CLOB_USDC_TOKEN_ID, "tok_yes", 100_000, 0)
    assert _normalize_order_fill(fill, WALLET, TOKEN_LOOKUP) is None


# Integration: fetch_wallet_data subgraph fallback

def test_fetch_keeps_rest_when_subgraph_not_larger():
    # 3000 REST rows vs a subgraph that normalizes to 1 mapped row → subgraph is
    # NOT larger, so the merge guard keeps the (more complete) REST walk and
    # records ACTIVITY_SUBGRAPH_SKIPPED rather than replacing it.
    import requests
    from sniper.analysis.wallet_protocol.fetch import fetch_wallet_data

    rest_rows = [
        {"asset": "tok_yes", "conditionId": "cid_a", "outcome": "Yes",
         "side": "BUY", "price": "0.6", "size": "1.0",
         "timestamp": str(1_700_000_000 + i), "transactionHash": f"0x{i:04x}"}
        for i in range(_ACTIVITY_CAP)
    ]
    subgraph_fills = [
        {
            "id": "0xfill1_0x00",
            "timestamp": "1699000000",
            "maker": "0xwallet",
            "taker": "0xother",
            "makerAssetId": _CLOB_USDC_TOKEN_ID,
            "takerAssetId": "tok_yes",
            "makerAmountFilled": "600000",
            "takerAmountFilled": "1000000",
            "fee": "0",
            "transactionHash": "0xfill1",
        },
        {
            "id": "0xfill2_0x00",
            "timestamp": "1699000001",
            "maker": "0xwallet",
            "taker": "0xother",
            "makerAssetId": _CLOB_USDC_TOKEN_ID,
            "takerAssetId": "unknown_tok",
            "makerAmountFilled": "200000",
            "takerAmountFilled": "500000",
            "fee": "0",
            "transactionHash": "0xfill2",
        },
    ]

    with patch("sniper.markets.polymarket_api.fetch_closed_positions", return_value=[]), \
         patch("sniper.markets.polymarket_api.fetch_user_positions", return_value=[]), \
         patch("sniper.markets.polymarket_api.fetch_user_activity", return_value=rest_rows), \
         patch("sniper.markets.polymarket_api.fetch_user_value", return_value=0.0), \
         patch(
             "sniper.analysis.wallet_protocol.fetch.fetch_order_filled_events",
             return_value=subgraph_fills,
         ):
        data = fetch_wallet_data("0xwallet", requests.Session())

    # REST walk kept (subgraph would lose data); cap_hit stays True, no replacement
    assert any(r.get("conditionId") == "cid_a" for r in data.activity_rows)
    assert data.subgraph_fallback_used is False
    assert any("ACTIVITY_SUBGRAPH_SKIPPED" in f for f in data.fetch_quality_flags)
    assert not any("ACTIVITY_SOURCE:subgraph" in f for f in data.fetch_quality_flags)


def test_fetch_no_subgraph_when_cap_not_hit():
    import requests
    from sniper.analysis.wallet_protocol.fetch import fetch_wallet_data

    rest_rows = [
        {"asset": "tok_yes", "conditionId": "cid_a", "outcome": "Yes",
         "side": "BUY", "price": "0.6", "size": "1.0",
         "timestamp": "1700000000", "transactionHash": "0x0000"}
    ]

    with patch("sniper.markets.polymarket_api.fetch_closed_positions", return_value=[]), \
         patch("sniper.markets.polymarket_api.fetch_user_positions", return_value=[]), \
         patch("sniper.markets.polymarket_api.fetch_user_activity", return_value=rest_rows), \
         patch("sniper.markets.polymarket_api.fetch_user_value", return_value=0.0), \
         patch(
             "sniper.analysis.wallet_protocol.fetch.fetch_order_filled_events",
         ) as mock_sg:
        data = fetch_wallet_data("0xwallet", requests.Session())

    mock_sg.assert_not_called()
    assert not any("ACTIVITY_SOURCE" in f for f in data.fetch_quality_flags)


# ---------------------------------------------------------------------------
# CTF event-stream fetch tests (spec §8.3, §8.7)
# ---------------------------------------------------------------------------

def test_redemptions_splits_merges_happy_path():
    """§8.7 happy-path: mocked readers return non-empty lists; fields populated
    on WalletData (spec §8.3). Round-trip is separately covered by
    test_fixture_v2_schema.py::test_redemptions_splits_merges_none_survives_round_trip."""
    import requests
    from sniper.analysis.wallet_protocol.fetch import fetch_wallet_data

    red_events = [{"condition": "0xcond1", "payout": "1000000"}]
    split_events = [{"condition": "0xcond2", "amount": "500000"}]
    merge_events = [{"condition": "0xcond3", "amount": "300000"}]

    with patch("sniper.markets.polymarket_api.fetch_user_positions", return_value=[]), \
         patch("sniper.markets.polymarket_api.fetch_user_activity", return_value=[]), \
         patch("sniper.markets.polymarket_api.fetch_user_value", return_value=0.0), \
         patch(
             "sniper.analysis.wallet_protocol.fetch.fetch_redemptions",
             return_value=red_events,
         ), \
         patch(
             "sniper.analysis.wallet_protocol.fetch.fetch_splits",
             return_value=split_events,
         ), \
         patch(
             "sniper.analysis.wallet_protocol.fetch.fetch_merges",
             return_value=merge_events,
         ):
        data = fetch_wallet_data("0xwallet", requests.Session())

    assert data.redemptions == red_events
    assert data.splits == split_events
    assert data.merges == merge_events
    assert not any("REDEMPTIONS_FETCH_FAILED" in f for f in data.fetch_quality_flags)
    assert not any("SPLITS_FETCH_FAILED" in f for f in data.fetch_quality_flags)
    assert not any("MERGES_FETCH_FAILED" in f for f in data.fetch_quality_flags)


def test_redemptions_splits_merges_failure_isolation():
    """§8.7 failure isolation: one reader raises; only that field becomes None,
    the other two remain populated; fetch_quality_flags gains a named flag."""
    import requests
    from sniper.analysis.wallet_protocol.fetch import fetch_wallet_data

    split_events = [{"condition": "0xcond2", "amount": "500000"}]
    merge_events = [{"condition": "0xcond3", "amount": "300000"}]

    with patch("sniper.markets.polymarket_api.fetch_user_positions", return_value=[]), \
         patch("sniper.markets.polymarket_api.fetch_user_activity", return_value=[]), \
         patch("sniper.markets.polymarket_api.fetch_user_value", return_value=0.0), \
         patch(
             "sniper.analysis.wallet_protocol.fetch.fetch_redemptions",
             side_effect=RuntimeError("subgraph timeout"),
         ), \
         patch(
             "sniper.analysis.wallet_protocol.fetch.fetch_splits",
             return_value=split_events,
         ), \
         patch(
             "sniper.analysis.wallet_protocol.fetch.fetch_merges",
             return_value=merge_events,
         ):
        data = fetch_wallet_data("0xwallet", requests.Session())

    # Only redemptions is None; the others remain populated
    assert data.redemptions is None, "Failed fetch must produce None, not []"
    assert data.splits == split_events
    assert data.merges == merge_events
    # fetch_quality_flags must name the failure
    assert any("REDEMPTIONS_FETCH_FAILED" in f for f in data.fetch_quality_flags)
    assert not any("SPLITS_FETCH_FAILED" in f for f in data.fetch_quality_flags)
    assert not any("MERGES_FETCH_FAILED" in f for f in data.fetch_quality_flags)


def test_redemptions_splits_merges_absent_in_old_fixture():
    """§8.7 old-fixture compat: deserializing a pre-surface fixture (no
    redemptions/splits/merges keys) yields all three as None."""
    from sniper.analysis.wallet_protocol.replay import deserialize_wallet_data

    payload = {
        "schema_version": 2,
        "wallet": "0xlegacy",
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
        # Note: NO redemptions / splits / merges keys — pre-surface fixture
        "replay_surface_meta": {},
    }
    restored = deserialize_wallet_data(payload)
    assert restored.redemptions is None
    assert restored.splits is None
    assert restored.merges is None
