# October 11 entry errors and execution repair plan

> **For agentic workers:** Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task by task. This request authorizes investigation and a plan only. Production implementation and activation are separate actions.

**Goal:** Correct misleading execution results and Ethereum reserve counting, shorten avoidable native-funding and cold-safety delays, and measure the remaining inclusion problem without weakening signed limits or source authority.

**Architecture:** Keep the existing funded hook, per-chain lanes, reservations, safety cache, transport and notification worker. Correct reporting on the notification thread; move a provably unaffordable BSC native attempt to its existing unsigned handoff earlier. Correct the existing execution-time reserve calculation for its owned Ethereum pending entry. Reuse existing background safety and pool observations for diagnostics before proposing further execution changes.

**Tech stack:** Python, SQLite, pytest, existing EVM and Solana executors. No new dependency.

**Contracts:**

- [Source integrity](../specs/2026-10-04-bsc-recovery-source-integrity-design.md).
- [Robinhood native lane](../specs/2026-10-01-memecoin-4663-fast-lane-design.md).
- [BSC native lane](../specs/2026-10-02-memecoin-bsc-56-fast-lane-design.md).
- [Solana landing](../specs/2026-10-09-memecoin-slot-lane-s-landing-design.md).
- [ANR verified incident](../../audits/2026-10-11-rh-anr-0060e015.md).

## Findings

The seven messages contain four actual failed swaps, one successful but late Solana inclusion, one failed BSC native leg followed by an ordinary fill of the same trader and coin, and one unsigned Ethereum funding refusal. They do not justify removing price floors, relaxing proof, raising tips, or resizing trades.

| Ref | Actual result | Cause and evidence |
|---|---|---|
| `092a4d9b`, 100xbroker | Secondary unsigned watcher refusal. Native `015242d1d2ae490f8e315701897797ff` had already failed on chain. | Native PumpSwap proof passed. Minimum 1,795,584.267835 tokens; receipt available 1,472,476.779494. Source slot 455575152, failure 455575154: S+2. Receipt price +46.6% over the source post-price against a 30% cap. Watcher independently lacked initiation proof because its valuation decoder recorded `unsupported_buy_instruction`. |
| `0060e015`, ANR | Failed swap, no position or fill. | Saved receipt/calldata proof: B+10, one chain second, exact signed 30% post-price ceiling. Same-pool local tape had moved +69.03%. Cold safety 372.995 ms; decide-to-send-start 616.952 ms; acceptance 196.159 ms. Original transaction-prefix revert selector is not independently proved. |
| `be9677a1`, BSC | Failed swap, no position or fill. | Native lane records B+3. Signed minimum 817816910102774299626623 atoms; copy-premium floor 675839370984406447427732 did not bind. Safety 322.555 ms. Original calldata/reserves and exact failure selector are not retained locally. |
| `310efaeb`, META | October 8 failed swap, no position or fill. | Native lane records B+9. Old signed copy floor 608817837675872629403445 atoms was 5.02% stricter than the detector floor. That extra deposit-path floor was already removed in `88fed8013`. Historical output recorded in code/history was below both floors; the removal does not prove this buy would have filled. Safety 326.135 ms. |
| `9522af39`, ACCELERANDO | Filled $150, position 73333, S+4. | Receipt-to-dispatch 8.089 ms, local queue 0.293 ms, first transport acknowledgment 2.951 ms. Nozomi B really tipped 0.005 SOL and eventually accepted the exact bytes in 9.640 ms. Confirmation observation 1605.140 ms and balance read 112.013 ms explain most of the 1729.647 ms attempt. A faster confirmation poll cannot move the actual landing slot. |
| `367eb301`, 但丁 | Native attempt aborted unsigned at `float_short`; same trader/coin filled through the ordinary feed about 2.9 s after detection. | Native approved input 0.129396497518295048 BNB exceeded the configured 0.1 BNB float before gas. Exact historical native balance/gas snapshot is absent. Inline safety cost about 252–255 ms before shortage was checked. Chain-56 volume gate was disabled and `allowed=true`; its $99.4/$3064.987 comparison was a misleading diagnostic. Ordinary fill `c192d959`, position 73334, $124.63. Its opaque feed ID does not prove identical source transaction identity. |
| `b6ae558c`, PAYZ | Ethereum reservation 5163 failed unsigned; no position, fill, nonce or transaction. | Partial unwrap required 18718818305633530 wei; wallet WETH was 16522745418509292 wei. Deficit 0.002196072887124238 ETH = $5.497539 at $2503.35/ETH. Execution's full-book reserve counts its own pending entry, then adds another prospective entry; extra exit reserve is 0.0022 ETH under the chain-1 gas inputs, matching the deficit within $0.009831 clip rounding. Actual admission/probe native, probe gas and book floor were not retained, so other historical balance/reserve changes remain unproved. |

