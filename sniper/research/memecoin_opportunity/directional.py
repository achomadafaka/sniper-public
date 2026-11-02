"""Transparent Level-1 Radar movement rules from the approved design."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from .venue_policy import scoreable_venues
from .state import PUMP_SOL_QUOTE_MINT


class RadarZone(str, Enum):
    OUTER = "OUTER"
    TRACKING = "TRACKING"
    APPROACHING = "APPROACHING"
    INNER = "INNER"
    CENTER = "CENTER"


FORWARD_CANDIDATE_RULES = (
    "pump-clean-start-v1",
    "pump-ignition-v1",
    "pump-confirmation-v1",
)

# `pump-curve-break-v1` gated on `immediate_round_trip_return_bps` in
# [-3500, -1200). Under v4 that field is the modeled round-trip cost, which for
# the $25 primary clip is ~[-300, -190] — the band is unreachable, so the arm
# has been structurally dead since the v4 cutover. Retired rather than ported:
# its band was a 2s price move wearing a cost costume and it never had evidence.
RETIRED_CANDIDATE_RULES = ("pump-curve-break-v1",)

CONFIRMATION_MICROSCOPE_RULES = (
    "pump-confirmation-truth-v2",
    "pump-confirmation-independent-v1",
    "pump-confirmation-clean-v1",
    "pump-confirmation-low-friction-v1",
    "pump-confirmation-delayed-v1",
)

CONFIRMATION_V3_RULES = (
    "pump-confirmation-truth-v3",
    "pump-confirmation-delayed-v2",
)
CONFIRMATION_V4_RULES = (
    "pump-confirmation-truth-v4",
    "route-control-v2",
)

# The creator-history arm. The +1,453 bps separation it was added on was RETRACTED on
# 2026-07-30 for target leakage: priors were ordered by alert time, so a prior whose
# outcome resolved AFTER this decision still counted. Recomputed causally it is +392 bps
# at t=+0.70 over 193 pairs -- no effect. The pre-v4 "replication" carried the same leak
# and is not independent evidence. See the 2026-07-29 measurement-integrity design.
#
# The arm's own forward store is causal by construction (op_creator_outcomes is credited
# only when an outcome finalizes), so the arm does not leak. But it is an UNTESTED
# HYPOTHESIS ON A ZERO CLOCK, not an evidence-backed arm. Do not restate the retracted
# numbers as support for keeping it.
CREATOR_PRIOR_RULES = ("pump-creator-prior-v1",)

# Forward experiment: the breadth gate raised from 5 to 10 eligible buyers. In the
# permissive control population buyers >=10 ran +660 against -259 (t=+3.68) while curve
# size alone did not separate (t=+1.50), and few-buyer launches were far more
# concentrated (median HHI 5,061 bps against 1,132). It does NOT clear the matched gate
# retrospectively -- 26 pairs at +206 with a creator interval of [-324,+807], decaying
# to +49 as the band widens -- so v5 REPLACES v4 on a fresh clock rather than mutating
# it, which would silently redefine what v4's accrued evidence meant.
CONFIRMATION_V5_RULES = ("pump-confirmation-truth-v5",)
CONFIRMATION_V5_MIN_ELIGIBLE_BUYERS = 10

# v6 = v5 plus two corrections that the route-priced paper cohort forced. v5 keeps its
# history and stops firing; raising its bar in place would redefine its accrued evidence.
#
# 1. `launch_mode == "standard"`. `_launch_mode_cost` below records the belief that the
#    cashback tier is the cheap one -- 95 bps fee, -191 bps modeled cost, against 125 bps
#    and -250 for standard. The MEASURED round trip refutes it. Over 5,026 alerts that
#    carry both numbers, the median observed pair return is -456 bps for cashback and
#    -448 bps for standard: cashback is not cheaper, it is 8 bps worse, and its model
#    error is larger (-265 bps against -198 bps). Cashback was 60% of v5 fires and carries
#    the loss under every fill contract. Route-priced at the expected fill it ran -8.63%
#    mean over 31 results while standard ran +2.37% over 21.
# 2. `observed_pair_return_bps >= -500`. This is a solvency floor, not a prediction. The
#    correlation between the decision-time round trip and the 5-second route mark is
#    -0.03 on standard, so the band does not forecast anything. What it does is refuse a
#    position that already sits below the -800 bps stop before the token moves. -500 bps
#    leaves 300 bps of room, which covers p90 adverse execution on both legs (2 x 122 bps
#    measured on 31 funded Solana chain entries).
#
# The gate reads the MEASURED pair, not `immediate_round_trip_return_bps`. That field
# carries `modeled_round_trip_cost_bps` on the v4 receipt path, and the model prices the
# round trip 198 to 1,566 bps too cheap depending on the launch mode.
CONFIRMATION_V6_RULES = ("pump-confirmation-truth-v6",)
CONFIRMATION_V6_LAUNCH_MODE = "standard"
CONFIRMATION_V6_MIN_OBSERVED_ROUND_TRIP_BPS = -500

VENUE_CONFIRMATION_RULES = {
    "bags_robinhood_4663": "bags-confirmation-v1",
    "pons_v1_4663": "pons-confirmation-v1",
}
VENUE_CONTROL_RULES = {
    "bags_robinhood_4663": "bags-route-control-v1",
    "pons_v1_4663": "pons-route-control-v1",
}

# Scoreable venues only: a retired venue must not keep an arm identity in the registry.
_ALL_ADAPTIVE_V3_BY_VENUE = {
    "pumpfun_solana": "pump-adaptive-v3",
    "bags_robinhood_4663": "bags-adaptive-v3",
    "pons_v1_4663": "pons-adaptive-v3",
}
ADAPTIVE_V3_BY_VENUE = {
    venue: rule
    for venue, rule in _ALL_ADAPTIVE_V3_BY_VENUE.items()
    if venue in frozenset(v.value for v in scoreable_venues())
}
ADAPTIVE_V3_RULES = tuple(sorted(ADAPTIVE_V3_BY_VENUE.values()))
ADAPTIVE_V3_MIN_AGE_MS = 15_000
ADAPTIVE_V3_MAX_AGE_MS = 35_000
ADAPTIVE_V3_MAX_LP_REMOVED_BPS = 500
ADAPTIVE_V3_MAX_QUOTE_AGE_MS = 2_000

# Rules whose outcomes are measured past the 60s primary horizon. `outcome_tracker` and
# `retention` must agree: a launch pruned before its extended outcome resolves loses the
# measurement. They kept separate copies and v5 was in one of them only.
EXTENDED_HORIZON_RULES = (
    *FORWARD_CANDIDATE_RULES,
    *CONFIRMATION_MICROSCOPE_RULES,
    *CONFIRMATION_V3_RULES,
    *CONFIRMATION_V4_RULES,
    *CONFIRMATION_V5_RULES,
    *CONFIRMATION_V6_RULES,
    *ADAPTIVE_V3_RULES,
)


@dataclass(frozen=True)
class DirectionalVerdict:
    zone: RadarZone
    would_fire: bool
    reason_codes: tuple[str, ...]
    rule_version: str = "level1-v2"
    truth_label: str = "EXPLORATORY — EDGE NOT YET ESTABLISHED"


def evaluate_level1(
    current: Mapping[str, object],
    *,
    prior: Mapping[str, object] | None,
) -> DirectionalVerdict:
    blockers = tuple(str(code) for code in (current.get("blocker_codes") or ()))
    if not current.get("eligible"):
        return DirectionalVerdict(RadarZone.OUTER, False, blockers or ("INELIGIBLE",))
    if prior is None:
        return DirectionalVerdict(RadarZone.TRACKING, False, ("MISSING_PRIOR_INTERVAL",))

    reasons: list[str] = []
    capital_rising = int(current.get("capital_velocity_atomic_per_s") or 0) > int(
        prior.get("capital_velocity_atomic_per_s") or 0
    )
    buyers_rising = int(current.get("buyer_velocity_per_s_micros") or 0) > int(
        prior.get("buyer_velocity_per_s_micros") or 0
    )
    route = bool(current.get("local_exit_exists"))
    if not capital_rising:
        reasons.append("CAPITAL_VELOCITY_NOT_RISING")
    if not buyers_rising:
        reasons.append("BUYER_VELOCITY_NOT_RISING")
    if not route:
        reasons.append("LOCAL_EXIT_UNAVAILABLE")
    if not (capital_rising and buyers_rising and route):
        return DirectionalVerdict(RadarZone.TRACKING, False, tuple(reasons))

    current_hhi = current.get("effective_buyer_hhi_bps")
    prior_hhi = prior.get("effective_buyer_hhi_bps")
    concentration_ok = (
        current_hhi is not None and prior_hhi is not None and int(current_hhi) <= int(prior_hhi)
    )
    creator_clean = not bool(current.get("creator_sale_since_prior"))
    round_trip = current.get("immediate_round_trip_return_bps")
    runway_ok = round_trip is not None and int(round_trip) > -800
    if not concentration_ok:
        reasons.append("CONCENTRATION_WORSENED")
    if not creator_clean:
        reasons.append("CREATOR_LINKED_SALE")
    if not runway_ok:
        reasons.append("ROUND_TRIP_BELOW_LOSS_BARRIER")
    if not (concentration_ok and creator_clean and runway_ok):
        return DirectionalVerdict(RadarZone.APPROACHING, False, tuple(reasons))

    remote_entry = bool(current.get("remote_entry_quote_ok"))
    remote_exit = bool(current.get("remote_sell_simulation_ok"))
    if remote_entry and remote_exit:
        return DirectionalVerdict(RadarZone.CENTER, True, ("REMOTE_ROUND_TRIP_EXECUTABLE",))
    if not remote_entry:
        reasons.append("REMOTE_ENTRY_QUOTE_MISSING")
    if not remote_exit:
        reasons.append("REMOTE_SELL_SIMULATION_MISSING")
    remote_blocker = current.get("remote_quote_blocker_code")
    if remote_blocker:
        reasons.append(str(remote_blocker))
    return DirectionalVerdict(RadarZone.INNER, False, tuple(reasons))


def evaluate_forward_arm(
    current: Mapping[str, object],
    *,
    prior: Mapping[str, object] | None,
) -> DirectionalVerdict | None:
    """Classify a point-in-time Pump.fun state into one forward paper arm."""
    if current.get("venue_id") != "pumpfun_solana":
        return None
    if not current.get("eligible") or prior is None:
        return None

    capital_rising = int(current.get("capital_velocity_atomic_per_s") or 0) > int(
        prior.get("capital_velocity_atomic_per_s") or 0
    )
    buyers_rising = int(current.get("buyer_velocity_per_s_micros") or 0) > int(
        prior.get("buyer_velocity_per_s_micros") or 0
    )
    current_hhi = current.get("effective_buyer_hhi_bps")
    prior_hhi = prior.get("effective_buyer_hhi_bps")
    concentration_ok = (
        current_hhi is not None and prior_hhi is not None and int(current_hhi) <= int(prior_hhi)
    )
    round_trip = current.get("immediate_round_trip_return_bps")
    shared_safety = (
        capital_rising
        and buyers_rising
        and bool(current.get("local_exit_exists"))
        and concentration_ok
        and not bool(current.get("creator_sale_since_prior"))
        and bool(current.get("remote_entry_quote_ok"))
        and bool(current.get("remote_sell_simulation_ok"))
    )
    if not shared_safety or round_trip is None:
        return None

    progress_bps = int(current.get("progress_bps") or 0)
    launch_age_ms = int(current.get("launch_age_ms") or 0)
    buyer_count = int(current.get("effective_buyer_count") or 0)
    quote_drag_bps = int(round_trip)
    if (
        progress_bps <= 1_000
        and launch_age_ms <= 10_000
        and 2 <= buyer_count <= 4
        and int(current_hhi) <= 4_000
        and -500 <= quote_drag_bps <= 0
    ):
        return DirectionalVerdict(
            RadarZone.CENTER,
            True,
            ("PUMP_CLEAN_START_FORWARD_ARM",),
            rule_version="pump-clean-start-v1",
        )

    quote_cost_ok = -1_200 <= quote_drag_bps <= 0
    if (
        progress_bps >= 2_000
        and quote_cost_ok
        and launch_age_ms <= 10_000
        and current.get("quote_mint") == PUMP_SOL_QUOTE_MINT
        and int(current.get("capital_velocity_atomic_per_s") or 0) >= 500_000_000
    ):
        return DirectionalVerdict(
            RadarZone.CENTER,
            True,
            ("PUMP_IGNITION_FORWARD_ARM",),
            rule_version="pump-ignition-v1",
        )
    if progress_bps >= 2_000 and quote_cost_ok and 10_000 < launch_age_ms <= 60_000 and buyer_count >= 5:
        return DirectionalVerdict(
            RadarZone.CENTER,
            True,
            ("PUMP_BUYER_CONFIRMATION_FORWARD_ARM",),
            rule_version="pump-confirmation-v1",
        )
    return None


def evaluate_confirmation_arms(
    current: Mapping[str, object],
    *,
    prior: Mapping[str, object] | None,
) -> tuple[DirectionalVerdict, ...]:
    """Return forward-only paired Confirmation receipts from one verified quote."""
    if current.get("venue_id") != "pumpfun_solana" or not current.get("eligible") or prior is None:
        return ()
    capital_rising = int(current.get("capital_velocity_atomic_per_s") or 0) > int(
        prior.get("capital_velocity_atomic_per_s") or 0
    )
    buyers_rising = int(current.get("buyer_velocity_per_s_micros") or 0) > int(
        prior.get("buyer_velocity_per_s_micros") or 0
    )
    current_hhi = current.get("effective_buyer_hhi_bps")
    prior_hhi = prior.get("effective_buyer_hhi_bps")
    concentration_ok = (
        current_hhi is not None and prior_hhi is not None and int(current_hhi) <= int(prior_hhi)
    )
    quote_drag = current.get("immediate_round_trip_return_bps")
    base = (
        capital_rising
        and buyers_rising
        and concentration_ok
        and bool(current.get("local_exit_exists"))
        and not bool(current.get("creator_sale_since_prior"))
        and bool(current.get("remote_entry_quote_ok"))
        and bool(current.get("remote_sell_simulation_ok"))
        and int(current.get("progress_bps") or 0) >= 2_000
        and 10_000 < int(current.get("launch_age_ms") or 0) <= 60_000
        and int(current.get("effective_buyer_count") or 0) >= 5
        and quote_drag is not None
        and -1_200 <= int(quote_drag) <= 0
    )
    if not base:
        return ()
    arms = [DirectionalVerdict(
        RadarZone.CENTER,
        True,
        ("PUMP_CONFIRMATION_TRUTH_V2",),
        rule_version="pump-confirmation-truth-v2",
    )]
    independent = int(current.get("new_eligible_buyers") or 0) >= 2
    if independent:
        arms.append(DirectionalVerdict(
            RadarZone.CENTER,
            True,
            ("PUMP_CONFIRMATION_INDEPENDENT",),
            rule_version="pump-confirmation-independent-v1",
        ))
    sell_pressure = current.get("sell_pressure_bps")
    if independent and sell_pressure is not None and int(sell_pressure) <= 2_500:
        arms.append(DirectionalVerdict(
            RadarZone.CENTER,
            True,
            ("PUMP_CONFIRMATION_CLEAN",),
            rule_version="pump-confirmation-clean-v1",
        ))
    fee_bps = int(current.get("effective_fee_bps") or 125)
    if int(quote_drag) >= -(8 * fee_bps):
        arms.append(DirectionalVerdict(
            RadarZone.CENTER,
            True,
            ("PUMP_CONFIRMATION_LOW_FRICTION",),
            rule_version="pump-confirmation-low-friction-v1",
        ))
    return tuple(arms)


def evaluate_creator_prior_arm(
    current: Mapping[str, object],
    *,
    prior: Mapping[str, object] | None,
) -> DirectionalVerdict | None:
    """Fire on the permissive route base when the creator's own record is positive.

    Preregistered exactly as measured -- at least one scored prior outcome and a
    positive mean. The split was discovered on the sign of the mean and is forward
    tested on the sign of the mean; there is no threshold to tune, which is the point.

    Disjoint from `route-control-v2` by construction. Emitting both on one launch would
    produce two receipts from one decision with identical outcomes, which is not a
    control at all -- the matched-control machinery pairs this against control rows from
    other launches inside the preregistered bands.
    """
    if current.get("venue_id") != "pumpfun_solana":
        return None
    if not current.get("eligible") or prior is None:
        return None
    if not (
        bool(current.get("local_exit_exists"))
        and bool(current.get("remote_entry_quote_ok"))
        and bool(current.get("remote_sell_simulation_ok"))
    ):
        return None
    samples = int(current.get("creator_prior_samples") or 0)
    mean_bps = current.get("creator_prior_mean_bps")
    if samples < 1 or mean_bps is None or int(mean_bps) <= 0:
        return None
    return DirectionalVerdict(
        RadarZone.CENTER,
        True,
        ("PUMP_CREATOR_PRIOR_POSITIVE",),
        rule_version="pump-creator-prior-v1",
        truth_label="CREATOR PRIOR — DIAGNOSTIC PAPER",
    )


def evaluate_confirmation_v5(
    current: Mapping[str, object],
    *,
    prior: Mapping[str, object] | None,
) -> DirectionalVerdict | None:
    """v4's predicate with the breadth gate raised to 10 eligible buyers."""
    if evaluate_confirmation_v3(current, prior=prior) is None:
        return None
    if int(current.get("effective_buyer_count") or 0) < CONFIRMATION_V5_MIN_ELIGIBLE_BUYERS:
        return None
    return DirectionalVerdict(
        RadarZone.CENTER,
        True,
        ("PUMP_CONFIRMATION_TRUTH_V5",),
        rule_version="pump-confirmation-truth-v5",
        truth_label="CONFIRMATION V5 — DIAGNOSTIC PAPER",
    )


