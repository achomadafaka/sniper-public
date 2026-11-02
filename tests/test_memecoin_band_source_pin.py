"""The band source is pinned to the blind path on DexScreener-blind chains (2026-08-30).

Adding `"4663": "robinhood"` to FOMO_TO_DEX is required to arm six market-data ENTRY gates that
were admitting unconditionally. It also populates `st["market_cap"]` on 4663 for the first time,
and both mcap resolution sites read `st` BEFORE the blind path. Without this pin the map entry
would silently move the band source from GeckoTerminal to DexScreener -- and the band drives
EXIT policy, not just admission.

Measured across the nano/micro/small boundary regions we trade: 18 of 39 tokens (46%) change
band between the two sources; 9 of 11 tokens where GeckoTerminal has no cap (82%) move off
out_of_scope into a real band. Not FDV-vs-circulating (DS marketCap == fdv on 28 of 34, median
GT/DS 0.9925), not price (GT vs our own route quotes 0.967 median over ~4,000 paired marks).
Supply basis, unresolved.
"""
import pytest

from _source_guard import code_of

import sniper.services.memecoin_real_runner as R
from sniper.settings import memecoin as cfg

# NO load_env_files HERE, deliberately. conftest pins the scoring/exit env to canonical
# defaults; loading the operator's live .env at import time overwrites them for every other
# test sharing this xdist worker -- it failed 9 unrelated trail/derisk tests before this was
# removed. The bare default of MEMECOIN_DEX_BLIND_CHAINS is already "4663", so this file
# needs no env at all. Loading env is correct when MEASURING production and wrong inside a test.


def test_blind_chain_refuses_the_dexscreener_market_cap():
    """4663 is DexScreener-mapped AND blind. Being mapped must not win the band."""
    assert "4663" in (cfg.MEMECOIN_DEX_BLIND_CHAINS or ()), "fixture assumes 4663 is blind"
    assert R.dex_mcap_allowed("4663") is False


@pytest.mark.parametrize("chain", ["solana", "8453", "56", "1"])
def test_non_blind_chains_still_use_dexscreener(chain):
    """The pin must be scoped to blind chains; solana's band source is unchanged."""
    assert R.dex_mcap_allowed(chain) is True


def test_pin_follows_the_blind_set_rather_than_naming_4663(monkeypatch):
    """Driven off MEMECOIN_DEX_BLIND_CHAINS, not a hardcoded '4663'.

    A pin that names the chain cannot protect the next blind chain someone adds -- the same
    shape as the bug it is fixing, where a chain was armed in one list and absent from another.
    """
    monkeypatch.setattr(cfg, "MEMECOIN_DEX_BLIND_CHAINS", {"9999"}, raising=False)
    assert R.dex_mcap_allowed("9999") is False, "pin does not follow the configured blind set"
    assert R.dex_mcap_allowed("4663") is True, "pin has 4663 hardcoded somewhere"


def test_both_mcap_sites_read_the_primitive():
    """Guard against a future edit reintroducing a raw `st.get("market_cap")` at either site.

    Two copies of one money rule drift. This is the reintroduction guard, not a style check:
    the funded path and the manual path must band the same coin identically.
    """
    for fn in (R.make_real_buy_fn, R.real_entry_hook):
        src = code_of(fn)
        if 'get("market_cap")' in src:
            assert "dex_mcap_allowed" in src, (
                f"{fn.__name__} reads market_cap without the band-source pin"
            )


def test_the_funded_resolution_site_is_pinned():
    """The funded path lives in a very large function; pin its specific line shape."""
    src = code_of(R.make_real_buy_fn)
    assert "dex_mcap_allowed" in src, "manual/real buy path lost the band-source pin"
