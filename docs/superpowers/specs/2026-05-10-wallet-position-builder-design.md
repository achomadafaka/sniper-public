# Wallet Position Builder / Accumulation Surface Design

**Date:** 2026-05-10  
**Status:** design approved; implementation not started  
**Surface:** `position_building_v1`  
**Core rule:** identity and reconstruction honesty

W2 is a pure explanatory surface. It reconstructs how a wallet built,
reduced, and re-accumulated positions from normalized fill rows. It does
not decide whether to copy a wallet, recommend a trigger, simulate copy
results, call price history, mutate verdict logic, or depend on ledger
tables.

This follows the Timing Surface v1 pattern: pure compute first, no I/O,
no simulator, no external lookup, no verdict/gate/primary-edge mutation,
and no analyzer batch side effects.

---

## 1. Core Boundary

Use the hybrid source-of-truth model:

1. Core builder over normalized activity rows.
2. Optional ledger enrichment contract for stronger identity/provenance.
3. No DB dependency inside the core builder.

Conceptual API:

```python
def compute_position_building_surface(
    activity_rows: Sequence[Mapping],
    closed_positions: Sequence[Mapping] | None = None,
    *,
    wallet: str | None = None,
    activity_cap_hit: bool = False,
    subgraph_fallback_used: bool = False,
    walk_full_history: bool = False,
) -> dict | None:
    ...
```

Core builder must not:

- import DB or ledger modules;
- query ledger tables;
- call live APIs;
- call price-history functions or endpoints;
- read or write files;
- mutate persistence;
- run analyzer batches;
- restart services;
- touch `overall_verdict`, `gate_result`, `primary_edge_type`, or
  `copyability_note`;
- emit simulator or trigger recommendations.

Return `None` only when there are no usable activity rows and no
`closed_positions` context. If `closed_positions` exist but no usable
activity rows exist, return a surface with unavailable paths and explicit
caveats. Do not let "no surface" hide "wallet had positions but no
usable fill path."

### 1.1 Normalized Row Contract

Required normalized fields:

```text
condition_id
asset
outcome
side
price
size
timestamp
```

Accepted aliases:

```text
conditionId -> condition_id
token_id -> asset
ts -> timestamp
usdcSize -> notional
usd_equivalent_amount -> notional
```

Optional enrichment fields:

```text
wallet
event_uid
capture_run_id
source
source_ref
source_batch_id
source_timestamp
ingestion_path
raw_event_id
transaction_hash
logIndex / log_index
row_index
input_order
notional
identity_quality
source_quality
metadata_json
```

Side normalization:

- normalize `side` case-insensitively to `BUY` or `SELL`;
- malformed/unknown side rows are ignored for reconstruction and counted
  in row-quality caveats;
- no simulator-style interpretation of side.

Notional handling:

- if `notional` exists, preserve it;
- if `notional` is missing, derive `abs(size * price)` only when units
  are known;
- if units are ambiguous, leave notional unavailable;
- when `size`, `price`, and `notional` all exist, cross-check them.

Mismatch rule:

```text
abs(size * price - notional) / max(abs(notional), 1) > epsilon
```

If the mismatch exceeds tolerance, emit `SIZE_NOTIONAL_MISMATCH`. The
tolerance should absorb USDC 6-decimal rounding and normal float noise;
implementation may choose the exact epsilon, but must test the boundary.

### 1.2 Identity Preference

Use stable identity before human-readable labels.

Preferred identity order:

```text
event_uid
raw_event_id
transaction_hash + logIndex
capture_run_id + event_uid, if event_uid is capture-scoped
source_batch_id/source_ref + asset + timestamp + row_index
source_batch_id/source_ref + condition_id + asset + timestamp + row_index
source_batch_id/source_ref + condition_id + outcome + timestamp + row_index
condition_id + outcome + timestamp only as ambiguous fallback
```

`asset` / token id is more stable than `outcome` text. Use asset before
label whenever available.

`row_index` is only an ordinal inside the normalized input batch. It is
not globally stable. Use it for durable identity only when paired with a
source or batch scope:

```text
source_batch_id + row_index
source_ref + row_index
capture_run_id + row_index
```

Bare `row_index` can preserve local ordering, but is not durable across
REST windows, subgraph fallback, ledger materialization, fixture replay,
or combined sources.

