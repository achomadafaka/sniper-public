# HL Alt-Short Causal Evidence and Forward Paper Arm — Design

Date: 2026-07-26
Status: implemented; retrospective sealed; forward paper activation pending
Owner surfaces: `sniper/hyperlive/`, `scripts/run_hl_copy.py`,
`scripts/hl_altshort_arm.py`, `data/research/altshort_study/`,
`data/hl_intel.sqlite`

## 1. Decision

Treat alt-short as a research candidate, not a proven deployable edge.

The existing evidence supports one narrow claim: the wallet-selected alt-short entries
in the bearish W2 window beat an event-matched leave-one-out alt basket. It does not yet
prove that the effect survives causal regime selection, portfolio accounting, open-book
marking, exit counterfactuals, or forward observation.

Build one causal evidence pipeline and one provenance-complete paper arm. The output is
one of:

- `paper_promote`: continue the versioned forward paper arm;
- `paper_continue`: evidence is incomplete but not disproven;
- `paper_reject`: the causal or portfolio tests fail;
- `blocked_missing_feed`: required evidence is absent or invalid.

None of these states authorizes funding, real orders, a real-leg allowlist override, or
changes to the funded runner.

## 2. Evidence anchor

The design starts from these reproducible observations, not from the handoff headline:

- Honest follow-ledger alt-short performance was positive, but the reported cohort and
  mean drift as new rows close.
- The original standalone study used a full-window regime label, creating lookahead.
- Its “no-filter continuous drift” baseline was sampled trades, not a passive short
  basket.
- Re-entering at the next bar changed W1 from positive to negative while leaving W2
  materially positive.
- In W2, the signal returned about `+128.6 bps` per event while an equal-weight
  leave-one-out basket over the same entry/exit intervals returned about `-6.9 bps`;
  day-clustered residual CI was approximately `[+50.9, +221.3] bps` over 46 days and
  200 trades.
- A passive equal-weight W2 alt-basket short returned about `+1591 bps`, with about
  `-1478 bps` max drawdown. This is a materially different capital path from the
  event-matched result.
- The historical standalone harness reproduced about one fifth of wallet-copy PnL;
  almost every simulated position hit its time cap. Exit selection is therefore a
  first-class hypothesis, not an implementation detail.
- The live paper scorecard is closed-trade biased while old open positions remain.
  Closed-only results cannot support promotion.

These values are discovery evidence. The new pipeline must recompute them from pinned
inputs before using them in a verdict.

## 3. Scope

In scope:

- canonical alt-short taxonomy;
- point-in-time regime features and labels;
- event-matched beta residuals;
- wallet-exit versus mechanical-exit attribution;
- fixed-slot, costed portfolio accounting;
- open-position mark-to-market and censoring disclosure;
- coin/day/week concentration and block-bootstrap inference;
- immutable research and forward-arm provenance;
- paper-only runtime identity, watchdog, heartbeat, and reporting hardening;
- deterministic promotion, continuation, rejection, and blocked verdicts.

Out of scope:

- funding an HL account;
- submitting or simulating funded orders;
- changing funded risk, sizing, or execution behavior;
- overriding the real-leg proven-wallet gate;
- claiming the result is generally valid in bullish regimes;
- converting analyzer output into copy advice;
- live API calls during historical study runs;
- strategy optimization on the final holdout.

## 4. Three arms, kept separate

Every report separates these hypotheses:

| arm | question | entry | exit |
|---|---|---|---|
| `wallet_copy_alt_short` | Do selected wallet events add value? | observed wallet event, next tradeable bar | observed wallet close |
| `standalone_exhaustion` | Can public price state reproduce the entry? | causal exhaustion rule, next tradeable bar | pre-registered mechanical rule |
| `passive_alt_beta` | What did persistent short-alt exposure pay? | window start or event-matched basket | matched interval or window end |

No blended “alt-short edge” metric is allowed. Wallet selection, standalone entry
selection, exit selection, and passive beta must remain attributable.

## 5. Canonical taxonomy

The strategy universe excludes the core assets `BTC`, `ETH`, and `SOL`. All other
eligible perpetuals are “alt” for this strategy. The name `major` is not used in new
evidence because another module currently treats a broader 12-coin liquidity set as
majors.

One new source of truth owns the exclusion set and normalization. Copy filtering,
decomposition, study construction, arm reporting, and tests import it. Compatibility
aliases may remain temporarily, but they cannot define a second set.

