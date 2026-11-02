# Desk cohort read plumbing — F, SJ, SR per cohort (Gamma, DRAFT, 2026-10-21)

Status: DRAFT; checked against desk step 1 `abd311423` / spec rev 6 `811180f83`. It is written against Beta's desk plumbing spec
(`docs/superpowers/specs/2026-10-21-desk-execution-plumbing-design.md` on
`beta/desk-execution-plumbing`, rev 5 `0bff99539`). It will be re-checked when Beta posts the
step-1 SHA.

Read contract: `docs/audits/2026-10-21-desk-opportunity-map.md` §2, §8–§11.

## 0. What the step-1 schema must carry (gaps in rev 5)

The read cannot be computed from rev 5 alone. Four things are missing. Each is additive and
cheap if it goes in at step 1, and expensive to backfill later.

1. **Cohort on the ticket.** Add `desk_tickets.producer TEXT NOT NULL CHECK (producer IN
   ('H','U'))`. Rev 5 dedupes both producers into one open ticket per `(network_id, mint)`
   (§2.0). The read needs the producer that **opened** the ticket. If both producers see the coin,
   the opener is the cohort and the other producer's sighting is logged in `inputs_json` only. The
   cohorts are never pooled (map §10).
2. **Decision time = JUDGE response time.** `desk_tickets.ts` is the row write time. The read
   needs the instant the entry seat's answer was received (map §9). Add
   `response_at TEXT` on model rows. This is not `ts − latency_ms`, which mixes clocks.
3. **Shadow records SJ and SR.** Rev 5 has no shadow tables. Add `desk_shadow_fills` (§1). It
   is written by a desk-side shadow worker that never touches the ledger and never sits on the fill
   path.
4. **Eligible-set snapshot for SR.** SR draws uniformly from the cohort's eligible set at the
   response instant (map §9). Add `desk_eligible_snapshots` (§1). Without it the draw cannot be
   re-derived, and a disputed draw cannot be audited.

The F link already exists in rev 5:
- the ENTER intent carries `origin_event_id='desk:<ticket_id>'`;
- positions carry `signal_source='desk'`, not a desk strategy name (Beta, step 1 `abd311423`: `real_exit_sweep` selects by `strategy == MEMECOIN_REAL_POLICY`, so a desk strategy would get no exits);
- the fill stage row records `position_id` (§2.4 step 6).

## 1. Proposed tables (desk DB, not `sniper_tracker.db`)

<!-- schema:external=desk_shadow_fills -->
<!-- schema:external=desk_eligible_snapshots -->
<!-- schema:external=desk_tickets -->
<!-- schema:external=desk -->
```sql
CREATE TABLE IF NOT EXISTS desk_eligible_snapshots (
    snapshot_id TEXT PRIMARY KEY,        -- sha256(producer || response_at || sorted mints)
    producer TEXT NOT NULL CHECK (producer IN ('H','U')),
    response_at TEXT NOT NULL,           -- the JUDGE response instant this set is "as of"
    mints_json TEXT NOT NULL,            -- sorted [(network_id, mint, pool)] that passed hard_filter
                                         --   and had an open ticket at response_at
    n INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS desk_shadow_fills (
    id INTEGER PRIMARY KEY,
    record TEXT NOT NULL CHECK (record IN ('SJ','SR')),
    producer TEXT NOT NULL CHECK (producer IN ('H','U')),
    ticket_id TEXT NOT NULL,             -- the desk ticket whose ENTER/WAIT-fired decision this mirrors
    attempt_key TEXT NOT NULL,           -- 'desk:<ticket_id>'; SR draw = sha256(attempt_key)
    snapshot_id TEXT,                    -- SR only: desk_eligible_snapshots row it drew from
    network_id TEXT NOT NULL,
    mint TEXT NOT NULL,                  -- SJ: JUDGE's coin; SR: drawn coin
    pool TEXT,
    response_at TEXT NOT NULL,           -- shadow clock start (map §9)
    rails_json TEXT NOT NULL,            -- SJ: JUDGE rails; SR: drawn from the same menu bounds
    clip_usd REAL NOT NULL,              -- 25 x clip_scale (SJ) / 25 x drawn scale (SR)
    entry_quote_id TEXT, entry_quote_at TEXT,
    entry_price REAL,                    -- exact-route buy at response_at + measured median latency
    entry_latency_ms_applied REAL,
    exit_reason TEXT, exit_quote_id TEXT, exit_quote_at TEXT, exit_price REAL,
    fees_usd_modeled REAL,               -- network + priority, same model both records
    return_pct REAL,                     -- (exit_value - clip - fees)/clip*100; NULL until closed
    status TEXT NOT NULL CHECK (status IN ('open','closed','censored','skipped_full')),
    censor_reason TEXT,                  -- missing exact quote, route gone, display-only price
    inserted_at TEXT NOT NULL, closed_at TEXT,
    UNIQUE (record, attempt_key)
);
```

