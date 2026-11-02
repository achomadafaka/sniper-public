"""A V2 exit is VALUED on the whole bag less a realistic tax, not on the 15% max-tax cut (2026-09-27).

dex_v2_exit_quote cuts the sold amount by MEMECOIN_EVM_MAX_TAX_PCT (15%) before getAmountsOut, so
its minBuyAmount survives any tax up to 15%. The exit decision priced the bag on that same cut
buyAmount: 14 V2-quoted BSC exits (09-20..09-27) filled +6..+26% above their decision quote,
median +15.6%, while the real sell taxes read from their receipts were 1.0-5.0% (median 1.0%).
b_stop fired at a cut -35..-40% while the true loss was -23..-32%.

The fixture is position 65763's own exit, read from BSC: the pair's reserves are rebuilt from the
Sync log of tx 0xd07e7b83..., and the model below reproduces what the pair paid to the wei.
"""
import pytest

from sniper.execution import evm_v2_exit_quote as Q

PANCAKE_V2 = "0x10ed43c718714eb63d5aa57b78b54704e256024e"
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
BNPAID = "0x11dc0fd5e2c9a4407fdb15aa6871501ba9307777"
TAKER = "0x898be9c27f8b391c47a3d9f2da17781aa8aa3a1d"

BAG = 231780634320291585587262            # tokens we sent (input_atomic of every decision quote)
PAIR_IN = 228303924805487211803454        # what reached the pair: 1.5% transfer tax taken
WBNB_OUT = 85593627783129669              # what the pair paid us (Transfer log, receipt)
R_TOKEN = int("4f78ed86086dd5a81cce18", 16) - PAIR_IN      # reserves BEFORE our swap (Sync after)
R_WBNB = int("01f3f0aca9b4f1d7b9", 16) + WBNB_OUT
CUT_QUOTE = 74183593847833363             # the decision quote that fired the stop (route series)


def _pancake_out(amount_in):
    with_fee = amount_in * 9975
    return with_fee * R_WBNB // (R_TOKEN * 10_000 + with_fee)


def _amounts_result(amounts):
    return ("0x" + format(32, "064x") + format(len(amounts), "064x")
            + "".join(format(x, "064x") for x in amounts))


def _reserves_call(seen=None):
    def call_fn(to, data):
        assert to.lower() == PANCAKE_V2
        assert data[2:10] == Q.GET_AMOUNTS_OUT
        amount_in = int(data[10:74], 16)
        if seen is not None:
            seen.append(amount_in)
        return _amounts_result([amount_in, _pancake_out(amount_in)])
    return call_fn


def _cut_quote(call_fn):
    return Q.dex_v2_exit_quote(
        "56", BNPAID, WBNB, BAG, TAKER, slippage_bps=1_000, cfg_router=PANCAKE_V2,
        connector_tokens=(), tax_buffer_bps=1_500, call_fn=call_fn, now_fn=lambda: 1_000,
        pair_tokens_fn=lambda *_a: ())


def test_the_reserve_model_reproduces_the_chain_to_the_wei():
    """Negative control: without this the fixture could agree with any code."""
    assert _pancake_out(PAIR_IN) == WBNB_OUT


