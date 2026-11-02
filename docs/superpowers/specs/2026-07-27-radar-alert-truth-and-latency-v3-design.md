# Radar Alert Truth and Latency v3 Design

**Status:** Approved on 2026-07-27.

## Goal

Make every Radar alert represent one exact paper entry made from information available at the alert timestamp, then measure the executable path after that entry. Reduce quote latency and resource use without increasing provider request rates.

The Radar remains capture-only and paper-only. It never signs, submits, sizes, reserves funds, opens funded positions, or imports funded execution code.

## Confirmed defects

The v2 evidence paths use different counterfactual entries:

- `op_forward_alerts` records the first `CENTER` prediction and its remote round-trip quote.
- projected fired samples use a launch-level outcome selected independently of the alert timestamp;
- target-ladder and near-miss diagnostics use the launch's earliest counterfactual entry;
- the UI presents the alert-time round-trip quote beside these unrelated forward outcomes.

Consequently, a fired sample can be labelled with an outcome from before or after its alert. Existing fired-alert metrics do not measure alert quality.

The Solana quote lane also spends work on every eligible landmark. A two-call Jupiter round trip is paced at the configured provider rate, and a completed result can remain reusable for the cache TTL. Under load, low-quality launches consume quote capacity before inward-moving candidates.

## Truth boundary

Three evidence families remain separate:

1. **Launch diagnostics:** generic landmark entries used to study when launches become tradeable.
2. **Alert entry:** one exact paper entry created from the fresh remote quote that caused the first transition to `CENTER`.
3. **Alert forward result:** executable observations and target results attached only to that alert entry.

No launch-level outcome may be displayed or aggregated as an alert outcome unless it is joined through the alert's exact entry identity.

Existing v2 alerts have no durable exact-entry link. They remain immutable but are exposed as `UNSCORED_LEGACY`. Existing generic launch diagnostics remain valid for their stated landmark entry and must not be relabelled as alert results.

## Alert-specific entry contract

Add an append-only `op_forward_alert_entries` relation with:

```text
alert_id
entry_id
snapshot_id
quote_observed_at
quote_completed_at
quote_age_ms
entry_effective_at
provider
evaluator_version
row_fingerprint
inserted_at
```

`alert_id` and `entry_id` are unique. The entry stores:

- configured $25 clip;
- exact acquired token quantity from the remote entry quote;
- modeled entry inclusion latency;
- provider and quote version;
- the landmark that produced the alert;
- no information received after quote completion.

An alert is scored only when this link exists. Redelivery is idempotent. Missing or stale exact quote evidence produces no alert entry and no scored alert.

## Quote freshness

Remote quote results carry corrected-UTC timing for:

```text
queued_at
entry_quoted_at
exit_quoted_at
completed_at
consumed_at
```

The trigger records both provider latency and queue delay. `CENTER` requires:

- entry quote succeeded;
- exact acquired-quantity sell quote succeeded;
- the pair was consumed within the configured completion-age limit;
- the entry leg is within the configured pair-age limit;
- no source gap or immediate risk interrupt is open.

Freshness blockers are explicit: `REMOTE_QUOTE_QUEUE_STALE`, `REMOTE_QUOTE_PAIR_STALE`, and `REMOTE_QUOTE_RESULT_STALE`.

Initial defaults are:

```text
maximum queue age:       2,000 ms
maximum quote-pair age:  3,500 ms
maximum completion age:  1,000 ms
maximum cached results:  256
maximum completions:     256
```

They are configurable, but a missing or invalid setting fails startup validation. The defaults accommodate the existing two-call paced pair without allowing the prior five-second post-completion trigger window.

## Quote scheduling

Use two-stage evaluation:

1. Evaluate curve kinetics, buyer breadth, concentration, creator flow, local runway, and integrity without a remote quote.
2. Request a remote quote only when all non-route conditions reach `INNER`.

The quote lane:

- coalesces one active request per venue, token, and clip;
- prioritizes smaller zone distance, higher inward velocity, then freshness;
- discards queued requests that exceed the queue-age limit;
- exposes completed results through a bounded completion queue;
- bounds cached entries and removes expired entries without requiring that token to be requested again;
- preserves the existing provider request-rate default for the first rollout.