Rules:
- **Exact-route quotes only.** A missing quote sets `status='censored'`; the row never
  substitutes a display price.
- **SR's book is its own.** If SR's 5 slots are full at `response_at`, it writes
  `status='skipped_full'` and makes no draw.
- **SJ's book** mirrors F's admission. An SJ row exists for every F ENTER, including F entries the
  runner refused. The refusal is recorded in F's fill row and does not stop SJ.
- **Fail-closed JUDGE calls write nothing.** A JUDGE call that times out or errors writes no SJ
  and no SR row (map §9).

## 2. Read views

### 2.1 F (funded JUDGE)

<!-- schema:external=desk_tickets -->
<!-- schema:external=desk -->
```sql
-- attach: ledger = sniper_tracker.db (read-only), desk = desk DB
CREATE TEMP VIEW v_desk_F AS
SELECT t.producer,
       p.id            AS position_id,
       substr(p.origin_event_id, 6) AS ticket_id,
       p.opened_at, p.closed_at, p.exit_reason,
       p.entry_notional,
       p.realized_pnl,
       100.0 * p.realized_pnl / p.entry_notional AS return_pct
FROM memecoin_positions p
JOIN desk.desk_tickets t
  ON t.ticket_id = substr(p.origin_event_id, 6) AND t.stage = 'fill'
WHERE p.execution_mode = 'real'
  AND p.signal_source = 'desk'          -- NOT strategy: real_exit_sweep keys on MEMECOIN_REAL_POLICY (Beta, step 1 abd311423)
  AND p.is_open = 0
  AND p.entry_notional > 0
  AND p.origin_event_id LIKE 'desk:%';
```

Exclusions (map §2): operator-modified rows, meaning any non-desk exit override on the position,
are dropped, and so is any position whose policy changed mid-hold. The read script applies these
through `memecoin_position_exit_overrides` before computing, and lists the dropped rows.

### 2.2 SJ and SR

```sql
CREATE TEMP VIEW v_desk_SJ AS
SELECT producer, ticket_id, mint, response_at, closed_at, return_pct
FROM desk.desk_shadow_fills WHERE record = 'SJ' AND status = 'closed';

CREATE TEMP VIEW v_desk_SR AS
SELECT producer, ticket_id, mint, response_at, closed_at, return_pct
FROM desk.desk_shadow_fills WHERE record = 'SR' AND status = 'closed';
```

Censored and `skipped_full` rows are counted and reported, never dropped silently:
`SELECT record, producer, status, count(*) FROM desk.desk_shadow_fills GROUP BY 1,2,3`.

### 2.3 Model cost and latency per cohort

```sql
CREATE TEMP VIEW v_desk_model_calls AS
SELECT producer, ticket_id, stage, seat, model, latency_ms, cost_usd, ts, response_at,
       (output_json IS NULL) AS failed
FROM desk.desk_tickets WHERE model IS NOT NULL;
```

- Cost per F close = Σ `cost_usd` over **all** model rows of that cohort in the read window, on
  every ticket including SKIPs, ÷ F closes in the window.
- `cost_usd IS NULL` counts as **unknown**, is reported, and blocks a PASS (pass rule 4 needs
  the cost).
- Latency p50/p90 is computed per seat in the script.

### 2.4 Execution error (paired)