def evaluate_confirmation_v6(
    current: Mapping[str, object],
    *,
    prior: Mapping[str, object] | None,
) -> DirectionalVerdict | None:
    """v5's predicate on standard launches only, above the measured solvency floor."""
    if evaluate_confirmation_v5(current, prior=prior) is None:
        return None
    if current.get("launch_mode") != CONFIRMATION_V6_LAUNCH_MODE:
        return None
    observed = current.get("observed_pair_return_bps")
    if observed is None or int(observed) < CONFIRMATION_V6_MIN_OBSERVED_ROUND_TRIP_BPS:
        return None
    return DirectionalVerdict(
        RadarZone.CENTER,
        True,
        ("PUMP_CONFIRMATION_TRUTH_V6",),
        rule_version="pump-confirmation-truth-v6",
        truth_label="CONFIRMATION V6 — DIAGNOSTIC PAPER",
    )


def evaluate_venue_confirmation(
    current: Mapping[str, object],
    *,
    prior: Mapping[str, object] | None,
) -> DirectionalVerdict | None:
    """Apply the common confirmation hypothesis with venue readiness checks."""
    venue = str(current.get("venue_id") or "")
    rule = VENUE_CONFIRMATION_RULES.get(venue)
    if rule is None or not current.get("eligible") or prior is None:
        return None
    if venue == "bags_robinhood_4663" and int(current.get("progress_bps") or 0) < 2_000:
        return None
    if venue == "pons_v1_4663" and current.get("lifecycle") != "active_pool":
        return None
    current_hhi = current.get("effective_buyer_hhi_bps")
    prior_hhi = prior.get("effective_buyer_hhi_bps")
    observed = current.get("observed_pair_return_bps")
    sell_pressure = current.get("sell_pressure_bps")
    common_pass = (
        int(current.get("capital_velocity_atomic_per_s") or 0)
        > int(prior.get("capital_velocity_atomic_per_s") or 0)
        and int(current.get("buyer_velocity_per_s_micros") or 0)
        > int(prior.get("buyer_velocity_per_s_micros") or 0)
        and current_hhi is not None
        and prior_hhi is not None
        and int(current_hhi) <= int(prior_hhi)
        and int(current.get("effective_buyer_count") or 0) >= 10
        and int(current.get("new_eligible_buyers") or 0) >= 2
        and sell_pressure is not None
        and int(sell_pressure) <= 2_500
        and not bool(current.get("creator_sale_since_prior"))
        and bool(current.get("local_exit_exists"))
        and bool(current.get("remote_entry_quote_ok"))
        and bool(current.get("remote_sell_simulation_ok"))
        and 10_000 < int(current.get("launch_age_ms") or 0) <= 60_000
        and observed is not None
        and int(observed) >= CONFIRMATION_V6_MIN_OBSERVED_ROUND_TRIP_BPS
    )
    if not common_pass:
        return None
    return DirectionalVerdict(
        RadarZone.CENTER,
        True,
        ("VENUE_CONFIRMATION_V1",),
        rule_version=rule,
        truth_label="VENUE CONFIRMATION — DIAGNOSTIC PAPER",
    )


