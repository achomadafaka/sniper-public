"""H5 — Tag-level edge concentration.

Per spec §3 H5: for each tag with `n_held_to_resolution >= 10`, test whether
this wallet's held-to-resolution WR exceeds the tag's population mean WR.

Methodology review locked the decision rule (2026-04-25):
- peer_count >= 30  → confidence_flag = "ok"; run inference normally
- 10 <= peer_count < 30 → confidence_flag = "low_population"; run inference but
  mark verdict explicitly so downstream consumers know not to weigh it
- peer_count < 10   → confidence_flag = "low_population"; verdict = "inference_not_run"
                       (refuse — baseline too thin for any meaningful percentile)

This mirrors B2's existing `wallet_tag_fit.confidence_flag` ladder. Same
thresholds intentionally — operators familiar with one understand the other.
"""

from __future__ import annotations

from dataclasses import dataclass


# Thresholds locked by methodology review 2026-04-25.
# Mirrors B2 wallet_tag_fit confidence_flag pattern.
PEER_COUNT_OK_THRESHOLD = 30
PEER_COUNT_INFERENCE_FLOOR = 10


@dataclass
class H5TagResult:
    """Result of running H5 for one tag on one wallet."""

    tag: str
    peer_count: int
    confidence_flag: str               # "ok" | "low_population"
    n_held: int
    wr_held: float | None              # None when n_held < 10 (descriptive-only)
    pop_mean_wr: float | None
    excess_wr_pp: float | None         # (wr_held - pop_mean_wr) * 100
    ci_lower_pp: float | None
    ci_upper_pp: float | None
    bh_adjusted_p: float | None
    verdict: str
    """One of:
       - 'supported'              — BH p < 0.10 AND CI excludes zero AND confidence_flag='ok'
       - 'refuted'                — CI fully on the wrong side
       - 'inconclusive'           — CI includes zero
       - 'underpowered'           — n_held < 10
       - 'baseline_underpowered'  — n_held >= 10 but peer_count < 10
       - 'inference_not_run'      — baseline too thin to run at all
       - 'descriptive_only'       — n_held < 10 (low sample on this tag)
    """


def classify_peer_confidence(peer_count: int) -> str:
    """Map peer_count to the locked confidence_flag values.

    Returns:
        'ok'             when peer_count >= 30
        'low_population' otherwise
    """
    if peer_count >= PEER_COUNT_OK_THRESHOLD:
        return "ok"
    return "low_population"


def should_run_inference(peer_count: int, n_held: int) -> tuple[bool, str]:
    """Apply the protocol's decision rule. Returns (run_inference, reason).

    `reason` is the verdict label to use when run_inference=False.
    """
    if n_held < PEER_COUNT_INFERENCE_FLOOR:
        return False, "descriptive_only"
    if peer_count < PEER_COUNT_INFERENCE_FLOOR:
        return False, "inference_not_run"
    return True, ""
