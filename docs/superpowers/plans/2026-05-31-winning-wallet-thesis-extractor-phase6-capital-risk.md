# Winning-Wallet Thesis Extractor - Phase 6 Capital/Risk Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add per-slice and per-cluster capital/risk decomposition to the winning-wallet thesis extractor, including budget share, bet-size concentration, turnover ROI, drawdown approximations, and exit value added.

**Architecture:** Keep capital/risk separate from edge slicing. `slicer.py` continues to answer where `(r - p)` and PnL live. New `capital_risk.py` answers how the wallet allocates and manages money. The assembler attaches a `CapitalRiskProfile` and warnings without changing validation or trade behavior.

**Tech Stack:** Python 3, dataclasses, `pytest`, existing `PositionBook`, `PositionEpisode`, `EdgeCluster`, `ThesisProfile`.

**Spec:** `docs/superpowers/specs/2026-05-31-winning-wallet-capital-risk-design.md`

---

## File Structure

| File | Responsibility |
|---|---|
| `sniper/analysis/winning_wallet_thesis/capital_risk.py` | Capital/risk metrics, exit value, per-axis/per-cluster profiles. |
| `sniper/analysis/winning_wallet_thesis/types.py` | Add `CapitalRiskSlice`, `ExitValueSummary`, `CapitalRiskProfile`; add `capital_risk` field to `ThesisProfile`. |
| `sniper/analysis/winning_wallet_thesis/assembler.py` | Attach capital/risk profile and warnings. |
| `tests/winning_wallet_thesis/test_capital_risk.py` | Unit tests for capital/risk metrics. |
| `tests/winning_wallet_thesis/test_capital_risk_profile.py` | Integration tests with profile assembler. |

---

## Task 1: Capital/risk types

**Files:**
- Modify: `sniper/analysis/winning_wallet_thesis/types.py`
- Test: `tests/winning_wallet_thesis/test_capital_risk.py`

- [ ] **Step 1: Write failing test**

```python
from sniper.analysis.winning_wallet_thesis.types import CapitalRiskSlice, ExitValueSummary


def test_capital_risk_types_hold_budget_and_exit_fields():
    row = CapitalRiskSlice(
        slice_key="UFC",
        axis="tag",
        n_positions=10,
        n_events=8,
        buy_cost=1000.0,
        budget_share_pct=25.0,
        realized_pnl=100.0,
        turnover_roi_pct=10.0,
        avg_bet=100.0,
        median_bet=90.0,
        p90_bet=200.0,
        max_bet=250.0,
        bet_cv=0.5,
        top_event_budget_share_pct=20.0,
        peak_deployed=None,
        roi_on_peak_deployed_pct=None,
        max_drawdown=None,
        exit_value_added=None,
        loser_rescue_value=None,
        winner_upside_lost=None,
    )
    exits = ExitValueSummary(2, 1, 1, -10.0, 40.0, -50.0)
    assert row.slice_key == "UFC"
    assert exits.loser_rescue_value == 40.0
```

- [ ] **Step 2: Run failing test**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_capital_risk.py::test_capital_risk_types_hold_budget_and_exit_fields -q`

Expected: import failure for `CapitalRiskSlice`.

- [ ] **Step 3: Implement types**

Add to `types.py`:

```python
@dataclass
class CapitalRiskSlice:
    slice_key: str
    axis: str
    n_positions: int
    n_events: int
    buy_cost: float
    budget_share_pct: float
    realized_pnl: float
    turnover_roi_pct: float
    avg_bet: float
    median_bet: float
    p90_bet: float
    max_bet: float
    bet_cv: float
    top_event_budget_share_pct: float
    peak_deployed: float | None
    roi_on_peak_deployed_pct: float | None
    max_drawdown: float | None
    exit_value_added: float | None
    loser_rescue_value: float | None
    winner_upside_lost: float | None


@dataclass
class ExitValueSummary:
    n_sold: int
    n_sold_losers: int
    n_sold_winners: int
    sell_vs_hold_value: float
    loser_rescue_value: float
    winner_upside_lost: float


@dataclass
class CapitalRiskProfile:
    wallet: str
    total_buy_cost: float
    total_realized_pnl: float
    slices: dict[str, list[CapitalRiskSlice]] = field(default_factory=dict)
    cluster_risk: dict[str, CapitalRiskSlice] = field(default_factory=dict)
    exit_summary: ExitValueSummary | None = None
    warnings: list[str] = field(default_factory=list)
