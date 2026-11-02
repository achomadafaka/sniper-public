# Memecoin Wallet Map, slice 3: flow change around a source buy (pre-registration)

2026-10-04 status amendment: impact-v1 remains historical diagnostic evidence. The new map-size
observer follows [the measured-size contract](2026-10-04-wallet-universe-measurements.md), with
resumable dense history, source clocks and venue quote assets kept separate. It measures the post-buy
window; the prior window is optional. Legacy v1 rows cannot enter its comparable sizes or readiness
counts. Funded trading remains outside both measurements.

**Date:** 2026-09-30 · **Author:** Lambda · **Status:** DRAFT for Gamma's pre-registration review,
round 2. Round 1 (9c33bd4d3) had 3 MEDIUM and 4 LOW; all 7 are folded in below and marked
"(Gamma M1)" and so on. No impact read may run before the review is CLEAN. · **Parent spec:**
`docs/superpowers/specs/2026-09-30-memecoin-wallet-map-design.md` (slice 3 outline) · **Order:**
operator, 2026-09-30. The metric and section 1 were approved in episode 2c1310c4. Section 2 was
approved with launch reads excluded, and the spec was approved in chat.

## Question

When one of our Solana sources buys a coin, how does the rest of the market's net SOL flow into
that coin change in the next 60 s, compared with the 60 s before?

The map uses the answer as its default bubble size. The number is descriptive and not causal
(Gamma L4). A source that buys into strong inflow can read negative by mean reversion alone.

It never arms, sizes, admits or exits anything. The funded roster changes only on funded results
(operator, 2026-09-11).

## Operator decisions (2026-09-30)

| Question | Answer |
|---|---|
| Metric | Net SOL flow, 60 s after minus 60 s before the source buy. The change in distinct buyers is stored too |
| Where it runs | Inside the hourly `scripts/research/wallet_link_graph.py` job; results go in the graph DB |
| Read | One Helius `getTransactionsForAddress` read per source buy on the mint (research key) |
| Quote | SOL-quoted trades only |
| Attribution | Owner balance deltas (`dump_forensics.trade_of`), never the tape `maker` (it is the fee payer) |
| Map | Size-by impact becomes the default. A wallet with n < 10 is unknown |
| Launch reads | Excluded from the main numbers (section 2) |

Gamma round 1 added four rules; the operator can override any of them:

- the main numbers use detection-fed units only;
- the mover test needs n >= 20;
- a wallet whose censored share is over 25% is unknown;
- launch is decided from the mint's first transaction.

## The rule (method `impact-v1:w60:sol:first-buy:det-origin:age60:show10:test20:cens25:loo-median:bh05`)

### Unit of observation

One **source buy**: the earliest `ok` row of `memecoin_wallet_slot_reads` per
`(source_user_id, mint)` with `network_id='solana'`, taken inside the job's 14-day window. A later
buy of the same coin by the same source is never a second unit. The read table already holds
`signature`, `slot` and `idx` (transactionIndex) of the source transaction.

**Origin (Gamma M2).** Every unit has one of two origins:

- `detection`: the source signature has an `onchain_wallet_detections` row with a non-null
  `block_time`. The watcher saw the buy, whatever our funded gates then did.
- `fill`: otherwise. The unit reached the read queue only because we filled a copy of it. Our
  funded gates chose it:
  - the chase cap refuses a coin whose price ran after the source buy, which biases the after
    window low;
  - the volume floors pick busy before-windows, which mean-revert.
  - Both push `delta_sol` down.

**The main numbers, the size and the test use `detection` units only.** Fill-fed units are read and
stored, and shown separately with their own n and median. They never enter the size or the test.

Sample on 2026-09-30, read-only, window from 2026-09-16T00Z, before censoring and launch exclusion:

| | units | wallets with n >= 10 | wallets with n >= 20 | wallets with n >= 30 |
|---|---|---|---|---|
| all first buys | 749 (65 wallets) | 23 | 14 | 10 |
| detection-fed | 516 | 19 | 8 | 5 |
| fill-fed | 233 | | | |