Same-second fills must not collapse. If multiple rows share
`condition_id + asset + timestamp` and no stable discriminator exists,
preserve rows separately when possible and mark:

```text
identity_quality = ambiguous_same_second
ORDER_AMBIGUOUS_SAME_SECOND
```

If rows cannot be separated, mark the affected position reconstruction
as inferred/ambiguous instead of pretending it is exact.

### 1.3 Source and Identity Quality

Incoming rows may include `source_quality` and `identity_quality`, but
the builder may downgrade them. Example: if input claims
`activity_rows_complete` but `activity_cap_hit=True` and neither
`subgraph_fallback_used` nor `walk_full_history` is true, downgrade to
`activity_rows_partial`.

Source-quality labels:

```text
ledger_enriched
activity_rows_complete
activity_rows_partial
activity_rows_ambiguous
unavailable
```

Identity-quality labels:

```text
stable_event_uid
stable_transaction
source_bounded_ordinal
local_order_only
ambiguous_same_second
label_fallback
unavailable
```

Examples:

- `event_uid` present and unique -> `stable_event_uid`;
- `transaction_hash + logIndex` present -> `stable_transaction`;
- `source_ref + row_index` only -> `source_bounded_ordinal`;
- bare row order only -> `local_order_only`;
- same-second fills with no discriminator -> `ambiguous_same_second`;
- `(condition_id, outcome)` fallback -> `label_fallback`;
- no usable identity -> `unavailable`.

### 1.4 Ledger Adapter Contract

The ledger adapter is a contract in this spec, not a core dependency:

```text
ledger rows -> normalized position-builder rows
activity rows -> normalized position-builder rows
normalized rows -> compute_position_building_surface(...)
```

When ledger identity exists, preserve:

```text
event_uid
capture_run_id
source
source_ref
source_timestamp
ingestion_path
raw_event_id / transaction id
```

Do not implement DB-backed ledger wiring in W2 core. No imports from
`sniper.analysis.wallet_protocol.ledger` inside the core builder.

### 1.5 `closed_positions` Are Context

`closed_positions` may provide:

- known position universe;
- final known position context;
- `avgPrice` if trustworthy;
- reconciliation checks;
- missing-path caveats.

They must not invent an accumulation path. If activity rows are missing
or truncated, return unavailable/inferred path fields with caveats. Do
not fill in fake BUY or SELL events from closed-position summaries.

If `closed_positions` imply a position exists but activity rows contain
no usable BUYs for that key, emit a position object with:

```text
accumulation_pattern = unavailable
fields_provenance fill-path fields = unavailable
caveats includes NO_USABLE_BUY_PATH
```

Context-only position keys follow the same identity rule as row-backed
positions: use `(condition_id, asset)` with `position_key.type="asset"`
when the closed-position row carries token identity; otherwise fall back
to `(condition_id, outcome)` with `position_key.type="label_fallback"`,
`identity_quality="label_fallback"`, and `LABEL_FALLBACK_IDENTITY`.

---

## 2. Reconstruction and Provenance

Primary position key:

```text
(condition_id, asset)
```

Fallback only when `asset` is unavailable:

```text
(condition_id, outcome)
```

Fallback keys must set:

```text
position_key.type = label_fallback
identity_quality = label_fallback
caveats includes LABEL_FALLBACK_IDENTITY
```

Do not use human-readable outcome labels when token identity exists.

### 2.1 Fill Stream Ordering

For each position key:

```text
fills = normalized rows for key sorted by:
1. timestamp
2. source-bounded row_index / input_order if available
3. stable identity only as deterministic tie-breaker, not temporal truth
```

Do not imply `event_uid`, transaction hash, or other stable identity
gives true intra-second order unless the source guarantees it. If
same-second order is not knowable, preserve deterministic order and mark
`ORDER_AMBIGUOUS_SAME_SECOND`.

The builder tracks per position:

```text
buy_fills
sell_fills
ignored_rows
ambiguous_rows
malformed_rows
missing_price_rows
missing_size_rows
missing_timestamp_rows
size_notional_mismatch_rows
```

### 2.2 Size and Notional Paths

BUY increases observed token size. SELL decreases observed token size.

Raw `cumulative_size_path` keeps all usable rows, including dust. Dust
is ignored only for pattern labels and reaccumulation detection.

If SELL exceeds observed size:

