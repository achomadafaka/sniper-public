# Brain durable writing implementation

Approved design4f5af7cd1 (GammaPASS). Scope/ownership as design; committed-path artifacts require path@sha.

1. Add offline lifecycle tests in tests/test_brain_work_records.py; RED before creating bot/brain_graph/work_records.py. Implement record/complete, durable association/version, retry and recovery, no-integration artifact checks. Extend shared API retract guard with typed reasons; migrate direct test callers.
2. Add bounded first-parent coverage and typed-closure audit tests; use Lambda batch/existing paged reads. Refuse incomplete/unknown; exact commitentity qualification, no ranking.
3. Add CLI tests then parser/dispatch for record-work, complete, work-coverage, closure-audit and typed retract; preserve Lambda entity fallback. Add provenance labels and handover host-fact checks, no automatic-seal claim without Delta proof. Mirrored handover skills stay identical.
4. Focused tests -n2, negative witnesses checked, docs/schema gate; explicit owned commit via crew_commit; Gamma exact diff review before merge. No production/runtime/env/money action. Partial completion retains finding; failed recovery is explicit. No historical backfill or semantic auto-extraction.
