# Memecoin Impulse Conditioner Forward Quote Preregistration

**Date:** 2026-08-20 (v2, amended the same day, before activation)
**Status:** SEALED. The collector is not started. A separate approval starts it.
**Origin:** `scripts/research/wallet_impulse_conditioners.py` (commit `f2bd6872`),
`scripts/research/wallet_impulse_activity_study.py`, and the memory
`finding-memecoin-impulse-conditioners-2026-08-20`.

## Amendment Record

v1 sealed a hot-streak gate with a +60 second primary horizon. The same-day chart review and
the +300 second measurement changed the design before any forward data existed:

- Inside the sub-1M band, a QUIET pre-signal tape beat an active one (+6.34% vs +2.94%
  median at +300s; quiet n=492, CI [+3.9,+8.4]).
- The hot-streak condition added nothing at +300s once quiet and band held (+6.64% vs
  +6.34%). The streak stays as a recorded feature, not a gate condition.
- The event charts showed moves that develop over 2 to 5 minutes; +60 seconds truncates
  them. The primary horizon is now +300 seconds.

No forward event was collected under v1. This amendment is therefore a pre-data revision,
not a peek.

## Question

Does the gate (source market cap below 1M, quiet pre-signal tape) give a positive net
quote-to-quote return at +300 seconds on new events?

The in-sample study showed a median mid-tape response of +6.34% for this gate. A mid-tape
response is not a fill, and quiet thin pools carry the worst route cost. This study measures
the same gate with exact executable route quotes on both legs. The quotes embed route cost,
so no slippage knob exists in the metric.

This study grants no funded authority. It opens no position. It signs nothing. It creates no
paper strategy arm.

## In-Sample Provenance

The gate came from a search over the 2026-07-21 to 2026-08-20 window. The search examined
many cuts. The forward sample starts only at collector activation. The collector must record
its activation timestamp. Events before activation never enter the forward sample.

## Frozen Gate — Primary Arm

A trigger is a new `memecoin_events` row that meets all these conditions:

1. `side = 'buy'` and `their_price > 0`.
2. `network_id = 'solana'`.
3. `their_market_cap` is present and below 1,000,000. A missing market cap excludes the
   event. This rule is fail-closed.
4. The pre-signal tape is quiet: at most 12 distinct consecutive `mid` changes on clean
   `memecoin_position_marks` rows for this mint in the 600 seconds before the trusted
   receive stamp (`received_at_normalized`, else `received_at`). Row count does not count;
   only value changes count.
5. Episode dedupe: no primary measurement started for the same mint in the prior 600 seconds.
   The first qualifying event wins.

The collector records, but does not gate on, the wallet's hot-streak state: whether its
trailing 3 scored impulse responses are all above zero, with the study's exact scoring
definition and closed windows only.

The marks tape covers only coins our runner traded, so a quiet tape can also mean a poorly
watched coin. This coverage confound is known, frozen, and reported; the executable quotes
are the correction, because an unroutable quiet coin censors itself.

## Frozen Control Arm

For each primary trigger, the collector takes the next event that meets conditions 1-3 and 5
but fails only condition 4 (an active tape, 13 or more distinct changes), inside a 30 minute
window after the trigger. The collector quotes the control identically. A missing control
does not remove the primary measurement; the collector marks the control absent.

The control isolates the quiet-tape component. The band component alone is not the claim
under test.

## Measurement Protocol

For each measured event (primary or control):

1. At the trigger, request one Jupiter ExactIn quote, USDC to the mint, amount 100 USDC
   (100,000,000 atomic), through the existing quote seam
   (`sniper/services/memecoin_v6_paper_quotes.py:_jupiter_quote`). Record `outAmount` as the
   acquired quantity.
2. Persist the row before the exit leg fires.
3. At trigger time +300 seconds, request one Jupiter ExactIn quote, mint to USDC, with the
   exact acquired quantity as the input amount. This is the primary exit leg.
4. At trigger time +60 seconds, request one more sell quote of the same quantity. This leg
   is descriptive only. It never enters the primary metric.
5. Net return = primary exit `outAmount` / 100,000,000 - 1, in USDC atomic units.

Timing and retry rules:

- The entry quote must complete within 10 seconds of the receive stamp. A late entry quote
  censors the event with reason `entry_quote_late`.
