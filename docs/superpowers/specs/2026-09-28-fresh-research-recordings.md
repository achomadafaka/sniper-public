# Fresh research recordings — approved contract, 2026-09-28

Status: approved and active as `fresh-20260928-v4` since 2026-09-28T12:04:00.277554Z (08:04 EDT). The runtime manifest records the actual start. All earlier histories and v1-v3 validation pilots are excluded.

## Outcome

Start a genuinely new active research/paper cohort with verified recording logic and frozen settings. Learn what winners share, what losers share, what both share, and which differences survive a fresh comparison. Produce an explicit list of features that are useful, harmful, redundant, indistinguishable at useful precision, or still unknown.

The operator's constraints are authoritative:

- No paper results older than the last seven days may support any proof. The fresh cohort uses **zero pre-activation outcomes**, even if they are less than seven days old.
- Radar and V6 are archived and excluded.
- Retain old data for forensic history; do not overwrite, delete or relabel it as fresh evidence.
- This changes research recordings and research analysis. It does not change funded admission, sizing, exits, wallet authority, signing or submission.

## Recommended approach

**One shared observation path, multiple research labels.** Reuse the scanner, its sources and quote plumbing. Golden, Conviction, Candidate and Confirmed become labels whose incremental value is tested; they do not define the entire measured population. Keep copy/refusal experiments separate because their source universe and outcome model differ.

Keeping independent quote loops for every named arm preserves the current timing confound and wastes capacity. A new automated model-training platform is unnecessary. A small read-only comparison report over immutable snapshots is sufficient for the first cycle.

This is an experiment-contract change across existing collectors/reporting, not a one-line reset. The design must be settled before the producer switch.

## 1. Fresh means a verified producer boundary

Create one experiment manifest with a unique ID, code hashes, configuration hashes, policy and feature definitions, provider identities without secrets, clock definition, quantities/decimals and intended read schedule. Freeze the Golden roster and Conviction definitions as **unvalidated labels**, not inherited proof.

Prepare fresh scanner/impulse databases at new experiment-specific paths. The active research manifest resolves the paths for their writers and readers; the old databases remain readable as historical artifacts. Review every existing reader of `runner_signals.db` before switching. Do not reset raw market feeds, the funded ledger, wallets, or funded positions.

For the paper clone and refusal collectors already writing separate ledgers, establish an explicit new **reporting cohort** on fresh origins rather than clearing their databases or changing the funded producer. Reuse existing per-position frozen policy and fill metadata. Exclude any row whose producer/policy provenance cannot be verified. A reporting boundary is not a new code version and must not be presented as one.

Record per-lane ready acknowledgements. Activate a common corrected-UTC boundary only after all intended producers acknowledge the manifest. Entries and origins must both follow that boundary. A late-starting or unhealthy lane remains `not_ready`; its missing interval is visible. No backfill of prices or decisions and no retroactive “started at” claim.

On restart, resume only the active cohort. An experiment-scoped dedupe must not inherit lifetime Golden/Conviction `done` sets. An unfinished old paper position remains in the old cohort. Pending work that misses its window becomes typed incomplete data, not a stale fresh entry.

## 2. What is a winner?

**Proposed primary target: exact-quantity quoted round-trip return at +30 seconds after entry-quote completion.**

- Fixed research clip: **100 USDC**, matching the current scanner. This is measurement size, not funded sizing.
- Obtain an entry quote after the primary decision-feature snapshot is frozen. Retain discovery time, feature availability, quote request time and completion time separately.
- Define a quote winner as positive measured round-trip return, quote loser as negative, and flat as zero. This is explicitly **quote** performance. It is not an executed net winner.
- Record expected output and slippage floor separately. Never substitute the floor for expected proceeds or describe a quote as a fill.
- If complete transaction-cost inputs are available, publish a separate net-after-recorded-costs label. If they are unavailable, net outcome is unknown. Do not invent a universal execution-cost estimate.
- Missing quote, late quote, malformed amount, provider failure or wrong identity produces an **unknown/unmeasured outcome**, not a loser, zero, or success.
- Severity stays visible: continuous return, severe-loss frequency and tail losses beside win rate. A high win rate cannot hide a worse mean.

