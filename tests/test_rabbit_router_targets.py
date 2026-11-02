"""The router preparation targets (Gamma LOW on bd7c9f927): an OKX swap_tob or Jupiter route_v2 is a
pump_amm preparation target only when the owner authorises it AND the route has a PumpSwap leg.
Anything else would be counted as a buy in the lead-read pairs (arrivals.mark_buy) and spend the
prepare feed's warm-up reads, though the handlers re-check both before any fire."""
import json
from pathlib import Path

import pytest

from sniper.execution import jupiter_route, okx_swap
from sniper.services import memecoin_shyft_prepare as prep
from test_memecoin_rabbit_dflow_amm import candidate

FIXTURES = Path(__file__).parent / "fixtures/memecoin"
OKX = json.loads((FIXTURES / "okx_pump_amm_frames.json").read_text())
JUP = json.loads((FIXTURES / "jupiter_pump_amm_frames.json").read_text())
CASES = [(prep.okx_idl_target, okx_swap.OKX_PROGRAM, OKX), (prep.jupiter_idl_target, jupiter_route.JUPITER_PROGRAM, JUP)]


def _router_ix(fix, program):
    cand = candidate(fix["pure"][sorted(fix["pure"])[0]])
    (ix,) = [ix for ix in cand["instructions"] if ix[0] == program]
    return cand["owner"], ix[1], ix[2]


@pytest.mark.parametrize("target, program, fix", CASES)
def test_an_owner_authorised_pump_route_is_a_target(target, program, fix):
    owner, accounts, data = _router_ix(fix, program)
    assert target(accounts, data, owner)["venue"] == "pump_amm"


@pytest.mark.parametrize("target, program, fix", CASES)
def test_a_route_the_owner_did_not_authorise_is_no_target(target, program, fix):
    owner, accounts, data = _router_ix(fix, program)
    assert target((accounts[1],) + tuple(accounts[1:]), data, owner) is None


def test_an_okx_route_without_a_pump_leg_is_no_target(monkeypatch):
    owner, accounts, data = _router_ix(OKX, okx_swap.OKX_PROGRAM)
    real = okx_swap.decode_swap_tob
    monkeypatch.setattr(okx_swap, "decode_swap_tob", lambda d: dict(
        real(d), routes=tuple(("MeteoraDlmmSwap2", w, i) for _x, w, i in real(d)["routes"])))
    assert prep.okx_idl_target(accounts, data, owner) is None


def test_a_jupiter_route_without_a_pump_step_is_no_target(monkeypatch):
    owner, accounts, data = _router_ix(JUP, jupiter_route.JUPITER_PROGRAM)
    real = jupiter_route.decode_route_v2
    monkeypatch.setattr(jupiter_route, "decode_route_v2", lambda d: dict(
        real(d), steps=tuple(("Whirlpool", b, i, o) for _v, b, i, o in real(d)["steps"])))
    assert prep.jupiter_idl_target(accounts, data, owner) is None
