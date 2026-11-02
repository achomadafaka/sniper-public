"""v23 record path: forecast-v1 stored in vet inputs.forecast (record only); a re-entry WATCH ticket refuses E-now and
holds px_up above the post-exit high from the structure block the model saw."""
import pytest

from sniper.persistence import db_memecoin_desk as store
from sniper.settings import memecoin as cfg
from test_memecoin_desk_contract import candidate, evidence, policy, verdict
from test_memecoin_desk_pipeline import AT, POLICY, api, databases, decide  # noqa: F401
from test_desk_watch_v22_pipeline import STANDING, PLAN, BASIS, vol_structure
from test_desk_v23_contract import F

V23 = {**POLICY, "standing_orders": STANDING, "forecast": "forecast-v1", "facts": "v23-facts-v1",
       "structure": {"version": "structure-v1", "read_margin_s": 0.0}}
ASOF = {"candidate": candidate(), "meta": {"rank": {"price_now": BASIS}, "as_of": 1.8e9, "sol_usd": 180.0, "sol_age_s": 2.0}}


def ticket_in(databases, monkeypatch, pol=V23, cand=None):
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_STANDING_ORDERS", True)
    cohort = api().provision_cohort(*databases, producer="U", policy=pol, prompt_version="desk-judge-v23", created_at=AT)
    c = cand or candidate()
    t = api().ingest_candidate(databases[0], c, cohort_id=cohort, now_iso=AT)["ticket_id"]
    api().evaluate_candidate(databases[0], t, evidence=evidence(c), policy=policy(), now_iso=AT)
    return t


def vet(db, t):
    return [s for s in store.get_ticket(db[0], t)["stages"] if s["stage"] == "vet"][0]


def test_forecast_is_stored_with_the_code_reference(databases, monkeypatch):
    t = ticket_in(databases, monkeypatch)
    out = decide(databases, t, {**verdict(v="S", r=None), "p": None, "f": F}, asof=ASOF, structure=vol_structure(4.0, 30))
    assert out["action"] == "SKIP" and out["forecast"] == F
    rec = vet(databases, t)["inputs"]["forecast"]
    assert rec["v"] == "forecast-v1" and rec["f"] == F and rec["ref_price_sol"] == BASIS
    assert rec["ref_price_usd"] == pytest.approx(BASIS * 180.0) and rec["range5_pct"] == 4.0 and rec["t_f"] == 1.8e9
    assert rec["trigger"] == "first" and rec["prompt_version"] == "desk-judge-v23"


def test_missing_f_is_recorded_ungradable_and_the_enter_stands(databases, monkeypatch):
    t = ticket_in(databases, monkeypatch)
    out = decide(databases, t, {**verdict(), "p": PLAN}, asof=ASOF, structure=vol_structure(4.0, 30))
    assert out["action"] == "ENTER"
    assert vet(databases, t)["inputs"]["forecast"]["f"] == {"ungradable": "missing", "raw_sha256": None}


def test_unknown_reference_stores_null_ref(databases, monkeypatch):
    t = ticket_in(databases, monkeypatch)
    decide(databases, t, {**verdict(v="S", r=None), "p": None, "f": F}, asof=None, structure=None)
    rec = vet(databases, t)["inputs"]["forecast"]
    assert rec["ref_price_sol"] is None and rec["ref_price_usd"] is None


def test_v22_cohort_records_no_forecast(databases, monkeypatch):
    pol = {k: v for k, v in V23.items() if k not in ("forecast", "facts")}
    t = ticket_in(databases, monkeypatch, pol=pol)
    decide(databases, t, {**verdict(v="S", r=None), "p": None}, asof=ASOF, structure=vol_structure(4.0, 30))
    assert "forecast" not in vet(databases, t)["inputs"]


@pytest.mark.parametrize("bad", [{"forecast": "forecast-v0"}, {"facts": "x"}])
def test_provisioning_refuses_a_malformed_v23_key(databases, bad):
    with pytest.raises(ValueError):
        api().provision_cohort(*databases, producer="U", policy={**V23, **bad}, prompt_version="desk-judge-v23",
                               created_at=AT)


def test_forecast_needs_the_watch_grammar(databases):
    pol = {k: v for k, v in V23.items() if k != "standing_orders"}
    with pytest.raises(ValueError):
        api().provision_cohort(*databases, producer="U", policy=pol, prompt_version="desk-judge-v23", created_at=AT)


def reentry_structure(high_vs_now):
    st = vol_structure(4.0, 30)
    st["reentry"] = {"kind": "win", "high_vs_now_pct": high_vs_now}
    return st


