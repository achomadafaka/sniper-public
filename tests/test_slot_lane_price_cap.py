"""Slot-lane price cap: the signed minimum output is a UNIT-PRICE cap on the whole USDC ticket.

Before 2026-09-26 both slot-lane venues signed `expected * (1 - cap/100)`, a token haircut. At the
live cap of 30 that lets our unit price run 1/0.70 - 1 = 42.9% above even our own executable
estimate, and more above the source's price. The curve and PumpSwap builders now sign
max(output floor, price floor), where the price floor is the fewest tokens the ticket may buy at
source price x (1 + cap/100), rounded UP, in the base token's own decimals.

The curve rows are REAL mainnet frames (tests/fixtures/memecoin/slot_price_limit_a47a6c14.json):
send 4020's source buy and the 6042 failure it produced; the PumpSwap rows are the live frames in
tests/fixtures/pumpswap_buy_frames.json.
"""
import json
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from types import SimpleNamespace

import pytest

from sniper.execution import pump_curve_buy as pcb
from sniper.execution import pumpswap_buy as ps

FIX = Path(__file__).parent / "fixtures"
ROWS = json.loads((FIX / "memecoin" / "slot_price_limit_a47a6c14.json").read_text())
SOURCE = ROWS[0]
SPEND = 1_418_853_206                   # send 4020's lamports into the curve (the leg's minimum out)
SIGNED_MIN_4020 = 21_600_089_757_031    # what 4020 actually signed: int(expected * 0.70)
TICKET = 175_000_000                    # 4020's USDC ticket, atomic
SOURCE_PRICE = 5.320240846346843e-6     # 4020's stored source-price basis, USD per whole token
CAP = 30.0
BLOCKHASH = "11111111111111111111111111111111"


def _unit_price_within_cap(ticket_atomic, tokens_atomic, price, cap, decimals):
    unit = (Decimal(ticket_atomic) / 10 ** 6) / (Decimal(tokens_atomic) / 10 ** decimals)
    return unit <= Decimal(str(price)) * (1 + Decimal(str(cap)) / 100)


def _curve_event():
    return pcb.parse_trade_event(SOURCE["trade_event_log"])


def test_price_floor_rounds_up_to_the_fewest_tokens_inside_the_cap():
    exact = (Decimal(TICKET) / (Decimal(str(SOURCE_PRICE)) * Decimal("1.3")))
    floor = pcb.source_price_floor_atomic(TICKET, SOURCE_PRICE, CAP)
    assert floor == 25_302_498_233_294
    assert floor - 1 < exact <= floor                     # rounding never lowers the floor
    assert _unit_price_within_cap(TICKET, floor, SOURCE_PRICE, CAP, 6)
    assert not _unit_price_within_cap(TICKET, floor - 1, SOURCE_PRICE, CAP, 6)
    nine = pcb.source_price_floor_atomic(TICKET, SOURCE_PRICE, CAP, base_decimals=9)
    assert nine == int((exact * 1000).to_integral_value(rounding=ROUND_CEILING))
    for bad in ((0, SOURCE_PRICE, CAP), (TICKET, 0.0, CAP), (TICKET, float("nan"), CAP),
                (TICKET, SOURCE_PRICE, -1.0), (TICKET, None, CAP)):
        with pytest.raises(ValueError):
            pcb.source_price_floor_atomic(*bad)


def test_curve_signs_the_stronger_floor_and_the_old_haircut_broke_the_cap():
    event = _curve_event()
    ixs, amounts = pcb.curve_buy_instructions(
        event, SPEND, CAP, user=event["user"], token_program=pcb.TOKEN_PROGRAM,
        buyback_fee_recipient=event["fee_recipient"], source_price_usd=SOURCE_PRICE,
        source_cost_usdc_atomic=TICKET, source_price_cap_pct=CAP)
    assert amounts["output_floor_atomic"] == SIGNED_MIN_4020
    assert amounts["source_price_floor_atomic"] == 25_302_498_233_294
    assert amounts["min_tokens_out"] == 25_302_498_233_294
    assert bytes(ixs[1].data) == pcb.buy_exact_sol_in_data(SPEND, amounts["min_tokens_out"])
    assert _unit_price_within_cap(TICKET, amounts["min_tokens_out"], SOURCE_PRICE, CAP, 6)
    # The haircut 4020 actually signed permitted a unit price over source x 1.30.
    assert not _unit_price_within_cap(TICKET, SIGNED_MIN_4020, SOURCE_PRICE, CAP, 6)


