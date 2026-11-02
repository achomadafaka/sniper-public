# 4663 fast lane: cached-only sends, micro-cuts, coverage

Date: 2026-10-14. Author: lambda. Approved in principle by Alpha 2026-10-14 (scope message
"Approved; spec, then plan, then TDD, then Gamma"). Branch `lambda/fast-cached-only`.


## 0. Revision 2 (2026-10-14, Alpha's answers)

- 3.4 (stale pre-state, a1) is DROPPED. Continuity proves no other swap hit the pool, not that the
  source swaps through it. Accepting an old quiet pool reopens SHROOM (13b180d9d): a fabricated
  post price and cap, and a min_out that no longer protects on a thin pool. `state_class` is still
  recorded (`exact` / `fresh`). Binding the source's pool from the solver's calldata is a later
  decode study.
- 3.2.2 (moving `chase_tape_move_shadow`) is DROPPED. Measured: running the read beside the hook
  adds p50 0.00 / p90 0.09 / max 0.67 ms (n=60). Only the `onchain_m5_read` substage mark stays.
- m5 prefetch: rejected. The gate excludes the source tx and needs it; a shifted window fails a
  floor gate open.
- 3.5: no new-pool warming. **The Chainstack inline host warms while idle** (operator via Alpha:
  "Go, it's unlimited / plenty"), as 3.6 below.
- coincurve: approved by the operator via Alpha; the runtime install is Alpha's at deploy.
- The cached-only knob is read per signal (`lane_cfg.MEMECOIN_RH_FAST_CACHED_ONLY`, like
  `MEMECOIN_RH_FAST_PARALLEL_SAFETY`), not at build time.

### 3.6 The inline host's idle warmer

- One thread, `rh-fast-inline-warm`. It runs only while `MEMECOIN_RH_FAST_CACHED_ONLY` is on and
  `MEMECOIN_RH_FAST_INLINE_WARM` is on (default `1`). Under cached-only the fill path makes no
  inline read, so the host is idle by construction; turning the knob off stops the warmer before
  any inline read can return.
- Work: the same `SafetyCache` priority queue as the research warmer (priority 0 for coins armed
  wallets touched: their signals, plus their buys in the last hour; then the activity rank). A
  claim set keeps both warmers off the same coin at once.
- Per coin: the inline host's facts and safety verdict. Stats go through the same `DexBudget`
  (DexScreener) only when the coin's stats are absent or due, and never faster than one call per
  `MEMECOIN_RH_FAST_INLINE_WARM_STATS_GAP_S` (default 1 s). A DexScreener 429 stops every lane
  stats call until restart, and the funded path's breaker shares that provider.
- Pacing: `MEMECOIN_RH_FAST_INLINE_WARM_GAP_S` (default 0.25 s) between verdicts.
- The first inline 429 (the pacer's counter rises) or quota answer (a raised or refused check whose
  text matches `429|quota|rate.?limit|too many requests|limit exceeded`) pauses the warmer for
  600 s and counts it. A provider failure never caches a verdict (`background_safety` without a
  rotation).
- Heartbeat: `inline_warm` JSON with verdicts, passes, refusals, errors, pauses and paused_until.

## 1. Goal

Send at p90 <= 30 ms from feed arrival on every signal the 4663 fast lane admits, and record that
number on every send. Keep every safety gate. Accept a stale pre-state only when the tape proves
it current. Measure cache coverage before and after.

## 2. Measured baseline (read-only, 2026-10-14)

Six live lane sends, 10-08..10-14, all on the legacy `SequencerSender`. Time 0 is our feed frame.
Monotonic receipt clock where present; otherwise two corrected-UTC stamps (`decided_at -
arrival_utc`). `sent_epoch` is host time and is never diffed against `arrival_utc`.

| row | decide verdict | tape wait | inline inputs | hook gates | inline safety | t_send | send reply | landed |
|---|---|---|---|---|---|---|---|---|
| 101 | +7 | 0 | 0 | 13 | cached | +26 | 171 | S+4 |
| 266 | +8 | 0 | 0 | 17 | 179 | +210 | 232 | S+5 |
| 274 | +121 | 109 | 236 | 12 | cached | +377 | 162 | S+7 |
| 100 | +91 | ? | 155 | 12 | 328 | +673 | 158 | S+9 |
| 177 | +71 | 63 | 219 | 13 | 376 | +688 | 196 | S+10 |
| 243 | +154 | 145 | 184 | 26 | 472 | +906 | 207 | S+11 |

