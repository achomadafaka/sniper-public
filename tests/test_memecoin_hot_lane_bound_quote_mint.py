"""The fast lane must decode a pool's OWN quote mint, like the pull lane does.

The pull lane was fixed on 2026-09-22 (`memecoin_solana_tape.py`, QQQ position 63509); the fast
lane was left on `vault_rows`' default `QUOTE_MINTS=(WSOL, USDC)`. `vault_rows` matches the quote
leg BY MINT, so a pool quoted in anything else finds no quote leg: no `sol_swap`, and a `sol_sync`
whose amount1 is None.

Measured 2026-09-23 on `data/memecoin_sol_tape.db`: 295 of 1,094 bound pools (27.0%) carry a quote
mint that is neither WSOL, USDC nor native-sol — including `Xs8S1uUs...`, which the pull lane's own
comment names as having held ZERO pool_ticks for its entire life.
"""
from __future__ import annotations

from sniper.services import memecoin_solana_amounts as sa
from sniper.services.memecoin_hot_supervisor import SolanaDecodeContext, build_decode_ctx_fn

EXOTIC = "Xs8S1uUs1zvS2p7iwtsG3b6fkhpvmwz4GYU3gWAmWHZ"   # a real bound quote mint, 4 pools
COIN = "CoinMint11111111111111111111111111111111111"
CV = "CoinVault1111111111111111111111111111111111"
QV = "QuoteVault111111111111111111111111111111111"
OWNER = "PoolAuthority11111111111111111111111111111"


def _tx(coin_delta, quote_mint, quote_delta):
    """A tx whose pool authority owns the coin vault and one quote vault of `quote_mint`."""
    keys = [{"pubkey": "RelayerBot1111111111111111111111111111111"}, {"pubkey": CV},
            {"pubkey": QV}]
    def bal(idx, mint, amt):
        return {"accountIndex": idx, "mint": mint, "owner": OWNER,
                "uiTokenAmount": {"amount": str(amt)}}
    # Coin INTO the pool (+) and quote OUT of it (-): opposite signs, which is a trade. Same-sign
    # legs are an LP add/remove and `vault_rows` labels them sol_mint/sol_burn instead.
    return {"transaction": {"message": {"accountKeys": keys}},
            "meta": {"preTokenBalances": [bal(1, COIN, 0), bal(2, quote_mint, quote_delta)],
                     "postTokenBalances": [bal(1, COIN, coin_delta), bal(2, quote_mint, 0)]}}


def test_an_exotic_quote_mint_is_invisible_to_the_static_pair():
    """The defect itself: the default pair finds no quote leg, so no swap row is produced."""
    tx = _tx(1000, EXOTIC, 2)
    rows = sa.vault_rows(tx, COIN, CV, slot=1, signature="sig")
    kinds = {r["kind"] for r in rows}
    assert "sol_swap" not in kinds, "the default pair must not match an exotic quote"
    sync = [r for r in rows if r["kind"] == "sol_sync"]
    assert sync and sync[0]["amount1"] is None, "and its sync carries no quote side"


def test_passing_the_bound_quote_mint_recovers_the_swap():
    tx = _tx(1000, EXOTIC, 2)
    rows = sa.vault_rows(tx, COIN, CV, slot=1, signature="sig", quote_mints=(EXOTIC,))
    swaps = [r for r in rows if r["kind"] == "sol_swap"]
    assert len(swaps) == 1
    assert swaps[0]["amount0"] == "1000"
    assert swaps[0]["amount1"] == "-2", "positive = INTO the pool, so coin in / quote out"


def test_the_decode_context_carries_the_bound_quote_mint():
    entries = {("solana", "P"): {"coin_vault": CV, "coin_mint": COIN, "quote_mint": EXOTIC}}
    resolve = build_decode_ctx_fn(entries, get_sigs_fn=lambda _v, _p: [], get_tx_fn=lambda _s: None)
    ctx = resolve(("solana", "P"))
    assert isinstance(ctx, SolanaDecodeContext)
    assert ctx.quote_mint == EXOTIC


def test_a_context_without_a_bound_mint_keeps_the_old_behaviour():
    """An absent binding must not claim a quote asset it never looked up."""
    entries = {("solana", "P"): {"coin_vault": CV, "coin_mint": COIN}}
    resolve = build_decode_ctx_fn(entries, get_sigs_fn=lambda _v, _p: [], get_tx_fn=lambda _s: None)
    assert resolve(("solana", "P")).quote_mint == ""


def test_native_sol_is_not_passed_through_as_a_mint():
    """A pump curve holds lamports, not an SPL vault. The pull lane decodes it with
    `pump_curve_rows`; passing 'native-sol' as a mint would match nothing, so the fast lane keeps
    the default for those pools and the gap stays NAMED."""
    tx = _tx(1000, sa.WSOL, 2)
    rows = sa.vault_rows(tx, COIN, CV, slot=1, signature="sig", quote_mints=(sa.NATIVE_SOL,))
    assert "sol_swap" not in {r["kind"] for r in rows}
    # ...whereas the default still decodes a genuinely WSOL-quoted pool.
    assert "sol_swap" in {r["kind"] for r in sa.vault_rows(tx, COIN, CV, slot=1, signature="s")}


def test_the_fast_lane_call_site_passes_a_quote_mint_at_all():
    """A source guard: the whole defect was a call site that omitted the argument."""
    import inspect
    from sniper.services import memecoin_hot_supervisor as hs
    src = inspect.getsource(hs)
    call = src[src.index("for r in vault_rows("):]
    assert "quote_mints=" in call[:200], "the fast lane must pass quote_mints to vault_rows"
