"""SOL-1 (Codex review 2026-10-06): the hard copy-premium cap on the CPMM and LaunchLab slot lanes.

MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT caps our unit price over the SOURCE'S AVERAGE fill (USDC paid /
coins received). The chase cap is a separate floor over the source's POST-trade marginal price. On
a high-impact source buy the post-trade price is far above the average, so 30% over it can sit 41%
over the average: the builders signed it because they never passed the average. They now pass the
routed receipt's own atoms; an unknown average with the cap on refuses before signing.

Reserves are synthetic but coherent: the source's output is computed with each venue's own fee and
curve functions, as notes/solana_repro.py in the review did.
"""
import dataclasses

import pytest

from sniper.execution import pump_curve_buy as P
from sniper.execution import raydium_cpmm_buy as C
from sniper.execution import raydium_launchlab_buy as LL
from sniper.services import memecoin_slot_lane as L
from sniper.services import memecoin_slot_signal as S
from sniper.settings import memecoin as cfg

import test_slot_lane_cpmm as TC
import test_slot_lane_launchlab as TL

PRE_QUOTE, PRE_BASE, COPY_USD = 10_000 * 10**6, 1_000_000_000 * 10**6, 175.0


@pytest.fixture(autouse=True)
def _caps(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT", 35.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 30.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_EXEC_PREMIUM_BY_WALLET", {})


def _routed(ev, mint, spent, delta, decimals=6):
    return S.RoutedBuy(source_user_id="u1", handle="alice", owner="Owner1111", signature="SIG",
                       slot=100, mint=mint, token_delta_atomic=delta, token_decimals=decimals,
                       usdc_spent_atomic=spent, recv_monotonic=1.0, recv_utc="t",
                       paired_monotonic=1.0, venue_events=(ev,))


def _cpmm(source_in, **routed):
    ev = TC.USDC_EV
    cache = TC._cache(ev)
    state, rates = cache.get(ev.pool)["state"], cache.get(ev.pool)["config"]
    trade, creator = C.fee_split(source_in, rates.trade_fee_rate, C.creator_rate_for(state, rates))
    out = C.curve_out(source_in - trade - creator, PRE_QUOTE, PRE_BASE)
    ev = dataclasses.replace(ev, input_vault_before=PRE_QUOTE, output_vault_before=PRE_BASE,
                             input_amount=source_in, output_amount=out, input_transfer_fee=0,
                             output_transfer_fee=0, trade_fee=trade, creator_fee=creator)
    cache._facts[ev.pool]["mints"][ev.output_mint] = (PRE_BASE, 6, 0, 0)
    args = {"spent": source_in, "delta": out, **routed}
    buy = L.cpmm_slot_buy(_routed(ev, ev.output_mint, **args))
    builder = C.SlotLaneCpmmBuilder(user=TL.USER, rpc=None, jup_call=None, leg_fn=None,
                                    clock=lambda: 50.0)
    builder.pools = cache
    return builder.build(buy, COPY_USD, TC._snap()), source_in / 1e6 / (out / 1e6)


def _launchlab(source_in, **routed):
    row = TL.USDC_B
    cache = TL._cache(row)
    ev = TL._ev(row)
    rates = cache.get(ev.pool)["rates"]
    protocol, platform = LL.fees(source_in, rates.trade_fee_rate, rates.platform_fee_rate)
    out = LL.curve_out(source_in - protocol - platform, PRE_BASE, PRE_QUOTE, 0, 0)
    ev = dataclasses.replace(ev, total_base_sell=900_000_000 * 10**6, virtual_base=PRE_BASE,
                             virtual_quote=PRE_QUOTE, real_base_before=0, real_quote_before=0,
                             real_base_after=out, real_quote_after=source_in - protocol - platform,
                             amount_in=source_in, amount_out=out, protocol_fee=protocol,
                             platform_fee=platform, creator_fee=0, share_fee=0)
    cache._facts[ev.pool]["coin"] = (PRE_BASE, 6, 0, 0)
    mint = row["ix_accounts"][9]
    args = {"spent": source_in, "delta": out, **routed}
    buy = L.launchlab_slot_buy(_routed(ev, mint, **args), cache)
    builder = LL.SlotLaneLaunchLabBuilder(user=TL.USER, rpc=None, leg_for=None, clock=lambda: 50.0)
    builder.pools = cache
    return builder.build(buy, COPY_USD, TL._snap()), source_in / 1e6 / (out / 1e6)


VENUES = [pytest.param(_cpmm, id="cpmm"), pytest.param(_launchlab, id="launchlab")]


@pytest.mark.parametrize("venue", VENUES)
def test_high_impact_source_refuses_over_the_average_cap(venue):
    """A $4,000 source buy into $10,000 of quote: 30% over its post-trade price is ~41% over its
    average. The review's builders signed it; the hard cap now refuses before signing."""
    (message, detail), _average = venue(4_000 * 10**6)
    assert message is None, "signed a copy over the hard average cap: %r" % (detail,)
    assert "refuses" in detail


@pytest.mark.parametrize("venue", VENUES)
def test_low_impact_source_signs_at_least_the_average_floor(venue):
    (message, validity), average = venue(40 * 10**6)
    assert message is not None, validity
    floor = P.source_price_floor_atomic(int(COPY_USD * 1e6), average, 35.0, 6)
    assert validity["min_tokens_out"] >= floor
    limit = validity["pump_price_limit"]
    assert limit["source_average_price_usd"] == pytest.approx(average, rel=1e-12)


@pytest.mark.parametrize("venue", VENUES)
@pytest.mark.parametrize("routed", [dict(spent=0), dict(delta=0), dict(decimals=9)],
                         ids=["no_payment", "no_coins", "decimals_mismatch"])
def test_unknown_average_with_the_cap_on_refuses(venue, routed):
    (message, reason), _average = venue(40 * 10**6, **routed)
    assert message is None and reason == "source average price unavailable"


@pytest.mark.parametrize("venue", VENUES)
def test_cap_off_ignores_the_average(venue, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT", 0.0)
    (message, validity), _average = venue(40 * 10**6, spent=0)
    assert message is not None, validity


def test_shared_cap_refuses_a_missing_average():
    with pytest.raises(ValueError, match="source average price unavailable"):
        P.capped_min_out(10**14, 30.0, source_price_usd=1e-5, source_cost_usdc_atomic=175_000_000,
                         source_price_cap_pct=30.0, source_average_price_usd=None)
    with pytest.raises(TypeError):
        P.capped_min_out(10**14, 30.0, source_price_usd=1e-5, source_cost_usdc_atomic=175_000_000,
                         source_price_cap_pct=30.0)
