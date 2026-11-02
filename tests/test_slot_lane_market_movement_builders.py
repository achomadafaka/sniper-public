"""Market-movement policy never turns source/own impact into a signed source-price floor."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from sniper.execution import pump_curve_buy as pcb
from sniper.services import memecoin_slot_lane as lane
from sniper.settings import memecoin as cfg

import test_slot_lane_cpmm as cpmm
import test_slot_lane_damm_v2 as damm
import test_slot_lane_dbc as dbc
import test_slot_lane_launchlab as launchlab
import test_slot_lane_price_cap as pump


@pytest.fixture(autouse=True)
def _movement_policy(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT", 35.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 30.0)


def test_source_and_our_impact_do_not_raise_the_expected_output_floor():
    # Source average $1, source post $2, our average $3: output safety alone remains signed.
    assert pcb.capped_min_out(
        100, 30, source_price_usd=2, source_cost_usdc_atomic=300_000_000,
        source_price_cap_pct=30, base_decimals=0, source_average_price_usd=1) == (70, 70, 0)


@pytest.mark.parametrize("expected", [0, 1])
def test_market_movement_mode_never_signs_a_zero_output_floor(expected):
    with pytest.raises(ValueError, match="no tokens"):
        pcb.capped_min_out(expected, 30, source_average_price_usd=None)


@pytest.mark.parametrize("slippage", [-1, 100, 110, float("nan"), float("inf")])
def test_invalid_native_output_slippage_refuses(slippage, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", slippage)
    with pytest.raises(ValueError, match="output slippage"):
        pcb.capped_min_out(100, 30, source_average_price_usd=None)


def test_missing_source_price_does_not_disable_output_slippage():
    assert pcb.capped_min_out(100, 30, source_price_cap_pct=30,
                               source_average_price_usd=None) == (70, 70, 0)


def test_legacy_mode_keeps_its_source_price_floor(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY", False)
    assert pcb.capped_min_out(
        100, 30, source_price_usd=2, source_cost_usdc_atomic=100_000_000,
        source_price_cap_pct=30, base_decimals=0, source_average_price_usd=1) == (75, 70, 75)


def _build(venue):
    if venue == "curve":
        builder, buy = pump._curve_builder()
        snapshot = SimpleNamespace(blockhash=pump.BLOCKHASH, last_valid_height=1, sol_usd=121.5)
        return builder.build(buy, 175.0, snapshot)
    if venue == "pumpswap":
        builder, buy, _event, _facts = pump._ps_builder(6)
        snapshot = SimpleNamespace(blockhash=pump.BLOCKHASH, last_valid_height=1, sol_usd=175.0)
        return builder.build(buy, 5.0, snapshot)
    if venue == "cpmm":
        builder = cpmm.C.SlotLaneCpmmBuilder(user=cpmm.USER, rpc=cpmm._no_pool_reads,
                     leg_fn=lambda: cpmm._leg(100.0), clock=lambda: 50.0)
        builder.pools = cpmm._cache(cpmm.SOL_EV)
        ev = cpmm.SOL_EV
        source = replace(cpmm._routed(ev),
                         usdc_spent_atomic=round((ev.input_amount + ev.input_transfer_fee) * 0.1225),
                         token_delta_atomic=ev.output_amount - ev.output_transfer_fee)
        return builder.build(lane.cpmm_slot_buy(source), 100.0, cpmm._snap())
    builder = launchlab._builder(lambda _mint: launchlab._leg(100.0), launchlab.SOL_B)
    ev = launchlab._ev(launchlab.SOL_B)
    source = replace(launchlab._routed(launchlab.SOL_B), token_delta_atomic=ev.amount_out,
                     usdc_spent_atomic=round(ev.amount_in * 0.1225))
    return builder.build(lane.launchlab_slot_buy(source), 100.0, launchlab._snap())


@pytest.mark.parametrize("venue", ["curve", "pumpswap", "cpmm", "launchlab"])
def test_native_messages_keep_only_their_nonzero_expected_output_floor(venue):
    message, detail = _build(venue)
    assert message is not None, detail
    limit = detail["pump_price_limit"]
    assert limit["price_floor_atomic"] == 0
    assert limit["min_tokens_atomic"] == limit["output_floor_atomic"] > 0
    assert limit["min_tokens_atomic"] == int(limit["expected_tokens_atomic"] * 0.7)
    assert limit["price_basis"] == "expected_output_slippage"
    assert limit["output_slippage_pct"] == 30.0
    # Every native ExactIn venue encodes amount_in then minimum_out after its discriminator.
    programs = {pcb.PUMP_PROGRAM, pump.ps.AMM_PROGRAM, cpmm.C.CPMM_PROGRAM,
                launchlab.LL.LAUNCHLAB_PROGRAM}
    swaps = [ix for ix in message.instructions
             if str(message.account_keys[ix.program_id_index]) in programs]
    assert len(swaps) == 1
    assert int.from_bytes(bytes(swaps[0].data)[16:24], "little") == limit["min_tokens_atomic"]


def test_a_wallet_movement_lift_over_100_keeps_the_base_native_output_floor(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT", 150.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_EXEC_PREMIUM_BY_WALLET", {"solana:lifted": 3.0})
    builder, buy = pump._curve_builder()
    buy.source_user_id = "lifted"
    snapshot = SimpleNamespace(blockhash=pump.BLOCKHASH, last_valid_height=1, sol_usd=121.5)
    message, detail = builder.build(buy, 175.0, snapshot)
    assert message is not None, detail
    limit = detail["pump_price_limit"]
    assert limit["cap_pct"] >= 100
    assert limit["min_tokens_atomic"] == int(limit["expected_tokens_atomic"] * 0.7) > 0
    assert limit["price_floor_atomic"] == 0


def test_payment_provenance_validation_still_rejects_an_inflated_whole_receipt():
    with pytest.raises(ValueError, match="routed average"):
        pcb.bounded_whole_average(2, 1)


@pytest.mark.parametrize("venue", ["damm", "dbc"])
def test_native_venues_without_expected_output_refuse_before_signing(venue, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_DBC", "live")
    if venue == "damm":
        builder, _leg = damm._builder(damm.CLASSIC)
        buy, why = damm.D.damm_slot_buy(damm._routed(damm.CLASSIC), builder.pools)
        assert why is None
        snapshot = SimpleNamespace(blockhash=damm.BLOCKHASH, last_valid_height=1, sol_usd=150.0)
    else:
        builder = dbc._builder(dbc._leg(pcb.WSOL_MINT, out=600_000_000))
        buy, snapshot = dbc._buy(dbc.SOL), dbc.SNAP
    assert builder.build(buy, 100.0, snapshot) == (None, "output_quote_unavailable")
