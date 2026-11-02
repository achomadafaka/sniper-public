"""Forward-tracking job (D4) — turn queued paper-card enrollments into verdicts.

Pure verdict core + a thin orchestrator. For each enrolled card, re-run its producer on
the wallet's POST-enrolled_at positions (PIT-anchored, handed in by an injected provider)
and apply the pre-registered rules:

  - kill   : n >= min_forward_n AND effect <= kill_threshold        (decisive negative)
  - pass   : evidence_state=="supported" AND effect >= max(success, enrolled_effect*decay)
  - promote: two non-overlapping passing batches (sustained replication)  [S1]
  - finalize at horizon: completed_no_edge (n>=mn) / completed_inconclusive (n<mn)
  - otherwise stay active (awaiting_data / confirming)

No new card logic (reuses the producers via METRIC_PRODUCERS), no metric substitution
(unknown metric -> untrackable, no verdict), no cross-card logic, no operator selection,
no live orders, no sizing. Spec: docs/superpowers/specs/2026-05-30-forward-tracking-job-d4.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from sniper.analysis.thesis_cards.contract import ThesisCard
from sniper.analysis.thesis_cards.enrollment import PaperState
from sniper.analysis.thesis_cards.sizing_card import sizing_card
from sniper.analysis.thesis_cards.timing_card import timing_card

# Dispatch on the STORED forward_test_design.metric -> the producer that computes it.
METRIC_PRODUCERS: dict[str, Callable[[Sequence[Mapping[str, Any]]], ThesisCard]] = {
    "calibration_edge": sizing_card,
    "dead_zone_drift_gap": timing_card,
}

DEFAULT_MAX_TRACKING_DAYS = 90
DEFAULT_DECAY_FRACTION = 0.5
_DAY = 86400


@dataclass(frozen=True)
class ForwardDecision:
    new_state: PaperState         # state to write (active|killed|completed|promoted)
    verdict: str | None
    forward_metric: dict


def _prior(row: Mapping[str, Any]) -> dict:
    return dict(row.get("forward_metric") or {})


def _horizon_elapsed(row: Mapping[str, Any], now: int, max_tracking_days: int) -> bool:
    ea = row.get("enrolled_at")
    return ea is not None and (now - int(ea)) >= max_tracking_days * _DAY


def evaluate(
    row: Mapping[str, Any],
    window: Sequence[Mapping[str, Any]] | None,
    *,
    now: int,
    max_tracking_days: int = DEFAULT_MAX_TRACKING_DAYS,
    decay_fraction: float = DEFAULT_DECAY_FRACTION,
    producers: Mapping[str, Callable[[Sequence[Mapping[str, Any]]], ThesisCard]] = METRIC_PRODUCERS,
) -> ForwardDecision:
    """Pure: (current row, the next forward batch) -> the next state + forward_metric."""
    ftd = row.get("forward_test_design") or {}
    metric = ftd.get("metric")
    prior = _prior(row)
    pass_count = int(prior.get("pass_count", 0))
    last_end = prior.get("last_pass_window_end") or row.get("enrolled_at")
    H = _horizon_elapsed(row, now, max_tracking_days)

    producer = producers.get(metric)
    if producer is None:                                  # untrackable -> NO substitution
        return ForwardDecision(row["state"], None,
                               {**prior, "metric": metric, "status": "untrackable_metric",
                                "pass_count": pass_count, "last_pass_window_end": last_end})

    mn = int(ftd.get("min_forward_n", 20))
    success = float(ftd.get("success_threshold", 0.0))
    kill = float(ftd.get("kill_threshold", 0.0))
    enrolled_effect = row.get("enrolled_effect")
    # decay floor: promotion needs the forward effect to retain its share of the in-sample
    # effect; with no baseline a card cannot pass (cannot verify non-decay) [S2].
    decay_floor = (enrolled_effect * decay_fraction) if enrolled_effect is not None else float("inf")

    def fm(status: str, *, n=0, value=None, p=None, est=None, pc=pass_count, le=last_end) -> dict:
        return {"metric": metric, "value": value, "p_value": p, "evidence_state": est,
                "forward_n": n, "pass_count": pc, "last_pass_window_end": le,
                "horizon_elapsed": H, "status": status}

    if not window:                                        # provider can't answer yet
        if H:
            return ForwardDecision("completed", "completed_inconclusive_insufficient_data",
                                   fm("final"))
        return ForwardDecision("active", None, fm("awaiting_data"))

    fwd = producer(window)
    n, effect, est = fwd.n, fwd.effect, fwd.evidence_state

    # 1. kill — requires n>=mn AND a decisive negative effect (no tiny-n / bare-sign kill) [S3]
    if n >= mn and effect is not None and effect <= kill:
        return ForwardDecision("killed", "killed_below_kill_threshold",
                               fm("final", n=n, value=effect, p=fwd.p_value, est=est))

    # 2. pass this batch?
    is_pass = (est == "supported" and n >= mn and effect is not None
               and effect >= success and effect >= decay_floor)
    if is_pass:
        new_pc = pass_count + 1
        if new_pc >= 2:                                   # two sustained non-overlapping passes
            return ForwardDecision("promoted", "promoted_sustained",
                                   fm("final", n=n, value=effect, p=fwd.p_value, est=est,
                                      pc=new_pc, le=now))
        return ForwardDecision("active", None,
                               fm("confirming", n=n, value=effect, p=fwd.p_value, est=est,
                                  pc=new_pc, le=now))

    # 3. scored batch that did not pass (and not killed) -> reset the pass streak
    if n >= mn:
        if H:
            return ForwardDecision("completed", "completed_no_edge",
                                   fm("final", n=n, value=effect, p=fwd.p_value, est=est, pc=0))
        return ForwardDecision("active", None,
                               fm("confirming", n=n, value=effect, p=fwd.p_value, est=est, pc=0))

    # 4. underpowered batch (n<mn) -> no batch; do not reset the streak
    if H:
        return ForwardDecision("completed", "completed_inconclusive_insufficient_data",
                               fm("final", n=n, value=effect, p=fwd.p_value, est=est))
    return ForwardDecision("active", None,
                           fm("awaiting_data", n=n, value=effect, p=fwd.p_value, est=est))


# (wallet, since_ts, metric) -> the next non-overlapping forward batch, or None if the
# data source can't answer yet. Real provider reuses the wallet fetch (data-ops, separate).
ForwardWindowProvider = Callable[[str, int, str], "Sequence[Mapping[str, Any]] | None"]


def run_forward_tracking(
    store: Any,
    provider: ForwardWindowProvider,
    *,
    now: int,
    max_tracking_days: int = DEFAULT_MAX_TRACKING_DAYS,
    decay_fraction: float = DEFAULT_DECAY_FRACTION,
    producers: Mapping[str, Callable[[Sequence[Mapping[str, Any]]], ThesisCard]] = METRIC_PRODUCERS,
) -> list[tuple[str, str]]:
    """Walk queued+active rows in storage order; evaluate; conditionally advance.

    Returns (idempotency_key, outcome) per row where outcome ∈ {new_state, "unchanged",
    "conflict"}. No row is selected by wallet/name; storage order only.
    """
    results: list[tuple[str, str]] = []
    for row in store.iter_active():
        metric = (row.get("forward_test_design") or {}).get("metric")
        since = (row.get("forward_metric") or {}).get("last_pass_window_end") or row.get("enrolled_at")
        # `now` is the upper bound of this batch's slice, so consecutive runs at increasing
        # `now` form non-overlapping batches (the two-pass replication requires this).
        window = provider(row["wallet"], since, metric, until_ts=now)
        decision = evaluate(row, window, now=now, max_tracking_days=max_tracking_days,
                            decay_fraction=decay_fraction, producers=producers)
        key = _row_key(row)
        if (decision.new_state == row["state"]
                and decision.forward_metric == (row.get("forward_metric") or {})
                and decision.verdict == row.get("verdict")):
            results.append((key, "unchanged"))
            continue
        ok = store.update_tracking(
            key=_key_fields(row), expected_state=row["state"], new_state=decision.new_state,
            forward_metric=decision.forward_metric, verdict=decision.verdict,
        )
        results.append((key, decision.new_state if ok else "conflict"))
    return results


def _key_fields(row: Mapping[str, Any]) -> dict:
    return {k: row[k] for k in ("wallet", "card_id", "card_version", "profile_run_id")}


def _row_key(row: Mapping[str, Any]) -> str:
    return "|".join(str(row[k]) for k in ("wallet", "card_id", "card_version", "profile_run_id"))
