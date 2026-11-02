# Desk producer U: Pump.fun universe worker and candidate snapshots

Status: building (Lambda, 2026-10-21, branch `lambda/desk-universe`). Order: Alpha 2026-10-21.

Parents:
- `docs/superpowers/specs/2026-10-21-desk-execution-plumbing-design.md`: §2.0 producer U, §4
  bars, §7 data lifecycle;
- the umbrella `2026-10-21-llm-trading-desk-design.md`;
- Lambda's seats spec §1d and §2.4b (branch `lambda/desk-feeds-seats`).

Ownership: Beta owns `desk_tickets`, the seat validator, the hard filter, intents and
reservations. Lambda owns this producer and the model harnesses. Until Beta agrees a DDL,
emissions go to `data/desk/candidate_emissions/<YYYY-MM-DD>.jsonl` and to no table.

## 1. What it does

A separate process, `scripts/memecoin_desk_universe_worker.py`, runs on its own gRPC connection
with the filter from seats §1d:
- `transactions{pump: vote=False, failed=False, account_include=[Pump.fun program]}`;
- `blocks_meta{}`;
- commitment CONFIRMED.

`sniper/services/memecoin_desk_universe.py` holds all decisions as pure, tested code:

1. **Decode.**
   - Pump.fun anchor events come from the transaction's inner CPI instructions
     (`e445a52e51cb9a1d` prefix), with `Program data:` log lines as a fallback.
   - They are decoded by the opportunity engine's `decode_pumpfun_event` and wrapped in its
     `OpportunitySourceEnvelope`:
     - source_key `opportunity:pumpfun_grpc`, provider `shyft_grpc`;
     - cursor `slot:sig:ordinal`; confirmation CONFIRMED;
     - block_time and blockhash joined from `blocks_meta` by slot.
   - A blocked decode is counted and never dropped silently.
2. **Curve state** per mint, from each trade's own virtual reserves, never a provider mid:
   - price_sol = (virtual_sol / 1e9) / (virtual_token / 1e6);
   - mcap_usd = price_sol × total supply (tokens) × SOL/USD.
   - Total supply comes from the create event. When the create was not seen, the Pump.fun
     constant of 1e9 tokens is used, labelled `assumed_supply`.
3. **Band detection.** A candidate is emitted when curve mcap moves from outside [30k, 100k] to
   inside it.
   - Re-entry emits again only after the coin left the band and `MEMECOIN_DESK_UNIVERSE_COOLDOWN_S`
     has passed.
   - A mint whose state is unknown never emits.
4. **1 s bars on the slot clock** for watched pools only (bounded by
   `MEMECOIN_DESK_WATCH_MAX_POOLS`). A pool joins the watch set at band entry.
   - Bucket key = slot. A bar is the set of slots whose `blocks_meta.block_time` falls in that
     second.
   - A slot with no block time is `DATA_INSUFFICIENT`; it is never converted with an assumed
     0.4 s.
   - A bar carries slot_lo, slot_hi, n_swaps, n_makers, n_slots_missing_time and gap.
5. **Candidate snapshot** (seats §2.4b). One immutable JSON per emission.
   - Every field is `{value, source, known_at, slot, completeness}`, and
     `known_at` ≤ snapshot time.
   - `completeness` ∈ complete | partial | missing | window_not_elapsed.
   - Candles go in by reference (pool, slot_lo, slot_hi, sha256), never inline.
6. **Persistence** (plumbing §7). Raw frames never touch disk; they sit in a RAM ring bounded by
   `MEMECOIN_DESK_UNIVERSE_RING_MB`. Day files are `data/desk/<stream>/<YYYY-MM-DD>.db` for
   `launches`, `candles_1s` and `swaps` (the ML corpus).
   - Soft cap `MEMECOIN_DESK_DISK_SOFT_CAP_GB`: stop candles for the oldest watched pools.
   - Hard cap `MEMECOIN_DESK_DISK_HARD_CAP_GB`, or free disk under `MEMECOIN_DESK_MIN_FREE_GB`:
     stop the producer and page.
   - Retention and Parquet compaction are a separate job (plumbing §7.2a). It is not part of
     this step.
