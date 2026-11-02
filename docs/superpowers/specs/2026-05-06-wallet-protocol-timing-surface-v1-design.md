# Wallet Protocol — Timing Surface v1 Design

**Status:** approved at r2 with audit conditions applied (2026-05-06)
**Author:** atodorov20 + Claude
**Branch:** main, builds on commit `9a03140` (Completeness Surface v1)
**Predecessors:** Edge Surface v1 (`ef7dcd8`), Edge Surface v1.1 (`4657a54`), Exit Attribution v1 (`021c2af`), Completeness Surface v1 (`9a03140`)

**Revision log:**
- 2026-05-06 r0 — initial draft.
- 2026-05-06 r1 — review pushback applied:
  - §5: dropped `n_unmapped_fills > 0` from `coverage_low` forcing
    condition; explicit mixed-rule ordering note (mixed only after
    dominant rules fail).
  - §6: pre-resolution metric renamed `avg_captured_move_*` →
    `avg_last_sell_move_*` to make the "latest SELL approximation"
    explicit (not full size-weighted exit).
  - §6.1: removed asymmetric `entry − later` flip for NO. ADR-003
    guarantees `avgPrice` and `activity_rows[].price` are both
    held-token prices, so the long-token rule (`later − entry`) applies
    uniformly. Held-bucket travel uses universal payout convention
    (resolved token = $1 win, $0 loss) to avoid taking a position on
    `curPrice`'s coordinate.
  - §8.7: added "front-ran", "knew before the market", "pre-news edge",
    "early information edge", "latency edge" to forbidden-phrase list.
  - §9: added explicit NO-token sign-correctness test cases that lock
    the §6.1 fix.
- 2026-05-06 r2 — semantic-correctness audit accepted; required
  changes applied (see audit
  `2026-05-06-wallet-protocol-semantic-audit.md`):
  - §5: split `coverage_low` into three independent concepts —
    `force_underpowered` (gate), `coverage_caveats` (caveat tag list),
    `confidence_band` + `confidence_downgraded` (display). Unmapped
    fills no longer alone force `underpowered`; they fire a caveat and
    drop confidence one band.
  - §6: pre-resolution metric renamed `avg_last_sell_move_*` →
    `avg_sw_sell_move_*` (size-weighted). Per-position computation now
    uses size-weighted average SELL; fallback to latest SELL only when
    size is invalid, with explicit `sell_price_method` tracking. New
    aggregate dict fields: `confidence_band`, `confidence_downgraded`,
    `coverage_caveats`, and pre_resolution.sell_price_method_counts.
  - §6.1: rewritten to lock the raw owned-token movement rule
    (`later_price − entry_price`) for any outcome label. Added
    size-weighted SELL formula. Removed the latest-SELL terminology
    from the body except in fallback paths.
  - §9: replaced T1–T5 with the audit's locked oracle test set
    (NO-token raw movement, OTHER-token raw movement, size-weighted
    SELL beats dust-close, curPrice-coordinate invariance, unmapped-
    fills-caveat-without-force-underpowered).
  - §10: added explicit cross-references to the three followup tickets
    (R1, R2, R3) so they cannot quietly disappear.

---

## 1. Purpose

Add an explanatory render surface to the wallet protocol report that helps the
reader answer **why** a wallet may be profitable: holding through resolution,
active pre-resolution trading, both, or neither.

Timing Surface v1 is **descriptive and display-only**. It MUST NOT feed
`overall_verdict`, `gate_result`, `primary_edge_type`, or `copyability_note`.
Same closed-position universe → identical verdict signature regardless of
timing-surface presence or content (mirrors Completeness Surface v1's
verdict-invariance contract).

Timing Surface v1 operates **only on existing attributed data**. It MUST NOT:
- call `price_history_fn`,
- call CLOB `/prices-history`,
- introduce any new external lookups,
- add simulator or backtest logic,
- introduce per-grain attribution,
- alter verdict logic.

Strict "did the wallet enter before favorable repricing?" claims require
external price history and are explicitly deferred to v1.5 / H6 (see §10).

---

## 2. Scope (active in v1)

Captured-travel only. The surface describes price travel **already realized**
on closed positions, derived from `closed_positions` + `activity_rows`:

1. **Held-to-resolution wins** — distance from `avgPrice` to the resolved
   value (1.0 for YES wins / 0.0 for NO wins, both equivalently expressed in
   outcome-side terms). Reported as average captured travel.
2. **Pre-resolution profitable exits** — SELL price observed in
   `activity_rows` minus entry price (side-adjusted). Reported as average
   captured move on winning pre-resolution exits.
3. **Pre-resolution loss cuts** — same shape as above but with negative
   captured move; reports whether the wallet exited before full adverse
   resolution.
4. **Mixed behavior** — both buckets profitable with material n in each.
5. **Underpowered / unclear** — explicit refusal to force a timing
   explanation when the data does not support one.

---

## 3. Out of scope (deferred to v1.5 / H6)

These belong in a later enrichment layer; v1 must not implement them:

- Strict "entered before repricing" — requires comparing entry price to a
  later market price for unrealized positions.
