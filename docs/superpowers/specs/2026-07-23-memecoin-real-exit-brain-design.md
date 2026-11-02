# Memecoin real exit brain — design

**Date:** 2026-07-23 · **Status:** design, for review · **Branch:** `worktree-memecoin-real-exit-brain`
**Deploy posture:** everything here is **log-only / zero funded-behaviour change**. No rail, env, DB-production, or runner
behaviour ships from this spec. Funded action is gated behind an operator checkpoint (Gate 3) that this spec only *proposes*.

**Companion evidence:** `docs/audits/2026-07-23-memecoin-real-exit-brain-phase0-evidence-audit.md` (Phase 0). Read it first;
every scope choice here traces to a tagged finding there.

---

## 1. Framing & inherited doctrine

Goal: an explainable, causal, live market-state engine that can *eventually* replace fixed-threshold funded exits — earned,
not assumed. It is a **hypothesis** until it passes the gates in §8.

**Binding retraction** (Phase 0 §1): `memecoin_position_marks.executable_exit_value` is an alias `== mid`; **no executable
tape exists**. No study, default, or funded setting in this project may treat mid-plus-haircut as executable, or cite the
retracted "+$14 / +$85–92" numbers. The structure counterfactual's higher-high claim is unimplemented and void.

**Inherited refusals** (from `2026-07-13-memecoin-adaptive-exit-observation-foundation-design.md`, still binding):
1. No learned model touches parameters or real money at this data depth. Frozen, interpretable templates only.
2. **Monotonic risk-reduction only.** The candidate may tighten stop / lower TP / de-risk / trail / exit — never widen a
   stop, raise TP, add size, reset peak, or re-enter. Worst case bounded by the fixed-rails worst case.
3. **Experimental unit = the token EPISODE** (one position on one mint), enforced in the harness API, not by discipline.
4. **One code path** for replay, forward, and live. No separate backtest reimplementation.
5. **Executable sell value, never chart price**, for every real exit. (The alias silently violated this — the whole point.)
6. **Chain-specific everything.** Never pool Solana and EVM into one verdict.
7. **Backtest is pruning-only.** History kills broken shapes; it never tunes. Promotion evidence is forward + canary.

**Honest prior:** the fixed rails are close to locally optimal; the observation layer is worth more than the controller;
0/46 traders beat luck; 4/10 stopped funded positions never had a profitable exit at any rail. Design small.

## 2. Scope & non-goals

**In scope (this spec):** the versioned `MarketState` contract; the pure feature library; the block-time source; the
standalone-process materializer; the compact snapshot store; the candidate decision state machine (pure, proposes only); the
zero-authority shadow logger; deterministic replay + the pre-registration/sealed-read tooling; a minimal read-only
observability endpoint; and the **funded-canary proposal package** (drafted, not activated).

**Explicitly NOT in scope:** any funded behaviour change; enabling the fast lane; a new paid RPC provider; Solana holder
observations; the distribution-state detector (§10, future spec only); the full day-trading cockpit / order UI; unique-buyer
/ holder-concentration / V3-LP-pull features (unsupported — Phase 0 §3–4).

## 3. Data-truth envelope (the honest subset)

### 3.1 Eligibility matrix (operator-settled)
A position's MarketState is **candidate-capable** only if every **required** field *for its chain/pool profile* is trusted;
otherwise the engine emits a non-actionable state and the shadow falls back to legacy.

| profile | flow | realized vol | price/structure | depth | verdict |
|---|---|---|---|---|---|
| **4663 V2** | signed net flow (pool-level) | from V2 price path | V2 execution price | **V2 `Sync` reserves** | **candidate-capable** when flow+vol+depth trusted |
| **4663 V3** | signed net flow (pool-level) | from V3 price path | **V3 execution price** `|Δquote/Δbase|` (spec-defined, §3.3) | **unavailable** | **candidate-capable for flow/vol/structure**; depth field forced unavailable |
| **Solana** | unavailable | unavailable | unavailable | unavailable | **forced legacy.** Even arrival is unavailable today: Solana rows carry no amounts and their slots are absent from the EVM-only `block_times` map, so a trustworthy arrival rate needs slot→time data that does not exist yet. `SOURCE_UNTRUSTED`; add arrival only once a Solana slot→time source lands. |
| **8453 / other EVM** | as V2/V3 once tape exists | — | — | V2 only | inherits the EVM rules; no tape today |

