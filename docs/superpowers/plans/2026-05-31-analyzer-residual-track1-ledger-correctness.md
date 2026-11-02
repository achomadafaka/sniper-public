# Analyzer Residual — Track 1: Ledger Correctness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the analyzer's PnL/significance accounting correct from the ledger we already have — full split/merge re-accounting (superseding the inventory band-aid), reward-denomination verification, and `finding.n` standardization.

**Architecture:** A new pure `position_ledger.py` does avg-cost per-leg share accounting (buy/split/sell/merge/redeem) so split-minted shares land on per-outcome episodes with correct cost basis. `position_builder` feeds it; `PositionEpisode.realized_pnl` is restated; the wallet-level `inventory_net_cash` band-aid is removed in favour of an `inventory_reconciliation ≈ 0` guard. Two small fixes round it out.

**Tech Stack:** Python 3, dataclasses, pytest. No new data sources.

**Spec:** `docs/superpowers/specs/2026-05-31-analyzer-residual-work-design.md` §3.

**Branch:** create `feat/analyzer-track1-ledger` before Task 1 (per branch hygiene); merge to main at the end.

---

## File Structure

| File | Responsibility |
|---|---|
| `sniper/analysis/winning_wallet_thesis/position_ledger.py` | NEW. Pure per-leg accountant (`account_leg`, consumes pre-allocated split/merge cash) + condition-pair allocators (`allocate_split_pair`, `allocate_merge_pair`). |
| `sniper/analysis/winning_wallet_thesis/types.py` | Add `split_cost`/`merge_cash` to `PositionEpisode`; restate `realized_pnl`; `is_directional` UNCHANGED (buy-driven); add `PositionBook.resolved_with_pnl`; adjust `CapitalRiskProfile` (drop band-aid, add `inventory_reconciliation`). |
| `sniper/analysis/winning_wallet_thesis/position_builder.py` | Attribute SPLIT/MERGE pair cash onto BOTH outcome legs (still records the basket view). Selection population unchanged. |
| `sniper/analysis/winning_wallet_thesis/capital_risk.py` | Replace `inventory_net_cash` correction with reconciliation guard. |
| `sniper/analysis/winning_wallet_thesis/methods/types.py` | Add `n_events: int` to `MethodFinding`. |
| `sniper/analysis/winning_wallet_thesis/methods/*.py` | Each detector sets `n_events`. |
| `sniper/analysis/winning_wallet_thesis/validation/cards.py` | Proxy p-value uses `n_events`. |
| `sniper/analysis/winning_wallet_thesis/slicer.py` + `methods/reward.py` | Reward-denomination flag. |

---

## Task 1: `position_ledger` — avg-cost per-leg accountant

**Files:**
- Create: `sniper/analysis/winning_wallet_thesis/position_ledger.py`
- Test: `tests/winning_wallet_thesis/test_position_ledger.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/winning_wallet_thesis/test_position_ledger.py
from sniper.analysis.winning_wallet_thesis.position_ledger import LegEvent, account_leg


def test_plain_held_winner():
    # buy 100 @0.40 ($40), hold to a WIN -> redeem 100*$1 - $40 = +$60
    r = account_leg([LegEvent("buy", 100, 40.0)], resolved=True, won=1, is_push=False)
    assert round(r.realized_pnl, 2) == 60.0
    assert r.split_shares == 0.0


def test_split_held_to_win_uses_allocated_cost():
    # split mints 100 shares of this leg; position_builder allocated $50 of the pair cost here.
    # Hold to WIN -> 100 payout - 50 cost = +50.
    r = account_leg([LegEvent("split", 100, 50.0)], resolved=True, won=1, is_push=False)
    assert r.split_shares == 100.0
    assert round(r.split_cost, 2) == 50.0
    assert round(r.realized_pnl, 2) == 50.0


def test_split_plus_buy_cost_accumulates():
    # buy 100 @0.80 ($80); split 100 allocated $80 to this leg (builder priced at the 0.80 leg).
    # Lose -> 0 payout - (80 buy + 80 split) = -160.
    r = account_leg([LegEvent("buy", 100, 80.0), LegEvent("split", 100, 80.0)],
                    resolved=True, won=0, is_push=False)
    assert round(r.split_cost, 2) == 80.0
    assert round(r.realized_pnl, 2) == -160.0


def test_sell_realizes_against_avg_cost():
    # buy 100 @0.40, sell 50 @0.60 -> realized 50*(0.60-0.40)=+10 ; hold 50 to WIN -> +50-20=+30
    r = account_leg([LegEvent("buy", 100, 40.0), LegEvent("sell", 50, 30.0)],
                    resolved=True, won=1, is_push=False)
    assert round(r.realized_pnl, 2) == 40.0               # sell +10, held 50: 50 - 20 = +30


def test_merge_realizes_pair_pnl():
    # buy 100 @0.40 ($40); MERGE 100 with $50 cash allocated here -> +50-40 = +10.
    # (merge is NOT cost-neutral in general: cheap basis + face-value cash = real gain.)
    r = account_leg([LegEvent("buy", 100, 40.0), LegEvent("merge", 100, 50.0)],
                    resolved=True, won=1, is_push=False)
    assert r.merge_shares == 100.0 and round(r.merge_cash, 2) == 50.0
    assert round(r.realized_pnl, 2) == 10.0 and r.held_shares == 0.0


def test_split_then_merge_roundtrip_is_neutral():
    # split alloc $50 in, merge alloc $50 out on the same shares -> cost-neutral round trip.
    r = account_leg([LegEvent("split", 100, 50.0), LegEvent("merge", 100, 50.0)],
                    resolved=True, won=1, is_push=False)
    assert round(r.realized_pnl, 2) == 0.0 and r.held_shares == 0.0
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_position_ledger.py -q`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement `position_ledger.py`**

