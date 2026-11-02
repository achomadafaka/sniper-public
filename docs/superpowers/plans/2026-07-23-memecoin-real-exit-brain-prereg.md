# Memecoin real exit brain — PRE-REGISTRATION (frozen)

**Date frozen:** 2026-07-23 · **Status:** FROZEN · **Engine:** `exit-brain-v1` · **Features:** `v1`
**Companion:** `docs/superpowers/specs/2026-07-23-memecoin-real-exit-brain-design.md` (§6 requires this file).

## 0. Attestation

- **No outcome has been read.** Nothing below was fitted, tuned, or selected on the 29-position cohort,
  on replay output, or on any shadow/paper/funded result. No backtest argmax appears in this document.
- Every constant is one of: (a) **inherited** verbatim from the already-live funded policy, (b) **mechanical**
  (forced by the data's own structure), or (c) an **answer-independent bound** (a round factor chosen before
  any read). Each is tagged `[inherited]`, `[mechanical]`, or `[bound]` below.
- `feature_version` stays **`v1`**: the exit-state worker has never run (`MEMECOIN_EXIT_STATE_ENABLED`
  default OFF, not in cron), so **zero** snapshots exist under any prior definition. `v1` therefore names the
  definitions in this file, not a superseded one.
- **Unfreezing requires:** a new dated prereg file + a `feature_version` / `engine_version` bump + a stated
  reason that is not "the numbers would look better". Editing a constant in place is a protocol violation.

## 1. Frozen feature definitions (Phase 1)

Implemented in `sniper/analysis/exit_features.py` + `sniper/services/memecoin_exit_market_state.py`.
All windows are in **seconds of `block_ts`** (true event time), never ingest time.

| # | Feature | Frozen definition | Constant | Basis |
|---|---|---|---|---|
| F1 | price path | size-weighted per-BLOCK VWAP `Σ\|quote\| / Σ\|base\|`, atomic ratio | — | `[mechanical]` one price per block is the finest honest granularity; per-print carries impact and gives equal timestamps |
| F2 | realized vol | zero-mean RMS of consecutive log returns: `sqrt(Σrᵢ²/n)` over the window | `VOL_WINDOW_S=300` | `[mechanical]` RMS is the standard realized-vol estimator and is continuous in `n`; sample std-dev was not (one +10% → 0.0953, two identical → ~0). `[bound]` 300 s = 5 min, a round horizon fixed before any read |
| F3 | confirmed pivots | swing high/low reported only after `confirm` strictly-later bars exist (causal, no lookahead) | `PIVOT_CONFIRM=2` | `[bound]` 2 is the minimum that rejects a single-bar wick; 1 would confirm every tick |
| F4 | structure state | `higher_low_state` = last confirmed low > previous; `lower_high_state` = last confirmed high < previous; `None` with <2 of that kind | — | `[mechanical]` |
| F5 | flow | pool-level signed net quote flow; **dust-only** wash filter; `wash_filter=disabled_router_ambiguity` | `DUST_ATOMIC=0` | `[inherited]` router≠buyer (Phase 0 §3) forbids actor filtering; 0 = filter nothing until a dust level is measured |
| F6 | arrival rate | clean swaps per second over the FIXED closed window `(end−W, end]` | `ARRIVAL_WINDOW_S=60` | `[bound]` 1 min, round, fixed before any read. Never `count/(last−first)` |
| F7 | arrival accel | `rate(current window) − rate(previous adjacent window)` | same `W` | `[mechanical]` |
| F8 | depth (V2 only) | last `Sync` quote reserve + delta between the last two Syncs | — | `[mechanical]` |
| F9 | `depth_pull_flag` | **permanently `None`** | — | `[mechanical]` V2 emits `Sync` on ordinary swaps, so reserve direction cannot separate LP removal from trading without Mint/Burn decode (undecoded) |
| F10 | executable drawdown | `drawdown_from_exec_peak` and executable-peak velocity are **permanently `None`** | — | `[inherited]` spec §1 binding retraction + §3.4: no executable series exists; deriving one from print prices is forbidden |
| F11 | warmup floor | `< MIN_STRUCTURE_POINTS` per-block points → `WARMUP` | `MIN_STRUCTURE_POINTS=5` | `[bound]` 5 = the fewest points that can contain a confirmed pivot (`1 + 2·confirm`) plus one |
| F12 | staleness | family freshness `> STALE_S` → that family is `degraded`; newest-event freshness `> STALE_S` → `DATA_STALE` | `STALE_S=180` | `[bound]` 3 min, round; ≈ the funded mark cadence order-of-magnitude |
| F13 | read bound | at most `MAX_TICKS` recent ticks per pool per pass | `MAX_TICKS=5000` | `[bound]` fan-out cap, not a signal parameter |
| F14 | header budget | at most `BLOCKTIME_MAX_PER_TICK` headers fetched per pass, newest-first | `64` | `[bound]` RPC spend cap, not a signal parameter |

