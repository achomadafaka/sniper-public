# Winning-Wallet Thesis Extractor - Phase 3 Validation Spine Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire Phase 2 thesis candidates into the existing paper/enrollment/forward/replication system so the extractor stops at hypotheses unless validation accepts them.

**Architecture:** Convert cluster-level `MethodFinding`s into thesis-specific paper cards only when they are forward-testable and powered. Underpowered single-wallet theses become replication-required, not failed. Existing `paper_card_enrollments`, `run_forward_tracking`, `replication_check`, and Null Calibration Gate are reused; no new verdict system is invented.

**Tech Stack:** Python 3, `pytest`, `sniper.analysis.thesis_cards`, `sniper.analysis.winning_wallet_thesis`.

**Depends on:** Phase 2 detector pack complete.

---

## Scope

Phase 3 builds:

1. `ValidationPlan` data object;
2. forward-power estimator;
3. method-finding-to-thesis-card adapter;
4. validation spine orchestration;
5. replication fallback adapter;
6. profile JSON validation block;
7. CLI flags for dry-run enrollment only.

It does not run live trading, does not size, and does not write analyzer verdict fields.

## File Structure

| File | Responsibility |
|---|---|
| `sniper/analysis/winning_wallet_thesis/validation/types.py` | `ValidationPlan`, `ValidationState`, power estimate objects. |
| `sniper/analysis/winning_wallet_thesis/validation/power.py` | Estimate future event power from historical cluster cadence. |
| `sniper/analysis/winning_wallet_thesis/validation/cards.py` | Convert a `MethodFinding` + cluster into a `ThesisCard`. |
| `sniper/analysis/winning_wallet_thesis/validation/spine.py` | Decide paper vs replication vs descriptive; optionally enroll using existing store. |
| `sniper/analysis/winning_wallet_thesis/validation/replication.py` | Cross-wallet replication wrapper around existing `replication_check`. |
| `tests/winning_wallet_thesis/test_validation_*.py` | Focused validation tests. |

---

## Task 1: Validation data objects

**Files:**
- Create: `sniper/analysis/winning_wallet_thesis/validation/types.py`
- Create: `sniper/analysis/winning_wallet_thesis/validation/__init__.py`
- Test: `tests/winning_wallet_thesis/test_validation_types.py`

- [ ] **Step 1: Write failing tests**

```python
from sniper.analysis.winning_wallet_thesis.validation.types import ForwardPower, ValidationPlan


def test_validation_plan_marks_unpowered_as_replication_required():
    power = ForwardPower(expected_events=4, horizon_days=30, powered=False)
    plan = ValidationPlan(
        thesis_id="t1",
        validation_state="replication_required",
        forward_power=power,
        paper_card=None,
        blockers=["underpowered_single_wallet_forward"],
    )
    assert plan.validation_state == "replication_required"
```

- [ ] **Step 2: Run failing tests**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_validation_types.py -q`

Expected: missing module failure.

- [ ] **Step 3: Implement types**

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

VALIDATION_STATES = {
    "paper_queued",
    "active",
    "promoted",
    "killed",
    "inconclusive",
    "replication_required",
    "descriptive_only",
    "blocked_missing_feed",
}


@dataclass(frozen=True)
class ForwardPower:
    expected_events: int
    horizon_days: int
    powered: bool


@dataclass(frozen=True)
class ValidationPlan:
    thesis_id: str
    validation_state: str
    forward_power: ForwardPower
    paper_card: Any | None = None
    paper_enrollment_id: str | None = None
    replication: dict[str, Any] = field(default_factory=lambda: {"state": "not_run"})
    null_calibration: dict[str, Any] = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.validation_state not in VALIDATION_STATES:
            raise ValueError(f"unknown validation_state: {self.validation_state}")
```

- [ ] **Step 4: Verify**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_validation_types.py -q`

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/winning_wallet_thesis/validation tests/winning_wallet_thesis/test_validation_types.py
git commit -m "feat(thesis): add validation plan types"
```

---

## Task 2: Forward-power estimator

**Files:**
- Create: `sniper/analysis/winning_wallet_thesis/validation/power.py`
- Test: `tests/winning_wallet_thesis/test_validation_power.py`

- [ ] **Step 1: Write failing tests**