- Blocks: 9.87/s (100 ms), from 60 s of the public feed.
- Feed lag: the first block of each sequencer second reaches us 121 / 220 / 283 ms after that
  second starts (min / p50 / p90; host clock within 0.1 ms of NTP).
- Sequencer: AWS us-east-2, warm RTT 103 ms.
- Floor from this host with zero compute: about S+3.
- Signing: eth_account on the pure-Python `NativeECCBackend`, p50 3.1 ms. `coincurve` is not
  installed.
- Feed sender recover (`Account.recover_transaction`): 4.2-6.4 ms per signal
  (`rh_feed_signals.recover_ms`).
- Cache state at the safety step since 10-07: 23 hits, 25 inline misses (avg 269 ms).
- The background warmer: research host paced at 2 s; verdict p50 9.9 s, p90 18.5 s. In the last
  24 h it made 3,891 verdicts over only 102 coins.
- The inline host is Chainstack: 38 calls at most in any run since 10-07, never a 429. Its
  plan's quota is unknown.
- Pool age at signal (from `pool_meta.resolved_at`, a lower bound on the true age): misses p10
  6,866 s, p50 88,943 s; hits p10 20,884 s. None of the 25 misses was within 60 s of its pool's
  creation; one was within 600 s.
- Decide estimate vs truth (`est_post_sqrt` vs `true_post_sqrt`, price error):
  - fresh `pre`, n=28: p50 0.14 %, p90 9.3 %, max 71 %;
  - stale `pre`, n=52: p50 0.43 %, p90 7.2 %.

## 3. Changes

### 3.1 Cached-only fast lane (4663)

`LiveLane.handle` today reads inline on a safety cache miss:
- `no_safety_verdict` or `safety_stale`: the inline inputs, plus a parallel inline safety read.

With `MEMECOIN_RH_FAST_CACHED_ONLY` on (default `1`) and `CHAIN_ID == 4663`, the lane refuses at
once with the precheck's own reason (`no_safety_verdict` / `safety_stale`):
- No inline read, no parallel safety read, no hook call.
- `LaneDispatch._on_datagram` releases the `LaneHolds` entry when `handle` returns, as for every
  refusal. The on-chain lane and the J7 path then judge the signal exactly as they do today
  (fail-open hand-off; no new code there).
- A missing supply (`no_mcap`) or missing market stats (`no_market_stats`) already refuse without
  an inline read. That is unchanged.
- Base 8453 (`BaseLive`) and the BSC lane are untouched. The knob is read at lane build time.

Safety is unchanged:
- A send still needs a passing `token_safety_fn` verdict, at most `MEMECOIN_RH_FAST_SAFETY_TTL_S`
  (60 s) old.
- Every hook gate still runs.
- The inline safety host keeps its URL and pacer, now idle on the fill path.

### 3.2 Micro-cuts

1. **Signing and sender recover through `coincurve`.**
   - eth_keys selects `CoinCurveECCBackend` automatically when `coincurve` is importable. Both
     `sign_tx_fn` (eth_account) and the feed's `Account.recover_transaction` speed up with no code
     change.
   - Change: pin `coincurve==21.0.0` in `requirements.txt`, plus a parity test (3.2.1).
   - Also a startup log line naming the active eth_keys backend in the runner and the feed
     process.
   - Measure first, in a scratch venv: sign and recover p50/p90 on both backends.
   - **Blocked:** the download needs the operator's yes (PyPI wheel, `coincurve-21.0.0`, about
     1.5 MB).
   - Installing into the runtime interpreter (`/home/bigpapi/.pyenv/versions/3.13.0`) changes
     every eth_keys user on its next restart, whatever the deployed SHA. That is the operator's
     step at deploy.
   - Rollback: `pip uninstall coincurve`, or `ECC_BACKEND_CLASS=eth_keys.backends.NativeECCBackend`
     in `.env`.
   - eth_account stays the only signing API.

   3.2.1 Parity check:
   - A test signs fixed and random transactions (legacy and type-2, chain 4663 and 56) with
     `KeyAPI(NativeECCBackend)` and `KeyAPI(CoinCurveECCBackend)` and asserts byte-identical raw
     transactions and identical recovered senders.
   - Both backends sign deterministically (RFC 6979) with low-s.
   - The test is skipped, with a reason, when coincurve is absent. The startup line makes an
     absent backend visible.

