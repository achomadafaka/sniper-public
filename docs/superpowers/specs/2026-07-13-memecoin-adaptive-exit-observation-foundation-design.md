# Memecoin adaptive-exit engine — Sub-project #1: Observation + Replay Foundation (design)

Status: **design, for review** · Branch: `worktree-memecoin-real-shadow-featurestore` (off origin/main @ 3c7a6e5f) · Date: 2026-07-13
Deploy posture: **everything in this sub-project is log-only / zero-behaviour-change.** No adaptive rule, no controller
action, no real-money behaviour change ships here. Those are later sub-projects (each its own spec), each gated on this one.

---

## 0. Framing (read before scoping anything)

We want to replace the fixed exit rails (one `+60%` TP / `−50%` stop / one gate stack for every trade) with a per-entry
and during-position **adaptive** policy. Before building, two things must temper expectations — both from our own brain,
neither derivable by an outside model:

- **`finding-memecoin-beat-trader-counterfactual-2026-07-11` (n=211):** the *strongest form* of the co-holder / during
  idea — mirroring the source wallet's first sell — **loses ~$1k vs the dumb fixed rails.** The controller is therefore
  the *least* promising lever, and its authority must be earned, not assumed.
- **`finding-memecoin-signal-conditioning-2026-07-11`:** the only signal-time conditioning feature that survives
  per-token dedup is **market cap** (consensus is a queued second). Everything else was pseudo-replication.

Honest prior: **the fixed rails are close to locally optimal; the adaptive engine's EV is concentrated in one or two
coarse mcap/consensus branches; the observation layer is worth more than the controller.** Design small.

Five independent model lineages (GPT-Pro, DeepSeek, Fable, Sonnet, the main loop) converged on the same spine. Treat that
convergence as a **sanity cross-check, not evidence** — correlated models share training priors, and this sentence must not
later be quoted to justify skipping validation. The evidence base is the two brain findings above + forward-paper results;
model agreement only lowered the odds we designed something obviously wrong.

## 1. Design principles & refusals (non-negotiable, all five lineages agreed)

1. **No learned model touches parameters or real money at this data depth** (~198 tokens with price paths, ~6 days,
   ~4,200 feed events). No online RL, no bandit, no per-token model, no regression-fit continuous rail curve. Frozen,
   interpretable templates only. A learned *hazard* model is a Phase-7 north star (≥1000 episodes), never a black-box
   end-to-end policy.
2. **Monotonic risk-reduction only.** After entry, the (future) controller may tighten stop, lower TP, de-risk, trail, or
   exit. It may **never** widen the stop, raise TP, add size, reset peak, or re-enter. This bounds the worst case of any
   adaptive action at the fixed-rails worst case, which is what makes later increments shippable.
3. **The experimental unit is the token EPISODE, not the signal / fill / observation.** Dedup is structural (enforced in
   the harness API), not a discipline someone remembers. Prevents pseudo-replication at the definition level.
4. **One code path** powers historical replay, forward-paper, and live. No separate backtest reimplementation — that is
   the most common source of false-confidence results.
5. **Executable sell value, never chart price**, for every rail and every label. A failed sell route is a recorded loss
   (`EXIT_ROUTE_FAILED` → outcome ≈ −100%), never a theoretical exit.
6. **Chain-specific everything** (calibration, graduation, execution model). Never pool Solana and EVM into one verdict.
7. **The backtest is pruning-only.** History is too thin/drifted to *tune* from; it kills broken shapes. Promotion
   evidence is forward-paper only, recency-dominated.

## 2. Scope

