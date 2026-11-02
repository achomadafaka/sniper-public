"""Tests: ±2% accounting tie in analyze_wallet."""
from __future__ import annotations
from dataclasses import replace

import pytest

from sniper.analysis.wallet_protocol.capital_ledger import CapitalLedger
from sniper.analysis.wallet_protocol.analyze_wallet import _compute_accounting_tie
from sniper.analysis.wallet_protocol.types import ProtocolReport, WalletData


def _ledger(net=10_000.0) -> CapitalLedger:
    return CapitalLedger(
        wallet="0xtest",
        net_capital_funded_usdc=net,
        total_deposits_usdc=net + 2_000.0,
        total_withdrawals_usdc=2_000.0,
        total_redemption_payout_usdc=0.0,
        n_splits=5, n_merges=2, n_redemptions=0,
    )


def _open_pos(cur_price, avg_price, size):
    return {"curPrice": str(cur_price), "avgPrice": str(avg_price), "size": str(size)}


def test_current_value_cross_check_emits_open_mark_ok():
    """Current `/value` parity is checked against open mark, not lifetime PnL."""
    ledger = _ledger(net=10_000.0)
    open_positions = [_open_pos(0.7, 0.5, 100.0)]  # open mark = 70
    flags = _compute_accounting_tie(ledger, 1_000.0, open_positions, 75.0)
    assert not any("ACCOUNTING_TIE_FAILED" in f for f in flags)
    assert any("PORTFOLIO_VALUE_OPEN_MARK_OK" in f for f in flags)
    assert any("ACCOUNTING_TIE_UNSUPPORTED" in f for f in flags)


def test_current_value_cross_check_fails_when_open_mark_exceeds_value():
    """Open mark materially above `/value` is a current-value mismatch."""
    ledger = _ledger(net=10_000.0)
    open_positions = [_open_pos(0.7, 0.5, 100.0)]  # open mark = 70
    flags = _compute_accounting_tie(ledger, 5_000.0, open_positions, 50.0)
    assert any("PORTFOLIO_VALUE_OPEN_MARK_FAILED" in f for f in flags)
    fail_flag = next(f for f in flags if "PORTFOLIO_VALUE_OPEN_MARK_FAILED" in f)
    assert "portfolio_value" in fail_flag
    assert "open_mark" in fail_flag
    assert "delta_pct" in fail_flag


def test_current_portfolio_value_matching_open_mark_is_not_accounting_failure():
    """`/value` is current holdings, so open mark parity is not a lifetime tie failure."""
    ledger = _ledger(net=33_555.52)
    open_positions = [
        _open_pos(0.75, 0.50, 1_000_000.0),  # open_value=750k, unrealized=250k
    ]
    flags = _compute_accounting_tie(
        ledger,
        556_810.27,
        open_positions,
        750_009.44,
    )
    assert not any("ACCOUNTING_TIE_FAILED" in f for f in flags)
    assert any("PORTFOLIO_VALUE_OPEN_MARK_OK" in f for f in flags)
    assert any("ACCOUNTING_TIE_UNSUPPORTED" in f for f in flags)


def test_current_value_diagnostic_carries_observed_timestamp():
    flags = _compute_accounting_tie(
        _ledger(net=33_555.52),
        556_810.27,
        [_open_pos(0.75, 0.50, 1_000_000.0)],
        750_009.44,
        portfolio_value_observed_at=1_777_673_439,
    )
    assert any("observed_at=1777673439" in f for f in flags)


def test_tie_skipped_when_no_ledger():
    flags = _compute_accounting_tie(None, 1000.0, [], 11_000.0)
    assert flags == []


def test_tie_skipped_when_no_portfolio_value():
    flags = _compute_accounting_tie(_ledger(), 1000.0, [], None)
    assert flags == []


def test_tie_shows_discrepancy_details():
    ledger = _ledger(net=5_000.0)
    # estimated = 5000, actual = 1000 → ~400% off
    flags = _compute_accounting_tie(ledger, 0.0, [], 1_000.0)
    unsupported_flag = next(
        (f for f in flags if "ACCOUNTING_TIE_UNSUPPORTED" in f), None
    )
    assert unsupported_flag is not None
    # Either raw number or formatted with commas
    assert "5000" in unsupported_flag.replace(",", "") or "5,000" in unsupported_flag
    assert "1000" in unsupported_flag.replace(",", "") or "1,000" in unsupported_flag


def test_accounting_tie_flag_on_report():
    """ACCOUNTING_TIE flag must appear in report.data_quality_flags."""
    from unittest.mock import patch
    from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet

    closed = [
        {"conditionId": f"cid_{i}", "outcome": "Yes", "realizedPnl": "10.0",
         "avgPrice": "0.5", "size": "20", "totalBought": "100",
         "endDate": "2025-01-01T00:00:00Z", "curPrice": "1.0"}
        for i in range(30)
    ]
    data = WalletData(
        wallet="0xtest",
        closed_positions=closed,
        open_positions=[],
        activity_rows=[],
        activity_cap_hit=False,
        archetype_layer1=None,
        execution_profile=None,
        fetched_at=1_700_000_000,
        capital_ledger=_ledger(net=10_000.0),
        portfolio_value_usdc=10_300.0,
        fetch_quality_flags=[],
    )
    report = analyze_wallet("0xtest", pre_fetched_data=data)
    tie_flags = [f for f in report.data_quality_flags if "ACCOUNTING_TIE" in f]
    assert len(tie_flags) == 1


def test_analyze_wallet_populates_open_dead_header_count():
    from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet
    from sniper.analysis.wallet_protocol.report import render_report

    closed = [
        {"conditionId": f"cid_{i}", "outcome": "Yes", "realizedPnl": "10.0",
         "avgPrice": "0.5", "size": "20", "totalBought": "100",
         "endDate": "2025-01-01T00:00:00Z", "curPrice": "1.0"}
        for i in range(30)
    ]
    open_positions = (
        [{"curPrice": "0.0", "size": "100", "avgPrice": "0.5"} for _ in range(57)]
        + [{"curPrice": "0.5", "size": "100", "avgPrice": "0.5"} for _ in range(13)]
    )
    data = WalletData(
        wallet="0xtest",
        closed_positions=closed,
        open_positions=open_positions,
        activity_rows=[],
        activity_cap_hit=False,
        archetype_layer1=None,
        execution_profile=None,
        fetched_at=1_700_000_000,
        capital_ledger=None,
        portfolio_value_usdc=None,
        fetch_quality_flags=[],
    )

    report = analyze_wallet("0xtest", pre_fetched_data=data)

    assert report.n_open_dead == 57
    assert "**n_open_dead:** 57" in render_report(report)
