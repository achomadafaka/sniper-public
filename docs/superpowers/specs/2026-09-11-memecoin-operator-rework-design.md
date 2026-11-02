# Memecoin operator interface and notification rework

Date: 2026-09-11. Base: `c44a26781980a90f0b2004813ba030bb10dd7aa4`.
Status: implementation authorized for the full Memecoin experience. The audit itself made no runtime changes.

## Scope and result

Replace the Memecoin operator experience across the browser, entry results, position values, charts, Telegram, and links.
The operator must see the same funded result for the same event on every surface.
Memecoin is the first complete scope. The operator can extend the scope to the other Sniper products.

The rework preserves entry eligibility, sizes, thresholds, exit policy, wallet authority, and transaction transport.
Notification eligibility can change where it currently hides an entry result after an earlier notification.
The operator requested no new backups. Deployment must use the supported backup-defer option.

## Verified defects

| Defect | Current evidence |
|---|---|
| Open settings stop full refreshes | `static/app.js:3710,5247`: an open, unkeyed System details element blocks the full refresh. |
| Full responses erase live price fields | `static/app.js:4583,5100`; `sniper/routes/api_memecoin.py:517`: the full and stream payloads have different fields. |
| Old prices can acquire new timestamps | `static/app.js:4577`: independent field fallback combines observations. Node reproduction confirmed the result. |
| Remaining exit value includes recovered cash | `static/app.js:4731`; `sniper/persistence/db_memecoin.py:6277`: total return supplies the exit-value label. |
| Small token prices display as zero | `static/app.js:2861,4768`: fixed decimals display `0.00000004` as `0`. |
| Market cap changes valuation basis | `sniper/routes/api_memecoin.py:475`: the ticker can derive market cap from an exit quote with sale impact. |
| Candle charts compress elapsed time | `static/app.js:7808,7878`: array indexes place candles and fill markers. |
| Paper activity looks like a funded entry | `static/app.js:5935,5955`: tracked wallets and paper actions lack the funded result beside them. |
| Unknown manual submissions display as refused | `sniper/services/memecoin_manual_intents.py:308`: every result except filled maps to refused. |
| Recovery is absent from old decision rows | `sniper/persistence/db_memecoin.py:9025`: the Decisions query does not reconcile reservations and fills. |
| Some submitted or terminal results produce no Telegram message | `sniper/services/memecoin_alerts.py:133`; `memecoin_real_runner.py:6887`: unknown results and some final retries are silent. |
| Message queues disagree about order and delivery | Source and operational messages use separate queues. Source deduplication precedes queue acceptance. |
| Transaction links are incomplete | `sniper/services/memecoin_alerts.py:65,181,425`: BSC has no entry link; exit messages truncate signatures. |
| Evidence labels conceal meaning | `honest alpha_b /18c` means 18 closes, not 18 coins. The receipt label uses message preparation time. |

The latest 100 operational delivery-audit rows contained 90 rows without source identity.
The remaining 10 rows were correlated REAL-FILLED messages. This sample does not measure all historical delivery loss.

The authenticated browser audit captured desktop and 390-pixel mobile views.
The mobile first screen places manual entry controls before positions and entry results.
The funded header also displays paper-position counts. This makes the scope unclear.
Baseline images reside in `output/playwright/ui-rework-audit/desktop-live.png` and `mobile-live.png` in the main checkout.

## Approach

| Approach | Result |
|---|---|
| Change labels and layout only | Leaves contradictory status copies and incomplete delivery intact. |
| Replace the full application stack | Adds migration risk without evidence that a new framework is necessary. |
| Replace the shared data contract and Memecoin presentation | Recommended. Reuses the ledger, chain registry, dispatcher, endpoints, styles, and action routes. |

One server projection supplies the browser and Telegram. The projection reads existing durable evidence.
It does not create a second trade state machine or a new event system.
The browser uses one state store for full responses and stream updates.
Separate render functions present positions, entry results, wallet authority, and research.

## Entry result contract

Identity includes the canonical chain and exact source event. Attempts, reservations, positions, and fills retain their own identities.
Reuse `entry_attempt_for_origin`. Never attribute a result by ticker, wallet, or mint alone.
Retain all retry attempts in the event detail. A later attempt must not erase an earlier refusal.

