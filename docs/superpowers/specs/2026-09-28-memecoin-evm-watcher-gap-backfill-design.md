# EVM watcher: backfill the blocks lost across a gap (D2) — SPEC ONLY, ends in an operator question

## Amendment 2026-10-10: bounded canonical BSC decoding

Canonical BSC push receipts now leave the socket callback through the existing two-worker,
128-capacity decoder. The queued-age limit remains `MEMECOIN_ONCHAIN_LANE_MAX_AGE_S`; native
eligibility retains its original receive deadline. Original UTC/roster and J7 delivery survive
admission. Current source identity is rechecked before publication. Async admission never proves
coverage: pending/full/expired/failed work holds the restart frontier alongside gap and Relay work.
Existing synchronous bounded backfill may recover a hole; no-host failures remain unread until
named action-window expiry. Each worker owns ledger/tape handles. Shutdown persists conservative
coverage, fences new publication and drains already-claimed local publications for five seconds;
timeout is `watcher_publication_drain_timeout`. Shared read transport closes after workers stop.
This changes live BSC callback completion semantics; historical design text below is retained.
Evidence: `docs/audits/2026-10-10-bsc-entry-pipeline.md`.

## Historical proposal (2026-09-28)

Status: SPEC, nothing built. Author: Lambda, 2026-09-28. Open item "D2" from the 11:04Z watcher-gap
report. Alpha: spec only, ending in the operator question.

## What a gap is, and what it costs today

The push watcher (scripts/research/onchain_evm_wallet_watch.py) sees a watched wallet's buy only
while its log subscription is live. Nothing re-reads the blocks it missed:

- A **restart** (watchdog "previous release" relaunch on every deploy, or a coverage kill) is dark
  from the stop until the new process subscribes.
  - Since 11:08Z (stamped lines), 14 restarts measured stop -> "push mode on": p50 2.05 s, max
    2.91 s.
  - There were 32 watcher launches on 4663 today, a deploy-heavy day.
- A **stall or socket error** reconnects and drops the blocks in between.
  - 4663: 0 stall lines since 11:08Z, against 67 before b494ff21, whose stale check read the clock
    before a slow handler returned.
  - 56 and 8453: 1 socket error each since 11:00Z.
- The **poll fallback** after 120 s dark starts at head and cannot reach back. Its eth_getLogs fails
  on both chains anyway.

Expected loss now: about 20 watched J7 4663 buys in 6.5 h (about 3 an hour), against a dark share
of about 14 x 2.5 s / 7,200 s = 0.5%. That is about 0.4 lost watched buys a day. A buy is actionable
only inside the on-chain lane's 45 s window (MEMECOIN_ONCHAIN_LANE_MAX_AGE_S), so a backfill helps
only when it reads the gap within that window.

## Endpoints (measured 2026-09-28 13:14Z, 3 calls)

