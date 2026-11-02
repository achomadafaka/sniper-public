"""Regression: analyze_wallet computes closed-position ROI from totalBought, not size.

Codex 2026-05-27 audit: Polymarket /closed-positions returns size=0 (no
current holdings) and totalBought as the gross share count over the
position's lifetime. The pre-fix code used `avgPrice * size` for closed
cost basis, which zeroed out realized_roi and combined_roi on every
closed-only wallet.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet  # noqa: E402
from sniper.analysis.wallet_protocol.types import WalletData  # noqa: E402


def _data_with_closed_only() -> WalletData:
    return WalletData(
        wallet="0x" + "a" * 40,
        closed_positions=[
            # /closed-positions response shape: size=0, totalBought is shares
            {
                "conditionId": "0x" + "b" * 64,
                "asset": "1001",
                "outcome": "Yes",
                "title": "Closed market",
                "avgPrice": 0.4,
                "size": 0.0,
                "totalBought": 100.0,  # 100 shares
                "realizedPnl": 60.0,   # 100 × (1.0 - 0.4) = 60 → resolution=1.0
            },
            {
                "conditionId": "0x" + "c" * 64,
                "asset": "1002",
                "outcome": "Yes",
                "title": "Closed market 2",
                "avgPrice": 0.5,
                "size": 0.0,
                "totalBought": 50.0,   # 50 shares
                "realizedPnl": -25.0,  # 50 × (0.0 - 0.5) = -25 → resolution=0.0
            },
        ],
        open_positions=[],
        activity_rows=[],
        activity_cap_hit=False,
        archetype_layer1=None,
        execution_profile=None,
        fetched_at=1_700_000_000,
    )


def test_realized_roi_uses_totalbought_not_size(tmp_path, monkeypatch):
    """Pre-fix: realized_roi was None because cost_basis = sum(avgPrice * size)
    and size was always 0. Post-fix: cost_basis = sum(avgPrice * totalBought)
    → realized_roi = realized_pnl / cost_basis."""
    monkeypatch.chdir(tmp_path)
    data = _data_with_closed_only()
    report = analyze_wallet(data.wallet, pre_fetched_data=data)

    # Expected cost basis: 0.4*100 + 0.5*50 = 40 + 25 = 65
    # Expected realized PnL: 60 + (-25) = 35
    # Expected realized_roi: 35 / 65 = 0.5384...
    assert report.realized_pnl == pytest.approx(35.0)
    expected_roi = 35.0 / 65.0
    assert report.realized_roi is not None, (
        "Pre-fix: realized_roi was None because cost_basis used size=0"
    )
    assert report.realized_roi == pytest.approx(expected_roi, rel=1e-6)


def test_combined_roi_uses_totalbought_not_size(tmp_path, monkeypatch):
    """combined_roi = combined_pnl / (closed_cost_basis + open_cost_basis).
    Closed cost basis must use totalBought; pre-fix it used size=0."""
    monkeypatch.chdir(tmp_path)
    data = _data_with_closed_only()
    report = analyze_wallet(data.wallet, pre_fetched_data=data)

    assert report.combined_pnl == pytest.approx(35.0)
    # No open positions → total basis = closed basis = 65
    assert report.combined_roi is not None
    assert report.combined_roi == pytest.approx(35.0 / 65.0, rel=1e-6)
