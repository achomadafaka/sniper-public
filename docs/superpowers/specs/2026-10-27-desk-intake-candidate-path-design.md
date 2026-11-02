# Desk intake candidate path (design, phase 2 of the curve retirement)

Status: initial slice implemented and reviewed; dated amendments govern each cohort's scope. Original rev 2 proposal retained below. Author: Beta, 2026-10-27.
Operator order: brain decision 48d0986c (2026-10-27). The Pump.fun curve tape is retired and band-crossing entries
stop. Desk candidates come only from the intake feeds. Graduated coins are the target. Market cap is a safety bound
only. No paper activation until the intake feeds run.

## 1. What changes

Before this change, U band crossings on the curve tape create desk candidates. The candidates pass the hard filter
(mcap band, safety proofs) and then the JUDGE seat, the watch-list and standing orders.

After this change:

```
intake feeds (pumpfun lists, dexscreener boosts/profiles, fomo lists)
  -> data/desk/intake/<feed>/<day>.db intake_rows   (collect-only, already built)
  -> intake emitter (NEW, own process): a coin currently listed by a feed AND graduated
     to a PumpSwap pool with a fresh proven pool state -> one desk candidate
     (producer "U" + intake field, venue pumpswap, feed label kept; section 9)
  -> data/desk/candidate_emissions/<day>.jsonl   (existing path; assembler, hard filter, JUDGE unchanged in shape)
  -> hard filter policy intake-v1: NO mcap band; safety bounds only
  -> watch-list / JUDGE (playbook v26, new cohort)
  -> PumpSwap prints drive watch relooks, standing fires and re-entry   (NEW: today only curve prints drive them)
```

## 2. Intake emitter

- Input: `intake_rows` of every feed directory, tailed by a cursor per day file.
  Gamma's study uses `first_sightings`. The desk uses current sightings: a coin first seen on `pumpfun:new` while it
  was on the curve must be able to enter when it later graduates and shows up on a list.
- Graduation proof: the coin's canonical PumpSwap pool (`memecoin_desk_ps_tape.canonical_pool(mint)`) has a usable
  state in the PS tape (`lessons.load_pool` + `_LearningTape.pool_value_at` barriers: fresh within POOL_FRESH_S, no
  newer invalid state, no PS capture gap after it, PS cut committed through the emission time).
  No proven pool state means no candidate. The emitter counts it under a reason, for example `not_graduated`,
  `pool_stale`, `ps_capture_unproven`. It never guesses.
- Candidate: the existing PumpSwap candidate schema (`K.PUMPSWAP_SCHEMA`, venue `pumpswap`), producer `U`, and a new
  field `intake = {feed, filter, provider_rank, rank_kind, observed_at, host_received_at, clock_offset_s,
  intake_row_ref}`. The contract allow-lists the field. Section 9 explains why the producer stays `U`. The mcap and liquidity snapshot comes from the PS pool reserves (pump.fun supply 1e9
  whole tokens). No provider number is ever a price, fill or mark.
- Freshness: emit only when the sighting's `observed_at` is within `MEMECOIN_DESK_INTAKE_SIGHTING_MAX_AGE_S`
  (default 120 s) of now. A sighting with NULL observed_at (clock offset unknown) never emits.
- Dedupe: one candidate per `(mint, feed)` per `MEMECOIN_DESK_INTAKE_COOLDOWN_S` (default 3600 s). A coin listed by two
  feeds is two candidates sharing one mint. The existing per-mint head and claim rules decide which one gets a JUDGE
  seat. Section 11 covers durability and the losing feed's record.
- Kill knob: `MEMECOIN_DESK_INTAKE_EMIT` (default 1 once reviewed; the cron line is installed by Alpha).

## 3. Hard filter policy intake-v1

- No `mcap_band` kill. `mcap_usd` stays a SAFETY bound: `mcap_safety = [lo, hi]` (proposed 5k..50M), outside it
  `kill:mcap_safety`, unknown mcap `unknown:market_data:mcap_usd`.
- Minimum pool liquidity: `liq_min_usd` (proposed 10k; PS real quote reserve x SOL/USD, causal FX).
- Maximum exit size: the existing `sell_probe.round_trip_loss_pct` at the cohort clip, with `max_round_trip_loss_pct`
  (proposed 8 %). An unknown probe refuses, as today (mandatory exit dependency).
