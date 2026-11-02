# Memecoin fill timing: which slot we landed in, and why not S+1

**2026-10-19 retry clarification:** EVM timing details name multiple submitted
attempts and the first receipt-to-submit interval only when distinct saved
transaction identities, accepted/ambiguous acknowledgements and ordered same-boot
monotonic clocks prove them. Duplicate events collapse. Missing, malformed,
unclocked or conflicting evidence adds no retry note. Primary submit/confirmation
metrics remain bound to the terminal transaction; source gaps stay unknown when
the source transaction is unavailable. No extra resolver reads or schema change.

**2026-10-13 native EVM boundary amendment:** native RH/BSC sender handoff,
submission acknowledgement and canonical receipt observation retain `attempt_no=1`
and the exact signed `tx_sig`. The timing reader recognizes their existing native
aliases. Legacy native recovery requires exactly one signed handoff, one accepted
or ambiguous acknowledgement and one coherent same-boot ordered receipt. Missing,
conflicting or retry evidence remains unknown. Confirmation observation includes
existing canonical transaction/block reads; it does not prove first receipt arrival
or token acquisition. Existing checker component timings remain call-local and are
published only after completion, without adding a call, lock, wait or retry.
Runnable checks and incident evidence: [MOTION/BSC audit](../../audits/2026-10-13-motion-bsc-timing.md).

**2026-10-11 BSC observation amendment:** the approved
[BSC entry-speed contract](2026-10-11-bsc-sub200-entry-design.md) adds separate timing-file
tables for original boot-bound receipt clocks and off-path exact successful receipt observations.
The minute resolver validates observations against the canonical booking receipt and actual
funded fill. ACK and sender handoff cannot prove execution or physical socket dispatch. The
observer is default OFF, uses the existing tape head stream and a nonblocking read budget, and
never changes the funded ledger. Missing witnesses, eligibility and publication gaps stay unknown.

**Author:** Beta · **Order:** the operator via Alpha, 2026-10-02: "completely unacceptable, we need
to know every time what slot we filled and see what went wrong". Alpha read the slots by hand the same
day: slot lane fills S+1 (13) and S+2 (3), onchain_watch Solana fills S+3 to S+7 (5), 4663 fills
19-33 blocks (2-4 s). **Design approved by Alpha 2026-10-02**, with these decisions: (A) no alert
hold; (B) the read budget goes to the operator, and reads stay off until he answers; (C) the slot
lane's skip reasons are persisted now, not in a later phase. **Revision 2** takes Gamma's spec review
of 9d58715bd: a separate DB file (M1), the watcher rollout (M2), retryable read-dependent gaps (M3),
and cause changes Q2(a)-(d) plus the confirm paths in Q1.

**2026-10-10 shared-delivery amendment:** BSC V3/Flap shared handoffs preserve
`signal_source=onchain_watch` for the original duplicate identity. Timing recognizes them as
fast deliveries only on chain 56 with exact `bsc_v3_fast_lane` / `bsc_flap_fast_lane` labels.
Source block still requires matching chain-bound evidence; missing/foreign data remains unknown.
Successful shared delivery is not labeled `fast_lane_skipped`. Explicit clocks/slots and stored
historical rows retain precedence; no transport, authority or data rewrite changes.
Runnable proof: `docs/audits/2026-10-10-shared-fast-lane-timing.md`.

**2026-10-11 J7/BSC receipt amendment:** the existing asynchronous source-receipt read retains
`source_receipt_observation` even when venue decoding returns `no_venue_leg`. It requires the exact
requested transaction hash, successful receipt, positive block and coherent nonremoved logs.
The terminal trace peeks without waiting and stores a measurement-only
`evm_source_receipt_observation` substage. Timing requires the attempt's exact chain and source hash;
attempt/trace/watcher/fast-lane facts retain precedence. This never grants source-initiation or
native-lane authority. Missing or late evidence stays unknown until the background resolver.

With EVM reads enabled, BSC now uses the existing dedicated
`MEMECOIN_ONCHAIN_EVM_TX_READ_URL_56`, never the exact custody URL, after HTTPS and chain-ID checks.
Resolver work is capped and rotates by last update through the existing state index. The
`--max-seconds` budget now stops before starting another provider call; it cannot interrupt an
already-started socket call. Already-known source and landing blocks require no header read merely
to label the measured gap. Receipt observations do not change economic ledger rows.
Runnable proof: `docs/audits/2026-10-11-bsc-jixiangyang/verify.py` and the focused receipt/timing tests.