```python
# sniper/analysis/winning_wallet_thesis/position_ledger.py
"""Avg-cost per-(condition,outcome,episode) leg accountant (Track 1a). SPLIT/MERGE are
CONDITION-PAIR events (a complete set = 1 share of every outcome = $1; raw rows carry no
outcome index and usdcSize == size). The pair cash is real and known; only its ALLOCATION
across legs is synthesized, and that is done by position_builder (which sees both legs).
This module CONSUMES the pre-allocated per-leg cash: split LegEvent.usdc = cost basis
allocated to this leg; merge LegEvent.usdc = cash proceeds allocated to this leg. Merge
realizes real PnL (cash - avg cost of burned shares) — buying both legs cheap then merging
for face value is a genuine gain, NOT cost-neutral. Supersedes the inventory_net_cash band-aid."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LegEvent:
    kind: str          # buy | split | sell | merge
    shares: float
    usdc: float        # buy: paid · sell: received · split: allocated cost · merge: allocated proceeds


@dataclass
class LegResult:
    realized_pnl: float
    held_shares: float
    total_in_shares: float
    total_cost: float
    split_shares: float
    split_cost: float
    merge_shares: float
    merge_cash: float


def account_leg(events: list[LegEvent], *, resolved: bool, won: int | None,
                is_push: bool) -> LegResult:
    shares = cost = realized = 0.0
    split_shares = split_cost = merge_shares = merge_cash = 0.0
    in_shares = in_cost = 0.0
    for e in events:
        if e.kind == "buy":
            shares += e.shares; cost += e.usdc
            in_shares += e.shares; in_cost += e.usdc
        elif e.kind == "split":
            # usdc = pre-allocated cost basis for THIS leg (pair allocs sum to the split's cash)
            shares += e.shares; cost += e.usdc
            in_shares += e.shares; in_cost += e.usdc
            split_shares += e.shares; split_cost += e.usdc
        elif e.kind == "sell":
            avg = cost / shares if shares > 0 else 0.0
            realized += e.usdc - avg * e.shares
            shares -= e.shares; cost -= avg * e.shares
        elif e.kind == "merge":
            # usdc = pre-allocated cash proceeds for THIS leg; realize cash vs avg cost burned
            avg = cost / shares if shares > 0 else 0.0
            realized += e.usdc - avg * e.shares
            merge_shares += e.shares; merge_cash += e.usdc
            shares -= e.shares; cost -= avg * e.shares
    held = max(shares, 0.0)
    if resolved:
        payout = held * (0.5 if is_push else (1.0 if won else 0.0))
        realized += payout - cost                                   # redeem remainder, write off its cost
    return LegResult(realized_pnl=round(realized, 6), held_shares=round(held, 6),
                     total_in_shares=round(in_shares, 6), total_cost=round(in_cost, 6),
                     split_shares=round(split_shares, 6), split_cost=round(split_cost, 6),
                     merge_shares=round(merge_shares, 6), merge_cash=round(merge_cash, 6))
```

