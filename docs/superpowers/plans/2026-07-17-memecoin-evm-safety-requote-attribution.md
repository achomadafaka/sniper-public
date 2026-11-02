# EVM Safety Requote and Quote Attribution Plan

1. Add failing tests for a decoded safety min-out that succeeds after one fresh quote, a repeated
   min-out that stays blocked, and an unknown revert that is never called a proven honeypot.
2. Preserve JSON-RPC revert detail, reuse the existing classifier, and add one bounded safety
   requote without changing the generic fail-closed safety contract.
3. Add failing trace tests proving post-approval and retry quotes receive their own reconciled
   timestamps and matching quote ages.
4. Bind final quote metadata to the final quote observation and compute age against the matching
   confirmation observation.
5. Run focused EVM tests, the full memecoin suite, schema validation, compileall, and diff checks.

