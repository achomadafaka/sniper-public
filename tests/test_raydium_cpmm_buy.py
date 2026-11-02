"""Raydium CPMM decoder and swap math against real source buys (venue census, 2026-09-27).

The fixture holds 83 CPMM swaps from 80 routed source buys, their 29 pools' state and their 5
configs, all read on the research key. Every assertion here is on chain data, not on numbers typed
by the author of the module.
"""
import base64
import json
from pathlib import Path

import pytest

from sniper.execution import raydium_cpmm_buy as C

FX = json.loads((Path(__file__).parent / "fixtures" / "memecoin" / "raydium_cpmm_frames.json").read_text())
EVENTS = [C.parse_swap_event(base64.b64decode(e["event_b64"])) for e in FX["events"]]


def _pool(ev):
    return C.parse_pool_state(base64.b64decode(FX["pool_states_b64"][ev.pool]))


def _config(pool):
    return C.AmmConfig(*FX["config_rates"][pool.amm_config])


def test_the_fixture_is_the_census_sample():
    assert len(EVENTS) == 83 and len({e.pool for e in EVENTS}) == 29


def test_every_output_is_recomputed_to_the_unit_from_the_event_alone():
    assert [C.event_output(e) for e in EVENTS] == [e.output_amount for e in EVENTS]


def test_the_fee_split_comes_from_the_pools_config_and_flag():
    got, want = [], []
    for e in EVENTS:
        pool = _pool(e)
        cfg = _config(pool)
        got.append(C.fee_split(e.input_amount, cfg.trade_fee_rate,
                               C.creator_rate_for(pool, cfg)))
        want.append((e.trade_fee, e.creator_fee))
    assert got == want


def test_a_disabled_creator_fee_is_charged_as_zero():
    off = [e for e in EVENTS if not _pool(e).enable_creator_fee]
    assert off and all(e.creator_fee == 0 for e in off)


def test_an_input_transfer_fee_is_already_out_of_the_events_input_amount():
    """The first live Token-2022-quoted CPMM buy (ANTHROPIC into AjxqTiS6, 2026-09-27; the census
    had none): input_amount is what ARRIVED. The sender paid input_amount + input_transfer_fee, the
    fee is ceil(3%) of that gross, the fees split on input_amount, and the curve on its net gives
    the output to the unit. Subtracting the transfer fee again (the old rule) does not."""
    ev = C.swap_events(FX["exotic_quote_frame"]["logMessages"])[0]
    gross = ev.input_amount + ev.input_transfer_fee
    assert C.transfer_fee(gross, 300, 2**64 - 1) == ev.input_transfer_fee
    assert C.fee_split(ev.input_amount, 2500, 10000) == (ev.trade_fee, ev.creator_fee)
    assert C.event_output(ev) == ev.output_amount
    old = ev.input_amount - ev.input_transfer_fee - ev.trade_fee - ev.creator_fee
    assert C.curve_out(old, ev.input_vault_before, ev.output_vault_before) != ev.output_amount
    # our own buy of the same gross, priced with the quote's fee, lands on the same output
    pool = C.PoolState(**{**C.parse_pool_state(base64.b64decode(next(iter(FX["pool_states_b64"].values())))).__dict__,
                          "creator_fee_on": 0, "enable_creator_fee": True})
    out, _ = C.expected_buy(gross, ev.input_vault_before, ev.output_vault_before, pool,
                            C.AmmConfig(2500, 0, 0, 10000), input_is_0=True, in_fee=(300, 2**64 - 1))
    assert out == ev.output_amount


def test_our_own_buy_prices_like_the_program_on_the_sources_reserves():
    """expected_buy on the source's own inputs and pre-trade reserves returns its actual output."""
    for e in EVENTS:
        pool = _pool(e)
        input_is_0 = e.input_mint == pool.mint_0
        if e.input_transfer_fee:
            continue                                   # our inputs (SOL, USDC) carry no transfer fee
        gross, _net = C.expected_buy(e.input_amount, e.input_vault_before, e.output_vault_before,
                                     pool, _config(pool), input_is_0=input_is_0)
        assert gross == e.output_amount