The native 100xbroker failure **also paged**: watcher REFUSED delivered 07:51:48.145323 UTC; native TRANSACTION FAILED delivered 07:51:49.199861. Different origin prefixes left both notification results current. The watcher label `slot lane submitted` remained stale after deferred settlement.

ANR, BSC and META's 04:04 AM Eastern messages were delivered at 08:04:11–14 UTC following the classification repair. Their executions occurred at 04:55 UTC October 11, 14:59 UTC October 10, and 00:06 UTC October 8. NotificationLifecycle treated a newly recognized terminal label as a new resolution and replaced its event time with the restart time.

The BSC BOUGHT notification delivered at 09:33:05.009515 UTC. Exact original-source fallback `c899e160` correctly lost the coin claim to the ordinary feed's reservation 5162. Do not merge opaque feed IDs using only wallet, coin and nearby time.

PAYZ used a plain pre-transaction partial unwrap, not the V4 in-swap unwrap branch. `native_float_probe()` calls `required_entry_native()` after the owned reservation exists; sizing called the same reserve formula before it existed. A fixed-input in-memory ledger reproduces 0.0033 ETH before own reservation, 0.0055 ETH after it, and 0.0077 ETH after a distinct second reservation. The prospective-entry helper is correct for a new entry; using it unchanged for the already reserved execution is the code defect.

PAYZ took 1729.509 ms total. Its 433.768 ms balance work, 704.307 ms quote and 1055.907 ms token safety overlap; do not add them. V4 hit at 346.286 ms, but the existing measured-route comparison waited for a losing multihop result at 701.324 ms. That is deliberate route-quality policy; removing this roughly 355 ms wait would increase the safety join here, not shorten this attempt. Cold-cache early quoting could start only after the shared 433 ms balance read, about 6 ms before reservation completed. It recorded `balance_unavailable` even though the gate accepted the same fresh tuple; the remaining exit condition is a stale generation. Own-attempt invalidation versus another chain-1 spend is not retained. Neither shortening route grace nor ignoring spend-generation invalidation is authorized by this incident.

## Current release and work already done

At this audit, main, the memecoin symlink and runner PID 517248 resolve to `266e31d485266ed072a13e6744b4eb8a6b71ce14`. Web symlink resolves to the same SHA. Heartbeat reports authenticated runner, slot lane connected, 35 armed wallets, `nozomi_ab_ready=true`, `nozomi_wallet_scope=all`.

Already in that release:

1. `54d2e5b7b`: RH/BSC failed-receipt projection, retained landing evidence, atomic identity-guarded terminal reset/release. Do not redo it.
2. `d98d9bac7`: exact unsigned BSC `float_short` handoff. It ran in this incident; do not replace it with a broad fallback.
3. `266e31d48`: Nozomi scope expanded to all armed Solana wallets. ACCELERANDO used it. Do not propose merely turning it on again.
4. Existing priority/fair safety warmup and parallel cold safety. Another worker or cache is not justified.
5. Existing BSC snapshot worker and native-float maintenance. Refilling faster to the same target cannot cover a 0.1294 BNB input when the float target is approximately 0.1 BNB plus the base gas reserve.
6. Chain-1 native-spendable sizing and partial unwrap already exist (`bd4b8bccd`, `c61ea0db2`). PAYZ exercised partial unwrap. Turning the feature on or wrapping more WETH does not repair the own-reservation reserve mismatch.

100xbroker ran under `831460fe`, before the all-wallet Nozomi release; its trace selected Helius Sender B with a 5,000-lamport tip. Do not pool that with the later Nozomi cohort.

## Global constraints and edge cases

1. **Money path:** Tasks 4–10 can affect or observe entry. Signed output floors, approved sizing, source initiation, route safety, global exposure, exits and durable transaction bindings remain authoritative. No new retries of these historical trades.
2. **Speed:** No new synchronous provider call, wait or custody lock. Reporting queries run on read-only notification connections. Telemetry uses existing observations/queues. Benchmark added local computation with measured p50/p90 before activation; no invented speed promise.
3. **Concurrency:** Cache mutation stays on its owning lane or existing locked cache API. An abandoned safety result cannot publish or mutate a returned record. Per-attempt data must not mutate module attributes, environment or shared provider state.
4. **Absent/zero/unknown:** Missing balance, gas, pool state, quote or signer evidence is unknown. A measured zero balance is a real shortage. Missing quote remains NULL; a local AMM estimate must not populate an executable quote field.
5. **Dependency failures:** Test timeout, rejection, 429, ban/disconnect, partial and malformed response, wrong chain/identity, and stale but plausible data. Preserve provider failure versus token failure. Unknown signed sends remain unresolved.
6. **Units/identity:** BNB/ETH use 18-decimal atomic integers; PumpSwap quantities here use 6 decimals. Source reference can be wallet average or post-trade pool price and must be labeled. Solana signatures are case-sensitive. A chain, wallet, mint and exact source transaction must all match before linking outcomes.
7. **Denominators:** Report all eligible source observations, then attempted, signed, accepted, failed, filled and censored separately. Latency denominators exclude unknown durations and label exclusions. Do not grade only successful fills or pool old/new release populations.
8. **Rollback:** One logical commit per task; no schema migration needed for the reporting changes. Retain the prior pinned release and all signed reservations. No historical ledger rewrite, notification resend, DB backup or policy activation is part of implementation validation.

