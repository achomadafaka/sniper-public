# Winning-Wallet Thesis Extractor - Phase 4 Data Expansion Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unblock the high-upside methods Phase 2 must mark as blocked or proxy-only: full event-basket/negRisk accounting, stale-liquidity/CLV, dead-hour timing, crowd/momentum, first-attention, and maker/microstructure.

**Architecture:** Phase 4 adds data adapters and detector upgrades behind explicit data-tier gates. Each upgraded detector must degrade to `blocked_missing_feed` rather than fake proof when the feed is absent. Data capture and backfill are separate from detector scoring.

**Tech Stack:** Python 3, parquet/JSON caches under `runs/`, existing order-book capture snapshots, Data API/Gamma/CLOB clients where already present.

**Depends on:** Phase 1-3 complete.

---

## Scope

Build in this order:

1. event-basket / negRisk exposure accounting;
2. price-path provider for post-entry drift and stale-liquidity proxies;
3. liquidity/dead-hour/crowd-momentum detectors using price-path provider;
4. fill-identity provider contract for first-attention and follower-flow;
5. maker/microstructure detector contracts, mostly blocked until maker/taker/order detail is present.

Cross-venue and news-source proof remain explicit gaps unless a real feed is added.

## File Structure

| File | Responsibility |
|---|---|
| `sniper/analysis/winning_wallet_thesis/data/price_path.py` | Query captured order-book snapshots / price index for condition paths. |
| `sniper/analysis/winning_wallet_thesis/data/fill_identity.py` | Contract for wallet-identified fills; returns unavailable if no source. |
| `sniper/analysis/winning_wallet_thesis/event_basket.py` | Full event-level exposure accounting for negRisk/split/merge baskets. |
| `sniper/analysis/winning_wallet_thesis/methods/liquidity.py` | Stale-liquidity, dead-hour, drift detectors. |
| `sniper/analysis/winning_wallet_thesis/methods/attention.py` | Crowd/momentum/first-attention detector; direct only with fill identity. |
| `sniper/analysis/winning_wallet_thesis/methods/microstructure.py` | Maker/spread/adverse-selection detectors; blocked unless maker/taker/order data exists. |
| `tests/winning_wallet_thesis/test_event_basket_accounting.py` | NegRisk fixtures. |
| `tests/winning_wallet_thesis/test_data_price_path.py` | Price-path provider tests. |
| `tests/winning_wallet_thesis/test_method_liquidity_attention.py` | Feed-gated detector tests. |

---

## Task 1: Full event-basket accounting

**Files:**
- Create: `sniper/analysis/winning_wallet_thesis/event_basket.py`
- Modify: `sniper/analysis/winning_wallet_thesis/position_builder.py`
- Test: `tests/winning_wallet_thesis/test_event_basket_accounting.py`

- [ ] **Step 1: Write failing tests**

```python
from sniper.analysis.winning_wallet_thesis.event_basket import build_event_basket_accounting
from sniper.analysis.winning_wallet_thesis.types import LedgerRow, WalletEventLedger


def test_split_merge_basket_counts_event_once_not_per_leg():
    ledger = WalletEventLedger("0xw", rows=[
        LedgerRow("buy", "0xw", "c_yes", 100, outcome_index=0, price=.45, size=100, usdc=45, event_slug="e"),
        LedgerRow("buy", "0xw", "c_no", 101, outcome_index=1, price=.45, size=100, usdc=45, event_slug="e"),
        LedgerRow("redeem", "0xw", "c_yes", 200, outcome_index=0, size=100, usdc=100, event_slug="e"),
    ])
    baskets = build_event_basket_accounting(ledger)
    assert len(baskets) == 1
    assert baskets["e"].condition_ids == {"c_yes", "c_no"}
```

- [ ] **Step 2: Run failing tests**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_event_basket_accounting.py -q`

Expected: missing module failure.

- [ ] **Step 3: Implement first-pass event accounting**

```python
from __future__ import annotations

from sniper.analysis.winning_wallet_thesis.types import EventBasket, WalletEventLedger


