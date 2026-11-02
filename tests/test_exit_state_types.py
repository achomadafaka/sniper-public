"""Phase 1 Task 2 — MarketState value type + state labels (exit brain).

Pins: None != 0.0 (unknown vs known-zero), non-actionable states are not candidate-capable,
immutability, and that every feature family can carry provenance.
"""
import dataclasses

import pytest

from sniper.analysis.exit_state_types import MarketState, Provenance, StateLabel


def _mk(state, **kw):
    return MarketState(chain_id="4663", mint="X", pool="0xpool", position_id=1,
                       engine_version="v1", feature_version="v1", state=state, **kw)


def test_none_is_not_zero_and_untrusted_not_capable():
    s = _mk(StateLabel.SOURCE_UNTRUSTED, nominal_price=None)
    assert s.nominal_price is None            # unknown, distinct from a known 0.0
    assert s.candidate_capable() is False


@pytest.mark.parametrize("state", [
    StateLabel.SOURCE_UNTRUSTED, StateLabel.DATA_INSUFFICIENT, StateLabel.DATA_STALE,
    StateLabel.WARMUP,
])
def test_non_actionable_states_never_capable(state):
    assert _mk(state).candidate_capable() is False


@pytest.mark.parametrize("state", [
    StateLabel.TRENDING, StateLabel.WEAKENING, StateLabel.BREAK_NOMINATED,
    StateLabel.BREAK_CONFIRMED, StateLabel.EXIT_PROPOSED,
])
def test_actionable_needs_trusted_required_families(state):
    # the label ALONE never authorizes: bare (no required families) fails closed...
    assert _mk(state).candidate_capable() is False
    # ...missing/untrusted a required family fails closed...
    untrusted = _mk(state, required_families=("flow",),
                    provenance={"flow": Provenance("pool_ticks", quality="degraded")})
    assert untrusted.candidate_capable() is False
    # ...only every required family trusted authorizes.
    ok = _mk(state, required_families=("flow",),
             provenance={"flow": Provenance("pool_ticks", quality="trusted")})
    assert ok.candidate_capable() is True


def test_marketstate_is_frozen():
    s = _mk(StateLabel.TRENDING)
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.nominal_price = 1.0


def test_executable_price_defaults_none_never_fabricated():
    # executable price is None until the fast lane runs under an approved provider
    s = _mk(StateLabel.TRENDING, nominal_price=0.001)
    assert s.executable_price is None


def test_provenance_carries_required_fields():
    p = Provenance(source="pool_ticks", event_time_window=("2026-07-23T00:00:00+00:00",
                                                           "2026-07-23T00:05:00+00:00"),
                   freshness_s=12.0, coverage=0.9, quality="trusted", feature_version="v1")
    assert p.quality == "trusted" and p.coverage == 0.9 and p.source == "pool_ticks"
