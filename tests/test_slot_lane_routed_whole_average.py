"""N1 (Codex night review 2026-10-06, P1): a routed buy's hard copy-premium cap is judged against the
WHOLE receipt's average, on every pump venue.

9dce09194 lets a receipt resume a split route on its smaller venue leg. PumpSwap and the pump
curve still priced MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT from that venue event's own average, so a
route that paid $2,100 for twice the leg's coins (a cheaper second venue) signed a minimum that
allowed 157% over the source's real average. CPMM and LaunchLab already used the whole receipt
(routed_average_price_usd). Now PumpSwap and both curve builders do too: a routed buy's average is
USDC paid / coins acquired over the whole receipt, in the coin's own decimals; an unknown or
mismatched one refuses while the cap is on; the post-trade chase floor is unchanged.
"""
import dataclasses
from types import SimpleNamespace

import pytest

from sniper.execution import pump_curve_buy as pcb
from sniper.execution import pumpswap_buy as ps
from sniper.settings import memecoin as cfg
from test_slot_lane_price_cap import (BLOCKHASH, CAP, TICKET, _curve_builder, _curve_event,
                                      _ps_builder)

HARD = 35.0


@pytest.fixture(autouse=True)
def _caps(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", CAP)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT", HARD)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_EXEC_PREMIUM_BY_WALLET", {})


def _routed(spent_atomic, acquired_atomic, decimals=6):
    return SimpleNamespace(usdc_spent_atomic=int(spent_atomic), token_delta_atomic=int(acquired_atomic),
                           token_decimals=decimals)


# --- PumpSwap ----------------------------------------------------------------------------------

def _ps(routed=None):
    b, buy, event, facts = _ps_builder(6)
    snap = SimpleNamespace(blockhash=BLOCKHASH, last_valid_height=1, sol_usd=175.0)
    if routed is not None:
        buy = SimpleNamespace(**vars(buy), routed=routed)
    return b.build(buy, 5.0, snap), event


def _ps_leg_average():
    """The venue event's own average the builder used before N1, USD per whole token."""
    (message, validity), _event = _ps()
    assert message is not None, validity
    return validity["pump_price_limit"]["source_average_price_usd"]


def test_a_split_route_is_capped_at_its_whole_average_not_its_venue_leg():
    """Codex's shape: the leg here, twice its coins over the whole route for 5% more USDC. At the
    whole average +35% the ticket cannot be bought, so it refuses; before N1 it signed."""
    leg_avg = _ps_leg_average()
    acquired = 10 ** 15
    spent = int(leg_avg * 1.05 / 2 * acquired / 1e6 * 1e6)          # USDC atoms
    (message, why), _event = _ps(_routed(spent, acquired))
    assert message is None, "signed against the venue leg's average"
    assert "refuses" in why


def test_a_single_leg_route_signs_at_its_receipt_average():
    leg_avg = _ps_leg_average()
    acquired = 10 ** 15
    spent = int(leg_avg * acquired / 1e6 * 1e6)
    (message, validity), _event = _ps(_routed(spent, acquired))
    assert message is not None, validity
    limit = validity["pump_price_limit"]
    whole = spent / 1e6 / (acquired / 1e6)
    assert limit["source_average_price_usd"] == pytest.approx(whole, rel=1e-12)
    assert limit["min_tokens_atomic"] >= pcb.source_price_floor_atomic(pcb.clip_atomic(5.0), whole, HARD, 6)


@pytest.mark.parametrize("routed", [_routed(0, 10 ** 15), _routed(10 ** 6, 0), _routed(10 ** 6, 10 ** 15, 9)],
                         ids=["no_payment", "no_coins", "decimals_mismatch"])
def test_an_unknown_whole_average_refuses_while_the_cap_is_on(routed):
    (message, why), _event = _ps(routed)
    assert message is None and why == "source average price unavailable"


def test_with_the_hard_cap_off_the_whole_average_is_not_needed(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT", 0.0)
    (message, validity), _event = _ps(_routed(0, 10 ** 15))
    assert message is not None, validity


def test_native_sol_funding_takes_the_same_whole_average(monkeypatch):
    leg_avg = _ps_leg_average()
    b, buy, _event, _facts = _ps_builder(6)
    acquired = 10 ** 15
    buy = SimpleNamespace(**vars(buy), routed=_routed(int(leg_avg * 1.05 / 2 * acquired), acquired))
    snap = SimpleNamespace(blockhash=BLOCKHASH, last_valid_height=1, sol_usd=175.0)
    lamports = int(5.0 / 175.0 * 1e9)
    message, why = b.build(buy, 5.0, snap, funding_lamports=lamports)
    assert message is None and "refuses" in why


# --- the SOL curve ------------------------------------------------------------------------------

def _curve(routed=None, clip=175.0):
    b, buy = _curve_builder()
    if routed is not None:
        buy = SimpleNamespace(**vars(buy), routed=routed)
    return b.build(buy, clip, SimpleNamespace(blockhash=BLOCKHASH, last_valid_height=1, sol_usd=121.5))


def test_a_split_curve_route_is_capped_at_its_whole_average():
    message, validity = _curve()
    assert message is not None, validity
    leg_avg = validity["pump_price_limit"]["source_average_price_usd"]
    acquired = 10 ** 15
    message, why = _curve(_routed(int(leg_avg * 1.05 / 2 * acquired), acquired))
    assert message is None and "refuses" in why


def test_a_curve_route_with_unknown_decimals_refuses():
    message, why = _curve(_routed(10 ** 6, 10 ** 12, decimals=9))
    assert message is None and why == "source average price unavailable"


def test_a_curve_route_signs_at_its_receipt_average():
    message, validity = _curve()
    leg_avg = validity["pump_price_limit"]["source_average_price_usd"]
    acquired = 10 ** 15
    spent = int(leg_avg * acquired)
    message, validity = _curve(_routed(spent, acquired))
    assert message is not None, validity
    assert validity["pump_price_limit"]["source_average_price_usd"] == pytest.approx(
        spent / 1e6 / (acquired / 1e6), rel=1e-12)


# --- the quote-token curve ----------------------------------------------------------------------

def test_a_split_quote_curve_route_is_capped_at_its_whole_average(monkeypatch):
    import test_slot_lane_quote_curve as Q
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES", frozenset({"pump_curve"}), raising=False)
    b = Q._builder()
    buy = Q._slot_buy(Q.EXACT_IN)
    assert b.ondemand_quote(buy)[2] is None
    usd = 100.0 / (118_090 / 1e8)
    message, validity = b.build(buy, 100.0, Q.SNAP, leg=Q._leg(), quote=(8, usd, 0, 0))
    assert message is not None, validity
    leg_avg = validity["pump_price_limit"]["source_average_price_usd"]
    acquired = 10 ** 15
    split = dataclasses.replace(buy, routed=_routed(int(leg_avg * 1.05 / 2 * acquired), acquired))
    message, why = b.build(split, 100.0, Q.SNAP, leg=Q._leg(), quote=(8, usd, 0, 0))
    assert message is None and "refuses" in why
