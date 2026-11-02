"""Solana swap amounts from token-balance deltas — program-agnostic, fail-closed, read-only."""
from sniper.services import memecoin_solana_amounts as sa

COIN = "CoinMint111111111111111111111111111111111111"
TRADER = "Trader11111111111111111111111111111111111111"
POOL = "PoolVault111111111111111111111111111111111111"


def _tx(balances_pre, balances_post, payer=TRADER):
    """Pool-shaped by default: the POOL owner holds both the coin and the quote, which is how a real
    swap looks. An earlier trader-shaped fixture passed while live mainnet decoded 0/5 — the fee
    payer owns none of the token accounts in a real routed swap."""
    def bal(rows):
        return [{"mint": m, "owner": o, "uiTokenAmount": {"amount": str(a), "decimals": 9}}
                for m, o, a in rows]
    return {"transaction": {"message": {"accountKeys": [{"pubkey": payer}, {"pubkey": POOL}]}},
            "meta": {"preTokenBalances": bal(balances_pre), "postTokenBalances": bal(balances_post)}}


def test_seller_puts_coin_INTO_the_pool():
    # pool gains 1000 coin, pays out 2 WSOL
    tx = _tx([(COIN, POOL, 0), (sa.WSOL, POOL, 2)],
             [(COIN, POOL, 1000), (sa.WSOL, POOL, 0)])
    assert sa.swap_amounts(tx, COIN) == (1000, -2)


def test_buyer_takes_coin_OUT_of_the_pool():
    tx = _tx([(COIN, POOL, 3000), (sa.WSOL, POOL, 0)],
             [(COIN, POOL, 0), (sa.WSOL, POOL, 5)])
    assert sa.swap_amounts(tx, COIN) == (-3000, 5)


def test_account_opened_inside_the_tx_counts_from_zero():
    """A token account absent from preTokenBalances started at zero — the common Solana case."""
    tx = _tx([(COIN, POOL, 3000)], [(COIN, POOL, 0), (sa.WSOL, POOL, 5)])
    assert sa.swap_amounts(tx, COIN) == (-3000, 5)


def test_usdc_quote_works_too():
    tx = _tx([(COIN, POOL, 0), (sa.USDC, POOL, 7)],
             [(COIN, POOL, 1000), (sa.USDC, POOL, 0)])
    assert sa.swap_amounts(tx, COIN) == (1000, -7)


def test_one_sided_move_is_not_a_swap():
    """A plain transfer moves one leg only — recording it as a swap would fabricate flow."""
    tx = _tx([(COIN, POOL, 1000)], [(COIN, POOL, 0)])
    assert sa.swap_amounts(tx, COIN) is None       # no quote leg on the same owner -> not a pool


def test_both_legs_same_direction_is_not_a_swap():
    """An LP deposit adds BOTH sides; it is not directional flow and must not be counted as one."""
    tx = _tx([(COIN, POOL, 1000), (sa.WSOL, POOL, 5)],
             [(COIN, POOL, 0), (sa.WSOL, POOL, 0)])
    assert sa.swap_amounts(tx, COIN) is None


def test_missing_payer_or_balances_fails_closed():
    assert sa.swap_amounts({}, COIN) is None
    assert sa.swap_amounts(_tx([], []), COIN) is None
    assert sa.swap_amounts(_tx([(COIN, POOL, 1)], [(COIN, POOL, 0)]), None) is None


def test_swap_row_is_pool_ticks_shaped_with_coin_as_amount0():
    tx = _tx([(COIN, POOL, 0), (sa.WSOL, POOL, 2)],
             [(COIN, POOL, 1000), (sa.WSOL, POOL, 0)])
    row = sa.swap_row(tx, COIN, slot=555, signature="sig1", observed_at="t")
    assert row["chain_id"] == "solana" and row["block_no"] == 555 and row["kind"] == "sol_swap"
    assert row["amount0"] == "1000" and row["amount1"] == "-2"     # quote_side=1 for this shape
    assert row["maker"] == TRADER


def test_module_never_touches_the_custody_rpc():
    import inspect
    body = inspect.getsource(sa).split('"""', 2)[-1]
    assert "MEMECOIN_SOLANA_RPC_URL" not in body
    assert "solana_read_rpc" in body


def test_multi_hop_touching_two_pools_of_the_same_coin_fails_closed():
    """A routed swap can touch several pools of one coin. Picking one leg as 'the' market would
    fabricate flow, so ambiguity refuses. Live mainnet shows this is the common case."""
    POOL2 = "PoolVault222222222222222222222222222222222222"
    tx = _tx([(COIN, POOL, 0), (sa.WSOL, POOL, 2), (COIN, POOL2, 0), (sa.WSOL, POOL2, 9)],
             [(COIN, POOL, 1000), (sa.WSOL, POOL, 0), (COIN, POOL2, 500), (sa.WSOL, POOL2, 0)])
    assert sa.swap_amounts(tx, COIN) is None


def test_fee_payer_owning_nothing_is_fine():
    """The decisive real-world case: in a routed swap the fee payer owns NONE of the token accounts.
    A trader-centric decoder returned None on 5/5 live transactions; the pool-centric one works."""
    tx = _tx([(COIN, POOL, 0), (sa.WSOL, POOL, 2)],
             [(COIN, POOL, 1000), (sa.WSOL, POOL, 0)], payer="SomeRelayerBot1111111111111111111111111111")
    assert sa.swap_amounts(tx, COIN) == (1000, -2)


def _record_calls():
    seen = []

    def call(method, params):
        seen.append((method, params))
        return {"ok": True}

    return seen, call


def test_get_transaction_sends_no_commitment_by_default():
    """Silence keeps the RPC default (`finalized`). The WO-2 sale-authority reader relies on it."""
    seen, call = _record_calls()
    assert sa.get_transaction_fn(call=call)("SIG") == {"ok": True}
    method, (signature, opts) = seen[0]
    assert (method, signature) == ("getTransaction", "SIG")
    assert "commitment" not in opts
    assert opts["encoding"] == "jsonParsed"
    assert opts["maxSupportedTransactionVersion"] == 1


def test_get_transaction_can_read_at_confirmed():
    """The `finalized` default put a 13.8 s floor under every funded pool bind, which
    BIND_MAX_ATTEMPTS could not outlive. Reading a LANDED transaction at `confirmed` is correct
    for that caller: it names a pool to observe, it never signs and never sizes."""
    seen, call = _record_calls()
    assert sa.get_transaction_fn(call=call, commitment="confirmed")("SIG") == {"ok": True}
    _method, (_signature, opts) = seen[0]
    assert opts["commitment"] == "confirmed"
    assert opts["encoding"] == "jsonParsed"