**Not handled:** guaranteed first-seen warm safety, price movement after signing, validator inclusion order, wrong-high provider balances, arbitrary unknown source instructions, and exact original EVM failure diagnosis without transaction-prefix evidence. Raising float capital, changing tip/fee policy or buying a new feed is a separate operator decision supported by measurements.

## Review focus

- Same wallet and mint with a different source transaction must never be collapsed.
- A historical label correction must not mute a genuinely newly resolved old transaction.
- A smaller finally approved BSC clip may fit although the pilot ceiling does not.
- A safety future finishing after native handoff must not change cache or returned result.
- Transport's first-return `ambiguous/after_accept` and a later accepted provider result are distinct observations, not contradictory chain verdicts.
- Execution must count its own entry once while preserving every other pending/open position, actual route-gas reserve and conservative behavior for a signed or unproved own row.
- A stale balance generation cannot be mislabeled as a failed RPC or made reusable by removing its generation check.

## Task 1: Show only the gate that actually refused

**Files:** `sniper/services/memecoin_telegram.py:1050`; `tests/test_memecoin_notification_lifecycle.py`.

**Interface:** `render_entry_notification(view)` consumes the existing `status`, `reason` and `gate_evidence`; no DB or funded interface changes.

- [ ] Write tests for disabled BSC volume + `float_short`, filled BSC + disabled volume, an actual enforced m5 refusal, and an enforced in-band refusal with `active=false`.
- [ ] Run them red. The diagnostic check below already reproduces the first two cases on current code.
- [ ] Replace the unconditional volume rendering condition with a refusal check and `row.get('allowed') is False`. Keep the existing measured amount/basis. Do not filter by `active`: that field selects the market-cap band, not whether the final gate refused.

```python
if (status == 'refused' and row.get('name') == 'volume_gate'
        and row.get('allowed') is False
        and row.get('tested_volume_m5') is not None
        and row.get('threshold') is not None):
    lines.append(f"Volume m5: ${row['tested_volume_m5']:,.0f} vs ${row['threshold']:,.0f} "
                 f"({row.get('measurement_basis') or 'unknown basis'})")
```

- [ ] Add chain-specific plain wording for `float_short`: native funding buffer cannot cover approved input plus gas. Do not call it a liquidity failure, and do not claim exact available balance unless recorded.
- [ ] Run `python3 -m pytest -n 2 -o addopts='' -q tests/test_memecoin_notification_lifecycle.py` and commit this reporting-only change.

**Acceptance:** Actual m5 refusals retain their correct dollar/count basis. Disabled/passing checks do not appear as another refusal or on BOUGHT cards.

## Task 2: Let the proven native outcome own its secondary watcher notification

**Files:** `sniper/persistence/db_memecoin.py:4445,14836`; `sniper/services/memecoin_entry_view.py:338,459`; `sniper/persistence/db_memecoin_notifications.py:112`; `sniper/services/memecoin_slot_lane.py:1377`; `sniper/services/memecoin_slot_skips.py:32,75`; `tests/test_memecoin_entry_view.py`; `tests/test_memecoin_notification_lifecycle.py`; `tests/test_memecoin_slot_deferred.py`; `tests/test_memecoin_slot_skips.py`.

**Interfaces:** Preserve `entry_view(conn, *, origin_event_id=None, attempt_id=None, event=None)` and existing attempt projections. Extend its existing `duplicate_same_source` evidence for the exact structural secondary refusal, with a distinct `native_owned_secondary_refusal` delivery flag. Reuse `resolve_source_evidence()` identity rules. Extend skip `note(signature, outcome, reason=None, *, attempt_id=None)` and add `finish_note(signature, expected_attempt_id, outcome, reason=None) -> bool`; preserve `decision_for()`'s two-element return and the existing timing DB schema. Add private `_finish_traced(buy, result, trace, facts, t0, *, expected_note_attempt=None)`; only the deferred closure supplies an owner.

