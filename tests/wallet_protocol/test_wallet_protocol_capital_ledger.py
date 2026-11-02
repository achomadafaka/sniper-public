"""Tests for C1 capital_ledger aggregator.

Aggregates splits / merges / redemptions from the Goldsky activity-subgraph
into a deposit/withdrawal/redemption ledger denominated in USDC. Closes
wallet-protocol-bulletproofing C1.

HTTP is mocked at the `subgraph_reader.fetch_*` functions to keep these
tests hermetic.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from sniper.analysis.wallet_protocol.capital_ledger import (
    CapitalLedger,
    USDC_DECIMALS,
    compute_capital_ledger,
)
from sniper.analysis.wallet_protocol.ledger.collateral import (
    CLOB_V2_COLLATERAL_CUTOFF_TS,
    PUSD_ADDRESS,
    USDCE_ADDRESS,
)


def _split(amount, ts, sid="0xs1"):
    return {"id": sid, "timestamp": str(ts), "stakeholder": "0xw",
            "condition": "0xc", "amount": str(amount)}


def _merge(amount, ts, sid="0xm1"):
    return {"id": sid, "timestamp": str(ts), "stakeholder": "0xw",
            "condition": "0xc", "amount": str(amount)}


def _redemption(payout, ts, sid="0xr1"):
    return {"id": sid, "timestamp": str(ts), "redeemer": "0xw",
            "condition": "0xc", "indexSets": ["1"], "payout": str(payout)}


def test_usdc_decimals_is_six():
    """Sanity check the conversion constant matches Polymarket USDC ERC20."""
    assert USDC_DECIMALS == 6


def test_empty_inputs_yield_zero_ledger():
    with patch("sniper.analysis.wallet_protocol.capital_ledger.fetch_splits",
               return_value=[]), \
         patch("sniper.analysis.wallet_protocol.capital_ledger.fetch_merges",
               return_value=[]), \
         patch("sniper.analysis.wallet_protocol.capital_ledger.fetch_redemptions",
               return_value=[]):
        ledger = compute_capital_ledger("0xw")
    assert isinstance(ledger, CapitalLedger)
    assert ledger.n_splits == 0
    assert ledger.n_merges == 0
    assert ledger.n_redemptions == 0
    assert ledger.total_deposits_usdc == 0.0
    assert ledger.total_withdrawals_usdc == 0.0
    assert ledger.total_redemption_payout_usdc == 0.0
    assert ledger.net_capital_funded_usdc == 0.0
    assert ledger.first_event_ts is None
    assert ledger.last_event_ts is None
    assert ledger.events == []


def test_single_split_counts_as_deposit():
    """SPLIT amount=125_000_000_000 → $125,000 deposited."""
    splits = [_split(125_000_000_000, ts=1000)]
    with patch("sniper.analysis.wallet_protocol.capital_ledger.fetch_splits",
               return_value=splits), \
         patch("sniper.analysis.wallet_protocol.capital_ledger.fetch_merges",
               return_value=[]), \
         patch("sniper.analysis.wallet_protocol.capital_ledger.fetch_redemptions",
               return_value=[]):
        ledger = compute_capital_ledger("0xw")
    assert ledger.n_splits == 1
    assert ledger.total_deposits_usdc == 125_000.0
    assert ledger.net_capital_funded_usdc == 125_000.0


def test_single_merge_counts_as_withdrawal():
    merges = [_merge(50_000_000_000, ts=2000)]
    with patch("sniper.analysis.wallet_protocol.capital_ledger.fetch_splits",
               return_value=[]), \
         patch("sniper.analysis.wallet_protocol.capital_ledger.fetch_merges",
               return_value=merges), \
         patch("sniper.analysis.wallet_protocol.capital_ledger.fetch_redemptions",
               return_value=[]):
        ledger = compute_capital_ledger("0xw")
    assert ledger.n_merges == 1
    assert ledger.total_withdrawals_usdc == 50_000.0
    # Withdrawals subtract from net.
    assert ledger.net_capital_funded_usdc == -50_000.0


def test_redemption_payout_separate_from_deposit_withdraw():
    """Redemption payout is settlement value, NOT a withdrawal."""
    reds = [_redemption(10_000_000, ts=3000)]
    with patch("sniper.analysis.wallet_protocol.capital_ledger.fetch_splits",
               return_value=[]), \
         patch("sniper.analysis.wallet_protocol.capital_ledger.fetch_merges",
               return_value=[]), \
         patch("sniper.analysis.wallet_protocol.capital_ledger.fetch_redemptions",
               return_value=reds):
        ledger = compute_capital_ledger("0xw")
    assert ledger.n_redemptions == 1
    assert ledger.total_redemption_payout_usdc == 10.0
    assert ledger.total_deposits_usdc == 0.0
    assert ledger.total_withdrawals_usdc == 0.0
    assert ledger.net_capital_funded_usdc == 0.0


def test_mixed_events_aggregate_and_sort_by_timestamp():
    splits = [_split(200_000_000, ts=1000, sid="s1"),
              _split(300_000_000, ts=3000, sid="s2")]
    merges = [_merge(100_000_000, ts=2000, sid="m1")]
    reds = [_redemption(50_000_000, ts=4000, sid="r1")]
    with patch("sniper.analysis.wallet_protocol.capital_ledger.fetch_splits",
               return_value=splits), \
         patch("sniper.analysis.wallet_protocol.capital_ledger.fetch_merges",
               return_value=merges), \
         patch("sniper.analysis.wallet_protocol.capital_ledger.fetch_redemptions",
               return_value=reds):
        ledger = compute_capital_ledger("0xw")
    assert ledger.total_deposits_usdc == 500.0
    assert ledger.total_withdrawals_usdc == 100.0
    assert ledger.total_redemption_payout_usdc == 50.0
    assert ledger.net_capital_funded_usdc == 400.0
    assert ledger.first_event_ts == 1000
    assert ledger.last_event_ts == 4000
    # Events sorted ascending by timestamp.
    assert [e["timestamp"] for e in ledger.events] == [1000, 2000, 3000, 4000]
    # Each event tagged with kind so consumers can filter.
    kinds = [e["kind"] for e in ledger.events]
    assert kinds == ["split", "merge", "split", "redemption"]


def test_capital_ledger_events_carry_collateral_token_summary_across_cutover():
    pre = CLOB_V2_COLLATERAL_CUTOFF_TS - 60
    post = CLOB_V2_COLLATERAL_CUTOFF_TS + 60
    splits = [_split(100_000_000, ts=pre, sid="pre-split")]
    merges = [_merge(25_000_000, ts=post, sid="post-merge")]
    reds = [_redemption(10_000_000, ts=post, sid="post-redemption")]

    with patch("sniper.analysis.wallet_protocol.capital_ledger.fetch_splits",
               return_value=splits), \
         patch("sniper.analysis.wallet_protocol.capital_ledger.fetch_merges",
               return_value=merges), \
         patch("sniper.analysis.wallet_protocol.capital_ledger.fetch_redemptions",
               return_value=reds):
        ledger = compute_capital_ledger("0xw")

    by_id = {e["id"]: e for e in ledger.events}
    assert by_id["pre-split"]["collateral_token"] == "USDC.e"
    assert by_id["pre-split"]["collateral_token_address"] == USDCE_ADDRESS
    assert by_id["post-merge"]["collateral_token"] == "pUSD"
    assert by_id["post-merge"]["collateral_token_address"] == PUSD_ADDRESS
    assert by_id["post-redemption"]["collateral_token"] == "pUSD"

    assert ledger.collateral_summary["USDC.e"]["deposits_usdc"] == 100.0
    assert ledger.collateral_summary["pUSD"]["withdrawals_usdc"] == 25.0
    assert ledger.collateral_summary["pUSD"]["redemption_payout_usdc"] == 10.0


def test_amount_overflow_safe_for_large_split():
    """Polymarket has had whales splitting >$1M. BigInt strings → float
    must not lose precision at this scale."""
    splits = [_split(1_500_000_000_000, ts=1000)]  # $1.5M
    with patch("sniper.analysis.wallet_protocol.capital_ledger.fetch_splits",
               return_value=splits), \
         patch("sniper.analysis.wallet_protocol.capital_ledger.fetch_merges",
               return_value=[]), \
         patch("sniper.analysis.wallet_protocol.capital_ledger.fetch_redemptions",
               return_value=[]):
        ledger = compute_capital_ledger("0xw")
    assert ledger.total_deposits_usdc == 1_500_000.0


def test_capital_ledger_passes_session_through_to_fetch_functions():
    """compute_capital_ledger must thread session into all three fetches."""
    sentinel = object()
    with patch("sniper.analysis.wallet_protocol.capital_ledger.fetch_splits",
               return_value=[]) as ms, \
         patch("sniper.analysis.wallet_protocol.capital_ledger.fetch_merges",
               return_value=[]) as mm, \
         patch("sniper.analysis.wallet_protocol.capital_ledger.fetch_redemptions",
               return_value=[]) as mr:
        compute_capital_ledger("0xw", session=sentinel)
    assert ms.call_args.kwargs["session"] is sentinel
    assert mm.call_args.kwargs["session"] is sentinel
    assert mr.call_args.kwargs["session"] is sentinel
