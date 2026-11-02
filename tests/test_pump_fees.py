"""pump.fun fees from the pump_fees FeeConfig, pinned to 60 live buys (2026-09-28, research key).

The J7 native quote has no TradeEvent to read fees from. These frames are the decoder's evidence:
every modelled buy's fee bps must equal what the program itself reported in the event. A mayhem
curve is priced (95, 30) for a regular trader; the (0, 0) frames are the Mayhem agent's own trades.
"""
import base64
import json
import struct
from pathlib import Path

import pytest

from sniper.execution import pump_fees as PF
from sniper.execution.pump_curve_buy import parse_trade_event
from sniper.execution.pumpswap_buy import (
    mint_supply_and_decimals, parse_buy_event, parse_pool_account)

FX = json.loads((Path(__file__).parent / "fixtures" / "memecoin" /
                 "pump_fee_config_frames.json").read_text())
CURVE_CFG = PF.parse_fee_config(FX["fee_config_curve"]["data_b64"])
AMM_CFG = PF.parse_fee_config(FX["fee_config_amm"]["data_b64"])


def test_the_live_configs_decode_to_the_published_shapes():
    assert CURVE_CFG["flat"] == (0, 95, 30) and CURVE_CFG["tiers"] == [(0, (0, 95, 30))]
    assert AMM_CFG["flat"] == (25, 5, 0) and len(AMM_CFG["tiers"]) == 25
    assert AMM_CFG["tiers"][0] == (0, (2, 93, 30))
    assert AMM_CFG["tiers"][1] == (420 * 10**9, (20, 5, 95))
    assert AMM_CFG["tiers"][-1] == (98240 * 10**9, (20, 5, 5))


def test_every_pumpswap_buy_pays_the_decoded_fee():
    assert len(FX["amm_frames"]) == 30
    canonical = 0
    for frame in FX["amm_frames"]:
        ev = parse_buy_event(frame["line"])
        pool = parse_pool_account(FX["pool_accounts_b64"][ev["pool"]])
        supply = mint_supply_and_decimals(FX["mint_accounts_b64"][pool["base_mint"]])["base_supply"]
        got = PF.amm_fee_bps(AMM_CFG, pool, base_supply=supply,
                             base_reserve=ev["pool_base_token_reserves"],
                             quote_reserve=ev["pool_quote_token_reserves"])
        assert got == (ev["lp_fee_basis_points"], ev["protocol_fee_basis_points"],
                       ev["coin_creator_fee_basis_points"]), frame["sig"]
        from sniper.execution.pumpswap_buy import is_pump_migrated_pool
        canonical += is_pump_migrated_pool(pool)
    assert canonical == 23                    # both branches carry evidence: 23 tiered, 7 flat


def test_the_market_cap_includes_the_pools_virtual_quote():
    """The one boundary frame: vault-only reads 48,965 SOL (tier 35 bps); with virtual_quote it
    crosses 49,120 SOL and the program charged 30."""
    frame = next(f for f in FX["amm_frames"]
                 if parse_buy_event(f["line"])["pool"].startswith("8UC2p1cH"))
    ev = parse_buy_event(frame["line"])
    pool = parse_pool_account(FX["pool_accounts_b64"][ev["pool"]])
    supply = mint_supply_and_decimals(FX["mint_accounts_b64"][pool["base_mint"]])["base_supply"]
    kw = dict(base_supply=supply, base_reserve=ev["pool_base_token_reserves"],
              quote_reserve=ev["pool_quote_token_reserves"])
    assert PF.amm_fee_bps(AMM_CFG, pool, **kw) == (20, 5, 30)
    assert PF.amm_fee_bps(AMM_CFG, dict(pool, virtual_quote=0), **kw) == (20, 5, 35)