- [ ] Add an isolated fixture for the two 100xbroker origins. Native attempt has successful initiation proof and a signed failed swap; secondary is unsigned `onchain_watch/missing_receipt_proof`.
- [ ] Assert both attempt verdicts remain readable, but only the native TRANSACTION FAILED owns delivery. Native filled and unresolved cases must retain their own filled/unknown behavior. Different signature/wallet/mint/chain and absent native evidence must still deliver the independent refusal. A genuinely submitted fallback result must still deliver.
- [ ] Use a bounded exact-origin lookup for `slotlane:<signature>` on the notification read connection. Require chain Solana, case-sensitive source signature, wallet, mint, native lane ownership and signed/submitted evidence. Reuse `_silent_duplicate_refusal`; do not invent a wallet/coin/time collapse or change admission/dedup keys.
- [ ] Tighten the existing blocker branch too: `_blocker_source()` currently proves only matching wallet. Resolve both attempts' exact source transaction from retained metadata with `resolve_source_evidence()`, then compare chain, wallet, mint and transaction. Position-only or opaque-feed blockers without that proof remain independent refusals. Add same-wallet/mint but different-signature and opaque-feed blocker tests. Preserve funded reservation/coin admission unchanged.
- [ ] In both `NotificationLifecycle.observe()` and `deliver()`, suppress the proven `native_owned_secondary_refusal` regardless of whether that watcher origin already delivered a SOURCE card. The existing `_source_card_delivered` exception would otherwise re-page it. Add that fixture and preserve ordinary source-card completion delivery for every other refusal kind.
- [ ] At `_handle` line 1368, attach `trace.attempt_id` inside the deferred-result branch immediately after `_reserve_and_execute`; do not rely on an injected executor's provenance. Pass that owner into the existing `_handle_buy` note call at line 940. The deferred closure passes `expected_note_attempt=trace.attempt_id` to `_finish_traced`; synchronous completion leaves the default and keeps its existing post-return note.
- [ ] Add `finish_note` on the existing `_LAST_LOCK`: require matching attempt owner and current `submitted` outcome, then atomically update memory and append the accepted terminal note. Move normal `note`'s deque append into its accepted-memory branch under that same existing lock, so rejected stale notes never reach `flush`. Any accepted ownerless/newer producer replaces the owner; ignored lower-rank hints do not. This adds no provider call or new lock; initial registration remains after dispatch.
- [ ] Extend deferred settlement tests so the timing skip note becomes terminal after finalization, before wake. Preserve in-flight `submitted` until settlement is actually complete.

```python
changed = trace.finish(str(result.get('status') or 'unknown'),
                       str(result.get('reason') or result.get('status') or 'unknown')[:200],
                       **{key: result[key] for key in ('pending_id', 'position_id', 'fill_id',
                                                      'gate_inputs_json') if key in result},
                       **({'tx_sig': result['sig']} if result.get('sig') else {}))
if (changed and expected_note_attempt is not None
        and result.get('status') not in (None, 'submitted', 'submitted_unknown')):
    _finish_skip_note(buy.signature, expected_note_attempt,
                      result['status'], result.get('reason'))
# Existing wake follows; _finish_skip_note is the imported finish_note alias.
```

- [ ] Keep the note inside successful trace-finalization handling and require `trace.finish()` to return true. Test failed/duplicate persistence, two same-signature attempts completing in both orders, lower-rank hint, accepted ownerless replacement, and absent/evicted owner. An older completion must never overwrite a newer owning attempt in memory or after the single FIFO drain is flushed. An absent owner rejects the note; the ledger remains authoritative. This telemetry fix does not repair notes lost across restart/processes or dropped by the bounded ring.
- [ ] Run focused view/lifecycle/deferred/slot-skips tests with `-n 2`; commit.

**Acceptance:** This exact case produces one authoritative failure notification. Proof refusal remains unchanged. No cross-source result is hidden.

## Task 3: Preserve event time when only a terminal label is corrected

**Files:** `sniper/services/memecoin_entry_view.py:398`; `sniper/persistence/db_memecoin_notifications.py:231`; `tests/test_memecoin_notification_lifecycle.py`.

**Interface:** Store terminal reservation status and resolution evidence in the existing transition `evidence_json`; no new table/column. Add the pending row's `resolution_reason` to the shared view under a distinct reservation-evidence key.

- [ ] Reproduce post-activation `needs_review` for a reservation already failed with `rh_fast_reverted`, then `transaction_failed` after a restart with unchanged completion/transaction. Current code re-dates it to now and pages; the check below fails on that behavior.
- [ ] Record reservation status, resolution reason, tx identity and completed/status timestamps in each new transition's evidence. In `observe`, read that prior evidence with the prior state.
- [ ] If those prior facts already prove the same terminal reservation and only the projection label changed, retain the old event stamp and record the corrected transition as `baseline`. Do not use restart time as execution time and do not create a missed-results summary for the correction.
- [ ] Legacy transitions without sufficient prior proof remain conservative; do not parse Telegram prose as ledger evidence. If delivered, identify an old correction explicitly rather than presenting it as a new failed execution.
- [ ] Preserve notification on a genuinely new durable completion, a changed resolution timestamp/identity, and a previously genuinely unresolved signed transaction. Keep delivered/ambiguous-send idempotence.
- [ ] Run the lifecycle suite including `test_historical_review_still_delivers_a_new_durable_completion` and `test_old_actual_unresolved_transaction_still_notifies_new_resolution`; commit.

