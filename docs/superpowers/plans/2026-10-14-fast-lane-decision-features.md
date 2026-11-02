# Fast-lane decision features: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Native execution, because the standing rule is no subagents. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Every 4663 rh-lane and Solana slot-lane attempt that reaches its decision records the features the lane already holds, in `trace_json.decision_features`. The record is measurement only.

**Architecture:**
- A pure builder module turns captured decision-time inputs into a `decision_features_v1` dict.
- At decision, each lane captures those inputs as one `functools.partial`. The 4663 lane puts it on the `LaneEvent`, and the slot lane puts it on its trace.
- `EntryAttemptTrace.finish` calls the capture with the trace's own `decision_at` and writes the result. So the decision path pays for one object creation, with no clock read, no serialization and no lock.

**Tech stack:** Python 3.13, sqlite3, pytest (`-n 4` at most, targeted files only).

**Spec:** `docs/superpowers/specs/2026-10-14-fast-lane-decision-features-design.md` (approved, 799630d41).

**Spec amendment (Task 6):** the stamp is the top-level trace key `decision_features`, written at finish, not a substage. `EntryAttemptTrace.start` serializes `source_evidence` into the shell-row INSERT before the send, so the stamp stays out of it.

## Global constraints

- Measurement only: nothing writes to `classification_json`, `gate_inputs_json` or any funded contract.
- Held data only: before dispatch, no new provider call, DB read, lock, clock-file read or wait.
- An unknown value is null with `<field>_reason`, never a default. A known zero stays zero.
- Every attempt that reaches classification is stamped, refusals included.
- Added decision-path CPU must have p90 under 20 µs. Measure it on the route tests and with a microbenchmark, and put the numbers in the commit.
- Option B (a provider read after dispatch) is out of scope.

## Review focus

1. Stats past the cache TTL. `SafetyCache.stats` returns None past its TTL, so the stamp is null with a reason, never stale numbers. Task 3 tests this (native path, `stats=None`).
2. A builder that raises at finish. The trace and its result must not change. Task 2 tests this.
3. A pool learned by RabbitStream preparation or warm-up. It must not get `migration_observed`, because only `on_migrated` records that. Task 4 tests this.
4. A non-SOL quote. `liquidity_usd` must be null with reason `quote_usd_not_held`, never priced at SOL. Task 5 tests this for curves and pools.
5. A refusal after classification is still stamped. Solana: `slot_lane_scaled_clip`. 4663: a hook refusal. Tasks 3 and 5 test these.

---

### Task 1: One lifecycle rule

**Files:**
- Modify: `sniper/services/memecoin_market_state.py`, the inline lifecycle block in `classify_funded_entry` (about lines 591-600).
- Test: `tests/test_memecoin_decision_features.py` (new).

**Interfaces:**
- Produces: `lifecycle_for(age_s, runup_pct) -> str`, returning `unknown`, `fresh`, `young`, `revival` or `mature`. The thresholds are today's.

- [ ] **Step 1: Write the failing tests.**

```python
import pytest
from sniper.services.memecoin_market_state import lifecycle_for
from sniper.settings import memecoin as cfg

@pytest.mark.parametrize("age,runup,want", [
    (None, None, "unknown"), (0.0, None, "fresh"), (3600.0, None, "fresh"),
    (3600.1, None, "young"), (86400.0, None, "young"), (86400.1, None, "mature"),
])
def test_lifecycle_for_matches_the_classifier_thresholds(age, runup, want):
    assert lifecycle_for(age, runup) == want

def test_a_runup_past_the_post_peak_threshold_after_a_day_is_revival():
    assert lifecycle_for(90000.0, cfg.MEMECOIN_FUNDED_POST_PEAK_RUNUP_PCT) == "revival"
```

- [ ] **Step 2: Run them.** Command: `python3 -m pytest -q tests/test_memecoin_decision_features.py`. Expected: ImportError.

