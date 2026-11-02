"""Same-state parity between the event-driven hot path and its pull fallback policy."""
import pytest

from sniper.analysis.exit_state_types import MarketState, Provenance, StateLabel
from sniper.services.exit_engine_hot import HotPositionEngine
from sniper.services.memecoin_exit_brain import exit_engine


def test_hot_v5_and_pull_v5_return_the_same_decision_for_the_same_state():
    families = ("price", "flow", "vol", "depth")
    provenance = {f: Provenance("pool_ticks", quality="trusted") for f in families}

    def state_fn(_features, *, now_ts):
        return MarketState(
            chain_id="solana", mint="M", pool="P", position_id=7,
            engine_version="market-state-v2", feature_version="v2",
            state=StateLabel.TRENDING, required_families=families, provenance=provenance,
            realized_vol=0.60, lower_high_state=True, net_flow_quote="-1")

    version, proposer = exit_engine("v5")
    hot = HotPositionEngine(
        7, {"trail_pct": 30.0, "coin_band": "nano", "opened_ts": 0.0},
        quote_side=0, state_fn=state_fn, decision_fn=proposer, engine_version=version)
    hot_decision = hot.on_tick(
        {"kind": "sol_swap", "block_ts": 100.0, "amount0": "100", "amount1": "-50"},
        now_ts=1524.0)
    pull_decision = proposer(hot.contract, hot.last_state)
    assert hot_decision == pull_decision
    assert hot_decision.proposed_fraction == pytest.approx(7 / 12)