**Acceptance:** The ANR/BSC/META label-correction shape cannot become three fresh executions. A real late resolution still pages once. Current wallet disarm status is not inferred to be historical authority.

## Task 4: Hand off a known BSC native shortage before waiting for safety

**Files:** `sniper/services/memecoin_bsc_fast_lane.py:1811`; `sniper/services/memecoin_real_runner.py:2564`; `sniper/services/memecoin_evm_leg.py:748`; `tests/test_memecoin_bsc_fast_lane_live.py`; `tests/test_memecoin_bsc_float_short_fallback.py`; `tests/test_memecoin_bsc_parallel_safety.py`; `tests/test_memecoin_evm_leg.py`.

**Interface:** Add `native_funding_precheck_fn(approved_clip_usd: float) -> str | None` to the BSC native context, forwarded by `_make_evm_entry_fn` to an optional keyword on `evm_real_entry`. Default is `None` for ordinary/RH/shared routes. The BSC closure consumes the finally approved clip and existing snapshot, returns `float_short` only for a known, fresh, exact shortage, and returns `None` otherwise. It performs no release or cache mutation; `evm_real_entry` owns the exact pending release and result.

- [ ] Write a red test with a finally approved $96.72 clip, fresh 0.1 BNB native balance and a deliberately blocked safety future. The attempt must return without awaiting that future, nonce claim, signing or submission.
- [ ] Call the pure precheck after successful reservation/approved-size selection and before consuming sequential safety. Reuse `clip_wei_for()`, `inputs_reason()` and `float_covers()` arithmetic on the same snapshot and price basis. No provider call or new lock.
- [ ] For a proven `float_short`, use the existing atomic unsigned-only helper `db_memecoin.release_unsigned_blocked_entry()` at line 14035. Do not use `release_pending()`: it only guards row ID/status and can release signed rows. Return the shortage result only when the unsigned compare-and-set succeeds:

```python
if not m.release_unsigned_blocked_entry(conn, pid, 'bsc_fast_float_short'):
    return {'status': 'unknown', 'reason': 'funding_precheck_release_conflict',
            'pending_id': pid, 'approved_clip_usd': approved_clip_usd}
return {'status': 'aborted', 'reason': 'float_short',
        'pending_id': pid, 'approved_clip_usd': approved_clip_usd}
```

The conflict path preserves the pending binding and forbids the float-short fallback; its durable current result remains authoritative.

- [ ] Unknown/stale/malformed balance or gas is not proof of a known shortage; retain the existing typed fallback/refusal. Retain the final locked snapshot/generation/funds recheck before signing.
- [ ] Test smaller approved clip fitting despite a larger pilot ceiling; affordable unsafe token still refused; stale generation; signed/bound pending cannot hand off; ordinary feed wins one coin claim; abandoned future cannot publish safety/cache/result changes.
- [ ] Record the observed native/gas/read-age and required clip/gas/base amounts in the existing result after the decision. Never label skipped safety passed.
- [ ] Run the four focused modules with `-n 2`; benchmark successful-path added local work p50/p90 and prove the blocked-safety test returns immediately; commit.

**Acceptance:** Preserve the existing exact handoff contract and final safety gate for affordable entries. Avoid the observed roughly 252 ms wait on provably impossible native funding. Organic population gain remains unproved until forward measurement.

**Do not:** increase float or lower clip silently, return a false token-safety verdict for a funding shortage, or move `_inline` cache mutations to a worker. The current refill target cannot cover this clip; changing capital allocation needs a separate concrete proposal.

## Task 5: Locate the remaining cold-safety delay using existing warmup

**Files:** `sniper/services/memecoin_rh_fast_lane.py:632,642,654,1771,2734`; `sniper/services/memecoin_rh_fast_decide.py:506`; `sniper/services/memecoin_bsc_fast_lane.py:1716`; `tests/test_memecoin_rh_fast_lane_cache.py`; `tests/test_memecoin_rh_parallel_safety.py`; `tests/test_memecoin_bsc_parallel_safety.py`.

- [ ] Add deterministic tests for a missed candidate, an expired verdict, fair priority refresh and a provider-overrun/abandoned result. Retain the existing amount-bound safety and expiration behavior.
- [ ] Capture candidate delivery time, existing cache miss reason/verdict timestamp, warm-worker start/end, and reservation-to-verdict wait from the existing locked lookups/worker records. Flush diagnostics after result; do not add a second entry cache, lock, read or provider.
- [ ] Compare detector `make_candidates`, dispatcher candidate delivery and signal priority against `SafetyCache.note_candidates/next_due/run_one`. Repair only a demonstrated missed-candidate or starvation path with its failing fixture. If all paths work, keep the instrumentation and report first-seen cold safety as the measured limit.
- [ ] Keep TTL, provider pacing/concurrency and safety scope unchanged. Parallel cold safety already ran on ANR/BSC; merely turning it on is not a fix.
- [ ] Report warm/cold shares and same-chain p50/p90 from all attempts, including refusals/timeouts and censor counts. The three selected failed EVM swaps here waited 322.6–373.0 ms for safety; these incident-selected numbers cannot establish a population improvement.
- [ ] Run the three focused safety modules with `-n 2`; commit any independently proved correction separately from instrumentation.