def build_event_basket_accounting(ledger: WalletEventLedger) -> dict[str, EventBasket]:
    baskets: dict[str, EventBasket] = {}
    for row in ledger.rows:
        event = row.event_slug or row.condition_id
        if not event:
            event = "wallet"
        basket = baskets.setdefault(event, EventBasket(event_slug=event))
        if row.condition_id:
            basket.condition_ids.add(row.condition_id)
        if row.kind == "buy":
            basket.net_cost += row.usdc
        elif row.kind == "sell":
            basket.net_proceeds += row.usdc
        elif row.kind == "redeem":
            basket.redeem_payout += row.usdc
        elif row.kind == "split":
            basket.split_shares += row.size
        elif row.kind == "merge":
            basket.merge_shares += row.size
        elif row.kind == "reward":
            basket.rewards += row.usdc
    for basket in baskets.values():
        basket.final_pnl = basket.net_proceeds + basket.redeem_payout + basket.rewards - basket.net_cost
    return baskets
```

- [ ] **Step 4: Add negRisk fixture**

Add a fixture where one event has multiple condition IDs and one final event PnL. Assert:

```python
assert basket.final_pnl == basket.net_proceeds + basket.redeem_payout + basket.rewards - basket.net_cost
assert len(baskets) == 1
```

- [ ] **Step 5: Verify**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_event_basket_accounting.py -q`

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add sniper/analysis/winning_wallet_thesis/event_basket.py tests/winning_wallet_thesis/test_event_basket_accounting.py
git commit -m "feat(thesis): add event-basket accounting"
```

---

## Task 2: Price-path provider

**Files:**
- Create: `sniper/analysis/winning_wallet_thesis/data/price_path.py`
- Test: `tests/winning_wallet_thesis/test_data_price_path.py`

- [ ] **Step 1: Write failing tests**

```python
from sniper.analysis.winning_wallet_thesis.data.price_path import InMemoryPricePathProvider


def test_price_path_returns_none_when_condition_absent():
    provider = InMemoryPricePathProvider({})
    assert provider.path("c1", start_ts=100, end_ts=200) is None


def test_price_path_filters_window():
    provider = InMemoryPricePathProvider({"c1": [(90, .4), (110, .5), (210, .6)]})
    assert provider.path("c1", start_ts=100, end_ts=200) == [(110, .5)]
```

- [ ] **Step 2: Run failing tests**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_data_price_path.py -q`

Expected: missing module failure.

- [ ] **Step 3: Implement provider contract**

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class PricePathProvider(Protocol):
    def path(self, condition_id: str, *, start_ts: int, end_ts: int) -> list[tuple[int, float]] | None:
        ...


@dataclass
class InMemoryPricePathProvider:
    rows: dict[str, list[tuple[int, float]]]

    def path(self, condition_id: str, *, start_ts: int, end_ts: int) -> list[tuple[int, float]] | None:
        if condition_id not in self.rows:
            return None
        return [(ts, price) for ts, price in self.rows[condition_id] if start_ts <= ts <= end_ts]
```

- [ ] **Step 4: Verify**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_data_price_path.py -q`

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/winning_wallet_thesis/data/price_path.py tests/winning_wallet_thesis/test_data_price_path.py
git commit -m "feat(thesis): add price-path provider contract"
```

---

## Task 3: Stale-liquidity and dead-hour detectors

**Files:**
- Create: `sniper/analysis/winning_wallet_thesis/methods/liquidity.py`
- Test: `tests/winning_wallet_thesis/test_method_liquidity.py`

- [ ] **Step 1: Write failing tests**

```python
from sniper.analysis.winning_wallet_thesis.data.price_path import InMemoryPricePathProvider
from sniper.analysis.winning_wallet_thesis.methods.context import ClusterContext
from sniper.analysis.winning_wallet_thesis.methods.liquidity import stale_liquidity, dead_hour_timing
from sniper.analysis.winning_wallet_thesis.types import EdgeCluster, PositionBook, PositionEpisode


def _ctx():
    ep = PositionEpisode("0xw", "c1", "e1", 0, 0, buy_shares=10, buy_cost=4,
                         first_buy_ts=100, resolved=True, won=1)
    cluster = EdgeCluster("c", "test", {}, "tag", 1, 1, 1, 10,
                          [(ep.condition_id, ep.outcome_index, ep.episode_index)])
    return ClusterContext(cluster, PositionBook("0xw", [ep]))


def test_stale_liquidity_blocks_without_price_path():
    finding = stale_liquidity(_ctx(), price_path=None)
    assert finding.state == "blocked_missing_feed"
    assert "missing_price_path" in finding.blockers