### 3.2 Disabled features (unsupported — never proxied)
- **Unique buyers / holder concentration** — `maker = topics[1]` is the router, not the user (Phase 0 §3). Disabled. The
  only actor feature permitted is `top_actor_concentration` (routers/senders), and it is **`SOURCE_UNTRUSTED` by default**.
- **Actor-based wash filtering** — the maker both-sides exclusion is removed; **dust-only** filter remains. Flow is
  pool-level signed net flow; provenance records `wash_filter=disabled_router_ambiguity`.
- **V3 LP pulls** — no liquidity-change events decoded. Disabled.
- **Solana flow/depth/price** — all unavailable. Arrival too, pending a trustworthy slot→time source (slots are not
  in the block-time map; ingest time is banned for windows by §3.5). Solana stays forced-legacy `SOURCE_UNTRUSTED`.

### 3.3 Trusted V3 price/vol definition (spec-defined per the matrix)
V3 lacks a decoded mid, but the **stored signed amounts** yield a legitimate price series: per swap,
`exec_price = |amount_quote| / |amount_base|` (both already persisted). Aggregated **size-weighted per block**, this is the
realized **execution-price** path — sufficient for structure (higher-high/lower-low) and realized volatility. It is **not a
mid** (large prints carry impact); provenance flags `price_kind=exec_price_v3` and features carry a `price_bias=exec` marker.
The executable *exit* price remains the fast lane's job (§3.4) — never this. (Extension v1.1, out of scope: decode
`sqrtPriceX96` for a true V3 mid.)

### 3.4 Price honesty (three distinct things, never merged)
- **nominal price** = pool/quote mid (marks) or the §3.3 exec-price path (ticks). For structure/vol/display.
- **executable price** = fast-lane whole-bag route quote (`preview_exit_fn().expected_usdc / open_qty`). **None until the
  fast lane is enabled under an approved provider.** The only price a real exit may act on.
- **actual fills** = `memecoin_fills`. Ground truth for realized outcomes.

### 3.5 Time (block-time, not ingest-time)
`observed_at` is ingest time (Phase 0 §2c). All time-based features window on **event time** from an immutable
`block_times(chain_id, block_no, block_ts)` map, backfilled via bounded/batched `eth_getBlockByNumber` on the **existing
ingest RPC**, forward/live only (no historical tape overlaps the cohort — Phase 0 §6), each block fetched **once**, cached
forever. **Fail-closed:** a window whose blocks lack timestamps is `DATA_INSUFFICIENT` — never converted with an assumed
block-time constant. Windows are defined in **seconds of block_ts**, so catch-up ingest stamping cannot poison them.

## 4. Phase 1 — the `MarketState` contract

A typed, versioned, immutable value object per open position per materialization tick. Pure dataclass; no DB/network.

**Identity & versioning:** `chain_id, mint, pool, position_id, entry_contract_version, engine_version, feature_version`.
**Provenance (every feature family carries):** `source`, `event_time_window (block_ts range)`, `freshness_s`, `coverage`
(fraction of the window with data), `quality` ∈ {trusted, degraded, untrusted}, `feature_version`.

**Families (each optional; `None` = not available, distinct from `0.0`):**
- **time:** `source_event_time, observed_time, computed_time, age_s, freshness_state`.
- **price:** `nominal_price, executable_price(None until fast lane), entry_price, nominal_peak, executable_peak,
  drawdown_from_exec_peak, drawdown_velocity` (per block_ts second; irregular Δt handled).
- **structure (causal, no lookahead):** `confirmed_pivots` (swing highs/lows from *closed* windows only),
  `prior_high, prior_low, higher_low_state, lower_high_state, confirmation_delay_blocks`.
- **volatility:** `realized_vol[window]` over pre-registered block-time windows.
- **flow (EVM, trusted-only):** `buy_n, sell_n, buy_vol_quote, sell_vol_quote, net_flow_quote, arrival_rate,
  arrival_accel`; `wash_filter=disabled_router_ambiguity`, `rejected_dust_share`.
- **depth (V2 only):** `reserve_quote, reserve_delta, depth_pull_flag`.
- **actor (untrusted):** `top_actor_concentration` — populated but `quality=untrusted`; the engine must not gate on it.
- **explicit states:** `WARMUP` (insufficient closed windows), `DATA_INSUFFICIENT`, `DATA_STALE` (freshness > bound),
  `SOURCE_UNTRUSTED` (profile not candidate-capable, e.g. Solana / 4663-V3-depth).