### Source time t0

- `t0` is the source transaction's chain `blockTime` in seconds.
- It is taken from `onchain_wallet_detections.block_time` by signature when that value is not null.
- Otherwise it is taken from the slot read's `detected_at`. Only fill-fed reads reach this case, and
  they store the chain blockTime there. Gamma confirmed this: all 516 detection-fed first buys have
  `block_time`, and the 25 signatures with only null-`block_time` detection rows came through the
  fill path.
- A detection-fed read's `detected_at` is **host** time. Over the 516 detection-fed first buys,
  detected_at minus block_time is p50 1.32 s, p90 1.78 s, max 25.02 s. It is never used as t0.
- The window read checks t0 against the page. The source signature's `blockTime` in the page must
  equal t0, or the read is censored as `clock_mismatch`.
- For fill-fed reads this check is circular, because their t0 came from a Helius page. It is
  harmless and kept for uniformity.

### Reads (research key, Solana book flat)

Both calls go through `funded_exit_truth.rpc`, which reaches `pick_research_url`. That function
refuses a funded or watcher key. Before every call, the job checks that no real Solana position or
reservation is open (`solana_book_open`), as the slot reads already do. An open book stops the
pass for this run. It does not censor anything.

1. **Age probe (1 call; Gamma L1).**
   - The call is `getTransactionsForAddress(mint)` with `sortOrder: "asc"`, `limit: 1`,
     `transactionDetails: "full"`, and no filter. It returns the mint's first transaction.
   - `age_s = t0 - blockTime(first transaction)`. It is stored on every read.
   - When `age_s <= 60`, the read is stored as `launch` and the window read is skipped.
   - An empty page is `unread`, never `launch`. A mint with a known source buy must have a
     transaction, so an empty page is an index gap.
2. **Window read** (at most 10 calls). `getTransactionsForAddress(mint)`, `sortOrder: "asc"`,
   `limit: 100`, `transactionDetails: "full"`, filters `status: succeeded` and
   `blockTime in [t0 - 60, t0 + 60]`. The read follows `paginationToken` for up to 10 pages. A
   short page, or a page without a token, ends the read. If a token is still left after 10 pages,
   the read is censored as `truncated`.

The worst case is 11 calls per read. The job allows `IMPACT_BUDGET = 300` calls per run, so at
least 27 reads fit in a run. A read starts only when its worst case fits the remaining budget.
Calls are spaced 1.1 s apart, so a full budget adds about 5.5 minutes to the run.

The impact pass runs after the slot reads and before the funder reads. Every Helius call in the
run stays under `SLOT_BUDGET_MAX = 2000`. The CLI flag is `--impact-budget`, default 300.

Pending buys are read in this order:

1. detection-fed before fill-fed;
2. newest t0 first within each origin.

**Truncation bias (Gamma M1).** The 10-page cap is 1,000 transactions over 121 s.

- The 1-s slot reads (3 pages) already truncated on 5 of 749 first buys.
- In the source's own second, buyers plus undecoded transactions are p50 1, p90 15, p99 76. A p90
  coin at that rate for 121 s is about 1,800 transactions.
- So the busiest coins are censored. Those are the coins where a large flow change is most
  plausible.
- The cap stays at 10 pages, as the operator approved. The per-wallet censored-share rule below
  keeps a verdict from resting on the quieter half of a wallet's coins.
- If many wallets end up unknown for this reason, raising the cap is a separate operator decision
  with its own budget.

### Before and after

- The page's transactions are ordered by `(slot, transactionIndex)`.
- **Before** = transactions ordered strictly before the source transaction, with `blockTime >= t0 - 60`.
- **After** = transactions ordered strictly after the source transaction, with `blockTime <= t0 + 60`.
- The source transaction itself is in neither window.
- blockTime has one-second resolution. Inside the source's own second, chain order decides the side.

### What counts as a trade

- Each transaction is decoded with `wallet_link_graph._default_trade`. That is `trade_of` with the
  quote unit named. It gives one trade per transaction. A second trade inside the same transaction
  is not counted.
