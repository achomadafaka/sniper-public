"""structure-v2 sheet (watch-list cohort): facts instead of verdict words. v1 stays byte-identical. Deterministic fixtures, one per rule."""
import json

import pytest

from sniper.services import memecoin_desk_contract as C
from sniper.services import memecoin_desk_structure as S

T, CUT = 1_000_000.0, 2000
AGENT = next(iter(S.AGENTS))
V2 = S.Params(sheet="v2")


def sw(rel, price, buy, usd, actor, **kw):
    return {"received_at": T + rel, "slot": 1000 + int(abs(rel)), "signature": "s%s%s" % (rel, actor), "ordinal": 0, "is_buy": 1 if buy else 0, "usd": usd,
            "price_usd": price, "actor": actor, **kw}


PRE = [sw(-50, 1.0, True, 10, "A"), sw(-40, 1.4, True, 10, "B"), sw(-30, 1.6, True, 10, "C")]       # prior high 1.6 in [-60, -15)


def run(swaps, params=V2, **kw):
    return S.structure(swaps, as_of=T, cut_slot=CUT, params=params, **kw)


def b60(swaps, **kw):
    return run(swaps, **kw)["breakout"]["60s"]


def test_v1_is_unchanged_and_default():
    base = [sw(-290, 1.0, True, 10, "A")] + PRE + [sw(-10, 1.8, True, 40, "F")]
    a, b = S.structure(base, as_of=T, cut_slot=CUT), S.structure(base, as_of=T, cut_slot=CUT, params=S.Params(sheet="v1"))
    assert a == b and a["version"] == "structure-v1" and "volume_accel" in a and "holding" in a["breakout"]["60s"] and "strength" in a["windows"]["300s"]["trend"]


def test_v2_stamps_version_and_drops_the_old_names():
    out = run(PRE + [sw(-10, 1.8, True, 40, "F")], launched_at=T - 290)
    assert out["version"] == "structure-v2" and out["sheet"] == "facts-v2" and "volume_accel" not in out
    assert "recent_volume_concentration_15_of_60" in out and "history_s" in out
    b = out["breakout"]["60s"]
    assert "holding" not in b and "broke" not in b and "volume_vs_avg" not in b and b["crossed"] is True
    assert "strength" not in out["windows"]["300s"]["trend"] and "abs_slope_pct_per_min" in out["windows"]["300s"]["trend"]


def test_one_crossing_print_has_no_follow_through():
    b = b60(PRE + [sw(-10, 1.8, True, 40, "F")])
    assert (b["crossed"], b["seconds_since_cross"], b["trades_above_after_cross"], b["regular_buy_usd_after"], b["sell_usd_after_cross"]) == (True, 10.0, 0, 0.0, 0.0)
    assert b["recovered_after_sell"] is None and b["cross_by"] == "regular"


def test_repeated_trades_above_the_level_without_selling():
    b = b60(PRE + [sw(-10, 1.8, True, 40, "F"), sw(-8, 1.85, True, 12, "G"), sw(-6, 1.9, True, 8, "H")])
    assert b["trades_above_after_cross"] == 2 and b["regular_buy_usd_after"] == 20.0 and b["sell_usd_after_cross"] == 0.0 and b["recovered_after_sell"] is None


def test_sell_then_recovery_and_sell_without_recovery():
    rec = b60(PRE + [sw(-12, 1.8, True, 40, "F"), sw(-9, 1.5, False, 5, "G"), sw(-6, 1.9, True, 9, "H")])
    assert rec["sell_usd_after_cross"] == 5.0 and rec["recovered_after_sell"] is True and rec["trades_above_after_cross"] == 1
    no = b60(PRE + [sw(-12, 1.8, True, 40, "F"), sw(-9, 1.5, False, 5, "G"), sw(-6, 1.55, True, 9, "H")])
    assert no["recovered_after_sell"] is False and no["trades_above_after_cross"] == 0


def test_cross_by_agent_mixed_unknown_regular():
    assert b60(PRE + [sw(-10, 1.8, True, 40, AGENT)])["cross_by"] == "agent"
    assert b60(PRE + [sw(-12, 1.55, True, 5, AGENT), sw(-10, 1.8, True, 40, "F")])["cross_by"] == "mixed"
    assert b60(PRE + [sw(-10, 1.8, True, 40, "F", price_only=True)])["cross_by"] == "unknown"
    assert b60(PRE + [sw(-10, 1.8, True, 40, "F")])["cross_by"] == "regular"


def test_no_cross_and_no_reference_stay_explicit():
    assert b60(PRE + [sw(-10, 1.6, True, 40, "F")])["crossed"] is False                  # equal to the prior high is not above it
    flat = b60(PRE + [sw(-10, 1.5, True, 40, "F")])
    assert flat["crossed"] is False and flat["seconds_since_cross"] is None and flat["trades_above_after_cross"] is None and flat["cross_by"] is None
    assert b60([sw(-10, 1.8, True, 40, "F")]) == {"state": "unknown", "reason": "no_reference"}