def test_curve_refuses_before_signing_when_its_own_impact_breaks_the_cap():
    event = _curve_event()
    with pytest.raises(ValueError, match="own impact"):
        pcb.curve_buy_instructions(
            event, SPEND * 20, CAP, user=event["user"], token_program=pcb.TOKEN_PROGRAM,
            buyback_fee_recipient=event["fee_recipient"], source_price_usd=SOURCE_PRICE,
            source_cost_usdc_atomic=TICKET * 20, source_price_cap_pct=CAP)


class _Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def _curve_builder():
    event = _curve_event()
    b = pcb.SlotLaneCurveBuilder(user=event["user"], jup_call=lambda *a, **k: None,
                                 rpc=lambda *a: None, clock=_Clock())
    b._buyback = event["fee_recipient"]
    b._leg = {"ixs": [], "tables": [], "usdc_atomic": TICKET, "min_out_lamports": SPEND,
              "out_lamports": SPEND, "fetched_at": _Clock.t}
    buy = SimpleNamespace(mint=event["mint"], token_program=pcb.TOKEN_PROGRAM,
                          trade_event_log=SOURCE["trade_event_log"])
    return b, buy


def test_curve_builder_prices_from_the_event_and_publishes_the_limit(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", CAP)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT", 35.0)
    b, buy = _curve_builder()
    event = _curve_event()
    sol_usd = 121.5
    snap = SimpleNamespace(blockhash=BLOCKHASH, last_valid_height=1, sol_usd=sol_usd)
    message, validity = b.build(buy, 175.0, snap)
    assert message is not None
    limit = validity["pump_price_limit"]
    price = (event["virtual_sol_reserves"] / 1e9) / (event["virtual_token_reserves"] / 1e6) * sol_usd
    assert limit["source_price_usd"] == pytest.approx(price, rel=1e-12)
    assert limit["min_tokens_atomic"] == b.last_build["min_tokens_out"]
    assert limit["min_tokens_atomic"] == max(limit["output_floor_atomic"], limit["price_floor_atomic"])
    assert (limit["cap_pct"], limit["cost_usdc_atomic"], limit["base_decimals"]) == (CAP, TICKET, 6)
    assert limit["chase_cap_pct"] == CAP
    assert limit["source_average_cap_pct"] == 35.0
    assert _unit_price_within_cap(TICKET, limit["min_tokens_atomic"],
                                 limit["source_average_price_usd"], 35.0, 6)
    assert _unit_price_within_cap(TICKET, limit["min_tokens_atomic"], price, CAP, 6)


@pytest.mark.parametrize("sol_usd", [None, 0.0, float("nan")])
def test_curve_builder_refuses_without_a_usable_sol_price(monkeypatch, sol_usd):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", CAP)
    b, buy = _curve_builder()
    snap = SimpleNamespace(blockhash=BLOCKHASH, last_valid_height=1, sol_usd=sol_usd)
    assert b.build(buy, 175.0, snap) == (None, "source_price_unreadable")


# --- PumpSwap (RR-03) ----------------------------------------------------------------------------

PS = json.loads((FIX / "pumpswap_buy_frames.json").read_text())
PS_LIVE, PS_ACC = PS["direct_buy_exact_quote_in"], PS["direct_buy_exact_quote_in"]["accounts"]


def _ps_builder(decimals):
    facts = ps.parse_pool_account(PS["pool_account_b64"])
    facts.update(base_token_program=PS_ACC[11][0], quote_token_program=PS_ACC[12][0])
    if decimals is not None:
        facts["base_decimals"] = decimals
    b = ps.SlotLanePumpSwapBuilder(user=PS_ACC[1][0], jup_call=lambda *a, **k: None,
                                   rpc=lambda *a: None, clock=_Clock())
    b._buyback = PS_ACC[24][0]
    event = ps.parse_buy_event(PS_LIVE["buy_event_log"])
    b.pools._pools[event["pool"]] = facts
    b._leg = {"ixs": [], "tables": [], "usdc_atomic": pcb.clip_atomic(5.0),
              "min_out_lamports": 30_000_000, "out_lamports": 30_400_000, "fetched_at": _Clock.t}
    return b, SimpleNamespace(pool=event["pool"], trade_event_log=PS_LIVE["buy_event_log"], mint=None), event, facts


