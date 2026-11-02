"""The on-chain rung ladder must not be switched off by a PRICING decision.

2026-09-04: the v2/v4 gate read `MEMECOIN_DEX_BLIND_CHAINS`, which answers "which chains need a
price source other than DexScreener". Clearing that list to retire GeckoTerminal disabled the V4
rung on 4663 -- where every pool is V4 -- and 26 entries refused `no_route_all_venues` in 84
minutes against pools that were quotable the whole time. `evm.quote_miss` recorded it exactly:
"no route on v3 (v2/v4 off)" with v2 and v4 both SKIPPED.
"""
from sniper.execution.evm_real_seams import _rung_ladder_chains


class _Cfg:
    def __init__(self, ladder=None, blind=None):
        if ladder is not None:
            self.MEMECOIN_EVM_RUNG_LADDER_CHAINS = ladder
        self.MEMECOIN_DEX_BLIND_CHAINS = blind if blind is not None else set()


def test_retiring_a_pricing_source_does_not_disable_the_rungs():
    """The exact regression: pricing list emptied, ladder must survive."""
    assert "4663" in _rung_ladder_chains(_Cfg(ladder={"4663"}, blind=set()))


def test_the_dedicated_setting_wins_when_present():
    cfg = _Cfg(ladder={"4663"}, blind={"8453"})
    chains = _rung_ladder_chains(cfg)
    assert "4663" in chains and "8453" not in chains


def test_it_falls_back_to_the_pricing_list_when_unset():
    # An older deployment that never sets the new name keeps its previous behaviour.
    assert _rung_ladder_chains(_Cfg(blind={"4663"})) == {"4663"}


def test_an_empty_dedicated_setting_falls_back_rather_than_disabling_everything():
    assert _rung_ladder_chains(_Cfg(ladder=set(), blind={"4663"})) == {"4663"}


def test_4663_runs_the_ladder_under_the_real_shipped_settings():
    """Guards the live default: every 4663 pool is V4, so this chain losing its rungs means it
    cannot route at all."""
    from sniper.settings import memecoin as cfg
    assert "4663" in _rung_ladder_chains(cfg)
