"""Jupiter route_v2 -> pump_amm pre-execution decoder (Alpha GO 2026-10-17; memecoin_rabbit_jupiter_amm).

Fixture: tests/fixtures/memecoin/jupiter_pump_amm_frames.json, real armed-wallet Jupiter frames
(PumpSwapBuyV3 = buy_exact_quote_in and PumpSwapBuyV4 = buy_exact_quote_in_v2), their pools and
mints, the funded lane's cached sol_usd within 14 s of each pure frame's block, and the wire bytes of
the v1 pure frames. The read state is built from the source's own executed BuyEvent.
"""
import asyncio
import base64
import json
import time
from pathlib import Path

import pytest

from sniper.execution import jupiter_route
from sniper.execution.pump_curve_buy import TOKEN_PROGRAM, TOKEN_2022_PROGRAM, WSOL_MINT
from sniper.execution.pumpswap_buy import parse_buy_event
from sniper.services import memecoin_preconf_signal as P
from sniper.services import memecoin_rabbit_jupiter_amm as jup
from sniper.services import memecoin_rabbit_signal as R
from sniper.services import memecoin_shyft_prepare as prep
from sniper.services.memecoin_feed_arrivals import StampedArrivals
from sniper.services.memecoin_rabbit_dflow_amm import DflowAmmHandler, encode_buy_event
from sniper.services.memecoin_source_initiation import source_initiation_refusal
from sniper.settings import memecoin as cfg
from test_memecoin_rabbit_dflow_amm import _fee_config, _pool_at_trade, _token_account, candidate, executed_event
from test_memecoin_rabbit_okx_amm import gross, trade_time_virtual_quote
from test_memecoin_rabbit_signal_stage2 import _ev, backstop

FIX = json.loads((Path(__file__).parent / "fixtures/memecoin/jupiter_pump_amm_frames.json").read_text())
PURE = sorted(FIX["pure"])


