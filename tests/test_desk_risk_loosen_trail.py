"""RISK-live LOOSEN_TRAIL (v8): the model may widen ONLY the trailing stop of a desk position, bounded, through the
tighten apply guard, and never when a rail would fire, after an attention tighten, or too often."""
import json
import time
from datetime import datetime, timezone

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_desk_risk as R
from sniper.services import memecoin_desk_risk_apply as A
from sniper.settings import memecoin as cfg
from test_memecoin_desk_risk_apply import desk, _order, _acks, _effective, _quote  # noqa: F401

REASON = "loosen:higher_lows,trend_up"


def _at(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _run(c, pos, db, now=None):
    now = time.time() if now is None else now
    return A.apply_orders(c, pos, now_epoch=now, now_iso=_at(now), path=db)


def _loosen(r, pos, trail=22, reason=REASON, **kw):
    return _order(r, pos, "LOOSEN_TRAIL", {"trail_pct": trail}, reason=reason, **kw)


def test_a_loosen_widens_only_the_trail_and_records_the_citation(desk):
    c, pos, db, r = desk
    before = _effective(c, pos)
    d = _loosen(r, pos, 22)
    _run(c, pos, db)
    eff = _effective(c, pos)
    assert eff["trail_pct"] == 22.0
    assert (eff["stop_pct"], eff["tp_pct"], eff["derisk_pct"]) == (before["stop_pct"], before["tp_pct"], before["derisk_pct"])
    ack = _acks(c)[d]
    assert (ack["status"], ack["reason"], ack["action"]) == ("applied", "loosened", "LOOSEN_TRAIL")
    detail = json.loads(ack["detail_json"])
    assert detail["cited"] == ["higher_lows", "trend_up"] and detail["old_trail_pct"] == before["trail_pct"]
    assert detail["new_trail_pct"] == 22.0 and detail["cap"] == 25.0
    contract = json.loads(pos["funded_contract_json"])["policy"]
    assert contract["trail_pct"] == before["trail_pct"]                           # frozen contract untouched


@pytest.mark.parametrize("rails,reason,why", [
    ({"trail_pct": 26}, REASON, "above_cap"), ({"trail_pct": 12}, REASON, "not_wider"),
    ({"trail_pct": 15}, REASON, "not_wider"), ({"sl_pct": 30}, REASON, "rails_malformed"),
    ({"trail_pct": 22, "sl_pct": 30}, REASON, "rails_malformed"), ({"trail_pct": True}, REASON, "rails_malformed"),
    ({"trail_pct": 22}, None, "loosen_no_cite"), ({"trail_pct": 22}, "loosen:", "loosen_no_cite"),
    ({"trail_pct": 22}, "loosen:free_text_here", "loosen_no_cite")])
def test_a_bad_loosen_is_refused_and_changes_nothing(desk, rails, reason, why):
    c, pos, db, r = desk
    before = m.effective_exit_override(c, pos["id"])
    d = _order(r, pos, "LOOSEN_TRAIL", rails, reason=reason)
    _run(c, pos, db)
    assert (_acks(c)[d]["status"], _acks(c)[d]["reason"]) == ("refused", why)
    assert m.effective_exit_override(c, pos["id"]) == before


def test_the_cap_is_the_knob_never_above_the_cohort_bound_and_the_kill_knob_stops_new_loosens(desk, monkeypatch):
    c, pos, db, r = desk
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_RISK_TRAIL_MAX_PCT", 40.0)            # the cohort bound is 30
    assert R.loosen_cap(cfg.MEMECOIN_DESK_RAIL_BOUNDS) == 30.0
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_RISK_TRAIL_MAX_PCT", 18.0)
    d = _loosen(r, pos, 20)
    _run(c, pos, db)
    assert _acks(c)[d]["reason"] == "above_cap"
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_RISK_TRAIL_MAX_PCT", 25.0)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_RISK_TRAIL_LOOSEN", False)
    d2 = _loosen(r, pos, 22)
    _run(c, pos, db)
    assert _acks(c)[d2]["reason"] == "loosen_disabled" and _effective(c, pos)["trail_pct"] == 15.0   # kill = no NEW loosen


