# Office evaluation report and post-activation read plan

> **Execution:** Delta implements inline with RED/GREEN; Gamma reviews measurement language and RED witnesses before source. No subagents. Alpha owns activation.

**Goal:** Read the evaluated paper epoch without changing any database, show bounded health/coverage, and release economics only for an externally sealed preregistered look.

**Spec:** `docs/audits/2026-10-29-office-paper-eval-1-preregistration.md`, Amendments 1–11; Gamma's report contract (2026-10-29 22:43:58Z).

**Owned files:** new `sniper/office/eval_report.py`, `scripts/office_eval_report.py`, `scripts/office_eval_health.py`, `scripts/office_eval_seal_prepare.py`, their new tests and this plan. Epsilon retains `report.py`, `floor.py` and UI ownership; no changes there.

## Constraints and edge cases

- Read-only SQLite URIs, read transactions, query-only connection policy, progress deadlines, row/query/byte limits. No DDL, checkpoints, network/provider calls, services, environment edits, keys or money actions.
- Health output contains counts, states and evidence only. No equity USD, realized USD, NET, per-coin results or win/loss counts. Internal hashes do not authorize a look.
- The admitted denominator is exactly "admitted by the near-pass cohort hard filter (desk tickets)". Office list/sheet/judgement observations are separate coverage. Missing desk/cohort data never becomes a zero denominator or a claim about all coins.
- Corrupt/malformed/future rows, unknown marks/FX, wrong epoch/book and incomplete source windows remain unknown or partial. A budget limit is not absence; no NET on any incomplete required section.
- A look requires at least 30 distinct filled coins, all terminal, and 14 days since first order; or the 28-day ceiling. FAILED/BROKEN epochs and >20% censored filled coins have no efficacy read.
- Gamma externally commits a look record before economics. It pins corrected look_at, report script SHA256, selected input heads/digests, eligible counts, limits and optional FX. The expected SHA256 is supplied separately. This reader never registers or mutates a look; identical input/record yields identical JSON, otherwise refuse.
- Marked equity lower bounds, last-mark unverified reading, costs, strata, descriptive intervals, demotion and post-halt provenance remain separately labeled. Never use the neutral entry-cost halt proxy as a mark or NET point.
- Coverage holes are observed call/heartbeat intervals; a long gap is not itself proof of service downtime. No paired clock-jitter claim; frozen .125-second tolerance comes from the 1075-offset envelope against one retained watcher sample, not paired dispatch jitter. Demotion 1800 seconds is configured/unmeasured.
- The health script samples process birth before/after, records UTC lstart, checks exact supervisor entrypoint and release path/link separately, active epoch, five agents, post-start Judge/settle receipts and read-only safety state. A current symlink alone does not prove which Python modules an older process loaded.
- Not handled: activation, durable look registration, grading/promotion, funded data, strategy changes, private Judge memo exposure or source repair.

## Amendment 10

Feed attribution comes from the verified sheet request, and an order’s immutable bundle identifies its sheet. `pass`, `near_pass`, and `unknown` remain separate in feed tables; whole-book count summaries are explicitly labeled. Admitted PASS and refused-unknown SKIP tickets are separate denominators. A missing listing trace is `list_evidence_unavailable`; no item-limit or age exclusion is invented without evidence. Per-feed trading net is descriptive and appears only after the same external look seal.

## Amendment 11

Round reporting binds Beta producer `7a2397c87`: `judge.round:latest`, `judge.round.assigned:<candidate>` and `judge.round.end:<round_id>`. Counts, exact feed, publication labels, incomplete reasons and sheet-to-round latency are read from verified receipts. Missing historical omission reasons remain unknown; current age exclusion is labeled as-of, not a causal claim. Host `pass_unlisted` snapshot counts cover only its 200-row window and are never summed as distinct tickets. Call retention/read failures cannot become a claimed outage. Calendar costs use the same tested frozen formula without importing the SciPy inference engine inside the one-second reader budget.

## Work

1. Write failing report-contract tests and send their exact commit to Gamma: pre-look quarantine, seal/hash/cut/heads, deterministic rerun/refusal, typed lower bounds, strata/coverage, lost ENTER reasons, exit askers/unknown cause, budget/read failures, no mutation.
2. Implement the bounded report module and CLI using existing ledger/cost/Judge primitives where safe. Verify section query plans and causality. Run focused tests at `-n 2`; commit explicit owned paths through `scripts/crew_commit.sh`; Gamma reviews language.
3. Write failing post-start health tests: stale/reused PID, wrong entrypoint/release, wrong epoch/roster, old receipts, missing safety state. Implement read-only script using the shared reader, test, commit and send Alpha the exact command. Never inject a production failure or call a model to obtain proof.

## Reviewed look-v2 contract (Gamma HOLD repairs)

