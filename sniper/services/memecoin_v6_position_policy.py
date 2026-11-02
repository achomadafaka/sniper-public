"""Private cadence-invariant policy helpers for the one public V6 position engine.

This is not a selectable engine. ``memecoin_v6_engine.step`` is the only public decision
transition.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from sniper.analysis.memecoin_v6_statistics import linear_percentile

EARLY_S = 3 * 3600.0
FRENZY_RATE = 0.10
CROWDED_SHARE = 1.0 / 3.0
VOL_P = 90
VOL_MIN_OBS = 12
MAX_TRANCHES = 2
NEG_STREAK_CONFIRM = 2
MAX_VOL_HISTORY = 64

REQUIRED_POINT_EVIDENCE = (
    "chain_vol_p90",
    "route_degraded",
    "source_wallet_exited",
    "wallet_tier_adjustment",
)

POLICY_ID = "v6-position-break-manager-v2"


@dataclass(frozen=True)
class V6State:
    tranches: int = 0
    awaiting: bool = False
    break_level: float | None = None
    neg_streak: int = 0
    recovered: bool = False
    vol_hist: tuple = field(default_factory=tuple)


def missing_evidence(obs) -> list:
    return sorted(field_name for field_name in REQUIRED_POINT_EVIDENCE if field_name not in obs)


def crash_score(obs, vol_hist) -> int:
    early = obs.get("held_s") is not None and obs["held_s"] < EARLY_S
    frenzy = obs["arrival_rate"] is not None and obs["arrival_rate"] >= FRENZY_RATE
    crowded = obs["buy_share"] is not None and obs["buy_share"] >= CROWDED_SHARE
    if len(vol_hist) >= VOL_MIN_OBS:
        vol_threshold = linear_percentile(vol_hist, VOL_P)
    else:
        vol_threshold = obs.get("chain_vol_p90")
    vol_shock = (obs["realized_vol"] is not None and vol_threshold is not None
                 and obs["realized_vol"] >= float(vol_threshold))
    return int(early) + int(frenzy) + int(crowded) + int(vol_shock)


def crash_tier(obs, vol_hist) -> tuple[int, int, bool]:
    """Return total score, actionable tier, and the diagnostic early flag.

    Early age is predictive context, not independent full-cut evidence. It can raise a quiet break
    to tier 1, but tier 2 requires two non-time severity features.
    """
    early = obs.get("held_s") is not None and obs["held_s"] < EARLY_S
    frenzy = obs["arrival_rate"] is not None and obs["arrival_rate"] >= FRENZY_RATE
    crowded = obs["buy_share"] is not None and obs["buy_share"] >= CROWDED_SHARE
    if len(vol_hist) >= VOL_MIN_OBS:
        vol_threshold = linear_percentile(vol_hist, VOL_P)
    else:
        vol_threshold = obs.get("chain_vol_p90")
    vol_shock = (obs["realized_vol"] is not None and vol_threshold is not None
                 and obs["realized_vol"] >= float(vol_threshold))
    severity = int(frenzy) + int(crowded) + int(vol_shock)
    total = int(early) + severity
    return total, (2 if severity >= 2 else 1 if severity == 1 or early else 0), early


def v6_step(state: V6State, obs) -> tuple:
    action, fraction, reasons = None, 0.0, []
    tranches, awaiting = state.tranches, state.awaiting
    break_level, neg_streak, recovered = state.break_level, state.neg_streak, state.recovered

    if obs.get("is_break"):
        recovered = False
        score, tier, early = crash_tier(obs, state.vol_hist)
        prior_adjustment = int(obs.get("wallet_tier_adjustment") or 0)
        source_exited_now = (obs.get("source_status") == "exited"
                             or obs.get("source_wallet_exited") is True)
        if source_exited_now or obs.get("route_degraded") is True:
            prior_adjustment = 1
        tier = max(0, min(2, tier + prior_adjustment))
        if tranches == 0:
            if tier >= 2:
                action, fraction = "cut", 1.0
                reasons = ["break", f"score:{score}", f"tier:{tier}"]
                if early:
                    reasons.append("early")
            else:
                action, fraction = "derisk", (0.5 if tier == 1 else 0.25)
                awaiting, break_level = True, obs["prior_low"]
                tranches += 1
                reasons = ["break", f"score:{score}", f"tier:{tier}", "tranche:1"]
                if early:
                    reasons.append("early")
        elif tranches == 1:
            action, fraction = "derisk", 0.5
            tranches += 1
            awaiting = False
            reasons = ["break_confirmation", "tranche:2"]
        elif tier >= 1:
            action, fraction = "cut", 1.0
            reasons = ["break", f"tier:{tier}", "budget_exhausted"]
    elif awaiting and tranches == 1:
        neg_streak = neg_streak + 1 if (obs["net"] is not None and obs["net"] < 0) else 0
        source_exited = (obs.get("source_status") == "exited"
                         or obs.get("source_wallet_exited") is True)
        route_degraded = obs.get("route_degraded") is True
        if break_level is not None and obs["price"] is not None and obs["price"] >= break_level:
            awaiting = False
            recovered = True
            reasons = ["recovery"]
        elif neg_streak >= NEG_STREAK_CONFIRM or source_exited or route_degraded:
            action, fraction = "derisk", 0.5
            tranches += 1
            awaiting = False
            reasons = ["confirmation",
                       ("neg_flow" if neg_streak >= NEG_STREAK_CONFIRM
                        else "source_full_exit" if source_exited else "route_degraded"),
                       "tranche:2"]

    vol_hist = state.vol_hist
    if obs["realized_vol"] is not None:
        vol_hist = (vol_hist + (obs["realized_vol"],))[-MAX_VOL_HISTORY:]
    new_state = replace(
        state, tranches=tranches, awaiting=awaiting, break_level=break_level,
        neg_streak=neg_streak, recovered=recovered, vol_hist=vol_hist,
    )
    return action, fraction, new_state, reasons
