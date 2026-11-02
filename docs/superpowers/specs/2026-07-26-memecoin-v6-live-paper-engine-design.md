# Memecoin V6 Live-Paper Position Engine — design

**Date:** 2026-07-26
**Status:** approved direction
**Scope:** live paper only; Solana and Robinhood chain 4663; default OFF
**Supersedes:** the V5-as-final doctrine and the replay/V5-comparison evaluation contract in
`2026-07-25-memecoin-v6-break-manager-v0-prereg.md`

## Decision

V6 is the only candidate exit-engine lineage. It is tested by giving it realistic, live mock
positions and letting it own each position from the completed paper fill until close. V5 is not a
comparison arm. Historical replay is not the acceptance harness.

The lifecycle boundary is exact:

```text
accepted upstream signal -> realistic live paper fill -> V6 owns the position -> modeled live exits
```

V6 has no authority over signal selection, entry admission, entry sizing, or entry fills. Once a
fill exists, no legacy TP, stop, trail, mirror, timecap, catastrophe rule, V5 rule, or other strategy
may act on that mock position. V6 alone returns `HOLD`, `DERISK`, or `EXIT_FULL`. A poor hold through
a rug, an unfilled exit, or an indefinitely open bag is evidence about V6 and must not be hidden by
an external rescue rail.

The existing funded runner remains unchanged and pinned during this experiment. Its current rails
continue to protect actual funded positions, but they never read or mutate the V6 paper book. No
result from this live-paper arm automatically grants funded authority.

## Goals

1. Open V6 paper positions from real signals and live executable entry quotes.
2. Let V6 manage several positions concurrently across Solana and chain 4663.
3. Feed V6 production-shaped, point-in-time chain, route, flow, wallet, and quote observations.
4. Model every V6 partial/full exit from a fresh executable sell quote, never a chart mid.
5. Persist the complete lifecycle: entry evidence, observations, thoughts, decisions, attempted
   actions, fills, remaining inventory, realized PnL, and terminal outcome.
6. Show these positions in a clearly separated **V6 Paper** section of the Real Positions tab.
7. Expose strengths and weaknesses immediately from live positions without waiting to build a
   historical replay corpus.

## Non-goals

- No funded signing, approval, broadcast, reservation, balance, exposure, or production-policy
  mutation.
- No V5 live or replay arm.
- No duplicate control/challenger positions for the same signal.
- No replay harness, outcome reconstruction, or synthetic historical point-in-time inputs.
- No external exit rail on a V6 paper position.
- No claim that a quote-only fill equals an on-chain fill. It is a production-shaped paper estimate.
- No automatic funded promotion, regardless of sample size or PnL.

## Chosen architecture

### Dedicated live-paper book

Use a dedicated SQLite database under the mutable state root, separate from both
`memecoin_positions` and the exit-state/tape databases. It owns V6 paper positions, modeled fills,
point-in-time observations, decisions, and worker cursors.

This is isolation, not a second product surface. The Real Positions tab reads the V6 database and
renders a separate section. Dedicated persistence is required because:

- `memecoin_positions.execution_mode='paper'` is consumed by legacy paper exit sweeps;
- `execution_mode='real'` is consumed by funded controls, reconciliation, and manual actions;
- adding a third mode would require auditing every broad `!= 'real'` reader and still leave an easy
  path for cross-book corruption;
- one database gives V6 atomic position/fill/decision transitions without cross-DB action commits.

The V6 database must never contain funded credentials, signed payloads, transaction identities, or
reservation rows. The funded runner must never open it for writing.

### Components

1. **Live-paper entry adapter** — receives an already accepted upstream buy signal, requests one
   live executable buy quote for the configured paper notional, and emits an immutable filled-entry
   record. It is not part of V6.
2. **V6 position engine** — the only post-fill decision policy. It consumes the prior immutable V6
   state plus one point-in-time observation and returns the next state, action, fraction, reason
   codes, and a concise human-readable thought.
3. **V6 live-paper worker** — owns the V6 database, discovers new filled entries, tracks every open
   V6 position, supplies observations to the engine, and applies modeled fills idempotently.
4. **Chain observation adapters** — reuse the existing Solana tape and EVM tick/market-state
   machinery. They may extend capture eligibility to V6-held pools/vaults, but cannot add exit
   decisions.
5. **V6 paper executor** — converts `DERISK`/`EXIT_FULL` into a fresh whole- or partial-bag
   executable sell preview and records the modeled fill. It never signs or broadcasts.
6. **Read-only API/UI** — exposes the V6 book in the Real Positions tab without mixing it into the
   funded endpoint's existing `positions` array or enabling funded buttons.

Rejected alternatives:

- Reuse the legacy paper book: existing strategy sweeps could close or mutate V6 positions.
- Store mock rows as real: funded reconciliation/manual execution could touch them.
- Shadow actual funded positions only: that cannot open independent mock positions or grade V6's
  partial inventory and terminal outcome.

