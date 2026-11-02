# Phase 2B-fix: Analyzer Semantic Bugs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land tests + fixes for the four semantic bugs the Phase 2A audit found in the wallet-analysis protocol (FH4 baseline-unit mismatch, low-population grain promotion, FH3 zero-cost fallback, FH6 +24h offset on both endpoints), so that Phase 2B-capture (richer fixture surfaces) cannot encode broken behavior into v2 calibration baselines.

**Architecture:** Each bug becomes one TDD task — failing regression test first, then minimal code change, then verification that the test passes. Bugfixes touch only `sniper/analysis/wallet_protocol/{flipper.py,held_analyst.py,price_history_adapter.py}` and add tests under `tests/test_wallet_protocol_*.py`. No fixture-schema changes, no new DB tables, no protocol-version bump (the bugs are correctness fixes, not output-shape changes). Final task re-runs the v2 calibration harness + report and explains any baseline delta before declaring 2B-fix done.

**Tech Stack:** Python 3, pytest, existing protocol modules, sqlite (for `db.peer_baseline_for_grain` regression check), `unittest.mock` for closure stubs.

---

## Task 1: FH4 unit mismatch — suppress until flip-specific peer baseline exists

**Files:**
- Test: `tests/test_wallet_protocol_fh4_unit_mismatch.py` (create)
- Modify: `sniper/analysis/wallet_protocol/flipper.py:218-261` (`_build_fh4_grain`)

The current code subtracts a held-only win rate from a flip ROI. Operator-approved fix from the audit: suppress FH4 (return `inference_not_run` for every grain) until a flip-specific peer aggregate exists. This is the surgical fix; building a `wallet_tag_flip_pnl` table is its own line item.

- [ ] **Step 1: Write failing test**

```python
# tests/test_wallet_protocol_fh4_unit_mismatch.py
"""FH4 must NOT consume db.peer_baseline_for_grain's held-only win rate
as a flip ROI baseline (Bug 1 from docs/audits/2026-05-01-wallet-data-completeness.md).

Until a flip-specific peer aggregate exists, FH4 grains must return
verdict='inference_not_run' regardless of peer_baseline_fn output.
"""
from __future__ import annotations

from sniper.analysis.wallet_protocol.flip_reconstruction import Flip
from sniper.analysis.wallet_protocol.flipper import _build_fh4_grain


def _flip(idx: int, roi: float = 0.10) -> Flip:
    return Flip(
        condition_id=f"0xcid{idx}",
        outcome="Yes",
        asset_id=f"asset{idx}",
        buy_ts=1700000000 + idx * 60,
        sell_ts=1700000000 + idx * 60 + 3600,
        buy_notional=100.0,
        sell_notional=100.0 * (1 + roi),
        buy_price=0.5,
        sell_price=0.5 * (1 + roi),
        flip_roi=roi,
        hold_duration_seconds=3600,
    )


def _wr_baseline_fn(grain, tag, wallet):
    # Mirrors db.peer_baseline_for_grain return shape: (mean_wr, peer_count)
    # mean_wr is a held-only win rate in [0, 1] — NOT a flip ROI.
    return (0.65, 50)


def test_fh4_grain_returns_inference_not_run_under_held_wr_baseline():
    flips = [_flip(i, roi=0.20) for i in range(20)]
    indices = list(range(20))
    result = _build_fh4_grain(
        "leaf", "politics", indices, flips, _wr_baseline_fn,
        n_iter=200, seed=1, wallet="0xabc",
    )
    assert result.verdict == "inference_not_run", (
        f"FH4 must not promote with a held-WR baseline; got {result.verdict!r}. "
        "See docs/audits/2026-05-01-wallet-data-completeness.md Bug 1."
    )


def test_fh4_grain_inference_not_run_even_when_peer_count_is_high():
    # peer_count=200 ≥ 30 (would normally classify as 'ok'), and n_at_grain ≥ FH4_MIN_N_PER_GRAIN.
    # Without a flip-specific baseline, the grain still must NOT promote.
    flips = [_flip(i, roi=0.30) for i in range(50)]
    indices = list(range(50))
    result = _build_fh4_grain(
        "nav", "geopolitics", indices, flips,
        lambda g, t, w: (0.80, 200),
        n_iter=200, seed=1, wallet="0xabc",
    )
    assert result.verdict == "inference_not_run"


def test_fh4_grain_inference_not_run_with_no_baseline_fn():
    flips = [_flip(i) for i in range(15)]
    result = _build_fh4_grain(
        "leaf", "politics", list(range(15)), flips, None,
        n_iter=200, seed=1, wallet="0xabc",
    )
    assert result.verdict == "inference_not_run"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_wallet_protocol_fh4_unit_mismatch.py -q`

