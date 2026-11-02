"""The PumpSwap sell instruction rebuilt from pool facts matches five real sells byte for byte.

Fixture: tests/fixtures/memecoin/pumpswap_sell_instructions.json, read 2026-10-17 (Alpha GO): one
creator-layout sell (24 accounts, the layout of every migrated pump pool we hold) and four
default-creator sells (23 accounts)."""
import json
from pathlib import Path

import pytest

from sniper.execution import pumpswap_sell as PS
from sniper.execution.pumpswap_buy import parse_pool_account

FIXTURE = json.loads((Path(__file__).parent / "fixtures/memecoin/pumpswap_sell_instructions.json").read_text())
DEFAULT_CREATOR = "11111111111111111111111111111111"


def _facts(accounts, creator):
    return {"base_mint": accounts[3], "quote_mint": accounts[4], "base_token_program": accounts[11],
            "quote_token_program": accounts[12], "pool_base_token_account": accounts[7],
            "pool_quote_token_account": accounts[8], "coin_creator": creator}


def _creator(sell):
    if len(sell["accounts"]) == 23:
        return DEFAULT_CREATOR
    pool = parse_pool_account(FIXTURE["state"]["pool24"]["value"][0]["data"][0])
    return pool["coin_creator"]


@pytest.mark.parametrize("sell", FIXTURE["sells"], ids=lambda s: "%d-%s" % (len(s["accounts"]), s["signature"][:8]))
def test_the_rebuilt_sell_matches_the_chain(sell):
    a = sell["accounts"]
    built = PS.pumpswap_sell_accounts(
        user=a[1], pool=a[0], pool_facts=_facts(a, _creator(sell)), protocol_fee_recipient=a[9],
        protocol_fee_recipient_token_account=a[10], buyback_fee_recipient=a[-2])
    assert [str(k) for k, _s, _w in built] == a
    raw = bytes.fromhex(sell["data_hex"])
    assert raw[:8] == PS.SELL_DISCRIMINATOR and len(raw) == 24
    amount, floor = int.from_bytes(raw[8:16], "little"), int.from_bytes(raw[16:24], "little")
    assert PS.sell_data(amount, floor) == raw


def test_both_layouts_are_in_the_fixture():
    assert sorted({len(s["accounts"]) for s in FIXTURE["sells"]}) == [23, 24]


def test_a_zero_amount_or_floor_never_builds():
    with pytest.raises(ValueError):
        PS.sell_data(0, 1)
    with pytest.raises(ValueError):
        PS.sell_data(1, 0)


def _events():
    out = []
    for sell in FIXTURE["sells"]:
        for line in sell.get("logs") or ():
            if line.startswith("Program data: "):
                event = PS.parse_sell_event(line.split(": ", 1)[1])
                if event is not None:
                    out.append(event)
    return out


def test_the_fixture_carries_five_sell_events():
    assert len(_events()) == 5


@pytest.mark.parametrize("event", [e for e in _events() if e["coin_creator_fee_basis_points"] == 0],
                         ids=lambda e: str(e["base_amount_in"]))
def test_the_sell_math_reproduces_the_program_within_its_reserve_rounding(event):
    got = PS.expected_quote_out(event["base_amount_in"], event["pool_base_token_reserves"],
                                event["pool_quote_token_reserves"], lp_bps=event["lp_fee_basis_points"],
                                protocol_bps=event["protocol_fee_basis_points"],
                                creator_bps=event["coin_creator_fee_basis_points"])
    assert got == pytest.approx(event["user_quote_amount_out"], rel=5e-7)
    assert PS._ceil_bps(event["quote_amount_out"], event["lp_fee_basis_points"]) == event["lp_fee"]
    assert PS._ceil_bps(event["quote_amount_out"], event["protocol_fee_basis_points"]) == event["protocol_fee"]


def test_a_migrated_pump_pool_sell_prices_the_virtual_quote():
    event = [e for e in _events() if e["coin_creator_fee_basis_points"] > 0][0]
    virtual = parse_pool_account(FIXTURE["state"]["pool24"]["value"][0]["data"][0])["virtual_quote"]
    kw = dict(lp_bps=event["lp_fee_basis_points"], protocol_bps=event["protocol_fee_basis_points"],
              creator_bps=event["coin_creator_fee_basis_points"])
    args = (event["base_amount_in"], event["pool_base_token_reserves"], event["pool_quote_token_reserves"])
    assert PS.expected_quote_out(*args, virtual_quote=virtual, **kw) == pytest.approx(
        event["user_quote_amount_out"], rel=1e-4)
    assert PS.expected_quote_out(*args, virtual_quote=0, **kw) < 0.92 * event["user_quote_amount_out"]


def test_the_floor_sits_under_the_chain_at_a_conservative_fee():
    event = [e for e in _events() if e["coin_creator_fee_basis_points"] > 0][0]
    virtual = parse_pool_account(FIXTURE["state"]["pool24"]["value"][0]["data"][0])["virtual_quote"]
    floor = PS.sell_floor(event["base_amount_in"], event["pool_base_token_reserves"],
                          event["pool_quote_token_reserves"], virtual_quote=virtual, slippage_pct=1.0,
                          fee_bps=150)
    assert 0.97 * event["user_quote_amount_out"] < floor < event["user_quote_amount_out"]
