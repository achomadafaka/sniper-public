# Radar Actionability v2 Design

**Status:** Approved for implementation on 2026-07-27.

## Goal

Turn the Radar from a launch activity display into a paper-only executable-opportunity instrument. A coin moves inward when point-in-time demand, route quality, and recent trajectory improve. CENTER means the configured clip has a current read-only entry quote and an acquired-quantity sell quote. It does not mean a funded order.

## Scope

- Pump.fun on Solana, Bags on Solana, and Bags on Robinhood Chain 4663.
- Read-only chain events, quote requests, evidence persistence, UI alerts, and forward-paper receipts.
- Fixed exploratory clip: $25. Quote ladder remains $5/$10/$25/$50.
- Profit target diagnostics: +2%, +4%, +6%, +8%, and +12% over 5s, 10s, 20s, and 60s.
- Loss barrier: -8%. Migration, deterministic exit failure, and incomplete data remain competing failures.

## Non-goals and authority boundary

- No signing keys, funded balances, transaction construction, submission, positions, or funded-runner imports.
- No X or wallet-follow signal in the live decision path.
- No model or minimum-sample gate is required to emit an exploratory paper alert. Historical evidence grades alerts; it does not suppress observation during cold start.
- Bags launch-feed API supplies a forward-only pool allowlist and coverage audit. Exact chain
  notifications and transactions remain the event clock and event truth.

## Executability

Solana uses a dedicated opportunity-only Jupiter quote adapter. The adapter requests native SOL to token, then quotes the exact acquired token amount back to native SOL. It is paced, cached, deduplicated, timeout-bounded, and never requests a swap transaction.

The hot event path never waits on the provider. It enqueues one request per venue/token/clip/state and consumes a completed result at a later state evaluation. Pending, throttled, failed, and stale results are explicit.

Robinhood 4663 keeps its read-only exact route probe.

## Radar movement

The active surface contains only fresh launch states. Stale launches are counted as archived history and remain queryable.

Ranking order is:

1. zone distance;
2. inward velocity, measured from the prior persisted prediction;
3. data quality;
4. freshness.

The UI renders zone transition and inward velocity. Static proximity without improvement is not ranked above an equally close, rapidly improving launch.

## Evidence

Each matured counterfactual entry records:

- target-ladder result for every target and horizon;
- maximum favorable and adverse executable return;
- time to best executable return;
- the terminal competing outcome;
- the selected point-in-time feature state.

Near misses are entries that did not reach the selected target but produced positive executable excursion. They are ranked by best return and retain the blocker or terminal outcome.

## Forward-paper alerts

A fresh first transition to CENTER creates one immutable exploratory alert receipt per launch, rule version, and clip. Redelivery is idempotent. Alerts appear in the Radar UI/API only. They do not create a paper position or call funded execution.

## Bags coverage

When an API key is configured, the first launch-feed page establishes a forward watermark and the
current Bags pool allowlist. Only launches first seen afterward are capture obligations. Missing
forward launches open a coverage warning and queue a paced read of the feed's exact launch
transaction; that recovery receipt is non-actionable. This avoids replaying old feed rows and avoids
false gaps after hot retention. Without a key, coverage is `unmeasured`; this does not falsely
degrade the chain-native WebSocket lane.

## Resource controls

- One bounded quote worker and bounded dedupe cache.
- Separate Pump WebSocket plus four stable Bags pool shards. Each Bags socket stays below 50 active
  or in-flight subscriptions and never consumes Pump's subscription slot.
- At most 0.5 Solana provider requests per second by default.
- Bounded alert, target, near-miss, and history queries.
- Existing hot-ledger retention remains authoritative.
- No broad test suite or backtest is required for this change.