- A transaction that does not decode is counted as `undecoded` on its side. Examples are
  aggregator-routed swaps, transfers and account creations.
- **Excluded traders.** Their trades are dropped before any sum:
  - every verified Solana address of the source: the inverse of `solana_owners(ledger)`, plus the
    trader of the source transaction itself;
  - every address in `MEMECOIN_OWN_ADDRESSES`;
  - every transaction whose signature is one of our Solana `memecoin_fills.tx_sig`, for **entries
    and exits**. Today the job's `our_entry_sigs` covers entries only; it widens to all fill kinds.
- Other roster sources count as market. On 2026-09-30, 8 of 749 first buys had another roster
  source's first buy on the same mint within 60 s.
- **SOL only.** If any decoded trade in either window has a `unit` other than `SOL`, the read is
  censored as `non_sol`. A `unit` of None counts as not SOL.

### Per-read numbers (status `ok` only)

- `net_before`, `net_after` = SOL bought minus SOL sold by the counted trades on that side.
- `delta_sol = net_after - net_before`.
- `buyers_before`, `buyers_after` = distinct traders with at least one buy on that side.
  `delta_buyers = buyers_after - buyers_before`.
- `tx_before`, `tx_after` = transactions on the side. `undecoded_before` and `undecoded_after` are
  stored with them.
- A side with no counted trade has a net of 0.0 SOL. That is a known zero, because the read
  succeeded.

### Status (one per read; the first rule that applies wins)

| status | meaning | in the main numbers | retried |
|---|---|---|---|
| `unread` | a call failed (quota, HTTP, RPC error, timeout), the book opened mid-read, or the age probe did not return the mint's first transaction (an empty page, or a first transaction after t0) on fewer than 3 runs | no (pending) | yes, next run |
| `probe_empty` | the age probe failed that way on 3 runs (build, Gamma LOW): an index gap that does not heal | no (censored) | no |
| `launch` | `0 <= age_s <= 60`: the mint's first transaction is at most 60 s before t0 | no (excluded, counted) | no |
| `truncated` | a pagination token is left after 10 pages (checked first: a cut page can miss the source) | no (censored) | no |
| `not_found` | the source signature is not in the window page | no (censored) | no |
| `clock_mismatch` | the page's source blockTime is not t0 | no (censored) | no |
| `non_sol` | a decoded trade in the window is not SOL-quoted | no (censored) | no |
| `ok` | everything above passed | yes, if `detection` origin | no |

A censored read is never a zero. Every censored and launch read is stored with its reason and
counted in the wallet's row.

### Per wallet (Solana; the job's 14-day window; detection-fed units)

- `n` = the wallet's detection-fed `ok` reads.
- `censored_share` = (`truncated` + `not_found` + `clock_mismatch` + `non_sol` + `probe_empty`)
  divided by (those five + `ok`), over detection-fed reads. It excludes `launch` and `unread`.
- The verdict, in order:
  1. `unknown` when `n < 10`, or when `censored_share > 0.25` (Gamma M1). In the second case the
     reason `censored` is shown.
  2. `untested` when `10 <= n < 20`. The stats are shown and no test is run (Gamma M3).
  3. When `n >= 20`, the mover test below decides: `mover` or `not_distinguished`.
- The stats are stored whenever `n >= 10`, even when the verdict is `unknown` for censoring:
  - the median of `delta_sol`, with p25 and p75 (the IQR);
  - the share of reads with `delta_sol > 0`;
  - the median of `delta_buyers`;
  - the median undecoded share, `(undecoded_before + undecoded_after) / (tx_before + tx_after)`,
    taken over reads with at least one transaction.
- Always stored:
  - the counts of each censored status and of `launch`;
  - `censored_share`;
  - the fill-fed `n_fill` and `median_delta_sol_fill`.

### Mover test (the only claim on the map)

The tested set holds the wallets with `n >= 20` and `censored_share <= 0.25`. Its size is `m`.

