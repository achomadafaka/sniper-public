# Recording the pre-signal path at decision time — design (SPEC ONLY, no build)

**Date:** 2026-09-30 · **Author:** Beta · **Order:** Alpha (operator question on trading setups), "spec, to
Gamma (no build): record the pre-signal path going forward at decision time from data already in hand,
with zero new calls on the entry path." · **Status:** BUILT (4663, option (a); prune horizon unchanged), beta/pre-signal-recording; section 3a rules stamped with Gamma 2026-09-30.

## 1. Why

The setup inventory (`docs/audits/2026-09-30-setup-inventory.md`) found no stored pre-signal price or
volume path on any chain, and the step 2 backfill cannot recover it within the research-host rules:

| chain | host allowed | what happened | calls |
|---|---|---|---|
| 56 | https://1rpc.io/bnb, 1/s | serves about 3-4 days of BSC blocks (600k back served, 800k not); receipts older than that pruned; HTTP 429 after 35 calls at 1.05 s spacing | 35 run + 12 probe |
| 4663 | research QuickNode, 0.45 s | `eth_getLogs` over 5 blocks answers; over 50 blocks is HTTP 413 (plan limit). One hour is about 36,000 blocks, so about 7,200 calls per decision against Alpha's 4,000-call budget for all 401 | 229 |
| solana | research Helius, flat book | cost probe only: 20 decisions, 383 calls, 37,274 transactions, 3,830 credits; mean 191.5 credits per decision (median 60, max 600; 4 of 20 cut at 60 pages, so the true mean is higher); about 164,000 credits extrapolated to 856 decisions | 383 |

A path that we keep at the moment of the decision costs nothing to read later. This spec says what we
already hold at that moment, per chain, and how to keep it.

## 2. What is in hand at decision time (zero new calls)

| chain | source already running | holds | retention | usable |
|---|---|---|---|---|
| 4663 | `memecoin_volume_writer` -> `data/memecoin_onchain_volume.db` `pool_ticks` (whole-chain Swap tape: V2, V3, V4, Pons; amounts, sqrt price, maker, block) + `block_times` + `pool_meta` | every pool's swaps | pruned at `observed_at < now - 3600 s`, every 60 s | **yes**: exactly [t - 1 h, t] if copied within seconds |
| 8453 | same writer | watched pools only (`watched_base_pools`) | same | partial: only pools we already watch |
| 56 | none: the volume gate reads `uncovered_bsc_v2`; `memecoin_bsc_v2_live` holds open-position pools only | nothing pre-signal | — | **no** |
| solana | none: the arrival lane (`memecoin_ticks.db` sol_tx) subscribes to OPEN positions' mints only; the slot lane reads the source transaction only | nothing pre-signal | — | **no** |
| all | tracked-wallet feed (`memecoin_events`), entry quotes | already durable | kept | already usable post hoc (the S-axis) |

## 3. Proposed recording (4663; 8453 on watched pools)

