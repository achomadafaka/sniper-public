"""Re-entry backoff for a bag whose sell ladder already failed (2026-09-02).

Measured on 4663 since 08-30: 83 stuck attempts across only 5 positions. Position 46888 alone ran
65 failing ladders over 31 minutes -- 8.0s, 8.0s, 10.2s, 10.5s, 11.1s -- fired every 10-16 seconds
with no backoff, driven by THREE rails interleaved (real_tp, manual, b_timecap). Roughly nine
minutes of continuous futile work on one unsellable position, each run walking every slippage rung
and quoting at each. Invisible in a per-attempt mean because each attempt looks like a normal
8-second exit.

`mark_stuck_bag` already records the state; nothing consulted it before re-entering. The age comes
from `memecoin_exit_execution_attempts`, which already stores every stuck attempt with its time --
no schema change, and `stuck_bag` is a bare 0/1 flag with no timestamp anyway.

Six constraints, each with a test below. The last three are the ones that turn a waste bug into a
stranded-money bug if they are wrong.
"""
import sqlite3

import pytest

from sniper.services.memecoin_stuck_backoff import stuck_backoff


def _db(attempts=()):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE memecoin_exit_execution_attempts (position_id INT, network_id TEXT,"
              " exit_reason TEXT, attempted_at TEXT, status TEXT, sig TEXT, total_ms REAL)")
    for pid, at, status in attempts:
        c.execute("INSERT INTO memecoin_exit_execution_attempts VALUES (?,?,?,?,?,NULL,0)",
                  (pid, "4663", "real_tp", at, status))
    c.commit()
    return c


NOW = "2026-09-02T12:00:00+00:00"


def _at(sec_ago):
    import datetime
    t = datetime.datetime.fromisoformat(NOW) - datetime.timedelta(seconds=sec_ago)
    return t.isoformat()


def test_1_a_FIRST_attempt_is_never_delayed():
    """A position's first attempt to leave must be instant, always. Backoff applies only to
    RE-ENTRY after a ladder has already failed on that bag."""
    blocked, why = stuck_backoff(_db(), 1, "4663", exit_reason="real_tp", now_iso=NOW)
    assert blocked is False and why is None


def test_2_MANUAL_always_pierces():
    """The operator is currently the exit mechanism -- 4 of 7 stuck positions closed by hand,
    +$369.78 realized. A backoff that blocks the human rescuing a bag converts a wasted-RPC bug
    into a stranded-money bug. Manual is not a rail."""
    db = _db([(1, _at(1), "stuck")] * 5)
    assert stuck_backoff(db, 1, "4663", exit_reason="manual", now_iso=NOW)[0] is False
    # ...while an automated rail at the same instant IS suppressed
    assert stuck_backoff(db, 1, "4663", exit_reason="real_tp", now_iso=NOW)[0] is True


def test_3_an_unreadable_state_FAILS_OPEN():
    """A backoff that fails closed strands a position, which is strictly worse than the waste it
    prevents. Same direction _wallet_is_armed takes on unknown input."""
    bare = sqlite3.connect(":memory:")          # no attempts table at all
    assert stuck_backoff(bare, 1, "4663", exit_reason="real_tp", now_iso=NOW)[0] is False


def test_4_the_backoff_has_a_CEILING_so_recovering_liquidity_is_still_sellable():
    """An unsellable bag at 10:00 may be sellable at 10:30. The wait must decay, not latch."""
    db = _db([(1, _at(400), "stuck")] * 40)     # 40 consecutive failures, long ago
    blocked, _ = stuck_backoff(db, 1, "4663", exit_reason="real_tp", now_iso=NOW)
    assert blocked is False, "40 failures must not latch a position shut forever"


def test_5_it_is_keyed_PER_POSITION_not_per_mint():
    """Two positions in the same coin are separate bags with separate balances, and
    `_prepared_quote` already treats balance as identity."""
    db = _db([(1, _at(1), "stuck")] * 5)
    assert stuck_backoff(db, 1, "4663", exit_reason="real_tp", now_iso=NOW)[0] is True
    assert stuck_backoff(db, 2, "4663", exit_reason="real_tp", now_iso=NOW)[0] is False


def test_6_a_suppression_states_its_REASON_AND_AGE():
    """A skipped exit that leaves no trace is the failure mode we spent the night on: LI.FI's probe
    returned False silently for 26 hours."""
    db = _db([(1, _at(3), "stuck")] * 4)
    blocked, why = stuck_backoff(db, 1, "4663", exit_reason="real_tp", now_iso=NOW)
    assert blocked is True
    assert "stuck" in why and "3" in why and "4" in why, f"reason must carry age and count: {why}"


