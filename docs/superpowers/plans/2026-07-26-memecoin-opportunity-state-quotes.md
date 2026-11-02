# Memecoin Opportunity State, Quotes, and Direction Plan

**Goal:** Turn canonical events into honest point-in-time launch state, self-impact-aware clip quotes, landmarks, and transparent Level-1 Radar zones.

**Architecture:** One reducer per venue feeds a shared immutable snapshot contract. Curve math is venue-specific. A shared feature layer computes kinetics, effective demand, concentration, creator risk, route state, and quality. The Level-1 rules from the approved design map eligible snapshots to `OUTER/TRACKING/APPROACHING/INNER/CENTER` without a weighted score or sample floor.

**Constraints:** Integer atomic math; no future state; no raw-wallet-count trigger; no remote quote until `INNER`; missing prior state blocks acceleration claims; creator sales and gaps are risk interrupts; `$25` is primary and `$5/$10/$50` are diagnostics.

## Tasks

1. Extend the opportunity schema with immutable state snapshots, entity edges, quote observations, predictions/verdicts, runtime heartbeat, and model-registry metadata using explicit schema migration.
2. Implement Pump constant-product and Bags Robinhood constant-product quote math. For Meteora DBC, require the supported curve segment/config snapshot and fail closed on unsupported curve shapes.
3. Implement venue reducers and state restoration from the ledger, including launch lifecycle, reserves, progress, buys/sells, creator-linked activity, and source quality.
4. Implement observed-signer breadth, same-actor deduplication, balances, HHI, and creator-linked sales. Keep common-funder/bundle clustering out of Level 1 until that evidence is actually captured.
5. Materialize one state per crossed landmark, compute interval kinetics, self-impact entry plus acquired-quantity exit, and persist all clip diagnostics.
6. Implement the approved Level-1 rules and explanation facts. `CENTER` records `WOULD_FIRE`; it never opens a paper or funded position.
7. Add counterfactual replay/outcome finalization and completeness/evidence summaries. Data failures censor; migration ends v0 at the last executable state.

## Verification

```bash
python3 -m pytest tests/test_memecoin_opportunity_store.py tests/test_memecoin_opportunity_curve_math.py tests/test_memecoin_opportunity_state.py tests/test_memecoin_opportunity_directional.py tests/test_memecoin_opportunity_projection.py -q
```