- Unchanged: token_safety, market_data, sell_probe mandatory; cluster, holders and dump_group are soft facts, except
  the dump-group kill (Lambda).

## 4. Watch-list, standing orders and re-entry on PumpSwap prints (Alpha's carry-over)

- `Worker.on_pumpswap` calls the same `watch_relook`, `standing_fire` and re-entry hooks as `on_swap`. It uses the
  PS pool price (proven virtual quote, `load_pool` semantics) and the PS chain-time floor.
- The hooks are venue-blind today: they key by mint. PS prints for a curve-venue order must not fire it, so the order
  carries its venue, and a mismatch is skipped and counted.

## 5. Per-position readers for a graduated position

These readers currently read curve swaps only. Each gets a PS branch keyed by the position's venue: structure sheet,
attention, risk_tape and the risk watcher (live and paper), exit observer, paper fills/marks
(`memecoin_desk_paper.sell_proceeds` already prices graduated coins on the pool). `capture_proof` takes
`venues=("ps",)` for PS positions. A reader without a PS branch returns `unknown("venue_unsupported")`, never 0.

## 6. Playbook and cohort

- Playbook v26 = v25 plus three things. Candidates come from a named feed list, and the sheet shows feed, filter and
  provider rank as context, never as a signal score. The coin is graduated, the pool is PumpSwap, and the exit is a
  pool sell. Market cap is no longer an entry band.
- New cohort, frozen policy: `hard_filter.intake = {mcap_safety, liq_min_usd, max_round_trip_loss_pct}`, no
  `mcap_band`, `watch_list` as v25, producer `U` with the `intake` field (section 9). Old cohorts keep their policies; no row mixes.

## 7. Edge cases (rule 5)

1. Money path: yes. This produces desk entries. Paper first: no activation until the intake feeds run (operator).
   The funded desk entry path is unchanged in shape and still gated by its existing knobs.
2. Fill speed: the emitter is its own process, off the fill path. The PS hook additions in `on_pumpswap` add a dict
   lookup per print; to be measured (p50/p90) before the deploy.
3. Concurrency: one emitter per state root (flock). Read-only connections to the intake and PS tapes.
4. Absent vs zero vs unknown: NULL observed_at, no pool, stale pool, unknown FX, unknown probe: each is a named
   no-candidate or SKIP reason, never 0.
5. Dependency failures: an intake feed down means its poll rows are errors and no fresh sightings, so no candidates.
   A PS capture gap means no proven state, so no candidate. A failing emitter pass is logged with its exception and
   the cursor does not advance.
6. Units and identity: `(network_id='solana', mint)`. Pool = canonical PumpSwap pool of the mint; a mint with a
   different or non-canonical pool is a reason, not a guess. Reserves are atomic; SOL is 1e9 lamports; supply is
   1e9 tokens x 1e6 atomic (pump.fun).
7. The sample: feed lists are promotion and attention, so a candidate's selection depends on the provider. Gamma's
   intake read (0304e1283 + Amendment 1 61de43949) measures the feeds. Desk results are reported per feed label.
8. Rollback: `MEMECOIN_DESK_INTAKE_EMIT=0` stops new candidates, and the cohort's existing tickets keep their
   policy. Re-enabling curve capture (`MEMECOIN_DESK_CURVE_CAPTURE=1`) restores U band emissions only on an old cohort.

## 8. Build order (each step reviewed separately)

1. Contract: `intake` candidate field plus hard filter policy `intake-v1` (no band, safety bounds), with tests.
2. Intake emitter process plus watchdog (shape of the intake collector watchdog).
3. PumpSwap-driven watch, standing and re-entry hooks in the universe worker.
4. PS branches in the per-position readers (one commit per reader owner).
5. Playbook v26 plus cohort provisioning, with Gamma's read on what the cohort reports.

## 9. Routing and schema (Delta rev-1 gap 1)

Producer stays `U`. A new producer letter would need table rebuilds: SQLite cannot alter CHECK constraints, and
`sniper/persistence/db_memecoin_desk.py` has `CHECK(producer IN ('H','U'))` on cohorts (L95), heads (L102),
emissions (L112) and tickets (L121). Contract L191 and provisioning L191 would need edits too, and every U/H reader
would need a review. The intake origin is carried by the `intake` field and the ticket-id prefix (section 11).
The JUDGE's cohort and playbook (v26) separate the arm, not the producer column.