def test_a_loosen_never_unfires_a_rail_that_would_exit_now(desk):
    c, pos, db, r = desk
    entry = float(pos["entry_price"])
    c.execute("UPDATE memecoin_positions SET executable_peak_price=? WHERE id=?", (entry * 1.5, pos["id"]))
    c.commit()
    pos = m.get_position(c, pos["id"])
    _quote(c, pos, price=entry * 1.2)                  # 20% under the 1.5 peak: the 15% trail has already fired
    d = _loosen(r, pos, 22)
    _run(c, pos, db)
    ack = _acks(c)[d]
    assert ack["status"] == "refused" and ack["reason"] == "rail_would_fire:trail"
    assert _effective(c, pos)["trail_pct"] == 15.0


def test_the_no_unfire_guard_is_what_refuses_it(desk, monkeypatch):
    c, pos, db, r = desk
    entry = float(pos["entry_price"])
    c.execute("UPDATE memecoin_positions SET executable_peak_price=? WHERE id=?", (entry * 1.5, pos["id"]))
    c.commit()
    pos = m.get_position(c, pos["id"])
    _quote(c, pos, price=entry * 1.2)
    monkeypatch.setattr(A, "_current_rails_fire", lambda *a, **k: None)          # mutation: guard removed
    d = _loosen(r, pos, 22)
    _run(c, pos, db)
    assert _acks(c)[d]["status"] != "refused" or _acks(c)[d]["reason"] != "rail_would_fire:trail"
    # the wider trail (22%) would also fire at -20% from the peak? no: 20 < 22, so with the guard gone it APPLIES
    assert _acks(c)[d]["status"] == "applied"


@pytest.mark.parametrize("bad", ["stale", "absent"])
def test_a_loosen_needs_a_fresh_exact_quote(desk, bad):
    c, pos, db, r = desk
    if bad == "stale":
        _quote(c, pos, now=time.time() - 60)
    else:
        c.execute("DELETE FROM memecoin_route_series")
        c.commit()
    d = _loosen(r, pos, 22)
    _run(c, pos, db)
    assert _acks(c)[d]["status"] == "refused" and _effective(c, pos)["trail_pct"] == 15.0


def test_operator_override_refuses_a_loosen(desk):
    c, pos, db, r = desk
    m.set_position_exit_override(c, pos["id"], "operator", tp_pct=60, sl_pct=25, trail_pct=15, actor="op")
    d = _loosen(r, pos, 22)
    _run(c, pos, db)
    assert _acks(c)[d]["reason"] == "operator_override_active"


def test_one_applied_loosen_per_gap_and_none_within_the_gap_of_a_tighten(desk, monkeypatch):
    from sniper.util import clock
    c, pos, db, r = desk
    now = time.time()
    sim = [now]                                                  # loosens are stamped at application time: simulate it
    monkeypatch.setattr(time, "time", lambda: sim[0])
    _run = lambda c, pos, db, at=None: (sim.__setitem__(0, now if at is None else at),
                                        A.apply_orders(c, pos, now_epoch=sim[0], now_iso=_at(sim[0]), path=db))[1]
    d1 = _loosen(r, pos, 18, now=now)
    _run(c, pos, db, now)
    assert _acks(c)[d1]["status"] == "applied"
    d2 = _loosen(r, pos, 22, now=now + 1)
    _run(c, pos, db, now + 2)
    assert _acks(c)[d2]["reason"] == "loosen_rate_limited" and _effective(c, pos)["trail_pct"] == 18.0
    # after the gap a further loosen is fine
    d3 = _loosen(r, pos, 22, now=now + 70)
    _quote(c, pos, now=now + 70)                                 # a quote fresh at the simulated time
    _run(c, pos, db, now + 71)
    assert _acks(c)[d3]["status"] == "applied" and _effective(c, pos)["trail_pct"] == 22.0
    # a tighten, then a loosen inside the gap: refused
    t = _order(r, pos, "TIGHTEN", {"trail_pct": 12}, now=now + 140)
    _quote(c, pos, now=now + 140)
    _run(c, pos, db, now + 141)
    assert _acks(c)[t]["status"] == "applied"
    d4 = _loosen(r, pos, 20, now=now + 142)
    _quote(c, pos, now=now + 142)
    _run(c, pos, db, now + 143)
    assert _acks(c)[d4]["reason"] == "recent_tighten" and _effective(c, pos)["trail_pct"] == 12.0


