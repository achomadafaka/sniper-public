"""Regression tests: watchlist wallets bypass is_bot scan filter."""

import sqlite3
import time

import analyze_users
import db


def _make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS wallet_analysis_queue (
            wallet TEXT PRIMARY KEY,
            priority_score INTEGER NOT NULL,
            first_event_ts INTEGER NOT NULL,
            last_event_ts INTEGER NOT NULL,
            state TEXT NOT NULL DEFAULT 'queued',
            locked_by TEXT NOT NULL DEFAULT '',
            locked_ts INTEGER NOT NULL DEFAULT 0,
            retry_count INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ignored_wallets (
            wallet TEXT PRIMARY KEY
        )
    """)
    conn.commit()
    return conn


_UA_DEFAULTS = dict(
    position_count=10, pnl=100.0, max_position_value=5000.0,
    top_open_position_title="test", top_open_position_value=5000.0,
    total_open_value=10000.0, trade_count=50, low_buy_hits_14d=5,
    avg_low_buy_notional_14d=500.0, market_diversity_14d=3,
    concentration_ratio=0.2, suspicion_score=20.0,
    insider_score=40.0, speculator_score=30.0,
    tier="C", strategy_group="balanced_mixed",
    strategy_confidence=0.5, strategy_note="test",
    wins=30, losses=10, closed_sample_size=40, win_rate=0.75,
    high_winrate_qualified=1, first_activity_ts=1700000000,
    account_age_days=100.0, is_fresh_account=0,
    position_signal_count=5, position_insider_candidate_count=1,
    position_speculator_count=1, max_position_insider_score=60.0,
    max_position_speculator_score=40.0, total_capital_deployed=50000.0,
    roi_percent=10.0, qualifying_pnl=5000.0, qualifying_capital=50000.0,
    closed_realized_pnl=4000.0, open_cash_pnl=1000.0,
    analysis_version=1, is_bot=0, bot_detection_reason="",
    bot_checked_at=1700000000, total_closed_count=100,
    resolution_rate=0.8, effective_resolved_wins=30,
    effective_resolved_losses=10, matches_criteria=1,
    updated_at="2026-03-01 00:00:00", wallet_edge_tier="strong",
)


def _insert_ua(conn, wallet, *, is_bot=0, analyzed_ts=None, classification=None):
    if analyzed_ts is None:
        analyzed_ts = int(time.time()) - 86400
    if classification is None:
        classification = "arb_fund" if is_bot else "high_winrate_review"
    cols = dict(_UA_DEFAULTS)
    cols["wallet"] = wallet
    cols["analyzed_ts"] = analyzed_ts
    cols["is_bot"] = is_bot
    cols["classification"] = classification
    col_names = ", ".join(cols.keys())
    placeholders = ", ".join(["?"] * len(cols))
    conn.execute(
        f"INSERT OR REPLACE INTO user_analysis ({col_names}) VALUES ({placeholders})",
        list(cols.values()),
    )
    conn.commit()


def _add_to_watchlist(conn, wallet):
    conn.execute(
        "INSERT OR IGNORE INTO watchlist_wallets (wallet, label, added_ts, last_synced_ts) VALUES (?, ?, ?, ?)",
        (wallet, "test", int(time.time()), int(time.time())),
    )
    conn.commit()


def test_watched_bot_wallet_included_in_scan():
    """A bot wallet on the watchlist SHOULD appear in the scan set."""
    conn = _make_db()
    wallet = "0xwatched_bot_wallet"
    _insert_ua(conn, wallet, is_bot=1)
    _add_to_watchlist(conn, wallet)

    result = db.wallets_for_open_position_scan(conn, limit=100, stale_after_seconds=600)
    assert wallet in result


def test_unwatched_bot_wallet_excluded_from_scan():
    """A bot wallet NOT on the watchlist should still be excluded."""
    conn = _make_db()
    wallet = "0xunwatched_bot_wallet"
    _insert_ua(conn, wallet, is_bot=1)

    result = db.wallets_for_open_position_scan(conn, limit=100, stale_after_seconds=600)
    assert wallet not in result


def test_non_bot_wallet_still_included():
    """Normal non-bot wallets continue to appear in scan set."""
    conn = _make_db()
    wallet = "0xnormal_wallet"
    _insert_ua(conn, wallet, is_bot=0)

    result = db.wallets_for_open_position_scan(conn, limit=100, stale_after_seconds=600)
    assert wallet in result


def test_recently_analyzed_bot_on_watchlist_excluded():
    """Even a watched bot wallet is excluded if recently analyzed (not stale)."""
    conn = _make_db()
    wallet = "0xfresh_bot_wallet"
    _insert_ua(conn, wallet, is_bot=1, analyzed_ts=int(time.time()))
    _add_to_watchlist(conn, wallet)

    result = db.wallets_for_open_position_scan(conn, limit=100, stale_after_seconds=600)
    assert wallet not in result


def test_run_once_analyzes_watchlisted_bot_wallet(monkeypatch):
    """run_once should not skip watchlisted wallets just because is_bot=1."""
    conn = _make_db()
    wallet = "0xrunonce_watchlisted_bot"
    _insert_ua(conn, wallet, is_bot=1)
    _add_to_watchlist(conn, wallet)

    called = []

    monkeypatch.setattr(analyze_users, "ANALYSIS_SOURCE", "legacy")
    monkeypatch.setattr(analyze_users, "wallets_for_open_position_scan", lambda *_args, **_kwargs: [wallet])
    monkeypatch.setattr(analyze_users, "auto_follow_scope_wallets", lambda _conn: [])

    def _fake_analyze_wallet(_conn, _http_session, wallet_arg, auto_follow_wallets=None):
        called.append((wallet_arg, tuple(auto_follow_wallets or ())))
        raise RuntimeError("stop after proving analyze_wallet was called")

    monkeypatch.setattr(analyze_users, "analyze_wallet", _fake_analyze_wallet)

    processed = analyze_users.run_once(conn, http_session=None)
    assert processed == 0
    assert called == [(wallet, ())]


def test_run_once_still_skips_unwatchlisted_bot_wallet(monkeypatch):
    """run_once should continue to skip bot wallets that are not watchlisted."""
    conn = _make_db()
    wallet = "0xrunonce_unwatchlisted_bot"
    _insert_ua(conn, wallet, is_bot=1)

    called = []

    monkeypatch.setattr(analyze_users, "ANALYSIS_SOURCE", "legacy")
    monkeypatch.setattr(analyze_users, "wallets_for_open_position_scan", lambda *_args, **_kwargs: [wallet])
    monkeypatch.setattr(analyze_users, "auto_follow_scope_wallets", lambda _conn: [])

    def _fake_analyze_wallet(*_args, **_kwargs):
        called.append("called")
        raise RuntimeError("should not run")

    monkeypatch.setattr(analyze_users, "analyze_wallet", _fake_analyze_wallet)

    processed = analyze_users.run_once(conn, http_session=None)
    assert processed == 0
    assert called == []
