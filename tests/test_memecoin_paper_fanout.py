"""The paper copy fan-out leaves the funded threads (2026-09-27, Alpha GO).

Both watchdog kills on 2026-09-27 (12:38:01Z stale, 12:44:01Z sweep_stale) caught the funded main
loop inside the PAPER copy trader's executable quote (an EVM V4 pool scan / erc20_decimals through a
direct urlopen). The fan-out also ran inline on the Fomo-activity dispatch threads. A paper quote that
hangs must now cost the funded thread nothing.
"""
import os
import sqlite3
import threading
import time

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_paper_fanout as F
from sniper.services import memecoin_runner as run
from sniper.services.memecoin_follow_activity import fast_lane_wallets, follow_activity_pass

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
NOW = "2026-07-06T12:00:00Z"


def _db(tmp_path):
    path = str(tmp_path / "ledger.db")
    c = sqlite3.connect(path, check_same_thread=False)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    c.commit()

    def connect():
        w = sqlite3.connect(path, timeout=30, check_same_thread=False)
        w.row_factory = sqlite3.Row
        return w
    return c, connect


def _wait(pred, seconds=5.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def fanout(tmp_path, monkeypatch):
    c, connect = _db(tmp_path)
    fan = F.PaperFanout(connect=connect, now_iso_fn=lambda: NOW)
    monkeypatch.setattr(F, "_ACTIVE", fan)
    yield c, fan, connect
    c.close()


@pytest.fixture
def blocking_paper(monkeypatch):
    """A paper fan-out whose quote hangs, the way the V4 scan hung at 12:38Z."""
    release, calls = threading.Event(), []

    def stub(conn, ev, size_usd, now_iso, *, arms, **kw):
        calls.append(ev.fomo_event_id)
        release.wait(30)
        return {a: "opened" for a in arms}
    monkeypatch.setattr("sniper.services.memecoin_copy_trader.copy_event_to_arms", stub)
    yield release, calls
    release.set()


def _action(c, event_id):
    row = c.execute("SELECT action FROM memecoin_events WHERE fomo_event_id=?",
                    (event_id,)).fetchone()
    return row["action"] if row else None


# --- (i) the main loop -------------------------------------------------------------------------

def test_a_hanging_paper_quote_leaves_run_once_under_a_second(fanout, blocking_paper, monkeypatch):
    c, fan, _connect = fanout
    release, calls = blocking_paper
    m.add_wallet(c, "u1", "alice")
    feed = {"responseObject": {"feed": [
        {"id": "b1", "type": "large_buy", "userId": "u1", "tokenAddress": "MINT",
         "createdAt": "2026-07-07T10:00:00Z",
         "body": {"userHandle": "alice", "ticker": "PEPE", "price": 0.005,
                  "marketCap": "1000000", "currentSizeUsd": 3000.0, "isFirstBuy": True}}]}}

    class _Auth:
        access = "t"; refresh = "r"; ca_id = "c"

        def refresh_token(self, http=None):
            return "t"

    from sniper.settings.memecoin import DEFAULT_MEMECOIN_ARMS
    monkeypatch.setattr("sniper.settings.memecoin.MEMECOIN_ARMS", DEFAULT_MEMECOIN_ARMS)
    started = time.monotonic()
    run.run_once(c, _Auth(), 100.0, http=lambda *a, **k: (200, feed, "Sun, 07 Jul 2026 10:00:01 GMT"),
                 fill_fn=lambda *a, **k: (0.005, "jupiter"))
    assert time.monotonic() - started < 1.0, "the funded loop waited on a paper quote"
    assert _wait(lambda: calls), "the paper job never reached the worker"
    release.set()
    assert _wait(lambda: fan.processed == 1)
    ids = [r["fomo_event_id"] for r in c.execute("SELECT fomo_event_id FROM memecoin_events")]
    assert [_action(c, i) for i in ids] == ["opened"]


# --- (ii) the Fomo-activity dispatch path ------------------------------------------------------

def test_a_hanging_paper_quote_leaves_the_follow_pass_under_a_second(fanout, blocking_paper):
    c, fan, _connect = fanout
    release, calls = blocking_paper
    m.add_wallet(c, "u_armed", "alice", source="follow")
    c.execute("UPDATE memecoin_wallets SET real_approved=1, real_chains='solana' "
              "WHERE source_user_id='u_armed'")
    c.commit()
    swap = {"id": "s1", "inTokenAddress": USDC, "outTokenAddress": "MINT",
            "humanUsdAmountIn": "3000", "humanUsdAmountOut": "3000",
            "inHumanAmount": "3000", "outHumanAmount": "600000",
            "createdAt": "2026-07-06T11:55:00Z", "isOffPlatform": "False",
            "networkId": 1399811149}
    started = time.monotonic()
    follow_activity_pass(c, None, 100.0, now_fn=lambda: NOW, real_now_fn=lambda: NOW,
                         fill_fn=lambda *a, **k: (0.005, "jupiter"),
                         swaps_fn=lambda uid: [swap], sleep_fn=lambda s: None,
                         wallets=fast_lane_wallets(c))
    assert time.monotonic() - started < 1.0, "the dispatch thread waited on a paper quote"
    assert _wait(lambda: calls)
    release.set()
    assert _wait(lambda: fan.processed == 1)
    assert _action(c, calls[0]) == "opened"


# --- (iii)-(v) the queue and the worker --------------------------------------------------------

class _Ev:
    def __init__(self, event_id, side="buy"):
        self.side = side; self.source_user_id = "u1"; self.source_handle = "alice"
        self.mint = "MINT"; self.ticker = "PEPE"; self.their_price = 0.005
        self.their_market_cap = 1e6; self.their_size_usd = 3000.0; self.is_first_buy = True
        self.fomo_event_id = event_id; self.created_at = NOW; self.received_at = NOW
        self.lag_s = 0.1; self.signal_source = "global_feed"; self.network_id = "solana"


def _submit(c, ev):
    m.insert_event(c, ev.__dict__)
    return F.submit_or_none(c, ev, 100.0, arms=["control"], fill_fn=None, stats_fn=None,
                            delete_on_failure=False, action_rule="feed")


def test_a_full_queue_drops_labels_and_counts_without_blocking(tmp_path, monkeypatch,
                                                                blocking_paper):
    release, calls = blocking_paper
    c, connect = _db(tmp_path)
    fan = F.PaperFanout(connect=connect, now_iso_fn=lambda: NOW, maxsize=1)
    monkeypatch.setattr(F, "_ACTIVE", fan)
    assert _submit(c, _Ev("e1")) is True
    assert _wait(lambda: calls == ["e1"])            # the worker is now stuck inside job 1
    started = time.monotonic()
    assert _submit(c, _Ev("e2")) is True             # fills the one slot
    assert _submit(c, _Ev("e3")) is False            # dropped
    assert time.monotonic() - started < 0.5
    assert _action(c, "e3") == F.QUEUE_FULL and fan.dropped_full == 1
    release.set()
    assert _wait(lambda: fan.processed == 2)
    c.close()


def test_a_job_that_waited_too_long_is_censored_not_quoted(tmp_path, blocking_paper):
    _release, calls = blocking_paper
    c, connect = _db(tmp_path)
    fan = F.PaperFanout(connect=connect, now_iso_fn=lambda: NOW)
    ev = _Ev("late1")
    m.insert_event(c, ev.__dict__)
    job = F.PaperJob(ev=ev, size_usd=100.0, arms=("control",), fill_fn=None, stats_fn=None,
                     funded_quote=None, funded_clip=None, delete_on_failure=False,
                     action_rule="feed", enqueued_mono=fan.clock() - (F.MAX_LAG_S + 1.0))
    w = connect()
    fan.run_job(w, job)
    assert calls == [] and fan.censored_late == 1
    assert _action(c, "late1") == F.LATE
    w.close(); c.close()


def test_the_worker_holds_no_write_across_a_buy_or_a_sell_quote(tmp_path, monkeypatch):
    c, connect = _db(tmp_path)
    fan = F.PaperFanout(connect=connect, now_iso_fn=lambda: NOW)
    w = connect()
    seen = []

    def buy_fill(*a, **k):
        seen.append(("buy", w.in_transaction))
        return (0.005, "jupiter")

    def exit_fill(mint, network_id, qty):
        seen.append(("sell", w.in_transaction))
        return (0.006, "route_quote")
    monkeypatch.setattr("sniper.services.memecoin_copy_trader.executable_exit_fill", exit_fill)
    m.add_wallet(c, "u1", "alice")
    c.commit()
    for ev in (_Ev("buy1"), _Ev("sell1", side="sell")):
        m.insert_event(c, ev.__dict__)
        w.execute("UPDATE memecoin_events SET action=action WHERE fomo_event_id='none'")
        assert w.in_transaction                        # a leftover write from an earlier job
        fan.run_job(w, F.PaperJob(
            ev=ev, size_usd=100.0, arms=("control",), fill_fn=buy_fill, stats_fn=None,
            funded_quote=None, funded_clip=None, delete_on_failure=False, action_rule="feed",
            enqueued_mono=fan.clock()))
        assert not w.in_transaction                    # committed per job
    assert seen == [("buy", False), ("sell", False)], seen
    assert _action(c, "buy1") == "opened" and _action(c, "sell1") == "exit"
    w.close(); c.close()


# --- (vi) rollback and wiring ------------------------------------------------------------------

def test_the_flag_off_installs_nothing_and_both_call_sites_run_inline():
    assert F.enabled({"MEMECOIN_PAPER_FANOUT_THREAD": "0"}) is False
    assert F.enabled({}) is True
    assert F.submit_or_none(None, _Ev("x"), 1.0, arms=[], fill_fn=None, stats_fn=None,
                            delete_on_failure=False, action_rule="feed") is None


def test_the_runner_installs_the_fan_out_before_any_submitting_thread_and_reports_it():
    src = open(os.path.join(os.path.dirname(__file__), "..", "scripts",
                            "memecoin_runner_loop.py")).read()
    install = src.index("_paper_fanout_mod.install(")
    assert "if _paper_fanout_mod.enabled():" in src[install - 600:install]
    assert install < src.index("target=_fast_activity_worker")
    assert install < src.index("target=_fomo_activity_dispatch_worker")
    assert install < src.index("res = run_once(conn, auth")
    assert "paper_fanout=(" in src


def test_a_worker_ledger_connect_failure_is_retried_not_fatal():
    attempts, sleeps = [], []

    def connect():
        attempts.append(1)
        if len(attempts) < 3:
            raise sqlite3.OperationalError("unable to open database file")
        return "conn"
    fan = F.PaperFanout(connect=connect, now_iso_fn=lambda: NOW)
    assert fan.connect_with_retry(sleep=sleeps.append) == "conn"
    assert len(attempts) == 3 and sleeps == [5.0, 5.0] and fan.failed == 2
