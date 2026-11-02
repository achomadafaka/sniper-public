# Spec — Analyzer Residual Work (ledger correctness + Goldsky fills + fill-backed methods)

**Date:** 2026-05-31
**Status:** design, rev 1
**Parent:** the winning-wallet thesis extractor (`sniper/analysis/winning_wallet_thesis/`, spec `2026-05-31-winning-wallet-thesis-extractor-design.md`). This closes the honest residual after the extractor + capital/risk + audit work.

## 1. Goal

Address every remaining gap in the analyzer that is actionable, in one organizing design across three dependency-ordered tracks:

```
Track 1 — Ledger-only correctness   (no feeds, independent, build first)
Track 2 — Goldsky historical fill fetch  (independent infra; feeds Track 3)
Track 3 — Fill-backed methods        (depends on Track 2)
```

Each track becomes its own implementation plan. The analyzer stays **one wallet at a time**; nothing here adds cross-wallet logic, live trading, or sizing.

## 2. Scope decisions (locked during brainstorming)

- **Feeds are Goldsky-only.** No live CLOB WebSocket capture loop. Historical `OrderFilled` events (which the subgraph indexes) are fetched on demand per wallet. Consequence: **mark-to-market drawdown stays DEFERRED** — it needs continuous order-book price, which a fill feed does not provide. This is an explicit non-goal here, not an oversight.
- **Split-derived directional exposure gets full re-accounting**, not a detect-and-flag. The position model is extended so SPLIT/MERGE shares are attributed to per-outcome episodes with cost basis.
- **One organizing spec, three plans.**

## 3. Track 1 — Ledger-only correctness

No new data sources; all three items work from the existing Data-API ledger.

### 3.1 Split-derived directional re-accounting (`position_ledger`)

**Problem.** Today `position_builder` routes SPLIT/MERGE rows to the event basket (sentinel `outcome_index = -1`) and `PositionEpisode.realized_pnl` uses `held_shares = buy_shares − sell_shares`, ignoring `split_shares`/`merge_shares` (which are always 0 on episodes). A wallet that goes *directional* via split-then-sell-one-leg therefore has uncaptured exposure, and its episode PnL is wrong. A recent band-aid (`inventory_net_cash = merge_usdc − split_usdc`, added to the wallet total) captures the net cash but not the per-position attribution.

**Design.** Attribute split/merge shares to the per-`(condition, outcome, episode)` legs:

- A **SPLIT** of `size` on condition C mints `size` shares of *every* outcome of C. For a binary market: `+size` to outcome 0's episode and `+size` to outcome 1's episode (via the existing-but-unused `episode.split_shares`), plus an allocated cost basis (new `episode.split_cost`).
- A **MERGE** of `size` removes `size` shares of every outcome (via `episode.merge_shares`) and returns `$size` cash.
- **Cost-basis allocation for a split (rev 2 — pair-level, actual cash):** verified against real data, split/merge rows are complete-set events with `usdcSize == size` and **no outcome index**. The pair cost is the row's **actual cash** (`$X`); only its allocation across legs is synthesized, and it **must sum to that actual cash** (a per-leg VWAP synthesis is wrong — `0.80 + 0.50 = 1.30 ≠ 1.00`). Allocate with the wallet's own revealed price where a leg has a buy VWAP (leg0 at `p`, leg1 at `1 − p`), else 50/50, scaled to the row's cash. The allocator lives in `position_ledger` (pure) and is applied by `position_builder` (which sees both legs); `account_leg` consumes the pre-allocated per-leg cash.
- **Merge realizes real PnL (rev 2):** a MERGE returns the set's `$size` face value, NOT the cost basis — buying both legs cheap then merging is a genuine gain (SwissMiss has ~$190k of buy-both-then-merge across ~20 conditions the old model misvalued). Merge is not assumed cost-neutral.
- **Restated `realized_pnl`** per episode (per-leg cash in − cash out):
  - `total_in_shares = buy_shares + split_shares`
  - `total_cost = buy_cost + split_cost`
  - `shares_out = sell_shares (proceeds) + merge_shares (cash) + redeemed_remainder (resolution payout)`
  - `realized_pnl = sell_proceeds + merge_cash_attributed + redeem_payout − total_cost`, with `redeem_payout = max(total_in − sell − merge, 0) × won`.
- **Selection population (rev 2 — load-bearing):** split/merge legs are attributed for the wallet TOTAL but are **NOT** selection bets. `is_directional` stays `buy_shares > 0`, so structural complete-set activity (her $34M round-trip on one condition, with zero directional trades, is capital parked for yield — not a pick) does **not** pollute the selection-edge calibration or manufacture phantom canceling pairs. The wallet total is summed over a complete population accessor (`PositionBook.resolved_with_pnl`); the selection slicer keeps using `resolved_directional()`.