def test_nine_seconds_of_history_is_insufficient_not_a_low_number():
    out = run([sw(-9, 1.0, True, 10, "A"), sw(-5, 1.1, True, 10, "B")])
    assert out["history_s"] == 9.0 and out["recent_volume_concentration_15_of_60"] is None and out["recent_volume_concentration_reason"] == "insufficient_history"
    w = out["windows"]["60s"]
    assert w["covered_s"] == 9.0 and w["partial"] is True and w["buy_usd"] == 20.0         # the observed sums stay facts, only the ratio is nulled


def test_quiet_observed_is_not_missing_history():          # a launch time never extends coverage: history counts from our first swap
    quiet = run([sw(-200, 1.0, True, 10, "A")], launched_at=T - 250)
    assert quiet["history_s"] == 200.0 and quiet["recent_volume_concentration_15_of_60"] is None and quiet["recent_volume_concentration_reason"] == "no_volume_60"
    assert quiet["windows"]["60s"]["buy_usd"] == 0.0 and quiet["windows"]["60s"]["partial"] is False       # zero is KNOWN in a covered window


def test_full_history_concentration_matches_the_old_ratio_and_is_bounded():
    base = [sw(-290, 1.0, True, 10, "A"), sw(-100, 1.5, True, 20, "C"), sw(-40, 1.4, True, 10, "D"), sw(-20, 1.6, True, 30, "E"),
            sw(-10, 1.8, True, 40, "F"), sw(-5, 1.7, False, 8, "G")]
    v1, v2 = S.structure(base, as_of=T, cut_slot=CUT), run(base)
    assert v2["recent_volume_concentration_15_of_60"] == v1["volume_accel"] and v2["recent_volume_concentration_reason"] is None
    allin = run([sw(-300, 1.0, True, 1, "A"), sw(-5, 1.1, True, 100, "B")])
    assert allin["recent_volume_concentration_15_of_60"] == 4.0                          # the ceiling by construction


def test_equal_and_opposite_slopes_have_equal_abs_slope():
    up = run([sw(-100, 1.0, True, 1, "A"), sw(-50, 1.1, True, 1, "B"), sw(-1, 1.2, True, 1, "C")], launched_at=T - 290)["windows"]["300s"]["trend"]
    dn = run([sw(-100, 1.2, True, 1, "A"), sw(-50, 1.1, True, 1, "B"), sw(-1, 1.0, True, 1, "C")], launched_at=T - 290)["windows"]["300s"]["trend"]
    assert up["abs_slope_pct_per_min"] > 0 and dn["slope_pct_per_min"] < 0 < up["slope_pct_per_min"]
    assert up["abs_slope_pct_per_min"] == pytest.approx(abs(up["slope_pct_per_min"])) and dn["abs_slope_pct_per_min"] == pytest.approx(abs(dn["slope_pct_per_min"]))


def test_tape_gap_in_the_window_is_still_unknown():
    out = run(PRE + [sw(-10, 1.8, True, 40, "F")], gaps=(T - 30,))
    assert out["breakout"]["60s"] == {"state": "unknown", "reason": "tape_gap"} and out["recent_volume_concentration_15_of_60"] is None
    assert out["recent_volume_concentration_reason"] == "tape_gap"


def test_the_contract_accepts_v2_and_still_refuses_unknown_keys():
    out = run(PRE + [sw(-10, 1.8, True, 40, "F")], launched_at=T - 290)
    assert C.validate_structure(json.loads(json.dumps(out)))["version"] == "structure-v2"
    bad = json.loads(json.dumps(out))
    bad["breakout"]["60s"]["surprise"] = 1
    with pytest.raises(ValueError):
        C.validate_structure(bad)
    v1 = S.structure(PRE, as_of=T, cut_slot=CUT)
    assert C.validate_structure(json.loads(json.dumps(v1)))["version"] == "structure-v1"


def test_after_cross_uses_strict_tuple_order_equal_price_and_who_trades():
    cross = sw(-10, 1.8, True, 40, "F")
    # same received_at and slot, ordinal 1 after the cross print: it IS after; equal-to-prior is not above
    s2 = dict(sw(-10, 1.5, False, 7, "G"), ordinal=1, slot=cross["slot"], signature="x1")
    s3 = dict(sw(-8, 1.6, True, 3, "H"), signature="x2")                       # equal to the prior high 1.6: not above
    b = b60(PRE + [cross, s2, s3])
    assert b["trades_above_after_cross"] == 0 and b["sell_usd_after_cross"] == 7.0 and b["regular_buy_usd_after"] == 3.0 and b["recovered_after_sell"] is False
    # a price-only sell and an agent sell are never regular flow and never a recovery trigger
    po = b60(PRE + [cross, sw(-9, 1.5, False, 9, "G", price_only=True), sw(-6, 1.9, True, 5, "H")])
    ag = b60(PRE + [cross, sw(-9, 1.5, False, 9, AGENT), sw(-6, 1.9, True, 5, "H")])
    for x in (po, ag):
        assert x["sell_usd_after_cross"] == 0.0 and x["recovered_after_sell"] is None and x["trades_above_after_cross"] == 1


