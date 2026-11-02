"""Tests for Phase 2B: market-level intelligence queries.

Verifies market_intelligence_top_markets() and market_intelligence_domain_summary()
return correct aggregates across wallets and markets.
"""

import time

import pytest

from db import (
    get_connection,
    init_db,
    market_intelligence_domain_summary,
    market_intelligence_top_markets,
    upsert_position_signal,
    upsert_user_analysis,
)


def _seed_wallet(conn, wallet):
    now_ts = int(time.time())
    upsert_user_analysis(
        conn,
        wallet=wallet,
        analyzed_ts=now_ts,
        position_count=5,
        pnl=500.0,
        max_position_value=3000.0,
        top_open_position_title="Test",
        top_open_position_value=3000.0,
        total_open_value=6000.0,
        trade_count=30,
        low_buy_hits_14d=2,
        avg_low_buy_notional_14d=800.0,
        market_diversity_14d=4,
        concentration_ratio=0.3,
        suspicion_score=60.0,
        insider_score=72.0,
        speculator_score=20.0,
        classification="high_winrate_review",
        tier="B",
        strategy_group="balanced_mixed",
        strategy_confidence=0.75,
        strategy_note="test",
        wins=20,
        losses=8,
        closed_sample_size=28,
        win_rate=0.71,
        high_winrate_qualified=True,
        first_activity_ts=now_ts - 86400 * 90,
        account_age_days=90.0,
        is_fresh_account=False,
        total_closed_count=28,
        resolution_rate=0.85,
        effective_resolved_wins=20,
        effective_resolved_losses=8,
        position_signal_count=2,
        position_insider_candidate_count=1,
        position_speculator_count=0,
        max_position_insider_score=72.0,
        max_position_speculator_score=20.0,
        roi_percent=18.0,
        qualifying_pnl=300.0,
        qualifying_capital=2000.0,
        qualifying_roi=18.0,
        commit=True,
    )


def _seed_signal(conn, wallet, *, condition_id, title="Test market", insider_score=72.0,
                 entry_notional=500.0, is_open=True):
    ts = int(time.time())
    upsert_position_signal(
        conn,
        wallet=wallet,
        condition_id=condition_id,
        outcome="YES",
        title=title,
        is_open=is_open,
        position_value=3000.0,
        position_share=0.15,
        position_count_wallet=3,
        entry_anchor_ts=ts - 600,
        entry_price=0.40,
        entry_size=entry_notional / 0.40,
        entry_notional=entry_notional,
        current_price=0.45,
        current_size=entry_notional / 0.40 if is_open else 0.0,
        latest_trade_ts=ts,
        buys_24h=2,
        trades_24h=3,
        notional_24h=3000.0,
        position_insider_score=insider_score,
        position_speculator_score=20.0,
        position_classification="position_watch",
        signal_reason="test",
        analyzed_ts=ts,
        end_date_iso="2026-05-01T00:00:00+00:00",
    )
    conn.commit()


@pytest.fixture
def conn():
    c = get_connection(":memory:")
    init_db(c)
    yield c
    c.close()


class TestTopMarkets:
    def test_markets_ranked_by_capital(self, conn):
        """Markets should be ordered by total insider capital descending."""
        _seed_wallet(conn, "0xaaa1")
        _seed_wallet(conn, "0xbbb2")
        _seed_signal(conn, "0xaaa1", condition_id="big-market", entry_notional=5000.0)
        _seed_signal(conn, "0xbbb2", condition_id="big-market", entry_notional=3000.0)
        _seed_signal(conn, "0xaaa1", condition_id="small-market", entry_notional=100.0)

        results = market_intelligence_top_markets(conn)
        assert len(results) == 2
        assert results[0]["condition_id"] == "big-market"
        assert results[0]["total_capital"] == 8000.0
        assert results[0]["wallet_count"] == 2
        assert results[1]["condition_id"] == "small-market"
        assert results[1]["total_capital"] == 100.0

    def test_insider_score_filter(self, conn):
        """Markets with no positions above min_insider_score are excluded."""
        _seed_wallet(conn, "0xaaa1")
        _seed_signal(conn, "0xaaa1", condition_id="market-1", insider_score=80.0)
        _seed_signal(conn, "0xaaa1", condition_id="market-2", insider_score=30.0)

        results = market_intelligence_top_markets(conn, min_insider_score=55.0)
        assert len(results) == 1
        assert results[0]["condition_id"] == "market-1"

    def test_open_only_filter(self, conn):
        """open_only=True excludes closed positions."""
        _seed_wallet(conn, "0xaaa1")
        _seed_signal(conn, "0xaaa1", condition_id="open-market", is_open=True, entry_notional=500.0)
        _seed_signal(conn, "0xaaa1", condition_id="closed-market", is_open=False, entry_notional=1000.0)

        results = market_intelligence_top_markets(conn, open_only=True)
        assert len(results) == 1
        assert results[0]["condition_id"] == "open-market"

    def test_open_closed_counts(self, conn):
        """Each market should report correct open and closed counts."""
        _seed_wallet(conn, "0xaaa1")
        _seed_wallet(conn, "0xbbb2")
        _seed_signal(conn, "0xaaa1", condition_id="mixed-market", is_open=True)
        _seed_signal(conn, "0xbbb2", condition_id="mixed-market", is_open=False)

        results = market_intelligence_top_markets(conn)
        assert results[0]["open_count"] == 1
        assert results[0]["closed_count"] == 1

    def test_limit(self, conn):
        """Limit parameter caps the number of returned markets."""
        _seed_wallet(conn, "0xaaa1")
        for i in range(5):
            _seed_signal(conn, "0xaaa1", condition_id=f"market-{i}", entry_notional=float(100 * (i + 1)))

        results = market_intelligence_top_markets(conn, limit=3)
        assert len(results) == 3
        # Highest capital first
        assert results[0]["total_capital"] == 500.0


