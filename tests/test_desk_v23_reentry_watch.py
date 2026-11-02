"""v23 re-entry as a WATCH (operator 2026-10-23): a winner or soft-loss close re-emits the mint once, on the first
post-close trade, with trigger "watch"; JUDGE then sets a trigger or skips. Only a winner-reentry-v3 cohort accepts it."""
import json

import pytest

from sniper.persistence import db_memecoin_desk as desk
from sniper.services.memecoin_desk_universe import WinnerReentry
from test_memecoin_desk_store import at, candidate
from test_desk_winner_reentry import EXIT, ENTRY, RULE2, closed, soft_close, soft_conn

RULE3 = {"version": "winner-reentry-v3", "per_hour": 2, "window_s": 1800, "loss_soft": True}


def claim(conn, key, seconds, trigger, *, price=EXIT):
    cand = {**candidate(key, seconds=seconds), "reentry": {"prev_position_id": 42, "exit_price_usd": price,
                                                          "trigger": trigger}}
    return desk.record_candidate(conn, cand, cohort_id="cohort-U", now_iso=at(seconds), cooldown_s=900,
                                 reentry_enabled=True)


def test_v3_cohort_accepts_watch_after_a_win(tmp_path):
    c = soft_conn(tmp_path, rule=RULE3)
    closed(c)
    assert claim(c, "u-re", 120, "watch")["is_primary"] is True


def test_v3_cohort_accepts_watch_after_a_soft_loss(tmp_path):
    c = soft_conn(tmp_path, rule=RULE3)
    soft_close(c)
    assert claim(c, "u-re", 120, "watch", price=ENTRY)["is_primary"] is True


@pytest.mark.parametrize("reason", ["b_stop", "catastrophe", "farm_exit", None])
def test_v3_hard_loss_never_rearms(tmp_path, reason):
    c = soft_conn(tmp_path, rule=RULE3)
    soft_close(c, reason=reason)
    assert claim(c, "u-re", 120, "watch", price=ENTRY)["reason"] == "reentry_not_winner"


@pytest.mark.parametrize("trigger", ["recross", "above_exit"])
def test_v3_cohort_refuses_the_old_price_triggers(tmp_path, trigger):
    c = soft_conn(tmp_path, rule=RULE3)
    closed(c)
    assert claim(c, "u-re", 120, trigger)["reason"] == "reentry_trigger"


def test_v2_cohort_refuses_watch(tmp_path):
    c = soft_conn(tmp_path, rule=RULE2)
    closed(c)
    assert claim(c, "u-re", 120, "watch")["reason"] == "reentry_trigger"


def test_watch_spends_the_one_chance(tmp_path):
    c = soft_conn(tmp_path, rule=RULE3)
    closed(c)
    assert claim(c, "u-re", 120, "watch")["is_primary"] is True
    assert claim(c, "u-re2", 130, "watch")["is_primary"] is False


CLOSE = json.dumps({"realized_pnl": -2.47, "exit_price_usd": ENTRY * 0.92, "position_id": 42,
                    "closed_at": "2026-10-23T12:45:44+00:00", "exit_reason": "desk_risk_exit",
                    "entry_fill_price_usd": ENTRY})
T = 1792759544.0


def test_watch_arm_fires_on_the_first_post_close_trade_without_price_or_flow():
    arm = WinnerReentry(window_s=1800, watch=True)
    arm.load([("M", CLOSE)], T + 10)
    assert arm.check("M", None, T - 1, None) is None              # a trade from before the close never fires
    got = arm.check("M", None, T + 1, None)                         # unknown price and flow: JUDGE reads the sheet
    assert got == {"prev_position_id": 42, "exit_price_usd": ENTRY, "trigger": "watch"}
    assert arm.check("M", None, T + 2, None) is None              # one emission per close


def test_watch_arm_carries_watch_through_a_band_recross():
    arm = WinnerReentry(window_s=1800, watch=True)
    arm.load([("M", CLOSE)], T + 10)
    assert arm.take("M", T + 1)["trigger"] == "watch"


def test_legacy_arm_is_unchanged():
    arm = WinnerReentry(window_s=1800)
    arm.load([("M", CLOSE)], T + 10)
    assert arm.check("M", ENTRY * 1.01, T + 1, None) is None and arm.take("M", T + 1)["trigger"] == "recross"


def test_close_of_position_reads_the_desk_close_with_epoch(tmp_path):
    c = soft_conn(tmp_path, rule=RULE3)
    closed(c)
    got = desk.close_of_position(c, 42)
    assert got["realized_pnl"] == 2.56 and got["closed_at_s"] == __import__("datetime").datetime.fromisoformat(at(100)).timestamp()
    assert desk.close_of_position(c, 999) is None
