# Memecoin entry feature capture

Date: 2026-09-29, written before 21:43Z. Operator order via Alpha. Author: Beta. Status: SPEC; Gamma reviews it,
then it is built in phases.

## Why

Gamma's study (`docs/audits/2026-09-29-funded-winners-vs-losers.md`, 338 funded closes 09-20..09-29)
found no stored entry-time feature that replicates as predictive:
- 44 numeric and 13 categorical features were tested; none passed the split-half hold-out.
- One source wallet made +$716; everyone else lost $321.
- The study also listed what we never store: order flow before the decision, holder structure,
  LP state, the source's context, and third-party token stats.
- It listed fields we do store but that are blind: consensus 100% unknown, chase columns 100%
  NULL on fills, momentum 87% missing, on-chain m5 83% missing, fill_gap_pct a duplicate, and
  is_first_buy constant.

The operator: "we will never be able to train a model if we dont store quality data".

## Goal and non-goals

- **Goal:** one durable feature row for every entry decision on every chain, fills and refusals
  alike. Every feature carries its value or a typed reason it is missing, its source and its read
  lag. Rows are written after the decision, in their own database.
- **Non-goal:** this changes no gate, no size and no exit. Nothing reads these rows on the money
  path. A feature earns a gate only through a pre-registered study on this data.
- **Non-goal:** no new third-party dependency on this host (see "Third-party sources").

## Architecture

- **A separate process**, `scripts/memecoin_entry_capture_loop.py`, started by the watchdog like
  the refusal capture loop.
- **Input:** the terminal rows of `memecoin_entry_attempts`, read through
  `get_readonly_connection()`. It follows a cursor on the row id, so it reads only committed
  decisions.
  - The funded runner does not know the capture exists: no import, no call, no queue. The runner
    cannot wait for it, and a capture outage cannot touch an entry.
- **Output:** `data/memecoin_entry_features.db`, its own SQLite file (WAL). Never the funded
  ledger.
- **Timing:** each attempt is captured as soon as its terminal row is committed.
  - Features derived from ledger or tape history are exact post hoc: each window is bounded by
    `decision_started_at`, and nothing after the decision is read.
  - Chain STATE reads (holders, LP) happen after the decision. They record `read_at` and
    `lag_s`, and they state that they describe the state at read time, not at the decision.
- **Tiers** (cost scales with how far the decision got):
  - **T0 (every attempt):** ledger and tape features only. No network call.
  - **T1** (the attempt passed the source-authority gate, i.e. `terminal_reason` is not
    `not_approved` / disarmed / quiet hours): adds network features within a per-chain budget.
  - **T2** (fills): adds the post-fill reads that only make sense for positions we hold.

<!-- schema:external=memecoin_entry_features -->
| column | type | meaning |
|---|---|---|
| attempt_id | TEXT PRIMARY KEY | memecoin_entry_attempts.attempt_id |
| network_id | TEXT | chain |
| mint | TEXT | coin |
| source_user_id | TEXT | the copied wallet |
| decision_at | TEXT | decision_started_at (UTC ISO) |
| outcome | TEXT | terminal_status |
| outcome_reason | TEXT | terminal_reason |
| tier | INTEGER | 0, 1 or 2 |
| feature_version | INTEGER | bumped when a definition changes; rows are never rewritten across versions |
| features_json | TEXT | {name: value}; a missing feature has a JSON null |
| coverage_json | TEXT | {name: "ok" or "unsupported_chain" / "unavailable" / "error:<kind>" / "not_attempted:<tier>"} |
| sources_json | TEXT | {name: source id}: `ledger`, `evm_tape`, `sol_tape`, `helius_gtfa`, `rpc`, `blockscout`, `stored_stats` |
| read_lag_json | TEXT | {name: seconds between decision_at and the read, state reads only} |
| computed_at | TEXT | when this row was written |

"JSON is the lab, columns are the shelf" (the `memecoin_features.py` doctrine): a feature is
promoted to a column only after a study.

## Feature catalog

Coverage is by chain: sol = Solana, 4663 = Robinhood, 56 = BSC, 8453 = Base. The numbers are what
is designed; the phase-1 build measures real coverage and reports it before phase 2.

### (a) Order flow in the N seconds before the decision (N = 30, 60, 300)