- display/clamp observed size at zero;
- emit `SELL_EXCEEDS_OBSERVED_SIZE`;
- emit `LIKELY_STARTED_MID_POSITION` when the capture likely began after
  the wallet built inventory.

Do not treat this as automatically bad data. It may be incomplete
capture.

`final_observed_size` means final observed net token size from the
available fill stream. It is not necessarily the wallet's true final
position size.

If activity is truncated, missing buys, missing sells, or starts
mid-position, `final_observed_size` provenance is inferred or
unavailable. Possible caveats:

```text
PARTIAL_FILL_PATH
STARTS_MID_POSITION
ACTIVITY_TRUNCATED_POSITION_PATH
SELL_EXCEEDS_OBSERVED_SIZE
FINAL_SIZE_RECONCILIATION_MISMATCH
```

Milestones are measured against `peak_observed_size`, not final net size:

```text
time_to_25pct_size_ms
time_to_50pct_size_ms
time_to_75pct_size_ms
n_buys_before_50pct
n_buys_before_75pct
```

Also emit:

```text
peak_observed_size
peak_observed_size_ts
time_to_peak_size_ms
time_to_final_observed_size_ms
```

### 2.3 Average Entry

`average_entry_path` uses a simple weighted-average observed entry cost
over currently observed BUY inventory.

Before the first SELL, `average_entry_path` can be factual when BUY
prices and sizes are valid. Once any SELL has occurred for the position,
post-SELL average-entry values are inferred. They remain arithmetically
valid under the explicit `average_cost_observed` assumption, but they
are not facts about the wallet's true cost basis because FIFO/tax-lot
ordering is not tracked.

Do not imply FIFO, tax-lot, or per-grain attribution unless explicitly
implemented. Emit this caveat whenever any SELL has been processed for
the position:

```text
AVERAGE_ENTRY_INFERRED_AFTER_SELL
```

### 2.4 Sell Ladders and Reaccumulation

Do not collapse sell ladders into a last-sell concept.

`partial_sell_ladder` contains every usable SELL row for the position.

`reaccumulation_detected` is true when:

```text
material_delta = max(1e-9 tokens, 0.10 * peak_observed_size)

a SELL reduces observed size by >= material_delta
and a later BUY increases observed size by >= material_delta
```

This avoids dust sell/buy noise and division issues when prior observed
size is near zero.

### 2.5 Field Provenance

Every major output field has per-position provenance:

```text
factual
inferred
unavailable
```

Examples:

- `first_buy_ts`: factual if from usable BUY row; unavailable if no BUY
  row.
- `cumulative_size_path`: factual if all sizes valid and identity/order
  usable; inferred if partial/truncated; unavailable if no usable fills.
- `average_entry_path`: factual if BUY price and size are valid;
  inferred after any SELL; unavailable if missing price/size.
- `sell_ladder_summary`: factual if SELL rows are usable; inferred if
  source is partial; unavailable if no SELL visibility.
- `observation_window_quality`: inferred or unavailable only.

### 2.6 Observation Window Quality

Use `observation_window_quality`, not `copy_window_quality`.

Meaning: how well we observed position-building. It does not mean
copying would have been profitable.

Labels:

```text
good
partial
poor
unavailable
```

Rules:

- `good`: source quality is `ledger_enriched` or
  `activity_rows_complete`; identity quality is `stable_event_uid` or
  `stable_transaction`; at least one usable BUY exists; none of the
  caveats listed below are present; `SIZE_NOTIONAL_MISMATCH` appears on
  at most one row.
- `partial`: usable BUY path exists but source, identity, or
  reconciliation caveats limit confidence.
- `poor`: position exists but first entry, milestone path, or sell ladder
  is mostly missing/ambiguous.
- `unavailable`: no usable fill path.

Max-quality rules:

- if `activity_cap_hit=True` and neither `subgraph_fallback_used` nor
  `walk_full_history` is true, max quality is `partial`;
- if `identity_quality` is `ambiguous_same_second`, `unavailable`, or
  `label_fallback`, max quality is `partial`;
- if no usable BUY path exists, quality is `unavailable`;
- if first entry or most milestone path is missing/ambiguous, quality is
  `poor`.

Caveats that forbid `good`:

```text
LABEL_FALLBACK_IDENTITY
ORDER_AMBIGUOUS_SAME_SECOND
SELL_EXCEEDS_OBSERVED_SIZE
STARTS_MID_POSITION
PARTIAL_FILL_PATH
ACTIVITY_TRUNCATED_POSITION_PATH
```

