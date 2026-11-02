# Radar v5 → V6 Realistic-Fill Paper Adapter Design

**Date:** 2026-08-05
**Status:** Superseded by `2026-08-06-radar-owned-realistic-paper-lifecycle-design.md`.
**Verified against:** `origin/main` at `7acceb16`, deployed opportunity release `4e24e56c`.
**Scope:** Measurement only. No arm is created, no rule is changed, no funded path is touched.
**Activation:** Default OFF.

---

## Problem

`pump-confirmation-truth-v5` is the strongest Radar arm and is close to clearing its own
pre-registered gate. Read live on 2026-08-05T14:04Z:

| measure | value |
|---|---|
| valid outcomes / invalid | 314 / 12 |
| mean / median terminal return | +686 bps / +476 bps |
| profit-first / loss-first / time exits | 113 / 9 / 190 |
| matched pairs vs `route-control-v2` | 59 |
| matched advantage mean / median | +759 bps / +698 bps |
| creator-clustered CI95 | [−21, +1476] bps |
| leave-one-creator-out minimum | +617 bps |

Every one of those numbers is scored against a **modeled** fill computed from local curve math on
the captured decision state. This design produces a **conservative quote-modeled fill** instead: a
fresh executable quote, scored at its slippage floor. A Jupiter threshold is not proof of signing,
submission, inclusion, or actual execution — it is a tighter, provider-sourced bound than local
math, and nothing more.

### What is and is not realistic today

| stage | realistic | not realistic |
|---|---|---|
| v5 entry | Exact constant-product math on the captured decision state: own price impact, curve fee, configured network cost. A live Jupiter probe verifies a buy route and a sell route exist. | The fill itself is local math (`sniper/research/memecoin_opportunity/engine.py:481-499`, `constant_product_clip_quote` at `:493`), labelled `local_curve_math_at_decision` (`engine.py:973`). No post-decision request, signing, submission, or inclusion delay. Jupiter gates the route; it does not define the fill. |
| v5 exit | Sells the exact modeled quantity against each later captured curve state; fee, impact, and next-trade barrier overshoot included (`outcome_tracker.py:86` `record_launch_outcomes`, `curve_math.py:54` `constant_product_exit_quote`). | No fresh sell quote at the trigger, no exit latency, no competition, no slippage-floor failure, no expiry or revert. |
| V6 paper | Fresh executable buy quote and fresh exact-quantity sell preview. | Scores Jupiter `outAmount` — the *expected* output — at both `sniper/services/memecoin_v6_paper_quotes.py:584` (entry quantity) and `:660` (exit proceeds). The slippage floor is never read. |

The floor plumbing already exists on the Radar side and is unused for scoring:
`sniper/research/memecoin_opportunity/solana_probe.py:87` defines
`JupiterQuoteReceipt(expected_out_atomic, minimum_out_atomic)`, and `solana_probe.py:95` parses
`otherAmountThreshold`. The gap is in V6 normalization, not in Radar capture.

---

## Goals

1. Produce, for each qualifying v5 decision, a conservative quote-modeled fill from a fresh
   executable quote — or durable typed evidence of why no fill happened.
2. Hand that fill to V6 and let V6 own the position, unchanged.
3. Score both entry and exit on the slippage floor, not the expected amount.
4. Keep v5's modeled clock running untouched.

## Non-goals

- No new arm. `pump-confirmation-truth-v5` is not modified, replaced, or re-scored.
- No standard-only rule. That is a later, separate question needing its own fresh clock.
- No funded authority. Nothing here can arm, size, or execute anything.
- No V6 policy change. The position engine's decisions are out of scope.
- No `$50` claim. `$25` only; see *Clip scope*.
- No artificial entry delay. See *Scheduling*.

---

## Architecture

```
v5 verdict fires (radar worker, in-process)
   │
   ├─ same transaction as the alert: append immutable op_paper_fill_requests row
   │
   ▼
existing OpportunitySolanaProbe worker, lower priority than v5 capture/route probe
   │
   ├─ fresh executable buy quote at $25
   │
   ▼
append immutable op_paper_fills terminal receipt (filled | entry_unfilled:<reason>)
   │
   ▼   (read-only cursor on receipt_seq, separate process, separate database)
V6 worker ──▶ opens an isolated mock position on `filled` receipts only
   │
   ▼
V6 owns the position: its own exit rules, fresh exact-quantity sell preview, action_unfilled
```