def test_reentry_watch_ticket_refuses_enter_now(databases, monkeypatch):
    from sniper.services import memecoin_desk as service
    head = {"candidate": {"reentry": {"trigger": "watch"}}}
    assert service._reentry_rule(head, STANDING, reentry_structure(6.0)) == {"min_px_up_pct": 6.0}
    assert service._reentry_rule(head, STANDING, None) == {"min_px_up_pct": None}
    assert service._reentry_rule({"candidate": {"reentry": {"trigger": "recross"}}}, STANDING, None) is None
    assert service._reentry_rule({"candidate": {}}, STANDING, None) is None
    assert service._reentry_rule(head, None, None) is None


def test_entry_gate_default_passes_and_is_recorded(databases, monkeypatch):
    t = ticket_in(databases, monkeypatch)
    out = decide(databases, t, {**verdict(), "p": PLAN, "f": F}, asof=ASOF, structure=vol_structure(4.0, 30))
    assert out["action"] == "ENTER"
    assert vet(databases, t)["inputs"]["entry_gate"] == {"version": "entry-gate-v0", "decision": "pass", "error": None}


def test_entry_gate_watch_converts_enter_like_a_model_watch(databases, monkeypatch):
    from sniper.services import memecoin_desk as service
    monkeypatch.setattr(service, "entry_gate", lambda state: "watch")
    t = ticket_in(databases, monkeypatch)
    out = decide(databases, t, {**verdict(), "p": PLAN, "f": F}, asof=ASOF, structure=vol_structure(4.0, 30))
    assert out["action"] == "WATCH" and out["reason"] == "entry_gate_watch" and out["forecast"] == F
    gated = store.get_ticket(databases[0], t)["state"]
    c2 = {**candidate(), "ticket_id": "desk:U:fixture-h", "mint": candidate()["mint"][:-1] + "Z"}
    t2 = ticket_in(databases, monkeypatch, cand=c2)
    model_h = decide(databases, t2, {**verdict(v="H", r=None, k="desk:U:fixture-h"), "p": None, "f": F},
                     asof={**ASOF, "candidate": c2}, structure=vol_structure(4.0, 30))
    assert model_h["action"] == "WATCH" and store.get_ticket(databases[0], t2)["state"] == gated


def test_entry_gate_error_passes_with_the_error_named(databases, monkeypatch):
    from sniper.services import memecoin_desk as service
    monkeypatch.setattr(service, "entry_gate", lambda state: 1 / 0)
    t = ticket_in(databases, monkeypatch)
    assert decide(databases, t, {**verdict(), "p": PLAN, "f": F}, asof=ASOF, structure=vol_structure(4.0, 30))["action"] == "ENTER"
    assert vet(databases, t)["inputs"]["entry_gate"]["error"] == "ZeroDivisionError"


def test_entry_gate_knob_off_calls_nothing(databases, monkeypatch):
    from sniper.services import memecoin_desk as service
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_ENTRY_GATE", False)
    monkeypatch.setattr(service, "entry_gate", lambda state: "watch")
    t = ticket_in(databases, monkeypatch)
    assert decide(databases, t, {**verdict(), "p": PLAN, "f": F}, asof=ASOF, structure=vol_structure(4.0, 30))["action"] == "ENTER"
    assert "entry_gate" not in vet(databases, t)["inputs"]


GATE = {**V23, "entry_gate": "entry-gate-v1"}


def test_entry_gate_v1_turns_enter_now_into_the_frozen_px_hold(databases, monkeypatch):
    """Alpha GO 2026-10-26 (Gamma: WAIT -15% vs NOW -21%): no instant buy; the level is px_hold_max_noise x range5 deep."""
    t = ticket_in(databases, monkeypatch, pol=GATE)
    out = decide(databases, t, {**verdict(), "p": PLAN, "f": F}, asof=ASOF, structure=vol_structure(4.0, 30))
    assert out["action"] == "WAIT" and out["reason"] == "entry_gate_wait"
    inputs = vet(databases, t)["inputs"]
    assert inputs["entry_gate"] == {"version": "entry-gate-v1", "decision": "wait", "error": None}
    trig = inputs["trigger"]
    assert trig["kind"] == "px_hold" and trig["level_pct"] == pytest.approx(-2 * 4.0) and trig["hold_s"] == 5
    assert trig["level_price_sol"] == pytest.approx(BASIS * 0.92)