Also update the Step 4 expected count: **6 tests** (not 5).

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_position_ledger.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/winning_wallet_thesis/position_ledger.py tests/winning_wallet_thesis/test_position_ledger.py
git commit -m "feat(thesis): avg-cost per-leg position ledger (split re-accounting core)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Attribute split/merge to episodes (pair allocation; selection population unchanged)

> **Design (verified against real data, supersedes the original draft):** split/merge are
> condition-pair complete-set events ($1/pair, no outcome index). SwissMiss's split book is a
> $34.7M round-trip dominated by ONE condition (0xe2bdf6) with **zero directional trades** —
> capital parked in complete sets for yield, NOT a selection bet. Therefore split-minted legs
> are attributed for the wallet TOTAL but are **NOT** selection bets: `is_directional` stays
> `buy_shares > 0`, so the selection edge population is untouched (no manufactured bets, no
> phantom canceling pairs). The pair cost ALLOCATION lives in `position_ledger` (pure) and is
> applied by `position_builder` (which sees both legs).

**Files:**
- Modify: `sniper/analysis/winning_wallet_thesis/position_ledger.py` (add `allocate_split_pair`, `allocate_merge_pair`)
- Modify: `sniper/analysis/winning_wallet_thesis/types.py` (`split_cost`/`merge_cash` fields; restate `realized_pnl`; `PositionBook.resolved_with_pnl`)
- Modify: `sniper/analysis/winning_wallet_thesis/position_builder.py`
- Test: `tests/winning_wallet_thesis/test_position_ledger.py`, `tests/winning_wallet_thesis/test_position_builder.py`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/winning_wallet_thesis/test_position_ledger.py
from sniper.analysis.winning_wallet_thesis.position_ledger import allocate_split_pair, allocate_merge_pair


def test_allocate_split_pair_uses_revealed_price_and_sums_to_cash():
    a0, a1 = allocate_split_pair(cash=100.0, vwap0=0.80, vwap1=None)
    assert round(a0, 2) == 80.0 and round(a1, 2) == 20.0       # leg0 revealed 0.80 -> leg1 0.20
    b0, b1 = allocate_split_pair(cash=100.0, vwap0=None, vwap1=0.30)
    assert round(b0, 2) == 70.0 and round(b1, 2) == 30.0       # leg1 revealed 0.30 -> leg0 0.70
    c0, c1 = allocate_split_pair(cash=100.0, vwap0=None, vwap1=None)
    assert c0 == 50.0 and c1 == 50.0                            # no revealed price -> 50/50
    assert round(sum(allocate_split_pair(215000.0, None, 0.30)), 2) == 215000.0


def test_allocate_merge_pair_is_5050():
    assert allocate_merge_pair(100.0) == (50.0, 50.0)
```

```python
# append to tests/winning_wallet_thesis/test_position_builder.py
def test_split_then_sell_one_leg_captured_in_total_not_selection():
    # SPLIT mints YES+NO; SELL the NO leg -> net-long YES via the split. The YES leg carries
    # split_shares (counted in the wallet TOTAL) but is NOT a selection bet (no buy) -> it is
    # excluded from the selection edge population.
    acts = [
        {"type": "SPLIT", "conditionId": "c", "eventSlug": "e", "timestamp": 1, "size": 100, "usdcSize": 100},
        {"type": "TRADE", "conditionId": "c", "outcomeIndex": 1, "outcome": "No", "title": "t",
         "eventSlug": "e", "timestamp": 2, "side": "SELL", "size": 100, "usdcSize": 30},
    ]
    book = PB.build_position_book(EnrichedLedger(
        ledger=WalletEventLedger("0xw", rows=L.normalize(acts), raw_activity=acts),
        resolutions={"c": {"resolved": True, "winner": 0, "push": False}},
        tags={"e": ["Sports"]}, templates={"e": "x"}))
    yes = next(e for e in book.episodes if e.outcome_index == 0)
    assert yes.split_shares == 100
    assert not yes.is_directional                       # split-only leg is NOT a selection bet
    assert yes not in book.resolved_directional()       # excluded from selection edge
    assert yes in book.resolved_with_pnl()              # but counted in the wallet total


