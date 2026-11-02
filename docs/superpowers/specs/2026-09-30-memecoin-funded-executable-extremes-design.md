# Funded positions: executable top and bottom (spec delta)

**Date:** 2026-09-30 · **Author:** Lambda · **Status:** BUILT on `lambda/exec-extremes` (round 4 =
round 3, Gamma CLEAN for build at c7481d0c3, plus the build notes marked "(build)"). It folds in the operator's
three rulings and Gamma's review of 2373e44ab (2 MEDIUM, 3 LOW, marked "(Gamma M1)" and so on). The
operator's OK for M1 came through Alpha on 2026-09-30, on one condition: this spec lists every
funded reader of `max_gain_pct` / `time_to_peak_s` and the build repoints each (slice C). Round 3
adds that list and **corrects one fact of rounds 1 and 2**: the normal funded close does not write
the two columns at all (see the table below and slice B). No build before Gamma's CLEAN. · **Order:** operator via Alpha, 2026-09-30 (orders 367d4ad6 / 86d053ce): funded price
recording uses only the high-cadence executable setup; record each funded position's executable
top and bottom with timestamps and a sample count; never from display feeds; display-derived
values that stay for the UI are labelled display-only and never used by research or grades; no
change to exit decisions without a separate operator OK. · **Companion:**
`docs/audits/2026-09-30-display-data-inventory.md` (Gamma).

## What exists today (measured 2026-09-30, read-only)

