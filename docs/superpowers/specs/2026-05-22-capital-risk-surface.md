# Spec — Capital & Risk Surface (`capital_risk_surface`)

**Date:** 2026-05-22
**Status:** design — ready for TDD
**Origin:** Live-wallet validation of `0x6ade597c…` (TheOnlyHuman). The analyzer
reported throughput ROI **+0.77%** and "$21.4M redeemed", which read as
*mediocre* and *whale*. A prototype capital reconstruction showed the truth:
a **~$606K** capital base recycled **40×** for a **69% return on base**. The
analyzer measures endpoint PnL and per-trade ROI but not **path, denominator,
or capital structure** — so it could not tell a small-capital-feasible
strategy from a whale operation, nor a smooth winner from one that was deeply
underwater first.

## 1. Problem

Two questions the analyzer currently cannot answer for any wallet:

1. **How much capital did this actually take?** `net_pnl` is an endpoint;
   throughput ROI is per-trade. Neither gives the *capital base* (peak
   simultaneous USDC committed) — the number that decides whether a strategy
   is feasible at a given bankroll.
2. **How much capital did it take to *survive*?** We never compute the
   equity-curve drawdown. A wallet that made $419K monotonically and one that
   was $200K underwater first are different risks. Max drawdown is the
   survival bankroll, distinct from the deploy bankroll.

Plus a copyability question: is the PnL **broad** (many small edges) or
**concentrated** (a few outsized hits — fragile, unrepeatable)?

## 2. Scope

Add ONE display-only surface, `capital_risk_surface`, to `ProtocolReport`.
Pure function of data the analyzer already fetches. **In scope:** the seven
fields in §3. **Out of scope** (explicitly backlogged): sizing-skill
diagnostic, null-strategy baselines, copy-latency decay, survivorship
correction. None of those are touched here.

## 3. Surface schema

`report.capital_risk_surface: dict | None` — `None` only on the pre-surface
backward-compat path (a `ProtocolReport` built before this field existed).
When produced, a dict:

| Key | Type | Meaning |
|---|---|---|
| `data_state` | str | `ok` \| `proxy_only` \| `underpowered` \| `data_unavailable` (§5) |
| `peak_concurrent_deployed_usdc` | float \| None | Max simultaneous USDC committed across open positions — the capital base |
| `peak_deployed_at` | int \| None | Unix ts of the peak (lets the reader see whether the base was built up or present from the start) |
| `cumulative_deployed_usdc` | float \| None | Σ of all BUY cashflow — total throughput |
| `recycle_factor` | float \| None | `cumulative_deployed / peak_concurrent_deployed` — how many times the pool churned |
| `max_drawdown_usdc` | float \| None | Largest peak-to-trough decline of the realized-PnL equity curve |
| `max_drawdown_pct_of_peak` | float \| None | `max_drawdown_usdc / peak_concurrent_deployed_usdc` — drawdown as a fraction of the capital base (the survival-buffer ratio) |
| `return_on_peak_deployed` | float \| None | `net_pnl / peak_concurrent_deployed_usdc` — outcome on the real base |
| `pnl_concentration` | dict \| None | `{gini, top5_share_of_gross_profit, n_winning_positions, bucket}` — see §4.3 |
| `lifespan_days` | float \| None | First → last event span (context for `return_on_peak_deployed`) |
| `caveats` | list[str] | Surface-local honesty caveats (NOT `data_quality_flags`) |

Numeric fields are `None` (not `0.0`) when not computable — `None` means
unknown, `0.0` means a known zero (domain rule 4).

## 4. Producer algorithm — `compute_capital_risk_surface()`

`sniper/analysis/wallet_protocol/capital_risk.py`:

```
compute_capital_risk_surface(
    closed_positions: list, activity_rows: list,
    capital_ledger: dict | None, net_pnl: float,
    n_unmapped_fills: int = 0,
) -> dict
```

Pure; no I/O; deterministic (sorted iteration only).

### 4.1 Concurrent-deployment curve → peak, cumulative, recycle

Per-asset committed-capital model. Capital is tracked per outcome-token
(`asset`), not per condition — buying YES and NO of the same market are
distinct commitments.
- BUY trade → `committed[asset] += cost`; `shares[asset] += size`;
  `cumulative_deployed += cost`
- SELL trade → release the **cost basis of the shares sold**, NOT the sale
  proceeds: `released = committed[asset] * (shares_sold / shares_held)`.
  A losing sale's proceeds fall below cost basis (releasing proceeds would
  leave phantom committed capital); a winning sale's proceeds exceed it
  (releasing proceeds would over-release). Cost-basis fraction is correct
  both ways.
- redemption event → release all `committed[asset]` for every asset of the
  resolved condition (winner or loser; at resolution the capital is freed)
- `cost` per trade row: `usdcSize` if present and > 0, else `price * size`
  (REST rows carry `usdcSize`; subgraph-sourced rows carry `price`+`size`).
- `shares` per trade row: the `size` field; when absent/zero it is derived
  as `usdc / price` — applies symmetrically to BUYs (`usdc` = buy outlay)
  and SELLs (`usdc` = sale proceeds). A trade that still has no derivable
  share count is counted (`untracked_buys` / `untracked_sells`, the latter
  only when a position is held) → `proxy_only` + a surface-local caveat.
- Walk events sorted by `(timestamp, kind-order BUY<SELL<REDEEM)`. The
  running global sum's maximum is `peak_concurrent_deployed_usdc`;
  `peak_deployed_at` is its timestamp.
- `recycle_factor = cumulative_deployed / peak` (None if peak ≤ 0).

### 4.2 Realized-PnL equity curve → max drawdown