- Health defaults: shared 512 queries, 4 MiB fetched, 1 second, 1000-row sentinel. A look starts with those limits and accepts only explicit overrides up to 10000 rows, 10000 queries, 64 MiB and 10 seconds; every value is pinned by the external record. The report never raises limits itself. Optional work gets at most 0.4 seconds (and reserves time for required computation).
- NET-required: causal ledger orders/cashflows/open-leg marks/capital/askers, ledger note/books, epoch/control existence, epoch.end at the cut, and the nonfinancial safety projection. Bulk sheets/tickets/Judge receipts, memo blocker tags, coverage and other descriptive histories are optional. Their failures are unknown and do not block NET. SQL aggregate counts are labeled not_body_verified. Only order-linked attribution is reverified at look; missing attribution is unknown.
- External `office-eval-look-v2` fields: epoch_id, look_at, report_script_sha256, input_heads (required only), eligible_counts, limits, fx, receipt_seq_cut, safety_snapshot, descriptive_snapshot, descriptive_snapshot_sha256. Gamma supplies and commits the record and its separately supplied hash. The tools do not write, register, sign or amend one.
- The descriptive snapshot contains counts/labels, computed_by, the exact as_of, optional input heads and per-table known/unknown states. Structural quarantine runs at preparation and again at look. The look labels it descriptive_snapshot_sealed / not_reverified_at_look. No stale preparation or swapped snapshot is accepted.
- Receipt reads are bounded by the sealed max seq and SQL time cut. Required order hashes use a sorted causal projection: a future fill/close/exit/halt is excluded; mutable state-index ordering does not change the hash. Live control-pointer changes are excluded from historical financial heads; the control record must remain readable and structurally valid, and the frozen epoch record is independently pinned.
- Safety snapshot: state, config_hash, last_equity_read_at, halt_kind, halted_at; never equity/peak/USD. The sealed state must be known/partial_lower_bound, the read no older than unknown_max_s, current config must match the frozen note and sealed hash, current read time cannot regress, and the current row must be readable. A halt at/before the look blocks efficacy (FAILED/BROKEN); a later halt is flagged post_look_halt and does not rewrite the historical look.
- Required failures/truncation still withhold NET. Optional snapshots do not replace the order's own verified sheet/ticket/bundle. Absent historical equity/heartbeat/learning series remain unknown. Index/hash/clock/process evidence remains separate from runtime effectiveness.


## Draft-record preparation (Gamma request, 2026-10-30 UTC)

`python3 scripts/office_eval_seal_prepare.py --office OFFICE --ledger LEDGER --desk DESK --now CORRECTED_UTC [--limits-json JSON] [--sol-usd FX --sol-at FX_AT]` prints canonical candidate bytes on stdout with no newline and SHA256 plus `draft_unapproved` metadata on stderr. It never writes a file, registers a look, supplies approval or invokes the economics path with a record. Gamma reviews and commits the exact bytes separately. Limits default to the explicit look ceilings; use the same limits JSON on `office_eval_report.py --mode look --look-record FILE --look-sha256 HASH`. Different FX/limits refuse replay. The candidate is reproducible at the same cut; the supplied hash binds exact bytes. Preparation and replay both structurally quarantine the count-only snapshot.

Demotion labels: the look cannot reconstruct a continuously refreshed capture-gap pause versus demotion from the latest mark alone. Both have the same zero NET lower bound; demotion counts/durations come from the sealed optional safety observation. Missing latch/demotion metadata stays unknown, including per-feed counters.


## Amendment 13: declared model basis

`decisions.by_seat_and_basis` and the same table in each feed count committed `judge.decide` receipts (including unpublished rows), not paired targets or model calls. The optional aggregate joins `turn_decision_id` to the exact `memo.commit` action key and checks the memo actor, turn id, known_at cut and nonempty served-model metadata. It preserves declared `echoed` / `requested_pinned`; missing, future, wrong-identity, absent-model or invalid-basis evidence is `unknown`. An unavailable aggregate is a typed unknown table. No private memo text is fetched for this count, no time-proximity/model-name inference or model_calls fallback is used, and no admission/transport authority changes. As with other descriptive SQL counts, body verification is not claimed.


PASS sheet shape correction: real SH.sheet PASS requests omit feed. A known sheet with a request object and an omitted feed is pass; unfamiliar explicit values remain unknown. Absent or unknown-state sheet evidence stays unknown. SQL basis/source aggregations use the same missing-feed default and count only known-sheet receipts for source attribution; unpriced attempts are not known coins. Regression uses the byte-exact seq726 payload from the producer fixture.

Completed-round diagnostics: a Claude-row/GPT-column decision matrix counts distinct candidate rounds with a recorded complete end and a published two-Judge pair. It is split by receipt-derived feed and retains the prereg `split` label. Missing complete-pair evidence is unattributed, never inferred from private or single-Judge answers.

Am15 sheet-failure diagnostics count `judge.round.sheet_failed` receipts by reason and feed under an indexed cache-key prefix, receipt-sequence and corrected-time cut. Exact epoch_id is required for attribution; legacy missing-epoch rows in the time window are separately attribution_unknown. Unknown/foreign/future identity is never joined by time proximity. These are assignment/freshness attempt counts, not proof of missing market prices and never distinct coin counts. Optional aggregate failure is unknown, not zero; no NET-required input or fill-path behavior changes.

Am14 round rule/age evidence: `rounds.by_rule` splits recorded `am11`/`am14`/unknown, each with per-feed rounds and published-label counts. Missing historical rule is unknown rather than inferred from timestamps or release. Assignment age is corrected round_cut minus sheet clock; a conflicting recorded sheet_age_s stays unknown. Judge age is the first committed `judge.decide.view_cut` minus that same candidate sheet clock, never decision commit time. Missing/uncommitted, future-to-commit or pre-sheet cuts stay typed unknown. These are committed decision observations, not proof of the frame seen by a Judge that never committed. No new DB queries are needed; computation is linear over bounded, already-read rounds and receipts.

Sheet failure detail (producer fe7342ad15): `reason_details` groups attributed attempts by the tool's recorded state/reason. The row's `state: known` means that detail was present and well-shaped, not that the sheet or price was known; `tool_state` preserves the producer's state. Null, absent, malformed or oversized detail counts as `reason_detail_unavailable`, with no backfill. The 200-character reason and bounded object projection keep the existing optional SQL/query budget; aggregate failure makes details unknown. Overall and per-feed reason/attempt denominators stay unchanged.
