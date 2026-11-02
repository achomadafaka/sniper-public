# Memecoin Copy Mirror — Copy Logic Design

**Date:** 2026-07-05
**Status:** Draft for review (brainstorming → spec)
**Depends on:** `2026-07-05-memecoin-copy-mirror-design.md` (v0 shipped). Refines the trading logic only. Paper-only; no live execution.
**Concurrency note:** `sniper/services/memecoin_runner.py` and `sniper/persistence/db_memecoin.py` are being edited in parallel by the UI-integration build (heartbeat, `set_event_action`, `list_recent_events`, `wallet_stats`). This spec's changes must **merge with** that work — see §Integration points. Do not clobber the heartbeat/event-action additions.

---

## Decisions (operator-confirmed)

| Dimension | Choice |
|---|---|
| Entry selectivity | Copy **every** curated `large_buy` |
| Averaging | **None** — one open position per `(trader, mint)`; re-entry allowed only after a full exit |
| Chase guard | **Skip** if our entry quote is **>15%** above the trader's fill price |
| Exit | **Whichever first:** trader sells (mirror) · **−60%** stop-loss · **+500%** take-profit |
| Time cap | **None** |
| Position cap | **None** |
| Sizing | Fixed **$100** paper (unchanged) |

---

## 1. Entry gate — `on_copy_event`, `side == "buy"`

Evaluate in order; the first that matches wins and is recorded as the event `action`:

1. **Open dup** — `get_open_position(trader, mint)` exists → `"skipped_dup"` (no averaging). *Re-entry after a full exit is automatic: only OPEN positions block, so a fresh buy after close opens a new position — no code change needed.*
2. **Chase guard** — quote our honest fill (`honest_fill(mint, size, "buy", their_price)` → `(price, source)`). If `their_price > 0` **and** `price > their_price * (1 + CHASE_MAX_PCT/100)` → `"skipped_chase"` (don't open; we're buying the pump they caused).
   - **Measure the counterfactual.** A skip that would have won is a *cost* of the guard — we must be able to detect that. On `skipped_chase`, record on the event: the would-be entry `price`, `their_price`, and `chase_gap_pct = (price/their_price - 1)*100` (new nullable `memecoin_events` columns `wouldbe_price` + `chase_gap_pct`, idempotent). A later analysis (out of scope here) checks how skipped tokens moved after — if they mostly kept running, the guard is too tight. Without this we can't tune `CHASE_MAX_PCT` honestly.
3. **Open** — insert position at `(price, source)`, fixed `$N`, entry fill = the same quote. Action `"opened"`.

Notes:
- The chase quote and the entry fill are the **same** `honest_fill` call — reuse it, don't quote twice.
- If `their_price` is 0/unknown, chase guard is skipped (open normally) — never block on missing data.

## 2. Exit — two independent paths, whichever fires first

Every close writes `exit_reason ∈ {mirror, stop_loss, take_profit, manual}` on the position.

**Path A — mirror (feed-driven, existing `on_copy_event` sell branch):** curated trader sells that token → close at an honest sell fill; `exit_reason = "mirror"`.

**Path B — brackets (NEW, `evaluate_brackets(conn, price_map, now_iso)`):** for each open position, using a current price:
- `pnl_pct = (current_price / entry_price - 1) * 100` (guard `entry_price > 0`).
- `pnl_pct <= -STOP_LOSS_PCT` → trigger `stop_loss`; `pnl_pct >= TAKE_PROFIT_PCT` → trigger `take_profit`.
- On trigger: sell the **remaining** qty `m.open_qty(conn, pid)` (NOT entry qty — a prior partial_exit already reduced it) at `current_price`, insert a `bracket_exit` fill, then close with realized = `m._realized_from_exits(conn, pid, entry_price)` (sums all exit fills incl. partials) and the `exit_reason`.

**⚠ Refinement — brackets use `open_qty`, not `entry_qty`.** The parallel P1 build added partial exits, so `entry_qty != remaining`. Every bracket/mirror close MUST fill `open_qty` and realize via `_realized_from_exits`, or a partially-sold position double-counts. (This is the load-bearing fix from spec review.)

**⚠ SL/TP are cadence-checked trigger thresholds, not guaranteed fill prices.** Brackets are evaluated every ~15s; between checks the price can gap through the level. The `bracket_exit` fills at the **actual `current_price` at the check**, which may be worse (or better) than exactly ±the threshold — that is the honest model (paper). Document this in code + UI; a real venue would fill even further from the trigger on a gap. Do not pretend the SL fills at exactly −60%.

Manual close (existing endpoint) sets `exit_reason = "manual"`.

**Thread `exit_reason` through ALL close paths** (reconcile with the parallel P1 build): `on_copy_event` mirror → `"mirror"`; `evaluate_brackets` → `"stop_loss"`/`"take_profit"`; `partial_exit`'s final close → `"partial"` (or the reason that triggered it); the route's manual close → `"manual"`. `close_position(...)` gains an optional `exit_reason=None` param used by every caller.

## 3. Bracket evaluation cadence — rate-safe