## Entry contract

The engine begins only after the entry adapter has produced a fill. The immutable filled-entry
contract carries at least:

- stable entry id and origin event id;
- corrected-UTC signal, quote, and fill timestamps;
- `network_id`, mint, ticker, source wallet id/handle, and signal source;
- configured paper notional, modeled filled quantity, unit entry price, and quote asset;
- quote input/output amounts, route target/pool or vault binding, quote id/provider, and quote age;
- modeled fees/slippage assumptions and whether net PnL can be computed honestly;
- entry market cap/liquidity when observed, with missing values left null;
- V6 policy id, policy hash, and complete frozen parameter document.

The entry adapter opens nothing when chain identity is unresolved, the executable route quote is
missing, the quote is stale, or the modeled quantity is non-positive. It does not fall back to the
source trader's price, DexScreener mid, GeckoTerminal mid, or a cross-chain guess.

One entry quote is enough. Do not build a simulated mempool or block-latency harness. The UI and
reports label the result `modeled_live_quote`, not `actual_fill`.

Dedup is exact on `(origin_event_id, network_id, mint, policy_id)`. V6 has an independent,
configurable maximum-open-position limit and supports more than one simultaneous position. The cap
is an experiment capacity control only; it is not a trading signal or engine decision.

## Post-fill ownership and engine contract

V6 is a pure transition:

```python
step(state: V6PositionState, observation: V6Observation) -> V6Decision
```

`V6Decision` contains:

- `action`: `HOLD | DERISK | EXIT_FULL`;
- `fraction_of_remaining`: `0` for hold, `(0, 1)` for derisk, `1` for full exit;
- `state_if_filled`: immutable successor after the requested modeled fill is applied;
- `state_if_unfilled`: immutable successor when no executable fill is obtained; it may retain
  observation history and a pending-action token, but may not consume a tranche or reduce inventory;
- `reason_codes`: stable machine-readable codes;
- `thought`: concise operator-facing explanation derived only from current/prior inputs;
- `input_fingerprint` and `policy_hash`.

`thought` is a deterministic rendering of the engine decision and its reason payload. It is not a
separate online LLM opinion that can drift from the action.

The complete V6 engine owns the current break-manager logic and every later post-fill rule. The
current `memecoin_v6_policy.py` is only the break-manager seed; its statement that callers own hard
rails is superseded by this design and must be removed when the complete engine is built.

There is no caller-side TP, stop, trail, mirror, timecap, catastrophe, LP-pull, or unsellable action.
Those facts may be observations and reason inputs. Only V6 may turn them into an action.

Missing or incomplete data is part of the observation. If V6 refuses to decide, the persisted
decision is `HOLD` with an explicit data-insufficient reason. The worker may not invent a default
exit. This makes capture failures and policy blindness visible in performance.

## Live observations

Each decision point freezes the facts available at that corrected-UTC instant:

- executable whole-bag sell price/proceeds and quote age/status;
- current remaining quantity, cost basis, realized PnL, and executable high-water;
- held time, drawdown, break/prior-low state, realized volatility, and chain trailing p90;
- decoded buy/sell counts, arrival rate, buy share, and net flow;
- route degradation, sellability/quote failures, venue/pool/vault identity;
- LP/depth changes and tape completeness/provenance;
- source-wallet holding/exit state and frozen wallet-tier adjustment;
- chain id, block/slot identity, source event time, and capture version.

No field is backfilled from future data. Missing values remain missing. Every observation is written
before its decision and is content-fingerprinted.

The worker consumes event-driven updates when available and performs a bounded periodic catch-up so
quiet positions still advance. Both paths call the same `step()` implementation. Solana and 4663
may have different capture adapters but share one V6 policy and one decision schema.

## Modeled exits

For `DERISK` or `EXIT_FULL`, the worker requests a fresh executable sell preview for the exact
remaining quantity multiplied by the requested fraction. A modeled fill records the preview's
expected output, quote age, route, fees, and resulting unit price.

- No fresh executable preview: record `action_unfilled` and keep the inventory open.
- Partial fill: atomically append the fill, realize that slice, reduce remaining quantity, and
  persist `state_if_filled`.
- Full fill or dust remainder: atomically append the fill and close the position.
- Repeated worker delivery: the stable decision/action token returns the existing result and never
  books a second fill.

An unfilled action persists `state_if_unfilled`. It cannot consume partial budget, mark a tranche as
sold, or close the position. The next observation returns to V6 with the pending/unfilled outcome so
V6—not the worker—decides whether to retry, hold, or choose another action.

The outcome report separates gross price PnL, modeled route/impact cost, known fees, and net modeled
PnL. Unknown costs remain unknown; they are never silently set to zero in a net claim.

An operator may terminate a broken experiment position, but that writes `censored_operator` and is
excluded from engine performance. Manual termination is not an engine exit.