1. For each tested wallet, compute the pooled median `M` of the `delta_sol` of every **other**
   wallet's detection-fed `ok` reads (leave-one-out). All other wallets count, whatever their n.
2. `k` = the wallet's reads with `delta_sol > M`, strictly. Ties count as not above, which makes the
   test conservative.
3. `p` = one-sided binomial tail `P(X >= k)`, with `X ~ Bin(n, 0.5)`, from the job's existing
   `binomial_tail`.
4. Apply Benjamini-Hochberg at q = 0.05 across the `m` tested wallets in this run.
   - A pass gives the verdict `mover`.
   - Otherwise the verdict is `not_distinguished`.
5. `k_needed` = the smallest k with `P(X >= k) <= 0.05 / m`. That is the count a rank-1 BH pass
   would need. It is stored and shown, so a `not_distinguished` verdict carries its bar.

**Power (Gamma M3).**

- At m = 23, a rank-1 pass needs 10 of 10 at n = 10, and 17 of 20 at n = 20.
- When the true P(above M) is 0.7, power is 3-15% at n = 10, 11-42% at n = 20, and 16-73% at n = 30.
  When it is 0.8, power is 41-80% at n = 20.
- Today about 8 wallets reach n >= 20 detection-fed, so m is about 8 at most.
- `not_distinguished` therefore carries little. The hover never words it as "not a mover".

**Limits.**

- The test compares a wallet with the rest of **our roster**, not with the market.
- It assumes reads on different coins are independent. Reads on the same day share market regime.
- Each hourly run re-tests the same wallets, so verdicts near the BH line can flip from run to run
  (Gamma L3). The hover says "this run only". Nothing forward-grades the verdict, and no decision
  reads it.

## Storage (graph DB, `data/memecoin_wallet_graph.db` under the STATE ROOT)

<!-- schema:external=memecoin_wallet_impact_reads -->
`memecoin_wallet_impact_reads` has one row per source buy, keyed by the source signature. It is
written once, except for `unread` rows, which are rewritten on retry.

| column | type | note |
|---|---|---|
| `signature` | TEXT PK | source transaction |
| `source_user_id` | TEXT | |
| `network_id` | TEXT | `solana` |
| `mint` | TEXT | |
| `origin` | TEXT NOT NULL | `detection` / `fill` |
| `t0` | INTEGER | chain blockTime, seconds |
| `age_s` | INTEGER | t0 minus the mint's first transaction; NULL when the probe did not answer |
| `status` | TEXT NOT NULL | the status table above |
| `delta_sol`, `net_before`, `net_after` | REAL | NULL unless `ok` |
| `buyers_before`, `buyers_after` | INTEGER | NULL unless `ok` |
| `tx_before`, `tx_after`, `undecoded_before`, `undecoded_after` | INTEGER | NULL unless `ok` |
| `calls` | INTEGER | Helius calls spent on this read |
| `method_version` | TEXT NOT NULL | |
| `read_at` | TEXT NOT NULL | |

<!-- schema:external=memecoin_wallet_impact -->
`memecoin_wallet_impact` has one row per `(source_user_id, network_id, method_version)`. Every run
rewrites it (delete by `method_version`, then insert), the same way the edges are written.

| column | type | note |
|---|---|---|
| `source_user_id`, `network_id` | TEXT | |
| `n` | INTEGER | detection-fed ok reads |
| `median_delta_sol`, `p25_delta_sol`, `p75_delta_sol` | REAL | NULL when `n < 10` |
| `share_positive`, `median_delta_buyers`, `median_undecoded_share` | REAL | NULL when `n < 10` |
| `censored` | TEXT | JSON counts per censored status, plus `launch` |
| `censored_share` | REAL | NULL when the denominator is 0 |
| `n_fill`, `median_delta_sol_fill` | INTEGER, REAL | fill-fed; shown separately; the median is NULL when `n_fill < 10` |
| `k`, `k_needed`, `p_value`, `q_value` | REAL | NULL unless tested |
| `verdict` | TEXT NOT NULL | `unknown` / `untested` / `mover` / `not_distinguished` |
| `verdict_reason` | TEXT | for `unknown`: `n` or `censored` |
| `window_from`, `computed_at`, `method_version` | TEXT | |

