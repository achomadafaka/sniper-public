"""A blind miss and an unindexed pool wait on different things, so they get different budgets.

The one retry window is calibrated for INDEX LAG — the code's own comments cite a 4663 pool's
Initialize surfacing 38-61s after the block. A provider outage is a different phenomenon with a
different duration, and it shares the budget.

HODL (0x110cfd84, 2026-09-08T19:06:02Z, DegenCapitalLLC, ~$1.0k on a $43.2k coin) retried six times
over 53 seconds while v4 discovery and the v2 read alternated failures, then the window expired.
Running the same discovery afterwards found its pool in 670ms. The entry was lost to a budget
calibrated for something else.

Default 0 = OFF, one budget for both, exactly as before.
"""
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


NOW = "2026-09-08T19:08:00+00:00"
OLD = "2026-09-08T19:06:00+00:00"          # 120s before NOW


def _queue(c, eid, *, blind, queued_at=OLD):
    m.enqueue_entry_retry(c, eid, "0xmint", "4663", "{}", queued_at,
                          retry_after_s=6, blind=blind)
    c.execute("UPDATE memecoin_entry_retry_queue SET first_queued_at=? WHERE fomo_event_id=?",
              (queued_at, eid))
    c.commit()


def _ids(rows):
    return sorted(r["fomo_event_id"] for r in rows)


# --- the column ---------------------------------------------------------------------------

def test_a_blind_miss_is_recorded(conn):
    _queue(conn, "e1", blind=True)
    assert conn.execute("SELECT blind_misses FROM memecoin_entry_retry_queue").fetchone()[0] == 1


def test_a_non_blind_miss_is_not(conn):
    _queue(conn, "e1", blind=False)
    assert conn.execute("SELECT blind_misses FROM memecoin_entry_retry_queue").fetchone()[0] == 0


def test_a_row_that_ever_missed_blindly_keeps_the_longer_budget(conn):
    """One later attempt reaching a real verdict does not end the outage it is waiting out."""
    _queue(conn, "e1", blind=True)
    _queue(conn, "e1", blind=False)
    assert conn.execute("SELECT blind_misses FROM memecoin_entry_retry_queue").fetchone()[0] == 1


# --- the reap -------------------------------------------------------------------------------

def test_default_is_one_budget_for_both(conn):
    """`blind_window_s=None` must behave exactly as before this change."""
    _queue(conn, "blind", blind=True)
    _queue(conn, "plain", blind=False)
    reaped = m.reap_entry_retries(conn, NOW, window_s=60, max_attempts=12)
    assert _ids(reaped) == ["blind", "plain"], "both are past a 60s window"


def test_the_blind_row_survives_its_longer_budget(conn):
    _queue(conn, "blind", blind=True)
    _queue(conn, "plain", blind=False)
    reaped = m.reap_entry_retries(conn, NOW, window_s=60, max_attempts=12, blind_window_s=180)
    assert _ids(reaped) == ["plain"], "the unindexed-pool row is reaped, the blind one waits"
    assert conn.execute("SELECT COUNT(*) FROM memecoin_entry_retry_queue").fetchone()[0] == 1


def test_the_blind_row_is_still_reaped_past_its_own_budget(conn):
    _queue(conn, "blind", blind=True)
    reaped = m.reap_entry_retries(conn, NOW, window_s=60, max_attempts=12, blind_window_s=90)
    assert _ids(reaped) == ["blind"], "120s elapsed against a 90s blind budget"


def test_a_shorter_blind_window_never_shortens_the_normal_one(conn):
    """Misconfiguration must not make the blind budget the smaller of the two."""
    _queue(conn, "blind", blind=True)
    reaped = m.reap_entry_retries(conn, NOW, window_s=180, max_attempts=12, blind_window_s=10)
    assert reaped == [], "max(window, blind_window) — a blind row is never reaped sooner"


def test_the_attempt_cap_is_shared(conn):
    """A longer clock must not also buy more attempts."""
    _queue(conn, "blind", blind=True, queued_at=NOW)
    conn.execute("UPDATE memecoin_entry_retry_queue SET attempts=12")
    conn.commit()
    assert _ids(m.reap_entry_retries(conn, NOW, window_s=60, max_attempts=12,
                                     blind_window_s=600)) == ["blind"]


# --- the wiring -----------------------------------------------------------------------------

def test_the_runner_classifies_a_provider_miss_as_blind():
    import inspect
    from sniper.services import memecoin_real_runner as R
    src = inspect.getsource(R._maybe_enqueue_retry)
    assert 'quote_miss_code' in src and 'startswith("provider_")' in src


def test_the_lane_passes_the_blind_window():
    import inspect
    from sniper.services import memecoin_real_runner as R
    assert "MEMECOIN_ENTRY_RETRY_BLIND_WINDOW_S" in inspect.getsource(R.run_entry_retry_lane)


def test_the_setting_defaults_to_off():
    import os
    from sniper.settings import memecoin as cfg
    if not os.getenv("MEMECOIN_ENTRY_RETRY_BLIND_WINDOW_S"):
        assert cfg.MEMECOIN_ENTRY_RETRY_BLIND_WINDOW_S == 0.0


def test_a_broken_enqueue_cannot_pass_silently(conn):
    """`enqueue_entry_retry` catches its own exceptions and returns False, so a malformed INSERT
    disables EVERY retry with no raise and one log line nobody reads.

    Found the hard way on 2026-09-08: adding the `blind_misses` placeholder without its bound value
    made this return False for every call. The gate is the return value AND the row.
    """
    assert m.enqueue_entry_retry(conn, "e-live", "0xmint", "4663", "{}", NOW,
                                 retry_after_s=6, blind=True) is True
    row = conn.execute("SELECT fomo_event_id, blind_misses FROM memecoin_entry_retry_queue "
                       "WHERE fomo_event_id='e-live'").fetchone()
    assert row is not None, "the enqueue reported success and wrote nothing"
    assert row["blind_misses"] == 1
