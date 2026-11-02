"""Tests for fetch.py subgraph fallback completeness guard (fix #4c / #13).

The guard ensures we never replace a more-complete REST walk with a
token-lookup-limited (lossy) subgraph set.

Decision rule:
  - len(normalized) > len(activity_rest)  → use subgraph (genuinely recovered more)
  - len(normalized) <= len(activity_rest) → keep REST, record ACTIVITY_SUBGRAPH_SKIPPED
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
import requests

from sniper.analysis.wallet_protocol.fetch import _ACTIVITY_CAP, fetch_wallet_data
from sniper.analysis.wallet_protocol.fetch import _CLOB_USDC_TOKEN_ID

WALLET = "0xwallet"


def _rest_row(i: int, asset: str = "tok_yes") -> dict:
    return {
        "asset": asset,
        "conditionId": "cid_a",
        "outcome": "Yes",
        "side": "BUY",
        "price": "0.5",
        "size": "1.0",
        "timestamp": str(1_700_000_000 + i),
        "transactionHash": f"0x{i:08x}",
    }


def _fill(i: int, taker_asset: str = "tok_yes") -> dict:
    return {
        "id": f"0xfill{i}_0x00",
        "timestamp": str(1_699_000_000 + i),
        "maker": WALLET,
        "taker": "0xother",
        "makerAssetId": _CLOB_USDC_TOKEN_ID,
        "takerAssetId": taker_asset,
        "makerAmountFilled": "600000",
        "takerAmountFilled": "1000000",
        "fee": "0",
        "transactionHash": f"0xfill{i}",
    }


# ---------------------------------------------------------------------------
# Test 1: walk_full_history=True — REST walk is 30 000 rows (>> 3000 cap
# trigger). Subgraph returns 100 mapped fills. Must KEEP REST, record skip flag.
# ---------------------------------------------------------------------------

def test_rest_walk_beyond_cap_subgraph_skipped():
    """REST walk already returned 30 000 rows; subgraph returns fewer.

    Expected:
    - activity_rows == original REST rows (len 30 000)
    - subgraph_fallback_used is False
    - activity_cap_hit stays True (cap trigger fired, but we kept REST)
    - fetch_quality_flags contains ACTIVITY_SUBGRAPH_SKIPPED
    - fetch_quality_flags does NOT contain ACTIVITY_SOURCE:subgraph
    """
    n_rest = 30_000  # well beyond the 3000 hard-cap threshold
    rest_rows = [_rest_row(i) for i in range(n_rest)]
    # Subgraph returns far fewer mapped fills than REST rows
    subgraph_fills = [_fill(i) for i in range(100)]

    with patch("sniper.markets.polymarket_api.fetch_closed_positions", return_value=[]), \
         patch("sniper.markets.polymarket_api.fetch_user_positions", return_value=[]), \
         patch("sniper.markets.polymarket_api.fetch_user_activity", return_value=rest_rows), \
         patch("sniper.markets.polymarket_api.fetch_user_value", return_value=0.0), \
         patch(
             "sniper.analysis.wallet_protocol.fetch.fetch_order_filled_events",
             return_value=subgraph_fills,
         ), \
         patch("sniper.analysis.wallet_protocol.fetch.fetch_redemptions", return_value=[]), \
         patch("sniper.analysis.wallet_protocol.fetch.fetch_splits", return_value=[]), \
         patch("sniper.analysis.wallet_protocol.fetch.fetch_merges", return_value=[]):
        data = fetch_wallet_data(WALLET, requests.Session())

    # REST activity is preserved
    assert len(data.activity_rows) == n_rest, (
        f"REST walk rows must be kept; got {len(data.activity_rows)}"
    )
    # Subgraph did NOT replace REST
    assert data.subgraph_fallback_used is False
    # activity_cap_hit stays True — cap fired, but we didn't upgrade to subgraph
    assert data.activity_cap_hit is True
    # Skip flag present
    assert any("ACTIVITY_SUBGRAPH_SKIPPED" in f for f in data.fetch_quality_flags), (
        f"Expected ACTIVITY_SUBGRAPH_SKIPPED in flags; got {data.fetch_quality_flags}"
    )
    # No replacement flag
    assert not any(
        "ACTIVITY_SOURCE" in f and "subgraph" in f
        for f in data.fetch_quality_flags
    ), f"Should not have ACTIVITY_SOURCE:subgraph flag; got {data.fetch_quality_flags}"


# ---------------------------------------------------------------------------
# Test 2: Subgraph returns strictly MORE rows than REST cap.
# Must REPLACE REST, set subgraph_fallback_used=True.
# ---------------------------------------------------------------------------

def test_subgraph_larger_than_rest_replaces():
    """REST exactly at cap (3000 rows). Subgraph returns 5000 mapped fills.

    Expected:
    - activity_rows == subgraph rows (len 5000)
    - subgraph_fallback_used is True
    - activity_cap_hit set to False (subgraph overcame cap)
    - fetch_quality_flags contains ACTIVITY_SOURCE:subgraph
    """
    rest_rows = [_rest_row(i) for i in range(_ACTIVITY_CAP)]
    # Subgraph returns more rows than REST
    n_subgraph = 5000
    subgraph_fills = [_fill(i) for i in range(n_subgraph)]

    with patch("sniper.markets.polymarket_api.fetch_closed_positions", return_value=[]), \
         patch("sniper.markets.polymarket_api.fetch_user_positions", return_value=[]), \
         patch("sniper.markets.polymarket_api.fetch_user_activity", return_value=rest_rows), \
         patch("sniper.markets.polymarket_api.fetch_user_value", return_value=0.0), \
         patch(
             "sniper.analysis.wallet_protocol.fetch.fetch_order_filled_events",
             return_value=subgraph_fills,
         ), \
         patch("sniper.analysis.wallet_protocol.fetch.fetch_redemptions", return_value=[]), \
         patch("sniper.analysis.wallet_protocol.fetch.fetch_splits", return_value=[]), \
         patch("sniper.analysis.wallet_protocol.fetch.fetch_merges", return_value=[]):
        data = fetch_wallet_data(WALLET, requests.Session())

    # Subgraph rows replaced REST
    assert len(data.activity_rows) == n_subgraph, (
        f"Expected {n_subgraph} subgraph rows; got {len(data.activity_rows)}"
    )
    assert data.subgraph_fallback_used is True
    assert data.activity_cap_hit is False
    assert any(
        "ACTIVITY_SOURCE" in f and "subgraph" in f
        for f in data.fetch_quality_flags
    ), f"Expected ACTIVITY_SOURCE:subgraph flag; got {data.fetch_quality_flags}"
    # No skip flag
    assert not any("ACTIVITY_SUBGRAPH_SKIPPED" in f for f in data.fetch_quality_flags)


# ---------------------------------------------------------------------------
# Test 3: Skip flag records row counts accurately.
# ---------------------------------------------------------------------------

def test_subgraph_skipped_flag_includes_row_counts():
    """Skip flag text must contain rest_rows and subgraph_rows counts for diagnosis."""
    n_rest = 10_000
    rest_rows = [_rest_row(i) for i in range(n_rest)]
    subgraph_fills = [_fill(i) for i in range(50)]  # 50 mapped fills

    with patch("sniper.markets.polymarket_api.fetch_closed_positions", return_value=[]), \
         patch("sniper.markets.polymarket_api.fetch_user_positions", return_value=[]), \
         patch("sniper.markets.polymarket_api.fetch_user_activity", return_value=rest_rows), \
         patch("sniper.markets.polymarket_api.fetch_user_value", return_value=0.0), \
         patch(
             "sniper.analysis.wallet_protocol.fetch.fetch_order_filled_events",
             return_value=subgraph_fills,
         ), \
         patch("sniper.analysis.wallet_protocol.fetch.fetch_redemptions", return_value=[]), \
         patch("sniper.analysis.wallet_protocol.fetch.fetch_splits", return_value=[]), \
         patch("sniper.analysis.wallet_protocol.fetch.fetch_merges", return_value=[]):
        data = fetch_wallet_data(WALLET, requests.Session())

    skip_flags = [f for f in data.fetch_quality_flags if "ACTIVITY_SUBGRAPH_SKIPPED" in f]
    assert len(skip_flags) == 1
    flag = skip_flags[0]
    assert "10000" in flag or "rest_rows=10000" in flag, f"Flag must mention REST row count: {flag}"
    assert "50" in flag or "subgraph_rows=50" in flag, f"Flag must mention subgraph row count: {flag}"


# ---------------------------------------------------------------------------
# Test 4: n_unmapped_fills is still populated even when skip branch is taken.
# ---------------------------------------------------------------------------

def test_n_unmapped_fills_set_in_skip_branch():
    """n_unmapped_fills reflects unmapped token count even when REST is kept."""
    rest_rows = [_rest_row(i) for i in range(_ACTIVITY_CAP + 100)]
    # 1 mapped fill + 3 unmapped fills (unknown token, not in REST token_lookup)
    subgraph_fills = [
        _fill(0, taker_asset="tok_yes"),   # mapped
        _fill(1, taker_asset="unknown_1"),  # unmapped
        _fill(2, taker_asset="unknown_2"),  # unmapped
        _fill(3, taker_asset="unknown_3"),  # unmapped
    ]

    with patch("sniper.markets.polymarket_api.fetch_closed_positions", return_value=[]), \
         patch("sniper.markets.polymarket_api.fetch_user_positions", return_value=[]), \
         patch("sniper.markets.polymarket_api.fetch_user_activity", return_value=rest_rows), \
         patch("sniper.markets.polymarket_api.fetch_user_value", return_value=0.0), \
         patch(
             "sniper.analysis.wallet_protocol.fetch.fetch_order_filled_events",
             return_value=subgraph_fills,
         ), \
         patch("sniper.analysis.wallet_protocol.fetch.fetch_redemptions", return_value=[]), \
         patch("sniper.analysis.wallet_protocol.fetch.fetch_splits", return_value=[]), \
         patch("sniper.analysis.wallet_protocol.fetch.fetch_merges", return_value=[]):
        data = fetch_wallet_data(WALLET, requests.Session())

    assert data.subgraph_fallback_used is False
    assert data.n_unmapped_fills == 3, (
        f"Expected 3 unmapped fills; got {data.n_unmapped_fills}"
    )


# ---------------------------------------------------------------------------
# Test 5: REST exactly equal to subgraph → skip (not strictly larger).
# Boundary: len(normalized) == len(activity_rest) must keep REST.
# ---------------------------------------------------------------------------

def test_equal_row_counts_keeps_rest():
    """When REST == subgraph rows, REST is kept (not strictly larger)."""
    n = _ACTIVITY_CAP  # exactly 3000 rows both sides
    rest_rows = [_rest_row(i) for i in range(n)]
    subgraph_fills = [_fill(i) for i in range(n)]  # all map to tok_yes

    with patch("sniper.markets.polymarket_api.fetch_closed_positions", return_value=[]), \
         patch("sniper.markets.polymarket_api.fetch_user_positions", return_value=[]), \
         patch("sniper.markets.polymarket_api.fetch_user_activity", return_value=rest_rows), \
         patch("sniper.markets.polymarket_api.fetch_user_value", return_value=0.0), \
         patch(
             "sniper.analysis.wallet_protocol.fetch.fetch_order_filled_events",
             return_value=subgraph_fills,
         ), \
         patch("sniper.analysis.wallet_protocol.fetch.fetch_redemptions", return_value=[]), \
         patch("sniper.analysis.wallet_protocol.fetch.fetch_splits", return_value=[]), \
         patch("sniper.analysis.wallet_protocol.fetch.fetch_merges", return_value=[]):
        data = fetch_wallet_data(WALLET, requests.Session())

    assert data.subgraph_fallback_used is False
    assert len(data.activity_rows) == n
    assert any("ACTIVITY_SUBGRAPH_SKIPPED" in f for f in data.fetch_quality_flags)
