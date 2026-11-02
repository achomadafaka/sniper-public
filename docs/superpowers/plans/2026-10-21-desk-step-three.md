# Desk Step Three Implementation Plan

> **For agentic workers:** Use `superpowers:subagent-driven-development` for the independent pure-contract and desk-store tasks; the coordinator implements the funded seams and integration. Use TDD for each task.

**Goal:** Persist immutable candidates and decisions, validate the frozen verdict contract, hand off an idempotent ENTER, and enforce a durable cumulative cohort kill.

**Architecture:** Slow desk work uses its own SQLite file. The funded ledger stores immutable cohort membership and kill/cost state; entry never reads the desk database. Existing intent and funded-contract paths carry the decision into execution and recovery.

**Tech Stack:** Python standard library, SQLite, existing Sniper execution interfaces, pytest with two workers.

**Spec:** `docs/superpowers/specs/2026-10-21-desk-execution-plumbing-design.md` §§2, 6, 7; Lambda `desk-judge-v6` at `153930a9f`; cohort-read plumbing §7.

## Global Constraints

- Local branch only. No push, deploy, service restart, production DB mutation or `.env` edit.
- Desk clip $25, desk max 5, shared daily halt $200, cumulative cohort kill at net <= -$200. Preserve existing default per-chain cap 3 and portfolio total cap 11; change neither.
- No LLM, provider request, retry, desk-DB connection or new lock on the funded fill path.
- Keep signed/unknown transactions, exits and reconciliation working after a kill and with historical contracts.
- Missing facts and costs never become zero. Mandatory safety unknowns refuse entry; optional facts remain explicitly null.
- WAIT grammar is validated, but WAIT execution refuses `cond_unavailable` until a calibrated bar source is supplied.
- Use aware UTC persisted timestamps and supplied monotonic elapsed milliseconds for the five-second verdict deadline. Do not reset either clock.
- Lambda owns U producer/core/worker files. Beta retains LP, VaultTape and exit-timing work. Money-seam changes are separate commits for Beta/Gamma review.
- Gamma owns shadow execution/read views. This increment leaves immutable emissions and independent arm-admission records; it does not fabricate shadow fills or valuations.

## Review Focus

1. Crash after ledger enqueue but before desk acknowledgement must replay one identical intent with its original expiry.
2. Future facts, partial windows, poisoned strings and mismatched receipts must never become JUDGE facts or admission authority.
3. Concurrent killed-cohort reservations and delayed loss pricing must preserve a write-once kill across restart and midnight.
4. A copied position must retain its frozen cohort through unknown-fill recovery; missing metadata must block unsigned desk admission, not signed recovery.
5. Gross trade cash PnL must not masquerade as net: separately incurred costs are uniquely attributed, unpriced costs refuse new admission, and later winners never clear a trip.

## Task 1: Pure candidate and seat contracts

**Files:** create `sniper/services/memecoin_desk_contract.py`, `tests/test_memecoin_desk_contract.py`.

**Interfaces:**
- `validate_candidate(candidate: dict) -> dict`: returns normalized envelope with `opportunity_id` equal to producer's stable `ticket_id`, parsed UTC/emission identity and validated snapshot. Raises `ValueError` with a machine failure slug.
- `render_judge_sheet(candidate: dict) -> dict`: exact typed v6 fields; optional missing fields are null and named in `missing`; no raw text or addressing fields are exposed except the opaque opportunity key.
- `validate_timing_caps(caps) -> dict`: requires frozen integer caps `enter_ttl_max_s`, `boundary_max_s`, `wait_max_s`; rejects malformed or excessive caps.
- `validate_verdict(raw, *, opportunity_id, bounds, timing_caps, elapsed_ms, call_error=None, bars_available=False) -> dict`: returns normalized `action`, `reason`, `rails`, `condition`, `reason_code`, `enter_ttl_s`, `next_check_s`; malformed/late/refused input returns SKIP with a distinct cause.
- `hard_filter(candidate, *, evidence: dict, policy: dict) -> dict`: pure mandatory safety/market-data checks, typed dependency failures, H/U activity handling; no I/O.
- `build_h_candidate(...) -> dict`: confirmed signer/payment/acquisition receipt, unarmed tracked wallet, causal fields; reuses receipt initiation proof and Lambda's snapshot representation without persisting the raw receipt.

- [x] Write RED cases for finite numeric types, exact keys, snapshot hash, future knowledge, incomplete windows, no raw text, H receipt identity/commitment, U ungated identity, strict E/W/S, five-second late refusal and uncalibrated WAIT.
- [x] Implement the smallest pure code; reuse `validate_desk_rails` and the confirmed-receipt proof.
- [x] Run focused tests with `-n 2`, demonstrate representative mutation failures, commit only owned files.

