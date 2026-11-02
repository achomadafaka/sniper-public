# Spec — Position Reconciliation Surface (`position_reconciliation_surface`)

**Date:** 2026-05-23
**Status:** design — ready for implementation plan
**Origin:** Item 6 of the analyzer "shortsightedness" backlog, surfaced
empirically while investigating wallet `0x20ad75e19b…879c9a`. The wallet's
`/positions` snapshot returned `open_positions = 0` and
`/closed-positions` returned 10,608 rows (= 10,580 unique conditionIds);
the activity-subgraph reported 12,277 CTF Redemption events covering
12,258 unique conditionIds for the same proxy. **1,678 redeemed
conditionIds are missing from /closed-positions** — that's **15.9% of
the visible closed-condition base** (1678 / 10580; the "how much would
the closed book grow if we fixed this?" denominator) and **13.7% of
the redeemed-condition base** (1678 / 12258; the
`undercount_ratio` denominator the surface emits, i.e. "what share of
redemptions is the API hiding?"). The two denominators answer different
questions; the surface uses the redeemed-side one because that's the
honest "what fraction of the wallet's actual closed activity is the
API missing." 661 have an orderbook trace, 1,017 have no trace in any
rail we read. The analyzer's "we've seen this wallet's closed book"
assumption is wrong for this wallet class. Investigation persisted at
`/home/bigpapi/.claude/jobs/414082e3/redeem_pnl_dedup_by_condition.json`
and `/home/bigpapi/.claude/jobs/414082e3/ctf_event_recon.json`.

## 1. Problem

`closed_positions` from the Polymarket data API is the analyzer's
authoritative "closed book" for every wallet. Every shipped surface —
`capital_risk_surface`, `sizing_outcome_surface`, `null_baseline_surface`,
`copy_latency_surface`, `survivorship_surface`, plus `trading_mode` and
verdict inputs — reads off this list. When the API undercounts (drops
conditions that resolved off-orderbook via direct CTF Redemption, or
acquired through Polymarket's NegRisk converter rail), those surfaces are
silently computed on a partial book. Counts, distributions, and peer
percentiles all become **lower bounds** without flagging.

The fix is **honesty, not imputation.** We reconcile `/closed-positions`
against the CTF Redemption stream the analyzer already has the reader
for (`subgraph_reader.fetch_redemptions`), report the gap, name the
acquisition rails we *can* see, and stop. We do not try to recover
per-condition PnL from event payouts — that would require NegRisk-adapter
event tracing not currently in scope, and Redemption payouts for binary
conditions cover both outcomes together (`indexSets=[1,2]`) so payout
attribution cannot be done from Redemption events alone.

## 2. Scope

One display-only surface, `position_reconciliation_surface`, on
`ProtocolReport`. Function of:
- `data.closed_positions` (already fetched)
- `data.activity_rows` (already fetched — every REST `/activity?type=TRADE`
  row, or subgraph-normalized fills on `activity_cap_hit`, carries `asset`
  and `conditionId` per `fetch.py:47-48,129-132`; this is the canonical
  orderbook-rail source — no new orderbook fetch is needed for the surface
  itself)
- `data.redemptions` (**NEW** — added to fetch path; see §8)
- `data.splits` (**NEW**)
- `data.merges` (**NEW**)

**Out of scope:** computing hidden-slice PnL, ROI, payout attribution
across multi-outcome indexSets, NegRisk-adapter event tracing,
mutating any other surface's numbers, feeding verdict / gate /
copyability / `primary_edge_type`.

## 3. Surface schema

`report.position_reconciliation_surface: dict | None` — `None` only on
the pre-surface backward-compat path. When produced, a dict:

| Key | Type | Meaning |
|---|---|---|
| `surface_version` | int | schema version (1) |
| `data_state` | str | `ok` \| `proxy_only` \| `data_unavailable` (§6) |
| `closed_positions_count` | int | `len(data.closed_positions)` — row count (kept for auditability; can exceed `closed_condition_count` when the API returns multiple rows per condition, as on 0x20ad75 where 10,608 rows reduce to 10,580 unique conditionIds) |
| `closed_condition_count` | int | **`\|closed_cond\|`** — unique conditionIds in `/closed-positions`. This is the condition-set denominator the reconciliation arithmetic actually uses |
| `redeemed_condition_count` | int | unique `conditionId` across all Redemption events for the wallet |
| `redeemed_not_in_closed_count` | int | `\|redeemed_cond − closed_cond\|` — condition-set arithmetic |
| `undercount_ratio` | float \| None | `redeemed_not_in_closed_count / max(redeemed_condition_count, 1)` — denominator is condition count, NOT row count |
| `redeemed_orderbook_traced_count` | int | of the undercounted, the subset with ≥1 orderbook fill on a token belonging to that condition |
| `untraced_redemption_count` | int | `redeemed_not_in_closed_count` minus traces from rails that were actually read. An **upper bound** when any auxiliary stream (`splits`/`merges`) is `None` — the missing rail could have traced some of these conditions |
| `rails_observed` | dict | `{orderbook: int, redemption: int, split: int \| None, merge: int \| None}` — distinct conditions per rail. A rail value is `None` when that stream's fetch failed (unknown, not measured-zero). `redemption` and `orderbook` are always concrete when we reach this point |
| `acquisition_rail_gap_share` | float \| None | `untraced_redemption_count / max(redeemed_condition_count, 1)` — share of redemptions with no traceable acquisition. `None` when any auxiliary stream (`splits`/`merges`) is `None`, because the gap is unknowable without all acquisition rails |
| `condition_set_summary` | dict | `{closed_only: int, redeemed_only: int, both: int}` — set-arithmetic on closed-positions condition set vs redemption condition set |
| `caveats` | list[str] | surface-local caveats (NOT `data_quality_flags`) |
| `downstream_lower_bound` | bool | `True` when other surfaces should be read as a lower bound (see §5.4) |
| `note` | str | rendered honesty preamble for the report block |

Numeric fields are `None` (unknown) only when the inputs are missing,
never silently `0`. The surface emits zero-valued counts (`0`) when
inputs are present and the answer is honestly zero.

## 4. Constants

- `UNDERCOUNT_PROXY_ONLY_THRESHOLD = 0.05` — when
  `undercount_ratio > 0.05` (the closed-positions API misses more than
  5% of redeemed conditions) → `data_state = proxy_only` and
  `downstream_lower_bound = True`.
- `ACQUISITION_GAP_NOTABLE_THRESHOLD = 0.10` — when
  `acquisition_rail_gap_share > 0.10` the rendered note explicitly names
  the untraced-acquisition rail in the caveat string.
- Severity order (worst wins) — `data_unavailable > proxy_only > ok`.

These constants live as module-level uppercase names in
`sniper/analysis/wallet_protocol/position_reconciliation.py`; tests bind
to them by import, not by hard-coding the numbers.

## 5. Producer algorithm — `compute_position_reconciliation_surface()`

`sniper/analysis/wallet_protocol/position_reconciliation.py`:

```
compute_position_reconciliation_surface(
    closed_positions: list[dict],
    activity_rows: list[dict],
    redemptions: list[dict] | None,
    splits: list[dict] | None,
    merges: list[dict] | None,
    activity_cap_hit: bool,
    n_unmapped_fills: int,
) -> dict
```

Pure; no I/O; deterministic. `activity_cap_hit` and `n_unmapped_fills`
are pulled from `data.activity_cap_hit` and `data.n_unmapped_fills`
respectively, and feed only the §5.3 caveat logic — they don't change
the bucket arithmetic.

### 5.1 Build condition sets

- `closed_cond = { lower(p["conditionId"]) for p in closed_positions if p.get("conditionId") }`
- `redeemed_cond = { lower(e["condition"]) for e in (redemptions or []) if e.get("condition") }`
- Introduce availability booleans: `splits_unavailable = (splits is None)`, `merges_unavailable = (merges is None)`.
- `split_cond = { lower(e["condition"]) for e in splits if e.get("condition") }` if `not splits_unavailable`, else `None` (the set itself is absent — not an empty set).
- `merge_cond = { lower(e["condition"]) for e in merges if e.get("condition") }` if `not merges_unavailable`, else `None`.

The `or []` coercion form is allowed for `redeemed_cond` only (redemptions=None already escalates to `data_unavailable` before reaching this point, so that `or []` is purely defensive). For `splits` and `merges`, the producer MUST track whether the input was `None` (fetch failed) vs `[]` (fetch succeeded, no events), because that distinction changes how §5.2 subtracts sets and how §5.3 reports rail counts.

`orderbook_cond = { lower(r["conditionId"]) for r in activity_rows if r.get("type") == "TRADE" and r.get("conditionId") }` — the set of conditionIds the wallet ever touched via CLOB. Source: REST `/activity` rows (which carry `conditionId` per `fetch.py:47-48`) or, on `activity_cap_hit`, the subgraph-normalized rows produced by `_normalize_order_fill` (which emit `conditionId` per `fetch.py:129`). Either source yields the same shape downstream.

**Why not raw `OrderFilledEvent` rows?** `activity_rows` already carries the asset → conditionId mapping resolved at fetch time; raw fills don't (their `makerAssetId`/`takerAssetId` are asset IDs, not conditions, and must be joined through `_build_token_lookup`). Reusing the resolved view means we don't need a second orderbook fetch or a parallel asset-map join.

### 5.2 Compute counts

- `closed_positions_count = len(closed_positions)`
- `redeemed_condition_count = |redeemed_cond|`
- `redeemed_not_in_closed = redeemed_cond − closed_cond`
- `redeemed_orderbook_traced = redeemed_not_in_closed ∩ orderbook_cond`
- `untraced_redemption` — start with `redeemed_not_in_closed − orderbook_cond`, then subtract only the rails that are available:
  ```
  untraced_redemption = redeemed_not_in_closed − orderbook_cond
  if not splits_unavailable:
      untraced_redemption -= split_cond
  if not merges_unavailable:
      untraced_redemption -= merge_cond
  ```
  When any auxiliary stream is unavailable, `untraced_redemption_count` is an **upper bound** — we know AT MOST this many conditions are untraced, because the missing rail's data might have reduced (or in edge cases, not changed) the count.
- `condition_set_summary = { closed_only: |closed_cond − redeemed_cond|,
   redeemed_only: |redeemed_cond − closed_cond|,
   both: |closed_cond ∩ redeemed_cond| }`

### 5.3 Rails observed

`rails_observed[rail]` is the count of **distinct conditionIds** that
have at least one event on that rail for this wallet, OR `None` if
that rail's stream fetch failed:
- `rails_observed["orderbook"] = |orderbook_cond|` (always concrete at this point)
- `rails_observed["redemption"] = |redeemed_cond|` (always concrete — `redemptions=None` already exits above)
- `rails_observed["split"] = None if splits_unavailable else |split_cond|`
- `rails_observed["merge"] = None if merges_unavailable else |merge_cond|`

A condition can appear in multiple rails simultaneously (orderbook +
redemption is the common case for held-to-resolution scrapes).

**Partial aux-stream failure:** when `splits_unavailable` or
`merges_unavailable`, the producer emits a caveat
`ACQUISITION_RAIL_FETCH_PARTIAL_splits` and/or
`ACQUISITION_RAIL_FETCH_PARTIAL_merges` (one per failed stream). This
is informational and does NOT change `data_state` (the
redemption-undercount finding can still be reported — we still have
redemptions). Additionally, `acquisition_rail_gap_share` is set to
`None` because the gap is unknowable when a rail is missing, and the
`UNTRACED_ACQUISITION_RAIL` caveat is SUPPRESSED — we cannot honestly
claim acquisition is untraced if we did not read all acquisition rails.

**Orderbook-rail honesty:** if `activity_cap_hit is True and
n_unmapped_fills > 0`, the surface emits a caveat
`ORDERBOOK_RAIL_INCOMPLETE_n=<n_unmapped_fills>` noting that the
orderbook rail count may undercount because the subgraph fallback
dropped fills whose asset IDs weren't in the REST token-lookup
window. This does NOT change `rails_observed["orderbook"]`; it just
flags the floor.

### 5.4 `downstream_lower_bound` and the note

`downstream_lower_bound = (undercount_ratio > UNDERCOUNT_PROXY_ONLY_THRESHOLD)`.

When `True`, the rendered `note` is:

> Closed-positions API undercount detected. `redeemed_not_in_closed_count`
> redeemed conditionIds are not surfaced by `/closed-positions`
> (`undercount_ratio × 100`%). Other surfaces in this report are computed
> on the visible closed book and should be read as lower bounds.

When `False`:

> Closed-positions API reconciles with redemption events within the
> tolerance band. No lower-bound caveat applied to downstream surfaces.

The `note` is rendered text; it does NOT mutate any other surface's
field. The `downstream_lower_bound: bool` is the machine-readable flag.

## 6. `data_state` — worst-wins

Explicit severity order (worst wins):
`data_unavailable` > `proxy_only` > `ok`.

- `data_unavailable` — either:
  - `redemptions is None` (fetch failed) → caveat
    `RECONCILIATION_INPUTS_MISSING`. This is a capture-side problem;
    the wallet may or may not have events, we simply couldn't read
    them.
  - `redemptions == []` AND `closed_positions == []` (fetch succeeded
    but the wallet has nothing to reconcile) → caveat
    `NO_CLOSED_OR_REDEEMED_CONDITIONS`. This is a wallet-side fact,
    not a capture failure.

  Splitting the two follows the same pattern as
  `copy_latency_surface`'s `data_unavailable_no_post_entry_grid_captured`
  vs `all_horizon_prices_out_of_range` — never conflate "we couldn't
  measure" with "we measured and there's nothing there." Both states
  share `data_state = "data_unavailable"` because no reconciliation
  is possible either way, but the caveat names which is which.
- `proxy_only` — `undercount_ratio > UNDERCOUNT_PROXY_ONLY_THRESHOLD`.
  Adds caveat `CLOSED_POSITIONS_UNDERCOUNT_<pct>`. Sets
  `downstream_lower_bound = True`. Adds a second caveat
  `UNTRACED_ACQUISITION_RAIL` when
  `acquisition_rail_gap_share > ACQUISITION_GAP_NOTABLE_THRESHOLD` AND
  `acquisition_rail_gap_share is not None`. The `UNTRACED_ACQUISITION_RAIL`
  caveat is SUPPRESSED when any auxiliary stream (`splits`/`merges`) is
  `None` — in that case `acquisition_rail_gap_share` is `None` and the
  `> threshold` predicate cannot be evaluated honestly. The partial-stream
  failure is reported via `ACQUISITION_RAIL_FETCH_PARTIAL_<rail>` instead.
- `ok` — neither of the above.

Note: partial aux-stream failure (splits=None or merges=None) does NOT
escalate `data_state` to `data_unavailable`. Only `redemptions=None` and
the both-empty case escalate. Partial aux-stream failure adds a caveat
and downgrades `acquisition_rail_gap_share` to `None`, but the
redemption-undercount finding (`proxy_only` or `ok`) remains reportable.

## 7. Hard wall — non-negotiable

Identical contract to `capital_risk_surface` /
`sizing_outcome_surface` / `null_baseline_surface` /
`copy_latency_surface`:

- **Display-only.** Read ONLY by the report renderer. MUST NOT feed
  `overall_verdict`, `gate_result`, `primary_edge_type`,
  `copyability_note`, `trading_mode`, branch/hypothesis logic, or any
  other producer's numbers. **In particular, `downstream_lower_bound`
  is informational — it does NOT mutate `null_baseline_surface`'s
  `n_total_closed`, `capital_risk_surface`'s exposure counts, or any
  other surface's outputs.** Other surfaces remain pure functions of
  their visible inputs; this surface only tells the reader those
  inputs are incomplete.
- **No `data_quality_flags` append.** Caveats live in the surface's
  own `caveats` field.
- **No live I/O at compute time.** Pure function of already-fetched
  inputs. Live fetch happens in §8.
- The display-only guarantee is the hard wall — no verdict/gate/other-
  surface code reads the field. Enforced by:
  - **grep-verified**: scope-guard tests assert no producer reads
    `position_reconciliation_surface`.
  - **mutation-proof test**: a garbage-stub producer (returns a dict
    with sentinel-bogus numbers) leaves `overall_verdict`,
    `gate_result`, `primary_edge_type`, `copyability_note`, every
    other surface's dict, and `data_quality_flags` byte-identical.
- `types.py` carries the field with the standard hard-wall comment.

## 8. Fetch additions

This surface is the first ProtocolReport surface that persists CTF
event streams onto `WalletData`. (Orderbook fills are NOT a new fetch —
the existing `activity_rows` already carries the resolved
condition-level view; §2 explains why.) Three CTF event streams are
new: `redemptions`, `splits`, `merges`.

### 8.1 New reader helpers (already present)

`sniper/analysis/wallet_protocol/subgraph_reader.py:206-264` already
defines `fetch_splits`, `fetch_merges`, `fetch_redemptions`. No new
reader code needed; only wiring.

### 8.2 `WalletData` field additions

`sniper/analysis/wallet_protocol/types.py` — three new optional fields
on `WalletData`. **Placement matters:** `WalletData` is a `@dataclass`
whose non-default fields run through `fetched_at: int` at
`types.py:218`. Inserting defaulted fields adjacent to `activity_rows`
(line 211) would break the dataclass with "non-default argument
follows default argument." Place the new fields AFTER `fetched_at`,
adjacent to the existing Completeness Surface v1 block (around
`n_unmapped_fills` at `types.py:227`) since they are part of the same
"raw event stream completeness" concept:

```python
# CTF event streams captured for position_reconciliation_surface.
# `None` (fetch failed) is distinct from `[]` (fetch succeeded, no
# events) — the surface relies on that distinction to pick
# `data_unavailable` vs `ok`. Old fixtures without these keys
# deserialize to `None`, preserving backward compatibility.
redemptions: list[dict] | None = None    # CTF Redemption events
splits: list[dict] | None = None         # CTF Split events
merges: list[dict] | None = None         # CTF Merge events
```

### 8.3 `fetch.py` wiring

Add three import lines and three try/except'd fetch calls in the main
fetch flow (NOT inside the `activity_cap_hit` branch — these run for
every wallet, not just cap-hit ones):

```python
from sniper.analysis.wallet_protocol.subgraph_reader import (
    fetch_redemptions, fetch_splits, fetch_merges,
)
# ...
try:
    redemptions = fetch_redemptions(wallet, session=session)
except Exception:
    logger.exception("wallet_protocol redemption fetch failed wallet=%s", wallet[:14])
    redemptions = None
# (same shape for splits, merges)
```

Each helper paginates at `PAGE_SIZE = 1000`. Failures degrade to
`None`, not `[]`, and emit a `fetch_quality_flags` entry naming the
failed stream.

### 8.4 Replay serialize / deserialize

`sniper/analysis/wallet_protocol/replay.py` round-trips `WalletData`.
Both directions need the three new fields, and **`None` MUST survive
the round trip**:

- Serialize (currently around line 65) — persist the field value
  literally, do NOT coerce `None` to `[]`:

  ```python
  "redemptions": data.redemptions,    # may be None — preserve literally
  "splits":      data.splits,
  "merges":      data.merges,
  ```

  An `list(data.redemptions or [])` form would silently collapse the
  fetch-failure `None` into an empty-events `[]`, destroying the
  distinction §6 / `data_state` depends on (and the
  `RECONCILIATION_INPUTS_MISSING` vs `NO_CLOSED_OR_REDEEMED_CONDITIONS`
  caveat split would become unreachable after a single round trip).
  Do not do this.

- Deserialize (currently around line 120):
  ```python
  redemptions=payload.get("redemptions"),  # no default — missing key → None
  splits=payload.get("splits"),
  merges=payload.get("merges"),
  ```
  Using `.get()` with no default means a missing key deserializes to
  `None`, preserving both:
  - **fetch failure round-trip** — a `WalletData` captured with a
    failed redemption fetch (`redemptions = None`) serializes to
    `"redemptions": null` and deserializes back to `None`. The
    surface still sees `data_unavailable` with
    `RECONCILIATION_INPUTS_MISSING`.
  - **pre-surface backward-compat** — an old fixture written before
    these fields existed has no `"redemptions"` key at all;
    `.get("redemptions")` returns `None`, same surface behavior as a
    capture-time failure.

### 8.5 Snapshot persistence

`record_fetch_snapshot.py` (and any other writer that persists
`WalletData` to a fixture on disk) must serialize the three new fields.
Old fixtures without the keys deserialize to `redemptions = splits =
merges = None` → surface reports `data_state = data_unavailable`. No
fixture-format version bump required; the absence of a key IS the
backward-compat signal.

### 8.6 Calibration re-baseline (operator-gated)

**Required, but live.** All 10 calibration fixtures in
`tests/calibration/` need recapture under the new fetch contract so
the surface has populated `redemptions`/`splits`/`merges`. Cost
breakdown:

- Per fixture: three paginated subgraph queries (redemptions, splits,
  merges). For typical wallets ≤ 1 page each. For the heaviest
  fixtures (whale-class), up to ~15 pages per stream.
- Total live cost (10 fixtures): roughly 30–100 paginated subgraph
  requests, plus the existing `/positions` + `/closed-positions` +
  `/activity` REST traffic. Order of seconds-to-minutes of wall time,
  Goldsky rate limits permitting.
- `tests/calibration/labels.yaml` `calibration_version` bumps `3 → 4`.
  The 10 surface labels per fixture gain
  `position_reconciliation_surface_data_state`,
  `position_reconciliation_surface_undercount_ratio` (or equivalent
  pinned scalar — exact labels chosen during plan, not spec).
- Recipe identical to the 2026-05-15 v3 recapture documented in
  `[[project_calibration_loop_v1]]`. Operator must authorize the
  live-fetch run; CI cannot do it autonomously.

### 8.7 Fetch-test coverage

Three new tests in `tests/test_wallet_protocol_fetch.py` (or sibling):

1. **Happy path** — mocked subgraph readers return non-empty lists; the
   three fields are populated on `WalletData` and survive a
   serialize→deserialize round trip.
2. **Failure isolation** — one of the three readers raises; only that
   field becomes `None`, the other two remain populated, and
   `fetch_quality_flags` carries a named flag.
3. **Old-fixture compatibility** — loading a pre-surface fixture (no
   keys for `redemptions`/`splits`/`merges`) deserializes cleanly with
   all three `None`.

## 9. Wiring

- **`types.py`** — add
  `position_reconciliation_surface: dict | None = None` on
  `ProtocolReport` with the standard hard-wall comment. The
  `WalletData` field additions for `redemptions` / `splits` / `merges`
  are covered in §8.2.
- **`analyze_wallet.py`** — one call alongside the
  `null_baseline_surface` call, fed
  `data.closed_positions, data.activity_rows, data.redemptions,
   data.splits, data.merges, data.activity_cap_hit, data.n_unmapped_fills`.
  `n_unmapped_fills` is already a typed field on `WalletData`
  (`types.py:227`); read it directly. **Do not** string-parse
  `data.fetch_quality_flags` for the `ACTIVITY_SUBGRAPH_UNMAPPED: n=…`
  pattern — that flag string exists for human log inspection; the
  typed counterpart is the canonical source.
- **`report.py`** — a `## Position reconciliation` markdown block.
  Renders counts, the `note`, and the `caveats` list. When
  `downstream_lower_bound` is `True`, the block renders BEFORE the
  other quantitative surfaces in the report so the reader sees the
  caveat first.

## 10. Acceptance tests — `tests/test_wallet_protocol_position_reconciliation_surface.py`

1. **Clean wallet (no gap)** — `closed_positions` and `redemptions`
   conditionId sets are identical → `undercount_ratio = 0.0`,
   `data_state = "ok"`, `downstream_lower_bound = False`.
1b. **Row vs condition count** — `closed_positions` contains multiple
   rows for the same conditionId (e.g. 100 rows reducing to 80 unique
   conditions); the surface emits `closed_positions_count = 100` AND
   `closed_condition_count = 80`. The reconciliation denominator and
   `condition_set_summary` use the 80, not the 100. Asserts the
   surface does not blur the two.
2. **Hidden-redemption wallet** — synthesized inputs reproducing the
   0x20ad75 shape: 10,608 closed-position rows reducing to 10,580
   unique conditions + 12,258 unique redeemed conditions, of which
   1,678 are gap → `closed_positions_count = 10608`,
   `closed_condition_count = 10580`, `redeemed_condition_count =
   12258`, `redeemed_not_in_closed_count = 1678`,
   `undercount_ratio ≈ 0.137`, `data_state = "proxy_only"`, caveat
   `CLOSED_POSITIONS_UNDERCOUNT_*`, `downstream_lower_bound = True`.
3. **Untraced-acquisition caveat** — when
   `acquisition_rail_gap_share > 0.10`, caveat
   `UNTRACED_ACQUISITION_RAIL` is present.
4. **Rails observed** — wallet with TRADE activity_rows on 50
   conditions, redemptions on 60 (50 shared + 10 not in orderbook),
   splits=0, merges=0 → `rails_observed == {orderbook:50, redemption:60,
   split:0, merge:0}`.
4b. **Orderbook rail incompleteness** — `activity_cap_hit = True` AND
   `n_unmapped_fills = 7` (subgraph-fallback dropped 7 fills) → caveat
   `ORDERBOOK_RAIL_INCOMPLETE_n=7` present; `rails_observed["orderbook"]`
   is the count of conditions with mapped rows and is NOT inflated by
   the unmapped count.
5. **`condition_set_summary`** — basic set arithmetic on a tiny
   constructed case is exact.
6. **`data_unavailable` from fetch failure** — `redemptions = None`
   → `data_state = "data_unavailable"`, caveat
   `RECONCILIATION_INPUTS_MISSING` (NOT
   `NO_CLOSED_OR_REDEEMED_CONDITIONS`), all count fields `None` not
   `0`. Pins that fetch-failure caveat distinct from empty-wallet
   caveat.
6b. **Partial aux-stream failure — splits or merges None** —
   `redemptions = [{"condition":"0xa"},{"condition":"0xb"}]`,
   `closed_positions = [{"conditionId":"0xa"}]`, `splits = None`,
   `merges = []`, `activity_rows = []`. Asserts:
   `data_state = "proxy_only"` (undercount finding stands — 0xb is
   redeemed but not in closed), `rails_observed["split"] is None`
   (NOT 0 — unknown, not measured-zero), `rails_observed["merge"] == 0`
   (measured zero), `acquisition_rail_gap_share is None` (cannot compute
   when a rail is unknown), `untraced_redemption_count == 1` (upper
   bound — 0xb has no orderbook trace, but unavailable splits might have traced it),
   caveat `ACQUISITION_RAIL_FETCH_PARTIAL_splits` present,
   caveat `UNTRACED_ACQUISITION_RAIL` ABSENT (suppressed under
   partial-stream condition). Symmetric test repeated for `merges = None`
   and for both `splits = None` AND `merges = None` simultaneously.
7. **`data_unavailable` from empty wallet** — `redemptions = []` AND
   `closed_positions = []` → `data_state = "data_unavailable"`,
   caveat `NO_CLOSED_OR_REDEEMED_CONDITIONS` (NOT
   `RECONCILIATION_INPUTS_MISSING`). Count fields are `0` (we
   measured zero), not `None` (couldn't measure). Asserts the
   capture-failure / wallet-state distinction is honored.
8. **No-orderbook-trace gap** — a redeemed condition with no
   `closed_positions` row AND no orderbook fill on any of its tokens
   → counted into `untraced_redemption_count`, NOT
   `redeemed_orderbook_traced_count`.
9. **Activity row without conditionId** — an `activity_rows` TRADE
   row whose `conditionId` field is empty/missing is silently
   excluded from `orderbook_cond` (covered by the §5.1 filter
   `r.get("conditionId")`); no caveat, no crash. (`activity_rows`
   carries `conditionId` per `fetch.py:47-48,129` so this should be
   rare; the test pins the silent-skip behavior.)
10. **Hard wall — mutation-proof** — a garbage-stub
    `compute_position_reconciliation_surface` returning a dict whose
    counts and `downstream_lower_bound` are inverted sentinels (e.g.
    `closed_positions_count = -1`, `downstream_lower_bound = True`
    when it should be False) leaves `overall_verdict`, `gate_result`,
    `primary_edge_type`, `copyability_note`, EVERY OTHER surface dict,
    and `data_quality_flags` byte-identical to a reference run.
11. **Hard wall — grep guard** — `grep -rn
    "position_reconciliation_surface" sniper/` finds the field ONLY
    in `types.py`, `analyze_wallet.py`, `report.py`, and the producer
    module. No other producer references it.
12. **Replay determinism** — same input → byte-identical surface
    JSON.
12b. **`None` survives replay round-trip** — a `WalletData` with
    `redemptions = None` (capture-side failure) serializes to a
    payload containing `"redemptions": null`, NOT `"redemptions":
    []`. Deserializing that payload yields `redemptions = None`
    again. The same round-trip applied to `redemptions = []` yields
    `redemptions = []` back. Pins that the serializer does NOT
    coerce `None → []` (which would silently collapse
    `RECONCILIATION_INPUTS_MISSING` into
    `NO_CLOSED_OR_REDEEMED_CONDITIONS`). Same assertion repeated
    for `splits` and `merges`.
13. **Backward-compat** — a `ProtocolReport` built without the field
    → `None`, no crash in any consumer.
14. **Scope-guard field-set tests updated** —
    `tests/test_wallet_protocol_honesty_floor.py:562` and
    `tests/test_wallet_protocol_tag_grain_rescue_floor.py:640`
    include `position_reconciliation_surface` in their expected sets.
15. **Calibration baseline** — recaptured fixtures yield surfaces
    whose `data_state` and `undercount_ratio` are pinned in
    `tests/calibration/labels.yaml` v4. Calibration tests pass.
16. **Note rendering** — when `downstream_lower_bound = True`, the
    `note` string contains the literal phrase "should be read as
    lower bounds"; when `False`, it does not.
17. **No mutation of other surfaces** — a paired-run test computes
    the report twice with `data.redemptions` empty vs populated; only
    `position_reconciliation_surface` differs between the two reports.
    Every other surface dict is byte-identical.

## 11. Out of scope

- Computing hidden-slice PnL, ROI, payout attribution.
- Tracing NegRisk-adapter acquisition events.
- Mutating `null_baseline_surface`'s `n_total_closed` or any other
  surface's input population to "correct" for the undercount. The
  surface is a flag, not a corrector.
- Promoting `downstream_lower_bound = True` into a hard verdict or
  gate signal.
- Per-token redemption payout attribution (would require gamma or
  on-chain `outcomeSlotCount` + indexSet expansion that's a separate
  research pass).
- Phantom-sell investigation (1,084 tokens on 0x20ad75 with USDC
  proceeds but no orderbook BUY and no Split — likely NegRisk
  converter). Deferred to a later surface or out-of-band tracer.

## 12. Evidence appendix — 0x20ad75e19b…879c9a

Captured 2026-05-23, persisted at
`/home/bigpapi/.claude/jobs/414082e3/`:

| Metric | Value |
|---|---:|
| `closed_positions_count` (rows from `/closed-positions`) | 10,608 |
| `closed_condition_count` (unique conditionIds in those rows) | 10,580 |
| `redeemed_condition_count` (subgraph) | 12,258 |
| `redeemed_not_in_closed_count` | **1,678** |
| `undercount_ratio` | **0.137** (13.7%) |
| `redeemed_orderbook_traced_count` | 661 |
| `untraced_redemption_count` | 1,017 |
| `acquisition_rail_gap_share` | **0.083** (8.3%) |
| `rails_observed.orderbook` (placeholder — depends on the wallet's `activity_rows`, see below) | TBD on recapture |
| `rails_observed.split` | **0** (the wallet uses an off-CTF acquisition rail; almost certainly NegRiskAdapter) |
| `rails_observed.merge` | 1 |
| `downstream_lower_bound` (proposed) | **True** |

This is the calibration target for test #2 above. The
`rails_observed.orderbook` number is left TBD here because it depends
on `activity_rows` content at recapture time (the 661 figure from this
investigation came from a separate full-history orderbook subgraph
pull, not from `activity_rows`); the surface's contract is that it
reads from `activity_rows`, so the calibration label must be pinned to
what `activity_rows` actually contains in the recaptured fixture, not
to the deeper subgraph view.
