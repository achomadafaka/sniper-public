"""Behavioral classifier — proposes whether a wallet's edge is ALGORITHMIC or
DISCRETIONARY, with a confidence, and a route hint. This is the classify→route
step: it turns the rich behavioral signals the dossier already computes (but
which the rule extractor discards) into an actual decision.

It is a HYPOTHESIS, not a verdict: it emits a label + confidence and routes
ambiguous wallets to human review. It never claims certainty — two wallets can
look identical in behavior. External-intel dependence (news/odds-driven entries)
is a SEPARATE detector that needs live event timestamps; it is not decided here
(Becker historical timestamps are too coarse — see the feasibility study).

Routes:
  algorithmic   → rebuild_rule  (deploy an independent rule as a paper strategy
                                  and compare its market selection to the wallet)
  discretionary → copy_mirror   (mirror the wallet's actual fills in paper;
                                  gate on copy-latency edge-retention)
  ambiguous     → human_review

Doctrine: strategy-intelligence. Output is decision-support for which PAPER
experiment to run — never a live trigger, sizing, or expected PnL. Forward paper
is the only proof of edge; this just picks the experiment.

Heuristic thresholds via env (codebase inline-getenv convention).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping

CLASSIFIER_VERSION = "algo_discovery_behavior_classifier_v1"

# Algorithmic-leaning vs discretionary-leaning thresholds (heuristic).
BURST_ALGO = float(os.getenv("ALGO_DISCOVERY_BURST_ALGO", "0.30"))        # >= → bot-like bursts
BURST_DISC = float(os.getenv("ALGO_DISCOVERY_BURST_DISC", "0.10"))        # <= → not bursty
FAST_SECONDS = float(os.getenv("ALGO_DISCOVERY_FAST_SECONDS", "60"))       # < → fast/reactive
SLOW_SECONDS = float(os.getenv("ALGO_DISCOVERY_SLOW_SECONDS", "86400"))    # > → slow/deliberate
HOURS_24_7 = float(os.getenv("ALGO_DISCOVERY_HOURS_24_7", "0.66"))         # >= frac of 24h → always-on
HOURS_CLUSTERED = float(os.getenv("ALGO_DISCOVERY_HOURS_CLUSTERED", "0.33"))
CV_UNIFORM = float(os.getenv("ALGO_DISCOVERY_CV_UNIFORM", "0.30"))         # <= → uniform sizing
CV_VARIABLE = float(os.getenv("ALGO_DISCOVERY_CV_VARIABLE", "1.0"))        # >= → highly variable
NET_DECIDE = int(os.getenv("ALGO_DISCOVERY_CLASSIFY_NET_DECIDE", "2"))     # |net votes| to leave ambiguous


@dataclass
class BehaviorClassification:
    classifier_version: str
    label: str          # algorithmic | discretionary | ambiguous | insufficient_data
    confidence: float   # 0..1
    route_hint: str     # rebuild_rule | copy_mirror | human_review
    net_score: int      # +algo .. -discretionary
    signals: dict[str, Any]


def _buy_rows(rows: list[Mapping], *, side_field: str = "side") -> list[Mapping]:
    out = []
    for r in rows or []:
        s = str(r.get(side_field) or "").upper()
        if s in ("BUY", ""):  # accept BUY; "" tolerated for side-agnostic tables
            out.append(r)
    return out


def _weighted_mean(rows: list[Mapping], value_field: str, weight_field: str) -> float | None:
    num = den = 0.0
    for r in rows:
        v, w = r.get(value_field), r.get(weight_field)
        if v is None or w is None:
            continue
        try:
            v = float(v); w = float(w)
        except (TypeError, ValueError):
            continue
        if w <= 0:
            continue
        num += v * w
        den += w
    return (num / den) if den > 0 else None


def _hour_coverage(rows: list[Mapping]) -> float | None:
    hours = {
        int(r["hour_utc"]) for r in (rows or [])
        if r.get("hour_utc") is not None and float(r.get("n_trades") or 0) > 0
    }
    return (len(hours) / 24.0) if rows else None


def classify_wallet_behavior(dossier_tables: Mapping[str, Any]) -> BehaviorClassification:
    """Classify a wallet's behavior from the dossier descriptor tables.

    Reads cadence (burst_share, inter-trade seconds), sizing (coeff_of_variation),
    and hour distribution (24h coverage). Each signal votes algorithmic (+1),
    discretionary (-1), or neutral; the net decides the label + a confidence =
    |net| / signals_available.
    """
    cadence = _buy_rows(list(dossier_tables.get("descriptor_cadence") or []))
    sizing = _buy_rows(list(dossier_tables.get("descriptor_sizing") or []))
    hours = list(dossier_tables.get("descriptor_hour_distribution") or [])

    burst = _weighted_mean(cadence, "burst_share", "n_intervals")
    inter_s = _weighted_mean(cadence, "median_seconds_between_trades", "n_intervals")
    cv = _weighted_mean(sizing, "coeff_of_variation", "n_fills")
    coverage = _hour_coverage(hours)

    votes: dict[str, int] = {}
    if burst is not None:
        votes["burst_share"] = 1 if burst >= BURST_ALGO else (-1 if burst <= BURST_DISC else 0)
    if inter_s is not None:
        votes["inter_trade_seconds"] = 1 if inter_s < FAST_SECONDS else (-1 if inter_s > SLOW_SECONDS else 0)
    if coverage is not None:
        votes["hour_coverage"] = 1 if coverage >= HOURS_24_7 else (-1 if coverage <= HOURS_CLUSTERED else 0)
    if cv is not None:
        votes["sizing_cv"] = 1 if cv <= CV_UNIFORM else (-1 if cv >= CV_VARIABLE else 0)

    signals = {
        "burst_share": burst, "median_seconds_between_trades": inter_s,
        "hour_coverage": coverage, "sizing_cv": cv, "votes": votes,
    }

    n = len(votes)
    if n == 0:
        return BehaviorClassification(CLASSIFIER_VERSION, "insufficient_data", 0.0,
                                      "human_review", 0, signals)
    net = sum(votes.values())
    confidence = min(1.0, abs(net) / n)

    if net >= NET_DECIDE:
        return BehaviorClassification(CLASSIFIER_VERSION, "algorithmic", confidence,
                                      "rebuild_rule", net, signals)
    if net <= -NET_DECIDE:
        return BehaviorClassification(CLASSIFIER_VERSION, "discretionary", confidence,
                                      "copy_mirror", net, signals)
    return BehaviorClassification(CLASSIFIER_VERSION, "ambiguous", confidence,
                                  "human_review", net, signals)


# --------------------------------------------------------------------------
# CSE-backed classification (supersedes the behavioral heuristic when a
# point-in-time MarketContext is available)
# --------------------------------------------------------------------------

CSE_CLASSIFIER_VERSION = "cse_counterfactual_selection_v1"

# CSE verdict.classification → (algo_discovery label, route_hint)
_CSE_CLASS_MAP = {
    "algorithmic_self_contained": ("algorithmic", "rebuild_rule"),
    "algorithmic_unobserved_signal": ("algorithmic", "human_review"),  # edge, no rule → human/AI
    "human_or_noise": ("discretionary", "copy_mirror"),
    "mixed": ("mixed", "copy_mirror"),
    "undetermined": ("insufficient_data", "human_review"),
}


def cse_classification(wallet_data: Mapping[str, Any], context: Any,
                       *, n_perm: int = 500) -> BehaviorClassification:
    """Run the Counterfactual Selection Engine and map its 4-dimension verdict to
    the algo_discovery classification shape. The CSE is the stronger classifier —
    it separates SELECTION automation from EXECUTION automation (the conflation
    that mislabels human-pick/bot-fill wallets), and refuses to claim 'algorithmic'
    without a real selection edge AND an extractable rule (G8). Requires a
    point-in-time MarketContext (the Becker index); the heuristic above is the
    fallback when no context is built."""
    from sniper.analysis.counterfactual_selection.runner import run_counterfactual_selection

    activity = list(wallet_data.get("activity_rows") or [])
    closed_positions = list(wallet_data.get("closed_positions") or [])
    out = run_counterfactual_selection(
        activity,
        context,
        n_perm=n_perm,
        closed_positions=closed_positions,
    )
    v = out["verdict"]
    cls = v.get("classification", "undetermined")
    label, route_hint = _CSE_CLASS_MAP.get(cls, ("insufficient_data", "human_review"))
    net = {"algorithmic": 2, "discretionary": -2, "mixed": -1}.get(label, 0)
    best = (out.get("rule") or {}).get("best")
    signals = {
        "source": "cse",
        "cse_classification": cls,
        "cse_route": v.get("route"),
        "dimensions": v.get("dimensions"),
        "confidence_reasons": v.get("qualitative_note"),
        "honest_caveat": v.get("honest_caveat"),
        "selection_edge": out.get("selection_edge"),
        "selection_edge_matched": out.get("selection_edge_matched"),
        "matching_balance": out.get("matching_balance"),
        "confound_flag": out.get("confound_flag"),
        "calibration": out.get("calibration"),
        "calibration_bands": out.get("calibration_bands"),
        "placebo": out.get("placebo"),
        "promotion": out.get("promotion"),
        "best_rule": {"feature": best.get("feature"), "op": best.get("op"),
                      "threshold": best.get("threshold")} if best else None,
        "candidate_quality": out.get("candidate_quality"),
        "n_decisions": out.get("candidate_quality", {}).get("n_picked"),
        "decision_signals": out.get("decision_signals"),
    }
    return BehaviorClassification(
        CSE_CLASSIFIER_VERSION, label, float(v.get("confidence", 0.0) or 0.0),
        route_hint, net, signals,
    )
