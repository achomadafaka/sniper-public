from __future__ import annotations
from .models import CandidateSpec
from .store import enroll_candidate


EXIT_POLICY = {
    "tp_pct": 60, "sl_pct": 50, "sl_confirm": 4, "timecap_s": 86400,
    "mirror": False,
}
SIZING = {"mode": "fixed", "notional_usd": 100, "execution_mode": "paper"}
COSTS = {"entry_bps": 100, "exit_bps": 100}
CONTROLS = {"enabled": True, "window_s": 1800, "same_network": True,
            "same_market_cap_band": True, "past_only": True}
GATES = {
    "base_alpha": 0.05,
    "read_closes": 40, "read_coins": 20, "read_days": 10,
    "kill_pnl": -150, "kill_closes": 25,
    "starvation_days": 30, "starvation_coins": 10,
}


def _specs(now_iso):
    return [
        CandidateSpec(
            name="cluster_momentum_v0",
            predicate={
                "window_s": 1800, "freshness_s": 120,
                "clauses": [
                    {"field": "event_type", "op": "eq", "value": "large_buy"},
                    {"field": "market_cap", "op": "lt", "value": 5_000_000},
                    {"field": "was_curated", "op": "eq", "value": True},
                    {"field": "distinct_buyer_count", "op": "gte", "value": 2},
                ],
            },
            exit_policy=EXIT_POLICY, sizing=SIZING, costs=COSTS, controls=CONTROLS,
            gates=GATES, discovery_cutoff=now_iso,
            notes="Frozen v0 seed; machinery validation only, no claimed edge.",
        ),
        CandidateSpec(
            name="thesis_momentum_v0",
            predicate={
                "window_s": 0, "freshness_s": 120,
                "clauses": [
                    {"field": "event_type", "op": "eq", "value": "thesis_created"},
                    {"field": "market_cap", "op": "lt", "value": 5_000_000},
                    {"field": "was_curated", "op": "eq", "value": True},
                    {"field": "thesis_stance", "op": "eq", "value": "winning"},
                    {"field": "thesis_score", "op": "gte", "value": 20},
                ],
            },
            exit_policy=EXIT_POLICY, sizing=SIZING, costs=COSTS, controls=CONTROLS,
            gates=GATES, discovery_cutoff=now_iso,
            notes="Frozen v0 seed; objective poster PnL stance, not text sentiment.",
        ),
    ]


def seed_v0_candidates(conn, now_iso):
    return [enroll_candidate(conn, spec, now_iso) for spec in _specs(now_iso)]