The run's note in `memecoin_wallet_graph_runs.note` gains `impact_reads_new`, `impact_stop` and
`impact_calls`. Impact is not projected into the brain in this slice.

## Map changes

- **API** (`sniper/services/memecoin_wallet_map.py`). The builder reads `memecoin_wallet_impact` for
  the chain through the same read-only graph connection.
  - Each source node gains `impact`: `{n, median_delta_sol, p25, p75, share_positive,
    median_delta_buyers, median_undecoded_share, censored, censored_share, n_fill,
    median_delta_sol_fill, k, k_needed, q_value, verdict, verdict_reason}`, or `null` when the
    wallet has no row. A stranger's `impact` is `null`.
  - The stamp gains `impact: {method_version, computed_at, window_from, wallets_known}`, where
    `wallets_known` counts verdicts other than `unknown`.
  - A missing table (before the first run) gives `stamp.impact = null`. That is not an error.
- **UI** (`static/wallet_map.js`).
  - The size-by label is "flow change after vs before", never "moves the market" (Gamma L4).
  - Size-by defaults to it when `stamp.impact` has `wallets_known > 0`, and to `activity`
    otherwise.
  - Radius = `5 + sqrt(max(0, median_delta_sol)) * R`, where the plan fixes the constant `R`.
    - A negative median draws at the base size and shows its value in the hover.
    - `unknown` draws hollow at the base size, like unknown activity.
  - A `mover` gets a ring.
  - The hover shows:
    - the median ΔSOL, the IQR, n, the share positive, Δbuyers and the undecoded share;
    - the censored share with counts per reason, and the launch count;
    - the fill-fed n and median, labelled "our fills (selected by our gates)";
    - the verdict line:

      | verdict | hover text |
      |---|---|
      | `mover` | "k of n above the roster median, BH q …, this run only" |
      | `not_distinguished` | "k of n above the roster median; k_needed needed at this n, this run only" |
      | `untested` | "n < 20: not tested" |
      | `unknown` | "n < 10: unknown", or "censored share over 25%: unknown" |
  - The stamp adds: `flow change: <wallets_known> wallets · window from <window_from> · method <v>
    · computed <t>`. It prints the job's real window start, not "14 d": the constant is true only
    while the cron runs `--days 14` (Gamma, build review).

## Edge cases (CLAUDE.md rule 5)

1. **Money path:** none.
   - The reads use the research key only (`pick_research_url` refuses a funded or watcher key).
   - They run only while the Solana book is flat.
   - Nothing in the job or on the map arms, sizes, admits or exits.
2. **Fill-path speed:** none.
   - The job is a separate hourly cron under `flock -n`, on the research Helius host.
   - The runner's process is untouched.
3. **Concurrency:** the hourly job is the only writer of both tables, and `flock -n` prevents
   overlapping runs. The web opens the graph DB with `mode=ro` per request.
4. **Absent vs zero vs unknown:**
   - `delta_sol = 0` is an ok read whose flow balanced out.
   - A side with no trades is a known 0.0.
   - A censored read has NULL numbers and a reason.
   - An empty age probe is `unread`, never `launch`.
   - A wallet with `n < 10` or `censored_share > 0.25` is `unknown`.
   - A wallet with no row is `impact: null` (unknown).
   - A missing table is `stamp.impact = null`.
5. **Dependencies:**
   - A 429, quota, HTTP or RPC error, or timeout gives `unread` for that read, stops the pass for the
     run, and the read is retried next run.
   - Wrong answers the rule can see: t0 disagrees with the page (`clock_mismatch`); a pool quote is
     misread (`non_sol`); an index gap on the mint (empty probe).
   - Wrong answers it cannot fully see: `trade_of` counts one trade per transaction and misses
     aggregator routes. So the undecoded share is stored and shown. Its bias on `delta_sol` has no
     known sign.