```sql
CREATE TEMP VIEW v_desk_exec_error AS
SELECT f.producer, f.ticket_id, f.return_pct - j.return_pct AS exec_error_pts
FROM v_desk_F f JOIN v_desk_SJ j ON j.ticket_id = f.ticket_id AND j.producer = f.producer;
```

## 3. Read script (to write once step 1 lands)

`scripts/research/desk_cohort_read.py --producer {H,U} --at <read-1|read-2|final>`:
1. Open both DBs read-only (`mode=ro`).
2. Build the views above.
3. Apply the §2 exclusions.
4. Compute, per cohort:
   - n F, SJ and SR;
   - selection value: two-sample bootstrap, 10,000 resamples, seed frozen in the map §2 block;
   - lower bound LB90, drop-best, F live net after fees and model cost, execution-error median;
   - latency p50/p90 and censor counts.
5. Print PASS / FAIL / INCONCLUSIVE / UNDERPOWERED against map §8–§11.

It writes nothing. Its one runnable check is `tests/test_desk_cohort_read.py`, a synthetic
fixture that covers four cases:
- a desk arm +20 pts vs random, which must PASS;
- a 0-pt arm, which must be INCONCLUSIVE;
- a censored-only arm, which must refuse;
- a mixed-cohort input, which must refuse to pool.

## 4. Edge cases

- **Money path.** None. Every read is read-only. The shadow worker writes only the desk DB and
  must stay off the fill path; its latency is never added to F.
- **Clocks.** `response_at` and `entry_quote_at` are host UTC from `datetime.now(timezone.utc)`.
  The J7-corrected clock is never mixed in (finding: the corrected clock carries J7 transit).
- **Absent vs zero.** `return_pct` NULL means open or censored, never 0. `cost_usd` NULL means
  unknown, never 0.
- **Identity.** Joins use `(network_id, mint)` and the `desk:<ticket_id>` key, never mint alone.
- **Sample.** The read counts only rows produced by the desk's own decisions. A count of F rows
  measures the desk, which is the intent.
- **Rollback.** Drop the views. The tables are append-only and harmless if left.

## 5. Amendment — split every read on `gate_state` (Alpha, 2026-10-21)

The activity gate (`swaps_since_source_5s <= 1` gives SKIP, from
`docs/audits/2026-10-21-activity-feature-study.md`) was measured on **armed-wallet** source buys.
**It is not validated for cohort H,** whose candidates are buys by **unarmed** roster wallets.
The armed-to-unarmed transport gap is **unmeasured** (consistent with Lambda `9446f9736`).
(Corrected 2026-10-21; the earlier text said "validated for cohort H only".) Cohort U has no
validated anchor either (`docs/audits/2026-10-21-cohort-u-anchor-study.md`). Until an H-sample read
exists, the gate is a **hypothesis carried in the fact sheet**, and H rows record `gate_state` for
a later stratified read.

The values are a closed set:
- `pass`: the gate was computed and said KEEP;
- `skip`: it was computed and said SKIP, so no F entry follows; SJ/SR shadow rows still record the
  counterfactual;
- `null`: it was not computed or not applicable. **Every U row is `null`** until a U anchor
  passes a freeze.

**Step-3 schema need:** `desk_tickets.gate_state TEXT CHECK (gate_state IN ('pass','skip')) NULL`
on the hard-filter row, plus `gate_inputs_json` carrying `swaps_since_source_5s` and its anchor.

<!-- schema:external=desk_tickets -->
<!-- schema:external=desk_shadow_fills -->
<!-- schema:external=desk -->
```sql
-- every §2 view gains the ticket's gate_state; every report groups by (producer, gate_state)
CREATE TEMP VIEW v_desk_gate AS
SELECT ticket_id, producer, COALESCE(gate_state, 'null') AS gate_state
FROM desk.desk_tickets WHERE stage = 'hard_filter';

CREATE TEMP VIEW v_desk_F_g AS
SELECT f.*, g.gate_state FROM v_desk_F f JOIN v_desk_gate g USING (ticket_id);
CREATE TEMP VIEW v_desk_SJ_g AS
SELECT s.*, g.gate_state FROM v_desk_SJ s JOIN v_desk_gate g USING (ticket_id);
CREATE TEMP VIEW v_desk_SR_g AS
SELECT s.*, g.gate_state FROM v_desk_SR s JOIN v_desk_gate g USING (ticket_id);
```