def test_a_SUCCESSFUL_exit_since_the_failures_clears_the_backoff():
    """The streak counts CONSECUTIVE failures. A fill in between means the bag moved."""
    db = _db([(1, _at(60), "stuck"), (1, _at(50), "stuck"), (1, _at(5), "closed")])
    assert stuck_backoff(db, 1, "4663", exit_reason="real_tp", now_iso=NOW)[0] is False


def test_the_wait_GROWS_with_consecutive_failures():
    """One failure should not buy the same silence as twenty."""
    one = _db([(1, _at(45), "stuck")])
    many = _db([(1, _at(45), "stuck")] * 6)
    assert stuck_backoff(one, 1, "4663", exit_reason="real_tp", now_iso=NOW)[0] is False
    assert stuck_backoff(many, 1, "4663", exit_reason="real_tp", now_iso=NOW)[0] is True


@pytest.mark.parametrize("reason", ["catastrophe", "vol_death"])
def test_safety_rails_are_NOT_special_cased(reason):
    """Only `manual` pierces. A catastrophe rail re-driving an unsellable bag every 10s is the
    same waste as any other rail, and it is not a human waiting on a rescue."""
    db = _db([(1, _at(1), "stuck")] * 5)
    assert stuck_backoff(db, 1, "4663", exit_reason=reason, now_iso=NOW)[0] is True


# --- the streak must survive bookkeeping rows (2026-09-02, caught in review) --------------------
# `already_closing` is 3.4x more common than `stuck` (279 vs 83 on 4663 since 08-30) and it sits ON
# TOP of the stuck rows in DESC order. Position 46888's real tape, newest first:
#     00:21:29 closed  / 00:21:26 stuck / 00:21:23 already_closing / 00:21:16 already_closing ...
# Breaking on any non-stuck status meant the scan hit `already_closing` first, broke at streak 0,
# and allowed every re-entry — the backoff would have been INERT against its own motivating case.
#
# Only a REAL outcome breaks the streak. `already_closing` is the tick's shadow while a ladder holds
# the claim (measured 0.2ms) and is evidence of nothing.

def _rows(db, pid, seq):
    for at, status in seq:
        db.execute("INSERT INTO memecoin_exit_execution_attempts VALUES (?,?,?,?,?,NULL,0)",
                   (pid, "4663", "real_tp", _at(at), status))
    db.commit()
    return db


def test_already_closing_does_NOT_break_the_streak():
    """The defect that would have made this a no-op in production."""
    db = _rows(_db(), 1, [(60, "stuck"), (50, "stuck"), (40, "stuck"), (30, "stuck"),
                          (3, "already_closing"), (2, "already_closing")])
    blocked, why = stuck_backoff(db, 1, "4663", exit_reason="real_tp", now_iso=NOW)
    assert blocked is True, "already_closing rows masked four real failures"
    assert "4" in why, f"all four stuck rows must count: {why}"


def test_already_closing_is_not_COUNTED_either():
    """Skipped, not counted. Counting the shadow would inflate the streak and over-suppress."""
    db = _rows(_db(), 1, [(45, "stuck")] + [(i, "already_closing") for i in range(1, 12)])
    blocked, why = stuck_backoff(db, 1, "4663", exit_reason="real_tp", now_iso=NOW)
    assert blocked is False, "one real failure at 45s must not be suppressed by 11 shadow rows"


def test_a_real_fill_still_breaks_the_streak():
    db = _rows(_db(), 1, [(60, "stuck"), (50, "stuck"), (5, "closed")])
    assert stuck_backoff(db, 1, "4663", exit_reason="real_tp", now_iso=NOW)[0] is False


def test_already_closed_breaks_the_streak():
    """The bag is gone; there is nothing left to back off from."""
    db = _rows(_db(), 1, [(60, "stuck"), (50, "stuck"), (5, "already_closed")])
    assert stuck_backoff(db, 1, "4663", exit_reason="real_tp", now_iso=NOW)[0] is False


@pytest.mark.parametrize("status", ["unknown", "gone", "some_new_status_nobody_added_yet"])
def test_an_UNCERTAIN_status_breaks_the_streak_fail_open(status):
    """`unknown` means the sell may have landed. An unrecognised status means we do not know what
    happened. Neither is evidence of a failed ladder, so neither may suppress an exit."""
    db = _rows(_db(), 1, [(60, "stuck"), (50, "stuck"), (5, status)])
    assert stuck_backoff(db, 1, "4663", exit_reason="real_tp", now_iso=NOW)[0] is False