## Task 2: Desk storage and durable handoff records

**Files:** create `sniper/persistence/db_memecoin_desk.py`, `tests/test_memecoin_desk_store.py`.

**Interfaces:**
- `connect(path)`, `ensure_tables(conn)` own only the dedicated desk file.
- `register_cohort(conn, *, cohort_id, producer, prompt_version, policy, created_at)` freezes canonical configuration; conflicting replay refuses.
- `record_candidate(conn, candidate, *, cohort_id, now_iso, cooldown_s) -> dict` stores immutable emission/snapshot before any verdict/book decision; dedupes open coin tickets while retaining later sightings.
- `record_stage(conn, *, ticket_id, stage, seat, verdict, reason, inputs, ts, output=None, response_at=None, latency_ms=None, cost_usd=None, outbox=None) -> dict` atomically writes one stage and optional fixed-payload outbox row.
- `get_ticket(conn, ticket_id)`, `list_outbox(conn, *, limit=10)`, `resolve_outbox(conn, ticket_id, *, state, intent_id=None, reason=None, now_iso)`.
- `record_admission(conn, *, ticket_id, arm, decision, decided_at)` records F/SJ/SR independently.

Tables include canonical ticket heads, append-only `desk_tickets` stage rows, immutable snapshots/emissions, cohort configuration, independent arm admissions and an outbox. Keep stage uniqueness, identical-input dedupe and coin/seat cooldown. A later trigger on an open coin is a retained sighting, not another model call.

- [x] Write RED tests for conflicting immutable replay, two-connection coin race, producer opener identity, cooldown, stage no-reroll, immutable response clock, atomic stage/outbox and absolute-expiry preservation.
- [x] Implement SQLite constraints/short desk transactions; no funded-ledger imports or connections.
- [x] Run focused tests and mutation checks, commit only owned files.

## Task 3: Funded cohort identity and cumulative kill

**Files:** narrow edits to `db_memecoin.py`, `memecoin_manual_intents.py`, `memecoin_origin.py`, `memecoin_real_runner.py` entry-only, funded-policy provenance, risk reservation labels; new `tests/test_memecoin_desk_cohort.py`.

Freeze `{cohort_id, producer, opportunity_id, snapshot_hash}` in intent replay comparison and `source.desk`; derive indexed position membership from that frozen source during `open_position`. Historical contracts remain readable.

Provide main-ledger helpers to register frozen cohorts, record uniquely keyed known/unknown expense events, query cohort accounting/refusal and latch a kill. Unknown incurred expenses and unpriced closed positions refuse new admission. No expense row means no such charge has been recorded; a new desk fill must carry an explicit execution-cost marker, never an invented zero.

The cumulative guard belongs in the same reservation INSERT. Capture its refusal reason before commit. All settled-PnL writers and negative residue reversals evaluate the same commit-free latch; positive adjustments cannot clear it. Copy paths short-circuit and retain their existing single-PK close check.

- [x] Write RED tests for cohort metadata conflicts, unsigned legacy refusal, historical signed recovery, cross-midnight cumulative loss, delayed earlier winner, independent H/U cohorts, unknown costs, idempotent costs/refunds, residue reversal and reservation race.
- [x] Implement minimal additive schema/provenance/SQL predicates and covered writer hooks.
- [x] Run existing desk/manual/reservation tests with updated explicit cohort fixtures where required; measure the copy reservation guard p50/p90 on temporary data.
- [ ] Commit the money delta separately for Beta/Gamma review.

## Task 4: Integrated offline funnel and replay-safe ENTER

**Files:** create `sniper/services/memecoin_desk.py`, `scripts/memecoin_desk_ingest.py`, `tests/test_memecoin_desk_pipeline.py`.

**Interfaces:**
- `ingest_candidate(desk_conn, candidate, *, cohort_id, now_iso, cooldown_s=900) -> dict` is Lambda's storage boundary; ingestion alone never buys.
- `evaluate_candidate(...)` persists a hard-filter result from already collected evidence.
- `record_decision(...)` validates one supplied model result; this increment never calls a model. A valid ENTER writes a size-stage outbox with frozen rails, metadata and absolute expiry. No verdict or failed validation means no outbox.
- `dispatch_outbox(desk_conn, ledger_conn, *, now_iso, limit=10) -> list` uses existing `enqueue`, fixed IDs/expiry and then desk acknowledgement. Existing ledger intents are recognized even after a crash/expiry; no second buy.
- `reconcile_intents(...)` records terminal/unknown fill-stage truth through existing `read_status`.

