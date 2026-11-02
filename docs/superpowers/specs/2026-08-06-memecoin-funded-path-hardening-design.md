# Memecoin Funded Path Hardening Design

**Date:** 2026-08-06

## Problem

The funded runner has six defects on its active code path.

- The funded contract builder omits `entry_mcap`. `scalp_c2` can freeze the default TP.
- The fastlane can stop before its loop starts. The slow pass then skips all funded exits.
- The paper hook runs before the funded hook on both entry paths.
- A paper failure can prevent a funded attempt or create a duplicate retry risk.
- A failed native-SOL read becomes a valid zero balance.
- The startup log uses a chainless approval query and reports zero approved wallets.

The current paper evidence does not meet the exit-policy promotion gate. This change does not alter
the funded exit policy. The current wallet exclusions also need an operator authority decision.

## Contract

### Frozen funded policy

- Pass `effective_mcap` to `funded_policy_for_arm` during funded contract preparation.
- Freeze the selected arm policy with its correct market-cap band.
- Keep the current immutable contract and overlay rules unchanged.

### Fastlane liveness

- Put fastlane setup and each pass in one retry boundary.
- Track the worker state, the last success time, and a sanitized failure code.
- Restart the worker if its thread stops.
- Serialize all funded exit sweeps with one process lock.
- Run the slow sweep when the fastlane state is stale or failed.
- Evaluate the fallback state after the slow sweep gets the exit lock.
- Publish the fastlane state in the heartbeat and the existing status APIs.
- Do not mark the main runner unhealthy when the slow fallback is active.

This design prevents two funded sweeps from running at the same time. It also keeps funded exits
active after a fastlane setup failure or pass failure.

### Funded hook order

- Run the funded hook before the paper hook on the global and follow paths.
- Isolate funded failures so the paper hook can still run.
- Retain the dedup row after a funded hook call, even if the paper hook fails.
- Keep the old paper retry behavior when no funded hook exists.
- Record and log a paper failure without a second funded attempt.

### Native-SOL read truth

- Add a typed native-SOL result with `ok`, `balance`, and `failure` fields.
- Report a valid on-chain zero as `ok=True` and `balance=0.0`.
- Report RPC, JSON-RPC, and malformed response failures as `ok=False`.
- Use the typed result in the funded entry and gas-alert paths.
- A read failure blocks the entry with an unavailable reason. It does not report low funds.
- A read failure does not send a GAS-LOW alert.
- Keep `sol_balance()` as a compatibility wrapper for non-funded callers.

### Startup authority

- Count approved wallets for each configured funded chain.
- Print the per-chain authority map at startup.
- Do not use the chainless approval query for this log.

## Scope

Change the funded runner, its loop, its status surfaces, the two entry paths, the Solana wallet
read, and focused tests. Add no schema, dependency, funded policy, wallet arm, database write,
service restart, or deployment.

## Verification

Tests must reproduce each defect before its fix. Tests must use the funded contract path and the
real hook control flow. Fastlane tests must prove setup recovery, slow fallback, sweep exclusion,
and status publication. Native-SOL tests must separate a valid zero from a failed read. Run focused
tests, adjacent funded tests, schema checks, compile checks, and `git diff --check`.
