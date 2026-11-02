# Hyperlive Engine — design

A smart-but-deterministic ("flexible-A"), single-trade-at-a-time paper quant engine for
Hyperliquid. Researches all markets → selects ONE opportunity at whatever horizon scores
best → computes everything → forms a trade idea → enters and actively manages on paper →
closes → evaluates → loops. Fully observable on a new **hyperlive** UI tab. Paper only.
Not assumed profitable — a research/iteration harness whose value is computing correctly,
betting flexibly, and collecting structured outcome data we tweak between rounds.

## Pipeline state machine (each state shown on the UI)
`SCANNING → SELECTING → DESIGNING → ARMED → MANAGING → CLOSING → EVALUATING →` (loop)
Exactly one state at a time; one open position at a time. NO-TRADE returns to SCANNING.

## Flexible-A decision core (deterministic, no LLM, can't propose −EV)
1. **Setup library** — parameterized hypotheses: continuation, reversion, cluster-magnet,
   event-reaction, cross-venue-lead, breakout. Each scan evaluates every setup × asset ×
   horizon.
2. **Online calibration** — each setup's expected move, P(win), target/stop are estimated
   from rolling recent outcomes, not hardcoded. The engine tracks the market.
3. **Regime gating** — a classifier (vol, trend vs range, liquidation activity, funding
   extreme) decides which setups are live; calm → often NO-TRADE.
4. **EV-after-cost + sample gate** — `EV = P(win)·target − P(loss)·stop − round_trip_fee`,
   with confidence from sample size. Pick the highest EV that clears the fee with margin
   and passes a significance gate; else NO-TRADE.
5. **UCB bandit** — mostly exploits best setups, allocates a slice to under-sampled
   setup×regime cells to keep learning. This is the automated iteration loop.

The **catalyst agent** (world/news) is a *feature provider* only — emits structured
"catalyst on asset X" flags consumed by scoring; never the decision-maker.

## Layers / modules (sniper/hyperlive/)
- `models.py` — MarketSnapshot, Candidate, TradeIdea, Position, CycleRecord, EngineState.
- `compute.py` — per-asset MarketSnapshot from raw feeds (reuses leadlag primitives:
  microprice, OFI, executable marking, vol).
- `regime.py` — regime classifier.
- `setups.py` — setup library; each: `applicable(regime)`, `evaluate(snapshot, calib) → Candidate`.
- `calibration.py` — rolling online calibration of setup params from outcome history.
- `bandit.py` — UCB over (setup, regime) cells.
- `strategist.py` — scan → gate → bandit-select → TradeIdea | NO_TRADE.
- `cluster_map.py` — liquidation cluster-map lane: harvest addresses (trades feed) →
  poll `clearinghouseState` → per-asset liq heatmap. Exposed as a feature AND an
  **entry-timing helper** (assist ARMED→ENTER), never the sole trigger.
- `execution.py` — paper executor: executable fills (buy ask / sell bid + fee), active mgmt.
- `evaluation.py` — post-trade thesis-vs-reality scoring + iteration data.
- `engine.py` — the state-machine loop orchestrator.
- `persistence.py` — SQLite schema + writes (engine_state, cycles, ideas, positions,
  fills, evaluations, cluster_snapshots).
- `scripts/run_hyperlive_engine.py` — WS-fed live runner + fixture mode.

## Guardrails (non-negotiable, from this session)
Executable marking only; strategist can't propose a trade whose expected move doesn't
clear the round-trip fee with margin; no micro-momentum churn unless re-enabled; cluster-
map is helper not trigger; NO-TRADE is first-class; paper only, no keys/orders.

## UI — hyperlive tab
Clears the current Hyperliquid section. New `hyperlive` tab shows at all times: the live
pipeline state-strip (which stage now + elapsed), the SELECTED market's computed snapshot,
the candidate ranking + chosen setup with its EV math, the active TradeIdea + thesis +
plan, live position management (PnL vs target/stop), and the evaluation history (last N
cycles: thesis, outcome, right/wrong). Routes: `sniper/routes/api_hyperlive.py`
(`/api/hyperlive/state`, `/api/hyperlive/history`). app.js `hyperlive` module.

## Build order
- P1 substrate: models, compute, regime, setups(core), strategist (EV gate, no bandit yet),
  execution, engine loop, persistence — runnable end-to-end in fixture mode, paper-trades.
- P2 brain: online calibration + UCB bandit + cluster-map lane + catalyst feature.
- P3 surface: hyperlive UI tab + routes; evaluation depth; iteration tooling.

## Honest framing
v1 will mostly confirm the known nulls and collect data; its worth is the harness +
flexibility + the calibration/bandit learning loop we steer. Forward paper adjudicates.
