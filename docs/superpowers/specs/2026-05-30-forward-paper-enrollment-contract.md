# Forward-Paper Enrollment Contract (rev 2 — review-patched, foundation + router built)

**Status:** approved direction; review fixes applied; **foundation + router + durable storage
built** (D1 first-class fields, D2 deterministic run id, the admission `explain` helper,
`enrollment.py`, and D3 — `enrollment_store.py` + `supabase_023` table). Router consumes
`profile.decisions` + `profile.eligible`, enrolls to `queued`, persists idempotently to
`public.paper_card_enrollments` (re-run never resets D4-advanced state). §11's 6 required + 2
recommended tests green; store has 5 adapter tests + a live round-trip. **Still open:** D4
forward-tracking job.

**Scope:** how a `ThesisProfile` becomes a set of paper enrollments + an admission log.
**Non-goals:** no live trading, no sizing recommendation, no forward-tracking job, no DDL
(those are separate). This contract stops at "eligible card → queued paper enrollment + log".

**rev 2 review fixes (applied):** (C1) rejection reasons now come from
`explain_paper_admission` — a decision record for **every** card with its true reason —
not re-derived from `profile.cards` (which lacked q on rejected cards and would mislog
everything as `ineligible_q`). (C2) `profile_run_id` is now a real, deterministic field on
`ThesisProfile`. (I3) `forward_test_design` + `card_version` are first-class `ThesisCard`
fields. (I4) PARTIAL may enroll **only** with a valid `forward_test_design`.

---

## 1. Input

`ThesisProfile` (`sniper/analysis/thesis_cards/profile.py`):

| field | meaning |
|---|---|
| `wallet` | wallet address the profile is for |
| `n_positions` | positions the producers ran over |
| `cards` | **all** cards, ranked (eligible first), nothing hidden — the router's full universe |
| `eligible` | the admitted subset == `apply_paper_admission(cards)` output (annotated, rank-sorted) |
| `headline` | the one honest line (display only) |

The router consumes a `ThesisProfile`. It enrolls only from `eligible`; it iterates **all**
of `cards` to build the admission log (every card accounted for).

## 2. Decision log = `explain_paper_admission` output (C1 fix)

`apply_paper_admission` returns **only** admitted cards, so rejected cards in
`profile.cards` are original producer cards with **no q_value** — the router could not tell
`ineligible_q` from `ineligible_placebo`/`ineligible_n`/`ineligible_state`/data-tier. Fixed:

- **`explain_paper_admission(cards)`** (`admission.py`) returns an `AdmissionDecision` for
  **every** card (admitted **and** rejected), each carrying `decision`, `reason`, `rank`,
  `q_value`, `n`, `evidence_state`, `data_tier`. This is the admission-log source of truth,
  used by **both** the assembler and the router.
- `assemble_profile` exposes it as **`profile.decisions`** (full log) and
  **`profile.eligible`** (admitted subset, rank-sorted). `profile.cards` stays the display
  list (eligible first, dead-ends shown).
- enroll-from set := `profile.eligible`; admission-log := `profile.decisions`. The router
  does **not** re-derive reasons.

Admission's statistical gate (logged by `explain`, precedence in §6):
`q is not None and q <= 0.20` **and** `placebo_passed is True` **and** `n >= card_min_n`
**and** `evidence_state ∉ {underpowered, contradicted, descriptive_only}` **and**
`data_tier ∉ {BLOCKED, PROXY}`. `explain` additionally requires a valid `forward_test_design`
(else `missing_forward_test_design`) before a card is enrollment-eligible. In practice for the
current producers `placebo_passed is True ⟺ evidence_state == "supported"`.

## 3. Required card fields the router reads

| field | owner | enrollment role |
|---|---|---|
| `card_id` | producer | idempotency-key + log key |
| `lens` | producer | display |
| `n` | producer | min-n gate (applied in admission; logged) |
| `effect` | producer | magnitude — logged, **never** a stake |
| `score` | producer | source of `rank_key` |
| `p_value` | producer | BH input; logged |
| `q_value` | router (admission) | eligibility gate; logged |
| `placebo_passed` | producer | eligibility gate; logged |
| `rank_key` | router (admission) | slot ordering (= score) |
| `evidence_state` | producer | gate + log |
| `evidence` | producer | carries `data_tier`; admission applies the §10 data-tier guard |
| `forward_test_design` | producer | **REQUIRED to enroll; absence blocks** (first-class, §4) |
| `card_version` | producer | idempotency-key component (first-class, §4) |

## 4. Contract additions (D1 — ACCEPTED + built)

`forward_test_design` and `card_version` are now first-class producer-owned `ThesisCard`
fields (`contract.py`), so admission/enrollment gate on them without parsing `evidence`:

- `forward_test_design: dict | None = None` — producers that have a real forward test emit
  it (sizing, timing); PROXY/BLOCKED cards leave it None (no valid test on current data).