7. **Knob** `MEMECOIN_DESK_UNIVERSE_ENABLED`: 0 means the worker exits at once.
   - It is ON at deploy per the standing order.
   - The deploy is gated on the throttle result and Gamma's pass, not on the default.

**Events the opportunity decoder blocks (2026-10-21).** These were 13.5% of decoded lines in the
11:37Z capture. They are named from the official IDL (github.com/pump-fun/pump-public-docs
`idl/pump.json`) and handled here, without touching the funded-path decoder
`sniper/research/memecoin_opportunity/pumpfun.py`.

| discriminator | event | handling in U |
|---|---|---|
| e3bed7ceb0b4a584 | DistributeFeeToHoldersEvent | counted `known_irrelevant` |
| 742b4dbd117a482b | SweepBondingCurveFeeEvent | counted `known_irrelevant` |
| 929fbdac925838f4 | CloseUserVolumeAccumulatorEvent | counted `known_irrelevant` |
| 86240d48e86582d8 | InitUserVolumeAccumulatorEvent | counted `known_irrelevant` |
| a537817004b3ca28 | DistributeCreatorFeesEvent | counted `known_irrelevant` |
| 7a027f010ebf0caf | CollectCreatorFeeEvent | counted `known_irrelevant` |
| e2d6f62107f293e5 | ClaimCashbackEvent | counted `known_irrelevant` |
| 6161d7905d92167c | ExtendAccountEvent | counted `known_irrelevant` |
| 9ba768dcd56cf303 | MigrateBondingCurveCreatorEvent | counted `known_irrelevant` |
| 6fb06d8b316cd5fb | PostCompleteBuyEvent | curve-complete flag on the mint (bytes 40..72); the mint is treated as graduated |

Any other blocked discriminator is counted as `blocked_disc:<hex>`, with 3 samples
(length, signature, slot).

The launch rate is 34/min decoded. A grep for "Instruction: Create" over-counts, because it
also matches the token program's "Instruction: CreateIdempotent".

**Out of v1, stated:**
- PumpSwap pools after graduation. A graduated coin's state becomes `graduated` and its mcap
  null, and it emits nothing more. Coins graduate inside the band (~$70-90k at today's SOL
  price), so the top of the band is under-covered until PumpSwap decode is added.
- H producer: Beta's or a later step.
- Any seat call.

## 2. Rule-5 edge-case map

1. **Money path: no.** The producer only emits candidates. The desk funnel (hard filter, JUDGE,
   gate, engine) decides, and every gate of its own still runs. Indirect effect: a wrong
   candidate costs a seat call and maybe a bet, so the mcap math and band edges are tested.
2. **Fill path: none.**
   - It is a separate process with its own gRPC connection.
   - It never imports the runner, holds no ledger handle and makes no funded RPC read.
   - SOL/USD comes from the funded Jupiter quote seam (`sol_usd_from_jupiter`) at most once per
     60 s. That is the only shared dependency, at about 1,440 calls/day against a keyed quota
     that runs 464k/day.
   - **Shared Shyft token:** the one real risk. It was measured by the throttle test (A vs B,
     2026-10-21), and the result is appended when final.
3. **Concurrency and global state.**
   - One asyncio loop, one writer per day file.
   - No module attribute is mutated after start, no env var is written, no connection is shared.
4. **Absent vs zero vs unknown.**
   - A stream gap or reconnect, or ring overflow, makes every mint's curve state `unknown`
     until its next trade event; mcap is null, so there is no emission.
   - A missing block time gives a `DATA_INSUFFICIENT` bar.
   - A SOL/USD read older than `MEMECOIN_DESK_UNIVERSE_SOL_MAX_AGE_S` makes mcap null.
   - A window not yet elapsed is `window_not_elapsed`, never 0.
   - A pool not in the watch set has null bars, never a flat tape.