2. **`chase_tape_move_shadow` off the fill path.**
   - It is measurement only. Today `_shadow_tape_move` submits a read to the momentum pool inside
     the hook, between `probe_pre_volume` and `onchain_volume_m5`.
   - The substage's 5-11 ms includes `_own_m5_for_gate`, a real gate read, so the shadow's own
     share is unmeasured.
   - Change, for `delivery_lane == "rh_fast_lane"` only:
     - stamp `decision_at_s` and `measured_at_s` at the same point as today;
     - submit the read at trace finish, after the send;
     - mark the shadow from that finish.
   - `_read_tape_move_stamped` takes `measured_at_s` as an argument, so a deferred read measures
     the same instant.
   - A new substage `onchain_m5_read` is marked right after `_own_m5_for_gate`, so the two costs
     separate.
   - Other lanes are unchanged.

### 3.3 Recording every send

`rh_fast_lane_live` gains columns (`add_missing_columns`, written by `write_live_record`):

<!-- schema:external=rh_fast_lane_live -->
| column | type | meaning |
|---|---|---|
| `feed_to_send_ms` | REAL | `elapsed_receipt_ms(payload.receipt_clock, monotonic_ns at t_send)`; NULL when the clock is absent or from another boot (unknown, never 0) |
| `accepted_by` | TEXT | the shared transport's `accepted_by` provider id; NULL on the legacy sender or no accept |
| `state_class` | TEXT | `exact` (tape_post), `fresh` (pre within `MEMECOIN_RH_FAST_TAPE_MAX_LAG_BLOCKS`), `stale` (3.4), NULL for shared routes |

The heartbeat gains one JSON column, `send_counters`:
- per state class: sends, fills, `reverted`, and the stale switch's state (3.4);
- `feed_to_send_ms` p50/p90 over the process run.

### 3.4 Stale pre-state (a1)

In `_state_checks` (decide, feed process, main leg only), a `pre` row older than
`max_lag_blocks` is accepted as `state_class = "stale"` only when all of these hold:

1. The row is not `learned` (it came from the tail, not a provider read).
2. `S - tape.head <= 1`: the tail has ingested block S-1 or later.
3. `volume_feed` for 4663 has `since <= row.observed`: the writer's socket has been continuous
   since the state row was observed. Any reset since then is a cursor reset. Both stamps are
   corrected UTC.
4. `volume_feed.received` and `through` are within 15 s of now (the volume gate's freshness
   rule).
5. No unresolved `tick_gaps` row for 4663 overlaps `[row.block_no, S-1]` (`tape_gap_fn`).
   Note: 4663 writes no `tick_gaps` today; the reset log in (3) is the working evidence.
6. The stale switch is on (below).

Any other case stays `state_stale` and is re-asked as today.

Further rules:
- The bridge leg (`a_` prefix) keeps today's rule. `v4_bridge` is off live.
- In-memory continuity per pool holds by construction: a pool's rows are dropped only while its
  `pool_meta` is unresolved, and a row in memory means the meta was known from that row onward.
- `min_out` from the estimate stays the protection.

The stale switch lives in the lane (runner):
- `MEMECOIN_RH_FAST_STALE_STATE` (default `1`) turns stale acceptance on.
- `MEMECOIN_RH_FAST_STALE_STATE_SINCE` (ISO; default the deploy time) bounds the evidence window.
- From `rh_fast_lane_live` rows written after `SINCE`: once the stale class has 20 sends, compare
  the revert rate of its last 20 with the revert rate of the last 20 non-stale sends (`exact` +
  `fresh`).
- If the stale rate is above 2x the baseline, the lane refuses stale datagrams with
  `stale_state_off` and alerts once.
- The rule is derived from rows each time, so it survives restarts and needs no flag file. With
  no new stale sends it stays tripped.
- Reset: a new `SINCE`.
- Baseline below 20 sends: the comparison is unjudged and the beat says so. Proposed backstop:
  10 or more reverts in the last 20 stale sends trips regardless (Alpha to confirm).

### 3.5 Coverage

Before and after: signals per day by cache state at the safety step (`cache` / `no_safety_verdict`
/ `safety_stale`), from `rh_fast_lane_events.reason` and `safety_path`. Each report states its
window, n and query.

- **Warm new pools at creation: not built.** The data in section 2 shows it would have hit none of
  the 25 misses. Armed wallets buy coins whose pools are hours to days old.