def evaluate_adaptive_v3(
    current: Mapping[str, object],
    *,
    prior: Mapping[str, object] | None,
) -> DirectionalVerdict | None:
    """Apply the adaptive paper entry predicate to each approved venue."""
    venue = str(current.get("venue_id") or "")
    rule = ADAPTIVE_V3_BY_VENUE.get(venue)
    if rule is None or not current.get("eligible") or prior is None:
        return None
    if venue == "bags_robinhood_4663" \
            and int(current.get("progress_bps") or 0) < 2_000:
        return None
    if venue == "pons_v1_4663" and current.get("lifecycle") != "active_pool":
        return None
    age_ms = int(current.get("launch_age_ms") or 0)
    if not ADAPTIVE_V3_MIN_AGE_MS < age_ms <= ADAPTIVE_V3_MAX_AGE_MS:
        return None
    removed = current.get("liquidity_removed_fraction_bps")
    if removed is None or int(removed) >= ADAPTIVE_V3_MAX_LP_REMOVED_BPS:
        return None
    acceleration = current.get("capital_acceleration_atomic_per_s2")
    ceiling = current.get("frenzy_ceiling_atomic_per_s2")
    if acceleration is None or ceiling is None \
            or int(acceleration) > int(ceiling):
        return None
    quote_age_ms = current.get("quote_age_ms")
    if quote_age_ms is None or int(quote_age_ms) >= ADAPTIVE_V3_MAX_QUOTE_AGE_MS:
        return None
    current_hhi = current.get("effective_buyer_hhi_bps")
    prior_hhi = prior.get("effective_buyer_hhi_bps")
    observed = current.get("observed_pair_return_bps")
    sell_pressure = current.get("sell_pressure_bps")
    common_pass = (
        int(current.get("capital_velocity_atomic_per_s") or 0)
        > int(prior.get("capital_velocity_atomic_per_s") or 0)
        and int(current.get("buyer_velocity_per_s_micros") or 0)
        > int(prior.get("buyer_velocity_per_s_micros") or 0)
        and current_hhi is not None
        and prior_hhi is not None
        and int(current_hhi) <= int(prior_hhi)
        and int(current.get("effective_buyer_count") or 0) >= 10
        and int(current.get("new_eligible_buyers") or 0) >= 2
        and sell_pressure is not None
        and int(sell_pressure) <= 2_500
        and not bool(current.get("creator_sale_since_prior"))
        and bool(current.get("local_exit_exists"))
        and bool(current.get("remote_entry_quote_ok"))
        and bool(current.get("remote_sell_simulation_ok"))
        and observed is not None
        and int(observed) >= CONFIRMATION_V6_MIN_OBSERVED_ROUND_TRIP_BPS
    )
    if not common_pass:
        return None
    return DirectionalVerdict(
        RadarZone.CENTER,
        True,
        ("ADAPTIVE_V3",),
        rule_version=rule,
        truth_label="ADAPTIVE V3 - DIAGNOSTIC PAPER",
    )