Universe membership is point-in-time where listings data exists. If only the current
universe is available, the run records `survivorship_bias=true`; it cannot earn a
funded or generalization claim.

## 6. Data and provenance contract

### 6.1 Historical study manifest

Every run consumes an immutable JSON manifest containing:

- `study_id` and schema version;
- code SHA and dirty-tree flag;
- input paths plus SHA-256 hashes;
- candle source, bar size, timezone, and coverage bounds;
- point-in-time universe source and exclusions;
- fee, spread, slippage, borrow/funding, and stress-cost assumptions;
- decision timestamp and next-tradeable-entry rule;
- train, validation, and sealed holdout windows;
- all candidate rule grids and random seeds;
- bootstrap cluster unit and replication count;
- minimum coverage and verdict thresholds.

The runner refuses a verdict when a required hash differs. Dirty-tree studies may run,
but their verdict is `blocked_missing_feed` with reason `unversioned_code`.

Tracked outputs are compact JSON/CSV summaries and the manifest. Large raw caches stay
untracked and are referenced by hash.

### 6.2 Forward arm identity

A paper arm has an immutable `arm_id`, exact UTC `activated_ts_ms`, code SHA, policy
JSON and hash, normalized target set, taxonomy version, cost model, benchmark version,
and regime-feature version.

Three paper-only SQLite tables are added to `hl_intel.sqlite`:

<!-- schema:external=hl_altshort_arms -->
<!-- schema:external=hl_altshort_observations -->
<!-- schema:external=hl_altshort_regime_marks -->

| table | purpose |
|---|---|
| `hl_altshort_arms` | one immutable row per activation; identity and policy only |
| `hl_altshort_observations` | one row per eligible paper copy; entry provenance, causal regime features, benchmark entry, latest mark, and close attribution |
| `hl_altshort_regime_marks` | hourly-upserted cross-sectional marks used to compute trailing features without future data or per-entry history fetches |

`hl_altshort_observations.copy_id` is unique. Existing copy rows are never silently
adopted into a new arm. Eligibility requires `detect_ts_ms >= activated_ts_ms`.

Entry provenance is written with the paper open. If the metadata write fails, the copy
may remain in the general paper book, but it is excluded from the forward arm and emits
`provenance_write_failed`. Missing metadata is never reconstructed from current state.

The paper loop already obtains marks. It stores at most one mark per coin per UTC hour,
updates the latest mark and timestamp for open arm observations during its normal cycle,
and prunes or bounds the tape to the configured lookback. The reporter performs no extra
live fetch.

### 6.3 Activation boundary

Building and testing the feature does not activate it. Activation is a separate,
explicit operator action because it restarts a paper service and writes an arm row.
Activation must:

1. require a clean pinned SHA;
2. validate the manifest and target set;
3. generate the epoch from corrected UTC at restart time;
4. write the immutable arm row before accepting observations;
5. launch with the exact arm manifest path;
6. verify heartbeat SHA, arm ID, epoch, targets, and `alt_short_only=true`.

No existing row receives the new `arm_id`.

## 7. Causal regime features

Regime features at decision time `t` use information available no later than the last
fully closed bar before `t`. No window label may use the eventual study-window return.

Version 1 computes:

- equal-weight and median alt-basket return over trailing 24 h, 72 h, 7 d, and 14 d;
- breadth below 24 h and 7 d anchors;
- alt-basket return minus BTC return over the same horizons;
- cross-sectional dispersion;
- funding-rate median and positive/negative breadth when coverage is sufficient;
- BTC trend and realized volatility;
- fraction of the point-in-time universe with valid inputs.

Features are continuous. Any discrete `bearish`, `neutral`, or `bullish` label is a
versioned rule fitted or chosen on training data only. A label with less than 80%
required universe coverage is `unknown`, and `unknown` cannot pass a conditional-edge
gate.

The existing seconds-scale execution regime helper remains separate; it is not a valid
substitute for these research features.

## 8. Entry timing and beta controls

### 8.1 Entry timing

The primary historical entry is the first bar open strictly after the wallet event or
standalone decision. Same-close entry is diagnostic only. Missing next-bar prices make
the event ineligible and increment an explicit exclusion counter.

### 8.2 Event-matched benchmark