## 1. What exists today

- `memecoin_entry_attempts` already carries every stage stamp: `source_created_at`, `observed_at`,
  `decision_started_at`, `quote_completed_at`, `signing_completed_at`, `transaction_submitted_at`,
  `chain_confirmation_completed_at`, `completed_at`, plus `signal_source`, `delivery_lane`,
  `origin_event_id`, `tx_sig`, `terminal_status`, `terminal_reason` and `trace_json`.
- **The slot lane** (`signal_source='slot_lane'`): `trace_json.source_evidence.source_slot` and the
  `chain_confirmation` submission event's `landing_slot` are both stored, so S+n is computable today.
- **onchain_watch, Solana:** `onchain_wallet_detections` keeps the source `signature` and `block_time`
  (seconds), but no slot. The watcher held `tx["slot"]` (the getTransaction answer) and dropped it.
  Our landing slot: `rpc_confirm` returns a `ConfirmState` carrying `landing_slot`, but
  `solana_swap_executor` marks `chain_confirmation` without it (`solana_slot_executor` keeps it).
  When the entry is booked by the balance-delta receipt recovery, no landing slot is held at all.
- **onchain_watch, EVM:** the EVM watcher's log carries `blockNumber`, and the detection drops it.
  `eth_confirm` reads our receipt (which has `blockNumber`) and returns only a status string.
- **The slot lane's skips** (routed buy handed to the on-chain lane: `pool_unknown`, unsupported venue,
  a pair that never arrived, a learner hand-off) exist only as runner log lines. A slot-lane refusal
  after routing is already an attempt row (`origin_event_id = 'slotlane:<sig>'`), read today by
  `memecoin_onchain_lane.slot_lane_outcome`.
- Telegram entry alerts render from `memecoin_entry_view` in the notification worker, off the fill
  path (`enqueue_entry_notification` queues identity only).

## 2. Design

### 2.1 Zero-read captures (values we already hold)

Nothing new on the fill path: no call, no lock, no retry. Each change adds a value to a write or a
trace mark that already happens. The added cost is measured (p50/p90 of the mark or insert with and
without the field) and reported to Alpha before merge.

1. `onchain_wallet_detections.source_slot INTEGER` and `recv_slot INTEGER` (NULL = not captured).
   - The Solana watcher writes `tx["slot"]` as `source_slot`, and the logs notification's
     `context.slot` as `recv_slot`, so on-chain fills get recv-S in slots like the slot lane (Gamma
     Q2(d)).
   - The EVM watcher writes `int(log["blockNumber"], 16)` as `source_slot`. It has no recv block
     without a read, so its `recv_slot` stays NULL.
   - Both values go into the existing INSERT. `db_memecoin.record_onchain_detection` takes both,
     optional.
   - **Rollout (Gamma M2):** the watchers are separate processes, and an INSERT naming a column that
     does not exist yet would drop EVERY detection. Each watcher therefore runs the idempotent
     `ALTER TABLE ... ADD COLUMN` at its own startup, as it already does for
     `source_valuation_json`. `record_onchain_detection` retries the old column list once on
     `no such column`. A test inserts against a pre-migration table and the detection is still
     recorded.
2. `solana_swap_executor`: the existing `trace_mark_submission(trace, "chain_confirmation", ...)` gains
   `landing_slot=getattr(status, "landing_slot", None)`, the slot executor's own pattern.
3. `eth_receipt_state` and `eth_confirm` return a `str` subclass, `ReceiptState`, carrying
   `block_number` from the receipt they already read (like `ConfirmState`).
   `confirm_receipt_across_pool` passes it through. The `evm.confirmation` substage and the
   `chain_confirmation` mark gain `block=getattr(status, "block_number", None)`.
   - **Caller audit (Gamma Q1):** all 19 callers (14 operator scripts and 5 seams in
     `evm_real_seams`) are checked for `type(x) is str`, `is "..."`, and any
     `str()`/`.lower()`/format before the mark, which would drop the attribute. Any such caller
     is listed in the PR.
   - The rh fast lane's own `_receipt` read records the receipt's `blockNumber` in the event or trace
     row it already writes. The BSC lane does the same where it reads a receipt.
   - Any lane with no held receipt is left to the resolver (2.4).

