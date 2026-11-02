# Moderate-turnover source-buy setup

Status: superseded proposal. No activation or funded setting change occurred.

The user replaced fixed stop and holding-time exits with a demand-continuation design.

Current direction: [coin-attention anticipation and demand continuation](2026-09-08-coin-attention-demand-continuation-design.md).

The exit table below remains the historical replication proposal. It is not the current requested policy.

Setup identity: `turnover_source_1k_10k_v1`.

## Decision

Use the existing funded copy runner. Add one deterministic selector and retain the existing execution path.

The selector qualifies a followed buy when raw five-minute turnover is 10–50% and the source buy is $1k–$10k.

The historical result supports a limited replication study. It does not establish future profit.

## Evidence

The September 8 snapshot contains 39 matching closes, 38 coins, and 18 traders. All 39 originate from `follow_activity`.

| Period | Trades | Recorded PnL | Stake-weighted return |
| --- | ---: | ---: | ---: |
| August 20–31 | 17 | +$195.52 | +17.05% |
| September 1–8 | 22 | +$499.89 | +25.04% |
| Combined | 39 | +$695.41 | +22.13% |

Removing the best trader leaves +$524.43. Removing the two best traders leaves +$409.96.

Solana contributes 18 trades and +$129.65. Robinhood 4663 contributes 21 trades and +$565.76.

The strictest matched comparison retains nine candidate trades and eleven controls. Its return difference is +18.08 percentage points.

The historical search examined many combinations. Neither period is an untouched holdout. Missing fees remain outside recorded PnL.

The worst matching trade loses 80.54%. A stop threshold is not a guaranteed fill price.

Full evidence: `output/research/win-loss-2026-09-08/setup-discovery/findings.md`.

## Exact entry contract

The following conditions define the candidate. They do not replace existing authority or safety checks.

| Field | Required value |
| --- | --- |
| Source | Exact `follow_activity` buy event |
| Chain | `solana` or `4663`; activate each explicitly |
| Wallet | Already armed and approved on the exact chain |
| Identity | Existing normalized chain, mint, origin, and attempt identifiers |
| Source buy | `1000 <= source_size_usd < 10000` |
| Raw turnover | `0.10 <= raw_m5_volume_usd / effective_mcap_usd < 0.50` |
| Market cap | Finite and strictly positive; the existing resolver supplies it |
| Raw volume | Finite and nonnegative; the existing market-data path supplies it |
| Remaining admission | Every current authority, route, age, price, exposure, and loss check still applies |

Use unrounded numeric inputs. Exactly $1,000 and exactly 10% pass. Exactly $10,000 and exactly 50% fail.

Reject booleans and malformed numeric values. Do not coerce an absent field to zero.

Do not subtract the source purchase from raw volume. Do not replace the denominator with `volume_mcap`, current display market cap, or FDV.

The existing resolver can use different cap sources. Preserve its effective value and record the source basis; do not change the resolver here.

Do not add an $80k market-cap floor. The observed minimum is a sample property, not an independently tested threshold.

Do not add a nano requirement, a five-close requirement, a positive prior-PnL requirement, or a trader-name list.

Do not accept missing values through an assumed-nano fallback. Record `unknown` and reject this setup when its selector is active.

Missing data does not mean that the coin failed a measured turnover threshold.

## Position size and exits

Use the current approved bankroll-size calculation. Apply the current chain-local balance clamp, clip ceiling, route-capacity reduction, and global reservation limits.

This design adds no capital allocation, raises no limit, and creates no second funded order for a qualifying origin.

The source buy of $1k–$10k is not our position size. Source size must never become the order's notional.

Use these numeric exit-policy values for the replication cohort:

| Exit field | Value |
| --- | ---: |
| Take-profit threshold | +25% |
| Stop threshold | −35% |
| Maximum hold | 14,400 seconds |
| De-risk | Off |
| Trailing stop | Off |
| Mirror source sells | Off |