def test_buy_both_then_merge_realizes_arbitrage_in_total():
    # buy YES@0.55 ($55) + NO@0.40 ($40) = $95/pair, MERGE 100 -> $100 -> +$5, captured in total
    acts = [
        {"type": "TRADE", "conditionId": "c", "outcomeIndex": 0, "outcome": "Y", "title": "t",
         "eventSlug": "e", "timestamp": 1, "side": "BUY", "size": 100, "usdcSize": 55},
        {"type": "TRADE", "conditionId": "c", "outcomeIndex": 1, "outcome": "N", "title": "t",
         "eventSlug": "e", "timestamp": 2, "side": "BUY", "size": 100, "usdcSize": 40},
        {"type": "MERGE", "conditionId": "c", "eventSlug": "e", "timestamp": 3, "size": 100, "usdcSize": 100},
    ]
    book = PB.build_position_book(EnrichedLedger(
        ledger=WalletEventLedger("0xw", rows=L.normalize(acts), raw_activity=acts),
        resolutions={"c": {"resolved": True, "winner": 0, "push": False}},
        tags={"e": ["Sports"]}, templates={"e": "x"}))
    total = sum(e.realized_pnl or 0 for e in book.resolved_with_pnl())
    assert round(total, 2) == 5.0
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_position_ledger.py tests/winning_wallet_thesis/test_position_builder.py -q`
Expected: FAIL (`allocate_*` missing; split shares not attributed; `resolved_with_pnl` missing).

- [ ] **Step 3: Add the pair-allocation helpers to `position_ledger.py`**

```python
def allocate_split_pair(cash: float, vwap0: float | None, vwap1: float | None) -> tuple[float, float]:
    """Allocate a SPLIT's actual cash across the binary pair so the two legs sum to `cash`.
    Use the wallet's own revealed price where a leg has a buy VWAP (leg0 at p, leg1 at 1-p);
    else 50/50. Guarantees pair-sum == cash regardless of which leg revealed a price."""
    if vwap0 is not None:
        p = vwap0
    elif vwap1 is not None:
        p = 1.0 - vwap1
    else:
        p = 0.5
    p = min(1.0, max(0.0, p))
    return round(p * cash, 6), round((1.0 - p) * cash, 6)


def allocate_merge_pair(cash: float) -> tuple[float, float]:
    """A complete set redeems $1 with no canonical per-leg face value; split the MERGE cash
    50/50. Per-leg merge ROI is approximate; the pair total is exact (== cash)."""
    return round(cash / 2.0, 6), round(cash / 2.0, 6)
```

- [ ] **Step 4: `types.py` — fields, restated `realized_pnl`, complete-population accessor**

In `PositionEpisode`, add (with the other fields):
```python
    split_cost: float = 0.0          # cost basis allocated to this leg's split-minted shares
    merge_cash: float = 0.0          # cash proceeds allocated to this leg on MERGE
```
Leave `is_directional` UNCHANGED (`buy_shares > 0`) — split/merge legs are structural, not selection bets. Restate `realized_pnl` (per-leg cash PnL = cash in − cash out):
```python
    @property
    def realized_pnl(self) -> float | None:
        if not self.resolved or self.won is None:
            return None
        total_in = self.buy_shares + self.split_shares
        held = max(total_in - self.sell_shares - self.merge_shares, 0.0)
        payout = held * (0.5 if self.is_push else (1.0 if self.won else 0.0))
        total_cost = self.buy_cost + self.split_cost
        return self.sell_proceeds + self.merge_cash + payout - total_cost
```
Add to `PositionBook` (complete population for the wallet TOTAL — selection bets PLUS structural split/merge legs):
```python
    def resolved_with_pnl(self) -> list[PositionEpisode]:
        return [e for e in self.episodes
                if e.resolved and e.won is not None and not e.is_push
                and (e.buy_shares > 0 or e.split_shares > 0 or e.merge_shares > 0)]
```
(`resolved_directional()` stays buy-driven — the selection slicer keeps using it.)

- [ ] **Step 5: `position_builder` — create per-leg episodes for split/merge and allocate**

Replace the SPLIT/MERGE branch so each binary condition's pair cash is allocated onto both
outcome legs (creating episodes if needed). Track each leg's running buy VWAP for split pricing:
```python
from sniper.analysis.winning_wallet_thesis.position_ledger import allocate_split_pair, allocate_merge_pair
# ...
def _leg_vwap(cond, oidx, episode):
    e = episodes.get((cond, oidx, episode))
    return (e.buy_cost / e.buy_shares) if (e and e.buy_shares > 0) else None