**Acceptance:** Each 323–373 ms incident wait has a typed warmup explanation. Any cache correction proves the original missed delivery/ordering defect and retains all unknown/unsafe behavior.

## Task 6: Keep source-state limits separate from sign-time pool diagnostics

**Files:** `sniper/services/memecoin_rh_fast_decide.py:241,584`; `sniper/services/memecoin_rh_fast_lane.py:1885,2126`; `sniper/services/memecoin_bsc_fast_lane.py:1842`; `sniper/execution/evm_real_seams.py:2852`; `tests/test_memecoin_rh_fast_decide.py`; `tests/test_memecoin_rh_fast_entry.py`; `tests/test_memecoin_bsc_fast_lane_live.py`.

- [ ] Add fixture tests that newer same-pool observations cannot replace the source swap state or change signed cap/slippage/copy floors. Test foreign pool, chain, reorg, stale state, missing liquidity and unsupported hook/tick crossing.
- [ ] Use existing TapeState background rows to retain a separate latest same-pool state at the decision/sign boundary, with source block/log index and observation age. Treat missing state as unknown. No inline RPC or new entry gate.
- [ ] Keep `source_pool_est_out()` labeled as a one-range known-hook estimate. It cannot certify a V4 executable fill. Keep `quote_price` NULL where no exact quote exists.
- [ ] Persist which floor bound the signed minimum: cap, slippage or copy floor, with atomic amounts and source average/post-price label. This is needed to decide what actually failed; never widen the chosen floor from the diagnostic.
- [ ] Extend post-settlement `evm_revert_reason()` evidence with replay basis: block-end, verified transaction prefix, or unknown. Reuse existing prefix replay when available, outside the entry path. Retain native receipts/calldata/pool binding in bounded public fixtures for future incidents; no fabricated BSC/META historical proof.
- [ ] Run focused floor/decide/receipt tests with `-n 2`; run saved ANR `verify.py`; commit.

**Acceptance:** A `reverted_min_out` classifier is not presented as original-prefix proof. No routing/admission change results from incomplete state. A later proposal to avoid knowingly failing sends must first prove usable current-state coverage and preserve the signed ceiling; it is not automatically authorized here.

## Task 7: Measure Solana inclusion after the existing Nozomi rollout

**Files:** `sniper/execution/submission_transport.py:244`; `sniper/execution/solana_real_seams.py:3245`; `sniper/services/memecoin_slot_lane.py`; `tests/test_memecoin_submission_transport.py`; `tests/test_memecoin_slot_lane_nozomi_ab.py`; `tests/test_memecoin_nozomi_all_wallets.py`.

- [ ] Add fixtures for TPU first acceptance + late matching Nozomi acceptance, wrong returned signature, timeout, not-started cancellation, rejected send and unknown confirmation. Verify identical signed bytes throughout.
- [ ] Retain actual per-provider POST start/finish and final state using the existing asynchronous late-result path. Never overwrite the early submission snapshot with a late answer; never equate provider acceptance with landing.
- [ ] Grade source-receipt head age, dispatch head age, source-to-landing slot, first acknowledgment, late Nozomi acknowledgment, confirmation observation and balance read separately. Use monotonic/local clocks for local durations; whole-second chain timestamps cannot provide millisecond latency.
- [ ] Freeze forward cohort at 09:02:05 UTC October 11, SHA `266e31d48`, all-wallet Nozomi B. Include failed/unknown sends. Stratify wallet, venue, source-receipt age into slot and effective fee. Current ACCELERANDO sample is one S+4 landing; no sender causality follows from it.
- [ ] First inspect POST scheduling delay: this case Nozomi started 2.325 ms after dispatch. Change ordering/worker reuse only if repeated records show avoidable local dispatch delay; keep same-byte race/ambiguity semantics and use a red delayed-provider test.
- [ ] Keep bounded 250 ms confirmation polling. Faster polling may reduce recognition latency, not actual inclusion. Do not raise fees/tips or buy a feed from this one sample.
- [ ] Run the three focused transport/Nozomi modules with `-n 2`; publish the measured cohort including unknowns.

**Acceptance:** The user can distinguish fast local send from slow inclusion and late acknowledgment from actual failure. Any local transport edit must show lower p50/p90 dispatch delay without delaying the earliest funded send.

