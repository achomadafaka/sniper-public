# Crypto5m Direction-State Scalper v2 — Design

**Date:** 2026-06-21
**Status:** Design (approved, pre-implementation)
**Scope:** The continuous ACTIVE paper engine for the Polymarket BTC Up/Down 5-minute binary (`active-paper-v0`). Paper only, no orders.
**Related:** [[project-crypto5m-active-engine]], [[finding-crypto5m-walkforward-tape-model]], `docs/superpowers/specs/2026-06-21-crypto5m-conditional-edge-scanner-design.md`

---

## 1. Problem (root cause, established with data)

The live engine reads direction correctly but bleeds. Audit of 287 closed paper trades + the captured millisecond stream:

- **The tight price-stop is the entire bleed.** `stop` exits: 117 trades, **−$249**, 3% win — while `take_profit` (145 trades, +$202, 78% win) works fine.
- **The stop bails into a gap.** The binary moves **p95 5c / p99 12c in a single 2s poll** (11% of polls jump >3c). The stop threshold is ~3c, so it never executes at 3c — the realized adverse move *at* the stop is a **median 10c**. It fires at the worst possible instant.
- **The move we called then happens without us.** After a stop, the side recovered to **≥ entry within the window 72%** of the time, **≥ exit+3c 85%**, and **never recovered only 5%**; median max-favorable-move after the panic exit was **+26c**, ~25s later.
- **But "no stop" isn't free.** 55% of stopped positions' sides lose the *settlement*; the intra-window path recovers (72%) far more often than the window settles our way (~45%). Holding everything to settlement converts small stops into full −$10 losses.
- **The deeper defect:** the directional read is **instantaneous and memoryless**. It commits and un-commits on micro-noise — so it both *enters* on transient wiggles and *exits* on transient wiggles. It cannot distinguish a sustained, multi-scale move from a market micro-adjustment.

### Replay evidence that selected the design

Gap-faithful replay (2s grid; bid/ask from the ms `best_bid_ask` stream so inter-poll gaps are present; deferred one-grid fills; held positions settle at outcome; 58 settled windows). Same confirmed entry; only the layer under test varies:

| entry+exit | n | net | mean | win% | worst window |
|---|---|---|---|---|---|
| INSTANT + CURRENT (today) | 174 | −42.34 | −0.243 | 30% | −18.86 |
| INSTANT + PATIENCE (exit fix only) | 134 | −9.84 | −0.073 | 47% | −20.89 |
| **PERSIST + PATIENCE (exit fix + entry memory)** | **70** | **+26.37** | **+0.377** | **49%** | **−14.65** |
| SMOOTH (Holt) + PATIENCE | — | no trades | | | |

Two layers, each earning its keep: the gap-immune exit takes net −42 → −10; the entry-memory layer takes −10 → **+26** while trading half as often (only on sustained moves) and producing the lowest tail. The Holt smoothed-state entry produced no trades (its velocity sign is itself too noisy to stay stable) — consistent with the prior finding that the Holt prior is redundant. **The "memory" that works is persistence-of-the-read + multi-horizon agreement, not a Kalman/Holt filter.**

**Fidelity caveat (load-bearing):** the replay is touch-optimistic on intra-tick fills and is 58 windows / 70 trades. +$26 is **encouraging directional evidence, not proof**. The live forward run, with the slip logging already wired, is the judge of absolute PnL.

---

## 2. Goals / non-goals

**Goals**
- Stop entering on micro-wiggles: commit only on a **confirmed, persistent, multi-horizon** directional read.
- Stop the gap-driven bleed: replace the tight price-stop with a **gap-immune** exit.
- Keep what works: take-profit / trailing (78% win) is unchanged.
- Stay honest: every layer unit-tested; the replay stays as a regression harness; live forward run + slip log is the arbiter.

**Non-goals**
- No switch to "guess the window close" — the data shows intra-window scalping (72% recovery) strictly beats holding to settlement (45%).
- No new ML runtime dependency — the memory layer is a pure, inspectable rule.
- No change to the probability/fair-value model, the UI tab structure, or any other engine.
- No real orders. Paper only.

---

## 3. Design