### 2.2 Slot-lane skip reasons (decision C)

- A bounded `collections.deque(maxlen=4096)` in a small module `memecoin_slot_skips`, with `note(sig,
  outcome, reason)`: one `append` of a tuple carrying a monotonic time. The append happens on the
  routed thread (in `route_routed_buy`, at each exit: `lane`, `learning`, `handoff:<why>`,
  `shadow`) and in the routed pairer's expiry (`pair_expired`, `ambiguous`). The append cost on the
  lane thread is measured (p50/p90) and must stay in microseconds.
- A runner daemon thread `memecoin-slot-skips` drains the deque every 1 s into
  `memecoin_slot_lane_skips`. **That table lives in its own file, `data/memecoin_fill_timing.db`
  (Gamma M1)**, never the ledger, so the drain never takes the ledger's WAL write lock that the fill
  path's attempt and reservation writes wait on. It uses its own connection. A drain that fails keeps
  nothing (the deque is bounded) and logs once a minute.
- A signature the slot signal never saw has no row: shown as "slot lane never saw it (not watched or
  stream gap)".

### 2.3 `memecoin_fill_timing` (one row per SUBMITTED attempt)

Every attempt with `terminal_status IN ('filled', 'failed')` and a `tx_sig`. Refusals are out of
scope: they never sent. The table lives in `data/memecoin_fill_timing.db` (Gamma M1). The resolver
writes it; the entry view, the web and the notification worker `ATTACH` it read-only. NULL means not
resolved yet. A final unknown is a state, never 0.

States (Gamma M3):
- `pending`: something is still being captured.
- `unread`: the gap needs a read that is off (knob 0) or has no host. It is REOPENED when the knob
  turns on, inside a 24 h window, because getSignatureStatuses(searchTransactionHistory) and EVM
  receipts answer long after 30 min.
- `complete`: the gap was measured (R8 amendment 2026-10-06: never on a NULL gap).
- `no_source`: a manual entry, no source transaction to time.
- `unsupported`: the origin names no source transaction we can read (a lowercased J7 id, an
  unknown format).
- `not_landed`: a null for OUR signature 10 min after send, read with the knob on.
- `unknown`: reads were on and failed for 24 h, or the chain has no host (56 seconds).