# ... in the row loop, replacing the split/merge branch:
        elif r.kind in ("split", "merge"):
            b = basket(slug, cond)                      # keep the event-basket view
            legs = (0, 1)                               # binary complete set (verified)
            if r.kind == "split":
                b.split_shares += r.size
                v0 = _leg_vwap(cond, 0, current_episode[(cond, 0)])
                v1 = _leg_vwap(cond, 1, current_episode[(cond, 1)])
                allocs = allocate_split_pair(r.usdc, v0, v1)
                for oidx, alloc in zip(legs, allocs):
                    e = get(cond, oidx, "", "", slug, current_episode[(cond, oidx)])
                    e.split_shares += r.size; e.split_cost += alloc
                    e.last_ts = max(e.last_ts, r.ts)
            else:
                b.merge_shares += r.size
                for oidx, cash in zip(legs, allocate_merge_pair(r.usdc)):
                    e = get(cond, oidx, "", "", slug, current_episode[(cond, oidx)])
                    e.merge_shares += r.size; e.merge_cash += cash
                    e.last_ts = max(e.last_ts, r.ts)
```
The existing post-sweep enrichment loop already iterates `episodes.values()`, so the split/merge
legs get category/tags/resolution (and thus `won`) like any other episode. No `account_leg` call is
needed in the builder — `realized_pnl` (Step 4) is the per-leg source of truth and is proven
equivalent to `account_leg` in `test_position_ledger.py`.

> Implementer note: do NOT change `is_directional`. The existing
> `test_split_appears_in_event_basket_not_directional` MUST stay green (a pure split is
> non-directional). Verify `entry_vwap` (buy-only) is unaffected.

- [ ] **Step 6: Run to verify pass + no regression**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_position_ledger.py tests/winning_wallet_thesis/test_position_builder.py tests/winning_wallet_thesis/test_types.py -q`
Expected: PASS (including the unchanged `test_split_appears_in_event_basket_not_directional`).

- [ ] **Step 7: Commit**

```bash
git add sniper/analysis/winning_wallet_thesis/position_ledger.py sniper/analysis/winning_wallet_thesis/types.py sniper/analysis/winning_wallet_thesis/position_builder.py tests/winning_wallet_thesis/test_position_ledger.py tests/winning_wallet_thesis/test_position_builder.py
git commit -m "feat(thesis): pair-allocate split/merge onto legs; selection population unchanged

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Replace the inventory band-aid with a reconciliation guard

**Files:**
- Modify: `sniper/analysis/winning_wallet_thesis/types.py` (`CapitalRiskProfile`)
- Modify: `sniper/analysis/winning_wallet_thesis/capital_risk.py`
- Modify: `sniper/analysis/winning_wallet_thesis/assembler.py` (pass `ledger=` through)
- Test: `tests/winning_wallet_thesis/test_capital_risk_profile.py`

- [ ] **Step 1: Write the failing test**

```python
# replace test_inventory_net_cash_adjusts_total_pnl with:
def test_total_is_reaccounted_and_reconciliation_near_zero():
    from sniper.analysis.winning_wallet_thesis.types import LedgerRow, WalletEventLedger, EnrichedLedger
    from sniper.analysis.winning_wallet_thesis import position_builder as PB
    rows = [
        LedgerRow("split", "0xw", "c", 1, size=100, usdc=100, event_slug="e"),
        LedgerRow("merge", "0xw", "c", 2, size=90, usdc=90, event_slug="e"),
    ]
    led = WalletEventLedger("0xw", rows=rows)
    book = PB.build_position_book(EnrichedLedger(
        ledger=led,
        resolutions={"c": {"resolved": True, "winner": 0, "push": False}},
        tags={"e": ["Sports"]}, templates={"e": "x"}))
    profile = build_capital_risk_profile("0xw", book, [], ledger=led)
    # the attributed split/merge cash must reconcile to the raw net inventory cash flow
    assert abs(profile.inventory_reconciliation) < 1.0
    assert not hasattr(profile, "inventory_net_cash") or profile.inventory_reconciliation is not None
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_capital_risk_profile.py::test_total_is_reaccounted_and_reconciliation_near_zero -q`
Expected: FAIL (`build_capital_risk_profile` has no `ledger=` kwarg / `inventory_reconciliation` not defined).

- [ ] **Step 3: Update `CapitalRiskProfile` (types.py)**

Replace the two band-aid fields:
```python
    inventory_net_cash: float = 0.0                      # split/merge net cash (omitted from directional pnl)
    total_realized_pnl_with_inventory: float = 0.0       # directional + inventory net (true wallet PnL)
