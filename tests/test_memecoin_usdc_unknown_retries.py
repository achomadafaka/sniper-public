"""An unreadable USDC balance is a transient miss: the entry re-asks instead of dying.

2026-09-27 12:33Z (host Wi-Fi outage): probablyinsider's buy of 6uPEdkU2 reached J7 and the attempt
was refused 'USDC balance unavailable'. The pair was not in _RETRY_TRANSIENT, so it was never
queued, and the confirmed watcher's later detection of the same transaction was dropped as
duplicate_of_feed because the feed attempt counts as the owner. The signal died on every lane.
The risk gate itself says this refusal is retryable (memecoin_risk: "refuse honestly and
retryably"). The same refusal recurred at 12:52Z with the network up.

The reasons below come from the PRODUCER, memecoin_entry_allowed, not from a string typed here, so
a change in the gate's wording fails this file instead of silently un-retrying the class.
"""
import sqlite3
from dataclasses import replace

import pytest

from sniper.feeds.fomo_feed import CopyEvent
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as R
from sniper.services.memecoin_risk import build_memecoin_risk_context, memecoin_entry_allowed


def _refusal(usdc_balance):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m._ensure_memecoin_tables_in_transaction(c)
    ctx = replace(build_memecoin_risk_context(c, usdc_balance=usdc_balance, sol_balance=1.0,
                                              day="2026-09-27"), real_enabled=True)
    d = memecoin_entry_allowed(ctx, mint="M", trade_size=ctx.trade_size_usd)
    assert d.allowed is False
    return {"status": "rejected", "reason": d.reason}   # the hook's shape for a risk refusal


def test_an_unreadable_usdc_balance_is_a_transient_miss():
    assert R._is_transient_entry_miss(_refusal(None))


def test_a_genuine_empty_bankroll_is_not_retried():
    res = _refusal(0.0)
    assert "unavailable" not in res["reason"]
    assert not R._is_transient_entry_miss(res)


@pytest.fixture()
def _lane(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RETRY_WINDOW_S", 60.0)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RETRY_INTERVAL_S", 6.0)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RETRY_MAX_ATTEMPTS", 12)


def test_an_unreadable_usdc_balance_is_queued_and_stays_in_the_retry_lane(_lane):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    ev = CopyEvent(
        source_user_id="u1", source_handle="probablyinsider", mint="6uPEdkU2", ticker="",
        side="buy", their_price=1e-4, their_market_cap=0.0, their_size_usd=998.5, is_first_buy=True,
        fomo_event_id="swap:raybot:SIG:u1:6uPEdkU2", created_at="2026-09-27T12:32:53Z",
        received_at="2026-09-27T12:33:02Z", lag_s=9.6, signal_source="follow_activity",
        network_id="solana")
    refusal = _refusal(None)
    assert R._maybe_enqueue_retry(c, ev, refusal, "2026-09-27T12:33:02+00:00")
    calls = []

    def hook(conn, e):
        calls.append(e.fomo_event_id)
        return refusal                                  # the read is still failing

    R.run_entry_retry_lane(c, hook, "2026-09-27T12:33:09+00:00")
    assert calls == [ev.fomo_event_id]
    assert m.due_entry_retries(c, "2026-09-27T12:33:20+00:00", window_s=60, max_attempts=12)


# --- the read it retries is bounded -------------------------------------------------------------
# A retry re-runs the gate read INLINE on the main loop (run_entry_retry_lane, every poll). At
# 12:52Z one Solana gate spent ~14 s in the hook while the funded pool had no healthy endpoint, and
# a stalled main loop is what the watchdog kills. The Solana gate read has a wall-clock deadline;
# a read past it is unknown (None), never zero, and is never written to the gate cache.

def _slow(value, seconds, done):
    import time

    def read():
        time.sleep(seconds)
        done.append(value)
        return value
    return read


def test_a_hung_gate_read_returns_unknown_within_the_deadline():
    import time
    R._gate_balance_invalidate("solana-deadline-test")
    done = []
    t0 = time.monotonic()
    quote, native, cached = R._gate_balances(
        "solana-deadline-test", _slow(500.0, 10.0, done), lambda: 1.5, now_ts=100.0, deadline_s=0.3)
    assert time.monotonic() - t0 < 2.0
    assert quote is None and native == 1.5 and cached is False
    assert done == []                                  # the hung read was abandoned, not awaited


def test_a_read_that_finishes_after_the_deadline_never_feeds_the_cache():
    import time
    R._gate_balance_invalidate("solana-late-test")
    done = []
    R._gate_balances("solana-late-test", _slow(500.0, 0.5, done), lambda: 1.5, now_ts=100.0,
                     deadline_s=0.1)
    time.sleep(0.7)
    assert done == [500.0]                             # it did finish, late
    quote, _native, cached = R._gate_balances("solana-late-test", lambda: None, lambda: 1.5,
                                              now_ts=101.0, deadline_s=0.1)
    assert quote is None and cached is False           # and the late value was not cached


def test_without_a_deadline_the_gate_waits_as_before():
    R._gate_balance_invalidate("evm-no-deadline-test")
    done = []
    quote, _native, _cached = R._gate_balances("evm-no-deadline-test", _slow(500.0, 0.4, done),
                                               lambda: 0.05, now_ts=100.0)
    assert quote == 500.0 and done == [500.0]


def test_the_solana_gate_passes_the_deadline_and_the_evm_gate_does_not():
    import inspect
    src = inspect.getsource(R)
    # Both Solana gate reads carry it: the synchronous one and the early read that rides the
    # market-data GET (MEMECOIN_ENTRY_EARLY_GATE_READS).
    assert ('_gate_balances(\n                    "solana", sol["usdc_fn"], sol["sol_fn"], '
            'deadline_s=_GATE_BALANCE_DEADLINE_S,\n                    read_meta=_read_meta)') in src
    # The early-read object serves both legs since 2026-09-27 (A): it reads with its own deadline,
    # which is the Solana deadline for Solana and None (the in-line EVM behaviour) for EVM.
    assert ('_gate_balances(self.chain, sol["usdc_fn"], sol[self._native_key],\n'
            '                                            deadline_s=self._deadline_s, '
            'failures=self.failures,\n                                            read_meta=read_meta)') in src
    assert 'deadline_s=None if evm else _GATE_BALANCE_DEADLINE_S' in src
    assert ('_gate_balances(\n                chain, ctx["usdc_fn"], ctx["native_fn"], '
            'failures=gate_failures)') in src
