"""Agent-aware structure: the fee-free Mayhem agent is PRICE, never FLOW; its volume is reported apart."""
import json

import pytest

import test_desk_structure as TS
from sniper.research.memecoin_opportunity import state as research_state
from sniper.services import memecoin_desk_contract as contract
from sniper.services import memecoin_desk_structure as S

AGENT = "BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s"
sw = TS.sw
T = TS.T

REGULAR = [sw(-50, 1.0, True, 20, "A"), sw(-30, 1.1, True, 10, "B"), sw(-8, 1.2, False, 5, "C")]
AGENT_FLOW = [sw(-45 + 2 * i, 1.0 + 0.01 * i, bool(i % 2), 100, AGENT, sig="ag%d" % i) for i in range(20)]      # 2000 usd of agent trades


def run(swaps, **kw):
    return S.structure(swaps, as_of=T, cut_slot=TS.CUT, **kw)


def test_agent_ids_are_pinned_to_the_research_constant():
    assert S.AGENTS == research_state.MAYHEM_AGENT_IDS


def test_flow_figures_ignore_the_agent_and_report_it_apart():
    a, b = run(REGULAR), run(REGULAR + AGENT_FLOW)
    flow_keys = ["n_swaps", "buy_usd", "sell_usd", "net_delta_usd", "buyers", "sellers", "buy_share_trades_pct", "top3_trade_share_pct"]
    for w in ("15s", "60s", "300s", "launch"):
        for k in flow_keys:
            assert a["windows"][w][k] == b["windows"][w][k], (w, k)
    w60 = b["windows"]["60s"]
    assert w60["n_agent_swaps"] == 20 and w60["agent_buy_usd"] == 1000.0 and w60["agent_sell_usd"] == 1000.0
    assert a["windows"]["60s"]["n_agent_swaps"] == 0 and a["windows"]["60s"]["agent_buy_usd"] == 0.0


def test_a_mayhem_coin_with_thin_regular_flow_shows_thin_not_the_agents_volume():
    out = run(REGULAR + AGENT_FLOW)
    w = out["windows"]["60s"]
    assert (w["n_swaps"], w["buyers"], w["sellers"], w["buy_usd"], w["sell_usd"]) == (3, 2, 1, 30.0, 5.0)
    assert w["agent_buy_usd"] + w["agent_sell_usd"] == 2000.0                       # visible, separate
    assert out["volume_accel"] == run(REGULAR)["volume_accel"]                      # accel is regular flow only


def test_the_agent_stays_in_the_price_path():
    only_agent = run(AGENT_FLOW)
    w = only_agent["windows"]["60s"]
    assert w["n_swaps"] == 0 and w["buy_usd"] == 0.0 and w["buyers"] == 0              # no regular flow: a known zero
    assert w["high"] is not None and w["range_pct"] > 0 and only_agent["price_usd"] == pytest.approx(1.19)   # but the curve price moved
    assert only_agent["chart"] is not None and any(row[1] is not None for row in only_agent["chart"])


def test_chart_and_breakout_volume_exclude_the_agent():
    chart_a, chart_b = run(REGULAR)["chart"], run(REGULAR + AGENT_FLOW)["chart"]
    assert all(r_a[5:] == r_b[5:] for r_a, r_b in zip(chart_a, chart_b))              # buy_usd, sell_usd, makers per bar: identical
    base = TS.BASE + [sw(-3, 1.9, True, 25, AGENT, sig="agbo")]
    assert run(base)["breakout"]["60s"]["volume_vs_avg"] == run(TS.BASE)["breakout"]["60s"]["volume_vs_avg"]


def test_agents_param_can_be_emptied_and_the_sheet_validator_accepts_the_new_keys():
    counted = run(REGULAR + AGENT_FLOW, agents=frozenset())
    assert counted["windows"]["60s"]["n_swaps"] == 23 and counted["windows"]["60s"]["n_agent_swaps"] == 0
    contract.validate_structure(run(REGULAR + AGENT_FLOW))
    assert json.dumps(run(REGULAR + AGENT_FLOW), allow_nan=False)


def test_an_agent_swap_sharing_our_signature_is_still_removed_as_ours():
    base = run(REGULAR)
    out = run(REGULAR + [sw(-5, 9.0, True, 500, AGENT, sig="mine")], ours_sigs=frozenset({"mine"}))
    assert out["price_usd"] == base["price_usd"]                                    # ours (by signature) beats agent-in-the-price-path
