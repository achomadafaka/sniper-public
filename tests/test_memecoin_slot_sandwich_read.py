"""The sandwich read's block verdict (scripts/memecoin_slot_sandwich_read.py, Alpha GO 2026-10-15)."""
import importlib.util
import os

import pytest

_PATH = os.path.join(os.path.dirname(__file__), "..", "scripts", "memecoin_slot_sandwich_read.py")
_spec = importlib.util.spec_from_file_location("memecoin_slot_sandwich_read", _PATH)
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)

MINT, POOL, US = "CoinMint", "PoolOwner", "UsCustody"


def _tx(sig, signer, delta=0, err=None):
    """One getBlock(accounts) entry: `signer` moves `delta` coin units against the pool."""
    pre, post = [], []
    if delta:
        pre = [{"mint": MINT, "owner": signer, "uiTokenAmount": {"amount": "1000"}},
               {"mint": MINT, "owner": POOL, "uiTokenAmount": {"amount": "100000"}}]
        post = [{"mint": MINT, "owner": signer, "uiTokenAmount": {"amount": str(1000 + delta)}},
                {"mint": MINT, "owner": POOL, "uiTokenAmount": {"amount": str(100000 - delta)}}]
    return {"transaction": {"signatures": [sig], "accountKeys": [{"pubkey": signer, "signer": True}]},
            "meta": {"err": err, "preTokenBalances": pre, "postTokenBalances": post}}


def test_a_buy_right_before_us_and_a_sell_right_after_by_one_trader_is_an_adjacent_bracket():
    block = [_tx("a", "Bot", 500), _tx("ours", US, 300), _tx("b", "Bot", -500)]
    v = R.classify_block(block, "ours", MINT)
    assert v["adjacent"] is True and v["any"] == ["Bot"] and v["index"] == 1


def test_another_trader_between_keeps_the_bracket_but_not_adjacent():
    block = [_tx("a", "Bot", 500), _tx("ours", US, 300), _tx("x", "Fan", 50), _tx("b", "Bot", -500)]
    v = R.classify_block(block, "ours", MINT)
    assert v["adjacent"] is False and v["any"] == ["Bot"]


@pytest.mark.parametrize("block", [
    [_tx("a", "Bot", 500), _tx("ours", US, 300), _tx("b", "Other", -500)],      # two traders
    [_tx("a", "Bot", -500), _tx("ours", US, 300), _tx("b", "Bot", 500)],        # sell then buy
    [_tx("a", "Bot", 500), _tx("ours", US, 300), _tx("b", "Bot", -500, err={"x": 1})],  # back leg failed
    [_tx("ours", US, 300), _tx("a", "Bot", 500), _tx("b", "Bot", -500)],        # both after us
])
def test_no_bracket_without_one_trader_buying_before_and_selling_after(block):
    v = R.classify_block(block, "ours", MINT)
    assert v["adjacent"] is False and v["any"] == []


def test_our_own_coin_change_in_the_block_is_what_we_acquired():
    block = [_tx("a", "Bot", 500), _tx("ours", US, 300), _tx("b", "Bot", -500)]
    assert R.classify_block(block, "ours", MINT)["acquired"] == 300


def test_our_signature_missing_from_the_block_is_unknown_never_clean():
    assert R.classify_block([_tx("a", "Bot", 500)], "ours", MINT) is None


@pytest.mark.parametrize("acquired, minimum, within", [
    (1010, 1000, True), (1011, 1000, False), (None, 1000, None), (1000, None, None)])
def test_near_limit_is_within_one_percent_of_the_signed_minimum(acquired, minimum, within):
    assert R.near_limit(acquired, minimum)[1] is within