- The primary exit quote fires at +300 seconds and must complete by +315 seconds. A late
  exit quote censors the event with reason `exit_quote_late`. A late +60 second descriptive
  quote marks only that leg absent.
- Transient retries follow the sealed retry design
  (`docs/superpowers/specs/2026-08-05-solana-quote-infra-transient-retry-design.md`): one
  retry after 200 ms for `rpc_transport` or `provider_timeout` only, provider group
  `solana:exact_quote`.
- A quote failure censors the event with its typed reason. The collector never imputes a
  price from `their_price`, DexScreener, or a chart mid.

Concurrency: at most 4 in-flight measurements, and at most 1 per mint. An event skipped for
capacity is logged with reason `capacity_skip` and counts as censored.

## Persistence

Rows go to a new append-only table. The collector never updates a row after its +300 second
leg resolves.

<!-- schema:external=impulse_forward_quotes -->
```sql
CREATE TABLE IF NOT EXISTS impulse_forward_quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fomo_event_id TEXT NOT NULL,
    arm TEXT NOT NULL,               -- 'primary' | 'control'
    paired_primary_id INTEGER,       -- control rows only
    mint TEXT NOT NULL,
    source_user_id TEXT NOT NULL,
    source_handle TEXT,
    their_market_cap REAL NOT NULL,
    pre_moves INTEGER NOT NULL,      -- distinct mid changes, prior 600s
    hot_streak INTEGER NOT NULL,     -- recorded feature, not a gate
    streak_values TEXT,              -- JSON, the trailing scored responses if any
    t0 TEXT NOT NULL,                -- trusted receive stamp
    entry_quote_at TEXT,
    entry_out_atomic INTEGER,
    exit300_quote_at TEXT,           -- primary exit leg
    exit300_out_atomic INTEGER,
    exit60_quote_at TEXT,            -- descriptive leg
    exit60_out_atomic INTEGER,
    censored_reason TEXT,            -- NULL = scored
    created_at TEXT NOT NULL
)
```

## Read Gates

Before the primary gate, only counts, censor rates by reason, and fault states are visible.
Do not inspect return values.

The primary read occurs after all these floors are met:

- 50 non-censored primary measurements;
- 25 distinct primary mints;
- 7 distinct UTC trigger dates.

If the result is `WEAK`, one extension runs to 100 measurements, 40 mints, and 14 UTC dates.
No read occurs between the two gates. If the final floors are not met within 30 days of
activation, report `NOT ESTIMABLE` and stop.

A censor rate above 40% at either gate is itself a reportable defect. Report the all-censored
aggregate; a censor split is a latency signature, not noise to discard
(`finding-radar-current-bucket-decision-2026-08-12`).

## Metrics

Primary, with a mint-cluster bootstrap (10,000 samples, seed `20260820`) and Holm correction
at `alpha = 0.05` across the two:

1. Median net return of the primary arm at +300 seconds.
2. Median paired difference at +300 seconds, primary minus its present control.

Secondary, descriptive only: mean net return, net hit rates at +5% and +10%, the +60 second
net return, censor counts by reason, per-mint and per-wallet concentration, and the
hot-streak stratification of the primary arm.

Do not use the arithmetic sum of percentage returns.

## Decision Rules

- `REFUTED` (gate): the Holm-adjusted upper 95% bound of metric 1 is at or below zero. The
  gate does not clear route cost. Stop.
- `QUIET REFUTED`: metric 1 clears zero but the adjusted upper bound of metric 2 is at or
  below zero. The band carries the result and the quiet condition adds nothing. Report the
  band-only question as a new, separate preregistration; do not extend this one into it.
- `ESTABLISHED`: the adjusted lower bounds of both metrics are above zero.
- `WEAK`: every other result. Run the single extension, then apply the same rules as final.

An `ESTABLISHED` result is measurement evidence only. Any paper strategy arm or funded use
needs its own spec, its own approval, and its own gates.

## What This Study Does Not Do

- It does not open, reserve, sign, or submit anything.
- It does not touch the funded runner, its `.env`, or any kill switch.
- It does not write to any existing table.
- It does not change `VENUE_AUTHORITY`, V6, or any radar arm.