def test_entry_gate_v1_kill_knob_restores_enter_now(databases, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_ENTRY_GATE", False)
    t = ticket_in(databases, monkeypatch, pol=GATE)
    assert decide(databases, t, {**verdict(), "p": PLAN, "f": F}, asof=ASOF, structure=vol_structure(4.0, 30))["action"] == "ENTER"


@pytest.mark.parametrize("bad", [{"entry_gate": "entry-gate-v0"}, {"entry_gate": "x"}])
def test_provisioning_refuses_an_unknown_entry_gate(databases, bad):
    with pytest.raises(ValueError):
        api().provision_cohort(*databases, producer="U", policy={**V23, **bad}, prompt_version="desk-judge-v23", created_at=AT)


def test_entry_gate_needs_the_watch_grammar(databases):
    pol = {k: v for k, v in GATE.items() if k not in ("standing_orders", "forecast", "facts")}
    with pytest.raises(ValueError):
        api().provision_cohort(*databases, producer="U", policy=pol, prompt_version="desk-judge-v23", created_at=AT)


HOLD = {**verdict(v="W", e=["px_hold", "<=", -8, 2, 60]), "p": PLAN, "f": F}


def test_young_coin_px_hold_is_one_watch_then_skip(databases, monkeypatch):
    """P2: < 12 bars is too young to size a pullback; one re-judged WATCH, a second unknown-noise px_hold is SKIP."""
    from sniper.services import memecoin_desk as service
    t = ticket_in(databases, monkeypatch)
    out = decide(databases, t, HOLD, asof=ASOF, structure=vol_structure(4.0, 5))
    assert out["action"] == "WATCH" and out["reason"] == "px_hold_noise_watch" and "trigger" not in vet(databases, t)["inputs"]
    assert service._noise_watch_allowed(databases[0], t, {"policy": V23}) is True       # a first ticket may watch once
    from datetime import datetime, timedelta
    later = (datetime.fromisoformat(vet(databases, t)["inputs"]["response_at"]) + timedelta(seconds=31)).isoformat()
    assert service.rejudge_noise_watches(databases[0], now_iso=later) == [t + ":noise"]
    assert store.get_ticket(databases[0], t)["state"] == "expired"
    assert service._noise_watch_allowed(databases[0], t + ":noise", {"policy": V23}) is False  # the re-judge is final
    assert service.rejudge_noise_watches(databases[0], now_iso=later) == []                     # idempotent


def test_entry_gate_v1_on_a_young_coin_watches_instead_of_buying(databases, monkeypatch):
    t = ticket_in(databases, monkeypatch, pol=GATE)
    out = decide(databases, t, {**verdict(), "p": PLAN, "f": F}, asof=ASOF, structure=vol_structure(4.0, 5))
    assert out["action"] == "WATCH" and out["reason"] == "px_hold_noise_watch"


@pytest.mark.parametrize("knob,pol", [(False, V23), (True, {k: v for k, v in V23.items() if k not in ("forecast", "facts")})])
def test_young_coin_px_hold_skips_with_the_knob_off_or_on_a_v22_cohort(databases, monkeypatch, knob, pol):
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_PX_HOLD_WATCH", knob)
    t = ticket_in(databases, monkeypatch, pol=pol)
    raw = HOLD if "facts" in pol else {k: v for k, v in HOLD.items() if k != "f"}
    out = decide(databases, t, raw, asof=ASOF, structure=vol_structure(4.0, 5))
    assert out["action"] == "SKIP" and out["reason"] == "cond_unavailable"



@pytest.mark.parametrize("pct,action", [(95.0, "SKIP"), (89.0, "WAIT"), (None, "WAIT")])
def test_a_wait_past_the_graduation_guard_is_never_placed(databases, monkeypatch, pct, action):
    """Operator 10-26: 19 of 19 standing fires were refused by the 90 guard (coins pass 90% curve near 26k mcap).
    Unknown curve progress never refuses (the writer still guards every fire)."""
    from sniper.services import memecoin_desk as service
    monkeypatch.setattr(service, "_curve_pct", lambda asof, head: pct)
    t = ticket_in(databases, monkeypatch)
    out = decide(databases, t, HOLD, asof=ASOF, structure=vol_structure(4.0, 30))
    assert out["action"] == action
    if action == "SKIP":
        assert out["reason"] == "graduation_guard_at_wait" and "trigger" not in vet(databases, t)["inputs"]
        assert vet(databases, t)["inputs"]["curve_pct_at_wait"] == pct


def test_curve_pct_reads_the_asof_candidate_then_the_emission():
    from sniper.services import memecoin_desk as service
    snap = lambda v, c="complete": {"snapshot": {"fields": {"curve_pct": {"value": v, "completeness": c}}}}   # noqa: E731
    assert service._curve_pct({"candidate": snap(93.0)}, {"candidate": snap(10.0)}) == 93.0
    assert service._curve_pct(None, {"candidate": snap(10.0)}) == 10.0
    assert service._curve_pct({"candidate": snap(None)}, {"candidate": snap(95.0)}) is None      # as-of present: no fallback
    assert service._curve_pct({"candidate": snap(95.0, "partial")}, {"candidate": snap(10.0)}) is None
