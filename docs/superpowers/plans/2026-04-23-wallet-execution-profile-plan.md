# Wallet Execution Profile — Implementation Plan

**Date:** 2026-04-23
**Spec:** [2026-04-23-wallet-execution-profile-design.md](../specs/2026-04-23-wallet-execution-profile-design.md)
**Companion:** [MVE results](../../research/scopes/2026-04-22-execution-mve-results.md), [arb control](../../research/scopes/2026-04-22-execution-mve-arb-control.md)

---

## Ship order

Five phases, each independently shippable and testable. Commits must pass `python3 -m pytest tests/ -q` and leave the existing extractor behavior unchanged when `--with-execution` is not set.

| Phase | Scope | Deliverable | Gate to next phase |
|-------|-------|-------------|--------------------|
| A | Module scaffold + CLI flag + stub emitter | `execution_profile` key always in envelope; stub when flag off | Unit tests green; running extractor on any wallet without flag produces stub |
| B | Per-fill computation | Preceding-bar, tape-midpoint, drift, in-play + thin-market gates | Arb probe fixture reproduces (±1e-4) the aggregate skew/drift numbers committed in MVE memo |
| C | Aggregation + calibration signature + sample tiers | Populated `execution_profile` block | Running on kch123 / SwissMiss-equivalent produces expected distribution shape |
| D | Layer 3 mapping | `entry_timing_edge` promotes when drift evidence present | SwissMiss-equivalent moves from `unknown` → `medium`; arb stays `low`/`unknown` |
| E | End-to-end fixture tests + rollout note | 3 recorded fixtures as acceptance tests | All tests green, HEAD pushed |

Each phase = one commit (pre-commit hook runs pytest + schema validator).

---

## Phase A — Scaffold

### Files created

1. `scripts/execution_profile.py` — new module, public surface:

```python
def compute_execution_profile(
    wallet: str,
    activity: list[ActivityRow],
    client: PolymarketClient,
    *,
    force: bool = False,
    max_trades: int | None = None,
) -> dict:
    """Return execution_profile dict per spec §5.

    In Phase A, always returns a stub with status='not_run' unless explicitly
    wired. Later phases fill in the real path.
    """
```

Also internal helpers (filled in Phase B):

```python
def _should_run(classification: dict, counts: dict) -> tuple[bool, str]:
    """Returns (run, reason). Implements spec §7.1 gating."""

def _stub(reason: str) -> dict:
    return {"status": "not_run", "reason": reason}
```

2. `tests/test_execution_profile.py` — new test file with:

```python
def test_stub_shape_when_flag_off()
def test_stub_reason_layer1_terminal()
def test_stub_reason_insufficient_activity()
def test_force_flag_overrides_layer1_terminal_skip()
def test_multi_outcome_basket_builder_is_not_skipped()  # spec §7.1 correction
```

### Files modified

1. `scripts/wallet_archetype_extractor.py`:

   a. Add `argparse` flag `--with-execution` (default False) and `--force-execution` (default False; only meaningful with `--with-execution`).
   b. In `extract_wallet_archetype(wallet, ..., with_execution=False, force_execution=False)`, after classification, compute `execution_profile` and attach to envelope:

      ```python
      features["execution_profile"] = (
          compute_execution_profile(
              wallet,
              activity_rows,  # already fetched
              client=PolymarketClient(),  # shared
              force=force_execution,
          )
          if with_execution
          else _stub("shortlist_gate_not_met")
      )
      ```

   c. Envelope top-level key order: observations, behavior_pattern, edge_hypotheses, outcome_attribution, operational_implications, **execution_profile**, data_quality.

### Acceptance

- `python3 -m pytest tests/test_execution_profile.py -q` green (all stub-path tests pass).
- Running the extractor CLI without `--with-execution` still succeeds; envelope has `execution_profile={"status": "not_run", "reason": "shortlist_gate_not_met"}`.
- No new API calls issued when flag is off (easy to verify by monkeypatching `PolymarketClient.__init__` in a test).
- Existing tests stay green.