def test_65763_value_is_the_whole_bag_less_the_real_tax_and_min_out_is_unchanged():
    quote = _cut_quote(_reserves_call())
    cut_out = _pancake_out(BAG * 8_500 // 10_000)
    assert int(quote["buyAmount"]) == cut_out                     # today's cut, 0.0739 WBNB
    assert abs(cut_out - CUT_QUOTE) / CUT_QUOTE < 0.01            # ~ the quote that fired the stop
    assert int(quote["minBuyAmount"]) == cut_out * 9_000 // 10_000

    value = Q.exit_value_amount(quote, value_tax_bps=150, call_fn=_reserves_call())
    assert abs(value - WBNB_OUT) <= 1                             # the pair's real payout
    assert int(quote["minBuyAmount"]) == cut_out * 9_000 // 10_000   # the floor is not touched


def test_the_default_allowance_values_65763_within_its_real_tax(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_EXIT_VALUE_TAX_PCT", 4.0)
    seen = []
    quote = _cut_quote(_reserves_call())
    value = Q.exit_value_amount(quote, call_fn=_reserves_call(seen))
    assert seen == [BAG * 9_600 // 10_000]
    # 4% allowance vs the real 1.5%: at most 2.5% low, never above the chain's payout.
    assert 0.97 * WBNB_OUT < value < WBNB_OUT


def test_rollback_knob_at_the_cut_reads_nothing(monkeypatch):
    seen = []
    quote = _cut_quote(_reserves_call())
    assert Q.exit_value_amount(quote, value_tax_bps=1_500, call_fn=_reserves_call(seen)) is None
    assert seen == []


def test_an_unreadable_value_read_keeps_the_cut():
    quote = _cut_quote(_reserves_call())

    def down(_to, _data):
        raise Q.V2ReadUnavailable("timeout")
    assert Q.exit_value_amount(quote, value_tax_bps=400, call_fn=down) is None


@pytest.mark.parametrize("quote", [
    {"buyAmount": "10", "sellAmount": "100", "route": {"protocol": "uniswap_v3"}},
    {"buyAmount": "10", "sellAmount": "100", "route": {"protocol": "uniswap_v2"}},   # no marker
    None,
])
def test_a_quote_not_built_by_the_v2_exit_rung_is_left_alone(quote):
    def never(_to, _data):
        raise AssertionError("no read for a quote this module did not build")
    assert Q.exit_value_amount(quote, value_tax_bps=400, call_fn=never) is None


def test_the_exit_preview_is_valued_on_the_whole_bag(monkeypatch):
    """Through the real exit ladder: the preview (what every percent rail decides on) carries the
    value, the signed calldata keeps the cut floor, and the evidence names the basis."""
    from eth_account import Account
    from sniper.execution import evm_dex_quote, evm_real_seams as S, evm_wallet
    from sniper.settings import memecoin as cfg

    monkeypatch.setenv("MEMECOIN_EVM_EXEC_VENUE_4663", "direct")
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_EXIT_VALUE_TAX_PCT", 1.5)
    monkeypatch.setattr(evm_wallet, "erc20_balance_raw", lambda *_a, **_k: BAG)
    monkeypatch.setattr(S, "quote_usd_price", lambda *_a, **_k: 770.79)
    monkeypatch.setattr(evm_dex_quote, "dex_v3_quote", lambda *_a, **_k: None)
    real_v2 = Q.dex_v2_exit_quote
    monkeypatch.setattr(Q, "dex_v2_exit_quote", lambda chain, sell, buy, amount, taker, **kw: real_v2(
        chain, sell, buy, amount, taker, slippage_bps=kw.get("slippage_bps"),
        cfg_router=PANCAKE_V2, connector_tokens=(), tax_buffer_bps=kw.get("tax_buffer_bps"),
        call_fn=_reserves_call(), pair_tokens_fn=lambda *_a: ()))
    monkeypatch.setattr(Q, "_make_call_fn", lambda chain, http: _reserves_call())
    ex = S.build_real_evm_executor(
        acct=Account.create(), chain_id="4663", rpc_url="https://rpc",
        http=lambda *_a, **_k: (200, {}, None), usdc_fn=lambda: 0.0, token_fn=lambda _m: 0.0)

    preview = ex["preview_exit_fn"]({"mint": BNPAID})
    signed = preview.preview.quote                                 # the quote rung 0 reuses

    assert abs(int(signed["buyAmount"]) - WBNB_OUT) <= 1
    assert preview.expected_usdc == pytest.approx(WBNB_OUT / 1e18 * 770.79, rel=1e-9)
    cut_out = _pancake_out(BAG * 8_500 // 10_000)
    assert signed["_cut_buyAmount"] == str(cut_out)
    assert int(signed["minBuyAmount"]) < cut_out           # floor still from the 15% cut
    assert preview.quote_evidence["value_basis"] == "whole_bag_less_allowance"
    assert abs(int(preview.quote_evidence["expected_output_atomic"]) - WBNB_OUT) <= 1
