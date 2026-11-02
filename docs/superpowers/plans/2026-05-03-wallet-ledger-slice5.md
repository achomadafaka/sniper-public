# Wallet Ledger Slice 5 Implementation Plan

> **Status:** v4 — addresses Codex's third BLOCK audit on v3. The
> price-history closure now matches the live adapter exactly, and structure
> guards are scoped to the materialized branch path so legacy leaf calls keep
> their current contract.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land Slice 5 of the wallet fact ledger — `materialize_wallet_data`
and the per-hypothesis fail-closed guards. The materializer reads ledger
facts in a defined analysis window and returns a `MaterializedWalletData`
bundle whose `to_analyze_wallet_kwargs()` produces the exact kwargs the
existing `analyze_wallet()` entrypoint already accepts. No protocol
signature changes.

**Architecture:** Spec §14. Pure local work — no live API calls in Slice 5.
The boundary stays clean: capture/normalize/enrich (Slices 1–4) → materialize
(Slice 5) → analyze (existing protocol code) → persist findings (Slice 6).

**Tech Stack:** Python, DuckDB, pytest.

---

## Design Decisions (Locked, Audit-Closed)

### D1. `AnalysisWindow` shape — immutable input, output meta separate

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class AnalysisWindow:
    """Immutable input. Hashable. Used as identity for the materialized result.

    `capture_run_ids` is plural — a single analysis window may bridge several
    incremental capture runs that landed activity in `[start_ts, end_ts]`.
    Order matters for hashing and is preserved by the caller.

    `source_versions` is a tuple-of-tuples for hashability; convert to dict at
    read sites if a mapping is more convenient.
    """

    window_id: str
    capture_run_ids: tuple[str, ...]
    start_ts: int
    end_ts: int
    source_versions: tuple[tuple[str, str], ...]
```

Validation in `__post_init__`: `start_ts <= end_ts`,
`capture_run_ids` non-empty, `window_id` non-empty.
Manifest hashes and row counts live on `MaterializedWalletData` (output),
not on the window (input).

### D2. Execution profile re-aggregation — persist auxiliary fields, then lossless

`wallet_execution_probes` does not currently persist `ref_tape`, `tape_n`,
`in_play`, `thin_market`, `resolution_ts_unknown`. Without them
`_record_to_fill_result` cannot reconstruct a `FillResult` and the
materialized aggregate drifts from the live-API aggregate.

**Fix:** Task 0 schema migration adds five columns:

```sql
ALTER TABLE wallet_execution_probes ADD COLUMN ref_tape DOUBLE;
ALTER TABLE wallet_execution_probes ADD COLUMN tape_n INTEGER;
ALTER TABLE wallet_execution_probes ADD COLUMN in_play BOOLEAN;
ALTER TABLE wallet_execution_probes ADD COLUMN thin_market BOOLEAN;
ALTER TABLE wallet_execution_probes ADD COLUMN resolution_ts_unknown BOOLEAN;
```

Spec §11 column list grows by these five. Probe writer extended to
populate them from `FillProbeRecord`. After the migration the
materializer's re-aggregation is byte-for-byte identical to a live-API
aggregate over the same fills.

### D3. Materializer return shape — bundle keyed to `analyze_wallet()` kwargs

Audit caught that v2 used field names that don't match `analyze_wallet()`'s
actual kwargs. Verified at `sniper/analysis/wallet_protocol/analyze_wallet.py:306`:

```python
def analyze_wallet(
    wallet: str,
    *,
    session: requests.Session | None = None,
    archetype_extractor: Any = None,
    peer_baseline_fn: PeerBaselineFn | None = None,
    price_history_fn: PriceHistoryFn | None = None,
    leafs_by_cid: Mapping[str, Sequence[str]] | None = None,
    nav_lookup: Mapping[str, str] | None = None,
    ...
    pre_fetched_data: WalletData | None = None,
    walk_full_history: bool = False,
) -> ProtocolReport:
```

Bundle field names match those kwargs exactly:

```python
@dataclass
class MaterializedWalletData:
    wallet_data: WalletData                  # passed as pre_fetched_data
    price_history_fn: PriceHistoryFn         # Callable[[str, int], float | None]
    peer_baseline_fn: PeerBaselineFn | None  # H5 / FH4 per-grain baseline
    leafs_by_cid: dict[str, list[str]] | None
    nav_lookup: dict[str, str] | None

    # Diagnostics-only surface (NOT consumed by analyze_wallet):
    get_price_bars: Callable[[str], "PriceBarsResult"] | None

    # Provenance:
    window: AnalysisWindow
    replay_surface_meta: dict[str, dict]
    manifest_hashes: dict[str, str]
    row_counts: dict[str, int]

    def to_analyze_wallet_kwargs(self) -> dict:
        return {
            "pre_fetched_data": self.wallet_data,
            "price_history_fn": self.price_history_fn,
            "peer_baseline_fn": self.peer_baseline_fn,
            "leafs_by_cid": self.leafs_by_cid,
            "nav_lookup": self.nav_lookup,
        }