---

## Phase B — Per-fill computation

### Files modified

1. `scripts/execution_profile.py`:

   Port (not import) these helpers from `scripts/feasibility/probe_execution_reference.py`:
   - `_preceding_bar(history, trade_ts)` — spec §6.2 step 2.
   - `_bar_at_offset(history, trade_ts, offset_sec)` — spec §6.2 step 4.
   - `_tape_midpoint(client, condition_id, asset, trade_ts, exclude_wallet)` — spec §6.2 step 3.

   Add new:
   - `_resolve_ts_lookup(condition_id, *, sqlite_conn=None, gamma_client=None, _cache=None)` — per spec §6.2 step 5. Priority:
     1. `SELECT end_date FROM market_metadata WHERE condition_id = ?` (parse ISO → unix ts).
     2. Gamma API fallback via `https://gamma-api.polymarket.com/markets?conditionId=<cid>`.
     3. Return `None` on both misses; caller treats as `resolution_ts_unknown=true`.
     Cache is an in-process dict keyed on `condition_id`.

   Add the per-fill probe function:

   ```python
   @dataclass
   class FillResult:
       trade_ts: int
       condition_id: str
       asset: str
       title: str
       fill_price: float
       ref_preceding: float | None
       ref_tape: float | None
       tape_n: int
       drift_1h: float | None
       drift_6h: float | None
       drift_24h: float | None
       in_play: bool
       thin_market: bool
       resolution_ts_unknown: bool

   def _probe_fill(
       client: PolymarketClient,
       fill: tuple[int, str, str, str, float],
       wallet: str,
       history_cache: dict[str, list[dict]],
       resolve_cache: dict[str, int | None],
   ) -> FillResult
   ```

2. Batch rule: `history_cache` keyed on `asset_id`, spans a single `get_prices_series(asset_id, min_trade_ts-1800, max_trade_ts + 24*3600 + 300)`. Batched per wallet (spec §8: ~distinct_assets calls vs `trade_count`).

### Files created

1. `tests/fixtures/execution_profile/arb_probe.json` — copied from `/tmp/mve_arb.json` (the one live fixture we have). Keep the full `probes` list so aggregation can be re-exercised offline.

### Acceptance

- Unit tests:
  - `test_preceding_bar_returns_last_strictly_earlier_within_window`
  - `test_preceding_bar_none_when_gap_over_30min`
  - `test_tape_midpoint_excludes_own_wallet`
  - `test_resolve_ts_lookup_sqlite_hit`
  - `test_resolve_ts_lookup_gamma_fallback` (monkeypatch HTTP)
  - `test_resolve_ts_lookup_double_miss_returns_none`
  - `test_bar_at_offset_respects_tolerance`
  - `test_in_play_flag_boundary_at_2h` (1h59m → True, 2h01m → False)
  - `test_thin_market_flag_when_tape_n_lt_3`
- Replay: unit test loads `arb_probe.json`, re-runs aggregator (Phase C input) against the stored `probes` list, asserts skew/drift summary numbers within ±1e-4 of stored `summary`.

---

## Phase C — Aggregation + calibration

### Files modified

1. `scripts/execution_profile.py`: add:

   ```python
   def _dist_summary(values: list[float]) -> dict
   def _aggregate(fills: list[FillResult]) -> dict
   def _calibration_signature(skew: dict, drift_1h: dict) -> dict
   def _confidence_tier(n_skew: int, n_drift: int) -> str
   ```

   Wire `compute_execution_profile` to:
   1. Collect BUYs from activity (side=='BUY', price>0, asset & condition_id present, usdc_size or size known).
   2. Cap to `max_trades` oldest-first (default: no cap; implementation hard ceiling 5 min wall-clock per spec §6.7).
   3. For each, call `_probe_fill` with shared caches.
   4. Call `_aggregate(fills)` to produce the block.
   5. Set `status = "populated" | "insufficient_sample" | "api_failure"` per spec §9.

