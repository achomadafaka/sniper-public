# Winning-Wallet Thesis Extractor - Phase 2 Detector Pack Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the first cluster-level method detectors so Phase 1 edge clusters become explicit thesis candidates, not just ranked slices.

**Architecture:** Detectors run against each `EdgeCluster` plus the `PositionBook`/slice context from Phase 1. Each detector returns a `MethodFinding` with data tier, evidence state, runnability, blockers, and a frozen forward-test design when honest. Detectors are leads only; they do not validate, promote, mutate verdicts, or emit trade advice.

**Tech Stack:** Python 3, dataclasses, `pytest`, `sniper.analysis.winning_wallet_thesis` Phase 1 modules, existing `sniper.analysis.thesis_cards.contract.ThesisCard` vocabulary where useful.

**Depends on:** Phase 1 completed through `types.py`, `slicer.py`, `clusters.py`, `assembler.py`, and `cli.py`.

---

## Scope

Build-now detectors from spec rev 2 §9:

1. specialist domain edge;
2. recurring template edge;
3. market-weighted favorite/longshot calibration;
4. reward attribution;
5. sizing skill as rare/lower-priority;
6. exit/hold mechanism as descriptive;
7. capital velocity as amplifier/meta-method;
8. runnability classifier.

Do not build stale-liquidity/CLV, full event-basket negRisk accounting, market-making, queue, or first-attention yet. Those are Phase 4.

## File Structure

| File | Responsibility |
|---|---|
| `sniper/analysis/winning_wallet_thesis/methods/types.py` | `MethodFinding`, `ForwardTestDesign`, common enums. |
| `sniper/analysis/winning_wallet_thesis/methods/context.py` | `ClusterContext` helper: cluster members, sibling slices, event counts, reward totals. |
| `sniper/analysis/winning_wallet_thesis/methods/specialist.py` | Specialist-domain detector over tag/category/template/event-family clusters. |
| `sniper/analysis/winning_wallet_thesis/methods/template.py` | Recurring platform-series / slug-family detector. |
| `sniper/analysis/winning_wallet_thesis/methods/calibration.py` | Market-level favorite/longshot calibration detector. |
| `sniper/analysis/winning_wallet_thesis/methods/reward.py` | Prediction-vs-reward attribution detector. |
| `sniper/analysis/winning_wallet_thesis/methods/sizing.py` | Cluster-local sizing skill detector. |
| `sniper/analysis/winning_wallet_thesis/methods/exit_hold.py` | Held-vs-sold descriptive mechanism detector. |
| `sniper/analysis/winning_wallet_thesis/methods/velocity.py` | Capital velocity amplifier detector. |
| `sniper/analysis/winning_wallet_thesis/methods/runnability.py` | Converts method evidence + blockers into runnable/data-blocked/etc. |
| `sniper/analysis/winning_wallet_thesis/methods/runner.py` | Runs all detectors per cluster and returns findings. |
| `tests/winning_wallet_thesis/test_method_*.py` | Focused detector tests. |

---

## Task 1: Method finding types

**Files:**
- Create: `sniper/analysis/winning_wallet_thesis/methods/types.py`
- Create: `sniper/analysis/winning_wallet_thesis/methods/__init__.py`
- Test: `tests/winning_wallet_thesis/test_method_types.py`

- [ ] **Step 1: Write failing tests**

```python
from sniper.analysis.winning_wallet_thesis.methods.types import (
    ForwardTestDesign,
    MethodFinding,
    valid_forward_test_design,
)


def test_method_finding_requires_known_state():
    finding = MethodFinding(
        method="specialist_domain_edge",
        state="supported_proxy",
        score=1.2,
        effect=7.5,
        n=30,
        evidence={"tag": "UFC"},
        controls={"nearest_sibling": "sports_non_ufc"},
        blockers=[],
        runnability="forward_testable",
        forward_test_design=ForwardTestDesign(
            metric="cluster_calibration_edge",
            horizon="to_resolution",
            success_threshold=0.03,
            kill_threshold=-0.02,
            min_forward_n=20,
            stake_convention="uniform_paper_unit",
        ),
    )
    assert finding.state == "supported_proxy"
    assert valid_forward_test_design(finding.forward_test_design)


def test_blocked_finding_has_no_forward_design():
    finding = MethodFinding(
        method="stale_liquidity_sniping",
        state="blocked_missing_feed",
        score=0.0,
        effect=None,
        n=0,
        evidence={},
        controls={},
        blockers=["missing_price_path"],
        runnability="data_blocked",
    )
    assert finding.forward_test_design is None
```

