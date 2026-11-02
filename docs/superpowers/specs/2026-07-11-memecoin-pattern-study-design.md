# Memecoin Pattern Study - Design

**Date:** 2026-07-11
**Status:** Approved by operator directive
**Scope:** Read-only chart archetype discovery, conviction/thesis context, matched controls, and forward-validation manifests

## Goal

Determine whether moves like SCAM's initial pump, deep retracement, and later second leg recur often enough to support a paper strategy. Compare fixed exits and a fresh-conviction re-entry rule without changing funded execution.

## Safety Boundary

The study is offline and read-only. It opens SQLite with `mode=ro`, returns pure Python values, and writes only explicit report artifacts. It does not import execution modules, enroll candidates, update strategy tables, change environment settings, or restart services.

All output is research evidence. Legacy results are exploratory. V2 results remain forward evidence until existing promotion gates are met.

## Evidence Lanes

### Legacy Discovery

Legacy outcomes, events, quote-ledger rows, and theses may identify recurring shapes and candidate rules. Legacy wallet membership is unknown at information time, so all-wallet activity is labelled `observed_activity`, never `curated_conviction`. Rows still carrying `legacy_unknown` are classified in memory with the same source-versus-receipt clock proof used by `normalize_clock_domains.py`; the source database is not updated. Provably classified and genuinely ambiguous counts remain separate. Legacy rows with ambiguous clocks, bad quotes, invalid prices, or mixed networks are excluded and counted.

Dense legacy paths require ordered quote-ledger observations beginning no later than five minutes after the episode reference time and extending to the requested horizon within a two-minute tolerance. Sparse legacy horizon marks may populate descriptive summaries but may not enter first-touch exit simulation.

### V2 Forward Validation

V2 feed and quote observations retain capture-time cohort flags and ordered information time. `was_curated=1` supports `curated_conviction`; unknown flags remain unknown rather than false. V2 is never pooled with legacy for rates, controls, optimization, or significance.

V2 path completeness includes the quote-sweep denominator and budget-skipped observations. A 24-hour path is complete only when it reaches the deadline and satisfies at least 90% expected quote coverage.

## Episode Unit

The independent unit is one `(network_id, normalized_mint, UTC day)` episode, anchored on the first eligible buy. Repeated wallet sightings within an episode are features, not additional samples. Solana episodes with an entry market cap below $5 million are the primary population. Missing market cap is retained in the manifest but excluded from the primary population.

Entry price is the point-in-time reference price captured for the signal. The study never chooses a later local low as entry. Each episode records its price-source label so legacy and v2 assumptions remain visible.

## Path Features And Archetypes

For each dense path, compute:

- maximum gain and drawdown from entry;
- time to peak and trough;
- maximum peak-to-subsequent-drawdown;
- recovery from the trough;
- whether price reclaimed entry after a 30% or 50% drawdown;
- whether a second peak exceeded the first peak after a material retracement;
- terminal return and observed duration.

Classification is deterministic and mutually exclusive:

1. `immediate_runner`: reaches +60% before a 30% drawdown and never draws down 30% from entry first.
2. `deep_recovery_new_high`: draws down at least 30%, later reclaims entry, and later exceeds the pre-trough peak.
3. `deep_recovery`: draws down at least 30% and later reclaims entry without a new high.
4. `pump_fade`: reaches +20%, subsequently falls at least 40 percentage points from peak, and does not reclaim entry.
5. `terminal_failure`: never reaches +20% and finishes at or below -30%.
6. `slow_or_unresolved`: every other complete path.

Incomplete paths always retain terminal archetype `incomplete`. They also receive a separately named `observed_*` provisional shape so partial recoveries and drawdowns remain visible without entering policy comparison. The report emits the continuous metrics and labels provisional counts explicitly. Definitions are not silently tuned per coin.

## Conviction And Thesis Features

For each episode and rolling 5-, 15-, 30-, and 60-minute window, compute distinct buyers, distinct sellers, buy/sell notional, repeat buyers, and activity imbalance. V2 additionally computes the same fields for capture-time curated wallets. A fresh-conviction re-entry requires:

- the original position has already stopped;
- at least two distinct curated buyers after that stop within 30 minutes;
- no qualifying buyer may be the original signal wallet alone;
- price has reclaimed at least 20% from the post-stop low;
- entry uses the first qualifying quote at or after the final confirming event.

Legacy data may show how often the analogous all-wallet rule would have worked, but cannot validate the curated form.

