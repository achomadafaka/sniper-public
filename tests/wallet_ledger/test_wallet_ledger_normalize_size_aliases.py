"""Codex round 2 audit regression: ledger materialize must expose
`size` and `totalBought` aliases on materialized position rows.

`_normalize_for_walletdata()` mapped avgPrice/curPrice/realizedPnl/timestamp
but not size/totalBought. The writer preserves the live-API value as
`api_total_bought_raw` on the warehouse row (positions.py:189), but the
alias chain never re-exposed it under the camelCase Polymarket-API name.
Result: `analyze_wallet` (which reads `p.get("totalBought")` for closed
positions, per PR #78) silently sees None on every ledger-materialized
wallet.

Test exercises the function directly with synthetic DB rows.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from sniper.analysis.wallet_protocol.ledger.materialize import (  # noqa: E402
    _normalize_for_walletdata,
)


def test_normalize_exposes_totalbought_from_api_raw():
    """Codex round 2: API-shape totalBought must be exposed by the
    materializer's alias step so analyze_wallet can read it."""
    row = {
        "condition_id": "0xcid",
        "token_id": "asset1",
        "outcome_index": 0,
        "outcome_label": "Yes",
        "avg_price": 0.40,
        "current_price": 1.0,
        "realized_pnl_collateral": 60.0,
        "closed_at": 1_700_000_000,
        "api_total_bought_raw": 100.0,
    }
    out = _normalize_for_walletdata(row)
    assert out.get("totalBought") == 100.0, (
        "Pre-fix: api_total_bought_raw was preserved on the row but never "
        "exposed as totalBought; analyze_wallet's closed-position ROI math "
        "silently saw None."
    )


def test_normalize_exposes_size_from_size_tokens():
    """size_tokens column on the warehouse maps to the live-API `size`
    field — currently 0 for closed Polymarket positions but live for
    open ones. analyze_wallet's open-position math reads `size`."""
    row = {
        "condition_id": "0xcid",
        "token_id": "asset1",
        "avg_price": 0.50,
        "current_price": 0.60,
        "size_tokens": 75.0,
    }
    out = _normalize_for_walletdata(row)
    assert out.get("size") == 75.0


def test_normalize_does_not_overwrite_explicit_camel_aliases():
    """If a row already has the camelCase alias (rare), the existing
    value wins (setdefault semantics, matching avgPrice/curPrice)."""
    row = {
        "condition_id": "0xcid",
        "api_total_bought_raw": 100.0,
        "totalBought": 99.0,
        "size_tokens": 50.0,
        "size": 49.0,
    }
    out = _normalize_for_walletdata(row)
    assert out.get("totalBought") == 99.0
    assert out.get("size") == 49.0


def test_normalize_does_NOT_fall_back_to_total_bought_collateral_unit_mismatch():
    """Codex round 3 audit: api_total_bought_raw is shares (Polymarket
    /closed-positions semantic). total_bought_collateral is USDC cost
    basis (schema positions.py:131). They are NOT interchangeable.

    analyze_wallet computes `avgPrice * totalBought` for closed cost
    basis — a USDC fallback would double-count. The fallback that
    earlier versions of this test asserted has been REMOVED.
    """
    row = {
        "condition_id": "0xcid",
        "avg_price": 0.40,
        "api_total_bought_raw": None,
        "total_bought_collateral": 80.0,  # USDC, not shares
    }
    out = _normalize_for_walletdata(row)
    assert out.get("totalBought") is None, (
        "totalBought must stay None when api_total_bought_raw is absent; "
        "total_bought_collateral is a different unit (USDC) and must not "
        "be used as a fallback."
    )


def test_normalize_leaves_total_bought_none_when_api_raw_missing():
    """No api_total_bought_raw → totalBought absent. Better silent than fabricated."""
    row = {
        "condition_id": "0xcid",
        "avg_price": 0.40,
    }
    out = _normalize_for_walletdata(row)
    assert out.get("totalBought") is None
