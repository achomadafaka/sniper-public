"""The slot lane's prefetched legs call Jupiter on the FUNDED key and used to be counted nowhere
(Lambda's read 2026-09-27: ~2-4 calls/s, about 10-20x the metered funded quotes, invisible).
Each leg call now counts as a quote under role 'slot_leg' (the key and the funded priority are
unchanged), the role partition still sums to the total, and a counter failure never reaches the
leg refresh. Builds also report the leg's age into the slot_build substage."""
import json

import pytest

from sniper.execution import pump_curve_buy as pcb
from sniper.execution import solana_real_seams as seams


def _load():
    snap = seams.jupiter_load_snapshot()
    by_role = snap["by_role"]
    return snap["quotes"], {k: v.get("quotes", 0) for k, v in by_role.items()}


def test_each_leg_call_counts_under_slot_leg_and_the_partition_sums(monkeypatch):
    calls = []

    def fake_jup_call(method, path, body=None, **kw):
        calls.append((method, path.split("?")[0], seams.active_role()))
        return (200, {"otherAmountThreshold": "1", "outAmount": "1"}) if method == "GET" else (500, {})
    monkeypatch.setattr(seams, "_jup_call", fake_jup_call)
    total0, roles0 = _load()
    builder = pcb.SlotLaneCurveBuilder(user="11111111111111111111111111111111",
                                       rpc=lambda method, params: None)
    assert builder.refresh(100.0) is False                     # swap-instructions 500: no leg
    total1, roles1 = _load()
    # the key and the funded priority are unchanged: every call still runs as the FUNDED role
    assert calls == [("GET", "/quote", seams.ROLE_FUNDED),
                     ("POST", "/swap-instructions", seams.ROLE_FUNDED)]
    assert roles1.get(pcb.SLOT_LEG_ROLE, 0) - roles0.get(pcb.SLOT_LEG_ROLE, 0) == 2
    assert roles1.get(seams.ROLE_FUNDED, 0) == roles0.get(seams.ROLE_FUNDED, 0)
    # THE PAPER-SHARE TRAP: the total moves by exactly what the parts moved, never less
    delta_parts = sum(roles1.values()) - sum(roles0.values())
    assert total1 - total0 == delta_parts == 2


def test_a_failing_counter_never_reaches_the_leg(monkeypatch):
    monkeypatch.setattr(seams, "_jup_call", lambda method, path, body=None, **kw: (201, {"x": 1}))

    def broken(*a, **k):
        raise RuntimeError("counter down")
    monkeypatch.setattr(seams, "note_role_quote", broken)
    assert pcb._funded_jup_call("GET", "/quote?x=1") == (201, {"x": 1})


def test_the_leg_age_rides_the_build_detail_and_the_lane_moves_it_into_slot_build():
    """Builders report `leg_age_s`; the lane records it on slot_build and hands the executor the
    detail WITHOUT it, so the validity dict the executor reads is unchanged."""
    from sniper.services import memecoin_slot_lane as lane
    detail = {"blockhash": "h", "last_valid_height": 1, "target_mint": "M", "leg_age_s": 0.42}
    marks = []

    class Trace:
        def mark_substage(self, name, **kw):
            marks.append((name, kw))
    validity = lane.record_slot_build(Trace(), detail, duration_ms=1.5)
    assert marks == [("slot_build", {"duration_ms": 1.5, "leg_age_s": 0.42})]
    assert "leg_age_s" not in validity and validity["target_mint"] == "M"
    marks.clear()
    assert lane.record_slot_build(Trace(), None, duration_ms=2.0) is None     # a refused build
    assert marks == [("slot_build", {"duration_ms": 2.0})]