- `card_version: str = "1"` — bump when the statistic/null/protocol changes (idempotency).
- `valid_forward_test_design(d)` helper validates the §5 keys; `producer_card(...)` accepts
  both as kwargs.

## 5. `forward_test_design` schema

A producer-emitted dict declaring how the card is forward-tested. All keys required;
any missing key ⇒ treated as absent ⇒ enrollment blocked (`missing_forward_test_design`).

| key | meaning |
|---|---|
| `metric` | what's measured forward (e.g. `"calibration_edge"`, `"realized_minus_hold"`) |
| `horizon` | forward window (e.g. `"30d"`, `"to_resolution"`) |
| `success_threshold` | pre-registered pass bar (→ `promoted`) |
| `kill_threshold` | pre-registered stop bar (→ `killed`) |
| `min_forward_n` | min forward observations before any verdict |
| `stake_convention` | must be `"uniform_paper_unit"` — no per-card sizing (§10) |

## 6. Admission log (= `AdmissionDecision` per card, from `explain_paper_admission`)

One `AdmissionDecision` per input card (the router persists these with `profile_run_id`).
Invariant: every card is accounted for with exactly one decision + reason.

| field | note |
|---|---|
| `card` | the card (q/eligible/rank filled iff admitted), incl. `card_version` |
| `decision` | `admitted` \| `rejected` |
| `reason` | `admitted` \| one rejection reason (below) |
| `rank` | 1-based rank among admitted, else None |
| `q_value`, `n`, `evidence_state`, `data_tier` | the gate values that drove the decision |

**Reason precedence** (the single most-informative cause, evaluated in this order):
`data_tier → state → placebo → q → n`, then `missing_forward_test_design`, then `slot_cap`.

**Rejection reason taxonomy:**
- `ineligible_data_tier` — data_tier ∈ {BLOCKED, PROXY} (§10) — checked first
- `ineligible_state` — evidence_state ∈ {underpowered, contradicted, descriptive_only}
- `ineligible_placebo` — placebo_passed not True
- `ineligible_q` — q None or > 0.20
- `ineligible_n` — n < card min-n floor
- `missing_forward_test_design` — no/invalid forward_test_design (after statistical gate)
- `slot_cap` — statistically eligible + has design, but cut by the cap (§7)

**Admitted reason:** `"admitted"`, with the `rank` carried in its own field (1-based).

## 7. Slot cap behavior

- Cap `N` applies to `eligible`, sorted by admission's deterministic order:
  `(rank_key desc, q_value asc, card_id, lens)`.
- **Truncation only.** Keep the top `N`; the rest → rejected, `reason=slot_cap`.
- **No manual / operator / name / wallet selection at any point.** The only ordering inputs
  are `rank_key` (= score) and the deterministic tie-break. Reordering the input card list
  must not change the selected set.

## 8. Idempotency

- **Key:** `(wallet, card_id, card_version, profile_run_id)`.
- `profile_run_id` (D2 — built): assigned in `assemble_profile` — `sha256(wallet ||
  sorted card fingerprints)[:16]`, where a fingerprint is
  `card_id:card_version:n:score:p_value:evidence_state`. Deterministic (no wall-clock), so a
  re-run on unchanged content yields the same id and the same enrollment set (no duplicates);
  changed positions/methodology → different id → re-enrolls.
- `card_version`: producer-declared, bumped when the card's statistic/null/protocol changes.
  Methodology change → new version → new key → re-enrolls (intended). Identical re-run →
  same key → no-op.
- Enrollment is an **upsert on the key**: existing key ⇒ no new row, no state reset.

## 9. Paper state machine

States: `queued | active | completed | killed | promoted`.

Legal transitions (all others rejected):
- _enroll_ → `queued`
- `queued` → `active` (paper position opened / forward tracking begins)
- `active` → `completed` (horizon reached **and** `min_forward_n` met → verdict computed)
- `active` → `killed` (kill_threshold breached before horizon, **or** card_version superseded,
  **or** pre-registered stop)
- `completed` → `promoted` (forward metric ≥ success_threshold → graduates to gold board)

Terminal: `killed`, `promoted`. `completed` is terminal **unless** promoted (a below-bar
`completed` is a recorded null result — neither promoted nor killed). The router only enrolls
(→ `queued`); the queued→active→… advances are a separate forward-tracking job (D4), out of
scope here but the state set + legal transitions are fixed by this contract.

## 10. Invariants (hard)

- **No live trade.** The router never submits or recommends a real order.
- **No sizing recommendation.** Even an eligible `sizing_card` enrolls as a *hypothesis* under
  `uniform_paper_unit`; it never emits a per-trade size for live use.
- **Paper only.**
- **Blocked stays visible, never enrolls.** Cards with `evidence_state ∈ {underpowered,
  contradicted, descriptive_only}` **or** `data_tier ∈ {BLOCKED, PROXY}` appear in the profile
  and the admission log but never enroll. (Note: `data_tier == PARTIAL` — e.g. a supported
  `timing_card` — **may** enroll, **but only with a valid `forward_test_design`** (I4);
  PARTIAL ≠ BLOCKED.)