```
with:
```python
    total_realized_pnl_with_inventory: float = 0.0       # full wallet realized PnL (selection + structural split/merge)
    inventory_reconciliation: float = 0.0                # raw net inventory cash - attributed; ~0 guard
```

- [ ] **Step 4: Update `build_capital_risk_profile` (capital_risk.py)**

Thread the ledger and compute the total over the complete population plus the reconciliation guard.
Keep the selection slices on `resolved_directional()` (unchanged). Replace the band-aid block:
```python
def build_capital_risk_profile(wallet: str, book: PositionBook,
                               clusters: list[EdgeCluster], *, ledger=None) -> CapitalRiskProfile:
    episodes = book.resolved_directional()               # selection population (slices/clusters)
    total_buy = sum(ep.buy_cost for ep in episodes)
    total_pnl = sum(ep.realized_pnl or 0.0 for ep in episodes)
    # ... (slices, cluster_risk, exits, peak, drawdown unchanged) ...

    # full wallet realized PnL includes structural split/merge legs (e.g. her $34M complete-set book)
    full_pnl = round(sum(ep.realized_pnl or 0.0 for ep in book.resolved_with_pnl()), 2)
    # reconciliation: raw net inventory cash (band-aid quantity) vs attributed (split_cost/merge_cash).
    # ~0 confirms the pair allocation neither dropped nor double-counted any split/merge cash.
    raw_inv = 0.0
    if ledger is not None:
        raw_inv = (sum(r.usdc for r in ledger.rows if r.kind == "merge")
                   - sum(r.usdc for r in ledger.rows if r.kind == "split"))
    attr_inv = sum((ep.merge_cash - ep.split_cost) for ep in book.episodes)
    inventory_reconciliation = round(raw_inv - attr_inv, 2)
    if abs(inventory_reconciliation) > max(1.0, 0.01 * abs(full_pnl)):
        warnings.append("inventory_unreconciled")
```
Remove the old `inv = round(book.inventory_net_cash, 2)` block and the
`split_merge_inventory_cost_in_total` warning. Return:
```python
    return CapitalRiskProfile(wallet, round(total_buy, 2), round(total_pnl, 2),
                              slices=slices, cluster_risk=cluster_risk,
                              exit_summary=exits, warnings=sorted(set(warnings)),
                              peak_deployed=peak, max_drawdown=drawdown,
                              roi_on_peak_deployed_pct=round(_pct(total_pnl, peak), 2) if peak else None,
                              total_realized_pnl_with_inventory=full_pnl,
                              inventory_reconciliation=inventory_reconciliation)
