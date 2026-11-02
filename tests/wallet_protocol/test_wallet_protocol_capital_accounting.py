"""Tests: collateral-aware capital accounting and capital_summary surface.

Covers:
  (a) _amount_usdc / compute_capital_ledger excludes non-USDC collateral events
      from USDC totals and records them on CapitalLedger.n_non_usdc_collateral_events.
  (b) capital_summary uses initialValue (ADR-004 correct cost basis), not totalBought.
  (c) capital_summary data_gaps flags missing ledger and missing holdings_value.

All tests are hermetic — no live HTTP.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from sniper.analysis.wallet_protocol.capital_ledger import (
    CapitalLedger,
    capital_summary,
    compute_capital_ledger,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _split(amount: int, ts: int, sid: str = "0xs1") -> dict:
    """Minimal split event — no collateral fields (forces timestamp-based resolution)."""
    return {
        "id": sid,
        "timestamp": str(ts),
        "stakeholder": "0xw",
        "condition": "0xc",
        "amount": str(amount),
    }


def _split_with_unknown_collateral(amount: int, sid: str = "0xunk") -> dict:
    """Split with timestamp=0 so resolve_collateral returns None ('unknown')."""
    return {
        "id": sid,
        "timestamp": "0",
        "stakeholder": "0xw",
        "condition": "0xc",
        "amount": str(amount),
    }


def _merge_with_unknown_collateral(amount: int, sid: str = "0xunkm") -> dict:
    return {
        "id": sid,
        "timestamp": "0",
        "stakeholder": "0xw",
        "condition": "0xc",
        "amount": str(amount),
    }


def _redemption_with_unknown_collateral(payout: int, sid: str = "0xunkr") -> dict:
    return {
        "id": sid,
        "timestamp": "0",
        "redeemer": "0xw",
        "condition": "0xc",
        "indexSets": ["1"],
        "payout": str(payout),
    }


# ---------------------------------------------------------------------------
# (a) Non-USDC collateral exclusion
# ---------------------------------------------------------------------------

class TestNonUsdcCollateralExclusion:
    """Events with unresolvable collateral are excluded from USDC totals."""

    def test_unknown_collateral_split_excluded_from_deposits(self):
        """A split whose collateral resolves to 'unknown' must NOT add to
        total_deposits_usdc. It should increment n_non_usdc_collateral_events."""
        # ts=0 → capture_ts=None → resolve_collateral returns None ('unknown')
        splits = [_split_with_unknown_collateral(1_000_000_000, sid="s1")]
        with patch("sniper.analysis.wallet_protocol.capital_ledger.fetch_splits",
                   return_value=splits), \
             patch("sniper.analysis.wallet_protocol.capital_ledger.fetch_merges",
                   return_value=[]), \
             patch("sniper.analysis.wallet_protocol.capital_ledger.fetch_redemptions",
                   return_value=[]):
            ledger = compute_capital_ledger("0xw")

        # The $1000 must NOT appear in deposits (wrong scale assumption).
        assert ledger.total_deposits_usdc == 0.0
        assert ledger.net_capital_funded_usdc == 0.0
        # But the event is counted so callers know data was skipped.
        assert ledger.n_non_usdc_collateral_events == 1
        assert "unknown" in ledger.non_usdc_collateral_tokens

    def test_unknown_collateral_merge_excluded_from_withdrawals(self):
        merges = [_merge_with_unknown_collateral(500_000_000, sid="m1")]
        with patch("sniper.analysis.wallet_protocol.capital_ledger.fetch_splits",
                   return_value=[]), \
             patch("sniper.analysis.wallet_protocol.capital_ledger.fetch_merges",
                   return_value=merges), \
             patch("sniper.analysis.wallet_protocol.capital_ledger.fetch_redemptions",
                   return_value=[]):
            ledger = compute_capital_ledger("0xw")

        assert ledger.total_withdrawals_usdc == 0.0
        assert ledger.n_non_usdc_collateral_events == 1

    def test_unknown_collateral_redemption_excluded_from_payout(self):
        reds = [_redemption_with_unknown_collateral(200_000_000, sid="r1")]
        with patch("sniper.analysis.wallet_protocol.capital_ledger.fetch_splits",
                   return_value=[]), \
             patch("sniper.analysis.wallet_protocol.capital_ledger.fetch_merges",
                   return_value=[]), \
             patch("sniper.analysis.wallet_protocol.capital_ledger.fetch_redemptions",
                   return_value=reds):
            ledger = compute_capital_ledger("0xw")

        assert ledger.total_redemption_payout_usdc == 0.0
        assert ledger.n_non_usdc_collateral_events == 1

    def test_mixed_usdc_and_unknown_only_usdc_accumulates(self):
        """One USDC.e split (pre-cutoff ts) + one unknown split (ts=0) →
        deposits reflects only the USDC.e event."""
        from sniper.analysis.wallet_protocol.ledger.collateral import (
            CLOB_V2_COLLATERAL_CUTOFF_TS,
        )
        pre_cutoff_ts = CLOB_V2_COLLATERAL_CUTOFF_TS - 3600
        splits = [
            _split(100_000_000, ts=pre_cutoff_ts, sid="usdc_split"),  # 100 USDC.e
            _split_with_unknown_collateral(999_000_000_000, sid="unk_split"),  # excluded
        ]
        with patch("sniper.analysis.wallet_protocol.capital_ledger.fetch_splits",
                   return_value=splits), \
             patch("sniper.analysis.wallet_protocol.capital_ledger.fetch_merges",
                   return_value=[]), \
             patch("sniper.analysis.wallet_protocol.capital_ledger.fetch_redemptions",
                   return_value=[]):
            ledger = compute_capital_ledger("0xw")

        assert ledger.total_deposits_usdc == 100.0
        assert ledger.n_non_usdc_collateral_events == 1
        assert "unknown" in ledger.non_usdc_collateral_tokens

    def test_all_usdc_events_have_zero_non_usdc_count(self):
        """Normal wallet with only pre-cutoff events → n_non_usdc == 0."""
        from sniper.analysis.wallet_protocol.ledger.collateral import (
            CLOB_V2_COLLATERAL_CUTOFF_TS,
        )
        ts = CLOB_V2_COLLATERAL_CUTOFF_TS - 3600
        splits = [_split(50_000_000, ts=ts, sid="s1")]
        with patch("sniper.analysis.wallet_protocol.capital_ledger.fetch_splits",
                   return_value=splits), \
             patch("sniper.analysis.wallet_protocol.capital_ledger.fetch_merges",
                   return_value=[]), \
             patch("sniper.analysis.wallet_protocol.capital_ledger.fetch_redemptions",
                   return_value=[]):
            ledger = compute_capital_ledger("0xw")

        assert ledger.n_non_usdc_collateral_events == 0
        assert ledger.non_usdc_collateral_tokens == []
        assert ledger.total_deposits_usdc == 50.0

    def test_event_dict_carries_usdc_collateral_flag(self):
        """Each stamped event must carry usdc_collateral: bool so callers
        can filter per-event without re-resolving collateral."""
        from sniper.analysis.wallet_protocol.ledger.collateral import (
            CLOB_V2_COLLATERAL_CUTOFF_TS,
        )
        ts = CLOB_V2_COLLATERAL_CUTOFF_TS - 3600
        splits = [
            _split(100_000_000, ts=ts, sid="usdc"),
            _split_with_unknown_collateral(1_000, sid="unk"),
        ]
        with patch("sniper.analysis.wallet_protocol.capital_ledger.fetch_splits",
                   return_value=splits), \
             patch("sniper.analysis.wallet_protocol.capital_ledger.fetch_merges",
                   return_value=[]), \
             patch("sniper.analysis.wallet_protocol.capital_ledger.fetch_redemptions",
                   return_value=[]):
            ledger = compute_capital_ledger("0xw")

        by_id = {e["id"]: e for e in ledger.events}
        assert by_id["usdc"]["usdc_collateral"] is True
        assert by_id["unk"]["usdc_collateral"] is False


# ---------------------------------------------------------------------------
# (b) capital_summary uses initialValue, not totalBought
# ---------------------------------------------------------------------------

class TestCapitalSummaryAdr004:
    """capital_summary must use initialValue (ADR-004 correct cost basis)."""

    def _make_open_position(
        self,
        *,
        initial_value: float,
        current_value: float,
        cash_pnl: float,
        total_bought: float,
    ) -> dict:
        """Construct a position dict where totalBought is 80x initialValue."""
        return {
            "conditionId": "0xcid",
            "outcome": "Yes",
            "avgPrice": "0.5",
            "size": str(initial_value / 0.5),
            "initialValue": str(initial_value),   # ADR-004 correct cost basis
            "currentValue": str(current_value),
            "cashPnl": str(cash_pnl),
            "totalBought": str(total_bought),      # inflated — must NOT be used
            "curPrice": "0.6",
        }

    def test_open_cost_basis_uses_initial_value_not_total_bought(self):
        """With totalBought = 80x initialValue, open_cost_basis must equal
        the sum of initialValue, not totalBought."""
        open_pos = [
            self._make_open_position(
                initial_value=500.0,
                current_value=600.0,
                cash_pnl=100.0,
                total_bought=40_000.0,   # 80x inflation
            ),
        ]
        summary = capital_summary([], open_pos)

        assert summary["open_cost_basis"] == pytest.approx(500.0)
        # Verify totalBought (40000) is NOT what we got.
        assert summary["open_cost_basis"] != pytest.approx(40_000.0)

    def test_open_cost_basis_sums_multiple_positions(self):
        open_pos = [
            self._make_open_position(
                initial_value=300.0,
                current_value=360.0,
                cash_pnl=60.0,
                total_bought=24_000.0,
            ),
            self._make_open_position(
                initial_value=200.0,
                current_value=220.0,
                cash_pnl=20.0,
                total_bought=16_000.0,
            ),
        ]
        summary = capital_summary([], open_pos)

        assert summary["open_cost_basis"] == pytest.approx(500.0)
        assert summary["open_current_value"] == pytest.approx(580.0)
        assert summary["unrealized_pnl"] == pytest.approx(80.0)

    def test_realized_pnl_from_closed_positions(self):
        closed = [
            {"conditionId": "c1", "realizedPnl": "150.0"},
            {"conditionId": "c2", "realizedPnl": "-30.0"},
        ]
        summary = capital_summary(closed, [])
        assert summary["realized_pnl"] == pytest.approx(120.0)

    def test_string_numeric_fields_coerced(self):
        """API returns numeric values as strings; summary must handle both."""
        open_pos = [{
            "initialValue": "1000.0",
            "currentValue": "1200.0",
            "cashPnl": "200.0",
            "totalBought": "80000.0",
        }]
        closed = [{"realizedPnl": "500.0"}]
        summary = capital_summary(closed, open_pos)

        assert summary["open_cost_basis"] == pytest.approx(1000.0)
        assert summary["realized_pnl"] == pytest.approx(500.0)

    def test_net_capital_funded_from_ledger(self):
        ledger = CapitalLedger(
            wallet="0xtest",
            net_capital_funded_usdc=8_000.0,
            n_non_usdc_collateral_events=0,
        )
        summary = capital_summary([], [], ledger=ledger)
        assert summary["net_capital_funded"] == pytest.approx(8_000.0)

    def test_holdings_value_passthrough(self):
        summary = capital_summary([], [], holdings_value=12_345.67)
        assert summary["holdings_value"] == pytest.approx(12_345.67)

    def test_holdings_value_none_when_not_provided(self):
        summary = capital_summary([], [])
        assert summary["holdings_value"] is None


# ---------------------------------------------------------------------------
# (c) data_gaps flags
# ---------------------------------------------------------------------------

class TestCapitalSummaryDataGaps:
    """capital_summary data_gaps correctly names missing sources."""

    def test_missing_ledger_flagged(self):
        summary = capital_summary([], [], ledger=None)
        assert "capital_ledger_not_captured" in summary["data_gaps"]

    def test_no_missing_ledger_flag_when_ledger_provided(self):
        ledger = CapitalLedger(wallet="0xtest", net_capital_funded_usdc=0.0)
        summary = capital_summary([], [], ledger=ledger)
        assert "capital_ledger_not_captured" not in summary["data_gaps"]

    def test_missing_holdings_value_flagged(self):
        summary = capital_summary([], [], holdings_value=None)
        assert "holdings_value_not_provided" in summary["data_gaps"]

    def test_no_missing_holdings_flag_when_provided(self):
        summary = capital_summary([], [], holdings_value=100.0)
        assert "holdings_value_not_provided" not in summary["data_gaps"]

    def test_no_closed_positions_flagged(self):
        summary = capital_summary([], [])
        assert "no_closed_positions" in summary["data_gaps"]

    def test_no_gap_when_closed_positions_present(self):
        closed = [{"realizedPnl": "10.0"}]
        summary = capital_summary(closed, [])
        assert "no_closed_positions" not in summary["data_gaps"]

    def test_non_usdc_collateral_events_flagged_in_summary(self):
        """When ledger has non-USDC collateral events, summary.data_gaps records it."""
        ledger = CapitalLedger(
            wallet="0xtest",
            net_capital_funded_usdc=500.0,
            n_non_usdc_collateral_events=3,
            non_usdc_collateral_tokens=["unknown"],
        )
        summary = capital_summary([], [], ledger=ledger)
        gap_text = " ".join(summary["data_gaps"])
        assert "non_usdc_collateral_events_excluded" in gap_text
        assert "3" in gap_text
        assert "unknown" in gap_text

    def test_no_non_usdc_gap_when_all_events_are_usdc(self):
        ledger = CapitalLedger(
            wallet="0xtest",
            net_capital_funded_usdc=500.0,
            n_non_usdc_collateral_events=0,
            non_usdc_collateral_tokens=[],
        )
        summary = capital_summary([], [], ledger=ledger)
        assert not any(
            "non_usdc_collateral_events_excluded" in g for g in summary["data_gaps"]
        )

    def test_no_open_positions_returns_none_not_zero(self):
        """When open_positions is empty, metrics should be None (unknown),
        not 0.0 (known zero). Distinguishing unknown from actual-zero is
        required per project conventions."""
        summary = capital_summary([], [])
        assert summary["unrealized_pnl"] is None
        assert summary["open_cost_basis"] is None
        assert summary["open_current_value"] is None

    def test_net_capital_funded_none_when_no_ledger(self):
        summary = capital_summary([], [], ledger=None)
        assert summary["net_capital_funded"] is None