For every signal trade, construct an equal-weight short basket from all eligible alts
except the signaled coin. Use the exact signal entry and exit timestamps, the same
tradeable-price rule, and the same cost model. Require at least 80% of the eligible
basket to have valid prices.

Report per-event:

- signal net return;
- basket net return;
- residual `signal_net - basket_net`;
- basket breadth and coverage;
- beta-adjusted residual from a training-only beta estimate, when estimable.

The simple leave-one-out residual is primary. Regression alpha is diagnostic because
short, overlapping trade intervals and changing cross-sections make a single beta
fragile.

### 8.3 Passive benchmark

Also simulate a continuously rebalanced equal-weight short-alt basket over each window.
Report net return, turnover, funding, max drawdown, and capacity. It answers whether a
persistent beta position was preferable; it is not substituted for event matching.

## 9. Exit attribution

Entry and exit value are decomposed with the same entry cohort:

1. `wallet_exit`: observed wallet close;
2. fixed horizons: 1 h, 4 h, 8 h, 24 h, 48 h, and the legacy cap;
3. pre-registered price-only families: take-profit/stop-loss/time-cap and
   trailing-giveback/time-cap;
4. basket-relative exits: close when the signal residual mean-reverts through a
   training-selected threshold or reaches the cap.

Candidate grids are declared in the manifest before training. Selection uses training
only. Validation selects between families; the sealed holdout receives one evaluation
of the frozen winner. No holdout tuning or rule replacement is allowed.

For each counterfactual, report net return, tail loss, holding time, turnover,
concurrency, and the fraction terminated by each exit reason. If almost every trade
hits the cap, the exit is labeled `non_reproducing`, regardless of positive mean.

The wallet exit adds value only if its residual and fixed-slot portfolio result beat
the best frozen mechanical comparator on sealed holdout. Historical wallet exits are
not directly deployable because their future close is unknown; they identify what a
standalone exit would need to reproduce.

## 10. Portfolio accounting

Trade-average bps is descriptive. The decision metric is a fixed-slot portfolio:

- a manifest-pinned number of equal-notional slots;
- deterministic admission when signals overlap;
- no reuse of capital until a position closes;
- explicit rejected-signal and utilization counts;
- fees, spread, slippage, and funding charged at event time;
- chronological equity, drawdown, turnover, and exposure;
- open positions marked with a fresh paper-loop mark;
- stale marks called out and excluded from promotion.

Run base costs and a two-times cost stress. Capacity is reported as accepted notional
and rejected-signal rate, not extrapolated profit dollars.

## 11. Inference and concentration

The primary confidence interval resamples UTC entry days, preserving all trades and
overlap within a day. Diagnostics also resample coins and calendar weeks. IID
trade-level bootstrap cannot support a verdict.

Every report includes:

- event, coin, day, and week counts;
- top coin and top week contribution shares;
- leave-top-coin and leave-top-week results;
- median event residual and mean residual;
- day-block confidence interval;
- open/closed counts and open-book MTM;
- maximum concurrent positions and fixed-slot utilization.

Pre-registered concentration caps are in the manifest. Defaults for v1 are top coin
less than 35% of positive PnL and top week less than 50%. Failing a cap yields
`paper_continue` or `paper_reject`; it cannot be silently described as robust.

## 12. Verdict gates

### 12.1 Retrospective gate

`paper_promote` requires all of:

- causal features with no future-window labels;
- next-tradeable entry;
- positive event-matched residual whose day-block 95% CI lower bound exceeds zero;
- positive fixed-slot net return under base and two-times costs;
- positive total-book result after marking open positions;
- leave-top-coin and leave-top-week fixed-slot returns remain positive;
- concentration caps pass;
- sufficient point-in-time universe and benchmark coverage;
- a frozen exit comparator evaluated once on sealed holdout;
- all input, code, policy, and exclusion provenance valid.

Failure of a profitability or robustness gate is `paper_reject`. Insufficient sample,
missing marks, unknown regime, missing universe history, or incomplete provenance is
`paper_continue` or `blocked_missing_feed`, never a pass.

### 12.2 Forward paper gate

The forward arm is read only after both:

- at least 30 provenance-valid closes; and
- at least 20 distinct UTC entry days.

The read uses all arm observations, including open MTM. It requires the retrospective
gates plus positive forward beta residual and no target, coin, or week dominance. The
verdict remains paper-only. A target-specific result cannot authorize a target override
in the real leg.

