"""Tape-side attention signals (pure): creator_sell, sell_burst, buyer_exodus, volume_spike, holder_dump.
Definitions are Gamma-pre-registered; values here are PROVISIONAL test params, never production thresholds."""
import json

import pytest

from sniper.services import memecoin_desk_attention_signals as A

P = A.Params(creator_sell_min_usd=25.0, sell_burst_window_s=10.0, sell_burst_ratio=2.0, sell_burst_min_sellers=3,
             sell_burst_min_usd=50.0, exodus_active_s=30.0, exodus_quiet_s=15.0, exodus_min_buyers=5,
             volume_window_s=10.0, volume_baseline_s=120.0, volume_up_ratio=4.0, volume_down_ratio=4.0,
             volume_min_usd=100.0, holder_dump_pct=50.0, holder_dump_min_usd=25.0)
CREATOR = "CREATOR"
OURS = frozenset({"OUR_WALLET"})
T0 = 1_000_000.0


def sw(t, actor, buy, usd, sig=None, ordinal=0, token=1000):
    return {"received_at": T0 + t, "slot": 100 + int(t), "signature": sig or "s%s%s" % (t, actor), "ordinal": ordinal,
            "is_buy": 1 if buy else 0, "usd": usd, "actor": actor, "token_atomic": token}


def run(swaps, cut_t, **kw):
    return A.detect(swaps, creator=kw.pop("creator", CREATOR), ours=kw.pop("ours", OURS), opened_at=T0 + kw.pop("opened", 0),
                    cut=T0 + cut_t, params=P, **kw)


def kinds(events):
    return sorted(e["signal"] for e in events)


def test_creator_sell_fires_after_entry_above_floor_only():
    s = [sw(5, CREATOR, False, 30), sw(6, CREATOR, False, 10), sw(-5, CREATOR, False, 99)]
    ev = [e for e in run(s, 20, opened=0) if e["signal"] == "creator_sell"]
    assert len(ev) == 1 and ev[0]["value"]["usd"] == 30 and ev[0]["evidence"]["signature"] == "s5CREATOR"
    assert [e for e in run([sw(5, "someone", False, 99)], 20) if e["signal"] == "creator_sell"] == []


def test_our_own_wallet_never_counts():
    s = [sw(5, "OUR_WALLET", False, 500), sw(6, "OUR_WALLET", False, 500), sw(7, "OUR_WALLET", False, 500)]
    assert run(s, 20) == []
    s = [sw(5, CREATOR, False, 100)] + [sw(6, "OUR_WALLET", False, 100)]
    assert "sell_burst" not in kinds(run(s, 20))


def test_sell_burst_needs_distinct_noncreator_sellers_usd_floor_and_ratio():
    sells = [sw(10 + i, "a%d" % i, False, 30) for i in range(3)]                  # 3 sellers, 90 usd
    assert "sell_burst" in kinds(run(sells + [sw(11, "b", True, 20)], 20))         # ratio 4.5 >= 2
    assert "sell_burst" not in kinds(run(sells + [sw(11, "b", True, 80)], 20))     # ratio 1.1 < 2
    assert "sell_burst" not in kinds(run(sells[:2], 20))                            # 2 sellers < 3
    thin = [sw(10 + i, "a%d" % i, False, 5) for i in range(3)]
    assert "sell_burst" not in kinds(run(thin, 20))                                 # 15 usd < floor
    creator_heavy = [sw(10, CREATOR, False, 40), sw(11, CREATOR, False, 40), sw(12, "a", False, 40), sw(13, "b", False, 40)]
    assert "sell_burst" not in kinds(run(creator_heavy, 20))                        # creator is not a distinct seller: 2 sellers
    outside = [sw(0 + i, "a%d" % i, False, 30) for i in range(3)]
    assert "sell_burst" not in kinds(run(outside, 20))                              # older than the 10 s window


