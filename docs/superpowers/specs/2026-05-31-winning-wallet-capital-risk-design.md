# Spec - Winning-Wallet Capital and Risk Decomposition

**Date:** 2026-05-31
**Status:** design
**Parent:** `docs/superpowers/specs/2026-05-31-winning-wallet-thesis-extractor-design.md`
**Research note:** `docs/research/2026-05-31-swissmiss-exit-capital-correction.md`

## 1. Goal

Add a capital/risk layer to the winning-wallet thesis extractor.

The current extractor answers:

- where edge clusters are;
- where loss centers are;
- which method class may explain each cluster.

It does not fully answer:

- where the wallet allocates budget;
- whether capital is overallocated to loss centers;
- whether bet sizing is disciplined or concentrated;
- whether exits reduce risk or leak upside;
- whether ROI is real after accounting for peak deployed capital.

This spec adds that missing operating-profile layer.

## 2. Doctrine

Selection edge and money management are separate.

A wallet can have:

- positive selection edge but bad capital allocation;
- bad raw exits but good drawdown control;
- good slices but too much concentration;
- high turnover PnL but weak return on peak deployed capital.

The analyzer must report those separately. Do not collapse capital/risk into one wallet-level score.

## 3. New Objects

### 3.1 `CapitalRiskSlice`

One capital/risk decomposition row for one axis/slice/cluster.

Fields:

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
```

### 3.2 `ExitValueSummary`

Exit value is computed only for sold positions with known resolution.

Fields:

```python
@dataclass
class ExitValueSummary:
    n_sold: int
    n_sold_losers: int
    n_sold_winners: int
    sell_vs_hold_value: float
    loser_rescue_value: float
    winner_upside_lost: float
```

Definitions:

- `hold_value = held_shares * payout_if_held_to_resolution`;
- `sell_vs_hold_value = sell_proceeds - hold_value` for sold shares;
- loser rescue is positive sell value on eventual losers;
- winner upside lost is negative difference on eventual winners.

This is not a copy recommendation. It is an operating diagnostic.

### 3.3 `CapitalRiskProfile`

Wallet-level container:

```python
@dataclass
class CapitalRiskProfile:
    wallet: str
    total_buy_cost: float
    total_realized_pnl: float
    slices: dict[str, list[CapitalRiskSlice]]
    cluster_risk: dict[str, CapitalRiskSlice]
    exit_summary: ExitValueSummary
    warnings: list[str]
```

## 4. Metrics

### 4.1 Budget

`buy_cost = sum(ep.buy_cost)`.

`budget_share_pct = slice_buy_cost / total_buy_cost * 100`.

Use buy cost, not realized PnL, for capital allocation.

### 4.2 Bet-Size Distribution

Compute over `ep.buy_cost`:

- average;
- median;
- p90;
- max;
- coefficient of variation.

High `bet_cv` means sizing is concentrated.

### 4.3 Turnover ROI

`turnover_roi_pct = realized_pnl / buy_cost * 100`.

This is not the same as ROI on deployed capital.

### 4.4 Peak Deployed Capital

Use lifecycle events when timestamps exist:

- buy increases deployed cost;
- sell decreases deployed proportional cost;
- redeem/resolution closes remaining exposure;
- open unresolved exposure remains active.

Approximation is allowed in V1:

- if exit/redeem timestamps are missing, `peak_deployed = None`;
- do not fabricate timestamps;
- report `coverage` in warnings.

### 4.5 Drawdown

Build a cumulative realized PnL path by timestamp:

- sells add realized proceeds minus proportional basis;
- redemptions add payout minus remaining basis;
- unresolved mark-to-market is out of scope until price path feed is present.

V1 drawdown is realized-only. Mark it as such.

### 4.6 Exit Value

For sold positions:

- compare sold shares against hold-to-resolution payoff;
- split by eventual winner/loser.

This catches:

- loss-cutting value;
- winner upside lost;
- net sell-vs-hold value.

## 5. Axes

Compute capital/risk rows for:

- category;
- tag;
- subcat;
- template;
- price regime;
- mechanism;
- hour;
- tag-price;
- each `EdgeCluster`.

The output must make budget concentration visible next to edge clusters. Example:

```text
UFC: +edge, +PnL, 8% turnover ROI, 18% budget share
Iran: -edge, -PnL, -24% turnover ROI, 24% budget share, concentrated bets
```

## 6. Integration

Add `capital_risk` to `ThesisProfile`.

The thesis assembler should use it to generate warnings:

- `capital_overallocated_to_loss_center`;
- `winner_selling_leaks_upside`;
- `loss_cutting_adds_value`;
- `high_bet_concentration`;
- `peak_deployed_unknown`.

Do not use these warnings to suppress edge clusters. They are separate operating facts.

## 7. Testing

Required tests:

- budget share sums to about 100% across partition axes;
- multi-tag axes do not claim partition sums;
- Iran-style loss center can have higher budget than sports winner;
- exit value splits loser rescue from winner upside lost;
- missing timestamps leave peak deployed as `None`;
- cluster risk rows match cluster member positions;
- profile JSON includes `capital_risk`;
- no output uses copy/trade language.

## 8. Non-goals

- No trade sizing recommendation.
- No live portfolio risk engine.
- No mark-to-market drawdown without price-path feed.
- No Sharpe/volatility claims from sparse resolved-only data.
- No mutation of validation or paper enrollment.

## 9. Acceptance Criteria

- Cached SwissMiss profile can show sports edge and Iran capital over-allocation simultaneously.
- Exit readout says loss-cutting value and winner upside lost separately.
- Capital/risk rows are attached per cluster and major slice.
- Existing 208 thesis/conveyor tests remain green.
