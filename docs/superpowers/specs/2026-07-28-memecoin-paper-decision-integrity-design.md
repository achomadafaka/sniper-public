# Memecoin Paper Decision Integrity Design

> 2026-10-04 amendment: legacy sampled-path paper results are diagnostic only under
> `2026-10-04-memecoin-wallet-execution-research-design.md`. They cannot publish a
> supported strategy, promote a candidate, fit active exit rails, or select new
> funded copy-arm rails. Cached ready receipts are also blocked. Existing frozen
> funded contracts and explicit operator overlays remain authoritative. Complete
> causal one-second execution paths and an independently verified future-study
> receipt are required; that publication receipt is not implemented yet.


**Date:** 2026-07-28
**Scope:** Memecoin Algo -> Paper -> Strategy Lab and Closed. Legacy multi-arm paper engine only.

## Problem

The Paper tab presents unmatched arm rows as same-entry experiments, mixes hypothetical arms in a
portfolio headline, evaluates most open positions against mutable settings, hides excluded and
censored evidence, and performs repeated full-ledger and live-price work on a four-second UI poll.
The underlying origin identity and fill ledger are usable, but the current projection is not a safe
input to funded-policy decisions.

## Boundaries

- Paper remains measurement-only. It gains no signing, reservation, broadcast, or funded authority.
- V6 paper and funded entry/exit behavior are out of scope.
- Existing historical rows are never rewritten into a false clean cohort. Amended 2026-07-29: a row's
  cohort LABEL may be corrected when the correction is proven, never asserted. The only sanctioned
  path is `scripts/backfill_memecoin_paper_cohort.py`, which re-stamps an event only when every
  configured arm's policy — re-resolved from that row's own `entry_mcap` and `network_id` under the
  current settings — is byte-identical to the `assigned_policy_json` the row was opened with. That is
  proof the row already ran the cohort's rails. `assigned_policy_json`, the record that actually drove
  each exit, is never touched, and an event failing the check keeps its original contract. This exists
  because the pre-`dfd2c321` identity bug scattered events that ran byte-identical rails across one
  cohort per (mcap band x chain); correcting that label is not fabricating a cohort.
- Existing operator POST routes remain available, but operator-modified paper rows are excluded from
  experimental results.
- No new dependency and no production database rewrite. Schema changes are additive columns only.
- Unknown quote provenance stays unknown. A DexScreener mid is not relabeled as an executable route.

## Design

### 1. Immutable forward cohort

Every new automatic paper position stores:

- `paper_cohort_id`: SHA-256 of the canonical cohort contract;
- `paper_cohort_json`: `schema_version`, arm roster, fill-model version, each arm's **rule** hash, and
  `notional_usd` (amended 2026-07-29, `schema_version` 2). Notional is cohort identity, not
  metadata: every arm on an origin event shares one entry fill and one bet, so a paired dollar delta is
  equal-capital only while the bet is constant. `MEMECOIN_CONVICTION_SIZING` varies it per event, and
  without this field $100 and $300 events pooled into one dollar mean under a single cohort id. Null
  when the notional is unknown — never zero;

  Amended 2026-07-29 (`schema_version` 3): the per-arm hash covers the arm's **rule**, not its resolved
  instance, and the **code SHA is no longer part of cohort identity**. Hashing the resolved policy made
  the cohort a per-coin bucket — `scalp_c`/`scalp_c2` resolve an mcap band and `real_shadow` resolves
  `real_stop_pct_for_chain(network_id)`, so identity was really `release x mcap band x chain`. That
  minted 17 concurrent cohort ids in 15 hours; since the report keeps exactly one cohort, all 11
  complete matched cohort-events on the live ledger were discarded and the forward arm table read zero
  for every arm. Cohort identity is now the band table, the per-chain stop map, the remaining frozen
  rail parameters, and the notional — the arms are sorted, so roster order does not fork it either. The
  release SHA changed on every deploy for reasons unrelated to the rails and reset the cohort long
  before the evidence floor in §3 could be reached; it is recorded per policy version in
  `memecoin_policy_versions.code_commit`, which is where release provenance belongs. The resolved
  per-coin policy is unchanged and still stored in each row's `assigned_policy_json`;
- `experiment_status`: `eligible`, `excluded_fill`, `operator_modified`, `manual`, or
  `legacy_unversioned`;
- the existing `assigned_policy_id`, `assigned_policy_version`, and `assigned_policy_json` fields for
  every arm, not only scalp arms.