All 39 historical matches use the 25% and 35% thresholds. Thirty-four use the four-hour maximum hold and earn +$608.33.

The other five use earlier 600- or 780-second limits. Keep those historical cohorts separate from the four-hour replication cohort.

Reuse the full resolved funded policy. Preserve its catastrophe, debounce, route-loss, volume-death, and infrastructure handling.

If an existing custom policy differs from these numeric rails, retain it during observation and mark `policy_compatible=false`.

In `filter_follow`, that mismatch blocks this setup with `setup_policy_mismatch`. Do not silently replace a custom policy.

Latest persisted contracts show an 80% catastrophe threshold, two stop-confirmation sweeps, and a 900-second unquotable timeout.

These records do not prove that every runtime branch uses those defaults. Activation must freeze the complete effective policy and configuration.

Preserve exact executable exit confirmation, durable close claims, signed transaction identity, and atomic fill finalization.

Existing positions retain their entry contracts. An operator exit remains available and marks that position as operator-modified in the report.

Do not infer numeric rails from names such as `follow-30-50-v2`. The actual contract values are authoritative.

## Live operating modes

These modes are proposed. No configuration switch for this setup exists yet.

| Mode | Behavior |
| --- | --- |
| `off` | Current entry behavior; no setup authority |
| `observe` | Record setup qualification on existing attempts; existing funded admission remains unchanged |
| `filter_follow` | On selected chains, only matching followed buys proceed to normal funded reservation |

The initial implementation defaults to `off`. Live replication starts with `observe`, using natural events and actual existing funded fills.

Observation on existing funded fills measures the setup without additional orders. It does not estimate returns for rejected or unarmed events.

`filter_follow` is a material funded-policy change. It suppresses other followed buys on the selected chains.

Other chains and manual orders remain outside this setup. Their current authority remains unchanged, and their results are excluded from setup PnL.

No historical roster is restored. Watch-only wallets remain watch-only, and paused wallets remain paused.

A wallet's current authority is rechecked by the existing reservation path. A setup match never grants authority.

The setup is not exemptible per wallet. An exemption that bypasses its predicate would create a different strategy.

Normal selection exemptions retain their existing meaning. Freeze their identities and report their effects separately.

## Current feasibility

The authenticated status endpoint reports runner and web release `8f26f3965720740511d05bfb461f8cd1d0242ab8` during this review.

The reviewed checkout is `bfe945ad`; it also contains the newer entry-knob module. Checkout presence does not prove live availability.

The current terminal shadow already stores raw volume, effective cap, source size, and `turnover_10_50_v1`.

That shadow runs after the entry decision. It cannot act as the pre-reservation gate without a shared pure predicate.

At 12:12:58 UTC, a bounded read inspected the latest 1,000 attempt rows. It found 44 version-2 shadows.

Nine shadows contain all three finite inputs. None meets the exact combination. Thirty-five have incomplete inputs, often after an earlier refusal.

This proves input availability on natural attempts, not current signal frequency or a future match rate.

The read result is saved at `output/research/win-loss-2026-09-08/setup-discovery/live-feasibility.json`.

The current cockpit turnover controls cannot reproduce this predicate. Their organic-volume definition, denominator, scope, and exemptions differ.

## Minimal code path

1. Put one pure setup predicate beside the current market-state helpers.
2. Supply the exact event source size and frozen classification facts.
3. Attach its result to the draft classification and the existing attempt substage trace.
4. Evaluate qualification before balance reads and `real_entry()` when the mode requires a filter.
5. Recheck the same frozen qualification inside the shared funded-contract preparation seam before reservation completes.
6. Retain the qualification inside the new entry's canonical funded contract.
7. Use the same predicate in historical reports and terminal shadows.

The helper must perform no database write, provider request, signing operation, or policy mutation.

Its states are `pass`, `fail`, `unknown`, and `out_of_scope`, with explicit reason codes.