@pytest.fixture(autouse=True)
def knob_on(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_RABBIT_JUPITER_AMM", True)


def _sig(tx):
    return tx["transaction"]["signatures"][0]


def handler(tx=None, *, sol_usd=None, age=1.0, alerts=None):
    price = sol_usd if sol_usd is not None else FIX["sol_usd"].get(_sig(tx)) if tx else 100.0
    sink = [] if alerts is None else alerts
    return jup.JupiterAmmHandler(read_fn=lambda m, p: None, sol_usd_fn=lambda: (price, age),
                                 alert_fn=lambda t, x: sink.append(x))


def state_for(tx, plan, *, read_slot, held=0):
    ev = executed_event(tx)
    program = FIX["mints"][plan["mint"]]["owner"]
    accounts = {plan["pool"]: _pool_at_trade(FIX["pools"][plan["pool"]], trade_time_virtual_quote(ev)),
                plan["mint"]: FIX["mints"][plan["mint"]],
                plan["fee_config"]: _fee_config(int(ev["lp_fee_basis_points"]), int(ev["protocol_fee_basis_points"]),
                                                int(ev["coin_creator_fee_basis_points"])),
                plan["quote_vault"]: _token_account(WSOL_MINT, plan["pool"], int(ev["pool_quote_token_reserves"]), TOKEN_PROGRAM),
                plan["base_vaults"][program]: _token_account(plan["mint"], plan["pool"], int(ev["pool_base_token_reserves"]), program),
                plan["holders"][program]: (_token_account(plan["mint"], plan["owner"], held, program) if held else None)}
    other = TOKEN_2022_PROGRAM if program == TOKEN_PROGRAM else TOKEN_PROGRAM
    accounts.setdefault(plan["base_vaults"][other], None)
    accounts.setdefault(plan["holders"][other], None)
    return {"slot": read_slot, "accounts": accounts}


def _jup_ix(cand):
    return next(i for i, ix in enumerate(cand["instructions"]) if ix[0] == jupiter_route.JUPITER_PROGRAM)


def _with_data(cand, data):
    i = _jup_ix(cand)
    ixs = list(cand["instructions"])
    ixs[i] = (ixs[i][0], ixs[i][1], data)
    return dict(cand, instructions=tuple(ixs))


def _build(h, cand, plan, fill):
    return h.build(cand, plan, None, fill, source_user_id="uid", handle="h", signature_basis="ed25519_local",
                   feed="preconf:test", read_ms=3.0, recv_epoch=1792200000.0, now_mono=2.0)


def _step(tx):
    d = jupiter_route.decode_route_v2(candidate(tx)["instructions"][_jup_ix(candidate(tx))][2])
    return jupiter_route.pump_steps(d)[0][0]


def test_the_fixture_holds_a_v3_and_a_v4_frame():
    assert {_step(FIX["pure"][n]) for n in PURE} == {"PumpSwapBuyV3", "PumpSwapBuyV4"}


@pytest.mark.parametrize("name", PURE)
def test_the_in_amount_nets_the_platform_fee_and_is_the_spend_the_pool_took(name):
    tx = FIX["pure"][name]
    plan, why = handler(tx).match(candidate(tx))
    assert why is None, why
    spend = gross(executed_event(tx))
    assert abs(plan["sol_in"] - spend) * 10_000 <= 5 * spend                 # measured -2.3..+2.8 bps
    fee = plan["platform_fee_bps"]
    gross_of_fee = plan["sol_in"] * 10_000 / (10_000 - fee)                  # the same estimate, fee not taken
    assert abs(gross_of_fee - spend) * 10_000 >= (fee - 3) * spend


@pytest.mark.parametrize("name", PURE)
def test_a_modelled_fire_matches_the_executed_buy_event_on_v3_and_v4(name):
    tx = FIX["pure"][name]
    h, cand = handler(tx), candidate(tx)
    plan, why = h.match(cand)
    assert why is None, why
    ev = executed_event(tx)
    assert plan["pool"] == ev["pool"] and plan["pump_step"] in jupiter_route.PUMP_AMM_STEPS
    exact = dict(plan, sol_in=gross(ev))
    fill, why = h.verdict(exact, state_for(tx, exact, read_slot=tx["slot"] - 1), cand)
    assert why is None, why
    assert abs(fill["base_out"] - int(ev["base_amount_out"])) <= int(ev["base_amount_out"]) // 10 ** 9   # 2e-10
    assert (fill["lp_bps"], fill["protocol_bps"], fill["creator_bps"]) == (
        int(ev["lp_fee_basis_points"]), int(ev["protocol_fee_basis_points"]), int(ev["coin_creator_fee_basis_points"]))
    fill, why = h.verdict(plan, state_for(tx, plan, read_slot=tx["slot"] - 1), cand)
    assert why is None
    buy = _build(h, cand, plan, fill)
    assert buy.rabbit["route"] == jup.ROUTE == buy.source_initiation["route"]
    assert buy.rabbit["jup_pump_step"] == plan["pump_step"] and buy.rabbit["jup_platform_fee_bps"] == plan["platform_fee_bps"]
    assert int(parse_buy_event(buy.trade_event_log)["base_amount_out"]) == fill["base_out"]


def test_a_three_hop_route_refuses():
    for tx in FIX["three_hop"].values():
        assert handler(tx, sol_usd=100.0).match(candidate(tx)) == (None, "jup_route_unmodelled")


@pytest.mark.parametrize("variant", ["PumpSwapBuyV2", "PumpSwapBuy", "PumpSwapBuyV3WithCashbackClaim"])
def test_a_pump_step_variant_other_than_v3_or_v4_refuses(monkeypatch, variant):
    tx = FIX["pure"][PURE[0]]
    real = jupiter_route.decode_route_v2

    def decode(data):
        d = real(data)
        return dict(d, steps=tuple((variant if v.startswith("PumpSwapBuy") else v, b, i, o) for v, b, i, o in d["steps"]))
    monkeypatch.setattr(jupiter_route, "decode_route_v2", decode)
    assert handler(tx).match(candidate(tx)) == (None, "jup_pump_step_unmodelled")


def test_an_unknown_swap_variant_or_trailing_bytes_refuse():
    tx = FIX["pure"][PURE[0]]
    cand = candidate(tx)
    data = bytearray(cand["instructions"][_jup_ix(cand)][2])
    first = 8 + 8 + 8 + 2 + 2 + 2 + 4                 # disc, in, quoted out, slippage, fee, positive, vec len
    assert data[first] < 197
    data[first] = 197                                  # one past the pinned IDL's last Swap variant
    assert handler(tx).match(_with_data(cand, bytes(data))) == (None, "jup_decode")
    raw = cand["instructions"][_jup_ix(cand)][2]
    assert handler(tx).match(_with_data(cand, bytes(raw) + b"\x00")) == (None, "jup_decode")
    assert handler(tx).match(_with_data(cand, b"\x02" * 8 + bytes(raw[8:]))) == (None, "jup_not_route_v2")


@pytest.mark.parametrize("name", sorted(FIX["noncanonical"]))
def test_a_non_wsol_pool_refuses(name):
    tx = FIX["noncanonical"][name]
    assert handler(tx, sol_usd=100.0).match(candidate(tx)) == (None, "pool_noncanonical")


def test_shared_refusals_carry_the_jupiter_prefix_and_the_knob_is_its_own(monkeypatch):
    tx = FIX["pure"][PURE[0]]
    cand = candidate(tx)
    i = _jup_ix(cand)
    accts = list(cand["instructions"][i][1])
    ixs = list(cand["instructions"])
    ixs[i] = (ixs[i][0], tuple([accts[1]] + accts[1:]), ixs[i][2])
    assert handler(tx).match(dict(cand, instructions=tuple(ixs))) == (None, "jup_payer_not_owner")
    assert handler(tx, age=31.0).match(cand) == (None, "sol_usd_stale")
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_RABBIT_OKX_AMM", False)          # another router's knob
    assert handler(tx).match(cand)[1] is None
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_RABBIT_JUPITER_AMM", False)
    assert handler(tx).match(cand) is None


def test_the_landed_join_reads_the_v4_gross_and_pages_on_an_in_amount_miss():
    name = next(n for n in PURE if _step(FIX["pure"][n]) == "PumpSwapBuyV4")
    tx = FIX["pure"][name]
    for scale, pages in ((1.0, 0), (0.99, 1)):
        alerts = []
        h, cand = handler(tx, sol_usd=FIX["sol_usd"][_sig(tx)] * scale, alerts=alerts), candidate(tx)
        plan, _ = h.match(cand)
        fill, _ = h.verdict(plan, state_for(tx, plan, read_slot=tx["slot"] - 1), cand)
        _build(h, cand, plan, fill)
        ev = executed_event(tx)
        out = h.observe_processed(cand["signature"], encode_buy_event(dict(ev)))
        assert out["executed_spend"] == gross(ev) and out["miss_bps"] < 0.01 and len(alerts) == pages


def test_the_proof_passes_the_gate_only_with_the_jupiter_knob(monkeypatch):
    for knob in ("MEMECOIN_SLOT_LANE_RABBIT_SIGNAL", "MEMECOIN_SLOT_LANE_RABBIT_BUY"):
        monkeypatch.setattr(cfg, knob, True)
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_RABBIT_OKX_AMM", False)
    tx = FIX["pure"][PURE[0]]
    h, cand = handler(tx), candidate(tx)
    plan, _ = h.match(cand)
    fill, _ = h.verdict(plan, state_for(tx, plan, read_slot=tx["slot"] - 1), cand)
    buy = _build(h, cand, plan, fill)
    from sniper.services.memecoin_source_initiation import _pre_execution_refusal
    ev = _ev(buy)
    assert _pre_execution_refusal(ev, buy.source_initiation) is None
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_RABBIT_JUPITER_AMM", False)
    assert _pre_execution_refusal(ev, buy.source_initiation) == "wallet_initiation_unproved"


@pytest.mark.parametrize("sig", sorted(FIX["raw_b64"]))
def test_a_real_v1_frame_fires_end_to_end_through_the_preparation_gate(monkeypatch, sig):
    for knob in ("MEMECOIN_SLOT_LANE_RABBIT_SIGNAL", "MEMECOIN_SLOT_LANE_RABBIT_BUY", "MEMECOIN_SLOT_LANE_PRECONF"):
        monkeypatch.setattr(cfg, knob, True)
    tx = next(t for t in FIX["pure"].values() if _sig(t) == sig)
    price = FIX["sol_usd"][sig]
    owner = candidate(tx)["owner"]
    plan, _ = handler(tx).match(candidate(tx))
    state = state_for(tx, plan, read_slot=tx["slot"] - 1)
    raw = base64.b64decode(FIX["raw_b64"][sig])
    update, _verify = P.frame_update(tx["slot"], raw)
    cand = prep.preparation_candidate(update, {owner: ("uid", "h")}, recv_monotonic=1.0, generation=1)
    assert cand is not None and prep.preparation_target(cand)["venue"] == "pump_amm"

    def read(method, params):
        return {"context": {"slot": state["slot"]}, "value": [state["accounts"].get(k) for k in params[0]]}
    read.ping = lambda: None
    stats, fired = {}, []
    rabbit = backstop(read)
    buyer = R.RabbitBuyer(seen={}, owners_fn=lambda: {owner: ("uid", "h")}, on_buy=fired.append, backstop=rabbit,
                          feed=P.FEED, status=stats, landing=R.LandingWatch(backstop=rabbit, head_fn=lambda: (None, None),
                                                                            status={}),
                          head_fn=lambda: (None, None), epoch_fn=lambda: 1760000000.0)
    buyer.handlers += [DflowAmmHandler(read_fn=buyer.read),
                       jup.JupiterAmmHandler(read_fn=buyer.read, sol_usd_fn=lambda: (price, 1.0))]
    frames = type("Frames", (), {"frame": lambda self, *a: None, "note": lambda self, *a, **k: None})()
    status = {}
    sink = P.FrameSink(feed=P.FEED, owners_fn=lambda: {owner: ("uid", "h")},
                       arrivals=StampedArrivals(("shyft:grpc.fra.shyft.to", P.FEED)), frames=frames, status=status,
                       buyer=buyer)

    async def main():
        sink(tx["slot"], P.STATUS_SUCCESS, raw, time.monotonic(), 1)
        while buyer._tasks:
            await asyncio.sleep(0)
            await asyncio.gather(*list(buyer._tasks), return_exceptions=True)
    asyncio.run(main())
    assert stats["fired"] == 1 and len(fired) == 1, (status, stats)
    buy = fired[0]
    assert buy.rabbit["route"] == jup.ROUTE and buy.source_initiation["feed"] == P.FEED
    assert source_initiation_refusal(_ev(buy), "solana") is None