Callers that change:
1. `sniper/services/memecoin_desk_contract.py` `validate_candidate`: a PumpSwap-schema candidate with `intake` is
   accepted (keys plus shape of `intake`). A PumpSwap candidate without `intake` keeps today's rule.
2. `scripts/memecoin_desk_assembler.py` L525/L530: today it drops every PumpSwap-venue emission (`emission_venue`).
   A candidate with `intake` goes to `verify_intake_emission(cand, now)` instead of `S.verify_u_emission`.
   `verify_intake_emission` re-reads the referenced intake row (same feed/filter/mint/observed_at) and the PS pool
   state the snapshot came from, then ingests through the unchanged `desk.ingest_candidate`. Curve U emissions keep
   their path byte for byte.
3. Hard filter: `contract.hard_filter` reads `policy["intake"]` (section 3) when the cohort has it. `mcap_band` is
   then absent and `mcap_band()`'s default is never used for such a cohort. `memecoin_desk_provision_cohort.py`
   gets `--intake` to freeze the policy and refuses `--mcap-band-*` with it.
4. Acceptance test on a desk DB created by today's schema: a real intake emission goes through ingest, hard filter
   and JUDGE claim, with tickets, heads, emissions and stages written. U and H cohorts and their tests are
   unchanged.

## 10. Clocks and cut (Delta rev-1 gap 2)

Two clocks, never diffed:
- `intake_rows.observed_at` is CORRECTED UTC. It is used only for the sighting age: `corrected_now - observed_at`,
  where `corrected_now = host_now - clock_offset_s` and the offset is the row's own `clock_offset_s`. A NULL or
  non-finite offset, or an age < 0 (future) or > MAX_AGE, means no candidate (`sighting_clock_unknown`,
  `sighting_future`, `sighting_stale`).
- Every PS read is on the HOST clock (`received_at`, `load_pool`, `capture_proof`). The emitter freezes ONE host
  `as_of = cut`, where `cut = capture_proof(desk_dir, host_now, venues=("ps",))["through"]` (a healthy cut lags
  host now by up to COMMIT_SLACK_S; None means no candidate, `ps_capture_unproven`). The pool state is the one
  `_LearningTape.pool_value_at(as_of)` accepts. The candidate's `emitted_at` is the actual host now; its snapshot
  carries `as_of` and `state_t`.
- Tests: a nonzero offset (+7 h) with a fresh corrected age emits; a 1 s commit lag (cut = now - 1) emits at
  as_of = cut, never at now; a newer invalid PS state in (state_t, as_of] gives no candidate.

## 11. Identity, durability, cooldown (Delta rev-1 gap 3)

- Ticket id: `desk:U:intake:<sha256(network_id|mint|feed|filter|intake_row_ref|cohort_id)[:32]>`. It is
  deterministic, so a replay of the same sighting gets the same id. It is distinct per feed and filter, so two
  feeds sharing one pool state never collide (today's U ids derive from the trigger swap, so two feeds on one swap
  would collide).
