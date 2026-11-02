"""Tests for stink_bid_runner.py — runtime wiring for the stink bid system.

Tests cover synchronous methods only (no async WS loop):
- _on_tick dispatches ticks to resting orders
- sync_watchlist matches tap_state to resting orders
- EspnOutageTracker lifecycle (3 failures -> pause, success -> resume)
- discover_and_place skips when outage_tracker paused
- poll_espn_and_gate cancels orders when sets >= 2
- reprice_all skips when favorite flipped
"""

import json
import os
import sqlite3
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from db import init_db
from sniper.feeds.polymarket_trade_tap import TradeTapState, TradeTick
from sniper.feeds.tennis_scores import EspnOutageTracker, MatchState
from sniper.services.paper_limit_engine import PaperLimitEngine, compute_bid_price


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Module-level registry so the autouse Gamma-seed patch can mirror
# whatever rows each test pre-inserts into market_metadata.
_ACTIVE_TEST_DB_PATHS: list[str] = []


def _init_test_db() -> tuple[sqlite3.Connection, str]:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    _ACTIVE_TEST_DB_PATHS.append(path)
    return conn, path


def _gamma_pair(condition_id, title, volume_24h=50000.0, end_date=""):
    """Build a synthetic Gamma pair that matches the runner's discriminator.

    market.slug == event.slug AND market.question == event.title AND empty groupItemTitle.
    """
    return {
        "event": {
            "title": title,
            "slug": condition_id,
            "endDate": end_date,
            "volume24hr": volume_24h,
        },
        "market": {
            "conditionId": condition_id,
            "slug": condition_id,
            "question": title,
            "groupItemTitle": "",
        },
    }


