# Fomo Account Analyzer — Design Spec

**Date:** 2026-07-06
**Status:** approved design (brainstorming), pre-plan
**Owner:** memecoin copy-mirror subsystem
**Related:** [[research-fomo-family-ws-feasibility]], [[finding-memecoin-copy-duplicate-event-bug]], [[project-hl-memecoin-eval-gates]]

## 1. Problem

The memecoin copy-mirror is mechanically working but **stalled on curation**: we can only grade a
trader *after* we've been paper-copying them (`db_memecoin.trader_performance()` reads our own
`memecoin_positions`), and that sample is tiny/empty because curated traders are quiet. We cannot
screen a *candidate* before adding them, and we cannot tell which of the 21 curated traders are
worth keeping. The bottleneck is trader selection, not copy mechanics.

fomo exposes each trader's **own full swap ledger** (`/v2/users/{id}/swaps`, paginated) plus a
profile envelope (`/v2/users/{id}`). We can reconstruct their realized trading history independent
of our copies and compute a rich statistics table. That is what this analyzer does.

## 2. Scope

**In scope:** a per-trader metrics computation that pulls fomo profile + swap history, reconstructs
per-token round-trips, and emits a wide **raw statistics** row per trader; persistence; a CLI
runner; a read API endpoint; a sortable UI table.

**Explicitly out of scope (YAGNI / doctrine):**
- **No score. No add/watch/skip verdict. No copy advice, no sizing, no expected-PnL.** The output is
  a metrics table the operator ranks by whatever column they trust. (Operator chose this stance
  explicitly over a decision-oriented score.)
- No changes to the live copy engine, runner, brackets, or follow-sync.
- No mutation of `memecoin_positions` / `memecoin_events` / `memecoin_wallets`.
- No on-chain RPC. fomo's swap ledger + its own USD amounts are the sole data source for the core.

## 3. Live-verified data model (probed 2026-07-06, do not re-derive)

`/v2/users/{id}` (any user) returns: `numTrades`, `swapCount`, `totalVolume`, `followers`,
`following`, `createdAt`, `isRestricted`, `private`, `twitter`, `averageHoldTimeSeconds`
(self-only, often null for others), `userHandle`, `displayName`. **Aggregate PnL fields
(`totalPnL`, `pnl7d/30d`, `equity`) are self-only — they return `null` for other users**, so we do
NOT read PnL; we reconstruct it.

`/v2/users/{id}/swaps?page=N&limit=100` → `{swaps:[...], hasNextPage:bool}`. Each swap:
`inTokenAddress`, `outTokenAddress`, `inHumanAmount`, `outHumanAmount`, `humanUsdAmountIn`,
`humanUsdAmountOut`, `createdAt`, `platformFeeHumanAmount`, `referralFeeHumanAmount`,
`isOffPlatform`, `provider`, `networkId`.

**Verified facts that shape the design:**
1. **Cash leg is canonicalized to the Solana USDC mint `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v`
   on every chain** (confirmed on BSC=56, Base=8453, Solana=1399811149). WSOL
   `So11111111111111111111111111111111111111112` and USDT `Es9v…` also appear as cash. **0 of 300
   swaps had neither leg as cash.** So buy/sell direction is chain-agnostic:
   - **buy** = `inTokenAddress ∈ CASH` and `outTokenAddress ∉ CASH` (spent USD, got memecoin)
   - **sell** = `outTokenAddress ∈ CASH` and `inTokenAddress ∉ CASH` (spent memecoin, got USD)
   - **cash_route** = both legs cash (USDC↔WSOL) → ignore for round-trips
   - **token_route** = neither leg cash (direct token↔token, rare) → count toward `pct_route_approx`, exclude from clean round-trips
   USD amounts always come from `humanUsdAmountIn/Out` regardless of which cash token was used.
2. **Traders are MULTICHAIN** (Solana + Base + BSC + Ethereum in one ledger). Chain does not affect
   classification (fomo canonicalizes) but IS reported as `chain_mix`. Note for the operator: our
   live copy path is Solana-centric, so EVM-heavy traders may not be copyable even if profitable —
   this is a report attribute, not a filter.
3. **Active traders exceed our page window.** `gr3gor14n` and `insentos` both hit 800 swaps (8
   pages) with `hasNextPage` still true. Metrics are therefore **windowed** unless full history is
   paged. Coverage reporting is mandatory, never optional.
4. **win_rate is a misleading rank column.** `gr3gor14n`: 36% WR but **+$494k realized**, driven by
   a single **+37,914%** trade. Power-law payoff. Tail/concentration metrics
   (`profit_factor`, `top3_pnl_share`, `avg_win_pct` vs `avg_loss_pct`, percentiles) are load-bearing.