5. **Dependency failures.**
   - Shyft disconnect, timeout, auth refusal or a rate-limit status: reconnect with capped
     backoff, write a gap row (slot span, cause), mark all state unknown, and log a WARNING.
     Logger.info never reaches the logs (finding 2026-10-16).
   - `from_slot` replay on Shyft is unverified and not relied on.
   - Decode blocked: counted per blocker.
   - Jupiter SOL price failure: mcap null, counted.
   - Disk over the hard cap or a write error: the process stops and pages. It never fills
     another lane's DB.
   - A wrong answer from the stream (a reorg at confirmed): very rare; the envelope carries
     CONFIRMED and is never upgraded.
6. **Units and identity.**
   - SOL lamports 1e9; pump tokens 6 decimals; slot vs block_time in seconds.
   - network_id `solana` as a string, mints in base58 as-is.
   - The curve account is the pool id on curve.
7. **The sample.**
   - Emissions cover only band entries seen while the stream was healthy. Gap rows give a read
     its denominator holes.
   - Our own desk buys appear in the stream, and the snapshot excludes our signatures from
     activity counts where a field says "excluding ours".
8. **Rollback.**
   - Set the knob to 0 or kill the process. Nothing else depends on it.
   - Half-applied (worker down while the desk is up): the desk simply sees no U candidates.

**Check left behind:** `tests/test_memecoin_desk_universe.py`. It fails if any of these regress:
- band entry with re-entry cooldown;
- unknown state after a gap emits nothing;
- a slot without a block time gives DATA_INSUFFICIENT and never a guessed second;
- known_at ≤ snapshot time;
- mcap math against a hand-computed reserve pair.

## Prospective paper-input amendment — 2026-10-27

The v27 input/integrity contract is `2026-10-27-desk-pumpswap-inputs-integrity.md`; its dated event-account amendment and `docs/audits/2026-10-27-desk-event-account-envelope-proof.md` govern the new loss proof. The worker retains at most eight first-only full multi-hop response witnesses, each encoded file at most256KiB, from its existing stream. This is a bounded exception to RAM-only raw-frame handling; no new provider calls or funded hooks are added.

Newly inserted discarded transactions may also carry an atomic complete-account event-absence proof. It never substitutes account keys for pool IDs or decodes multi-hop prices. Prior losses/decisions remain unchanged, missing evidence stays unknown, and proof availability has an explicit activation epoch. Exact runtime activation and organic model/fill evidence remain separate checks.

## Bounded state-witness exception — 2026-10-27

The separately approved state-event diagnostic namespace is an additional bounded raw-frame exception; it does not replace or expand the eight first-only multi-hop slots above. See section 9 of `2026-10-27-desk-pumpswap-inputs-integrity.md` and `docs/superpowers/plans/2026-10-27-desk-state-witnesses.md`: six fixed event kinds plus three exact subtypes,20slots/key/host-UTC-day, hard200/day, encoded files at most256KiB and three days of owned retention (current135MiB; hard150MiB; foreign files preserved). The startup kill knob is `MEMECOIN_DESK_PS_STATE_WITNESSES`. Capture remains on the existing stream with guarded initialization/capture/status and no new provider/funded hook. First-N witnesses are diagnostic only: they grant no effect, price, pool-state, loss-clearance or cohort authority, and cannot promise capture of every later matching receipt. Original losses and ordinary tape processing remain intact.

## Explicit v28 consumer interpretation — 2026-10-27

Section 10 of `2026-10-27-desk-pumpswap-inputs-integrity.md` adds an explicitly registered v28/facts-v2 consumer interpretation under executed freeze `45895eb569bece68c527cb6438944f5530b1013e` Amendment 3. Whole-window seen lower-bound flow and partial chart demand are separate from the proven-complete suffix; missing evidence never becomes quiet or safe. Default v1/v27 and producer loss/proof authority remain unchanged. The independent state-witness release does not activate v28 or change cohort routing.
