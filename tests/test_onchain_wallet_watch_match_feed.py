"""`match_feed` must pick the event NEAREST the on-chain detection.

It selected the EARLIEST event inside the +/-180s window instead, which is systematically before
the detection, so `feed_lag_vs_onchain_s` was pulled negative and the fomo feed read as faster than
it is. That field is the evidence for "should we act on the chain signal rather than wait for the
feed", so a biased estimator answers a live strategy question wrongly.

Measured on the 503 stored detections before the fix: 61 of 308 matched rows chose a different
event, biased a median -48s.
"""
import importlib.util
import pathlib
import sqlite3
from datetime import datetime, timezone

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "research" / "onchain_wallet_watch.py"
_spec = importlib.util.spec_from_file_location("onchain_wallet_watch", _SRC)
W = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(W)


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE memecoin_events(id INTEGER PRIMARY KEY, fomo_event_id TEXT, "
              "source_user_id TEXT, mint TEXT, side TEXT, received_at TEXT, "
              "received_at_normalized TEXT)")
    return c


def _epoch(iso):
    """Compute it; never hard-code. A guessed constant is a hypothesis, and a wrong one here made
    every event fall outside the window so the test failed for the wrong reason."""
    return datetime.fromisoformat(iso).timestamp()


DETECTED = _epoch("2026-09-08T12:01:40+00:00")


def _ev(c, eid, ts, rowid):
    c.execute("INSERT INTO memecoin_events(id,fomo_event_id,source_user_id,mint,side,"
              "received_at_normalized) VALUES(?,?,'u','m','buy',?)", (rowid, eid, ts))


def test_it_picks_the_nearest_event_not_the_earliest(conn):
    """Two events in the window, one 100s BEFORE and one 5s AFTER. Nearest is the 5s one."""
    _ev(conn, "early", "2026-09-08T12:00:00+00:00", 1)   # -100s
    _ev(conn, "near", "2026-09-08T12:01:45+00:00", 2)    # +5s
    conn.commit()
    eid, lag = W.match_feed(conn, "u", "m", DETECTED)
    assert eid == "near", "the earliest event is not the nearest one"
    assert lag == pytest.approx(5.0, abs=1.0)


def test_the_bias_direction_is_the_one_that_was_wrong(conn):
    """Picking the earliest reports the feed as EARLIER than it was, i.e. the chain losing."""
    _ev(conn, "early", "2026-09-08T12:00:00+00:00", 1)
    _ev(conn, "near", "2026-09-08T12:01:45+00:00", 2)
    conn.commit()
    _, lag = W.match_feed(conn, "u", "m", DETECTED)
    assert lag > 0, "the nearest event is AFTER the detection, so the chain won this one"


def test_an_event_outside_the_window_is_not_matched(conn):
    _ev(conn, "far", "2026-09-08T11:00:00+00:00", 1)
    conn.commit()
    assert W.match_feed(conn, "u", "m", DETECTED) == (None, None)


def test_no_events_returns_no_match(conn):
    assert W.match_feed(conn, "u", "m", DETECTED) == (None, None)


def test_an_unparseable_timestamp_is_skipped_not_fatal(conn):
    _ev(conn, "bad", "not-a-time", 1)
    _ev(conn, "good", "2026-09-08T12:01:45+00:00", 2)
    conn.commit()
    eid, _ = W.match_feed(conn, "u", "m", DETECTED)
    assert eid == "good"
