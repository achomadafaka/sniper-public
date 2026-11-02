"""Exotic-quote pools bind and price when the quote token has a real USD market.

PEECOIN, 2026-08-14: the launchpad meta pairs memecoins against tokenized assets — its Raydium
CLMM pool quotes in GLDx (tokenized gold, ~$401, $350K liquidity). The resolver only accepted
WSOL/USDC, so the coin could never bind, the venue pricing never engaged, and the display fell
back to aggregator mids that showed +$3 on a close that realized -$20.54.

Fail-closed boundaries under test: a junk/junk pairing (validator false) stays unbound; no
validator means no dynamic pass; two candidate owners stay unbound; and an exotic mark prices
ONLY when quote_usd carries an independent USD price for its quote token.
"""
import pytest

from sniper.services.memecoin_solana_amounts import (
    QUOTE_MINTS, resolve_pool_accounts_dynamic)
from sniper.services.memecoin_solana_swap_marks import SolanaSwapMarkCache, PoolBinding

COIN = "CoinMint111111111111111111111111111111111111"
GLDX = "Xsv9hRk1z5ystj9MhnA7Lq4vjSsLwzL2nxrwmwtD3re"
PAYER = "Payer1111111111111111111111111111111111111"
POOL = "PoolOwner111111111111111111111111111111111"


def _tx(owner_mints):
    """A minimal tx whose postTokenBalances place each (owner, mint, post). account_states keys
    by accountIndex into accountKeys, so every balance entry needs its own key slot; index 0 is
    the fee payer by convention."""
    keys = [PAYER] + [f"TokenAcct{i}" for i in range(len(owner_mints))]
    post = [{"accountIndex": i + 1, "mint": mint, "owner": owner,
             "uiTokenAmount": {"amount": str(amount), "decimals": 6}}
            for i, (owner, mint, amount) in enumerate(owner_mints)]
    return {"transaction": {"message": {"accountKeys": keys}},
            "meta": {"preTokenBalances": [], "postTokenBalances": post}}


def test_gldx_shaped_pool_binds_when_the_quote_is_priceable():
    tx = _tx([(POOL, COIN, 1000), (POOL, GLDX, 500), (PAYER, COIN, 10)])
    out = resolve_pool_accounts_dynamic(tx, COIN, exclude_owner=PAYER,
                                        quote_ok_fn=lambda q: q == GLDX)
    assert out is not None
    owner, coin_vault, qmint, quote_vault = out
    assert owner == POOL and qmint == GLDX


def test_unpriceable_quote_stays_unbound():
    tx = _tx([(POOL, COIN, 1000), (POOL, GLDX, 500)])
    assert resolve_pool_accounts_dynamic(tx, COIN, exclude_owner=PAYER,
                                         quote_ok_fn=lambda q: False) is None


def test_no_validator_means_no_dynamic_pass():
    tx = _tx([(POOL, COIN, 1000), (POOL, GLDX, 500)])
    assert resolve_pool_accounts_dynamic(tx, COIN, exclude_owner=PAYER) is None


def test_two_candidate_owners_stay_unbound():
    tx = _tx([(POOL, COIN, 1000), (POOL, GLDX, 500),
              ("Other111", COIN, 900), ("Other111", GLDX, 400)])
    assert resolve_pool_accounts_dynamic(tx, COIN, exclude_owner=PAYER,
                                         quote_ok_fn=lambda q: True) is None


def test_standard_wsol_resolution_still_wins_first():
    wsol = next(iter(QUOTE_MINTS))
    tx = _tx([(POOL, COIN, 1000), (POOL, wsol, 500)])
    out = resolve_pool_accounts_dynamic(tx, COIN, exclude_owner=PAYER,
                                        quote_ok_fn=lambda q: pytest.fail("must not be asked"))
    assert out is not None and out[2] == wsol


def test_a_gldx_state_mark_prices_only_with_a_usd_reference():
    cache = SolanaSwapMarkCache()
    b = PoolBinding(mint=COIN, pool="P", coin_vault="cv", quote_vault="qv", quote_mint=GLDX)
    # A STATE mark needs a fresh swap at the same price to be admitted at all (2026-09-22): the
    # reserve ratio only prices a constant-product pool. This swap is that corroboration, at the
    # same 0.002 GLDX/coin, so what this test measures stays the USD reference for the quote.
    cache.record({"chain_id": "solana", "pool": "P", "kind": "sol_swap", "block_no": 4,
                  "amount0": "-1000000", "amount1": "2000", "decimals0": 6, "decimals1": 6},
                 b, now_ts=99.0, observed_at="swap")
    row = {"chain_id": "solana", "pool": "P", "kind": "sol_sync", "block_no": 5,
           "amount0": "1000000000", "amount1": "2000000", "decimals0": 6, "decimals1": 6,
           "tx_sig": None, "log_index": 0, "maker": None, "observed_at": None}
    assert cache.record(row, b, now_ts=100.0, observed_at="t")
    assert cache.fresh_price_map({}, now_ts=101.0, max_age_s=5.0) == {}, \
        "no USD reference for the quote -> no mark, never a guess"
    marks = cache.fresh_price_map({GLDX: 401.0}, now_ts=101.0, max_age_s=5.0)
    # spot = (2 GLDX / 1000 coin) = 0.002 GLDX/coin * $401 = $0.802
    assert marks[("solana", COIN)]["price"] == pytest.approx(0.802)
