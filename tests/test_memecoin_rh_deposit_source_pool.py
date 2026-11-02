"""Option C's v4_shared (Alpha GO 2026-10-08, pos 71908): a deposit-proved buy whose source swapped in a
V4 pool other than the coin's busiest native pool is bought through THAT pool, priced on the source's
own tape swap, behind MEMECOIN_RH_FAST_DEPOSIT_SOURCE_POOL (default off). The bridged deposit cap is
checked in X, not wei."""
import json
from fractions import Fraction

import pytest

from sniper.services import memecoin_rh_deposit_proof as dep
from sniper.services import memecoin_rh_fast_decide as fast
from sniper.services import memecoin_rh_fast_lane as lane
from sniper.settings import memecoin as cfg
from test_memecoin_rh_fast_decide import CTX, NATIVE, SIG, WETH, ZUP_PRE
from test_memecoin_rh_fast_routes import (A_SQRT, APOOL, B, B_LIQ, B_SQRT, BMETA, BPOOL, COIN, HOOK2,
                                          USDG, WPOOL2, _brow)

NPOOL, NPOOL2 = "0x" + "d4" * 32, "0x" + "e5" * 32
META = dict(BMETA, **{
    NPOOL: dict(protocol="v4", mint=COIN, token0=NATIVE, token1=COIN, fee=0, tick_spacing=200,
                hooks=HOOK2),
    NPOOL2: dict(protocol="v4", mint=COIN, token0=NATIVE, token1=COIN, fee=3000, tick_spacing=60,
                 hooks=NATIVE),
})
SRC_IN_X, SRC_OUT = 10 ** 8, 899_000_000
POST_B = B_SQRT + 1000


def _tape(source_row, *, a_liq=10 ** 26):
    """The busiest native pool NPOOL (two recent swaps), A = native/USDG, and the source's own swap."""
    tape = fast.TapeState(META.get)
    tape.ingest([_brow(NPOOL, B - 3, "0xn1", ZUP_PRE["sqrt"], liq=ZUP_PRE["liquidity"]),
                 _brow(NPOOL, B - 2, "0xn2", ZUP_PRE["sqrt"], liq=ZUP_PRE["liquidity"]),
                 _brow(APOOL, B - 2, "0xa", A_SQRT, liq=a_liq), source_row])
    tape.head = B - 1
    return tape


def _bridged_source():
    return _brow(BPOOL, B, "0xsrc", POST_B, liq=B_LIQ, a0=SRC_IN_X, a1=-SRC_OUT)