Each request carries the launch state fingerprint used for prioritization. The main worker consumes quote completions and immediately re-evaluates the latest immutable launch state. It does not wait for another chain transaction. The engine rechecks all non-route gates against that latest state; a state that deteriorated while the quote was in flight cannot reach `ROUTE READY`. Quote-completion evaluation references the last canonical event and persists its later decision timestamp separately.

## Forward evaluation

Alert results use only the linked alert entry. They retain:

```text
+2%, +4%, +6%, +8%, +12% first-passage results
5s, 10s, 20s, and 60s horizons
maximum favorable executable return
maximum adverse executable return
time to best executable return
loss-first, exit-failure, migration, data-gap, or timeout result
last executable return
```

The +12% barrier is no longer the sole success summary. The UI and API show the target ladder and continuous excursion. No target is promoted into a trading policy by this change.

Historical and live aggregation remains launch-clustered. Alert metrics are additionally clustered by one alert per launch and rule version.

## UI contract

Use operational language:

- `WATCH`: improving non-route evidence;
- `QUOTE NEEDED`: non-route gates passed and prioritized for an exact quote;
- `ROUTE READY`: a fresh $25 entry and exact acquired-quantity exit both quote now;
- `FORWARD RESULT`: the outcome after that exact paper entry;
- `UNSCORED LEGACY`: an older alert without an exact-entry link.

`ROUTE READY` is executable evidence, not proof of positive expected value. The UI must not call it actionable, profitable, or a recommendation.

For each alert, display separate fields:

```text
quote drag at decision
quote pair age
modeled entry time
forward best return
forward worst return
targets reached and elapsed time
terminal result
```

Generic launch target ladders remain in a separately labelled research section.

## Insight versioning

Introduce an alert evaluator version distinct from the generic launch evaluator. Live alert summaries read only exact-linked v3 samples.

Do not delete or rewrite existing immutable alerts. The projection may retain their receipts for audit, but they contribute zero observations to v3 alert precision, returns, or target-hit rates.

## Resource controls

- Keep the initial Jupiter provider pace unchanged.
- Bound pending requests, completions, and cached results.
- Expire stale queued work before making a provider call.
- Prefer alert-outcome sell observations over weak discovery requests only when both share the same bounded quote lane.
- Report queue delay, provider latency, discarded stale requests, cache size, evictions, and completion age.
- Preserve the hot-ledger and insight-ledger retention policies.

## Failure handling

- A stale, missing, throttled, or malformed quote fails closed for `ROUTE READY`.
- A worker restart restores durable state but does not recreate an alert entry from an old cached quote.
- A chain correction or open frontier gap invalidates the associated live trigger and censors affected forward evidence under existing rules.
- Provider failure never degrades chain capture health; quote health remains a separate lane.
- Unsupported Bags/Solana forward-exit math remains explicit and cannot silently inherit Pump.fun curve math.

## Validation

Use focused RED/GREEN tests only:

1. a fired alert selects its own linked entry outcome, not first/latest launch outcome;
2. old unlinked alerts are `UNSCORED_LEGACY` and excluded from alert metrics;
3. generic launch diagnostics remain unchanged and separately labelled;
4. weak landmarks do not enqueue remote quotes;
5. inward candidates outrank weak candidates without increasing configured RPS;
6. stale queued and stale completed quotes cannot reach `ROUTE READY`;
7. cache and completion structures remain bounded and evict expired values;
8. quote completion can produce a decision without a new chain event;
9. API/UI separates quote drag from forward return and renders terminal results;
10. opportunity modules retain no funded, signing, submission, position, or runner imports.

Operational verification uses the exact worker launch path, authenticated Radar API, process health, bounded storage, quote metrics, and one fresh alert receipt if naturally observed. It does not force an alert or run a broad test suite.

## Promotion boundary

This change repairs evidence and timing. It does not claim an edge and does not choose a funded entry or exit policy.

After enough v3 alerts mature, compare target hit rates, loss-first rates, MFE/MAE, and queue/quote latency by point-in-time pattern. Threshold or exit-policy changes require a separate paper-policy decision based only on v3 alert-linked evidence.
