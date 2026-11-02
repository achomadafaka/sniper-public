# Solana pre-signal feed for funded momentum (E14 on Solana) — design (SPEC ONLY, no build)

**Date:** 2026-10-09. **Author:** Lambda, from Beta's started work (beta/sol-presignal-spec 2950b47dc).
**Order:** Alpha, relaying operator decision 1 = (a)+(b):
- Solana momentum keeps DexScreener for now;
- we build our own Solana pre-signal feed so it can come off DexScreener later.

**Standing order 46d75aed:** plan the Solana pre-signal rebuild before spending. No code until the operator
approves.

**Inputs:**
- `2026-10-08-memecoin-funded-momentum-own-data-design.md` (E14, decision S);
- `2026-09-30-memecoin-pre-signal-path-recording-design.md` (section 4: "Solana needs a new stream");
- `2026-07-24-memecoin-tape-priced-momentum-design.md` (section 5: a validation gate before a new price drives
  anything).

## 0. What the feed is for

The funded classifier's momentum (`memecoin_market_state._momentum`) needs one coin's price path over the
900 s BEFORE the decision. On Solana today that path is DexScreener's: capture_v2 rows, plus `stats["price"]`
as the current point. On the slot lane the current point is the source's own on-chain price.

The phase reaches money in three ways:
- **Post-peak nomination.** It is confirmed or refused by an exact quote against `peak_price`.
- **Sizing.** A runup of at least 20% gives `hot_narrow`, which scales the clip to 0.75.
- **Slot-lane admission.** The slot lane classifies at `memecoin_slot_lane.py:1409` and compares
  `risk_scale` with the leg it prefetched. A scale that differs from the prefetch either re-guesses the leg
  or refuses. So on that lane a runup flip can also refuse an entry.

Measured over 10-07..08 (E14 spec section 1), Solana had:
- 176 runup `hot_narrow` attempts, 6 of them filled;
- 39 post-peak nominations, 1 refusal;
- 1,281 of 2,008 attempts unknown.

## 1. Which data: what we already run, and what is new

