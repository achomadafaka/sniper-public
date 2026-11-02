# V5 Final Engine — implementation plan

> **SUPERSEDED 2026-07-26 (operator decision).** V5 is no longer the go-forward "final engine."
> V6 (`docs/superpowers/specs/2026-07-26-memecoin-v6-live-paper-engine-design.md`) is the **single
> candidate exit lineage**. It is tested by owning realistic live-paper positions after their fills
> on Solana and 4663. There is no V5 comparison arm and no replay acceptance harness. One engine at
> a time.
>
> **Retired by this banner:** WO-8's "arm V5 as funded decision authority," WO-5's V5-tuned sizing
> constants as a *V5 deliverable*, and the framing of V5 as final.
>
> **NOT retired — retained as V6's data/execution foundation (do not revert):** the merged,
> engine-agnostic plumbing — WO-1 per-rule evidence contracts, WO-2 tx-grounded sale authority,
> WO-3 LP severity, WO-4 typed quote-failure, WO-5A controller state, WO-6 execution binding,
> WO-9 EVM hot lane, WO-10 V3 depth decode, and WO-7 fill/capture plumbing. V6 live paper needs this
> point-in-time evidence (route degradation, source-wallet state, chain volatility, wallet tier), so
> it stays. Replay/OOS evaluation work is retired.
>
> **Live-money note:** neither V5 nor V6 is armed. `MEMECOIN_ENGINE_AUTHORITATIVE_CHAINS` is empty;
> real funded exits run on the hard-rail envelope + existing pull logic. Retiring this plan changes
> nothing funded.

**Date:** 2026-07-25
**Spec:** `docs/superpowers/specs/2026-07-25-memecoin-v5-ultimate-exit-engine-design.md`
**Scope:** the performance-relevant subset of Phases 1–4. Every work order below changes what the
engine decides or executes — no observation-only scaffolding. End state: v5 wired in as funded
authority and deployed. Validation is one deterministic replay comparison, not a promotion ladder.

## Operator decision recorded (2026-07-25)

No forward-paper phase, no live canary cohort, no staged promotion. The single acceptance gate is
the same-tape replay (WO-7) plus green tests. Deploy uses `scripts/deploy_memecoin_runtime.sh`
as-is — its built-in SQLite backup and contract reconcile are part of the deploy command itself
(seconds, not ceremony) and stay.

## Work orders — dependency order

### WO-1 — Per-rule evidence contracts
**Why (performance):** today one stale/missing family blocks every rule. An LP pull during quiet
tape (no swaps ≥180 s → price/flow/vol untrusted) cannot CUT — the exact window rugs happen in.
Fixing this converts missed-rug full losses into fast exits, and stops TP/DERISK being vetoed by
irrelevant families.
**Change:**
- `sniper/analysis/exit_state_types.py` — `candidate_capable(families)` takes the rule's required
  set; per-family trust already exists in provenance, use it per rule.
- `sniper/services/memecoin_exit_brain.py` — each rule declares its evidence:
  LP CUT → depth + tape-complete + route health (NOT fresh swaps);
  structure CUT → price + flow (decoded, WO-2);
  TP / vol-DERISK → structure + vol + arrival + route;
  time-DERISK → position state + executable no-progress (no flow/vol requirement);
  RIDE → structure + flow + route. Anything missing → DEFER (envelope stays authoritative).
- `sniper/services/memecoin_hot_supervisor.py` `state_fn` — emit per-family trust instead of
  collapsing to one DATA_INSUFFICIENT label; keep hard per-family freshness caps (no decay).
**Tests:** quiet-tape + fresh-burn → CUT fires; stale-vol + TP-shaped state → TP blocked but
LP CUT not; each rule's contract exercised both ways.

### WO-2 — Transaction-grounded sale authority on the hot lane
**Why (performance):** slot-netted vault deltas fabricate burns (fee claims, multi-tx slots) and
can corrupt flow sign → false CUTs sell winners. Decoded evidence keeps sub-second latency while
killing the false-exit class. Invariant: vault-delta-only evidence may wake + prewarm; it may
never authorize CUT/DERISK/TAKE_PROFIT.
**Change:**
- `sniper/services/memecoin_solana_ws.py` — keep accountSubscribe as wake. Add signature capture:
  probe `logsSubscribe` (mentions=vault) on the free WS first; fallback =
  `getSignaturesForAddress` since last-seen + `getTransaction` (both already exist in the pull
  tape / `memecoin_solana_amounts.py`).
