# CROWD funded pilot — operator order 2026-11-01

The operator requests CROWD live with $25 clips and assigns Codex leadership. Office seats, paper watchers and Office cron jobs remain paused. This order promotes the previously shadow-only hypothesis for a bounded pilot; historical exploratory returns are not forward validation.

## Frozen strategy

- Separate cohort `large-buyer-crowd-v1`; never pool with the first-buyer cohort.
- Trigger when a distinct wallet first reaches cumulative 25 SOL buys in a pool with no earlier sell in the observed tape before its own first reach, and at least one other distinct qualifying wallet reached that threshold in the prior 60 seconds, causally known before this trigger. Later sells do not erase historical qualification.
- Freeze `history_basis=observed_tape` in every request and funded contract. The cohort policy explicitly marks capture incomplete. Missing history is unknown; neither the detector nor the study proves a wallet's lifetime absence of sells. The extra complete-history admission requirement is removed; token safety, mint proof, sell-route and risk gates remain mandatory.
- Real pool quote reserve before trigger must be at least 40 SOL. Preserve source ordering and receipt/detection times; missing order or crowd proof refuses funding.
- Target entry 10 seconds after trigger receipt. Detection must be within five seconds of receipt; producer skips after entry due plus five seconds. Queue TTL is five seconds on corrected UTC, not an execution/landing guarantee.
- $25 fixed clip; TP40, SL30, no trail, 300-second max hold frozen before signing; existing catastrophe/unquotable protections stay active.
- Hard 30 funded admissions including in-flight/unknown reservations, across concurrent producers and restarts. Admissions are append-only and never automatically released, including failed or expired requests. Two concurrent desk positions; realized daily-loss admission stop at $50. Open losses, gaps, fees and failed exits can exceed a realized-loss threshold; it is not guaranteed maximum PnL loss.

## Safety and isolation

Actual immutable mint/decimal evidence replaces suffix guessing. Causal finite SOL/USD evidence is required; future/stale/unknown evidence refuses. Global token safety must be enabled for the pilot, with override prohibited. The existing real runner must perform its token checks, planned-quantity reverse quote, payout-route proof, impact/round-trip checks, exposure/loss/kill gates and atomic cross-lane coin reservation before signing. No funded provider, signing or custody implementation is replaced. A quote is present sellability evidence, not future honeypot/rug immunity.

All trigger ingestion/proof caching is off the copy critical path. No new provider call, retry or process-global mutation in a copy entry. Measure detection and shared-ledger contention separately from copy execution; source tests do not establish production p50/p90 or a fill deadline.

## Rollout

Additive idempotent schema upgrades preserve existing history and leave unavailable old fields NULL. Independent exact-SHA money-path review and focused regressions precede integration. Deploy an immutable release, verify reservations/book state and current runner identity, then start only the dedicated pilot trigger/admission processes. Office remains paused. Feature kill is off by default; disabling admission must not stop exits for existing positions. No database backups during deployment unless explicitly requested. Prior explicit .env and money-confirmation limits require operator authorization before those actions.