class TestDomainSummary:
    def test_domain_classification(self, conn):
        """Signals are grouped by domain classification from title."""
        _seed_wallet(conn, "0xaaa1")
        _seed_signal(conn, "0xaaa1", condition_id="m1", title="Will Bitcoin reach $100K?")
        _seed_signal(conn, "0xaaa1", condition_id="m2", title="NBA Finals Game 7 winner")
        _seed_signal(conn, "0xaaa1", condition_id="m3", title="Trump wins 2028 election")

        results = market_intelligence_domain_summary(conn)
        domains = {r["domain"]: r for r in results}
        assert "Crypto" in domains
        assert "Sports" in domains
        assert "US Politics" in domains

    def test_domain_capital_aggregation(self, conn):
        """Capital is correctly summed per domain."""
        _seed_wallet(conn, "0xaaa1")
        _seed_wallet(conn, "0xbbb2")
        _seed_signal(conn, "0xaaa1", condition_id="m1", title="Bitcoin price", entry_notional=1000.0)
        _seed_signal(conn, "0xbbb2", condition_id="m2", title="Ethereum merge", entry_notional=2000.0)

        results = market_intelligence_domain_summary(conn)
        crypto = next((r for r in results if r["domain"] == "Crypto"), None)
        assert crypto is not None
        assert crypto["total_capital"] == 3000.0
        assert crypto["wallet_count"] == 2
        assert crypto["position_count"] == 2

    def test_domain_insider_filter(self, conn):
        """min_insider_score filters positions before domain grouping."""
        _seed_wallet(conn, "0xaaa1")
        _seed_signal(conn, "0xaaa1", condition_id="m1", title="Bitcoin pump", insider_score=80.0, entry_notional=1000.0)
        _seed_signal(conn, "0xaaa1", condition_id="m2", title="ETH flippening", insider_score=30.0, entry_notional=5000.0)

        results = market_intelligence_domain_summary(conn, min_insider_score=55.0)
        crypto = next((r for r in results if r["domain"] == "Crypto"), None)
        assert crypto is not None
        assert crypto["total_capital"] == 1000.0  # Only the 80-score signal

    def test_domain_open_only(self, conn):
        """open_only excludes closed positions from domain summary."""
        _seed_wallet(conn, "0xaaa1")
        _seed_signal(conn, "0xaaa1", condition_id="m1", title="Bitcoin top", is_open=True, entry_notional=500.0)
        _seed_signal(conn, "0xaaa1", condition_id="m2", title="Solana surge", is_open=False, entry_notional=2000.0)

        results = market_intelligence_domain_summary(conn, open_only=True)
        crypto = next((r for r in results if r["domain"] == "Crypto"), None)
        assert crypto is not None
        assert crypto["total_capital"] == 500.0
        assert crypto["open_count"] == 1
        assert crypto["closed_count"] == 0

    def test_sorted_by_capital(self, conn):
        """Domains should be sorted by total capital descending."""
        _seed_wallet(conn, "0xaaa1")
        _seed_signal(conn, "0xaaa1", condition_id="m1", title="Bitcoin", entry_notional=100.0)
        _seed_signal(conn, "0xaaa1", condition_id="m2", title="NBA Finals", entry_notional=5000.0)

        results = market_intelligence_domain_summary(conn)
        assert results[0]["total_capital"] >= results[-1]["total_capital"]