- "How much did price move after entry, over what window?" — requires
  external `/prices-history` enrichment with caching + rate-limit handling.
- "Was the move repeated across enough positions?" — requires per-position
  forward-price lookups + statistical aggregation.
- Per-grain (leaf / nav) timing slices.
- Any verdict / gate / primary_edge_type integration.

The v1 surface reserves four fields for the strict path; see §6.

---

## 4. Inputs (already produced by upstream surfaces)

All inputs come from `WalletData` / Exit Attribution v1. No new fetch path.

| Input | Source | Notes |
|---|---|---|
| `closed_positions[]` | `WalletData` | `avgPrice`, `curPrice`, `outcome`, `realizedPnl`, `timestamp`, `conditionId` |
| `activity_rows[]` | `WalletData` | `side` BUY/SELL, `price`, `timestamp`, `conditionId`, `outcome`, `asset` |
| `activity_cap_hit` | `WalletData` | from REST 3000-row cap |
| `subgraph_fallback_used` | `WalletData` | C2 backfill flag |
| `walk_full_history` | `WalletData` | full-walk capture flag |
| `n_unmapped_fills` | `WalletData` | typed counterpart of ACTIVITY_SUBGRAPH_UNMAPPED |
| `attribution_coverage_low` | derived | True iff `n_attributed / n_total < 0.50` (Completeness Surface threshold) |

The per-position attribution category (`held_to_resolution_win` /
`pre_resolution_profitable_exit` / etc.) is recomputed inside `timing.py`
from the same primitives Exit Attribution uses (`exit_attribution.classify_position`),
so the two modules cannot drift in their categorization. No persistent
cross-module dependency on Exit Attribution's internal state.

---

## 5. Classification rule (locked, conservative)

Using the per-position attribution categories from `exit_attribution`:

```
n_attributed = n_total − n_insufficient_data
held_n       = held_to_resolution_win.n + held_to_resolution_loss.n
preres_n     = pre_resolution_profitable_exit.n + pre_resolution_loss_cut.n
held_pnl     = held_to_resolution_win.total_pnl + held_to_resolution_loss.total_pnl
preres_pnl   = pre_resolution_profitable_exit.total_pnl + pre_resolution_loss_cut.total_pnl
held_share   = held_n   / n_attributed   (when n_attributed > 0; else 0.0)
preres_share = preres_n / n_attributed   (when n_attributed > 0; else 0.0)

# r2: split into three independent concepts — gating, caveats, confidence.

# A. Gate (forces classification = "underpowered"). Tightened per audit
#    so that a single unmapped fill in an otherwise strong sample does
#    NOT erase classification.
force_underpowered = (
    n_attributed < 20
    or attribution_coverage_low                              # n_attributed/n_total < 0.50
    or (activity_cap_hit and not (subgraph_fallback_used or walk_full_history))
)

# B. Caveat tags (informational; never alone force underpowered).
#    Each tag is a stable string consumers can match on.
coverage_caveats: list[str] = []
if activity_cap_hit and not (subgraph_fallback_used or walk_full_history):
    coverage_caveats.append("ACTIVITY_TRUNCATED")
if n_unmapped_fills > 0:
    coverage_caveats.append("SUBGRAPH_UNMAPPED_FILLS")
if attribution_coverage_low:
    coverage_caveats.append("ATTRIBUTION_COVERAGE_LOW")

# C. Confidence band (display-only).
#    "high"          — no caveats fired and not forced underpowered.
#    "low"           — at least one coverage caveat fired but classification stands.
#    "underpowered"  — force_underpowered fired (downgrade to "underpowered" label).
confidence_downgraded = False
if force_underpowered:
    confidence_band = "underpowered"
elif coverage_caveats:
    confidence_band = "low"
    confidence_downgraded = True
else:
    confidence_band = "high"

# coverage_downgraded retained for back-compat with callers that look at
# it (the r1 single bool). True iff `force_underpowered` AND the cause
# was specifically a coverage caveat (not just n_attributed < 20).
coverage_downgraded = force_underpowered and (
    attribution_coverage_low
    or (activity_cap_hit and not (subgraph_fallback_used or walk_full_history))
)

# D. Classification rule. Explicit ordering: dominant-bucket rules MUST
#    evaluate before mixed. A wallet with 25 held wins and 5 profitable
#    pre-res exits is `holding_driven`, not `mixed` — both buckets have
#    material n and positive PnL, but the 0.70 share threshold lands
#    the dominant label first.

if   force_underpowered:
        classification = "underpowered"
        reason = (
            f"n_attributed={n_attributed} below floor 20"
            if n_attributed < 20
            else f"coverage caveats: {','.join(coverage_caveats)}"
        )
elif held_share   ≥ 0.70 and held_pnl   > 0:
        classification = "holding_driven"
        reason = f"held_share={held_share:.2f}, held_pnl=${held_pnl:.2f}"
elif preres_share ≥ 0.70 and preres_pnl > 0:
        classification = "trading_driven"
        reason = f"preres_share={preres_share:.2f}, preres_pnl=${preres_pnl:.2f}"
elif held_pnl > 0 and preres_pnl > 0 and held_n ≥ 5 and preres_n ≥ 5:
        # Reached only after both dominant rules fail.
        classification = "mixed"
        reason = f"held_n={held_n}, preres_n={preres_n}, both buckets profitable"
else:
        classification = "unclear"
        reason = "neither bucket dominates and no clear positive concentration"
```