```

Add to `ThesisProfile`:

```python
capital_risk: CapitalRiskProfile | None = None
```

- [ ] **Step 4: Verify**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_capital_risk.py::test_capital_risk_types_hold_budget_and_exit_fields -q`

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/winning_wallet_thesis/types.py tests/winning_wallet_thesis/test_capital_risk.py
git commit -m "feat(thesis): add capital risk data objects"
```

---

## Task 2: Bet distribution and budget rows

**Files:**
- Create: `sniper/analysis/winning_wallet_thesis/capital_risk.py`
- Test: `tests/winning_wallet_thesis/test_capital_risk.py`

- [ ] **Step 1: Write failing tests**

```python
from sniper.analysis.winning_wallet_thesis.capital_risk import capital_row
from sniper.analysis.winning_wallet_thesis.types import PositionEpisode


def _ep(event, cost, pnl):
    return PositionEpisode(
        wallet="0xw",
        condition_id=event,
        event_slug=event,
        outcome_index=0,
        episode_index=0,
        buy_shares=100,
        buy_cost=cost,
        sell_proceeds=cost + pnl,
        sell_shares=100,
        resolved=True,
        won=1,
    )


def test_capital_row_computes_budget_share_and_bet_distribution():
    row = capital_row("UFC", "tag", [_ep("e1", 100, 10), _ep("e2", 300, 30)], total_buy_cost=1000)
    assert row.buy_cost == 400
    assert row.budget_share_pct == 40.0
    assert row.avg_bet == 200
    assert row.max_bet == 300
    assert row.turnover_roi_pct == 10.0
```

- [ ] **Step 2: Run failing test**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_capital_risk.py::test_capital_row_computes_budget_share_and_bet_distribution -q`

Expected: missing `capital_risk` module.

- [ ] **Step 3: Implement helpers**

```python
from __future__ import annotations

import math
from statistics import mean, median

from sniper.analysis.winning_wallet_thesis.types import CapitalRiskSlice, PositionEpisode


def _pct(num: float, den: float) -> float:
    return num / den * 100.0 if den else 0.0


def _p90(values: list[float]) -> float:
    if not values:
        return 0.0
    vals = sorted(values)
    idx = min(len(vals) - 1, math.ceil(0.9 * len(vals)) - 1)
    return vals[idx]


def _cv(values: list[float]) -> float:
    if not values:
        return 0.0
    m = mean(values)
    if m == 0:
        return 0.0
    var = sum((v - m) ** 2 for v in values) / len(values)
    return math.sqrt(var) / m


def capital_row(slice_key: str, axis: str, episodes: list[PositionEpisode], *,
                total_buy_cost: float) -> CapitalRiskSlice:
    costs = [ep.buy_cost for ep in episodes]
    buy_cost = sum(costs)
    pnl = sum(ep.realized_pnl or 0.0 for ep in episodes)
    by_event: dict[str, float] = {}
    for ep in episodes:
        by_event[ep.event_slug] = by_event.get(ep.event_slug, 0.0) + ep.buy_cost
    return CapitalRiskSlice(
        slice_key=slice_key,
        axis=axis,
        n_positions=len(episodes),
        n_events=len({ep.event_slug for ep in episodes if ep.event_slug}),
        buy_cost=round(buy_cost, 2),
        budget_share_pct=round(_pct(buy_cost, total_buy_cost), 2),
        realized_pnl=round(pnl, 2),
        turnover_roi_pct=round(_pct(pnl, buy_cost), 2),
        avg_bet=round(mean(costs), 2) if costs else 0.0,
        median_bet=round(median(costs), 2) if costs else 0.0,
        p90_bet=round(_p90(costs), 2),
        max_bet=round(max(costs), 2) if costs else 0.0,
        bet_cv=round(_cv(costs), 3),
        top_event_budget_share_pct=round(_pct(max(by_event.values(), default=0.0), buy_cost), 2),
        peak_deployed=None,
        roi_on_peak_deployed_pct=None,
        max_drawdown=None,
        exit_value_added=None,
        loser_rescue_value=None,
        winner_upside_lost=None,
    )
```

- [ ] **Step 4: Verify**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_capital_risk.py::test_capital_row_computes_budget_share_and_bet_distribution -q`

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/winning_wallet_thesis/capital_risk.py tests/winning_wallet_thesis/test_capital_risk.py
git commit -m "feat(thesis): compute capital risk rows"
```

---

## Task 3: Exit value summary

**Files:**
- Modify: `sniper/analysis/winning_wallet_thesis/capital_risk.py`
- Test: `tests/winning_wallet_thesis/test_capital_risk.py`

- [ ] **Step 1: Write failing test**