6. **Units and identity:**
   - SOL: lamports / 1e9 on the pump curve, WSOL decimals on pools.
   - blockTime and `age_s` are in seconds. Order is `(slot, transactionIndex)`.
   - Wallet identity is `source_user_id`. A trader is a base58 owner address.
   - t0 never uses a detection-fed `detected_at` (host time).
7. **Sample:**
   - The units are first buys by wallets watched in the window (Gamma L2). On 2026-09-30, only 28 of
     the 65 are `real_approved`, because 284 pairs were disarmed on 09-29.
   - The main numbers use detection-fed units, so our own funded gates do not select them (Gamma
     M2). Fill-fed units are shown apart.
   - The numbers describe our watched wallets, never the market.
   - Launch buys are excluded, because their after-flow belongs to the launch crowd.
   - The busiest coins are censored by truncation (Gamma M1). A wallet over 25% censored is unknown.
   - ΔSOL is not scaled by coin size: 5 SOL on a nano coin weighs the same as on a mid coin.
8. **Rollback:**
   - Revert the commit. The two tables can stay; nothing else reads them.
   - A half-applied state is safe both ways:
     - new job with the old web: the tables fill and the map ignores them;
     - new web with the old job: `stamp.impact = null` and size-by falls back to activity.
   - The job runs from `.runtime/memecoin/current`. So the job half ships with Alpha's memecoin
     runtime deploy, and the map half ships with the web deploy.

## Checks left behind (each proven red on a mutant)

`tests/test_wallet_link_graph_impact.py` uses page fixtures, with no network:

- **Exclusions:**
  - our fill's exit signature in the after window is excluded (mutant: entry signatures only);
  - a second verified address of the source is excluded (mutant: exclude the source transaction's
    trader only);
- **Before/after split:** a trade at the same second but a lower transactionIndex counts as before
  (mutant: split by blockTime);
- **Age probe:**
  - `age_s = 60` gives `launch` and makes no window call; `age_s = 61` reads the window (mutant: `<`);
  - an empty age-probe page gives `unread`, never `launch`;
- **Censoring:**
  - a token left after 10 pages gives `truncated`, even when the source is missing from the cut page
    (mutant: `not_found` first);
  - a USDC trade in the window gives `non_sol`;
  - a page blockTime that differs from t0 gives `clock_mismatch`;
- **t0 and origin:**
  - t0 comes from the detection's `block_time`, never a detection-fed `detected_at`;
  - a signature with a detection row is `detection` origin; without one it is `fill`;
- **Per wallet:**
  - fill-fed reads never enter `n`, the median, `M` or the test (mutant: all origins pooled);
  - verdicts by n: n = 9 is `unknown`, n = 19 is `untested`, n = 20 is tested (mutant: test from 10);
  - a wallet with n = 20 and 7 censored of 27 (0.259) is `unknown` with reason `censored`; 6 of 26
    (0.231) is tested;
  - the leave-one-out median leaves out the wallet's own reads (mutant: pooled including self);
  - BH ordering and `k_needed` are checked on a hand-computed three-wallet case;
- **Budget:** a budget of 10 starts no read, because the worst case is 11 calls.

`tests/test_memecoin_wallet_map.py` gains three tests:

- a missing impact table gives `stamp.impact = null`;
- a stranger's `impact` is null;
- `mover`, `untested`, `unknown` and `verdict_reason` pass through unchanged.

`tests/test_memecoin_wallet_map_ui.py` gains three tests:

- the default size-by switches only when `wallets_known > 0`;
- a negative median draws at the base radius;
- the label never contains "moves the market".

## Out of scope

- EVM impact.
- Any-coin lookup.
- Projecting impact into the brain.
- Scaling by coin size or market cap.
- Forward grading of the `mover` verdict.
- A 61-300 s launch-crowd cut. `age_s` is stored, so that cut can come later.
- Raising the page cap.
- 3b upstream discovery, which has its own pre-registration.