def confirmation_v3_labels(
    current: Mapping[str, object],
    *,
    prior: Mapping[str, object] | None,
) -> dict[str, bool]:
    """Classify one v3 receipt without creating duplicate paper arms."""
    independent = int(current.get("new_eligible_buyers") or 0) >= 2
    current_hhi = current.get("effective_buyer_hhi_bps")
    prior_hhi = prior.get("effective_buyer_hhi_bps") if prior is not None else None
    sell_pressure = current.get("sell_pressure_bps")
    clean = (
        independent
        and current_hhi is not None
        and prior_hhi is not None
        and int(current_hhi) <= int(prior_hhi)
        and sell_pressure is not None
        and int(sell_pressure) <= 2_500
        and not bool(current.get("creator_sale_since_prior"))
    )
    quote_drag = current.get("immediate_round_trip_return_bps")
    fee_bps = current.get("effective_fee_bps")
    low_friction = (
        quote_drag is not None
        and fee_bps is not None
        and int(quote_drag) >= -(8 * int(fee_bps))
    )
    return {
        "confirmation_independent_pass": independent,
        "confirmation_clean_pass": clean,
        "confirmation_low_friction_pass": low_friction,
    }


def evaluate_confirmation_v3(
    current: Mapping[str, object],
    *,
    prior: Mapping[str, object] | None,
) -> DirectionalVerdict | None:
    """Return one forward-only v3 receipt for the preregistered truth predicate."""
    if not evaluate_confirmation_arms(current, prior=prior):
        return None
    return DirectionalVerdict(
        RadarZone.CENTER,
        True,
        ("PUMP_CONFIRMATION_TRUTH_V3",),
        rule_version="pump-confirmation-truth-v3",
    )