| Display state | Required evidence |
|---|---|
| Source received | A persisted source event exists; no funded attempt is attributed yet. |
| Evaluating | An attributed attempt exists without a terminal result. |
| Retry scheduled | Durable retry ownership remains active. Show its next eligible time when known. |
| Transaction unresolved | A signed identity exists, but execution remains unresolved. A signature alone does not prove submission. |
| Submitted, confirmation unknown | Transport evidence supports submission, but confirmation remains unresolved. This state cannot display Refused. |
| Bought | A matching confirmed fill exists. A recovered fill takes precedence over an old unknown attempt. |
| Refused | A terminal gate refusal exists, with no active retry or unresolved transaction. |
| Failed before submission | Failure evidence proves that no transaction was submitted. |
| Transaction failed | The chain confirms transaction failure. Preserve its identity and known fees. |
| Needs review | Evidence conflicts or accounting requires review. State the conflict. |
| Not evaluated | Evidence explicitly shows no funded evaluation. Missing data alone cannot prove this state. |

The view keeps source activity, paper action, funded attempt verdict, execution state, and notification delivery separate.
Manual entry status uses the same projection and exposes the stored attempt identity.
Do not display Quoting or Signing from elapsed time. Current stages persist at completion, so unfinished attempts support Evaluating only.
Show stale or missing evidence explicitly. Do not rewrite historical decisions from current settings.

Each refusal shows the tested value, effective threshold, units, and recorded reason.
Store the effective volume ceiling and tested source-adjusted volume in future trace evidence.
Historical missing fields remain unknown. A paper position can never establish Bought.

## Browser structure

Primary navigation: Live, Entries, Wallets, Research. System status remains reachable from every view.
Research contains the existing paper and Radar surfaces, with explicit paper labels.
Existing supported functions remain reachable during the replacement. Retired duplicate views redirect to the replacement.

```text
Memecoin                 Chain: All       Data age / connection state
Live     Entries     Wallets     Research                    System

Needs attention: unresolved submissions, stale prices, failed delivery

Open real positions                       Recent entry results
Token / chain / copied wallet              Token / wallet / chain
Remaining value / quote age                Bought / Refused / Unresolved
Recovered cash / total return              Tested value and threshold
Market price / exact sell quote            Source, receipt, and result times
Effective exit policy                     Open event detail

Wallet balances                           Manual entry
Cash, gas, reserved amount by chain        Deliberate action panel
```

On mobile, needs-attention items and positions precede recent entry results. Manual entry remains reachable with a labeled button.
Remove duplicate Live/OK summaries. Connection health does not prove fresh market data or successful Telegram delivery.
Use readable contrast and visible labels. Remove visual effects that reduce text clarity.
Use native buttons and keyboard focus. Partial-sale controls say Sell 25%, Sell 50%, and Sell all.
Preserve each existing action's authorization and confirmation behavior.
Actions display queued, submitted, unresolved, or completed from the returned operation identity. Disable duplicate submission while ownership remains active.
Opening settings or editing a form cannot stop unrelated status and position updates.

## Prices, accounting, and charts

Every price is one observation: value, timestamp, source, basis, and quality travel together.
The same projection supplies the full response and the stream. An older observation cannot replace a newer observation.
Missing observations clear the current value or retain the complete old observation with a stale label.
Never attach a fresh timestamp to an old price. A failed request retains the last successful refresh time.

Show market price and exact sell quote separately. Show the selected execution quote only when an exit attempt supplies one.
Market cap uses the market-price basis and known supply. Sale impact must not change the displayed market cap.
Use significant digits for small token prices. Unknown values display Unknown, never zero.

Show remaining position value, recovered cash, and total position return separately.
Use the existing accounting helpers for costs and fees. A partial sale does not imply principal recovery.
Calculate price giveback from price observations, not from total return after recovered cash.
Each balance shows its own read time and unavailable state. Global totals retain their stated scope.

Charts use actual timestamps and preserve gaps. Every chart shows its source, interval, timezone, and selected time window.
Fill markers use the actual fill time. Effective stop and active trail levels use the position's effective policy.
Charts do not mix market marks, executable quotes, and transaction floors into one unexplained series.

## Telegram lifecycle

Reuse the existing dispatcher as the single delivery owner for source and result notifications.
Each notification carries exact source, attempt, chain, and applicable position identity through the delivery audit.
Read the durable result before formatting a delayed message. A late source receipt must not replace or contradict a terminal result.
Keep source observations explicit. A source buy is not our buy.
Every notified armed source event receives its applicable funded result, including final retry refusal and unresolved submission.
General noise filters cannot silently remove that result after an earlier entry or retry notice.