## Persistence invariants

The dedicated database contains focused tables for:

- immutable filled entries;
- current positions and immutable policy identity;
- append-only observations;
- append-only decisions/thoughts;
- append-only modeled fills;
- idempotent action claims;
- worker/capture cursors.

Required invariants:

- entry, policy, observation, and action identities are immutable;
- one open position per exact entry/policy identity;
- one applied fill per action token;
- position quantity equals entry quantity minus applied exit quantities;
- realized PnL reconstructs exactly from fills;
- a closed position has zero/dust remaining quantity and one terminal reason;
- every decision references an existing prior state and observation;
- no V6 table is read by funded reservation, reconciliation, or execution code.

## Real Positions tab

Add a separate **V6 Paper** section to the existing Real Positions tab. Do not merge mock rows into
the funded `/api/memecoin/real/positions` response.

Each position shows:

- prominent `V6 PAPER — NO FUNDS` badge;
- chain, token, source signal, modeled entry, age, remaining quantity, and modeled PnL;
- current executable sell value and quote freshness;
- latest V6 action/thought and the exact facts behind it;
- expandable observation/decision/fill timeline;
- open, action-unfilled, closed, or censored state.

Funded Buy, Sell, Mirror, override, and kill-switch controls are structurally absent from V6 rows.
The only operator action is `Censor paper position`, with confirmation and an audit reason.

## Evaluation

The experiment begins as soon as the worker is explicitly enabled and the first qualifying live
fill is available. There is no minimum replay corpus and no hidden-results floor.

Report continuously and by closed cohort:

- opened/open/closed/censored/unfilled-action counts by chain;
- gross and net modeled PnL where net is honestly computable;
- winner rate, median outcome, worst outcome, worst decile, and drawdown;
- winner retention and loss containment;
- time to first action, action count, partial inventory path, and hold duration;
- quote/capture availability and data-insufficient decisions;
- contribution concentration and results with top winners removed;
- reason-code outcomes and representative decision timelines.

The V6 operator surface shows V6 lifecycle evidence only. It does not add historical arms, paired
deltas, replay output, or V5 context. Any later cross-strategy analysis is a separate research task
with its own fill-compatibility contract.

The operator reviews strengths and weaknesses from actual live-paper lifecycles. Any later funded
promotion requires a separate explicit decision, funded integration design, current reconciliation,
and deployment gates. This experiment never flips funded authority.

## V5 and prior-plan disposition

- V5 is retired as a candidate and receives no tuning, live arm, replay arm, or new policy registry
  entry.
- Engine-agnostic capture, market-state, exact-quote, tx-grounded, LP/depth, hot-lane, and fill
  plumbing remains because V6 live paper consumes it.
- Do not add V5 audit, compatibility, selector, reporting, or recovery work to the V6 live-paper
  program. Stale candidate selectors may be removed separately; funded data-recovery changes remain
  a distinct funded-safety task and never make V5 a selectable engine.
- The current V6 break-manager module is retained only as input to the complete V6 position engine.
  The builder must remove its replay/funded-authority narrative and caller-owned-rails contract.

## Activation and failure safety

- Default OFF through a dedicated V6-paper enable flag.
- Separate worker/watchdog from the funded runner.
- Startup validates database path, policy hash, supported chains, positive concurrency cap, and
  quote/capture adapters before opening positions.
- A worker crash cannot change funded state. Restart reconstructs every open position from the V6
  database and resumes idempotently.
- Database or capture failure blocks new V6 entries and records/loudly reports degraded tracking; it
  does not invoke a fallback strategy.
- Deployment, restart, and live quote tests require explicit operator authority at implementation
  time. The design/plan phases perform no runtime mutation.

## Acceptance criteria

1. A synthetic accepted fill opens exactly one V6 position; V6 is not called before the fill.
2. Legacy paper and funded sweeps cannot discover or mutate a V6 position.
3. V6 alone determines every normal post-fill action.
4. Solana and 4663 positions receive production-shaped point-in-time observations.
5. Multiple positions advance independently and survive worker restart.
6. Every decision and modeled fill is reconstructable and idempotent.
7. An unfillable V6 exit remains open and visible instead of receiving a fallback close.
8. The Real Positions tab clearly separates funded and V6 paper positions and exposes no funded
   controls on mock rows.
9. Focused tests prove no signing, approval, broadcast, reservation, production-DB write, or funded
   policy activation is reachable from the V6-paper worker.
10. Reports use live-paper outcomes and honest fill labels; no replay/V5 superiority claim remains.

## Knowledge Sync

This is a doctrine and architecture change. Sync `AGENTS.md` and `CLAUDE.md`; supersede the V5-final
and V6 replay-comparison documents; add an `architecture` memory and retire the V5-final memory when
the operator explicitly authorizes the external Supabase write. The implementation PR must carry the
full Knowledge Delta Report.
