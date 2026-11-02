# Radar Confirmation Microscope Design

**Status:** Approved on 2026-07-28. The user authorized implementation, testing, merge, push, and
operational verification without intermediate approval pauses.

## Goal

Turn the current Pump.fun Confirmation result into an honest paper-policy experiment. Measure whether
a $25 executable entry can realize a net return before the opportunity decays, identify which
point-in-time mechanics improve that result, and keep the worker and browser bounded while evidence
accumulates.

The working hypothesis is sustained independent demand after meaningful curve progress but before the
wider crowd arrives. The system's speed advantage is measured by comparing the exact Confirmation
receipt with the same launch delayed by one eligible event.

## Boundaries

- Capture and paper research only. No funded imports, signing, submission, reservations, positions,
  sizing, or funded-runner changes.
- Existing immutable receipts are never relabeled or rewritten. Corrected economics and new arms use
  new evaluator and rule versions forward only.
- Chain events remain the decision clock. Remote services enrich or verify; they do not create a
  launch or manufacture event order.
- X, wallets, creator reputation, and social metadata remain context. None is a primary trigger.
- Do not require statistical significance or 200 positives to continue learning. Review descriptive
  evidence at 25, 50, and 100 completed entries per arm.

## Current evidence and interpretation

The retained ledger reports that `pump-confirmation-v1` reached an executable +12% target within 60
seconds on 33 of 72 scored receipts, versus 258 of 1,191 route-control receipts. At 300 seconds the
counts were 43 of 72 versus 437 of 1,191. Almost every Confirmation launch also received a route
control receipt, so the arm is selecting from the same executable launch population.

This is target-hit evidence, not policy profit. Static venue fees, mixed SOL/USDC quote assets,
protocol-generated Mayhem flow, and incomplete exact 60-second liquidation values can bias the
economic result. Those defects must be fixed before narrowing the entry rule.

## Economic truth

### Quote asset

`LaunchState` persists `quote_mint`, `quote_decimals`, and `quote_usd_price_micros`. Pump SOL and USDC
curves therefore use their own atomic scale and USD conversion. A missing or unsupported quote asset
blocks local executable math with `UNSUPPORTED_QUOTE_ASSET`; it never falls back to SOL.

The Pump decoder already emits `quote_mint`. Launch and state restoration carry it forward. Market
inputs expose asset-specific prices without adding per-event network requests: SOL uses the existing
cached SOL/USD feed; USDC uses a configured one-dollar value and six decimals.

### Effective fee

`LaunchState` persists the latest decoded protocol and creator fee basis points. Executable entry and
exit math uses their sum when both are known. The Pump fallback becomes 125 bps, matching the current
bonding-curve schedule, and is used only before a decoded fee-bearing trade establishes the state.

Mayhem agent trades may be fee-free. Their zero fee is an observation about that agent transaction,
not a new market-wide fee. They do not replace the curve's effective fee for our hypothetical entry.

Every corrected path is written as `opportunity-path-v3`; every new forward alert is a new rule
version. Historical `opportunity-path-v2` rows remain auditable but cannot be pooled into the new
policy-return headline.

## Independent-demand state

Pump launch facts persist `mayhem_mode` and `cashback_enabled`. The published Mayhem agent identities
are classified as protocol agents. Protocol-agent buys and sells remain in total reserve and market
flow because they move the curve, but they are excluded from:

- effective independent buyer count and HHI;
- independent capital and buyer velocity;
- buyer retention;
- buy-size distribution features.

The state also retains interval-safe facts needed at the alert timestamp: independent buyers added,
independent buy count and capital, protocol-agent flow, creator flow, sell pressure, buyer HHI change,
and buyer retention from the preceding landmark. No feature reads a future event.

## Immutable alert evidence

Each new forward alert stores one compact, versioned feature snapshot at the exact decision timestamp.
The snapshot includes:

- venue, quote mint, effective fee, Mayhem and cashback modes;
- launch age and curve progress;
- independent buyer count, new buyers, HHI, HHI change, and retention;
- independent capital velocity and acceleration;
- sell pressure and creator-sale state;
- remote quote drag, quote age, quote completion latency, and entry-effective time.

Feature snapshots are stored with the alert, projected once into the retained insight ledger, and
never reconstructed from a later launch state.

## Paper policy result

The primary policy is frozen per alert:

- clip: $25;
- entry: exact remote Confirmation receipt plus measured entry-effective latency;
- profit exit: first executable return at or above +12%;
- stop exit: first executable return at or below -8%;
- time exit: a fresh acquired-quantity sell computed at 60 seconds from the latest finalized chain
  state;
- failure exit: deterministic route failure or repeated transient failure under the existing outcome
  contract.

Fees, self-impact, network cost, and exit impact are included. A quiet launch receives a new due-time
quote computation at 60 seconds from the last finalized state while the source remains healthy. This
does not require another RPC request: in the absence of a later chain event, the reserves have not
changed. The last earlier observation cannot masquerade as the time exit. If a fresh due computation
cannot be produced, the result is `DATA_INVALID` or `EXIT_FAILED`, not `TIMEOUT` with a blank return.

The retained result stores `realized_policy_return_bps`, `exit_reason`, `exit_elapsed_ms`, MFE, MAE,
and time to best executable return. UI language is `WIN`, `LOSS`, `60S EXIT`, `EXIT FAILED`,
`DATA INVALID`, or `MEASURING`.

## Forward shadow arms

`pump-confirmation-v1` remains unchanged as historical context. New forward-only arms are:

1. `pump-confirmation-truth-v2`: the same Confirmation shape under asset-aware fees and protocol-agent
   filtering. This is the primary baseline.
