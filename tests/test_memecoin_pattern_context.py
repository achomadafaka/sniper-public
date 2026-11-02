from datetime import datetime, timedelta, timezone

import pytest

from sniper.research.memecoin_strategy.pattern_context import (
    conviction_windows,
    find_fresh_conviction_reentry,
    thesis_context,
)


START = datetime(2026, 7, 11, tzinfo=timezone.utc)


def _event(
    index,
    minute,
    user,
    side="buy",
    *,
    size=100,
    curated=True,
    network="solana",
    mint="M",
):
    return {
        "id": index,
        "observed_at": (START + timedelta(minutes=minute)).isoformat(),
        "source_user_id": user,
        "normalized_side": side,
        "their_size_usd": size,
        "was_curated": curated,
        "network_id": network,
        "mint": mint,
    }


def _quote(index, minute, price, *, network="solana", mint="M", quality="ok"):
    return {
        "id": index,
        "observed_at": (START + timedelta(minutes=minute)).isoformat(),
        "price": price,
        "quality": quality,
        "network_id": network,
        "mint": mint,
    }


def test_conviction_windows_count_distinct_wallets_not_events_and_keep_unknowns():
    rows = [
        _event(1, 1, "a", size=100),
        _event(2, 2, "a", size=200),
        _event(3, 4, "b", size=300, curated=None),
        _event(4, 5, "c", side="sell", size=150),
        _event(5, 15, "d", size=400),
        _event(6, 16, "e", size=500),
        _event(7, 2, "x", mint="OTHER"),
    ]
    result = conviction_windows(
        START.isoformat(), rows, lane="v2", network_id="solana", mint="M",
    )
    five = result["windows"]["5m"]
    assert five["observed"]["distinct_buyers"] == 2
    assert five["observed"]["repeat_buyers"] == 1
    assert five["observed"]["buy_notional"] == 600
    assert five["observed"]["distinct_sellers"] == 1
    assert five["observed"]["sell_notional"] == 150
    assert five["curated"]["distinct_buyers"] == 1
    assert result["unknown_cohort_rows"] == 1
    assert result["windows"]["15m"]["observed"]["distinct_buyers"] == 3


def test_legacy_conviction_has_no_curated_claim_and_evm_mints_normalize():
    result = conviction_windows(
        START.isoformat(),
        [_event(1, 1, "a", network="ethereum", mint="0xABC")],
        lane="legacy",
        network_id="ethereum",
        mint="0xabc",
    )
    assert result["windows"]["5m"]["observed"]["distinct_buyers"] == 1
    assert result["windows"]["5m"]["curated"] is None
    assert result["cohort_claim"] == "observed_activity"


def test_conviction_rejects_naive_timestamps_and_unsupported_lane():
    with pytest.raises(ValueError, match="timezone aware"):
        conviction_windows("2026-07-11T00:00:00", [], lane="v2", network_id="solana", mint="M")
    with pytest.raises(ValueError, match="lane"):
        conviction_windows(START.isoformat(), [], lane="mixed", network_id="solana", mint="M")


def test_thesis_context_measures_episode_and_poster_buy_lag_without_future_rows():
    feed = [
        _event(1, 0, "first"),
        _event(2, 5, "poster"),
        _event(3, 30, "poster"),
    ]
    theses = [
        {
            "id": 10,
            "observed_at": (START + timedelta(minutes=10)).isoformat(),
            "source_user_id": "poster",
            "their_pnl_pct": 25,
            "price": 1.0,
            "network_id": "solana",
            "mint": "M",
        },
        {
            "id": 11,
            "observed_at": (START + timedelta(minutes=40)).isoformat(),
            "source_user_id": "future",
            "their_pnl_pct": -50,
            "price": 1.0,
            "network_id": "solana",
            "mint": "M",
        },
    ]
    result = thesis_context(
        START.isoformat(), theses, feed, [_quote(1, 10, 1.0), _quote(2, 20, 1.5)],
        network_id="solana", mint="M", asof=(START + timedelta(minutes=25)).isoformat(),
    )
    assert result["count"] == 1
    assert result["first_stance"] == "winning"
    assert result["lag_from_first_buy_s"] == 600
    assert result["lag_from_poster_buy_s"] == 300
    assert result["forward_return_pct"] == pytest.approx(50)
    assert result["forward_age_s"] == 600


def test_thesis_context_preserves_missing_poster_buy_and_negative_episode_lag():
    thesis = {
        "id": 1,
        "observed_at": (START - timedelta(minutes=5)).isoformat(),
        "source_user_id": "poster",
        "their_pnl_pct": None,
        "price": 1.0,
        "network_id": "solana",
        "mint": "M",
    }
    result = thesis_context(
        START.isoformat(), [thesis], [], [], network_id="solana", mint="M",
        asof=START.isoformat(),
    )
    assert result["first_stance"] == "unknown"
    assert result["lag_from_first_buy_s"] == -300
    assert result["lag_from_poster_buy_s"] is None
    assert result["forward_return_pct"] is None


def test_reentry_requires_distinct_curated_buyers_and_price_reclaim_after_confirmation():
    events = [
        _event(1, 1, "original"),
        _event(2, 2, "new"),
    ]
    quotes = [_quote(1, 0, 0.50), _quote(2, 3, 0.40), _quote(3, 4, 0.47), _quote(4, 5, 0.49)]
    signal = find_fresh_conviction_reentry(
        START.isoformat(), "original", events, quotes,
        network_id="solana", mint="M",
    )
    assert signal is not None
    assert signal.confirmed_at == (START + timedelta(minutes=2)).isoformat()
    assert signal.entry_at == (START + timedelta(minutes=5)).isoformat()
    assert signal.entry_price == pytest.approx(0.49)
    assert signal.post_stop_low == pytest.approx(0.40)
    assert signal.distinct_buyers == ("new", "original")


def test_reentry_does_not_use_low_fill_or_unknown_noncurated_or_late_buyer():
    quotes = [_quote(1, 0, 0.5), _quote(2, 1, 0.4), _quote(3, 3, 0.6)]
    assert find_fresh_conviction_reentry(
        START.isoformat(), "original",
        [_event(1, 1, "original"), _event(2, 2, "unknown", curated=None)],
        quotes, network_id="solana", mint="M",
    ) is None
    assert find_fresh_conviction_reentry(
        START.isoformat(), "original",
        [_event(1, 1, "original"), _event(2, 31, "late")],
        quotes, network_id="solana", mint="M",
    ) is None
