# Forward-Tracking Job (D4) — Spec / Pre-registration (rev 2 — methodology-patched, DRAFT)

**Status:** **BUILT** (pure core + store methods + 16 acceptance tests; live store round-trip
verified). The real forward-window provider (data-ops) is still pending — D4 produces no real
verdicts until it exists. No new card logic, no batch runner. Cross-ref: the enrollment contract
`2026-05-30-forward-paper-enrollment-contract.md` (D4) and the thesis-extractor spec
`2026-05-30-thesis-extractor-multi-lens.md` (the forward router is "the gate that makes any card
real" — D4 is the gate's verdict step). Implementation: `forward_tracking.py` (`evaluate` pure
core + `run_forward_tracking` orchestrator); store `iter_active`/`update_tracking` on both
in-memory and Supabase stores. Defaults chosen: `max_tracking_days=90`, `decay_fraction=0.5`.

**rev 2 (research-methodologist review applied — 3 serious + 3 minor):** (S1) promotion now
requires **two-pass sustained confirmation** on non-overlapping forward batches — the
forward-stage multiplicity control the doctrine assumed but the rev-1 single-pass gate lacked.
(S2) promotion requires the forward effect to retain a pre-registered fraction of the
**enrolled effect** (decay gate) — `effect ≥ 0` was too weak; needs a new `enrolled_effect`
column. (S3) early kill now requires `n ≥ min_forward_n AND effect ≤ kill_threshold` — the
rev-1 bare-`contradicted` kill fired on tiny-n early noise (sequential peeking). Minors:
non-trivial success floors, survivor-bias documentation, freeze-boundary + negativeRisk
handling in the forward window.

