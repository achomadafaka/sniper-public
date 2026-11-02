# Paper path without DexScreener (entry-gates spec §7, item 3)

Date: 2026-10-08. Author: Lambda. Order: Alpha (operator: "get rid of dex from the money path, and if
possible from the paper path too"). Status: PROPOSED; each step to Gamma before it is built.

## 1. Where the paper path stands (measured 2026-10-08)

- Every paper entry is `funded_clone`, and since today's cutover (`MEMECOIN_PAPER_EXIT_ENGINE=
  onchain_ticks_v1`) each is frozen on the v1 engine: 363 v1 opens today, 157 v0 opens before the
  cutover, all closed. Open now: 49 paper positions, all v1 (solana 39, 4663 6, 56 4). So step 1 of
  §7 ("move the v0 arms to v1") is already true; v0 lives only as the rollback path.
- v1 exit DECISIONS read on-chain ticks only (Solana paper vault stream, the 4663/8453 tape, the 56
  bag quote). DexScreener still feeds four paper readers:

| Reader | Where | What it does today | v1 needs it? |
|---|---|---|---|
| A. paper price pass, `dexscreener_stats_batch_keyed(keys)` every ~15 s, plus the GT batch and blind-liquidity refresh for 4663 | `memecoin_runner.py:904-962` (`run_exit_sweep`) | feeds B, C, D and the v0 sweep | no: `sweep_exits` skips v1 except the 14-day backstop, which is a clock rail |
| B. `write_marks` of that map as `paper_dexscreener` | `memecoin_exits.sweep_exits` | the paper chart / candles; it also writes rows for coins a v1 position holds | display only |
| C. `record_quote_observations` and `record_paper_path` | `memecoin_runner.py:966-972` | the dense quote ledger (exit-search input) and the per-position paper price path | research only |
| D. `_entry_stats` at paper entry | `memecoin_copy_trader.py:149` (`copy_event_to_arms`) | entry snapshot features: mcap, liquidity, token age (pairCreatedAt), the observational m5 inputs | research only; entry_mcap / liquidity feed paper fill slippage where the quote has none |

## 2. Steps

### Step 1: the v1 tick becomes the paper mark and the paper path

- The v1 engine already holds each position's tick (price, `tick_kind`, source, age). It writes it to
  the mark tape (`memecoin_position_marks`, source `paper_tick:<kind>`, e.g. `paper_tick:v4_spot`,
  `paper_tick:sol_state`, `paper_tick:bag_quote`) at most every PERSIST_QUOTE_S (15 s, today's
  `last_quote_price` cadence) and appends the same point to the paper path (`record_paper_path`,
  basis `tick`).
- `sweep_exits` stops writing `paper_dexscreener` marks for coins held only by v1 positions.
- Rule 5: money path no (paper display and research). Speed: the v1 thread's own connection, one
  write per position per 15 s. Concurrency: the v1 thread already owns its rows. Unknown: no tick =
  no mark (never a mid). Units: the tick's USD price per token, as `last_quote_price`. Sample: v1
  positions only; v0 history keeps its rows. Rollback: revert; marks then come from the sweep again.

### Step 2: the paper price pass fetches only for v0

- `run_exit_sweep` calls DexScreener (and the 4663 GT batch / blind-liquidity refresh) only when an
  open v0 position exists. Today that is never; after a rollback of the engine knob it is the same
  code as now.
- The quote ledger (C, `record_quote_observations` -> `memecoin_quote_ledger`) is RETIRED for v1
  coins, not fed from ticks. Feeding it from ticks would put two bases in one table (tick spot next to
  provider mid), and every reader would have to split by `source`.
  - Its readers are research only, with no rail, gate or funded reader (Gamma, 2026-10-08).
  - Readers that go empty for v1 coins:
    - `scripts/memecoin_promotion_lane_loop.py` `exit_series` (the loop is not running; it already
      treats empty as unquoted);
    - `memecoin_paper_report.py`'s close-mid coverage line;
    - `brain_health.py`'s INFO row count drops.
  - v0 rows keep writing while a v0 position is open.
- The 14-day backstop needs no price (it is a clock rail; the close is `executable_exit_fill`).
- Rule 5: money path no. Speed: removes ~2 DexScreener batch calls per 15 s (~11k/day) and the GT
  batch from the paper thread. Absent: a v1 coin gets no DexScreener row at all, by design.
  Rollback: the engine knob back to v0 restores the fetch automatically (it keys on open v0 rows).

### Step 3: paper entry facts from the funded decision, not a second GET

A `funded_clone` paper entry follows a funded decision on the same origin. `_entry_stats`
(`memecoin_copy_trader.py:149`) stops calling the stats function for it. The entry takes that attempt's
classification facts instead.

NEW NAMES, PROVIDER NAMES ABSENT (Gamma, 2026-10-08). A `facts_source` column alone is not enough:
unaware readers pool by key name. The rules:
- The funded facts go to `features_json["funded_facts"]` = {attempt_id, effective_mcap, mcap_basis,
  liquidity, liquidity_source, pair_age_s, mint_age_s, volume_m5, volume_h1, own-tape evidence}.
- The DexScreener keys of the stats payload are ABSENT on new rows: price, market_cap, liquidity,
  pair_address, volume_*, txns_*, token_age_s, symbol, chain.
- An unaware reader then sees unknown, never another basis under the old name. No funded attempt for the
  origin means `funded_facts` is absent, never a DexScreener fill-in.

What each paper-ledger field becomes:
- `entry_mcap` (position column; the paper policy band in `_paper_assignments`): the order is now
  `their_market_cap`, then the funded attempt's `effective_mcap`, then `resolve_mint_mcap`. The DexScreener
  step is replaced, so a follow_activity coin (`their_market_cap` 0.0) bands on what funded banded on.
  - This column already mixes bases today (event / DexScreener / feed median), with no label.
  - `funded_facts.mcap_basis` now names the basis whenever the funded value was used.
- `entry_liquidity` and `entry_token_age_s` (position columns): NULL on new rows.
  - Our liquidity is a different quantity (E6: pool-store virtual depth, or the implied quote reserve).
  - The mint age is not the pair age.
  - Both live in `funded_facts` under their own names.
  - Paper fill slippage with an unknown `entry_liq` keeps today's unknown-liquidity rule: the paper
    quote's own price impact, else the cap. No paper fill is priced on a provider number.
- `symbol` (ticker backfill for follow_activity) comes from the funded attempt's ticker, else unknown.
  `chain` (the EVM network hint in `_entry_network`) is not needed: a funded attempt has a resolved
  `network_id`.
- `paper_m5_gate` (observational, `_paper_m5_gate`) takes the attempt's gate inputs
  (`gate_inputs_json`) rather than recomputing from a DexScreener payload.

Readers this changes:
- `memecoin_chart_truth` loses the entry snapshot's `pair_address` as a pool source for new coins
  (`memecoin_chart_truth.py:86-94`). Its other source, `memecoin_quote_observations_v2.pair_address`
  (`:83`), continues while capture_v2 runs.
- Research cohorts reading `features_json` price, liquidity and age keys read unknown on new rows.

Rule 5:
- Money path: no. Paper ledger only: entry snapshot, paper policy band, paper fill.
- Speed: removes one DexScreener GET per paper entry (~500/day) from the paper fan-out thread.
- Unknown: no funded attempt means unknown, never a provider fill-in.
- Units: `effective_mcap` is FDV on chains where E1 is ours.
- Sample: the paper policy band can move for rows where `their_market_cap` was missing and the funded
  mcap differs from DexScreener's. Gamma reads the band agreement on 1 day of both values before the
  switch (DexScreener still fetched as a shadow into `features_json["provider_shadow"]`, then dropped).

**2026-10-10 amendment:** the one-day provider comparison is retired (`PROVIDER_SHADOW=False`).
Existing namespaced evidence remains readable; new paper entries use the same funded facts
without fetching another provider payload. This does not change funded authority or paper gates.
- Rollback: revert.

### Step 4: what is left

- `mark_positions` (`memecoin_marks.py`) reads the sweep's map when given one; after step 2 it reads
  the v1 last tick (`last_quote_price`) for v1 rows. The web reads stored marks only.
- With steps 1-3, no paper reader calls DexScreener while v1 is the engine.

## 3. Out of scope: capture_v2

capture_v2 (`memecoin_quote_observations_v2`) is NOT changed by this spec. Its rows feed the FUNDED
classifier's momentum: `_momentum`, which drives `post_peak` nomination and the `hot_narrow` clip scale.
So moving capture_v2 off DexScreener (inventory P3) would change funded admission and sizing. That
change belongs to E14 (`2026-10-08-memecoin-funded-momentum-own-data-design.md`), shadow then switch per
chain. capture_v2 keeps writing for funded momentum, chart truth and research until E14 and P3 retire
its reader set.

## 4. Order and checks

Step 1, then 2, then 3, each its own branch, red tests first, Gamma. Each leaves a test: a v1-only
coin gets no `paper_dexscreener` mark (1); `run_exit_sweep` makes no DexScreener call with zero
open v0 rows and one with a v0 row (2); a funded_clone entry with a funded attempt makes no stats
call, writes `funded_facts`, and carries none of the DexScreener keys (3).