- `sniper/services/memecoin_hot_supervisor.py` — on a sale-shaped decision, resolve the
  triggering tx(s) through `vault_rows()` and re-propose on decoded rows before handing the
  decision to `on_decision`. Wake→prewarm proceeds in parallel; decode cost lands only when a
  sale is on the table.
- `sniper/services/exit_engine_hot.py` — ring accepts decoded rows replacing the provisional
  delta rows for the same slot (keyed by slot + tx_sig).
**Tests:** fabricated same-sign slot (swap + fee-claim fixture) → no CUT without decode; decoded
real burn → CUT; decode failure → fail-closed to pull lane, never a delta-authorized sale.

### WO-3 — LP-removal severity (depends on WO-2)
**Why (performance):** any-burn→CUT full-exits healthy positions on benign LP ops. Severity keeps
instant CUT for real rugs and keeps the position (or partial-derisks) otherwise.
**Change:**
- `sniper/analysis/exit_features.py` — `liquidity_removed_fraction` = |burn quote delta| /
  pre-burn quote reserve (burn row + same-tx `sol_sync` post-balances already carry both);
  route-degradation % from consecutive prewarm whole-bag quotes (already fetched every 3 s).
- `sniper/services/memecoin_exit_brain.py` — depth_pull → three-way: minor (below registered
  fraction, route intact) → no action, refresh route; material → DERISK; catastrophic (fraction
  above registered bound OR route gone / whole-bag impact blown) → CUT.
- Boundaries come from WO-7's replay over the recorded burn history — one measured registration,
  not invented round numbers.
**Tests:** small fee-claim burn → no CUT; large removal → CUT; mid removal + degraded route →
DERISK; route disappearance without decode → unsellable rail still acts (independent path).

### WO-4 — Typed quote-failure classification
**Why (performance):** provider outages must not condemn tokens (false unsellable-CUTs), and real
no-routes must keep counting (late-CUT = rug loss).
**Change:**
- Preview/quote seams (`memecoin_fastlane.py` / executor quote helpers) return a typed result
  (SUCCESS / NO_ROUTE / INSUFFICIENT_LIQUIDITY / TOKEN_REJECTED / SIMULATION_REVERT /
  RATE_LIMIT / RPC_TRANSPORT / PROVIDER_TIMEOUT / MALFORMED_RESPONSE) instead of bare None.
- `sniper/services/memecoin_real_runner.py` — only the four token-specific classes advance
  `exact_quote_misses`; infra classes advance a separate ops streak + alert. Defense in depth:
  all held positions failing in one sweep ⇒ infra, advance nothing.
**Tests:** simulated Jupiter outage (all positions fail, mids alive) → zero miss increments;
single-position NO_ROUTE streak → CUT-unsellable still fires at the registered count.

### WO-5 — Per-rule sizing + persisted controller state (depends on WO-1)
**Why (performance):** the biggest measured PnL lever. Translation-gates 07-16: TP cap sold
TrumpCoin for +$27 vs +$543 riding; conditioned-arm 07-15: timecap + tight stop lost −$921 vs
+$1739 on identical entries. Sizing by evidence and derisking on staleness (not age) directly
recovers that tail.
**Change:**
- `sniper/analysis/exit_flex_v5.py` — remove the strength-None → 0.5 midpoint. Each action sizes
  from its own inputs: DERISK from risk persistence + route degradation + remaining depth;
  TAKE_PROFIT from executable gain + drawdown-from-executable-peak + arrival exhaustion;
  time-DERISK from its own registered fraction. Final fraction =
  min(policy, max-fraction-inside-slippage-cap, remaining partial budget).
- Time-DERISK trigger: band age threshold AND no new executable high for the registered window —
  never age alone.
- Structure CUT: two decoded confirming events or the registered persistence window
  (catastrophic rules stay immediate).
- Controller state as columns on `memecoin_positions` (pattern exists: `derisked_at`,
  `exact_quote_misses`, `executable_peak_price`): `structure_break_since`,
  `consecutive_break_events`, `negative_flow_since`, `route_degradation_since`,
  `route_failure_streak`, `infra_failure_streak`. Survives restarts by construction.
  Brain index regen + schema validator after the migration.
- Sizing form (continuous vs 3-tier) and all constants: registered from WO-7 replay results.
**Tests:** healthy runner at age threshold with fresh executable high → no DERISK; one-tick break
without persistence → no CUT; sizing bounded by slippage cap; restart preserves hysteresis.