```python
from sniper.analysis.winning_wallet_thesis.types import EdgeCluster, PositionBook, PositionEpisode
from sniper.analysis.winning_wallet_thesis.validation.power import estimate_forward_power


def _ep(i, ts):
    return PositionEpisode("0xw", f"c{i}", f"e{i}", 0, 0, buy_shares=1, buy_cost=.4,
                           resolved=True, won=1, first_buy_ts=ts)


def test_forward_power_requires_enough_expected_events():
    eps = [_ep(i, 1_700_000_000 + i * 86400) for i in range(10)]
    cluster = EdgeCluster("c", "test", {}, "tag", 10, 10, 1, 5,
                          [(e.condition_id, e.outcome_index, e.episode_index) for e in eps])
    power = estimate_forward_power(PositionBook("0xw", eps), cluster, horizon_days=30, min_events=20)
    assert power.powered is False
```

- [ ] **Step 2: Run failing tests**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_validation_power.py -q`

Expected: missing module failure.

- [ ] **Step 3: Implement estimator**

```python
from __future__ import annotations

from sniper.analysis.winning_wallet_thesis.methods.context import ClusterContext
from sniper.analysis.winning_wallet_thesis.types import EdgeCluster, PositionBook
from sniper.analysis.winning_wallet_thesis.validation.types import ForwardPower

SECONDS_PER_DAY = 86400


def estimate_forward_power(
    book: PositionBook,
    cluster: EdgeCluster,
    *,
    horizon_days: int = 30,
    min_events: int = 20,
) -> ForwardPower:
    ctx = ClusterContext(cluster, book)
    ts_values = [ep.first_buy_ts for ep in ctx.episodes if ep.first_buy_ts is not None and ep.event_slug]
    events = {ep.event_slug for ep in ctx.episodes if ep.event_slug}
    if len(ts_values) < 2 or not events:
        return ForwardPower(expected_events=0, horizon_days=horizon_days, powered=False)
    span_days = max((max(ts_values) - min(ts_values)) / SECONDS_PER_DAY, 1.0)
    events_per_day = len(events) / span_days
    expected = int(events_per_day * horizon_days)
    return ForwardPower(expected_events=expected, horizon_days=horizon_days, powered=expected >= min_events)
```

- [ ] **Step 4: Verify**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_validation_power.py -q`

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/winning_wallet_thesis/validation/power.py tests/winning_wallet_thesis/test_validation_power.py
git commit -m "feat(thesis): estimate thesis forward power"
```

---

## Task 3: Method finding to ThesisCard adapter

**Files:**
- Create: `sniper/analysis/winning_wallet_thesis/validation/cards.py`
- Test: `tests/winning_wallet_thesis/test_validation_cards.py`

- [ ] **Step 1: Write failing tests**

```python
from sniper.analysis.winning_wallet_thesis.methods.types import ForwardTestDesign, MethodFinding
from sniper.analysis.winning_wallet_thesis.types import EdgeCluster
from sniper.analysis.winning_wallet_thesis.validation.cards import method_finding_to_card


def test_supported_forward_testable_finding_converts_to_thesis_card():
    finding = MethodFinding(
        "specialist_domain_edge", "supported_proxy", 10, 0.08, 30, evidence={"tag": "UFC"},
        controls={}, blockers=[], runnability="forward_testable",
        forward_test_design=ForwardTestDesign("cluster_calibration_edge", "to_resolution", .03, -.02, 20),
    )
    cluster = EdgeCluster("cluster1", "UFC", {"tag": "UFC"}, "tag", 30, 30, 100, 8)
    card = method_finding_to_card("0xw", cluster, finding)
    assert card.card_id == "winning_wallet:cluster1:specialist_domain_edge"
    assert card.forward_test_design["metric"] == "cluster_calibration_edge"
```

- [ ] **Step 2: Run failing tests**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_validation_cards.py -q`

Expected: missing module failure.

- [ ] **Step 3: Implement adapter**

```python
from __future__ import annotations

from sniper.analysis.thesis_cards.contract import ThesisCard, producer_card
from sniper.analysis.winning_wallet_thesis.methods.types import MethodFinding
from sniper.analysis.winning_wallet_thesis.types import EdgeCluster


def _state_for_card(finding: MethodFinding) -> str:
    if finding.state in {"supported_proxy", "supported_direct"}:
        return "supported"
    if finding.state == "contradicted":
        return "contradicted"
    if finding.state == "blocked_missing_feed":
        return "underpowered"
    return "descriptive_only"


def method_finding_to_card(wallet: str, cluster: EdgeCluster, finding: MethodFinding) -> ThesisCard:
    design = finding.forward_test_design.as_dict() if finding.forward_test_design else None
    return producer_card(
        card_id=f"winning_wallet:{cluster.cluster_id}:{finding.method}",
        lens=f"{cluster.label} / {finding.method}",
        n=finding.n,
        score=finding.score,
        effect=finding.effect,
        p_value=None,
        placebo_passed=True if finding.state in {"supported_proxy", "supported_direct"} else None,
        evidence_state=_state_for_card(finding),
        evidence={
            "wallet": wallet,
            "cluster_id": cluster.cluster_id,
            "method": finding.method,
            "data_tier": "HAVE" if finding.state != "blocked_missing_feed" else "BLOCKED",
            "method_evidence": finding.evidence,
        },
        card_version="winning_wallet_v1",
        forward_test_design=design,
    )
```