**This sub-project (#1) builds the foundation and nothing that acts:**
- The canonical signal + episode + observation contract.
- Immutable pre-gate entry snapshots + the entry-latency chain.
- Position marks (persist the quote `sweep_exits` already computes and discards).
- Confirmation-state on every on-chain event.
- `ExitPolicy` objectification — the current rails expressed as data, reproduced byte-for-byte.
- The deterministic, stateful replay/parallel-arm harness with its acceptance tests.

**Deferred to later sub-projects (named here so scope stays honest; each gets its own spec):**
- SP#2 — co-holder entity graph + regime + token-facts logging (observe-only).
- SP#3 — frozen adaptive exit templates (`P0_FIXED`/`P1_FAST_DERISK`/`P2_TREND_RIDE`/`P3_LIQUIDITY_DEFENSIVE`) as a paper arm.
- SP#4 — during-position controller (risk-reducing only), counterfactual-first.
- SP#5 — adaptive **sizing** (mcap+consensus) — likely the highest-EV *acting* lever; no fill-path dependence.

## 3. Architecture

Grounded in the current runtime: the copy loop in `sniper/services/memecoin_runner.py` (`run_once` → `copy_event_to_arms`),
the exit sweep `sweep_exits` in `sniper/services/memecoin_exits.py`, the real leg `sniper/services/memecoin_real_leg.py`,
persistence in `sniper/persistence/db_memecoin.py`, arm config `MEMECOIN_ARMS` in `sniper/settings/memecoin.py`, and the
existing feed tables `memecoin_events` / `memecoin_firehose` / `memecoin_fills` / `memecoin_positions` /
`memecoin_exit_targets`.

**Four of the six components are logging. That ratio is deliberate** (§0).

### A. `SignalNormalizer` — `sniper/services/memecoin_signal.py`
Converts Solana + EVM feed events into one canonical `CopySignal`: `chain_id, token_address, source_wallet,
source_entity_cluster, source_tx, source_slot_or_block, source_event_ts, received_ts, confirmation_state, token_amount,
notional_estimate, source_entry_price`. Generalises the existing normalisation already implicit in `memecoin_events`.
**`confirmation_state` is first-class** — Solana `processed`|`confirmed`|`finalized` (processed can roll back) and EVM
`observed`|`included`|`safe`|`finalized`|`reorged` are NOT equivalent; nothing downstream may act on an unfinalized
holder/exit event.

### B. `TokenEpisodeResolver` — `sniper/services/memecoin_episode.py`
The first eligible trusted-wallet BUY on a `(chain_id, token_address)` **opens an episode**; later trusted signals on the
same token while any arm is open **update episode context (consensus) but create no new experimental sample**. Episode ends
when all arms close or a max horizon expires. This is the structural dedup, and it composes with our existing
one-position-per-coin rule (`finding-memecoin-reentry-signal-study-2026-07-09`). The resolver **consumes
`memecoin_feed_observations_v2`** for signals and does **not** duplicate the forward-outcome pipeline —
`memecoin_signal_outcomes_v2` / `memecoin_horizon_marks_v2` remain the horizon-mark layer; the episode is a position-arm
grouping over them, not a second outcome tracker.

### C. `EntryObservationBuilder` — `sniper/services/memecoin_features.py`
Called from the copy hook **before entry gates run**, so *rejected* signals get a snapshot too (generalises the existing
`wouldbe_price`/`chase_gap_pct` counterfactual columns on `memecoin_events`). Writes an immutable feature snapshot keyed to
the episode. Every field carries `source_ts, received_ts, quality, source`. **No feature observed after the decision may
enter the snapshot.** Also records the **entry-latency chain** (source-submitted → source-included → detected → quoted →
submitted → included) so a bad outcome caused by copying *late* is never misattributed to exit policy.

### D. Position marks — persisted in `sweep_exits`
`sweep_exits` already quotes every open position every sweep and throws the quote away after trigger checks. **Persist it**:
one shared mark per `(mint, sweep)` fanned to all arms (a single quote stream — do not create per-arm-fill confounders; the
2026-07-07 review already caught that once). This table is the replay harness's input: real price/liquidity paths recorded
under real observation latency. Retention: never prune (memecoin no-prune rule); ~1 row/position/sweep is small.

### E. `ExitPolicy` object + resolver — `sniper/services/memecoin_policy.py`
`ExitPolicy` dataclass = the current rails made into data: `tp_pct, stop_pct, stop_debounce_sweeps, derisk_trigger_pct,
derisk_fraction, trail_mode, max_hold_s, mirror_sell_mode, chain_overrides`. `resolve_policy(snapshot) -> (ExitPolicy,
rule_id)` is pure/deterministic/versioned. **v1 resolver returns the current fixed rails for every input** — zero
behaviour change; the acceptance test (§7) proves it byte-for-byte. Every position (paper and real) gets its policy stamped
at entry, extending the audit-table discipline already used for wallet arming.

### F. `ReplayHarness` — `sniper/replay/memecoin.py`
Consumes the same event schema as the live observer and drives A0 (fixed control) plus (later) adaptive arms over recorded
marks. **Stateful per arm** — a de-risk changes later size/stop, so the harness simulates each arm's own state path, not
just exit levels applied to a price path. Fills at the **next** mark after a trigger, **minus a p75 slippage haircut
calibrated from `memecoin_fills`** realized fill-vs-quote gap. Route-unavailable → `EXIT_ROUTE_FAILED` (loss), never a
theoretical exit.

## 4. Data model

New tables (append-only / versioned), created via `ensure_memecoin_tables` in `db_memecoin.py` so the brain index picks
them up. They read from / attach to existing tables; none mutate an existing table.

<!-- schema:external=memecoin_token_episodes -->
<!-- schema:external=memecoin_entry_snapshots -->
<!-- schema:external=memecoin_position_marks -->
<!-- schema:external=memecoin_policy_versions -->
<!-- schema:external=memecoin_policy_assignments -->
<!-- schema:external=memecoin_arm_outcomes -->

| Table | Grain | Purpose |
|---|---|---|
| `memecoin_token_episodes` | episode_id | The experimental unit. `chain_id, token_address, started_ts, ended_ts, first_signal_id, consensus_cluster_count, status`. |
| `memecoin_entry_snapshots` | `upstream_event_id` (UNIQUE), nullable `episode_id` | Immutable pre-gate features for **every eligible signal, including gate-REJECTED ones** (a rejected signal has no episode → `episode_id` NULL, `action` distinguishes `opened`/`gate_rejected`/`route_failed`/`data_unavailable`). Promoted trusted columns (`mcap, liquidity, token_age_s, consensus_n_60m, wallet_trust_tier, their_size_usd, regime_id, chain, action`) + `features_json`, `feature_version`, `data_quality_json`, `entry_latency_json`, `snapshot_hash`. A feature graduates JSON→column only after a dedup-corrected study. |
| `memecoin_position_marks` | `(chain_id, mint, sweep_ts)` | ONE shared mark per (chain, mint, sweep), joined to positions — cross-arm mark identity is **structural**, not a discipline (the 2026-07-07 per-arm-fill confounder lesson). `mid, executable_exit_value, liquidity, mcap, route_quote_id, source, quality`. Never pruned. |
| `memecoin_policy_versions` | (policy_id, version) | Immutable frozen policy: `template, parameters_json, code_commit, immutable_hash, activated_ts, retired_ts`. |
| `memecoin_policy_assignments` | (episode_id, arm_id) | Which policy each arm's position ran, + `entry_snapshot_id, assignment_reason, assigned_ts`. Exact attribution. |
| `memecoin_arm_outcomes` | (episode_id, arm_id) | Token-level result: `entry_cost, net_exit_value, net_pnl, net_return, max_executable_return, max_drawdown, exit_reason, route_failures, closed_ts`. Net of gas/priority. |

Existing tables reused unchanged: `memecoin_events`/`memecoin_firehose` (signals), `memecoin_fills` (slippage calibration),
`memecoin_positions` (live positions; the replay acceptance test reconciles against its `realized_pnl`),
`memecoin_exit_targets` (the per-position mcap-band TP already shipped — SP#3 extends it from `actor='operator'` to
`actor='policy'`).

## 5. Data flow

```
trusted BUY (memecoin_events) → SignalNormalizer(+confirmation_state) → TokenEpisodeResolver
  → EntryObservationBuilder (pre-gate, immutable snapshot + latency chain)
  → existing gate chain (unchanged) → copy_event_to_arms opens positions per arm
  → sweep_exits every sweep: write memecoin_position_marks (shared) + existing trigger checks (unchanged)
  → position closes → memecoin_arm_outcomes (token-level, executable value, net of fees)
ReplayHarness reads {snapshots, marks, fills} through the SAME schema → drives A0 and (later) adaptive arms
```

## 6. Evaluation harness foundation (primitives built here, consumed by SP#3–5)

- **Unit = token episode.** `dedup_to_episode_outcomes()` is a shared, non-optional primitive the harness API enforces.
- **Primary statistic = paired per-episode PnL difference** `d_i = net_pnl_adaptive_i − net_pnl_fixed_i` on identical
  entries at equal notional. Pairing removes the dominant variance (which coins got signalled) — the biggest asset at low n.
- **Recency-weighted, but sized by effective sample, not a fixed half-life.** Exponential decay has an effective sample
  size `ESS ≈ 2.9 × episodes_per_day × half_life_days`. A fixed 48h half-life gives Solana (~5–8/day) ESS≈30–45 but 4663
  (~1–2/day) ESS≈6–12 — a 90% lower CB can *never* clear zero there, so the bar is unpassable by construction. Instead the
  half-life is **chosen per chain to hit an ESS floor** (Solana lands ~2–4d, EVM likely ~7d+). Report **2/4/7d** sensitivity.
  Honest consequence: **EVM graduation is inherently slower and on staler data** — accept it rather than fake it with a
  half-life that yields no power. Plus a **6-hour block bootstrap** (one meta-wave pumps many coins in the same hours) and a
  **sign-flip permutation test** on the paired diffs (fat-tail-tolerant).
- **Graduation bar (pre-registered — pinned before the arm is ENABLED, not before this spec merges; per-chain, no pooling):**
  `ESS ≥ 30` under the chosen half-life AND the most recent block non-losing; 90% lower CB of the weighted paired delta > 0;
  **leave-one-out robustness — the weighted paired delta stays > 0 after dropping the best TWO pairs** (guards one-lucky-rug
  without penalizing the tail-capture the validated mcap effect *is*: 22/53 hit20 means the money lives in a few runners);
  catastrophic-loss rate not higher than fixed; top-decile upside capture ≥80% of control; survives the 2/4/7d half-life
  sensitivity. **Paired win-rate is reported but is NOT a gate** — this program is PnL-first, and a legitimately +EV
  trend-ride template ties fixed on most episodes and wins big on the few runners, so it can be clearly profitable and fail
  a 55%-win-rate clause forever. Then a 10% canary + ≥50 more episodes before broader rollout, behind an auto-demote kill
  switch mirroring the wallet auto-demote.
- These primitives ship as code + tests in SP#1; no arm is *graded* until SP#3 produces one.

## 7. Correctness, error handling, testing

**Acceptance tests (a component is not done until these pass):**
1. **`ExitPolicy` reproduces current rails byte-for-byte** — for a corpus of recent closed positions, `resolve_policy` +
   the objectified rail evaluation produces the identical trigger sequence and exit the live code produced. Zero behaviour
   change is *proven*, not asserted.
2. **Replay reproduces reality** — replaying each closed position's *actual* policy over its recorded marks reproduces its
   `memecoin_positions.realized_pnl` within tolerance (after the p75 haircut). If it can't, the harness is lying and is
   blocked.
3. **Structural dedup** — the harness API rejects signal-grain rows; only episode-grain enters a statistic.

**Correctness guards baked in now (cheap while volume is small):**
- Every feature carries an explicit MISSING state; policy MISSING-behaviour = **fall back to fixed rails**, never a default
  numeric (repeats-of the mcap fail-closed bug are the enemy).
- Every on-chain event carries `confirmation_state`; unfinalized events never drive a real action.
- **Exclude our own flow** from velocity/exodus features (our buys inflate the velocity we measure; our exits *are* the
  exodus signal on microcaps).
- **Survivorship:** every *eligible* signal gets an outcome row (entered / gate-rejected / route-failed / data-unavailable
  / token-disappeared), and unquotable death marks to ≈−100%. Never compute performance on clean price paths only.
- **Deterministic-exit MEV note (for the future execution layer, not this sub-project):** live stops must eventually
  randomise the trigger within a ±0.5% band + a small block delay, in execution, not policy — recorded here so it isn't
  forgotten.
- Rug defense stays at the entry gate: rugs resolve inside poll latency; no controller catches them. "Adaptive rug
  detection" is out of scope by construction.

## 8. Out of scope (SP#1)
Any adaptive rule or template; any controller action; any co-holder-driven behaviour; any real-money behaviour change; any
learned model; sizing changes; changes to the exit-rail *values*. SP#1 only records, objectifies, and replays.

## 9. Build order (SP#1)
1. New tables via `ensure_memecoin_tables` + brain-index regen + schema-validator green.
2. `SignalNormalizer` + `TokenEpisodeResolver` + `confirmation_state` plumbing.
3. `EntryObservationBuilder` (pre-gate, immutable, latency chain) — write log-only from `run_once`.
4. Position marks persisted in `sweep_exits` (shared mark, never prune).
5. `ExitPolicy` object + resolver v1 (fixed rails) + acceptance test #1.
6. `ReplayHarness` (stateful, executable value, p75 haircut, route-failure) + acceptance tests #2/#3.
7. Evaluation primitives (`dedup_to_episode_outcomes`, paired-diff, block bootstrap, permutation, graduation-bar checker) + unit tests.

## 10. Decisions carried into later sub-projects
- Controller first *action* = partial de-risk (never full-exit); co-holder authority earned only after ≥100 validated
  episodes + the paired bar, and requires a confirming price move (never the graph alone) — the n=211 counterfactual is the
  reason.
- First adaptive policy shape = GPT's two-score (`fragility`, `momentum_quality`) → four frozen templates, with **v1
  authority restricted**: (a) the v1 selector branches **only on `fragility`** (≈ mcap + token-safety, the sole
  dedup-surviving axis); `momentum_quality` is computed and logged from day one but earns selector authority only by passing
  the same dedup-corrected study mcap passed; (b) v1 activates **two templates max** (`P0_FIXED` + one challenger) — `P2`/`P3`
  stay defined-but-dormant, because four live templates × 4+ rail params blows the ~6–8 free-number budget; (c) all templates
  share the debounce/guard machinery so the only free numbers are rail values.
- **Sizing (SP#5) may run BEFORE the controller (SP#4)** — it is likely the highest-EV acting lever, has no fill-path/replay
  dependence, and A/Bs cleanly. Numbering is dependency order, not priority; sizing should be sequenced early.
- Consensus is suspect until an independence check (are "N trusted buyers" one viral call seen N times?) clears it — this
  gates consensus as both a selector input and a sizing multiplier.
