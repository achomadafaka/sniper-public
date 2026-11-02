"""Chou: a passing quote must not sign a minimum allowing another 15% haircut."""
from copy import deepcopy

import pytest

from sniper.execution import solana_swap_executor as ex
from test_memecoin_entry_chase_quote_recheck import _c, _Deps


@pytest.mark.parametrize("venue", ["jupiter", "raydium"])
def test_chou_fallback_carries_the_source_cap_into_the_build(venue):
    c, pid = _c(source_price=6.974695382499303e-5)
    c.execute("UPDATE memecoin_pending_entries SET reserved_notional_usd=175 WHERE id=?", (pid,))
    c.commit()
    d = _Deps()
    quote = {"inAmount": "175000000", "outAmount": "1849836920495",
             "otherAmountThreshold": "1572361382421", "priceImpactPct": "0.0003",
             "slippageBps": 1500, "swapMode": "ExactIn"}
    if venue == "raydium":
        quote.update(_venue="raydium", _raydium_response={"success": True, "data": {
            "outputAmount": quote["outAmount"], "otherAmountThreshold": quote["otherAmountThreshold"],
            "slippageBps": 1500}})
    original = deepcopy(quote)
    signed = []
    result = ex.execute_entry(
        c, pending_id=pid, mint="MINT", usd_amount=175, strategy="alpha_b",
        source_user_id="u1", now_iso="2026-10-05T17:32:07+00:00",
        quote_fn=lambda *_: quote, reverse_quote_fn=lambda _, qty: {
            "inAmount": str(qty), "outAmount": "140246032", "otherAmountThreshold": "119209127"},
        usdc_fn=lambda: 1000, token_fn=lambda _: (0, 6),
        sign_fn=lambda q: signed.append(q), send_fn=d.send_fn, confirm_fn=d.confirm_fn,
        chase_max_pct=30, chase_reference_price=7.375893466399616e-5)
    assert result["status"] == "aborted"  # capture the build input, without signing or broadcasting
    assert len(signed) == 1
    built = signed[0]
    minimum = int(built["otherAmountThreshold"])
    assert 175 / (minimum / 1e6) <= 7.375893466399616e-5 * 1.3
    assert built["slippageBps"] < 1500
    assert int(built["outAmount"]) * (10_000 - built["slippageBps"]) // 10_000 >= minimum
    if venue == "raydium":
        assert built["_raydium_response"]["data"]["otherAmountThreshold"] == str(minimum)
        assert built["_raydium_response"]["data"]["slippageBps"] == built["slippageBps"]
    assert quote == original  # quote caches and vendor responses remain unchanged
    assert "send" not in d.calls
    c.close()


@pytest.mark.parametrize("decimals", [0, 6, 18])
def test_floor_uses_exact_atomic_units_and_never_weakens_existing_minimum(decimals):
    quote = {"inAmount": "10000000", "outAmount": str(100 * 10**decimals),
             "otherAmountThreshold": str(99 * 10**decimals), "slippageBps": 100}
    assert ex.cap_entry_quote_floor(quote, .1, 30, decimals) == quote
    weak = {**quote, "otherAmountThreshold": str(70 * 10**decimals), "slippageBps": 3000}
    built = ex.cap_entry_quote_floor(weak, .1, 30, decimals)
    assert 10 / (int(built["otherAmountThreshold"]) / 10**decimals) <= .13


def test_vendor_bps_cannot_undercut_its_reported_minimum():
    quote = {"inAmount": "10000000", "outAmount": "100000000",
             "otherAmountThreshold": "99000000", "slippageBps": 3000}
    built = ex.cap_entry_quote_floor(quote, .1, 30, 6)
    encoded_floor = int(built["outAmount"]) * (10000 - built["slippageBps"]) // 10000
    assert encoded_floor >= int(built["otherAmountThreshold"])
    assert encoded_floor >= int(quote["otherAmountThreshold"])


def test_hard_copy_ceiling_is_average_fill_even_with_post_trade_reference(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT', 35., raising=False)
    c, pid = _c(source_price=1.)
    d, signed = _Deps(), []
    result = ex.execute_entry(c, pending_id=pid, mint='MINT', usd_amount=10., strategy='alpha_b',
        source_user_id='u1', now_iso='2026-10-06T19:07:01+00:00',
        quote_fn=lambda *_: {'inAmount':'10000000','outAmount':'8000000',
            'otherAmountThreshold':'6800000','slippageBps':1500,'priceImpactPct':'0.001'},
        reverse_quote_fn=lambda _, amount: {'inAmount':str(amount),'outAmount':'9900000','otherAmountThreshold':'9900000'},
        usdc_fn=d.usdc_fn, token_fn=lambda _: (0., 6), sign_fn=lambda q: signed.append(q),
        send_fn=d.send_fn, confirm_fn=d.confirm_fn, chase_max_pct=100., chase_reference_price=1.3)
    assert result['status'] == 'aborted' and len(signed) == 1
    assert 10. / (int(signed[0]['otherAmountThreshold']) / 1e6) <= 1.35
    assert 'send' not in d.calls
    c.close()
