"""`vault_rows` must pair a pool's coin delta with its BOUND quote vault ADDRESS when we store one.

WHY (2026-09-23, measured). The decoder finds the quote leg by matching `owner == the coin vault's
owner AND mint in quote_mints`. A pool whose quote vault sits under a DIFFERENT program-derived
address therefore finds no quote leg whichever mint is passed. Measured on
`data/memecoin_sol_tape.db`: 60 of 60 pools still emitting null-quote syncs after the 2026-09-22
quote-mint fix DO carry a bound `quote_vault` address in `sol_pool_vaults` — the pool's own SOL
side, stored and unused. The worst of them, `2QrpqAEsi7wcrynfpoDbFNZDrDGiMC5bQJEU2H1r9mxk`, is the
exact pool the pull lane's own comment names as having held zero ticks for its entire life.

The quiet-hours audit's wording was exact: pair with "that pool's own SOL side".
"""
from __future__ import annotations

from sniper.services import memecoin_solana_amounts as sa

COIN = "CoinMint11111111111111111111111111111111111"
CV = "CoinVault1111111111111111111111111111111111"
QV = "QuoteVault111111111111111111111111111111111"
OWNER_COIN = "PoolAuthorityA1111111111111111111111111111"
OWNER_QUOTE = "PoolAuthorityB1111111111111111111111111111"   # a DIFFERENT PDA
EXOTIC = "Xs8S1uUs1zvS2p7iwtsG3b6fkhpvmwz4GYU3gWAmWHZ"


def _tx(*, quote_owner, quote_mint=EXOTIC, coin_delta=1000, quote_delta=2):
    """Coin INTO the pool (+), quote OUT of it (-): opposite signs, which is a trade."""
    keys = [{"pubkey": "RelayerBot1111111111111111111111111111111"}, {"pubkey": CV},
            {"pubkey": QV}]

    def bal(idx, mint, owner, amt):
        return {"accountIndex": idx, "mint": mint, "owner": owner,
                "uiTokenAmount": {"amount": str(amt)}}

    return {"transaction": {"message": {"accountKeys": keys}},
            "meta": {"preTokenBalances": [bal(1, COIN, OWNER_COIN, 0),
                                          bal(2, quote_mint, quote_owner, quote_delta)],
                     "postTokenBalances": [bal(1, COIN, OWNER_COIN, coin_delta),
                                           bal(2, quote_mint, quote_owner, 0)]}}


def _kinds(rows):
    return {r["kind"] for r in rows}


# --- (1) the defect, and the fix ----------------------------------------------------------------

def test_a_quote_vault_under_a_different_pda_is_unpriceable_by_owner_match():
    """THE DEFECT. Passing the right mint does not help: the owner does not match."""
    tx = _tx(quote_owner=OWNER_QUOTE)
    rows = sa.vault_rows(tx, COIN, CV, slot=1, signature="sig", quote_mints=(EXOTIC,))
    assert "sol_swap" not in _kinds(rows)
    sync = [r for r in rows if r["kind"] == "sol_sync"]
    assert sync and sync[0]["amount1"] is None, "no quote leg found, so no quote side"


def test_the_bound_quote_vault_address_prices_it():
    """THE FIX. The stored address needs no owner match and no mint match."""
    tx = _tx(quote_owner=OWNER_QUOTE)
    rows = sa.vault_rows(tx, COIN, CV, slot=1, signature="sig", quote_vault=QV)
    swaps = [r for r in rows if r["kind"] == "sol_swap"]
    assert len(swaps) == 1
    assert swaps[0]["amount0"] == "1000"
    assert swaps[0]["amount1"] == "-2", "positive = INTO the pool"
    sync = [r for r in rows if r["kind"] == "sol_sync"]
    assert sync and sync[0]["amount1"] == "0", "the reserve side is read from the bound vault too"


def test_the_address_does_not_need_the_mint_in_quote_mints():
    """The whole point: an address binding cannot be defeated by an unknown quote asset."""
    tx = _tx(quote_owner=OWNER_QUOTE, quote_mint="SomeMintNobodyListed1111111111111111111111")
    rows = sa.vault_rows(tx, COIN, CV, slot=1, signature="sig", quote_vault=QV,
                         quote_mints=(sa.WSOL,))
    assert "sol_swap" in _kinds(rows)