- [ ] **Step 2: Run failing tests**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_method_types.py -q`

Expected: `ModuleNotFoundError` or missing class failure.

- [ ] **Step 3: Implement types**

```python
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

METHOD_STATES = {
    "supported_proxy",
    "supported_direct",
    "descriptive",
    "blocked_missing_feed",
    "contradicted",
}

RUNNABILITY = {
    "forward_testable",
    "replication_required",
    "data_blocked",
    "speed_required",
    "capital_required",
    "non_replicable",
    "descriptive_only",
}


@dataclass(frozen=True)
class ForwardTestDesign:
    metric: str
    horizon: str
    success_threshold: float
    kill_threshold: float
    min_forward_n: int
    stake_convention: str = "uniform_paper_unit"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def valid_forward_test_design(design: ForwardTestDesign | None) -> bool:
    return (
        design is not None
        and design.metric != ""
        and design.horizon != ""
        and design.min_forward_n > 0
        and design.stake_convention == "uniform_paper_unit"
    )


@dataclass(frozen=True)
class MethodFinding:
    method: str
    state: str
    score: float
    effect: float | None
    n: int
    evidence: dict[str, Any] = field(default_factory=dict)
    controls: dict[str, Any] = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)
    runnability: str = "descriptive_only"
    forward_test_design: ForwardTestDesign | None = None

    def __post_init__(self) -> None:
        if self.state not in METHOD_STATES:
            raise ValueError(f"unknown method state: {self.state}")
        if self.runnability not in RUNNABILITY:
            raise ValueError(f"unknown runnability: {self.runnability}")
        if self.score < 0:
            raise ValueError("score must be non-negative")
```

- [ ] **Step 4: Verify**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_method_types.py -q`

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/winning_wallet_thesis/methods tests/winning_wallet_thesis/test_method_types.py
git commit -m "feat(thesis): add method finding contract"
```

---

## Task 2: Cluster context helper

**Files:**
- Create: `sniper/analysis/winning_wallet_thesis/methods/context.py`
- Test: `tests/winning_wallet_thesis/test_method_context.py`

- [ ] **Step 1: Write failing tests**

```python
from sniper.analysis.winning_wallet_thesis.methods.context import ClusterContext
from sniper.analysis.winning_wallet_thesis.types import EdgeCluster, PositionBook, PositionEpisode


def _ep(event_slug, condition_id, won, entry=0.40, cost=40.0):
    return PositionEpisode(
        wallet="0xw",
        condition_id=condition_id,
        event_slug=event_slug,
        outcome_index=0,
        episode_index=0,
        buy_shares=100,
        buy_cost=cost,
        resolved=True,
        won=won,
        tags=["UFC"],
        template="ufc-fight-night",
    )


def test_context_returns_cluster_member_episodes():
    ep = _ep("e1", "c1", 1)
    cluster = EdgeCluster(
        cluster_id="c",
        label="UFC",
        scope={"tag": "UFC"},
        axis="tag",
        n_positions=1,
        n_events=1,
        pnl=60,
        edge_pp=60,
        member_position_keys=[("c1", 0, 0)],
    )
    ctx = ClusterContext(cluster=cluster, book=PositionBook(wallet="0xw", episodes=[ep]))
    assert ctx.episodes == [ep]
    assert ctx.event_count == 1
```

- [ ] **Step 2: Run failing tests**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_method_context.py -q`

Expected: missing module failure.

- [ ] **Step 3: Implement context**

```python
from __future__ import annotations

from dataclasses import dataclass

from sniper.analysis.winning_wallet_thesis.types import EdgeCluster, PositionBook, PositionEpisode


def _key(ep: PositionEpisode) -> tuple:
    return (ep.condition_id, ep.outcome_index, ep.episode_index)


@dataclass(frozen=True)
class ClusterContext:
    cluster: EdgeCluster
    book: PositionBook

    @property
    def episodes(self) -> list[PositionEpisode]:
        wanted = set(self.cluster.member_position_keys)
        if not wanted:
            return []
        return [ep for ep in self.book.episodes if _key(ep) in wanted]

    @property
    def resolved_episodes(self) -> list[PositionEpisode]:
        return [ep for ep in self.episodes if ep.resolved and ep.won is not None and not ep.is_push]

    @property
    def event_count(self) -> int:
        return len({ep.event_slug for ep in self.episodes if ep.event_slug})

    @property
    def turnover(self) -> float:
        return sum(ep.buy_cost for ep in self.episodes)

    @property
    def pnl(self) -> float:
        return sum(ep.realized_pnl or 0.0 for ep in self.resolved_episodes)
```

