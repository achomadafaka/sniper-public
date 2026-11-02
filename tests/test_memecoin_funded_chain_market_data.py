"""Every funded chain must be able to produce market stats (2026-08-30).

Chain 4663 (Robinhood) had no `FOMO_TO_DEX` entry, so `_expected_chain` returned None,
`select_dex_pair` returned None, `dexscreener_stats` returned None, and `real_entry_hook`
ran with `st = {}`. Six funded entry gates then admitted unconditionally: the m5/h1 volume
revival guard, the execution premium cap, the chase gate and its impact allowance, the
sell-share cap, the coin-age ceiling and the winner-re-entry price gate. Measured over 30
days: the volume guard fired 348 times on solana and 0 times on 4663.

The blind path does NOT satisfy this invariant. `MEMECOIN_DEX_BLIND_CHAINS` resolves a market
cap and nothing else, so a chain that is only in the blind set still starves every gate that
needs volume, liquidity or txn counts. Membership of the blind set is therefore not accepted
as coverage here.

The funded-chain list is read from `memecoin_chains.CHAINS` rather than written out, because a
guard that intersects against a hardcoded list can only ever report members of that list --
which is the exact shape of the bug being pinned.
"""
import pytest

import sniper.services.memecoin_real_runner as runner
from sniper.services.memecoin_chains import funded_chain_keys, canonical_chain_id
from sniper.execution.solana_quote import (
    FOMO_TO_DEX, DEX_CHAIN_TO_FOMO, _expected_chain, select_dex_pair, resolve_evm_chain,
)

EVM_MINT = "0x8ecea3d0e648db646d824aa51eedeb16ac3d6878"


def _pair(chain, mint, *, liq, price="0.001681"):
    return {"chainId": chain, "baseToken": {"address": mint}, "pairAddress": "0xpair" + chain,
            "priceUsd": price, "liquidity": {"usd": liq}, "volume": {"h24": 1000.0}}


@pytest.mark.parametrize("key", funded_chain_keys())
def test_every_funded_chain_maps_to_a_dexscreener_slug(key):
    """A funded chain with no slug starves every market-data gate on that chain."""
    network_id = canonical_chain_id(key)
    assert network_id is not None, f"{key} has no canonical network id"
    assert _expected_chain(EVM_MINT, network_id) is not None, (
        f"funded chain {key!r} (network_id {network_id!r}) has no FOMO_TO_DEX entry, so "
        f"dexscreener_stats returns None and every market-data entry gate reads {{}} and admits"
    )


def test_4663_resolves_to_the_robinhood_slug():
    assert _expected_chain(EVM_MINT, "4663") == "robinhood"
    assert FOMO_TO_DEX["4663"] == "robinhood"


def test_5042_resolves_to_the_arc_slug():
    # verified live 2026-09-16: api.dexscreener.com/latest/dex/tokens/<Arc USDC address> returns
    # real indexed pairs with "chainId":"arc" — DexScreener already covers Arc at mainnet launch.
    assert _expected_chain(EVM_MINT, "5042") == "arc"
    assert FOMO_TO_DEX["5042"] == "arc"


def test_4663_reverse_map_is_present():
    """dexscreener_stats_batch_keyed and the copy trader key rows off the reverse map."""
    assert DEX_CHAIN_TO_FOMO["robinhood"] == "4663"


def test_select_dex_pair_picks_the_robinhood_pair():
    """The real payload shape: DexScreener serves 4663 tokens under chainId 'robinhood'."""
    pairs = [_pair("base", EVM_MINT, liq=999999.0), _pair("robinhood", EVM_MINT, liq=189700.03)]
    chosen = select_dex_pair(pairs, EVM_MINT, "4663")
    assert chosen is not None, "no pair selected for a well-formed robinhood payload"
    assert chosen["chainId"] == "robinhood", "selected another chain's pair for a 4663 mint"


# --- resolve_evm_chain: a SEPARATE, money-flow decision, default OFF -------------------------

def _http_both_chains(status=200):
    def http(method, url, headers=None, body=None):
        return status, {"pairs": [_pair("base", EVM_MINT, liq=1000.0),
                                  _pair("robinhood", EVM_MINT, liq=500000.0)]}, None
    return http


def test_resolve_evm_chain_does_not_route_to_4663_by_default(monkeypatch):
    """Restoring 4663 market data must not silently reroute chain resolution.

    Adding 4663 to the guess set does TWO things, not one: a robinhood-only token that
    previously resolved to None (and was dropped) becomes a funded entry, AND a token listed on
    both base and robinhood gains a second candidate, so the liquidity tie-break can rank 4663
    first or tie and return None where it previously resolved. Both are money-flow changes and
    neither is covered by the decision to arm the safety gates.
    """
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_RESOLVE_UNKNOWN_EVM_4663", False, raising=False)
    assert resolve_evm_chain(EVM_MINT, http=_http_both_chains()) != "4663"


def test_resolve_evm_chain_routes_to_4663_when_enabled(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_RESOLVE_UNKNOWN_EVM_4663", True, raising=False)
    assert resolve_evm_chain(EVM_MINT, http=_http_both_chains()) == "4663"


def test_5042_stays_out_of_resolvable_evm_networks():
    """FOMO_TO_DEX['5042'] restores Arc's MARKET DATA only. `_resolvable_evm_networks` derives
    its list FROM FOMO_TO_DEX, so that one addition also widened the set of chains an
    unknown-chain buy may ROUTE to — a money-flow change this function's own docstring says
    must not ride on the market-data approval. Pins the two apart."""
    from sniper.execution.solana_quote import _resolvable_evm_networks

    assert "5042" not in set(_resolvable_evm_networks())


# --- fail-closed guard: an allowlisted chain with no market-data provider must refuse --------

def test_a_mapped_chain_has_no_refusal_reason():
    import sniper.services.memecoin_real_runner as runner
    for chain in ("solana", "8453", "56", "4663", "1", "5042"):
        assert runner._market_data_unavailable_reason(chain) is None, chain


def test_an_unmapped_chain_names_itself_in_the_refusal():
    import sniper.services.memecoin_real_runner as runner
    reason = runner._market_data_unavailable_reason("99999")
    assert reason is not None
    assert "99999" in reason


def test_real_entry_hook_checks_market_data_before_any_gate_can_read_it():
    """Source-inspection pin, matching this file's own family precedent (band_source_pin.py):
    `real_entry_hook` is too large to invoke end-to-end here. Guards against the check being
    removed or moved after the chase gate's stats fetch, where a chain with no provider would
    again fall through to gates that fail open on missing data."""
    from _source_guard import code_of

    src = code_of(runner.real_entry_hook)
    assert "_market_data_unavailable_reason" in src


def test_blind_mcap_fallback_is_preserved_for_4663():
    """The GT blind path must stay: it covers the 37% of 4663 rows DexScreener leaves without
    a market cap. It is a SECOND source now, not a replacement."""
    # Bare import on purpose: the default is already "4663", and reloading the settings module
    # or loading the live .env mutates process-global state that other tests in this xdist
    # worker read.
    from sniper.settings import memecoin as cfg
    assert "4663" in (cfg.MEMECOIN_DEX_BLIND_CHAINS or ()), (
        "4663 dropped from MEMECOIN_DEX_BLIND_CHAINS: that removes the GeckoTerminal mcap "
        "fallback and the assume-micro promotion with it"
    )