Secondary horizon targets: **+5, +10, +15, +60, +120 and +180 seconds**, all relative to entry-quote completion. Primary quote completion must land in [30s, 32s]; descriptive legs use [h, h+2s]. Timing is intentionally strict: a slow provider reduces coverage rather than silently moving the horizon. Store actual elapsed times; feasibility must pass a bounded calibration before activation.

These horizons are separate outcomes. Do not choose the best one after inspecting results and call it the primary. Discrete observations measure sampled peaks/retraces only; they cannot establish the true intrainterval high, a touched stop, or an executable exit between observations.

Existing scalp/hold policy outcomes may remain a distinct diagnostic lens where the full required path is recorded. They are not ground-truth winner labels. No new TP/stop optimization in the first cycle, and no claim to reconstruct a 30-minute hold from a three-minute recording.

## 3. Record the denominator before selecting winners

The universe is the scanner's actual evaluated candidate stream, with its discovery-source and watch-coverage limits stated. It is not all Solana launches.

Persist each evaluation's identity, availability time, available feature snapshot and admission/skip reason. Distinguish no source data, incomplete data, profile non-match, duplicate, deterministic sample rejection, capacity skip and entry quote failure. Current early returns that discard nonmatching evaluations need explicit outcomes.

Use a deterministic cohort-seeded mint sample independent of Golden/Conviction labels and later returns. First eligible decision per mint is the primary unit. Keep repeated episodes as a labelled secondary dataset, clustered by mint. Retain sampled label-negative controls from the same candidate stream. Capacity skips remain in coverage accounting and never get priced retrospectively.

Cross-chain clone/refusal evidence stays chain-specific. Do not mix it with scanner coin-discovery observations or turn a funded refusal into a losing-outcome label.

## 4. Features and time-of-knowledge

Reuse fields already collected. The first cycle adds provenance and coverage, not a shopping list of new APIs.

| Feature group | Record | Required caveat |
|---|---|---|
| Coin state | observed market cap, age, venue/source, volume, momentum, distance from high, prior run | supply/decimals and source timestamps; estimated mcap stays labelled estimated |
| Buyers | unique count, concentration of buy volume, same-second activity, tail length/truncation | latest-N tail is not a complete time window or entity-resolved population |
| Holders | raw top shares, largest account, ex-largest shares, resolution coverage | unknown account purpose is not automatically pool or whale |
| Wallet labels | Golden/elite/recent/cold/rug presence and frozen version | absent buyer coverage is unknown, not zero labelled wallets |
| Execution conditions | signal-to-decision delay, quote RTT, quote-to-chart difference, amount, route and failure class | quote-to-chart difference combines time and pricing basis |
| Availability | collection start/end, source timestamp, coverage and failure reason per feature group | a post-entry feature cannot explain the entry-time decision |

Freeze the pre-entry feature set at the decision instant. Expensive enrichment arriving later is a separate observation with its actual arrival time. Confirmed wallet activity after entry is a time-varying event; compare it only using a new confirmation-time decision and fresh quote, not by leaking it into the original entry features.

For impulse recording, establish an observed pre-window before calling a coin quiet. No marks, one mark, sparse/stale marks and a flat well-covered path are different states. Insufficient coverage must not enter the quiet-versus-active causal comparison.

## 5. The comparison report

For each lane and version, show coverage first, then:

1. **Winners:** feature frequency/range and continuous return distribution.
2. **Losers:** the same fields, including severe-loss subsets.
3. **Shared:** features prevalent in both groups; these may describe the universe without separating outcomes.
4. **Differences:** risk difference, return difference, sample size and uncertainty, after matching/stratifying observable age, market cap, momentum, source and day where overlap exists.
5. **Unknown:** missing inputs/outcomes and whether missingness differs by label or outcome group. Do not drop this population silently.

Feature status is one of:

