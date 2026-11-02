"""Desk trail arming (2026-10-26): MEMECOIN_DESK_TRAIL_ARM_PCT delays a desk position's override trail until
the executable peak reaches +X%; other positions and an unset knob keep today's immediate arming."""
from sniper.services import memecoin_real_runner as R


def _pos(source, peak):
    return {"signal_source": source, "entry_price": 1.0, "executable_peak_price": peak, "opened_at": "2026-10-26T15:48:07Z"}


def test_desk_trail_waits_for_the_arm_threshold(monkeypatch):
    monkeypatch.setenv("MEMECOIN_DESK_TRAIL_ARM_PCT", "15")
    assert R.effective_trail(_pos("desk", 1.10), {"trail_pct": 12}) == (12.0, False)
    assert R.effective_trail(_pos("desk", 1.16), {"trail_pct": 12}) == (12.0, True)


def test_non_desk_and_unset_knob_arm_immediately(monkeypatch):
    monkeypatch.setenv("MEMECOIN_DESK_TRAIL_ARM_PCT", "15")
    assert R.effective_trail(_pos("global_feed", 1.0), {"trail_pct": 12}) == (12.0, True)
    monkeypatch.delenv("MEMECOIN_DESK_TRAIL_ARM_PCT")
    assert R.effective_trail(_pos("desk", 1.0), {"trail_pct": 12}) == (12.0, True)
