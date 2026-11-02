# PS capture auto-resume and WAL relief: port onto main's universe worker

Owner Lambda. Reviewers Delta, Zeta. Alpha order 2026-10-29: option (a), drop the promise tracker and feeds C/D (operator order 48d0986c retired the curve tape).
Archived source: `~/.cache/lambda/archive/promise-stack-2026-10-29.bundle` (branches lambda/promise-feeds, promise-feeds-rc2, promise-tracker, universe-wal-cap; base 558eec391).

## What is ported (live producer: `scripts/memecoin_desk_universe_worker.py`, `sniper/services/memecoin_desk_ps_tape.py`)

From the archive, re-applied against main (main's census paging and receipt-window bounds win wherever they overlap):
1. 5c91c188c WAL relief: `journal_size_limit`, the pinned WAL is named by holder pid in the alert (2026-10-23 stall: one reader pinned a 16.5 GB WAL).
2. 6ad45a208 auto-resume after a disk-cap stop: dwell, hourly flap budget, free-disk floor, truncatable WAL, hysteresis; the stop is a `ps_gap_spans` row.
3. a12e1e7b6 RC items 1-5: routine relief is `PRAGMA wal_checkpoint(PASSIVE)` only; spans are read by `ps_gaps()` and the census row; the stop is durable across restart; span writes are verified and retried; hysteresis default 0.25 GB and validated. Item 6 (paged census) is DROPPED: main's `e193e3ad8` and `83cfd4036` already bound those reads.
4. cff54fa1e and 90d1e80b1: a restore read error keeps capture OFF; a deferred resume does not leave its span close queued.
5. 1cfc0091c: pid-file test waits for `/proc/<pid>/cmdline` (flaky 967/1000).
6. New witnesses for the remaining RC#2 PS items, rebuilt from code (the original witness dir is gone):
   - R3 a window nested inside a longer or open stop span still reads as an outage;
   - R4 a stopped, sub-threshold WAL with unflushed frames is rechecked and reset while stopped;
   - R5 census page reads never drop a row that is stored late (held-back write).
Dropped: the promise tracker, feeds C/D, bucket-clock features, promise accounting (RC items 7-13).

## Edge cases

1. **Money path.** None. Desk research producer, never entry, exit, sizing or signing.
2. **Speed.** Nothing on the funded fill path. On the ingest thread the routine relief is PASSIVE: it never waits, never takes the checkpoint lock. TRUNCATE/RESTART stays an operator action (`scripts/wal_checkpoint.py --reclaim` rule). Any new per-frame cost is measured, not estimated, before merge.
3. **Concurrency.** Readers in other processes pin the WAL; the worker names them, never kills them. The worker's own threads share `self.files` connections: span writes go through one queue, verified, retried.
4. **Absent / zero / unknown.** A failed `ps_gap_spans` read is NOT "no stop": capture stays off and the filter stays subscribed until the state is read. A missing tape file means nothing to restore, and the tape is never created by the restore.
5. **Dependency failure.** Disk-full, locked DB, unreadable span table, a span write that matches no row (the closed span is then recorded itself), a pinned WAL that never frees (no resume, one page per 30 min).
6. **Units.** `ps_*_gb` knobs are decimal GB; byte counts via `ps_live_bytes()` (data + live WAL, never the file size, since a pruned file plateaus).
7. **Sample.** Gap spans are descriptive: a census hour overlapping a stop reads as an outage, never as a quiet pool.
8. **Rollback.** `MEMECOIN_DESK_PUMPSWAP_CAPTURE=0` (off) or resume knobs `ps_resume_*`; spans already written stay as the record.
9. **Restart mid-stop.** An open `disk_cap` span restores the stop and the resume history; the dwell counts from the ORIGINAL stop; other open spans close at start; a restore that cannot read keeps capture off.

## Not handled

- Reclaiming a huge WAL file (operator, with the pinner stopped).
- Offline recovery of a tape that was copied while stopped.
- Live WAL behavior under the real reader set: offline fixtures only until a post-deploy read.

## Follow-up notes (2026-10-29, after review start)

- **Commit 99ffc7170 wording.** Its message says "paged census" (item 6 of the archived a12e1e7b6). The paged census is NOT in this branch: main's `e193e3ad8` and `83cfd4036` already bound those reads. History is not rewritten; this note is the correction.
- **`gaps_in_range` counts points.** `ps_gaps()` now emits a gap point every 30 s inside a stop span (`SPAN_STEP_S`), so the structure `meta.gaps_in_range` (`len(gaps)` in `memecoin_desk_structure_tape.py`) is a count of gap POINTS, not events. A long stop adds many points: a 1 h stop is about 120. Any reader or prereg that treats `gaps_in_range` as an event count must use `gap_events` (census row) or the span table instead. Point gaps without spans are unchanged (one point each).
- **Span table lifecycle.** A real tape has no `ps_gap_spans` table until the first stop is written; reads never create it.

## Review round 2 (Zeta HOLD on a5560b832)

- **Item 3 corrected: intervals, not sampled points.** The 30 s point sample of the first fix missed windows narrower than its spacing (the 15 s structure window evaluated at `as_of - read_margin` while gaps are read through `as_of`). `SPAN_STEP_S` is gone. `memecoin_desk_structure_tape` now passes `ps_point_gaps` + `ps_gap_intervals` (clipped `(start, end)` tuples, an open span runs through `as_of`), and `structure.gap_hit` / `v23_facts._gap` overlap-test every window. `ps_gaps()` keeps its old float-only shape (clipped start, plus end when inside) for readers that only ask "any outage in the window". **This supersedes the follow-up note above:** `meta.gaps_in_range` counts events again (point gaps + one per span), not points. Regression runs through the real `structure_for` for the shortest window at three read margins.
- **Census row: unknown, not "no outage".** `hour_facts` returns `gap_spans_unreadable` (`None`, or `malformed:columns=...` / `unreadable:<error>`). Only a MISSING table is the backward-compatible "none". `PT.ps_gap_spans` already raises `TapeUnreadable` for a malformed table.
- **TRUNCATE at DayFiles open: CHANGED to PASSIVE (Alpha order, AGENTS "Routine SQLite maintenance" has no desk exemption).** Superseded text follows. Original:  `DayFiles.conn()` runs `PRAGMA wal_checkpoint(TRUNCATE)` once when it first opens `data/desk/pumpswap/tape.db`, on that connection with `timeout=0`: a reader pin answers busy at once and nothing waits. It touches only the desk PS day-file DB, never the main ledger (`sniper_tracker.db`), and it runs before any ingest thread uses the connection. The PASSIVE-only rule in CLAUDE.md is the main-ledger cron (`scripts/wal_checkpoint.py`); the routine relief this branch adds (`ps_wal_relief`) is PASSIVE only. So the open-time fold is on the right side of that rule. If the rule is meant to cover desk tapes too, that is a one-line change in main's `DayFiles`, not part of this port.

## Review round 3 (Delta)

- **Legacy two-column `ps_gap_spans` at startup.** `restore_ps_stop` now upgrades an older `(start_at, end_at)` table (adds `cause` and `ended_by`, marks cause-less rows `legacy`, adds the unique stop index; no time is changed, no row dropped) BEFORE its query names those columns. Closed-only legacy table: capture stays on. An OPEN legacy span is a stop of unknown cause (`cause: "legacy"`): restored as stopped, never failed open, closed by a resume through the normal guards. The live tape today has NO span table (read-only check 2026-10-29), so this path only matters for tapes that carry the older shape.
- **A checkpoint that did not run is not clean.** PASSIVE answering `(busy=1, -1, -1)` means no frame was checkpointed: `ps_wal_clean` stays False, the WAL keeps counting toward the cap, `ps_wal_checkpoint_busy` counts it, and `ps_wal_unknown_last` blocks a resume (the old caller only blocked at or above `ps_wal_try_gb`). Before, `-1` frames read as "nothing pinned" and a full WAL counted as 0.