`SIZE_NOTIONAL_MISMATCH` on one row is allowed under `good`; two or more
rows downgrade to `partial`.

---

## 3. Output Shape

Top-level:

```python
position_building_surface = {
    "wallet": wallet,
    "surface_version": "position_building_v1",
    "generated_from": {
        "activity_rows": len(activity_rows),
        "closed_positions": len(closed_positions or []),
        "activity_cap_hit": activity_cap_hit,
        "subgraph_fallback_used": subgraph_fallback_used,
        "walk_full_history": walk_full_history,
    },
    "source_quality_distribution": {...},
    "identity_quality_distribution": {...},
    "row_quality_summary": {...},
    "wallet_summary": {...},
    "positions": [...],
    "top_level_caveats": [...],
}
```

Distributions use count and share:

```python
{
    "ledger_enriched": {"n": 3, "share": 0.12},
    "activity_rows_complete": {"n": 10, "share": 0.40},
}
```

### 3.1 Position Object

```python
{
    "position_key": {
        "type": "asset" | "label_fallback",
        "condition_id": str | None,
        "asset": str | None,
        "outcome": str | None,
    },
    "condition_id": str | None,
    "asset": str | None,
    "outcome": str | None,

    "source_quality": str,
    "identity_quality": str,
    "caveats": [...],

    "first_buy_ts": int | None,
    "first_buy_price": float | None,
    "first_buy_notional": float | None,
    "n_buy_fills": int,
    "n_sell_fills": int,

    "peak_observed_size": float | None,
    "peak_observed_size_ts": int | None,
    "peak_observed_notional": float | None,
    "final_observed_size": float | None,
    "final_observed_notional": float | None,

    "cumulative_size_path": [...],
    "average_entry_path": [...],
    "buy_ladder_summary": {...},
    "partial_sell_ladder": [...],
    "sell_ladder_summary": {...},

    "time_to_25pct_size_ms": int | None,
    "time_to_50pct_size_ms": int | None,
    "time_to_75pct_size_ms": int | None,
    "time_to_peak_size_ms": int | None,
    "time_to_final_observed_size_ms": int | None,

    "n_buys_before_50pct": int | None,
    "n_buys_before_75pct": int | None,

    "accumulation_pattern": str,
    "small_buy_accumulation_detected": bool,
    "reaccumulation_detected": bool,
    "observation_window_quality": str,

    "row_quality": {...},
    "reconciliation": {...},
    "fields_provenance": {...},
}
```

Path entry:

```python
{
    "ts": int,
    "event_uid": str | None,
    "side": "BUY" | "SELL",
    "size_delta": float,
    "observed_size": float,
    "price": float | None,
    "notional": float | None,
    "observed_notional": float | None,
    "order_ambiguous": bool,
}
```

Average entry entry:

```python
{
    "ts": int,
    "avg_entry_price": float | None,
    "observed_inventory": float,
    "method": "average_cost_observed",
    "provenance": "factual" | "inferred" | "unavailable",
}
```

Buy ladder summary:

```python
{
    "n_buy_fills": int,
    "total_buy_size": float | None,
    "total_buy_notional": float | None,
    "first_buy_ts": int | None,
    "last_buy_ts": int | None,
    "size_weighted_entry_price": float | None,
    "largest_buy_share_of_peak": float | None,
}
```

Partial sell ladder entry:

```python
{
    "ts": int,
    "event_uid": str | None,
    "price": float | None,
    "size": float,
    "notional": float | None,
    "observed_size_after": float,
    "order_ambiguous": bool,
}
```

Sell ladder summary:

```python
{
    "n_sell_fills": int,
    "total_sell_size": float | None,
    "total_sell_notional": float | None,
    "first_sell_ts": int | None,
    "last_sell_ts": int | None,
    "size_weighted_sell_price": float | None,
    "sell_price_method": "size_weighted" | "partial" | "unavailable",
}
```

Reconciliation:

```python
{
    "closed_position_seen": bool,
    "closed_position_size": float | None,
    "observed_final_size": float | None,
    "size_delta": float | None,
    "status": "matched" | "mismatch" | "context_only" | "not_checked",
}
```

Row quality:

```python
{
    "malformed_rows": int,
    "ignored_rows": int,
    "ambiguous_rows": int,
    "missing_price_rows": int,
    "missing_size_rows": int,
    "missing_timestamp_rows": int,
    "size_notional_mismatch_rows": int,
}
```

Field provenance:

```python
{
    "first_buy_ts": "factual" | "inferred" | "unavailable",
    "cumulative_size_path": "factual" | "inferred" | "unavailable",
    "average_entry_path": "factual" | "inferred" | "unavailable",
    "sell_ladder_summary": "factual" | "inferred" | "unavailable",
    "observation_window_quality": "inferred" | "unavailable",
}
```

### 3.2 Wallet Summary

```python
{
    "n_positions": int,
    "n_reconstructed_positions": int,
    "n_unavailable_positions": int,
    "n_context_only_positions": int,

    "share_repeated_buy_positions": float | None,
    "share_small_buy_accumulation": float | None,
    "share_sell_ladder_positions": float | None,
    "share_reaccumulation_positions": float | None,

    "median_time_to_50pct_size_ms": int | None,
    "median_time_to_75pct_size_ms": int | None,
    "median_time_to_peak_size_ms": int | None,

    "median_peak_observed_notional": float | None,
    "median_final_observed_notional": float | None,
    "share_positions_with_notional_available": float | None,

    "observation_window_coverage": float | None,
    "observation_window_quality_distribution": {...},

    "share_positions_with_stable_identity": float | None,
    "share_positions_with_ambiguous_same_second": float | None,
    "share_positions_with_no_usable_buy_path": float | None,

    "dominant_accumulation_pattern": str | None,
    "dominant_limitation": str | None,
    "high_confidence_position_count": int,
    "low_confidence_position_count": int,

    "major_caveats": [...],
}
```

`n_positions` is all position keys from usable rows plus any
`closed_positions` keys not seen in rows.

`n_reconstructed_positions` requires a usable BUY path:

- at least one BUY with valid timestamp;
- valid side;
- valid price;
- valid size;
- stable or at least locally orderable identity.

If BUY rows exist but price/size is missing, the position may exist but
does not count as fully reconstructed.

`n_context_only_positions` is the count of positions found in
`closed_positions` but not reconstructed from activity rows.

`observation_window_coverage`:

```text
count(position.observation_window_quality in {"good", "partial"}) / n_positions
```

If `n_positions == 0`, use `None`.

Denominators are locked by metric class.

Shape shares use `n_reconstructed_positions`:

- `share_repeated_buy_positions`;
- `share_small_buy_accumulation`;
- `share_sell_ladder_positions`;
- `share_reaccumulation_positions`.

Quality/data-availability shares use `n_positions`:

- `share_positions_with_notional_available`;
- `share_positions_with_stable_identity`;
- `share_positions_with_ambiguous_same_second`;
- `share_positions_with_no_usable_buy_path`;
- `observation_window_coverage`.

When `n_reconstructed_positions == 0`, shape shares are `None`.

Medians exclude `None`.

Decision rules:

- `dominant_accumulation_pattern`: most frequent
  `accumulation_pattern` among reconstructed positions; ties sort by
  descending count, then by pattern precedence order in §4. If
  `n_reconstructed_positions == 0`, use `None`.
- `dominant_limitation`: most frequent caveat across positions and
  `top_level_caveats`; ties sort by descending count, then
  lexicographically. If no caveats exist, use `None`.
- `high_confidence_position_count`: count of positions with
  `observation_window_quality == "good"`.
- `low_confidence_position_count`: count of positions with
  `observation_window_quality in {"poor", "unavailable"}`.

---

## 4. Pattern Classification

`accumulation_pattern` describes shape. `small_buy_accumulation_detected`
describes split-buy style. They are independent.

Pattern precedence:

```text
unavailable
fragmented_or_ambiguous
single_fill
front_loaded
gradual_accumulation
small_buy_accumulation
repeated_buy
```

Dust rule:

```text
dust_delta = max(1e-9 tokens, 0.01 * peak_observed_size)
```

Keep dust fills in raw `cumulative_size_path`. Ignore size changes
`<= dust_delta` only for pattern labels and reaccumulation detection.

### 4.1 Pattern Definitions

`unavailable`:

```text
no usable BUY path
```

`fragmented_or_ambiguous`:

```text
identity_quality in {ambiguous_same_second, unavailable}
or source_quality == activity_rows_ambiguous
or row/caveat state prevents honest pattern choice
```