Notes:
- `unresolved_or_unknown` positions count toward `n_attributed` (they are
  not `insufficient_data`) but contribute to neither `held_n` nor
  `preres_n`. A wallet dominated by `unresolved_or_unknown` therefore lands
  naturally in `unclear`.
- The 20-position floor and 0.70 share threshold are conservative on
  purpose. v1 prefers `unclear` / `underpowered` over a confident wrong
  label. These thresholds are documented locked here; future tuning
  belongs in a v1.1 design doc, not in code drift.
- **`n_unmapped_fills > 0` does NOT alone force `underpowered`.** A small
  number of unmapped fills in an otherwise strong sample should downgrade
  *confidence*, not erase the timing classification. The r2 split makes
  this explicit:
  - `force_underpowered` does NOT include `n_unmapped_fills > 0`.
  - `coverage_caveats` includes `SUBGRAPH_UNMAPPED_FILLS` when fills
    are unmapped, regardless of count.
  - `confidence_band` drops to `low` whenever any coverage caveat fires
    (including this one), with `confidence_downgraded = True`.
  - Classification stands (e.g. `holding_driven`) on top of a `low`
    confidence band — operator sees both label and confidence.
  - The audit suggested an optional materiality threshold
    (`n_unmapped_fills >= 5` OR `n_unmapped / n_activity_rows >= 0.05`)
    to promote unmapped fills to `force_underpowered`. **Not adopted in
    v1.** Materiality is already captured indirectly: when unmapped
    fills cause material attribution loss, positions land in
    `insufficient_data` and `attribution_coverage_low` fires (which
    IS in `force_underpowered`).
- **R10 Option A clarification:** the Completeness Surface's
  `ATTRIBUTION_COVERAGE_LOW` caveat fires from `n_with_resolved_attribution
  / n_total < 0.50`, where `n_with_resolved_attribution = held_n + preres_n`
  (excludes `unresolved_or_unknown` and `insufficient_data`). This is a
  *reliability signal* for operator-visible warnings and W2 copyability
  consumers. Timing Surface's `n_attributed` and `attribution_coverage_low`
  kwarg remain a *sample-size floor* that includes unresolved positions —
  preserving the §5 contract that unresolved-heavy wallets land in
  `unclear`, not `underpowered`. See R10 ticket
  (`docs/superpowers/specs/2026-05-10-r10-resolved-attribution-coverage.md`)
  for rationale.

---

## 6. Returned dict (shape locked, r2)

`compute_timing_surface(...)` returns `None` when `closed_positions` is
empty. Otherwise returns:

```jsonc
{
  // Classification + reason (per §5).
  "classification": "holding_driven|trading_driven|mixed|unclear|underpowered",
  "classification_reason": "<short string>",

  // Confidence + coverage state (r2 audit changes).
  "confidence_band": "high|low|underpowered",
  "confidence_downgraded": false,         // True iff confidence_band == "low"
                                          //   (any coverage caveat fired but classification stands)
  "coverage_downgraded": false,           // back-compat: True iff classification was forced
                                          //   underpowered specifically by a coverage caveat
                                          //   (NOT by n_attributed < 20 alone)
  "coverage_caveats": [                   // stable tag list; consumers can match on these
    // possible values (any subset, in this order):
    // "ACTIVITY_TRUNCATED",
    // "SUBGRAPH_UNMAPPED_FILLS",
    // "ATTRIBUTION_COVERAGE_LOW"
  ],

  // Counts.
  "n_attributed": 25,
  "n_total_closed": 30,

  // Held-to-resolution bucket — captured-travel uses universal payout
  // (resolved token = $1 win / $0 loss); coordinate-independent.
  "held_to_resolution": {
    "n": 22, "wins": 18, "losses": 4,
    "win_rate": 0.818, "total_pnl": 10230.0,
    "avg_captured_travel": 0.58           // mean(held_resolution_travel(p, cat)) per §6.1; null when n=0
  },

  // Pre-resolution bucket — captured-move uses size-weighted average SELL
  // (latest-SELL fallback only when size data is invalid).
  "pre_resolution": {
    "n": 3, "wins": 2, "losses": 1,
    "win_rate": 0.667, "total_pnl": 420.0,

    // Size-weighted captured move, partitioned by realizedPnl sign.
    // Each is null when its partition is empty.
    "avg_sw_sell_move_winners": 0.112,    // mean(sw_sell_move(p, rows)) over winners
    "avg_sw_sell_move_losers": -0.080,    // same over losers

    // Sell-method aggregate transparency. Counts how each per-position
    // sw_sell_move was computed, so consumers can spot fallback-heavy
    // surfaces.
    "sell_price_method_counts": {
      "size_weighted":         3,         // all SELLs had size > 0
      "latest_sell_fallback":  0,         // size missing/invalid → fell back to latest SELL
      "unavailable":           0          // no SELL events at all (shouldn't happen for pre_resolution_*)
    }
  },

  // Future strict-repricing reservation (v1.5 / H6). v1 sets these to the
  // literal values below — never populated, never faked. Kept on the surface
  // so consumers can detect the v1.5 path being live without schema migration.
  "future_repricing_available": false,
  "future_repricing_signal":    null,
  "future_repricing_window":    null,
  "price_history_source":       null
}
```

