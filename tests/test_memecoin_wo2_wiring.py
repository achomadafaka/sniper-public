"""WO-2 wiring — activate corroboration in production.

`build_decode_ctx_fn` maps the hot book's per-pool vault identity (coin_mint, coin_vault) to a
SolanaDecodeContext backed by the read-RPC getSignaturesForAddress + getTransaction fns, so the
supervisor's _corroborate can actually decode. No read RPC (get_*_fn None) -> resolver is None ->
every vault-delta sale fails closed to the pull lane (safe).
"""
from sniper.services.memecoin_hot_supervisor import build_decode_ctx_fn, SolanaDecodeContext

_KEY = ("solana", "POOL")
_ENTRIES = {_KEY: {"position": {"id": 7}, "engine": object(),
                   "coin_vault": "CV", "coin_mint": "MINT"}}


def test_resolver_builds_context_per_key():
    fn = build_decode_ctx_fn(_ENTRIES, get_sigs_fn=lambda *_: [], get_tx_fn=lambda _s: {})
    ctx = fn(_KEY)
    assert isinstance(ctx, SolanaDecodeContext)
    assert ctx.coin_mint == "MINT" and ctx.coin_vault == "CV"
    assert callable(ctx.get_sigs_fn) and callable(ctx.get_tx_fn)


def test_resolver_none_without_read_rpc():
    # get_signatures_fn()/get_transaction_fn() return None when no read RPC is configured.
    assert build_decode_ctx_fn(_ENTRIES, get_sigs_fn=None, get_tx_fn=lambda _s: {}) is None
    assert build_decode_ctx_fn(_ENTRIES, get_sigs_fn=lambda *_: [], get_tx_fn=None) is None


def test_resolver_none_for_unknown_or_incomplete_key():
    fn = build_decode_ctx_fn(_ENTRIES, get_sigs_fn=lambda *_: [], get_tx_fn=lambda _s: {})
    assert fn(("solana", "UNKNOWN")) is None                 # not in the book
    partial = build_decode_ctx_fn(
        {_KEY: {"position": {}, "engine": object()}},        # no vault identity
        get_sigs_fn=lambda *_: [], get_tx_fn=lambda _s: {})
    assert partial(_KEY) is None