**Boundaries.** The Radar worker writes only the Radar database and **never imports V6 services**.
The V6 worker writes only the V6 database and opens the Radar database read-only. Neither process
writes the other's store.

This is the extension point the V6 design already names: *"Live-paper entry adapter — receives an
already accepted upstream buy signal, requests one live executable buy quote for the configured
paper notional, and emits an immutable filled-entry record. It is not part of V6."*
(`docs/superpowers/specs/2026-07-26-memecoin-v6-live-paper-engine-design.md`)

### Why in-process and immediate, not a downstream poller

1. **Latency.** v5 fires at launch age 10–60 s. Any poll interval is a large fraction of that, so a
   polled fill measures the poller.
2. **Retention race.** `op_forward_alerts`, `op_forward_alert_entries`, and
   `op_counterfactual_entries` are all in the retention delete set
   (`sniper/research/memecoin_opportunity/retention.py:238-249`), currently at a 360 s floor under
   storage pressure. A poller can arrive after the row is gone and never know it existed.

### Scheduling

**v1 schedules the quote immediately.** There is no configurable or artificial delay. Introducing
one without a pre-registered value would let the delay be tuned against outcomes after the fact.

What is measured instead is the **actual** queue delay the system produces:
`intent_at → quote_started_at`, plus `quote_started_at → quote_completed_at`. A deliberate delay is
a legitimate future experiment, but it requires a **new `adapter_version`** and its own cohort.

### The quote lane

Radar adapter jobs **reuse the existing `OpportunitySolanaProbe` worker and its pacing clock**
(`solana_probe.py:109`, `min_call_interval_s = 2.0` at `:120`, pacing gate at `:303`), enqueued at
**lower priority** than v5 capture and the existing route probe. No second Jupiter thread, no
separate HTTP client, no independent rate budget.

Consequences, stated rather than tuned away:

- capture is never delayed for an adapter quote;
- work is never silently dropped — a request that ages past the configured expiry without being
  serviced receives the terminal receipt `entry_unfilled:capacity_expired`;
- the probe's existing saturation metrics cover this lane, and its saturation rate is reported.

### Typed terminal reasons

| reason | meaning |
|---|---|
| `capacity_expired` | not serviced before expiry |
| `quote_unavailable` | provider returned nothing |
| `no_route` | provider returned no executable route |
| `malformed_quote` | response missing `outAmount` / `otherAmountThreshold` |
| `stale_quote` | quote age exceeded the configured bound at receipt time |
| `identity_invalid` | mint/network identity failed validation |
| `consumer_unavailable` | V6 consumer heartbeat not fresh at request time |

Unfilled is evidence, not an error. The rate and reason mix are a primary output of this work.

### Clip scope

`$25` only, matching `PRIMARY_CLIP_USD_MICROS` (`engine.py:78`). The local ladder quotes
`$5/$10/$25/$50` (`engine.py:81`) but only `$25` carries outcome evidence. A `$50` claim requires
its own cohort and its own proof; the ladder is not it.

---

## Schema: two immutable tables

<!-- schema:external=op_paper_fill_requests -->
<!-- schema:external=op_paper_fills -->

Neither table is ever updated. A request is written once; a receipt is appended once.
`pending` and `expired` are **derived** states (request with no receipt; request with no receipt
past expiry), never stored values that something mutates.

Both tables are fully denormalized with **no foreign keys to any retention-pruned table**, and both
are held **outside the retention delete set** — the precedent set by `op_creator_outcomes`. A row
must stay readable after `op_forward_alerts`, `op_launches`, and `op_state_snapshots` for that
launch are gone.