### 6.1 Captured-travel formula (locked, raw owned-token rule)

**Lock: the raw owned-token movement rule applies uniformly.** For any
outcome label (YES, NO, OTHER, candidate names, team names, country
names, Over/Under, esports rosters, anything categorical), the favorable
captured movement on the held token is:

```
captured_move = later_price − entry_price
```

where both `later_price` and `entry_price` are in **raw owned-token
coordinates** (held-token price space). **Do NOT** apply an asymmetric
`entry − later` flip for NO/Short. This is the exact class of mistake
the audit found in `flipper.py` FH6 (followup R1, see §10).

Justification: per ADR-003 and the audit's S2 finding,

- `closed_positions[].avgPrice` is the entry price of the **held token**
  (a NO position stores the NO token entry price; a YES position stores
  the YES token entry price; an OTHER position stores that outcome's
  token entry price).
- `activity_rows[].price = usdc_amt / outcome_amt` per
  `fetch.py:_normalize_order_fill` — the **held token's traded price**
  at the time of the BUY/SELL event. Held-token coordinate, label-
  agnostic.

Both are already in held-token coordinate. The long-token rule
(`later − entry`) is therefore correct for *any* outcome — including
multi-outcome categorical markets where a "Trump" token bought at 0.30
and sold at 0.55 yields `+0.25` favorable, exactly the same shape as
YES bought at 0.30 sold at 0.55.

