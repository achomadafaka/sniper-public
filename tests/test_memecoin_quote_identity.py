import math

from sniper.execution import solana_quote as q


MINT = "So11111111111111111111111111111111111111112"
EVM = "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"


def _pair(*, chain="solana", base=MINT, quote="USDC", price="0.01", liquidity=1000,
          volume=100, address="pair-a", dex="raydium", created=1000,
          volume_m5=None, volume_h1=None, txns_m5_buys=None, txns_m5_sells=None):
    vol = {"h24": volume}
    if volume_m5 is not None:
        vol["m5"] = volume_m5
    if volume_h1 is not None:
        vol["h1"] = volume_h1
    pair = {
        "chainId": chain,
        "pairAddress": address,
        "dexId": dex,
        "baseToken": {"address": base, "symbol": "TOK"},
        "quoteToken": {"address": quote},
        "priceUsd": price,
        "liquidity": {"usd": liquidity},
        "volume": vol,
        "marketCap": 12345,
        "pairCreatedAt": created,
    }
    if txns_m5_buys is not None or txns_m5_sells is not None:
        pair["txns"] = {"m5": {"buys": txns_m5_buys, "sells": txns_m5_sells}}
    return pair


def test_select_dex_pair_filters_chain_and_base_before_ranking():
    pairs = [
        _pair(chain="base", base=EVM, liquidity=9_000_000, address="wrong-chain"),
        _pair(base="OTHER", quote=MINT, liquidity=8_000_000, address="quote-side"),
        _pair(liquidity=100, address="shallow"),
        _pair(liquidity=50_000, address="deep"),
    ]
    selected = q.select_dex_pair(pairs, MINT, "solana")
    assert selected["pairAddress"] == "deep"


def test_select_dex_pair_uses_volume_then_pair_address_as_stable_ties():
    pairs = [
        _pair(liquidity=1000, volume=200, address="pair-z"),
        _pair(liquidity=1000, volume=300, address="pair-b"),
        _pair(liquidity=1000, volume=300, address="pair-a"),
    ]
    assert q.select_dex_pair(pairs, MINT, "solana")["pairAddress"] == "pair-a"


def test_select_dex_pair_rejects_nonpositive_nonfinite_and_missing_identity():
    pairs = [
        _pair(price="0", address="zero"),
        _pair(price="nan", address="nan"),
        _pair(price="inf", address="inf"),
        {**_pair(address="missing-pair"), "pairAddress": ""},
    ]
    assert q.select_dex_pair(pairs, MINT, "solana") is None


def test_network_rules_infer_solana_but_require_network_for_evm():
    assert q.select_dex_pair([_pair()], MINT, None)["pairAddress"] == "pair-a"
    assert q.select_dex_pair([_pair(chain="base", base=EVM)], EVM, None) is None


def test_evm_base_address_comparison_is_case_insensitive():
    checksummed = "0xAbCdEfAbCdEfAbCdEfAbCdEfAbCdEfAbCdEfAbCd"
    selected = q.select_dex_pair(
        [_pair(chain="base", base=checksummed, address="base-pair")], EVM, "8453",
    )
    assert selected["pairAddress"] == "base-pair"


def test_dexscreener_stats_returns_selected_pair_identity_and_age_across_same_mint_pools():
    def http(*_args, **_kwargs):
        return 200, {"pairs": [
            _pair(liquidity=5000, address="deep", created=2000),
            _pair(liquidity=1000, address="launch", created=1000),
        ]}, None

    st = q.dexscreener_stats(MINT, http=http, network_id="solana")
    assert st["price"] == 0.01
    assert st["pair_address"] == "deep"
    assert st["dex_id"] == "raydium"
    assert st["chain"] == "solana"
    assert st["pair_created_at"] == 1000.0
    assert st["n_pairs"] == 2


def test_keyed_batch_keeps_same_evm_address_separate_by_chain():
    def http(*_args, **_kwargs):
        return 200, {"pairs": [
            _pair(chain="base", base=EVM.upper(), price="0.01", liquidity=2000,
                  address="base-pair", dex="aerodrome"),
            _pair(chain="ethereum", base=EVM, price="0.02", liquidity=3000,
                  address="eth-pair", dex="uniswap"),
        ]}, None

    out = q.dexscreener_stats_batch_keyed([("8453", EVM), ("1", EVM)], http=http)
    assert out[("8453", EVM)]["price"] == 0.01
    assert out[("8453", EVM)]["pair_address"] == "base-pair"
    assert out[("1", EVM)]["price"] == 0.02
    assert out[("1", EVM)]["pair_address"] == "eth-pair"


