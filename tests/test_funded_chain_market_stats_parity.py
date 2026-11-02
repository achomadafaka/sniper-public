"""Every chain we arm for funded trading must be able to produce market stats.

This is the regression pin for 2026-08-30: chain 4663 was armed for funded trading with no
`FOMO_TO_DEX` entry, so `_expected_chain` returned None, `dexscreener_stats` returned None, and
six market-data entry gates read an empty dict and admitted unconditionally for the whole life
of the chain. See docs/audits/2026-08-30-4663-entry-gate-parity.md.

The arming set is READ FROM CONFIG, never written as a literal here. A hardcoded chain list in
this file would reproduce the exact bug it exists to catch: the defect was a chain being armed
in one place and absent from another, and a test that restates the second list cannot see that.
"""
import sniper.settings.memecoin as cfg
from sniper.execution.solana_quote import (
    DEX_CHAIN_TO_FOMO, FOMO_TO_DEX, _expected_chain)


def funded_armed_chains():
    """Chains a funded entry can be routed to. Solana is always armed; EVM comes from config."""
    return {"solana"} | {str(c) for c in (cfg.MEMECOIN_EVM_ALLOWLIST or ())}


def test_every_funded_armed_chain_has_a_dexscreener_mapping():
    """Membership in MEMECOIN_DEX_BLIND_CHAINS is NOT a substitute.

    The blind path (`_default_blind_mcap_fn`) resolves MARKET CAP only. It never populates the
    `st` dict, and `st` is what the m5/h1 volume revival guard, the chase gate, the sell-share
    cap, the coin-age ceiling, the winner-re-entry check and the execution premium cap all read.
    A chain can therefore be fully priced for banding and still admit every coin unconditionally.
    """
    missing = sorted(c for c in funded_armed_chains() if c not in FOMO_TO_DEX)
    assert not missing, (
        f"chains armed for funded trading with no FOMO_TO_DEX entry: {missing}. "
        "Their market-data entry gates read an empty dict and admit everything. "
        "Add the DexScreener chain slug to FOMO_TO_DEX in sniper/execution/solana_quote.py."
    )


def test_expected_chain_resolves_for_every_funded_armed_chain():
    """One level down from the map: the resolver the price path actually calls."""
    unresolved = sorted(
        c for c in funded_armed_chains()
        if _expected_chain("0x" + "0" * 40 if c != "solana" else "So11", c) is None)
    assert not unresolved, (
        f"_expected_chain returned None for armed chains: {unresolved} -> dexscreener_stats "
        "returns None -> real_entry_hook runs with st={}")


def test_dex_chain_slugs_round_trip():
    """A duplicate DexScreener slug would silently collapse the reverse map and misattribute a
    chain. Cheap to assert, and the reverse map feeds discovery and paper entry attribution."""
    assert len(DEX_CHAIN_TO_FOMO) == len(FOMO_TO_DEX), (
        "duplicate DexScreener slug in FOMO_TO_DEX collapsed DEX_CHAIN_TO_FOMO")
    for network, slug in FOMO_TO_DEX.items():
        assert DEX_CHAIN_TO_FOMO[slug] == network