- [ ] **Step 4: Verify**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_method_context.py -q`

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/winning_wallet_thesis/methods/context.py tests/winning_wallet_thesis/test_method_context.py
git commit -m "feat(thesis): add cluster detector context"
```

---

## Task 3: Specialist and recurring-template detectors

**Files:**
- Create: `sniper/analysis/winning_wallet_thesis/methods/specialist.py`
- Create: `sniper/analysis/winning_wallet_thesis/methods/template.py`
- Test: `tests/winning_wallet_thesis/test_method_specialist_template.py`

- [ ] **Step 1: Write failing tests**

```python
from sniper.analysis.winning_wallet_thesis.methods.context import ClusterContext
from sniper.analysis.winning_wallet_thesis.methods.specialist import specialist_domain
from sniper.analysis.winning_wallet_thesis.methods.template import recurring_template
from sniper.analysis.winning_wallet_thesis.types import EdgeCluster, PositionBook, PositionEpisode


def _episode(i, template="ufc-fight-night"):
    return PositionEpisode(
        wallet="0xw",
        condition_id=f"c{i}",
        event_slug=f"e{i}",
        outcome_index=0,
        episode_index=0,
        buy_shares=100,
        buy_cost=40,
        resolved=True,
        won=1,
        tags=["UFC"],
        category="sports",
        template=template,
    )


def _ctx(n=6):
    episodes = [_episode(i) for i in range(n)]
    cluster = EdgeCluster(
        cluster_id="ufc",
        label="UFC held",
        scope={"tag": "UFC"},
        axis="tag",
        n_positions=n,
        n_events=n,
        pnl=360,
        edge_pp=60,
        member_position_keys=[(ep.condition_id, ep.outcome_index, ep.episode_index) for ep in episodes],
    )
    return ClusterContext(cluster=cluster, book=PositionBook(wallet="0xw", episodes=episodes))


def test_specialist_supported_for_powered_positive_tag_cluster():
    finding = specialist_domain(_ctx())
    assert finding.method == "specialist_domain_edge"
    assert finding.state == "supported_proxy"
    assert finding.runnability == "forward_testable"
    assert finding.forward_test_design is not None


def test_recurring_template_supported_when_events_share_template():
    finding = recurring_template(_ctx())
    assert finding.method == "recurring_template_edge"
    assert finding.state == "supported_proxy"
    assert finding.evidence["template"] == "ufc-fight-night"
```

- [ ] **Step 2: Run failing tests**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_method_specialist_template.py -q`

Expected: missing detector failure.

- [ ] **Step 3: Implement detectors**

```python
# sniper/analysis/winning_wallet_thesis/methods/specialist.py
from __future__ import annotations

from collections import Counter

from sniper.analysis.winning_wallet_thesis.methods.context import ClusterContext
from sniper.analysis.winning_wallet_thesis.methods.types import ForwardTestDesign, MethodFinding


def specialist_domain(ctx: ClusterContext, *, min_events: int = 5) -> MethodFinding:
    eps = ctx.resolved_episodes
    tags = Counter(tag for ep in eps for tag in ep.tags)
    top_tag, tag_n = tags.most_common(1)[0] if tags else ("", 0)
    effect = ctx.cluster.edge_pp
    if ctx.event_count < min_events:
        return MethodFinding("specialist_domain_edge", "descriptive", 0.0, effect, len(eps),
                             evidence={"reason": "under_min_events", "events": ctx.event_count},
                             controls={}, blockers=[], runnability="descriptive_only")
    state = "supported_proxy" if effect > 0 and tag_n >= min_events else "contradicted"
    design = ForwardTestDesign("cluster_calibration_edge", "to_resolution", 0.03, -0.02, min_events)
    return MethodFinding(
        method="specialist_domain_edge",
        state=state,
        score=max(effect, 0.0) * min(ctx.event_count, 50) / 50.0,
        effect=effect,
        n=ctx.event_count,
        evidence={"tag": top_tag, "tag_events": tag_n, "axis": ctx.cluster.axis},
        controls={"guard": "cluster_guarded_by_phase1_min_events_min_positions"},
        blockers=[],
        runnability="forward_testable" if state.startswith("supported") else "descriptive_only",
        forward_test_design=design if state.startswith("supported") else None,
    )