# --- (2) no behaviour change where the old path already worked ----------------------------------

def test_a_same_owner_wsol_pool_is_byte_identical_with_and_without_the_binding():
    """The regression guard Alpha asked for: FULL row equality, not just the price."""
    tx = _tx(quote_owner=OWNER_COIN, quote_mint=sa.WSOL)
    old = sa.vault_rows(tx, COIN, CV, slot=7, signature="sigX", observed_at="2026-09-23T00:00:00Z")
    new = sa.vault_rows(tx, COIN, CV, slot=7, signature="sigX", observed_at="2026-09-23T00:00:00Z",
                        quote_vault=QV)
    assert old == new, "the binding must not change a pool the owner match already priced"
    assert "sol_swap" in _kinds(old)


def test_lp_and_one_sided_labels_are_unchanged_under_the_binding():
    """Same-sign legs stay sol_mint/sol_burn; the binding changes WHICH vault, not the sign rule."""
    add = _tx(quote_owner=OWNER_QUOTE, coin_delta=1000, quote_delta=-5)   # both INTO the pool
    rows = sa.vault_rows(add, COIN, CV, slot=1, signature="s", quote_vault=QV)
    assert "sol_mint" in _kinds(rows) and "sol_swap" not in _kinds(rows)


# --- (3) no binding stored -> today's path ------------------------------------------------------

def test_no_stored_quote_vault_takes_the_owner_and_mint_path():
    tx = _tx(quote_owner=OWNER_COIN, quote_mint=sa.WSOL)
    for absent in (None, ""):
        rows = sa.vault_rows(tx, COIN, CV, slot=1, signature="s", quote_vault=absent)
        assert "sol_swap" in _kinds(rows), "an absent binding must not break the old path"


def test_a_bound_vault_the_tx_never_touched_is_not_a_fabricated_zero():
    """If the bound vault is absent from the tx, there was no quote movement. Do NOT fall back to
    an owner match: that is how an unrelated vault gets adopted as the pool's quote side."""
    tx = _tx(quote_owner=OWNER_COIN, quote_mint=sa.WSOL)
    rows = sa.vault_rows(tx, COIN, CV, slot=1, signature="s",
                         quote_vault="UntouchedVault11111111111111111111111111111")
    assert "sol_swap" not in _kinds(rows)
    sync = [r for r in rows if r["kind"] == "sol_sync"]
    assert sync and sync[0]["amount1"] is None, "unknown quote side, never a zero"


# --- the plumbing: both lanes must actually pass the address --------------------------------------

def test_the_fast_lane_context_carries_the_bound_quote_vault():
    from sniper.services.memecoin_hot_supervisor import build_decode_ctx_fn
    entries = {("solana", "P"): {"coin_vault": CV, "coin_mint": COIN,
                                 "quote_mint": EXOTIC, "quote_vault": QV}}
    resolve = build_decode_ctx_fn(entries, get_sigs_fn=lambda _v, _p: [], get_tx_fn=lambda _s: None)
    ctx = resolve(("solana", "P"))
    assert ctx.quote_vault == QV and ctx.quote_mint == EXOTIC
    # An entry without the binding must not invent one.
    bare = {("solana", "Q"): {"coin_vault": CV, "coin_mint": COIN}}
    assert build_decode_ctx_fn(bare, get_sigs_fn=lambda _v, _p: [],
                               get_tx_fn=lambda _s: None)(("solana", "Q")).quote_vault == ""


def test_both_lane_call_sites_pass_quote_vault():
    """Source guards. The defect both times was an omitted argument at a call site."""
    import inspect
    from sniper.services import memecoin_hot_supervisor as hs
    from sniper.services import memecoin_solana_tape as tape
    for mod in (hs, tape):
        src = inspect.getsource(mod)
        call = src[src.index("vault_rows(tx,"):]
        assert "quote_vault=" in call[:400], f"{mod.__name__} must pass quote_vault"
