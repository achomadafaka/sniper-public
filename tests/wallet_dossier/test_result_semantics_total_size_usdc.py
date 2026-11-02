"""Regression: result_semantics.total_size_usdc must be in USDC, not shares.

Codex round 2 audit: ExitEpisode.size_exited is raw share count
(sniper/analysis/wallet_dossier/derivers.py:745), GrainAggregate.total_size
sums it raw (grain.py:139), then builder writes it as total_size_usdc
(builder.py:546). Same notional-vs-shares bug class as descriptor_sizing.

This test builds a wallet with a single closed binary position resolved
via redemption, then checks that result_semantics.total_size_usdc reflects
size × price, not raw shares.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from sniper.analysis.wallet_dossier.builder import build_dossier  # noqa: E402


def _fixture_one_redeemed_position(tmp_path: Path) -> tuple[Path, Path]:
    """Wallet bought 100 shares of asset A on a binary market at 0.40, held
    to redemption (won — payout 100 at 1.0). The single 'held_to_resolution'
    episode produces size_exited=100 at price_exit=1.0 → notional=100 USDC.
    The pre-fix code would sum size_exited=100 directly. Same number in
    this contrived case BUT we add a second pre-resolution SELL at price
    0.50 of 40 shares → notional=20 USDC. Pre-fix: total_size = 100+40 = 140
    (shares). Post-fix: total_size_usdc = 100×1.0 + 40×0.50 = 120 USDC.
    """
    wallet = "0xdeadbeef" + "0" * 32
    cond = "0x" + "a" * 64
    asset_a = "1001"
    asset_b = "1002"
    fixture = {
        "wallet": wallet,
        "fetched_at": 1_700_000_000,
        "schema_version": 2,
        "closed_positions": [
            {
                "conditionId": cond, "asset": asset_a, "outcome": "Yes",
                "title": "Test market", "avgPrice": 0.40,
                "size": 0.0, "totalBought": 100.0,
                "realizedPnl": 30.0, "outcomeIndex": 0,
                "oppositeAsset": asset_b, "oppositeOutcome": "No",
                "timestamp": 1_699_000_000,
            },
            # Sibling NO row to satisfy market_type=binary derivation
            {
                "conditionId": cond, "asset": asset_b, "outcome": "No",
                "title": "Test market",  "avgPrice": 0.60,
                "size": 0.0, "totalBought": 50.0,
                "realizedPnl": -30.0, "outcomeIndex": 1,
                "timestamp": 1_699_000_000,
            },
        ],
        "open_positions": [],
        "activity_rows": [
            # Entry BUY of 100 shares at 0.40
            {
                "conditionId": cond, "asset": asset_a, "type": "TRADE",
                "side": "BUY", "size": 100.0, "price": 0.40,
                "timestamp": 1_698_000_000, "outcome": "Yes",
            },
            # Pre-resolution SELL of 40 shares at 0.50 → notional 20 USDC
            {
                "conditionId": cond, "asset": asset_a, "type": "TRADE",
                "side": "SELL", "size": 40.0, "price": 0.50,
                "timestamp": 1_698_500_000, "outcome": "Yes",
            },
        ],
        "redemptions": [
            {
                "condition": cond, "indexSets": ["1"], "payout": "60",
                "timestamp": "1_699_000_000",
            },
        ],
        "leafs_by_cid": {cond: ["sports"]},
        "nav_lookup": {},
    }
    fix_path = tmp_path / "fixture.json"
    fix_path.write_text(json.dumps(fixture))
    rep_path = tmp_path / "report.json"
    rep_path.write_text(json.dumps({"protocol_version": "v2", "wallet": wallet}))
    return fix_path, rep_path


def test_result_semantics_total_size_usdc_is_notional_not_shares(tmp_path: Path):
    fix, rep = _fixture_one_redeemed_position(tmp_path)
    wallet = "0xdeadbeef" + "0" * 32
    tables = build_dossier(
        wallet=wallet,
        fixture_path=fix,
        protocol_report_path=rep,
    )
    # The closed YES position should produce a grain row
    grain_rows = [r for r in tables.result_semantics if r["n_episodes"] > 0]
    assert grain_rows, "expected at least one grain with episodes"
    # Sum across all grains (this synthetic fixture has just one anyway)
    total_size_usdc = sum(float(r["total_size_usdc"]) for r in grain_rows)

    # Pre-fix expected (raw shares): SELL 40 + held 60 (remaining after sell) = 100 shares.
    # Post-fix expected (USDC notional):
    #   pre-resolution SELL: 40 shares × 0.50 = 20 USDC
    #   held_to_resolution remaining 60 shares × 1.0 (redemption price) = 60 USDC
    #   total = 80 USDC notional
    # The raw-shares total (~100) is markedly different from the USDC total (~80).
    raw_share_total = 100.0  # what the bug emitted
    assert total_size_usdc != pytest.approx(raw_share_total, rel=0.05), (
        f"total_size_usdc={total_size_usdc} ≈ raw share count ({raw_share_total}) — "
        "still aggregating shares instead of notional"
    )
    # Should be ~80 USDC; allow slack for the held-redemption price ambiguity.
    assert 50.0 <= total_size_usdc <= 110.0, (
        f"total_size_usdc={total_size_usdc} outside the expected USDC band [50, 110]"
    )
