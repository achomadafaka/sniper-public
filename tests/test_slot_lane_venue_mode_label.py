"""A venue verifier's line names the mode the lane really ran the buy in (Alpha 2026-10-06).

AUTON 20:49:02Z was a LIVE-lane mcap refusal on DAMM v2, but the line read "slot lane damm_v2 shadow",
which made Alpha and then the operator believe DAMM v2 was shadow-only. The lane offers every venue
outcome to that venue's verifier; the line now says "live" when the lane would have sent the buy
(lane mode live and the venue not shadow-only) and "shadow" otherwise, as before.
"""
import inspect
from types import SimpleNamespace

import pytest

from sniper.execution import meteora_damm_v2_buy as D
from sniper.execution import raydium_cpmm_buy as C
from sniper.execution import raydium_launchlab_buy as LL
from sniper.services import memecoin_slot_lane as L
from test_slot_lane_cpmm import SOL_EV, _routed as cpmm_routed
from test_slot_lane_launchlab import SOL_B, _routed as launchlab_routed


def _damm_buy():
    return SimpleNamespace(handle="OmakaseOnly", mint="39ahtL8yAAAA", pool="FiYyzxapBBBB",
                           slot=454013078, signature="4PwMJ8uu", recv_utc=None, venue="meteora_damm_v2")


REFUSAL = {"status": "rejected", "reason": "mcap $915367 over ceiling $250000"}


@pytest.mark.parametrize("mode", ["live", "shadow"])
def test_every_venue_line_names_the_mode_it_was_given(mode):
    result = dict(REFUSAL, lane_mode=mode)
    assert ("slot lane damm_v2 %s OmakaseOnly" % mode) in D.shadow_line(_damm_buy(), result)
    cpmm = C.shadow_line(L.cpmm_slot_buy(cpmm_routed(SOL_EV)), result, None)
    assert ("slot lane cpmm %s " % mode) in cpmm
    launchlab = LL.shadow_line(L.launchlab_slot_buy(launchlab_routed(SOL_B)), result, None)
    assert ("slot lane launchlab %s " % mode) in launchlab


def test_a_line_with_no_mode_keeps_todays_shadow_word():
    assert "slot lane damm_v2 shadow " in D.shadow_line(_damm_buy(), dict(REFUSAL))


@pytest.mark.parametrize("lane_mode, shadow_only, status, want", [
    ("live", False, "rejected", "live"),     # AUTON: a live-lane refusal
    ("live", False, "filled", "live"),
    ("live", True, "rejected", "shadow"),    # the venue is shadow-only for this buy
    ("live", False, "shadow", "shadow"),     # a shadow build is shadow whatever the lane says
    ("shadow", False, "rejected", "shadow"),
    ("off", False, "rejected", "shadow"),
])
def test_the_outcome_mode_follows_the_lanes_own_send_predicate(monkeypatch, lane_mode, shadow_only,
                                                              status, want):
    monkeypatch.setattr(L, "shadow_only", lambda buy: shadow_only)
    assert L.outcome_mode(lane_mode, _damm_buy(), {"status": status}) == want


def test_the_lane_loop_stamps_the_mode_before_offering_to_a_verifier():
    src = inspect.getsource(L.start_slot_lane)
    stamp = src.index('result["lane_mode"] = outcome_mode(lane.mode, buy, result)')
    assert stamp < src.index("verifier.offer(buy, result)", stamp - 2000)