```

```python
# sniper/analysis/winning_wallet_thesis/methods/template.py
from __future__ import annotations

from collections import Counter

from sniper.analysis.winning_wallet_thesis.methods.context import ClusterContext
from sniper.analysis.winning_wallet_thesis.methods.types import ForwardTestDesign, MethodFinding


def recurring_template(ctx: ClusterContext, *, min_events: int = 3) -> MethodFinding:
    eps = ctx.resolved_episodes
    templates = Counter(ep.template for ep in eps if ep.template)
    template, n = templates.most_common(1)[0] if templates else ("", 0)
    effect = ctx.cluster.edge_pp
    if not template or n < min_events:
        return MethodFinding("recurring_template_edge", "descriptive", 0.0, effect, len(eps),
                             evidence={"reason": "no_repeated_platform_template"},
                             controls={}, blockers=[], runnability="descriptive_only")
    state = "supported_proxy" if effect > 0 else "contradicted"
    design = ForwardTestDesign("cluster_calibration_edge", "to_resolution", 0.03, -0.02, min_events)
    return MethodFinding(
        method="recurring_template_edge",
        state=state,
        score=max(effect, 0.0) * n / max(ctx.event_count, 1),
        effect=effect,
        n=n,
        evidence={"template": template, "template_events": n},
        controls={"source": "series_or_slug_family_not_title_regex"},
        blockers=[],
        runnability="forward_testable" if state.startswith("supported") else "descriptive_only",
        forward_test_design=design if state.startswith("supported") else None,
    )
```

- [ ] **Step 4: Verify**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_method_specialist_template.py -q`

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/winning_wallet_thesis/methods/specialist.py sniper/analysis/winning_wallet_thesis/methods/template.py tests/winning_wallet_thesis/test_method_specialist_template.py
git commit -m "feat(thesis): detect specialist and recurring-template clusters"
```

---

## Task 4: Market-weighted calibration detector

**Files:**
- Create: `sniper/analysis/winning_wallet_thesis/methods/calibration.py`
- Test: `tests/winning_wallet_thesis/test_method_calibration.py`

- [ ] **Step 1: Write failing tests**

```python
from sniper.analysis.winning_wallet_thesis.methods.calibration import favorite_longshot_calibration
from sniper.analysis.winning_wallet_thesis.methods.context import ClusterContext
from sniper.analysis.winning_wallet_thesis.types import EdgeCluster, PositionBook, PositionEpisode


def _ep(event_slug, condition_id, won, cost):
    return PositionEpisode(
        wallet="0xw", condition_id=condition_id, event_slug=event_slug, outcome_index=0,
        episode_index=0, buy_shares=100, buy_cost=cost, resolved=True, won=won,
    )


def test_calibration_uses_market_level_not_position_weighting():
    # e1 has two positions but counts once at market/event level.
    eps = [
        _ep("e1", "c1", 1, 20),
        _ep("e1", "c1b", 1, 20),
        _ep("e2", "c2", 0, 90),
    ]
    cluster = EdgeCluster("c", "test", {}, "price_regime", 3, 2, 0, 0,
                          [(e.condition_id, e.outcome_index, e.episode_index) for e in eps])
    finding = favorite_longshot_calibration(ClusterContext(cluster, PositionBook("0xw", eps)))
    assert finding.evidence["market_weighted"] is True
    assert finding.n == 2
```

- [ ] **Step 2: Run failing tests**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_method_calibration.py -q`

Expected: missing module failure.

- [ ] **Step 3: Implement market-level detector**