```

- [ ] **Step 5: Thread `ledger=` from the assembler**

In `assembler.py`, find the `build_capital_risk_profile(wallet, book, clusters)` call and pass the
enriched ledger: `build_capital_risk_profile(wallet, book, clusters, ledger=enriched.ledger)`
(use whatever the in-scope `EnrichedLedger`/`WalletEventLedger` variable is named).

- [ ] **Step 6: Run to verify pass**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_capital_risk_profile.py tests/winning_wallet_thesis/test_capital_risk.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add sniper/analysis/winning_wallet_thesis/capital_risk.py sniper/analysis/winning_wallet_thesis/types.py sniper/analysis/winning_wallet_thesis/assembler.py tests/winning_wallet_thesis/test_capital_risk_profile.py
git commit -m "refactor(thesis): re-accounting supersedes inventory band-aid; add reconciliation guard

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: SwissMiss parity — re-accounted total ≈ \$359.5k, reconciliation ≈ 0

> **Corrected target (real-data verification):** the rigorous per-leg re-accounting reproduces
> the analyzer's recorded realized total **≈ $359.5k** with `inventory_reconciliation ≈ 0`. The
> prior "$271k" was a band-aid error — it subtracted ~$88.6k of OPEN complete-set capital on the
> unresolved market `0xe2bdf6` (not a realized loss). Old (ignore split/merge) and new (per-leg)
> totals agree to the cent ($359,512.57): a complete set is worth $1 whether merged or redeemed,
> so her merge book nets to zero at the pair level.

**Files:**
- Test: `tests/winning_wallet_thesis/test_swissmiss_parity.py` (append to the existing file)

- [ ] **Step 1: Add the parity assertions**

```python
@pytest.mark.skipif(not _cache_present(), reason="SwissMiss cache absent (no live API in CI)")
def test_swissmiss_split_merge_reaccounting_reconciles_to_realized_total():
    from sniper.analysis.winning_wallet_thesis.capital_risk import build_capital_risk_profile
    enriched = E.enrich_ledger(L.build_ledger(SWISS), refresh=False)
    book = PB.build_position_book(enriched)
    full = sum(e.realized_pnl or 0.0 for e in book.resolved_with_pnl())
    assert 350_000 < full < 370_000, full                       # ~$359.5k, NOT the band-aid $271k
    cr = build_capital_risk_profile(SWISS, book, [], ledger=enriched.ledger)
    assert 350_000 < cr.total_realized_pnl_with_inventory < 370_000
    assert abs(cr.inventory_reconciliation) < 1.0               # all split/merge cash attributed
    # the $34M complete-set book on 0xe2bdf6 is OPEN -> ~$275k residual unrealized, excluded
    e2 = [e for e in book.episodes if e.condition_id.startswith("0xe2bdf6")]
    assert e2 and all(not e.resolved for e in e2)
    assert all(e.buy_shares > 0 for e in book.resolved_directional())   # no structural leak
```

- [ ] **Step 2: Run it**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_swissmiss_parity.py -v`
Expected: PASS locally (cache present); SKIP in CI.

- [ ] **Step 3: Commit**

```bash
git add tests/winning_wallet_thesis/test_swissmiss_parity.py
git commit -m "test(thesis): SwissMiss re-accounted total ~\$271k + reconciliation guard

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Reward token-vs-USDC verification (1b)

**Files:**
- Modify: `sniper/analysis/winning_wallet_thesis/slicer.py` (`reward_attribution`)
- Modify: `sniper/analysis/winning_wallet_thesis/methods/reward.py`
- Test: `tests/winning_wallet_thesis/test_method_economics.py`

- [ ] **Step 1: Write the failing test**

```python
def test_reward_attribution_flags_non_usdc_token():
    from sniper.analysis.winning_wallet_thesis.slicer import reward_attribution
    acts = [
        {"type": "REWARD", "usdcSize": 100.0, "asset": ""},               # USDC/pUSD cash
        {"type": "REWARD", "usdcSize": 50.0, "asset": "SOME_TOKEN_ID"},   # token-denominated
    ]
    ra = reward_attribution(acts)
    assert ra["usdc_total"] == 100.0                # only the cash row counts as USD
    assert ra["non_usdc_flagged"] is True
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_method_economics.py::test_reward_attribution_flags_non_usdc_token -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

In `slicer.reward_attribution`, separate USD-cash rewards (empty/absent `asset`, i.e. settled in pUSD/USDC) from token-denominated rows (non-empty `asset` that is not the USDC/pUSD asset). Return `{"income", "counts", "total", "usdc_total", "non_usdc_total", "non_usdc_flagged"}`. `reward_share_pct` callers use `usdc_total`. In `methods/reward.py`, set `evidence["non_usdc_flagged"]` and if true, exclude `non_usdc_total` from the share.

> Implementer note: confirm against a real wallet whether REWARD rows carry a non-empty `asset` distinct from the trade asset; if Polymarket settles all rewards in pUSD (expected), `non_usdc_flagged` stays False everywhere and this is a verified no-op. Document the finding in the commit message.

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_method_economics.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/winning_wallet_thesis/slicer.py sniper/analysis/winning_wallet_thesis/methods/reward.py tests/winning_wallet_thesis/test_method_economics.py
git commit -m "feat(thesis): flag non-USDC-denominated rewards in attribution

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: `finding.n` standardization (1c)

**Files:**
- Modify: `sniper/analysis/winning_wallet_thesis/methods/types.py` (`MethodFinding`: add `n_events`)
- Modify: each detector in `methods/*.py` to set `n_events`
- Modify: `sniper/analysis/winning_wallet_thesis/validation/cards.py` (`_proxy_p_value` uses `n_events`)
- Test: `tests/winning_wallet_thesis/test_validation_cards.py`

