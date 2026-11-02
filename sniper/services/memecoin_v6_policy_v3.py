"""The v3 adaptive policy for the Radar paper cohort."""
from __future__ import annotations

from sniper.services.memecoin_v6_engine import V6Policy

POLICY_V3_ID = "v6-position-engine-v3"

V3_PARAMS = {
    "take_profit_bps": 1_200,
    "take_profit_fraction_ppm": 500_000,
    "trail_giveback_fraction": 0.08,
    "exact_stop_bps": -1_000,
    "exact_stop_confirmations": 2,
    "time_cap_s": 120.0,
    "time_extend_s": 300.0,
    "lp_minor_fraction": 0.05,
    "lp_catastrophic_fraction": 0.30,
    "adverse_quote_ratio_limit": 10.0,
    "route_absent_min_checks": 4,
    "route_absent_min_s": 60.0,
    "max_tranches": 2,
}

V3_ORDERED_RULES = (
    "lp_loss", "creator_sale", "exact_stop", "route_loss", "time_cap",
    "trail", "take_profit", "staged_break",
)

V3_REQUIRED_EVIDENCE = (
    "executable_price", "quote_status", "creator_sale_since_entry",
    "route_degraded", "chain_vol_p90",
)

_V3_DOCUMENT = {
    "engine_policy_id": POLICY_V3_ID,
    "engine_evidence_mode": "per_rule",
    "engine_supported_networks": ["4663", "solana"],
    "evidence_semantics": {
        "route_degraded_statuses": ["route_absent"],
        "cancel_active_intent_on_healthy_no_break": True,
        "observation_identity": "semantic_sequence_v1",
        "unknown_quote_units_fail_closed": True,
        "trail_requires_filled_take_profit": True,
        "shared_tranche_budget": True,
    },
    "ordered_rules": list(V3_ORDERED_RULES),
    "required_evidence": list(V3_REQUIRED_EVIDENCE),
    "terminal_parameters": dict(sorted(V3_PARAMS.items())),
}

POLICY_V3 = V6Policy(
    policy_id=POLICY_V3_ID,
    document=_V3_DOCUMENT,
    required_evidence=V3_REQUIRED_EVIDENCE,
    ordered_rules=V3_ORDERED_RULES,
    params=V3_PARAMS,
)