Preserve the existing delivery audit and redaction helpers. Store one durable delivery row per correlated result transition in the existing persistence layer.
Source notifications use canonical chain, source-event identity, and message kind as their deduplication key.
Result notifications also include the attempt identity, execution state, and applicable transaction or fill identity.
A recovered fill therefore has a different key from an earlier unresolved result. Repeated reads of either result retain the same key.
Before delivery, supersede an unsent old result if the current projection contains a later result for that attempt.
The first activation records corrected UTC as its notification cutoff and establishes a baseline without replay messages.
New transitions after that cutoff remain eligible, including exits from positions that already existed at activation.
Restart retains the cutoff and delivery keys. It does not establish a new baseline.
Recover undelivered notifications from bounded durable event and attempt reads after restart, with a five-minute individual-message catch-up window.
Older missed results remain visible in the UI and produce one missed-delivery summary instead of individual historical messages.
Unresolved transactions remain eligible for a current needs-review notice regardless of age. They are not replayed as new source buys.
Keep queued, delivered, failed, and delivery-unknown distinct. Do not retry an ambiguous Telegram send as if it definitely failed.
Message preparation, queue saturation, or provider failure must not mark an event as delivered.
Notification work stays outside the funded decision and signature path.

Example terminal message, with labels rather than implicit symbols:

```text
REFUSED | FIRE | Solana
Source: xfilesboy bought about $700
Our size: $175
Reason: estimated round-trip cost 10.19%; cap 10.00%
Sell estimate: $157.17; required: $157.50
Measurement: independent quotes
No transaction submitted
Source: 11:09:32.966 UTC | Result: 11:09:35.436 UTC
Entry details | Token | Trader | Chart
```

Messages use actual persisted timestamps. Missing timestamps stay absent.
Wallet labels state authority on the signal chain. Evidence labels spell out closes and distinct coins.
Filled messages describe the filled position's policy. Failed exits cannot claim that an entry reservation was released.
Submitted messages include the full transaction link and say that confirmation remains unknown.

## Links and navigation

Reuse the canonical chain registry and existing provider mappings in one shared link builder.
Cover Solana, Robinhood 4663, BSC 56, Base 8453, and explicitly supported additional chains.
Preserve Solana mint case. Encode handle and identifier path segments. Unsupported destinations produce no guessed link.
Token, trader, transaction, chart, and internal entry links use different labels.
A token landing page is not proof that a chart pair exists. Prefer a verified pair URL when the provider supplies one.
Fomo's current route manifest places token and trader pages under its authenticated layout.
Both pages redirected an unauthenticated browser to the homepage during verification. Label Fomo links as login-required and provide a public explorer alternative.
Internal links select the exact chain and event or position. Reload and mobile navigation must preserve that selection.
Test every advertised destination for route shape and returned identity. Provider unavailability stays separate from a malformed link.

## Delivery sequence and acceptance

1. Implement the shared result and price projections with tests for the current contradictory cases.
2. Replace the Live and Entries views, then apply the shared states to Wallets and Research.
3. Route notifications and all links through the shared identity and result contract.
4. Verify desktop and mobile behavior against fixtures and authenticated live read-only pages.
5. Merge and deploy each complete change through the pinned runtime scripts without a new backup.
6. Verify the served assets, runtime processes, APIs, policy values, and notification evidence after deployment.

Required behavioral cases include unknown-to-recovered-fill, refusal-to-retry-to-fill, manual unknown, and the same mint on different chains.
Also cover paper-only open, missing event row, restart during evaluation, reconnect after completion, and responses received out of order.
Price tests cover partial sale, zero versus unknown, tiny prices, stale tuples, missing quote, and chart gaps.
Notification tests cover queue rejection, formatter error, send failure, ambiguous delivery, final retry refusal, and exact correlation.
Browser tests cover open settings during refresh, keyboard controls, deep links, failed requests, and 390-pixel layout.

FIRE, LABRAT, BinanceTown, and HOTDOG are fixed regression records. All four have no real fill in the reviewed source events.
No test or screenshot can claim that their historical entries should occur under a changed policy.
Fixture agreement proves code behavior. Organic post-deploy evidence must establish notification and refresh behavior under live traffic.