| Surface | Writer | Source | Finding |
|---|---|---|---|
| `memecoin_route_series`, seam `exit_decision` | `_keep_route_quote` in `real_exit_sweep`; buffered; flushed in the runner maintenance pass | the exact whole-bag route preview (`expected_usdc`, `unit_price = expected_usdc / open_qty`) | **The high-cadence executable series.** Cadence p50 1.0 s Solana, 1.09 s BSC, 1.2 s Robinhood; p90 1.7 s or less. 167,574 rows since 2026-08-26. 1,295 of 1,732 funded positions have rows, 1,294 of them with a positive price; all 40 closed since 2026-09-29T11:00Z do |
| `executable_peak_price`, `executable_peak_at` | `real_exit_sweep`, only when `exact_policy` (a trail, de-risk or stall rule) | the same exact quote | Written on 32 of 40 recent funded rows. Where written it equals the series high exactly on all 32. It is a **rail input** (trail, de-risk, peak-stall) |
| `peak_price`, `peak_ts` | `real_exit_sweep` | the display mid (DexScreener where on; our chain-state marks on Solana; else the exact quote) | Display. Examples against the executable high: position 66981 +150.6% nominal vs +13.7% executable; 67001 +230.7% vs +9.2%; 66877 +33.4% vs -3.7% |
| `max_gain_pct`, `time_to_peak_s` | on a funded row only `write_off_real_position` and `finalize_close` (its one funded caller found is the runner's `recovered_orphan_close`). The normal funded close, `book_full_exit_and_finalize`, and `finalize_claimed_close_without_fill` do not write them | the **nominal** `peak_price` / `peak_ts` | **NULL on 1,731 of 1,733 closed funded rows** (read 2026-09-30T12:38Z). The 2 set rows are the write-offs 66881 and 66990. Rounds 1 and 2 said every funded close wrote them; that was read off the `finalize_close` docstring, not counted. Paper rows: 39,119 of 41,695 closed rows hold a value |
| a bottom (trough) | none | — | Not recorded anywhere |
| `memecoin_exit_market_state.executable_price` / `executable_peak` | exit materializer | — | NULL on all 363 recent rows |

Series coverage of a hold, with its window (Gamma LOW):

- 40 positions closed since 2026-09-29T11:00Z (read at 12:06Z): the first sample lands p50 0.8 s,
  p90 1.5 s after the fill; the last sample is p50 0.9 s, p90 13.1 s before the close.
- Gamma's read, 38 closed in the last 24 h: lead p50 0.9 s, p90 1.4 s; tail p50 0.9 s, p90 26.1 s,
  max 30,359 s.
- The tail depends on the window because the sweep skips a position whose close is in flight or
  whose tracking is paused, and those tails are long (66891: 7,338 s).

## Design

### Slice A: record the executable top and bottom (no exit-path change)

A new table in the funded ledger, written by one pass in the runner. Nothing in the exit loop
changes and no provider call is added.

- **Where the pass runs (build).** Its own daemon thread, `memecoin-exec-extremes`, with its own
  connection, once a minute. Rounds 1 to 3 put it "next to the route-series flush"; that flush sits
  in the main loop's 120-second reconcile block, and main-loop phases delay the next poll and the
  periodic exit sweep. On its own thread no entry, exit or main-loop phase waits for it. The only
  shared resource is the ledger's write lock, held for one short transaction per pass. A guard test
  pins that `exec_extremes_pass` is called from that thread only and that no exit or entry module
  names it.

<!-- schema:external=memecoin_position_exec_extremes -->
| column | type | note |
|---|---|---|
| `position_id` | INTEGER PK | funded (`execution_mode='real'`) positions only |
| `high_price`, `low_price` | REAL NOT NULL | max and min `unit_price` over the position's `exit_decision` rows with `unit_price > 0` |
| `high_at`, `low_at` | TEXT NOT NULL | `observed_at` of the first row at that price |
| `samples` | INTEGER NOT NULL | the number of rows used |
| `first_at`, `last_at` | TEXT NOT NULL | the series' own span |
| `basis` | TEXT NOT NULL | `exact_route:exit_decision` |
| `computed_at` | TEXT NOT NULL | |

- **Writer:** `m.upsert_exec_extremes(conn, position_id, computed_at)`. It runs three reads on the
  existing index `(position_id, seam, observed_at)`.
- **What the in-runner pass computes (Gamma M2).** `m.exec_extremes_pass(conn, now_iso)`, only on
  recent closes:
  - every funded position closed in the last 10 minutes is computed or recomputed, because a flush
    can land up to about 2 minutes after the close;
  - a funded position closed in the last 24 hours with no row is computed (this covers a restart);
  - at most 20 positions per pass. **Order under the cap (Gamma LOW):** the 10-minute window first,
    newest close first; then the 24-hour no-row set, newest first.
  - **The cutoffs are ISO 'T' strings built in Python (Gamma LOW)**, never SQLite
    `datetime('now')`: its ' ' separator sorts below every 'T' stamp of the same day (53 rows
    against 39 for "the last 24 h" on this ledger).
- **Measured (build), 2026-09-30, read-only on the live ledger:** the aggregate per position p50
  0.06 ms, p90 0.25 ms, max 2.65 ms over the 100 newest funded closes; the largest series (13,289
  rows) 5.7 ms; a 20-position batch of reads 1.5 to 4.3 ms; the two candidate selects 0.02 ms. The
  write transaction (20 upserts, 20 clears, commit) p50 0.09 ms on a scratch WAL database in RAM;
  the commit on the real disk was not measured.
- **The historical backfill is not in the runner.** The 1,294 older positions are computed by the
  operator script in slice D, off-process.
- **Open positions:** no row. A reader that wants the live top and bottom runs the same aggregate
  (`m.exec_extremes_live(conn, position_id)`).
- **No row means unknown.** A position with no persisted quote gets no row, never zeros.
- **Percentages are not stored.** `high_price / entry_price - 1` is derived at read time by one
  helper, `m.exec_extremes_pct(row, entry_price)`.
  - `entry_price` is the fill cost per token. `unit_price` is the whole-bag sell quote per token,
    net of our own exit impact and fees.
  - So a fresh position reads below zero by its round-trip cost, and a position's best executable
    gain can be negative (66877: -3.7%). The helper's docstring says so (Gamma LOW).
- `executable_peak_price` is **not touched**. It stays the rail input, written exactly as today.

### Slice B: no display-derived extremes on funded rows (Gamma M1)

`max_gain_pct` and `time_to_peak_s` stay what they are on paper rows: a display measure. On funded
rows they are NULL, and `memecoin_position_exec_extremes` is the only funded source.

- **Already true on all but 2 rows.** 1,731 of 1,733 closed funded rows hold NULL today, because the
  normal funded close never wrote the columns. This slice keeps it so on the two paths that do
  write them (a write-off, an orphaned-close recovery), and slice D clears the 2 rows that hold a
  display value.

- **Why not write the executable value into them.** Round 1 did. That would put two instruments
  under one column name: the whole-bag sell quote on funded rows (net of impact, can be negative)
  and the display mid on paper rows. Nothing in the row would say which, and any query over both
  modes would compare them.
- **How.** `finalize_close` and `write_off_real_position` are **not edited** (Gamma (c), the
  lower-risk option). The slice-A pass owns the two columns on funded rows: every position it
  visits has both set to NULL in the pass's transaction, **with or without a series (Gamma LOW:**
  6 of 297 funded closes in 7 days had no series, and a written-off one would keep its display
  value).
  - **Every clear writes the backup first (build).** `m.clear_funded_display_extremes` copies the
    old values to `memecoin_position_display_extremes_backup` and then sets them to NULL, for the
    pass and for the history script alike. Round 3 backed up only in the script; the pass would
    then have cleared the write-offs 66881 and 66990 (inside its 24-hour window at deploy) with no
    copy, against operator ruling 1.
  - Cost: a funded row shows the display value between its close and the next pass.
  - A funded row the pass never reached (a gap longer than 24 hours) keeps the display value until
    the slice-D script is run again; the script is idempotent.