def test_sell_burst_with_zero_buys_is_infinite_ratio_not_a_division_error():
    sells = [sw(10 + i, "a%d" % i, False, 30) for i in range(3)]
    ev = [e for e in run(sells, 20) if e["signal"] == "sell_burst"]
    assert len(ev) == 1 and ev[0]["value"]["buy_usd"] == 0 and ev[0]["value"]["ratio"] is None


def test_buyer_exodus_active_window_then_quiet():
    active = [sw(i, "b%d" % i, True, 10) for i in range(6)]                         # 6 buyers in the first 6 s
    assert "buyer_exodus" in kinds(run(active, 25, opened=-60))                     # quiet since t=5 (>= 15 s at cut 25)
    assert "buyer_exodus" not in kinds(run(active, 12, opened=-60))                 # only 7 s quiet
    assert "buyer_exodus" not in kinds(run(active[:3], 25, opened=-60))             # never active (3 < 5 buyers)
    assert "buyer_exodus" not in kinds(run(active + [sw(20, "z", True, 10)], 25, opened=-60))   # a new buyer 5 s ago


def test_volume_spike_up_and_down_with_floor():
    base = [sw(-100 + 10 * i, "a%d" % i, i % 2 == 0, 30) for i in range(10)]        # ~30 usd per 10 s
    spike = [sw(5 + i, "s%d" % i, True, 100) for i in range(3)]                     # 300 usd in the window
    ev = [e for e in run(base + spike, 10, opened=-200) if e["signal"] == "volume_spike"]
    assert len(ev) == 1 and ev[0]["value"]["direction"] == "up"
    dead = [sw(-100 + 10 * i, "a%d" % i, True, 200) for i in range(10)]            # 200 usd per 10 s then silence
    ev = [e for e in run(dead, 10, opened=-200) if e["signal"] == "volume_spike"]
    assert len(ev) == 1 and ev[0]["value"]["direction"] == "down"
    quiet = [sw(-100 + 10 * i, "a%d" % i, True, 2) for i in range(10)]
    assert "volume_spike" not in kinds(run(quiet + [sw(5, "x", True, 8)], 10, opened=-200))     # under the usd floor


def test_holder_dump_fraction_of_snapshot_balance():
    holders = {"H1": 1000, "H2": 1000}
    s = [sw(5, "H1", False, 40, token=600), sw(6, "H2", False, 40, token=100), sw(7, "H1", True, 40, token=500)]
    ev = [e for e in run(s, 20, holders=holders) if e["signal"] == "holder_dump"]
    assert len(ev) == 1 and ev[0]["value"]["actor"] == "H1" and ev[0]["value"]["sold_pct"] == 60.0   # buys do not offset sells
    assert [e for e in run(s, 20) if e["signal"] == "holder_dump"] == []             # no snapshot: no signal (worker records unknown)
    tiny = [sw(5, "H1", False, 3, token=900)]
    assert [e for e in run(tiny, 20, holders=holders) if e["signal"] == "holder_dump"] == []     # under the usd floor


def test_only_swaps_in_the_committed_prefix_count():
    s = [sw(5, CREATOR, False, 30)]
    assert run(s, 5) == []                                                          # received_at == cut is excluded (strict)
    assert kinds(run(s, 5.001)) == ["creator_sell"]


def test_event_ids_are_stable_and_distinct_per_position_signal_and_key():
    s = [sw(5, CREATOR, False, 30)]
    a = run(s, 20)[0]
    b = run(s, 25)[0]
    assert A.event_id(7, a) == A.event_id(7, b) and A.event_id(7, a) != A.event_id(8, a)
    c = run([sw(6, CREATOR, False, 30, sig="other")], 20)[0]
    assert A.event_id(7, a) != A.event_id(7, c)