```sql
CREATE TABLE IF NOT EXISTS op_paper_fill_requests (
  request_id           TEXT PRIMARY KEY,   -- deterministic: alert_id + adapter_version
                                           --              + fill_model_version + clip
  alert_id             TEXT NOT NULL,
  launch_id            TEXT NOT NULL,
  network_id           TEXT NOT NULL,
  venue_id             TEXT NOT NULL,
  token_id             TEXT NOT NULL,
  creator_id           TEXT,
  launch_mode          TEXT NOT NULL,      -- standard | cashback | mayhem
  state_fingerprint    TEXT NOT NULL,
  rule_version         TEXT NOT NULL,
  evaluator_version    TEXT NOT NULL,
  adapter_version      TEXT NOT NULL,
  fill_model_version   TEXT NOT NULL,      -- route-floor-v1
  clip_usd_micros      INTEGER NOT NULL,
  decision_at          TEXT NOT NULL,
  decision_age_ms      INTEGER NOT NULL,   -- v5's own modeled decision clock
  intent_at            TEXT NOT NULL,
  expires_at           TEXT NOT NULL,
  local_qty_atomic     INTEGER,            -- what local curve math would have filled
  row_fingerprint      TEXT NOT NULL,
  inserted_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS op_paper_fills (
  receipt_seq            INTEGER PRIMARY KEY AUTOINCREMENT,  -- the only cursor key
  request_id             TEXT NOT NULL UNIQUE,
  status                 TEXT NOT NULL,     -- filled | entry_unfilled
  unfilled_reason        TEXT,              -- typed; NULL iff filled
  -- economics, complete on both sides of the quote
  quote_provider         TEXT,
  route_binding_json     TEXT,
  quote_in_asset         TEXT,
  quote_in_atomic        INTEGER,
  quote_in_decimals      INTEGER,
  quote_out_asset        TEXT,
  quote_out_decimals     INTEGER,
  expected_out_atomic    INTEGER,           -- Jupiter outAmount; diagnostics only
  floor_out_atomic       INTEGER,           -- Jupiter otherAmountThreshold; scored
  slippage_bps           INTEGER,
  quote_payload_json     TEXT,
  quote_fingerprint      TEXT,
  -- clocks
  quote_started_at       TEXT,
  quote_completed_at     TEXT,
  receipt_at             TEXT NOT NULL,
  queue_delay_ms         INTEGER,           -- intent_at    -> quote_started_at
  quote_latency_ms       INTEGER,           -- quote_started -> quote_completed
  receipt_latency_ms     INTEGER,           -- decision_at  -> receipt_at, end to end
  row_fingerprint        TEXT NOT NULL,
  inserted_at            TEXT NOT NULL
);
```

`request_id` is deterministic, so a retried request cannot create a second row and the
`request_id UNIQUE` constraint on the receipt makes double-receipt structurally impossible.

**Recovery sweep.** On startup and periodically, the Radar worker finds requests past `expires_at`
with no receipt and **appends** `entry_unfilled:capacity_expired`. It never updates the request row.

**`receipt_seq` is AUTOINCREMENT**, so it is backed by `sqlite_sequence`. That counter must never
be reset — doing so would rewind every consumer cursor and re-deliver receipts.

---

## V6 cursor and handoff ownership

### Cursor

- The cursor is **strictly `receipt_seq`** and monotonic. AUTOINCREMENT values may have gaps;
  gaps are irrelevant because consumption is `receipt_seq > cursor`, not `cursor + 1`.
- It **advances across both `filled` and `entry_unfilled`** receipts, so an unfilled row can never
  wedge it.
- **Only `filled` receipts open positions.**
- A receipt whose position already exists is a duplicate and is skipped, not reopened.

### Ownership invariant: the Radar filled receipt is authoritative