```python
from __future__ import annotations

from collections import defaultdict

from sniper.analysis.winning_wallet_thesis.methods.context import ClusterContext
from sniper.analysis.winning_wallet_thesis.methods.types import ForwardTestDesign, MethodFinding


def _band(price: float) -> str:
    if price < 0.10:
        return "longshot"
    if price < 0.35:
        return "underdog"
    if price < 0.65:
        return "mid"
    if price < 0.90:
        return "favorite"
    return "near_certainty"


def favorite_longshot_calibration(ctx: ClusterContext, *, min_events: int = 5) -> MethodFinding:
    by_event = defaultdict(list)
    for ep in ctx.resolved_episodes:
        if ep.entry_vwap >= 0.98:
            continue
        by_event[ep.event_slug].append(ep)
    event_rows = []
    for event_slug, eps in by_event.items():
        mean_entry = sum(ep.entry_vwap for ep in eps) / len(eps)
        win_rate = sum(ep.won or 0 for ep in eps) / len(eps)
        event_rows.append((event_slug, _band(mean_entry), win_rate, mean_entry))
    if len(event_rows) < min_events:
        return MethodFinding("favorite_longshot_calibration", "descriptive", 0.0, None, len(event_rows),
                             evidence={"market_weighted": True, "reason": "under_min_events"},
                             controls={}, blockers=[], runnability="descriptive_only")
    win_rate = sum(row[2] for row in event_rows) / len(event_rows)
    entry = sum(row[3] for row in event_rows) / len(event_rows)
    effect = win_rate - entry
    state = "supported_proxy" if effect > 0 else "contradicted"
    design = ForwardTestDesign("cluster_calibration_edge", "to_resolution", 0.03, -0.02, min_events)
    return MethodFinding(
        method="favorite_longshot_calibration",
        state=state,
        score=max(effect, 0.0) * 100.0,
        effect=effect,
        n=len(event_rows),
        evidence={"market_weighted": True, "edge": effect, "bands": sorted({r[1] for r in event_rows})},
        controls={"guard": "event_level_weighting_avoids_position_scale_artifact"},
        blockers=[],
        runnability="forward_testable" if state.startswith("supported") else "descriptive_only",
        forward_test_design=design if state.startswith("supported") else None,
    )
```

- [ ] **Step 4: Verify**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_method_calibration.py -q`

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/winning_wallet_thesis/methods/calibration.py tests/winning_wallet_thesis/test_method_calibration.py
git commit -m "feat(thesis): add market-weighted calibration detector"
```

---

## Task 5: Reward, exit/hold, and velocity detectors

**Files:**
- Create: `sniper/analysis/winning_wallet_thesis/methods/reward.py`
- Create: `sniper/analysis/winning_wallet_thesis/methods/exit_hold.py`
- Create: `sniper/analysis/winning_wallet_thesis/methods/velocity.py`
- Test: `tests/winning_wallet_thesis/test_method_economics.py`

- [ ] **Step 1: Write failing tests**

```python
from sniper.analysis.winning_wallet_thesis.methods.context import ClusterContext
from sniper.analysis.winning_wallet_thesis.methods.exit_hold import exit_hold_mechanism
from sniper.analysis.winning_wallet_thesis.methods.reward import reward_attribution
from sniper.analysis.winning_wallet_thesis.methods.velocity import capital_velocity
from sniper.analysis.winning_wallet_thesis.types import EdgeCluster, PositionBook, PositionEpisode


def _ctx(reward_income=None):
    eps = [
        PositionEpisode("0xw", "c1", "e1", 0, 0, buy_shares=100, buy_cost=40, resolved=True, won=1, redeemed=True),
        PositionEpisode("0xw", "c2", "e2", 0, 0, buy_shares=100, buy_cost=60, sell_shares=100, sell_proceeds=55, resolved=True, won=1),
    ]
    cluster = EdgeCluster("c", "test", {}, "tag", 2, 2, 55, 25,
                          [(e.condition_id, e.outcome_index, e.episode_index) for e in eps])
    return ClusterContext(cluster, PositionBook("0xw", eps, reward_income=reward_income or {"wallet": 3.0}))


def test_reward_attribution_is_descriptive_prediction_vs_rewards_gate():
    finding = reward_attribution(_ctx())
    assert finding.method == "reward_attribution"
    assert finding.state == "descriptive"
    assert finding.evidence["reward_share_pct"] >= 0


def test_exit_hold_is_descriptive_not_promotable():
    finding = exit_hold_mechanism(_ctx())
    assert finding.state == "descriptive"
    assert finding.forward_test_design is None


def test_velocity_is_meta_method_only():
    finding = capital_velocity(_ctx())
    assert finding.method == "capital_velocity"
    assert finding.runnability == "descriptive_only"
```

- [ ] **Step 2: Run failing tests**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_method_economics.py -q`

Expected: missing module failure.

- [ ] **Step 3: Implement detectors**

```python
# reward.py
from __future__ import annotations

