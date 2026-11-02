"""A wrapped-native quote token must be priced in ITS OWN asset, not always in ether.

BSC became a funded chain on 2026-09-06 with `CHAIN_QUOTE["56"]` = WBNB / 18 decimals / kind "eth".
`kind: "eth"` means WRAPPED NATIVE — `native_quote_chains()` says so — but `quote_usd_price` read
the price from `eth_usd_price()` unconditionally, so WBNB was marked at the live ETHER price.

On 2026-09-08 that was ETH $2478.4 against BNB about $748: a 3.31x error on every BSC money path.

    bankroll   quote_balance_usd_or_none  0.1756 WBNB -> "$131.34"; the wallet held $39.63
    clip       usd_to_quote_atomic        a $19.82 clip -> 0.0080 WBNB, worth $5.98 on the chain
    proceeds   receipt_proceeds_fn        WBNB received valued 3.31x high in the fill record

The operator caught it from the dashboard ("Ui shows what I said") while the chain read disagreed.
BSC had 0 fills at the time, so no position was opened on the wrong number.

The regression guard is the LAST test here: every wrapped-native chain must declare `usd_symbol`,
and a chain that declares none must price to None rather than inherit ether's mark.
"""
import pytest

from sniper.execution import evm_wallet as W


def _http_returning(price_by_symbol):
    """Stand in for the Binance ticker; assert the symbol the code actually asked for."""
    seen = []

    def http(method, url, **kw):
        sym = url.split("symbol=")[1]
        seen.append(sym)
        return 200, {"price": str(price_by_symbol[sym])}, None
    return http, seen


ETH, BNB = 2478.40, 748.00
PRICES = {"ETHUSDT": ETH, "BNBUSDT": BNB}


@pytest.mark.parametrize("chain,want", [("56", BNB), ("4663", ETH), ("8453", ETH)])
def test_each_wrapped_native_chain_prices_its_own_asset(chain, want):
    http, seen = _http_returning(PRICES)
    assert W.quote_usd_price(chain, http=http) == want
    assert seen == [("BNBUSDT" if chain == "56" else "ETHUSDT")]


def test_the_bsc_bankroll_is_no_longer_the_ether_mark():
    """THE REGRESSION, in the operator's own units."""
    http, _ = _http_returning(PRICES)
    wbnb = 131.34 / ETH            # the balance the dashboard rendered as "$131.34"
    assert wbnb * W.quote_usd_price("56", http=http) == pytest.approx(39.63, abs=0.02)


def test_a_stablecoin_quoted_chain_still_prices_at_one_without_a_network_read():
    def boom(*a, **k):
        raise AssertionError("a stablecoin quote must not fetch a price")
    assert W.quote_usd_price("5042", http=boom) == 1.0      # chain 1 quotes in WETH since 2026-09-30 (eth-mainnet spec); Arc (5042) is the USD-quoted 6-decimal chain


def test_an_unknown_chain_fails_closed():
    http, _ = _http_returning(PRICES)
    assert W.quote_usd_price("999", http=http) is None
    assert W.native_usd_price("999", http=http) is None


def test_native_usd_price_declines_a_stablecoin_chain():
    """`native_usd_price` answers about the WRAPPED-NATIVE asset. On a USDC-quoted chain there is
    no such quote asset, and returning 1.0 there would put a stablecoin mark on real ether."""
    assert W.native_usd_price("5042") is None               # chain 1 quotes in WETH since 2026-09-30 (eth-mainnet spec); Arc (5042) is the USD-quoted 6-decimal chain


def test_eth_usd_price_still_means_ether():
    """Research scripts and the 4663 tape call this by name; it must not become chain-relative."""
    http, seen = _http_returning(PRICES)
    assert W.eth_usd_price(http=http) == ETH
    assert seen == ["ETHUSDT"]


def test_a_price_source_that_fails_yields_none_not_a_zero():
    def dead(*a, **k):
        raise RuntimeError("binance down")
    assert W.asset_usd_price("BNB", http=dead) is None
    assert W.quote_usd_price("56", http=dead) is None


# --- the guard that would have caught this on the day BSC was configured ------------------------

def test_every_wrapped_native_chain_declares_the_asset_it_is_worth():
    missing = [c for c, q in W.CHAIN_QUOTE.items()
               if str(q.get("kind") or "").lower() == "eth" and not q.get("usd_symbol")]
    assert not missing, (
        "chains %s quote in wrapped native with no usd_symbol; they would inherit ether's mark"
        % missing)


def test_an_undeclared_wrapped_native_chain_prices_to_none_rather_than_ether():
    """Belt and braces: if someone adds a chain and skips the symbol, it must fail closed."""
    http, _ = _http_returning(PRICES)
    W.CHAIN_QUOTE["31337"] = {"token": "0x" + "00" * 20, "decimals": 18, "kind": "eth"}
    try:
        assert W.native_usd_price("31337", http=http) is None
        assert W.quote_usd_price("31337", http=http) is None
    finally:
        del W.CHAIN_QUOTE["31337"]