@pytest.mark.parametrize("decimals", [6, 9])
def test_pumpswap_caps_the_unit_price_in_the_pool_base_decimals(monkeypatch, decimals):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", CAP)
    b, buy, event, facts = _ps_builder(decimals)
    sol_usd = 175.0
    snap = SimpleNamespace(blockhash=BLOCKHASH, last_valid_height=1, sol_usd=sol_usd)
    message, validity = b.build(buy, 5.0, snap)
    assert message is not None, validity
    limit = validity["pump_price_limit"]
    base_post, quote_post = ps.post_trade_reserves(event)
    price = ((quote_post + facts["virtual_quote"]) / 1e9) / (base_post / 10 ** decimals) * sol_usd
    assert limit["source_price_usd"] == pytest.approx(price, rel=1e-12)
    assert limit["base_decimals"] == decimals
    assert limit["chase_cap_pct"] == CAP
    assert limit["source_average_cap_pct"] == cfg.copy_premium_cap(None, "solana")[0]
    assert limit["min_tokens_atomic"] == b.last_build["min_tokens_out"]
    assert limit["price_floor_atomic"] == pcb.source_price_floor_atomic(
        pcb.clip_atomic(5.0), price, CAP, base_decimals=decimals)
    assert _unit_price_within_cap(pcb.clip_atomic(5.0), limit["min_tokens_atomic"], price, CAP, decimals)
    # The haircut alone (the pre-2026-09-26 signed minimum) broke the cap on this live frame.
    assert not _unit_price_within_cap(pcb.clip_atomic(5.0), limit["output_floor_atomic"], price, CAP, decimals)


def test_pumpswap_refuses_when_the_base_decimals_are_unknown(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", CAP)
    b, buy, _event, _facts = _ps_builder(None)
    snap = SimpleNamespace(blockhash=BLOCKHASH, last_valid_height=1, sol_usd=175.0)
    assert b.build(buy, 5.0, snap) == (None, "base_decimals_unknown")


def test_pumpswap_refuses_before_signing_when_its_own_impact_breaks_the_cap():
    _b, _buy, event, facts = _ps_builder(6)
    base_post, quote_post = ps.post_trade_reserves(event)
    price = ((quote_post + facts["virtual_quote"]) / 1e9) / (base_post / 1e6) * 175.0
    with pytest.raises(ValueError, match="own impact"):
        ps.pumpswap_buy_instructions(
            event, facts, 30_000_000 * 4000, CAP, user=PS_ACC[1][0], buyback_fee_recipient=PS_ACC[24][0],
            source_price_usd=price, source_cost_usdc_atomic=pcb.clip_atomic(5.0) * 4000,
            source_price_cap_pct=CAP)


# --- per-wallet lift (2026-09-28, operator "widen the slot lane cap for top wallets") -------------
# DegenCapitalLLC BcoDT: the slot lane landed in the source slot behind 43 SOL of copiers, the price
# was 2.62x the source's, and min-out at the roster cap failed; the coin was +85% at 120 s. One knob,
# MEMECOIN_REAL_MAX_EXEC_PREMIUM_BY_WALLET, now lifts the J7 check AND the slot lane's signed floor.

def test_the_wallet_lift_is_a_lift_only(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_EXEC_PREMIUM_BY_WALLET", {"solana:degen": 3.0,
                                                                          "solana:tight": 1.1})
    assert cfg.solana_wallet_chase_cap_pct("DEGEN", 30.0) == 200.0
    assert cfg.solana_wallet_chase_cap_pct("tight", 30.0) == 30.0       # never lowers
    assert cfg.solana_wallet_chase_cap_pct("other", 30.0) == 30.0
    assert cfg.solana_wallet_chase_cap_pct(None, 30.0) == 30.0
    assert cfg.solana_wallet_chase_cap_pct("degen", 0.0) == 0.0          # never enables a cap that is off


def test_a_lifted_wallet_signs_a_wider_slot_lane_price_floor(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", CAP)
    snap = SimpleNamespace(blockhash=BLOCKHASH, last_valid_height=1, sol_usd=121.5)
    b, buy = _curve_builder()
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_EXEC_PREMIUM_BY_WALLET", {})
    buy.source_user_id = "degen"
    _msg, roster = b.build(buy, 175.0, snap)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_EXEC_PREMIUM_BY_WALLET", {"solana:degen": 3.0})
    _msg, lifted = b.build(buy, 175.0, snap)
    r, w = roster["pump_price_limit"], lifted["pump_price_limit"]
    assert (r["cap_pct"], w["cap_pct"]) == (CAP, 200.0)
    assert w["min_tokens_atomic"] < r["min_tokens_atomic"]
    assert w["min_tokens_atomic"] == w["price_floor_atomic"] > 0     # the 3x unit-price cap still binds