**Scope (exactly the user's list):** read `queued` rows → move eligible to `active` →
for `active` rows compute **only** the stored `forward_test_design.metric` → write
`forward_metric` / `verdict` / `state` / `updated_at` → terminal states only
`completed | killed | promoted` → re-runnable / idempotent → no live orders, no sizing,
no operator selection.

**Non-goals:** no batch runner / candidate feed (separate, later); no new metrics or card
producers; no verdict mutation of analyzer surfaces; no live trading; no sizing output.

---

## 1. The forward test (what "compute the metric" means)

A card was a **retrospective** hypothesis from the wallet's history. The forward test asks
the only honest question: **does the same edge reproduce out-of-sample, after we committed?**

- **PIT anchor = `enrolled_at`.** The forward window uses **only** the wallet's positions
  with `entry_ts > enrolled_at` (strictly post-commitment — no peeking). This is what makes
  D4 a forward test rather than more retrospection.
- **Reuse the producer; do not reinvent.** D4 re-runs the card's existing producer on the
  forward window and reads its `effect` + `evidence_state`. No new card logic. The producer's
  own permutation null **is** the out-of-sample significance test (e.g. forward
  `sizing_card` returns `supported` only if the forward sizing edge clears its permutation on
  the forward sample).

## 2. Metric dispatch (drive off the STORED metric)

D4 dispatches on `forward_test_design.metric` — the value persisted at enrollment — via a
fixed registry:

| `metric` | producer re-run on forward window | effect read |
|---|---|---|
| `calibration_edge` | `sizing_card` | dollar-wtd − equal-wtd calibration |
| `dead_zone_drift_gap` | `timing_card` | dead-hour − live-hour drift |

`metric` not in the registry → **untrackable** (§6): state unchanged, a diagnostic written
to `forward_metric`, **no verdict**. This is the "no silent metric substitution" guard — D4
never computes a different metric than the one stored.

Each run, D4 records into `forward_metric` (jsonb): `metric`, `value` (forward `effect`),
`p_value` (forward producer p), `evidence_state`, `forward_n`, `pass_count`,
`last_pass_window_end`, `status`, `horizon_elapsed`, `computed_at`. The forward `p_value` is
persisted so a future second-stage FDR over promotions remains possible if replication proves
insufficient.

## 3. State machine (legal transitions only)

Reuses `transition_paper_state` (enrollment.py): `queued→active`, `active→{completed,killed}`,
`completed→promoted`; `killed`/`promoted` terminal.

- **`queued → active`** — on first D4 visit, unconditionally. The router only enrolls cards
  with a valid `forward_test_design`, so every queued row is trackable-by-construction;
  activation just marks "now under forward observation" (window already anchored at
  `enrolled_at`). Sets `updated_at`.
- **`active → killed`** — kill threshold decisively crossed (may fire **before** horizon).
- **`active → completed`** — a verdict exists at/after horizon (pass, null, or inconclusive).
- **`completed → promoted`** — pass verdict only. D4 may take `active→completed→promoted` in
  one pass (two legal hops).
- **Terminal rows (`completed`/`killed`/`promoted`) are never re-evaluated.**

## 4. Verdict rules (pre-registered, rev 2)

A **forward batch** = the forward positions in one evaluation slice with `n ≥ mn` that the
producer can score; a "pass" on a batch = `fwd.evidence_state == "supported"` **and**
`fwd.effect ≥ max(s, enrolled_effect × decay_fraction)` (§4a). `pass_count` and the slice
boundary `last_pass_window_end` are tracked in `forward_metric`. Symbols: `fwd =
producer(forward_window)`, `s = success_threshold`, `k = kill_threshold` (negative),
`mn = min_forward_n`, `H` = horizon elapsed (§5), `n = fwd.n`.

Evaluate in order, per active row, each D4 run:

1. **Kill** — `n ≥ mn` **and** `fwd.effect ≤ k` → **killed**, `verdict="killed_below_kill_threshold"`.
   (May fire pre-horizon. **Requires `n ≥ mn`** — no kill on tiny-n early noise [S3]. A bare
   negative point estimate `0 ≥ effect > k` is **not** a kill; it leaves `pass_count=0` and the
   row stays active.)
2. **Pass this batch** — the row meets the batch-pass criteria on a window non-overlapping with
   `last_pass_window_end` → increment `pass_count`, set `last_pass_window_end` to this window's
   end. Then:
   - **`pass_count ≥ 2`** → **promoted** (`active → promoted` atomic), `verdict="promoted_sustained"`.
     (Two non-overlapping passing batches = the replication multiplicity control [S1]; a true-null
     card passes twice on independent samples at ≈ `alpha²`. Promotion does **not** wait for the
     horizon `H` — the two-pass replication is itself the early-luck guard, so a confirmed edge
     promotes as soon as the second pass lands, per the operator's "promotion only after two
     non-overlapping forward batches pass.")
   - else → **stays active** (`forward_metric.status="confirming"`, `pass_count=1`).
3. **Fail a batch after a prior pass** — a non-pass batch resets `pass_count=0` (the edge didn't
   sustain); stays active unless rule 1 fires.
4. **`H` and `n ≥ mn` and not promoted** → **completed**, `verdict="completed_no_edge"`
   (recorded null — neither promoted nor killed).
5. **`H` and `n < mn`** → **completed**, `verdict="completed_inconclusive_insufficient_data"`
   (pre-registered, design-allowed terminal for "ran out the clock without enough data" — an
   explicit verdict, **not** a silent metric).
6. **otherwise** (not `H`, no kill, < 2 passes) → **stays active**, `verdict=NULL`,
   `forward_metric.status ∈ {"awaiting_data","confirming"}`.

Asymmetry (intentional): **kill** requires a decisive negative (`effect ≤ k`, a real negative
bar, not merely `< 0`) at `n ≥ mn` — conservative, avoids over-killing weak-but-real cards on
one unlucky sample. **Promotion** requires two sustained non-overlapping passes **and** the
decay gate — hard to earn, the whole point of a truth gate. `H` gates only the non-promoted
finalizations (rules 4–5), so cards don't sit `active` past the tracking cap.

## 4a. Contract addition needed (S2 — store the enrolled effect)

The decay gate needs the **in-sample** effect magnitude at enrollment. It is **not** stored
today (only the `forward_test_design` dict is). Required additions, landed before D4:

- `paper_card_enrollments.enrolled_effect double precision` (migration `supabase_024`).
- `PaperEnrollment.enrolled_effect: float | None`; the router writes `enrolled_effect =
  card.effect` at enrollment (`enrollment.py` `_log`/`PaperEnrollment` + `enrollment_store._row`).
- `decay_fraction` — pre-registered job param (proposed **0.5**): forward effect must retain
  ≥ half the enrolled effect to promote. A card that decays from a 4pp edge to 0.2pp does **not**
  promote even though it clears `effect ≥ 0`.

## 4b. Survivor-set bias (S5 — document, surface)

The set of cards that ever reach a real verdict (promoted / killed / completed_no_edge) is
selected for wallets with **high forward trade volume**; a wallet who stops trading after
enrollment only ever yields `completed_inconclusive`. The gold board is therefore biased toward
high-forward-volume wallets regardless of forward-test quality. D4 does not "fix" this, but any
gold-board readout **must report the inconclusive rate by wallet forward-volume tier** so the
bias is visible, not silent.

## 5. Horizon-elapsed `H`

- `to_resolution` (calibration_edge): `H` when the forward window's positions have resolved,
  bounded by the job param `max_tracking_days` from `enrolled_at`.
- `next_session` (dead_zone_drift_gap): `H` when each forward entry's next session has passed,
  bounded by `max_tracking_days`.
- Hard cap: once `now ≥ enrolled_at + max_tracking_days`, `H` is true regardless — rows never
  stay `active` forever (rule 3/4 finalize them). `max_tracking_days` is an operator-set,
  pre-registered job parameter (default proposed: 90), **not** a per-card field.

## 6. Hard data-availability guard (the real blocker — explicit)

D4 computes the metric **only** if the required forward data is present; it never substitutes.

| metric | forward data needed | availability | behavior when absent |
|---|---|---|---|
| `calibration_edge` | wallet's post-`enrolled_at` closed positions **that have resolved** (`avgPrice`, resolution) | **available** via a wallet re-fetch (same source the producer used); gated on `n ≥ mn` resolved forward trades | `n < mn` → stays `active` (`awaiting_data`); at horizon → rule 4 inconclusive |
| `dead_zone_drift_gap` | post-entry next-session **price path** for forward entries | **PARTIAL** — needs a forward price feed (price-capture cron / price-history); not guaranteed | no price → producer underpowered → stays `active`; at horizon → rule 4 inconclusive. **No proxy, no substitution.** |
| unknown metric | — | — | **untrackable**: state unchanged, `forward_metric={"status":"untrackable_metric"}`, no verdict |

**No silent failure:** every stall writes a visible `forward_metric.status`
(`awaiting_data` / `confirming` / `untrackable_metric`) so a stuck row is observable, not
invisible. A row is only `completed`/`killed` via a pre-registered rule in §4 — never to
"clear the queue."

**Hedging contamination guard (methodology minor):** the forward window for `calibration_edge`
must **exclude `negativeRisk` markets and net-neutral hedged legs** — they inflate the sizing
denominator without adding conviction (the trap the earlier calibration study hit).
`sizing_card` already excludes entries ≥ 0.80; the forward provider (§7) additionally drops
`negativeRisk` positions before scoring.

## 7. Forward-window provider (injected dependency — the thing to wire)

D4 core is **pure** over a provided window; it does not fetch. Interface:

```
ForwardWindowProvider(wallet: str, since_ts: int, metric: str) -> list[dict] | None
```

returns the metric-appropriate forward position dicts (sizing fields / timing fields), or
`None` when the data source can't answer yet. The **real** provider reuses the existing
wallet fetch (`sniper/analysis/wallet_protocol/fetch.py`) + price sources; it is a data-ops
task and may be wired alongside the batch runner — **not** part of D4's pure core. Tests
inject synthetic windows. This keeps D4 narrow and testable and isolates the data blocker.

Provider invariants (so the forward window matches the in-sample contract):
- **Strict PIT:** include only `entry_ts > since_ts`; exclude positions opened at/before
  `enrolled_at` even if they resolve after.
- **Freeze boundary (methodology minor):** a position whose next-session window crosses the
  data-availability boundary must be returned with `next_session_price=None` — never a stale or
  synthesized truncated price. (Mirrors the in-sample `timing_card` behavior.)
- **Hedging:** drop `negativeRisk` positions from the `calibration_edge` window (§6).

**Build status:** `WalletForwardWindowProvider` (`forward_window_provider.py`) implements
**`calibration_edge`** — re-fetch closed positions, strict-PIT (`timestamp > since`),
resolved-only (curPrice ≥ 0.97 / ≤ 0.03), map to `{entry_price, size, won}` (size = ADR-004
`avgPrice × shares`), exclude negativeRisk. Returns `None` only on fetch failure, `[]` when
nothing qualifies. **`dead_zone_drift_gap` is DEFERRED** (returns `None`) — no reliable
per-position post-entry price feed exists, so D4 stays `awaiting_data` rather than fake a
drift. **negativeRisk enrichment — WIRED:** raw Data-API `/positions` rows carry no negRisk
flag, so `negrisk_lookup.load_negrisk_conditions(condition_ids, fetch_markets=...)` builds the
negRisk condition_id set from the Gamma `/markets` API (`gamma_negrisk_markets`, `closed=true`,
project session) and is fed via `WalletForwardWindowProvider(negrisk_conditions=...)`. Verified
live (SmallFish): 153 unique condition_ids → 26 negRisk → qualifying sizing rows 156 → 132 (24
negRisk positions excluded). Provider behavior unchanged (None on fetch fail, [] on no rows).
The batch runner builds the set per wallet from its forward positions' condition_ids.

## 8. Store extension needed (narrow)

D4 needs an **update** (the enrollment store is insert-only). Add one method to both
`InMemoryPaperEnrollmentStore` and `SupabasePaperEnrollmentStore`:

```
update_tracking(*, key, expected_state, new_state, forward_metric, verdict) -> bool
```

A **conditional** update: `WHERE <idem key> AND state = expected_state` (PostgREST
`?...&state=eq.<expected>`), setting `state`, `forward_metric`, `verdict`, `updated_at`.
Returns whether a row was updated. The `state=expected_state` guard makes concurrent D4 runs
safe (a row already advanced by another run won't be double-advanced) and keeps terminals
immutable. No other columns are writable by D4.

## 9. Invariants (hard)

- Computes **only** `forward_test_design.metric`; unknown → untrackable, never substituted.
- **No new card logic** — reuses the existing producers via the §2 registry.
- No live orders, no sizing output, no operator/wallet/name selection — D4 only walks rows in
  storage order and applies the pre-registered rules.
- Terminals immutable; `forward_metric`/`verdict`/`state`/`updated_at` are the only writes.
- No analyzer-surface verdict mutation.
- Idempotent: same rows + same forward data ⇒ same end state; write only on change.

## 10. Required tests (acceptance criteria for the build)

1. `queued_goes_active_on_first_pass` — a `queued` row → `active`, window anchored at `enrolled_at`.
2. `single_pass_stays_confirming` — one passing batch ⇒ stays `active`, `pass_count=1`,
   `status="confirming"`, **not** promoted.
3. `promote_on_two_sustained_passes` — two non-overlapping passing batches, decay gate met
   ⇒ `promoted` / `promoted_sustained` (no horizon wait) [S1].
4. `no_promote_on_decayed_effect` — forward `supported`, `effect ≥ 0` **but**
   `< enrolled_effect × decay_fraction` ⇒ **not** promoted (stays active / completed_no_edge) [S2].
5. `kill_requires_n_and_negative_threshold` — `n ≥ mn` and `effect ≤ kill` ⇒ `killed`; a
   tiny-n early negative (or `0 ≥ effect > kill`) does **not** kill [S3].
6. `failed_batch_resets_pass_count` — a non-pass batch after a prior pass resets `pass_count=0`.
7. `completed_no_edge_at_horizon` — `H`, `n ≥ mn`, no sustained pass, no kill ⇒ `completed_no_edge`.
8. `inconclusive_when_insufficient_data_at_horizon` — `H`, `n < mn` ⇒
   `completed_inconclusive_insufficient_data` (explicit verdict).
9. `stays_active_while_awaiting_data` — `n < mn`, not `H` ⇒ stays `active`, `awaiting_data`,
   no verdict.
10. `untrackable_metric_does_not_substitute` — unknown metric ⇒ state unchanged, diagnostic
    written, no verdict.
11. `terminals_are_immutable_and_idempotent` — re-running over terminal rows is a no-op; re-run
    with unchanged data produces no state change.
12. `conditional_update_prevents_double_advance` — an update with the wrong `expected_state`
    does not apply (concurrency safety).
13. `no_live_or_sizing_or_operator_selection` — structural: D4 emits no order/size and selects
    no row by wallet/name.
14. `negativeRisk_excluded_from_calibration_window` — the forward provider drops `negativeRisk`
    positions before sizing is scored.

## 11. Open parameters (resolve at build)

- `max_tracking_days` default (proposed 90) — operator-set pre-registration, job-level.
- `decay_fraction` default (proposed 0.5) — forward effect must retain ≥ this share of
  `enrolled_effect` to promote (§4a, S2). Job-level pre-registration.
- **Static success floors (S4 backstop):** raising the producers' `_FORWARD.success_threshold`
  off 0.0 (proposed `sizing_card` ≥ 0.01, `timing_card` ≥ 0.005) is an optional belt-and-suspenders
  alongside the decay gate; decide whether the decay gate alone suffices or both apply.
- **Non-overlapping batch definition** — a forward batch ends at `last_pass_window_end`; the
  next batch uses `entry_ts > last_pass_window_end`. Confirm the slice granularity (per-run vs
  fixed calendar window) at build; it must guarantee independence between the two confirming passes.
- Forward-window provider ownership — D4 ships with the pure core + the injected interface;
  the real provider (wallet re-fetch + price source) is wired separately (likely with the
  batch runner). D4 is not blocked on it for the core + tests, but **produces no real verdicts
  until it exists** — state that plainly, don't hide it.

---

**Cross-ref:** enrollment contract §9 (state machine) + §13 (table). Methodology review:
research-methodologist `needs-revision`/serious, 3 serious + 3 minor, all applied in rev 2
(S1 two-pass replication, S2 enrolled-effect decay gate, S3 `n≥mn` kill guard; minors:
success floors, survivor-bias readout, freeze-boundary + negativeRisk provider rules).

After this spec is approved: land the `enrolled_effect` column (`supabase_024`) + field (§4a);
build D4 pure core + `update_tracking` + the §10 tests against synthetic windows; then wire the
real forward-window provider (data-ops); then the batch runner can run the full conveyor and D4
turns queued hypotheses into verdicts.