```python
from sniper.analysis.winning_wallet_thesis.capital_risk import exit_value_summary
from sniper.analysis.winning_wallet_thesis.types import PositionEpisode


def test_exit_value_splits_loser_rescue_and_winner_upside_lost():
    loser = PositionEpisode("0xw", "c1", "e1", 0, 0, buy_shares=100, buy_cost=60,
                            sell_shares=100, sell_proceeds=40, resolved=True, won=0)
    winner = PositionEpisode("0xw", "c2", "e2", 0, 0, buy_shares=100, buy_cost=60,
                             sell_shares=100, sell_proceeds=70, resolved=True, won=1)
    out = exit_value_summary([loser, winner])
    assert out.n_sold == 2
    assert out.loser_rescue_value == 40
    assert out.winner_upside_lost == -30
    assert out.sell_vs_hold_value == 10
```

- [ ] **Step 2: Run failing test**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_capital_risk.py::test_exit_value_splits_loser_rescue_and_winner_upside_lost -q`

Expected: missing `exit_value_summary`.

- [ ] **Step 3: Implement exit summary**

```python
from sniper.analysis.winning_wallet_thesis.types import ExitValueSummary


def exit_value_summary(episodes: list[PositionEpisode]) -> ExitValueSummary:
    n_sold = n_losers = n_winners = 0
    rescue = 0.0
    upside = 0.0
    total = 0.0
    for ep in episodes:
        if ep.sell_shares <= 0 or not ep.resolved or ep.won is None:
            continue
        n_sold += 1
        payout_if_held = ep.sell_shares * (0.5 if ep.is_push else (1.0 if ep.won else 0.0))
        value = ep.sell_proceeds - payout_if_held
        total += value
        if ep.won:
            n_winners += 1
            upside += value
        else:
            n_losers += 1
            rescue += value
    return ExitValueSummary(
        n_sold=n_sold,
        n_sold_losers=n_losers,
        n_sold_winners=n_winners,
        sell_vs_hold_value=round(total, 2),
        loser_rescue_value=round(rescue, 2),
        winner_upside_lost=round(upside, 2),
    )
```

- [ ] **Step 4: Verify**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_capital_risk.py::test_exit_value_splits_loser_rescue_and_winner_upside_lost -q`

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/winning_wallet_thesis/capital_risk.py tests/winning_wallet_thesis/test_capital_risk.py
git commit -m "feat(thesis): compute exit value added"
```

---

## Task 4: Build profile by axes and clusters

**Files:**
- Modify: `sniper/analysis/winning_wallet_thesis/capital_risk.py`
- Test: `tests/winning_wallet_thesis/test_capital_risk_profile.py`

- [ ] **Step 1: Write failing test**

```python
from sniper.analysis.winning_wallet_thesis.capital_risk import build_capital_risk_profile
from sniper.analysis.winning_wallet_thesis.types import EdgeCluster, PositionBook, PositionEpisode


def _ep(cid, event, tag, cost, pnl):
    return PositionEpisode("0xw", cid, event, 0, 0, buy_shares=100, buy_cost=cost,
                           sell_shares=100, sell_proceeds=cost + pnl,
                           resolved=True, won=1, tags=[tag], subcat=tag)


def test_profile_surfaces_loss_center_overallocation():
    sports = _ep("s", "sports_event", "UFC", 100, 20)
    iran = _ep("i", "iran_event", "Iran", 400, -100)
    cluster = EdgeCluster("iran", "Iran", {"tag": "Iran"}, "tag", 1, 1, -100, -10,
                          [(iran.condition_id, iran.outcome_index, iran.episode_index)],
                          is_loss_center=True)
    profile = build_capital_risk_profile("0xw", PositionBook("0xw", [sports, iran]), [cluster])
    row = profile.cluster_risk["iran"]
    assert row.budget_share_pct == 80.0
    assert "capital_overallocated_to_loss_center" in profile.warnings
```

- [ ] **Step 2: Run failing test**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_capital_risk_profile.py -q`

Expected: missing `build_capital_risk_profile`.

- [ ] **Step 3: Implement builder**

