"""Tests: CapitalLedger flows WalletData → ProtocolReport → render_report.

Tests are hermetic — mocked ledger, no live HTTP.
"""
from __future__ import annotations
from unittest.mock import patch, MagicMock
from dataclasses import replace

import pytest

from sniper.analysis.wallet_protocol.capital_ledger import CapitalLedger
from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet
from sniper.analysis.wallet_protocol.report import render_report
from sniper.analysis.wallet_protocol.types import ProtocolReport, WalletData


def _minimal_wallet_data(wallet="0xtest") -> WalletData:
    """WalletData with enough closed positions to not fail R2 gate."""
    closed = [
        {"conditionId": f"cid_{i}", "outcome": "Yes",
         "realizedPnl": "10.0", "avgPrice": "0.5", "size": "20",
         "totalBought": "100", "endDate": "2025-01-01T00:00:00Z",
         "curPrice": "1.0"}
        for i in range(30)
    ]
    return WalletData(
        wallet=wallet,
        closed_positions=closed,
        open_positions=[],
        activity_rows=[],
        activity_cap_hit=False,
        archetype_layer1=None,
        execution_profile=None,
        fetched_at=1_700_000_000,
        capital_ledger=None,
        portfolio_value_usdc=None,
        fetch_quality_flags=[],
    )


def _sample_ledger(wallet="0xtest") -> CapitalLedger:
    return CapitalLedger(
        wallet=wallet,
        n_splits=10,
        n_merges=3,
        n_redemptions=5,
        total_deposits_usdc=10_000.0,
        total_withdrawals_usdc=2_000.0,
        total_redemption_payout_usdc=1_500.0,
        net_capital_funded_usdc=8_000.0,
        first_event_ts=1_600_000_000,
        last_event_ts=1_700_000_000,
        events=[],
    )


def test_capital_ledger_stored_on_protocol_report():
    data = _minimal_wallet_data()
    ledger = _sample_ledger()
    data = replace(data, capital_ledger=ledger)
    report = analyze_wallet("0xtest", pre_fetched_data=data)
    assert report.capital_ledger is ledger
    assert report.capital_ledger.net_capital_funded_usdc == 8_000.0


def test_capital_ledger_none_when_not_provided():
    data = _minimal_wallet_data()
    report = analyze_wallet("0xtest", pre_fetched_data=data)
    assert report.capital_ledger is None


def test_render_report_shows_capital_ledger():
    report = ProtocolReport(wallet="0xtest", analyzed_at=1_700_000_000)
    report.capital_ledger = _sample_ledger()
    md = render_report(report)
    assert "net_capital_funded" in md
    assert "8000" in md or "8,000" in md


def test_render_report_shows_collateral_token_summary():
    report = ProtocolReport(wallet="0xtest", analyzed_at=1_700_000_000)
    report.capital_ledger = CapitalLedger(
        wallet="0xtest",
        n_splits=1,
        n_merges=1,
        n_redemptions=0,
        total_deposits_usdc=100.0,
        total_withdrawals_usdc=25.0,
        total_redemption_payout_usdc=0.0,
        net_capital_funded_usdc=75.0,
        collateral_summary={
            "USDC.e": {
                "n_events": 1,
                "deposits_usdc": 100.0,
                "withdrawals_usdc": 0.0,
                "redemption_payout_usdc": 0.0,
            },
            "pUSD": {
                "n_events": 1,
                "deposits_usdc": 0.0,
                "withdrawals_usdc": 25.0,
                "redemption_payout_usdc": 0.0,
            },
        },
    )

    md = render_report(report)

    assert "collateral_tokens" in md
    assert "USDC.e" in md
    assert "pUSD" in md


def test_render_report_shows_net_ctf_flow_diagnostic_for_net_withdrawals():
    report = ProtocolReport(wallet="0xtest", analyzed_at=1_700_000_000)
    report.net_pnl = 3_000.0
    report.capital_ledger = CapitalLedger(
        wallet="0xtest",
        n_splits=1,
        n_merges=2,
        n_redemptions=0,
        total_deposits_usdc=2_000.0,
        total_withdrawals_usdc=5_000.0,
        total_redemption_payout_usdc=0.0,
        net_capital_funded_usdc=-3_000.0,
    )

    md = render_report(report)

    assert "realized_pnl_to_net_ctf_flow" in md
    assert "net_withdrawn" in md


def test_render_report_omits_ledger_section_when_none():
    report = ProtocolReport(wallet="0xtest", analyzed_at=1_700_000_000)
    assert report.capital_ledger is None
    md = render_report(report)
    assert "net_capital_funded" not in md


def test_fetch_wallet_data_with_capital_ledger_calls_compute():
    import requests
    from sniper.analysis.wallet_protocol.fetch import fetch_wallet_data
    mock_ledger = _sample_ledger()
    with patch("sniper.markets.polymarket_api.fetch_closed_positions", return_value=[]), \
         patch("sniper.markets.polymarket_api.fetch_user_positions", return_value=[]), \
         patch("sniper.markets.polymarket_api.fetch_user_activity", return_value=[]), \
         patch("sniper.markets.polymarket_api.fetch_user_value", return_value=0.0), \
         patch(
             "sniper.analysis.wallet_protocol.fetch.compute_capital_ledger",
             return_value=mock_ledger,
         ) as mock_cl:
        data = fetch_wallet_data("0xtest", requests.Session(), with_capital_ledger=True)
    mock_cl.assert_called_once()
    assert data.capital_ledger is mock_ledger


def test_fetch_wallet_data_no_ledger_by_default():
    import requests
    from sniper.analysis.wallet_protocol.fetch import fetch_wallet_data
    with patch("sniper.markets.polymarket_api.fetch_closed_positions", return_value=[]), \
         patch("sniper.markets.polymarket_api.fetch_user_positions", return_value=[]), \
         patch("sniper.markets.polymarket_api.fetch_user_activity", return_value=[]), \
         patch("sniper.markets.polymarket_api.fetch_user_value", return_value=0.0), \
         patch(
             "sniper.analysis.wallet_protocol.fetch.compute_capital_ledger",
         ) as mock_cl:
        data = fetch_wallet_data("0xtest", requests.Session())
    mock_cl.assert_not_called()
    assert data.capital_ledger is None