Extend the existing cron recorder `scripts/research/evm_signal_window_recorder.py` (runs every minute;
operator yes 2026-09-28; reads the tape and the ledger read-only; writes only
`data/research_signal_windows.db`). Today it copies each decided signal's pools from src - 1 s to
src + 1,200 s. Proposal: start the copy at **src - 3,600 s** (and record the decision's own block), so
each decision carries its pre-signal hour.

- **Zero calls:** a local SQLite read of a tape the volume writer already keeps. Nothing touches the
  entry path: the recorder is its own cron process, not the runner.
- **The retention race:** the tape prunes rows older than 3,600 s every 60 s, and the cron runs once a
  minute, so a window copied up to ~60 s after the decision loses its oldest ~60 s. Two options:
  (a) keep the window and flag `partial_start` with the exact missing seconds (no code outside the
  recorder); (b) raise the writer's prune horizon to 3,900 s. (b) touches a process whose tape feeds
  the funded volume gate: it only keeps rows longer, but it is the operator's call. **Recommend (a)
  first**, measure the loss, then decide.
- **Every decision, not just fills:** the recorder already keys on every entry attempt (filled or
  refused, one signal per source transaction and mint), so refusals get the same path.
- **Which pools:** every pool `pool_meta` maps to the mint, labelled; the executed pool is marked later
  from our receipt when there is a fill. The A1 rule (executed pool, else the busiest) is then a pure
  read.
- **Traps carried:** the V4 sign cutover (V4 rows before 2026-09-29T02:30:10Z carry the swapper sign;
  prices are unaffected); tape gaps and feed resets inside the window are recorded, never filled.

## 3a. Rules stamped with Gamma (2026-09-30, before any forward row exists)

- **Tape rows carry both amounts (checked 2026-09-30, last 20,000 4663 rows):** V3 and V4 swaps
  (13,827) hold amount0, amount1, sqrt_price_x96, pool, tx and maker, all non-null; Pons swaps (606)
  hold both amounts, tx and maker (no sqrt price). `block_times` holds the block timestamps
  (36,050 rows for 4663). So price = quote / coin per swap, the source's own swap and our fills are
  dropped by tx, and every swap has its block time.
- **Partial start (option a):** each window records `partial_start_s`, the exact seconds of the hour
  the tape had already pruned. last, low_5m, vol_5m and range_15m are unaffected. high_1h, high_pre
  and vol_base use the available span, vol_base over complete 5-minute buckets only. If
  `partial_start_s > 90`, the decision is C0 for C2, C4 and C5 (they need the full hour). pool_age
  stays ">= 1 h" unless the pool's creation falls inside the window (A1).
- **The source block (Gamma LOW 2):** `pre_ticks_n` and the pre-hour bound exclude the WHOLE source
  block, so swaps in that block before the source's own transaction are not counted as pre-signal.
  That is conservative; they are still stored (the post window starts at the source block) and a
  grader may include them by log index.
- **A full database says so (Gamma LOW 1):** past `--max-mb` the run result carries `"full": true` and
  `skipped_full`, so a size cap never reads as a quiet chain in the cron log.
- **Grading of the forward data:** the same A1 categories and FINDING rules, per chain, once a cell
  holds >= 20 decisions; train = the first 2/3 of each cell's decisions in time, test = the last 1/3.
  Nothing is graded before that.

## 4. Not covered, and what it would take (operator decisions, not zero-call)

- **56:** extend the volume writer to BSC V2/V3 pools (a new subscription on a 56 host); or give the
  research lane an archive-capable BSC key.
- **Solana:** a pre-signal pool tape needs a new stream (e.g. a LaserStream/websocket subscription by
  program) or a paid research budget (~164k Helius credits for a one-off 856-decision backfill, a
  lower bound).

## 5. Edge cases (CLAUDE.md rule 5)

1. Money path: no. A research cron reading local files; no entry, exit, sizing or signing path.
2. Fill path speed: nothing is added to the runner.
3. Concurrency: one cron process with its own lock file (already); SQLite readers never block the writer
   (WAL).
4. Absent vs zero: a window with no swaps is "0 swaps" only when the tape was healthy over it (no gap,
   no reset); otherwise it is "unknown", kept apart.
5. Dependencies: the volume writer's health; its gaps and resets are copied, and a dead writer means
   windows are marked missing, not empty.
6. Units: raw token amounts; prices are ratios within one pool, so decimals cancel.
7. Sample: every entry attempt with a source transaction, filled or refused.
8. Rollback: revert the window-start constant; the research database is the only thing written.

## 6. Ask

Gamma: confirm the recording is fit for the A1 features (last, high_1h/high_pre, low_5m, vol_5m,
vol_base, range_15m, pool_age) on 4663 going forward, and whether the ~60 s partial start in (a) is
acceptable. The operator decides 56 and Solana.
