"""Duplicate-open race (deep review P1): the copy engine check-then-inserts across the main
and follow-activity connections with no DB constraint, so two threads can both open a position
for the same (uid, mint, strategy). A partial UNIQUE index (WHERE is_open=1) makes at most one
OPEN position per key; the loser degrades to skipped_dup instead of crashing. Re-buys after a
close (same key, prior position closed) are still allowed."""
import sqlite3
import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_copy_trader as ct
from sniper.feeds.fomo_feed import CopyEvent


def _conn():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def _ev(uid="u1", mint="MINT"):
    return CopyEvent(source_user_id=uid, source_handle="h", mint=mint, ticker="T", side="buy",
                     their_price=0.005, their_market_cap=0.0, their_size_usd=3000.0,
                     is_first_buy=True, fomo_event_id="E1", created_at="t", received_at="t",
                     lag_s=0.3, signal_source="global_feed")


def test_second_open_for_same_key_is_rejected():
    c = _conn()
    m.open_position(c, source_user_id="u1", source_handle="h", mint="MINT", ticker="T",
                    entry_price=0.005, entry_notional=100.0, entry_fill_source="j",
                    entry_lag_s=0.1, their_size_usd=0.0, opened_at="t", strategy="control")
    with pytest.raises(sqlite3.IntegrityError):
        m.open_position(c, source_user_id="u1", source_handle="h", mint="MINT", ticker="T",
                        entry_price=0.005, entry_notional=100.0, entry_fill_source="j",
                        entry_lag_s=0.1, their_size_usd=0.0, opened_at="t", strategy="control")


def test_rebuy_after_close_is_allowed():
    c = _conn()
    pid = m.open_position(c, source_user_id="u1", source_handle="h", mint="MINT", ticker="T",
                          entry_price=0.005, entry_notional=100.0, entry_fill_source="j",
                          entry_lag_s=0.1, their_size_usd=0.0, opened_at="t", strategy="control")
    m.close_position(c, pid, "t2", 0.0, exit_reason="x")   # is_open -> 0
    # same key, prior position closed -> re-buy allowed (partial index only guards is_open=1)
    pid2 = m.open_position(c, source_user_id="u1", source_handle="h", mint="MINT", ticker="T",
                           entry_price=0.006, entry_notional=100.0, entry_fill_source="j",
                           entry_lag_s=0.1, their_size_usd=0.0, opened_at="t3", strategy="control")
    assert pid2 and pid2 != pid


def test_open_arm_degrades_to_skipped_dup_on_race(monkeypatch):
    # simulate the race: the pre-check sees no open position (as a concurrent thread would),
    # so _open_arm reaches the INSERT and the UNIQUE index rejects it -> must return skipped_dup.
    c = _conn()
    ev = _ev()
    assert ct._open_arm(c, ev, 100.0, 0.005, "dexscreener", None, "t", "control") == "opened"
    monkeypatch.setattr(m, "get_open_position", lambda *a, **k: None)   # pre-check blind to the row
    assert ct._open_arm(c, ev, 100.0, 0.005, "dexscreener", None, "t", "control") == "skipped_dup"
    assert len(m.open_positions_by_strategy(c, "control")) == 1         # still exactly one open