**Consistency note (load-bearing):** full re-accounting **supersedes** the `inventory_net_cash` band-aid — keeping both double-counts split/merge cash. Remove the wallet-level `inventory_net_cash` adjustment from `CapitalRiskProfile.total_realized_pnl_with_inventory` and instead keep an `inventory_reconciliation` field = raw net inventory cash (`Σ merge − Σ split`) minus attributed (`Σ merge_cash − Σ split_cost`), which should be ≈ 0 once allocation is correct (a regression guard, not a correction).

**Validation target (rev 2 — corrected from $271k):** re-accounted SwissMiss FULL realized PnL (via `resolved_with_pnl`) reproduces the analyzer's recorded realized total **≈ $359.5k**, with `inventory_reconciliation ≈ 0` (all split/merge cash attributed). The prior audit's **$271k was an error**: it subtracted ~$88.6k — the **wallet-wide net** of split/merge cash (`Σmerge − Σsplit`) — as if it were a realized loss. It isn't. Split/merge is structural complete-set activity (a set is worth $1 whether minted or merged), so it is neutral at the pair level; the only non-neutral piece is OPEN, unresolved split capital — dominated by the still-open market `0xe2bdf6` (≈ −$275k of the −$271.4k total **unresolved** split/merge net; Polymarket `/value` ≈ $904k open) — carried as inventory, not booked as a loss. (Note the ~$88.6k band-aid ≠ the $275k 0xe2bdf6 open capital: resolved split/merge legs net positively and offset most of it.) With each leg attributed, `resolved_with_pnl` excludes the open capital and the old (ignore split/merge) and new (per-leg) realized totals agree to the cent ($359,512.57). Her selection-edge numbers are unchanged (the selection population is untouched).

**Isolation.** Put the per-leg share-and-cost math in a focused `position_ledger.py` (pure, testable) consumed by `position_builder`; keep `position_builder` as the folding orchestrator.

### 3.2 Reward token-vs-USDC verification

Mirror the pUSD check: REWARD/YIELD `usdcSize` is assumed to be USD cash. Inspect the row's `asset` field (and, if present, any token indicator); if a reward row is denominated in a non-pUSD/USDC token, set a `reward_non_usdc_denominated` flag on the reward attribution and exclude/discount it from `reward_share_pct`. Expected outcome on real wallets: no-op (rewards settle in pUSD, itself 1:1 USD) — but verified, not assumed. Lives in `methods/reward.py` + `slicer.reward_attribution`.

### 3.3 `finding.n` standardization

The proxy p-value (`validation/cards.py`) uses `finding.n`, which is **event-count** for `specialist_domain_edge`/`recurring_template_edge` but **position-count** for `sizing_skill`/`calibration`. Positions within one event are correlated, so the correct independent-observation count for significance is **events**. Standardize every detector to expose an explicit `n_events` used for the p-value, while keeping `n_positions` as separate evidence. Removes overconfidence on detectors that currently pass position-count. Small change in `methods/types.py` (add `n_events`) + each detector + `cards.py`.

## 4. Track 2 — Goldsky historical fill fetch

**Purpose.** Supply real wallet-identified fills (maker/taker, asset, amounts, ts, tx) to the existing `OnchainFillIdentityProvider`, unblocking Track 3 on *historical* data with no live loop.

**Module:** `sniper/feeds/goldsky_fills.py`
- `fetch_wallet_fills(wallet, *, refresh) -> list[FillEvent]` — GraphQL query against the Polymarket order-book subgraph for `OrderFilled` (or equivalent) entities where `maker == wallet OR taker == wallet`, paginated by timestamp/skip, cached to `runs/wallet_anatomy/<wallet>_fills.json` (same cache discipline: transient failures stay unknown, never cached as empty).
- Rows normalized via the **existing** `sniper.feeds.fill_normalizer.fill_event_from_subgraph_row` (already built + tested in feed Phase 1).
- A `build_onchain_fill_provider(wallet)` helper wraps the fetched fills in `OnchainFillIdentityProvider`.

**DEPENDENCY (must be verified first in the plan, not assumed):** a reachable Polymarket order-book subgraph endpoint + auth (Goldsky or The Graph), and its `OrderFilled` schema field names. If unavailable, Track 2/3 are **blocked-pending-access** and the plan must say so rather than ship a stub that looks live. The schema may differ from the assumed field names in `fill_event_from_subgraph_row`; reconcile against the real schema.

**No live loop, no DB writes, operator-triggered, cached.**

## 5. Track 3 — Fill-backed methods

With real fills available, three methods stop being blocked placeholders. Each is fed an `OnchainFillIdentityProvider` (or `PricePathProvider` reconstructed from fills) instead of the `Unavailable*` default.

