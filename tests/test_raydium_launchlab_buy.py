"""Raydium LaunchLab decoder and buy math against real source buys (venue census v2, 2026-09-27).

The fixture holds the 59 routed LaunchLab source buys of the census week: each buy's TradeEvent, its
live buy_exact_in accounts and arguments, and the coin's and quote's transfer-fee schedules; plus
the 45 pools' state, 31 global and 2 platform configs, all read on the research key. Every
assertion is on chain data, not on numbers typed by the author of the module.
"""
import base64
import json
from pathlib import Path

import pytest

from sniper.execution import raydium_launchlab_buy as LL

FX = json.loads((Path(__file__).parent / "fixtures" / "memecoin" / "raydium_launchlab_frames.json").read_text())
BUYS = FX["buys"]
EVENTS = [LL.parse_trade_event(base64.b64decode(b["event_b64"])) for b in BUYS]


def _pool(b):
    return LL.parse_pool_state(base64.b64decode(FX["pool_states_b64"][b["ix_accounts"][4]]))


def _rates(b):
    curve, trade, _quote = LL.parse_global_config(base64.b64decode(FX["global_configs_b64"][b["ix_accounts"][2]]))
    platform, creator = LL.parse_platform_config(base64.b64decode(FX["platform_configs_b64"][b["ix_accounts"][3]]))
    return LL.Rates(curve, trade, platform, creator)


def test_the_fixture_is_the_census_sample():
    assert len(BUYS) == 59 and len({b["ix_accounts"][4] for b in BUYS}) == 45
    assert all(e.is_buy and e.exact_in and e.pool_status == 0 for e in EVENTS)
    assert [e.pool for e in EVENTS] == [b["ix_accounts"][4] for b in BUYS]


def test_every_output_is_recomputed_to_the_unit_from_the_event_alone():
    assert [LL.event_output(e) for e in EVENTS] == [e.amount_out for e in EVENTS]


def test_the_fees_come_from_the_global_and_platform_configs():
    got = [LL.fees(e.amount_in, _rates(b).trade_fee_rate, _rates(b).platform_fee_rate)
           for e, b in zip(EVENTS, BUYS)]
    assert got == [(e.protocol_fee, e.platform_fee) for e in EVENTS]


def test_the_platform_fee_is_the_ceil_total_minus_the_protocol_fee_not_its_own_rounding():
    """Mutant check: rounding the platform fee on its own (ceil) misses 30 of the 59."""
    own = [-(-e.amount_in * _rates(b).platform_fee_rate // 1_000_000) for e, b in zip(EVENTS, BUYS)]
    assert sum(o != e.platform_fee for o, e in zip(own, EVENTS)) == 30


def test_our_buy_prices_like_the_program_from_the_sources_instruction_and_reserves():
    """expected_buy on the source's own amount_in, reserves before and quote fee returns its output;
    the reserves after are exactly the event's (no derivation, unlike CPMM)."""
    for e, b in zip(EVENTS, BUYS):
        gross, net = LL.expected_buy(
            b["ix_amount_in"], virtual_base=e.virtual_base, virtual_quote=e.virtual_quote,
            real_base=e.real_base_before, real_quote=e.real_quote_before,
            total_base_sell=e.total_base_sell, rates=_rates(b), in_fee=b["quote_fee"][:2],
            out_fee=b["base_fee"][:2])
        assert gross == e.amount_out
        assert net == gross - LL.transfer_fee(gross, *b["base_fee"][:2])
        assert b["ix_min_out"] <= net
        assert e.real_base_after - e.real_base_before == gross


def test_the_quote_transfer_fee_is_taken_before_the_program_sees_the_amount():
    fee_paying = [(e, b) for e, b in zip(EVENTS, BUYS) if b["quote_fee"][0]]
    assert len(fee_paying) == 11
    for e, b in fee_paying:
        assert e.amount_in == b["ix_amount_in"] - LL.transfer_fee(b["ix_amount_in"], *b["quote_fee"][:2])


def test_trade_events_are_read_off_the_invoke_stack_of_a_routed_frame():
    for sig, logs in FX["frame_logs"].items():
        evs = LL.trade_events(logs)
        want = next(e for e, b in zip(EVENTS, BUYS) if b["sig"] == sig)
        assert evs == [want]


def test_pool_state_offsets_match_the_live_instruction_accounts():
    for b in BUYS:
        p, a = _pool(b), b["ix_accounts"]
        assert (p.global_config, p.platform_config) == (a[2], a[3])
        assert (p.base_vault, p.quote_vault, p.base_mint, p.quote_mint) == (a[7], a[8], a[9], a[10])
        assert (p.base_program, p.quote_program) == (a[11], a[12])


def test_the_built_instruction_matches_a_live_one_account_for_account():
    """User token accounts differ (the live ones are the router's); everything else is the live key,
    including the three remaining accounts derived as PDAs."""
    for b in BUYS:
        p, a = _pool(b), b["ix_accounts"]
        ix = LL.buy_exact_in_instruction(user=a[0], pool_address=a[4], pool=p, amount_in=5, minimum_out=1)
        keys = [str(m.pubkey) for m in ix.accounts]
        assert len(keys) == 18
        assert [keys[i] for i in range(18) if i not in (5, 6)] == [a[i] for i in range(18) if i not in (5, 6)]
    assert bytes(ix.data) == LL.BUY_EXACT_IN_DISCRIMINATOR + (5).to_bytes(8, "little") + (1).to_bytes(8, "little") + bytes(8)


def test_the_authority_and_event_authority_are_the_program_pdas():
    from solders.pubkey import Pubkey
    program = Pubkey.from_string(LL.LAUNCHLAB_PROGRAM)
    assert str(Pubkey.find_program_address([b"vault_auth_seed"], program)[0]) == LL.LAUNCHLAB_AUTHORITY
    assert str(Pubkey.find_program_address([b"__event_authority"], program)[0]) == LL.EVENT_AUTHORITY


def test_every_census_config_is_the_rates_the_math_supports():
    assert {(r.curve_type, r.trade_fee_rate, r.platform_fee_rate, r.creator_fee_rate)
            for r in map(_rates, BUYS)} == {(0, 2500, 10000, 0)}


@pytest.mark.parametrize("change,message", [
    ({"curve_type": 2}, "constant-product"), ({"creator_fee_rate": 5000}, "creator fee"),
])
def test_an_unseen_rate_refuses_instead_of_guessing(change, message):
    e, b = EVENTS[0], BUYS[0]
    rates = LL.Rates(**{**_rates(b).__dict__, **change})
    with pytest.raises(ValueError, match=message):
        LL.expected_buy(1_000_000, virtual_base=e.virtual_base, virtual_quote=e.virtual_quote,
                        real_base=e.real_base_before, real_quote=e.real_quote_before,
                        total_base_sell=e.total_base_sell, rates=rates)


def test_a_buy_that_reaches_the_end_of_the_curve_refuses():
    e, b = EVENTS[0], BUYS[0]
    with pytest.raises(ValueError, match="end of the curve"):
        LL.expected_buy(10**18, virtual_base=e.virtual_base, virtual_quote=e.virtual_quote,
                        real_base=e.real_base_before, real_quote=e.real_quote_before,
                        total_base_sell=e.total_base_sell, rates=_rates(b))


@pytest.mark.parametrize("amount,floor", [(0, 1), (1, 0)])
def test_a_zero_amount_or_floor_refuses(amount, floor):
    with pytest.raises(ValueError):
        LL.buy_exact_in_data(amount, floor)