Two small, independently-testable units inserted into the existing data flow. `direction.directional_thesis` (per-tick fusion) is **unchanged**.

```
live_desk.build_live_desk            (live data: book, BTC, tape order flow)
  -> direction.directional_thesis    (per-tick instantaneous read — UNCHANGED)
  -> direction_state.update          (NEW: memory — confirm persistence + multi-horizon)
  -> scalp_trader.decide             (enter on CONFIRMED only; PATIENCE exit)
  -> runner                          (deferred fill + slip logging — UNCHANGED)
```

### 3.1 `direction_state` (new, pure module: `sniper/algos/polymarket_crypto5m/direction_state.py`)

A small stateful helper that carries memory across ticks and answers one question: *is this a real direction or a wiggle?*

- **Input per tick:** the current `directional_thesis` result (side, score, conviction, votes), plus the multi-horizon momentum z-scores already on the desk (`ret_5s/15s/30s` → short and long horizons) and the tape order-flow vote.
- **State:** a bounded deque of recent (timestamp, score-sign, conviction) — only the last ~`PERSIST_WINDOW_S` seconds.
- **Confirmation rule — a direction is `confirmed` only when all hold:**
  1. **Persistence (memory):** the fused score has held the **same sign** for `≥ PERSIST_TICKS` consecutive updates (~6s).
  2. **Multi-horizon agreement:** a short horizon and a longer horizon agree in sign (a real trend shows at multiple scales; a wiggle at one). Reuses the desk's `ret_5s/15s/30s`.
  3. **Order-flow agreement:** the tape order-flow vote agrees with the side (already computed; the strongest predictor).
  4. **Strength floor:** conviction ≥ the existing `ENTER_CONVICTION` bar.
- **Output:** `{confirmed_side, persistence_ticks, horizons_agree, flow_agree, confirmed: bool}`.
- **Pure / testable:** the deque + clock are passed in or held in an instance; no I/O. Deterministic given an input sequence.

Implemented as a small class (`DirectionState`) the runner instantiates once and `.update(thesis, desk, now)` each tick; it returns the confirmation dict. Restart-safe by reconstruction is unnecessary (memory is only ~6s; a cold start simply waits one persistence window before its first confirmed entry).

### 3.2 `scalp_trader` exit → PATIENCE (gap-immune)

Entry uses `direction_state.confirmed` instead of the instantaneous thesis gate. The exit drops the tight price-stop:

- **Keep unchanged:** take-profit (`bid ≥ TAKE_PROFIT_PRICE`), trailing give-back lock (the +78% winners).
- **Remove:** the tight `gain ≤ −max(STOP_FLOOR, STOP_SPREAD_MULT·spread)` price-stop (the bleed).
- **Add — three gap-immune exits:**
  1. **Patience stop:** cut only if the adverse move **persists** ≥ `PATIENCE_S` seconds (a 2s gap that recovers is ignored). Threshold `PATIENCE_STOP` (~10c), well past the per-poll gap.
  2. **Persistent flip:** cut if the *confirmed* direction flips against us and **holds** ≥ `FLIP_PERSIST_S` seconds (not a single-tick opposite read).
  3. **Catastrophe cap:** hard cut at `CATASTROPHE` (~−25c) for the rare genuine collapse.

The patience/flip exits need short-lived state (when the adverse/opposite condition began). The scalper is currently a pure function; this state lives on the `Position` (e.g. `adverse_since`, `flip_since`) updated by the runner each tick, mirroring how `peak_bid` already works — keeping `decide()` a pure function of its inputs.

### 3.3 Parameters (initial, from the replay — tunable, not sacred)

```
direction_state:  PERSIST_TICKS = 3   (~6s)   PERSIST_WINDOW_S = 12
                  short horizon = ret_5s/ret_15s ; long horizon = ret_30s
scalp exit:       PATIENCE_STOP = 0.10   PATIENCE_S = 12   FLIP_PERSIST_S = 6   CATASTROPHE = 0.25
                  TAKE_PROFIT_PRICE, TRAIL_* = unchanged
```

