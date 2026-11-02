"""A leg_over_clip / leg_under_clip build refusal names the approved clip and every warm leg
(Alpha 2026-10-07: probablyinsider 0dbfd8c0 landed S+4 after "slot lane build refused:
leg_over_clip" with the $147 clip and the legs it was compared against unrecorded). Log only."""
import logging
from types import SimpleNamespace

from sniper.services import memecoin_slot_lane as L


class Builder:
    def __init__(self, reason):
        self.reason, self.clock = reason, lambda: 100.0
        self._leg = {"usdc_atomic": 155_090_000, "fetched_at": 99.0}
        self._cash_legs = (self._leg, {"usdc_atomic": 50_000_000, "fetched_at": 98.5})

    def build(self, buy, clip_usd, snapshot, **kw):
        return None, self.reason


def test_a_leg_clip_refusal_names_the_clip_and_the_warm_legs(caplog):
    build = L.dispatch_build({"pump_curve": Builder("leg_over_clip")})
    with caplog.at_level(logging.WARNING, logger=L.logger.name):
        assert build(SimpleNamespace(venue="pump_curve", signature="SIG"), 147.0, None) == (None, "leg_over_clip")
    line = next(r.getMessage() for r in caplog.records if "leg clip" in r.getMessage())
    assert "leg_over_clip" in line and "clip=147000000" in line and "155090000@1.0s" in line
    assert "50000000@1.5s" in line and "sig=SIG" in line


def test_another_refusal_logs_nothing(caplog):
    build = L.dispatch_build({"pump_curve": Builder("leg_stale")})
    with caplog.at_level(logging.WARNING, logger=L.logger.name):
        build(SimpleNamespace(venue="pump_curve", signature="SIG"), 147.0, None)
    assert not [r for r in caplog.records if "leg clip" in r.getMessage()]
