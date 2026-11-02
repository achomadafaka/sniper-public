"""dump_forensics.trade_of: the trader is a SIGNER, priced from its OWN quote change.

Gamma's dip grade (2026-10-01) scored two dust "fills" at +104x and +197x. Both were the old pool path:
the "pool" was any owner whose coin and WSOL legs had opposite signs, and the price was that owner's
WSOL leg. 8nSQRjqW was a real multi-hop buy priced on an intermediate's dust WSOL leg (118x too low);
ANzrdoVo was a Meteora DAMM v2 liquidity deposit read as a buy by the pool authority, which never
signed. Fixtures: research Helius reads of both, and a clean pump-curve TradeEvent pinned unchanged.
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts" / "research"))
import dump_forensics as dfx                     # noqa: E402
from slippage_fail_grading import sol_trade      # noqa: E402

FIX = json.loads((REPO / "tests/fixtures/solana/trade_of_artifacts.json").read_text())
MINT = "Coin1111111111111111111111111111111111111111"
USDC, WSOL = dfx.USDC, dfx.WSOL


def test_a_multihop_buy_is_priced_from_the_buyers_own_sol():
    f = FIX["multihop_usdc_buy_8nSQ"]
    t = dfx.trade_of(f["tx"], f["mint"], 1.0)
    assert (t["trader"], t["side"], t["quote"], t["kind"]) == (
        "98hxiPfWj3HGPg8edg5e9csLaMgGsbX5R189359LxCbP", "buy", "SOL", "pool")
    # The route took in 9,915,000 lamports of WSOL (9GTj's pool and GpMZ's leg); the signer's native SOL
    # fell 11,685,006: that, the 104,206 fee, the 1,574,800 rent of its new Token-2022 coin account and
    # 91,006 of tips. 9,915,000 for 18,506.138461 tokens is 5.36e-7 SOL a token, 1.21x the dip limit
    # (4.42e-7): no dip either way.
    assert t["tokens"] == pytest.approx(18506.138461)
    assert t["sol"] == pytest.approx(0.009915, abs=1e-12)
    assert t["price"] == pytest.approx(0.009915 / 18506.138461)
    assert sol_trade(dfx, f["tx"], f["mint"]) == pytest.approx(t["price"])


def test_a_liquidity_deposit_is_not_a_trade():
    f = FIX["damm_v2_lp_deposit_ANzr"]
    assert dfx.trade_of(f["tx"], f["mint"], 1.0) is None
    assert sol_trade(dfx, f["tx"], f["mint"]) is None


def test_a_clean_pump_curve_trade_is_unchanged():
    f = FIX["pump_curve_sell_3zjQ"]
    t = dfx.trade_of(f["tx"], f["mint"], 1.0)
    assert {k: t[k] for k in ("trader", "side", "sol", "tokens", "price", "creator", "kind")} == {
        "trader": "sssssDdMNAWKingjpEojkTNdVuZrBe7FsJLaGtexe7d", "side": "sell", "sol": 1.127695175,
        "tokens": 11118198.443999, "price": 9.944577431200504e-08,
        "creator": "JCvB1Mg1KFH73D344yPo3ihUTpkdH5gdghJXPzeLAX4d", "kind": "curve"}
    assert t["quote"] == "SOL"


def test_our_own_curve_entries_whose_event_does_not_parse_read_as_our_buys():
    """The two funded pump-curve entries in funded_entry_venues.json raise 'truncated borsh payload' in
    parse_trade_event and fell through to the old pool path as SELLS by other accounts. These early
    entries paid USDC, so the trade is labelled USDC with the USDC paid, never a SOL figure."""
    venues = json.loads((REPO / "tests/fixtures/solana/funded_entry_venues.json").read_text())
    for key, paid in (("pump_curve_entry", 40.79), ("pump_curve_entry_2", None), ("pumpswap_entry", 41.78)):
        tx, mint = venues[key]["tx"], venues[key]["mint"]
        t = dfx.trade_of(tx, mint, 1.0)
        assert t["trader"] == dfx._signers(tx)[0] and t["side"] == "buy" and t["quote"] == "USDC", key
        if paid is not None:
            assert t["sol"] == pytest.approx(paid), key
        assert sol_trade(dfx, tx, mint) is None, key


# ---------------------------------------------------------------- synthetic transactions
def _tx(signers, others, lamports, tokens, fee=5000):
    """`lamports` = {key: (pre, post)}; `tokens` = [(key, owner, mint, pre or None, post or None, dec)]."""
    keys = list(signers) + list(others)
    pre_tb, post_tb = [], []
    for key, owner, mint, pre, post, dec in tokens:
        idx = keys.index(key)
        if pre is not None:
            pre_tb.append({"accountIndex": idx, "owner": owner, "mint": mint,
                           "uiTokenAmount": {"amount": str(pre), "decimals": dec}})
        if post is not None:
            post_tb.append({"accountIndex": idx, "owner": owner, "mint": mint,
                            "uiTokenAmount": {"amount": str(post), "decimals": dec}})
    return {"transaction": {"message": {"accountKeys": keys, "header": {"numRequiredSignatures": len(signers)}},
                            "signatures": ["sig"]},
            "meta": {"err": None, "fee": fee, "logMessages": [],
                     "preBalances": [lamports.get(k, (0, 0))[0] for k in keys],
                     "postBalances": [lamports.get(k, (0, 0))[1] for k in keys],
                     "preTokenBalances": pre_tb, "postTokenBalances": post_tb}}


def test_a_usdc_paid_buy_is_labelled_usdc_and_never_reaches_a_sol_only_caller():
    tx = _tx(["B"], ["Bcoin", "Busdc", "P", "Pcoin", "Pusdc"],
             {"B": (10 ** 9, 10 ** 9 - 5000 - 3000)},           # fee and a 3,000-lamport tip in SOL
             [("Bcoin", "B", MINT, 0, 100 * 10 ** 6, 6), ("Busdc", "B", USDC, 50 * 10 ** 6, 30 * 10 ** 6, 6),
              ("Pcoin", "P", MINT, 10 ** 12, 10 ** 12 - 100 * 10 ** 6, 6),
              ("Pusdc", "P", USDC, 10 ** 9, 10 ** 9 + 20 * 10 ** 6, 6)])
    t = dfx.trade_of(tx, MINT, 1.0)
    assert (t["trader"], t["side"], t["quote"], t["sol"], t["tokens"]) == ("B", "buy", "USDC", 20.0, 100.0)
    assert sol_trade(dfx, tx, MINT) is None, "a USDC amount is never read as SOL"


USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"


def test_a_usdt_paid_sell_is_priced_in_usdt():
    """Dip set 10-01 (aoYvxp12, 1nXwxM4L): the signer sold the coin for USDT through a WSOL pool whose
    legs net to zero across the other accounts, so the fix returned None on a real sale."""
    tx = _tx(["S"], ["Scoin", "Susdt", "P", "Pcoin", "Pusdt"],
             {"S": (10 ** 9, 10 ** 9 - 5000)},
             [("Scoin", "S", MINT, 100 * 10 ** 6, 0, 6), ("Susdt", "S", USDT, 0, 19189932, 6),
              ("Pcoin", "P", MINT, 10 ** 12, 10 ** 12 + 100 * 10 ** 6, 6),
              ("Pusdt", "P", USDT, 10 ** 9, 10 ** 9 - 19189932, 6)])
    t = dfx.trade_of(tx, MINT, 1.0)
    assert (t["trader"], t["side"], t["quote"], t["tokens"]) == ("S", "sell", "USDT", 100.0)
    assert t["sol"] == pytest.approx(19.189932)
    assert sol_trade(dfx, tx, MINT) is None, "a USDT amount is never read as SOL"


def test_two_signers_moving_the_coin_are_ambiguous():
    tx = _tx(["A", "B"], ["Acoin", "Bcoin"], {"A": (10 ** 9, 9 * 10 ** 8), "B": (10 ** 9, 10 ** 9)},
             [("Acoin", "A", MINT, 0, 5 * 10 ** 6, 6), ("Bcoin", "B", MINT, 9 * 10 ** 6, 4 * 10 ** 6, 6)])
    assert dfx.trade_of(tx, MINT, 1.0) is None


def test_a_coin_move_by_a_non_signer_alone_is_not_a_trade():
    tx = _tx(["S"], ["Vault", "Vcoin"], {"S": (10 ** 9, 10 ** 9 - 5000)},
             [("Vcoin", "Vault", MINT, 0, 10 ** 6, 6)])
    assert dfx.trade_of(tx, MINT, 1.0) is None


def test_the_buyers_own_new_token_account_rent_is_not_its_price():
    """A first buy opens the buyer's coin account (2,039,280 lamports of rent): 0.1 SOL to the pool, not
    0.10204."""
    rent = 2_039_280
    tx = _tx(["B"], ["Bcoin", "P", "Pcoin"], {"B": (10 ** 9, 10 ** 9 - 10 ** 8 - rent - 5000), "Bcoin": (0, rent)},
             [("Bcoin", "B", MINT, None, 1000 * 10 ** 6, 6), ("Pcoin", "P", MINT, 10 ** 12, 10 ** 12 - 1000 * 10 ** 6, 6)])
    t = dfx.trade_of(tx, MINT, 1.0)
    assert (t["side"], t["quote"], t["sol"]) == ("buy", "SOL", pytest.approx(0.1))


def test_the_sellers_closed_token_account_refund_is_not_its_proceeds():
    rent = 2_039_280
    tx = _tx(["S"], ["Scoin", "P", "Pcoin"], {"S": (10 ** 9, 10 ** 9 + 2 * 10 ** 8 + rent - 5000), "Scoin": (rent, 0)},
             [("Scoin", "S", MINT, 500 * 10 ** 6, None, 6), ("Pcoin", "P", MINT, 10 ** 12, 10 ** 12 + 500 * 10 ** 6, 6)])
    t = dfx.trade_of(tx, MINT, 1.0)
    assert (t["side"], t["quote"], t["sol"]) == ("sell", "SOL", pytest.approx(0.2))


def test_a_wsol_account_kept_open_moves_its_rent_not_its_price():
    """The buyer pays from an existing WSOL account: the WSOL token leg is the payment."""
    tx = _tx(["B"], ["Bwsol", "Bcoin", "P"], {"B": (10 ** 9, 10 ** 9 - 5000), "Bwsol": (10 ** 9, 7 * 10 ** 8)},
             [("Bwsol", "B", WSOL, 10 ** 9 - 2_039_280, 7 * 10 ** 8 - 2_039_280, 9), ("Bcoin", "B", MINT, 0, 3 * 10 ** 6, 6)])
    t = dfx.trade_of(tx, MINT, 1.0)
    assert (t["side"], t["quote"], t["sol"], t["tokens"]) == ("buy", "SOL", pytest.approx(0.3), 3.0)


def test_a_tip_beside_a_wsol_pool_swap_is_not_its_price():
    """63LC6ePL (dip set): a 0.003 SOL sale paid a 0.002 SOL tip; the trade is what the pool paid."""
    tx = _tx(["S"], ["Scoin", "P", "Pcoin", "Pwsol", "Tip"],
             {"S": (10 ** 9, 10 ** 9 + 3_000_000 - 2_000_000 - 5000), "Tip": (10 ** 9, 10 ** 9 + 2_000_000)},
             [("Scoin", "S", MINT, 100 * 10 ** 6, 0, 6), ("Pcoin", "P", MINT, 10 ** 12, 10 ** 12 + 100 * 10 ** 6, 6),
              ("Pwsol", "P", WSOL, 10 ** 12, 10 ** 12 - 3_000_000, 9)])
    t = dfx.trade_of(tx, MINT, 1.0)
    assert (t["side"], t["quote"], t["sol"]) == ("sell", "SOL", pytest.approx(0.003))