2. `pump-confirmation-independent-v1`: requires at least two new independent buyers in the current
   interval.
3. `pump-confirmation-clean-v1`: independent condition plus non-worsening HHI, sell pressure at or
   below 25%, and no creator sale.
4. `pump-confirmation-low-friction-v1`: truth-v2 plus immediate quote drag no worse than four times the
   current round-trip fee floor. This adapts to fee mode without a static venue threshold.
5. `pump-confirmation-delayed-v1`: records the first otherwise eligible event after a truth-v2 alert.
   It is never presented as a live candidate; it measures the value of reaction speed.

One launch may produce the truth baseline and its applicable shadow receipts. Each rule produces at
most one receipt per launch. Shadow arms do not cause extra remote quote requests: they reuse the
verified quote and state already obtained for the primary decision event.

## Evaluation

The main comparison is paired by launch:

- truth-v2 versus route control;
- independent, clean, and low-friction versus truth-v2;
- immediate truth-v2 versus delayed.

For each checkpoint report completed entries, realized return sum and mean, median, win/loss/60-second
exit counts, exit failures, invalid data, +12% hit rate, drawdown, and latency. Never rank an arm by
hit rate alone. Show all checkpoints, including negative ones.

At 25, 50, and 100 completed entries an arm remains exploratory when mechanically valid. It becomes a
promising paper direction when cumulative realized return is positive, it improves over its paired
baseline, and one creator or launch mode does not contribute more than half of the gain. This is not a
funded-promotion gate.

## Bags and Robinhood

Bags/Solana remains coverage work until its source is healthy and exact DBC quote math produces
outcomes. Its future model must persist `dbcConfigKey`, initial buy, creator/fee-share identities, and
the config-specific pre-migration fee. It cannot inherit Pump thresholds.

Robinhood remains a coverage and latency lane until actual Bags launches are captured. Its sequencer
feed may prewarm state and measure arrival latency, but no Robinhood arm is created without launch
episodes and executable outcomes.

## Runtime and application performance

### Worker budget

- No new RPC call occurs for feature computation or shadow-arm classification.
- A launch has at most one due-time quote computation per entry and horizon. Repeated sweeps deduplicate
  by entry and due horizon.
- Due-time quote work is local curve math over finalized captured state. It does not use the RPC or
  quote governor and never raises configured RPS or concurrency.
- Active feature state is bounded by active retained launches and removed with the existing launch
  quarantine/retention lifecycle.
- Feature snapshots are compact JSON with a 4 KiB encoded ceiling. Oversize snapshots fail closed and
  record `FEATURE_SNAPSHOT_OVERSIZE`.

### Storage budget

- At most one feature snapshot per alert and one policy result per alert.
- Insight projection copies the compact alert snapshot, not the full state/event history.
- Existing retention deletes projected hot rows only after durable insight projection and continues
  protecting active frontiers.
- The hot database soft and hard caps remain unchanged. New tables participate in launch-scoped
  deletion and orphan checks.

### API and browser budget

- The compact Radar endpoint remains below 150 KiB for a representative maximum fixture.
- It exposes aggregate policy comparisons and at most 16 recent result receipts. Full evidence stays
  behind explicit evidence and launch-detail endpoints.
- SSE market digests exclude clock-only ages, heartbeat counters, and retained aggregate changes that
  do not alter the operator decision. Health-only refresh remains capped at five seconds.
- The browser keeps stable keyed DOM nodes for dots and result rows. It patches changed text/classes;
  it does not replace the Radar root, evidence panels, or open `<details>` elements.
- Rendering is coalesced to at most once per animation frame with a 250 ms minimum interval. Hidden or
  inactive Radar tabs close SSE and cancel pending renders.
- A development-only render metric records payload bytes, render duration, changed row count, and
  skipped identical snapshots without sending telemetry.

## Operator surface

Above the visualization, one sentence answers: `ACTION NOW`, `WATCH`, or `NOTHING ACTIONABLE`.
Immediately below it, the Confirmation Microscope shows:

- completed entries at the current checkpoint;
- realized paper return for truth-v2 and route control;
- immediate-versus-delayed difference;
- the best separating shadow condition;
- explicit data-invalid and exit-failure counts.

Recent receipts use plain outcomes and percentages. `TIMEOUT`, evaluator names, and raw basis-point
jargon remain available only in expanded evidence.

## Error handling

- Unknown quote asset, missing effective fee, protocol-agent classification uncertainty, stale remote
  quote, missing due-time quote, and source gaps fail closed for policy scoring.
- A single venue gap freezes only that venue. Healthy venue dots and evidence continue updating.
- Projection errors do not block capture; they keep hot evidence and expose a degraded insight status.
- UI rendering errors preserve the last good frame and mark it stale instead of clearing the board.

## Verification

- Decoder/state tests for SOL and USDC quote assets, dynamic fees, mode persistence, state restoration,
  and protocol-agent filtering.
- Curve/outcome tests for 125 bps fallback, fee-bearing versus fee-free agent events, exact 60-second
  due computations, route failure, and immutable evaluator separation.
- Directional/engine tests for every shadow arm, once-per-launch receipts, delayed pairing, and zero
  additional remote probes.
- Store/retention/insight tests for compact feature snapshots, launch deletion, projection, paired
  policy metrics, and evaluator isolation.
- API tests for the 150 KiB compact ceiling and bounded recent rows.
- JavaScript contract plus browser test for stable dots, preserved open panels, inactive-tab cleanup,
  and bounded render cadence.
- Focused Radar pytest suite, Python compile, JavaScript syntax, schema-reference validation, boundary
  guard, and `git diff --check` before merge.
