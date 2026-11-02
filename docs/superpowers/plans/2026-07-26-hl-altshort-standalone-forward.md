# HL Standalone Exhaustion Forward Paper Arm — Plan

Status: complete; paper arm active at corrected UTC 2026-07-26T18:24:26.627Z.

1. RED tests for sampled hourly OHLC/funding tape migration and upsert semantics.
2. RED pure-state tests for 30-day eligibility, 30%/72h upward crossing, causal bearish
   gate, next-hour entry, eight-slot admission, sampled SL/TP/cap, costs, funding, and
   leave-one-out benchmark.
3. Implement isolated signal persistence and deterministic scanner.
4. Add immutable manifest validation, heartbeat, exact watchdog, cache-only tape seeder,
   and read-only reporter.
5. Run focused and broad HL suites, schema validation, diff check, cache seeding against
   a DB copy, then merge.
6. Back up the operational paper DB, seed market history only, activate the paper
   scanner, verify exact runtime identity, and report `paper_continue` until sample gates.

No funded process, key, account, allowlist, real DB, or order surface is in scope.