def test_sell_and_recovery_in_the_same_second_follow_the_ordinal():
    cross = sw(-10, 1.8, True, 40, "F")
    sell = dict(sw(-8, 1.5, False, 5, "G"), ordinal=0, signature="a")
    back = dict(sw(-8, 1.9, True, 5, "H"), ordinal=1, signature="b")
    assert b60(PRE + [cross, sell, back])["recovered_after_sell"] is True
    back0, sell1 = dict(back, ordinal=0), dict(sell, ordinal=1)
    assert b60(PRE + [cross, back0, sell1])["recovered_after_sell"] is False         # the recovery print came BEFORE the sell


def test_nothing_at_or_after_as_of_or_past_the_cut_enters_any_aggregate():
    late = [sw(0, 2.5, True, 999, "L"), dict(sw(-3, 2.6, True, 999, "M"), slot=CUT + 5)]
    clean = run(PRE + [sw(-10, 1.8, True, 40, "F")])
    dirty = run(PRE + [sw(-10, 1.8, True, 40, "F")] + late)
    assert clean["breakout"] == dirty["breakout"] and clean["history_s"] == dirty["history_s"]
    assert clean["recent_volume_concentration_15_of_60"] == dirty["recent_volume_concentration_15_of_60"]


def test_concentration_interval_boundaries_and_ceiling():
    # a swap exactly at as_of-15 counts in BOTH windows, one exactly at as_of-60 only in the 60 s window: the ratio stays in [0, 4]
    out = run([sw(-300, 1.0, True, 1, "A"), sw(-60, 1.0, True, 50, "B"), sw(-15, 1.0, True, 10, "C")])
    assert out["windows"]["15s"]["buy_usd"] == 10.0 and out["windows"]["60s"]["buy_usd"] == 60.0
    assert out["recent_volume_concentration_15_of_60"] == pytest.approx(10 / (60 / 4), abs=1e-4) and 0 <= out["recent_volume_concentration_15_of_60"] <= 4
    assert run([sw(-300, 1.0, True, 1, "A"), sw(-15, 1.0, True, 10, "B")])["recent_volume_concentration_15_of_60"] == 4.0


def test_unknown_or_future_launch_never_manufactures_coverage():
    for launched in (None, T + 500, float("nan")):
        out = run([sw(-9, 1.0, True, 10, "A")], launched_at=launched)
        assert out["history_s"] == 9.0 and out["windows"]["60s"]["covered_s"] == 9.0 and out["windows"]["60s"]["partial"] is True
        assert out["recent_volume_concentration_reason"] == "insufficient_history"


def test_unknown_sheet_value_is_refused_not_downgraded():
    with pytest.raises(ValueError):
        run(PRE, params=S.Params(sheet="v3"))


# --- the cohort policy switch and the callers that must stay on v1 ---------------------------------------------------
from test_desk_structure_tape import build, _cohort, MINT, NOW  # noqa: E402
from sniper.services import memecoin_desk_structure_tape as ST  # noqa: E402


def sheet_cohort(sheet):
    c = _cohort()
    if sheet is not None:
        c["policy"]["structure"]["sheet"] = sheet
    return c


def test_policy_sheet_facts_v2_selects_v2_and_passes_the_validator(tmp_path):
    out = ST.judge_structure_fn(build(tmp_path), lambda t: sheet_cohort("facts-v2"), lambda m: ())({"cohort_id": "c"}, {"mint": MINT}, NOW)
    assert out["version"] == "structure-v2" and out["sheet"] == "facts-v2" and "volume_accel" not in out
    assert C.validate_structure(json.loads(json.dumps(out)))["version"] == "structure-v2"


def test_absent_sheet_key_is_v1_and_an_unknown_value_is_refused(tmp_path):
    desk = build(tmp_path)
    v1 = ST.judge_structure_fn(desk, lambda t: sheet_cohort(None), lambda m: ())({"cohort_id": "c"}, {"mint": MINT}, NOW)
    assert v1["version"] == "structure-v1" and "volume_accel" in v1
    bad = ST.judge_structure_fn(desk, lambda t: sheet_cohort("facts-v3"), lambda m: ())({"cohort_id": "c"}, {"mint": MINT}, NOW)
    assert bad == {"version": "structure-v1", "tape": "unknown", "reason": "structure_invalid"}