from sniper.analysis.winning_wallet_thesis.methods.context import ClusterContext
from sniper.analysis.winning_wallet_thesis.methods.types import MethodFinding


def reward_attribution(ctx: ClusterContext) -> MethodFinding:
    rewards = sum(ctx.book.reward_income.values())
    gross = abs(ctx.pnl) + rewards
    share = rewards / gross * 100.0 if gross else 0.0
    return MethodFinding(
        method="reward_attribution",
        state="descriptive",
        score=0.0,
        effect=share,
        n=len(ctx.episodes),
        evidence={"reward_usdc": rewards, "reward_share_pct": share},
        controls={"interpretation": "reward-heavy_clusters_are_not_selection_theses"},
        blockers=[],
        runnability="descriptive_only",
    )
```

```python
# exit_hold.py
from __future__ import annotations

from collections import Counter

from sniper.analysis.winning_wallet_thesis.methods.context import ClusterContext
from sniper.analysis.winning_wallet_thesis.methods.types import MethodFinding


def exit_hold_mechanism(ctx: ClusterContext) -> MethodFinding:
    counts = Counter(ep.exit_mechanism for ep in ctx.episodes)
    held = counts.get("held", 0)
    sold = counts.get("sold", 0)
    return MethodFinding(
        method="exit_hold_mechanism",
        state="descriptive",
        score=0.0,
        effect=None,
        n=len(ctx.episodes),
        evidence={"held": held, "sold": sold, "open": counts.get("open", 0)},
        controls={"guard": "no_clean_exit_time_null_yet"},
        blockers=["missing_exit_time_price_path"],
        runnability="descriptive_only",
    )
```

```python
# velocity.py
from __future__ import annotations

from sniper.analysis.winning_wallet_thesis.methods.context import ClusterContext
from sniper.analysis.winning_wallet_thesis.methods.types import MethodFinding


def capital_velocity(ctx: ClusterContext) -> MethodFinding:
    turnover = ctx.turnover
    pnl = ctx.pnl
    roi = pnl / turnover if turnover else 0.0
    return MethodFinding(
        method="capital_velocity",
        state="descriptive",
        score=abs(roi),
        effect=roi,
        n=len(ctx.episodes),
        evidence={"turnover": turnover, "pnl": pnl, "roi_per_dollar": roi},
        controls={"interpretation": "amplifier_only_velocity_without_edge_is_churn"},
        blockers=[],
        runnability="descriptive_only",
    )
```

- [ ] **Step 4: Verify**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_method_economics.py -q`

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/winning_wallet_thesis/methods/reward.py sniper/analysis/winning_wallet_thesis/methods/exit_hold.py sniper/analysis/winning_wallet_thesis/methods/velocity.py tests/winning_wallet_thesis/test_method_economics.py
git commit -m "feat(thesis): add economics method detectors"
```

---

## Task 6: Sizing and runnability

**Files:**
- Create: `sniper/analysis/winning_wallet_thesis/methods/sizing.py`
- Create: `sniper/analysis/winning_wallet_thesis/methods/runnability.py`
- Test: `tests/winning_wallet_thesis/test_method_sizing_runnability.py`

- [ ] **Step 1: Write failing tests**

```python
from sniper.analysis.winning_wallet_thesis.methods.context import ClusterContext
from sniper.analysis.winning_wallet_thesis.methods.runnability import classify_runnability
from sniper.analysis.winning_wallet_thesis.methods.sizing import sizing_skill
from sniper.analysis.winning_wallet_thesis.methods.types import MethodFinding
from sniper.analysis.winning_wallet_thesis.types import EdgeCluster, PositionBook, PositionEpisode


def _ctx():
    eps = [
        PositionEpisode("0xw", "c1", "e1", 0, 0, buy_shares=200, buy_cost=80, resolved=True, won=1),
        PositionEpisode("0xw", "c2", "e2", 0, 0, buy_shares=20, buy_cost=10, resolved=True, won=0),
    ]
    cluster = EdgeCluster("c", "sizing", {}, "tag", 2, 2, 110, 30,
                          [(e.condition_id, e.outcome_index, e.episode_index) for e in eps])
    return ClusterContext(cluster, PositionBook("0xw", eps))


def test_sizing_returns_exploratory_under_low_n():
    finding = sizing_skill(_ctx(), min_n=20)
    assert finding.state == "descriptive"
    assert finding.forward_test_design is None


