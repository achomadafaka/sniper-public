# Office first-call diagnostic repair

Alpha ordered this urgent repair after the live `a6cf8287e` first calls failed. Use inline TDD in `codex/office-call-diagnostics`; producer work is preserved separately.

## Scope and edge cases

- Codex owns `Supervisor.run_turn`, provider-result metadata in `model_child.py`/`models.py`/`runtime_steps.py`, a shared pure redactor, and focused tests. Epsilon owns `supervisor.run_child` stderr capture and launcher persistence; do not edit those functions.
- A returned late Step must be recorded, including its typed failure, but its actions must never execute. Preserve lease/generation fencing; unknown clocks or lost leases cannot authorize new writes.
- Copy only the four known Codex error categories. Claude error text is bounded and redacted before child persistence and again at the parent boundary; strip credentials, tokens, URLs, private paths, and exact supplied prompt strings. Success output and dispatch remain unchanged.
- No provider calls, retries, deadline increase, schema change, service restart, credential reads, `.env`, signing or funded action. This repairs evidence loss; it does not prove the provider's underlying failure is fixed.
- Additional work is CPU-only failure-path classification/copying. No new provider/file I/O on model dispatch. Source rollback restores prior behavior; existing diagnostic records remain.

## RED/GREEN work

- [x] Reproduce late-Step loss, missing provider error category, missing/redacted Claude detail, and end-to-end turn-value propagation with fake providers.
- [x] Persist late returned Step under the same lease fence, without action submission; add a typed deadline marker where no error reason exists.
- [x] Preserve whitelisted provider category and bounded redacted detail through Envelope and failed Step value. Existing UI reason projection receives the typed category via `value.reason`.
- [x] Run focused model/step/supervisor/launcher tests with `-n 2`; 148 PASS. `run_child` is unchanged. Redactor helper only: 1000 samples, p50 9.38 us / p90 9.61 us; not provider/fill latency.
- [ ] Commit owned paths with `scripts/crew_commit.sh`; original-author review/Zeta and Alpha handoff. No merge/install/live proof claimed.