In `observe`, a telemetry failure cannot change admission. In `filter_follow`, absent or inconsistent required qualification blocks reservation.

The shared reservation check prevents alternate entry callers from skipping an active setup gate.

It must validate the selected mode, chain scope, event source, and rule identity. A caller-supplied `pass` alone is insufficient.

Retries preserve the frozen decision and reservation identity. A new economic attempt receives a new observation; it does not reuse stale qualification.

Keep one runner, one funded ledger, and the current unique coin/reservation constraints.

| Existing component | Required change |
| --- | --- |
| `sniper/services/memecoin_market_state.py` | Shared pure predicate and serializable qualification |
| `sniper/services/memecoin_real_runner.py` | Capture source size, evaluate mode, and record the pre-reservation result |
| `sniper/services/memecoin_risk.py` | Validate active qualification at shared contract preparation |
| `sniper/services/memecoin_entry_shadow.py` | Reuse the predicate without changing existing study labels |
| Funded settings and report | Default-off mode, explicit chain scope, and setup result view |

Use existing JSON structures first. Add no position table or parallel execution service.

Verify canonical-contract and prior-release parser compatibility before adopting a nested qualification field.

## Qualification receipt

Use one immutable receipt per attempt. The proposed JSON fields are not existing database columns.

The receipt must contain:

- Setup ID, version, mode, activation cohort, and rule digest.
- Normalized chain, mint, source wallet, source event, and attempt identity.
- Exact source USD amount, raw m5 volume, effective cap, ratio, bounds, state, and reason.
- Event time, evaluation time, existing data receipt/cache age, and known provider/pair/cap-source identity.
- Classification hash, effective gate settings, relevant exemptions, and the full exit-policy digest.

Do not invent an upstream observation timestamp from the local receipt time. Missing source provenance remains explicit.

DEX Screener exposes pair identity, volume, market cap, and FDV as distinct fields. Preserve those distinctions in the adapter and receipt.

Source: [DEX Screener API reference](https://docs.dexscreener.com/api/reference).

For Robinhood, use the runner's current source path and cap resolver. Do not substitute a Solana provider or a different cap basis.

## Replication checks

1. Replay the frozen strict-copy cohort and recover all 39 matches with the exact predicate.
2. Test exact boundaries, absent values, NaN, infinity, negative values, and zero cap.
3. Prove that raw and organic volume give separate results when the source purchase is material.
4. Prove that `off` and `observe` retain current funded decisions.
5. Prove that active qualification cannot arm a wallet, bypass safety, or increase a clip.
6. Prove that failed qualification creates no reservation or signed transaction on either chain.
7. Prove that qualification, contract identity, and exit policy survive retries and recovery.
8. Verify ordinary entry, duplicate prevention, and exit regression tests on the exact release.
9. Verify natural pre-reservation receipts in `observe`; do not force an order to complete this check.
10. Compare receipts with the filled contract and final trade outcome when a natural match occurs.

Data agreement verifies the selector. It does not verify future profitability.

## Review and activation

The existing three-candidate day-14 study remains unchanged. This interaction receives its own later start boundary and separate result label.

Keep chain, policy digest, gate-setting period, open positions, missing outcomes, and operator changes separate.

Report actual net costs when available. Report unknown fees explicitly; do not convert ledger return into net account return.

Show results without the best trader, two best traders, best coin, and two best coins.

The implementation and natural-event checks must pass before `filter_follow` activation. Activation also requires explicit approval of its chain scope and capital settings.

No count threshold automatically proves an edge or authorizes more capital. A negative result remains visible; do not tune the bounds during the cohort.

On identity, contract, or observation corruption, block new setup reservations and retain exits for existing positions.

Disabling the filter must be explicit because it restores broader followed-buy admission. It is not an automatic error fallback.

The user's no-backup instruction remains active. This design creates no backup and runs no deployment.

The current deploy script supports `--defer-backup` for database copies. Any later deployment must also respect the instruction across its other copy steps.