- [ ] **Step 3: Extract the helper.** In the classifier, replace the inline block with `lifecycle = lifecycle_for(age, runup)`.

```python
def lifecycle_for(age_s, runup_pct):
    """The classifier's lifecycle rule, in one place (decision features reuse it)."""
    if age_s is None:
        return "unknown"
    if age_s <= 3600:
        return "fresh"
    if age_s <= 86400:
        return "young"
    if runup_pct is not None and runup_pct >= cfg.MEMECOIN_FUNDED_POST_PEAK_RUNUP_PCT:
        return "revival"
    return "mature"
```

- [ ] **Step 4: Run the tests with the classifier tests.** Command: `python3 -m pytest -q -n 4 tests/test_memecoin_decision_features.py tests/test_memecoin_market_state.py tests/test_memecoin_e14_solana_own_marks.py`. Expected: pass.

- [ ] **Step 5: Commit.**

### Task 2: Cached-stats builder and the trace carry

**Files:**
- Create: `sniper/services/memecoin_decision_features.py`.
- Modify: `sniper/services/memecoin_entry_trace.py` (a new field, `start`, `note_decision_features`, the `finish` payload).
- Test: `tests/test_memecoin_decision_features.py`.

**Interfaces:**
- `SCHEMA = "decision_features_v1"`.
- `from_cached_stats(*, decision_at, lane, held, their_usd) -> dict`. The lane fills `held`:
  - `entry`: the tuple `(stats_dict, fetched_at_host)`, or None.
  - `read_at`: the host epoch of the read.
  - `source`: `lane_cache` or `inline_read`.
  - A `held` with no `entry` key means the decision never read stats. Every stats field is then null with reason `stats_not_read`.
- `resolve(capture, decision_at) -> dict`. It returns `capture(decision_at=decision_at)`. If the capture raises, or returns something that is not a dict, it returns `{"schema": SCHEMA, "error": <exception type name or "not_a_dict">}`.
- `EntryAttemptTrace.note_decision_features(capture)`.
- `trace_json["decision_features"]` exists only when there is a capture.

- [ ] **Step 1: Write the failing tests.** Cover these cases:
  - Cached stats: pair age, an m5 of exactly 0 (kept as 0), stats age and lifecycle.
  - No cached stats.
  - Stats never read.
  - Age over a day with no runup held: lifecycle null, reason `runup_not_held`.
  - The capture is called only at finish, with the trace's `decision_at`.
  - A capture that raises keeps the trace.

- [ ] **Step 2: Run them.** Expected: ImportError.

- [ ] **Step 3: Write the module.**
  - `_num` accepts finite values ≥ 0 only.
  - `_null(out, field, reason)` sets the field to null and writes its reason.
  - `_finish`:
    - Sets `lifecycle` from `lifecycle_for(age, None)`.
    - If the age is None, `lifecycle` is null with the age's reason.
    - If the age is over 86400 s, `lifecycle` is null with reason `runup_not_held`, because without runup revival cannot be told from mature.
    - Sets `source_impact_pct = their_usd / liquidity_usd * 100`.
  - `from_cached_stats` uses the runner's `_pair_age_s(stats, decision_at)`.

- [ ] **Step 4: Carry the capture in `EntryAttemptTrace`.**
  - Add the field `_decision_features: Any = None`.
  - In `start`, set `_decision_features=_event_value(ev, "decision_features")`. This is a reference only; it is not called.
  - Add `note_decision_features`. It refuses after finish.
  - In the `finish` payload, add `**({"decision_features": resolve(self._decision_features, self._anchor.isoformat())} if self._decision_features is not None else {})`.

- [ ] **Step 5: Run the tests.** Command: `python3 -m pytest -q -n 4 tests/test_memecoin_decision_features.py tests/test_memecoin_entry_trace.py tests/test_memecoin_entry_trace_cpu.py`.