| feature | source | cost per attempt | coverage |
|---|---|---|---|
| buy_n, sell_n, buy_usd, sell_usd, net_flow_usd | EVM: our volume tape `data/memecoin_onchain_volume.db` pool_ticks (chain-wide Swap logs; quote side from pool_meta; USD from volume_quote_prices at the decision). sol: Helius `getTransactionsForAddress` on the coin's pool vault, slot/time filtered, research key | EVM 0 network calls. sol 1-3 gtfa pages (T1 only) | 4663/8453/56: the tape's own coverage (volume_feed gaps are recorded as coverage `unavailable`). sol: T1 only. The sol tape (`memecoin_solana_tape.py`) follows the pools our open positions trade in (`sol_position_pools`); it starts after the buy and cannot serve pre-decision flow |
| unique_buyers, largest_buy_usd | same | same | same |
| source_share_of_buys | same, excluding nothing (the source IS in the window) | 0 extra | same |
| flow_ex_source: all of the above with the source's own transaction excluded | same | 0 extra | same |
| same_slot_buyers_after_source, same_block_buyers_after_source | sol: gtfa on the source slot (the slot-end shadow's method, `memecoin_slot_end_shadow.py`). EVM: the tape by block and log index | sol 1 page. EVM 0 | sol: T1 with a source signature (J7 uuid origins have none). EVM: every tape-covered pool |
| our_own_trades_in_window | ledger fills | 0 | all. They are excluded from every flow number: our own trade is not organic volume |

### (b) Holder structure

| feature | source | cost | coverage |
|---|---|---|---|
| top10_share, top1_share | sol: RPC `getTokenLargestAccounts` (top 20) + `getMultipleAccounts` for owners (pool, curve and burn accounts marked, not counted as holders) + supply. EVM: balances replayed from Transfer logs in our own tape when the coin's full history is inside it (young coins); otherwise `unavailable` | sol 2-3 RPC calls (T1). EVM 0 | sol T1. EVM: coins younger than the tape's retention |
| holder_count | sol: Helius DAS `getTokenAccounts` (paginated, capped at 5 pages, else `>=N`). 4663 and 8453: Blockscout `/api/v2/tokens/<addr>/counters` (keyless). 56: `unavailable` (no keyless explorer) | sol ≤5, EVM 1 (T1) | sol, 4663, 8453 |
| creator_share | sol: the pump / LaunchLab / DBC create transaction names the creator; its token account balance. EVM: the launchpad's creation event where decoded; else `unavailable` | 1-2 | pump.fun, LaunchLab, DBC; EVM launchpads where decoded |
| sniper_share_at_launch, bundle_share_at_launch | sol: gtfa on the coin from its creation slot, first 3 slots: share of supply bought, and by how many signers (Jito bundle ids when the explorer API gives them, see the 09-27 finding). EVM: tape, first 3 blocks | sol 1-2 (T1). EVM 0 | young coins only (creation inside the lookback) |

### (c) LP state

| feature | source | cost | coverage |
|---|---|---|---|
| liquidity_usd | stored stats the gate already fetched (see (e)); EVM tape reserve_quote in pool_features | 0 | same as today |
| lp_burned_share, lp_locked_share | sol: the LP mint's largest holders (burn / incinerator / known lockers) for Raydium AMM/CPMM and Meteora; PumpSwap and bonding curves marked `not_applicable`. EVM V2: LP token held by the dead address or known lockers. V3/V4: `not_applicable` (positions are NFTs) | 1-2 (T1) | V2-style pools and Solana LP pools |
| pool_age_s | EVM tape: the pool's first tick. sol: the pool account's creation (gtfa oldest) | EVM 0, sol 1 | all |
| migrated, migration_age_s | sol: curve `complete` plus the PumpSwap/DAMM pool's creation. EVM launchpads: the graduation event where decoded | 0-1 | pump.fun, DBC, 4663 launchpad |
| bonding_progress | sol pump curve state (real reserves); 4663 launchpad curve reserves | 1 | pre-migration coins |

### (d) The source's context

| feature | source | cost | coverage |
|---|---|---|---|
| src_size_usd, src_size_over_own_median_30d | memecoin_events + onchain_wallet_detections for that wallet, strictly before decision_at | 0 | all |
| src_prior_buys_mint (the repeat-buy flag, replacing is_first_buy) | events + detections for (wallet, chain, mint) before decision_at | 0 | all |
| src_prior_n, src_prior_sum, src_prior_win_rate (our funded book on this source, closed before decision_at) | ledger | 0 | all |
| src_entry_vs_launch_price | tape: the coin's first trade price vs the source's fill | EVM 0; sol with (b)'s launch read | young coins |
| src_bankroll_usd, src_size_over_bankroll | RPC native + quote balance of the source address at read time (read lag recorded) | 1-2 (T1) | chains where the address is known (detections) |
| src_priority_fee, src_in_bundle | sol: the source transaction (getTransaction; v1 txs carry the fee in transactionConfig, see the 09-28 finding) | 1 (T1) | sol with a source signature |

### (e) Third-party sources

| source | keyless | what it has | use here |
|---|---|---|---|
| DexScreener | yes | pairs: price, liquidity, volume m5/h1/h6/h24, txns buys/sells, pairCreatedAt, fdv/mcap. No holders | NO new calls. The funded gate already fetched it at the decision; the capture copies the stored `memecoin_entry_snapshots.features_json` / `classification_json` values as `stored_stats`. The funded path's DexScreener budget is not the capture's |
| GeckoTerminal | yes, 30 calls/min per IP | pools, OHLCV; token info (holders fields may exist on some networks, unverified here) | NO. This host shares that per-IP budget with the funded runner; 4/4 probes answered 429 on 09-29, before 21:43Z (memory: never GT from this host) |
| Birdeye | no, API key (free tier limited) | holders, trades, security, top traders | not without a key and an operator decision |
| GMGN | no (terms; Cloudflare) | holder/sniper/smart-money stats | retired from the pipeline; not used |
| Helius RPC + DAS (research key) | our key | gtfa, getAsset, getTokenAccounts | used, Solana T1, only under the research-read rule (0 open real Solana positions and 0 reserved), budgeted |
| Blockscout (4663, 8453) | yes | token counters (holders), transfers | holder_count only, ≤1 call per T1 attempt, the holdings scan's headers |
| our own tapes | ours | EVM chain-wide swaps; sol own-position pools | preferred wherever they cover |

Computing a statistic ourselves from chain data beats a third party: it has a known writer, known
units and no shared budget.

## Blind spots Gamma found, and the fix for each

| blind spot | cause (read in the code) | fix |
|---|---|---|
| consensus_n_60m 100% unknown | `memecoin_real_runner.py` computes `prior_buyers_60m` only when the consensus gate is active (`eff_mc >= MEMECOIN_REAL_CONSENSUS_ABOVE_MCAP`); otherwise it passes None | The capture computes `m.prior_buyers_60m(before_iso=event time)` for every attempt: exact post hoc, 0 network. The runner is not changed |
| chase_* 100% NULL on fills | `update_entry_attempt_fields(chase_*...)` runs only in the would-refuse branch of the display chase | Two parts. (1) The capture recomputes the naive gap from the trace's `chase_pre_quote_reference` substage and the quote fields already stored. (2) OPTIONAL, runner-side, in memory only: mark the same substage on the pass branch with the naive gap and the base cap, no network and no DB write. It needs Alpha's go because it touches the entry path; (1) alone closes the fill gap for the naive gap |
| runup / giveback / momentum 87% missing | the episode facts exist only for coins with prior feed events | The capture computes them from the tapes (EVM) and the T1 gtfa window (sol), with the window bounded at decision_at |
| on-chain m5 83% missing (EVM) | the stage is unpriced when the quote-asset USD mark is absent | The capture prices it with `volume_quote_prices` at decision_at. If there is no mark within 10 min, the feature is `unavailable`, never 0 |
| fill_gap_pct == total_copy_drift_pct on every row | every executor passes `fill_gap_pct` = `drift["total_copy_drift_pct"]` (evm_swap_executor.py:1407, solana_swap_executor.py:205/1504, solana_slot_executor.py:256): one number stored twice by construction | The capture stores the two real components separately (source_drift_pct: source to quote; execution_slippage_pct: quote to fill). fill_gap_pct is documented as an alias in the knowledge layer; no writer change |
| is_first_buy constant 0 since 09-10 | follow_activity hard-codes it | Replaced by src_prior_buys_mint (d), computed from history. The events column is left alone |

## Proof that no fill-path latency is added

1. Structural test: no module on the entry path (`memecoin_real_runner`, `memecoin_real_leg`,
   `memecoin_evm_leg`, the executors, the lanes) imports or references the capture module,
   checked by AST.
2. Harness test: the real EVM entry over the scripted chain (tests/ink FakeInk pattern) and the
   Solana executor fake make the same sequence of RPC and DB calls with the capture process
   running against the same ledger and without it. The capture opens the ledger read-only (WAL
   readers never block the writer).
3. The optional runner-side chase mark (blind-spot fix (2)) is an in-memory trace field. If
   approved, its cost is measured on the harness (p50/p90 of the pass branch) and reported in its
   commit.

## Rule 5

1. **Money path:** none. A read-only ledger connection and its own database, in its own process.
   The only entry-path change is the optional in-memory chase mark, behind its own go.
2. **Speed on the fill path:** nothing added (see the proof section).
3. **Concurrency:** one process, one writer to its own DB, and a WAL reader on the ledger. The
   cursor is persisted in the capture DB; a crash re-reads from the cursor, and inserts are
   idempotent on attempt_id.
4. **Absent / zero / unknown:** every feature carries a coverage code. "No trades in the window"
   is 0 with coverage `ok` only when the tape proves the window was covered (no tick_gaps
   overlap). Otherwise the feature is null with `unavailable`.
5. **Dependency failures:** a Helius 429/timeout, a Blockscout error or a tape gap each give
   `error:<kind>`, never a value. The research-read rule gates every Solana read; Helius work
   waits while a real Solana position is open or reserved. The waiting is recorded as coverage
   `not_attempted:book_not_flat`.
6. **Units and identity:**
   - Identity is (network_id, mint), always: the same 0x address on Base and Robinhood are two
     coins.
   - USD values use the quote-asset mark at decision_at, never now.
   - Atomic amounts are stored as decimal TEXT.
   - Windows are in seconds on the chain's block time (EVM block_times; sol blockTime is not a
     latency anchor per the 09-28 finding, so the sol windows use slots with the decision's slot
     estimate recorded).