| host | eth_getLogs by Transfer topic, any token | verdict |
|---|---|---|
| robinhood-rpc.publicnode.com (4663) | -32701 "Please specify an address"; a 500-block span 403s as archive | no |
| bsc-rpc.publicnode.com (56) | -32701 "Please specify an address" | no |
| bsc-dataseed1.defibit.io (56) | JSON-RPC error (11:04Z report) | no |
| the custody read host (4663) | HTTP failure (11:04Z report) | no |
| Alchemy (the operator's key, now last in MEMECOIN_EVM_RPC_POOL_URLS_4663/8453/56) | getLogs by topic; the free tier caps a call at 10 blocks, about 1 s of 4663 | unmeasured, and a FUNDED pool member |

A recipient-only query needs no token address. At this measurement, only Alchemy was known to serve it.

2026-10-06 amendment: `MEMECOIN_ONCHAIN_EVM_BACKFILL_RPC_URL_<chain>` selects a dedicated
log-read host before the Alchemy pool fallback, for both gap recovery and websocket-dark polling.
The chain must still be enabled in `MEMECOIN_ONCHAIN_EVM_BACKFILL_CHAINS`; all pacing and bounds
remain unchanged. Arc Publicnode rejects this query with -32701; `https://5042.rpc.thirdweb.com`
accepted five bounded reads (p50 210 ms, p90 213 ms). These are fallback-read timings, not fill timings.

## Design, if approved

1. The watcher keeps `last_head`, the newest block it received. On every (re)subscribe, and at
   start from a small persisted file, it reads the gap `(last_head, head]` once, clipped to the
   newest 45 s of blocks.
2. `eth_getLogs` {topics: [Transfer, null, watched addresses]} in 10-block pages. At about 10 blocks
   a second, a 3 s restart is about 3 calls and a 45 s cap is at most 45 calls.
3. Each log goes through the SAME on_transfer path: per-tx dedupe, decode, commit, wake. A
   backfilled detection is marked `source_valuation_json.backfill = true`, so latency reads can
   exclude it.
4. Bounded and non-blocking: its own thread; a failed page is logged and skipped; no retry storm.

## Rule 5

1. **Money path.** One step removed: a backfilled detection wakes the on-chain lane, which runs
   every gate, including the 45 s age limit.
2. **Speed.** Off the live path: a separate thread after the subscription is live.
3. **Concurrency.** The backfill thread and the WS thread both call on_transfer. The per-tx dedupe
   dict is then shared and needs a lock.
4. **Absent vs zero.** A failed page is a hole, logged as one. It is never "no buys".
5. **Failures.**
   - A 429 on a FUNDED pool key cools that key for funded quotes and sends too. This is the main
     risk, and why the endpoint is the operator's call.
   - The cap bounds it: at most 45 calls per gap.
6. **Units.** Block numbers per chain. The 45 s window is converted with the chain's measured
   block time, never a constant.
7. **Sample.** Count backfilled detections, and how many were still inside 45 s.
8. **Rollback.** A knob, default off.

## Cheaper alternative (no endpoint needed)

Most of today's dark time is deploy restarts: 32 launches on 4663. The watchdog relaunches every
watcher on each new release, even when no watcher code changed. Relaunching only when the
watcher's own import closure changed would cut most restarts. Stall losses are already near zero
since b494ff21.

## Recommendation

Do not build the backfill now: the measured loss is about 0.4 watched buys a day. If it is ever
built, first measure the Alchemy getLogs cost and 429 behaviour on a non-funded key.

## Operator question

Use the Alchemy key, a member of the FUNDED RPC pools, for a bounded watcher backfill (at most 45
getLogs calls per gap)? Yes, or no, or keep it off until gaps grow. And separately: may the
watchdog skip watcher restarts on deploys that do not touch watcher code?

## Addendum A (2026-09-28): skip watcher restarts on deploys that do not touch the watcher

Operator: "if safe, yes" (question b above). Order: build this before the backfill.

### Rule

Today the watchdog's `upgrade_onchain_evm_watcher` stops every EVM watcher whose command line
names a release directory other than the current one; the next pass relaunches it. After this
change, it keeps an older-release watcher ONLY when all of the following hold. Otherwise it
restarts it exactly as today.

1. **Same code.** A fingerprint of the watcher's IMPORT CLOSURE is equal for the running release
   and the current release.
   - Closure: `scripts/research/onchain_evm_wallet_watch.py` plus every module it reaches,
     function-local imports included, restricted to files inside the release tree. It reaches
     sniper/services, sniper/settings, sniper/execution, sniper/util and more.
   - Computed statically by `modulefinder` over the release root.
   - A release with no computable fingerprint means restart.
2. **Same environment.** The watchers load .env only at start. The running watcher records the
   sha256 of the .env it loaded (heartbeat `env_sha`); if the current .env differs, restart. The
   WHOLE file is hashed, a superset of the keys any watcher reads: a knob change never survives a
   skip, at the cost of a few extra restarts. A heartbeat without `env_sha` (older watchers) means
   restart.
3. **Healthy.** The watcher's heartbeat is present. The degraded-watcher kill and the launch of a
   missing watcher are unchanged: a watcher that is down is always launched from the current
   release.

### Truthful reporting

- Each kept watcher logs one line per deploy in the watchdog log:
  `on-chain watcher chain=<c> kept on release <old sha8>: same watcher code and .env (fingerprint <f8>)`.
- The watcher heartbeat also carries `release` (its own release directory name). A post-deploy
  read therefore names an older release on purpose, rather than implying the deploy reached the
  watcher.

### Rule 5

1. **Money path.** One step removed: the watchers feed the on-chain lane. A wrong skip keeps an old
   decoder alive, which is why the closure is the unit and not the entry file.
2. **Speed.** The fingerprint runs only when a release mismatch is found (deploy time). It is
   cached per release SHA in data/watcher_fingerprints/, never computed on a detection path.
3. **Concurrency.** The watchdog is one process under flock; the cache file is written atomically.
4. **Absent vs equal.**
   - A missing fingerprint, a missing `env_sha`, or an unreadable .env: restart.
   - Unknown never means keep.
5. **Failures.**
   - A dynamic import that modulefinder cannot see would be missed. The watcher has none today
     (checked: no importlib or __import__ in the closure).
   - The fingerprint includes every file modulefinder resolves inside the tree, so over-inclusion
     only costs a restart.
6. **Identity.** Release SHA directory names; the fingerprint is sha256 over (relative path, bytes)
   pairs, sorted.
7. **Sample.** Count kept vs restarted watchers per deploy from the watchdog lines.
8. **Rollback.** MEMECOIN_WATCHER_RESTART_SKIP=0 in .env gives every deploy a restart, as before.
   A missing script also means restart.

### Tests

- A deploy that changes only a runner file keeps the watcher.
- One that changes a module the watcher imports, even inside a function, restarts it.
- One that changes .env restarts it.
- A watcher heartbeat without `env_sha` restarts.
- The watchdog harness keeps today's kill when the decision says restart.

### Addendum A outcome (2026-09-28): measured, never fires, DROPPED

- The watcher's static import closure is 268 repo files, and it includes the runner. The chain
  is settings/memecoin.py -> memecoin_v5_replay_gate -> ... -> db_memecoin -> memecoin_exits ->
  memecoin_real_runner, through function-local imports. A static closure must count them, or it
  would keep stale code.
- Fingerprints of the 6 releases on disk (f64457da .. 6bee26f6) differ for every consecutive
  pair, so the skip would have kept 0 of the last 5 deploys.
- A closure narrow enough to skip would be unsafe.
- Make-before-break (overlapping watchers) was also declined by Alpha: about 60 s of dark time a
  day against a double-detection path into funded admission. Nothing is built.
