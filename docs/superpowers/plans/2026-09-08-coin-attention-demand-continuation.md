# Coin attention and demand continuation implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to execute this plan task by task. Steps use checkbox syntax.

**Goal:** Identify coin demand before a qualified trader buy, then test an exit based on demand failure.

**Architecture:** Reuse the existing discovery feeds, swap tape, health checks, and executor. First verify data coverage without changes to funded decisions.

**Tech Stack:** Existing Python, SQLite, pytest, and research report tools. No new dependency or service is required for the first stage.

**Spec:** `docs/superpowers/specs/2026-09-08-coin-attention-demand-continuation-design.md`

## Global constraints

- No backups.
- No production writes, service restarts, or funded policy changes during the first stage.
- The proposed policy has no percentage stop or maximum position age.
- Existing positions retain their frozen contracts.
- The +25% automatic sale choice remains unresolved; always measure the executable target touch.
- Keep Solana and chain 4663 results separate.
- Preserve the original frozen candidate study and its labels.
- Missing data means unknown. Distinct addresses do not prove distinct people.

## Code evidence

| Existing component | Reuse | Limit found in code |
| --- | --- | --- |
| `scripts/memecoin_pool_prewarm.py::unresolved_recent_mints` | Pool preparation and bounded resource use | Its population starts from wallet buy events. It cannot establish independent discovery before those events. |
| `sniper/services/memecoin_solana_tape.py::pool_flow_window` | Pool flow arithmetic | It selects receipt-time windows and combines buyer and seller addresses. |
| `sniper/services/memecoin_solana_tape.py::flow_observation` | Flow relative to reserve depth | Its turnover denominator differs from the historical raw-volume/market-cap ratio. |
| `sniper/persistence/db_ticks.py` | Swap identity and block-time map | A missing block time cannot become an assumed event time. |
| `sniper/services/memecoin_volume_death.py` | Venue coverage and stream health checks | Swap silence is narrower than weak demand. Its price-movement veto needs separate assessment. |
| `sniper/services/memecoin_entry_shadow.py` | Frozen raw turnover and source-buy observations | A terminal observation does not authorize an entry. |
| `sniper/services/memecoin_risk.py::prepare_funded_contract` | Shared risk checks and frozen contract | A new strategy must preserve the common reservation path. |

The schema registry lacks the two tape tables. Their definitions were verified in `sniper/persistence/db_ticks.py`.

## Stage 1: Verify the observation path

This is the executable first stage. It produces a coverage report, without a new strategy or database writer.

The paper-lane audit now establishes an existing discovery source. See `docs/audits/2026-09-08-paper-lanes-before-armed-buys.md`.

The scanner recorded 347 of 617 known armed-bought coins before the first observed armed buy. Its signals preceded 178; independent paper preceded 63.

Reuse this path before adding discovery infrastructure. Fresh demand at confirmation, historical authority gaps, and event-time tape coverage still require work.

**Output:** `output/research/win-loss-2026-09-08/setup-discovery/attention-coverage.md`

1. [ ] Trace the existing discovery feeds to their first local receipt for each chain and coin.
2. [ ] Identify feeds that can discover a coin before the qualified source buy.
3. [ ] Inspect the tape producers and confirm the meaning of actor, amount, event time, and receipt time.
4. [ ] Read a bounded recent sample through read-only connections with a fixed report cutoff.
5. [ ] Count coins with observations before the buy, after the buy only, and without a qualified buy.
6. [ ] Report actor, event-time, route, depth, and source-health coverage separately for each chain and venue.
7. [ ] Report duplicate logs, ambiguous route legs, missing clocks, and capped collection as explicit exclusions.
8. [ ] Select the smallest existing feed extension that closes the measured gap.

A historical swap fetched after a buy does not prove the system knew that swap before the buy.

**Acceptance:** Each report count has a denominator and a time interval. The report separates historical reconstruction from information available at the decision.

**Decision:** Use an existing independent feed if coverage permits. Otherwise extend discovery before any claim of trader anticipation.

## Stage 2: Record a coin attention list

Record one chain-and-coin state per fixed observation interval. Preserve all eligible coins, including coins that receive no qualified buy.

Use raw volume, raw volume/market cap, distinct buyer and seller addresses, buyer arrival, buy/sell notional, concentration, and executable depth.

Measure short windows beside the m5 baseline. Freeze window definitions before evaluation; observation windows do not impose position exits.

Keep receipt and event clocks. Reject future information from a historical decision state. Keep unknown fields explicit.

Reuse pool-level arithmetic only where its units, event identity, and direction apply. Do not sum route legs into duplicate economic volume.

**Tests before implementation:** Delayed swaps cannot change a past state. Duplicate logs cannot increase volume. Missing actors cannot become unique buyers.

Also test chain isolation, reserve age, unavailable feeds, and unchanged funded admission. Use the existing tape tests for regression coverage.

**Acceptance:** A saved coin state predates the confirming event, and the same method records coins without that event.

## Stage 3: Test trader confirmation and demand failure

Start with the historical candidate: raw m5 turnover from 10% inclusive to 50% exclusive, and source buys from $1,000 inclusive to $10,000 exclusive.

These are candidate features. Do not assume a popular trader or this band guarantees a +25% move.

Compare similar coin states with and without a subsequent qualified buy. Fix the forecast interval before the evaluation starts.

Compare buyer arrival and net flow before and after the buy. Separate trader selection of demand from demand that follows the trader.

Fit a small demand-failure rule from buyer arrival, buy flow, sell pressure, and depth. Test the frozen rule on later observations.

Evaluate healthy swap silence and sustained weak buy flow separately. A provider failure cannot trigger either condition.

**Acceptance:** Report exact-quantity exits, costs, open inventory, unavailable routes, and the result without the largest trader and coin contributions.

The 39 historical trades used different exits. Their PnL cannot validate this policy.

## Stage 4: Wire the measured policy into the existing executor

Prepare route metadata when a coin qualifies. Recheck current demand and the executable quote when an authorized source buy arrives.

Apply the shared authority, route, exposure, sizing, and reservation checks. Do not buy merely because a coin enters the attention list.

Assign a new frozen policy identity to new positions. Audit every inherited exit branch for hidden price or age exits.

Submit a confirmed demand exit through the existing durable full-close path. Preserve manual close, transaction identity, and unknown-confirmation recovery.

**Tests before activation:** Prove source confirmation, contract consistency, absence of hidden stop/time exits, feed-failure behavior, and one close transaction per claim.

**Acceptance:** Require later-period evidence, a resolved +25% sale choice, explicit funded activation, and verification of the active runtime.

Only the first stage is fully scheduled here. Subsequent code changes depend on measured coverage and calibrated demand thresholds.