<!-- schema:external=memecoin_fill_timing -->
| column | meaning |
|---|---|
| attempt_id | PK, the attempt |
| network_id | chain |
| lane | `signal_source` / `delivery_lane` |
| source_sig | the source transaction |
| source_slot | Solana slot or EVM block of the source buy |
| source_slot_basis | `slot_lane`, `watcher`, `fast_lane` (an EVM fast lane's own trace), `rpc`, or `unknown` |
| source_time | chain-clock seconds of the source block (detections `block_time` or the block header) |
| landing_slot | our slot or block |
| landing_slot_basis | `confirmation`, `rpc`, `not_landed`, `unknown` |
| landing_time | chain-clock seconds of our block (EVM: the block header read; Solana: NULL) |
| gap | landing_slot - source_slot (Solana slots, EVM blocks) |
| gap_s | landing_time - source_time (EVM only, both chain clock) |
| cause | short code, below |
| cause_detail | words for the operator |
| state | `pending`, `unread`, `complete`, `no_source`, `unsupported`, `not_landed`, `unknown` |
| recv_slot | Solana slot at our receipt (slot lane: head at receipt; watcher: notification context) |
| stages_json | the stage milliseconds: detect, fallback_wait, decide, land |
| reads | RPC reads spent on this row |
| updated_at | last write |

<!-- schema:external=memecoin_slot_lane_skips -->
`memecoin_slot_lane_skips(signature TEXT PRIMARY KEY, outcome TEXT, reason TEXT, noted_at TEXT)`.

### 2.4 The resolver (its own process, never the runner)

`scripts/research/fill_timing_resolver.py`, run from cron every minute with `--max-seconds 50`:

1. Reads attempts read-only with `idx_mc_entry_view_completed` from a cursor. It inserts a `pending`
   row for every submitted attempt that has none, filling everything already stored: the slot-lane
   trace slots, `detections.source_slot` and `block_time`, and the captured landing slot or block.
2. Fills what is missing with reads, each behind its knob (default 0, decision B):
   - `MEMECOIN_FILL_TIMING_SOLANA_READS`: one `getSignatureStatuses([...], searchTransactionHistory)`
     per pass, holding every pending Solana signature, both ours and the sources' (at most 256). It
     runs on `MEMECOIN_RESEARCH_HELIUS_RPC_URL` only, under the operator's flat-book exception.
     A `null` for our signature after 3 passes over 10 min means `not_landed`.
   - `MEMECOIN_FILL_TIMING_EVM_READS`: `eth_getTransactionReceipt` for our tx only when the block was
     not captured, and `eth_getBlockByNumber(ours, false)` for `landing_time`. 4663 uses the research
     QuickNode key, 8453 `https://base.gateway.tenderly.co`; both are checked by the evm-key host
     guard and an `eth_chainId` read. 56 has no host: `unknown`.
   - A failed read leaves the field NULL and the row `pending`. A field that needs a read with the
     knob off is `unread` and is reopened when the knob turns on. After 24 h of failed reads it is
     `unknown`. It is never guessed and never 0.
   - It ATTACHes read-only the lanes' own decision files for causes:
     `data/memecoin_rh_feed_shadow.db` (`rh_fast_decisions`, `rh_fast_lane_events`) and the BSC
     lane's file (`bsc_lane_events`).
3. Computes `gap`, `gap_s` and the cause (2.5) with one pure function, `fill_timing.classify`. The
   entry view uses the same function, so the alert and the row agree.
4. Sends ONE follow-up Telegram line, `TIMING <ticker> landed S+7: <cause>`, only for a fill whose
   alert went out without S+n, i.e. its source slot needed this resolver (mostly J7). This is decided
   from `source_slot_basis = 'rpc'`.
5. Prints one summary line per pass: rows written, pending, unknown, reads used. A submitted attempt
   older than 5 min with no row is counted as `missing` in that line.

### 2.5 Cause ("why not S+1"), first match wins

2026-10-09 amendment: EVM rows lacking a source block and a bound lane decision use
`source_unknown`, distinguished as "source transaction unavailable" when their origin supplies
no transaction reference, or "source block unresolved" when the reference exists. A J7 UUID with
no transaction hash never inherits the gap or lane outcome of a nearby wallet/coin buy.
Unknown EVM gap/landing lines retain their measured local stages and failure reason, so the
dashboard and notifications do not discard `cause_detail`. Manual and not-landed states remain
distinct; a measured zero gap remains zero. Parallel BSC safety records label its remaining
post-reservation wait as "safety wait", rather than calling it the full safety read duration.

1. `on_time`, per chain, fixed in code:
   - Solana: landed at or before S+1;
   - 8453 (2 s blocks): +1 block;
   - 56 (about 0.45 s blocks): +2 blocks;
   - 5042 (0.5 s blocks): +2 blocks;
   - 4663 (about 0.12 s blocks): +8 blocks, about 1 s. "Next block" can never be met by a copy there
     (Gamma Q2(c)).
2. `price`: failed with a price or slippage reason (`terminal_reason`).
3. `not_landed`: no landing slot after the window (dropped or expired).
4. `slot_lane_failed` (Gamma Q2(a)): an on-chain fallback (`delivery_lane =
   'onchain_fallback_after_slot_lane'`) whose slot-lane attempt for the same signature sent and
   failed. The detail carries that attempt's own S+n and reason, plus `fallback_wait_ms` = the
   fallback's `decision_started_at` minus the slot attempt's `completed_at`. (WWWCAT and Shoggoth
   on 2026-10-02: the slot lane landed S+2/S+1 and failed on price, and the fallback started
   380/554 ms later because the 1 s on-chain sweep is not woken by it.)
5. `slot_lane_skipped` (Solana, any lane other than the slot lane): the slot lane's own decision for
   this signature. That is its attempt's `terminal_reason` (refused before sending), or the skip
   row's outcome and reason, or "never saw it".
6. `fast_lane_skipped` (EVM, Gamma Q2(c)): the chain's fast lane decision for the source tx, e.g.
   `rh_fast_decisions.reason` on 4663 (today `float_short`, `no_pool`, `no_native_v4_pool`) or
   `bsc_lane_events.reason` on 56; or "no fast-lane decision". Amendment 2026-10-07:
   a BSC unresolved-signed-transaction refusal whose matching chain/source/mint hook decision
   occurred strictly after this fill's broadcast is duplicate suppression, not its delay cause.
   Say "BSC duplicate suppressed after broadcast" and classify by the largest measured stage.
   Decision, broadcast and event-completion timestamps must be timezone-aware, with
   broadcast < decision <= event completion; absent or invalid proof retains the original reason.
7. Otherwise, the largest measured stage.

**`cause_detail` always appends the largest measured stage and its milliseconds, whatever rule
matched (Gamma Q2(b)).** The stages are:
- `detect`: Solana recv-S in slots (`recv_slot - source_slot`, from the slot lane's head at receipt
  or the watcher's notification context), or on EVM `detected_at - block_time`. Solana `block_time`
  has 1-second resolution, so it is never used for Solana detection (Gamma Q2(d)).
- `fallback_wait`: as in 4.
- `decide`: `observed_at` to `transaction_submitted_at`.
- `land`: submit to landing. Solana: the slot lane's `landing_minus_head`, otherwise submit to
  confirmation. EVM: blocks.

Thresholds are fixed in the code and printed in `cause_detail`. They are never tuned on the result.

### 2.6 Display (decision A: no hold)

- **Telegram entry alert:** the view computes S+n at send time from what is stored, so slot-lane and
  onchain fills show it at once:
  - `Landed S+1`
  - `Landed S+7 · slot lane: pool_unknown · detect 1.4 s`
  - EVM: `Landed +19 blocks` (the seconds come from the resolver and appear on the dashboard)
  - `Landed S+? (source slot resolving)`, followed by the resolver's line.

  A failed fill gets the same line or `not landed`.

  **Amendment 2026-10-14:** an EVM fill with a known landing block and no source transaction
  identity displays `Landed block <N> · block gap unavailable`. Exact source transactions awaiting
  their block retain the resolving label. UUID-only feed events cannot supply a block gap; no
  timestamp or nearby wallet event substitutes for an exact source identity. Historical timing rows
  remain unchanged.
- **Real positions row:** an `S+n` chip; its tooltip carries the cause and the stage milliseconds.
- **Copy tab:** a "Fills today" table with time, ticker, chain, lane, S, landing, gap, cause and the
  stage milliseconds, read from `memecoin_fill_timing` joined to attempts.

## 3. Edge cases (CLAUDE.md rule 5)

1. **Money path.** Only 2.1 and 2.2 touch the fill path. They add one field to existing marks and
   inserts, plus one deque append; no new call, lock or retry. Measured p50/p90 go to Alpha.
2. **Speed.** As in 1. The resolver and the skip drain are off the fill path. The resolver is its own
   process, so a slow host or a crash there cannot stall the runner.
3. **Concurrency.** The deque `append` and `popleft` are atomic under the GIL. The drain thread and
   the resolver write only `data/memecoin_fill_timing.db`, never the ledger, so neither can hold the
   ledger's WAL write lock. Ledger reads are read-only connections; the timing file is ATTACHed
   read-only by every reader.
4. **Absent / zero / unknown.** NULL means pending, `unknown` is final, `not_landed` is a fact, and a
   gap is never 0 for a missing slot. A slot lane fill at S+0 (same slot) is a real 0 and is shown.
5. **Dependency failures.** Timeout, 429, a null answer, or a wrong chain: a field stays NULL, the
   row stays pending, and it becomes `unknown` at 30 min. A 429 stops that pass's reads. A wrong
   chain id refuses the host.
6. **Units and identity.** Solana slots and EVM blocks live in the same column, always read with
   `network_id`. `gap_s` uses the chain clock only (block timestamps), never host minus chain.
   Signatures are matched exactly (Solana base58; EVM lowercased hex).
7. **The sample.** Only submitted attempts (filled or failed), which describes our sends. Refusals
   and paper are excluded and the display says so.
8. **Rollback.** Stop the cron and set both read knobs to 0. The table and column stay inert. The
   view shows S+n only when the slots exist.

## 4. Checks left behind

- `test_every_submitted_attempt_gets_a_timing_row`: one resolver pass over a ledger with filled,
  failed and refused attempts. Every filled or failed attempt has a row, and no refused one does. It
  fails if a fill can be written without its timing row.
- A unit test per cause rule, plus `unknown` versus 0.
- Capture tests: the detection insert carries `source_slot` and `recv_slot`, and a pre-migration
  table still records the detection. The Solana and EVM confirmation marks carry the landing slot or
  block. `ReceiptState` compares, binds and JSON-encodes as its string.
- A test that the drain and the resolver never open the ledger for writing.
- The micro-benchmarks for 2.1 and 2.2, with numbers in the PR.

## 5. Not handled

- On 56, the gap in BLOCKS comes from captures alone (the watcher's `blockNumber` and our receipt's
  `block_number`). Only `gap_s` needs a host, so it stays `unknown` until a 56 research host exists.
  The EVM watcher has no recv block without a read, so EVM `detect` uses `detected_at - block_time`
  in seconds.
- Slot-time precision on Solana: `detect_late` uses `block_time`, which has 1-second resolution, and
  says so.
- Before this ships, detections carry no `source_slot`. Those rows need the Solana read knob, or stay
  `unknown`.

## Amendment 2026-10-10: separate quote clock boundaries

`memecoin_fill_timing.py` adds read-only `quote_to_submission_ms` and
`quote_to_confirmation_ms`. Legacy persisted `quote_age_ms` remains quote observation through
confirmation observation. Submission is the recorded acknowledgment boundary, not physical first
dispatch. Direct row timestamps require `observed_real_utc`; legacy or missing boundaries require
matching stamps from this attempt's single-clock trace. Missing/naive/invalid/reversed clocks
remain unknown; source/block times and native send/receipt alone never manufacture a quote time.
No schema, funded policy or historical-row rewrite. Evidence:
`docs/audits/2026-10-10-bsc-entry-pipeline.md`.


## Amendment 2026-10-11: Base latency repair

The human label for persisted decide_ms is now detect-to-submit: observed_at through transaction_submitted_at includes more than decision work. Machine category decide/decide_slow and historical measurements remain unchanged. EVM exit timing separates waited approval from quote, and records proceeds receipt/accounting and settlement boundaries. No historical clocks are rewritten.

Contract: `docs/superpowers/plans/2026-10-11-base-latency-repair.md`.


## Amendment 2026-10-11: separate processed execution witness

The [sub200 entry contract](2026-10-11-solana-sub200-entry-design.md) adds an independent default-OFF processed observer and bounded source-receipt universe in the separate timing database. A successful processed observation becomes an execution upper bound only after exact signature and slot match the later confirmed cached receipt, under one boot and clock domain. Missing, failed or fork-mismatched evidence stays unknown. Canonical execution time and bank identity remain unverified.

Existing block/slot diagnostics and classification are retained. The new execution summary labels its bounded page; it does not claim an all-universe cohort verdict from a partial page. Observation never gates entries or writes economic ledger state.

## Amendment 2026-10-20: local processing and pretrace evidence

The card measures processing from this runner's receipt to its decision, never from the source block or another process's socket receipt. Missing, naive or malformed local clocks stay unknown. Legacy cards with only source and detection stamps retain their source-to-receipt interval and source-clock-ahead warning, while local processing stays unknown. Solana landing lines retain explicit receipt-to-dispatch observations, including successful S+1 outcomes; confirmation observation remains a separate boundary.

The existing `slot_receipt` substage includes `pretrace_phase_ms` for source proof, routing, first opportunity, admission, addon, withdrawal, metadata and momentum submission. `pretrace_total_ms` and matching-thread `pretrace_cpu_ms` are exposed in execution summaries. Missing CPU evidence stays unknown. Wall time in excess of thread CPU identifies off-CPU time but cannot identify GIL, scheduler or storage as its cause. This adds evidence without removing gates, changing deadlines or claiming the historical 11 ms was eliminated.