def test_structure_for_defaults_to_v1_for_every_other_caller(tmp_path, monkeypatch):
    seen = []
    real = S.structure
    monkeypatch.setattr(S, "structure", lambda *a, **k: (seen.append(k.get("params")), real(*a, **k))[1])
    out = ST.structure_for(MINT, desk_dir=build(tmp_path), as_of=NOW)
    assert out["version"] == "structure-v1" and seen and all(p.sheet == "v1" for p in seen)


def test_the_risk_watcher_and_regime_callers_never_name_a_sheet():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for rel in ("scripts/memecoin_desk_risk_watcher.py", "sniper/services/memecoin_desk_regime.py"):
        src = (root / rel).read_text()
        assert "sheet=" not in src and "Params(" not in src and "facts-v2" not in src, rel                 # they call structure_for with its default (v1)


def test_any_lost_swap_in_the_window_makes_the_breakout_unknown_zeta_p1():
    cross = sw(-10, 1.8, True, 40, "F")
    unk = {"state": "unknown", "reason": "swaps_lost"}
    for lost in ((T - 11, T - 9, T - 8), (T - 9,), (T - 20,)):
        out = run(PRE + [cross], lost=lost)
        assert out["breakout"]["60s"] == unk and out["breakout"]["300s"] == unk, lost
    # Zeta's witness: the lost -20 swap at 3.0 would have made the prior 3.0 and the print no cross: never report crossed with a lost swap inside
    w = run(PRE + [cross, sw(-5, 1.9, True, 10, "G")], lost=(T - 20,))["breakout"]["60s"]
    assert w == unk
    # a lost swap OUTSIDE the 60 s window leaves the 60 s block alone (and unknown only for the 300 s one that contains it)
    out = run(PRE + [cross], lost=(T - 100,))
    assert out["breakout"]["60s"]["crossed"] is True and out["breakout"]["300s"] == unk
    # no lost list (a curve reader): the block is computed as before
    assert b60(PRE + [cross])["crossed"] is True and "flow_is_lower_bound" not in b60(PRE + [cross])


def test_coverage_gates_compare_raw_seconds_not_rounded_ones_zeta_p2():
    out = run([sw(-59.999, 1.0, True, 10, "A"), sw(-5, 1.1, True, 10, "B")])
    assert out["recent_volume_concentration_reason"] == "insufficient_history" and out["recent_volume_concentration_15_of_60"] is None
    w = out["windows"]["60s"]
    assert w["partial"] is True and w["covered_s"] == 60.0                       # shown rounded, decided on the raw 59.999
    edge = run([sw(-60.0, 1.0, True, 10, "A"), sw(-5, 1.1, True, 10, "B")])
    assert edge["windows"]["60s"]["partial"] is False and edge["recent_volume_concentration_reason"] is None


# --- v1 stays byte-identical: a golden generated from the UNMODIFIED main module (4662c2a29) -----------------------------
from pathlib import Path  # noqa: E402

GOLDEN = Path(__file__).parent / "fixtures" / "desk_structure_v1_golden.json"


def golden_cases():
    base = [sw(-290, 1.0, True, 10, "A"), sw(-200, 1.2, False, 5, "B"), sw(-100, 1.5, True, 20, "C"), sw(-40, 1.4, True, 10, "D"),
            sw(-20, 1.6, True, 30, "E"), sw(-10, 1.8, True, 40, "F"), sw(-5, 1.7, False, 8, "G")]
    return {"base": (base, {}), "base_launch": (base, {"launched_at": T - 290}), "cross_sell_recover": (PRE + [sw(-12, 1.8, True, 40, "F"), sw(-9, 1.5, False, 5, "G"), sw(-6, 1.9, True, 9, "H")], {}),
            "agent_cross": (PRE + [sw(-10, 1.8, True, 40, AGENT)], {}), "price_only": (PRE + [sw(-10, 1.8, True, 40, "F", price_only=True)], {}),
            "gap": (base, {"gaps": [T - 30]}), "lost": (base, {"lost": [T - 11, T - 9, T - 8]}), "one_lost": (base, {"lost": [T - 9]}),
            "thin": ([sw(-9, 1.0, True, 10, "A"), sw(-5, 1.1, True, 10, "B")], {}), "empty": ([], {})}


def test_v1_output_equals_the_stored_prechange_golden():
    golden = json.loads(GOLDEN.read_text())
    for name, (swaps, kw) in golden_cases().items():
        kw = dict(kw, gaps=tuple(kw.get("gaps", ())), lost=tuple(kw.get("lost", ())))
        got = json.loads(json.dumps(S.structure(swaps, as_of=T, cut_slot=CUT, **kw), sort_keys=True))
        assert got == golden[name], name
