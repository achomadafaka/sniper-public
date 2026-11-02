# V6 Live-Paper Remediation Plan

1. Add RED tests for quote quarantine/debounce, LP severity, typed route absence, staged tiering, intent suppression, unit safety, feed-gap HOLD, and bounded receive timestamps.
2. Implement the V6 evidence and policy changes; retire the duplicate replay policy; bump the policy identity/hash.
3. Add RED report tests for intent outcomes, all-asset economics, path max drawdown, and unique reasons; implement them.
4. Add RED persistence/runtime tests for explicit migrations, unlocked provider pacing, trailing-hour volatility, and refreshed pool facts; implement them.
5. Add focused operational tests and fixes for deploy backup ordering, TPU ownership, HL cadence/heartbeat, and durable 4663 gaps.
6. Run focused V6 and directly adjacent tests, schema validation, compile checks, shell syntax checks, and `git diff --check`. Do not run broad/full suites or touch Radar.
