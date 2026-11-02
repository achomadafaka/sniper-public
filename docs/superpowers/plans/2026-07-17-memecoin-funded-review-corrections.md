# Memecoin funded review corrections plan

1. Add failing V4 tests for canonical swap/Permit2 ABI, corrected deadlines, integer bounds, the
   exit V4 rung, and one-time unwrap reuse across a mined min-out retry.
2. Fix V4 encoding/clock/range guards, venue symmetry, and retry-local unwrap reuse. Run focused
   EVM quote, seam, safety, executor, and exit tests.
3. Add failing identity/coverage tests for normalized first-seen lookup, malformed consensus time,
   reachable non-curated consensus alerts, and a mid-band position excluded from the micro cap.
4. Apply the smallest DB/runner corrections without adding hot-path network work. Run focused
   translation, runner, alert, and micro-policy tests.
5. Add failing funded-exit tests for executable trail confirmation and percent-vs-mcap trigger
   disambiguation; add an API test pinning bare-trail semantics. Apply the minimal corrections.
6. Add failing broadcast tests distinguishing explicit rejection from ambiguous transport for
   entry, pre-transaction, and exit paths. Preserve no-resubmit on ambiguity.
7. Add failing retry-trace tests proving per-submit history and final-submit stage columns. Keep
   quote ID/age bound to the final executable quote.
8. Run focused batches after each logical commit, then the full memecoin suite, concurrency/runtime
   deploy tests, schema references, compileall, and `git diff --check`.
9. Review the complete diff adversarially, run the Knowledge Sync Gate, push a PR, merge only after
   CI, deploy the exact merge SHA, and verify the pinned runner heartbeat/bracket sweep.
