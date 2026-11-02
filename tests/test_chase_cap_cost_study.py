"""The chase-cap cost study recomputes the slot lane's signed minimum at looser caps. These pin its
restated formula to the live builder and its threshold search, so a builder change cannot silently
make the study measure a different floor."""
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "chase_cap_cost_study", Path(__file__).resolve().parents[1] / "scripts/research/chase_cap_cost_study.py")
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)


def _spl(avg=True):
    d = dict(expected_tokens_atomic=1_300_000_000_000, cost_usdc_atomic=50_000_000, source_price_usd=0.00004,
             cap_pct=30.0, base_decimals=6)
    if avg:
        d["source_average_price_usd"] = 0.000037
    return d


@pytest.mark.parametrize("avg", [True, False])
@pytest.mark.parametrize("cap", [30.0, 35.0, 45.0])
def test_min_out_matches_the_live_builder(monkeypatch, avg, cap):
    from sniper.execution import pump_curve_buy as pcb
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT", S.HARD_CAP_PCT)
    spl = _spl(avg)
    if avg:
        live = pcb.capped_min_out(spl["expected_tokens_atomic"], cap, source_price_usd=spl["source_price_usd"],
                                  source_cost_usdc_atomic=spl["cost_usdc_atomic"], source_price_cap_pct=cap,
                                  source_average_price_usd=spl["source_average_price_usd"])[0]
    else:      # rows from before SOL-1 carried no average: no hard floor then
        live = max(int(spl["expected_tokens_atomic"] * (1.0 - cap / 100.0)),
                   pcb.source_price_floor_atomic(spl["cost_usdc_atomic"], spl["source_price_usd"], cap, 6))
    assert S.min_out(spl, cap, S.HARD_CAP_PCT) == live


def test_needed_pp_is_the_boundary_and_variant_a_stops_at_the_hard_cap():
    spl = _spl()
    signed = S.min_out(spl, 30.0, S.HARD_CAP_PCT)
    assert S.needed_pp(spl, signed) <= 0.01                         # already fills at the cap
    short = int(signed * 0.9)                      # landed 10% fewer tokens than signed
    need = S.needed_pp(spl, short, "B")
    assert need is not None and S.min_out(spl, *S.caps_at(30.0, need, "B")) <= short
    assert S.min_out(spl, *S.caps_at(30.0, need - 0.05, "B")) > short
    assert S.caps_at(30.0, 20.0, "A") == (35.0, 35.0)              # the settings clamp
    assert S.needed_pp(spl, int(signed * 0.5), "A") is None         # chase alone cannot buy it


def test_nearest_respects_the_tolerance():
    t0 = datetime(2026, 10, 5, tzinfo=timezone.utc)
    pts = [(t0 + timedelta(seconds=s), float(s)) for s in (5, 52, 80)]
    assert S.nearest(pts, t0 + timedelta(seconds=60), 15)[1] == 52.0
    assert S.nearest(pts, t0 + timedelta(seconds=300), 30) is None


def test_a_buy_fills_by_the_slot_leg_first_then_the_earliest_fallback_in_reach(monkeypatch):
    t0 = datetime(2026, 10, 5, tzinfo=timezone.utc)
    iso = lambda s: (t0 + timedelta(seconds=s)).isoformat()                  # noqa: E731
    monkeypatch.setattr(S, "capture_sells", lambda cap, mint, sig: [
        (t0 + timedelta(seconds=s), 2.0, None) for s in range(0, 700, 5)])
    legs = [(iso(1), 47.0, 30.0, 1.0, "a1"), (iso(9), 33.0, 30.0, 1.6, "a2")]   # needs +17 then +3
    r = dict(need_pp_A=None, need_pp_B=12.0, ret_60=5.0, ret_300=6.0)
    assert S.buy_at(r, legs, None, "m", "s", 20.0, "B", 0.5)["leg"] == "slot"
    out = S.buy_at(r, legs, None, "m", "s", 5.0, "B", 0.5)                  # slot out of reach
    assert out["leg"] == "fallback" and out["ret_60"] == pytest.approx(100 * (2.0 / 1.6 - 1) - 0.5)
    assert S.buy_at(dict(r, need_pp_B=None), legs[:1], None, "m", "s", 10.0, "B", 0.5) is None
    assert S.buy_at(dict(r, need_pp_A=None), legs[:1], None, "m", "s", 20.0, "A", 0.5) is None   # 47 > 35 clamp


def test_one_winner_cannot_carry_the_verdict():
    def buy(ret, first=1.0):
        o = dict(leg="slot", ret_60=ret, ret_300=ret)
        return dict(fate="missed", first_B=first, out={("B", t): o for t in S.THRESHOLDS})
    heavy = [buy(400.0)] + [buy(-30.0) for _ in range(9)]                   # mean +13, median -30
    assert S.verdict(heavy, S.MIN_POOLED) is None
    steady = [buy(4.0) for _ in range(9)] + [buy(-2.0)]
    assert S.verdict(steady, S.MIN_POOLED) == 20.0
    assert S.verdict(steady[:7], S.MIN_POOLED) is None                       # under the floor