### WO-6 — Execution-binding audit (small, verification-first)
**Why (performance):** tail-risk only — prevents the rare stale-decision or double-sell execution.
**Change:** verify then extend the existing claim/attempt persistence with any missing bindings:
decision_state_id, position revision, balance snapshot, quote digest, min_output, blockhash
validity window. Before signing: re-read position, verify claim ownership + revision + balance,
reject superseded decision state. Rebroadcast only identical signed bytes while valid.
**Tests:** superseded decision → refused pre-sign; crash-after-broadcast → submitted_unknown
never re-sold (existing tests extended, not rewritten).

### WO-7 — Replay registration + acceptance gate (REVISED 2026-07-25 after DB audit)
**Why:** the single validation step and the source of every registered constant — so it must
survive contact with the DB, and the first draft didn't. Verified facts that forced this
revision: `data/memecoin_sol_tape.db` is **empty** (0 rows every table — the solana structural
tape is what WO-2 creates, forward); the only tick tape is 4663 (5.87M rows);
`memecoin_position_marks.executable_exit_value` **never diverges from mid** across 1,241,441
rows (all dexscreener/geckoterminal/zerox feeds — "executable" in the marks is a copy of mid);
only 36 closed real solana positions exist. The v1 draft also fit WO-3/WO-5 constants on the
same tape it gated on — a gate that structurally could not fail.
**Change:**
- **Fill model, not mid marks.** Replay values every simulated exit through the executor's own
  fill model (the live slippage ladder + whole-bag impact cap), calibrated against the real
  closes (36 solana + the 4663 fills). Until the fill model is calibrated, the replay metric is
  named **net mid exit value** and CANNOT arm a chain — mid-only greens are not deploy evidence.
- **OOS split.** WO-3 boundaries and WO-5 sizing/persistence constants are fit on the
  earliest-in-time slice of the tape; acceptance is judged ONLY on the held-out later slice.
  Time-ordered split, never random.