Brackets need a current price for every open mint, but DexScreener rate-limits (~300/min) and the poll loop runs ~1.5s. So:
- Evaluate brackets on a **separate ~15s cadence**, not every poll cycle.
- Fetch prices with **one batched DexScreener call** for all open mints: `GET /latest/dex/tokens/{mint1,mint2,…}` (up to 30 addresses per request; chunk if more). Build a `{mint: price}` map, pass to `evaluate_brackets`.
- The existing marks endpoint (`mark_positions`) already fetches per-mint; refactor it to consume the same batched `price_map` so the UI marks and the bracket evaluator share one fetch.
- 15s SL/TP latency is acceptable for **paper** (documented limitation; live would need faster + a real price feed).

## 4. Config — `sniper/settings/memecoin.py`

```python
MEMECOIN_CHASE_MAX_PCT   = float(os.getenv("MEMECOIN_CHASE_MAX_PCT",   "15"))
MEMECOIN_STOP_LOSS_PCT   = float(os.getenv("MEMECOIN_STOP_LOSS_PCT",   "60"))
MEMECOIN_TAKE_PROFIT_PCT = float(os.getenv("MEMECOIN_TAKE_PROFIT_PCT", "500"))
# time cap / position cap intentionally omitted (operator chose none)
```

## 5. Schema — one idempotent migration

Idempotent migrations (mirror the `action`-column pattern the parallel build already added to `memecoin_events`):
```python
pos_cols = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_positions)")}
if "exit_reason" not in pos_cols:
    conn.execute("ALTER TABLE memecoin_positions ADD COLUMN exit_reason TEXT")
ev_cols = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_events)")}
for col in ("wouldbe_price", "chase_gap_pct"):
    if col not in ev_cols:
        conn.execute(f"ALTER TABLE memecoin_events ADD COLUMN {col} REAL")
```
`close_position(conn, position_id, closed_at, realized_pnl, exit_reason=None)` — add the optional param, default `None` (back-compat with the parallel build's existing callers). Run `brain_index.py generate` + `validate_schema_refs.py`.

## 6. Integration points (merge with the parallel UI build)

The parallel build already shipped on `feat/memecoin-ui-integration`: `partial_exit`, `manual_buy`, `open_qty`, `_realized_from_exits`, `get_position`, per-event `set_event_action`, `write_heartbeat`, `wallet_stats`, `list_recent_events`. Land the copy-logic engine changes **on top of** these — reconcile, don't rewrite:
- **Realized-PnL path already exists** — `close_position` callers use `_realized_from_exits(conn, pid, entry_price)`, which sums `partial_exit`/`mirror_exit`/`manual_close` fills. Add `bracket_exit` to that fill-kind list so bracket closes are counted.
- **`exit_reason`** — thread it through the existing close callers (`on_copy_event` mirror, `partial_exit`, the route manual close) plus the new `evaluate_brackets`; add the optional `close_position(..., exit_reason=None)` param.
- `run_once` — `set_event_action` already runs after `on_copy_event`; `skipped_chase` is just another return string, flows through unchanged. Also persist `wouldbe_price`/`chase_gap_pct` on the event when the action is `skipped_chase`.
- Runner loop — `write_heartbeat` runs per cycle; add the ~15s bracket pass with its own timer alongside it.
- `mark_positions` — refactor to accept an optional `price_map` (batched DexScreener fetch for all open mints) shared with `evaluate_brackets`; keep the no-arg behavior for tests. Note it already uses `open_qty` for unrealized PnL (parallel build) — good, brackets reuse that.
- Do NOT revert the heartbeat / `action` / `wallet_stats` / `list_recent_events` / `partial_exit` / `manual_buy` additions.

## 7. Testing (extend `tests/test_memecoin_*.py`)

- Chase guard: quote 16% over their fill → `skipped_chase`, no position; quote 10% over → `opened`; `their_price == 0` → `opened`.
- Re-entry: buy → close → buy again opens a NEW position (assert two rows, second open).
- Stop-loss: entry 0.005, price 0.002 (−60%) → `evaluate_brackets` closes with `exit_reason="stop_loss"`, correct realized PnL.
- Take-profit: entry 0.005, price 0.030 (+500%) → closes `take_profit`.
- No trigger: price −50% / +400% → stays open.
- Mirror still closes with `exit_reason="mirror"`; manual → `"manual"`; `partial_exit` final close → `"partial"`.
- **open_qty (load-bearing):** enter 2000 qty, `partial_exit` 50% (1000 left), then a stop-loss trigger → bracket fills the **remaining 1000**, and total realized via `_realized_from_exits` equals the sum of the partial + bracket slices (NOT recomputed on entry_qty). Assert no double-count.
- `skipped_chase` persists `wouldbe_price` and `chase_gap_pct` on the event row.
- `evaluate_brackets` uses an injected `price_map` (no network in tests).
- Batched fetch: unit-test the chunking (>30 mints → multiple requests) with an injected `http`.

## 8. Out of scope

Live execution / `armed=true`; per-trader or conviction-scaled sizing; trailing stops; time/position caps (operator chose none — add later only if exposure becomes a problem); faster-than-15s bracket latency (needs a real-time price feed, a live-trading concern).

## 9. Honest framing (unchanged)

Post-fill copy of the notification herd, large-trades-only. The brackets are risk management on an unproven edge, not a proven strategy. The `exit_reason` breakdown is the key diagnostic: if most closes are `stop_loss`, the chase/entry is bad; if `take_profit`/`mirror` dominate with positive PnL, the copy edge is real. Let the paper ledger decide.