def test_runnability_flags_blocked_feed():
    finding = MethodFinding("x", "blocked_missing_feed", 0, None, 0, blockers=["missing_news_feed"])
    assert classify_runnability(finding) == "data_blocked"
```

- [ ] **Step 2: Run failing tests**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_method_sizing_runnability.py -q`

Expected: missing module failure.

- [ ] **Step 3: Implement**

```python
# runnability.py
from __future__ import annotations

from sniper.analysis.winning_wallet_thesis.methods.types import MethodFinding


def classify_runnability(finding: MethodFinding) -> str:
    blockers = set(finding.blockers)
    if finding.state == "blocked_missing_feed" or any(b.startswith("missing_") for b in blockers):
        return "data_blocked"
    if "speed" in blockers or "low_latency_required" in blockers:
        return "speed_required"
    if "capital_constraint" in blockers:
        return "capital_required"
    if "private_information" in blockers:
        return "non_replicable"
    if finding.forward_test_design is not None and finding.state.startswith("supported"):
        return "forward_testable"
    return "descriptive_only"
```

```python
# sizing.py
from __future__ import annotations

from sniper.analysis.winning_wallet_thesis.methods.context import ClusterContext
from sniper.analysis.winning_wallet_thesis.methods.types import ForwardTestDesign, MethodFinding


def sizing_skill(ctx: ClusterContext, *, min_n: int = 30) -> MethodFinding:
    eps = ctx.resolved_episodes
    if len(eps) < min_n:
        return MethodFinding("sizing_skill", "descriptive", 0.0, None, len(eps),
                             evidence={"reason": "under_min_n"}, controls={},
                             blockers=[], runnability="descriptive_only")
    equal = sum((ep.won or 0) - ep.entry_vwap for ep in eps) / len(eps)
    total_cost = sum(ep.buy_cost for ep in eps)
    dollar = sum(((ep.won or 0) - ep.entry_vwap) * ep.buy_cost for ep in eps) / total_cost if total_cost else 0.0
    effect = dollar - equal
    state = "supported_proxy" if equal > 0 and effect > 0 else "contradicted"
    design = ForwardTestDesign("cluster_sizing_edge", "to_resolution", 0.01, -0.01, min_n)
    return MethodFinding(
        method="sizing_skill",
        state=state,
        score=max(effect, 0.0) * 100.0,
        effect=effect,
        n=len(eps),
        evidence={"equal_weight_edge": equal, "dollar_weight_edge": dollar},
        controls={"guard": "requires_positive_base_edge_first"},
        blockers=[],
        runnability="forward_testable" if state.startswith("supported") else "descriptive_only",
        forward_test_design=design if state.startswith("supported") else None,
    )
```

- [ ] **Step 4: Verify**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_method_sizing_runnability.py -q`

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/winning_wallet_thesis/methods/sizing.py sniper/analysis/winning_wallet_thesis/methods/runnability.py tests/winning_wallet_thesis/test_method_sizing_runnability.py
git commit -m "feat(thesis): add cluster sizing and runnability detectors"
```

---

## Task 7: Detector runner + assembler integration

**Files:**
- Create: `sniper/analysis/winning_wallet_thesis/methods/runner.py`
- Modify: `sniper/analysis/winning_wallet_thesis/types.py`
- Modify: `sniper/analysis/winning_wallet_thesis/assembler.py`
- Test: `tests/winning_wallet_thesis/test_method_runner.py`

- [ ] **Step 1: Write failing tests**

```python
from sniper.analysis.winning_wallet_thesis.methods.runner import run_detectors
from sniper.analysis.winning_wallet_thesis.types import EdgeCluster, PositionBook, PositionEpisode


def test_runner_returns_multiple_findings_per_cluster():
    ep = PositionEpisode(
        "0xw", "c1", "e1", 0, 0, buy_shares=100, buy_cost=40,
        resolved=True, won=1, tags=["UFC"], template="ufc-fight-night",
    )
    cluster = EdgeCluster("c", "UFC", {"tag": "UFC"}, "tag", 1, 1, 60, 60,
                          [(ep.condition_id, ep.outcome_index, ep.episode_index)])
    findings = run_detectors(PositionBook("0xw", [ep]), [cluster])
    assert "specialist_domain_edge" in {f.method for f in findings["c"]}
    assert "reward_attribution" in {f.method for f in findings["c"]}
```