`single_fill`:

```text
n_buy_fills == 1
```

This describes entry shape only. A position with one BUY and many SELLs
is still `single_fill`; sell-ladder behavior is represented separately
through `partial_sell_ladder`, `sell_ladder_summary`, and caveats.

`front_loaded`:

```text
n_buy_fills >= 2
peak_observed_size > 0
and position reaches >=75% of peak_observed_size
  either on the first BUY
  or within FRONT_LOADED_WINDOW_MS from first_buy_ts
```

Default:

```text
FRONT_LOADED_WINDOW_MS = 5 * 60 * 1000
```

`gradual_accumulation`:

```text
n_buy_fills >= 3
time_to_75pct_size_ms is not null
time_to_75pct_size_ms > FRONT_LOADED_WINDOW_MS
largest_buy_share_of_peak < 0.70
identity/order quality sufficient
```

`small_buy_accumulation`:

Dominant fallback only when the wallet clearly accumulated through
multiple smaller buys but timing shape cannot honestly be classified as
front-loaded or gradual.

```text
n_buy_fills >= 3
largest_buy_share_of_peak < 0.70
buys occur across >1 timestamp or >1 stable event identity
identity_quality not in {ambiguous_same_second, unavailable}
time_to_75pct_size_ms is null or timing shape is otherwise unavailable
```

`repeated_buy`:

```text
n_buy_fills >= 2
does not meet unavailable, fragmented, single_fill, front_loaded,
gradual_accumulation, or small_buy_accumulation
```

Independent boolean:

```text
small_buy_accumulation_detected = (
  n_buy_fills >= 3
  and largest_buy_share_of_peak < 0.70
  and buys occur across >1 timestamp or >1 stable event identity
  and identity_quality not in {ambiguous_same_second, unavailable}
)
```

This is descriptive only. It is not a trigger recommendation.

---

### 4.2 Test Matrix

Future core implementation tests:

1. Plain activity rows: single BUY reconstructs one position.
2. Repeated BUYs produce cumulative size path and average entry path.
3. Milestones use `peak_observed_size`, not final net size after SELL.
4. Same-second distinct `event_uid`s remain separate.
5. Same-second rows without discriminator are preserved but marked
   `ambiguous_same_second`.
6. `(condition_id, asset)` key wins over outcome text.
7. `(condition_id, outcome)` fallback marks `label_fallback`.
8. Missing side/price/size/timestamp increments row-quality counters.
9. Provided notional mismatch emits `SIZE_NOTIONAL_MISMATCH`.
10. SELL exceeding observed size emits `SELL_EXCEEDS_OBSERVED_SIZE` and
    `LIKELY_STARTED_MID_POSITION`.
11. Partial sell ladder emits multiple SELL entries, not last-sell
    collapse.
12. Reaccumulation detects material sell-down then later buy-up using
    `0.10 * peak_observed_size`.
13. Closed-position-only context emits `NO_USABLE_BUY_PATH`.
14. W2 does not read or mutate Timing Surface, Exit Attribution
    classification, verdict fields, gate fields, `primary_edge_type`, or
    `copyability_note`.
15. Output contains no forbidden simulator fields.
16. Front-loaded small buys: four BUYs, no single buy >=70%, reaches 75%
    of peak within five minutes -> `accumulation_pattern=front_loaded`
    and `small_buy_accumulation_detected=true`.
17. Gradual small buys: four BUYs, no single buy >=70%, reaches 75% of
    peak after more than five minutes ->
    `accumulation_pattern=gradual_accumulation` and
    `small_buy_accumulation_detected=true`.
18. Small-buy fallback: three or more smaller BUYs with insufficient
    milestone timing -> `accumulation_pattern=small_buy_accumulation`
    and `small_buy_accumulation_detected=true`.
19. Dust ignored for pattern classification: tiny sell/buy changes below
    dust threshold remain visible in raw path but do not trigger
    reaccumulation or alter accumulation pattern.
20. Observation quality max downgrade: `activity_cap_hit=True` with no
    fallback/walk-full-history cannot produce
    `observation_window_quality=good`.
21. Wallet parameter round-trip:
    `compute_position_building_surface(..., wallet="0xabc")` returns
    `result["wallet"] == "0xabc"`.