- [ ] **Step 4: Verify**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_validation_cards.py -q`

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/winning_wallet_thesis/validation/cards.py tests/winning_wallet_thesis/test_validation_cards.py
git commit -m "feat(thesis): adapt method findings to paper cards"
```

---

## Task 4: Validation spine routing

**Files:**
- Create: `sniper/analysis/winning_wallet_thesis/validation/spine.py`
- Modify: `sniper/analysis/winning_wallet_thesis/types.py`
- Modify: `sniper/analysis/winning_wallet_thesis/assembler.py`
- Test: `tests/winning_wallet_thesis/test_validation_spine.py`

- [ ] **Step 1: Write failing tests**

```python
from sniper.analysis.winning_wallet_thesis.methods.types import ForwardTestDesign, MethodFinding
from sniper.analysis.winning_wallet_thesis.types import EdgeCluster, PositionBook
from sniper.analysis.winning_wallet_thesis.validation.spine import build_validation_plans


def test_unpowered_supported_finding_becomes_replication_required():
    cluster = EdgeCluster("c", "thin", {}, "tag", 5, 5, 10, 5)
    finding = MethodFinding(
        "specialist_domain_edge", "supported_proxy", 5, 0.05, 5,
        evidence={}, controls={}, blockers=[], runnability="forward_testable",
        forward_test_design=ForwardTestDesign("cluster_calibration_edge", "to_resolution", .03, -.02, 20),
    )
    plans = build_validation_plans("0xw", PositionBook("0xw"), [cluster], {"c": [finding]})
    assert plans[0].validation_state == "replication_required"
```

- [ ] **Step 2: Run failing tests**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_validation_spine.py -q`

Expected: missing module failure.

- [ ] **Step 3: Implement routing**

```python
from __future__ import annotations

from sniper.analysis.winning_wallet_thesis.methods.types import valid_forward_test_design
from sniper.analysis.winning_wallet_thesis.types import EdgeCluster, PositionBook
from sniper.analysis.winning_wallet_thesis.validation.cards import method_finding_to_card
from sniper.analysis.winning_wallet_thesis.validation.power import estimate_forward_power
from sniper.analysis.winning_wallet_thesis.validation.types import ValidationPlan


def build_validation_plans(
    wallet: str,
    book: PositionBook,
    clusters: list[EdgeCluster],
    findings_by_cluster: dict[str, list],
    *,
    horizon_days: int = 30,
    min_events: int = 20,
) -> list[ValidationPlan]:
    cluster_by_id = {c.cluster_id: c for c in clusters}
    plans: list[ValidationPlan] = []
    for cluster_id, findings in findings_by_cluster.items():
        cluster = cluster_by_id[cluster_id]
        power = estimate_forward_power(book, cluster, horizon_days=horizon_days, min_events=min_events)
        for finding in findings:
            thesis_id = f"{cluster.cluster_id}:{finding.method}"
            if not valid_forward_test_design(finding.forward_test_design):
                plans.append(ValidationPlan(thesis_id, "descriptive_only", power,
                                            blockers=["missing_forward_test_design"]))
                continue
            if finding.runnability == "data_blocked" or finding.state == "blocked_missing_feed":
                plans.append(ValidationPlan(thesis_id, "blocked_missing_feed", power,
                                            blockers=finding.blockers))
                continue
            if not power.powered:
                plans.append(ValidationPlan(thesis_id, "replication_required", power,
                                            blockers=["underpowered_single_wallet_forward"]))
                continue
            card = method_finding_to_card(wallet, cluster, finding)
            plans.append(ValidationPlan(thesis_id, "paper_queued", power, paper_card=card))
    return plans
```

- [ ] **Step 4: Integrate with profile**

Add a field to `ThesisProfile`:

```python
validation_plans: list = field(default_factory=list)
```

In `assembler.py`, after method findings:

```python
from sniper.analysis.winning_wallet_thesis.validation.spine import build_validation_plans

