# Memecoin X Source Graph: Remaining-Phase Guidelines

**Date:** 2026-07-13
**Status:** execution contract for Phases 2-4
**Depends on:** Phase 1 through `ac089ecb` and the source-graph design spec

## Non-negotiable boundary

This lane is read-only and display-only. It may capture the burner's Following
feed, correlate forward observations, rank sources, notify an operator, and draft
a thesis. It may not follow accounts, interact with X, place trades, size trades,
veto entries, mutate a trading verdict, or claim complete feed coverage.

`posted_at` is metadata. Forward evidence uses only timestamps observed on our
clock in the `observed_real_utc` domain.

## Phase 2: read-only browser adapter

### Build

1. Give capture a dedicated Playwright persistent profile outside Git. Complete login once
   in a headed operator session; never automate credentials/challenges or reuse the normal
   browser profile.
2. First capture minimal sanitized DOM fixtures for real direct, repost, quote, and reply
   cards from the Following feed. Fixture capture performs no X actions and no Sniper DB
   writes. Remove cookies, storage, authorization data, unrelated DOM, and personal profile
   data before committing fixtures.
3. Keep browser/DOM code outside the normalizer and store. Build the parser against those real
   fixtures; synthetic fixtures cover unsafe-to-induce failure states. The adapter emits the
   existing card-fact contract; Phase 1 remains independently fixture-testable.
4. Read only the logged-in burner's Following feed. Never click Follow, Like,
   Repost, Reply, or any post composer.
5. Open one capture run per poll. Take one corrected-clock snapshot and reuse it
   for that run; do not re-read the offset for individual cards.
6. Persist every canonical card delivery as a
   `memecoin_x_card_observations` row. Relationships and mentions must retain its
   `source_observation_id`.
7. Treat cards without a canonical post ID, unresolved authors, invalid direct
   actor/author identity, or incomplete text as diagnostic-only. Record counts;
   do not manufacture forward evidence.
8. Version the DOM parser and selector fixture set. A parser upgrade may replace
   derived mention state only from a complete observation. Keep the old raw card.
9. Record poll start/end, clock health, visible-card count, eligible-card count,
   diagnostic reasons, and errors. Record gaps caused by logout, challenge pages,
   rate limits, browser closure, or DOM drift.
10. Enforce one owner per persistent profile. Pause on a profile lock, challenge, login
   screen, or repeated zero-card poll. Do not attempt
   bypasses or aggressive retries.

### Acceptance gate

- Sanitized real-DOM fixture tests cover direct posts, reposts, quotes, replies, truncated-to-complete
  delivery, duplicate cards, multiple amplifiers, deleted/missing fields, and DOM
  drift.
- Replaying a fixture is idempotent at post, observation, relationship, and mention
  levels.
- An unhealthy clock creates diagnostics but no forward-eligible observation.
- A later healthy delivery can create evidence only at its own observation time.
- A 24-hour dry run produces capture-run and gap metrics with no browser actions,
  DB writes outside the X tables, notifications, or trading effects.

Do not start Phase 3 until a human checks a sample of raw cards against the stored
normalization and relationship facts.

## Phase 3: forward correlator and rolling read models

### Evidence contract

1. Join each X mention through
   `memecoin_x_coin_mentions.source_observation_id` to
   `memecoin_x_card_observations.observed_at` and `clock_domain`.
   Never use `memecoin_x_forward_posts.first_observed_at` as mention evidence.
2. Require an exact normalized mint/contract and network. Ticker-only mentions are
   hypotheses, never forward links.
3. Require both clocks to be `observed_real_utc`. Compare X `observed_at` only with
   the FOMO event's normalized receipt time. Never compare X `posted_at` with FOMO
   `created_at` for lead time.
4. Restrict buyers to the point-in-time real-approved Tier-1 roster and FOMO events
   to v2 normalized buys. Stamp roster role and eligibility into the link so later
   roster edits do not rewrite history.
5. Require `x_observed_at < fomo_received_at_normalized`. Start with the spec's
   six-hour maximum lead horizon; report the distribution before changing it.
6. Keep two lanes:
   - **Amplified:** a qualifying Tier-1 relationship observation preceded the buy.
   - **Coincidence:** the source mention preceded the buy without public Tier-1
     amplification.
   Never merge their counts or imply the coincidence lane proves causality.
7. Exclude official-context accounts from source-rank denominators and coin-caller
   tables. They may appear on a separate narrative/context surface.
8. Quarantined backfill may nominate sources only. It may not create forward links,
   lead-time evidence, evaluation samples, or rank counts.

### Required schema hardening before implementation

The Phase-1 `memecoin_x_forward_links` unique key includes `coin_mention_id`.
Parser correction can replace mention rows, so that key is not a stable logical
identity. Phase 3 must migrate idempotency to a stable key containing canonical
post, FOMO event, buyer, network, and normalized address before writing links.
Preserve old rows during migration and prove duplicate collapse with fixtures.

