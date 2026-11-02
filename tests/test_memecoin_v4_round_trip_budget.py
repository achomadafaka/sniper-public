"""The measured round-trip cost gates an entry, replacing depth as a proxy for it.

`multihop_quote` has always quoted all four legs and reported `round_trip_pct` -- what a clip
loses buying in and selling straight back out -- and nothing read it. Depth stood in for that
cost, and on chain 4663 depth is wrong in BOTH directions: it admits a deep pool charging 90% and
rejects a shallow honest one.

FRIES (0x798c78cf) is the live case. Refused by the 100-clip depth floor demanding $13,900, its
route appears at 20 clips and quotes a -24.8% round trip against a 25% take-profit -- the exit it
aims at does not cover getting in and out.
"""
from sniper.execution.evm_v4_multihop import round_trip_within_budget


class _Cfg:
    def __init__(self, frac=0.4, hard=10.0):
        self.MEMECOIN_EVM_V4_MULTIHOP_MAX_ROUND_TRIP_TP_FRAC = frac
        self.MEMECOIN_EVM_V4_MULTIHOP_MAX_ROUND_TRIP_PCT = hard


def test_fries_is_refused_against_its_own_take_profit():
    # -24.8% round trip, 25% TP, budget 0.4*25 = 10%.
    assert round_trip_within_budget(-24.793289052086475, 25.0, cfg=_Cfg()) is False


def test_a_cheap_route_passes():
    assert round_trip_within_budget(-3.1, 25.0, cfg=_Cfg()) is True


def test_the_same_cost_is_judged_against_the_TP_it_must_clear():
    """A global percentage cannot express this: -24.8% is fatal against a 25% TP and merely
    expensive against a 200% one."""
    assert round_trip_within_budget(-24.8, 25.0, cfg=_Cfg()) is False
    assert round_trip_within_budget(-24.8, 200.0, cfg=_Cfg()) is True


def test_no_take_profit_falls_back_to_the_absolute_ceiling():
    # A bare TP-relative test would pass EVERYTHING whenever the take-profit is 0.
    assert round_trip_within_budget(-24.8, 0.0, cfg=_Cfg()) is False
    assert round_trip_within_budget(-4.0, 0.0, cfg=_Cfg()) is True


def test_an_unreadable_cost_is_not_a_cheap_one():
    assert round_trip_within_budget(None, 25.0, cfg=_Cfg()) is False
    assert round_trip_within_budget("nope", 25.0, cfg=_Cfg()) is False
    assert round_trip_within_budget(float("nan"), 25.0, cfg=_Cfg()) is False
    assert round_trip_within_budget(float("-inf"), 25.0, cfg=_Cfg()) is False


def test_the_sign_convention_does_not_matter():
    # round_trip_pct is negative for a loss; the budget is a magnitude.
    assert round_trip_within_budget(-3.0, 25.0, cfg=_Cfg()) is True
    assert round_trip_within_budget(3.0, 25.0, cfg=_Cfg()) is True


def test_zeroing_both_knobs_restores_depth_only_screening():
    assert round_trip_within_budget(-99.0, 25.0, cfg=_Cfg(frac=0.0, hard=0.0)) is True


def test_the_margin_widens_the_budget_only_when_asked():
    from sniper.execution.evm_v4_multihop import round_trip_within_budget as ok
    import types
    cfg = types.SimpleNamespace(MEMECOIN_EVM_V4_MULTIHOP_MAX_ROUND_TRIP_TP_FRAC=0.6,
                                MEMECOIN_EVM_V4_MULTIHOP_MAX_ROUND_TRIP_PCT=10.0)

    assert ok(-15.09, 25.0, cfg=cfg) is False                      # admission is unchanged
    assert ok(-15.09, 25.0, cfg=cfg, margin_pct=10) is True        # SKIBIDI, re-ask only
    assert ok(-16.60, 25.0, cfg=cfg, margin_pct=10) is False       # beyond the margin
    assert ok(-11.79, 10.0 / 0.6, cfg=cfg, margin_pct=10) is False  # OP829 stays one-shot