def test_stale_liquidity_uses_post_entry_drift_when_available():
    provider = InMemoryPricePathProvider({"c1": [(110, .55), (200, .60)]})
    finding = stale_liquidity(_ctx(), price_path=provider)
    assert finding.evidence["mean_post_entry_drift"] > 0
```

- [ ] **Step 2: Run failing tests**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_method_liquidity.py -q`

Expected: missing module failure.

- [ ] **Step 3: Implement feed-gated detectors**

```python
from __future__ import annotations

from sniper.analysis.winning_wallet_thesis.data.price_path import PricePathProvider
from sniper.analysis.winning_wallet_thesis.methods.context import ClusterContext
from sniper.analysis.winning_wallet_thesis.methods.types import ForwardTestDesign, MethodFinding


def stale_liquidity(ctx: ClusterContext, *, price_path: PricePathProvider | None, horizon_seconds: int = 3600) -> MethodFinding:
    if price_path is None:
        return MethodFinding("stale_liquidity_sniping", "blocked_missing_feed", 0, None, 0,
                             evidence={}, controls={}, blockers=["missing_price_path"],
                             runnability="data_blocked")
    drifts = []
    for ep in ctx.episodes:
        if ep.first_buy_ts is None:
            continue
        path = price_path.path(ep.condition_id, start_ts=ep.first_buy_ts, end_ts=ep.first_buy_ts + horizon_seconds)
        if not path:
            continue
        drifts.append(path[-1][1] - ep.entry_vwap)
    if not drifts:
        return MethodFinding("stale_liquidity_sniping", "blocked_missing_feed", 0, None, 0,
                             evidence={}, controls={}, blockers=["missing_price_path_window"],
                             runnability="data_blocked")
    effect = sum(drifts) / len(drifts)
    state = "supported_proxy" if effect > 0 else "contradicted"
    design = ForwardTestDesign("post_entry_drift_gap", "1h", 0.01, -0.01, 20)
    return MethodFinding("stale_liquidity_sniping", state, max(effect, 0) * 100, effect, len(drifts),
                         evidence={"mean_post_entry_drift": effect}, controls={"guard": "proxy_not_proof"},
                         blockers=[], runnability="forward_testable" if state.startswith("supported") else "descriptive_only",
                         forward_test_design=design if state.startswith("supported") else None)


def dead_hour_timing(ctx: ClusterContext) -> MethodFinding:
    hours = [(__import__("datetime").datetime.utcfromtimestamp(ep.first_buy_ts).hour)
             for ep in ctx.episodes if ep.first_buy_ts is not None]
    dead = [h for h in hours if h in {2, 3, 4, 5, 6, 7, 8, 9}]
    share = len(dead) / len(hours) if hours else 0.0
    return MethodFinding("dead_hour_timing", "descriptive", share, share, len(hours),
                         evidence={"dead_hour_share": share, "dead_hours_utc": [2,3,4,5,6,7,8,9]},
                         controls={"guard": "timing_alone_is_not_edge"},
                         blockers=[], runnability="descriptive_only")
```

- [ ] **Step 4: Verify**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_method_liquidity.py -q`

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/winning_wallet_thesis/methods/liquidity.py tests/winning_wallet_thesis/test_method_liquidity.py
git commit -m "feat(thesis): add price-path gated liquidity detectors"
```

---

## Task 4: Fill-identity contract and attention detectors

**Files:**
- Create: `sniper/analysis/winning_wallet_thesis/data/fill_identity.py`
- Create: `sniper/analysis/winning_wallet_thesis/methods/attention.py`
- Test: `tests/winning_wallet_thesis/test_method_attention.py`

- [ ] **Step 1: Write failing tests**

```python
from sniper.analysis.winning_wallet_thesis.data.fill_identity import UnavailableFillIdentityProvider
from sniper.analysis.winning_wallet_thesis.methods.attention import first_attention_discovery
from sniper.analysis.winning_wallet_thesis.methods.context import ClusterContext
from sniper.analysis.winning_wallet_thesis.types import EdgeCluster, PositionBook


def test_first_attention_blocks_without_identity_feed():
    ctx = ClusterContext(EdgeCluster("c", "x", {}, "tag", 0, 0, 0, 0), PositionBook("0xw"))
    finding = first_attention_discovery(ctx, fill_identity=UnavailableFillIdentityProvider())
    assert finding.state == "blocked_missing_feed"
    assert "missing_wallet_identified_fills" in finding.blockers
```

