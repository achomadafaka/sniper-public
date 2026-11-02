# Solana Swap-Mark Fastlane Design

Date: 2026-08-12
Status: Approved design

## 1. Problem

The memecoin UI polls the real-position ticker once each second. The ticker API responds in less than 15 ms in the measured sample.

The funded fastlane also writes a Solana mark about once each second. However, DexScreener changes the selected price about once every 30 seconds.

The current worker writes the repeated provider value with a new `sweep_ts`. The UI therefore shows a fresh quote age for an unchanged provider value.

The delay is in the Solana price source. It is not in the browser, the ticker API, or SQLite.

## 2. Goals

This change has these goals:

1. Deliver a confirmed Solana swap mark to the funded fastlane within two seconds.
2. Deliver the same mark to the existing UI ticker within two seconds.
3. Keep exact whole-bag exit quotes authoritative for funded exit confirmation.
4. Keep `executable_peak_price` dependent on exact whole-bag quotes.
5. Keep the existing DexScreener mark as the fallback.
6. Store the source observation time instead of a repeated worker time.

The two-second target starts when the read-only WebSocket receives a valid two-sided swap update. It ends when the UI can read the new DB mark.

## 3. Non-Goals

This change does not do these actions:

- It does not infer a price from pool reserve ratios.
- It does not increase the Jupiter exact-quote cadence.
- It does not change a funded exit threshold.
- It does not change the stop or trail confirmation rule.
- It does not change the hot exit engine.
- It does not add funded authority.
- It does not support a native-SOL Pump curve.
- It does not deploy or restart a process.

Native-SOL Pump curves keep the DexScreener fallback. Unknown pool types also keep the fallback.

## 4. Selected Approach

The runner will use confirmed vault swap deltas as a live last-trade mark.

The existing read-only Solana WebSocket already subscribes to token vault accounts. It receives confirmed account updates without a custody RPC or a key.

A valid two-sided update gives the token delta and the quote-asset delta. Their decimal-adjusted ratio gives the confirmed swap price in the quote asset.

This method works for constant-product and concentrated-liquidity pools. It does not use the unsafe reserve-ratio assumption.

## 5. Components

### 5.1 WebSocket balance parser

`sniper/services/memecoin_solana_ws.py` will retain the token decimal value from each account notification.

`VaultTape` will attach both decimal values to each complete two-sided row. Existing hot-engine amount and sign behavior will stay unchanged.

A missing decimal value will not stop the hot tape. It will only block price calculation for that row.

### 5.2 Swap-mark cache

A small thread-safe cache will own the latest Solana swap mark for each `(network_id, mint)` key.

The cache will accept a row only when all these conditions are true:

- `kind` is `sol_swap`.
- Both deltas are nonzero.
- The deltas have opposite signs.
- Both decimal values are integers from 0 through 18.
- The pool has a durable position binding.
- The quote mint is WSOL or USDC.

The cache will store the quote price, the quote mint, the pool, the slot, and the corrected receipt time.

The cache will clear its active rows when the WebSocket disconnects. A disconnected socket cannot supply a fresh mark.

### 5.3 Price-only WebSocket worker

The runner will start one price-only WebSocket worker when the funded fastlane is active.

The worker will use `MEMECOIN_SOLANA_READ_RPC_URL`. It will never use `MEMECOIN_SOLANA_RPC_URL`.

The worker will read `sol_position_pools` and `sol_pool_vaults` through a read-only Solana tape connection. It will not write either SQLite database.

The worker will subscribe only to open funded Solana positions with a durable WSOL or USDC vault binding.

The worker will rebuild its subscription book every 30 seconds. A new binding can join on the next book rebuild.

The worker will retry after a socket failure. The existing DexScreener path stays active during the failure.

### 5.4 Funded fastlane merge

The existing one-second fastlane remains the only owner of mark writes and funded exit sweeps.

The DexScreener batch will also request the required quote mints. This keeps the request count at one batch per fastlane pass.

The fastlane will reject a cached mark when its quote-to-USD reference is not positive and finite.

For a valid swap row, the cache will calculate `quote_per_token`. The fastlane will calculate `price_usd`.

```text
quote_per_token =
    abs(quote_atomic) / 10^quote_decimals
    -------------------------------------
    abs(token_atomic) / 10^token_decimals

price_usd = quote_per_token * quote_usd
```

The fastlane will use a cached swap mark only when its age is five seconds or less.

A fresh swap mark will replace the DexScreener mark for the same Solana position. An exact EVM route mark keeps its current precedence.