### 1.1 Trust contract (frozen)
A state is **candidate-capable** iff its lifecycle label is actionable AND **every** required family for its
profile is provenance-`trusted`. Required families `[inherited from spec §3.1]`:

| profile | required families | depth |
|---|---|---|
| 4663 / 8453 **V2** | `price, flow, vol, depth` | required (V2 `Sync`) |
| 4663 / 8453 **V3** | `price, flow, vol` | forced unavailable, NOT required |
| **Solana** | — | forced legacy; arrival unavailable (no amounts; slots absent from `block_times`) |
| **unknown chain / unknown pool kind** | — | forced legacy (fail-closed) |

Fail-closed cases, all → non-actionable, all frozen: incomplete block-time coverage; event time ahead of the
compute clock; a required family stale or absent; an actionable label with an empty required set.

## 2. Frozen candidate decision rules (Phase 3 / Task 11)

`propose(contract, state, cfg) -> CandidateDecision`. **Pure, proposes only, never submits.**

### 2.1 Absolute preconditions
1. `state.candidate_capable()` false ⇒ **`DEFER_TO_LEGACY`**. No exception, ever.
2. Solana ⇒ **`DEFER_TO_LEGACY`** always `[inherited spec §6]`.
3. **Monotonic risk-reduction only** `[inherited refusal #2]`: the candidate may only TIGHTEN a stop, LOWER a
   TP, de-risk, tighten a trail, or exit. Any proposal that would widen a stop, raise a TP, add size, reset a
   peak, or re-enter is a bug, not a decision. Enforced as a property test, not by discipline.

### 2.2 Nomination → confirmation (frozen)
- **Nominate weakness** when the latest per-block price closes **below the most recent confirmed swing low**
  (`prior_low`, F3/F4). Structure nominates; it never decides alone. `[inherited spec §6]`
- **Confirm** with verified flow over the frozen arrival window (F6): confirmation requires
  `net_flow_quote < 0` — **sign only, no magnitude threshold** `[mechanical]`. A magnitude cut would be a free
  parameter with no answer-independent basis, so none is registered.
- Flow `≥ 0` ⇒ the nomination is **rejected** (state returns to TRENDING/WEAKENING), not escalated.
- `WEAKENING → BREAK_NOMINATED → BREAK_CONFIRMED → EXIT_PROPOSED` requires the above in order; skipping is
  not permitted.

### 2.3 Volatility scaling, hard-bounded (frozen)
Realized vol (F2) may scale the proposed trail distance ONLY within these bounds, expressed against the
position's **live** funded policy trail `T` `[inherited from the running policy]`:

- proposed trail ∈ `[0.5·T, T]` `[bound]` — at most 2× tighter, **never looser than live**.
- Vol may never remove a guard, only move it inside that interval.
- If `realized_vol is None` ⇒ no scaling; use `T` unchanged.
- **Mapping (made explicit 2026-07-23, before any outcome was read — parameter-free by construction):**
  `proposed = T · (0.5 + 0.5 · min(1, realized_vol / (T/100)))`.
  Vol is compared to **the live trail itself**, so no new reference constant is introduced. Reading:
  a quiet tape (`vol ≪ T`) lets the trail tighten toward `0.5·T`; once one window's realized vol reaches
  the trail distance, the trail is inside the noise and stays at the live `T` — the candidate never
  tightens a stop into its own noise floor. Both endpoints are the registered bounds.