```

Caller pattern (locked, no protocol-side changes):

```python
materialized = materialize_wallet_data(wallet, window, ledger_root)
result = analyze_wallet(
    materialized.wallet_data.wallet,
    **materialized.to_analyze_wallet_kwargs(),
)
```

### D3a. `price_history_fn` contract — two surfaces

Existing protocol callers use `price_history_fn(token_id, ts) -> float | None`,
the spot-lookup signature defined at
`sniper/analysis/wallet_protocol/held_analyst.py:82` and
`sniper/analysis/wallet_protocol/flipper.py:66` as
`PriceHistoryFn = Callable[[str, int], float | None]`.

**Two surfaces:**

1. `price_history_fn(token_id: str, ts: int) -> float | None` — exact
   `PriceHistoryFn` signature. It mirrors
   `sniper/analysis/wallet_protocol/price_history_adapter.py:67-112`:
   query `market_price_points` for the token from `target_ts = ts` through
   `target_ts + max(fidelity_minutes * 60, 600)`, choose the first bar with
   `bar_ts >= target_ts`, fall back to the last bar in that tight forward
   window only if none reach `target_ts`, and return `None` when no usable
   bar exists. It never uses a nearest-bar or preceding-bar tolerance.
2. `get_price_bars(token_id: str) -> PriceBarsResult` — diagnostics. Returns
   the full bar list for the token in window, plus a `coverage_status` enum
   (`covered`, `gap`, `not_captured`) read from `coverage_ranges`. Not used
   by `analyze_wallet`; useful for materializer tests and manual probes.

Both surfaces share the same DuckDB cursor and fidelity assumption. The
nearest/tolerance rules from execution probes stay isolated to probe horizon
logic; they are not part of the H6/FH6 `PriceHistoryFn` contract.

### D4. `replay_surface_meta` content per surface

One entry per surface — events, positions, probes, flips, prices,
manifests. Each entry:

```python
{
    "ledger_source": "duckdb_warehouse",
    "wallet_path": str(...),
    "window_id": window.window_id,
    "capture_run_ids": list(window.capture_run_ids),
    "source_versions": dict(window.source_versions),
    "row_count": N,
    "captured_at_min": ...,  # min(captured_at) within the window for this surface
    "captured_at_max": ...,
}
```

`manifest_hashes` lives at the top of `MaterializedWalletData`, not inside
`replay_surface_meta`, because manifests are window-level artifacts, not
per-surface.

### D5. Per-hypothesis fail-closed contract — exhaustive over branch results

Audit caught that v2 misclassified H7 (it's a pre-pass returning
`StationarityResult`, not `HypothesisResult`) and omitted H2_sub080, H5,
FH4, FH5.

**Pre-pass results — out of scope for the per-hypothesis structure guard.**

| Pre-pass | Returns | Notes |
|---|---|---|
| H7 stationarity | `StationarityResult` | input: closed_positions ROI series |
| Flipper stationarity | `StationarityResult` | input: flips |

Pre-pass inputs are pre-filtered by the materializer (`closed_positions`,
`flips`) at construction time. The structure guard is not invoked on
them.

**Branch results — guarded explicitly.** Every emitted `HypothesisResult`
or `TagGrainResult` from `run_held_branch` / `run_flipper_branch` is in
this taxonomy:

| Result | Class | Reason |
|---|---|---|
| H1 | structure_tolerant | net PnL works on any structure with cost-basis + realized PnL |
| H2 | binary_outcome_required | needs win/loss labels from binary resolution |
| H2_sub080 | binary_outcome_required | same as H2; entry-price filter does not change structure dependence |
| H3 | binary_outcome_required | size vs win/loss |
| H4 | token_price_only | per-fill drift on token price |
| H5 (per tag grain `TagGrainResult`) | binary_outcome_required | tag-level win/loss |
| H6 | token_price_only | post-sell drift on token price |
| FH1 | token_price_only | realized BUY→SELL on token |
| FH2 | token_price_only | size vs flip ROI |
| FH3 | token_price_only | spread capture (uses `execution_profile` + token) |
| FH4 (per tag grain `TagGrainResult`) | token_price_only | tag-conditioned flip ROI |
| FH5 | structure_tolerant | hold duration, no resolution / no token price needed |
| FH6 | token_price_only | directional skill via post-sell price |

13 emitted result surfaces, exhaustively classified.

Mechanism:

```python
# sniper/analysis/wallet_protocol/protocol_guards.py

