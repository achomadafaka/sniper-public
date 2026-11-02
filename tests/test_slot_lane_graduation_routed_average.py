"""SOL01 (engineering review 2026-10-08, P1): a ROUTED source's graduation buy signs its hard floor on
the whole route's average, never on the single curve leg's.

The graduation builder passes both the source's curve-event average and the whole routed receipt's
average. The explicit-average branch in pumpswap_buy_instructions skipped bounded_whole_average, so a
split route (a cheaper parallel leg) signed a floor 21% over the whole average under a 15% cap. The
replay below is the review's own: the BLASTAR curve event, its canonical PumpSwap pool 10% under the
source's post-trade price, a routed receipt whose curve leg is half the coins it acquired. Every
minimum is decoded from the compiled AMM instruction, not from telemetry."""
import dataclasses
import struct
import time
from types import SimpleNamespace

import pytest

import test_slot_lane_dbc as D
import test_slot_lane_graduation as G
import test_slot_lane_graduation_quote_curves as Q
from sniper.execution import pump_curve_buy as pcb
from sniper.execution import pumpswap_buy as ps

CLIP = 50.0
LINE = G.FX["source"]["trade_event_log"]
SNAP = SimpleNamespace(blockhash=G.BLOCKHASH, last_valid_height=1, sol_usd=185.0)


@pytest.fixture
def cap15(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT", 15.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 30.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_EXEC_PREMIUM_BY_WALLET", {})
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT_BY_WALLET", {})
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ROUTED_AVG_MAX_RATIO", 1.06)
    return cfg


def _event_average():
    state, src = G._at_premium(G._state(), -0.10, SNAP.sol_usd)
    return state, pcb.curve_source_average_usd(pcb.parse_trade_event(LINE), src)


def _routed_buy(ratio):
    """The review's split route: the curve leg is half the coins, the whole route's average is
    `ratio` x the curve leg's. Converted by the lane's own pump_slot_buy."""
    state, event_average = _event_average()
    ev = pcb.parse_trade_event(LINE)
    tokens = 2 * int(ev["token_amount"])
    fields = G.S._curve(G.anchor_event_bytes(LINE), b"", any_user=True)
    venue = G.S.RoutedPumpBuy(venue="pump_curve", fields=tuple(fields.items()), trade_event_log=LINE,
                              token_program=pcb.TOKEN_2022_PROGRAM)
    routed = dataclasses.replace(
        D._routed(D.SOL), mint=G.MINT, source_user_id="u1", handle="degen", owner=ev["user"],
        signature=G.FX["source"]["signature"], slot=G.FX["source"]["slot"], token_delta_atomic=tokens,
        token_decimals=6, usdc_spent_atomic=round(event_average * ratio * tokens),
        venue_events=(venue,))
    buy, why = G.L.pump_slot_buy(routed, None)
    assert buy is not None and buy.routed == routed, why
    whole = routed.usdc_spent_atomic / 1e6 / (tokens / 1e6)
    return dataclasses.replace(buy, graduation=G._grad(state)), event_average, whole


def _signed_min(msg):
    keys = [str(k) for k in msg.account_keys]
    [ix] = [ix for ix in msg.instructions if keys[ix.program_id_index] == ps.AMM_PROGRAM]
    return struct.unpack_from("<QQ", bytes(ix.data), 8)[1]


def _build(buy):
    b = G._builder(time.monotonic)
    msg, detail = b.build(buy, CLIP, SNAP)
    return b, msg, detail


def _floor(average, cap):
    return pcb.source_price_floor_atomic(pcb.clip_atomic(CLIP), average, cap, 6)


def test_a_routed_graduation_signs_the_whole_route_floor(cap15):
    buy, event_average, whole = _routed_buy(0.95)
    b, msg, detail = _build(buy)
    assert msg is not None, detail
    # The review's numbers: 718,417,285,464 was signed against a 756,228,721,611 whole-route floor.
    assert _signed_min(msg) >= _floor(whole, 15.0) == 756_228_721_611
    assert b.last_build["source_average_price_usd"] == pytest.approx(whole)
    assert b.last_build["min_tokens_out"] == _signed_min(msg)


def test_a_whole_route_floor_over_the_expected_output_refuses(cap15):
    buy, _event_average, _whole = _routed_buy(0.60)
    _b, msg, why = _build(buy)
    assert msg is None and "source-price cap refuses" in why


def test_an_unknown_whole_average_refuses(cap15):
    buy, _event_average, _whole = _routed_buy(0.95)
    unknown = dataclasses.replace(buy, routed=dataclasses.replace(buy.routed, usdc_spent_atomic=0))
    _b, msg, why = _build(unknown)
    assert msg is None and why == "source average price unavailable"


