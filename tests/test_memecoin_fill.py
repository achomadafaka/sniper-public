from sniper.execution import solana_quote as q


def test_honest_fill_buy_uses_dexscreener_no_jupiter():
    # Jupiter quote endpoint is dead (quote-api.jup.ag unreachable) and its response omits
    # outDecimals -> a 1000x mispricing trap. The leg is REMOVED; a BUY prices straight off
    # DexScreener mid + modeled slippage. Assert Jupiter's URL is never even requested.
    seen = []

    def http(method, url, headers=None, body=None):
        seen.append(url)
        return 200, {"pairs": [{"chainId": "solana", "pairAddress": "p1",
                                 "baseToken": {"address": "MINT"},
                                 "priceUsd": "0.005", "liquidity": {"usd": "1e15"}}]}, None
    price, src = q.honest_fill("MINT", 100.0, "buy", fallback_price=0.9, http=http)
    assert src == "dexscreener" and abs(price - 0.005) < 1e-6
    assert not any("jup" in u for u in seen), "Jupiter leg must be removed (dead endpoint + decimals trap)"


def test_dexscreener_stats_token_age_uses_earliest_same_mint_pool():
    # Review (methodologist): pairs[0] is NOT the launch pool — memecoins migrate (pump.fun ->
    # Raydium), so entry_token_age_s from pairs[0].pairCreatedAt is wrong. Age = earliest pool
    # for THIS base mint; a pair where the token is the QUOTE side is ignored. n_pairs exposed.
    MINT = "So1111"
    def http(method, url, headers=None, body=None):
        return 200, {"pairs": [
            {"chainId": "solana", "pairAddress": "new", "baseToken": {"address": MINT},
             "priceUsd": "0.005", "liquidity": {"usd": "1e6"},
             "pairCreatedAt": 2000000},   # migrated pool (newer) — happens to be pairs[0]
            {"chainId": "solana", "pairAddress": "old", "baseToken": {"address": MINT},
             "priceUsd": "0.004", "liquidity": {"usd": "5e5"},
             "pairCreatedAt": 1000000},   # original launch pool (older)
            {"chainId": "solana", "pairAddress": "other", "baseToken": {"address": "OTHER"},
             "pairCreatedAt": 500000},   # token is QUOTE -> ignore
        ]}, None
    st = q.dexscreener_stats(MINT, http=http)
    assert st["pair_created_at"] == 1000000.0    # earliest same-mint pool, not pairs[0]'s 2000000
    assert st["n_pairs"] == 2                     # OTHER (token-as-quote) excluded
    assert st["price"] == 0.005                   # price/liquidity still from pairs[0] (unchanged)


def test_honest_fill_falls_back_to_dexscreener_then_fomo():
    def http_ds(method, url, headers=None, body=None):
        if "jup" in url:
            raise RuntimeError("jupiter down")
        return 200, {"pairs": [{"chainId": "solana", "pairAddress": "p1",
                                 "baseToken": {"address": "MINT"},
                                 "priceUsd": "0.007", "liquidity": {"usd": "1e15"}}]}, None
    price, src = q.honest_fill("MINT", 100.0, "buy", fallback_price=0.9, http=http_ds)
    assert src == "dexscreener" and abs(price - 0.007) < 1e-6

    def http_dead(method, url, headers=None, body=None):
        raise RuntimeError("all down")
    price, src = q.honest_fill("MINT", 100.0, "buy", fallback_price=0.9, http=http_dead)
    assert src == "fomo" and price == 0.9
