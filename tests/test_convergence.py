"""Tests for Phase 2A: market convergence detector.

Verifies that market_convergence_signals(), market_convergence_detail(),
and convergence_from_history() correctly identify markets where multiple
insider wallets converge.
"""

import sqlite3
import time

import pytest

from db import (
    convergence_from_history,
    get_connection,
    init_db,
    insert_signal_history,
    market_convergence_detail,
    market_convergence_signals,
    upsert_position_signal,
    upsert_user_analysis,
)


def _seed_wallet(conn, wallet, *, insider_score=72.0, classification="high_winrate_review"):
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
        insider_score=insider_score,
        speculator_score=20.0,
        classification=classification,
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
        max_position_insider_score=insider_score,
        max_position_speculator_score=20.0,
        roi_percent=18.0,
        qualifying_pnl=300.0,
        qualifying_capital=2000.0,
        qualifying_roi=18.0,
        commit=True,
    )


def _seed_signal(conn, wallet, *, condition_id, outcome="YES", insider_score=72.0,
                 entry_notional=500.0, entry_price=0.40, current_price=0.45, is_open=True):
    ts = int(time.time())
    upsert_position_signal(
        conn,
        wallet=wallet,
        condition_id=condition_id,
        outcome=outcome,
        title=f"Test market {condition_id}",
        is_open=is_open,
        position_value=3000.0,
        position_share=0.15,
        position_count_wallet=3,
        entry_anchor_ts=ts - 600,
        entry_price=entry_price,
        entry_size=entry_notional / max(entry_price, 0.01),
        entry_notional=entry_notional,
        current_price=current_price,
        current_size=entry_notional / max(entry_price, 0.01) if is_open else 0.0,
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


class TestMarketConvergenceSignals:
    def test_basic_convergence(self, conn):
        """Two insider wallets on the same market should produce a convergence signal."""
        _seed_wallet(conn, "0xaaa1")
        _seed_wallet(conn, "0xbbb2")
        _seed_signal(conn, "0xaaa1", condition_id="market-1", insider_score=75.0)
        _seed_signal(conn, "0xbbb2", condition_id="market-1", insider_score=80.0)

        results = market_convergence_signals(conn, min_insider_score=55.0, min_wallets=2)
        assert len(results) == 1
        r = results[0]
        assert r["insider_count"] == 2
        assert r["condition_id"] == "market-1"
        assert r["avg_insider_score"] == 77.5  # (75 + 80) / 2
        assert r["max_insider_score"] == 80.0
        assert "0xaaa1" in r["wallet_list"]
        assert "0xbbb2" in r["wallet_list"]

    def test_no_convergence_single_wallet(self, conn):
        """A single wallet on a market is NOT convergence."""
        _seed_wallet(conn, "0xaaa1")
        _seed_signal(conn, "0xaaa1", condition_id="market-solo", insider_score=90.0)

        results = market_convergence_signals(conn, min_insider_score=55.0, min_wallets=2)
        assert len(results) == 0

    def test_insider_score_filter(self, conn):
        """Wallets below the min insider score threshold are excluded."""
        _seed_wallet(conn, "0xaaa1")
        _seed_wallet(conn, "0xbbb2")
        _seed_signal(conn, "0xaaa1", condition_id="market-1", insider_score=80.0)
        _seed_signal(conn, "0xbbb2", condition_id="market-1", insider_score=40.0)  # below 55

        results = market_convergence_signals(conn, min_insider_score=55.0, min_wallets=2)
        assert len(results) == 0  # Only 1 wallet qualifies

    def test_closed_positions_excluded_by_default(self, conn):
        """Closed positions should be excluded when open_only=True (default)."""
        _seed_wallet(conn, "0xaaa1")
        _seed_wallet(conn, "0xbbb2")
        _seed_signal(conn, "0xaaa1", condition_id="market-1", insider_score=75.0)
        _seed_signal(conn, "0xbbb2", condition_id="market-1", insider_score=80.0, is_open=False)

        results = market_convergence_signals(conn, min_insider_score=55.0, min_wallets=2)
        assert len(results) == 0

    def test_three_wallet_convergence(self, conn):
        """Three insiders on same market — higher conviction."""
        for i, score in enumerate([70, 75, 85]):
            w = f"0x{i:040x}"
            _seed_wallet(conn, w)
            _seed_signal(conn, w, condition_id="hot-market", insider_score=float(score))

        results = market_convergence_signals(conn, min_insider_score=55.0, min_wallets=2)
        assert len(results) == 1
        assert results[0]["insider_count"] == 3

    def test_min_wallets_threshold(self, conn):
        """min_wallets=3 should require 3+ wallets."""
        _seed_wallet(conn, "0xaaa1")
        _seed_wallet(conn, "0xbbb2")
        _seed_signal(conn, "0xaaa1", condition_id="market-1", insider_score=75.0)
        _seed_signal(conn, "0xbbb2", condition_id="market-1", insider_score=80.0)

        results = market_convergence_signals(conn, min_insider_score=55.0, min_wallets=3)
        assert len(results) == 0

        results = market_convergence_signals(conn, min_insider_score=55.0, min_wallets=2)
        assert len(results) == 1

    def test_multiple_markets(self, conn):
        """Multiple convergence markets are returned, ordered by insider_count desc."""
        wallets = [f"0x{i:040x}" for i in range(5)]
        for w in wallets:
            _seed_wallet(conn, w)

        # Market A: 3 insiders
        for w in wallets[:3]:
            _seed_signal(conn, w, condition_id="market-A", insider_score=70.0)

        # Market B: 2 insiders
        for w in wallets[3:5]:
            _seed_signal(conn, w, condition_id="market-B", insider_score=65.0)

        results = market_convergence_signals(conn, min_insider_score=55.0, min_wallets=2)
        assert len(results) == 2
        assert results[0]["insider_count"] == 3  # Market A first
        assert results[1]["insider_count"] == 2

    def test_total_capital_aggregation(self, conn):
        """Total capital is the sum of entry_notional across wallets."""
        _seed_wallet(conn, "0xaaa1")
        _seed_wallet(conn, "0xbbb2")
        _seed_signal(conn, "0xaaa1", condition_id="market-1", insider_score=75.0, entry_notional=1000.0)
        _seed_signal(conn, "0xbbb2", condition_id="market-1", insider_score=80.0, entry_notional=2000.0)

        results = market_convergence_signals(conn, min_insider_score=55.0, min_wallets=2)
        assert results[0]["total_capital"] == 3000.0

    def test_outcome_separation(self, conn):
        """Wallets on opposite sides of the same market are NOT convergence."""
        _seed_wallet(conn, "0xaaa1")
        _seed_wallet(conn, "0xbbb2")
        _seed_signal(conn, "0xaaa1", condition_id="market-1", outcome="YES", insider_score=75.0)
        _seed_signal(conn, "0xbbb2", condition_id="market-1", outcome="NO", insider_score=80.0)

        results = market_convergence_signals(conn, min_insider_score=55.0, min_wallets=2)
        assert len(results) == 0  # YES has 1 wallet, NO has 1 wallet — no convergence


class TestMarketConvergenceDetail:
    def test_detail_returns_per_wallet_info(self, conn):
        """Detail query returns individual wallet entries for a convergence market."""
        _seed_wallet(conn, "0xaaa1")
        _seed_wallet(conn, "0xbbb2")
        _seed_signal(conn, "0xaaa1", condition_id="market-1", insider_score=75.0)
        _seed_signal(conn, "0xbbb2", condition_id="market-1", insider_score=80.0)

        rows = market_convergence_detail(conn, "market-1")
        assert len(rows) == 2
        wallets = {r["wallet"] for r in rows}
        assert "0xaaa1" in wallets
        assert "0xbbb2" in wallets
        # Ordered by insider score desc
        assert rows[0]["position_insider_score"] >= rows[1]["position_insider_score"]

    def test_detail_includes_wallet_stats(self, conn):
        """Detail rows should include wallet-level data from user_analysis."""
        _seed_wallet(conn, "0xaaa1")
        _seed_signal(conn, "0xaaa1", condition_id="market-1", insider_score=75.0)

        rows = market_convergence_detail(conn, "market-1", min_insider_score=0)
        assert len(rows) == 1
        assert rows[0]["win_rate"] is not None
        assert rows[0]["qualifying_roi"] is not None

    def test_detail_filters_by_outcome(self, conn):
        """Detail can filter by specific outcome."""
        _seed_wallet(conn, "0xaaa1")
        _seed_wallet(conn, "0xbbb2")
        _seed_signal(conn, "0xaaa1", condition_id="market-1", outcome="YES", insider_score=75.0)
        _seed_signal(conn, "0xbbb2", condition_id="market-1", outcome="NO", insider_score=80.0)

        rows = market_convergence_detail(conn, "market-1", outcome="YES")
        assert len(rows) == 1
        assert rows[0]["wallet"] == "0xaaa1"


class TestConvergenceFromHistory:
    """Tests for convergence_from_history() using immutable signal_history."""

    def _seed_history(self, conn, wallet, condition_id, *, outcome="YES",
                      insider_score=75.0, anchor_ts=1700000000,
                      analyzed_ts=1700001000, is_open=True):
        insert_signal_history(
            conn,
            signals=[{
                "wallet": wallet,
                "condition_id": condition_id,
                "outcome": outcome,
                "title": f"Market {condition_id}",
                "is_open": is_open,
                "entry_price": 0.40,
                "entry_notional": 500.0,
                "current_price": 0.45,
                "position_insider_score": insider_score,
                "position_speculator_score": 10.0,
                "entry_anchor_ts": anchor_ts,
            }],
            analyzed_ts=analyzed_ts,
        )
        conn.commit()

    def test_basic_convergence(self, conn):
        """Two wallets with anchored timestamps on same market."""
        self._seed_history(conn, "0xaaa", "mkt-1", anchor_ts=1700000000)
        self._seed_history(conn, "0xbbb", "mkt-1", anchor_ts=1700003600)

        result = convergence_from_history(conn, min_insider_score=55.0)
        assert len(result["items"]) == 1
        m = result["items"][0]
        assert m["insider_count"] == 2
        assert m["earliest_anchor_ts"] == 1700000000
        assert m["latest_anchor_ts"] == 1700003600
        assert m["window_hours"] == 1.0  # 3600s = 1h

    def test_excludes_zero_anchor(self, conn):
        """Rows with entry_anchor_ts=0 should be excluded."""
        self._seed_history(conn, "0xaaa", "mkt-1", anchor_ts=1700000000)
        self._seed_history(conn, "0xbbb", "mkt-1", anchor_ts=0)

        result = convergence_from_history(conn, min_insider_score=55.0)
        assert len(result["items"]) == 0  # Only 1 anchored wallet, need >= 2

    def test_tight_convergence_flag(self, conn):
        """Convergence within max_window_hours flagged as tight."""
        self._seed_history(conn, "0xaaa", "mkt-1", anchor_ts=1700000000)
        self._seed_history(conn, "0xbbb", "mkt-1", anchor_ts=1700003600)

        result = convergence_from_history(conn, max_window_hours=2.0)
        assert result["items"][0]["tight_convergence"] is True

        result = convergence_from_history(conn, max_window_hours=0.5)
        assert result["items"][0]["tight_convergence"] is False

    def test_coverage_metadata(self, conn):
        """Coverage dict reports anchored vs total rows."""
        self._seed_history(conn, "0xaaa", "mkt-1", anchor_ts=1700000000)
        self._seed_history(conn, "0xbbb", "mkt-1", anchor_ts=1700003600)
        self._seed_history(conn, "0xccc", "mkt-2", anchor_ts=0)  # unanchored

        result = convergence_from_history(conn)
        cov = result["coverage"]
        assert cov["total_rows"] == 3
        assert cov["anchored_rows"] == 2
        assert cov["coverage_pct"] == 66.7
        assert "caveat" in cov

    def test_insider_score_filter(self, conn):
        """Wallets below min_insider_score excluded from convergence."""
        self._seed_history(conn, "0xaaa", "mkt-1", anchor_ts=1700000000, insider_score=80.0)
        self._seed_history(conn, "0xbbb", "mkt-1", anchor_ts=1700003600, insider_score=40.0)

        result = convergence_from_history(conn, min_insider_score=55.0)
        assert len(result["items"]) == 0  # Only 1 wallet passes score gate

    def test_outcome_separation(self, conn):
        """Wallets on YES and NO counted separately."""
        self._seed_history(conn, "0xaaa", "mkt-1", outcome="YES", anchor_ts=1700000000)
        self._seed_history(conn, "0xbbb", "mkt-1", outcome="NO", anchor_ts=1700003600)

        result = convergence_from_history(conn, min_insider_score=55.0)
        assert len(result["items"]) == 0  # 1 on YES, 1 on NO — not convergence

    def test_first_snapshot_scores_used(self, conn):
        """When same wallet has multiple snapshots, first one's score is used."""
        # First snapshot: high score
        self._seed_history(conn, "0xaaa", "mkt-1", anchor_ts=1700000000,
                          insider_score=80.0, analyzed_ts=1700001000)
        # Second snapshot: score dropped (but should use first)
        self._seed_history(conn, "0xaaa", "mkt-1", anchor_ts=1700000000,
                          insider_score=30.0, analyzed_ts=1700002000)
        self._seed_history(conn, "0xbbb", "mkt-1", anchor_ts=1700003600,
                          insider_score=75.0, analyzed_ts=1700004000)

        result = convergence_from_history(conn, min_insider_score=55.0)
        assert len(result["items"]) == 1
        # avg_insider_score uses first-seen score (80) for 0xaaa, not the later 30
        assert result["items"][0]["avg_insider_score"] == 77.5  # (80+75)/2

    def test_three_wallet_window(self, conn):
        """Three wallets: window measured from earliest to latest anchor."""
        self._seed_history(conn, "0xaaa", "mkt-1", anchor_ts=1700000000)
        self._seed_history(conn, "0xbbb", "mkt-1", anchor_ts=1700007200)  # +2h
        self._seed_history(conn, "0xccc", "mkt-1", anchor_ts=1700036000)  # +10h

        result = convergence_from_history(conn, min_insider_score=55.0)
        m = result["items"][0]
        assert m["insider_count"] == 3
        assert m["window_hours"] == 10.0

    def test_open_only_filter(self, conn):
        """Closed positions excluded when open_only=True."""
        self._seed_history(conn, "0xaaa", "mkt-1", anchor_ts=1700000000, is_open=True)
        self._seed_history(conn, "0xbbb", "mkt-1", anchor_ts=1700003600, is_open=False)

        result = convergence_from_history(conn, open_only=True)
        assert len(result["items"]) == 0  # Only 1 open wallet

        result = convergence_from_history(conn, open_only=False)
        assert len(result["items"]) == 1  # Both counted