The entry fanout resolves every arm policy before opening any arm, then stamps them with one shared
cohort contract built from the arms' rules. Existing null-policy rows remain `legacy_unversioned`. A
change to any frozen rail parameter, band table, per-chain stop, arm roster, or notional creates a new
cohort automatically; it never changes an already-open eligible position. A deploy that leaves all of
those byte-identical does NOT create a new cohort (amended 2026-07-29). Historical rows keep the
contract they were opened with and age out of the active cohort on their own — they are never
rewritten.

All paper exit arms read their frozen parameters when present and retain current-setting fallback only
for legacy rows. `real_shadow` uses a paper-local frozen snapshot of the real-rail thresholds; it does
not import additional funded authority. Its report remains a modeled shadow, not proof of executable
funded exits.

### 2. Honest entry provenance

The entry writer fills the provenance columns already present in `memecoin_positions` and
`memecoin_fills`: source price, modeled quote price, attribution, source drift, modeled impact, total
copy drift, origin event, network, and signal source. Quote ID, route target, and quote age remain null
unless the source actually supplied them.

A `fomo` fallback receives `experiment_status=excluded_fill`. It remains visible in coverage and
legacy diagnostics but can never enter the decision cohort.

Amended 2026-07-30: the entry price itself is an executable route quote (see §3's fill-model note),
and there is no non-executable fallback left on either leg. `execution_slippage_pct` consequently
stops being a restatement of the model's own impact assumption and becomes a real route-versus-mid
observation. An entry that cannot be quoted is not written at all, so "no row" is now a meaningful
and expected outcome rather than a failure — coverage must show it (`skipped_no_executable_quote`)
instead of leaving a silent hole between the events seen and the positions opened.

### 3. One read-only Paper report

`sniper/services/memecoin_paper_report.py` performs one projected position/fill read and returns:

- coverage: raw positions, unique events, matched events, executable exclusions, censored outcomes,
  operator-modified rows, legacy rows, fresh/stale/unpriced opens;
- primary forward-cohort arm results, restricted to exact events where every configured arm entered
  under the same cohort and no arm contains a `fomo` fill;
- clearly separated legacy matched diagnostics;
- full-book PnL for open rows: booked partial PnL plus marked remaining quantity;
- conservative no-quote sensitivity;
- paired challenger-minus-control mean, median, percentiles, worst/best, top-two concentration check,
  unique coin count, and unique wallet count;
- paired sensitivity (amended 2026-07-29): the mean after dropping the largest profitable event and
  after dropping the two largest, plus a closed-only mean and its pair count. A mean carried by one or
  two events is not an effect, and open rows are marked rather than realised;
- an evidence grade (amended 2026-07-29): `evidence_clusters` = min(matched events, unique coins,
  unique wallets), `min_evidence_clusters` from `MEMECOIN_PAPER_MIN_EVIDENCE_CLUSTERS` (default 30),
  and `decision_grade` of `insufficient_evidence` or `sufficient_sample`. Repeated events on one coin
  or one wallet are not independent evidence. Below the floor the surface renders statistics but must
  suppress ranking language — no winner selection, threshold retuning, or funded-policy change;
- source coverage counts without presenting raw all-arm source PnL as an A/B result;
- complete arm rows for recent episodes and bounded recent closed rows.

The forward section is labelled a **modeled-fill** cohort (amended 2026-07-29). Accepted entries were
DexScreener mid plus modeled impact (`sniper/execution/solana_quote.py:slippage_frac`), never an
executable route quote; the label must not let a reader take them for fills.

**Superseded 2026-07-30 (`fill_model = jupiter-executable-route-v1`).** That labelling was necessary
but not sufficient: a correctly-labelled unachievable number is still unachievable, and the whole
report was denominated in one. Measured on the live ledger, the mid-priced paper leg booked a **−0.35%
median fill gap** against the copied trader — it claimed we filled BETTER than the wallet we copy —
while the funded leg measured **+7.54%** on the same events. The modelled impact was not the cause
(+0.05% paper vs +0.00% realised execution slippage); the **price source** was. Priced head-to-head at
one instant, a DexScreener mid runs ~2.4% better than an executable route on a settled coin but 7.5–11%
better at entry, because the mid lags the pool exactly during the launch pump these entries target. The
bias was therefore largest where the money is, and it compounded: exits priced off the same mid "sold"
a dumping coin above where a route would fill.

Both legs now take an executable route quote (`memecoin_v6_paper_quotes.quote_entry` / `quote_exit`),
which resolves token decimals over RPC — the Jupiter lite-api omits them and a 6dp default misprices a
9-decimal token 1000x — and raises rather than substituting a non-executable price. Both fail CLOSED:
no entry quote yields `skipped_no_executable_quote` (no position, no fill), and no exit quote yields a
`noquote` close, which §3 already censors. An unpriceable event is no evidence, never fabricated
evidence. `fill_model` is cohort identity, so mid-priced and route-priced rows can never pool into one
mean; the superseded cohort stays readable under its own id and is never rewritten.

Two consequences to state plainly. Paper entry volume DROPS, because events that cannot be quoted now
record nothing — the evidence floor in §3 gets further away before it gets closer. And
arm-vs-arm paired deltas from the superseded cohort remain usable: every arm shares one entry fill per
event, so the bias largely cancels between arms. What the old rows could never support is an absolute
dollar figure or any paper-to-real inference.

The endpoint accepts a chain filter and applies it before every aggregate. Null outcomes are censored,
not coerced to zero. Closed rows remain visible via the bounded `closed` list; `episodes` carries only
events with at least one still-open arm (amended 2026-07-29 — the Strategy Lab renders open episodes
only, so padding the list with fully closed events shipped ~440 KiB per poll that the UI discarded).
An episode always carries every configured arm, including the arms that have already exited.

### 4. DB-only operator reads

`GET /api/memecoin/positions/open` uses persisted runner marks only. It performs no DexScreener or
other outbound request. Stored marks include quote timestamp, age, and stale state. Quantity is
aggregated for all open positions in one query.

The Paper UI consumes the single report endpoint. Strategy Lab polls at 15 seconds, Closed at 30
seconds, while funded/live surfaces keep their current cadence. One in-flight guard prevents overlap.
Changing a chain filter reloads only the active section.

### 5. UI truth contract

- Primary label: `Forward matched cohort`.
- Historical label: `Legacy matched diagnostic - mutable/unversioned policy`.
- No all-arm portfolio headline.
- Every table shows matched-event count and excluded/censored coverage.
- Chain filters scope every displayed number.
- `exit-liquidity` is renamed `peak in first sweep` and described as a proxy, not causation.
- Stale marks display their age; unknown stays `-`.
- Open arm PnL includes booked partials.
- Manual buy, partial, and close controls are removed from Strategy Lab. Existing APIs mark affected
  paper rows `manual` or `operator_modified`.
- Arm filters are generated from the configured roster instead of the retired three-arm list.

## Rejected Changes

- **Exact funded-route quotes in the legacy loop:** rejected for that change; **SUPERSEDED 2026-07-30**
  once both objections were answered rather than argued away. A read-only exact-route contract now
  exists (`memecoin_v6_paper_quotes.quote_entry` / `quote_exit`), so no funded executor is imported and
  the paper boundary holds. Provider load does not multiply across arms because all arms share ONE
  entry fill per origin event, and Jupiter quotes are a separate free API rather than the funded RPC
  quota. Rejecting it was correct while the paper leg was demonstrably mispricing every entry by ~8
  points; that is a reason to fix the mispricing, not to keep reporting it.
- **Backfilling historical policy or quote identity:** rejected because it would fabricate provenance.
  Unchanged. The one sanctioned exception is the cohort LABEL correction in §Boundaries, which rewrites
  no price, policy, or route: it re-stamps a row only where every arm's policy re-resolves byte-identical
  under current settings, which is proof the row already ran those rails. A fill price, quote id, or
  route target is never back-written.
- **ML, adaptive rails, confidence claims, or automatic promotion:** rejected until a clean forward
  cohort has enough matched executable observations.
- **New indexes first:** rejected. The main cost is repeated Python materialization and outbound work;
  the new bounded single-pass projection is measured before adding an index.

## Failure Handling

- Invalid frozen policy JSON makes a new admission ineligible and logs the error; it never silently
  evaluates under a different policy.
- A legacy open position continues under the legacy setting fallback and is visibly excluded.
- A missing/stale open quote is counted and displayed; it never becomes zero PnL.
- A manual mutation marks the row before its modeled fill is booked.
- Report errors fail the Paper surface closed with an unavailable message and do not affect the runner.

## Verification

- RED/GREEN unit tests for schema, universal policy freeze, policy-use-after-setting-change, provenance,
  manual isolation, paired report math, censoring, chain scoping, full-book PnL, and complete episodes.
- API test proving `/positions/open` succeeds when the live pricing function raises.
- Focused Paper/API/UI tests, all top-level `test_memecoin*.py`, JS syntax, compileall, schema validation,
  and `git diff --check`.
- Merge before deployment. Deploy immutable memecoin and web releases, then verify process ownership,
  pinned SHA, authenticated health, reservations, kill switch, fresh heartbeat, and read-only live API
  payloads.