- `candidate_useful`: a frozen feature/band adds a useful holdout difference after multiplicity control; still research only.
- `candidate_harmful`: replicated holdout association with worse outcomes; not permission to introduce a funded gate.
- `redundant`: no incremental improvement beyond a simpler recorded feature set at the predeclared precision.
- `equivalent_at_resolution`: the entire adjusted interval lies within the predeclared practical-equivalence band.
- `insufficient`: too few independent observations, wide interval, poor overlap, excessive missingness, or invalid execution provenance.

**Not statistically significant does not mean useless. Shared does not automatically mean unnecessary.** A feature can matter through an interaction or a specific context. Test only a small predeclared set of such interactions; do not search combinations until something looks good. A redundant fetch can be removed from research only after checking that no safety/coverage dependency needs it.

The report must show mean and median alongside win rate. For the first cycle, the proposed practical resolution is **5 percentage points of win probability** and **3 percentage points of mean quoted return**. These are experiment design choices, not thresholds learned from invalid history. Use multiplicity-adjusted intervals across the frozen feature comparisons. A feature is not ruled out if either material benefit or material harm remains compatible with the interval.

## 6. Learn without moving the target mid-run

One seven-day cycle, using only freshly recorded data:

- First 72 hours: feature discovery and feasibility. Health metrics remain visible. Candidate features/bands and a minimal comparison model are selected here, without old outcomes.
- At hour 72: freeze the candidate list, bins, missing-value rules, effect thresholds and analysis code before viewing holdout returns. Persist its hash.
- Remaining 96 hours: untouched forward validation. No threshold tuning, roster refresh or policy edits inside this stage.
- At day seven: read once. Cluster by mint and report day-level sensitivity; name the limited number of independent days. Declare insufficient evidence when precision/coverage is inadequate instead of extending by pooling invalid or older-than-week data.

No low-support category may be called established or eliminated. Size requirements come from the desired interval precision and observed **fresh discovery** variance, not an arbitrary trade count or old paper returns. If projected capacity cannot reach that precision in the holdout, publish `not estimable at this budget` before chasing the outcome.

The next cycle begins with a new version and tests fewer hypotheses or a better-measured one. An elimination log records scope, cohort, test, effect interval and why the feature was removed. Prior-cycle findings become hypotheses/context; current proof must still respect the operator's seven-day maximum age. No automated changes to funded gates.

## 7. Budget and isolation

No new paid provider, funded key, funded quote request, synchronous funded callback, global resolver patch or shared writable connection is introduced. Main-ledger readers use `get_readonly_connection()` and short completed reads.

Proposed fresh recorder envelope: scanner **at most 40 quote attempts/minute**, impulse **at most 10**, with retries counted, a scanner in-flight cap of **2**, and no separate polling per same-quantity policy arm. The combined nominal envelope does not exceed the old scanner's 50/minute cap; this is a cap, not a claim about the upstream provider's actual allowance. Existing separate refusal budgets remain unchanged. Provider/IP contention must still be measured.

Use the same completed quote for the same mint, exact quantity, request purpose/time and all paired policy evaluations. Do not reuse stale observations or collapse different quantities. Prioritize already-promised horizons over new admissions; missed deadlines are typed, never silently stretched. Rate limits trigger research backoff, not endpoint hunting through custody providers.

Budget feasibility is an activation gate. Run a small bounded research-only calibration to measure completion lateness, p50/p90 RTT, actual poll gaps and attempts/minute under this envelope. Calibration rows are excluded from the experiment. If the timing contract cannot be met, amend it visibly before the fresh activation rather than silently relaxing it afterward.

## 8. Files and existing seams

- `scripts/research/runner_scanner.py`: evaluation denominator, feature freeze, timed quote records, shared observations, current cohort recovery and honest alerts.
- `scripts/research/impulse_forward_collector.py`: fresh cohort/cursor, timing validation and absent-versus-observed quiet coverage.
- Existing research watchdogs: active manifest/path and producer identity verification; never restart the funded runner as part of this research switch.
- One small research-only manifest/storage helper if required by both collectors; no general experiment framework.
- One research comparison/report script, reusing installed NumPy and existing statistical utilities where appropriate.
- Existing clone/refusal read scripts: enforce the active cohort boundary and retain immutable policy/fill eligibility; no entry-path modifications.
- Scanner consumers such as roster-refresh and scalp-probe scripts: resolve the active research path and refuse to mix historical results into current evidence. Disabled consumers stay disabled.

