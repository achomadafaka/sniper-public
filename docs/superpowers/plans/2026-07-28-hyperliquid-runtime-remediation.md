# Hyperliquid Runtime Remediation Plan

1. Add regression tests for real-leg timeout, fail-closed reconciliation, and heartbeat health.
2. Add read-only store connections and prove Hyperliquid GET routes do not call migrations.
3. Suppress unchanged excursion writes; gate paper scans by completed hour and funding cursor.
4. Reuse one HTTP session and omit unused open-copy rows from the Edge Desk request.
5. Add Hyperliquid logs to the existing rotation config.
6. Run focused tests, all Hyperliquid tests, schema validation, and `git diff --check`.
7. Commit, deploy the pinned web release, update/restart Hyperliquid runners, disable follow,
   and verify live health without enabling exchange submission.