```python
from sniper.analysis.winning_wallet_thesis.types import CapitalRiskProfile, EdgeCluster, PositionBook


def _axis_groups(episodes: list[PositionEpisode]) -> dict[str, dict[str, list[PositionEpisode]]]:
    groups: dict[str, dict[str, list[PositionEpisode]]] = {
        "category": {},
        "subcat": {},
        "template": {},
        "mechanism": {},
    }
    for ep in episodes:
        for axis, key in {
            "category": ep.category or "unknown",
            "subcat": ep.subcat or "untagged",
            "template": ep.template or "untagged",
            "mechanism": ep.exit_mechanism,
        }.items():
            groups[axis].setdefault(key, []).append(ep)
    return groups


def _ep_key(ep: PositionEpisode) -> tuple:
    return (ep.condition_id, ep.outcome_index, ep.episode_index)


def build_capital_risk_profile(wallet: str, book: PositionBook,
                               clusters: list[EdgeCluster]) -> CapitalRiskProfile:
    episodes = book.resolved_directional()
    total_buy = sum(ep.buy_cost for ep in episodes)
    total_pnl = sum(ep.realized_pnl or 0.0 for ep in episodes)
    slices = {
        axis: [capital_row(key, axis, eps, total_buy_cost=total_buy)
               for key, eps in table.items()]
        for axis, table in _axis_groups(episodes).items()
    }
    cluster_risk = {}
    by_key = {_ep_key(ep): ep for ep in episodes}
    warnings: list[str] = []
    for cluster in clusters:
        eps = [by_key[k] for k in cluster.member_position_keys if k in by_key]
        row = capital_row(cluster.label, f"cluster:{cluster.axis}", eps, total_buy_cost=total_buy)
        cluster_risk[cluster.cluster_id] = row
        if cluster.is_loss_center and row.budget_share_pct >= 20:
            warnings.append("capital_overallocated_to_loss_center")
        if row.bet_cv >= 1.5:
            warnings.append("high_bet_concentration")
    exits = exit_value_summary(episodes)
    if exits.loser_rescue_value > 0:
        warnings.append("loss_cutting_adds_value")
    if exits.winner_upside_lost < 0:
        warnings.append("winner_selling_leaks_upside")
    return CapitalRiskProfile(wallet, round(total_buy, 2), round(total_pnl, 2),
                              slices=slices, cluster_risk=cluster_risk,
                              exit_summary=exits,
                              warnings=sorted(set(warnings)))
```

- [ ] **Step 4: Verify**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_capital_risk_profile.py -q`

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/winning_wallet_thesis/capital_risk.py tests/winning_wallet_thesis/test_capital_risk_profile.py
git commit -m "feat(thesis): build capital risk profile"
```

---

## Task 5: Assembler integration

**Files:**
- Modify: `sniper/analysis/winning_wallet_thesis/assembler.py`
- Test: `tests/winning_wallet_thesis/test_capital_risk_profile.py`

- [ ] **Step 1: Write failing test**

```python
from dataclasses import asdict

from sniper.analysis.winning_wallet_thesis.assembler import assemble_profile
from sniper.analysis.winning_wallet_thesis.types import EdgeCluster, PositionBook, PositionEpisode


def test_assembler_attaches_capital_risk_profile():
    ep = PositionEpisode("0xw", "c1", "e1", 0, 0, buy_shares=100, buy_cost=50,
                         sell_shares=100, sell_proceeds=60, resolved=True, won=1)
    cluster = EdgeCluster("c", "edge", {}, "tag", 1, 1, 10, 5,
                          [(ep.condition_id, ep.outcome_index, ep.episode_index)])
    profile = assemble_profile("0xw", PositionBook("0xw", [ep]), {}, None, [cluster])
    blob = asdict(profile)
    assert blob["capital_risk"]["total_buy_cost"] == 50
```

- [ ] **Step 2: Run failing test**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_capital_risk_profile.py::test_assembler_attaches_capital_risk_profile -q`

Expected: `capital_risk` is `None`.

- [ ] **Step 3: Wire assembler**

In `assembler.py`:

```python
from sniper.analysis.winning_wallet_thesis.capital_risk import build_capital_risk_profile
```

Before returning `ThesisProfile`:

```python
capital_risk = build_capital_risk_profile(wallet, book, clusters)
gaps.extend(capital_risk.warnings)
```

Pass:

```python
capital_risk=capital_risk
```

- [ ] **Step 4: Verify**

Run:

```bash
python3 -m pytest tests/winning_wallet_thesis/test_capital_risk_profile.py tests/winning_wallet_thesis/test_phase2_profile_parity.py -q
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/winning_wallet_thesis/assembler.py tests/winning_wallet_thesis/test_capital_risk_profile.py
git commit -m "feat(thesis): attach capital risk to profiles"
```

---

## Task 6: Final verification

- [ ] **Step 1: Run focused suite**

```bash
python3 -m pytest tests/winning_wallet_thesis tests/thesis_cards -q
```

Expected: all pass.

- [ ] **Step 2: Run schema validator**

```bash
python3 scripts/validate_schema_refs.py
```

Expected: no schema violations.

- [ ] **Step 3: Commit any docs update if needed**

Only update docs if implementation materially differs from this plan.

## Phase 6 Exit Criteria

- Per-slice capital/risk rows exist.
- Per-cluster capital/risk rows exist.
- Exit value splits loser rescue and winner upside lost.
- Capital over-allocation to loss centers is visible.
- Profile JSON contains `capital_risk`.
- Existing thesis/conveyor tests stay green.