Expected: `test_fh4_grain_inference_not_run_even_when_peer_count_is_high` FAILS — under the current code that grain promotes through the bootstrap path; the other two may already pass via the early-return branches.

- [ ] **Step 3: Implement the fix in `_build_fh4_grain`**

Replace the body of `_build_fh4_grain` at `sniper/analysis/wallet_protocol/flipper.py:218-261` with:

```python
def _build_fh4_grain(
    grain: str,
    tag: str,
    indices: list[int],
    flips: Sequence[Flip],
    peer_baseline_fn: PeerBaselineFn | None,
    n_iter: int,
    seed: int,
    *,
    wallet: str,
) -> TagGrainResult:
    # Bug 1 (docs/audits/2026-05-01-wallet-data-completeness.md):
    # db.peer_baseline_for_grain returns held-only win_rate, not flip ROI.
    # Subtracting that from flip ROIs is unit-mismatched evidence.
    # Suppress FH4 entirely until a flip-specific peer aggregate exists
    # (e.g. wallet_tag_flip_pnl table + db.peer_flip_baseline_for_grain).
    n = len(indices)
    baseline_info = peer_baseline_fn(grain, tag, wallet) if peer_baseline_fn else None
    peer_count = baseline_info[1] if baseline_info else 0
    confidence = classify_peer_confidence(peer_count)
    return TagGrainResult(
        grain=grain, tag=tag, peer_count=peer_count,
        confidence_flag=confidence,
        n_at_grain=n, metric_value=None, pop_baseline=None,
        excess_pp=None, verdict="inference_not_run",
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_wallet_protocol_fh4_unit_mismatch.py -q`

Expected: `3 passed`.

- [ ] **Step 5: Run flipper module tests for regression**

Run: `python3 -m pytest tests/test_wallet_protocol_flipper.py -q`

Expected: PASS. If a pre-existing test asserts FH4 produces a non-`inference_not_run` verdict for high peer_count + n, that test was relying on the bug — update it (with a comment citing Bug 1) so it asserts `inference_not_run` instead.

- [ ] **Step 6: Commit**

```bash
git add tests/test_wallet_protocol_fh4_unit_mismatch.py sniper/analysis/wallet_protocol/flipper.py
# add any flipper test that needed an assertion update
git commit -m "fix: FH4 suppress until flip-specific peer baseline exists (Bug 1)"
```

---

## Task 2: Low-population grain promotion gate

**Files:**
- Test: `tests/test_wallet_protocol_low_population_gate.py` (create)
- Modify: `sniper/analysis/wallet_protocol/flipper.py:553-562` (FH4 grain post-pool loop)
- Modify: `sniper/analysis/wallet_protocol/held_analyst.py:967-979` (H5 grain post-pool loop)