validation_plans = build_validation_plans(wallet, book, clusters, method_findings)
```

- [ ] **Step 5: Verify**

Run:
```bash
python3 -m pytest tests/winning_wallet_thesis/test_validation_spine.py tests/winning_wallet_thesis -q
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add sniper/analysis/winning_wallet_thesis tests/winning_wallet_thesis/test_validation_spine.py
git commit -m "feat(thesis): route findings through validation spine"
```

---

## Task 5: Optional paper enrollment adapter

**Files:**
- Modify: `sniper/analysis/winning_wallet_thesis/validation/spine.py`
- Test: `tests/winning_wallet_thesis/test_validation_enrollment.py`

- [ ] **Step 1: Write failing tests**

```python
from sniper.analysis.thesis_cards.enrollment import InMemoryPaperEnrollmentStore
from sniper.analysis.winning_wallet_thesis.validation.spine import enroll_validation_plans


def test_enroll_validation_plans_only_enrolls_paper_queued_cards(valid_paper_plan, descriptive_plan):
    store = InMemoryPaperEnrollmentStore()
    result = enroll_validation_plans("0xw", [valid_paper_plan, descriptive_plan], store=store, enrolled_at=123)
    assert len(result.enrollments) == 1
```

- [ ] **Step 2: Run failing tests**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_validation_enrollment.py -q`

Expected: missing helper failure.

- [ ] **Step 3: Implement helper**

```python
from sniper.analysis.thesis_cards.admission import explain_paper_admission
from sniper.analysis.thesis_cards.enrollment import enroll_profile
from sniper.analysis.thesis_cards.profile import ThesisProfile as PaperProfile, _run_id


def enroll_validation_plans(wallet: str, plans: list[ValidationPlan], *, store, enrolled_at: int):
    cards = [plan.paper_card for plan in plans if plan.validation_state == "paper_queued" and plan.paper_card is not None]
    decisions = explain_paper_admission(cards)
    eligible = [d.card for d in decisions if d.decision == "admitted"]
    profile_run_id = _run_id(wallet, cards)
    profile = PaperProfile(
        wallet=wallet,
        n_positions=sum(card.n for card in cards),
        cards=cards,
        headline=f"{len(eligible)} winning-wallet theses queued for paper",
        profile_run_id=profile_run_id,
        decisions=decisions,
        eligible=eligible,
    )
    return enroll_profile(profile, store=store, enrolled_at=enrolled_at)
```

- [ ] **Step 4: Verify**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_validation_enrollment.py tests/thesis_cards/test_enrollment.py -q`

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/winning_wallet_thesis/validation tests/winning_wallet_thesis/test_validation_enrollment.py
git commit -m "feat(thesis): enroll validation plans into paper"
```

---

## Task 6: Replication fallback

**Files:**
- Create: `sniper/analysis/winning_wallet_thesis/validation/replication.py`
- Test: `tests/winning_wallet_thesis/test_validation_replication.py`

- [ ] **Step 1: Write failing tests**

```python
from sniper.analysis.winning_wallet_thesis.validation.replication import replication_required_summary
from sniper.analysis.winning_wallet_thesis.validation.types import ForwardPower, ValidationPlan


def test_replication_summary_counts_required_plans():
    plans = [
        ValidationPlan("a", "replication_required", ForwardPower(3, 30, False)),
        ValidationPlan("b", "descriptive_only", ForwardPower(0, 30, False)),
    ]
    assert replication_required_summary(plans)["replication_required"] == 1
```

- [ ] **Step 2: Run failing tests**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_validation_replication.py -q`

Expected: missing module failure.

- [ ] **Step 3: Implement first adapter**

```python
from __future__ import annotations

from sniper.analysis.winning_wallet_thesis.validation.types import ValidationPlan


def replication_required_summary(plans: list[ValidationPlan]) -> dict:
    required = [p for p in plans if p.validation_state == "replication_required"]
    return {
        "replication_required": len(required),
        "thesis_ids": [p.thesis_id for p in required],
        "state": "not_run" if required else "not_required",
    }
```

Do not run cross-wallet batches in this task. That requires operator-provided comparable winners.

- [ ] **Step 4: Verify**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_validation_replication.py -q`

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/winning_wallet_thesis/validation/replication.py tests/winning_wallet_thesis/test_validation_replication.py
git commit -m "feat(thesis): summarize replication-required theses"
```

## Phase 3 Exit Criteria

- Profile JSON contains validation plans for each thesis.
- Powered supported theses can be queued for paper through existing enrollment.
- Unpowered theses become `replication_required`, not failed.
- Blocked feed theses remain visible and unenrolled.
- No new DB schema unless the existing `paper_card_enrollments` contract is insufficient.
- `python3 -m pytest tests/winning_wallet_thesis tests/thesis_cards -q` passes.