- [x] Write RED end-to-end tests using real temporary desk and ledger databases: candidate -> filter -> decision -> one intent -> frozen contract/recovery; no providers/models.
- [x] Inject crashes before/after ledger commit and repeat dispatch; prove one request, original expiry and no resubmission on unknown fill.
- [x] Add a bounded JSONL ingestion CLI using an explicit desk-DB path; no implicit production connection, background process or runtime activation.
- [x] Run combined focused regression set with `-n 2`; run one independently reviewed integration/mutation pass. Broaden once only when changed shared seams justify it; full suite requires the host lock.

## Task 5: Final review and local delivery

- [x] Reconcile Lambda's current emission API with the strict consumer and tell him the stable signature/schema.
- [ ] Beta/Gamma review the separate money commit; fresh reviewer checks pure contracts/store/integration and the identified five failure classes.
- [x] Run audit-only Knowledge Sync and stale-spec sweep; update only relevant implementation/runbook docs.
- [ ] Report exact commits, test results, current limitations and no-push/no-deploy status. Do not claim funded readiness without configured safety collectors, calibrated bars or live proof.

## Local delivery evidence

See `docs/audits/2026-10-21-desk-step-three-review.md` for the rule-5 map, APIs,
separate money-path file list, 1,041 passing targeted checks and audit-only knowledge delta.
Crew sign-off is a review gate; no activation, push or deployment occurred here.

Review follow-ups: U refuses mint-as-pool; H activity skip retains JUDGE/SJ/SR while F records
no-take; the no-take crash gap and legacy unsent-outbox bypass have regressions. Preflight reads
the stored cohort kill limit and signed pending-cost lookup uses a partial cohort index. Final
combined crew-review set: 525 passed in 24.17 seconds at `-n 2`. U can run without an H cohort;
the withdrawn age/chase proposal adds no filters or parked knobs.

## Activation prerequisite (crew review correction)

Do not activate until an automated exact expense-pricing adapter values incurred desk execution
and model charges, mandatory safety collectors are configured, both crew reviews pass, and the
operator explicitly authorizes deployment. With the adapter absent, the first unpriced desk fill
correctly holds later cohort admissions. This branch builds the accounting boundary and does not
activate or implement the adapter. The configured 3 default per chain / 11 portfolio total caps
are distinct; desk 5/$200/-$200 limits and every existing chain override remain unchanged.

For slice 1, unknown signed same-cohort entry expenses intentionally hold additional desk
reservations. This serializes the unpriced signing interval, without interrupting reserved work,
exits or reconciliation. It is distinct from the five-open ceiling. The pricing adapter must
resolve both successful and failed per-signature charges before normal admission can continue.

## Dated follow-up: automatic expense adapter built locally

See `2026-10-21-desk-cost-pricing.md` and its design for the implemented off-path reader/resolver,
Solana receipt pricing, subscription billing evidence and explicit worker executable. The old
"adapter absent" prerequisite described the original step-3 scope. The new code still requires
separate review, evidence configuration and explicit activation; this branch starts nothing.
Final adapter/desk/manual/pending/partial check: 810 passed in 36.13 seconds with `-n 2`.

## Operator-approved follow-up: per-ticket timing

New verdicts use exactly `k`, `v`, `e`, `r`, `c`, `enter_ttl_s`, `next_check_s`. Both timing
fields are required integers on ENTER, WAIT and SKIP; no default or clamping. The frozen cohort
policy carries `enter_ttl_max_s` (3–30), `boundary_max_s` (5–120), and `wait_max_s` (10–300),
instead of a fixed `enter_ttl_s`. A policy change creates a different immutable cohort identity.
The model chooses entry TTL in `[3, enter_ttl_max_s]` and next check in `[5, boundary_max_s]`.
WAIT expiry remains `e[4]`, with its existing minimum 10 and the frozen `wait_max_s` ceiling.

ENTER expiry is computed once from the original response timestamp plus the model's TTL.
An optional caller expiry asserts exact equality; it cannot extend or shorten the decision.
Replay preserves that absolute expiry. Invalid timing records SKIP and the incurred call cost.
Unsent legacy decisions without timing authority refuse; an already existing ledger intent is
acknowledged before applying the new checks. Existing policies and stage rows are never rewritten.

`next_check_s` is retained in the immutable verdict for the boundary worker. This increment adds
no boundary scheduler or calibrated WAIT executor. The five-second model deadline, $25 clip and
5/$200/−$200 limits remain unchanged. Local build only; no runtime activation.

Timing verification: 92 new pure checks and 28 integration/legacy checks failed before their
implementations; the combined contract/pipeline set then passed 354 tests with `-n 2`.