HYPOTHESIS_STRUCTURE_CLASS: dict[str, str] = {
    "H1": "structure_tolerant",
    "H2": "binary_outcome_required",
    "H2_sub080": "binary_outcome_required",
    "H3": "binary_outcome_required",
    "H4": "token_price_only",
    "H5": "binary_outcome_required",
    "H6": "token_price_only",
    "FH1": "token_price_only",
    "FH2": "token_price_only",
    "FH3": "token_price_only",
    "FH4": "token_price_only",
    "FH5": "structure_tolerant",
    "FH6": "token_price_only",
}

def market_structure_supported(
    hypothesis_id: str, rows: Sequence[Mapping]
) -> tuple[bool, "UnderpoweredReason | None"]:
    cls = HYPOTHESIS_STRUCTURE_CLASS[hypothesis_id]
    if cls == "structure_tolerant":
        return True, None
    if cls == "token_price_only":
        if any(r.get("token_id") for r in rows):
            return True, None
        return False, "no_token_history"
    if cls == "binary_outcome_required":
        binary_rows = [r for r in rows if r.get("market_structure") == "binary"]
        if not binary_rows:
            return False, "market_structure_unsupported"
        if any(r.get("outcome_index") is None for r in binary_rows):
            return False, "outcome_index_missing"
        return True, None
    raise ValueError(f"unknown structure class: {cls}")