Read rules:
- **Pass/fail is computed per (producer, gate_state).** The `skip` stratum has no F rows by
  construction; its SJ-vs-SR comparison measures what the gate gave up.
- **Strata are never pooled.** Cohorts were already never pooled; within a cohort, `null` rows
  are reported apart and never folded into `pass`.
- **SR draws within the same gate_state** as the desk decision it mirrors. Otherwise random would
  include coins the gate removed, and selection value would credit the gate to JUDGE.

## 6. Amendment — current read contract is opportunity map §12 (Codex findings, 2026-10-21)

> **Superseded by §7 and opportunity map §13.** Do not use this section's response_at overwrite for WAIT, its lower-minus-lower missing bounds, or its nested / wider-of-two bootstrap. Those are replaced by `trigger_at` plus `wait_state`, by INCONCLUSIVE on any SR row without a valid mark, and by the crossed product-weight bootstrap. It is kept as history.

The views above follow `docs/audits/2026-10-21-desk-opportunity-map.md` §12. Changes to the
tables and views:

- **SR rows** (`desk_shadow_fills.record='SR'`) are created per **hard-filter-passing ticket**,
  selected by sha256(`ticket_id`) / 2^256 < p (p frozen per cohort). They are **not** tied to an
  ENTER/WAIT decision. `attempt_key` becomes `desk:<ticket_id>` for SR as well, and SR no longer
  needs an `snapshot_id` draw. `desk_eligible_snapshots` stays as an audit of the eligible set.
- **SJ for WAIT:** `response_at` holds the **trigger** time of a fired WAIT. An expired WAIT writes
  no SJ row; it is counted as an abstention from `desk_tickets` (verdict WAIT, no fill-stage
  `position_id`).
- **Whole-book cutoff:** the read script takes `--cutoff <utc>` and marks open F, SJ and SR rows at
  an exact-route sell quote at the cutoff. It reports a lower bound (missing = −100 %) and an upper
  bound (missing = best observed exact value). PASS needs the lower-bound book.
- **Intervals:** cluster bootstrap by coin (wallet nested), plus by UTC day; the wider is reported.
- **Looks:** read 1 and read 2 output only KILL / CONTINUE; PASS / FAIL only at the final read.

## 7. Amendment — schema and view changes from opportunity map §13

`desk_shadow_fills` changes:
- `response_at` stays immutable.
- **Add** `trigger_at TEXT` and `wait_state TEXT CHECK (wait_state IN ('expired', 'triggered_refused',
  'triggered_failed', 'triggered_submitted_unknown', 'triggered_filled'))`.
- **Add** `hash_domain TEXT NOT NULL DEFAULT 'desk-sr-v1'` and `u_json TEXT`, holding the per-domain
  draws (select, delay, tp, sl, trail, derisk) so a reviewer can re-derive every SR draw.
- **Add** `value_floor_usd REAL` (no cap column: an uncapped return has no finite upper bound),
  `valuation_requested_at`, `valuation_completed_at`, `valuation_status TEXT CHECK
  (valuation_status IN ('ok', 'censored_unavailable', 'censored_window'))`.

<!-- schema:external=desk_candidate_emissions -->
<!-- schema:external=desk_arm_admissions -->
New tables:
- `desk_candidate_emissions` (ticket_id, producer, gate_state, emitted_at), written **before** any
  arm decides.
- `desk_arm_admissions` (ticket_id, arm IN ('F', 'SJ', 'SR'), decision IN ('taken', 'skipped_full',
  'latched', 'halted', 'not_selected'), decided_at).

Read script:
- Returns INCONCLUSIVE if any SR row in the read has no valid in-window mark (uncapped estimand,
  opportunity map §13.1). Otherwise it computes the observed book and the floor book.
- Uses the product-weight multiway bootstrap with shared weights across arms. A positive result is
  labelled `exploratory_positive` (never PASS) until the pre-declared coverage simulation passes.
- The valuation window is [T, T + 5 s]; each mark is valued at its own `valuation_completed_at`.
- p_H = 0.10 is a constant in the read's freeze block; p_U is unset until pre-registered.