The Radar adapter creates the fill *before* V6 sees it, while V6's own path reserves capacity
before quoting (`sniper/persistence/db_memecoin_v6_paper.py:1217`, `claim_admissions`, "atomically
claim oldest ready candidates without exceeding book capacity"). Without an explicit rule, a filled
receipt V6 cannot admit becomes orphan paper inventory.

**Chosen invariant — authoritative fill.** Every fresh Radar `filled` receipt is authoritative:

- it **bypasses the fomo admission cap** — it is a different book and must not compete for fomo
  capacity, nor consume it;
- V6 must consume it **exactly once**;
- a receipt whose handoff arrives too late to be a faithful live position is recorded as
  **`censored_handoff_stale`** — a terminal censored measurement state, **never** a delayed live
  position opened at a stale price.

Rejected alternative: reserve V6 capacity before quoting. It requires reverse coordination from
Radar into V6's ledger, gives Radar a second write surface, and couples capture latency to V6
availability. Rejected unless the authoritative-fill invariant proves unworkable in practice.

### Consumer liveness gate

The Radar quote lane runs **only** when a fresh V6-consumer heartbeat is present. If the consumer
is absent or stale at request time, the adapter records `entry_unfilled:consumer_unavailable`
rather than minting fills nothing will consume. Combined with default-OFF activation, this makes
orphan inventory a reported state rather than a silent accumulation.

Activation is two-sided and ordered. Both defaults use the same state-root-relative heartbeat
path, `data/memecoin_v6_paper.heartbeat`:

1. enable `MEMECOIN_V6_RADAR_CONSUMER_ENABLED=1` while leaving Radar's adapter off;
2. verify the V6 worker publishes a fresh capability heartbeat and can read
   `MEMECOIN_V6_RADAR_DB` read-only;
3. enable `MEMECOIN_OPPORTUNITY_PAPER_ADAPTER=1` and restart Radar;
4. verify request-to-terminal coverage, receipt cursor advance, and exact cohort separation.

The V6 consumer max handoff age is 30 seconds by default. A later receipt is persisted for
provenance, then atomically marked `censored_handoff_stale`; it never enters the live open book.
Neither switch changes funded authority.

### Measurement censoring must be distinguishable from operator action

The pre-adapter `v6_paper_positions.status` allowed only `open`, `closed`, `censored_operator`
(`sniper/persistence/db_memecoin_v6_paper.py:164`, table at `:158`). Migration and handoff censoring **must not
masquerade as operator action**. The status vocabulary gains explicit measurement-censoring values,
at minimum `censored_migration` and `censored_handoff_stale`, so coverage accounting can separate
"a human intervened" from "the measurement could not be completed honestly".

### V6 entry provenance

`v6_paper_entries` (`sniper/persistence/db_memecoin_v6_paper.py:118`) carries no provenance for a
non-fomo source. It gains, as nullable columns so existing rows are untouched:

```sql
ALTER TABLE v6_paper_entries ADD COLUMN source_receipt_id   TEXT;    -- op_paper_fills.request_id
ALTER TABLE v6_paper_entries ADD COLUMN adapter_version     TEXT;
ALTER TABLE v6_paper_entries ADD COLUMN fill_model_version  TEXT;
ALTER TABLE v6_paper_entries ADD COLUMN rule_version        TEXT;
ALTER TABLE v6_paper_entries ADD COLUMN evaluator_version   TEXT;
ALTER TABLE v6_paper_entries ADD COLUMN launch_mode         TEXT;
```

---

## Floor semantics — `route-floor-v1`

A new fill-model version, applied **only** to positions carrying it.

| leg | scored on | retained for diagnostics |
|---|---|---|
| entry quantity | `otherAmountThreshold` | `outAmount` |
| exit proceeds | `otherAmountThreshold` of the fresh sell quote | `outAmount` |

**Both legs use the floor, or neither does.** Mixing an expected-amount entry with a floor exit
manufactures a spread that does not exist. Tests assert the pair together.

`memecoin_v6_paper_quotes.py:584` and `:660` currently normalize `outAmount`. The change is
**versioned, not in place**: existing fomo-source V6 positions keep expected-amount scoring and
their current fill-model label. Silently re-normalizing them would redefine what their accrued
evidence meant — the failure that forced v5 to replace v4 on a fresh clock rather than mutate it.

---

## Cohort separation is structural

Enforced **in queries, not in layout**. Every read producing a performance number filters on the
full discriminator tuple:

- `signal_source = 'radar_v5'`
- `adapter_version`
- `fill_model_version` (`route-floor-v1`)
- `rule_version`, `evaluator_version`
- `launch_mode`
- V6 `policy_hash`

`launch_mode` is in the key because capturing standard/cashback/mayhem together is fine but pooling
them is not — the standard-vs-cashback split is the open question this evidence will eventually be
asked about. A report that renders separate panels but computes a shared aggregate underneath has
pooled them. Tests assert the filter, not the layout.

---

## What this measures — and what it does not

### The composite claim

> This measures the end-to-end `radar_v5` + `route-floor-v1` + V6 candidate pipeline. Terminal
> delta versus Radar v5 is composite and is never attributed solely to fill realism.

That delta contains, at minimum: entry timing, route drift, **V6 exit policy**, exit timing,
censoring, and quote failures. The exit policies are not comparable and are not intended to be:

| | Radar v5 | V6 |
|---|---|---|
| exit rules | one symmetric barrier pair | `lp_loss`, `catastrophe`, `route_loss`, `time_expiry` |
| profit / loss barrier | +1200 bps / −800 bps (`outcomes.py:212`) | policy-driven `DERISK` / `EXIT_FULL` fractions |
| horizon | 60 000 ms (`outcomes.py:212`) | `TIME_EXPIRY_S = 43_200.0` (`memecoin_v6_engine.py:45`) |

That is a **720× horizon difference** before any other term. Any single number differencing the two
books is uninterpretable, and no such number may be published as "the cost of realism".

### Fill cost, measured directly

Fill cost is measured from **paired fields on the same quote**, where nothing else varies. No
shadow arm, no second cohort:

| comparison | fields | isolates |
|---|---|---|
| slippage floor cost | `expected_out_atomic` vs `floor_out_atomic` | conservatism of scoring at the threshold |
| model-vs-provider cost | `local_qty_atomic` vs `expected_out_atomic` / `floor_out_atomic` | local curve math vs a real route at the same decision |
| exit floor cost | expected vs floor proceeds at each V6 action | conservatism on the exit leg |

Each is a difference between two numbers produced by one quote at one instant. These are the only
fill-cost claims this design supports.

### Cohort size

The realistic cohort **starts at zero**. The existing 59 v5 matched pairs remain valid *modeled*
evidence and are not retro-fitted.

Expect the cohort to be **smaller** than v5's at equal wall-clock, because fresh quotes fail where
local math always succeeds. That shrinkage is a finding to report, not a defect to tune away.

---

## Operator view: current arm only

The Real-tab V6 cockpit is not a history browser. It shows only open positions whose entry matches
the current `radar_v5` / `radar-paper-adapter-v1` / `route-floor-v1` / v5 / evaluator-v4 contract
and the current V6 policy hash. Legacy Fomo rows, older adapter/fill models, and terminal position
cards remain queryable in the research API and ledger but never fill the operator cockpit.

The header names the exact `$25` route-floor arm. Its state is `NOT RUNNING`, `STALE`, or
`COLLECTING` from the three activation flags plus the capability-bearing V6 consumer heartbeat.
Request, pending, filled, and typed-unfilled counts come read-only from the immutable Radar receipt
ledger. If the current arm has no data, the UI renders an honest empty state; it never substitutes
an older cohort.

---

## Scope guards

- No arm created, no rule changed, no scoring formula altered.
- No funded authority, no signer, no broadcast, no reservation rows.
- Radar writes only the Radar database and never imports V6 services.
- `$25` only.
- V6 exit policy untouched.
- Default OFF.

## Rejected alternatives

| alternative | why rejected |
|---|---|
| Downstream poller | Poll latency is a large fraction of a 60 s decision window, and retention prunes the source rows in ~360 s. |
| Replace v5's scoring with quote-modeled fills | Resets 59 pairs of accrued evidence and destroys the paired within-quote fill-cost measurement. |
| Radar writes into the V6 database | Breaks Radar's single-writer isolation and gives it a second write surface. |
| Reserve V6 capacity before quoting | Reverse coordination, Radar writes into V6, and capture latency becomes coupled to V6 availability. |
| Mutable `pending → terminal` row | Not immutable, and gives the cursor no monotonic key. |
| Configurable entry delay in v1 | An unregistered delay is a knob that can be tuned against outcomes after the fact. |
| Create the standard-only arm first | Its value is a fresh prospective cohort; starting that clock before quote-modeled fills exist means changing fills mid-cohort. |

## Open risks

1. **Probe contention** — the adapter shares `OpportunitySolanaProbe`. Lower priority and the
   existing pacing clock bound it; the probe's saturation rate must be reported.
2. **Derived-state accounting** — `pending` and `expired` are derived, so coverage must compute
   them from requests-without-receipts or the denominator silently narrows.
3. **Retention-linked schema** — if either table is ever added to the delete set the handoff loses
   rows silently. A test asserts their exclusion.
4. **Cohort pooling** — any aggregate that forgets one discriminator silently mixes books.
5. **Expected/floor semantic drift** — scoring `outAmount` on one leg and the floor on the other
   invents a spread. Both legs are asserted together.
6. **Orphan inventory** — mitigated by the authoritative-fill invariant, the consumer heartbeat
   gate, and default-OFF, but the `consumer_unavailable` and `censored_handoff_stale` counts must
   be reported, not swallowed.
7. **`sqlite_sequence` reset** — would rewind every consumer cursor and re-deliver receipts.

## Verification

```bash
python3 -m pytest tests/ -q -k "opportunity or v6_paper"
python3 scripts/validate_schema_refs.py
python3 scripts/brain_index.py generate && python3 scripts/brain_index.py schema op_paper_fills
```

Live checks after deploy:

- **every request gains exactly one terminal receipt** (no request without a receipt past expiry;
  no `request_id` with two receipts);
- unfilled reason mix is reported, including `consumer_unavailable`;
- V6 opens positions only on `filled` receipts, exactly once each;
- `censored_migration` and `censored_handoff_stale` never appear as `censored_operator`;
- radar-sourced and fomo-sourced V6 aggregates never share a row set.