Live policy anchors as of freezing (`sniper/settings/memecoin.py`, `memecoin_funded_policy.py`):
nano/micro `derisk 20% @ fraction 0.5, trail 30%, stop 60%`; mid `derisk 30%, trail 35%, stop 60%`;
`trail_arm 15%`; `catastrophe 80%`; `stop_confirm_sweeps 2`; `MAX_CONCURRENT 7`.

### 2.4 Catastrophe guard (frozen, absolute)
The candidate never widens, delays, or overrides the live catastrophe stop (`80%` `[inherited]`). It is the
only guard the candidate may not scale.

### 2.5 Execution mapping
An `EXIT` proposal maps to an existing `_EXECUTABLE_PRICE_REASONS` reason and would run through
`executable_exit_allowed`. The engine never invents an exit route `[inherited spec §6]`.

## 3. Gate reads — definitions and pre-registered branches

**Minimum sample for ANY verdict: `n ≥ 30` eligible closes / divergent decisions** `[inherited spec §8]`.
Reading earlier is a protocol violation regardless of how the numbers look.

### Gate 1 — deterministic replay (reject-only)
- Report **separately**: nominal-mid model · executable observations (**currently none**) · real submitted
  fills · missing/censored. Never merge these into one number.
- Historical 29 positions have **no tick tape** ⇒ mid-marks structure/drawdown only; flow is **not** confirmable.
  Any Gate-1 statement about flow is void.
- **Branch:** replay may only **reject** unsafe shapes (a shape that proposes a widened guard, acts on an
  untrusted state, or exits worse than legacy in the worst case). It may **not** select a max-PnL threshold.

### Gate 2 — live shadow divergence
- Pre-registered before running: eligible-position definition (open real, candidate-capable, chain-split);
  divergence = candidate action ≠ legacy action at the same instant; feature-availability and stale/fallback
  rate; decision latency; chain and contract-band splits; censoring rules.
- **Censoring is stated in every read:** when the legacy engine closes the bag, the candidate's alternate later
  exit is unobservable. Shadow gives **state and timing truth, not uncensored alternate PnL.**
- **Branches:** (pass) ≥30 divergent decisions, zero monotonicity violations, fallback rate reported →
  proceed to draft a Gate-3 canary. (redesign) monotonicity violation or >50% fallback → fix and re-run.
  (stop) candidate diverges toward *more* risk in any case → kill the program.

### Gate 4 — sealed funded read (once, after an approved canary)
- Report: total + per-chain; by contract band; worst case; stop/catastrophe share; drawdown;
  drop-top-1 / drop-top-2 sensitivity; feature quality + fallback rate; latency; censored/ambiguous
  exclusions; exact deploy anchor + cohort cutoff.
- Run **once**, follow the registered branch exactly, no re-slicing after seeing it.

## 4. Explicitly NOT pre-registered (and therefore not permitted)

Unique buyers / holder concentration; actor-based wash filtering; V3 LP pulls; Solana flow/depth/price/arrival;
any executable price derived from mid + haircut; any learned model touching parameters or money; any threshold
fitted on the cohort. These stay disabled — adding one requires a new spec, not a config change.

## 5. Change control

| Change | Requires |
|---|---|
| any constant in §1 or §2 | new dated prereg + `feature_version`/`engine_version` bump |
| a new feature family | spec amendment + prereg + Gate 0 tests |
| reading a gate below `n ≥ 30` | not permitted |
| enabling `MEMECOIN_EXIT_STATE_ENABLED` | operator STOP condition (block-time RPC budget) |

**Activation blocker cleared (2026-07-23):** `realized_vol` is now the frozen continuous RMS estimator (F2),
versioned `v1` and covered by a continuity regression
(`tests/test_exit_features_vol.py::test_vol_is_continuous_in_n_and_reduces_to_abs_return_at_n1`). The prior
discontinuity no longer blocks activation. The remaining activation gates are the operator STOP conditions.
