# G2 — `closed_positions_fetched` / `closed_positions_cap_hit` on `data_completeness`

**Date:** 2026-05-17
**Status:** spec → tests → implementation
**Origin:** `docs/audits/2026-05-10-wallet-analyzer-live-sampling.md` §"Gaps / mistakes" → G2
**Scope:** Surface existing `fetch_termination["closed_positions"]` signal + `n_closed_positions` count onto `ProtocolReport.data_completeness` so downstream consumers (Strategy Pattern Audit, dossier writer, calibration replay) can read closed-position cap state from the same place they read activity coverage. No fetch changes. No schema migration. No new collection.

## 0. Doctrine

The wallet analyzer is a strategy-intelligence system, not a copy bot. Closed-position fetch cap is currently visible only via two indirect signals — `report.time_coverage["fetch_termination"]["closed_positions"] == "api_refused"` and the raw count `report.n_closed_positions == 5000`. Reviewers reading the report or audit consumers reading the JSON have to know to cross-reference both. The live-sampling review (2026-05-10) flagged this as a "blind spot" for Pestle and swisstony, who both returned exactly 5000 closed positions but the report's `data_completeness` section made no mention of a cap. PnL and hypothesis denominators are not necessarily complete when the closed-position fetch was capped; the report should say that as plainly as it says activity coverage.

## 1. Problem

```text
overall_verdict = edge_detected (Pestle)
n_closed_positions = 5000
time_coverage.fetch_termination.closed_positions = "api_refused"
data_completeness = {
  activity_capture: {…},
  attribution_coverage: {…},
  caveats: {…},
}
```

`data_completeness` does not name the closed-position cap. Consumers — including the Strategy Pattern Audit rule predicates and the operator reading the rendered report — need an explicit "closed positions were truncated" flag co-located with the rest of the completeness block.

## 2. What ships

### 2.1 New `data_completeness` subkey

A new top-level key `closed_positions_capture` on the `data_completeness` dict produced by `analyze_wallet`:

```python
data_completeness["closed_positions_capture"] = {
    "closed_positions_fetched": int,
    "closed_positions_cap_hit": bool,
}
```

### 2.2 Field semantics

- `closed_positions_fetched` — count of closed positions actually retrieved by the fetcher. Source: `report.n_closed_positions`. Always present, always non-negative. `0` is a valid value (wallets with no closed history).
- `closed_positions_cap_hit` — `True` iff `data.fetch_termination` has `"closed_positions" == "api_refused"`. `False` for `"empty_page"`, `"partial_page"`, missing key, or `fetch_termination is None`. The boolean is therefore conservative: it only fires when we have a clear cap signal, not when termination data is absent.

### 2.3 Existing surfaces unchanged

- `report.time_coverage["fetch_termination"]` continues to carry the raw enum string for `closed_positions` (and `open_positions`, `activity`). This is the source. Other consumers that already read `time_coverage` keep reading it.
- `report.n_closed_positions` unchanged.
- `data_completeness` keys `activity_capture`, `attribution_coverage`, and `caveats` unchanged.

### 2.4 Renderer (markdown report)

The completeness block in `sniper/analysis/wallet_protocol/report.py` should render one new line under the existing "Data completeness" section:

```text
Closed positions: <N> fetched (<cap_hit_marker>)
```

where `<cap_hit_marker>` is either `"capture complete"` (when `closed_positions_cap_hit is False`) or `"CAP HIT — closed history may be truncated"` (when `True`). Co-located with the existing activity capture line so operators see them together.

If the markdown render is in a separate slice / not co-located, that's fine — the JSON field is the primary contract; the render is a niceness.

### 2.5 Caveat propagation

When `closed_positions_cap_hit` is `True`, `data_completeness["caveats"]["closed_positions_truncated"] = True` is also set so the existing caveat-rendering logic surfaces it alongside `activity_truncated`. Caveats key default `False` when no cap.

### 2.6 Non-changes

- `analyze_wallet(...)` signature unchanged.
- `ProtocolReport` dataclass schema unchanged (no new fields; we extend the existing `data_completeness` dict in place).
- No fetcher changes — the cap signal already exists, we just plumb it.
- No new constants, no new env vars.

## 3. Acceptance tests

All tests live in `tests/test_wallet_protocol_completeness_closed_positions.py` (new file). The existing key-set assertion in `tests/test_wallet_protocol_completeness_surface.py::test_data_completeness_field_populated_after_analyze_wallet` needs the new key added to its expected set — that update is part of this slice's implementation step, not a separate slice.

### 3.1 Field presence + types

```text
test_closed_positions_capture_block_always_present
  Run analyze_wallet on a WalletData with any closed positions; assert
  data_completeness["closed_positions_capture"] is a dict with keys
  exactly {"closed_positions_fetched", "closed_positions_cap_hit"}.
  Fetched is int >= 0; cap_hit is bool.

test_closed_positions_capture_block_present_when_zero_closed
  Run analyze_wallet on a WalletData with closed_positions=[] (no closed
  history). Assert the block is still present, fetched == 0, cap_hit == False.
```

### 3.2 Fetched count

```text
test_closed_positions_fetched_matches_n_closed_positions
  Build a WalletData with N=5 closed positions. After analyze_wallet,
  assert data_completeness["closed_positions_capture"]["closed_positions_fetched"]
  == report.n_closed_positions == 5.

test_closed_positions_fetched_is_int_not_none
  Even on a fresh WalletData with no closed history, the field must be
  the integer 0, NOT None. Pin this so JSON consumers can rely on the type.
```