When the swap mark is missing or stale, the fastlane will use the existing DexScreener mark.

The fastlane will pass the selected mark to `real_exit_sweep`. This lets the funded rails read the faster nominal price.

### 5.5 Observation timestamps

Each mark can provide an `observed_at` value. `write_marks` will use this value as `sweep_ts` after timestamp validation.

Existing sources without `observed_at` will keep the current sweep time.

The DexScreener fastlane will retain one observation time while its price, liquidity, and market-cap values stay unchanged.

A changed DexScreener value will receive a new observation time. A repeated value will not create a false fresh mark.

A repeated cached swap mark will keep its original receipt time. SQLite uniqueness will prevent a duplicate row.

## 6. Price Precedence

The Solana mark order is:

1. A swap mark that is five seconds old or less.
2. The current DexScreener batch mark.
3. The existing exact preview fallback when the nominal mark is absent.

The UI will continue to read the newest admissible row from `memecoin_position_marks`.

No browser timer or ticker API change is necessary.

## 7. Funded Safety

The new source name is `fastlane_sol_swap`. Its quality is `live`.

The source is a nominal last-trade price. It is not an executable whole-bag price.

A nominal mark can nominate a stop, catastrophe, de-risk, or trail check. It cannot authorize a funded exit.

Every funded executable reason will still call `executable_exit_allowed`. That function will still require a fresh exact whole-bag preview.

Only an exact whole-bag price can update `executable_peak_price`. The new mark can update only the nominal display peak.

The existing spike quarantine will run before mark persistence and before a funded sweep uses the mark.

The exact-quote demand budget will remain active. A fast nominal move can request a quote, but it cannot remove the budget.

## 8. Failure Rules

The lane will fail to DexScreener for these conditions:

- The WebSocket URL is missing or invalid.
- The WebSocket disconnects.
- A pool binding is missing.
- A decimal value is missing.
- A quote mint is unsupported.
- A quote-to-USD reference is missing.
- A swap row is malformed.
- The cached mark is more than five seconds old.

The lane will log the fault class without a secret or an endpoint value.

A WebSocket fault will not stop the funded runner. It will not stop the slow funded exit fallback.

## 9. Configuration and Rollback

`MEMECOIN_SOLANA_SWAP_MARK_ENABLED` will control the lane. Its default value will be `1`.

The lane will run only when `MEMECOIN_FASTLANE_ENABLED` is also enabled.

An operator can set `MEMECOIN_SOLANA_SWAP_MARK_ENABLED=0` to restore the current DexScreener-only Solana path.

The setting will not change the funded authority state. It will only select a nominal mark source.

## 10. Tests

Focused tests will prove these items:

1. The WebSocket parser retains token decimals.
2. `VaultTape` preserves existing sign and amount behavior.
3. The cache calculates WSOL and USDC swap prices correctly.
4. The cache rejects missing decimals and malformed deltas.
5. A fresh swap mark replaces the DexScreener mark.
6. A stale swap mark falls back to DexScreener.
7. A disconnect invalidates the swap cache.
8. Repeated provider values keep one observation time.
9. `write_marks` stores a valid source observation time.
10. A nominal swap mark cannot update `executable_peak_price`.
11. A funded exit still needs exact whole-bag confirmation.
12. The runner uses the read-only Solana endpoint for the price worker.

The focused suite will include the current Solana WebSocket, fastlane, peak, and funded-exit tests.

## 11. Acceptance Criteria

The implementation is acceptable when all these conditions are true:

- A valid WebSocket swap reaches the mark cache immediately.
- The next one-second fastlane pass writes that observation.
- The next one-second UI poll can read that mark.
- The stored timestamp is the source receipt time.
- An unchanged DexScreener response does not receive a new timestamp.
- A stale or failed WebSocket lane falls back to DexScreener.
- No new exact-quote loop runs for each open Solana position.
- The existing funded exact-confirmation tests pass.
- No deployment or process restart occurs without separate approval.

## 12. Rollout Evidence

A later pinned deployment must measure these values before it claims success:

- The WebSocket receipt-to-mark latency count, median, and p90.
- The mark-to-ticker visibility latency count, median, and p90.
- The swap-mark coverage count for eligible open positions.
- The stale-cache fallback count.
- The WebSocket reconnect count.
- The exact-quote request count before and after activation.
- The funded exit audit result for each nomination from `fastlane_sol_swap`.

The target is at most two seconds from a valid WebSocket receipt to UI visibility. A test fixture does not prove this live target.