5. **Censoring is real.** `connerb`: 21 swaps, **all buys, 0 sells, 0 closed round-trips**. A copied
   trader who never sells on-platform yields no realized PnL. Reported explicitly via `n_open_bags`
   and `closed_coverage`; never hidden.

## 4. Architecture

New package `sniper/analysis/fomo_account/` (mirrors `sniper/analysis/wallet_protocol/` conventions):

| Unit | File | Responsibility | Deps |
|---|---|---|---|
| Data adapter | `sniper/feeds/fomo_account.py` | `get_profile(auth, uid, http=)`, `iter_swaps(auth, uid, max_pages, http=)` — pure fetch, injectable `http`/`fomo_get`, rate-limited by caller | `fomo_feed.fomo_get` |
| Classifier | `sniper/analysis/fomo_account/classify.py` | `classify_swap(swap) -> (kind, mint, usd, token_qty)`; `CASH_TOKENS` set | none |
| Reconstruction | `sniper/analysis/fomo_account/reconstruct.py` | `reconstruct(swaps) -> {roundtrips, open_bags, routes, off_platform, window}` avg-cost/FIFO per mint | classify |
| Metrics | `sniper/analysis/fomo_account/metrics.py` | `compute_metrics(profile, swaps, recon) -> dict` — the full stats envelope; pure, network-free | reconstruct |
| Orchestrator | `sniper/analysis/fomo_account/analyzer.py` | `analyze_account(auth, uid, *, max_pages, http=) -> dict` — fetch → reconstruct → metrics | adapter, metrics |
| Persistence | `sniper/persistence/db_memecoin.py` (extend) | `ensure` migration for `memecoin_trader_analytics`; `upsert_trader_analytics(conn, row)`; `list_trader_analytics(conn)` | db_core |
| CLI | `scripts/analyze_fomo_accounts.py` | analyze one uid / whole curated list / supplied handles; rate-limited ≥1s/call; writes rows; prints sorted table; dumps CSV to `reports/` | analyzer, db_memecoin |
| API | `sniper/routes/api_memecoin.py` (extend) | `GET /api/memecoin/traders/analytics` → rows | db_memecoin |
| UI | `static/app.js`, `templates/index.html` | sortable "Trader Analytics" table in the memecoin tab | api |

**Data flow:** CLI → `analyze_account(uid)` → adapter pages swaps (cap `max_pages`, respect
`hasNextPage`) + profile → `reconstruct` (classify each swap, pair per mint) → `compute_metrics`
(pure) → `upsert_trader_analytics` → surfaced via API/UI/CSV.

The reconstruction + metrics core is **pure and network-free** (input = list of swap dicts + profile
dict), so it is fully unit-testable with synthetic swaps. All network lives in the adapter.

## 5. Reconstruction method

- Sort swaps oldest→newest by `createdAt`.
- Per mint, maintain a FIFO queue of open buy lots `(token_qty, usd_price_per_token, ts, usd_cost)`.
  `usd_price_per_token = humanUsdAmountIn / outHumanAmount` for a buy.
- On a sell (`humanUsdAmountOut`, `inHumanAmount`), FIFO-match against open lots → each match is a
  round-trip: `realized_usd = (sell_price - lot_price) * matched_qty`, `pnl_pct`, `hold_s`.
- Sells with no matching open lot (bought off-platform / before window) → `unmatched_sell`, counted
  in coverage, excluded from round-trips (no cost basis).
- Leftover buy lots after the last swap → `open_bags` (censored; cost basis known, exit unknown).
- `token_route` / `cash_route` swaps do not enter round-trips; counted for coverage.

Realized PnL is **realized-only**; open bags are reported separately so a "never sells losers"
trader cannot look artificially profitable without the reader seeing `closed_coverage`.

## 6. Metrics catalog (the deliverable)

Grouped. Typed columns for rankable scalars; `metrics_json` carries distribution arrays + raw
sub-tables. All computed from the paged window; every number is qualified by §6e coverage fields.

**6a. Profitability (realized, closed round-trips only)**
`n_closed`, `realized_pnl_usd`, `mean_pnl_pct`, `median_pnl_pct`, `win_rate`, `profit_factor`
(Σwins/|Σlosses|), `expectancy_pct`, `pnl_pct_p10/p25/p50/p75/p90`, `best_pct`, `worst_pct`,
`return_std`, `sharpe_like` (mean/std of per-trade pct), `max_drawdown_usd` (on cumulative realized),
`avg_win_pct`, `avg_loss_pct`, `top3_pnl_share` (top-3 |PnL| / Σ|PnL| — lottery dependence).