2. Aggregation rules (spec §6.3):
   - `skew_vs_preceding_bar.samples` = fills where `in_play==False and thin_market==False and ref_preceding is not None`.
   - `skew_vs_tape_5min.samples` = fills where `tape_n >= 3 and ref_tape is not None`.
   - `drift_Xh.samples` = fills with non-null `drift_Xh`; `in_play` fills kept; note `in_play_count` alongside.
   - `tape_trade_count_median` = `statistics.median([f.tape_n for f in fills])`.

3. Sample tiers (spec §6.4):
   - `n_eff = min(n_skew, n_drift_1h) if both populated else max(n_skew, n_drift_1h)`.
   - tier thresholds exactly per spec.

4. Calibration signature (spec §6.5):
   - `central_tendency_near_zero = abs(median) <= 0.02 and abs(mean) <= 0.03`
   - `drift_near_zero = abs(drift_1h.median) <= 0.02 if drift_1h.n >= 10 else None`

### Acceptance

- Unit tests for `_aggregate`:
  - `test_in_play_fills_excluded_from_skew_kept_for_drift`
  - `test_thin_market_fills_excluded_from_skew`
  - `test_tape_midpoint_distribution_requires_tape_n_ge_3`
  - `test_confidence_tier_boundaries` (29→unknown, 30→medium, 99→medium, 100→high)
  - `test_calibration_signature_pass`
  - `test_calibration_signature_fail_on_wide_mean`
  - `test_calibration_signature_drift_none_when_n_lt_10`
- Replay: `test_arb_probe_aggregates_to_committed_numbers` — loads `arb_probe.json`, runs `_aggregate`, asserts skew_median ≈ +0.005, drift_1h_mean ≈ -0.009, calibration_signature.central_tendency_near_zero == True, calibration_signature.drift_near_zero == True.

---

## Phase D — Layer 3 mapping

### Files modified

1. `scripts/wallet_archetype_extractor.py:_build_edge_hypotheses`:

   Before returning, if `features.get("execution_profile", {}).get("status") == "populated"`, upgrade `entry_timing_edge` per spec §6.6:

   ```python
   ep = features.get("execution_profile", {})
   if ep.get("status") == "populated":
       drift = ep.get("drift_1h", {})
       tier = ep.get("confidence_tier")
       n = drift.get("n") or 0
       if n >= 10:
           dm = drift.get("mean") or 0.0
           dmed = drift.get("median") or 0.0
           if dm >= 0.05 and dmed >= 0.03 and tier == "high":
               entry_timing = _edge_signal("high", [...])
           elif dm >= 0.02 and dmed >= 0.01:
               entry_timing = _edge_signal("medium", [...], notes="")
           elif dm <= -0.02:
               entry_timing = _edge_signal("low", [...])
           else:
               # keep existing heuristic but append drift rationale
               entry_timing["rationale"].append(f"drift_1h_mean={dm:+.3f} (near-zero)")
   ```

   `exit_management_edge` v1: only append `drift_1h_mean=X` to rationale for reviewer cross-ref (spec §6.6 v1 caveat).

### Acceptance

- `test_entry_timing_promotes_to_medium_on_positive_drift`
- `test_entry_timing_stays_low_on_negative_drift`
- `test_entry_timing_keeps_heuristic_when_drift_near_zero`
- `test_entry_timing_unchanged_when_execution_profile_not_populated`

---

## Phase E — End-to-end + rollout

### Files created