## Task 8: Make the source decoder failure reproducible before changing it

**Files:** `sniper/services/memecoin_source_valuation.py:56,163,194`; `sniper/services/memecoin_source_initiation.py:70`; `tests/test_onchain_source_valuation.py`; `tests/test_pumpswap_v2_source_valuation.py`; `tests/test_memecoin_solana_source_initiation.py`.

- [ ] Preserve the already-decoded instruction name, program ID, account positions and typed failure locally when `unsupported_buy_instruction` occurs. Do not record credentials or perform another receipt call on entry.
- [ ] Obtain the exact retained/source receipt for `2vcqP5A3PJpGkeQZyQzgcCCgimJyFVQAugC2LvjfbaaCC2DHc4CJeVMCW9BzRmdp84EXyvPRhvvmQSoN2e9PRmP1` through a bounded research read outside the funded path if no local fixture is available. Until then, this incident's decoder repair remains evidence-limited.
- [ ] Fixture-test the precise name/layout/program mismatch. Current code already supports `buy`, `buy_exact_quote_in`, `buy_exact_quote_in_v2`; do not re-add support blindly.
- [ ] Correct only the proved binding/decode defect. A wallet must still be the actual signing/paying actor; a token balance increase alone does not authorize entry. Unknown USD valuation and unknown initiation are separate states.
- [ ] Run the three focused source tests with `-n 2`; commit only after the exact fixture fails before the correction and passes afterward.

**Acceptance:** Exact unsupported instruction is explained or retained as unknown. No generalized proof bypass and no replay of this trade.

## Task 9: Count the owned Ethereum reservation once in execution's gas reserve

**Files:** `sniper/services/memecoin_evm_risk.py:90`; `sniper/persistence/db_memecoin.py:11433,12114`; `sniper/execution/evm_real_seams.py:4858,4865`; `tests/test_memecoin_evm_native_spendable.py`; `tests/test_memecoin_evm_native_float.py`; `tests/test_memecoin_bsc_native_float_speed.py`; `tests/test_memecoin_evm_unwrap_guard.py`.

**Interface:** Extend `required_entry_native(conn, network_id, *, own_pending_id=None)`. The default continues to mean one additional future entry for gas alerts and admission. Execution passes its existing closure's validated `pid`; count that owned unsigned reservation through the formula's prospective `+1`, not in both the pending count and `+1`.

- [ ] Add a failing real-ledger test: freeze gas entry 0.001 ETH, approval 0.0001, exit 0.001, retry factor 2, minimum floor 0; the execution reserve remains 0.0033 ETH after its own reservation is inserted. A distinct second reservation raises it to 0.0055 ETH. The generic future-entry helper still returns 0.0055 ETH with one existing pending row.
- [ ] Use the actual `execute_fn` closure fixture in `test_memecoin_bsc_native_float_speed.py` with chain `1`, native-spendable enabled and a real in-memory ledger. Intercept its `NativeFloatProbe` to assert the correct floor was passed. Pure executor tests that stub `shortfall()` cannot detect this wiring defect. Existing helper mocks must accept the new keyword.
- [ ] Add the optional exclusion to the existing chain-local pending aggregation in `db_memecoin.py`, reusing `_unsigned_reservation_where()`. Require exact owned ID, normalized chain and every unsigned/submission/cleanup predicate; preserve the existing matching-open-position exclusion. Missing, wrong-chain, terminal, signed or cleanup-bound ownership never discounts another reservation. Do not subtract one blindly or use a pending row cached before quote. Keep manual/other/later reservations and the current open book counted.
- [ ] Pass the validated closure `pid` only at execution's existing reserve lookups at `evm_real_seams.py:4858,4865`. Leave the gas-alert/default callers unchanged. Replace the current pending query rather than adding a serial provider read, wait or lock; benchmark the altered local aggregation p50/p90.
- [ ] Preserve `max(actual_swap_gas_limit * actual_gas_price * 3 + LOW, correctly_counted_full_book_floor)`. Preserve the exact WETH-shortfall guard: true shortage or known zero still refuses; unknown native/gas/book reserve retains the existing full-unwrap behavior. Do not force-convert an in-swap route, lower the clip, lower gas buffers or enable maintenance to hide the error.
- [ ] Test own ID absent/wrong chain/terminal/signed/cleanup-bound, another pending inserted after admission, own already represented by an open position, zero WETH, insufficient combined funds and unknown provider inputs. Correct counting does not prove every historical balance move or open/pending interleaving safe; existing signing/binding fences remain authoritative.
- [ ] Retain already-read admission/native-probe balances, quote atomic balance, gas limit/price, book count/floor, final keep and computed shortfall in the existing post-decision trace. Unknown fields remain null. No new read or synchronous trace write before signing.
- [ ] Run the four focused modules with `-n 2 -o addopts=''`; review and commit this reserve correction separately from reporting/latency work.

