# Helius history backfill: design (Lambda, 2026-10-27)

Status: **design only, for Alpha, Gamma (feature list) and Delta (review).** Ordered by Alpha 2026-10-27 on the PAID Helius key `a56bd061` (brain decision 36dee152, nickname riderebony).
Research only, nightly, nice 19 / ionice idle. Never the funded provider, never a funded key. Columns requested by Gamma are in section 3.

## 1. What and why
Two jobs, one tool: (1) fill the PumpSwap tape holes (the 10-25 day, restart gaps) with exact in-slot order; (2) fetch the FULL trade history of every coin intake and the desk saw that
day (graduated venues included), because the tape reader's three history features (creator_sold, top_buyer, holders_pos) need since-launch history and our own tape does not have it.
Backfill rows are `source='helius_history'`, carry block_time, NOT our receipt time, and are never silently mixed with own-tape rows (Gamma: train/holdout/eval split by source; holdout stays own-tape).

## 2. Key, budget, ledger and the quota rule (revised after Delta's quota review)
- **Key.** Reuse `MEMECOIN_RESEARCH_HELIUS_RPC_URL` (no new URL knob; Alpha 10-27). `a56bd061` is the RAW api-key prefix; `key_id()` (sha256 prefix) names the same key `e1f95763`.
  Startup pin: the URL's api-key must start `a56bd061`, else fail closed (nothing fetched, a typed run row). The backfill uses ONLY that URL (it does not join the rotation; `dea05c94` is never touched).
- **Shared-key boundary, stated honestly.** `MEMECOIN_RH_FAST_DEPOSIT_HELIUS_URL` is the same key: funded 4663 deposit proofs (operator-approved 10-07) spend the same account credits at any time, and we cannot read
  remaining credits from the provider. The design therefore does NOT track the account's remaining credits and does not claim to. It bounds only OUR spend, by a cap sized once per billing cycle:
  `data/desk/backfill/cycle.json` = `{attested_at, attested_by, epoch_id, epoch_start, epoch_end, plan_credits, used_at_activation, month_cap, funded_reserve}` (operator, once per billing cycle; Alpha 10-27: a daily number would never be typed).
  Load-time checks, each a refusal with a typed gap: file present; `epoch_start <= now < epoch_end`; (sizing check below: the cap is sized so the funded reserve plus a 25% margin always remains);
  `attested_at` must be in `[epoch_start, now]` (a future or out-of-epoch stamp is refused); `used_at_activation` = the account's credits already spent this cycle at activation (one-time dashboard number, 0 only if the cycle is untouched; missing = refuse, so mid-cycle plan arithmetic cannot overstate headroom);
  all numbers finite and `>= 0` (`plan_credits`, `month_cap`, `funded_reserve` `> 0`). The sizing check uses what is left: `plan_credits - used_at_activation - month_cap >= funded_reserve * 1.25`. Usable budget of a run = `min(run_cap, month_cap - epoch_ledger_total)` where `epoch_ledger_total` = reserved + committed + ambiguous in the ledger for this `epoch_id`.
  What this protects and what it does not: the backfill can never take more than `month_cap`, so IF funded use stays within `funded_reserve` the funded proofs are never starved by us. If funded use exceeds its reserve, or
  other consumers use the account, this cap does not know; the persisted `max usage` stop below is the backstop for us, and the funded lane's own credit page remains its alarm. No funded reader or funded knob changes.