The SQLite index lacks the standalone scanner schema. Its real DDL and live PRAGMA schemas have been inspected. Any new schema must be documented as proposed and exercised in temporary databases before activation.

## 9. Edge cases and required proof

Money path: none of these changes may reach funded decisions. Added synchronous funded calls/locks/retries must be zero by call-site review; do not fabricate a measured latency improvement. Record funded heartbeat/entry continuity separately around activation.

Concurrency: independent process-owned research connections; quote result identity includes mint, quantity, cohort and purpose; no module/environment mutation in funded threads. A delayed result cannot fill a different request.

Absent/zero/unknown: preserve null plus reason for every unavailable feature and outcome. Zero output is not a valid quote. Flat observed price is distinct from absent history.

Dependency failures: typed timeout/transport, rate limit, authentication/ban, explicit no route, malformed/partial response, wrong mint/amount/decimals, and source-age failure. Never turn any of these into a market loss or a passing gate.

Units/identity: atomic token quantities stored as decimal text in new records, validated decimals and chain-qualified mint keys; both UTC provenance and monotonic scheduling elapsed times. Clock changes invalidate affected windows rather than backdating observations.

Sample: save skipped and unmeasured decisions; no survivor-only denominator, no chart-peak label as trade profit, no future feature leakage, no reuse of old entry quantities for fresh entries.

Rollback/partial activation: old files stay intact; stage and validate a new manifest, then atomically switch it. A failed acknowledgement keeps the new experiment inactive. Roll back research code/path only; failed-run rows remain invalid and are never merged into a retry cohort. No database backup is required or proposed.

Required red-then-green behavioral checks, using existing pytest conventions:

- +42s successful completion cannot enter a +30s result; the current audit reproducer already demonstrates this failure.
- Old origins/entries and wrong code/config hashes cannot enter the fresh cohort, including closes after activation.
- Same identity and quantity share one observation; different quantities do not.
- Restart resumes current pending work without replaying old histories; missed windows become incomplete.
- Absent tape is not quiet; an error is not zero; a late feature cannot enter pre-entry predictors.
- Synthetic identical winner/loser feature distributions are never called discriminating; small noisy samples stay insufficient; a predeclared strong difference can be detected on a separate holdout.
- A failed/half-applied activation cannot advertise `recording_fresh`; funded paths and archived lanes are untouched.

Not handled by this change: predicting an individual coin reliably, actual transaction landing/slippage proof, guarantees that a stop fills at its threshold, entity resolution from holder accounts, price paths between sampled quotes, automatic promotion, or automatic funded strategy tuning.

## Activation acceptance

Only call the restart complete after: focused checks pass; a calibration meets the frozen timing/budget contract; producer acknowledgements identify the intended code/settings; the first genuinely fresh rows and their raw quote timing are verified; old rows cannot appear in the current report; missing evidence renders unknown; archived lanes stay off; funded continuity is unchanged. Until then the status is preparing or not ready, not a fresh experiment already collecting.


## First-cycle implementation choices