If a future surface explicitly normalizes prices to YES/proposition
probability coordinates (it shouldn't, in v1), only then would
`NO: entry − later` apply. **v1 does not normalize.**

#### Pre-resolution bucket — `avg_sw_sell_move_*` (size-weighted)

Per audit R5, the v1 captured-move metric uses the size-weighted
average SELL price across all SELLs for the (cid, outcome) pair within
the position's lifetime — NOT the latest SELL by timestamp.
Justification: a 1-share dust close at $0.05 after a 99-share main
exit at $0.65 should not flip the reported move from `+0.25` to
`−0.35`. Size-weighting collapses the partial-fill ladder to its
true representative price.

```python
def sw_sell_move(
    position: Mapping,
    activity_rows: Sequence[Mapping],
) -> tuple[float | None, str, float | None, float | None, int]:
    """Returns (sw_move, sell_price_method, weighted_sell_price,
                last_sell_price, n_sell_fills).

    sw_move = weighted_sell_price - entry_price (raw owned-token rule).
    sell_price_method ∈ {"size_weighted", "latest_sell_fallback",
                          "unavailable"}:
      - "size_weighted":         all SELLs had size > 0; average is
                                  size-weighted.
      - "latest_sell_fallback":  at least one SELL had missing/invalid
                                  size; falls back to latest SELL by
                                  timestamp.
      - "unavailable":           no SELL events for this (cid, outcome);
                                  sw_move = None.
    """
    cid = position["conditionId"]; outcome = position["outcome"]
    entry = float(position.get("avgPrice") or 0.0)
    sells = [
        r for r in activity_rows
        if (r.get("side") or "").upper() == "SELL"
        and r.get("conditionId") == cid
        and (r.get("outcome") or "") == outcome
    ]
    n_sell_fills = len(sells)
    if n_sell_fills == 0:
        return (None, "unavailable", None, None, 0)

    # Last SELL by timestamp — used both as the fallback path AND as a
    # transparency field surfaced alongside the size-weighted price.
    last = max(sells, key=lambda r: int(r.get("timestamp") or 0))
    last_sell_price = float(last.get("price") or 0.0)

    # Size-weighted path: only valid when EVERY SELL has a positive size.
    sizes = [float(r.get("size") or 0.0) for r in sells]
    if all(s > 0 for s in sizes):
        prices = [float(r.get("price") or 0.0) for r in sells]
        weight_sum = sum(sizes)
        weighted_sell_price = (
            sum(p * s for p, s in zip(prices, sizes)) / weight_sum
        )
        return (
            weighted_sell_price - entry,
            "size_weighted",
            weighted_sell_price,
            last_sell_price,
            n_sell_fills,
        )

    # Fallback: at least one SELL had invalid size.
    return (
        last_sell_price - entry,
        "latest_sell_fallback",
        None,                     # weighted_sell_price unavailable
        last_sell_price,
        n_sell_fills,
    )
```

`avg_sw_sell_move_winners` = `mean(sw_move)` across positions
categorized `pre_resolution_profitable_exit` and where `sw_move` is
not None. `avg_sw_sell_move_losers` = same across
`pre_resolution_loss_cut`. Each is `null` when its partition is empty.

`pre_resolution.sell_price_method_counts` aggregates the `sell_price_method`
return value across all attributed pre-resolution positions, so consumers
can detect fallback-heavy surfaces.

#### Held-to-resolution bucket — `avg_captured_travel`

For held-to-resolution positions, `curPrice` is at resolution
(`exit_attribution._at_resolution(curPrice)` is True), but the field's
coordinate ambiguity (held-token price vs. YES probability) varies by
upstream code path — see audit R2 and followup
`2026-05-06-curprice-coordinate-empirical-resolution.md`. To **avoid
taking a position on `curPrice`'s coordinate**, v1 uses the universal
payout convention: a held token that wins resolves to $1; a held token
that loses resolves to $0. This is true for YES, NO, OTHER, and
categorical labels alike.

```python
def held_resolution_travel(position, attribution_category) -> float:
    """+ value = wallet captured price travel toward win; − value = lost entry."""
    entry = float(position.get("avgPrice") or 0.0)
    if attribution_category == "held_to_resolution_win":
        return 1.0 - entry        # token redeemed at $1
    if attribution_category == "held_to_resolution_loss":
        return -entry             # token redeemed at $0
    raise ValueError(...)
```

`avg_captured_travel` = `mean(held_resolution_travel(p, cat))` across
all positions whose attribution category is either
`held_to_resolution_win` or `held_to_resolution_loss`. `null` when the
bucket is empty.

This formulation:
- Is **coordinate-independent**: doesn't depend on whether `curPrice`
  is held-token price or YES probability (audit R2).
- Re-uses **exit_attribution's existing PnL-sign-based win/loss split**
  (already PnL-based per `exit_attribution.classify_position`), so it
  cannot drift from the attribution categorization.
- Works for any outcome label including categorical / OTHER, because
  resolution payouts are universal.

#### Held-to-resolution bucket — `avg_captured_travel`

For held-to-resolution positions, `curPrice` is at resolution
(`exit_attribution._at_resolution(curPrice)` is True), but the field's
coordinate ambiguity (held-token price vs. YES probability) varies by
upstream code path. To **avoid taking a position on `curPrice`'s
coordinate**, v1 uses the universal payout convention: a held token
that wins resolves to $1; a held token that loses resolves to $0. This
is true for YES, NO, OTHER, and categorical labels alike.

```python
def held_resolution_travel(position, attribution_category) -> float:
    """+ value = wallet captured price travel toward win; − value = lost entry."""
    entry = float(position.get("avgPrice") or 0.0)
    if attribution_category == "held_to_resolution_win":
        return 1.0 - entry        # token redeemed at $1
    if attribution_category == "held_to_resolution_loss":
        return -entry             # token redeemed at $0
    raise ValueError(...)
```

`avg_captured_travel` = `mean(held_resolution_travel(p, cat))` across
all positions whose attribution category is either
`held_to_resolution_win` or `held_to_resolution_loss`. `null` when the
bucket is empty.

This formulation:
- Is **coordinate-independent**: doesn't depend on whether `curPrice`
  is held-token price or YES probability.
- Re-uses **exit_attribution's existing PnL-sign-based win/loss split**
  (already PnL-based per `exit_attribution.classify_position`), so it
  cannot drift from the attribution categorization.
- Works for any outcome label including categorical / OTHER, because
  resolution payouts are universal.

#### Required tests (lock raw-token rule + size-weighted SELL)

See §9 for the full T1–T5 oracle suite. Three non-negotiable signs
that must hold for the NO-token / categorical / size-weighted paths:

1. NO held to resolution win (avgPrice=0.40, won): `held_resolution_travel
   = +0.60`.
2. NO pre-res profitable exit (avgPrice=0.40, sold 100 shares at 0.65):
   `sw_sell_move = +0.25`. Method = "size_weighted".
3. Dust-close protection: BUY 100 at 0.40, SELL 99 at 0.65 then SELL 1
   at 0.05. `sw_sell_move ≈ +0.244` (not −0.35). Method = "size_weighted".

If any sign comes out flipped, or the size-weighted path returns the
dust price, the test fails.

---

## 7. Plumbing

### 7.1 Types (`types.py`)

Add to `ProtocolReport`:

```python
# Timing Surface v1 — captured-travel-only timing classification.
# Display-only; NEVER feeds verdict, gate, or primary_edge_type.
# See docs/superpowers/specs/2026-05-06-wallet-protocol-timing-surface-v1-design.md.
timing_surface: dict | None = None
```

No `WalletData` change (no new capture-state field). No replay schema bump.

### 7.2 `analyze_wallet.py`

After `compute_attribution_summary(...)`:

```python
from sniper.analysis.wallet_protocol.timing import compute_timing_surface

ea = report.exit_attribution or {}
ea_summary = ea.get("summary") or {}
ea_coverage = ea.get("coverage") or {}
n_total = int(ea_coverage.get("n_total", 0) or 0)
n_insuf = int((ea_summary.get("insufficient_data") or {}).get("n", 0) or 0)
n_attributed = max(n_total - n_insuf, 0)
attribution_coverage_low = (n_total > 0) and ((n_attributed / n_total) < 0.50)

report.timing_surface = compute_timing_surface(
    data.closed_positions,
    data.activity_rows,
    activity_cap_hit=data.activity_cap_hit,
    subgraph_fallback_used=data.subgraph_fallback_used,
    walk_full_history=data.walk_full_history,
    n_unmapped_fills=data.n_unmapped_fills,
    attribution_coverage_low=attribution_coverage_low,
)
```

### 7.3 `report.py`

New helper `_timing_surface_block(report)` and a render call inserted
**after** the existing `_edge_surface_block(report)` call and **before**
`### Stage 1 result`. Block returns `[]` when `report.timing_surface is
None` (i.e. no closed positions).

---

## 8. Render layout

Section heading `## Timing surface`. Body is one interpretation paragraph
+ structured numbers + a note line. Concrete examples:

### 8.1 Holding-driven

```
## Timing surface

Classification: holding_driven
This wallet's gains came predominantly from holding positions through resolution.
Of 25 attributed positions, 22 (88%) were held-to-resolution and 18/22 resolved
in the wallet's favor (WR=81.8%, avg captured travel=58.0¢). Pre-resolution
exits contributed only $420 over 3 trades.

- Held to resolution: n=22 (18 win / 4 loss, WR=81.8%, avg captured travel=58.0¢), PnL=$10,230.00
- Pre-resolution exit: n=3 (2 win / 1 loss, WR=66.7%, avg observed exit move (winners)=11.2¢), PnL=$420.00
- Note: timing surface describes captured travel only. Strict "entered before
  repricing" analysis requires external price history and is not implemented in v1
  (`future_repricing_available=false`).
```

### 8.2 Trading-driven

```
## Timing surface

Classification: trading_driven
This wallet's gains came predominantly from pre-resolution exits. Of 28 attributed
positions, 23 (82%) closed via SELL before resolution; 17/23 of those were
profitable (WR=73.9%, avg observed exit move=14.1¢). Held-to-resolution positions
contributed −$45 over 5 trades.

- Held to resolution: n=5 (1 win / 4 loss, WR=20.0%, avg captured travel=42.0¢), PnL=−$45.00
- Pre-resolution exit: n=23 (17 win / 6 loss, WR=73.9%, avg observed exit move (winners)=14.1¢), PnL=$3,210.00
- Note: <same note as above>
```

### 8.3 Mixed

```
## Timing surface

Classification: mixed
Both holding and pre-resolution trading contributed to this wallet's edge.
Held-to-resolution: 12 trades, WR=83.3%, PnL=$2,150. Pre-resolution exits:
12 trades, WR=66.7%, PnL=$1,840. Neither pattern dominated.

- Held to resolution: n=12 (10 win / 2 loss, WR=83.3%, avg captured travel=46.0¢), PnL=$2,150.00
- Pre-resolution exit: n=12 (8 win / 4 loss, WR=66.7%, avg observed exit move (winners)=9.0¢), PnL=$1,840.00
- Note: <same>
```

### 8.4 Unclear

```
## Timing surface

Classification: unclear
Neither holding nor pre-resolution trading clearly explains this wallet's results.
Held-to-resolution and pre-resolution buckets each have material n but no
single bucket exceeds the share/PnL thresholds for a confident label.

- Held to resolution: n=14 (...), PnL=...
- Pre-resolution exit: n=10 (...), PnL=...
- Note: <same>
```

### 8.5 Underpowered

```
## Timing surface

Classification: underpowered
Timing evidence is limited — n_attributed=15 below the v1 floor of 20.
Captured-travel numbers below are reported but should not drive copy decisions.

- Held to resolution: n=8 (...)
- Pre-resolution exit: n=4 (...)
- Note: <same>
```

### 8.6 Underpowered + coverage_downgraded

```
## Timing surface

Classification: underpowered
Timing evidence is limited — Completeness caveats fired (capture truncated,
unmapped fills, or attribution coverage <50%). Captured-travel numbers below
are still reported but should not drive copy decisions. See `## Data completeness`.

- Held to resolution: n=... (...)
- Pre-resolution exit: n=... (...)
- Note: <same>
```

### 8.7 Allowed / forbidden phrasing

**Allowed:**
- "captured favorable price movement"
- "held through resolution"
- "exited profitably before resolution"
- "cut losses before final outcome"
- "timing evidence is limited"
- "timing surface is underpowered"

**Forbidden** (would imply strict-repricing or information-advantage
claims that v1 cannot back):
- "entered before repricing"
- "acted before the market"
- "had information advantage"
- "predicted the move early"
- "front-ran"
- "knew before the market"
- "pre-news edge"
- "early information edge"
- "latency edge"

A test asserts no rendered report on the v1 path contains any of the
forbidden phrases.

---

## 9. Tests (`tests/test_wallet_protocol_timing_surface.py`)

Test layout mirrors `tests/test_wallet_protocol_completeness_surface.py`.
Required cases include the **T1–T5 audit-locked oracle set** (see audit
§6) plus the structural cases inherited from r1.

### 9.A — T1–T5 audit oracles (non-negotiable; lock the audit findings)

```
T1 — NO-token raw movement sign (locks §6.1 raw-token rule)
   BUY NO at 0.40, SELL NO at 0.65.
   Expected sw_sell_move: +0.25.
   Method = "size_weighted".
   No `entry − later` flip applied.

T2 — Categorical / OTHER raw movement sign (label-agnostic rule)
   BUY outcome="Trump" at 0.30, SELL outcome="Trump" at 0.55.
   Expected sw_sell_move: +0.25.
   Method = "size_weighted".
   Confirms the rule applies to any outcome label, not just YES/NO.

T3 — Size-weighted SELL beats latest-SELL on dust closes (locks R5 fix)
   BUY 100 NO at 0.40.
   SELL 99 at 0.65 (timestamp 2000).
   SELL  1 at 0.05 (timestamp 3000).
   Expected weighted_sell_price ≈ 0.6440.
   Expected sw_sell_move ≈ +0.2440.
   sell_price_method_counts.size_weighted == 1.
   sell_price_method_counts.latest_sell_fallback == 0.
   Asserts: latest-SELL-only would have produced −0.35 (the dust price)
   — the new path must NOT return that.

T4 — curPrice-coordinate invariance (locks audit R2 routing)
   Build a held-to-resolution position set with all wins (avgPrice=0.40,
   realizedPnl>0). Build the "flipped" twin where every position's
   curPrice is replaced (1.0 ↔ 0.0). All other fields identical
   (especially realizedPnl, the source of truth for win/loss
   determination via exit_attribution).
   Expected: timing_surface dict is byte-for-byte identical between
   the two runs. Proves Timing v1 does NOT depend on curPrice's
   coordinate.

T5 — Unmapped-fills caveat without force-underpowered (locks §5 split)
   Build a strong sample: 25 held wins + 5 pre-res profitable exits,
   n_attributed = 30, attribution_coverage_low = False,
   n_unmapped_fills = 1.
   Expected:
     - classification == "holding_driven"
     - confidence_band == "low"
     - confidence_downgraded == True
     - coverage_downgraded == False    # not forced underpowered
     - coverage_caveats == ["SUBGRAPH_UNMAPPED_FILLS"]
     - force_underpowered would have been False
   A single unmapped fill must NOT erase the timing classification.
```

### 9.B — Structural cases (inherited from r1)

1. **Plumbing** — `report.timing_surface` populated; future-reserved fields
   are literal `false` / `null`.
2. **Verdict invariance** — same closed-positions universe with vs without
   the timing surface block computed → identical
   `(overall_verdict, gate_result, primary_edge_type, copyability_note)`.
3. **Classification matrix** —
   - Holding-driven (25 held wins, 5 pre-res profitable exits)
   - Trading-driven (25 pre-res profitable, 5 held)
   - Mixed (12 held profitable + 12 pre-res profitable)
   - Unclear (held losing, pre-res losing — neither share ≥ 0.70 with positive PnL)
   - Underpowered via `n_attributed = 15` → "n_attributed=15 below floor 20"
   - Underpowered + `coverage_downgraded=True` (cap_hit + no fallback + ≥50% positions outside window)
   - **Mixed-rule does NOT pre-empt holding_driven** (25 held + 5 pre-res
     profitable: must classify as `holding_driven`, not `mixed`).
4. **No external lookup** — assert no `requests.Session.get` (or any
   `requests` HTTP call) during `compute_timing_surface(...)` execution.
5. **Render** —
   - section appears after `## Edge surface` and before `### Stage 1 result`
   - structured numbers visible for each non-empty bucket
   - allowed-phrasing-only check: no forbidden phrase appears in rendered output
6. **Coverage downgrade phrasing** — `coverage_downgraded=True` path
   renders the explicit downgrade paragraph and references
   `## Data completeness`.
7. **Confidence band rendering** — `confidence_band="low"` path renders
   a clearly distinct "confidence: low" line that does NOT confuse the
   operator into thinking the classification is `underpowered`.

### 9.C — Total scope

Target: ~17 cases (T1–T5 oracle locks + 12 structural cases). Names and
sign-correctness are the audit's hard contract; the structural cases
mirror Completeness Surface v1's 14-case shape with two additions
(confidence band rendering, sell_price_method_counts).

---

## 10. Future enrichment (Timing Surface v1.5 / H6)

Strict repricing analysis is deliberately deferred. v1.5 will be built
once **all** of the following preconditions hold:

1. Completeness Surface v1 stable in production reports.
2. Exit Attribution v1 stable.
3. Timing Surface v1 stable and verdict-invariant under live runs.
4. Clean analyzer intake deployed (no contamination from legacy low-buy streams).
5. Batch runs are recoverable and not contaminated by legacy wallet sets.
6. A dedicated price-history adapter exists with caching, rate-limit
   handling, and dedicated tests (separate from the H6 ad-hoc adapter).
7. Validation evidence that CLOB `/prices-history` is reliable enough for
   the markets we analyze (per-market coverage audit).

When v1.5 is implemented, it will populate the four reserved fields:

- `future_repricing_available: true`
- `future_repricing_signal`: structured per-position summary (forward
  price move, window length, side-adjusted)
- `future_repricing_window`: e.g. `{"horizon": "24h", "fidelity": 60}`
- `price_history_source`: `"clob.polymarket.com/prices-history"`

The v1.5 surface answers:
- Did the wallet enter before favorable market movement?
- How much did price move after entry?
- Over what window?
- Was the move repeated across enough positions?
- Was the signal strong after controlling for coverage and sample size?

v1.5 must remain verdict-invariant in the same sense as v1: even when the
strict signal is populated, it MUST NOT drive `overall_verdict`,
`gate_result`, or `primary_edge_type` without an accompanying
methodology spec and BH pool integration (i.e. promoting it to H6's
status), which is its own separate scope.

### 10.1 Followup tickets (audit findings — must not disappear)

The semantic-correctness audit (`2026-05-06-wallet-protocol-semantic-audit.md`)
identified three pre-existing issues in adjacent code that Timing
Surface v1 routes around but that must not be forgotten. Each has its
own ticket spec — implementation can land independently and in any
order.

| Ticket | Subject | Severity | Spec doc |
|---|---|---|---|
| **R1** | Fix FH6 NO-side flip in `flipper.py:417-419` before v1.5 wires `price_history_fn` | critical | `2026-05-06-fh6-no-side-flip-fix.md` |
| **R2** | Resolve `closed_positions[].curPrice` coordinate empirically against live API | high | `2026-05-06-curprice-coordinate-empirical-resolution.md` |
| **R3** | Split `stage1_gates._count_open_dead` into dead-against vs unredeemed-winners (depends on R2) | high | `2026-05-06-stage1-f1-open-dead-split.md` |

Hard rules:

1. **R1 must land before any caller wires a real `price_history_fn`
   into the flipper branch.** Until R1 is fixed, FH6 will silently
   invert `with_market` vs `against_market` for NO flips the moment
   it has a real price source. Treating R1 as a v1.5 prerequisite is
   the cleanest enforcement.
2. **R2 should be resolved before any new surface consumes
   `closed_positions[].curPrice` directly for a magnitude (not a
   `_at_resolution` truthy check).** Timing v1 explicitly does not
   consume curPrice for magnitude (universal-payout path) and
   therefore does not block on R2.
3. **R3 depends on R2** and can land in the same commit as the R2
   resolution.

These tickets must be tracked in the same docs tree as this spec, with
their own commits. Do not bury them in a Timing v1 changelog.

---

## 11. Files touched

| File | Change |
|---|---|
| `sniper/analysis/wallet_protocol/timing.py` | new module — `compute_timing_surface()` |
| `sniper/analysis/wallet_protocol/types.py` | `+ timing_surface: dict \| None = None` on `ProtocolReport` |
| `sniper/analysis/wallet_protocol/analyze_wallet.py` | wire `compute_timing_surface()` after `compute_attribution_summary()` |
| `sniper/analysis/wallet_protocol/report.py` | `+ _timing_surface_block(report)` and call after `_edge_surface_block(...)` |
| `tests/test_wallet_protocol_timing_surface.py` | new test file — ~14 cases per §9 |
| `docs/superpowers/specs/2026-05-06-wallet-protocol-timing-surface-v1-design.md` | this spec |

No verdict logic touched. No new external calls. No simulator. No
per-grain attribution. No replay schema bump. No `WalletData` change.

---

## 12. Acceptance criteria (verbatim from approved scope)

- No new external price-history calls.
- No simulator logic.
- No per-grain attribution.
- No verdict logic changes.
- Timing Surface v1 is based only on currently available attributed fills/positions.
- Report language is careful and does not overclaim "future repricing"
  without price-history evidence.
- Future strict repricing support is documented but not implemented.
- Dedicated tests cover:
  - holding-driven wallet
  - trading-driven wallet
  - mixed wallet
  - unclear / underpowered wallet
  - low attribution coverage confidence downgrade
  - no external price-history call path
- Full repo suite green; calibration suite green.

---

## 13. Verification plan

After implementation:

1. `python3 -m pytest tests/test_wallet_protocol_timing_surface.py -q` — all new cases green.
2. `python3 -m pytest tests/ -q` — full repo suite green (target: existing
   `4106 passed, 16 skipped` plus the new ~14 cases, all pass).
3. Calibration suite green (`22 passed, 20 skipped` baseline must hold).
4. `python3 scripts/validate_schema_refs.py` — clean.
5. Smoke render on the YatSen fixture (0x5bff…d6ffbe): expect
   `classification = "underpowered"` with `coverage_downgraded = True`
   (because Completeness Surface v1 already reports
   `ATTRIBUTION_COVERAGE_LOW` at 12.3%). Verdict signature must equal the
   pre-Timing-Surface run.
