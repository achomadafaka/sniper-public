# PumpSwap keep extension (Alpha ruling 2026-10-30)

Status: built on `lambda/ps-keep-extend`; not merged or deployed at the time of writing. Author Lambda, source review Zeta.

## Why
The capture worker keeps a graduation pool's tape for a keep window (`MEMECOIN_DESK_PS_KEEP_WINDOW_S`, 1260 s: create_pool -60 s to +1200 s) and deletes other live rows after `ps_retention_h` (2 h). A trade held up to 3600 s, or any outcome row past +1200 s, therefore cannot be measured: the 2026-10-30 `8fc3eaa5` crash fell 20 s after its pool's keep window and the rows were gone by the time the outcome table was built.

## What
`data/desk/pumpswap/keep_extend.json` (`{version, written_at, pools: {pool: until}}`, host receipt clock) is written every minute by `scripts/memecoin_desk_keep_extend_writer.py` from hard_filter PASS tickets first seen in the last 120 s (until first sight + 3760 s) and Office orders not closed or censored (until now + 3700 s). The worker reads it every 30 s:
- a pool already in `ps_keep` gets `keep_until = MAX(keep_until, until)`; `keep_from` is never touched; the pre-extension end is recorded once in `ps_keep_ext`, and a later ordinary anchor raises that baseline;
- a pool outside the stored set is stored while listed, with no `ps_keep` row and a `ps_pool_selection` row (real capture session) from its actual inclusion, never backfilled;
- expiry runs on every tick whether or not the file is readable.

## Rules that hold
- Missing, stale (older than 300 s), oversized, malformed or clock-disagreeing file: no change, one warning per cause per hour.
- Deleting the file, a shorter request and ordinary rollback never shorten a window; written windows age out.
- Pressure shed (80 percent of the PS cap, or host free space under 100 GB; back on below 70 percent and above 110 GB): all extended pools are dropped and each extended window returns to `max(newest ordinary baseline, now)`, logged and counted (`ps_ext_rolled_back`). Capture stays on. This is the only shortening.
- Indirect effect, stated: extended tape can change mark and entry availability for the pools involved. Policy, rails and entry code are unchanged; the funded path does not read `ps_keep` timing (only `retained_from` reads `keep_from`, which is unchanged).

## Reach and rollback
Reaches the worker only through a release directory and a repin of the universe watchdog cron line; the restart leaves a tape gap of typically 2-5 s and up to about a minute. Rollback: stop the writer (the list goes stale in 300 s and the worker ignores it) and repin to the previous release; windows already written age out.

## Trial
One day under the 60 GB PS cap; report rows and GB per day and host free space at 24 h; stop if free disk falls under 100 GB.