def test_a_whole_average_far_over_its_curve_event_refuses(cap15):
    buy, _event_average, _whole = _routed_buy(1.10)
    _b, msg, why = _build(buy)
    assert msg is None and "routed average" in why


def test_a_whole_average_above_its_curve_event_within_the_bound_is_the_reference(cap15):
    # Operator 2026-10-08, option (a): the same rule as every other builder. The whole route paid more
    # per coin than its curve leg (fees on the way in) but within MEMECOIN_REAL_ROUTED_AVG_MAX_RATIO,
    # so the cap is over what the source actually paid.
    buy, event_average, whole = _routed_buy(1.03)
    b, msg, detail = _build(buy)
    assert msg is not None, detail
    assert whole > event_average
    assert b.last_build["source_average_price_usd"] == pytest.approx(whole)
    assert _signed_min(msg) >= _floor(whole, 15.0)


def test_a_non_routed_graduation_keeps_the_curve_event_average(cap15):
    state, event_average = _event_average()
    buy = G._slot_buy(LINE, graduation=G._grad(state))
    b, msg, detail = _build(buy)
    assert msg is not None, detail
    assert b.last_build["source_average_price_usd"] == pytest.approx(event_average)
    assert _signed_min(msg) >= _floor(event_average, 15.0)


def test_with_the_hard_cap_off_a_routed_graduation_ignores_both_averages(cap15):
    cap15.MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT = 0.0
    buy, _event_average, _whole = _routed_buy(0.95)
    unknown = dataclasses.replace(buy, routed=dataclasses.replace(buy.routed, usdc_spent_atomic=0))
    b, msg, detail = _build(unknown)
    assert msg is not None, detail
    assert b.last_build["source_average_price_usd"] is None


def test_a_wallet_cap_applies_to_the_whole_route_average(cap15):
    cap15.MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT_BY_WALLET = {"u1": 10.0}
    buy, _event_average, whole = _routed_buy(0.99)
    b, msg, detail = _build(buy)
    assert msg is not None, detail
    assert _signed_min(msg) >= _floor(whole, 10.0)


def test_a_routed_quote_curve_graduation_signs_the_whole_route_floor(cap15, monkeypatch):
    Q.quote_live.__wrapped__(monkeypatch)
    cap15.MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT = 35.0
    state = Q._hi_state()
    curve = pcb.parse_trade_event(Q.HI["trade_event_log"])
    quote_usd = 0.05
    src, average = pcb.quote_curve_prices(curve, 8, quote_usd, 6)
    q = int(src * 0.95 / quote_usd * state["base_reserve"] / 1e6 * 1e8) - state["virtual_quote"]
    grad = Q.G.payload(dict(state, quote_reserve=q), trigger="source_completed", offered_mono=0.0,
                       read_ms=1.0, now_mono=1.0)
    tokens = 1_000 * 10 ** 6
    routed = SimpleNamespace(usdc_spent_atomic=round(average * 0.95 * 1_000 * 1e6), token_delta_atomic=tokens,
                             token_decimals=6)
    whole = routed.usdc_spent_atomic / 1e6 / 1_000
    b = ps.SlotLanePumpSwapBuilder(user=Q.USER, jup_call=lambda *a, **k: None, rpc=lambda *a: None,
                                   clock=lambda: 50.0)
    b._buyback = ps.buyback_fee_recipient_from_global_config(Q.BL["global_config_b64"])
    b._protocol_recipient = ps.protocol_fee_recipient_from_global_config(Q.BL["global_config_b64"])
    spend_q = int(10.0 / quote_usd * 1e8)
    leg = {"ixs": [], "tables": [], "usdc_atomic": 10_000_000, "min_out_lamports": spend_q,
           "out_lamports": spend_q + 50, "fetched_at": 50.0, "quote": {}, "output_mint": Q.QUOTE}
    buy = Q._buy(Q.HI["trade_event_log"], graduation=grad, routed=routed)
    msg, detail = b.build(buy, 10.0, Q.SimpleNamespace(blockhash=Q.BLOCKHASH, last_valid_height=1, sol_usd=150.0),
                          leg=leg, quote=(8, quote_usd, 0, 0))
    assert msg is not None, detail
    assert b.last_build["source_average_price_usd"] == pytest.approx(whole)
    assert _signed_min(msg) >= pcb.source_price_floor_atomic(10_000_000, whole, 35.0, 6)
