# Spec — Null-Strategy Baseline Surface (`null_baseline_surface`)

**Date:** 2026-05-22
**Status:** design — approved, ready for implementation plan
**Origin:** Item 4 of the analyzer "shortsightedness" backlog. The analyzer
reports a wallet's return but never asks the counterfactual: *what would a
dumb strategy have returned on the same markets?* A wallet can look
profitable purely by riding the chalk (always betting favorites in markets
where favorites win often). Third display-only surface, after
`capital_risk_surface` and `sizing_outcome_surface`.

## 1. Problem

"This wallet made money" ≠ "this wallet has skill." On Polymarket, always
betting the favorite is itself a positive-looking strategy in many market
classes. To separate **side-selection skill** from **riding the odds**, the
surface compares the wallet's flat-staked return, on the exact markets it
traded, against three null strategies: always-favorite, always-underdog,
and random side. The wallet's edge over chalk = its return minus the
always-favorite return.

It is self-contained — no external base-rate data. For a *resolved* binary
position the **hold-to-resolution** ROI is pure arithmetic: bought at
`avgPrice`, resolves to 1 (win) or 0 (loss), so `roi =
(1−avgPrice)/avgPrice` if the side won, `−1` if it lost. The favorite is
the side priced `> 0.50`. Everything derives from `avgPrice` + the
resolution.

**Important — what the wallet's number is.** `wallet_ew_roi` scores the
wallet's *chosen side* on this same hold-to-resolution basis. For a
position the wallet held to resolution that equals its realized return;
for an early-exited position (the wallet sold before resolution, the
market later resolved) it does NOT — it is the return the chosen side
*would* have produced held to resolution. This is intentional: a pure
side-selection comparison must score the wallet's side and the baseline
sides on the *same* hold-to-resolution basis. Exit timing is a different
skill, measured elsewhere; this surface does not credit or debit it.

## 2. Scope

One display-only surface, `null_baseline_surface`, on `ProtocolReport`.
Pure function of `closed_positions`. It measures **side-selection** skill,
flat-staked — it deliberately does NOT credit/debit market selection
(the wallet already chose its markets) or sizing (that is
`sizing_outcome_surface`'s job). **Out of scope:** copy-latency decay,
survivorship — and any promotion of "beat the chalk" to "skill".

## 3. Surface schema

`report.null_baseline_surface: dict | None` — `None` only on the
pre-surface backward-compat path. When produced, a dict:

| Key | Type | Meaning |
|---|---|---|
| `surface_version` | int | schema version (1) |
| `data_state` | str | `ok` \| `proxy_only` \| `underpowered` \| `data_unavailable` (§6) |
| `comparison` | str | `beats_chalk` \| `matches_chalk` \| `underperforms_chalk` \| `inconclusive` (§5.4) |
| `n_total_closed` | int | all closed positions |
| `n_eligible` | int | resolved binary positions used (§5.1) |
| `coverage` | float \| None | `n_eligible / n_total_closed` |
| `wallet_ew_roi` | float \| None | the wallet's CHOSEN side scored on a **hold-to-resolution** basis, equal-weighted flat-$1 ROI over eligible markets (see §5.2 — this is a side-selection measure, NOT the wallet's realized return) |
| `favorite_ew_roi` | float \| None | always-favorite, equal-weighted ROI, same markets |
| `underdog_ew_roi` | float \| None | always-underdog, equal-weighted ROI, same markets |
| `random_ew_roi` | float \| None | random side — the per-market midpoint of favorite/underdog, averaged |
| `edge_vs_favorite` | float \| None | `wallet_ew_roi − favorite_ew_roi` — **the headline** |
| `favorite_hit_rate` | float \| None | share of eligible markets where the favorite won (base-rate sanity) |
| `n_on_favorite` | int | eligible positions where the wallet bet the favorite (`avgPrice ≥ 0.50`) |
| `n_on_underdog` | int | eligible positions where the wallet bet the underdog |
| `caveats` | list[str] | surface-local honesty caveats (NOT `data_quality_flags`) |

Numeric fields are `None` (unknown), never a false `0.0`.

## 4. Constants