- Final scanner sample: modulo 1 (every eligible first mint observation), independent of labels; six open measurement positions, at most two quote requests in flight. Impulse measures eligible source events independently of quiet/active labels; missing pre-window coverage is its own unknown group, excluded from quiet-versus-active comparisons, at most two open positions and one request in flight. These are capacity settings, not historical performance choices.
- Quote retries: zero. Every attempt is counted; missing or rate-limited legs remain unknown. Calibration uses six scanner and two impulse SOL-route positions through the actual scheduler. It tests scheduling, not memecoin route availability.
- Holder shares use observed token supply and matching decimals, not assumed billion-token supply. Coin candle market cap remains explicitly estimated at one billion supply. No unavailable transaction-cost inputs are invented.
- Six candidate comparisons are frozen at hour 72: Golden presence, Conviction >=10, buyer count, ex-largest five holder share, 60-second momentum and age. Continuous thresholds are fresh-discovery medians. No interactions in this cycle. Missing input is separate. Context strata are UTC day, discovery source, market-cap band, age band and momentum band. Poor overlap (<50%) stays insufficient. Bonferroni covers six features and both endpoints; Wilson win intervals protect tiny/constant samples, mint bootstrap describes mean-return uncertainty, and at least three days with consistent direction are required for a candidate association. These remain observational associations, not causal or funded verdicts.
- Freeze is recorded within 60 seconds of hour 72 before any holdout read. A missed freeze invalidates the holdout. The day-seven read is persisted once; proof expires under the rolling seven-day rule. Health never prints sealed returns.
- Existing clone/refusal writers continue untouched. Their current runtime identities and paper-contract signatures are frozen at preparation. A detected external producer change permanently invalidates that reporting epoch. Standalone refusal capture rows lack immutable producer hashes and remain excluded from proof; their data is retained.
- Legacy roster-refresh/scalp-probe entrypoints refuse historical mixing when a fresh manifest exists. No archived lane or disabled consumer is enabled. Historical paper lifecycle/scalp/hold PnL is not reconstructed from discrete fresh quotes.

Startup validation correction: scanner discovery admits only create/feed/axiom origins received at or after the common activation. Unseen coins are ordered newest first, then existing watches by stale evaluation time. The initial v1 startup populated its watch from the old 30-minute window (first observed ages 1,788–1,797 seconds); it is retained as an excluded pilot, not evidence. The final cohort begins after this correction. The public holdout reader serves the persisted single read rather than recomputing it during the read allowance.


Final feasibility setting: `fresh-20260928-v3` uses scanner sample modulus 1. The v2 pilot recorded 18 first eligible coins rejected by the 1-in-20 sampler and zero quote attempts; it is excluded from evidence. This is a coverage/budget correction made without reading any returns. Quote budgets (40/10), open-position caps (6/2), in-flight caps (2/1), horizons, snapshot definitions and analysis rules are unchanged. The v3 scheduler calibration completed 64/64 requests inside their windows; scanner RTT p50/p90 was 32.6/44.9 ms, impulse 32.6/34.2 ms. Maximum completion lateness was 135.1 ms inside a 2-second allowance. Calibration is excluded from evidence.

An external funded deployment changed the copy/refusal process during v3 preparation. Before activation, both unopened research stores were verified to contain zero decisions, requests and observations; their manifest provenance was rebound to the current external producer and both producers acknowledged again. The preparation revision is retained. Research code, settings and provider were unchanged, so the completed scheduler calibration remains applicable. Actual activation: 2026-09-28T11:55:07.399316Z. The runtime manifest is the authoritative receipt.


Inference guard: descriptive complete-case tables are not feature verdicts. Within the mature, successful-entry population, any missing primary outcome or missing compared feature leaves that comparison `insufficient`; no missing-at-random assumption or finite upper return bound is available. Precision projections flag this capture limitation before discussing sample size. Entries with less than 32 seconds of follow-up opportunity at the read boundary are separately counted as right-censored, including completed early replies in that boundary tail, rather than selected by whether their reply happened to arrive. Entry failures remain visible in coverage; they do not have an acquired-quantity return. Clean synthetic holdouts can still detect a registered difference. This report repair changes no quotes, selection settings, or producer behavior; the final activation receipt supersedes the provisional v3 receipt above.


Final receipt: `fresh-20260928-v4`, activated 2026-09-28T12:04:00.277554Z, pinned research release `da8ca04fdedc2340f373ef51d10a067dc1a00fee`. Settings match v3, including sample modulus 1. Only the analysis report changed after the passing v3 calibration: SHA-256 comparisons verified all eight measurement files byte-for-byte identical, with the same effective quote configuration and provider identity. That calibration and its compatibility proof are retained in the final cohort; no extra quote requests were needed to retest unchanged recording code. V3 remains a validation pilot, excluded from feature evidence. Discovery freezes at 2026-10-01T12:04:00.277554Z; the holdout read is 2026-10-05T12:04:00.277554Z.