- **The warmer's throughput is the lever.** It verdicts about 2.7 coins/min against about 100
  coins on a 60 s TTL.
- **Warming on the Chainstack inline host while idle** (its verdict p50 216 ms) is built only
  after its plan's quota is known. Probing a monthly quota is not a measurement we can take
  safely. That is a question for the operator.
- If the quota allows, the background worker takes a second slot on the inline host:
  - paced under the measured or documented rate;
  - yielding whenever an inline read is in flight;
  - counted in the heartbeat beside `inline_calls` / `inline_429`.

## 4. Edge cases (CLAUDE.md §5)

1. **Money path.** Yes: entry admission and signing.
   - 3.1 only removes a send path (a refusal).
   - 3.2.1's backend switch reaches signing, so it ships with the parity test and the operator
     installs the package.
   - 3.4 widens admission: estimated state on stale rows, bounded by `min_out` and the switch.
2. **Speed.** Nothing new is added to the fill path.
   - 3.1 removes 155-708 ms on misses.
   - 3.2 removes the signing and recover cost and the shadow submit.
   - 3.4 adds two indexed SQLite reads (`volume_feed`, `tick_gaps`) on the stale path only; p50
     and p90 are to be measured in the plan's first task. They replace a 63-145 ms tape wait.
3. **Concurrency.**
   - The stale switch reads rows on the lane thread; no shared mutable state.
   - The eth_keys backend is chosen at import, before threads start; nothing is mutated at
     runtime.
   - The deferred shadow read keeps today's bounded pool (`ShadowReads(workers=2, maxsize=32)`).
4. **Absent vs zero.**
   - `feed_to_send_ms` and `accepted_by` are NULL when unknown.
   - A missing `volume_feed` row, or `since`, `received` or `through` = NULL, fails the stale
     check (stays `state_stale`).
   - A baseline under 20 sends is unjudged, not 0.
5. **Dependency failures.**
   - Tape DB locked or unreadable: the stale check fails closed (`state_stale`), and `tape_gap_fn`
     returns True on error.
   - coincurve absent or broken: eth_keys falls back to native (today's speed); the startup line
     shows it.
6. **Units and identity.**
   - `volume_feed.since` and `pool_ticks.observed_at` are both corrected UTC.
   - `receipt_clock` is boot monotonic ns.
   - Block numbers are the feed `seq`.
   - `state_class` names are fixed strings.
7. **Sample.**
   - Revert rates count lane sends only.
   - The lane chooses its own sends (cached-only), so later fill rates describe the cached
     population, not the market. The report says so.
8. **Rollback.**
   - `MEMECOIN_RH_FAST_CACHED_ONLY=0` restores inline reads.
   - `MEMECOIN_RH_FAST_STALE_STATE=0` restores `state_stale`.
   - coincurve rollback is in 3.2.
   - New columns are additive. Half-applied (the runner on the new SHA, the feed on the old one)
     means no `stale` datagrams and the old behaviour.

Not handled:
- Same-block earlier swaps on the source's pool. The estimate cannot see them; `min_out` turns
  an underpriced estimate into a revert.
- The feed relay's lag.

## 5. Checks left behind

- A lane test: a cache-miss datagram on 4663 returns `no_safety_verdict` with zero inline calls
  and a released hold. With the knob at 0, it reads inline as today.
- A decide test per continuity condition: each failing condition yields `state_stale`; all
  passing yields `stale`.
- A switch test: 20 stale sends with a 2x revert rate trip `stale_state_off` and alert once.
- The coincurve parity test (3.2.1).
- A record test: a send writes `feed_to_send_ms`, `accepted_by` and `state_class`.

Live verification after deploy:
- `feed_to_send_ms` p90 <= 30 over the first 20 admitted sends;
- `accepted_by = '4663:sequencer'` on most of them;
- the cache-state report.

### 2026-10-14 amendment: state-stale grader identity

rh_state_stale_grade.py defaults its ledger to state_path('sniper_tracker.db'), preserving explicit --ledger. A J7 comparison requires the exact retained source transaction plus the existing wallet, mint, network and bounded decision window. Known different hashes are ignored; candidates without retained identity are reported as source_identity_unknown and censored. Nearby wallet/mint/time matches cannot become same-source outcomes or evidence for a fast-lane change. The grader remains read-only and changes no funded gate.