`h5_tag_edge.py:7-8` documents that `supported` requires `confidence_flag='ok'`. The code does not enforce that. Fix at the grain post-pool update site, not in `_verdict_after_pool` itself (the function is shared with regular hypotheses, which don't carry `confidence_flag`).

NOTE: After Task 1, FH4 grains never reach `_verdict_after_pool` — but the H5 grain path still does. Test must therefore use H5 (held branch), not FH4. We also keep a static FH4 guard test in case Task 1's `inference_not_run` short-circuit is ever weakened.

- [ ] **Step 1: Write failing test**

```python
# tests/test_wallet_protocol_low_population_gate.py
"""Low-population grains (peer_count in [10, 30)) must not be promoted to
'supported' even when the BH pool rejects null and CI excludes zero
(Bug 2 from docs/audits/2026-05-01-wallet-data-completeness.md).
"""
from __future__ import annotations

from sniper.analysis.wallet_protocol.types import TagGrainResult


def _grain(*, peer_count: int, ci_lower: float, ci_upper: float) -> TagGrainResult:
    return TagGrainResult(
        grain="leaf", tag="politics", peer_count=peer_count,
        confidence_flag="low_population" if peer_count < 30 else "ok",
        n_at_grain=15, metric_value=0.10, pop_baseline=0.0,
        excess_pp=10.0, verdict="inconclusive",
        ci_lower=ci_lower, ci_upper=ci_upper,
    )


def _apply_pool_promotion(g: TagGrainResult, *, rejected: bool) -> str:
    """Mimic the post-pool grain-update logic the protocol applies in
    held_analyst.run_held_branch and flipper.run_flipper_branch. Tests
    target the gate directly so a single regression covers both branches.
    """
    from sniper.analysis.wallet_protocol.flipper import _verdict_after_pool
    new_verdict = _verdict_after_pool(
        "inconclusive", rejected, g.ci_lower, g.ci_upper,
    )
    # The fix lives at the call sites — gate promotion on confidence_flag.
    if new_verdict == "supported" and g.confidence_flag != "ok":
        new_verdict = "inconclusive"
    return new_verdict


def test_low_population_grain_does_not_promote_via_bh_pool():
    g = _grain(peer_count=15, ci_lower=0.02, ci_upper=0.30)  # CI > 0
    # Pre-fix: protocol's grain loop would set g.verdict = "supported".
    # Post-fix: must stay "inconclusive".
    from sniper.analysis.wallet_protocol.flipper import _verdict_after_pool
    raw = _verdict_after_pool("inconclusive", True, g.ci_lower, g.ci_upper)
    assert raw == "supported", "sanity: _verdict_after_pool itself does promote"
    assert _apply_pool_promotion(g, rejected=True) == "inconclusive"


def test_ok_population_grain_still_promotes():
    g = _grain(peer_count=50, ci_lower=0.02, ci_upper=0.30)
    assert _apply_pool_promotion(g, rejected=True) == "supported"


def test_held_branch_low_pop_grain_stays_inconclusive_end_to_end():
    """End-to-end: a low_population H5 grain that BH-rejects with CI > 0
    must NOT show up as supported in the held branch report."""
    from sniper.analysis.wallet_protocol.held_analyst import run_held_branch
    from sniper.analysis.wallet_protocol.types import WalletData
    # Synthetic: enough closed_positions that H1/H2 run, with two
    # condition_ids tagged "politics" forming a single low-pop grain.
    closed = []
    for i in range(80):
        # Even split of wins/losses on entry 0.50; H1/H2 inconclusive.
        is_win = (i % 2 == 0)
        closed.append({
            "conditionId": f"0xcid{i % 2}",
            "outcome": "Yes",
            "avgPrice": 0.50,
            "size": 100,
            "realizedPnl": 50.0 if is_win else -50.0,
            "cashPnl": 50.0 if is_win else -50.0,
            "win": 1 if is_win else 0,
            "endDate": "2026-04-01T00:00:00Z",
            "redeemable": False,
        })
    data = WalletData(
        wallet="0xabc", closed_positions=closed, open_positions=[],
        activity_rows=[], activity_cap_hit=False, archetype_layer1=None,
        execution_profile=None, fetched_at=1700000000,
    )
    # leafs_by_cid maps both cids to the same tag → one grain, ~80 obs.
    leafs_by_cid = {"0xcid0": ["politics"], "0xcid1": ["politics"]}
    nav_lookup: dict[str, str] = {}
    # Force a low_population peer pool (peer_count=15, mean_wr=0.30 so the
    # wallet's 0.50 WR has CI > 0 vs baseline).
    def peer_fn(grain, tag, wallet):
        return (0.30, 15)
    branch = run_held_branch(
        data,
        peer_baseline_fn=peer_fn,
        price_history_fn=None,
        leafs_by_cid=leafs_by_cid,
        nav_lookup=nav_lookup,
        n_iter=200, seed=1,
    )
    politics_grains = [g for g in branch.tag_grains if g.tag == "politics"]
    assert politics_grains, "expected a politics grain"
    for g in politics_grains:
        if g.confidence_flag == "low_population":
            assert g.verdict != "supported", (
                f"low_population grain promoted to supported: {g}"
            )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_wallet_protocol_low_population_gate.py -q`

Expected: `test_held_branch_low_pop_grain_stays_inconclusive_end_to_end` FAILS — current code promotes the low-pop grain. The other two tests document the expected behavior of the helper gate.

- [ ] **Step 3: Apply the fix at both grain-update sites**

In `sniper/analysis/wallet_protocol/flipper.py` (around lines 553-562, the `for g in grain_results` loop after the BH pool):

```python
for g in grain_results:
    key = f"FH4:{g.grain}:{g.tag}"
    if key in pooled:
        adj_p, rejected = pooled[key]
        g.p_value_bh_adjusted = adj_p
        new_verdict = _verdict_after_pool(
            "inconclusive", rejected, g.ci_lower, g.ci_upper,
        )
        # Bug 2: enforce h5_tag_edge.py confidence ladder — supported requires
        # confidence_flag='ok'. Without this, low_population grains can
        # promote on a thin peer pool.
        if new_verdict == "supported" and g.confidence_flag != "ok":
            new_verdict = "inconclusive"
        g.verdict = new_verdict
```

In `sniper/analysis/wallet_protocol/held_analyst.py` (around lines 967-979, the equivalent H5 grain loop) apply the same gate. Use `f"H5:{g.grain}:{g.tag}"` as the key (verify in code) and the same `confidence_flag != "ok"` check.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_wallet_protocol_low_population_gate.py tests/test_wallet_protocol_held_analyst.py tests/test_wallet_protocol_flipper.py -q`

Expected: all green. Update any pre-existing test that depended on a low-pop grain being `supported` (cite Bug 2 in the comment).

- [ ] **Step 5: Commit**

```bash
git add tests/test_wallet_protocol_low_population_gate.py \
        sniper/analysis/wallet_protocol/flipper.py \
        sniper/analysis/wallet_protocol/held_analyst.py
git commit -m "fix: gate grain promotion on confidence_flag='ok' (Bug 2)"
```

---

## Task 3: FH3 zero-cost fallback → underpowered

**Files:**
- Test: `tests/test_wallet_protocol_fh3_no_exec_underpower.py` (create)
- Modify: `sniper/analysis/wallet_protocol/flipper.py:146-210` (`run_fh3_spread_capture`)

When `execution_profile` is missing or has no usable cost surface, FH3 must return `verdict='underpowered'` with `underpowered_reason='no_execution_profile'`. The current zero-cost fallback silently makes FH3 a duplicate of FH1.

- [ ] **Step 1: Write failing test**

```python
# tests/test_wallet_protocol_fh3_no_exec_underpower.py
"""FH3 must return underpowered when execution_profile is missing or has
no round-trip-cost surface (Bug 3 from
docs/audits/2026-05-01-wallet-data-completeness.md).
"""
from __future__ import annotations

from sniper.analysis.wallet_protocol.flip_reconstruction import Flip
from sniper.analysis.wallet_protocol.flipper import (
    FH1_MIN_N, run_fh3_spread_capture,
)


def _flip(idx: int) -> Flip:
    return Flip(
        condition_id=f"0xcid{idx}",
        outcome="Yes",
        asset_id=f"asset{idx}",
        buy_ts=1700000000 + idx * 60,
        sell_ts=1700000000 + idx * 60 + 3600,
        buy_notional=100.0,
        sell_notional=120.0,
        buy_price=0.5,
        sell_price=0.6,
        flip_roi=0.20,
        hold_duration_seconds=3600,
    )


def test_fh3_underpowered_with_no_execution_profile():
    flips = [_flip(i) for i in range(FH1_MIN_N + 5)]
    result = run_fh3_spread_capture(flips, execution_profile=None, n_iter=200, seed=1)
    assert result.verdict == "underpowered"
    assert result.underpowered_reason == "no_execution_profile"


def test_fh3_underpowered_with_execution_profile_but_no_cost_surface():
    flips = [_flip(i) for i in range(FH1_MIN_N + 5)]
    # status=populated but neither round_trip_cost_estimate nor tape_midpoint_pairs.
    exec_profile = {"status": "populated"}
    result = run_fh3_spread_capture(flips, execution_profile=exec_profile, n_iter=200, seed=1)
    assert result.verdict == "underpowered"
    assert result.underpowered_reason == "no_execution_profile"


def test_fh3_runs_normally_with_round_trip_cost_estimate():
    flips = [_flip(i) for i in range(FH1_MIN_N + 5)]
    exec_profile = {"round_trip_cost_estimate": 0.05}
    result = run_fh3_spread_capture(flips, execution_profile=exec_profile, n_iter=200, seed=1)
    assert result.verdict in ("supported", "refuted", "inconclusive")
    assert result.underpowered_reason is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_wallet_protocol_fh3_no_exec_underpower.py -q`

Expected: `test_fh3_underpowered_with_no_execution_profile` and `test_fh3_underpowered_with_execution_profile_but_no_cost_surface` FAIL — current code reports `supported`/`refuted`/`inconclusive` via the zero-cost fallback. The third test passes today.

- [ ] **Step 3: Replace the fallback with underpowered**

In `sniper/analysis/wallet_protocol/flipper.py`, replace the `if not matched_pairs:` block at lines 174-176 (and the surrounding cost-resolution logic) with:

```python
matched_pairs: list[float] = []
explicit_estimate: float | None = None
if execution_profile:
    explicit = execution_profile.get("round_trip_cost_estimate")
    if isinstance(explicit, (int, float)):
        explicit_estimate = float(explicit)
    else:
        pairs = execution_profile.get("tape_midpoint_pairs") or []
        for pr in pairs:
            fill_price = pr.get("fill_price")
            tape_mid = pr.get("tape_midpoint")
            if fill_price and tape_mid:
                cost = 2.0 * (fill_price - tape_mid) / tape_mid
                matched_pairs.append(cost)

# Bug 3 (docs/audits/2026-05-01-wallet-data-completeness.md):
# without an execution_profile cost surface, FH3 has no spread to test
# against. Return underpowered instead of silently defaulting to zero
# (which made FH3 a duplicate of FH1).
if explicit_estimate is None and not matched_pairs:
    return HypothesisResult(
        name="FH3", verdict="underpowered", n=n,
        notes=["no execution_profile cost surface (round_trip_cost_estimate or tape_midpoint_pairs)"],
        underpowered_reason="no_execution_profile",
    )

notes: list[str] = []
if explicit_estimate is not None:
    round_trip_cost = explicit_estimate
elif len(matched_pairs) < FH3_MATCHED_PAIRS_FLOOR:
    round_trip_cost = float(np.mean(matched_pairs))
    notes.append(
        f"confidence=low_spread_estimate (matched_pairs={len(matched_pairs)} < {FH3_MATCHED_PAIRS_FLOOR})"
    )
else:
    round_trip_cost = float(np.mean(matched_pairs))
```

(The rest of `run_fh3_spread_capture` — bootstrap, verdict logic, return — stays the same.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_wallet_protocol_fh3_no_exec_underpower.py tests/test_wallet_protocol_flipper.py -q`

Expected: all green. Update any pre-existing FH3 test that asserted a non-underpowered verdict from a zero-cost path (cite Bug 3).

- [ ] **Step 5: Commit**

```bash
git add tests/test_wallet_protocol_fh3_no_exec_underpower.py \
        sniper/analysis/wallet_protocol/flipper.py
git commit -m "fix: FH3 underpowered when execution_profile cost surface missing (Bug 3)"
```

---

## Task 4: FH6 / H6 raw-timestamp price lookup

**Files:**
- Test: `tests/test_wallet_protocol_price_history_offset.py` (create)
- Modify: `sniper/analysis/wallet_protocol/price_history_adapter.py:42-128`
- Modify: `sniper/analysis/wallet_protocol/held_analyst.py:734-790` (`run_h6_exit_quality`)

Operator-approved feature contract:

| consumer | comparison | timestamps |
|---|---|---|
| H6 exit-quality | `sell_price` vs market mid at `sell_ts + 24h` | RAW sell_ts; +24h applied by H6 explicitly |
| FH6 directional-skill | market mid at `buy_ts` vs market mid at `sell_ts` | RAW buy_ts and RAW sell_ts; NO offset |

Implementation: strip `offset_seconds` from `build_price_history_fn` so the closure ALWAYS returns the price at the input ts. H6's call site applies +24h explicitly. FH6's existing call sites (already pass raw `f.buy_ts` / `f.sell_ts`) become correct without further change.

- [ ] **Step 1: Write failing test**

```python
# tests/test_wallet_protocol_price_history_offset.py
"""price_history_fn must return the price at the RAW input timestamp.
H6 explicitly offsets +24h at its call site. FH6 reads raw entry/exit
prices (Bug 4 from docs/audits/2026-05-01-wallet-data-completeness.md).
"""
from __future__ import annotations

import inspect

from sniper.analysis.wallet_protocol.flip_reconstruction import Flip
from sniper.analysis.wallet_protocol.flipper import run_fh6_directional_skill
from sniper.analysis.wallet_protocol.held_analyst import run_h6_exit_quality
from sniper.analysis.wallet_protocol.price_history_adapter import (
    build_price_history_fn,
)


def test_build_price_history_fn_does_not_take_offset_seconds_kwarg():
    sig = inspect.signature(build_price_history_fn)
    assert "offset_seconds" not in sig.parameters, (
        "Bug 4 fix: build_price_history_fn must not silently apply an "
        "offset. H6 applies +24h at its call site; FH6 needs raw lookups."
    )


def test_h6_passes_sell_ts_plus_24h_to_price_history_fn():
    """H6 must call the closure with sell_ts + 24h, not raw sell_ts."""
    calls: list[tuple[str, int]] = []

    def fake_price_history_fn(market_id: str, ts: int) -> float:
        calls.append((market_id, int(ts)))
        return 0.40  # constant; the test checks ts not value

    activity = []
    sell_ts = 1_700_000_000
    for i in range(15):
        activity.append({
            "asset": f"asset{i}",
            "side": "SELL",
            "timestamp": sell_ts + i * 60,
            "price": 0.50,
        })
    run_h6_exit_quality(activity, fake_price_history_fn)
    assert calls, "H6 should call price_history_fn at least once"
    for market_id, ts in calls:
        # H6 applies +24h explicitly. Find the originating sell_ts:
        # ts must equal one of the activity rows' timestamp + 24*3600.
        assert any(
            ts == int(r["timestamp"]) + 24 * 3600 for r in activity
        ), f"H6 called fn with ts={ts}, none match sell_ts+24h"


def test_fh6_passes_raw_timestamps_to_closure():
    """Contract lock: FH6 hands the closure raw buy/sell timestamps with
    no caller-side offset. Passes today (FH6's call site is already raw);
    locks the contract so a future regression that adds caller-side offset
    fires."""
    calls: list[tuple[str, int]] = []

    def fake_price_history_fn(market_id: str, ts: int) -> float:
        calls.append((market_id, int(ts)))
        return 0.40 + (ts % 100) / 1000.0

    flips = []
    for i in range(60):
        flips.append(Flip(
            condition_id=f"0xcid{i}", outcome="Yes",
            asset_id=f"asset{i}",
            buy_ts=1_700_000_000 + i * 3600,
            sell_ts=1_700_000_000 + i * 3600 + 600,
            buy_notional=100.0, sell_notional=110.0,
            buy_price=0.50, sell_price=0.55,
            flip_roi=0.10, hold_duration_seconds=600,
        ))
    run_fh6_directional_skill(flips, fake_price_history_fn, n_iter=200, seed=1)
    assert calls, "FH6 should call price_history_fn at least once"
    expected_raw = set()
    for f in flips:
        expected_raw.add(f.buy_ts)
        expected_raw.add(f.sell_ts)
    raw_timestamps = {ts for _, ts in calls}
    assert raw_timestamps.issubset(expected_raw), (
        f"FH6 called fn with non-raw timestamps: "
        f"{raw_timestamps - expected_raw}"
    )


def test_real_adapter_returns_price_at_input_timestamp_no_offset():
    """The real adapter (post-Bug 4) must not silently apply an offset.
    Mock requests.Session.get and verify the startTs/endTs in the request
    match the input ts (no +24h)."""
    import unittest.mock as mock
    captured: dict = {}

    class _FakeResp:
        status_code = 200
        def json(self):
            return {"history": [{"t": 1_700_000_000, "p": 0.42}]}

    def _fake_get(url, params=None, timeout=None):
        captured["params"] = dict(params or {})
        return _FakeResp()

    sess = mock.Mock()
    sess.get = _fake_get
    fn = build_price_history_fn(session=sess)
    fn("asset123", 1_700_000_000)
    assert captured["params"]["startTs"] == 1_700_000_000
    # endTs may equal startTs OR a tight window (e.g. startTs + fidelity*60),
    # but MUST NOT be startTs + 24*3600. Lock the upper bound.
    assert captured["params"]["endTs"] - captured["params"]["startTs"] < 24 * 3600, (
        "Bug 4 fix: adapter must not silently apply +24h offset"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_wallet_protocol_price_history_offset.py -q`

Expected:
* `test_build_price_history_fn_does_not_take_offset_seconds_kwarg` FAILS (current adapter signature has `offset_seconds=24*3600` default).
* `test_h6_passes_sell_ts_plus_24h_to_price_history_fn` FAILS (current H6 call site at `held_analyst.py:757` passes raw `sell_ts` and relies on the adapter offset).
* `test_real_adapter_returns_price_at_input_timestamp_no_offset` FAILS (current adapter sets `endTs = startTs + 24*3600`).
* `test_fh6_passes_raw_timestamps_to_closure` PASSES (FH6's call sites already pass raw values; the bug is in the adapter, not the FH6 call site).

- [ ] **Step 3: Strip offset from `build_price_history_fn`**

In `sniper/analysis/wallet_protocol/price_history_adapter.py`:

* Remove `offset_seconds` from `build_price_history_fn` signature.
* Rename closure parameter from `sell_ts` to `ts` for clarity.
* Closure body: window is `[ts, ts + fidelity*60]` (or a small fidelity-aligned window). Return the first bar at or after `ts`, or fall back to the last bar in the window.
* Update module docstring lines 1-31 to describe the new contract.
* Update `__all__` if needed.

```python
def build_price_history_fn(
    session: requests.Session | None = None,
    fidelity: int = 60,
    cache: dict | None = None,
    timeout: float = 10.0,
) -> Callable[[str, int], float | None]:
    """Return a memoized closure that fetches the mid price at the given ts.

    Args:
        session: Optional requests.Session for connection reuse.
        fidelity: prices-history fidelity (seconds). 60 = 60-second bars.
        cache: Optional memoization dict.
        timeout: per-request HTTP timeout in seconds.

    The closure signature is ``(market_id: str, ts: int) -> float | None``.
    It returns the mid price at the bar covering `ts`. NO implicit offset is
    applied — callers that need a future price (e.g. H6 at sell_ts+24h)
    must add the offset themselves before calling.

    See docs/audits/2026-05-01-wallet-data-completeness.md Bug 4 for why
    the offset was removed.
    """
    if session is None:
        session = requests.Session()
    if cache is None:
        cache = {}

    def _price_history_fn(market_id: str, ts: int) -> float | None:
        if not market_id or not ts:
            return None
        key = (market_id, int(ts))
        if key in cache:
            return cache[key]
        # Tight window around ts so we always have at least one bar.
        window = max(int(fidelity) * 60, 600)
        params: dict[str, Any] = {
            "market": market_id,
            "fidelity": fidelity,
            "startTs": int(ts),
            "endTs": int(ts) + window,
        }
        try:
            resp = session.get(CLOB_PRICES_HISTORY_URL, params=params, timeout=timeout)
        except requests.RequestException:
            cache[key] = None
            return None
        if resp.status_code >= 400:
            cache[key] = None
            return None
        try:
            data = resp.json()
        except ValueError:
            cache[key] = None
            return None
        history = (data or {}).get("history") or []
        if not history:
            cache[key] = None
            return None
        chosen: dict | None = None
        for bar in history:
            try:
                t = int(bar.get("t"))
            except (TypeError, ValueError):
                continue
            if t >= int(ts):
                chosen = bar
                break
        if chosen is None:
            chosen = history[-1]
        try:
            price = float(chosen.get("p"))
        except (TypeError, ValueError):
            cache[key] = None
            return None
        cache[key] = price
        return price

    return _price_history_fn
```

- [ ] **Step 4: Update H6 to apply the offset explicitly**

In `sniper/analysis/wallet_protocol/held_analyst.py:run_h6_exit_quality`, change the call site (around line 757):

```python
# Bug 4 fix: H6 applies the +24h offset itself; the adapter no longer
# does it. FH6 reads raw timestamps via the same closure.
H6_POST_SELL_OFFSET_SECONDS = 24 * 3600
future = price_history_fn(market_id, sell_ts + H6_POST_SELL_OFFSET_SECONDS)
```

(Define `H6_POST_SELL_OFFSET_SECONDS` as a module-level constant near the other H6 constants.)

Update the comment block above the call to reflect the contract change.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_wallet_protocol_price_history_offset.py tests/test_wallet_protocol_held_analyst.py tests/test_wallet_protocol_flipper.py -q`

Expected: all green. Update any pre-existing test that constructed `build_price_history_fn(offset_seconds=...)` to use the new signature (cite Bug 4).

- [ ] **Step 6: Commit**

```bash
git add tests/test_wallet_protocol_price_history_offset.py \
        sniper/analysis/wallet_protocol/price_history_adapter.py \
        sniper/analysis/wallet_protocol/held_analyst.py
git commit -m "fix: price_history_fn returns price at raw ts; H6 offsets explicitly (Bug 4)"
```

---

## Task 5: Final regression — calibration baseline and full suite

After all four fixes land, the v2 calibration baseline must still be 10/10 OR any delta must be explained before Phase 2B-capture begins.

**Files:** none modified directly by this task (any change here surfaces as a separate decision).

- [ ] **Step 1: Run the v2 calibration harness**

Run: `python3 -m pytest tests/calibration/test_calibration_set.py -q`

Expected: `18 passed` (10 parametrized + 8 static) — same as before the four fixes, since the harness flags `--no-tags --no-peers --no-exec --no-history --no-archetype` keep H4/H5/H6/FH3/FH4/FH6 suppressed. If any parametrized test fails, the protocol's verdict changed for that wallet under the same fixture and same flags — investigate the delta:

* What is the new `overall_verdict`?
* Which hypothesis verdict moved (H1/H2/H2_sub080/H3 are the only ones that should fire under harness flags)?
* Does the move correspond to one of the four fixes?

If yes and explainable: update the affected `tests/calibration/labels.yaml` `expected` field with rationale citing the fix. If not explainable: STOP, do not start 2B-capture, file an investigation.

- [ ] **Step 2: Run the calibration report**

Run:

```bash
python3 scripts/calibration_report.py \
    --labels tests/calibration/labels.yaml \
    --out docs/audits/2026-04-30-calibration-report.md
echo "exit=$?"
```

Expected: `exit=0` and `match=10 miss=0 missing=0`. If exit ≠ 0, see Step 1's investigation rules.

- [ ] **Step 3: Run the full test suite**

Run: `python3 -m pytest tests/ -q --tb=line`

Expected: at least 3636 passed (the prior baseline) plus the new tests added in Tasks 1-4 (≈ 4 new test files × 3-4 tests each ≈ 12-16 additional passes), 0 failed. Skip count and deselected count unchanged.

- [ ] **Step 4: Schema validator + brain index**

Run:

```bash
python3 scripts/validate_schema_refs.py
python3 scripts/brain_index.py generate
```

Expected: validator clean; brain index regeneration produces no git diff. If either fires, investigate before declaring 2B-fix done.

- [ ] **Step 5: Commit and write Phase 2B-fix close-out audit**

If all checks above pass with no calibration delta:

```bash
# (no code changes in Step 5 itself; commit is just the audit doc)
```

Write `docs/audits/2026-05-01-phase-2b-fix-closeout.md` with:

* Summary of the four fixes (commit SHAs).
* Calibration regression result (10/10 unchanged or delta explanation).
* Confirmation that Phase 2B-capture is unblocked.

```bash
git add docs/audits/2026-05-01-phase-2b-fix-closeout.md
git commit -m "audit: phase 2B-fix close-out — analyzer semantic bugs landed"
```

---

## Acceptance (overall)

- [ ] `tests/test_wallet_protocol_fh4_unit_mismatch.py` exists and passes (Task 1).
- [ ] `tests/test_wallet_protocol_low_population_gate.py` exists and passes (Task 2).
- [ ] `tests/test_wallet_protocol_fh3_no_exec_underpower.py` exists and passes (Task 3).
- [ ] `tests/test_wallet_protocol_price_history_offset.py` exists and passes (Task 4).
- [ ] v2 calibration harness still passes 18/18 (or any delta explained in label rationale).
- [ ] Full suite passes; schema validator clean; brain index clean.
- [ ] Close-out audit written.

After all of the above, Phase 2B-capture (richer fixture surfaces) is unblocked.