- [ ] **Step 2: Run failing tests**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_method_attention.py -q`

Expected: missing module failure.

- [ ] **Step 3: Implement blocked-first contract**

```python
# data/fill_identity.py
from __future__ import annotations

from typing import Protocol


class FillIdentityProvider(Protocol):
    available: bool


class UnavailableFillIdentityProvider:
    available = False
```

```python
# methods/attention.py
from __future__ import annotations

from sniper.analysis.winning_wallet_thesis.data.fill_identity import FillIdentityProvider
from sniper.analysis.winning_wallet_thesis.methods.context import ClusterContext
from sniper.analysis.winning_wallet_thesis.methods.types import MethodFinding


def first_attention_discovery(ctx: ClusterContext, *, fill_identity: FillIdentityProvider) -> MethodFinding:
    if not fill_identity.available:
        return MethodFinding("first_attention_discovery", "blocked_missing_feed", 0, None, 0,
                             evidence={}, controls={}, blockers=["missing_wallet_identified_fills"],
                             runnability="data_blocked")
    return MethodFinding("first_attention_discovery", "descriptive", 0, None, len(ctx.episodes),
                         evidence={"state": "provider_available_detector_not_implemented"},
                         controls={}, blockers=[], runnability="descriptive_only")
```

- [ ] **Step 4: Verify**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_method_attention.py -q`

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/winning_wallet_thesis/data/fill_identity.py sniper/analysis/winning_wallet_thesis/methods/attention.py tests/winning_wallet_thesis/test_method_attention.py
git commit -m "feat(thesis): add fill-identity gate for attention methods"
```

---

## Task 5: Microstructure method contracts

**Files:**
- Create: `sniper/analysis/winning_wallet_thesis/methods/microstructure.py`
- Test: `tests/winning_wallet_thesis/test_method_microstructure.py`

- [ ] **Step 1: Write failing tests**

```python
from sniper.analysis.winning_wallet_thesis.methods.context import ClusterContext
from sniper.analysis.winning_wallet_thesis.methods.microstructure import market_making_spread_capture
from sniper.analysis.winning_wallet_thesis.types import EdgeCluster, PositionBook


def test_market_making_blocks_without_maker_taker_feed():
    finding = market_making_spread_capture(ClusterContext(EdgeCluster("c", "x", {}, "tag", 0, 0, 0, 0), PositionBook("0xw")))
    assert finding.state == "blocked_missing_feed"
    assert "missing_maker_taker_or_order_detail" in finding.blockers
```

- [ ] **Step 2: Run failing tests**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_method_microstructure.py -q`

Expected: missing module failure.

- [ ] **Step 3: Implement contracts**

```python
from __future__ import annotations

from sniper.analysis.winning_wallet_thesis.methods.context import ClusterContext
from sniper.analysis.winning_wallet_thesis.methods.types import MethodFinding


def market_making_spread_capture(ctx: ClusterContext) -> MethodFinding:
    return MethodFinding("market_making_spread_capture", "blocked_missing_feed", 0, None, 0,
                         evidence={}, controls={}, blockers=["missing_maker_taker_or_order_detail"],
                         runnability="data_blocked")


def adverse_selection_aware_making(ctx: ClusterContext) -> MethodFinding:
    return MethodFinding("adverse_selection_aware_making", "blocked_missing_feed", 0, None, 0,
                         evidence={}, controls={}, blockers=["missing_passive_fill_baseline"],
                         runnability="data_blocked")
```

- [ ] **Step 4: Verify**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_method_microstructure.py -q`

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/winning_wallet_thesis/methods/microstructure.py tests/winning_wallet_thesis/test_method_microstructure.py
git commit -m "feat(thesis): expose microstructure methods as feed-blocked"
```

## Phase 4 Exit Criteria

- Full event-basket accounting has a negRisk/split/merge fixture.
- Stale-liquidity direct detector runs only with price path; otherwise blocked.
- First-attention and microstructure methods visibly block on missing feeds.
- No detector invents news, cross-venue, maker/taker, or identity proof.
- `python3 -m pytest tests/winning_wallet_thesis -q` passes.