- [ ] **Step 6: Commit.**

### Task 3: 4663 rh lane, all routes

**Files:**
- Modify: `sniper/services/memecoin_rh_fast_lane.py`:
  - `LiveLane.handle`, on the native path, where it builds `ev = LaneEvent(payload, decimals=decimals, supply=supply)`.
  - `_handle_shared`: its `stats_fn` and its `LaneEvent`.
- Test: `tests/test_memecoin_decision_features.py`. Reuse `_live` from `tests/test_memecoin_rh_fast_live.py` and the shared harness pattern from `tests/test_memecoin_pons_counter_route.py`.

**Native route.** The hook receives `cached` (the stats dict) when it is built.
- `entry` is the `(stats, fetched_at)` tuple behind `cached`: `stats` from the dispatcher, or `got` when the inline branch re-read it.
- `read_at` is the host time of that read: `host_now`, or the inline `self.host_clock()`.
- `source` is `lane_cache`.

**Shared routes.** The hook reads stats through the lane's `stats_fn` during its decision. Wrap `stats_fn` so it records into `held`:
- Cache hit: `entry=got`, `read_at=now`, `source=lane_cache`.
- Cache miss: `entry=(stats, started)`, or None when no stats came back; `source=inline_read`.
- Then set `ev.decision_features = partial(from_cached_stats, lane="rh_fast_lane", held=held, their_usd=ev.their_size_usd)`.

- [ ] **Step 1: Write the failing tests.**
  - Native path: `hook_factory` returns an object whose `attempt(conn, ev)` records `ev` and returns a rejection. Assert that `resolve(ev.decision_features, AT)` carries the dispatcher's stats with `stats_age_s == 5.0`.
  - Native path with `stats=None` (inline path): reason `stats_not_cached`.
  - Shared path: call `_handle_shared` with a `shared_hook_factory` whose hook calls `stats_fn`, then refuses. Assert that `held` was filled and that the hook saw the same stats as before the change.

- [ ] **Step 2: Run them.** Expected: AttributeError (`decision_features`).

- [ ] **Step 3: Implement.**

- [ ] **Step 4: Run the tests and the RH covering set.** List the files with `grep -l -E "memecoin_rh_fast_lane|LaneEvent|_handle_shared" tests/*.py | grep -v -E "watchdog|deploy"` and run them with `-n 4`.

- [ ] **Step 5: Microbenchmark the added capture.** Run it 10,000 times and record p50 and p90 in the commit.

- [ ] **Step 6: Commit.**

### Task 4: PumpSwap migration-observed time

**Files:**
- Modify: `sniper/execution/pumpswap_buy.py` (`PumpSwapPoolCache`): add `_migrated_at`, `note_migration` and `migration_observed_at`.
- Modify: `sniper/services/memecoin_slot_lane.py`:
  - `queue_migration` stamps `real_utc_now()` at put.
  - `migration_loop` passes the stamp through.
  - `on_migrated(pool, observed_at=None)` records it. When the direct listener calls it without a stamp, it records the call time.
- Test: `tests/test_memecoin_decision_features.py`.

**Rules:**
- Only `on_migrated` records. `offer_stream` stays as it is, because `memecoin_shyft_prepare` also calls it for preparation.
- The record is bounded by `store_cap` and written under the existing `self._lock`.
- `migration_observed_at` is a lock-free dict get, like `cached`.
- `real_utc_now()` reads the offset file. That read happens on the migration thread only, never on the decision path.

- [ ] **Step 1: Write the failing tests.**
  - Only `note_migration` records, not `offer_stream`.
  - The record is bounded.
  - A time queued through the slot lane's migration queue reaches the cache unchanged.

- [ ] **Step 2: Run them.** Expected: fail.

- [ ] **Step 3: Implement.**

- [ ] **Step 4: Run the tests.** Run `tests/test_pumpswap_buy.py` and the migration tests: `grep -l "on_migrated\|migration_queue\|on_migration" tests/*.py`.