- `time_to_high_s` for a funded position is `high_at - opened_recorded_at`; that stamp is the fill
  (Gamma LOW). It comes from the helper, not from a column.
- `peak_price` / `peak_ts` stay for the dashboard and are labelled display-only: a comment at the
  writer, and the API adds `"peak_basis": "display"` beside them.

### Slice C: readers

- One reader helper for research and grades: `m.funded_exec_extremes(conn, position_ids)`.
- **Every funded reader of the two columns is repointed in this build** (the operator's condition).
  The list is below.
- The research scripts in the audit's table 2 that read `peak_price` for funded rows move to the
  helper when each is next redone. This slice does not rewrite those.
- The dashboard shows the executable top and bottom on the funded position card (the live
  aggregate) next to the display peak, each with its label.

#### Every reference to `max_gain_pct` / `time_to_peak_s` (main at 9023f80d8)

Found with `grep -rnE "max_gain_pct|time_to_peak_s"` over `sniper/`, `scripts/`, `static/`,
`templates/`, `bot/` and the root `*.py`: 20 files. Each was read for the rows it touches.

**Funded readers (9 sites in 6 files), and what each becomes:**

| # | Site | Rows | What it does today | Repoint |
|---|---|---|---|---|
| 1 | `sniper/routes/api_memecoin.py:839`, `_real_ticker_snapshot` | open funded | emits `max_gain_pct` as the display `peak_price / entry - 1`; falls back to the column when `peak_price` is unset. The column is NULL on every open row (nothing writes it before a close) | the fallback is removed, so the value comes only from `peak_price`; `"peak_basis": "display"` is added. No executable read here: the ticker is the hot endpoint |
| 2 | `sniper/routes/api_memecoin.py:3082`, `_real_positions_read` | open funded | the same derivation and fallback; feeds the "Market peak" column | the fallback is removed; `peak_basis` is added; `exec_high_pct`, `exec_low_pct`, `exec_high_at`, `exec_low_at` and `exec_samples` are added from `m.exec_extremes_live` and `m.exec_extremes_pct` |
| 3 | `static/app.js:5716`, `5728`, `5766`, `6121` | the two payloads above | renders the payload's `max_gain_pct` as "Market peak" | the label stays, because the value is the display peak; the card adds "Sell-quote high / low" from the `exec_*` fields and shows a dash when they are absent |
| 4 | `sniper/services/memecoin_radar_view.py:177`, `radar_rows` (`real_open`) | open funded | `_hold_detail(pnl, max_gain_pct)` prints "peak +N%". The column is NULL on an open row, so that part never prints for a funded hold | the column leaves the SELECT and `id` joins it; the value is `m.exec_extremes_live`, printed as "sell-quote high +N%" |
| 5 | `sniper/services/memecoin_radar_view.py:184` (`real_closed`) | closed funded | selected, never used | dropped from the SELECT |
| 6 | `sniper/services/memecoin_radar_view.py:362`, `coin_timeline` (`arm_positions`) | every mode, funded included | passes the column through in the JSON; `static/app.js:7297` renders `peak_price`, not this key | on a funded row `max_gain_pct` is forced to None and `exec_high_pct` / `exec_low_pct` come from `m.funded_exec_extremes`; paper rows are unchanged; each row carries `max_gain_basis` (`display` on paper, None on funded) |
| 7 | `scripts/research/funded_winners_losers.py:46`, `192-217` (`OUTCOME_SIDE`) | closed funded | two outcome columns; NULL on 1,731 of 1,733 rows, so both are already blank in its output | replaced by `exec_high_pct`, `exec_low_pct` and `time_to_high_s` from `m.funded_exec_extremes`; a position with no extremes row stays None and is counted |
| 8 | `scripts/research/entry_side_money_map.py:100` | closed funded | selected, never consumed | dropped from the SELECT |
| 9 | `scripts/research/position_shape.py:109`, `156-157` | funded and paper | selects both; discards them on funded rows (`None if real`) | none. It stays on the guard's allowlist with that reason |

**Funded writers:** `db_memecoin.py:8013` (`write_off_real_position`) and `:8881` (`finalize_close`).
Both stay untouched (Gamma (c)); the slice-A pass sets the columns back to NULL.

**Not funded readers (the other 14 files), with the reason:**

- Paper rows only (`execution_mode='paper'` in the query or the caller):
  - `sniper/services/memecoin_paper_report.py:33`, `544` (`_project_rows` filters paper);
  - `sniper/persistence/db_memecoin.py:11548`, `_accumulate_arm` (both callers of `strategy_summary`
    pass the default `mode="paper"`);
  - `scripts/research/memecoin_conditioning_study.py:67`, `memecoin_real_vs_shadow.py:193` (the
    shadow arm) and `memecoin_bigcap_scalp_policy_study.py` (a local variable).
- Paper writers: `sniper/services/memecoin_exits.py:267`, `298` and
  `sniper/services/memecoin_copy_trader.py:208`, through `set_position_instrumentation`
  (`db_memecoin.py:6163`). Real positions are closed by `real_exit_sweep`, which calls neither.
- Another table (`memecoin_signal_outcomes.max_gain_pct`, or the feature table built from it):
  `sniper/services/memecoin_pump.py`, `db_memecoin.py:2251` and `9158-9337`,
  `static/app.js:3079-3082`, `scripts/research/memecoin_feature_store.py`,
  `memecoin_feature_scan.py` (`memecoin_signal_features`) and `memecoin_pattern_study.py`.
- Computed locally, no column read: `sniper/research/memecoin_strategy/pattern_paths.py`.
- A frozen constant with a comment: `sniper/analysis/exit_flex_v5.py:125` (`BAND_TIME_TO_PEAK_S`,
  queried 2026-07-25). No live read.
- A substring match only: `sniper/analysis/wallet_protocol/position_building.py`
  (`time_to_peak_size_ms`).
- The schema: `db_memecoin.py:1409`.

**Not covered by a grep:** a reader outside this repository (a saved query, a notebook, the custom
GPT link) that selects the columns. None is known. Such a reader already gets NULL on 1,731 of
1,733 funded rows.

**`SELECT *` pass-through (Gamma nit):** `db_memecoin.py` has `SELECT * FROM memecoin_positions`
readers (Gamma counted 17) that hand the whole row on, so a consumer reads the columns by KEY without the SQL naming
them. Every such key read is in the grep above (it finds the key name in Python and JavaScript
alike); the two funded ones were the API fallbacks, sites 1 and 2.

**The guard (replaces round 2's research-only guard for these two columns).**
`tests/test_memecoin_exec_extremes_readers.py` greps the same trees and pins the set of files that
name either column to the list above, each with its class. A new file, or a new funded read in a
listed file, fails the test until it is classified. For `peak_price` the round-2 guard over
`scripts/research/*.py` stays, with an allowlist that may only shrink.

### Slice D: history (operator ruling 1, amended by Gamma M1 and M2)

`scripts/memecoin_exec_extremes_history.py`, run by the operator, off the runner's process.

- **Dry run by default.** It prints the counts and writes nothing. `--confirm` writes.
- **Step 1: backfill the extremes rows.** Every closed funded position with a positive-price series
  and no extremes row gets one (1,295 positions on 2026-09-30, read-only count at build), in
  batches of 200, one transaction each.
- **Step 2: clear the display-derived columns, with a backup.** For every closed funded row whose
  `max_gain_pct` or `time_to_peak_s` is not NULL (2 rows on 2026-09-30: 66881 and 66990):
  - the old values go to a side table first, in the same transaction;
  - then both columns are set to NULL.

  <!-- schema:external=memecoin_position_display_extremes_backup -->
  | column | type | note |
  |---|---|---|
  | `position_id` | INTEGER PK | |
  | `old_max_gain_pct` | REAL | NULL when the row had none |
  | `old_time_to_peak_s` | REAL | NULL when the row had none |
  | `copied_at` | TEXT NOT NULL | |

  The backup row is written once and never overwritten.
- **Counts it prints:** closed funded rows; extremes rows present and written in this run; rows
  with no positive-price series (438 on 2026-09-30: 410 opened before the series began on
  2026-08-26, 27 after it with no persisted quote, 1 with no positive price); rows backed up in this
  run and earlier; rows cleared.
- **It refuses a ledger without the two tables** (exit 2, with the reason): the runner release
  creates them at start, so the script runs after the deploy. Its target queries took 15 ms
  read-only on the live ledger.
- **Idempotent.** A second `--confirm` run writes 0 extremes rows, backs up 0 rows and clears 0.
- **Undo:** `--restore --confirm` copies the backup values back onto the rows and leaves the backup
  table in place.
- Open positions and paper rows are never touched.

## Operator rulings (2026-09-30, relayed by Alpha)

1. **History: overwrite**, with the old display values copied to a side table first so it is
   undoable; rows with no series become NULL.
   - **Amended by Gamma M1, operator OK 2026-09-30 (relayed by Alpha):** the funded rows are NULL
     for every row, not overwritten with the executable value, and the executable numbers live only
     in `memecoin_position_exec_extremes`. The backup and the undo are unchanged. The condition is
     the reader list in slice C.
   - Round 3's count changes the size of this ruling, not its content: the history step backs up
     and clears 2 rows, not about 1,300.
2. **No chain-state basis.** Exact-route only.
3. **The web's chart mark write keeps running.** Its rows carry `source`. Research reads only the
   `executable` and `swap` source classes.

## Edge cases (CLAUDE.md rule 5)

1. **Money path.**
   - The exit loop, the rails, `executable_peak_price`, `finalize_close` and
     `write_off_real_position` are all untouched.
   - The pass writes one new table and sets two research columns to NULL on closed funded rows,
     from the maintenance thread.
2. **Exit-path speed (Gamma M2).**
   - No provider call, no lock and no write in the exit loop, the entry lanes or the main loop: the
     pass has its own thread and connection.
   - The pass takes at most 20 positions: reads 1.5 to 4.3 ms per 20 (measured on the live ledger),
     one write transaction (0.09 ms p50 on a scratch database; not measured on the real disk).
   - **The ledger write lock (build, Alpha).** The pass does every read first and opens its one
     write transaction for the writes only, so it holds the lock for the writes and the commit,
     not for the aggregates. When ANOTHER writer holds the lock (an exit booking, an entry), the
     pass does not queue behind it: its connection has `busy_timeout=250` ms, the pass rolls back,
     writes nothing, returns `busy=True` and the next pass (60 s later) does the work. An error
     that is not a held lock is raised and logged, never reported as busy.
   - **The commit on the real ledger file is measured after deploy, not before.** The pass returns
     `read_ms`, `lock_wait_ms` and `write_ms`, and the worker logs them on every pass that wrote,
     cleared or was busy. Lambda reads the first day of those lines and reports p50 and p90.
   - **First live lines (2026-09-30, release b52291e04):** 13:37:10Z `visited=20 written=20
     cleared=2 read_ms=80.6 write_ms=129.2`, 0.23 s after the thread started inside the runner's
     startup; 13:38:10Z `visited=19 written=19 cleared=0 read_ms=2.5 write_ms=0.2`. In that
     release `write_ms` ran from the end of the reads to the end of the commit, so it could not
     tell a 129 ms WAIT for the lock from a 129 ms HOLD, nor exclude a WAL auto-checkpoint on that
     commit. The pass now takes the lock with `BEGIN IMMEDIATE` and reports the wait
     (`lock_wait_ms`) apart from the hold (`write_ms`), and the worker's connection sets
     `wal_autocheckpoint=0`, so `write_ms` is the hold and nothing else.
   - **The funded writers' own busy timeout is 60,000 ms** (`get_connection`), so a hold of 129 ms
     cannot fail a funded write; it can delay one by that long.
   - The history script reads a batch before it writes it, for the same reason; it keeps the
     default 60 s busy timeout, because the operator runs it and it may wait.
   - The 1,295-row backfill runs in the operator script, not in the runner.
3. **Concurrency.**
   - In the runner, the extremes thread is the only writer of the new table. The operator script
     writes the same rows by the same idempotent upsert, so the two can overlap safely.
   - A position that closes while the pass runs is picked up on the next pass by the 10-minute
     recompute window.
4. **Absent vs zero vs unknown.**
   - No persisted quote: no row.
   - `unit_price` NULL or not positive: not a sample.
   - `samples`, `first_at` and `last_at` say how much of the hold the series saw, so a sparse series
     is visible instead of read as a flat price.
   - A funded row's `max_gain_pct` is NULL (not recorded here), never 0.
5. **Dependencies.**
   - The route-series buffer holds 2,000 rows and drops the oldest. A failed flush loses its batch,
     and a runner crash loses the buffer.
   - So the recorded high is a lower bound and the low an upper bound on what was quoted.
   - `samples` against the hold time shows the loss. The buffer's own `dropped_full` and `errors`
     counters stay in the heartbeat.
6. **Units and identity.**
   - `unit_price` is USD per token from the whole-bag sell quote; it includes our own price impact
     and fees.
   - After a partial exit it stays per token.
   - `entry_price` is USD per token paid.
   - Times are the corrected real clock (`observed_at`, `opened_recorded_at`).
   - Only seam `exit_decision` counts. `exit_audit` and `execution_quote` price other moments and
     quantities and are excluded.
7. **Sample.** Every funded position with at least one persisted exit-decision quote at a positive
   price. The sweep does not quote a position whose close is in flight or whose tracking is paused,
   so the series ends where quoting ended, not where the coin went. `last_at` against `closed_at`
   shows it.
8. **Rollback.**
   - Slices A and B: revert. The table can stay; nothing else reads it. Funded rows closed while
     it ran keep NULL in the two columns; `(peak_price / entry_price - 1) * 100` and
     `peak_ts - opened_at` rebuild the display values for them.
   - Slice D: `--restore --confirm` puts the backed-up values back. A run that stops half-way has
     backed up exactly the rows it cleared, because backup and clear share one transaction per
     batch.

## Checks left behind (each proven red on a mutant)

- Slices A and B, `tests/test_memecoin_exec_extremes.py`:
  - the high and low with their first timestamps, and the sample count, on a fixture series;
  - `exit_audit` rows and non-positive prices are not samples;
  - no rows gives no extremes row;
  - a late flush inside the 10-minute window updates the row;
  - a position closed 25 hours ago with no row is left to the operator script;
  - the 20-per-pass bound;
  - a paper position is never written, and its `max_gain_pct` is unchanged;
  - a computed funded position has NULL `max_gain_pct` and `time_to_peak_s`;
  - the REAL `finalize_close` and `write_off_real_position` run on a funded fixture and still
    close it (Gamma (c): the statements are untouched, and this pins that they stay that way).
- Slice C:
  - `tests/test_memecoin_exec_extremes_readers.py`: the file set that names `max_gain_pct` or
    `time_to_peak_s` equals the classified list; red when a file is added;
  - each repointed reader, on a funded fixture whose column holds a display value: the ticker and
    the positions payload do not return it when `peak_price` is unset; the positions payload
    carries the `exec_*` fields; `radar_rows` prints the sell-quote high; `coin_timeline` returns
    None in `max_gain_pct` on the funded row and the stored value on the paper row;
    `funded_winners_losers` reads the extremes and counts a position with no row;
  - the research guard over `peak_price`, with its allowlist.
- Where the pass runs: `exec_extremes_pass` is called once, inside `_exec_extremes_worker`, on its
  own connection; no exit or entry module names `exec_extremes`.
- A clear always leaves the backup row, and never overwrites the first copy.
- Slice D, `tests/test_memecoin_exec_extremes_history.py`:
  - a dry run writes nothing and prints the counts;
  - `--confirm` writes the missing extremes rows, backs up the old values, then sets both columns
    to NULL;
  - a second `--confirm` run writes 0, backs up 0 and clears 0, and the backup still holds the
    DISPLAY values;
  - `--restore --confirm` returns every row to its backed-up values;
  - open positions and paper rows are untouched.

## Out of scope

- Any change to a rail, to `executable_peak_price`, to the close statements, or to what the exit
  loop quotes.
- Re-running the studies in the audit.
- Chain-state extremes (operator ruling 2: exact-route only).
- The Solana exact-route rows in `memecoin_position_marks`: the fast lane's mark refresh is EVM-only,
  and the route series already holds those quotes.
