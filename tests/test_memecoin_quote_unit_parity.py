"""Route-quote fills must be USD per token on every chain (audit 2026-07-30).

Chains 4663 and 8453 quote in WETH; every other chain quotes in a USD stablecoin. Storing the
WETH number next to USD marks inflated every 4663 position by the ETH/USD rate — a $100
GME clip booked $202,724 of fake profit on a mirror exit (~2027x). Base joined the WETH side on
2026-09-06, so it is now on the CONVERTED side of this file, not the pass-through side.
"""
import pytest

from sniper.services import memecoin_copy_trader as ct


def test_usd_chains_pass_through_unchanged():
    for chain in ("solana", "5042"):
        price, reason = ct._quote_price_to_usd(1.25e-6, chain)
        assert price == 1.25e-6 and reason is None, chain


@pytest.mark.parametrize("chain", ["4663", "8453", "56", "1"])
def test_eth_quoted_chain_is_converted(monkeypatch, chain):
    monkeypatch.setattr(
        "sniper.services.memecoin_quotes.quote_usd_price", lambda c: 2000.0)
    price, reason = ct._quote_price_to_usd(2.033653e-08, chain)
    assert reason is None
    assert price == pytest.approx(2.033653e-08 * 2000.0)


@pytest.mark.parametrize("chain", ["4663", "8453", "56", "1"])
def test_an_eth_quoted_chain_fails_closed_without_a_mark(monkeypatch, chain):
    """The GME regression on Base would be the same 2027x, so the same rule guards it."""
    monkeypatch.setattr(
        "sniper.services.memecoin_quotes.quote_usd_price", lambda c: None)
    price, reason = ct._quote_price_to_usd(2.033653e-08, chain)
    assert price is None and reason == "quote_asset_price_unavailable"


def test_missing_quote_asset_price_fails_closed(monkeypatch):
    """No ETH price must mean NO fill, never an unconverted WETH number."""
    monkeypatch.setattr(
        "sniper.services.memecoin_quotes.quote_usd_price", lambda c: None)
    price, reason = ct._quote_price_to_usd(2.033653e-08, "4663")
    assert price is None and reason == "quote_asset_price_unavailable"


def test_the_gme_regression(monkeypatch):
    """The exact observed case: entry 2.034e-08 WETH vs a 4.123e-05 USD mirror mark.

    Unconverted that is a 2027x 'gain' on a $100 clip. Converted, entry and exit are the
    same unit and the move is single-digit percent.
    """
    monkeypatch.setattr(
        "sniper.services.memecoin_quotes.quote_usd_price", lambda c: 1918.77)
    entry_usd, _ = ct._quote_price_to_usd(2.033653e-08, "4663")
    exit_mark_usd = 4.122720e-05
    assert exit_mark_usd / entry_usd < 1.2, "entry and exit must be the same unit"