- [ ] **Step 5: Commit.**

### Task 5: Solana slot-lane stamping

**Files:**
- Modify: `sniper/services/memecoin_decision_features.py`: add `from_slot_buy`.
- Modify: `sniper/services/memecoin_slot_lane.py`: in `_reserve_and_execute`, right after `trace.slot_classification = classification`.
- Test: `tests/test_memecoin_decision_features.py`, with `_lane` and `_buy` from `tests/test_slot_lane_scaled_sol.py`.

**Interfaces:**
- `from_slot_buy(*, decision_at, buy, their_usd, sol_usd, pool_facts, migration_observed_at) -> dict`.

**Curve buys:**
- Stamp the four post-trade reserves from `SlotBuy`, as integers.
- `virtual_sol_reserves == 0` means a non-SOL curve, the same rule as `quote_curve_completed`. Its `quote_mint` is `buy.quote_mint`, and `liquidity_usd` is null with reason `quote_usd_not_held`.
- Otherwise the quote is WSOL with 9 decimals, and `liquidity_usd = real quote / 1e9 * sol_usd`, basis `curve_post_trade_reserves`.
- `pair_age_s` is null with reason `not_in_event`.

**PumpSwap buys:**
- `real_*` are the PRE-trade pool reserves. The virtual quote comes from the pool facts.
- `liquidity_usd = 2 * quote / 10^dec * quote_usd`, basis `pool_pre_trade_reserves`. `quote_usd` is the SOL price for WSOL and 1.0 for USDC. Any other quote: null, reason `quote_usd_not_held`.
- `pair_age_s = decision_at - migration_observed_at`, basis `migration_observed`.
  - No record: reason `migration_not_observed`.
  - A negative age: reason `migration_after_decision`.

**Other venues:** null with reason `not_held`.

**m5, h1 and stats age:** always null with reason `not_held`.

**Capture:**

```python
pools = self.amm_pools if getattr(buy, "venue", None) == "pump_amm" and buy.pool else None
trace.note_decision_features(partial(
    from_slot_buy, buy=buy, their_usd=facts.get("their_usd"), sol_usd=snap.sol_usd,
    pool_facts=pools.cached(buy.pool) if pools is not None else None,
    migration_observed_at=pools.migration_observed_at(buy.pool) if pools is not None else None))
```

- [ ] **Step 1: Write the failing tests.** Cover:
  - A curve buy.
  - A non-SOL curve.
  - A PumpSwap buy after a live migration.
  - A PumpSwap buy whose pool was learned without a migration.
  - A non-SOL pool.
  - A `slot_lane_scaled_clip` refusal: it is stamped, and `classification_json` stays free of the features.

- [ ] **Step 2: Run them.** Expected: fail.

- [ ] **Step 3: Implement.**

- [ ] **Step 4: Run the slot covering set.** List the files with `grep -l -E "memecoin_slot_lane|_reserve_and_execute" tests/*.py | grep -v -E "watchdog|deploy"` and run them with `-n 4`.

- [ ] **Step 5: Measure.** Microbenchmark the capture block 10,000 times, and record p50 and p90 in the commit. Also check the gap between `slot_classification` and `slot_leg_scale` in the slot route tests.

- [ ] **Step 6: Commit.**

### Task 6: Spec amendment, reader note, handoff

- [ ] Update spec §3:
  - Location: `trace_json.decision_features`, written at finish.
  - New reasons: `stats_not_read`, `runup_not_held`, `quote_usd_not_held`, `migration_after_decision`.
  - New field: `stats_source`.
- [ ] Commit.
- [ ] Record brain episodes with the SHAs and the measurements.
- [ ] Send Gamma the branch, the reader rule and the location.
- [ ] Send Beta the location, for the BSC Flap mirror.
- [ ] Send Alpha a 5-line summary.
