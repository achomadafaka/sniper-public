# Memecoin Copy Hardening — Parallel Execution-Strategy Arms

**Date:** 2026-07-07
**Status:** approved design (brainstorming), pre-plan
**Owner:** memecoin copy-mirror subsystem
**Related:** [[project-memecoin-discovery-funnel]], [[project-fomo-account-analyzer]], the signal-source A/B (`signal_source` attribution)

## 1. Problem

The copy engine has a core failure mode: **we become exit liquidity.** A sharp memecoin
trader's edge is often *selling into the notification herd*. We front-run the herd on their
**buy** — but if they're accumulating to dump on the herd, the herd (and our early bag) is who
they sell to, and we eat the reversal. The early ledger fits: losses are 100% stop-losses
(4 SL = −$245), the only green is mirror-exits (+$17) — the shape of "bought a pump that reversed."

Root cause is **thesis incoherence.** The engine is two theses stapled together:
- It **selects** like we copy **trader alpha** (curated good traders) — Thesis B.
- It **exits** like a **slow position** (SL −60% / TP +500% / mirror-on-sell) — half Thesis B.
- But the **stated edge** is **front-running the herd pump** — Thesis A (seconds-to-minutes).

These want opposite exits. We don't know which thesis is real, so instead of guessing we run
them **in parallel on the same signal and let live paper PnL judge.**

## 2. Approach

**Parallel execution-strategy arms on a shared entry stream.** Every curated large-buy event
(entry logic unchanged) spawns up to **three tagged paper positions**, one per arm. Arms differ
**only in exit policy** in v1 — entry and sizing stay shared, so the *only* variable is the exit
(where the exit-liquidity failure lives). Attribution reuses the `signal_source` pattern: a new
`strategy` tag on positions, compared PnL-by-strategy in the Closed tab.

| Arm | tag | exit policy | thesis it tests |
|---|---|---|---|
| Control | `control` | current: SL −60% / TP +500% / mirror-on-sell | the status quo (baseline) |
| Thesis A | `momentum_a` | momentum-rollover fast exit (§4) | *our* fast read of the herd pump is the edge |
| Thesis B | `alpha_b` | mirror-the-trader's-sell + wide catastrophe rails, no TP (§5) | *the trader's* exit timing is the edge |

A > B > control (any ordering) is directly interpretable because the arms are cleanly distinct.

## 3. Data model & attribution

**`memecoin_positions` gains** (idempotent migration):
- `strategy TEXT DEFAULT 'control'` — which arm opened it.
- `peak_price REAL`, `peak_ts TEXT`, `no_new_high_sweeps INTEGER DEFAULT 0` — momentum state.
- `time_to_peak_s REAL`, `max_gain_pct REAL` — computed at close (instrumentation).

**Dedup key changes:** `get_open_position` becomes per **(source_user_id, mint, strategy)** so the
three arms hold independent positions on the same (trader, token). `skipped_dup` is now per-arm.

**`strategy_summary(conn)`** (mirrors `signal_source_summary`): opened/closed/win_rate/realized/
median-hold **and** `exit_liquidity_rate` (§6) per arm.

## 4. Arm A — momentum rollover (fast pump-ride)

Runs inside the existing ~15s bracket sweep (extends the batched DexScreener price fetch we
already do — no new feed). Per open `momentum_a` position, each sweep:
1. Read `price`. If `price > peak_price` → update `peak_price`/`peak_ts`, reset `no_new_high_sweeps=0`.
   Else → `no_new_high_sweeps += 1`.
2. Exit (fill at modeled slippage) on **whichever fires first**:
   - **Rollover:** `no_new_high_sweeps >= MEMECOIN_A_ROLLOVER_SWEEPS` (momentum stalled).
   - **Trailing:** `price <= peak_price * (1 - MEMECOIN_A_TRAIL_PCT/100)` (sharp reversal between sweeps).
   - **Hard stop:** `price <= entry_price * (1 - MEMECOIN_A_STOP_PCT/100)` (rug/dump cap; tighter than B).
   - **Time cap:** held longer than `MEMECOIN_A_MAX_HOLD_S` (no slow bleed).
   `exit_reason` records which fired (`a_rollover|a_trailing|a_stop|a_timecap`).