**Acceptance:** The same candidate's reservation cannot fabricate an extra entry's exit reserve. Every other liability and the larger actual gas reserve remains protected. The controlled reproduction passes after the correction; a real insufficient-WETH case still refuses before signing. A successful replay of historical PAYZ is not promised without its missing native/gas inputs.

## Task 10: Explain the missed Ethereum early quote without changing route policy

**Files:** `sniper/services/memecoin_real_runner.py:2257,2282,2563`; `sniper/services/memecoin_evm_early_quote.py:45,112`; `tests/test_evm_early_cached_sizing.py`; `tests/test_memecoin_entry_early_quote.py`; `tests/test_memecoin_entry_early_quote_alt_take.py`; `tests/test_memecoin_evm_measured_ladder.py`.

- [ ] In `test_evm_early_cached_sizing.py`, pause the early worker after obtaining the successful shared tuple, let the main caller invalidate, then resume. Distinguish `balance_stale_generation` from missing tuple/quote (`balance_unavailable`). Record pre/current generation, sizing completion and this attempt's own invalidation generation/time through existing worker facts and post-decision telemetry. Capture generation through existing ownership/lock operations; do not add another pre-entry lock/read. Retain the original deadline and nonblocking one-shot launch claim.
- [ ] Preserve cancellation for any genuine other spend, missing balance, failed provider, clip/mint mismatch or late result. Do not remove `pre.stale()` or make the funded caller wait for the early worker.
- [ ] Bound the potential overlap gain from observed worker/gate/reservation timings before implementing a handoff. If repeated traces prove useful overlap is lost only to this attempt's own invalidation, propose an immutable, attempt-bound authoritative handoff under the existing launch claim; it must preserve detection of a different spend. Otherwise stop at the correct diagnosis. PAYZ alone does not prove the invalidator or a material speed improvement.
- [ ] Keep the existing best-measured-route grace and alternates. A separate proposed grace reduction must quantify latency versus executable round-trip quality, including failed/unknown outcomes; the roughly 355 ms wait for multihop here is not proof that fastest-hit selection is safe.
- [ ] Test missing quote versus known zero versus generation mismatch, other-attempt invalidation, cross-chain isolation, late/expired result, clip/mint mismatch and failed instrumentation. Run the three focused early-quote modules and measured-ladder module with `-n 2 -o addopts=''`; commit typed diagnostics separately. Any later handoff needs its own red concurrency/deadline test and independent review.

**Acceptance:** Provider failure and stale spend generation are distinct. Any overlap fix proves ownership and a measured gain; no route-quality or freshness policy is silently relaxed.

## Verification and activation

- [ ] Run each task's new test red, its implementation, then the specified focused regression modules. Use `-n 2 -o addopts=''`; no unbounded/full suite is needed for a reporting-only commit. If a full suite is required by integration scope, take `flock /home/bigpapi/.cache/codex-pytest-full.lock` and retain two workers.
- [ ] Validate the plan/changed schema references with `python3 scripts/validate_schema_refs.py --files docs/superpowers/plans/2026-10-11-entry-errors-and-execution-repair.md --warn-unknown-tables`; run `git diff --check` on changed files.
- [ ] Review money-path tasks independently. Keep reporting and execution commits independently reversible.
- [ ] Only after implementation is authorized and merged, build the exact immutable release with the repository deployment tool, explicit affected/core tests, reconciliation guards and `--defer-backup`. Activation is a separate action; do not restart or alter the funded runner during this planning task.
- [ ] Verify symlink, manifest, actual PID command/cwd, heartbeat effective configuration, exact API/DB result projection, unresolved reservations and preserved exit monitoring. Do not claim faster execution from a green suite or heartbeat.
- [ ] Compare organic same-chain/source-lane/release timing and failure counts. Retain price/proof/sizing controls throughout; never execute a synthetic funded transaction for validation.

## Runnable diagnostic check

`python3 docs/audits/2026-10-11-entry-errors/check_reporting.py`

This isolated in-memory check intentionally fails on the current reporting defects: disabled/passing volume shown as refusal evidence, and unchanged post-activation terminal evidence re-dated at restart. It uses no production DB, provider, signing, Telegram or service operation. A control checks that a genuinely new durable completion still pages. The implementation should move these cases into the owning pytest modules and make the check pass.

Evidence snapshot: [seven refs and related attempts](../../audits/2026-10-11-entry-errors/evidence.json). Existing ANR chain proof remains separately runnable in its incident directory. Explorer website fetches were unavailable; no new chain RPC was used in this audit.

PAYZ reserve reproduction: `python3 docs/audits/2026-10-11-entry-errors/check_payz_reserve.py`. One expected failure demonstrates the current post-reservation execution call overcounts by 0.0022 ETH; a control preserves the additional reserve for a genuinely different future entry. Move the fixture into the owning pytest tests and exercise the real closure when implementing Task 9.
