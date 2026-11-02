# Design: persist Jupiter load by host and role

Date: 2026-08-30. Author: Gamma. **PROPOSAL — not shipped.** Touches the funded quote path, so
Protocol C review before any code lands.

## Problem, stated as the thing we cannot currently answer

`JUPITER_LOAD` (`solana_real_seams.py:471`) counts `quotes`, `rate_limited`, `errors`,
`auth_failed`, `by_host` and `by_role` **in process memory only**. The runner restarted twice on
2026-08-30, so the funded Jupiter 429 rate for today is unrecoverable.

That makes the following unanswerable after any restart:

- What is the funded 429 rate right now, and is it rising?
- Did adding the funded key help? (`solana_real_seams.py:61` already records this gap: "JUPITER
  counts 429s but has never recorded WHICH")
- Is Alpha's 6x headroom real? Their figure divides a documented 10 req/s limit by a measured
  1.4-1.6 req/s demand. **It is arithmetic, not an observation of absent throttling**, and it
  cannot be observed until this exists.
- Did a capture or paper lane cause a funded incident? The 08-29 attribution took a
  cross-reference between the runner log, the arms config and a study's position count.

The counters are otherwise well built — one seam, fail-soft, role-aware. **Nothing about the
counting logic changes.** This adds a durable sink behind it.

## Non-goals

- No change to any quote outcome, retry, host failover, or gating. The instrument is READ-ONLY
  with respect to funded decisions.
- No new provider calls. It records calls we already make.
- No per-event row. See "granularity".

## Granularity: per-minute buckets, not per-event rows

Funded demand is 1.4-1.6 req/s, so per-event rows would be ~130k/day. Every question above is a
RATE question, and the reference incident (2026-08-27) was "one 36-minute window at ~1.0 429/s" —
a per-minute bucket resolves that comfortably.

Per-minute aggregation by `(role, host)` is roughly 4 rows/minute worst case, under 6k rows/day,
and reuses the `ts_minute` convention already used by `memecoin_quote_ledger`.

## Schema

<!-- schema:external=memecoin_jupiter_load -->
```sql
CREATE TABLE IF NOT EXISTS memecoin_jupiter_load (
  ts_minute     TEXT NOT NULL,     -- UTC minute bucket, 'YYYY-MM-DDTHH:MM'
  role          TEXT NOT NULL,     -- funded | paper | capture | unknown
  host          TEXT NOT NULL,     -- 'api' | 'lite-api' | 'unknown' (label only, never a URL)
  quotes        INTEGER NOT NULL DEFAULT 0,
  rate_limited  INTEGER NOT NULL DEFAULT 0,
  errors        INTEGER NOT NULL DEFAULT 0,
  auth_failed   INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (ts_minute, role, host)
);
```

`host` uses the existing `_host_label()` helper, which deliberately returns `api` / `lite-api`
rather than a URL "so a base that ever carries a secret keeps it that way". Preserve that.

`auth_failed` stays a SEPARATE column and is never folded into `rate_limited`. The existing
`_note_host_auth_failure` docstring is explicit about why: a 429 says we are asking too much, a
401 says the key is revoked, and conflating them reads as pressure when the fix is a new key.

## Mechanism, mirroring `memecoin_route_series`

That module is the accepted precedent in this codebase for "record something from a hot path
without touching it", and this should not invent a second pattern.

1. **A per-minute accumulator beside `JUPITER_LOAD`**, not a diff of it. `JUPITER_LOAD` is
   cumulative-since-process-start; differencing it produces a NEGATIVE delta across a restart,
   which is exactly the failure this work exists to remove. A separate bucket dict keyed
   `(ts_minute, role, host)` is drained on flush.
2. **The hot path only increments a dict under a short lock.** No I/O, no sleep, no DB handle on
   the quote path. This preserves the funded-first asymmetry at `solana_real_seams.py:477` — a
   funded preview must never queue behind bookkeeping.
3. **Flush from the MAINTENANCE pass**, never the quote path or the exit loop, using the
   maintenance connection, exactly as `memecoin_route_series.flush` does.
4. **A failed write drops that batch** rather than retrying against a broken table and growing
   without bound. Losing an observation is acceptable; blocking a funded quote is not.
5. **Every increment and flush is wrapped**, matching the existing "a counter must never be able
   to refuse a funded quote" contract at all three current seams.

## Call sites — additive only, no logic moved

| seam | today | added |
|---|---|---|
| `_note_host_429(base)` | `JUPITER_LOAD["by_host"][label] += 1` | bucket `(minute, role, label).rate_limited += 1` |
| `_note_host_auth_failure(base, status)` | `auth_failed += 1` | bucket `.auth_failed += 1` |
| `note_role_quote(role)` | `by_role[role].quotes += 1` | bucket `.quotes += 1` |
| `note_role_rate_limited(role)` | `by_role[role].rate_limited += 1` | (same event as `_note_host_429`; see below) |

**One correctness detail that must not be got wrong.** `_note_host_429` and
`note_role_rate_limited` fire for the SAME network event on the funded path, and the paper path at
`memecoin_v6_paper_quotes.py:169` calls `_note_role_rate_limited` then CONTINUES to the next host.
So a single logical rate-limit can produce several role increments across a failover, while the
durable row must not double count. **Record the bucket increment at exactly ONE seam per event**
— `_note_host_429`, which already receives the host — and pass the role through to it rather than
incrementing at both. Otherwise the persisted rate exceeds the true one by the failover fan-out,
which is the same shape as the counter-versus-table discrepancy already recorded in
`feedback-check-the-instrument-not-the-arithmetic-2026-08-24` (a recovered 429 is +1 on the
counter and a priced row in the table).

## Verification before it is trusted

1. A test that the bucket is incremented exactly once per simulated 429 across a two-host
   failover — the double-count above is the defect most likely to ship.
2. A test that a raising write function cannot propagate out of the flush, and that a raising
   bucket increment cannot propagate out of a quote.
3. Mutation check both directions, per standing practice: break the once-per-event property and
   the test must go red.
4. After one day live, reconcile the persisted per-minute totals against `JUPITER_LOAD`'s
   in-process cumulative for an interval with no restart. They must agree. If they do not, the
   table is wrong and the in-process counter is the reference, not the other way round.

## What this settles once it runs

It converts Alpha's "~6x headroom" from arithmetic into a measurement, in whichever direction the
data goes. It also makes the next capture-lane proposal decidable: `solana_real_seams.py:465`
records that a proposed capture worker was estimated at ~2.5x funded's average and ~7x its burst,
and that no second consumer is enabled "until funded's own load is a measured number rather than
an estimate". This is that number.

## Open question for the reviewer

Whether to also record the `paper`/`capture` roles from the start. They share one account and one
byte-identical key, so their combined draw is what matters to that account, not the split. I
propose recording the role label anyway — it costs one column already in the primary key, and the
08-29 incident showed the attribution question arrives after the fact, when re-deriving it is
expensive.

---

# Amendment 1 — two quantities, not one; and the reconciliation rule, written before it runs

Appended 2026-08-30 after Alpha's approval and BEFORE any code, per their two additions.

## A1.1 Schema gate (run first, as instructed)

`python3 scripts/brain_index.py schema memecoin_jupiter_load` -> not found. Adjacent candidates
checked rather than assumed: `memecoin_quote_ledger` (successful quotes only, no failure column),
`memecoin_quote_sweeps_v2` / `memecoin_quote_observations_v2` (sweep observations), and
`v6_paper_entry_quote_attempts` — the closest-sounding one. That last is declared in the registry
but **absent from the live DB**, and is keyed `(origin_event_id, policy_id='v6', attempt_no)`:
per-attempt, paper-scoped, answering "did this v6 admission attempt get a quote". It does not
answer "what is the funded 429 rate over time". No duplication.

## A1.2 THE SPEC WAS WRONG: the two seams measure DIFFERENT EVENTS by design

The main body said "record the bucket increment at exactly ONE seam per event —
`_note_host_429`". Reading the call sites before writing SQL shows that is incorrect and would
have built an instrument that disagrees with its own reference every day.

- **`_note_host_429(base)` — `solana_real_seams.py:298`** — fires INSIDE the host failover loop,
  once **per host attempt**. Two hosts 429 on one quote -> two increments. It feeds
  `JUPITER_LOAD["by_host"]`.
- **`JUPITER_LOAD["rate_limited"] += 1` — `solana_real_seams.py:637`** — fires at the OUTER level,
  once **per quote**, and only when the FINAL status is 429, i.e. every host failed.

So `by_host` counts PRESSURE (host attempts refused) and `rate_limited` counts LOST DECISIONS
(quotes that ended 429 after failover). A quote that 429s on `api` and succeeds on `lite-api` is
+1 pressure and +0 lost decision. `by_host` sum >= `rate_limited`, always, by construction.

This is the distinction already registered in `ac1cc837` ("the bound is on LOST DECISIONS, not on
429s") and in `feedback-check-the-instrument-not-the-arithmetic-2026-08-24`. It appears here
inverted, and I nearly rebuilt the same error.

**Corrected schema — both quantities, separate columns:**

<!-- schema:external=memecoin_jupiter_load -->
```sql
CREATE TABLE IF NOT EXISTS memecoin_jupiter_load (
  ts_minute     TEXT NOT NULL,
  role          TEXT NOT NULL,
  host          TEXT NOT NULL,
  quotes        INTEGER NOT NULL DEFAULT 0,   -- per quote,        seam note_role_quote
  host_429      INTEGER NOT NULL DEFAULT 0,   -- per HOST ATTEMPT, seam _note_host_429      (pressure)
  rate_limited  INTEGER NOT NULL DEFAULT 0,   -- per QUOTE,        seam line 637            (lost decision)
  errors        INTEGER NOT NULL DEFAULT 0,
  auth_failed   INTEGER NOT NULL DEFAULT 0,   -- per HOST ATTEMPT, seam _note_host_auth_failure
  PRIMARY KEY (ts_minute, role, host)
);
```

`host_429` and `auth_failed` are host-attempt scoped and always carry a real host label.
`rate_limited` and `quotes` are quote scoped; the terminal seam knows the role but not a single
host, so those rows carry `host='_quote'` rather than inventing an attribution. **Never sum
`host_429` and `rate_limited` together**; they are different denominators.

## A1.3 Reconciliation: tolerance and disposition, fixed in advance

Stated now, before the number is seen, so the repair is auditable rather than fitted.

**Comparison** — over a window with NO process restart, using CLOSED minutes only (the current
partial minute is excluded; a quote in flight across the boundary is not a discrepancy):

| persisted | reference | expected |
|---|---|---|
| `SUM(host_429)` | `sum(JUPITER_LOAD["by_host"].values())` excluding `:auth` keys | **exact equality** |
| `SUM(rate_limited)` | `JUPITER_LOAD["rate_limited"]` | **exact equality** |
| `SUM(quotes)` | `JUPITER_LOAD["quotes"]` | **exact equality** |
| `SUM(auth_failed)` | `JUPITER_LOAD["auth_failed"]` | **exact equality** |

**Tolerance is ZERO, and that is deliberate.** Each bucket increment happens in the same `try`
block, at the same instant, as the counter increment it mirrors. There is no sampling, no clock
skew and no network between them, so any nonzero difference is a defect and not noise. A
tolerance band here would only hide the fan-out double-count this amendment exists to prevent.

**Disposition, pre-committed:**

| observation | ruling |
|---|---|
| persisted < reference | the flush dropped a batch. Check `COUNTS["errors"]` and `dropped_full`. **Table wrong, counter is reference.** |
| persisted > reference | double count, almost certainly failover fan-out recorded at the wrong seam. **Table wrong.** |
| exact match over one restart-free day | table becomes usable for rate claims |
| any mismatch | **the table does not become authoritative**, and no rate claim is published from it until it reconciles |

The known counter-versus-table gap Alpha warned about is handled STRUCTURALLY rather than by a
tolerance: `host_429` and `rate_limited` are separate columns each reconciled against their own
counter, so the by-design difference between pressure and lost decisions can never present as a
discrepancy.

**Until the first clean reconciliation, the 6x headroom figure stays labelled arithmetic.**