- [ ] **Step 2: Run failing tests**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_method_runner.py -q`

Expected: missing runner failure.

- [ ] **Step 3: Implement runner**

```python
from __future__ import annotations

from collections.abc import Callable

from sniper.analysis.winning_wallet_thesis.methods.calibration import favorite_longshot_calibration
from sniper.analysis.winning_wallet_thesis.methods.context import ClusterContext
from sniper.analysis.winning_wallet_thesis.methods.exit_hold import exit_hold_mechanism
from sniper.analysis.winning_wallet_thesis.methods.reward import reward_attribution
from sniper.analysis.winning_wallet_thesis.methods.sizing import sizing_skill
from sniper.analysis.winning_wallet_thesis.methods.specialist import specialist_domain
from sniper.analysis.winning_wallet_thesis.methods.template import recurring_template
from sniper.analysis.winning_wallet_thesis.methods.velocity import capital_velocity
from sniper.analysis.winning_wallet_thesis.types import EdgeCluster, PositionBook

DETECTORS: tuple[Callable[[ClusterContext], object], ...] = (
    specialist_domain,
    recurring_template,
    favorite_longshot_calibration,
    reward_attribution,
    sizing_skill,
    exit_hold_mechanism,
    capital_velocity,
)


def run_detectors(book: PositionBook, clusters: list[EdgeCluster]) -> dict[str, list]:
    out = {}
    for cluster in clusters:
        ctx = ClusterContext(cluster=cluster, book=book)
        out[cluster.cluster_id] = [detector(ctx) for detector in DETECTORS]
    return out
```

- [ ] **Step 4: Integrate with `ThesisProfile`**

Add a field to `ThesisProfile`:

```python
method_findings: dict[str, list] = field(default_factory=dict)
```

In `assembler.py`, after clusters are built:

```python
from sniper.analysis.winning_wallet_thesis.methods.runner import run_detectors

method_findings = run_detectors(book, clusters)
profile = ThesisProfile(..., method_findings=method_findings)
```

- [ ] **Step 5: Verify**

Run:
```bash
python3 -m pytest tests/winning_wallet_thesis/test_method_runner.py tests/winning_wallet_thesis -q
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add sniper/analysis/winning_wallet_thesis tests/winning_wallet_thesis
git commit -m "feat(thesis): run method detectors per edge cluster"
```

---

## Task 8: Phase 2 fixture parity

**Files:**
- Test: `tests/winning_wallet_thesis/test_phase2_profile_parity.py`
- Modify as needed: `sniper/analysis/winning_wallet_thesis/cli.py`, `assembler.py`

- [ ] **Step 1: Write fixture test**

```python
def test_swissmiss_profile_contains_sports_thesis_and_iran_loss_center(cached_swissmiss_profile):
    profile = cached_swissmiss_profile
    headlines = " ".join(t.headline for t in getattr(profile, "theses", []))
    clusters = " ".join(c.label for c in profile.edge_clusters + profile.loss_centers)
    assert "sports" in clusters.lower()
    assert "iran" in clusters.lower() or "middle east" in clusters.lower()
    assert "copy" not in profile.headline.lower()
```

If the fixture helper does not exist yet, make this an explicit cached JSON fixture under `tests/fixtures/winning_wallet_thesis/` and keep it offline.

- [ ] **Step 2: Run fixture test**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_phase2_profile_parity.py -q`

Expected: fail until the assembler serializes method findings/theses.

- [ ] **Step 3: Fix serialization**

Ensure CLI JSON includes:

```json
{
  "edge_clusters": [],
  "loss_centers": [],
  "method_findings": {},
  "theses": [],
  "data_gaps": []
}
```

- [ ] **Step 4: Verify Phase 2**

Run:
```bash
python3 -m pytest tests/winning_wallet_thesis -q
python3 scripts/validate_schema_refs.py
```

Expected: all pass, no schema violations.

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/winning_wallet_thesis tests/winning_wallet_thesis
git commit -m "test(thesis): lock phase2 detector profile parity"
```

## Phase 2 Exit Criteria

- Each edge cluster has method findings.
- Specialist, template, market-weighted calibration, reward, sizing, exit/hold, and velocity detectors exist.
- Blocked/proxy/descriptive methods are visible but never promoted.
- No detector emits copy advice, live triggers, expected PnL, or sizing.
- `python3 -m pytest tests/winning_wallet_thesis -q` passes.