### 3.3 Cap-hit semantics

```text
test_cap_hit_true_when_fetch_termination_api_refused
  WalletData with fetch_termination={"closed_positions": "api_refused"};
  assert closed_positions_cap_hit is True.

test_cap_hit_false_when_fetch_termination_empty_page
  WalletData with fetch_termination={"closed_positions": "empty_page"};
  assert closed_positions_cap_hit is False.

test_cap_hit_false_when_fetch_termination_partial_page
  WalletData with fetch_termination={"closed_positions": "partial_page"};
  assert closed_positions_cap_hit is False.

test_cap_hit_false_when_fetch_termination_missing_closed_positions_key
  WalletData with fetch_termination={"activity": "empty_page"} (no
  closed_positions key); assert closed_positions_cap_hit is False
  (conservative: absence-of-signal != cap).

test_cap_hit_false_when_fetch_termination_is_none
  WalletData with fetch_termination=None; assert
  closed_positions_cap_hit is False.
```

### 3.4 Caveat propagation

```text
test_caveat_closed_positions_truncated_fires_when_cap_hit
  fetch_termination={"closed_positions": "api_refused"};
  assert data_completeness["caveats"]["closed_positions_truncated"] is True.

test_caveat_closed_positions_truncated_false_when_not_cap_hit
  fetch_termination={"closed_positions": "empty_page"};
  assert data_completeness["caveats"]["closed_positions_truncated"] is False.
```

### 3.5 Existing keys unchanged

```text
test_existing_data_completeness_keys_still_present
  After analyze_wallet, assert data_completeness has all of
  {"activity_capture", "attribution_coverage", "caveats",
   "closed_positions_capture"} — no removed key, no renamed key. This
  is a defensive pin against future refactors that move semantics
  between blocks.
```

### 3.6 Acceptance ceiling

Ten tests in the new file (2 + 2 + 5 + 2 + 1 = 12 — let me recount: 3.1 is 2, 3.2 is 2, 3.3 is 5, 3.4 is 2, 3.5 is 1. Total: 12). Plus the existing key-set assertion in `tests/test_wallet_protocol_completeness_surface.py` needs widening to include `closed_positions_capture` — that's a one-line change to an existing test, not a new test.

## 4. Implementation surface

| File | Change |
|---|---|
| `sniper/analysis/wallet_protocol/analyze_wallet.py` | Add `closed_positions_capture` sub-dict to the `data_completeness` literal. Pull `closed_positions_fetched` from `report.n_closed_positions`, `closed_positions_cap_hit` from `data.fetch_termination`. Set `caveats["closed_positions_truncated"] = closed_positions_cap_hit`. |
| `tests/test_wallet_protocol_completeness_surface.py` | Widen the existing expected key set to include `closed_positions_capture`. |
| `sniper/analysis/wallet_protocol/report.py` | (Optional) Add one rendered line in the completeness section. Implementer may defer to a follow-up slice if the renderer surface is more complex than expected; spec calls the JSON field as the primary contract. |

No other file touched.

## 5. Risks

1. **`fetch_termination` shape drift.** The audit confirms shape `{"closed_positions": str, "open_positions": str, "activity": str}` and the producer at `sniper/analysis/wallet_protocol/fetch.py:185` writes the `.reason` enum string. If a future fetcher emits a non-string or restructures, the boolean check `== "api_refused"` becomes wrong. Mitigated by the explicit unit tests in §3.3.
2. **Caveat consumer breakage.** Existing renderer/consumers that iterate the `caveats` dict will see a new key. Markdown render typically renders all caveat keys uniformly; if any consumer hardcodes the four current keys (`activity_truncated`, `subgraph_unmapped_fills`, `attribution_coverage_low`, `limitation_present`), they need updating. Grep for the keys before merging.
3. **The renderer line is operator-visible.** If the markdown rendering doesn't land in this slice, an operator looking at the rendered report has to read the JSON to see the cap state. Acceptable for the first slice; tracked separately if a follow-up is needed.
4. **Test idempotency.** The fetched count comes from `report.n_closed_positions` which is computed earlier in `analyze_wallet` from `len(data.closed_positions)`. Pinning the count to that source means any future change to `n_closed_positions` automatically propagates. Good.

## 6. Out of scope

- Closed-position fetch cap mitigation (paginating past cap, raising the cap, alternative source). G2 is a transparency fix only; the underlying data limit stays as-is.
- Adding similar fields for `open_positions` (the audit only flagged the closed cap). Symmetric work can land separately if needed.
- Strategy Pattern Audit rule changes to actually consume the new field. G2 ships the surface; rule consumption is downstream and out of scope here.
- The G3, G4, G5, G6 slices.

## 7. Knowledge sync expectations

- `docs/audits/2026-05-10-wallet-analyzer-live-sampling.md` G2 status → resolved with commit ref.
- No AGENTS.md surface change expected (no new CLI flag, no new workflow).
- No Supabase memory write (this is an implementation surface, not new doctrine).

## 8. Done definition

- 12 acceptance tests in `tests/test_wallet_protocol_completeness_closed_positions.py` red, then green.
- Existing key-set test in `tests/test_wallet_protocol_completeness_surface.py` updated and green.
- `python3 -m pytest tests/test_wallet_protocol_completeness*.py -q` passes.
- `python3 scripts/validate_schema_refs.py` passes.
- `git diff --stat` shows only the analyzer + spec + two test files (and optionally the renderer).