```

Guard application happens in the materialized branch path, not in the legacy
leaf hypothesis functions. Direct calls such as `run_h2_excess_wr(...)`,
`run_h3_size_conviction(...)`, `run_h4_entry_timing(...)`, and the `run_fh*`
helpers keep their existing signatures and behavior for tests and non-ledger
callers. The branch/orchestrator receives materializer row metadata and
short-circuits individual branch result surfaces before calling the leaf when
structure is unsupported.

Whole-wallet verdict is poisoned only when EVERY active branch hypothesis
returns `underpowered` with a structure reason. Pre-passes that signal
do not poison the verdict (they re-shape the closed_positions subset and
the branch then evaluates the post-regime subset).

### D5a. `underpowered_reason` Literal extension — minimum disruption

Audit caught that v2 used a `limitation_reason` field that doesn't exist
on `HypothesisResult`. The right field is `underpowered_reason`, defined
as a `Literal[...]` at `sniper/analysis/wallet_protocol/types.py:63`.

**Fix:** extend the Literal with three new values, used only by the
structure guard:

```python
UnderpoweredReason = Literal[
    "n_below_threshold",
    "heavy_tail_inconclusive",
    "regime_change_too_recent",
    "no_execution_profile",
    "no_price_history_fn",
    "peer_count_below_floor",
    "zero_variance",
    # Slice 5 additions (market structure guard):
    "market_structure_unsupported",
    "outcome_index_missing",
    "no_token_history",
]
```

Verified consumers of `underpowered_reason` (audit grep, no exhaustive
matching anywhere):
* `types.py` — Literal definition.
* `report.py:50,99` — string interpolation only.
* `persistence.py:371` — pass-through to Supabase.
* `held_analyst.py`, `flipper.py` — call sites that assign reason strings.

The Literal extension is low-risk: no consumer breaks because no consumer
exhaustively matches the type. Tests for guarded hypotheses simply assert
the new reason strings appear when expected.

### D6. Position materialization — as-of snapshot, not `is_latest`

`is_latest` is a pointer to warehouse-head state. For an analysis window
that ends BEFORE warehouse head, `is_latest=true` returns nothing if a
later snapshot has flipped the bit.

**Fix: as-of snapshot query:**

```sql
WITH ranked AS (
    SELECT
        *,
        ROW_NUMBER() OVER (
            PARTITION BY position_uid
            ORDER BY captured_at DESC, capture_run_id DESC
        ) AS rn
    FROM wallet_positions
    WHERE wallet = ? AND captured_at <= ?  -- window.end_ts
)
SELECT * FROM ranked WHERE rn = 1
```

Then split by `status`:

* `open_positions = [row for row in as_of if row.status == "open"]`.
* `closed_positions = [row for row in as_of if row.status.startswith("closed_")
   and row.closed_at is not None
   and window.start_ts <= row.closed_at <= window.end_ts]`.

`is_latest` is reserved for warehouse-head queries (e.g. live dashboards)
and is never used by the materializer.

---

## File Structure

| Path | Slice | Responsibility |
|---|---|---|
| `sniper/analysis/wallet_protocol/types.py` | modify | Extend `UnderpoweredReason` Literal with three structure-guard values. |
| `sniper/analysis/wallet_protocol/ledger/window.py` | create | `AnalysisWindow` dataclass + `__post_init__` validation. |
| `sniper/analysis/wallet_protocol/ledger/materialize.py` | create | `materialize_wallet_data(wallet, window, ledger_root) -> MaterializedWalletData` + `MaterializedWalletData` dataclass. |
| `sniper/analysis/wallet_protocol/ledger/price_history.py` | create | `make_ledger_price_history_fn(conn, window) -> tuple[PriceHistoryFn, get_price_bars_fn]`. |
| `sniper/analysis/wallet_protocol/ledger/probes.py` | modify | Persist + read the five auxiliary fields. |
| `sniper/analysis/wallet_protocol/ledger/schema.py` | modify | `ALTER TABLE wallet_execution_probes ADD COLUMN …`. |
| `scripts/execution_profile.py` | modify (small) | `_record_to_fill_result` reads aux fields from rehydrated records (live-API path unchanged because record already has fields). |
| `sniper/analysis/wallet_protocol/protocol_guards.py` | create | `HYPOTHESIS_STRUCTURE_CLASS` + `market_structure_supported`. Pure helper; no live or DuckDB dependency. |
| `sniper/analysis/wallet_protocol/held_analyst.py` | modify | Add materialized-branch guard application around H1/H2/H2_sub080/H3/H4/H5 grain/H6 result construction without changing legacy leaf function signatures. H7 unaffected (pre-pass). |
| `sniper/analysis/wallet_protocol/flipper.py` | modify | Add materialized-branch guard application around FH1/FH2/FH3/FH4 grain/FH6 result construction without changing legacy leaf function signatures. FH5 + flipper stationarity unaffected. |
| `tests/test_wallet_ledger_window.py` | create | Frozen dataclass + canonical hashing + validation. |
| `tests/test_wallet_ledger_materialize.py` | create | Materializer reader paths + execution-profile aggregate parity. |
| `tests/test_wallet_ledger_price_history.py` | create | Both surfaces; coverage-status reporting. |
| `tests/test_wallet_ledger_probes.py` | modify | Auxiliary-field roundtrip assertions. |
| `tests/test_wallet_ledger_protocol_guards.py` | create | Per-hypothesis fail-closed for all 13 surfaces. |
| `tests/test_wallet_ledger_slice5_integration.py` | create | End-to-end through `analyze_wallet(**bundle.to_analyze_wallet_kwargs())`. |
| `docs/superpowers/specs/2026-05-02-wallet-fact-ledger-design.md` | modify | Spec §11 column list grows by five auxiliary fields. |

---

## Tasks

### Task 0: Probe Auxiliary Fields Schema Migration

**Files:**
- Modify: `sniper/analysis/wallet_protocol/ledger/schema.py`
- Modify: `sniper/analysis/wallet_protocol/ledger/probes.py`
- Modify: `tests/test_wallet_ledger_probes.py`
- Modify: `docs/superpowers/specs/2026-05-02-wallet-fact-ledger-design.md`

- [ ] Spec §11 amendment: add the five aux fields with rationale.
- [ ] Schema: extend `ensure_wallet_schema` to add columns NULLable.
- [ ] Probe writer: include aux fields in INSERT.
- [ ] Tests: assert roundtrip on a happy-path probe row.
- [ ] Migration regression: create an old-schema `wallet_execution_probes`
  table without the five aux columns, run `ensure_wallet_schema`, assert all
  five columns exist and a second call is idempotent.
- [ ] Run: `python3 -m pytest tests/test_wallet_ledger_probes.py
  tests/test_wallet_ledger_schema.py tests/test_execution_probes_iter.py
  tests/test_wallet_ledger_slice4_integration.py -q` — green.
- [ ] Commit as `feat(wallet-ledger): persist execution probe auxiliary fields`.

### Task 1: AnalysisWindow Dataclass

**Files:**
- Create: `sniper/analysis/wallet_protocol/ledger/window.py`
- Test: `tests/test_wallet_ledger_window.py`

- [ ] Failing tests: frozen + mutation refused; `start_ts > end_ts`
  rejected; empty `capture_run_ids` rejected; `source_versions` is
  tuple-of-tuples; identical-field windows hash equal; differing-by-any-
  field hash distinct.
- [ ] Implement.
- [ ] Commit as `feat(wallet-ledger): add AnalysisWindow dataclass`.

### Task 2: UnderpoweredReason Literal Extension

**Files:**
- Modify: `sniper/analysis/wallet_protocol/types.py`
- Test: extend an existing types test or create
  `tests/test_wallet_protocol_types_underpowered_reason.py` for the
  three new values.

- [ ] Failing test: assert
  `"market_structure_unsupported"`, `"outcome_index_missing"`,
  `"no_token_history"` are all valid Literal members; assigning any of
  them to `HypothesisResult.underpowered_reason` does not raise; existing
  values still accepted.
- [ ] Implement (one-line edit).
- [ ] Commit as
  `feat(wallet-protocol): extend UnderpoweredReason for structure guard`.

### Task 3: Protocol Guards Module

**Files:**
- Create: `sniper/analysis/wallet_protocol/protocol_guards.py`
- Test: `tests/test_wallet_ledger_protocol_guards.py`

- [ ] Failing tests for `market_structure_supported`:
  - structure_tolerant returns `(True, None)` regardless of rows.
  - token_price_only returns `(True, None)` when ANY row has non-null
    `token_id`; `(False, "no_token_history")` otherwise.
  - binary_outcome_required returns `(False, "market_structure_unsupported")`
    when no row has `market_structure = "binary"`;
    `(False, "outcome_index_missing")` when binary rows exist but
    `outcome_index` is None on any; `(True, None)` otherwise.
  - Every hypothesis_id in `HYPOTHESIS_STRUCTURE_CLASS` resolves; an
    unknown id raises `ValueError`.
- [ ] Implement.
- [ ] Commit as
  `feat(wallet-protocol): per-hypothesis market structure guard`.

### Task 4: Wire Guards Into Branches

**Files:**
- Modify: `sniper/analysis/wallet_protocol/held_analyst.py`
- Modify: `sniper/analysis/wallet_protocol/flipper.py`
- Modify: `sniper/analysis/wallet_protocol/h5_tag_edge.py` (if H5 grain runner lives there)
- Tests: extend `tests/test_wallet_ledger_protocol_guards.py`.

- [ ] Failing tests: through the materialized branch path, feed only
  non-binary row metadata to H2 / H2_sub080 / H3 / H5 grain → result
  `verdict="underpowered"` with
  `underpowered_reason="market_structure_unsupported"`. Feed
  no-token-history metadata to H4 / H6 / FH1 / FH2 / FH3 / FH4 / FH6 →
  `underpowered` + `no_token_history`. Feed mixed structure → only
  affected hypotheses return underpowered; others run.
- [ ] Backward-compat tests: direct legacy calls to `run_h2_excess_wr`,
  `run_h3_size_conviction`, `run_h4_entry_timing`, and representative
  `run_fh*` helpers with plain dict fixtures still run exactly as before
  when no materializer row metadata is supplied.
- [ ] Failing test for whole-wallet verdict: every branch hypothesis
  underpowered for structure → wallet-level verdict carries the
  structure limitation; at least one supported → wallet-level verdict
  proceeds normally.
- [ ] Implement guard calls at branch/orchestrator result construction
  boundaries, not inside leaf helpers. Materialized metadata is required
  for fail-closed ledger analysis; missing metadata on direct legacy calls
  must not produce structure underpowering.
- [ ] Commit as
  `feat(wallet-protocol): apply structure guard to branch hypotheses`.

### Task 5: Materializer Reader

**Files:**
- Create: `sniper/analysis/wallet_protocol/ledger/materialize.py`
- Test: `tests/test_wallet_ledger_materialize.py`

- [ ] Failing tests:
  - `MaterializedWalletData` returned with `wallet_data`, `price_history_fn`,
    `peer_baseline_fn=None` (Slice 5 omits peer baseline source — flagged
    as out-of-scope below), `leafs_by_cid=None`, `nav_lookup=None`,
    `replay_surface_meta`, `manifest_hashes`, `row_counts`.
  - `to_analyze_wallet_kwargs()` returns dict with exact keys
    `pre_fetched_data`, `price_history_fn`, `peer_baseline_fn`,
    `leafs_by_cid`, `nav_lookup` — keys verified against the actual
    `analyze_wallet` signature.
  - Events read filtered to `ts BETWEEN start_ts AND end_ts AND
    ingest_status = 'normalized'`.
  - Activity rows reconstructed with `event_uid` field stamped from
    `wallet_events.event_uid`.
  - Open / closed positions selected via the as-of query (D6); a snapshot
    OUTSIDE the window's ts range excluded even with `is_latest=true`.
  - Closed positions filtered additionally by `closed_at` in window.
  - `replay_surface_meta` has one entry per surface with the D4 keys.
  - `wallet_data.price_history_lookups` populated from the same source as
    `price_history_fn` (latest bar per token within window).
- [ ] Implement.
- [ ] Commit as `feat(wallet-ledger): add materialize_wallet_data reader`.

### Task 6: Price-History Two-Surface Closure

**Files:**
- Create: `sniper/analysis/wallet_protocol/ledger/price_history.py`
- Test: `tests/test_wallet_ledger_price_history.py`

- [ ] Failing tests:
  - `price_history_fn(token_id, ts)` matches the
    `Callable[[str, int], float | None]` signature; mirrors
    `build_price_history_fn` by selecting the first bar at or after `ts`
    inside `[ts, ts + max(fidelity_minutes * 60, 600)]`, falling back to
    the last bar in that tight forward window only when no bar reaches
    `ts`, and returning `None` when coverage is missing.
  - Explicit regression: a previous bar closer to `ts` than the forward bar
    is ignored; nearest/predecessor lookup must not be used here.
  - `get_price_bars(token_id)` returns full bar list + `coverage_status`
    enum (`covered`, `gap`, `not_captured`) read from `coverage_ranges`.
  - No `PolymarketClient` import on the module (verify via grep).
- [ ] Implement.
- [ ] Commit as
  `feat(wallet-ledger): add ledger price-history closures`.

### Task 7: Execution Profile Re-Aggregation Parity

**Files:**
- Modify: `scripts/execution_profile.py` (small; record-rehydration helper)
- Test: extend `tests/test_wallet_ledger_materialize.py`

- [ ] Failing parity test: seed activity with the deterministic 30-fill
  fixture from `tests/fixtures/execution_profile/aggregate_baseline_v1.json`,
  capture via the Slice 1–4 path (now persisting aux fields per Task 0),
  materialize, then assert the materialized execution_profile envelope
  equals the baseline byte-for-byte.
- [ ] If `_record_to_fill_result` needs a wrapper to load from DuckDB
  rather than from in-memory `FillProbeRecord`, add it here. Otherwise
  the existing helper works directly because the record now carries the
  aux fields end-to-end.
- [ ] Commit as
  `test(wallet-ledger): verify materialized execution profile parity`.

### Task 8: Slice 5 Integration

**Files:**
- Test: `tests/test_wallet_ledger_slice5_integration.py`

- [ ] End-to-end deterministic fixture: seed activity (binary fixture +
  one multi-outcome row) → run Slices 1–4 writers → call
  `materialize_wallet_data(wallet, window, ledger_root)` →
  `analyze_wallet(materialized.wallet_data.wallet,
   **materialized.to_analyze_wallet_kwargs())` → assert:
    - `analyze_wallet` returns a `ProtocolReport` (no signature mismatch).
    - The report references the `replay_surface_meta` provenance via
      `WalletData.replay_surface_meta`.
    - Non-binary rows produce
      `underpowered_reason="market_structure_unsupported"` on H2/H3/H5 grains.
    - Structure-tolerant H1 still runs.
    - Whole-wallet verdict is NOT poisoned when at least one supported
      hypothesis exists.
- [ ] Implement.
- [ ] Commit as `test: verify wallet ledger slice five integration`.

---

## Slice 5 Verification

- [ ] `python3 -m pytest tests/ -q` — full suite green.
- [ ] `python3 scripts/validate_schema_refs.py` — clean.
- [ ] `python3 scripts/brain_index.py generate` — no diff.
- [ ] No live API calls anywhere in Slice 5 paths. Verify by grep:
  `grep -l 'PolymarketClient\|requests\.\|httpx\.'
   sniper/analysis/wallet_protocol/ledger/materialize.py
   sniper/analysis/wallet_protocol/ledger/price_history.py
   sniper/analysis/wallet_protocol/ledger/window.py
   sniper/analysis/wallet_protocol/protocol_guards.py` returns empty.
- [ ] `materialize_wallet_data` is idempotent: same wallet + same window
  → byte-for-byte equal `MaterializedWalletData` dump (excluding the two
  callable surfaces, which are identity-compared by source code).

## Out Of Scope

- `peer_baseline_fn` source from ledger (H5/FH4 baselines) — peer
  baselines are not yet a ledger surface. Slice 5 returns
  `peer_baseline_fn=None`, which makes H5 / FH4 emit `underpowered`
  with `underpowered_reason="peer_count_below_floor"` (existing
  behaviour). Promoting peer baselines into the ledger is its own slice.
- `leafs_by_cid` / `nav_lookup` source from ledger — same status; tag
  surfaces stay `None`. H5 grain runner already handles `None` by
  emitting `inference_not_run` per existing Phase 2 logic.
- Supabase output writers — Slice 6.
- Re-baselining the 10 calibration wallets — Slice 6 + requires live
  capture infrastructure.
- Brain index DuckDB introspection — orthogonal standing gap.