- [ ] **Step 1: Write the failing test**

```python
def test_proxy_p_value_uses_n_events_not_positions():
    from sniper.analysis.winning_wallet_thesis.validation.cards import _proxy_p_value
    from sniper.analysis.winning_wallet_thesis.methods.types import MethodFinding
    # same effect, but few EVENTS (correlated positions) -> larger (less confident) p than n=positions
    f_events = MethodFinding("favorite_longshot_calibration", "supported_proxy", 5, 0.10, 50,
                             n_events=5)
    f_positions = MethodFinding("favorite_longshot_calibration", "supported_proxy", 5, 0.10, 50,
                                n_events=50)
    assert _proxy_p_value(f_events) > _proxy_p_value(f_positions)   # fewer independent obs -> bigger p
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_validation_cards.py::test_proxy_p_value_uses_n_events_not_positions -q`
Expected: FAIL (`MethodFinding` has no `n_events`).

- [ ] **Step 3: Implement**

In `methods/types.py MethodFinding`, add `n_events: int = 0` (default to `n` if a detector doesn't set it — but every detector should). In each detector, set `n_events = ctx.event_count` (specialist/template already use event_count for `n`) or the event-distinct count for position-based detectors (calibration already groups by event → `len(event_rows)`; sizing → distinct events of its episodes). In `cards._proxy_p_value`, replace `math.sqrt(finding.n)` with `math.sqrt(max(finding.n_events, 1))`.

- [ ] **Step 4: Run to verify pass + full regression**

Run: `python3 -m pytest tests/winning_wallet_thesis -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/winning_wallet_thesis/methods sniper/analysis/winning_wallet_thesis/validation/cards.py tests/winning_wallet_thesis/test_validation_cards.py
git commit -m "fix(thesis): proxy p-value uses independent event count, not position count

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: Track 1 final verification + merge

- [ ] **Step 1: Full focused suite + conveyor + schema**

Run:
```bash
python3 -m pytest tests/winning_wallet_thesis tests/thesis_cards -q
python3 scripts/validate_schema_refs.py
python3 scripts/brain_index.py generate && git status --short index/
```
Expected: all pass; index clean.

- [ ] **Step 2: Real-data sanity on SwissMiss**

Run: `PYTHONPATH=. python3 scripts/run_winning_wallet_thesis.py 0xdbade4c82fb72780a0db9a38f821d8671aba9c95 --no-save 2>/dev/null | head -5`
Expected: headline still shows the sports edge; total realized now reflects the re-accounted ~\$271k path.

- [ ] **Step 3: Merge to main**

```bash
git checkout main && git merge --no-ff feat/analyzer-track1-ledger -m "merge: analyzer Track 1 — ledger correctness (split re-accounting + reward + n)"
git push origin main && git branch -d feat/analyzer-track1-ledger
```

## Self-Review

- **Spec §3.1 (split re-accounting):** Tasks 1–4 (per-leg ledger + pair allocators, episode attribution, band-aid removal, parity). ✓
- **Spec §3.2 (reward denomination):** Task 5. ✓
- **Spec §3.3 (finding.n):** Task 6. ✓
- **Consistency note (1a supersedes inventory_net_cash):** Task 3 removes it + reconciliation guard. ✓
- **Design correction (post-Codex review, verified against real data):** split/merge are condition-pair $1/pair events with no outcome index; allocation must sum to the row's ACTUAL cash (per-leg VWAP synthesis was wrong). `is_directional` stays buy-driven so structural split/merge (her $34M complete-set book) does NOT pollute the selection edge; the wallet TOTAL uses `resolved_with_pnl()`. The existing `test_split_appears_in_event_basket_not_directional` stays green. Recorded as spec rev 2.
- **No placeholders:** Tasks 2 and 3 now give the full allocation helpers and the concrete reconciliation formula (no `sum(... for r in [])`). Implementer notes are integration pointers, not skipped code.
- **Types:** `account_leg`/`allocate_split_pair`/`allocate_merge_pair` (Tasks 1–2); `split_cost`/`merge_cash` + `resolved_with_pnl` (Task 2) consumed by Task 3/4; `inventory_reconciliation` (Task 3) used in Task 4; `n_events` (Task 6) used in `_proxy_p_value`.