1. `tests/fixtures/execution_profile/kch123_probe.json` — re-run probe once on kch123, commit output.
2. `tests/fixtures/execution_profile/swissmiss_probe.json` — same, SwissMiss.
3. `tests/test_execution_profile_e2e.py` — loads all three fixtures, runs aggregator + Layer 3 mapping, asserts:
   - kch123 → central tendency near zero, n_drift low, calibration pass, `entry_timing_edge.level == "unknown"` (not enough drift samples in intraday-resolving sports markets).
   - SwissMiss → drift_1h positive, `entry_timing_edge.level == "medium"`, `notes == ""`.
   - Arb → central tendency near zero, drift near zero, `entry_timing_edge.level == "unknown"` with drift rationale appended.

### Rollout steps (manual, after Phase E commits)

1. `python3 -m pytest tests/ -q` on HEAD — must be green.
2. `python3 scripts/brain_index.py generate` if any new table/column was introduced (Phase A–E add none; should be no-op).
3. Run `python3 -c "from scripts.wallet_archetype_extractor import extract_wallet_archetype; ..."` on 5 wallets from `knowledge/wallets/` with `--with-execution` and spot-check output.
4. Compare Layer 3 `entry_timing_edge` across 5 wallets vs prior run — document in `knowledge/wallets/<wallet>.md` HUMAN block.
5. Push branch; no service restart needed (extractor is a script, not a daemon).

Shortlist runner (scheduled execution-profile pass on followed + watchlist) is **out of scope**. Documented in spec §7.3 as follow-up.

---

## Test plan total

| Category | Count |
|----------|-------|
| Unit (A + B + C + D) | ~22 |
| Replay (fixtures) | 3 |
| E2E | 1 |
| Total new | ~26 |

Baseline drifts — run `python3 -m pytest tests/ --collect-only -q | tail -1` at Phase start; target +~26 new across all phases. `python3 scripts/validate_schema_refs.py` stays green (no new SQL).

---

## Risk register

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| `market_metadata.end_date` has partial coverage; many wallets' old conditions aren't in our local table | high | Gamma API fallback (spec §6.2 step 5); cache hits amortize cost |
| Gamma API rate-limits or schema-drifts | low | Keep fallback defensive; `resolution_ts_unknown=true` is a valid terminal state |
| API call budget blows through for SwissMiss-tier wallets (500+ fills → ~500 trades_window calls) | medium | Phase C implements max_trades ceiling; spec §6.7 hard-stops at 5 min wall-clock |
| Drift cross-reference promotes `entry_timing_edge` too aggressively on small-n fills | medium | `n >= 10` gate + `confidence_tier == high` gate on promotion to `high` |
| Fixture drift (recorded probe outputs go stale as Polymarket changes data shape) | low | Fixtures are snapshots of our own probe outputs, not Polymarket responses; they don't drift with the API |
| Test coverage gap: no `multi_outcome_basket_builder` fixture | low | Spec §10.4 future work; not a Phase E blocker. Add as QA after rollout |

---

## Dependencies / prerequisites

- `data.polymarket_activity_client.iter_activity` — exists.
- `data.polymarket_client.PolymarketClient.get_prices_series` — exists, signature `(token_id, start_ts, end_ts, fidelity=1)`.
- `data.polymarket_client.PolymarketClient.get_trades_window` — exists, signature `(condition_id, start_ts, end_ts, page_size, max_pages)`.
- `market_metadata` SQLite table with `condition_id, end_date` columns — exists (populated by streamer).
- `scripts/wallet_archetype_extractor.py:_build_edge_hypotheses` — entry point for Phase D.
- No new dependencies, no new schema.

---

## Open items deferred to post-ship

- Exit-side execution measurement (spec §6.6 v2).
- Layer 4 outcome attribution cross-check (spec §12 open question 3).
- Market-category-dependent in-play threshold (spec §12 open question 5).
- Shortlist runner scheduling (spec §7.3).

---

## Bottom line

Five incremental commits. Each phase testable on its own. Final acceptance = the three MVE probe outputs reproducing through the production path. No scope creep past the locked spec. Ready to execute Phase A on the next turn.