- `RESOLVED_WIN = 0.97`, `RESOLVED_LOSS = 0.03` — `curPrice` resolution
  thresholds (match the project's `RESOLUTION_*_THRESHOLD`).
- `PRICE_BAND = (0.02, 0.98)` — eligible `avgPrice` range.
- `MIN_ELIGIBLE = 20` — below this → `underpowered`.
- `COVERAGE_FLOOR = 0.50` — below this → `proxy_only`.
- `FAVORITE_DOMINANCE = 0.90` — wallet on the favorite above this share →
  `proxy_only` (almost no deviation from chalk to measure).
- `EDGE_BAND = 0.03` — `|edge_vs_favorite|` below this → `matches_chalk`.

## 5. Producer algorithm — `compute_null_baseline_surface()`

`sniper/analysis/wallet_protocol/null_baseline.py`:

```
compute_null_baseline_surface(closed_positions: list) -> dict
```

Pure; no I/O; deterministic.

### 5.1 Eligibility

A closed position is eligible when **all** of:
- **Fields present** — both `avgPrice` and `curPrice` are present and
  numeric. A missing field makes the position ineligible; it MUST NOT be
  coerced to `0.0` (a missing `curPrice` coerced to `0.0` would be
  silently misread as a resolved loss).
- **Resolved** — `curPrice >= RESOLVED_WIN` or `curPrice <= RESOLVED_LOSS`.
- **Binary-priced** — `avgPrice` strictly within `PRICE_BAND`. The band
  drops degenerate near-certain bets (where favorite/underdog is trivial)
  and the worst negative-risk price weirdness.
- **Not flagged negative-risk** — if the position row carries a truthy
  `negRisk` / `negativeRisk` field, exclude it (synthetic-short pricing
  violates the two-sides-sum-to-1 assumption §5.2 relies on). When no such
  field is present the `PRICE_BAND` plus the §7 disclosure Note are the
  only guard.

**Binary-condition assumption.** §5.2 treats the wallet's outcome and its
complement at `1 − avgPrice` as the two sides of a binary market. This
holds because every Polymarket CTF condition is binary at the protocol
level (multi-candidate markets are negative-risk bundles of binary
sub-markets, so each closed position is still a YES/NO on one condition).
The surface does not attempt a single "favorite outcome" across a 3+-way
market — it compares the wallet's side against its own complement.

`n_eligible` counts eligible positions; `coverage = n_eligible /
n_total_closed`.

### 5.2 Per-eligible-market ROI

- `p_w = avgPrice` — the wallet's side price.
- `won = curPrice >= RESOLVED_WIN` — the wallet's side resolved to win.
- `p_fav = max(p_w, 1 − p_w)`, `p_dog = 1 − p_fav`.
- `wallet_on_favorite = p_w >= 0.50`. A coin-flip market (`p_w` exactly
  `0.50`) has no real favorite; the `>=` tiebreak classifies it as
  favorite. This is harmless and symmetric — at `0.50`, `p_fav == p_dog`
  and `wallet_roi == favorite_roi`, so a coin-flip contributes exactly
  `0` to `edge_vs_favorite`.
- `favorite_won = won if wallet_on_favorite else (not won)` — binary
  market: exactly one side wins.
- `roi(p, side_won) = (1 − p) / p if side_won else −1.0`.
- `wallet_roi = roi(p_w, won)` — equals `favorite_roi` on markets where the
  wallet picked the favorite, `underdog_roi` where it picked the underdog.
- `favorite_roi = roi(p_fav, favorite_won)`.
- `underdog_roi = roi(p_dog, not favorite_won)`.
- `random_roi = (favorite_roi + underdog_roi) / 2`.

### 5.3 Aggregation

Equal-weighted (flat $1 per market) means over the eligible set:
`wallet_ew_roi`, `favorite_ew_roi`, `underdog_ew_roi`, `random_ew_roi`.
`edge_vs_favorite = wallet_ew_roi − favorite_ew_roi`.
`favorite_hit_rate = mean(favorite_won)`.
`n_on_favorite` / `n_on_underdog` partition the eligible set by
`wallet_on_favorite`.

Equal-weighting BOTH the wallet and the baselines is deliberate: it isolates
side selection. Sizing is held constant — it is `sizing_outcome_surface`'s
concern, not this one.

### 5.4 `comparison` label

- `inconclusive` — whenever `data_state != "ok"`. A beats/underperforms
  verdict is only honest on a clean surface: an underpowered sample, a
  low-coverage slice, or a wallet that barely deviated from chalk
  (favorite-dominance) cannot support a side-selection judgment.
- `beats_chalk` — `data_state == "ok"` and `edge_vs_favorite > EDGE_BAND`.
- `underperforms_chalk` — `data_state == "ok"` and
  `edge_vs_favorite < −EDGE_BAND`.
- `matches_chalk` — `data_state == "ok"` otherwise.

The ROI numbers (`*_ew_roi`, `edge_vs_favorite`, `favorite_hit_rate`) are
still computed and emitted whenever `n_eligible >= 1` — they are
arithmetically valid even when thin; `data_state` and `comparison` carry
the trust signal. They are `None` only when `n_eligible == 0`.

Rendered as "beat / matched / underperformed the chalk," never "skill".

## 6. `data_state` — worst-wins

Explicit severity order (worst wins): `data_unavailable` > `underpowered`
> `proxy_only` > `ok`.

- `data_unavailable` — no closed positions, OR `n_eligible == 0`.
- `underpowered` — `n_eligible < MIN_ELIGIBLE`.
- `proxy_only` — `coverage < COVERAGE_FLOOR` (the baseline sees only a
  slice of the wallet), OR `n_on_favorite / n_eligible > FAVORITE_DOMINANCE`
  (the wallet barely deviates from chalk — `edge_vs_favorite` is near
  structurally zero and not meaningful). Each adds a named caveat.
- `ok` — none of the above.

## 7. Hard wall — non-negotiable

Identical to `capital_risk_surface` / `sizing_outcome_surface`:
- **Display-only.** Read ONLY by the report renderer. MUST NOT feed
  `overall_verdict`, `gate_result`, `primary_edge_type`,
  `copyability_note`, branch/hypothesis logic.
- **No `data_quality_flags` append.** Caveats live in `caveats`.
- **No live I/O.** Pure function of already-fetched `closed_positions`.
- The display-only guarantee is the hard wall — no verdict/gate code reads
  the field (grep-verified + a garbage-stub mutation-proof test) — NOT the
  producer call's position in `analyze_wallet`.
- `types.py` carries the field with the standard hard-wall comment.

## 8. Wiring

- `types.py` — `null_baseline_surface: dict | None = None` on
  `ProtocolReport`, hard-wall comment.
- `analyze_wallet.py` — one call alongside the `sizing_outcome_surface`
  call, fed `data.closed_positions`.
- `report.py` — a `## Null-strategy baseline` markdown block. Its rendered
  Note states the structural limitation: the favorite/underdog split
  assumes a binary market whose two sides sum to ≈ 1 — a spread /
  negative-risk approximation — and the surface measures side selection
  only, not market selection or sizing.

## 9. Acceptance tests — `tests/test_wallet_protocol_null_baseline_surface.py`

1. **Eligibility** — an unresolved position (`curPrice` mid-range) and an
   `avgPrice = 0.99` position are both excluded; both still count in
   `n_total_closed` / `coverage`.
2. **ROI math** — a favorite bought at 0.60 that won → `wallet_roi` and
   `favorite_roi` = `(1−0.6)/0.6 ≈ 0.667`; `underdog_roi = −1.0`.
3. **Wallet on the underdog** — `avgPrice = 0.30`, lost → `wallet_roi =
   −1.0`, equals `underdog_roi`; `favorite_won` is True.
4. **Always-favorite wallet** — every eligible position has `avgPrice ≥
   0.50` → `edge_vs_favorite ≈ 0`, `data_state = proxy_only`
   (favorite dominance), AND `comparison = inconclusive`.
5. **Underdog edge** — a clean (`data_state == "ok"`) wallet that bet
   underdogs that hit beats the favorite line → `edge_vs_favorite > 0`,
   `comparison = beats_chalk`.
6. **`comparison` labels** — `beats_chalk`, `underperforms_chalk`,
   `matches_chalk` each reachable on an `ok` surface; and `inconclusive`
   whenever `data_state != "ok"`.
7. **`data_state`** — `data_unavailable` (no eligible), `underpowered`
   (< 20 eligible), `ok` (clean).
8. **Missing `curPrice`** — a position with a missing `curPrice` is
   excluded, NOT scored as a resolved loss.
9. **negative-risk exclusion** — a position carrying a truthy
   `negRisk` / `negativeRisk` field is excluded from `n_eligible`.
10. **Division guards** — `avgPrice` at the band edges, no divide-by-zero,
    no crash on empty input.
11. **`favorite_hit_rate`** — matches the constructed base rate.
12. **Hard wall** — a garbage-stub producer leaves verdict / gate /
    primary_edge_type / copyability_note / `data_quality_flags`
    byte-identical (mutation-proof).
13. Field present + dict-shaped on the live path; `None` only pre-surface.
14. Replay determinism — same input → byte-identical surface JSON.
15. Backward-compat — a `ProtocolReport` built without the field → `None`.
16. **Low coverage** — many ineligible positions → `coverage < 0.50` →
    `data_state = proxy_only`, `LOW_COVERAGE` caveat, `comparison`
    inconclusive.
17. **Missing / out-of-range `avgPrice`** — a missing `avgPrice`, and a
    negative or `> 1` `avgPrice`, are all excluded — no crash.
18. Scope-guard field-set tests (`test_wallet_protocol_honesty_floor.py`,
    `test_wallet_protocol_tag_grain_rescue_floor.py`) updated to include
    `null_baseline_surface`.

## 10. Out of scope

Copy-latency decay · survivorship correction · market-selection
attribution · sizing attribution · any promotion of "beat the chalk" to
"skill" · any verdict / gate / Strategy-Pattern-Audit coupling. The
surface is self-contained and stops at display.