## 13. Runtime hardening

Before arm activation:

- make `HL_REAL_ALT_SHORT_ONLY` backward-compatible for test/config doubles and parse
  booleans through the shared normalized parser;
- document the setting in `.env.example`;
- make the copy watchdog process identity include `--alt-short-only` and the arm
  manifest so it cannot attach to an old unfiltered loop;
- report exact UTC epoch rather than local-date midnight;
- include code SHA, dirty flag, arm ID, policy hash, target set, taxonomy version, and
  filter state in heartbeat/status output;
- make `hl_altshort_arm.py` report closed PnL, open MTM, beta residual, concentration,
  mark freshness, and provenance failures;
- keep real execution `DRY_RUN` and do not alter funding or the real target gate.

The runtime refuses to claim an active forward arm if the manifest, SHA, policy hash,
epoch, or filter identity differs. General paper capture may continue, but its rows are
not arm evidence.

## 14. Module boundaries

Planned surfaces:

- `sniper/hyperlive/altshort_universe.py`: normalization and strategy exclusion set;
- `sniper/hyperlive/altshort_evidence.py`: pure causal features, benchmarks, exit
  counterfactuals, fixed-slot portfolio, concentration, and verdict functions;
- `sniper/hyperlive/intel_store.py`: paper-arm schema and atomic provenance methods;
- `scripts/hl_altshort_study.py`: cache-only reproducible historical runner;
- `scripts/hl_altshort_arm.py`: provenance-complete retrospective/forward reporter;
- `scripts/run_hl_copy.py`: arm manifest validation and per-entry metadata capture;
- focused tests under `tests/` plus small immutable fixtures.

`data/research/altshort_study/analyze.py` remains discovery material until the new
runner reproduces or supersedes it. Its outputs cannot be promoted by copying them into
the new report.

## 15. Failure handling

- Missing required candles, funding, universe membership, or marks: explicit coverage
  counters and `blocked_missing_feed` when below threshold.
- Hash or policy mismatch: no verdict; identify the exact mismatch.
- Duplicate arm observation: idempotent when identical, hard error when conflicting.
- Clock ambiguity: store epoch milliseconds and render UTC ISO; never derive an arm
  boundary from a local date.
- Bootstrap too few clusters: no CI-based pass.
- Zero or stale open mark: do not assume zero PnL.
- Empty target set or non-short entry under the arm: refuse arm attribution.
- Watchdog identity mismatch: start the exact configured process; never accept a loose
  substring match.

## 16. Testing and verification

TDD is required. Minimum coverage:

- taxonomy consistency and symbol normalization;
- causal feature cutoffs reject future bars;
- next-bar entry and missing-bar exclusions;
- leave-one-out basket construction and coverage thresholds;
- passive basket accounting;
- exit rule train/validation/holdout isolation;
- fixed-slot admission, overlap, costs, funding, open MTM, and stale marks;
- day/coin/week block resampling determinism;
- concentration and verdict gates;
- arm-row immutability, observation idempotency, and epoch eligibility;
- backward-compatible real settings and normalized booleans;
- watchdog exact process identity;
- heartbeat and report provenance;
- end-to-end fixture reproduction with a sealed expected JSON report.

Verification order:

1. focused RED/GREEN tests per component;
2. all HL alt-short, copy-filter, scorecard, real-settings, and real-runner tests;
3. schema-reference validation;
4. deterministic study rerun from pinned cache;
5. `git diff --check`;
6. read-only arm report against the current DB;
7. explicit operator-approved paper activation and post-restart heartbeat probe.

No historical result is called forward evidence. No unit suite is called runtime proof.

## 17. Delivery and knowledge sync

Implementation is split into reviewable commits: runtime correctness, pure evidence
core, reproducible study runner, provenance persistence/reporting, then optional paper
activation. No commit mixes funded behavior with research behavior.

Before any PR, run the Knowledge Sync Gate. This specification and any new research
verdict require the appropriate knowledge delta. Supabase writes remain an explicit
operator action; an omitted write must be disclosed, not implied complete.

## 18. Success condition

This work succeeds even if it rejects the strategy. Success means the system can state,
from causal and reproducible evidence, whether the observed return came from wallet
entry selection, wallet exit selection, passive alt beta, or sampling/censoring—and can
collect the next forward paper sample without changing its definition after seeing the
result.