### Read models

- Show distinct Tier-1 amplifiers over 24 hours, 7 days, and 30 days.
- Keep direct, repost, quote, and reply counts separate.
- Show amplified and coincidence link counts separately.
- Show eligible observations, diagnostic observations, polling gaps, clock failures,
  and sampled time coverage beside every ranking.
- Use raw counts first. Do not introduce a composite conviction score.
- Make new upstream accounts review suggestions only; never auto-follow them.

### Acceptance gate

- Boundary fixtures prove strict-before ordering, equality rejection, horizon edges,
  clock-domain rejection, parser replacement, roster point-in-time stamping, and
  backfill quarantine.
- Re-running the correlator creates no duplicate logical links.
- A hand-audited sample reproduces every stored lead time from the two observation
  rows used by the join.
- Read models reconcile exactly to the underlying forward-link rows.

## Phase 4: display-only operator output

### Build

1. Add a source-graph panel, bounded notifications, and draft signal-thesis output.
2. For every item show source, coin/address, X observation time, FOMO receipt time,
   lead time, evidence lane, relationship type, sample count, clock status, and
   coverage gaps.
3. Show X `posted_at` only as claimed source metadata and label it separately from
   our observation time.
4. Keep official narrative/context accounts in a separate section.
5. Deduplicate notifications by stable forward-link identity and cap repeats.
6. Drafts must say "observed correlation" or "source candidate". Do not describe
   them as proven alpha, causal sources, entries, sizing, or expected PnL.

### Acceptance gate

- No imports from real execution modules and no writes to execution/config tables.
- No changes to `overall_verdict`, `gate_result`, entry, exit, sizing, or veto paths.
- Empty or unhealthy capture state renders as unavailable/incomplete, not zero
  activity or a negative signal.
- Operator-visible counts reconcile to Phase-3 read models.
- Notifications remain disabled until fixture tests and a manual dry-run review pass.

## Future influence gate

This work remains display-only until a preregistered forward evaluation has at
least 100 clock-valid links and beats both a flat baseline and the agreed on-chain
baseline on held-out realized outcomes. Freeze the metric, horizon, exclusions,
and comparison cohort before looking at results. Any later proposal to affect
trading requires a new design, explicit human approval, paper validation, and the
existing memecoin promotion gates.

## Phase 4 extension: tiered, auditable LLM enrichment

The deterministic layer remains the record of truth. The LLM is asynchronous, optional,
display-only, and tool-free. It cannot delay or change capture, correlation, alerts, metrics,
token safety, or execution.

### Invocation tiers

1. **Capture tag:** deterministic only; report observed polling latency without an "instant"
   or lead-time promise.
2. **Fast consensus:** deterministic notification after two distinct point-in-time Tier-1
   actors mention the same exact `(network, mint)` within 60 minutes.
3. **Event enrichment:** queue one idempotent LLM job after fast consensus or an exact-coin
   Tier-2 scout event. Publish the deterministic event first.
4. **Scheduled digest:** call only when eligible evidence exists after the committed digest
   watermark. No new evidence means no call.
5. **On-request:** answer from a persisted `as_of` snapshot, never from the live browser.
6. **Degraded mode:** provider, schema, budget, timeout, or validation failure leaves the
   deterministic pipeline unchanged.

The 60-minute window is an alert policy only. Source-graph metrics and forward evaluation keep
the six-hour primary horizon.

### Job and output gate

- Persist logical trigger, mode, status/attempt, `as_of`, evidence IDs, canonical input hash,
  provider/model/prompt/output-schema versions, generation parameters, token use, budget result,
  validation result, bounded error, raw structured response, and accepted annotation.
- Supply only structured rows and stored observation text. Never supply live DOM/HTML, browser
  profile data, secrets, arbitrary fetched URLs, execution state, or later evidence.
- Accept only closed-schema summaries, explicitly labeled intent interpretation, uncertainty,
  risk concerns, and evidence citations. Every factual claim and exact coin identity must resolve
  to the frozen input rows.
- Reject unsupported output; do not repair it with uncaptured information.
- Never emit a token-safety verdict, price/return forecast, entry/exit recommendation, composite
  conviction score, execution veto, or execution-table lookup.
- Pin prompt/model versions and validate changes on frozen fixtures before enablement.

### Acceptance gate

- Prompt injection through text, handle, ticker, quote, or URL produces no action or accepted
  unsupported claim.
- Delayed, duplicate, stale, invalid, timed-out, rate-limited, and over-budget jobs are fixture-
  tested and cannot change deterministic output.
- Digest and event enrichment remain disabled until an operator reviews a dry-run batch.
- The authoritative edge-case and failure contract is design spec §11; Phase 2-4 plans must map
  every applicable row to a test, diagnostic, or explicit later-phase boundary.