Thesis context records count, first timestamp, poster stance from captured PnL, lag from the poster's own observed buy, lag from the episode's first observed buy, and forward return after the thesis. Text is not classified as predictive in v0. Web or social discoveries made after the fact may explain an episode but are excluded from predictor tests unless their original timestamps were captured.

## Exit Policies

Dense complete paths compare these frozen policies using conservative same-timestamp ordering and explicit modeled costs:

- `tp60_sl50x4_24h`: current paper baseline;
- `tp60_sl40x4_24h`: tighter fixed stop;
- `tp100_sl50x4_24h`: wider profit target;
- `principal_at_60_runner_150`: sell enough at +60% to recover the initial notional, then exit the remainder at +150%, a confirmed -50% stop, or 24 hours;
- `stop_then_fresh_conviction_reentry`: baseline exit followed by at most one fresh-conviction entry, with TP +100%, four-confirmation stop -40%, and a 12-hour time cap;
- `timecap_6h` and `timecap_24h`: no profit target, four-confirmation -50% stop.

Every policy starts from the same signal entry and notional. Re-entry capital is reported separately from the original trade, and combined return divides combined net PnL by total capital deployed. No policy may assume fills at an intrabar high, low, or threshold when the observed quote crossed beyond it.

## Controls And Comparisons

Each recovery episode is matched to a non-recovery episode from the same evidence lane using only facts known by the comparison time: network, market-cap band, entry-liquidity band when available, initial 15-minute return band, and observation date proximity. Controls cannot share a mint and cannot occur after the case when used for a live-detectable rule.

The report shows matched and unmatched counts. Missing controls stay in the denominator and are never replaced with a convenient future winner or loser.

Policy ranking is by mean net return subject to both median net return not below zero and 10th-percentile loss no worse than the baseline. Results are grouped by archetype and market-cap band only when there are at least 20 distinct coins; thinner groups report counts and distributions without naming a winner.

## Evidence Gates

Historical discovery may create a preregistered paper hypothesis only. V2 forward evidence requires at least 40 closed episodes, 20 distinct coins, and 10 distinct UTC days for a fixed candidate read.

Offline parameter search remains governed by the existing stricter gate: 21 distinct days, 200 complete 24-hour paths, 100 distinct coins, and at least 90% expected-observation coverage including budget gaps. This study does not weaken or bypass it.

## Outputs

The command accepts `--db`, `--lane legacy|v2`, `--asof`, modeled entry/exit cost basis points, and an output directory. It writes:

- a JSON manifest containing query bounds, row counts, exclusions, coverage, parameters, and a hash;
- an episode CSV with metrics, archetype, conviction, thesis, and eligibility fields;
- a policy CSV with one row per eligible episode and frozen policy;
- a Markdown report containing denominators, distributions, matched controls, and explicit blocked claims.

Output filenames include lane and as-of time. Running against the same database snapshot and parameters produces the same evidence hash and analytical rows.

## Failure Handling

The CLI always opens the source database read-only. It fails on mixed capture versions, naive timestamps, non-finite inputs, or an output target that would overwrite source data. Unsupported-network rows, duplicate sightings, invalid entries, and missing market caps are excluded and counted. Empty and insufficient datasets produce valid manifests and blocked reports, not invented estimates.

## Usage

```bash
python3 scripts/research/memecoin_pattern_study.py \
  --db sniper_tracker.db --lane legacy \
  --asof 2026-07-11T23:59:59+00:00 \
  --output-dir docs/research/assets/memecoin-pattern-study
```

Use `--legacy-clock-offset-s <seconds>` only when the clock classifier has an independently measured host-wall offset; rows already within the real-UTC proof window do not need it. Use `--lane legacy` only for hypothesis discovery. Use `--lane v2` for forward evidence. Neither lane can promote a strategy before the existing lifecycle and exit-search gates pass.

## Tests

- Episode dedup, network identity, time awareness, and point-in-time entry.
- Archetype boundaries, second-leg ordering, and incomplete paths.
- Legacy/v2 separation and unknown cohort handling.
- Conviction windows, seller pressure, original-wallet exclusion, and fresh re-entry chronology.
- Thesis lag against first buy and poster buy with missing evidence preserved.
- Policy first-touch ordering, costs, principal recovery, re-entry capital accounting, and no future low fill.
- Past-only matched controls and explicit unmatched cases.
- Evidence gates, thin-group blocking, deterministic hashes, and read-only database use.