- **Per-chain evidence floors.** A chain's gate can only run on that chain's own decoded
  structural tape: ≥14 position-days of held-pool tape, ≥20 closed positions overlapping the
  tape window, and ≥10 decoded LP-removal events for the WO-3 boundary fit. Below the burn
  floor, WO-3 severity stays at its conservative degenerate setting on that chain (every
  decoded burn = catastrophic → CUT, i.e. today's behavior) until the evidence accrues.
- **Solana tape comes from WO-2 running live.** After WO-2 ships, the recorder accumulates
  while the runner trades under the existing rails — data collection on real fills, not a
  paper arm. 4663's gate can run as soon as WO-9/WO-10 land (its tape already exists) and may
  pass BEFORE solana's.
- Mechanics unchanged: old-SHA vs new-SHA from isolated worktrees, identical inputs, inventory
  tracked per arm; outputs = net fill-modeled exit value per episode, catastrophic-loss rate,
  false/late CUT rate, upside retained after partials.
- **Acceptance bar (per chain):** on that chain's held-out slice, new SHA ≥ old SHA on net
  fill-modeled exit value, no regression in catastrophic-loss rate, evidence floors met, all
  touched tests green, schema validator clean.

### WO-9 — EVM hot exit lane (4663 + 8453)
**Why (performance):** no EVM chain gets armed on the 30 s pull cadence — a rug resolves in
seconds. This delivers block-cadence decisions on Robinhood/Base with the code that already
exists on both ends.
**Facts that shape it:** `decode_evm_log` (`memecoin_tick_stream.py`) already normalizes
V2/V3 Swap, V2 Sync/Mint/Burn — and every EVM row is natively transaction-grounded
(tx hash + log index), so WO-2's Solana corroboration invariant does not apply here; decoded
EVM rows may authorize sales directly. The engine side (`HotPositionEngine`, `HotEngineSet`,
`HotSupervisor`, `PrewarmedExits`, `DecisionGapTracker`) is chain-agnostic already.
**Change:**
- **Transport probe first, recorded in the WO evidence:** try `eth_subscribe("logs",
  {address: [held pools]})` over WSS on the 4663 and 8453 RPCs; measure block cadence and
  notification latency. If no WSS: bounded per-block poll — `eth_blockNumber` (or
  `eth_subscribe newHeads`) ~1 s; on a new block, ONE `eth_getLogs` scoped to held funded
  pools only (≤7 positions by the concurrency bound). At ~1-2 s EVM block times, per-block
  polling IS hot cadence.
- New thin feeder (`sniper/services/memecoin_evm_hot.py`): raw log → `decode_evm_log` (reused,
  not reimplemented) → `supervisor.on_ws_row`-equivalent per (chain_id, pool). Stamp
  `block_ts` from the block header when cheap, else corrected receive time (Solana-lane
  precedent).
- Pull materializer stays verifier + fail-closed fallback; same 60 s decision-gap bound; same
  prewarm loop (preview fn is already chain-generic in the runner).
- Scope: held funded pools only. The unfiltered observation tape stays the tick-stream
  worker's job — this lane never widens into it.
**Tests:** fixture logs → hot decision parity with the pull lane on identical rows; WS/poll
loss → pull fallback engages inside the gap bound; latency instrumentation populated.
**Acceptance:** measured decision p50 on 4663 ≤ ~2× block cadence; parity replay clean.

### WO-10 — Uniswap-V3 pool depth: LP-pull CUT on the Robinhood route
**Naming note:** "V3" here means the **Uniswap V3 AMM protocol** the 4663 pools run
on-chain (`v3_swap`/`v3_burn` are chain event kinds). It is unrelated to the retired
exit-engine v3 — the decision engine is v5-only everywhere in this plan.
**Why (performance):** the 4663 funded path is V3, and today V3 has no depth family — the
burn-CUT rug rule is structurally absent exactly where the funded EVM money is. V3 pools DO
emit the events; our decoder just skips them.
**Facts that shape it:** Uniswap-V3-style pools emit `Mint`/`Burn`/`Collect`, and every V3
`Swap` carries post-swap **in-range liquidity** in the data words `decode_evm_log` already
receives (it currently reads only amount0/amount1). Fee claims are a separate `Collect` event
on V3 — cleaner than Solana, where fee claims masquerade as burns.
**Change:**
- `memecoin_tick_stream.decode_evm_log`: add V3 `Mint`/`Burn`/`Collect` decoding. Derive each
  topic0 by keccak from the canonical event signature AND verify against live 4663/8453 logs
  before trusting (repo rule: fixtures encode the bug — decoders are live-verified, three
  cases 07-24). Extend `v3_swap` handling to also emit a companion `v3_liq` row carrying
  post-swap in-range liquidity in `amount0` — the exact analog of `v2_sync`/`sol_sync`, so
  **no pool_ticks schema migration** (`kind` is free TEXT; regen brain index after).
- Semantics: `v3_collect` is a fee claim and must NEVER set depth-pull. `v3_burn` severity =
  burned in-range liquidity vs current in-range liquidity (latest `v3_liq`); out-of-range
  burns are discounted — they do not change executable depth. WO-3's route-degradation
  cross-check (prewarm quotes) applies unchanged.
- `exit_features.profile_capability`: V3 gains depth (supported + required) per chain, gated
  on the decoder being live-verified for that chain; feature version bump.
- `exit_features` / market state: V3 depth series from `v3_liq`; `depth_pull_flag` from
  `v3_burn` through the WO-3 severity classifier.
**Tests:** live-captured 4663 fixtures for all three topics; Collect → no depth-pull;
in-range vs out-of-range burn severity; capability flips only with the verified decoder;
V2-pool behavior unchanged.

### WO-8 — Wire-in + funded deploy (REVISED 2026-07-25: per-chain arming)
0. Preconditions per chain: the chain's own WO-7 gate passed on its own decoded tape with the
   fill model and OOS split; for EVM chains additionally WO-9 + WO-10 complete. No chain is
   armed on the 30 s pull cadence alone, and no chain is armed on a mid-only or in-sample
   green. The v1 premise "solana first — it has the decoded tape" was **wrong** (tape was
   empty); arming order is now whatever chain meets its evidence floor first — plausibly 4663
   before solana.
1. `.env`: `MEMECOIN_HOT_EXIT_ENABLED=1`; add each chain to `MEMECOIN_ENGINE_AUTHORITATIVE_CHAINS` as its
   gate passes (8453 joins the same way once its bankroll is funded).
2. New entries reserve engine-envelope-v1 contracts frozen to v5; existing open positions keep
   their frozen contracts by construction (immutability, not a canary).
3. Deploy: `scripts/deploy_memecoin_runtime.sh <merged-sha>` — built-in backup + reconcile + gates
   run as part of the command. Verify: runner heartbeat fresh + auth_ok, first engine-envelope
   contract row frozen v5, hot-lane decision latency p50 logged.

## Explicitly out

- No forward-paper arm, no canary cohort, no staged promotion (operator decision above).
- No observation-only shadow features (episode/regime/deployer stay never-read-by-rules).
- No five-quote sellability ladder; whole-bag prewarm + exact action quote at execution.
- No soft freshness decay; no round-number rewrites of measured constants.
- No multiple partial tranches until a later replay proves them.
- Token-authority CUT rule deferred: no historical evidence base to register a trigger from, and
  a wrong trigger is a false full-exit. Collect the events first; promote in a later cut.