def test_an_attention_auto_tighten_is_never_undone_by_a_model_loosen(desk):
    c, pos, db, r = desk
    now = time.time()
    m.record_desk_risk_apply(c, decision_id="attn-1", position_id=pos["id"], action="TIGHTEN", status="applied",
                             reason="tightened", decided_at=now - 500, applied_at=_at(now - 500), source="attention_auto")
    c.commit()
    d = _loosen(r, pos, 22, now=now)
    _run(c, pos, db, now)
    assert _acks(c)[d]["reason"] == "attention_tightened" and _effective(c, pos)["trail_pct"] == 15.0


def test_the_newest_change_wins_and_older_orders_are_superseded(desk):
    c, pos, db, r = desk
    now = time.time()
    old = _order(r, pos, "TIGHTEN", {"trail_pct": 12}, now=now, age=3.0)
    new = _loosen(r, pos, 20, now=now, age=1.0)
    _run(c, pos, db, now)
    acks = _acks(c)
    assert acks[new]["status"] == "applied" and (acks[old]["status"], acks[old]["reason"]) == ("refused", "superseded")


def test_loosened_is_pure_and_only_touches_the_trail():
    eff = {"stop_pct": 25.0, "tp_pct": 60.0, "trail_pct": 15.0, "derisk_pct": 0.0, "trail_arm_pct": 0.0, "trail_requires_derisk": False}
    merged, why = A.loosened(eff, {"trail_pct": 22}, 25.0)
    assert why == "loosened" and merged == {"stop_pct": 25.0, "tp_pct": 60.0, "trail_pct": 22.0, "derisk_pct": 0.0}
    assert A.loosened({**eff, "trail_pct": 0.0}, {"trail_pct": 22}, 25.0) == (None, "trail_off")       # OFF is never created
    assert A.loosened({**eff, "trail_pct": None}, {"trail_pct": 22}, 25.0) == (None, "trail_off")
    assert A.loosened({**eff, "trail_arm_pct": 10.0}, {"trail_pct": 22}, 25.0) == (None, "trail_arming_semantics")
    assert A.loosened({**eff, "trail_requires_derisk": True}, {"trail_pct": 22}, 25.0) == (None, "trail_arming_semantics")
    assert A.loosened(eff, {"trail_pct": float("nan")}, 25.0) == (None, "rails_malformed")


def _verdict(**over):
    item = {"k": "pos:1", "a": "L", "r": {"trail_pct": 20}, "c": 17, "f": ["trend_up", "higher_lows"]}
    item.update(over)
    return R.validate(json.dumps(item), key="pos:1", policy={"stop_pct": 25, "trail_pct": 15, "tp_pct": 60},
                      bounds=cfg.MEMECOIN_DESK_RAIL_BOUNDS)


def test_the_watcher_validator_accepts_only_a_well_formed_loosen():
    assert _verdict() == ("LOOSEN_TRAIL", {"trail_pct": 20.0}, "loosen:higher_lows,trend_up", 17)
    for over, why in (({"f": None}, "loosen_bad_cite"), ({"f": []}, "loosen_bad_cite"), ({"f": ["trend_up", "trend_up"]}, "loosen_bad_cite"),
                      ({"f": ["fear"]}, "loosen_bad_cite"), ({"f": ["a", "b", "c", "d", "e"]}, "loosen_bad_cite"),
                      ({"c": 6}, "loosen_bad_code"), ({"c": 20}, "loosen_bad_code"),
                      ({"r": {"trail_pct": 20, "sl_pct": 30}}, "loosen_rails"), ({"r": {"sl_pct": 30}}, "loosen_rails"),
                      ({"r": {"trail_pct": 10}}, "loosen_not_wider"), ({"r": {"trail_pct": 26}}, "loosen_above_cap"),
                      ({"r": {"trail_pct": True}}, "loosen_rails"), ({"r": None}, "loosen_rails")):
        out = _verdict(**over)
        assert out[0] == "HOLD" and out[2] == why, (over, out)
    no_f = json.dumps({"k": "pos:1", "a": "L", "r": {"trail_pct": 20}, "c": 17})
    assert R.validate(no_f, key="pos:1", policy={"trail_pct": 15}, bounds=cfg.MEMECOIN_DESK_RAIL_BOUNDS)[2] == "loosen_no_cite"
    # an f on any other action is a malformed verdict; a trail that is OFF cannot be loosened
    assert R.validate(json.dumps({"k": "pos:1", "a": "T", "r": {"sl_pct": 12}, "c": 7, "f": ["trend_up"]}), key="pos:1",
                      policy={"stop_pct": 25}, bounds=cfg.MEMECOIN_DESK_RAIL_BOUNDS)[2] == "verdict_keys"
    off = R.validate(json.dumps({"k": "pos:1", "a": "L", "r": {"trail_pct": 20}, "c": 17, "f": ["trend_up"]}), key="pos:1",
                     policy={"stop_pct": 25, "trail_pct": 0}, bounds=cfg.MEMECOIN_DESK_RAIL_BOUNDS)
    assert off[2] == "loosen_no_trail"