- Each closed position contributes `realizedPnl` at its **close time**:
  redemption-event ts for its `conditionId` if available, else parsed
  `endDate`, else the position `timestamp`. Record which basis was used.
- Sort contributions by close time; accumulate → equity curve.
- `max_drawdown_usdc` = max over the curve of `(running_peak_equity − equity)`.
  Always ≥ 0; `0.0` for a monotonic winner.
- `max_drawdown_pct_of_peak = max_drawdown_usdc / peak_concurrent_deployed_usdc`
  (None if peak ≤ 0).
- If any position's close time fell back to `endDate`/`timestamp` (no
  redemption ts), add caveat `DRAWDOWN_ORDERING_APPROXIMATE` and `data_state`
  cannot be better than `proxy_only`.

### 4.3 PnL concentration

Over per-position `realizedPnl`:
- `n_winning_positions` = count with `realizedPnl > 0`.
- `gini` = Gini coefficient of the winning positions' PnL (0 = perfectly
  even, →1 = one position holds it all). None if `n_winning_positions < 2`.
- `top5_share_of_gross_profit` = Σ(top 5 winners) / Σ(all positive PnL).
  None if no winners.
- `bucket`: `concentrated` if `top5_share ≥ 0.60`, `moderate` if `≥ 0.30`,
  else `broad`. `None` if undeterminable.

### 4.4 `return_on_peak_deployed`

`net_pnl / peak_concurrent_deployed_usdc` (None if peak ≤ 0). `net_pnl` is
passed in (authoritative — Σ closed-position realizedPnl); the surface does
not recompute it.

## 5. `data_state` semantics

Evaluated worst-wins:
- `data_unavailable` — no closed positions at all. Nothing is computed.
- `underpowered` — `< 20` closed positions, OR `n_winning_positions < 2`
  (concentration meaningless), OR **no usable trade cashflow** (the
  capital-base half is dead — peak/recycle/return are `None` — but the
  realized-PnL equity curve IS still computed from closed positions, so
  `max_drawdown_usdc` and `pnl_concentration` are emitted honestly; this is
  not `data_unavailable`, which would imply nothing was computed).
- `proxy_only` — computed, but with a degrading caveat: drawdown ordering
  approximate (§4.2), or `n_unmapped_fills > 0` (subgraph fills that could
  not be mapped to a condition → BUY/SELL cashflow is under-counted).
- `ok` — full clean computation, no degrading caveat.

When numeric fields cannot be trusted they are `None` (unknown), never a
false `0.0`. The dict is still emitted (never `None` itself on the live
path) for every wallet with ≥ 1 closed position.

## 6. Hard walls — non-negotiable

Identical contract to `liquidity_surface`:
- **Display-only.** `capital_risk_surface` is read ONLY by the renderer.
  It MUST NOT feed `overall_verdict`, `gate_result`, `primary_edge_type`,
  `copyability_note`, branch logic, or hypothesis evaluation.
- **No `data_quality_flags` append.** Caveats live in the surface's own
  `caveats` list. The top-level `data_quality_flags` is untouched.
- **No live I/O.** Pure function of already-fetched data.
- `types.py` carries the field with the standard hard-wall comment.

## 7. Wiring

- `types.py` — `capital_risk_surface: dict | None = None` on `ProtocolReport`,
  hard-wall comment.
- `analyze_wallet.py` — one call near the `liquidity_surface` call:
  `report.capital_risk_surface = compute_capital_risk_surface(...)`, fed from
  `data.closed_positions`, `data.activity_rows`, `data.capital_ledger`,
  `report.net_pnl`. Placed AFTER verdict/gate are finalized so it structurally
  cannot influence them.
- `report.py` — a `## Capital & Risk` markdown block + JSON passthrough.

## 8. Acceptance tests — `tests/test_wallet_protocol_capital_risk_surface.py`

1. Field present + dict-shaped on the live path; `None` only pre-surface.
2. Peak/cumulative/recycle correct on a hand-built BUY/SELL/REDEEM fixture.
3. SELL releases capped at committed (winning sell's profit excess is not a
   release).
4. Redemption releases remaining committed (held-to-resolution winner AND
   loser).
5. `usdcSize` vs `price*size` source handling — both yield identical curves.
6. Max drawdown = 0 for a monotonic winner; correct value for an
   underwater-then-recovered curve.
7. `max_drawdown_pct_of_peak` None when peak ≤ 0.
8. PnL concentration: `concentrated` when one hit dominates; `broad` when
   spread; gini None for `< 2` winners.
9. `data_state` transitions: `data_unavailable` (no closed), `underpowered`
   (< 20 closed), `proxy_only` (approximate drawdown ordering / unmapped
   fills), `ok` (clean).
10. Division guards: peak = 0, zero net_pnl, single position.
11. **Hard wall:** verdict / gate / primary_edge_type / copyability_note /
    `data_quality_flags` are byte-identical with and without the surface
    (mutation-proof, mirrors the liquidity-surface scope test).
12. Replay determinism: same fixture → byte-identical `capital_risk_surface`
    JSON across two runs.
13. Backward-compat: a fixture predating the surface analyzes cleanly.
14. Scope-guard field-set tests (`test_wallet_protocol_honesty_floor.py`,
    `test_wallet_protocol_tag_grain_rescue_floor.py`) updated to include
    `capital_risk_surface` in the expected `ProtocolReport` field set.

## 9. Out of scope (backlogged, do NOT build here)

Sizing-skill diagnostic · null-strategy baselines (favorite/random/
market-implied) · copy-latency decay · survivorship correction · any
survivorship caveat that touches verdict semantics or Strategy Pattern Audit
labels. The capital surface is self-contained and stops at display.
