# Axiom Completion Implementation Plan

> Execute the independent repair tasks with isolated file ownership and review their combined result.

**Goal:** Complete Axiom source measurement, existing research intake, and operator account discovery without a new paper arm.
**Architecture:** Keep capture separate from funded execution. Preserve typed source provenance in SQLite and reuse existing account/research paths.
**Tech Stack:** Python, SQLite, Playwright, existing web UI, and pytest.
**Spec:** docs/superpowers/specs/2026-09-16-axiom-observation-lanes-design.md, operator amendment section 9.

## Global constraints

- J7 remains the funded source. Do not add a new paper arm or grant new funded authority.
- Do not change the user's Axiom account automatically. Use explicit account actions.
- No backups. Preserve unrelated work and use focused red-green tests.
- Legacy observations remain unknown where clocks, units, or provenance are absent.

## Task 1 — Identity and revocation

Files: axiom_address_verify.py, onchain_evm_wallet_watch.py, memecoin_evm_onchain_watch.py, seed command, focused tests.
- [x] Reproduce self-transfer, unrelated-token gift, empty revocation, seed overwrite, and historical-window defects.
- [x] Require net acquisition and supported payment evidence; unsupported relayer evidence stays unknown.
- [x] Resolve historical bounds from block timestamps with bounded RPC reads.
- [x] Share supported receipt semantics between proof and detection.
- [x] Add an explicit bounded verifier command and preserve existing funded authority checks.

## Task 2 — J7 receipt persistence and comparison

Files: j7_feed.py, receipt portion of memecoin_runner_loop.py, axiom_feed_compare.py, receipt_clock.py, focused tests.
- [x] Prove that a blocked writer cannot delay the funded loop.
- [x] Persist receipts through one bounded background writer with loss/error counters.
- [x] Use clock_domain() -> str | None for durable host and boot identity.
- [x] Compare chain, owner, token, side, and transaction identity one-to-one.
- [x] Separate exact, weak, amountless, history, unmatchable, and missing-domain results.
- [x] Add a read-only operator report with sample and observation limits.

## Task 3 — Capture and persistence

Files: db_axiom.py, axiom_browser.py, axiom_session.py, focused tests.
- [x] Migrate older schemas with duplicate rows without loss of first receipt identity.
- [x] Add nullable clock_domain, capture_run_id, parser_version, transport, and observation_kind.
- [x] Preserve REST as history and WebSocket observations as live only after supported parsing.
- [x] Reparse repeated REST payloads independently of raw storage deduplication.
- [x] Add first/last observation metadata, units, and durable callout identity.
- [x] Enforce saved halts, document-specific 403 handling, usable-response counts, single process, and raw size retention.
- [ ] Verify real wallet WebSocket shapes from stored raw data or read-only browser observations.

## Task 4 — Existing research and account paths

Files: existing Conviction/Gold intake and account UI modules, selected after code inspection; axiom_parse.py and focused tests.
- [x] Validate actual supported layouts and native units; unknown volume windows remain unknown.
- [x] Identify existing lane admission and identity contracts before connecting source data.
- [x] Add source-labelled intake and account candidates from KOL, live trades, and curated lists.
- [x] Provide explicit account add/remove/swap and link actions with no automatic funded approval.

## Task 5 — Integration and active verification

- [x] Run focused integrated tests and review every changed file.
- [x] Run schema references and the Knowledge Sync audit.
- [x] Merge only completed, tested changes within the operator's authorization.
- [x] Restart affected observation services and verify their actual process paths and persisted data.
- [x] Verify the funded source and authority separately if a receipt-only runtime update is required.
- [x] Record remaining provider/data limits without claiming a speed or reliability result before sufficient forward evidence exists.

## Evidence before integration

The final integrated suite passed 400 tests. The schema validator found no violations.
The provider wallet push envelope remains unsupported; its observations cannot support a latency verdict.
The operator approved integration. Runner and web use `6093963e`; capture also receives `ea6ad147`.
Runtime checks passed. Fresh Axiom discovery and wallet push evidence remain pending.
The detailed report is `docs/audits/2026-09-16-axiom-completion.md`.