def _synth_gamma_pairs_from_last_test_db():
    """Read is_open=1 rows from the most recent test DB and synth pairs.

    Used by the autouse fixture so existing discover_and_place tests still
    work: whatever they pre-insert into market_metadata is echoed back as a
    Gamma seed response, preserving the old "scan all is_open=1" semantics
    in the unit-test context.
    """
    if not _ACTIVE_TEST_DB_PATHS:
        return []
    path = _ACTIVE_TEST_DB_PATHS[-1]
    try:
        conn = sqlite3.connect(path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT condition_id, title, slug, event_slug, volume_24h, end_date_iso "
                "FROM market_metadata WHERE is_open = 1"
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return []
    return [
        {
            "event": {
                "title": r["title"] or "",
                "slug": r["event_slug"] or r["condition_id"],
                "endDate": r["end_date_iso"] or "",
                "volume24hr": r["volume_24h"] or 0.0,
            },
            "market": {
                "conditionId": r["condition_id"],
                "slug": r["slug"] or r["event_slug"] or r["condition_id"],
                "question": r["title"] or "",
                "groupItemTitle": "",
            },
        }
        for r in rows
    ]


def _make_tick(
    token_id: str = "tok_fav",
    condition_id: str = "cond_1",
    price: float = 0.40,
    size: float = 100.0,
    side: str = "SELL",
    timestamp_ms: int = 1_712_000_000_000,
) -> TradeTick:
    return TradeTick(
        token_id=token_id,
        condition_id=condition_id,
        price=price,
        size=size,
        side=side,
        timestamp_ms=timestamp_ms,
    )


def _place_order(
    engine: PaperLimitEngine,
    strategy_id: int = 99,
    condition_id: str = "cond_1",
    favorite_token_id: str = "tok_fav",
    favorite_token_label: str = "Player A",
    favorite_price: float = 0.70,
    tour: str = "atp",
    player1: str = "Djokovic",
    player2: str = "Alcaraz",
) -> int:
    order_id = engine.place_order(
        strategy_id=strategy_id,
        condition_id=condition_id,
        market_title="ATP: Djokovic vs Alcaraz",
        favorite_token_id=favorite_token_id,
        favorite_token_label=favorite_token_label,
        favorite_price=favorite_price,
        min_fill_size=50.0,
        tour=tour,
        player1=player1,
        player2=player2,
    )
    assert order_id is not None
    return order_id


def _insert_market_metadata(conn, condition_id, title, volume_24h=50000.0, is_open=1):
    """Insert a row into market_metadata for test discovery."""
    conn.execute(
        """INSERT OR REPLACE INTO market_metadata
           (condition_id, title, slug, event_slug, volume_24h, is_open)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (condition_id, title, "slug-" + condition_id, "event-slug", volume_24h, is_open),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# _load_strategy_params / _normalize_params / _PARAM_ALIASES
# ---------------------------------------------------------------------------


class TestLoadStrategyParams:
    def test_loads_params_from_db(self):
        """Params stored in strategies.parameters JSON are loaded and returned."""
        conn, path = _init_test_db()
        try:
            conn.execute(
                """INSERT INTO strategies (id, wallet, label, strategy_type, parameters)
                   VALUES (?, ?, ?, ?, ?)""",
                (42, "test", "stink bid", "stink_bid_v1",
                 json.dumps({"dip_pct": 0.20, "min_favorite": 0.60})),
            )
            conn.commit()

            from sniper.services.stink_bid_runner import _load_strategy_params
            params = _load_strategy_params(path, 42)
            assert params["dip_pct"] == 0.20
            assert params["min_favorite"] == 0.60
        finally:
            conn.close()
            os.unlink(path)

    def test_missing_strategy_returns_empty(self):
        """Missing strategy ID returns empty dict (uses defaults)."""
        conn, path = _init_test_db()
        try:
            from sniper.services.stink_bid_runner import _load_strategy_params
            params = _load_strategy_params(path, 9999)
            assert params == {}
        finally:
            conn.close()
            os.unlink(path)

    def test_invalid_json_returns_empty(self):
        """Invalid JSON in parameters returns empty dict."""
        conn, path = _init_test_db()
        try:
            conn.execute(
                """INSERT INTO strategies (id, wallet, label, strategy_type, parameters)
                   VALUES (?, ?, ?, ?, ?)""",
                (42, "test", "stink bid", "stink_bid_v1", "NOT-JSON{{{"),
            )
            conn.commit()

            from sniper.services.stink_bid_runner import _load_strategy_params
            params = _load_strategy_params(path, 42)
            assert params == {}
        finally:
            conn.close()
            os.unlink(path)


class TestNormalizeParams:
    def test_alias_normalization(self):
        """Spec-style key names are normalized to runner canonical names."""
        from sniper.services.stink_bid_runner import _normalize_params
        raw = {
            "min_favorite_price": 0.60,
            "min_bid_price": 0.08,
            "max_resting_orders": 5,
            "max_position_size_usdc": 50.0,
            "dip_pct": 0.25,  # already canonical — should pass through
        }
        result = _normalize_params(raw)
        assert result["min_favorite"] == 0.60
        assert result["min_bid"] == 0.08
        assert result["max_resting"] == 5
        assert result["position_size_usdc"] == 50.0
        assert result["dip_pct"] == 0.25

    def test_canonical_names_pass_through(self):
        """Runner-native key names are unchanged by normalization."""
        from sniper.services.stink_bid_runner import _normalize_params
        raw = {"min_favorite": 0.55, "max_resting": 3}
        result = _normalize_params(raw)
        assert result == raw

    def test_db_params_applied_to_runner(self):
        """DB params (with spec-style keys) override defaults in StinkBidRunner."""
        conn, path = _init_test_db()
        try:
            from sniper.services.stink_bid_runner import StinkBidRunner
            runner = StinkBidRunner(
                db_path=path, strategy_id=99,
                params={"min_favorite_price": 0.70, "max_resting_orders": 10},
            )
            assert runner._min_favorite == 0.70
            assert runner._max_resting == 10
            # Defaults for non-overridden keys
            assert runner._dip_pct == 0.30
        finally:
            conn.close()
            os.unlink(path)


# ---------------------------------------------------------------------------
# EspnOutageTracker lifecycle
# ---------------------------------------------------------------------------


class TestEspnOutageTrackerLifecycle:
    def test_initial_state_not_paused(self):
        tracker = EspnOutageTracker(pause_after=3, cancel_after_min=1)
        assert not tracker.should_pause_new_orders
        assert not tracker.should_cancel_all

    def test_three_failures_pause(self):
        tracker = EspnOutageTracker(pause_after=3, cancel_after_min=1)
        tracker.record_failure()
        tracker.record_failure()
        assert not tracker.should_pause_new_orders
        tracker.record_failure()
        assert tracker.should_pause_new_orders

    def test_success_resets_pause(self):
        tracker = EspnOutageTracker(pause_after=3, cancel_after_min=1)
        tracker.record_failure()
        tracker.record_failure()
        tracker.record_failure()
        assert tracker.should_pause_new_orders
        tracker.record_success()
        assert not tracker.should_pause_new_orders

    def test_partial_failures_no_pause(self):
        tracker = EspnOutageTracker(pause_after=3, cancel_after_min=1)
        tracker.record_failure()
        tracker.record_failure()
        tracker.record_success()
        tracker.record_failure()
        assert not tracker.should_pause_new_orders


# ---------------------------------------------------------------------------
# _on_tick dispatches to resting orders
# ---------------------------------------------------------------------------


class TestOnTick:
    def test_tick_dispatches_to_matching_order(self):
        """A tick matching a resting order at fill price should trigger a fill."""
        conn, path = _init_test_db()
        try:
            engine = PaperLimitEngine(conn)
            oid = _place_order(engine, strategy_id=99, condition_id="cond_1",
                               favorite_token_id="tok_A")

            from sniper.services.stink_bid_runner import StinkBidRunner
            runner = StinkBidRunner(db_path=path, strategy_id=99)

            # Send a tick matching token at or below bid price
            order = conn.execute(
                "SELECT bid_price FROM paper_limit_orders WHERE order_id = ?",
                (oid,),
            ).fetchone()
            bid = float(order["bid_price"])

            tick = _make_tick(token_id="tok_A", price=bid - 0.01, size=100.0)
            runner._on_tick(tick)

            # Verify order is now filled
            row = conn.execute(
                "SELECT state FROM paper_limit_orders WHERE order_id = ?",
                (oid,),
            ).fetchone()
            assert row["state"] == "filled"
        finally:
            conn.close()
            os.unlink(path)

    def test_tick_only_matches_correct_token(self):
        """Ticks for unmatched tokens should not trigger fills."""
        conn, path = _init_test_db()
        try:
            engine = PaperLimitEngine(conn)
            oid1 = _place_order(engine, strategy_id=99, condition_id="cond_1",
                                favorite_token_id="tok_A")
            oid2 = _place_order(engine, strategy_id=99, condition_id="cond_2",
                                favorite_token_id="tok_B")

            from sniper.services.stink_bid_runner import StinkBidRunner
            runner = StinkBidRunner(db_path=path, strategy_id=99)

            # Get bid price for order 1
            row1 = conn.execute(
                "SELECT bid_price FROM paper_limit_orders WHERE order_id = ?",
                (oid1,),
            ).fetchone()
            bid1 = float(row1["bid_price"])

            # Send tick matching tok_A at fill price
            tick = _make_tick(token_id="tok_A", price=bid1 - 0.01, size=100.0)
            runner._on_tick(tick)

            # Order 1 should be filled, order 2 still resting
            s1 = conn.execute("SELECT state FROM paper_limit_orders WHERE order_id = ?",
                              (oid1,)).fetchone()
            s2 = conn.execute("SELECT state FROM paper_limit_orders WHERE order_id = ?",
                              (oid2,)).fetchone()
            assert s1["state"] == "filled"
            assert s2["state"] == "resting"
        finally:
            conn.close()
            os.unlink(path)

    def test_tick_above_bid_no_fill(self):
        """A tick above the bid price should not fill."""
        conn, path = _init_test_db()
        try:
            engine = PaperLimitEngine(conn)
            oid = _place_order(engine, strategy_id=99, condition_id="cond_1",
                               favorite_token_id="tok_A")

            from sniper.services.stink_bid_runner import StinkBidRunner
            runner = StinkBidRunner(db_path=path, strategy_id=99)

            row = conn.execute(
                "SELECT bid_price FROM paper_limit_orders WHERE order_id = ?",
                (oid,),
            ).fetchone()
            bid = float(row["bid_price"])

            tick = _make_tick(token_id="tok_A", price=bid + 0.05, size=100.0)
            runner._on_tick(tick)

            s = conn.execute("SELECT state FROM paper_limit_orders WHERE order_id = ?",
                             (oid,)).fetchone()
            assert s["state"] == "resting"
        finally:
            conn.close()
            os.unlink(path)


# ---------------------------------------------------------------------------
# sync_watchlist
# ---------------------------------------------------------------------------


class TestSyncWatchlist:
    def test_watchlist_tracks_resting_orders(self):
        """Watched tokens should match favorite_token_ids of resting orders."""
        conn, path = _init_test_db()
        try:
            engine = PaperLimitEngine(conn)
            _place_order(engine, strategy_id=99, condition_id="cond_1",
                         favorite_token_id="tok_A")
            _place_order(engine, strategy_id=99, condition_id="cond_2",
                         favorite_token_id="tok_B")

            from sniper.services.stink_bid_runner import StinkBidRunner
            runner = StinkBidRunner(db_path=path, strategy_id=99)
            runner.sync_watchlist()

            assert runner._tap_state.watched_tokens == {"tok_A", "tok_B"}
        finally:
            conn.close()
            os.unlink(path)

    def test_watchlist_removes_cancelled_orders(self):
        """Cancelled orders should be unwatched."""
        conn, path = _init_test_db()
        try:
            engine = PaperLimitEngine(conn)
            oid1 = _place_order(engine, strategy_id=99, condition_id="cond_1",
                                favorite_token_id="tok_A")
            _place_order(engine, strategy_id=99, condition_id="cond_2",
                         favorite_token_id="tok_B")

            from sniper.services.stink_bid_runner import StinkBidRunner
            runner = StinkBidRunner(db_path=path, strategy_id=99)
            runner.sync_watchlist()
            assert "tok_A" in runner._tap_state.watched_tokens

            engine.cancel_order(oid1, "test")
            runner.sync_watchlist()
            assert "tok_A" not in runner._tap_state.watched_tokens
            assert "tok_B" in runner._tap_state.watched_tokens
        finally:
            conn.close()
            os.unlink(path)

    def test_watchlist_empty_when_no_resting(self):
        """No resting orders means empty watchlist."""
        conn, path = _init_test_db()
        try:
            from sniper.services.stink_bid_runner import StinkBidRunner
            runner = StinkBidRunner(db_path=path, strategy_id=99)
            runner.sync_watchlist()
            assert runner._tap_state.watched_tokens == set()
        finally:
            conn.close()
            os.unlink(path)


# ---------------------------------------------------------------------------
# discover_and_place — unit tests with mocked external calls
# ---------------------------------------------------------------------------


class TestDiscoverAndPlace:
    @pytest.fixture(autouse=True)
    def _mirror_metadata_as_gamma_seed(self, monkeypatch):
        """Echo pre-inserted market_metadata rows back as Gamma pairs so the
        existing tests' "pre-insert and call discover" pattern still works
        after the Gamma-seed scope restriction."""
        _ACTIVE_TEST_DB_PATHS.clear()
        monkeypatch.setattr(
            "sniper.services.stink_bid_runner._fetch_gamma_tennis_h2h",
            lambda _session: _synth_gamma_pairs_from_last_test_db(),
        )
        yield
        _ACTIVE_TEST_DB_PATHS.clear()

    def test_skips_when_outage_paused(self):
        """When ESPN outage tracker is paused, discover_and_place skips entirely."""
        conn, path = _init_test_db()
        try:
            from sniper.services.stink_bid_runner import StinkBidRunner
            runner = StinkBidRunner(db_path=path, strategy_id=99)

            # Force outage state on ATP tour
            for _ in range(5):
                runner._outage_tracker("atp").record_failure()
            assert runner._outage_tracker("atp").should_pause_new_orders

            # Insert a qualifying tennis market
            _insert_market_metadata(
                conn, "cond_tennis_1",
                "ATP: Djokovic vs Alcaraz - Winner",
                volume_24h=50000.0,
            )

            runner.discover_and_place()

            # No orders should have been placed
            resting = conn.execute(
                "SELECT COUNT(*) FROM paper_limit_orders WHERE strategy_id = 99"
            ).fetchone()[0]
            assert resting == 0
        finally:
            conn.close()
            os.unlink(path)

    def test_skips_non_tennis_markets(self):
        """Non-tennis market titles should be ignored."""
        conn, path = _init_test_db()
        try:
            from sniper.services.stink_bid_runner import StinkBidRunner
            runner = StinkBidRunner(db_path=path, strategy_id=99)

            _insert_market_metadata(
                conn, "cond_crypto_1",
                "Will BTC reach $100K?",
                volume_24h=100000.0,
            )

            # Mock fetch_market_by_condition to ensure it's never called
            with patch("sniper.services.stink_bid_runner.fetch_market_by_condition") as mock_fetch:
                runner.discover_and_place()
                mock_fetch.assert_not_called()

            resting = conn.execute(
                "SELECT COUNT(*) FROM paper_limit_orders WHERE strategy_id = 99"
            ).fetchone()[0]
            assert resting == 0
        finally:
            conn.close()
            os.unlink(path)

    def test_self_heals_when_gamma_returns_empty(self):
        """If Gamma returns no market for an is_open=1 row, the runner must
        mark that row is_open=0 so it is not rediscovered every cycle.

        Regression for the tennis-stink-bid incident (2026-04-20): two stale
        tennis rows remained is_open=1 in SQLite but returned 200 [] from
        Gamma. Every 15-min cycle re-scanned them, logged skipped_gamma, and
        never placed a single order.
        """
        conn, path = _init_test_db()
        try:
            from sniper.services.stink_bid_runner import StinkBidRunner
            runner = StinkBidRunner(db_path=path, strategy_id=99)

            _insert_market_metadata(
                conn, "cond_dead_tennis",
                "ATP: Jelena Ostapenko vs Mirra Andreeva",
                volume_24h=50000.0,
                is_open=1,
            )

            # Gamma returns empty for this condition (market no longer indexed)
            with patch(
                "sniper.services.stink_bid_runner.fetch_market_by_condition",
                return_value=None,
            ):
                runner.discover_and_place()

            # Row must be marked closed so next cycle skips it entirely
            row = conn.execute(
                "SELECT is_open FROM market_metadata WHERE condition_id = ?",
                ("cond_dead_tennis",),
            ).fetchone()
            assert row is not None
            assert row["is_open"] == 0, (
                "runner must mark market is_open=0 when Gamma returns empty"
            )

            # Second pass: the row should no longer be picked up even though
            # we would mock Gamma to return empty again.
            with patch(
                "sniper.services.stink_bid_runner.fetch_market_by_condition",
                return_value=None,
            ) as mock_fetch:
                runner.discover_and_place()
                # Gamma should NOT be called because the row is is_open=0 now
                mock_fetch.assert_not_called()

            # No orders placed
            resting = conn.execute(
                "SELECT COUNT(*) FROM paper_limit_orders WHERE strategy_id = 99"
            ).fetchone()[0]
            assert resting == 0
        finally:
            conn.close()
            os.unlink(path)

    def test_does_not_self_heal_on_gamma_exception(self):
        """A transient Gamma exception must NOT mark the row closed — only an
        explicit empty response (200 []) indicates the market is gone.
        """
        conn, path = _init_test_db()
        try:
            from sniper.services.stink_bid_runner import StinkBidRunner
            runner = StinkBidRunner(db_path=path, strategy_id=99)

            _insert_market_metadata(
                conn, "cond_flaky_tennis",
                "ATP: Djokovic vs Alcaraz",
                volume_24h=50000.0,
                is_open=1,
            )

            # Gamma raises (network hiccup, 503, timeout)
            with patch(
                "sniper.services.stink_bid_runner.fetch_market_by_condition",
                side_effect=RuntimeError("boom"),
            ):
                runner.discover_and_place()

            # Row must STILL be is_open=1 — transient failures don't close markets
            row = conn.execute(
                "SELECT is_open FROM market_metadata WHERE condition_id = ?",
                ("cond_flaky_tennis",),
            ).fetchone()
            assert row["is_open"] == 1, (
                "transient Gamma exception must NOT mark the market closed"
            )
        finally:
            conn.close()
            os.unlink(path)

    def test_places_order_for_qualifying_market(self):
        """A qualifying tennis market should result in an order placement."""
        conn, path = _init_test_db()
        try:
            from sniper.services.stink_bid_runner import StinkBidRunner
            runner = StinkBidRunner(db_path=path, strategy_id=99)

            _insert_market_metadata(
                conn, "cond_tennis_1",
                "ATP: Djokovic vs Alcaraz - Match Winner",
                volume_24h=50000.0,
            )

            gamma_response = {
                "outcomes": json.dumps(["Djokovic", "Alcaraz"]),
                "clobTokenIds": json.dumps(["tok_djok", "tok_alc"]),
                "outcomePrices": json.dumps(["0.75", "0.25"]),
            }

            match_state = MatchState(
                sets_completed=0,
                score_by_set=[],
                match_status="scheduled",
                source="espn",
            )

            # Discovery path calls fetch_scoreboard + parse_scoreboard + find_match
            # (not get_match_state) so we mock the primitives.
            fake_competition = {"status": {"type": {"name": "STATUS_SCHEDULED"}}}
            fake_entry = {"player1": "Djokovic", "player2": "Alcaraz", "competition": fake_competition}

            with patch("sniper.services.stink_bid_runner.fetch_market_by_condition", return_value=gamma_response), \
                 patch("sniper.services.stink_bid_runner.fetch_scoreboard", return_value={"events": []}), \
                 patch("sniper.services.stink_bid_runner.parse_scoreboard", return_value=[fake_entry]), \
                 patch("sniper.services.stink_bid_runner.find_match", return_value=fake_entry), \
                 patch("sniper.services.stink_bid_runner.get_sets_completed", return_value=match_state):
                runner.discover_and_place()

            resting = conn.execute(
                "SELECT COUNT(*) FROM paper_limit_orders WHERE strategy_id = 99 AND state = 'resting'"
            ).fetchone()[0]
            assert resting == 1

            order = conn.execute(
                "SELECT * FROM paper_limit_orders WHERE strategy_id = 99"
            ).fetchone()
            assert order["favorite_token_id"] == "tok_djok"
            assert order["favorite_token_label"] == "Djokovic"
            assert order["condition_id"] == "cond_tennis_1"
        finally:
            conn.close()
            os.unlink(path)

    def test_place_order_lock_does_not_abort_cycle(self, caplog):
        """A DB lock on engine.place_order for one market must not abort the
        whole cycle. The runner should log a WARNING, increment skipped_lock,
        and continue processing subsequent markets."""
        conn, path = _init_test_db()
        try:
            from sniper.services.stink_bid_runner import StinkBidRunner
            runner = StinkBidRunner(db_path=path, strategy_id=99)

            _insert_market_metadata(
                conn, "cond_tennis_first",
                "ATP: Djokovic vs Alcaraz - Match Winner",
                volume_24h=50000.0,
            )
            _insert_market_metadata(
                conn, "cond_tennis_second",
                "ATP: Sinner vs Medvedev - Match Winner",
                volume_24h=50000.0,
            )

            gamma_first = {
                "outcomes": json.dumps(["Djokovic", "Alcaraz"]),
                "clobTokenIds": json.dumps(["tok_djok", "tok_alc"]),
                "outcomePrices": json.dumps(["0.75", "0.25"]),
            }
            gamma_second = {
                "outcomes": json.dumps(["Sinner", "Medvedev"]),
                "clobTokenIds": json.dumps(["tok_sin", "tok_med"]),
                "outcomePrices": json.dumps(["0.70", "0.30"]),
            }
            gamma_by_cond = {
                "cond_tennis_first": gamma_first,
                "cond_tennis_second": gamma_second,
            }

            match_state = MatchState(
                sets_completed=0,
                score_by_set=[],
                match_status="scheduled",
                source="espn",
            )
            fake_competition = {"status": {"type": {"name": "STATUS_SCHEDULED"}}}

            def fake_find_match(_parsed, p1, _p2):
                return {"player1": p1, "player2": _p2, "competition": fake_competition}

            # First call raises lock, second call succeeds.
            place_side_effects = [
                sqlite3.OperationalError("database is locked"),
                42,
            ]
            place_mock = MagicMock(side_effect=place_side_effects)

            caplog.set_level("WARNING", logger="__main__")
            with patch(
                "sniper.services.stink_bid_runner.fetch_market_by_condition",
                side_effect=lambda _s, cid: gamma_by_cond[cid],
            ), patch("sniper.services.stink_bid_runner.fetch_scoreboard", return_value={"events": []}), \
                 patch("sniper.services.stink_bid_runner.parse_scoreboard", return_value=[{}]), \
                 patch("sniper.services.stink_bid_runner.find_match", side_effect=fake_find_match), \
                 patch("sniper.services.stink_bid_runner.get_sets_completed", return_value=match_state), \
                 patch.object(PaperLimitEngine, "place_order", place_mock):
                runner.discover_and_place()

            # Both markets should have been attempted — proves the loop
            # continued past the lock rather than aborting.
            assert place_mock.call_count == 2

            # A WARNING log line should have been emitted for the lock.
            warnings = [
                r for r in caplog.records
                if r.levelname == "WARNING" and "place_order lock" in r.getMessage()
            ]
            assert len(warnings) == 1, (
                f"expected 1 place_order lock warning, got {len(warnings)}: "
                f"{[r.getMessage() for r in caplog.records]}"
            )
            assert "cond_tennis_first" in warnings[0].getMessage()
        finally:
            conn.close()
            os.unlink(path)

    def test_skips_below_min_favorite(self):
        """Markets where no outcome exceeds min_favorite price should be skipped."""
        conn, path = _init_test_db()
        try:
            from sniper.services.stink_bid_runner import StinkBidRunner
            runner = StinkBidRunner(
                db_path=path, strategy_id=99,
                params={"min_favorite": 0.65},
            )

            _insert_market_metadata(
                conn, "cond_tennis_1",
                "ATP: Djokovic vs Alcaraz - Match Winner",
                volume_24h=50000.0,
            )

            gamma_response = {
                "outcomes": json.dumps(["Djokovic", "Alcaraz"]),
                "clobTokenIds": json.dumps(["tok_djok", "tok_alc"]),
                "outcomePrices": json.dumps(["0.55", "0.45"]),
            }

            with patch("sniper.services.stink_bid_runner.fetch_market_by_condition", return_value=gamma_response):
                runner.discover_and_place()

            resting = conn.execute(
                "SELECT COUNT(*) FROM paper_limit_orders WHERE strategy_id = 99"
            ).fetchone()[0]
            assert resting == 0
        finally:
            conn.close()
            os.unlink(path)

    def test_skips_when_sets_completed_ge_2(self):
        """Markets where sets_completed >= 2 should be skipped."""
        conn, path = _init_test_db()
        try:
            from sniper.services.stink_bid_runner import StinkBidRunner
            runner = StinkBidRunner(db_path=path, strategy_id=99)

            _insert_market_metadata(
                conn, "cond_tennis_1",
                "ATP: Djokovic vs Alcaraz - Match Winner",
                volume_24h=50000.0,
            )

            gamma_response = {
                "outcomes": json.dumps(["Djokovic", "Alcaraz"]),
                "clobTokenIds": json.dumps(["tok_djok", "tok_alc"]),
                "outcomePrices": json.dumps(["0.75", "0.25"]),
            }

            match_state = MatchState(
                sets_completed=2,
                score_by_set=[(6, 4), (7, 5)],
                match_status="live",
                source="espn",
            )

            # Discovery path uses fetch_scoreboard + parse_scoreboard + find_match
            fake_competition = {"status": {"type": {"name": "STATUS_IN_PROGRESS"}}}
            fake_entry = {"player1": "Djokovic", "player2": "Alcaraz", "competition": fake_competition}

            with patch("sniper.services.stink_bid_runner.fetch_market_by_condition", return_value=gamma_response), \
                 patch("sniper.services.stink_bid_runner.fetch_scoreboard", return_value={"events": []}), \
                 patch("sniper.services.stink_bid_runner.parse_scoreboard", return_value=[fake_entry]), \
                 patch("sniper.services.stink_bid_runner.find_match", return_value=fake_entry), \
                 patch("sniper.services.stink_bid_runner.get_sets_completed", return_value=match_state):
                runner.discover_and_place()

            resting = conn.execute(
                "SELECT COUNT(*) FROM paper_limit_orders WHERE strategy_id = 99"
            ).fetchone()[0]
            assert resting == 0
        finally:
            conn.close()
            os.unlink(path)


# ---------------------------------------------------------------------------
# poll_espn_and_gate — cancel orders when sets >= 2
# ---------------------------------------------------------------------------


class TestPollEspnAndGate:
    def test_cancels_order_when_sets_ge_2(self):
        """Resting orders should be cancelled when ESPN shows sets >= 2."""
        conn, path = _init_test_db()
        try:
            engine = PaperLimitEngine(conn)
            oid = _place_order(
                engine, strategy_id=99, condition_id="cond_1",
                favorite_token_id="tok_A",
                tour="atp", player1="Djokovic", player2="Alcaraz",
            )

            from sniper.services.stink_bid_runner import StinkBidRunner
            runner = StinkBidRunner(db_path=path, strategy_id=99)

            # Mock ESPN returning sets_completed=2
            match_state = MatchState(
                sets_completed=2,
                score_by_set=[(6, 4), (7, 5)],
                match_status="live",
                source="espn",
            )
            with patch("sniper.services.stink_bid_runner.fetch_scoreboard") as mock_fetch_sb, \
                 patch("sniper.services.stink_bid_runner.parse_scoreboard") as mock_parse, \
                 patch("sniper.services.stink_bid_runner.find_match") as mock_find, \
                 patch("sniper.services.stink_bid_runner.get_sets_completed", return_value=match_state):
                mock_fetch_sb.return_value = {"events": []}
                mock_parse.return_value = [{"player1": "Novak Djokovic",
                                             "player2": "Carlos Alcaraz",
                                             "competition": {}}]
                mock_find.return_value = {"player1": "Novak Djokovic",
                                          "player2": "Carlos Alcaraz",
                                          "competition": {}}

                runner.poll_espn_and_gate()

            row = conn.execute(
                "SELECT state, cancel_reason FROM paper_limit_orders WHERE order_id = ?",
                (oid,),
            ).fetchone()
            assert row["state"] == "cancelled"
            assert "sets_completed" in row["cancel_reason"]
        finally:
            conn.close()
            os.unlink(path)

    def test_keeps_order_when_sets_lt_2(self):
        """Resting orders should stay when ESPN shows sets < 2."""
        conn, path = _init_test_db()
        try:
            engine = PaperLimitEngine(conn)
            oid = _place_order(
                engine, strategy_id=99, condition_id="cond_1",
                favorite_token_id="tok_A",
                tour="atp", player1="Djokovic", player2="Alcaraz",
            )

            from sniper.services.stink_bid_runner import StinkBidRunner
            runner = StinkBidRunner(db_path=path, strategy_id=99)

            match_state = MatchState(
                sets_completed=1,
                score_by_set=[(6, 4)],
                match_status="live",
                source="espn",
            )
            with patch("sniper.services.stink_bid_runner.fetch_scoreboard") as mock_fetch_sb, \
                 patch("sniper.services.stink_bid_runner.parse_scoreboard") as mock_parse, \
                 patch("sniper.services.stink_bid_runner.find_match") as mock_find, \
                 patch("sniper.services.stink_bid_runner.get_sets_completed", return_value=match_state):
                mock_fetch_sb.return_value = {"events": []}
                mock_parse.return_value = [{"player1": "Novak Djokovic",
                                             "player2": "Carlos Alcaraz",
                                             "competition": {}}]
                mock_find.return_value = {"player1": "Novak Djokovic",
                                          "player2": "Carlos Alcaraz",
                                          "competition": {}}

                runner.poll_espn_and_gate()

            row = conn.execute(
                "SELECT state FROM paper_limit_orders WHERE order_id = ?",
                (oid,),
            ).fetchone()
            assert row["state"] == "resting"
        finally:
            conn.close()
            os.unlink(path)

    def test_records_outage_on_espn_failure(self):
        """ESPN fetch failure should be recorded on the outage tracker."""
        conn, path = _init_test_db()
        try:
            engine = PaperLimitEngine(conn)
            _place_order(
                engine, strategy_id=99, condition_id="cond_1",
                favorite_token_id="tok_A",
                tour="atp", player1="Djokovic", player2="Alcaraz",
            )

            from sniper.services.stink_bid_runner import StinkBidRunner
            runner = StinkBidRunner(db_path=path, strategy_id=99)

            with patch("sniper.services.stink_bid_runner.fetch_scoreboard", return_value=None):
                runner.poll_espn_and_gate()

            # One failure recorded on the ATP tour tracker
            assert runner._outage_tracker("atp")._consecutive_failures == 1
        finally:
            conn.close()
            os.unlink(path)


# ---------------------------------------------------------------------------
# reprice_all — reprice resting orders based on current market prices
# ---------------------------------------------------------------------------


class TestRepriceAll:
    def test_skips_when_favorite_flipped(self):
        """Orders where the favorite price dropped below 0.50 should be cancelled, not repriced."""
        conn, path = _init_test_db()
        try:
            engine = PaperLimitEngine(conn)
            oid = _place_order(
                engine, strategy_id=99, condition_id="cond_1",
                favorite_token_id="tok_A",
                favorite_token_label="Djokovic",
                favorite_price=0.70,
            )

            from sniper.services.stink_bid_runner import StinkBidRunner
            runner = StinkBidRunner(db_path=path, strategy_id=99)

            # Gamma returns flipped prices (favorite now at 0.30)
            gamma_response = {
                "outcomes": json.dumps(["Djokovic", "Alcaraz"]),
                "clobTokenIds": json.dumps(["tok_A", "tok_B"]),
                "outcomePrices": json.dumps(["0.30", "0.70"]),
            }

            with patch("sniper.services.stink_bid_runner.fetch_market_by_condition", return_value=gamma_response):
                runner.reprice_all()

            row = conn.execute(
                "SELECT state, cancel_reason FROM paper_limit_orders WHERE order_id = ?",
                (oid,),
            ).fetchone()
            assert row["state"] == "cancelled"
            assert "favorite_flipped" in row["cancel_reason"]
        finally:
            conn.close()
            os.unlink(path)

    def test_reprices_when_price_moved(self):
        """Orders should be repriced when the favorite price moves materially."""
        conn, path = _init_test_db()
        try:
            engine = PaperLimitEngine(conn)
            oid = _place_order(
                engine, strategy_id=99, condition_id="cond_1",
                favorite_token_id="tok_A",
                favorite_token_label="Djokovic",
                favorite_price=0.70,
            )

            old_bid = conn.execute(
                "SELECT bid_price FROM paper_limit_orders WHERE order_id = ?",
                (oid,),
            ).fetchone()["bid_price"]

            from sniper.services.stink_bid_runner import StinkBidRunner
            runner = StinkBidRunner(db_path=path, strategy_id=99)

            # Favorite price moved from 0.70 to 0.85
            gamma_response = {
                "outcomes": json.dumps(["Djokovic", "Alcaraz"]),
                "clobTokenIds": json.dumps(["tok_A", "tok_B"]),
                "outcomePrices": json.dumps(["0.85", "0.15"]),
            }

            with patch("sniper.services.stink_bid_runner.fetch_market_by_condition", return_value=gamma_response):
                runner.reprice_all()

            new_bid = conn.execute(
                "SELECT bid_price FROM paper_limit_orders WHERE order_id = ?",
                (oid,),
            ).fetchone()["bid_price"]

            # With dip_pct=0.30: old = 0.70*0.70=0.49, new = 0.85*0.70=0.595
            # Python's round(0.595, 2) = 0.59 (banker's rounding)
            assert new_bid != old_bid
            assert new_bid == compute_bid_price(0.85, dip_pct=0.30)
        finally:
            conn.close()
            os.unlink(path)


# ---------------------------------------------------------------------------
# Tour-fallback regression — 2026-04-17
# ---------------------------------------------------------------------------


class TestTourFallback:
    """Regression for 2026-04-17 bug: WTA matches on tournaments not named "WTA"
    (e.g. Porsche Tennis Grand Prix) were misrouted to ATP scoreboard and never
    matched, silently skipping every WTA-draw market."""

    @pytest.fixture(autouse=True)
    def _mirror_metadata_as_gamma_seed(self, monkeypatch):
        _ACTIVE_TEST_DB_PATHS.clear()
        monkeypatch.setattr(
            "sniper.services.stink_bid_runner._fetch_gamma_tennis_h2h",
            lambda _session: _synth_gamma_pairs_from_last_test_db(),
        )
        yield
        _ACTIVE_TEST_DB_PATHS.clear()

    def test_wta_match_placed_even_when_title_has_no_wta_keyword(self):
        """Title lacks "WTA" keyword so _extract_tour returns "atp", but the
        match is actually in the WTA scoreboard. With tour fallback, the order
        still places and records tour='wta' (the tour where find_match hit)."""
        conn, path = _init_test_db()
        try:
            from sniper.services.stink_bid_runner import StinkBidRunner
            runner = StinkBidRunner(db_path=path, strategy_id=77)

            _insert_market_metadata(
                conn,
                "cond_porsche_1",
                "Porsche Tennis Grand Prix: Ostapenko vs Andreeva",
                volume_24h=500000.0,
            )

            gamma_response = {
                "outcomes": json.dumps(["Ostapenko", "Andreeva"]),
                "clobTokenIds": json.dumps(["tok_ost", "tok_and"]),
                "outcomePrices": json.dumps(["0.62", "0.38"]),
            }

            # ATP scoreboard is empty; WTA scoreboard has the match.
            wta_entry = {
                "player1": "Ostapenko",
                "player2": "Andreeva",
                "competition": {"status": {"type": {"name": "STATUS_SCHEDULED"}}},
            }

            def _fake_fetch_scoreboard(tour):
                return {"events": [wta_entry]} if tour == "wta" else {"events": []}

            def _fake_parse_scoreboard(sb):
                return sb.get("events", []) if sb else []

            def _fake_find_match(parsed, p1, p2):
                # Only return a hit when the parsed list contains WTA entry
                if parsed and any(
                    e.get("player1") == "Ostapenko" for e in parsed
                ):
                    return wta_entry
                return None

            match_state = MatchState(
                sets_completed=0,
                score_by_set=[],
                match_status="scheduled",
                source="espn",
            )

            with patch("sniper.services.stink_bid_runner.fetch_market_by_condition", return_value=gamma_response), \
                 patch("sniper.services.stink_bid_runner.fetch_scoreboard", side_effect=_fake_fetch_scoreboard), \
                 patch("sniper.services.stink_bid_runner.parse_scoreboard", side_effect=_fake_parse_scoreboard), \
                 patch("sniper.services.stink_bid_runner.find_match", side_effect=_fake_find_match), \
                 patch("sniper.services.stink_bid_runner.get_sets_completed", return_value=match_state):
                runner.discover_and_place()

            orders = conn.execute(
                "SELECT condition_id, tour, state FROM paper_limit_orders WHERE strategy_id = 77"
            ).fetchall()
            assert len(orders) == 1, f"expected 1 order, got {len(orders)}"
            assert orders[0]["condition_id"] == "cond_porsche_1"
            assert orders[0]["tour"] == "wta", (
                f"order must record tour=wta (where match was found), got {orders[0]['tour']!r}"
            )
        finally:
            conn.close()
            os.unlink(path)


# ---------------------------------------------------------------------------
# Discover-cycle summary log — 2026-04-17
# ---------------------------------------------------------------------------


class TestDiscoverCycleSummary:
    @pytest.fixture(autouse=True)
    def _mirror_metadata_as_gamma_seed(self, monkeypatch):
        _ACTIVE_TEST_DB_PATHS.clear()
        monkeypatch.setattr(
            "sniper.services.stink_bid_runner._fetch_gamma_tennis_h2h",
            lambda _session: _synth_gamma_pairs_from_last_test_db(),
        )
        yield
        _ACTIVE_TEST_DB_PATHS.clear()

    """Every discover_and_place call must emit exactly one INFO-level summary
    line, even when zero orders are placed. This closes the observability gap
    that hid the 2026-04-17 tour-routing bug — zero orders with no log output
    is no longer an indistinguishable state."""

    def test_summary_log_emitted_on_empty_cycle(self, caplog):
        import logging
        conn, path = _init_test_db()
        try:
            from sniper.services.stink_bid_runner import StinkBidRunner
            runner = StinkBidRunner(db_path=path, strategy_id=1)
            # No markets inserted — scanned=0, tennis=0, placed=0

            with caplog.at_level(logging.INFO, logger="sniper.services.stink_bid_runner"):
                runner.discover_and_place()

            summaries = [
                r.getMessage() for r in caplog.records
                if "discover_cycle" in r.getMessage()
            ]
            assert len(summaries) == 1, (
                f"expected exactly 1 discover_cycle summary, got {len(summaries)}: {summaries}"
            )
            msg = summaries[0]
            assert "scanned=0" in msg
            assert "tennis=0" in msg
            assert "placed=0" in msg
        finally:
            conn.close()
            os.unlink(path)

    def test_summary_log_counts_placed_and_skipped(self, caplog):
        """Three markets: one qualifies (ATP), one is tennis but below volume,
        one is not tennis. Summary must show scanned=3 tennis=2 placed=1
        skipped_vol=1."""
        import logging
        conn, path = _init_test_db()
        try:
            from sniper.services.stink_bid_runner import StinkBidRunner
            runner = StinkBidRunner(db_path=path, strategy_id=2)

            _insert_market_metadata(
                conn, "cond_a",
                "ATP: Djokovic vs Alcaraz - Match Winner",
                volume_24h=500000.0,
            )
            _insert_market_metadata(
                conn, "cond_b",
                "WTA: Swiatek vs Sabalenka - Match Winner",
                volume_24h=500.0,  # below 10k default
            )
            _insert_market_metadata(
                conn, "cond_c",
                "Will BTC reach $100K by year end?",
                volume_24h=500000.0,
            )

            gamma_response = {
                "outcomes": json.dumps(["Djokovic", "Alcaraz"]),
                "clobTokenIds": json.dumps(["tok_a", "tok_b"]),
                "outcomePrices": json.dumps(["0.62", "0.38"]),
            }
            fake_entry = {
                "player1": "Djokovic", "player2": "Alcaraz",
                "competition": {"status": {"type": {"name": "STATUS_SCHEDULED"}}},
            }
            match_state = MatchState(
                sets_completed=0, score_by_set=[],
                match_status="scheduled", source="espn",
            )

            with caplog.at_level(logging.INFO, logger="sniper.services.stink_bid_runner"):
                with patch("sniper.services.stink_bid_runner.fetch_market_by_condition", return_value=gamma_response), \
                     patch("sniper.services.stink_bid_runner.fetch_scoreboard", return_value={"events": [fake_entry]}), \
                     patch("sniper.services.stink_bid_runner.parse_scoreboard", return_value=[fake_entry]), \
                     patch("sniper.services.stink_bid_runner.find_match", return_value=fake_entry), \
                     patch("sniper.services.stink_bid_runner.get_sets_completed", return_value=match_state):
                    runner.discover_and_place()

            summaries = [
                r.getMessage() for r in caplog.records
                if "discover_cycle" in r.getMessage()
            ]
            assert len(summaries) == 1, summaries
            msg = summaries[0]
            assert "scanned=3" in msg, msg
            assert "tennis=2" in msg, msg
            assert "placed=1" in msg, msg
            assert "skipped_vol=1" in msg, msg
        finally:
            conn.close()
            os.unlink(path)


# ---------------------------------------------------------------------------
# _fetch_gamma_tennis_h2h — Gamma tag + discriminator filtering
# ---------------------------------------------------------------------------


class TestGammaTennisH2HFetch:
    """Tests for the module-level _fetch_gamma_tennis_h2h helper (no autouse)."""

    @staticmethod
    def _mock_session(events_payload):
        session = MagicMock()
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value=events_payload)
        session.get = MagicMock(return_value=resp)
        return session

    def test_filters_by_tennis_tag(self):
        """Tennis-tagged event kept, nba-tagged event dropped."""
        from sniper.services.stink_bid_runner import _fetch_gamma_tennis_h2h
        payload = [
            {
                "title": "Madrid Open: Tatjana Maria vs Laura Samson",
                "slug": "wta-maria-samson",
                "endDate": "2026-04-28T09:00:00Z",
                "volume24hr": 157844.11,
                "tags": [{"slug": "tennis"}, {"slug": "sports"}],
                "markets": [{
                    "conditionId": "0xTENNIS",
                    "slug": "wta-maria-samson",
                    "question": "Madrid Open: Tatjana Maria vs Laura Samson",
                    "groupItemTitle": "",
                }],
            },
            {
                "title": "Lakers vs Celtics - Winner",
                "slug": "nba-lal-bos",
                "endDate": "2026-04-22T00:00:00Z",
                "volume24hr": 50000.0,
                "tags": [{"slug": "nba"}, {"slug": "sports"}],
                "markets": [{
                    "conditionId": "0xNBA",
                    "slug": "nba-lal-bos",
                    "question": "Lakers vs Celtics - Winner",
                    "groupItemTitle": "",
                }],
            },
        ]
        pairs = _fetch_gamma_tennis_h2h(self._mock_session(payload))
        assert len(pairs) == 1
        assert pairs[0]["market"]["conditionId"] == "0xTENNIS"

    def test_outright_event_no_vs_skipped(self):
        """Tennis-tagged but title lacks ' vs ' (outright) -> dropped."""
        from sniper.services.stink_bid_runner import _fetch_gamma_tennis_h2h
        payload = [{
            "title": "2026 Men's French Open Winner",
            "slug": "2026-mens-french-open-winner",
            "endDate": "2026-06-07T00:00:00Z",
            "volume24hr": 1842319.84,
            "tags": [{"slug": "tennis"}],
            "markets": [{
                "conditionId": "0xOUTRIGHT",
                "slug": "2026-mens-french-open-winner",
                "question": "2026 Men's French Open Winner",
                "groupItemTitle": "",
            }],
        }]
        pairs = _fetch_gamma_tennis_h2h(self._mock_session(payload))
        assert pairs == []

    def test_seeds_h2h_only_not_props(self):
        """Event with 1 H2H + 8 props -> only the H2H market kept."""
        from sniper.services.stink_bid_runner import _fetch_gamma_tennis_h2h
        event_slug = "atp-holmgre-bu-2026-04-19"
        event_title = "Gwangju: August Holmgren vs Yunchaokete Bu"
        payload = [{
            "title": event_title,
            "slug": event_slug,
            "endDate": "2026-04-27T01:00:00Z",
            "volume24hr": 162280.12,
            "tags": [{"slug": "tennis"}],
            "markets": [
                # The H2H moneyline — passes discriminator
                {"conditionId": "0xH2H",
                 "slug": event_slug,
                 "question": event_title,
                 "groupItemTitle": ""},
                # Props — each fails at least one discriminator
                {"conditionId": "0xPROP1",
                 "slug": event_slug + "-first-set-total-8pt5",
                 "question": "First set total over 8.5",
                 "groupItemTitle": "Over 8.5"},
                {"conditionId": "0xPROP2",
                 "slug": event_slug + "-set-handicap-home-1pt5",
                 "question": "Set handicap Holmgren -1.5",
                 "groupItemTitle": "Holmgren -1.5"},
                {"conditionId": "0xPROP3",
                 "slug": event_slug + "-total-games-over-22pt5",
                 "question": "Total games over 22.5",
                 "groupItemTitle": ""},
            ],
        }]
        pairs = _fetch_gamma_tennis_h2h(self._mock_session(payload))
        assert len(pairs) == 1
        assert pairs[0]["market"]["conditionId"] == "0xH2H"

    def test_gamma_fetch_failure_returns_empty(self):
        """Session raises -> returns [] (runner must survive Gamma outage)."""
        from sniper.services.stink_bid_runner import _fetch_gamma_tennis_h2h
        session = MagicMock()
        session.get = MagicMock(side_effect=RuntimeError("connection reset"))
        assert _fetch_gamma_tennis_h2h(session) == []


# ---------------------------------------------------------------------------
# _seed_tennis_markets_from_gamma — upsert semantics
# ---------------------------------------------------------------------------


class TestGammaSeedUpsert:
    def test_upserts_all_eight_columns_via_helper(self):
        """Seed must populate title/slug/event_slug/end_date_iso/category/
        category_source/volume_24h/is_open via db.upsert_market_metadata."""
        conn, path = _init_test_db()
        try:
            from sniper.services.stink_bid_runner import StinkBidRunner
            runner = StinkBidRunner(db_path=path, strategy_id=99)

            pairs = [{
                "event": {
                    "title": "Madrid Open: Tatjana Maria vs Laura Samson",
                    "slug": "wta-maria-samson-2026-04-21",
                    "endDate": "2026-04-28T09:00:00Z",
                    "volume24hr": 157844.11,
                },
                "market": {
                    "conditionId": "0xSEED1",
                    "slug": "wta-maria-samson-2026-04-21",
                    "question": "Madrid Open: Tatjana Maria vs Laura Samson",
                    "groupItemTitle": "",
                },
            }]
            written, seeded = runner._seed_tennis_markets_from_gamma(conn, pairs)
            assert written == 1
            assert seeded == {"0xSEED1"}

            row = conn.execute(
                "SELECT condition_id, title, slug, event_slug, end_date_iso, "
                "category, category_source, volume_24h, is_open "
                "FROM market_metadata WHERE condition_id = ?",
                ("0xSEED1",),
            ).fetchone()
            assert row is not None
            assert row["title"] == "Madrid Open: Tatjana Maria vs Laura Samson"
            assert row["slug"] == "wta-maria-samson-2026-04-21"
            assert row["event_slug"] == "wta-maria-samson-2026-04-21"
            assert row["end_date_iso"] == "2026-04-28T09:00:00Z"
            assert row["category"] == "tennis"
            assert row["category_source"] == "gamma"
            assert abs(row["volume_24h"] - 157844.11) < 1e-3
            assert row["is_open"] == 1
        finally:
            conn.close()
            os.unlink(path)
            if path in _ACTIVE_TEST_DB_PATHS:
                _ACTIVE_TEST_DB_PATHS.remove(path)

    def test_calls_upsert_helper_not_raw_sql(self):
        """Seed must go through db.upsert_market_metadata (ADR principle 5)."""
        conn, path = _init_test_db()
        try:
            from sniper.services.stink_bid_runner import StinkBidRunner
            runner = StinkBidRunner(db_path=path, strategy_id=99)
            pairs = [_gamma_pair("0xSEED2", "Rome: A vs B", volume_24h=9000.0)]
            with patch("sniper.services.stink_bid_runner.upsert_market_metadata") as mock_upsert:
                runner._seed_tennis_markets_from_gamma(conn, pairs)
                mock_upsert.assert_called_once()
                _args, kwargs = mock_upsert.call_args
                assert kwargs["category"] == "tennis"
                assert kwargs["category_source"] == "gamma"
                assert kwargs["is_open"] == 1
                assert kwargs["title"] == "Rome: A vs B"
        finally:
            conn.close()
            os.unlink(path)
            if path in _ACTIVE_TEST_DB_PATHS:
                _ACTIVE_TEST_DB_PATHS.remove(path)


# ---------------------------------------------------------------------------
# Scan is scoped to freshly-seeded condition IDs
# ---------------------------------------------------------------------------


class TestScanScopedToSeededCids:
    def test_unseeded_vs_market_is_ignored(self, monkeypatch):
        """A market_metadata row with 'X vs Y' title but NOT in the Gamma seed
        must not be evaluated (prevents esports/college false positives)."""
        conn, path = _init_test_db()
        try:
            from sniper.services.stink_bid_runner import StinkBidRunner
            runner = StinkBidRunner(db_path=path, strategy_id=99)

            _insert_market_metadata(
                conn, "cond_tennis_real",
                "Madrid Open: Maria vs Samson",
                volume_24h=50000.0,
            )
            _insert_market_metadata(
                conn, "cond_csgo_vs",
                "Counter-Strike: Atputies vs 3DMAX",
                volume_24h=50000.0,
            )

            # Gamma seeds ONLY the tennis cond (esports has no tennis tag).
            monkeypatch.setattr(
                "sniper.services.stink_bid_runner._fetch_gamma_tennis_h2h",
                lambda _s: [_gamma_pair("cond_tennis_real",
                                        "Madrid Open: Maria vs Samson",
                                        50000.0)],
            )

            with patch(
                "sniper.services.stink_bid_runner.fetch_market_by_condition",
                return_value=None,
            ) as mock_fetch:
                runner.discover_and_place()
                # Only the tennis cond is eligible for per-condition lookup.
                calls = [c.args[1] for c in mock_fetch.call_args_list]
                assert "cond_csgo_vs" not in calls
                assert calls == ["cond_tennis_real"]
        finally:
            conn.close()
            os.unlink(path)
            if path in _ACTIVE_TEST_DB_PATHS:
                _ACTIVE_TEST_DB_PATHS.remove(path)


# ---------------------------------------------------------------------------
# Relaxed _is_tennis_match_title
# ---------------------------------------------------------------------------


class TestRelaxedTitleFilter:
    def test_accepts_madrid_open_h2h(self):
        from sniper.strategies.stink_bid_v1 import _is_tennis_match_title
        assert _is_tennis_match_title("Madrid Open: Tatjana Maria vs Laura Samson")

    def test_accepts_bare_vs(self):
        from sniper.strategies.stink_bid_v1 import _is_tennis_match_title
        assert _is_tennis_match_title("Nadal vs Djokovic")

    def test_rejects_no_vs(self):
        from sniper.strategies.stink_bid_v1 import _is_tennis_match_title
        assert not _is_tennis_match_title("2026 Men's French Open Winner")
