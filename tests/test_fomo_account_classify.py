from sniper.analysis.fomo_account.classify import classify_swap, CASH_TOKENS

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL = "So11111111111111111111111111111111111111112"
MEME = "4U4U8oXwDyVXGeTffMXds4NAgBgLFwq3wNvTCRTSpump"
MEME2 = "GaZb3DE2U3Jcjx7ddAVwobsBKnCaDoJWLbzTvJYhpump"


def _swap(inm, outm, usd_in, usd_out, in_amt, out_amt):
    return {"inTokenAddress": inm, "outTokenAddress": outm,
            "humanUsdAmountIn": usd_in, "humanUsdAmountOut": usd_out,
            "inHumanAmount": in_amt, "outHumanAmount": out_amt}


def test_buy_is_cash_in_meme_out():
    k, mint, usd, qty = classify_swap(_swap(USDC, MEME, "500", "500", "500", "29663"))
    assert k == "buy" and mint == MEME and usd == 500.0 and qty == 29663.0


def test_sell_is_meme_in_cash_out():
    k, mint, usd, qty = classify_swap(_swap(MEME, USDC, "600", "600", "29663", "600"))
    assert k == "sell" and mint == MEME and usd == 600.0 and qty == 29663.0


def test_cash_route_both_legs_cash():
    k, mint, usd, qty = classify_swap(_swap(USDC, WSOL, "100", "100", "100", "0.5"))
    assert k == "cash_route" and mint is None


def test_token_route_neither_leg_cash():
    k, mint, usd, qty = classify_swap(_swap(MEME, MEME2, "100", "98", "1000", "2000"))
    assert k == "token_route" and mint is None


def test_malformed_numbers_default_zero():
    k, mint, usd, qty = classify_swap(_swap(USDC, MEME, None, None, None, None))
    assert k == "buy" and usd == 0.0 and qty == 0.0
