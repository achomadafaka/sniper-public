# HL Cross-Sectional Rollover Backtest — Design

Date: 2026-07-27
Status: approved for research-only implementation
Scope: deterministic historical research; no paper arm, service, database, SDK, or funded path

## Decision

Create a separate research lineage for the price-only pattern inferred from wallet
`0xc9c7`: fade an extreme cross-sectional alt winner only after its six-hour momentum
rolls non-positive. This is not a revision of the sealed `30%/72h/8h` exhaustion
study and cannot change its verdict.

The first deliverable is a reproducible backtest and falsification report. A forward
paper arm is out of scope until the historical report passes its declared gates and
the remaining continuation-squeeze tail has a causal risk hypothesis.

## Frozen rule v1

At each fully closed UTC hourly decision bar:

- universe: Hyperliquid perpetuals except `BTC`, `ETH`, and `SOL`;
- eligibility: at least 720 prior hourly slots, at least 80% populated;
- `return_24h_bps = close[t] / close[t-24h] - 1`;
- `runup_from_low_24h_bps = close[t] / min(close[t-24h:t]) - 1`;
- require both cross-sectional percentile ranks to be at least `0.95`;
- require six-hour return `<= 0`;
- trigger only on the false-to-true transition;
- enter at the next hourly open, never the decision close;
- admit the five strongest crossings in each rolling 24-hour interval;
- permit one open position per coin;
- exit on the first sampled hourly close with short gross return `>= 200 bps`, or
  the first sampled close at or after 96 hours;
- charge 9 bps fees plus 15 bps slippage per side and exact cached funding.

No threshold, ordering, exit, or cost value may be selected from report output.
Sensitivity is diagnostic and cannot replace v1.

## Data contract

The runner consumes a cache directory containing `_universe.json`,
`candles_<coin>.json`, and `funding_<coin>.json`. It never fetches data. Required
input files receive SHA-256 hashes in `run_meta.json`; coverage, missing coins, and
funding gaps are explicit.

The current official API source is limited to the most recent 5,000 candles. Older
official Hyperliquid data is available through requester-pays S3 and supplies L2
snapshots or asset contexts, not candles. Downloading that archive is a separate,
operator-authorized acquisition because it can incur transfer charges. Until an
older normalized cache is supplied, the report states `older_history=blocked_paid_archive`.

Third-party candle data cannot silently substitute for Hyperliquid data. A future
adapter must identify its venue, symbol mapping, price field, survivorship policy,
and hashes, and it produces a separate-domain result.

## Evidence outputs

The runner writes one output directory containing:

- `events.csv`: one row per admitted mature event with decision/entry/exit identity,
  causal entry features, costs, funding, exit reason, and matched-basket residual;
- `periods.csv`: calendar month and ISO-week results;
- `regimes.csv`: trailing-only 72-hour basket regime and quintile results;
- `feature_falsification.csv`: predeclared continuation-risk feature splits;
- `report.json`: verdict, coverage, inference, concentration, tail, and sensitivity;
- `run_meta.json`: code SHA, dirty state scoped to research code, policy hash, and
  input hashes.

Reports are written atomically. Existing results are not overwritten unless the
caller explicitly selects a new output directory.

## Benchmarks and inference

Every event receives an equal-weight short-alt benchmark excluding the signal coin,
using the exact signal entry and exit clock and the same cost/funding model. Minimum
coverage is 80%.

Primary inference resamples UTC entry days. Report absolute mean and matched-basket
residual with 95% day-block intervals, win rate, cap-exit rate and severity, worst
trade, top-coin contribution, and leave-top-coin mean. Calendar periods with fewer
than five entry days remain descriptive.

The report also compares the frozen rule with:

- extreme-winner entries without the six-hour rollover;
- two-times slippage costs;
- a maximum-five-open-position capacity constraint.

These are falsification and risk diagnostics, not replacement policies.

## Causal continuation-risk research

Only features available by the decision close are allowed:

- decision-candle close location and upper-wick rejection;
- one-, three-, six-, 12-, 24-, and 72-hour returns;
- distance below the trailing 24-hour high;
- 24-hour realized volatility;
- current volume versus trailing 24-hour median volume;
- funding level and trailing funding change;
- trailing 72-hour equal-weight basket return and cross-sectional dispersion.

The target is cap-exit severity, not win-rate optimization. Feature splits are
exploratory because all currently cached history has already been inspected. They may
name the next frozen hypothesis but cannot promote v1.

Open interest, liquidation flow, spot/perp basis, and order-book replenishment are
recorded as missing feeds. They cannot be fabricated from candles.

## Verdict

The historical result is `candidate_continue` only when all are true:

- matched-basket residual day-block CI lower bound is positive;
- absolute and matched-residual means both exceed the extreme-winner baseline without
  the six-hour rollover condition;
- at least two calendar months have positive absolute CI lower bounds;
- leave-top-coin residual remains positive;
- absolute mean remains positive after doubling per-side slippage; matched-basket
  residual is unchanged when identical flat costs are charged to both arms;
- at least 80% benchmark and funding coverage;
- no provenance or causality failure.

There is no historical `promote` verdict. Failure returns `candidate_reject`; missing
older history or required provenance returns `candidate_continue` with explicit
blockers. Any paper-arm design requires a separate approved spec.

## Post-v1 exploratory direction

The v1 report may carry one explicitly post-hoc next hypothesis. It does not alter the
frozen rule or historical verdict:

- first nominate the original top-five rollover episodes;
- execute only when one-hour return is greater than `-60 bps`, 12-hour return is at
  most `350 bps`, and decision volume is at most `1.2x` its trailing median;
- rejected nominations still consume the observation quota and are not replaced by
  weaker candidates;
- track each rejection as a zero-capital shadow position until the original sampled
  `+200 bps` short target or 96-hour cap. This preserves the one-episode-per-coin
  cooldown without fabricating PnL or exposure.

The ordering and shadow cooldown are load-bearing and must be compared with a
replacement control that filters before nomination. Thresholds came from inspected v1
features, anchored to rounded February-April medians. May-July was not a sealed
holdout. Report this only as `posthoc_not_validated`, with one-at-a-time threshold
sensitivity, sampled-close cap sensitivity at 12/24/48/72/96 hours, 96-hour stop-loss
sensitivity at 300/500/1,000/2,000 bps, and a new independent history or forward
episode as the next falsification.

## Selective forward preregistration

`selective_prereg.v1.json` freezes the post-hoc rule separately from the rejected broad
v1 policy. Its historical cutoff is the latest inspected cached candle,
`2026-07-26T12:00:00Z`; forward evidence begins at `2026-07-26T13:00:00Z`. The
historical runner rejects any cache containing a candle after the cutoff.

The preregistration has no promotion authority. Review eligibility requires at least
200 forward closes across at least two separately accounted nomination episodes, at
least 30 closes per episode, positive residual in every episode, positive
leave-one-episode-out residual, and a positive day-block residual interval. Episode
separation requires 168 hours without a nomination, not merely a new calendar label.

It remains machine-blocked until all are true:

- historical universe coverage is at least 95%, including delisted/renamed contracts;
- open interest, liquidation volume, spot/perp basis, and order-book replenishment each
  cover at least 90% of decisions;
- execution paths use observed marks sampled no slower than 300 seconds;
- nomination episodes are persisted and independently countable;
- a separately approved risk envelope defines expected shortfall, maximum drawdown,
  worst loss, stop-through p99, liquidation distance, and concurrent exposure limits.

Historical profitability, hit rate, or tight confidence intervals cannot clear any of
these blockers. Selective in-sample intervals are stamped
`inference_status=invalid_post_selection`, `headline_allowed=false`, and remain only
for descriptive audit.

## Safety

- No live API or S3 call occurs in the analyzer.
- No production SQLite database is opened.
- No service, watchdog, manifest, heartbeat, copy runner, or real runner changes.
- No order, SDK, credential, account, sizing, or funded action.
- Results are research evidence, not trade instructions or expected live PnL.