**6b. Behavior / timing**
`median_hold_s`, `mean_hold_s`, `hold_p10`, `hold_p90`, `pct_held_under_60s`, `pct_held_over_1d`,
`avg_buy_usd`, `buy_usd_p10/p50/p90`, `max_buy_usd`, `size_cv` (std/mean of buy USD),
`re_entry_rate` (swaps / distinct mint), `avg_buys_per_token`, `avg_down_rate` (fraction of buys at
a lower price than the same mint's prior buy), `fee_drag_pct` (Σ(platform+referral fees) / Σvolume).

**6c. Recency (does this trader feed signal NOW)**
`last_swap_age_h`, `swaps_24h`, `swaps_7d`, `volume_7d`, `large_buys_7d` (buys ≥
`MEMECOIN_COPY_SIZE_USD` in last 7d), `active_days`, `longest_gap_h`.

**6d. Profile (from `/v2/users/{id}`)**
`num_trades`, `total_volume`, `account_age_days`, `followers`, `following`, `follower_ratio`,
`has_twitter`, `is_restricted`, `is_private`, `profile_swap_count`.

**6e. Coverage / trust (never read a number without these)**
`swaps_analyzed`, `pages_fetched`, `hit_full_history` (bool: `hasNextPage` false at stop),
`window_days`, `oldest_swap`, `newest_swap`, `pct_off_platform`, `pct_route_approx`
(token_route+cash_route / total), `n_open_bags`, `n_unmatched_sells`, `closed_coverage`
(`n_closed / (n_closed + n_open_bags)`), `computed_at`, `data_as_of` (newest swap).

**6f. Mix**
`chain_mix` (json: networkId → count), `provider_mix` (json: provider → count).

## 7. Persistence

<!-- schema:external=memecoin_trader_analytics -->
New table `memecoin_trader_analytics` (idempotent `CREATE TABLE IF NOT EXISTS` + column-add
migrations inside `ensure_memecoin_tables`). One row per `source_user_id`, upserted latest
(`ON CONFLICT(source_user_id) DO UPDATE`), keyed unique on `source_user_id`. Typed columns for every
§6 scalar above; `chain_mix`, `provider_mix`, `metrics_json` as TEXT (JSON). `computed_at` kept so
staleness is visible. History is NOT retained in v0 (upsert-latest); a `memecoin_trader_analytics_history`
append table is a deferred follow-up if trend tracking is wanted.

## 8. Surfaces

1. **CLI `scripts/analyze_fomo_accounts.py`:**
   - `--uid <id>` / `--handle <h>` (resolve via `/resolve`) / `--all` (curated list) / `--ids a,b,c`.
   - `--max-pages` (default from settings), `--sleep` (default 1.0s, WAF-safe).
   - Writes rows, prints a table sorted by `--sort` (default `realized_pnl_usd`), dumps CSV to
     `reports/fomo_trader_analytics_<date>.csv`.
2. **API `GET /api/memecoin/traders/analytics`** → `{rows:[...]}` from `list_trader_analytics`.
   Read-only; no auth beyond existing memecoin routes.
3. **UI:** "Trader Analytics" table in the memecoin tab — sortable columns, coverage columns visually
   de-emphasized but present, `computed_at` age shown. Follows the existing `memecoin` controller
   poll/render pattern in `static/app.js`.

## 9. Settings (new, in `sniper/settings/memecoin.py`)

- `MEMECOIN_ANALYZER_MAX_PAGES` (default 20 → up to 2000 swaps).
- `MEMECOIN_ANALYZER_SLEEP_S` (default 1.0 — fomo WAF safety, matches runner).
- Reuse `MEMECOIN_COPY_SIZE_USD` for the `large_buys_7d` threshold.

## 10. Testing (TDD)

Pure-core unit tests with synthetic swap sequences (network-free):
- clean single round-trip (buy→sell) → correct pnl/hold/win.
- partial sell (sell < buy qty) → one round-trip + residual open bag.
- average-down (two buys then one sell) → FIFO matching, `avg_down_rate` > 0.
- power-law (many small losers + one huge winner) → `win_rate` low, `realized_pnl_usd` high,
  `top3_pnl_share` high (regression guard for the win_rate-lies finding).
- no-sell trader (all buys) → `n_closed=0`, `n_open_bags>0`, `closed_coverage=0`, no crash.
- unmatched sell (sell with no prior buy) → counted, excluded, no negative-cost-basis artifact.
- token_route + cash_route + off_platform swaps → coverage fields populated, excluded from PnL.
- multichain mix → `chain_mix` correct, classification unaffected.
- page-cap hit (`hasNextPage` true at stop) → `hit_full_history=False`.
- One adapter test with a stubbed `http` returning canned pages (no live network).

Then: schema validator green, `brain_index.py generate` committed, focused pytest for the new
package green.

## 11. Honesty rails (doctrine)

- The table is **descriptive statistics of past on-platform behavior**, not a prediction or
  recommendation. No column implies future PnL or a copy decision.
- Every profitability number sits next to its `closed_coverage`, `hit_full_history`,
  `pct_off_platform`, `n_open_bags` so the operator sees the footing.
- Reconstructed PnL is realized-only and windowed; the spec and the UI both state this. Off-platform
  and pre-window activity are acknowledged blind spots, quantified not hidden.

## 12. Risks / open items

- **Deep whales:** full history for a high-volume trader may exceed 20 pages; default window is a
  recent-2000-swap sample. Acceptable for ranking; `hit_full_history` flags it. Deep mode = raise
  `--max-pages`.
- **Rate limits:** `--all` over 21 traders × ~avg pages at ≥1s/call ≈ a few minutes. On-demand, not
  a loop. Never run inside the copy runner.
- **Cash-token drift:** if fomo adds a new cash representation, some swaps fall to `token_route` and
  inflate `pct_route_approx` — visible, not silent. `CASH_TOKENS` is one small set to maintain.
- **`numTrades` vs paged count mismatch** (profile `numTrades=10` but 21 swaps seen for connerb):
  profile counters are unreliable; trust our paged `swaps_analyzed`, keep `profile_*` as context only.

---

## 13. Post-review revision (2026-07-06) — AUTHORITATIVE, overrides earlier sections

Two adversarial reviews (platform-reviewer, research-methodologist) plus deeper live probing changed the design. Where this section conflicts with §3–§12, **this section wins.**

### 13a. HARD CONSTRAINT: fomo has NO working swap pagination — window is the most-recent ≤100 swaps
Probed exhaustively on `gr3gor14n` (509 lifetime swaps): `?page`, `?offset`, `?skip`, `page=0`,
`before`, `beforeId`, `cursor`, `lastId`, `startAfter`, `createdBefore`, `until`, `endTime` — **all
12 return the identical newest 100 swaps** (id-overlap 100/100). `limit=200` → HTTP 400 (cap 100).
`/v2/users/{id}/activity` is the same (page1≡page2) and worse (100 items = ~46 swaps + transfers).
`hasNextPage: true` is meaningless on both endpoints.

**Consequences (replace §3.3, §4 adapter, §5 windowing, §8 CLI, §12):**
- The adapter does a **single** `GET /v2/users/{uid}/swaps?limit=100`, **dedups by `id`**, and stops.
  There is NO page loop and NO `max_pages`. (The earlier plan's `iter_swaps` page-loop would fetch
  the same 100 repeatedly and duplicate-inflate every metric — this is the exact bug that produced a
  bogus "+$494k / 800 swaps" in early probing; the honest single-window figure is +$61.8k.)
- Coverage is exact and cheap: `coverage_ratio = swaps_analyzed / profile.swapCount`;
  `hit_full_history = swaps_analyzed >= profile.swapCount` (true only for traders with ≤100 lifetime
  swaps). Verified: connerb 21/24 (87%), gr3gor14n 100/509 (19%), insentos 100/421 (24%).
- The analyzer measures **recent behavior**, explicitly. Every profitability figure is
  "over the most-recent-≤100-swaps window." This is arguably the right horizon for "is this trader
  active and winning *now*" (the curation question), but it must be labeled, never implied as lifetime.
- Settings: drop `MEMECOIN_ANALYZER_MAX_PAGES`. Keep `MEMECOIN_ANALYZER_SLEEP_S` (delay *between
  traders* in `--all`). Add `MEMECOIN_ANALYZER_SWAP_LIMIT = 100` (fomo caps anyway; explicit).

### 13b. Reconstruction correctness fix (was a real bug)
When a sell partially matches available lots and then lots run out mid-sell, the leftover must still
be counted. Track `remaining` after matching; `if remaining > 1e-9: unmatched_sells += 1`
**regardless of whether any quantity matched**. (Verified this matters: gr3gor14n shows **36
unmatched sells vs 35 closed** — over half its sells reference buys older than the 100-swap window.
Without the fix those leftovers vanish silently.)

### 13c. Metric changes (fold into §6)
- **Add** `coverage_ratio` (§13a), `total_buy_notional` (Σ buy USD in window),
  `open_bag_cost_usd` (Σ cost of still-open lots = capital at risk, outcome unknown),
  `dollar_weighted_return_pct` = `realized_pnl_usd / (total_buy_notional − open_bag_cost_usd) × 100`
  when denom > 0 (capital-scaled realized return, so a $50-notional and a $5M-notional trader are
  comparable — closes the "absolute PnL is the only headline" gap),
  `unmatched_sell_ratio` = `n_unmatched_sells / (n_closed + n_unmatched_sells)` (window-truncation
  severity; gr3gor14n ≈ 0.51 → loud "don't trust realized PnL as complete").
- **Rename** `max_drawdown_usd` → `realized_max_drawdown_usd` (it is blind to unrealized open-bag
  losses — the name must not overclaim); `sharpe_like` → `pnl_pct_dispersion_ratio` (it is per-trade
  return dispersion, not an annualized Sharpe) and **null it when `n_closed < 5`** (unstable).
- **Split** `top3_pnl_share` → `top3_win_share` (top-3 positive realized / Σ positive) and
  `top3_loss_share` (top-3 |negative| / Σ|negative|). The old `abs()` version conflated "one moonshot
  drove the gains" with "one blowup drove the losses" — the exact distinction the metric exists for.
- **Add** `undefeated` (1 when `n_closed > 0` and zero losing round-trips) so a `profit_factor=None`
  from "no losses ever" is distinguishable from `None` = "no closed trades / no data."
- **Drop** `expectancy_pct` (mathematically identical to `mean_pnl_pct`; keeping both invites treating
  them as independent confirmation). **Drop** `pages_fetched` (no paging).
- **Caveat** (spec + UI): `pct_off_platform` counts only swaps flagged `isOffPlatform`; a buy
  off-platform → sell on-platform leaves a *missing leg* that surfaces as `unmatched_sells` /
  `open_bags`, not here. So `pct_off_platform = 0%` does NOT mean "no cross-platform contamination";
  `unmatched_sell_ratio` is the better contamination proxy.

### 13d. Codebase-fit fixes (platform-reviewer)
- The memecoin route module exposes a **blueprint factory** `create_api_memecoin_blueprint(ctx)` with
  a shared long-lived `conn = ctx["conn"]`; `db_memecoin` is imported as `m`; routes use `@bp.get`.
  The new endpoint is registered **inside** the factory, uses the closed-over `conn`, returns
  `jsonify({"rows": m.list_trader_analytics(conn)})`, and **must not open/close its own connection**
  (closing the shared handle would break every later memecoin request). Its test builds the blueprint
  via the factory with an in-memory conn.
- CLI handle→uid lookup goes through a new `db_memecoin.get_wallet_by_handle(conn, handle)` (no raw
  SQL in the CLI, per the repo's single-source-of-truth rule).
- UI: use the app's shared `fetchJson(url, {suppressErrorToast:true})` helper; hook `_loadAnalytics`
  into the `memecoin` controller's `load()` dispatch (app.js ~2262). Analytics is on-demand — it does
  NOT join the 4s poll.

### 13e. Operator-sort stance (research-methodologist BLOCKER 3, reconciled with the chosen output stance)
The operator explicitly chose "raw metrics table, I rank it myself," so the default sort stays
`realized_pnl_usd` and no hard coverage gate is imposed. To keep the coverage-blind trap visible
without removing operator control, the CLI table and the UI table **lead with the coverage columns**
(`coverage_ratio`, `unmatched_sell_ratio`, `n_closed`, `closed_coverage`, `hit_full_history`)
adjacent to `realized_pnl_usd`, and both surfaces carry a one-line caveat that realized PnL is over a
recent, often-partial window and is dollar-weighted (`dollar_weighted_return_pct`) vs trade-weighted
(`mean_pnl_pct`) — the two can diverge in opposite directions.

### 13f. token↔token rotations are split into sell + buy (2026-07-07, post-audit)
A `token_route` (A→B, neither leg cash) is economically a **sell of A + a buy of B** and is now
reconstructed as both legs: the A-leg FIFO-matches A's tracked lots (realizing A's PnL, or counting
`unmatched_sells` if A was bought off-window), and the B-leg opens a bag at `humanUsdAmountOut`.
Previously these were dropped as a flagged no-op, which understated realized activity for
token-rotators. `token_routes`/`routes` counters and `pct_route_approx` are retained **for
transparency only** — they no longer imply excluded PnL. `cash_route` (USDC↔WSOL currency
conversion) remains correctly excluded (not a memecoin position). Impact is small in practice
(≤~1/300 swaps observed) but removes the "PnL understates activity" caveat instead of documenting it.