These are starting values that produced +$26 in replay. They are explicit module constants (consistent with the current engine), tuned later against the live slip-logged forward run — **not** against the touch-optimistic replay (the stop-tuning lesson from `0b4c1a4`).

### 3.4 Persistence / observability

- Runner already persists `decision_price / fill_slip / entry_conviction / entry_ofi` on `crypto5m_active_positions` (separate paper DB `data/crypto5m/active_trader_v0.sqlite`; not the brain-indexed main DB). Add `entry_persistence` (ticks the direction held before entry) to that row and to the state JSON so the forward review can bucket PnL by how "confirmed" the entry was. One idempotent `ALTER TABLE ... ADD COLUMN` (same pattern as the existing migrations).
- `crypto5m_active_state` JSON gains `confirmed_side`, `persistence_ticks`, `horizons_agree` for the UI to show *why* the engine is waiting vs committed (the "memory" made visible).
- UI (`/api/crypto5m/active` + the 5m Binary tab desk) shows the confirmation state ("read forming: Up held 4s, horizons agree" vs "confirmed"). Display-only.

---

## 4. Testing

- **`direction_state` unit tests:** a wiggle (sign flips every tick) is never confirmed; a sustained same-sign sequence with multi-horizon + flow agreement confirms after exactly `PERSIST_TICKS`; a strong but single-tick spike does not confirm; order-flow disagreement blocks confirmation.
- **`scalp_trader` exit unit tests:** a 2s adverse gap that recovers does **not** trigger the patience stop; a sustained adverse move ≥ `PATIENCE_S` does; a single-tick opposite read does not flip-exit; a persistent flip does; catastrophe cap fires at the cap; take-profit/trailing unchanged.
- **Runner test:** extend the existing deterministic deferred-fill test — a confirmed-then-held sequence opens on confirmation (not on the first spike) and records `entry_persistence`.
- **Regression harness:** `scripts/crypto5m_exit_replay.py` (already built) is the entry×exit regression — re-run after parameter changes; the PERSIST+PATIENCE row must stay clearly best.
- Focused suite: `tests/test_crypto5m_direction_scalp.py tests/test_crypto5m_active_runner.py tests/test_crypto5m_live_desk.py` + a new `tests/test_crypto5m_direction_state.py`.

---

## 5. Validation / success criteria

- **Pre-merge:** all focused tests green; the replay shows PERSIST+PATIENCE ≫ today's INSTANT+CURRENT (it does: +26 vs −42).
- **Live (the real judge):** run the forward paper engine; after ~150 trades, the slip-logged review (`scripts/crypto5m_active_review.py`, extended to bucket by `entry_persistence` and exit kind) shows: take-profit win-rate preserved, stop-bleed gone, net per-trade ≥ 0 after real fills, and `fill_slip` within tolerance (the deferred-fill realism check). If the live net is negative despite the replay, the touch-optimism gap is the cause — investigate execution, don't re-tune on replay.

---

## 6. Risks

- **Replay is touch-optimistic / small sample (58 windows).** +$26 ranks the design; it does not prove live profitability. Mitigation: the live slip log is the arbiter; do not scale or claim edge off the replay.
- **Fewer trades (70 vs 174).** Selectivity slows data accrual. Accept it — quality over volume is the entire point; the forward run accrues regardless.
- **Parameter overfit.** PERSIST_TICKS / PATIENCE_S were picked on this capture. Mitigation: they're starting values; tune on the forward run, not the replay; keep them explicit constants.
- **State on `Position`.** Adding `adverse_since/flip_since/persistence` to the runtime position must survive restart-recovery (reconstruct conservatively as None → one persistence window of warmup, same as cold start). No correctness risk, only a brief re-confirm delay.

---

## 7. Rollout

1. Build `direction_state` + tests.
2. Switch `scalp_trader` entry to confirmed-only + PATIENCE exit + tests.
3. Wire the runner (instantiate `DirectionState`, thread exit-state on `Position`, persist `entry_persistence`); extend the deferred-fill test.
4. UI: surface the confirmation state (display-only).
5. Re-run the replay (regression) + full focused suite.
6. Restart the engine; land to main; let the forward run accrue; review by `entry_persistence` bucket at ~150 trades.