- **Determinism.** Same `ThesisProfile` + same cap ⇒ same enrollment set + same log.
- **No verdict mutation.** Enrollment never writes `overall_verdict`, `gate_result`,
  `primary_edge_type`, or any analyzer surface.

## 11. Required tests (acceptance criteria for the router)

1. `no_operator_picked_enrollment` — enrolled set is exactly rank-ordered
   `eligible ∩ has(forward_test_design)`; no out-of-rank or by-name/wallet admission.
2. `rejected_cards_log_reason` — every rejected card has the **correct** taxonomy reason
   (not a blanket `ineligible_q`); `profile.decisions` covers every card exactly once.
3. `cap_truncates_by_rank_only` — cap=K ⇒ enrolled are top-K by rank_key; the (K+1)th eligible
   is rejected `slot_cap`; permuting the input order doesn't change the selected set.
4. `rerun_is_idempotent` — same profile (same `profile_run_id`, `card_version`) enrolled twice
   ⇒ identical set, no duplicate rows, no state reset.
5. `blocked_descriptive_cannot_enroll` — discovery(BLOCKED) / exit(PROXY, descriptive_only) /
   underpowered / contradicted never enroll; logged rejected with the correct reason.
6. `missing_forward_test_design_blocks` — an otherwise-eligible card with no
   `forward_test_design` ⇒ rejected `missing_forward_test_design`, not enrolled.
7. _(recommended)_ `state_transitions_legal_only` — illegal transitions (e.g. queued→completed,
   promoted→active) are rejected.
8. _(recommended)_ `partial_tier_may_enroll` — a supported `timing_card` with
   `data_tier=PARTIAL` + valid `forward_test_design` **does** enroll.

## 12. Decisions

- **D1 — first-class fields. ACCEPTED + built.** `forward_test_design` + `card_version` on
  `ThesisCard`; producers emit them; `valid_forward_test_design` helper added.
- **D2 — `profile_run_id`. ACCEPTED + built.** Deterministic `sha256[:16]` in
  `assemble_profile`; `ThesisProfile.profile_run_id` + `.decisions` added.
- **Admission `explain` helper. BUILT.** `explain_paper_admission` + `AdmissionDecision`;
  used by the assembler; will be used by the router.
- **D3 — storage. BUILT.** Supabase table `public.paper_card_enrollments`
  (`migrations/supabase_023_paper_card_enrollments.sql`, applied) + the runtime adapter
  `SupabasePaperEnrollmentStore` (`enrollment_store.py`) behind the same `.upsert()` shape.
  Insert-if-absent on the unique key via PostgREST `resolution=ignore-duplicates`; a re-run
  returns the existing row UNCHANGED (D4-advanced state never reset). RLS on / service-key
  only / no live-trade fields. Live round-trip verified (insert → advance to active → re-run
  preserves active → cleaned up). `forward_metric`/`verdict` are nullable, D4-owned, inert here.
- **D4 — forward-tracking job. OPEN.** Whatever advances `queued → active →
  completed/killed/promoted` and writes `forward_metric`/`verdict` is separate from the router;
  the router only enrolls (→ `queued`).

## 13. Storage (BUILT — `public.paper_card_enrollments`)

Supabase Postgres table (`migrations/supabase_023_paper_card_enrollments.sql`, applied):

`id` (bigint identity PK) · `wallet` · `card_id` · `card_version` · `profile_run_id` ·
`rank` · `q_value` · `evidence_state` · `data_tier` · `forward_test_design` (jsonb) ·
`state` (CHECK ∈ the 5 paper states, default `queued`) · `forward_metric` (jsonb, D4) ·
`verdict` (text, D4) · `enrolled_at` · `updated_at`.
Unique constraint `paper_card_enrollments_idem_key` on `(wallet, card_id, card_version,
profile_run_id)` (§8 key). RLS enabled, no policy (service-key only). **No live-trade
fields** (no price/size/order_id/pnl). The embedded admission snapshot = the inline
`rank`/`q_value`/`evidence_state`/`data_tier` columns. Runtime writes go through
`SupabasePaperEnrollmentStore` (PostgREST, not raw SQL).

---

**Cross-ref:** build-sequence step 4 of `docs/superpowers/specs/2026-05-30-thesis-extractor-multi-lens.md`.
Foundation + router + storage built (D1, D2, `explain` helper, `enrollment.py`,
`enrollment_store.py` + `supabase_023` — `tests/thesis_cards/` green; router
integration-verified on real `assemble_profile` output; store live round-trip verified).
**Next:** D4 the forward-tracking job (advances state + writes `forward_metric`/`verdict`),
then the batch runner + non-survivorship candidate feed.