22. No-HTTP-call regression: monkeypatch `requests.Session.get` and
    `requests.Session.post`; compute surface; assert zero invocations.
23. Phase-1 boundary lock: `analyze_wallet(...)` reports do not have a
    `position_building_surface` attribute during core-only Phase 1.
24. Fixture-replay smoke: run W2 core over an existing calibration
    fixture such as SwissMiss `0xdbade4c8...9c95` or YatSen
    `0x5bffcf56...ffbe`; assert `n_positions > 0`,
    `accumulation_pattern` values are in the known enum, and no
    forbidden output keys appear.

Forbidden output keys:

```text
recommended_trigger
trigger_threshold
copy_after_first_buy
copy_after_n_buys
expected_copy_pnl
simulated_copy_result
copy_strategy
copy_signal
```

---

## 5. Scope and Phasing

### 5.1 Design Phase

Deliverable:

```text
docs/superpowers/specs/2026-05-10-wallet-position-builder-design.md
```

Commit message:

```text
docs(wallet-protocol): design position-building surface
```

Design-only verification:

```bash
python3 scripts/validate_schema_refs.py
git status --short
```

Design-only constraints:

- do not create `sniper/analysis/wallet_protocol/position_building.py`;
- do not create `tests/test_wallet_protocol_position_building.py`;
- do not add `ProtocolReport.position_building_surface`;
- do not wire `analyze_wallet.py`;
- do not render a report section;
- do not implement DB-backed ledger adapter;
- do not touch PR #6 ledger materialization;
- do not run analyzer batches;
- do not mutate DB;
- do not restart services.

### 5.2 Future Implementation Phase 1: Core Only

Files:

```text
sniper/analysis/wallet_protocol/position_building.py
tests/test_wallet_protocol_position_building.py
```

Scope:

- pure core builder;
- normalized row parsing;
- identity/source-quality derivation;
- position-level reconstruction;
- wallet summary;
- boundary tests.

Verification:

```bash
python3 -m pytest tests/test_wallet_protocol_position_building.py -q
python3 scripts/validate_schema_refs.py
```

### 5.3 Future Implementation Phase 2: ProtocolReport Wiring

Potential file:

```text
sniper/analysis/wallet_protocol/types.py
```

Optional field:

```python
ProtocolReport.position_building_surface: dict | None = None
```

Only add this after core-only is reviewed and approved.

### 5.4 Future Implementation Phase 3: Analyzer / Report Integration

Potential files:

```text
sniper/analysis/wallet_protocol/analyze_wallet.py
sniper/analysis/wallet_protocol/report.py
```

Scope:

- populate `position_building_surface`;
- optionally render compact summary fields:
  - `dominant_accumulation_pattern`;
  - `dominant_limitation`;
  - `observation_window_coverage`;
  - `high_confidence_position_count`;
  - `low_confidence_position_count`.

Do not do this in the first W2 implementation PR unless explicitly
approved.

---

## 6. Non-Goals

W2 must not emit or implement:

```text
recommended_trigger
trigger_threshold
copy_after_first_buy
copy_after_n_buys
expected_copy_pnl
simulated_copy_result
copy_strategy
copy_signal
```

W2 must not perform:

```text
price-history calls
live API calls
analyzer batches
DB writes
service restarts
verdict/gate/primary_edge_type/copyability_note mutation
legacy low-buy stream dependency
```

Allowed wording:

```text
position reached 50% of peak observed size after 4 buys over 18 minutes
small-buy accumulation detected
observation_window_quality: partial
identity_quality: ambiguous_same_second
```

Forbidden wording:

```text
copy after first buy
copy after 3 buys
copy after threshold
recommended trigger
expected copy PnL
simulated copy result
```

---

## 7. Handoff Checklist

- Section 1 contracts accepted.
- Section 2 reconstruction/provenance accepted.
- Section 3 output shape accepted.
- Section 4 pattern thresholds accepted.
- Section 5 scope/non-goals accepted.
- No simulator fields.
- No trigger recommendations.
- No ledger dependency in core.
- No ProtocolReport wiring in first implementation unless separately
  approved.
- Core builder works from ProtocolReport activity rows without ledger.
- Ledger enrichment path is defined as an adapter contract only.
- Same-second fills are preserved or marked ambiguous.
- Partial sells and sell ladders are represented, not collapsed.
- Field provenance is per-position and per-field.
- `observation_window_quality` describes observability, not profitability.