def _decide(tape, *, on=True, estimated=True, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_DEPOSIT_SOURCE_POOL", on)
    ctx = dict(CTX, allow_estimated_source=estimated)
    return fast.decide(dict(SIG, mint=COIN, payment_token=USDG, paid=-(-SRC_IN_X * 100 // 99)),
                       tape=tape, **ctx)


def test_off_waits_for_the_shared_routes_receipt(monkeypatch):
    # Off, the source's pool is a shared route, and the lane's shared executor refuses one without
    # its receipt (2026-10-14: 11 armed signals refused source_fill_unproved), so it re-asks.
    out = _decide(_tape(_bridged_source()), on=False, monkeypatch=monkeypatch)
    assert out["reason"] == "source_fill_pending" and out["needs_source_fill"] is True
    assert "pool_choice" not in out and out.get("tx") is None


def test_a_coin_x_source_pool_is_bridged_through_it_at_its_exact_post_state(monkeypatch):
    out = _decide(_tape(_bridged_source()), monkeypatch=monkeypatch)
    assert out["reason"] == "would_send", out
    assert (out["route_kind"], out["pool_choice"], out["pool"], out["bridge_pool"], out["bridge"]) == (
        "v4_bridge", "source_pool", BPOOL, APOOL, USDG)
    assert out["state_kind"] == "tape_post" and out["src_out"] == SRC_OUT
    clip_wei = int(Fraction(170) / Fraction(2500) * 10 ** 18)
    x_mid = clip_wei * A_SQRT ** 2 >> 192                        # hop A at its pre-trade MID
    cap = Fraction(2 ** 192, POST_B ** 2) * Fraction(13, 10)     # post x (1 + chase), premium off
    assert out["cap_price"] == cap and out["min_out"] == x_mid * cap.denominator // cap.numerator
    assert out["est_out"] >= out["min_out"]
    from sniper.execution.evm_v4_multihop_tx import native_multihop_buy_tx
    assert out["tx"] == native_multihop_buy_tx(
        "4663", COIN, [(USDG, 500, 10, NATIVE), (COIN, 10000, 200, HOOK2)], clip_wei, out["min_out"], 1060)


def test_a_thin_bridge_leg_whose_impact_exceeds_the_chase_is_refused(monkeypatch):
    # A's impact comes out of the chase headroom (min_out is at A's mid); past it, a sure revert.
    out = _decide(_tape(_bridged_source(), a_liq=10 ** 17), monkeypatch=monkeypatch)   # ~74% impact
    assert out["reason"] == "source_pool_impact_over_cap", out
    assert out["est_out"] < out["min_out"]


def test_the_hook_take_is_in_the_estimate_only(monkeypatch):
    tape = _tape(_bridged_source())
    with_take = _decide(tape, monkeypatch=monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_HOOK_OUTPUT_TAKE_BPS", {})
    without = _decide(tape, monkeypatch=monkeypatch)
    assert with_take["min_out"] == without["min_out"] and with_take["cap_price"] == without["cap_price"]
    assert with_take["est_out"] == without["est_out"] * (10_000 - 270) // 10_000


def test_a_native_source_pool_that_is_not_the_busiest_is_bought_through_itself(monkeypatch):
    src = _brow(NPOOL2, B, "0xsrc", ZUP_PRE["sqrt"] - 10 ** 30, liq=ZUP_PRE["liquidity"],
                a0=10 ** 16, a1=-(10 ** 21))
    out = _decide(_tape(src), monkeypatch=monkeypatch)
    assert (out["reason"], out["route_kind"], out["pool"], out["pool_choice"]) == (
        "would_send", "v4_native", NPOOL2, "source_pool"), out
    assert out["key"] == dict(currency0=NATIVE, currency1=COIN, fee=3000, tick_spacing=60, hooks=NATIVE)
    assert out["state_kind"] == "tape_post" and out["src_in_wei"] == 10 ** 16


def test_a_weth_source_pool_is_bought_through_itself(monkeypatch):
    src = _brow(WPOOL2, B, "0xsrc", ZUP_PRE["sqrt"], liq=ZUP_PRE["liquidity"], a0=10 ** 16, a1=-(10 ** 21))
    out = _decide(_tape(src), monkeypatch=monkeypatch)
    assert (out["reason"], out["route_kind"], out["pool"], out["in_is_c0"]) == (
        "would_send", "v4_weth", WPOOL2, True), out


def test_a_coin_x_pool_with_no_native_x_pool_is_a_named_fallback(monkeypatch):
    tape = fast.TapeState(META.get)
    tape.ingest([_brow(NPOOL, B - 2, "0xn", ZUP_PRE["sqrt"], liq=ZUP_PRE["liquidity"]), _bridged_source()])
    tape.head = B - 1
    assert _decide(tape, monkeypatch=monkeypatch)["reason"] == "deposit_bridge_no_a_pool"


def test_the_receipt_path_is_untouched(monkeypatch):
    # Without a deposit (no estimate allowed) the shared route still waits for its receipt.
    out = _decide(_tape(_bridged_source()), estimated=False, monkeypatch=monkeypatch)
    assert out["reason"] == "source_fill_pending" and "pool_choice" not in out


def _payload(out):
    p = lane.lane_payload(dict(SIG, mint=COIN, permit_nonce=1), out, sent_epoch=1000.0)
    return json.loads(json.dumps(p))


def test_the_lane_gates_a_source_pool_buy_under_v4_shared(monkeypatch):
    p = dict(_payload(_decide(_tape(_bridged_source()), monkeypatch=monkeypatch)), tx_hash="0xs")
    assert p["pool_choice"] == "source_pool" and p["route_kind"] == "v4_bridge"
    live = lane.LiveLane.__new__(lane.LiveLane)
    live.stopped, live.host_clock = False, lambda: 1000.0
    live.routes = frozenset({"v4_native", "v4_bridge"})
    assert live.handle(p, recv_epoch=1000.0, safety_reason=None)["reason"] == "route_off:v4_shared"
    # With v4_shared on, the gate passes (the next check needs the pilot state this stub lacks).
    live.routes = frozenset({"v4_native", "v4_shared"})
    live.shared_worker, live.fills, live._pilot_pending, live.pilot_fills = None, 20, 0, 20
    assert live.handle(p, recv_epoch=1000.0, safety_reason=None)["reason"] == "pilot_complete"


def test_the_lane_rebuilds_the_source_pool_buy_from_the_datagram(monkeypatch):
    out = _decide(_tape(_bridged_source()), monkeypatch=monkeypatch)
    assert lane.lane_buy_tx(_payload(out), out["clip_wei"], 1060, CTX["taker"]) == (out["tx"], out["min_out"])


def _bridged_cap_payload(cap_x):
    post_x = Fraction(2 ** 192, POST_B ** 2)                         # X per coin atomic
    post_wei = post_x * Fraction(2 ** 192, A_SQRT ** 2)                 # wei per coin atomic
    return {"route_kind": "v4_bridge", "a_sqrt": A_SQRT, "post_wei_num": post_wei.numerator,
            "post_wei_den": post_wei.denominator, "cap_num": cap_x.numerator, "cap_den": cap_x.denominator}


@pytest.mark.parametrize("factor, ok", [
    (Fraction(13, 10), True),                    # exactly post x (1 + chase): the cap decide builds
    (Fraction(1), True),                         # tighter
    (Fraction(131, 100), False),                 # looser than post x (1 + chase)
])
def test_a_bridged_deposit_cap_is_checked_in_x(monkeypatch, factor, ok):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 30.0)
    cap_x = Fraction(2 ** 192, POST_B ** 2) * factor
    assert dep.cap_bound(_bridged_cap_payload(cap_x)) is ok


def test_the_decided_bridged_cap_passes_cap_bound(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 30.0)
    out = _decide(_tape(_bridged_source()), monkeypatch=monkeypatch)
    assert dep.cap_bound(_payload(out)) is True


def test_a_thin_single_hop_source_pool_is_refused(monkeypatch):
    src = _brow(NPOOL2, B, "0xsrc", ZUP_PRE["sqrt"] - 10 ** 30, liq=10 ** 15, a0=10 ** 16, a1=-(10 ** 21))
    out = _decide(_tape(src), monkeypatch=monkeypatch)
    assert out["reason"] == "source_pool_impact_over_cap" and out["est_out"] < out["min_out"], out


def test_a_weth_source_pool_where_the_coin_is_currency0_swaps_one_for_zero(monkeypatch):
    # Gamma LOW: a coin whose address sorts below WETH is currency0, so the buy pays currency1 (WETH).
    from sniper.execution import evm_v4_quote as v4
    from test_memecoin_rh_fast_decide import TAKER
    lo = "0x01" + "ab" * 19
    npool, wpool = "0x" + "f6" * 32, "0x" + "f7" * 32
    meta = {npool: dict(protocol="v4", mint=lo, token0=NATIVE, token1=lo, fee=0, tick_spacing=200,
                        hooks=HOOK2),
            wpool: dict(protocol="v4", mint=lo, token0=lo, token1=WETH, fee=3000, tick_spacing=60,
                        hooks=NATIVE)}
    assert lo < WETH
    post = int(Fraction(1, 10 ** 5) ** Fraction(1, 2) * 2 ** 96)       # ~1e-5 WETH per coin atomic
    tape = fast.TapeState(meta.get)
    tape.ingest([_brow(npool, B - 3, "0xn1", ZUP_PRE["sqrt"], liq=ZUP_PRE["liquidity"]),
                 _brow(npool, B - 2, "0xn2", ZUP_PRE["sqrt"], liq=ZUP_PRE["liquidity"]),
                 _brow(wpool, B, "0xsrc", post, liq=10 ** 24, a0=-(10 ** 21), a1=10 ** 16)])
    tape.head = B - 1
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_DEPOSIT_SOURCE_POOL", True)
    out = fast.decide(dict(SIG, mint=lo, payment_token=WETH, paid=-(-10 ** 16 * 100 // 99)), tape=tape,
                      **CTX)
    assert (out["reason"], out["route_kind"], out["pool"], out["in_is_c0"]) == (
        "would_send", "v4_weth", wpool, False), out
    assert out["tx"] == v4.weth_buy_tx(dict(currency0=lo, currency1=WETH, fee=3000, tick_spacing=60,
                                            hooks=NATIVE), False, out["clip_wei"], out["min_out"],
                                       1060, taker=TAKER)