- Durable claim: an emitter DB `data/desk/intake_emitter.db` with table `claims(feed, filter, network_id, mint,
  claimed_at_host, ticket_id, intake_row_ref, status)`, PRIMARY KEY (feed, network_id, mint, window). Each pass:
  (1) INSERT the claim with status `pending` and commit; (2) append the candidate line to candidate_emissions (an
  fsync'd append); (3) set the claim to `emitted` and advance the intake cursor in the same transaction.
  On restart a `pending` claim re-appends the same line. The assembler's ingest is idempotent on ticket_id
  (`duplicate`), so a crash at any step neither loses the candidate nor ingests it twice.
  Cooldown = the claim's window, so it survives restarts.
- Midnight: cursors are per intake day file; the claim key holds no day, so a coin listed at 23:59 and 00:01 is one
  claim.
- Per-feed denominator: every sighting the emitter reads gets one outcome row in `outcomes(feed, filter, mint,
  intake_row_ref, reason)`. Reasons: `emitted`, `not_graduated`, `pool_stale`, `ps_capture_unproven`,
  `sighting_stale`, `sighting_clock_unknown`, `cooldown`. A feed that loses the per-mint head keeps its emitted
  ticket. The assembler's typed `duplicate` or head-loser reason stays on that ticket, so each feed's origin and
  denominator survive selection.
- Tests: crash after the append and before the cursor, then restart (one ingest); the same mint on two feeds from one
  pool state (two ids, one head, the loser's reason kept); a midnight split (one claim); a restart inside the
  cooldown (no second emission).

## 12. Current implemented slice — 2026-10-27

Merged `04e67e6e2928cf0844a734cc046bb874267ec0de` contains the reviewed intake/assembler, PumpSwap paper accounting and GPT caller integrations. This records implementation scope; it grants no provisioning, `.env`, funded or model-arm activation authority.

- The initial cohort enrolls **Pump.fun lists only**, enforced by `IntakeBook.FEEDS=("pumpfun",)`. DexScreener and Fomo remain collection-only for this cohort. Its real quote-reserve floor is **40 SOL**, superseding the proposed USD floor in sections 3 and 6; market-cap safety remains 5k–50M USD and round-trip loss bound remains 8% at the $25 clip.
- New intake provisioning freezes JUDGE `gpt-6-luna`, provider `codex_cli`, reasoning `low` and subscription accounting into the policy hash. The isolated adapter prefers the executable bundled CLI; historical Claude cohorts retain their original identity. Busy capacity refuses immediately, tool use is refused, and there is no automatic retry or Opus fallback. Any supplied generation-token cap that Codex cannot enforce refuses before dispatch; absent cap remains absent. The configured decision deadline is unchanged.
- This first v26 slice records ENTER/SKIP; WAIT is recorded without firing. Intake provisioning rejects watch-list and standing-order combinations. The broader watch/standing/re-entry design in sections 4 and 6 is not an activation claim for this slice. Initial routing is one v26 primary playbook; mixed-playbook routing stays deferred.
- The dedicated assembler owns bounded mint reads, the persisted hard-filter result, daily read budget and atomic byte cursor with deferred tails. It retains incomplete old-day sources while newer days progress; missing/corrupt sources are named gaps. PumpSwap paper execution uses the reviewed kernel and persisted mint decimals, with exact atomic quantities stored as TEXT. Neither milestone proves organic fills or profitability.
- The authoritative counting contract is `docs/audits/2026-10-27-intake-desk-cohort-preregistration.md`, through **Amendment 4 at `84f35a5a496009116484937579f3dbce706bf126`**. P1 starts at first PASS plus delay; judged ENTER/SKIP executable comparisons start at the model response plus delay, never before the answer. New provisioning must bind that freeze and a new policy hash. Existing cohorts remain separate.

The merged code passed 1,111 focused checks plus an offline actual-caller/transport seam. Runtime activation and forward evidence remain separate gates; no ENTER-versus-SKIP outcomes were read for this amendment.

## 13. Three-feed enrollment amendment — 2026-10-27

The operator subsequently requested Fomo and DexScreener candidate supply alongside Pump.fun. This requires a **new** paper cohort, frozen under `docs/audits/2026-10-27-three-feed-desk-cohort-preregistration.md`; it does not widen the cohort recorded in section 12.

- Optional `hard_filter.intake.feeds` is a nonempty sorted unique list from `dexscreener`, `fomo`, `pumpfun`. Missing `feeds` means legacy Pump.fun-only; old policy bytes and IDs remain unchanged. Provisioning `--intake-feeds pumpfun fomo dexscreener` canonicalizes the list and produces a new policy hash. Malformed enrollment refuses before either registry write.
- The universe resolves its source list once from the configured cohort using a read-only local lookup. Enrollment-resolver failures inside the worker disable intake with a named error while capture continues. Earlier main-process band validation is unchanged: malformed registry JSON or a malformed frozen band can still refuse startup before that catcher. The module's legacy default is not broadened. Local feed reload remains off the tape write path and provider polling cadence is unchanged.
- Candidate ingestion checks frozen feed membership before storing an emission or consuming a mint RPC; the hard filter checks it independently. Collector row/feed identity and pending-replay membership are preserved. This does not implement the earlier proposed `verify_intake_emission` source-row verifier in section 9; the existing collector-backed emitter remains the source of sightings.
- The unchanged v26 prompt already accepts each feed label. Feed, filter and returned-list position are context only. Dex boosts, profiles, takeovers and ads are paid/self-submitted promotion, not trending evidence. Only Solana sightings followed by a proven buy on a canonical graduated PumpSwap pool qualify; a Fomo bonding-board sighting never authorizes a curve entry.
- Existing safety bounds, $25 clip, five-position limit, paper routing and the **shared** 40-call/hour JUDGE cap remain. No extra per-feed call budget, ranker, model arm or funded authority is introduced. The previously identified cumulative paper-loss enforcement defect is separate and remains disclosed in the new preregistration.

## 14. Prospective v27 input/integrity amendment (Codex, 2026-10-27)

Operator request: address the remaining Desk gaps. The contract is `2026-10-27-desk-pumpswap-inputs-integrity.md`, with a new v27 playbook and `docs/audits/2026-10-27-three-feed-desk-inputs-v27-preregistration.md`. Registration and activation remain separate; prior v26 cohort inputs and evidence are not reinterpreted.

- New admissions authenticate the original collector row and successful parent poll through an indexed local verifier. A stored immutable receipt permits replay after source pruning; a caller-supplied pass/hash does not authenticate it. All three verified observation clocks survive; provider-reported creation stays Pump.fun-only.
- Complete instruction/account/ABI receipts can scope a newly captured decoder loss to its affected pools. Individual receipts close the coalesced-marker hole. Unknown, legacy, incomplete and global losses remain conservative. Strict readers reach JUDGE, assembler, paper prices and PAPER RISK; legacy cohorts retain their reader contract.
- New sheets bind PumpSwap chart/flow/ranges and explicit coverage to one claim receipt/slot/FX cut. Verified birth, pool birth, observation span and provider age remain separate. Missing history stays unknown. Available general attention context is enabled; retired curve aggregates remain unknown. Initial hard-filter block metadata may arrive after the print, with its actual later knowledge recorded separately.
- The registered cumulative paper-loss limit is now enforced by a permanent per-cohort latch. Atomic admission and final pending-fill checks prevent concurrent overruns; schema/lock/quote delay contributes to actual enforcement knowledge. Known stops cancel unfilled requests; open exits and PAPER RISK continue. This does not backdate enforcement of the earlier cohorts.
- Before switching the single primary, drain/account for pending old emissions, assembler tail, active heads and outbox. Preserve cursors and cooldowns and never reattach old globally keyed emissions to the new cohort. Registration remains operator-only; code availability is not proof of activation, organic model calls, fills or edge.

## 15. Prospective event-account proof amendment — 2026-10-27

V27 may implement the same pinned-ABI instruction/account-attestation rule through the separate proof in `docs/audits/2026-10-27-desk-event-account-envelope-proof.md`. A closed supported transaction's complete resolved account set can prove absence of a recognized AMM event for a target pool; readonly accounts are included. Its compact Bloom representation can only add conservative false positives. It does not provide multi-hop prices or continuous vault/fee state, and it never changes `pools_json` or attaches proof to old losses.

Keep original unknown receipts, actual proof knowledge times, generic gaps, reserve checks, source/cooldown clocks and old decisions. Record a prospective release epoch under the same v27 policy/CID/prompt. Missing, corrupt, unsupported, future or incomplete proof remains global. The bounded eight-slot diagnostic exception is defined in the input/integrity spec's dated amendment; a diagnostic witness alone grants no acceptance authority.

## 16. Explicit v28 partial-observation amendment — 2026-10-27

V28/facts-v2 is a new prospective policy/cohort, governed by `2026-10-27-desk-pumpswap-inputs-integrity.md` section 10 and preregistration `45895eb569bece68c527cb6438944f5530b1013e` Amendment 3. It shows all causal usable seen 60-second swaps as lower bounds, retains zero-suffix observations and partial chart demand, and reports complete suffix coverage separately. Unknown safety and complete price proofs remain conservative; default v1/v27 and old decisions/identities remain intact. The v28 system prompt is hash-bound; strict paper/RISK routing accepts only its supported frozen facts version. New registration authority, exact pinned validation and single-primary draining remain prerequisites to activation. Neither this source change nor diagnostic capture grants funded authority.