7. **The sample:**
   - The denominator is every terminal attempt; tier and outcome are columns, so a study can
     choose fills, refusals, or both. Refusals caused by our own state (not_approved,
     book_not_flat) are labelled, not dropped.
   - Our own trades are excluded from every flow feature.
8. **Rollback:** stop the process and remove it from the watchdog. The DB is additive and nothing
   reads it on the money path.

## Budgets (T1)

- sol: about 1,000 attempts a day, most below T1. The T1 share is measured in phase 1 before any
  network feature is switched on.
- Planned ceiling: 6 Helius calls per T1 attempt (gtfa flow 1-3, largest accounts + owners 2,
  DAS holders ≤5 pages behind its own switch), and a 1 call/s pace on the research key (the slot
  shadow's MIN_INTERVAL_S 1.1).
- EVM: tape reads are local. Blockscout ≤1 call per T1 attempt on 4663 (213 attempts/day)
  and 8453 (24/day).
- Every network feature has its own on/off switch, default OFF until phase 1 reports coverage
  and budget.

## Phases

1. **T0, zero network.** The process, the DB, the cursor; features (d) all, (a) EVM from the
   tape, the consensus, chase-naive and drift-component fixes, and stored_stats. It reports
   coverage per chain after 48 h.
2. **T1 Solana flow and holders** (gtfa window, largest accounts). It reports the budget.
3. **T1 LP state, EVM holders (Blockscout), creator and launch share.**
4. **The optional runner-side chase mark**, on Alpha's go.
Each phase is its own commit with its tests, Gamma's review and a coverage report.

## Tests (phase 1)

- The structural no-import test and the same-call-sequence harness test (proof section).
- Causality: a tape tick at decision_at + 1 s is never counted, and one at decision_at - 1 s is.
- Our own fill in the window is excluded from the flow.
- A tape gap overlapping the window gives `unavailable`, not 0.
- Chain identity: the same address on 4663 and 8453 gives two rows with separate histories.
- Consensus equals `m.prior_buyers_60m` at the event time, including on attempts where the
  runner passed None.
- The cursor survives a crash; a re-run inserts nothing twice.
- The capture holds only a read-only ledger connection (a write attempt raises).

## Open questions for Gamma

1. N windows of 30/60/300 s: enough for the pre-registered forward read, or add 10 s?
2. Holder count via DAS pages: is a capped `>=N` acceptable as a feature, or only top-k shares?
3. Should refusals caused by our own state (not_approved) get T0 rows at all? They are about half
   of Solana's attempts, and they are free at T0.

## Amendments after Gamma's review (2026-09-29, written before 22:30Z; spec c5d121fd2)

Gamma checked all six blind-spot causes against the code. The findings and how phase 1 answers
them:

- **MED 1, the cursor.** It follows (completed_at, attempt_id) with a 15-minute overlap, and
  inserts are idempotent on attempt_id. Rows that never complete are never waited on. Test: a lower
  rowid that completes second is captured.
- **MED 2, tape retention.** The writer prunes ticks and blocks older than 1 h and writes no gap
  row for it. A window is covered only if it lies inside the retained block range, with no
  unresolved tick_gaps and no feed reset in it; otherwise it is `unavailable:tape_retention` (or
  `tape_gap` / `feed_reset`). Test: a window older than the oldest retained block gives null, not
  0.
  - Chain 56 is not on this tape (BSC lives in `memecoin_bsc_v2_live.db`): unsupported in P1.
  - No Transfer logs are stored, so the EVM holder replay in (b) needs a new collector and is
    `unavailable` until then.
- **MED 3, Solana state reads after our exit (T1, not P1).**
  - Fill state reads (holders, LP, source bankroll) are labelled `post_exit` with their lag.
  - Any study comparing fills with refusals, or winners with losers, on a state feature must use
    lag-matched strata, or be scoped to refusals.
  - Holders (Gamma, 2026-10-02, on 1847dc536): T1 reads wait for a flat book, and our own open
    position keeps it busy, so every filled (tier 2) row is read `post_exit` by construction. Holder
    reads at decision time exist only for refused rows decided while the book was flat. A post-exit
    read carries the outcome (the dump, holders leaving). Any holder study pre-registers a lag cap
    and compares rows only inside one lag bucket and tier. `entry_capture_coverage.py` prints
    top10_share by tier x lag (<=60 s, <=1 h, >1 h).
  - Holder shares are per token account, not per owner. Only the source pool's own accounts, burn
    owners and our own wallets (`MEMECOIN_OWN_ADDRESSES`) are excluded. A second venue's pool still
    counts, so the source label says `source_pool_excluded_only`. top1 and top10 are each
    `unavailable:top20_exhausted` when a full 20-account list cannot prove them.
  - gtfa flow is exact post hoc and unaffected.
  - getTokenLargestAccounts and getMultipleAccounts use the research key only, never the pooled
    public lane; a test checks the resolved URL.
- **LOW 4, chase sources per lane.**
  - `chase_display_gap_pct` comes from the refusal columns, or from the new `chase_display_check`
    trace substage on the pass branch. The display chase runs where the runner fetches stats.
  - `chase_quote_gap_pct` comes from `chase_quote_confirmation` (Solana onchain_watch, the EVM
    executor).
  - The slot lane writes neither: its coverage is `unavailable`.
  - The coverage report splits every feature by lane.
- **LOW 5, clocks.**
  - Every history read keeps the relay's created_at window AND requires
    `received_at <= decision_started_at`. The relay ran 23 s ahead of our receipt on 4663.
  - Consensus is recomputed under that bound. The runner's own number is kept beside it as
    `consensus_n_60m_runner`, and the report prints their agreement.
  - Tape windows are in chain block time.
  - The quote-asset mark is latest-only, so its signed offset from the decision is stored in
    read_lag_json. The bound is 1800 s (about 1% of ETH drift), raised from 600 s after the dry
    run showed that a backlog pass loses rows at 600.
- **LOW 6, labels.**
  - Fills carry realized PnL.
  - Refusals from the refusal-capture gates (m5 families, near-ceiling mcap, floors) carry that
    capture's post-refusal paths.
  - not_approved and the other refusals have NO label yet. For them a later phase needs the
    coin's own post-decision price path from the tape (EVM, 1 h retention) or gtfa (Solana).
    Until then they are features without labels.
- **LOW 7, the real separate-process risks.**
  - No read transaction is held open: tested after every pass on the ledger, the tape and the
    capture DB.
  - Batch reads are bounded: 200 attempts per pass.
  - T1 will share one pacer and one 429 group with the slot-end shadow and the wallet graph on the
    research key (about 490 Solana T1 attempts a day).
  - Rule 5 item 2 is measured after deploy: runner entry-stage p50/p90 for 24 h before and after.
- **Q1, windows.** Kept (decision-N, decision]. Added [src_t-N, src_t), which excludes the
  source's block, and the copier queue (src_t, decision], which starts at the next block;
  same-block buyers after the source are their own feature. src_t is the source transaction's
  block time when the tape holds it, else our receipt time, labelled in `source_anchor`.
- **Q2, holders.** Top-20 shares are primary. holder_count is a (value, censored) pair from one
  DAS page, never a bare `>=N`.
- **Q3, not_approved.** Yes: T0 rows, labelled, never T1. They are the counterfactual population
  for roster decisions.

The phase-1 dry run on the live ledger and tape (read-only; 38 attempts over 15 min; output
outside data/):
- Source context and causal consensus: 100% ok.
- 4663 flow: 38.5% ok at the 600 s price bound, all others quote_price_stale from the backlog;
  hence the bound above.
- same_block_buyers_after_source: 15% ok on 4663, the rest `no_source_tx` (J7 uuid origins).
- Solana flow: `not_attempted:t1`.
