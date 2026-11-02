"""Smoke tests for data.polymarket_activity_client — hits LIVE Polymarket Data API.

Marked `integration`; not run by default. Run manually to verify endpoint
shape + assumptions:

    pytest -m integration tests/test_polymarket_activity_client.py -v

The wallet used is a known-active 2024-election-window thesis trader picked
during 2026-04-21 probing. If these tests go red, the Polymarket Data API
endpoint shape or behavior has changed — re-probe before trusting downstream
feature extraction built on this client.
"""
from __future__ import annotations

import pytest

from data.polymarket_activity_client import (
    ActivityRow,
    PAGE_LIMIT_CAP,
    get_activity_page,
    get_positions,
    iter_activity,
)

pytestmark = pytest.mark.integration

# Known-active wallet (Oct–Nov 2024 election trader, $16.6M lb_pnl).
# Probed 2026-04-21: ~3000+ activity rows, mix of TRADE/REDEEM/MERGE/REWARD.
ACTIVE_WALLET = "0x1f2dd6d473f3e824cd2f8a89d9c69fb96f6ad0cf"

_OBSERVED_TYPES = {"TRADE", "REDEEM", "MERGE", "REWARD", "MAKER_REBATE"}


def test_get_activity_page_returns_typed_rows():
    rows = get_activity_page(ACTIVE_WALLET, limit=10)
    assert isinstance(rows, list)
    assert len(rows) > 0, (
        "known-active wallet returned empty activity — API glitch or wallet churned"
    )
    first = rows[0]
    assert isinstance(first, ActivityRow)
    assert first.proxy_wallet.lower() == ACTIVE_WALLET.lower()
    assert first.timestamp > 0
    # Activity type present; open-set assertion — if a new type appears,
    # this test stays green and we learn about the type in downstream audits.
    assert first.activity_type, "activity_type is empty string"


def test_trade_rows_have_side_set_to_buy_or_sell():
    rows = get_activity_page(ACTIVE_WALLET, limit=100)
    trade_rows = [r for r in rows if r.activity_type == "TRADE"]
    assert trade_rows, "no TRADE rows in 100-row page on known-active wallet"
    for row in trade_rows:
        assert row.side in ("BUY", "SELL"), (
            f"TRADE row has unexpected side={row.side!r} on conditionId={row.condition_id}"
        )


def test_non_trade_rows_have_none_side():
    rows = get_activity_page(ACTIVE_WALLET, limit=100)
    non_trade = [r for r in rows if r.activity_type != "TRADE"]
    for row in non_trade:
        assert row.side is None, (
            f"non-TRADE row has side={row.side!r} type={row.activity_type!r}"
        )


def test_iter_activity_paginates_past_first_page():
    seen = 0
    for _ in iter_activity(ACTIVE_WALLET):
        seen += 1
        if seen > PAGE_LIMIT_CAP + 1:
            break
    assert seen > PAGE_LIMIT_CAP, (
        f"pagination produced only {seen} rows; expected > {PAGE_LIMIT_CAP} for active wallet"
    )


def test_iter_activity_respects_since_ts():
    # Cut off at a timestamp in the future → should yield nothing.
    future_ts = 4_000_000_000  # year 2096
    assert list(iter_activity(ACTIVE_WALLET, since_ts=future_ts)) == []


def test_get_positions_returns_list():
    result = get_positions(ACTIVE_WALLET)
    assert isinstance(result, list)
    # No length assertion: wallet may have 0 open positions legitimately.


def test_observed_types_in_sample_are_known():
    """Sanity: across a 500-row sample we shouldn't see wildly novel types.

    If a new type appears, audit it — it may be a legitimate new activity
    kind (e.g., CLAIM, LIQUIDATE) we need to handle in the feature extractor.
    """
    rows = get_activity_page(ACTIVE_WALLET, limit=500)
    types = {r.activity_type for r in rows}
    unknown = types - _OBSERVED_TYPES
    assert not unknown, (
        f"new activity types observed not in _OBSERVED_TYPES: {unknown}. "
        "If legitimate, update _OBSERVED_TYPES and the downstream feature extractor."
    )