- **One persistent ledger** `data/desk/backfill/credits.db` (NOT the daily output DBs; daily files never reset monthly use): `ledger(attempt_id PK, epoch_id, method, reserved_credits, committed_credits, state, ts)` with `state in (reserved, committed, ambiguous)`.
  An upper-bound cost is RESERVED and committed to disk BEFORE every request attempt (prototype run, each page, each retry, pagination, every failed attempt). On a clean response the row becomes `committed` at the documented cost.
  A timeout, lost response, connection reset, crash after reserve, or malformed body leaves the row `ambiguous` and it COUNTS in full: a restart never refunds an uncertain send. A method with no entry in the cost table is refused,
  never guessed. The cost table is a documented upper bound; the 20-pool prototype run calibrates it against the dashboard delta before the nightly job is enabled.
  **Amendment 2026-10-28 (Alpha order, corrected after Delta's review): the cost model is an OPERATOR-ATTESTED, SHARED-KEY, FLOOR-ONLY assumption.** The paid key `a56bd061` is also spent by the research rotation and the funded RH deposit proofs, so the dashboard counts more than this ledger and a physical per-call ceiling is NOT provable. The ledger charges 10 credits per attempt INCLUDING failed attempts (the docs bill a failed response at 0; that is a documented-model assumption, not a verified upper bound). It was attested by `attest-costs --shared-key --expected-min 430` on a dashboard delta of +800: 430 = 42 successful calibration calls x 10 + one 10-credit header probe, and the delta only has to REACH that aggregate floor. The floor cannot detect an undercounting model (other consumers could supply all of the +800); it only refuses a zero or lower delta. The attestation never claims a validated upper bound, a per-call verification or the account's remaining credits. The month cap (5M) and the nightly run cap are enforced on THIS ledger model; the operator's dashboard is read after the first night's run (run cap 20,000) to re-check about 10 credits per call before the cap is raised to 100,000. The provider returns no credit header (probe 2026-10-28) and the dashboard lags by hours.
- **Immutable epoch identity.** The ledger stores `(epoch_id, epoch_start, epoch_end, plan_credits, used_at_activation)` at the first reservation of an epoch and refuses any later `cycle.json` that names the same `epoch_id` with different values for those fields. Changing `month_cap`, `funded_reserve` or `attested_at` never resets usage: spend is summed by `epoch_id` over reserved + committed + ambiguous.
- **No overlapping or renamed epochs.** The ledger persists every epoch's `[epoch_start, epoch_end)` and refuses a new `epoch_id` whose dates overlap any stored epoch (a renamed alias, identical or partial overlap would open a fresh zero-spend bucket in a real cycle).
  Epoch dates are OUR accounting boundary: the next `cycle.json` must start at or after the previous `epoch_end` (so if the previous end was set late as a safety margin, the next epoch starts there, not at the provider's reset).
  Credit values (cost table, `actual`) are non-negative integers (no bool, float, negative or zero cost); `commit` works only on this epoch's still-open `reserved` attempt and every terminal state (`committed`, `ambiguous`) is immutable.
- **Billing epoch** = the provider's billing cycle named in `cycle.json` (`epoch_id`, `epoch_start`, `epoch_end`, UTC). Usage is summed by `epoch_id`. At `epoch_end` the previous epoch's ledger is closed; a new run needs a NEW `cycle.json` naming the new epoch (once per cycle).
  A missing, expired or mismatched file is a refusal (`no_cycle_attestation`).
- **Quota stop persists.** `stops(epoch_id, utc_night, reason, ts)` in `credits.db`: a `max usage` finding writes a stop for the whole UTC night (and for the epoch if the body says monthly); every start or cron re-run that night refuses on it. NEVER retried that night.
- **Classification (HTTP and JSON-RPC 200).** QUOTA = body text contains `max usage` (case-insensitive) in an HTTP 429/402/403 body OR in a JSON-RPC `error.message` returned with HTTP 200. RATE/BATCH = 429 without that text (brain trap 9f0cd7fc):
  shrink the batch, bounded to 3 retries per page with 1-30 s backoff, EACH retry reserved and charged. Anything else non-2xx is `http_error` (typed, charged as ambiguous if the send may have been processed).

## 3. What is stored (`data/desk/backfill/<day>.db`, one writer, flock)
- `swaps_hist` (every atomic amount and reserve is DECIMAL TEXT, never a float or a JSON number; uniqueness `(signature, instruction_index, inner_index, event_ordinal)` (event_ordinal = position among the events one instruction emits; 0 when one) enforced GLOBALLY across day files through a `backfill/seen.db`, like the news poller): network_id, venue (`pump_curve` | `pumpswap` | other), pool, mint, signature, ordinal (in-tx instruction order), slot, tx_index, block_time, is_buy, actor, quote_atomic, token_atomic,
  quote_mint, pre/post base+quote reserves (curve: virtual + real), lp/protocol/creator fee components, fee_bps (as the live capture), source `helius_history`, fetched_at. Failed txs excluded.
  Sort key `(slot, tx_index, ordinal)`; the Helius `getTransactionsForAddress` filter/sort and `transactionIndex` give exact in-slot order for one address without getBlock (brain efd5e679).
- `reserve_events`: non-swap reserve changes on the same pool (add/remove liquidity, create_pool / migration, withdraw): kind, slot, tx_index, amounts. They explain reserve breaks.
- `launches_hist`: mint, creator, first swapper, launch slot, graduation slot (curve complete -> pool create).
- `pool_runs`: one row per (pool, day): status `complete | reserve_break | partial`, typed gap (`credits_exhausted | max_usage | unreadable_tx | pagination_incomplete | rate_limited | decode_unsupported`),
  credits used, rows, first/last slot fetched. **A pool with no row or a gap row is UNKNOWN, never empty.**
- Continuity rule = the live `PSNormalizer` test: post[i] must equal pre[i+1]; a break gets a typed `reserve_break` marker and is NOT repaired. No raw tx bodies beyond these fields (optional `raw_json`
  for N days only if the cost allows). No wallet-level data.

## 4. Target order under the cap (Gamma)
1 PumpSwap pools seen by intake/desk that day, full history from pool creation. 2 The 10-25 and restart-gap holes of the PS tape. 3 The curve phase (launch -> migration) of coins that graduated that day.
4 Everything else, only if budget remains. Targets come from `data/desk/launches/<day>.db`, `data/desk/pumpswap/pool_mints.db` and the intake candidates table; the target list is written to `backfill_targets`
BEFORE any fetch so the denominator is fixed and an unfetched target shows as a gap.

## 5. Build risk and the prototype gate
The live PS capture decodes gRPC events; a history fetch returns parsed transactions, so the PS swap decode (instruction/inner-instruction layout of the pump AMM, fee fields, reserve snapshots) is NEW
decode code. Step 0, before any nightly run: fetch 20 pools whose own-tape rows exist, decode, and compare row by row to the tape (match rate, reserve-chain breaks, ordinal agreement). The nightly job is
enabled only if the match is exact on the compared fields; any mismatch class is reported, not patched around.

## 6. Edge cases (CLAUDE.md rule 5)
1 Money path: none; separate process, no funded provider call. 2 Fill path: none; cron nice 19 / ionice idle, no shared lock with the runner. 3 Concurrency: single flock'd writer; no globals; one HTTP session.
4 Absent vs zero vs unknown: see `pool_runs`; an empty page for a pool with a known swap in our tape is `pagination_incomplete`, not zero. 5 Failures: timeout, 429 (rate vs `max usage`), partial page,
malformed tx, undecodable instruction -> a typed row each, never `[]` as success. 6 Units: lamports/atomic as returned, block_time seconds, slot ints; SOL vs token quote identified by `quote_mint`.
7 Sample: backfilled coins are the ones we SAW (selection by our funnel): a training set built from them describes our funnel, not the market (Gamma reports it). Rows keep `source`, never mixed.
8 Rollback: remove the cron; tables are append-only and read by nothing until Gamma's training prereg names them.

## 7. Required RED tests (written before the code, each must fail on a naive implementation)
crash after reserve (row stays ambiguous and counts); lost response / timeout (ambiguous, counted, no refund on restart); cap at the exact boundary (reserve that would cross is refused, the one that equals is allowed);
restarted quota stop (cron re-run the same UTC night refuses; next night allowed); HTTP 429 with `max usage` vs HTTP 429 rate (stop vs bounded charged retry); JSON-RPC 200 with `max usage` in `error.message` (stop);
billing rollover (new epoch needs a new cycle.json; old ledger does not leak; daily files do not reset monthly use); unknown method cost refused; expired/mismatched cycle file refused; plan_credits - month_cap < funded_reserve*1.25 refused; budget = month_cap minus ledger total (reserved+committed+ambiguous), never more; decimal-TEXT amounts above 2^53 round-trip exactly;
duplicate `(signature, instruction_index, inner_index, event_ordinal)` across two day files stored once; two events from one instruction both stored; future / out-of-epoch `attested_at` refused; missing `used_at_activation` refused; same `epoch_id` with changed plan/dates refused; changed `month_cap` does not reset ledger usage.

## 8. Open items
a) Operator, ONCE per billing cycle: `cycle.json` (section 2): epoch dates, plan credits, backfill month cap, funded reserve (Alpha asks the operator; no `.env` change, the file is data; a run cap is code-config, default 10% of month_cap). b) Per-method credit costs: confirm from the Helius docs/dashboard during step 0.
c) Gamma: the exact feature list (35 FEATS + any new) for the training prereg; this spec stores everything needed for them. d) Delta: review the quota/stop logic and the budget ledger.