def test_every_curve_buy_pays_the_flat_fee_and_a_mayhem_curve_is_priced_like_any_other():
    assert len(FX["curve_frames"]) == 30
    modelled = agent = 0
    for frame in FX["curve_frames"]:
        ev = parse_trade_event(frame["line"])
        got = PF.curve_fee_bps(CURVE_CFG, FX["curve_accounts_b64"][ev["mint"]])
        if ev["mayhem_mode"] and ev["fee_basis_points"] == 0:       # the fee-free Mayhem agent's own trade
            assert got == (95, 30), frame["sig"]                    # never its (0, 0)
            agent += 1
        else:
            assert got == (ev["fee_basis_points"], ev["creator_fee_basis_points"]), frame["sig"]
            modelled += 1
    assert (modelled, agent) == (27, 3)


def _curve_with(**patch):
    raw = bytearray(base64.b64decode(next(iter(FX["curve_accounts_b64"].values()))).ljust(166, b"\0"))   # a V3 account
    raw[81] = patch.get("mayhem", raw[81])
    raw[82] = patch.get("cashback", 0)
    raw[83:115] = patch.get("key", bytes(32))
    struct.pack_into("<Q", raw, 115, patch.get("creator_bps", 0))
    return base64.b64encode(bytes(raw)).decode()


@pytest.mark.parametrize("patch, creator", [
    ({}, 30),                                                        # untouched account: the config default
    ({"key": b"\x07" * 32, "creator_bps": 300}, 300),               # per-coin creator fee (tape: 300)
    ({"key": b"\x07" * 32, "creator_bps": 100, "mayhem": 1}, 100),
    ({"key": b"\x07" * 32, "creator_bps": 0}, 30),                  # key set, bps 0: tape shows the default
    ({"cashback": 1}, 30),                                           # cashback coin: creator 0 + cashback 30 = the same total
    ({"mayhem": 1}, 30)])                                            # mayhem alone: (95, 30)
def test_the_creator_fee_is_read_from_the_curve_account(patch, creator):
    assert PF.curve_fee_bps(CURVE_CFG, _curve_with(**patch)) == (95, creator)


def test_live_curve_accounts_give_the_creator_fee_the_tape_charged():
    live = json.loads((Path(__file__).parent / "fixtures/memecoin/pump_curve_creator_fee_accounts.json").read_text())
    assert {f["tape_creator_bps"] for f in live} >= {300, 100, 0, 30} and any(f["mayhem"] for f in live)
    for f in live:
        cashback = 30 if f["is_cashback_coin"] else 0      # the tape's creator column omits the cashback leg
        assert PF.curve_fee_bps(CURVE_CFG, f["curve_b64"]) == (f["tape_protocol_bps"], f["tape_creator_bps"] + cashback), f["mint"]


def test_a_creator_fee_that_is_not_a_basis_point_count_is_refused():
    assert PF.curve_fee_bps(CURVE_CFG, _curve_with(creator_bps=10_000)) is None
    assert PF.curve_fee_bps(CURVE_CFG, _curve_with(creator_bps=2**40)) is None


@pytest.mark.parametrize("length", [81, 82, 114, 116, 117, 122, 123, 150, 165])
def test_a_curve_account_of_an_unrecorded_length_is_refused(length):
    raw = base64.b64decode(_curve_with(key=b"\x07" * 32, creator_bps=300))[:length]
    assert PF.curve_fee_bps(CURVE_CFG, base64.b64encode(raw).decode()) is None


def test_the_recorded_legacy_lengths_take_the_config_creator_fee():
    lengths = {len(base64.b64decode(v)) for v in FX["curve_accounts_b64"].values()}
    assert lengths == {125, 151} == set(PF.CURVE_LEGACY_LENGTHS)


@pytest.mark.parametrize("data", ["", "AAAA", None])
def test_an_unreadable_account_is_refused_not_guessed(data):
    assert PF.curve_fee_bps(CURVE_CFG, data) is None
    with pytest.raises((ValueError, TypeError)):
        PF.parse_fee_config(data)


def test_a_curve_config_with_tiers_is_refused_until_its_basis_is_verified():
    tiered = dict(CURVE_CFG, tiers=[(0, (0, 95, 30)), (10**12, (0, 90, 30))])
    curve = next(iter(FX["curve_accounts_b64"].values()))
    assert PF.curve_fee_bps(tiered, curve) is None
