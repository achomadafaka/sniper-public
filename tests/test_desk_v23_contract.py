"""v23 grammar (operator 2026-10-23): forecast-v1 "f" on every answer (Gamma's frozen grid) and re-entry as a WATCH
(only W px_hold, W px_up above the post-exit high, or S). Older cohorts are unchanged."""
import pytest

from test_memecoin_desk_contract import api, judge, verdict
from test_desk_watch_v22_contract import STANDING, PLAN

F = {"u": 20, "d": 10, "h": 300, "p": 0.35, "t": 25, "i": 12, "s": "grind"}


def fj(raw, **kw):
    return judge(raw, standing=STANDING, bars_available=True, forecast=True, **kw)


def test_v22_cohort_refuses_f_and_keeps_results_byte_identical():
    out = judge({**verdict(), "p": PLAN}, standing=STANDING, bars_available=True)
    assert out["action"] == "ENTER" and "forecast" not in out
    assert judge({**verdict(), "p": PLAN, "f": F}, standing=STANDING, bars_available=True)["reason"] == "verdict_keys"   # not a v23 cohort


@pytest.mark.parametrize("raw", [
    {**verdict(), "p": PLAN, "f": F},
    {**verdict(v="W", e=["px_hold", "<=", -8, 2, 600]), "p": PLAN, "f": F},
    {**verdict(v="H", r=None), "p": None, "f": F},
    {**verdict(v="S", r=None), "p": None, "f": F}])
def test_every_answer_carries_the_forecast(raw):
    out = fj(raw)
    assert out["forecast"] == F and out["reason"] != "forecast_out_of_range"


@pytest.mark.parametrize("v,extra,action", [("E", {"r": [60, 20, None], "p": PLAN}, "ENTER"),
                                             ("S", {"r": None, "p": None}, "SKIP")])
def test_missing_f_is_ungradable_never_a_veto(v, extra, action):
    out = fj({**verdict(v=v), **extra})
    assert out["action"] == action and out["forecast"] == {"ungradable": "missing", "raw_sha256": None}


@pytest.mark.parametrize("bad", [
    None, {}, {**F, "u": 15}, {**F, "d": 25}, {**F, "h": 200}, {**F, "p": 0}, {**F, "p": 1}, {**F, "p": 1.2},
    {**F, "p": True}, {**F, "t": None}, {**F, "i": "x"}, {**F, "s": "moon"}, {**F, "x": 1},
    {k: v for k, v in F.items() if k != "s"}, {**F, "u": 20.0}])
def test_off_grid_f_is_ungradable_and_keeps_the_valid_enter(bad):
    out = fj({**verdict(), "p": PLAN, "f": bad})
    assert out["action"] == "ENTER" and out["forecast"]["ungradable"] == "off_grid" and len(out["forecast"]["raw_sha256"]) == 64


def test_a_bad_core_verdict_still_refuses_with_a_good_f():
    assert fj({**verdict(), "p": None, "f": F})["reason"] == "plan_out_of_range"
    assert fj({**verdict(v="Q"), "p": PLAN, "f": F})["reason"] == "verdict_enum"


def test_forecast_flag_needs_the_standing_grammar():
    with pytest.raises(ValueError):
        judge({**verdict(), "f": F}, forecast=True)


# --- re-entry as a WATCH ------------------------------------------------------------------------------------------
RE = {"min_px_up_pct": 4.0}


def rj(raw, reentry=RE):
    return fj(raw, reentry=reentry)


def test_reentry_never_enters_now():
    out = rj({**verdict(), "p": PLAN, "f": F})
    assert out["action"] == "SKIP" and out["reason"] == "reentry_enter_refused"


def test_reentry_never_watches_open_ended():
    assert rj({**verdict(v="H", r=None), "p": None, "f": F})["reason"] == "reentry_watch_refused"


def test_reentry_pullback_and_skip_pass():
    assert rj({**verdict(v="W", e=["px_hold", "<=", -8, 2, 600]), "p": PLAN, "f": F})["action"] == "WAIT"
    assert rj({**verdict(v="S", r=None), "p": None, "f": F})["reason"] == "model_skip"


def test_reentry_breakout_must_clear_the_post_exit_high():
    assert rj({**verdict(v="W", e=["px_up", ">=", 5, 1, 600]), "p": PLAN, "f": F})["action"] == "WAIT"
    assert rj({**verdict(v="W", e=["px_up", ">=", 4, 1, 600]), "p": PLAN, "f": F})["reason"] == "reentry_below_post_exit_high"


def test_reentry_breakout_with_unknown_high_is_unavailable():
    out = rj({**verdict(v="W", e=["px_up", ">=", 50, 1, 600]), "p": PLAN, "f": F}, reentry={"min_px_up_pct": None})
    assert out["reason"] == "cond_unavailable"


DRIFT = {"sheet_price_usd": 3.0e-5, "quote_price_usd": 3.3e-5, "drift_pct": 7.5, "noise_pct": 2.0, "k": 3.0, "limit_pct": 6.0}


def test_drift_block_on_the_sheet_only_when_given():
    from test_memecoin_desk_contract import candidate
    assert "drift" not in api().render_judge_sheet(candidate())
    assert api().render_judge_sheet(candidate(), drift=DRIFT)["drift"] == DRIFT


@pytest.mark.parametrize("bad", [{}, {**DRIFT, "x": 1}, {**DRIFT, "drift_pct": None}, {**DRIFT, "quote_price_usd": 0},
                                 {**DRIFT, "k": float("nan")}])
def test_malformed_drift_refuses(bad):
    with pytest.raises(ValueError):
        api().validate_drift(bad)


def test_run_once_reads_the_requeue_claim():
    from sniper.services import memecoin_desk_judge as J
    assert J._drift_block({"candidate": {"drift_requeue": {"prev_ticket_id": "t", "drift": DRIFT}}}) == DRIFT
    assert J._drift_block({"candidate": {"drift_requeue": {"prev_ticket_id": "t", "drift": {"bad": 1}}}}) is None
    assert J._drift_block({"candidate": {}}) is None