| Source | Runs today | Holds before the signal | Verdict |
|---|---|---|---|
| Onchain wallet watcher (`ONCHAIN_WATCH_HELIUS_RPC_URL`) | yes | the TRACKED wallets' own transactions | the source's trade only, no market path |
| Slot lane (`memecoin_slot_lane`) | yes | the source transaction at slot S | one point (the source's price) |
| Funded swap marks (`memecoin_solana_swap_marks`, `fastlane_sol_*` on the mark tape) | yes | OPEN funded positions' pools only | repeat coins we hold |
| Paper vault stream (`memecoin_paper_sol_ticks`, key dea05c94) | yes, inside the runner process | open v1 paper positions' pools. Since paper step 1 (deployed 2026-10-09T00:31Z) its ticks land on the mark tape as `paper_tick:sol_*` (one per coin per 30 s, the mark floor) | repeat coins with an open paper position |
| Solana paper/exit tapes (`memecoin_sol_tape.db`, `memecoin_ticks.db` sol_tx) | yes | position pools only | post-entry |
| Signer-window research tape (`research_signal_windows_solana.db`, 5.5 GB) | research lanes stopped 2026-09-30 by operator order | per-signal windows (research) | not a live source; used here only to classify source venues |
| Wallet Map tape (`memecoin_wallet_graph.db`) | yes | tracked wallets' trades | flow of OUR wallets, not the market |
| **Cohort stream** (new, section 3 step 1) | no | a coin's pool from its FIRST tracked-wallet buy sighting for a lifetime L | repeat sightings |
| **Pump-family firehose** (new, section 3 step 2) | no | every pump curve and PumpSwap swap, chain-wide | first sightings on pump venues |

Before the FIRST sighting of a coin, no source we run sees it. Pricing a first signal's pre-signal minutes
needs a chain-wide stream (the firehose) or a backfill after the fact. A backfill cannot serve the live
decision: it measured 191.5 Helius credits per decision mean in the 09-30 probe, and it takes seconds.

**Cost** (Helius billing docs, read 2026-10-09):
- Every plan: standard, Enhanced and LaserStream WebSocket = 2 credits per 0.1 MB uncompressed;
  getAccountInfo, getMultipleAccounts, getTransaction, getSignaturesForAddress = 1 credit.
- Plans: Free $0 / 1M credits; Developer $49 / 10M; Business $499 / 100M; Professional $999 / 200M.
- Each Helius key is its own account, and only e1f95763 is paid (brain 3adb623f). A new stream key is an
  operator action.

| Option | Bytes measured (Beta, 2026-10-09) | Credits / month | Plan |
|---|---|---|---|
| 0: own marks already on the tape | none new | 0 | none |
| 1: cohort stream, lifetime 1 h | paper stream rate 674.5 rows/min on 38 subscriptions (~17.7 per subscription per minute), payloads 517-2,376 B, scaled to the cohort's concurrency with no activity decay | ~0.5M | Free |
| 1: lifetime 6 h | same | ~2.1M | Developer $49 |
| 1: lifetime 24 h | same | ~5.3M (upper bound) | Developer $49 |
| 2: pump curve + PumpSwap logs firehose | 333 + 500 tx/s, logs median 472 / 782 B, ~0.9 MB/s | ~47M | Business $499 (range $499-999) |

Option 1's estimate is an upper bound because it assumes no activity decay. A 24 h byte-count canary on
the real key measures it before a plan is chosen.

## 2. Coverage, measured on our own decisions

**Funded-lane Solana decisions, 2026-10-07..08** (n = 1,877; shadow lanes excluded; one row per origin and
mint; 48 fills). Reproduced by Lambda from Beta's
`scripts/research/sol_presignal_coverage.py --since 2026-10-07T00:00:00+00:00 --until
2026-10-09T00:00:00+00:00 --tape-since 2026-10-05T00:00:00+00:00`: the output is identical.

| Source | Decisions with >= 90 s of path (3 points on a 30 s grid) | Share | Fills covered |
|---|---|---|---|
| Provider today (DexScreener phase known) | 650 | 35% | — |
| Cohort, lifetime 15 min | 583 | 31% | — |
| Cohort, 1 h | 794 | 42% | — |
| Cohort, 6 h | 960 | 51% | — |
| Cohort, 24 h | 1,119 | 60% | 15 of 48 |

- Concurrent cohort subscriptions (p50 / p90 / max): 15 min 5 / 10 / 18; 1 h 18 / 31 / 39; 6 h 89 / 129 / 139;
  24 h 304 / 324 / 329.
- 27 of the 48 fills had no prior sighting, so no cohort stream can price them.
- The cohort source is `memecoin_events` J7 follow_activity buys: 1,823 sightings in the two days.

**Chain-wide alternative.** Signals with a source transaction 10-05..09 (n = 2,315), by the source swap's venue:
- pump_amm 1,130 (49%);
- pump_curve 276 (12%);
- meteora_dlmm 279, damm_v2 177, raydium_cpmm 168, dbc 155, whirlpool 47, launchlab 28, clmm 20, rest 36.

Crossed with a 24 h cohort:

| | cohort | no cohort |
|---|---|---|
| pump family | 751 | 655 |
| other venue | 629 | 280 |

So the cohort alone covers 60%, the pump family alone 61%, and the union 88%.

**Option 0, measured now.** Solana attempts since 2026-10-09T00:40Z (n = 230), own marks with >= 3 points
over >= 30 s in the 900 s window:

| | own spot marks | own swap marks only | none |
|---|---|---|---|
| provider phased (113) | 5 | 38 | 70 |
| provider unknown (117) | 6 | 9 | 102 |

That is 58 of 230 (25%), against the provider's 113 (49%) on the same attempts.

**Upper bounds.** Every cohort figure assumes the coin's pool binds and the stream lives:
- Pump-family pools bind by PDA with zero reads: the curve, and the canonical pool per `pump_mint_proven`.
- Other venues (about 40% of sources) have no source transaction on a J7 sighting. They bind from the paper
  position's own binding when one opens (`paper_tick_json`, zero reads), else need a resolver. The
  resolver is unmeasured.
- The canary measures the binding rate.

## 3. The plan (each step its own spec section, branch and Gamma review; steps 1 and 2 need the operator)

**Step 0: the Solana shadow on own marks already on the tape.**
- Zero cost, no key.
- Turns on the E14 shadow for Solana: `facts["momentum_own"]` from `memecoin_position_marks` rows of OWN
  sources only (`paper_tick:sol_*`, `fastlane_sol_*`).
- One kind per series: spot-type marks (`*sol_state*`, `*sol_curve`) when they make >= 3 points, else
  swap-type marks, never mixed. A STATE mark on DLMM/CLMM is not spot; the corroboration band already
  filters those (`memecoin_solana_swap_marks`).
- Same grid and thresholds as EVM E14.
- It builds the Solana plumbing and starts the comparison now, on the 25% it can see.
- **The slot lane records its classification on rejected rows** (Gamma, required). Today a
  `slot_lane_scaled_clip` rejection leaves `classification_json` NULL: all 5 since 10-01, two of them
  Frank's (DQxG8fJb and 2zNDBDwd on 10-02). So a runup-caused refusal cannot be told from a
  regime-caused one. Step 0 persists the classification (regime, runup_pct, consensus, risk_scale) plus
  the prefetched `leg_scale` on every slot-lane row, rejected or not. Without it, section 6's leg-scale
  metric is unmeasurable.
- **Our own fills are flagged** (Gamma). `fastlane_sol_*` marks exist only on coins we hold funded, and
  after our fill the pool's path includes our buy's impact, so on a repeat attempt the own runup partly
  measures us. A series gets `includes_own_fill = true` when any of our funded fills on the coin
  (entry or exit) falls inside its window, and the read reports those series apart.
  `paper_tick:sol_*` is simulated, with no impact, and needs no flag.

**Step 1: the cohort stream.**
- Operator decides: a new dedicated Helius key, and its plan after the canary.
- A third instance of the vault-stream machinery (VaultTape, CurveTape, PoolMarks, as paper_sol_ticks).
- Runs in ITS OWN process under the watchdog cron, not in the funded runner. The paper stream's 38
  subscriptions already share the runner process; ~300 more must not.
- Subscribes a coin at its first J7 buy sighting and holds it for lifetime L after each sighting.
  L = 1 h / 6 h / 24 h, chosen from the canary.
- Binds pump-family coins by PDA. Others bind from the paper binding, or stay unbound (unknown).
- Writes per-swap rows (time, side, quote and coin amounts, price, kind) and spot marks to its own tape DB.
  That is the pre-signal price AND flow the research program asks for (f1755d7f), from bytes already
  received.
- First a 24 h canary: byte count, binding rate, and live-subscription share. Then the plan choice.

**Step 1 as built** (branch lambda/sol-cohort-stream; operator OK 2026-10-09 via Alpha; Gamma's five
points):

- **Process and key.** `scripts/memecoin_sol_cohort_stream.py`, with
  `scripts/memecoin_sol_cohort_watchdog.sh`, which runs only while `MEMECOIN_SOL_COHORT_HELIUS_URL` is in
  `.env`. The process refuses a keyless key, option C's key, or a key shared with ANY other configured
  variable. Code: `sniper/services/memecoin_sol_cohort.py`. Store: `data/memecoin_sol_cohort.db`, marks
  kept 25 h. The ledger is read only.
- **Triggers.** `own` is `onchain_wallet_detections` (solana), bound from the source transaction. `j7` is
  `memecoin_events` buys (solana), bound from the latest paper position's own pool, else the pump curve,
  else the canonical PumpSwap pool. The first poll starts at the current end (no backfill).
- **J7 FENCE (Gamma 1, defined as coverage() measures it).** Every coins row and every mark carries
  `trigger_first` (own or j7), and every coin carries `onchain_first_at`. J7 is measurement only: step
  1b may read into the funded classifier ONLY marks at or after the coin's own (onchain) first
  sighting, which is exactly what the own trigger alone would have held. It may read more only if the
  operator separately allows J7-triggered coverage. J7 is not a wallet-signal source (operator
  10-01/10-02).
  - The fence is not "trigger_first = own". In the 24 h to 2026-10-09 every own coin was also a J7 coin
    (own 68 buys / 34 mints, J7 1,051 / 304, union 304), so that version would keep almost nothing.
- **Lifetime and cap (Gamma 5).** Each sighting extends the lifetime: 24 h in the canary, the upper
  bound. At most `COIN_CAP` (400) coins. Past it J7-only coins go first (Gamma LOW 1), then the oldest
  last sighting. Cap hits are counted, and evictions are counted per trigger.
- **Marks.** Fresh paper-machinery ticks only, every 15 s, in QUOTE units (SOL/USD lives in the
  runner). A blind stream writes nothing, and the epoch timeline (connect, disconnect, error) tells it
  from a quiet pool (Gamma 3). Each coin's binding state is kept, so `graduation_unresolved` is counted,
  not read as quiet (Gamma 4).
**2026-10-10 metering correction.** RPC credit estimates now use recorded method counts and tariff version `helius-rpc-2026-10-10-v2`: `getTransaction` and `getBlock` are one credit, `getProgramAccounts` ten, and unlisted methods stay unknown. Reports separate known estimates from unknown request counts; they do not price every call at one or equate the estimate with an invoice. Flushes are immutable, idempotent fragments with recorded interval bounds; missing observation intervals remain unknown. Distinct API keys do not establish separate provider accounts or quotas, so the key/account assumption above must be verified before allocating capacity. No paid budget is raised by this correction.

- **Canary metrics.**
  - Received WS bytes per minute at uncompressed size, metered at the socket. Credits = 2 per 0.1 MB plus
    RPC calls.
  - Stream rows by coin age (under 1 h, 1-6 h, 6-24 h), so one 24 h run prices every lifetime
    (`lifetime_projection`, `plan_for`).
  - Coverage AT `decision_started_at` per trigger: >= 3 marks over >= 30 s in [t-900, t), counting only
    marks after that trigger's own first sighting. First sightings (within 60 s of the decision) and
    never-sighted coins are reported apart (Gamma 2).
  - `scripts/research/sol_cohort_canary_report.py` prints all of it.
- **A refused key leaves a marker** (`data/memecoin_sol_cohort.refused`, the reason only). The watchdog
  stays idle while the marker is newer than `.env`. Residual (Gamma on a5fd9ee49): only `.env` is
  compared, so a conflicting key that lives in `.fomo.env` keeps the marker until `.env` is touched.
  `.fomo.env` holds no api-key today.

**Step 2: the pump-family firehose.**
- Operator decides, after the step 0/1 shadow read.
- +28 pp coverage (first sightings on pump venues), at about Business $499/month.

## 4. Latency: nothing on the fill path

| Read | Measured | n |
|---|---|---|
| Mark tape (step 0): main ledger, fresh `get_readonly_connection`, one coin's 900 s of marks | p50 1.97 ms, p90 2.02 ms, max 2.20 ms | 230 (Lambda, 2026-10-09) |
| Cohort tape prototype (320 coins, 1 h kept, beside a live writer) | p50 0.25 ms, p90 0.35 ms, max 0.67 ms | 1,000 (Beta) |
| E14-shaped grid read on the 5.5 GB Solana tape | p50 0.35 ms, p90 3.72 ms, max 21.5 ms | 300 (Beta) |

- The read runs on the E14 read pool (`_momentum_read_pool`) and is PEEKED with zero wait
  (`_momentum_own_peek`). A late result lands on the trace (`_momentum_own_late_best_effort`) with
  `wait_would_ms`, exactly as on EVM.
- Start points: on the J7/hook path, beside `_start_evm_supply_read` (the E14 start site, extended to
  `solana`). On the slot lane, when the buy is decoded, before `classify_funded_entry` at `:1409`; it has no
  GET to hide behind, so the peek is all it gets.
- A switch later joins only if the shadow's p90 `wait_would_ms` is at most 1 ms on every Solana lane.

## 5. Absent vs zero vs unknown

- **Not subscribed** (no prior sighting, or lifetime expired): `not_subscribed`, unknown.
- **Unbound pool**: `unbound`, unknown.
- **Stream down or epoch reset inside the window**: points never precede the socket's (re)start, the
  E14 `since` rule. A window starting before it is shortened (`covered_s`), never filled. A gap leaves no
  carried price across it.
- **Subscribed, live, no trade:**
  - A spot-type binding (curve or CPMM state) carries its spot: a measured flat, as funded curve marks hold
    while their subscription is live.
  - A swap-only binding (DLMM/CLMM) has no spot to carry. No trade in the window is unknown, not flat.
- **USD quote** (SOL, USDC) not fresh in the cached on-chain quote price: unknown, never a stale USD.
- **Unknown keeps today's meaning:** no post-peak nomination, no runup (so no `hot_narrow` by runup),
  `data_quality` partial. On the slot lane, unknown leaves `risk_scale` as the regime alone, which is
  today's behaviour for unknown momentum.

## 6. Shadow first, the comparison pre-registered (the EVM E14 method)

Every Solana attempt that gets past the start site carries `facts["momentum_own"]` (with `momentum_basis` =
`provider_mid`) beside the provider phase. Nothing it records decides anything. The read reuses the EVM E14
read (due at or after 2026-10-10T00:25Z), and is written here before any Solana row exists:

- **Sample:** funded-lane Solana attempts past the start site, one per origin and mint, shadow lanes
  excluded. Per lane: J7 hook, slot lane, on-chain fallback.
- **Coverage:** own phased share vs provider phased share on the same attempts, per step (0, then 1).
- **Phase agreement:** the 5x5 table where both are known.
- **Sizing:** runup >= 20 under each basis; the attempts whose `hot_narrow` would flip; on the slot lane,
  the attempts whose `risk_scale` would differ from the prefetched leg (counted from the classification
  step 0 persists on rejected rows; before step 0 this cannot be counted).
- **Nominations** under each basis. **Giveback at nomination** under both bases against the SAME
  `quote_price` (the attempt's exact quote). Rows with a requote after `post_peak_confirmation` are read
  apart.
- **Display price at the decision, by kind** (Gamma): own vs provider, relative error over attempts where
  both exist, reported separately for spot-type series (state, curve) and swap-type series (execution
  prices, which carry impact). Swap-type is 47 of the 58 option-0 attempts. The 07-24 gate applies to EACH
  kind with n >= 30: median <= 5% and p90 <= 15%. A kind below n = 30 reports progress and no verdict, so a
  swap-heavy sample cannot fail on basis rather than units. On FAIL, investigate units first: decimals,
  then the quote leg.
- **Own fills:** series with `includes_own_fill` are reported apart in every metric above.
- **Latency:** p90 `wait_would_ms` per lane.
- **Recommendation rule:** a switch of Solana to `E14:solana` is recommended only when ALL of these hold:
  - display price PASS on every kind with n >= 30;
  - own coverage >= provider coverage on the same attempts;
  - p90 `wait_would_ms` <= 1 ms on every lane;
  - the sizing and nomination differences are reported, with a GIVEBACK_PCT refit proposal if the spot
    basis shifts giveback, as E11's cap was refit.

  The switch itself is the operator's.
- No re-slicing and no threshold change after the numbers are seen.

## 7. Rule 5

1. **Money path:** steps 0-2 are measurement only (shadow facts). The later switch would touch nomination,
   sizing and, on the slot lane, admission. It is not part of this spec.
2. **Speed:** no new synchronous work on the fill path. The read is a zero-wait peek, measured above. The
   cohort stream runs in its own process, so it adds no GIL or CPU load in the runner.
3. **Concurrency:**
   - The read uses its own read-only connection per call.
   - The stream process owns its DB. The runner only reads it (WAL).
   - The E14 pool is created under its lock.
4. **Absent/zero/unknown:** section 5.
5. **Dependency failures:**
   - Helius socket loss: no points across the gap.
   - Credit exhaustion or a 429: the stream stops and pages once; the read returns unknown.
   - A tape DB that is missing or locked: `read_failed`, unknown.
   - Nothing reads as flat by accident: a carry needs a live subscription.
6. **Units:**
   - USD per token at the cached on-chain quote price, one quote USD per series.
   - Solana decimals from the mint (pump: 6).
   - Swap marks are execution prices, state and curve marks are spot, never mixed in one series.
   - Chain id `solana`.
7. **Sample:** our attempts only. Cohort coverage depends on what our wallets buy, so it describes our
   signal stream, not the market. A funded-mark series that includes our own fill measures our own
   impact (`includes_own_fill`, reported apart).
8. **Rollback:**
   - Step 0: revert, or drop `solana` from the read's chain set.
   - Step 1: stop the process, so every read becomes `not_subscribed`.
   - Step 2: unsubscribe.
   - Shadow facts are additive keys.

## 8. Operator decisions

1. Approve step 0. Zero cost; build next.
2. Step 1: create a dedicated Helius key (own account), run the 24 h canary on it, then choose the plan
   and lifetime from its bytes and binding rate. Estimate: Free at 1 h, Developer $49 at 6-24 h.
   - **The cohort trigger is your choice.** The coverage above uses the first J7 buy sighting
     (`memecoin_events` follow_activity). Your standing order (10-01/10-02) is that J7 is not a wallet-signal
     path and is to become an X-intel funnel. Using it to start a measurement is not routing, but it is a
     J7 dependency.
   - The own alternative: subscribe at our own on-chain sighting, i.e. the slot lane and on-chain watcher
     decodes of the armed roster.
   - The canary measures BOTH triggers' coverage side by side, so you pick from data.
3. Step 2: the pump-family firehose (about $499/month, +28 pp), only after the step 0/1 shadow read.