def test_chain_resolution_rejects_equal_depth_multichain_ambiguity():
    def http(*_args, **_kwargs):
        return 200, {"pairs": [
            _pair(chain="base", base=EVM, liquidity=1000, address="base-pair"),
            _pair(chain="ethereum", base=EVM, liquidity=1000, address="eth-pair"),
        ]}, None

    assert q.resolve_evm_chain(EVM, http=http) is None


def test_quote_key_and_lookup_prefer_network_key_over_legacy_mint_key():
    price_map = {
        ("8453", EVM): {"price": 0.01},
        EVM: {"price": 999.0},
    }
    assert q.quote_key("8453", EVM) == ("8453", EVM)
    assert q.quote_for(price_map, EVM, "8453")["price"] == 0.01


def test_selected_price_is_finite():
    selected = q.select_dex_pair([_pair(price="1.25")], MINT, "solana")
    assert math.isfinite(float(selected["priceUsd"]))


def test_select_dex_pair_ranks_by_quote_side_depth_not_self_valued_liquidity():
    """PURR copycat incident (position 40687, 2026-08-19): a hostile pool held ~833M
    tokens, and DexScreener valued liquidity.usd at the pool's own wash price. The
    fake $92.8M depth beat the real $20K pool for 25 minutes, so every mid lane
    printed a 4,560x wrong unit. Quote-side USD (usd - base*priceUsd) is the part a
    pool creator cannot fake without a deposit of real quote tokens."""
    scam = _pair(price="0.1114", liquidity=92_800_000, address="scam")
    scam["liquidity"]["base"] = 833_000_000     # 833M * 0.1114 ~= the whole "usd" claim
    real = _pair(price="0.0000244", liquidity=20_000, address="real")
    real["liquidity"]["base"] = 400_000_000     # base side ~$9.8K -> quote side ~$10.2K real
    assert q.select_dex_pair([scam, real], MINT, "solana")["pairAddress"] == "real"


def test_select_dex_pair_falls_back_to_usd_liquidity_without_base_reserve():
    """A response without liquidity.base keeps the old deepest-by-usd order."""
    pairs = [
        _pair(liquidity=100, address="shallow"),
        _pair(liquidity=50_000, address="deep"),
    ]
    assert q.select_dex_pair(pairs, MINT, "solana")["pairAddress"] == "deep"


def test_stats_from_pairs_sums_volume_and_txns_across_same_identity_pools():
    """A coin split across pools (bonding-curve migration, a copy pool, a second DEX)
    must report TOKEN-level volume, not the routed pool's own slice."""
    pairs = [
        _pair(liquidity=50_000, address="deep", volume_m5=1_000, volume_h1=9_000,
              txns_m5_buys=5, txns_m5_sells=2),
        _pair(liquidity=100, address="shallow", volume_m5=400, volume_h1=1_500,
              txns_m5_buys=1, txns_m5_sells=3),
    ]
    st = q._stats_from_pairs(pairs, MINT, "solana")
    assert st["pair_address"] == "deep"          # routing/price still the deepest pool
    assert st["volume_m5"] == 1_400
    assert st["volume_h1"] == 10_500
    assert st["txns_m5_buys"] == 6
    assert st["txns_m5_sells"] == 5
    # the single-pool numbers survive for audit, unchanged by the sum
    assert st["volume_m5_top"] == 1_000
    assert st["volume_h1_top"] == 9_000
    assert st["txns_m5_top"] == {"buys": 5, "sells": 2}


def test_stats_from_pairs_missing_field_on_one_pool_is_not_zero_for_the_sum():
    """A pool with no m5 data is excluded from the sum, not folded in as a 0. If NO pool
    reports the field the result is None (unknown), never a fabricated 0."""
    pairs = [
        _pair(liquidity=50_000, address="deep", volume_m5=1_000),   # no volume_h1 at all
        _pair(liquidity=100, address="shallow"),                    # no m5 data at all
    ]
    st = q._stats_from_pairs(pairs, MINT, "solana")
    assert st["volume_m5"] == 1_000              # shallow pool's missing m5 is excluded, not 0
    assert st["volume_h1"] is None               # neither pool reports h1 -> unknown, not 0
    assert st["txns_m5_buys"] is None
    assert st["txns_m5_sells"] is None


def test_stats_from_pairs_single_pool_matches_pre_fix_behavior():
    """A coin with exactly one pool is unaffected: the sum equals the single reading."""
    pairs = [_pair(liquidity=50_000, address="only", volume_m5=250, volume_h1=900,
                    txns_m5_buys=4, txns_m5_sells=1)]
    st = q._stats_from_pairs(pairs, MINT, "solana")
    assert st["n_pairs"] == 1
    assert st["volume_m5"] == st["volume_m5_top"] == 250
    assert st["volume_h1"] == st["volume_h1_top"] == 900
    assert st["txns_m5_buys"] == 4
    assert st["txns_m5_sells"] == 1
    assert st["txns_m5_top"] == {"buys": 4, "sells": 1}