def test_event_values_are_closed_numbers_and_enums_only():
    for e in run([sw(5, CREATOR, False, 30)] + [sw(10 + i, "a%d" % i, False, 30) for i in range(3)], 20):
        A.validate_event(e)
        json.dumps(e["value"], allow_nan=False)
    bad = {"signal": "creator_sell", "value": {"usd": 30, "note": "BUY NOW"}, "evidence": {"signature": "s"}, "observed_at": 1.0}
    with pytest.raises(ValueError):
        A.validate_event(bad)
    with pytest.raises(ValueError):
        A.validate_event({"signal": "mystery", "value": {}, "evidence": {}, "observed_at": 1.0})
    with pytest.raises(ValueError):
        A.validate_event({"signal": "creator_sell", "value": {"usd": float("nan")}, "evidence": {"signature": "s"}, "observed_at": 1.0})


def test_our_fill_signatures_drop_swaps_even_when_the_actor_is_not_ours():
    sells = [sw(10 + i, "a%d" % i, False, 30) for i in range(3)]
    assert "sell_burst" in kinds(run(sells, 20))
    assert "sell_burst" not in kinds(run(sells, 20, ours_sigs=frozenset({sells[0]["signature"]})))    # a router in our tx


AGENT = "BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s"


def test_agent_ids_are_pinned_to_the_research_constant():
    from sniper.research.memecoin_opportunity import state as research_state
    assert A.AGENTS == research_state.MAYHEM_AGENT_IDS


def test_the_fee_free_agent_never_makes_a_burst_an_exodus_or_a_volume_spike():
    agent_sells = [sw(10 + i, AGENT, False, 100, sig="ag%d" % i) for i in range(6)]
    assert kinds(run(agent_sells, 20)) == []                                          # six agent sells: not a burst
    agent_buys = [sw(i, AGENT, True, 100, sig="ab%d" % i) for i in range(8)]
    assert "buyer_exodus" not in kinds(run(agent_buys, 40, opened=-60))               # the agent is not a crowd of buyers
    spike = [sw(5 + i, AGENT, True, 100, sig="as%d" % i) for i in range(3)]
    assert "volume_spike" not in kinds(run(spike, 10, opened=-200))                   # agent volume is not a spike
    assert "sell_burst" in kinds(run(agent_sells + [sw(11 + i, "r%d" % i, False, 30) for i in range(3)], 20))   # regular sellers still count


def test_agents_param_can_be_emptied():
    agent_sells = [sw(10 + i, AGENT + str(i), False, 100) for i in range(3)]
    assert "sell_burst" in kinds(run(agent_sells, 20))                                # different ids: counted
    sells = [sw(10 + i, AGENT, False, 100, sig="x%d" % i) for i in range(6)]
    assert "sell_burst" not in kinds(run(sells, 20))
    assert "creator_sell" in kinds(A.detect([sw(5, AGENT, False, 100)], creator=AGENT, ours=OURS, opened_at=T0, cut=T0 + 20, params=P))   # creator logic untouched


def test_agent_sells_cannot_lift_a_regular_burst_over_its_usd_floor():
    regular = [sw(10 + i, "r%d" % i, False, 10) for i in range(3)]                  # 3 sellers, 30 usd: under the 50 usd floor
    assert "sell_burst" not in kinds(run(regular + [sw(12, AGENT, False, 200, sig="ag")], 20))


def test_a_recent_agent_buy_does_not_hide_a_regular_buyer_exodus():
    active = [sw(i, "b%d" % i, True, 10) for i in range(6)]                          # 6 regular buyers, then silence
    assert "buyer_exodus" in kinds(run(active + [sw(22, AGENT, True, 100, sig="agq")], 25, opened=-60))


def test_agent_volume_is_not_a_spike_and_agent_baseline_is_not_a_collapse():
    base = [sw(-100 + 10 * i, "a%d" % i, i % 2 == 0, 3) for i in range(10)]          # ~3 usd per 10 s of regular flow
    agent_now = [sw(5, AGENT, True, 500, sig="agn")]
    assert "volume_spike" not in kinds(run(base + agent_now, 10, opened=-200))
    agent_base = [sw(-100 + 10 * i, AGENT, True, 500, sig="agb%d" % i) for i in range(10)]
    assert "volume_spike" not in kinds(run(agent_base, 10, opened=-200))             # no regular baseline: nothing to collapse from