3. A **ignores the trader's sell** — its thesis is *our* momentum read, kept pure so the A-vs-B
   comparison isolates "our timing vs their timing." (A hybrid that also uses the trader's sell is
   an explicit v2 variant.)

Every A close computes `time_to_peak_s = peak_ts - opened_at` and `max_gain_pct`. After ~30–50 A
closes we read the **pump-duration distribution** (median/spread of time_to_peak_s, max_gain_pct)
and **calibrate ROLLOVER_SWEEPS / TRAIL / MAX_HOLD from data** instead of the launch guesses.

## 5. Arm B — alpha-ride (trust the trader)

- **Primary exit = mirror the trader's sell** (proportional, as today; now faster via the per-follow
  activity poll). `exit_reason='b_mirror'`.
- **Catastrophe rails only** (NOT market-timing): wide stop `MEMECOIN_B_STOP_PCT` (default 50) and
  long `MEMECOIN_B_MAX_HOLD_S` (default 12h). These bound the "we failed to catch their sell" case
  (feed gap / off-venue exit / dead bag), nothing more. `exit_reason='b_stop'|'b_timecap'`.
- **No take-profit** — a TP overrides the trader's timing, which is the thesis under test.

## 6. Exit-liquidity meter (free, from §4 instrumentation)

For **every** closed position (all arms), `time_to_peak_s` is stored. Define
`exit_liquidity_rate` per arm = fraction of closes whose peak was within the first sweep of entry
(price only fell after we bought → **we were the exit liquidity**). A high rate confirms the failure
mode; A's fast exit should show a lower *realized loss* even when the rate is high (it bails on the
rollover). Surfaced in the Closed-tab arm panel — a direct read on the thing this whole spec targets.

## 7. Settings (`sniper/settings/memecoin.py`)

`MEMECOIN_A_ROLLOVER_SWEEPS` (default 2), `MEMECOIN_A_TRAIL_PCT` (15), `MEMECOIN_A_STOP_PCT` (30),
`MEMECOIN_A_MAX_HOLD_S` (900); `MEMECOIN_B_STOP_PCT` (50), `MEMECOIN_B_MAX_HOLD_S` (43200);
`MEMECOIN_ARMS` (default `control,momentum_a,alpha_b`) — the enabled arms (kill switch per arm).

## 8. Surfaces

- Closed tab: arm-comparison panel next to the source A/B (PnL/WR/median-hold/exit_liquidity_rate
  per arm) + the pump-dynamics readout (median time-to-peak, max_gain distribution) once A has closes.
- Open/Positions: show the `strategy` tag per position (so 3 positions on one token read clearly).
- Reuses existing endpoints; adds `GET /api/memecoin/strategies` (→ `strategy_summary`).

## 9. Testing (TDD)

- **Momentum state machine** (pure, synthetic price paths): new-high tracking + reset; rollover after
  N stalled sweeps; trailing trigger on a between-sweep reversal; hard stop; time cap; whichever-first
  ordering; a monotonic-pump path that peaks then rolls (exits at rollover, not at entry or floor).
- **Arm B**: mirror fires on trader sell; wide stop only on catastrophe; time cap closes a dead bag;
  no TP path exists.
- **Fan-out**: one event → one position per enabled arm; per-(uid,mint,strategy) dedup; `strategy`
  persisted; `strategy_summary` partitions correctly.
- **Instrumentation**: time_to_peak_s / max_gain_pct computed at close; exit_liquidity_rate math.
- Full memecoin suite green; schema validator + brain index fresh.

## 10. Out of scope (v2 — explicit)

Per-thesis **selection** (A = follower-count-weighted, B = quality-gated on `median_hold_s`/coverage/
median%), quality/conviction **sizing**, **concentration caps** per token across arms, **liquidity-aware**
exit sizing, and **calibrated** exit constants derived from §4's instrumentation. We add these only
after the arm comparison shows which exit even wins — selecting/sizing before we know the exit is
premature. Entry stays flat-$100/all-curated in v1 to keep the exit the sole variable.

## 11. Honesty rails

All arms are paper; realized PnL uses the honest modeled-slippage exits already shipped. The arm
comparison is a forward A/B/(control) — read it at a pre-registered n (≥30–50 closes per arm), not
early (optional-stopping bias). "Which thesis wins" is only answerable once each arm has a real
sample; until then the panel is descriptive, not a verdict.