def test_the_coin_transfer_fee_is_the_ceil_rule():
    """Census coins charged 3% (56 swaps), 1% (16) or nothing (11). The rate is the mint's; here it
    is read back from the event, so what this pins is the ROUNDING: ceil, exact on every one."""
    seen = set()
    for e in EVENTS:
        if e.output_transfer_fee:
            bps = round(10_000 * e.output_transfer_fee / e.output_amount)
            seen.add(bps)
            assert C.transfer_fee(e.output_amount, bps, 2**64 - 1) == e.output_transfer_fee
    assert seen == {100, 300}


def test_router_events_with_the_same_name_are_not_read_as_cpmm():
    """16 census lines were 169- and 221-byte router SwapEvents carrying CPMM's discriminator."""
    frame = FX["frames"][0]
    raw_same_disc = [l for l in frame["logMessages"] if l.startswith("Program data: ")
                     and base64.b64decode(l[14:])[:8] == C.SWAP_EVENT_DISCRIMINATOR]
    evs = C.swap_events(frame["logMessages"])
    assert len(raw_same_disc) > len(evs) >= 1
    assert all(isinstance(e, C.CpmmSwap) for e in evs)


def test_the_invoke_stack_attribution_is_what_keeps_the_router_event_out():
    """Mutant check: a discriminator-only reader trips on the router's 169-byte event."""
    frame = FX["frames"][0]
    with pytest.raises(ValueError):
        for l in frame["logMessages"]:
            if l.startswith("Program data: "):
                raw = base64.b64decode(l[14:])
                if raw[:8] == C.SWAP_EVENT_DISCRIMINATOR:
                    C.parse_swap_event(raw)


def test_pool_state_offsets_match_the_live_instruction_accounts():
    for pool, acc in FX["pool_ix_accounts"].items():
        st = C.parse_pool_state(base64.b64decode(FX["pool_states_b64"][pool]))
        assert st.amm_config == acc[2]
        assert {st.vault_0, st.vault_1} == {acc[6], acc[7]}
        assert {st.mint_0, st.mint_1} == {acc[10], acc[11]}
        assert st.observation == acc[12]


def test_config_rates_parse_from_the_raw_account():
    for key, b64 in FX["config_raw_b64"].items():
        cfg = C.parse_amm_config(base64.b64decode(b64))
        assert [cfg.trade_fee_rate, cfg.protocol_fee_rate, cfg.fund_fee_rate,
                cfg.creator_fee_rate] == FX["config_rates"][key]


def test_the_authority_is_the_program_pda():
    from solders.pubkey import Pubkey
    pda = Pubkey.find_program_address([b"vault_and_lp_mint_auth_seed"], Pubkey.from_string(C.CPMM_PROGRAM))[0]
    assert str(pda) == C.CPMM_AUTHORITY


def test_the_built_instruction_matches_a_live_one():
    pool, acc = next(iter(FX["pool_ix_accounts"].items()))
    st = C.parse_pool_state(base64.b64decode(FX["pool_states_b64"][pool]))
    input_is_0 = acc[10] == st.mint_0
    ix = C.swap_base_input_instruction(user=acc[0], pool=pool, state=st, input_is_0=input_is_0,
                                       amount_in=1_000_000, minimum_out=5)
    keys = [str(m.pubkey) for m in ix.accounts]
    # the live frame's user token accounts belong to the router's authority, so only the rest is
    # compared by address; ours are the user's own ATAs
    assert [keys[i] for i in (0, 1, 2, 3, 6, 7, 8, 9, 10, 11, 12)] == [
        acc[i] for i in (0, 1, 2, 3, 6, 7, 8, 9, 10, 11, 12)]
    assert bytes(ix.data) == C.SWAP_BASE_INPUT_DISCRIMINATOR + (1_000_000).to_bytes(8, "little") + (5).to_bytes(8, "little")


@pytest.mark.parametrize("amount,floor", [(0, 1), (1, 0)])
def test_a_zero_amount_or_floor_refuses(amount, floor):
    with pytest.raises(ValueError):
        C.swap_base_input_data(amount, floor)


def test_an_output_side_creator_fee_refuses_instead_of_guessing():
    e = EVENTS[0]
    pool = _pool(e)
    flipped = C.PoolState(**{**pool.__dict__, "creator_fee_on": 2 if e.input_mint == pool.mint_0 else 1,
                             "enable_creator_fee": True})
    cfg = C.AmmConfig(2500, 120000, 40000, 10000)
    with pytest.raises(ValueError):
        C.expected_buy(1_000_000, e.input_vault_before, e.output_vault_before, flipped, cfg,
                       input_is_0=e.input_mint == pool.mint_0)