- **`first_attention_discovery`** — already wired to `fills_for_market`; now fed real data. Detect whether the wallet is among the first-N traders in a market and whether independent volume/holders followed. Promotes from `blocked_missing_feed` to `supported_proxy` / `replication_required` (it already returns that shape).
- **`market_making_spread_capture`** — compute the wallet's maker fraction and per-fill spread capture from the fills; flag low-adverse-selection making. No longer a hard `blocked` stub when fills are present; stays `blocked_missing_feed` when they're not.
- **`adverse_selection_aware_making`** — post-fill price drift reconstructed from **subsequent fills on the same asset** (a sparse price path, not an order book). This is an explicit **PROXY** (`supported_proxy` at best, never `supported_direct`); the detector states the proxy limitation in `controls`.

**MTM drawdown — DEFERRED (non-goal).** It requires continuous order-book mid, which fills do not give. `capital_risk` keeps `max_drawdown` as realized-only with its existing honest caveat; no change here.

## 6. Data objects / interface changes

- `PositionEpisode` — populate existing `split_shares`/`merge_shares`; add `split_cost: float = 0.0`. `realized_pnl` restated (§3.1).
- `CapitalRiskProfile` — replace `total_realized_pnl_with_inventory` semantics: it now equals the re-accounted total directly; add `inventory_reconciliation: float` (≈0 guard).
- `MethodFinding` — add `n_events: int` (the significance count); detectors set it.
- `FillEvent` — already exists (feed Phase 1); reused unchanged.
- No new DB schema. No Supabase writes.

## 7. Build order & boundaries

`Track 1 → Track 2 → Track 3` (Track 1 independent; Track 3 needs Track 2). New files: `position_ledger.py` (1a), `goldsky_fills.py` (2). Existing detectors (3) are fed, not rewritten. Each track ships its own plan with its own tests; nothing here is a single mega-change.

## 8. Testing strategy

- **1a:** unit tests for the share-and-cost ledger (split→both legs, cost-basis allocation, merge removes shares, redeem on remainder); a synthetic split-then-sell-one-leg wallet proving directional capture; SwissMiss parity — re-accounted FULL realized total ≈ $359.5k ($359,512.57, via `resolved_with_pnl`) and selection edge unchanged; `inventory_reconciliation ≈ 0`.
- **1b:** a reward row with a non-USDC `asset` sets the flag and is discounted; normal USDC rewards unaffected.
- **1c:** detectors expose `n_events`; the p-value uses it; a position-heavy/event-light cluster gets a *less* confident p than before.
- **2:** offline test with a fixture subgraph response → `FillEvent`s; cache dedup/transient-failure discipline; a live read-only smoke gated on endpoint availability (skips in CI).
- **3:** each method returns `blocked_missing_feed` with no provider and a non-blocked state with a fills fixture; adverse-selection states its proxy limitation.
- No live API in CI; live runs operator-triggered and cached.

## 9. Non-goals

- Live CLOB WebSocket capture loop / continuous archive.
- Mark-to-market drawdown.
- Cross-wallet anything.
- Live trading, sizing, expected-PnL, copy advice.
- New DB schema or Supabase writes.

## 10. Approaches considered

- **Split: detect-and-flag vs full re-accounting.** Chose full re-accounting (operator decision) — correct for every archetype, supersedes the inventory band-aid, and the per-leg ledger is reusable.
- **Feeds: live CLOB capture vs Goldsky-only.** Chose Goldsky-only (operator decision) — historical fills unblock the methods that matter with no always-on infra; MTM stays honestly deferred.
- **`finding.n`: leave as cosmetic vs standardize.** Standardize — it's a real (if small) overconfidence on position-count detectors feeding the FDR gate.

## 11. Revision history
- **rev 1 (2026-05-31):** initial design — ledger correctness (split re-accounting, reward denomination, n standardization), Goldsky historical fill fetch, fill-backed methods; MTM drawdown explicitly deferred.
- **rev 2 (2026-05-31):** split/merge accounting corrected after a Codex review + real-data verification. (a) split/merge are condition-pair $1/pair events with no outcome index; allocation must sum to the row's ACTUAL cash, done at the pair level in `position_ledger`/`position_builder`, not per-leg VWAP synthesis. (b) MERGE realizes real PnL (face value vs cost), not cost-neutral. (c) split/merge legs are excluded from the SELECTION edge population (`is_directional` stays buy-driven); the wallet total uses `resolved_with_pnl`. (d) `inventory_reconciliation` = raw net inventory cash − attributed. Track 2/3 plan fixes: explicit `_post_graphql` (the reused `_request_json` is GET-only), a durable endpoint-gate audit doc, asset→condition via `fetch_market_by_condition` (not the active-only `fetch_market_index`), `first_attention_discovery` keeps deriving the wallet from episodes (no new `wallet=` param), and the runner only wires the microstructure detectors to the fill provider (first-attention is already wired; no DETECTORS double-run).