Every field must be explainable from provenance alone: source, event-time window, freshness, coverage, quality, version.

## 5. Phase 2 — architecture (1A + 2A, operator-approved)

Five components + the block-time source. The **runner never scans the firehose**; it reads a compact snapshot.

1. **Pure feature library** — `sniper/analysis/exit_features.py`. Deterministic `compute_state(swaps, block_times,
   position_contract, cfg) -> MarketState`. No DB/network. Reused verbatim by the materializer *and* replay (refusal #4).
2. **Block-time source** — `block_times` in the ticks DB, backfilled by the standalone exit-state worker (§3.5, §5.3) — only the headers open-position windows need, bounded and cached. Immutable, cached,
   fail-closed.
3. **Materializer (1A)** — a **standalone worker process** (its own entrypoint, not a thread inside
   `scripts/memecoin_runner_loop.py`), supervised by its own watchdog, env-gated `MEMECOIN_EXIT_STATE_ENABLED`
   **default OFF**, with its own ticks+ledger connections. **Rationale (settled):** feature/RPC/SQLite faults must
   NOT share the funded runner's failure domain — an out-of-process worker cannot stall, deadlock, or OOM the money
   path; the runner keeps trading if the materializer dies, and the materializer restarts independently. Per tick: for
   each **open real position** (bounded by `MEMECOIN_MAX_CONCURRENT`), read its pool's `pool_ticks` in the block-time
   window (2A: direct from ticks), call `compute_state`, write **one versioned snapshot** to
   `memecoin_exit_market_state`. Exposes freshness/health. Read-only to the money path; a crash cannot touch rails.
4. **Candidate decision engine** — `sniper/services/memecoin_exit_brain.py`. Pure
   `propose(position_contract, market_state, cfg) -> CandidateDecision` (action + full reason object). **Never submits.**
5. **Shadow adapter** — logs candidate vs legacy decision + divergence to `memecoin_exit_candidate_decisions`. **Zero
   authority:** it cannot nominate, size, sign, approve, broadcast, or mutate any position/reservation/rail. It runs beside
   the legacy engine; the legacy engine and the frozen contract stay authoritative.
6. **Replay + sealed-read tooling** — `scripts/memecoin_exit_brain_replay.py` (same `compute_state`+`propose`), records the
   exact price-series limitation for each position (mid-only for history; executable=None).

No new bus. The materializer is a **separate process** (own entrypoint + watchdog) — deliberately NOT a runner
thread — so its failure domain is isolated from the funded runner (settled 2026-07-23). Every other memecoin worker
is a runner thread; this one is not, precisely because it does feature/RPC/SQLite work that must never stall rails.

## 6. Phase 3 — candidate decision engine v1

An interpretable state machine, not a learned model:

```
WARMUP → TRENDING → WEAKENING → BREAK_NOMINATED → BREAK_CONFIRMED → EXIT_PROPOSED
        (plus non-actionable: DATA_INSUFFICIENT | DATA_STALE | SOURCE_UNTRUSTED → legacy fallback)
```

- **Structure nominates weakness** (a break of the most recent *confirmed* swing low; no future bars).
- **Verified flow confirms or rejects** the break (signed net-flow sign/magnitude over a pre-registered window).
- **Realized volatility scales** structure tolerances / trailing floors within **hard bounds** — vol widens/tightens, never
  removes, a guard.
- **Only the candidate catastrophe guard is absolute.**
- **Monotonic risk-reduction only** (refusal #2). During shadow the legacy engine + frozen contract remain authoritative.
- **Fallback:** any missing-required / stale / low-quality / untrusted state ⇒ the candidate proposes **`DEFER_TO_LEGACY`**,
  never a forced adaptive exit. Solana always `DEFER_TO_LEGACY`.
- **Reuse the real exit primitives** — a proposed exit maps to an existing `_EXECUTABLE_PRICE_REASONS` reason and would run
  through `executable_exit_allowed` (§Phase 0 seams); the engine never invents an exit route.

**Pre-registration (before any outcome is read):** every formula, bound, window length, minimum sample, and chain-specific
difference is fixed in this spec's companion pre-registration file and frozen. **No threshold is fit on the 29-position
cohort.** Defaults come from market mechanics, existing funded settings, or an answer-independent grid — never a backtest
argmax.

**Decision record (every tick, actionable or not):** engine+feature version; MarketState snapshot ref; proposed action+size;
**legacy action at that instant** (`alpha_b_real_exit_reason(...)`); reason codes; feature freshness/quality; exact input
block_ts; whether executable confirmation was available; whether the proposal was later censored (legacy closed the bag);
no-action reason.

## 7. Phases handled elsewhere in the pipeline
Phase 5 (observability) and Phase 6 (distribution detector) are §9 and §10. Phase 4 (validation) is §8.

## 8. Phase 4 — honest paper-free validation

No synthetic paper fill is proof. Gates are sequential; each has its own pre-registered read.

**Gate 0 — semantic truth.** Every input has verified meaning, causal block_ts semantics, coverage measurement, and tests.
Unsupported features stay disabled. (This spec + its tests.)

**Gate 1 — deterministic replay.** Replay real funded entries against their recorded tapes with the production
`compute_state`+`propose`. Report **separately**: nominal-mid model · executable observations (currently **none**) · real
submitted fills · missing/censored. **Never fabricate an executable series from mid+haircut.** **Honest limitation (Phase 0
§6):** the 29 historical positions have **no tick tape** — Gate 1 on history is **mid-marks structure/drawdown only** and
cannot confirm flow. Full-tape replay begins with forward positions. Use replay to *reject* unsafe shapes, not to *select* a
max-PnL threshold.

**Gate 2 — live shadow divergence.** Run the candidate on live open funded positions without changing execution.
Pre-register: eligible-position definition; divergence definition; safety metrics; feature-availability + stale/fallback
rate; decision latency; chain / contract-band splits; minimum divergent-decision count; censoring rules; sensitivity;
pass/redesign/stop branches. **Critical limitation stated in every read:** when the legacy engine closes the bag, the
candidate's alternate later exit is **censored** — shadow gives real-state and timing truth, **not** uncensored alternate
PnL. **Blocked today** on (a) the tripped kill switch ⇒ 0 open positions, and (b) a quota-backed RPC for the executable lane.

**Gate 3 — operator-approved funded canary.** Shadow cannot prove funded alternate outcomes. If Gates 0–2 pass, draft a
separate versioned funded-canary proposal (a new funded policy version + cohort cutover, per the rollover/entry-contract
specs) that preserves: frozen entry contracts; old-cohort immutability; exact full-balance executable confirmation;
unknown-confirm fail-closed; atomic de-risk state transitions; global exposure + reservation accounting; kill switch; pinned
release deploy; backup/validation/reservation/reconcile gates; rollback path. **Do NOT activate — Checkpoint A.**

**Gate 4 — sealed funded read.** Only after an approved canary reaches enough eligible real closes / divergent funded
decisions (default `n ≥ 30`) run the pre-registered read once: total + per-chain; by contract band; worst case;
stop/catastrophe share; drawdown; drop-top-1 / drop-top-2 sensitivity; feature quality + fallback rate; latency;
censored/ambiguous exclusions; exact deploy anchor + cohort cutoff. Follow the registered branch exactly.

## 9. Phase 5 — observability (Track-A minimal)
Read-only surface for an open position: current MarketState; feature freshness/quality; structure state + confirmed levels;
volatility state; flow state (unsupported fields shown explicitly unavailable); candidate action + reasons + legacy action +
divergence; materializer + shadow health. **No** cockpit/order UI.

## 10. Phase 6 — distribution detector (future spec ONLY, not built)
Entry gates for a later spec: ≥2–4 weeks of stable tape; chain-specific coverage thresholds; validated actor identity;
hardened wash filtering; stable event-time semantics; enough labeled real outcomes; leakage checks; train/validation time
separation; explanation + rollback. No online learning, no direct funded control.

## 11. Proposed schema (log-only; migrations follow the repo process)

New tables live in the **ticks DB** where they read the tape, except the decision/state snapshots which reference the ledger.

<!-- schema:external=block_times -->
<!-- schema:external=memecoin_exit_market_state -->
<!-- schema:external=memecoin_exit_candidate_decisions -->
```sql
-- ticks DB (data/memecoin_ticks.db): immutable block→time map
CREATE TABLE IF NOT EXISTS block_times (
    chain_id TEXT NOT NULL, block_no INTEGER NOT NULL, block_ts TEXT NOT NULL,
    fetched_at TEXT NOT NULL, PRIMARY KEY (chain_id, block_no));

-- compact versioned MarketState snapshot per open position per tick
CREATE TABLE IF NOT EXISTS memecoin_exit_market_state (
    id INTEGER PRIMARY KEY, position_id INTEGER NOT NULL, chain_id TEXT NOT NULL, mint TEXT NOT NULL,
    pool TEXT, engine_version TEXT NOT NULL, feature_version TEXT NOT NULL,
    computed_at TEXT NOT NULL, source_event_time TEXT, freshness_s REAL, state TEXT NOT NULL,
    nominal_price REAL, executable_price REAL, executable_peak REAL, drawdown REAL, drawdown_velocity REAL,
    realized_vol REAL, net_flow_quote TEXT, arrival_rate REAL, arrival_accel REAL,
    reserve_quote TEXT, state_json TEXT,          -- COMPLETE versioned snapshot (MarketState.to_dict())
    UNIQUE(position_id, computed_at));
-- state_json carries structure, per-family coverage/quality/source/freshness/price_kind,
-- required_families and entry_contract_version. Separate structure/coverage/quality blobs were
-- byte-identical subsets of it and were dropped 2026-07-23; query with json_extract.

-- shadow decision record (candidate vs legacy; censoring-aware)
CREATE TABLE IF NOT EXISTS memecoin_exit_candidate_decisions (
    id INTEGER PRIMARY KEY, position_id INTEGER NOT NULL, decided_at TEXT NOT NULL,
    engine_version TEXT NOT NULL, feature_version TEXT NOT NULL, state_id INTEGER,
    candidate_action TEXT NOT NULL, candidate_reason_codes_json TEXT NOT NULL DEFAULT '[]',
    proposed_fraction REAL, legacy_action TEXT, divergent INTEGER NOT NULL DEFAULT 0,
    executable_confirmed INTEGER NOT NULL DEFAULT 0, censored INTEGER NOT NULL DEFAULT 0,
    input_block_ts TEXT, no_action_reason TEXT, outcome TEXT,
    UNIQUE(position_id, decided_at, engine_version));
```

## 12. Safety invariants (the shadow MUST NOT violate)
- No write to any funded table, reservation, rail, override, or `memecoin_positions` money field.
- No call that nominates, sizes, signs, approves, or broadcasts.
- Frozen `FundedExitPolicy` contracts unchanged; old cohort immutable.
- Materializer runs as a **default-OFF, env-gated standalone process** with its own connections; the shadow is
  **default OFF**, env-gated, and runs **after** funded hooks with its own connection. Neither shares the runner's
  failure domain — the materializer is out-of-process; a fault in either cannot stall the funded rails.
- Fail-closed everywhere: missing/stale/ambiguous ⇒ non-actionable state ⇒ legacy fallback. Never a fabricated feature.

## 13. Tests (Gate 0 — production-shaped fixtures, not self-agreeing stubs)
Causal pivot detection + confirmation delay; no-lookahead property; realized-vol + clamps; drawdown-velocity with irregular
Δt; arrival-accel event-time vs ingest-time; dust filter + provenance; router-vs-user identity cases; V2/V3/Solana
capability differences; stale/missing/partial/duplicate/out-of-order ticks; restart + bounded catch-up; feature-version
change; deterministic replay/live parity (same `compute_state`); snapshot atomicity; materializer health/freshness; shadow
cannot mutate funded state; shadow cannot nominate/sign/approve/broadcast; frozen contracts unchanged; candidate fallback to
legacy on unknown data; censoring + sealed-read accounting; `validate_schema_refs`; existing funded-executor regressions;
`git diff --check`. Drive the **production** decoder/repository/context/runner-adapter with controlled inputs where possible.

## 14. Scope guards & stop conditions
Proceed autonomously through the offline/materialization/replay/shadow-code/observability build and tests. **Stop and ask
before:** any live RPC/API call; choosing/enabling a paid RPC provider; enabling the fast lane; writing production DB state;
editing `.env`; touching the kill switch; changing funded rails/runner behaviour; staging funded v3; restarting a service;
deploying; activating the canary (Gate 3 / Checkpoint A); pushing/merging/opening a PR.

## 15. Open questions
- Exact block-time backfill cadence + a hard per-tick `getBlockByNumber` cap (probe read-only at implementation).
- Whether `MEMECOIN_MARK_WRITE_MIN_INTERVAL_OPEN_S` freshness is enough to define `DATA_STALE`, or the materializer needs
  its own tick cadence.
- The precise pre-registered structure/flow/vol formulas + bounds — settled in the pre-registration file before Gate 1.

## Amendment 2026-07-24 — Solana unfreeze path (v2)

The §3.1 Solana row ("forced legacy … no amounts … slots absent from `block_times`") and the §6
"Solana always `DEFER_TO_LEGACY`" rule are **conditionally superseded**: the data gap they encoded
was closed by read-only capture verified against live mainnet — slot→time via `getBlockTime` into
`block_times` (`chain_id='solana'`, `memecoin_solana_blocktime.py`) and program-agnostic swap
amounts via `getTransaction` pool-vault balance deltas (`memecoin_solana_amounts.py`), both on the
strictly-read-only `MEMECOIN_SOLANA_READ_RPC_URL` lane (never the custody var).

Governing document: `docs/superpowers/plans/2026-07-24-memecoin-real-exit-brain-solana-prereg.md`
(frozen v2 prereg). Summary of what it registers: a Solana profile with required families
`price, flow, vol` (depth forced unavailable, like V3); all v1 window constants inherited verbatim;
all-or-nothing coverage gates (per-signature resolution ledger, subscription liveness); and a
pre-registered Gate-0 capture-fidelity study (recall ≥ 0.95, decode yield ≥ 0.90, direction
fidelity ≥ 0.95 against `getSignaturesForAddress` ground truth) that must PASS before any Solana
state is trusted. Until the implementing PR lands, Gate 0 passes, and the v1 Gate-2+ ladder runs
chain-split, Solana remains forced legacy exactly as §3.1 states. Nothing is armed by this
amendment.

## Amendment 2026-07-24b — storage architecture correction (Knowledge Sync)

§11's placement of the exit-state tables and the worker's RPC-backfilled block-time copy in
`data/memecoin_ticks.db` is SUPERSEDED. After the two-writer WAL lock incident (2026-07-24) the
worker owns `data/memecoin_exit_state.db` (market-state snapshots, candidate decisions, its own
`block_times`) and the Solana pull tape owns `data/memecoin_sol_tape.db`; the worker's handle on
the funded tick tape is structurally READ-ONLY (`connect_ticks_readonly`). The funded runner's
ingest-time `blockTimestamp` capture remains the primary head-time source; the worker's copy is
fallback. See commits 6baa024c / 685e6172 / 65f02e77.

## Amendment 2026-07-24c — activation hardening round 4 (Knowledge Sync)

The funded adapter remains default OFF and undeployed, but four implementation contracts are now
explicit:

- `sol_sig_cursor.frontier_before` is mode tagged. `enum:<signature>` resumes bounded enumeration;
  `decode:<signature>` re-enumerates from the head while a cursor-contiguous decode backlog drains.
  The frontier clears only after an unrestricted walk reaches the cursor and every returned
  signature is decoded. RPC/transaction failure retains incompleteness.
- A de-risk claim owns an opaque `memecoin_positions.derisk_claim_id`. The signed transaction
  identity is committed as claim-owned submitted-unknown before broadcast. Release, reset, fill,
  and recovery transitions compare the claim and signature, so crash-after-broadcast and stale
  recoverers cannot re-arm a second partial sell.
- `MEMECOIN_ENGINE_AUTHORITATIVE_CHAINS` is a live funded kill lever read directly from
  `$SNIPER_STATE_ROOT/.env` at each boundary. The state-root file is authoritative; missing,
  blank, invalid, or unreadable state fails OFF without mutating the process environment.
- Engine decisions accumulate in memory during the funded sweep. Only after every funded hook
  returns are complete one-row records inserted into `memecoin_exit_candidate_decisions`, including
  `state_id`, proposed fraction, exact-confirmation result, and final outcome. Exit-state DB lock or
  write failure can delay/drop telemetry only after funded submission, never before it. The funded
  handle opens the existing worker-owned file without schema initialization or journal-mode writes;
  a missing file fails to envelope-only instead of being created on the funded path.

Knowledge delta: architecture and funded-safety workflow updated; frozen v3 decision rules,
activation state, deployment state, and research verdicts unchanged.