def test_the_watcher_validator_honours_the_kill_knob(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_RISK_TRAIL_LOOSEN", False)
    assert _verdict()[2] == "loosen_disabled"


def test_the_prompt_names_the_option_and_the_version_moved():
    from sniper.services import memecoin_desk_risk_live as L
    assert L.RISK_LIVE_VERSION == "desk-risk-live-v12"
    assert "LOOSEN_TRAIL" in L.SYSTEM_PROMPT and "at most 25" in L.SYSTEM_PROMPT and "__TRAIL_CAP__" not in L.SYSTEM_PROMPT
    assert all(("%d " % c) in L.SYSTEM_PROMPT for c in (17, 18, 19))


def test_a_partial_attention_tighten_also_blocks_a_loosen(desk):
    c, pos, db, r = desk
    now = time.time()
    m.record_desk_risk_apply(c, decision_id="attn-2", position_id=pos["id"], action="TIGHTEN", status="applied_partial",
                             reason="tightened_partial", decided_at=now - 500, applied_at=_at(now - 500), source="attention_auto")
    c.commit()
    d = _loosen(r, pos, 22, now=now)
    _run(c, pos, db, now)
    assert _acks(c)[d]["reason"] == "attention_tightened" and _effective(c, pos)["trail_pct"] == 15.0


def test_the_sixty_second_windows_use_the_ack_clock_not_the_raw_host_clock(desk):
    # acks are stamped with now_iso (the corrected clock). With the raw host clock 5 s AHEAD of it, a window start of
    # now_epoch - 60 lands after the first loosen's stamp when the second order comes 58 s later on the corrected clock, and
    # would let it through; the ack clock keeps it inside the window.
    c, pos, db, r = desk
    t0, skew = time.time(), 5.0
    d1 = _loosen(r, pos, 18, now=t0)
    A.apply_orders(c, pos, now_epoch=t0 + skew, now_iso=_at(t0), path=db)
    t1 = t0 + 58
    _quote(c, pos, now=t1 - 1)
    d2 = _loosen(r, pos, 22, now=t1)
    A.apply_orders(c, pos, now_epoch=t1 + skew, now_iso=_at(t1), path=db)
    acks = _acks(c)
    assert acks[d1]["status"] == "applied" and acks[d2]["reason"] == "loosen_rate_limited"
    assert A._ack_clock("not-a-time", 123.0) == 123.0 and A._ack_clock(_at(5.0), 99.0) == 5.0


def test_a_cohort_without_frozen_trail_bounds_refuses_a_loosen(desk):
    c, pos, db, r = desk
    cid = json.loads(pos["funded_contract_json"])["source"]["desk"]["cohort_id"]
    c.execute("UPDATE memecoin_desk_cohorts SET policy_json=? WHERE cohort_id=?",
              (json.dumps({"policy": {}}), cid))
    c.commit()
    d = _loosen(r, pos, 22)
    _run(c, pos, db)
    ack = _acks(c)[d]
    assert (ack["status"], ack["reason"]) == ("refused", "cohort_bounds_unknown")
