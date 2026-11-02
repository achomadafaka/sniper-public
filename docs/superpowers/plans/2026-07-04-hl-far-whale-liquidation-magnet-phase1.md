# Far-Whale Liquidation Magnet — Paper Study v0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** `docs/superpowers/specs/2026-07-04-hl-far-whale-liquidation-magnet-paper-study.md` (as amended in `9a72c6b` — episode clustering, clustered-bootstrap decision rule, ITT primary). Read §2 (side semantics) before writing any code.

**Goal:** Build the paper-only measurement pipeline that tests whether a large visible single-whale liquidation level 50–300 bps from mid acts as a price magnet, with pre-registered controls and an episode-clustered bootstrap verdict.

**Architecture:** Forward-only capture loop (address harvest → `clearinghouseState` polling → market-state + whale snapshots + candidate/episode assignment → paper sim stepping) writing new `hl_far_whale_liq_*` tables in `data/hyperlive_engine.sqlite`, plus an operator-gated evaluator (controls, dose-response, concentration, bootstrap CIs) and a sparse-sample-labelled UI lane. Detection/sim/eval logic is pure and unit-tested; the runner script only orchestrates I/O.

**Tech Stack:** Python 3.13, sqlite3 (WAL), requests + websocket-client (existing HL patterns), pytest, Flask blueprint (existing `api_hyperlive`).

## Global Constraints

- **Paper-only. Public data only. No live orders, no private keys, no service restarts, no changes to the live A/B policy.** (spec header)
- **Side semantics (§2):** never carry forward `cluster_map`'s `side` field. Canonical fields only: `direction_to_liquidation` (`up`/`down`), `position_side` (`long`/`short`), `liquidation_type`. Invariant `long_liquidation ⇔ direction down`, `short_liquidation ⇔ direction up`. Violations are **dropped + counted**, never coerced.
- **Frozen v0 params (§16) are module constants, NOT env knobs** — they are the pre-registration contract (hyperlive convention: `models.py`-style constants). `study_version = "fwlm-v0"`. Values: distance band `[50, 300]` bps, `TARGET_FRAC = 0.75`, `ADVERSE_FRAC = 0.50`, primary horizon 30 min (diagnostics 10/20/60), `PAPER_NOTIONAL = 1_000.0`, `MIGRATE_MIN_FRAC = 0.25`, `MAX_EVENT_SHARE = 0.40`, `MAX_SPREAD_BPS = 5.0`, `MAX_STALENESS_MS = 5000`, `EPISODE_LIQ_TOL_BPS = 25.0`, `EPISODE_GAP_MS = 3_600_000`, `CAMPAIGN_CAP_DAYS = 60`, fee = `sniper.hyperlive.models.ROUND_TRIP_FEE_BPS` (~9).
- **Episodes (§3.1) are the unit of observation.** First valid entry per episode is primary; re-entries are diagnostics. Pass-bar counts are episode counts.
- **ITT primary (§9 Control 4):** persistence never filters the primary set.
- **Decision rule (§12):** episode-clustered bootstrap (resample episodes), 10,000 draws, fixed seed; PASS = 95% CI lower bound > 0 on net pnl AND on migration delta vs each control; CI-spans-0 = `decayed`.
- **Whale thresholds are NOT hardcoded** (§4): capture records all whales ≥ a low sweep floor; per-asset thresholds are frozen via the sweep script into `hl_far_whale_liq_reports`; **no paper entries until a freeze row exists** (fail closed).
- **Timestamps: local wall clock consistently** (§10). This lane is self-referential; never compare to exchange time.
- **Fail loudly:** no bare `except: pass` on data-shaping paths; anomaly/drop counts logged per round.
- **UI copy (§11):** the lane MUST carry `"Sparse visible whale sample — NOT full liquidation density."` Never the words "liquidation book" or "crowd cluster".
- After any `ensure_*` schema addition: `python3 scripts/brain_index.py generate` and commit the regenerated `index/*.json` (CI freshness gate), and run `python3 scripts/validate_schema_refs.py`.
- The forward campaign (spec §15 step 8) is **operator-gated**: this plan builds the watchdog script but does NOT install any cron and does NOT start capture.

---

### Task 1: Detector core — canonical side semantics + candidate gate

**Files:**
- Create: `sniper/hyperlive/far_whale.py`
- Test: `tests/test_far_whale_detector.py`

**Interfaces:**
- Consumes: nothing project-internal (pure module). `sniper.hyperlive.models.ROUND_TRIP_FEE_BPS` for the fee constant re-export.
- Produces (used by Tasks 2–7):
  - module constants listed in Global Constraints (exact names: `STUDY_VERSION`, `DIST_MIN_BPS`, `DIST_MAX_BPS`, `MAX_SPREAD_BPS`, `MAX_STALENESS_MS`, `SWEEP_FLOOR_NOTIONAL`, `EPISODE_LIQ_TOL_BPS`, `EPISODE_GAP_MS`, `TARGET_FRAC`, `ADVERSE_FRAC`, `HORIZON_MS`, `DIAG_HORIZONS_MS`, `PAPER_NOTIONAL`, `MIGRATE_MIN_FRAC`, `MAX_EVENT_SHARE`, `CAMPAIGN_CAP_DAYS`, `FEE_BPS`)
  - `whale_rows_from_clearinghouse(state: dict, address: str) -> list[dict]` — rows with keys `address, asset, liquidation_price, szi, position_side, entry_px, position_notional`
  - `evaluate_candidate(row: dict, mid: float, *, spread_bps: float | None, staleness_ms: int, threshold_notional: float) -> tuple[dict | None, str | None]` — `(candidate, None)` on pass; `(None, reason)` on drop, reason ∈ `{"no_mid","anomaly_side_invariant","below_threshold","out_of_band","spread_wide","spread_missing","stale"}`. Candidate keys: `asset, whale_address, liquidation_price, mid_price, distance_bps, direction_to_liquidation, position_side, liquidation_type, position_notional, position_size, spread_bps`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_far_whale_detector.py
"""Far-whale magnet detector — §2 side semantics + §3 candidate gate (fwlm-v0)."""
from sniper.hyperlive import far_whale as FW


def _state(coin="ETH", liq="2950", szi="100", entry="3010"):
    return {"assetPositions": [{"position": {
        "coin": coin, "liquidationPx": liq, "szi": szi, "entryPx": entry}}]}


def test_whale_rows_extracts_position_side_from_szi_sign():
    rows = FW.whale_rows_from_clearinghouse(_state(szi="100"), "0xabc")
    assert len(rows) == 1
    r = rows[0]
    assert r["address"] == "0xabc"
    assert r["asset"] == "ETH"
    assert r["position_side"] == "long"
    assert r["liquidation_price"] == 2950.0
    assert r["position_notional"] == 100 * 3010.0
    short = FW.whale_rows_from_clearinghouse(_state(szi="-100"), "0xabc")[0]
    assert short["position_side"] == "short"
    assert short["position_notional"] == 100 * 3010.0   # abs(szi)


def test_whale_rows_skips_zero_or_missing():
    assert FW.whale_rows_from_clearinghouse({}, "0xabc") == []
    assert FW.whale_rows_from_clearinghouse(_state(liq=None), "0xabc") == []
    assert FW.whale_rows_from_clearinghouse(_state(szi="0"), "0xabc") == []


def _row(side="long", liq=2950.0, notional=2_000_000.0):
    szi = 100.0 if side == "long" else -100.0
    return {"address": "0xabc", "asset": "ETH", "liquidation_price": liq,
            "szi": szi, "position_side": side, "entry_px": 3010.0,
            "position_notional": notional}


def test_long_liquidation_is_below_mid_direction_down():
    # long liquidates BELOW -> direction down -> long_liquidation (§2 invariant)
    cand, reason = FW.evaluate_candidate(
        _row("long", liq=2950.0), 3000.0, spread_bps=2.0, staleness_ms=0,
        threshold_notional=1_000_000.0)
    assert reason is None
    assert cand["direction_to_liquidation"] == "down"
    assert cand["liquidation_type"] == "long_liquidation"
    assert round(cand["distance_bps"], 1) == round(50 / 3000 * 1e4 / 3, 1) * 3  # ~166.7


def test_short_liquidation_is_above_mid_direction_up():
    cand, reason = FW.evaluate_candidate(
        _row("short", liq=3050.0), 3000.0, spread_bps=2.0, staleness_ms=0,
        threshold_notional=1_000_000.0)
    assert reason is None
    assert cand["direction_to_liquidation"] == "up"
    assert cand["liquidation_type"] == "short_liquidation"


def test_side_invariant_violation_dropped_not_coerced():
    # a "long" whose liq sits ABOVE mid is a source/schema anomaly (§2) -> drop
    cand, reason = FW.evaluate_candidate(
        _row("long", liq=3050.0), 3000.0, spread_bps=2.0, staleness_ms=0,
        threshold_notional=1_000_000.0)
    assert cand is None and reason == "anomaly_side_invariant"


def test_gate_distance_band_50_to_300():
    near, r1 = FW.evaluate_candidate(_row("long", liq=2999.0), 3000.0,
                                     spread_bps=2.0, staleness_ms=0,
                                     threshold_notional=1_000_000.0)
    far, r2 = FW.evaluate_candidate(_row("long", liq=2800.0), 3000.0,
                                    spread_bps=2.0, staleness_ms=0,
                                    threshold_notional=1_000_000.0)
    assert near is None and r1 == "out_of_band"     # ~3.3 bps < 50
    assert far is None and r2 == "out_of_band"      # ~667 bps > 300


def test_gate_threshold_spread_staleness_fail_closed():
    small, r1 = FW.evaluate_candidate(_row(notional=500_000.0), 3000.0,
                                      spread_bps=2.0, staleness_ms=0,
                                      threshold_notional=1_000_000.0)
    wide, r2 = FW.evaluate_candidate(_row(), 3000.0, spread_bps=6.0,
                                     staleness_ms=0, threshold_notional=1_000_000.0)
    nospread, r3 = FW.evaluate_candidate(_row(), 3000.0, spread_bps=None,
                                         staleness_ms=0, threshold_notional=1_000_000.0)
    stale, r4 = FW.evaluate_candidate(_row(), 3000.0, spread_bps=2.0,
                                      staleness_ms=6000, threshold_notional=1_000_000.0)
    assert (small, r1) == (None, "below_threshold")
    assert (wide, r2) == (None, "spread_wide")
    assert (nospread, r3) == (None, "spread_missing")   # unknown spread -> fail closed
    assert (stale, r4) == (None, "stale")


def test_frozen_params_match_spec_v0():
    assert FW.STUDY_VERSION == "fwlm-v0"
    assert (FW.DIST_MIN_BPS, FW.DIST_MAX_BPS) == (50.0, 300.0)
    assert (FW.TARGET_FRAC, FW.ADVERSE_FRAC) == (0.75, 0.50)
    assert FW.HORIZON_MS == 30 * 60_000
    assert FW.MIGRATE_MIN_FRAC == 0.25
    assert FW.MAX_EVENT_SHARE == 0.40
    assert FW.EPISODE_LIQ_TOL_BPS == 25.0
    assert FW.EPISODE_GAP_MS == 3_600_000
    assert FW.CAMPAIGN_CAP_DAYS == 60
    assert 8.5 < FW.FEE_BPS < 9.5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_far_whale_detector.py -q`
Expected: FAIL — `ModuleNotFoundError`/`AttributeError` (module doesn't exist).

- [ ] **Step 3: Write the implementation**

```python
# sniper/hyperlive/far_whale.py
"""Far-Whale Liquidation Magnet — candidate detection (paper study fwlm-v0).

Spec: docs/superpowers/specs/2026-07-04-hl-far-whale-liquidation-magnet-paper-study.md
Canonical side semantics per spec §2. NEVER reuse cluster_map's `side` field — it labels
the magnet direction, the INVERSE of the liquidating trader's side. Here:
  position_side          = the liquidating trader's side (long if szi > 0)
  direction_to_liquidation = up if liquidation_price > mid else down (paper entry direction)
  invariant: long_liquidation <=> direction down; short_liquidation <=> direction up.
Violations are dropped + counted upstream — never coerced.
"""
from __future__ import annotations

from sniper.hyperlive.models import ROUND_TRIP_FEE_BPS

# --- frozen v0 pre-registration (spec §16) — constants, NOT env knobs. Changing any of
# these starts a new study_version; they are the contract, so they live in code.
STUDY_VERSION = "fwlm-v0"
DIST_MIN_BPS = 50.0
DIST_MAX_BPS = 300.0
MAX_SPREAD_BPS = 5.0
MAX_STALENESS_MS = 5000
SWEEP_FLOOR_NOTIONAL = 50_000.0        # capture floor for the §4 sweep pool, NOT the study gate
EPISODE_LIQ_TOL_BPS = 25.0             # §3.1 episode identity tolerance
EPISODE_GAP_MS = 3_600_000             # §3.1: 60 min out of gate -> new episode
TARGET_FRAC = 0.75
ADVERSE_FRAC = 0.50
HORIZON_MS = 30 * 60_000               # primary; diagnostics below
DIAG_HORIZONS_MS = (10 * 60_000, 20 * 60_000, 60 * 60_000)
PAPER_NOTIONAL = 1_000.0
MIGRATE_MIN_FRAC = 0.25                # directional-progress label; looser than TARGET_FRAC by design
MAX_EVENT_SHARE = 0.40                 # Control 5 — applied to positive pnl AND candidate counts
CAMPAIGN_CAP_DAYS = 60                 # §13 sparsity kill
FEE_BPS = ROUND_TRIP_FEE_BPS           # ~9 bps round-trip taker


def whale_rows_from_clearinghouse(state: dict, address: str) -> list[dict]:
    """One clearinghouseState -> per-position rows keeping szi SIGN (position_side source).
    cluster_map.positions_from_clearinghouse drops the sign — do not reuse it here."""
    out = []
    for ap in (state or {}).get("assetPositions", []) or []:
        pos = ap.get("position") or {}
        coin = pos.get("coin")
        liq = _f(pos.get("liquidationPx"))
        szi = _f(pos.get("szi"))
        entry = _f(pos.get("entryPx"))
        if not coin or liq is None or liq <= 0 or szi is None or szi == 0:
            continue
        out.append({
            "address": address,
            "asset": str(coin).upper(),
            "liquidation_price": liq,
            "szi": szi,
            "position_side": "long" if szi > 0 else "short",
            "entry_px": entry,
            "position_notional": abs(szi) * (entry if entry else liq),
        })
    return out


def evaluate_candidate(row: dict, mid: float, *, spread_bps: float | None,
                       staleness_ms: int, threshold_notional: float
                       ) -> tuple[dict | None, str | None]:
    """§3 gate + §2 invariants. Returns (candidate, None) or (None, drop_reason).
    Order matters: anomalies are detected before the band check so they are COUNTED
    even when the level is also out of band (spec §2: log dropped anomalies)."""
    if not mid or mid <= 0:
        return None, "no_mid"
    liq = float(row["liquidation_price"])
    direction = "up" if liq > mid else "down"
    # §2 invariant: long liquidates below mid, short above. Mismatch = source anomaly.
    expected = "down" if row["position_side"] == "long" else "up"
    if direction != expected:
        return None, "anomaly_side_invariant"
    if float(row["position_notional"]) < threshold_notional:
        return None, "below_threshold"
    distance_bps = abs(liq - mid) / mid * 1e4
    if not (DIST_MIN_BPS <= distance_bps <= DIST_MAX_BPS):
        return None, "out_of_band"
    if spread_bps is None:
        return None, "spread_missing"        # unknown spread -> fail closed
    if spread_bps > MAX_SPREAD_BPS:
        return None, "spread_wide"
    if staleness_ms > MAX_STALENESS_MS:
        return None, "stale"
    return {
        "asset": row["asset"],
        "whale_address": row["address"],
        "liquidation_price": liq,
        "mid_price": float(mid),
        "distance_bps": distance_bps,
        "direction_to_liquidation": direction,
        "position_side": row["position_side"],
        "liquidation_type": ("long_liquidation" if row["position_side"] == "long"
                             else "short_liquidation"),
        "position_notional": float(row["position_notional"]),
        "position_size": abs(float(row["szi"])),
        "spread_bps": float(spread_bps),
    }, None


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_far_whale_detector.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/hyperlive/far_whale.py tests/test_far_whale_detector.py
git commit -m "feat(fwlm): far-whale detector core — canonical side semantics + candidate gate (fwlm-v0)"
```

---

### Task 2: Store — schema, episode assignment, freeze/report IO

**Files:**
- Create: `sniper/hyperlive/far_whale_store.py`
- Test: `tests/test_far_whale_store.py`
- Modify (generated): `index/schema_registry.json`, `index/dependency_index.json` (via `brain_index.py generate`)

**Interfaces:**
- Consumes: `far_whale` constants (`EPISODE_LIQ_TOL_BPS`, `EPISODE_GAP_MS`, `STUDY_VERSION`); `sniper.hyperlive.persistence.connect` for the connection factory.
- Produces (used by Tasks 4–8):
  - `connect(db_path) -> sqlite3.Connection` (re-export of `persistence.connect`)
  - `ensure_schema(conn) -> None`
  - `save_market_state(conn, rows: dict[str, tuple], ts_ms: int) -> None` — `rows[asset] = (mid, oi, day_ntl_vlm, spread_bps)`; idempotent on `(asset, ts_ms)`
  - `save_round(conn, ts_ms: int, stats: dict) -> None` — keys `addresses_polled, whales_seen, anomalies_dropped, candidates_new, sweep_mode`
  - `save_snapshots(conn, ts_ms: int, rows: list[dict]) -> None` — whale rows (Task 1 shape) in the distance band ≥ sweep floor
  - `assign_episode(conn, cand: dict, signal_ts_ms: int) -> tuple[str, bool]` — `(episode_id, is_new)` per §3.1; closes a stale/moved episode as a side effect
  - `insert_candidate(conn, cand: dict, *, episode_id: str, signal_ts_ms: int, is_primary: int, source_snapshot_hash: str) -> int`
  - `open_episodes(conn) -> list[dict]`, `update_episode(conn, episode_id, *, last_seen_ts_ms=None, status=None) -> None`
  - `insert_entry(conn, entry: dict) -> int`, `open_entries(conn) -> list[dict]`, `settle_entry(conn, entry_id: int, outcome: dict) -> None`
  - `save_report(conn, *, kind: str, payload: dict, ts_ms: int) -> None`, `load_freeze(conn) -> dict | None` (latest `kind='freeze'` payload: `{"thresholds": {...}, "campaign_start_ts_ms": int}`), `latest_report(conn, kind) -> dict | None`
  - `read_market_series(conn, asset: str, since_ms: int, until_ms: int) -> list[tuple[int, float]]` — `(ts_ms, mid)` ascending

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_far_whale_store.py
"""fwlm-v0 store — schema, §3.1 episode assignment, freeze IO."""
import pytest

from sniper.hyperlive import far_whale_store as S


@pytest.fixture()
def conn():
    c = S.connect(":memory:")
    S.ensure_schema(c)
    return c


def _cand(liq=2950.0, addr="0xabc", asset="ETH"):
    return {"asset": asset, "whale_address": addr, "liquidation_price": liq,
            "mid_price": 3000.0, "distance_bps": 166.7,
            "direction_to_liquidation": "down", "position_side": "long",
            "liquidation_type": "long_liquidation", "position_notional": 2e6,
            "position_size": 100.0, "spread_bps": 2.0}


def test_schema_has_all_study_tables(conn):
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("hl_far_whale_liq_market_state", "hl_far_whale_liq_rounds",
              "hl_far_whale_liq_position_snapshots", "hl_far_whale_liq_candidates",
              "hl_far_whale_liq_episodes", "hl_far_whale_liq_paper_entries",
              "hl_far_whale_liq_outcomes", "hl_far_whale_liq_controls",
              "hl_far_whale_liq_reports"):
        assert t in names, t


def test_market_state_idempotent_on_asset_ts(conn):
    S.save_market_state(conn, {"ETH": (3000.0, 1e6, 5e8, 2.0)}, ts_ms=1000)
    S.save_market_state(conn, {"ETH": (3000.0, 1e6, 5e8, 2.0)}, ts_ms=1000)
    n = conn.execute("SELECT COUNT(*) FROM hl_far_whale_liq_market_state").fetchone()[0]
    assert n == 1
    assert S.read_market_series(conn, "ETH", 0, 2000) == [(1000, 3000.0)]


def test_assign_episode_same_level_reuses(conn):
    e1, new1 = S.assign_episode(conn, _cand(liq=2950.0), signal_ts_ms=1_000_000)
    e2, new2 = S.assign_episode(conn, _cand(liq=2950.5), signal_ts_ms=1_180_000)  # ~1.7bps drift
    assert new1 is True and new2 is False and e1 == e2


def test_assign_episode_liq_move_over_25bps_starts_new_and_closes_old(conn):
    e1, _ = S.assign_episode(conn, _cand(liq=2950.0), signal_ts_ms=1_000_000)
    moved = 2950.0 * (1 + 30 / 1e4)   # +30 bps
    e2, new2 = S.assign_episode(conn, _cand(liq=moved), signal_ts_ms=1_180_000)
    assert new2 is True and e2 != e1
    old = conn.execute("SELECT status FROM hl_far_whale_liq_episodes WHERE episode_id=?",
                       (e1,)).fetchone()[0]
    assert old == "liq_price_moved"


def test_assign_episode_60min_gap_starts_new(conn):
    e1, _ = S.assign_episode(conn, _cand(), signal_ts_ms=1_000_000)
    e2, new2 = S.assign_episode(conn, _cand(), signal_ts_ms=1_000_000 + 3_600_001)
    assert new2 is True and e2 != e1


def test_candidate_primary_flag_and_counts(conn):
    eid, _ = S.assign_episode(conn, _cand(), signal_ts_ms=1_000_000)
    S.insert_candidate(conn, _cand(), episode_id=eid, signal_ts_ms=1_000_000,
                       is_primary=1, source_snapshot_hash="h1")
    S.insert_candidate(conn, _cand(), episode_id=eid, signal_ts_ms=1_180_000,
                       is_primary=0, source_snapshot_hash="h2")
    rows = conn.execute("SELECT is_primary, study_version FROM hl_far_whale_liq_candidates"
                        " ORDER BY candidate_id").fetchall()
    assert [r[0] for r in rows] == [1, 0]
    assert all(r[1] == "fwlm-v0" for r in rows)


def test_freeze_roundtrip_and_fail_closed_before_freeze(conn):
    assert S.load_freeze(conn) is None            # no freeze -> capture must not enter
    S.save_report(conn, kind="freeze",
                  payload={"thresholds": {"ETH": 1e6}, "campaign_start_ts_ms": 5_000},
                  ts_ms=5_000)
    fz = S.load_freeze(conn)
    assert fz["thresholds"]["ETH"] == 1e6 and fz["campaign_start_ts_ms"] == 5_000


def test_entry_settle_roundtrip(conn):
    eid, _ = S.assign_episode(conn, _cand(), signal_ts_ms=1_000_000)
    entry_id = S.insert_entry(conn, {
        "candidate_id": 1, "episode_id": eid, "asset": "ETH",
        "direction_to_liquidation": "down", "entry_ts_ms": 1_000_000,
        "entry_price": 3000.0, "paper_notional": 1000.0, "d_bps": 166.7,
        "target_price": 2962.5, "adverse_stop_price": 3025.0,
        "liquidation_price": 2950.0, "spread_bps": 2.0, "is_primary": 1})
    assert S.open_entries(conn)[0]["entry_id"] == entry_id
    S.settle_entry(conn, entry_id, {
        "exit_ts_ms": 1_900_000, "exit_price": 2962.0, "exit_reason": "target_hit",
        "paper_pnl": 1.5, "mfe_bps": 130.0, "mae_bps": -20.0,
        "migrated_toward_liq": 1, "hit_liq_level": 0, "liq_fired": 0,
        "position_persisted": 1, "horizon_ms": 1_800_000})
    assert S.open_entries(conn) == []
    out = conn.execute("SELECT exit_reason, migrated_toward_liq FROM"
                       " hl_far_whale_liq_outcomes WHERE entry_id=?", (entry_id,)).fetchone()
    assert tuple(out) == ("target_hit", 1)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_far_whale_store.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Write the implementation**

```python
# sniper/hyperlive/far_whale_store.py
"""SQLite persistence for the fwlm-v0 far-whale magnet paper study (spec §10).
Forward-accruing, hyperlive_engine.sqlite. Episodes (§3.1) are the unit of observation."""
from __future__ import annotations

import json
import sqlite3

from sniper.hyperlive.far_whale import EPISODE_GAP_MS, EPISODE_LIQ_TOL_BPS, STUDY_VERSION
from sniper.hyperlive.persistence import connect  # noqa: F401  (re-export)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS hl_far_whale_liq_market_state (
            asset TEXT NOT NULL, ts_ms INTEGER NOT NULL,
            mid REAL NOT NULL, oi REAL, day_ntl_vlm REAL, spread_bps REAL,
            UNIQUE(asset, ts_ms)
        );
        CREATE INDEX IF NOT EXISTS idx_fwlm_ms_asset_ts
            ON hl_far_whale_liq_market_state(asset, ts_ms);

        CREATE TABLE IF NOT EXISTS hl_far_whale_liq_rounds (
            ts_ms INTEGER PRIMARY KEY,
            addresses_polled INTEGER, whales_seen INTEGER,
            anomalies_dropped INTEGER, candidates_new INTEGER, sweep_mode INTEGER
        );

        CREATE TABLE IF NOT EXISTS hl_far_whale_liq_position_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_ms INTEGER NOT NULL, address TEXT NOT NULL, asset TEXT NOT NULL,
            liquidation_price REAL NOT NULL, position_notional REAL NOT NULL,
            szi REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_fwlm_snap_addr_ts
            ON hl_far_whale_liq_position_snapshots(address, asset, ts_ms);

        CREATE TABLE IF NOT EXISTS hl_far_whale_liq_episodes (
            episode_id TEXT PRIMARY KEY,
            asset TEXT NOT NULL, whale_address TEXT NOT NULL,
            ref_liquidation_price REAL NOT NULL,
            first_ts_ms INTEGER NOT NULL, last_seen_ts_ms INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'open', n_candidates INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_fwlm_ep_addr
            ON hl_far_whale_liq_episodes(asset, whale_address, status);

        CREATE TABLE IF NOT EXISTS hl_far_whale_liq_candidates (
            candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
            episode_id TEXT NOT NULL, study_version TEXT NOT NULL,
            asset TEXT NOT NULL, signal_ts_ms INTEGER NOT NULL,
            mid_price REAL NOT NULL, liquidation_price REAL NOT NULL,
            distance_bps REAL NOT NULL, direction_to_liquidation TEXT NOT NULL,
            position_side TEXT NOT NULL, liquidation_type TEXT NOT NULL,
            whale_address TEXT NOT NULL, position_notional REAL NOT NULL,
            position_size REAL NOT NULL, spread_bps REAL,
            source TEXT NOT NULL DEFAULT 'clearinghouseState',
            source_snapshot_hash TEXT, is_primary INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_fwlm_cand_episode
            ON hl_far_whale_liq_candidates(episode_id);

        CREATE TABLE IF NOT EXISTS hl_far_whale_liq_paper_entries (
            entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER NOT NULL, episode_id TEXT NOT NULL,
            study_version TEXT NOT NULL, asset TEXT NOT NULL,
            direction_to_liquidation TEXT NOT NULL,
            entry_ts_ms INTEGER NOT NULL, entry_price REAL NOT NULL,
            paper_notional REAL NOT NULL, d_bps REAL NOT NULL,
            target_price REAL NOT NULL, adverse_stop_price REAL NOT NULL,
            liquidation_price REAL NOT NULL, spread_bps REAL,
            is_primary INTEGER NOT NULL DEFAULT 0,
            mfe_bps REAL NOT NULL DEFAULT 0, mae_bps REAL NOT NULL DEFAULT 0,
            whale_event TEXT, status TEXT NOT NULL DEFAULT 'open'
        );
        CREATE INDEX IF NOT EXISTS idx_fwlm_entry_status
            ON hl_far_whale_liq_paper_entries(status);

        CREATE TABLE IF NOT EXISTS hl_far_whale_liq_outcomes (
            entry_id INTEGER PRIMARY KEY,
            exit_ts_ms INTEGER NOT NULL, exit_price REAL NOT NULL,
            exit_reason TEXT NOT NULL, paper_pnl REAL NOT NULL,
            mfe_bps REAL NOT NULL, mae_bps REAL NOT NULL,
            migrated_toward_liq INTEGER NOT NULL, hit_liq_level INTEGER NOT NULL,
            liq_fired INTEGER, position_persisted INTEGER, horizon_ms INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS hl_far_whale_liq_controls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id INTEGER NOT NULL, control_type TEXT NOT NULL,
            matched_asset TEXT NOT NULL, matched_ts_ms INTEGER NOT NULL,
            direction_to_liquidation TEXT NOT NULL, d_bps REAL NOT NULL,
            paper_pnl REAL, migrated_toward_liq INTEGER, exit_reason TEXT,
            UNIQUE(entry_id, control_type)
        );

        CREATE TABLE IF NOT EXISTS hl_far_whale_liq_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_ms INTEGER NOT NULL, study_version TEXT NOT NULL,
            kind TEXT NOT NULL, payload_json TEXT NOT NULL
        );
    """)
    conn.commit()


def save_market_state(conn, rows: dict, ts_ms: int) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO hl_far_whale_liq_market_state"
        "(asset, ts_ms, mid, oi, day_ntl_vlm, spread_bps) VALUES (?,?,?,?,?,?)",
        [(a, int(ts_ms), v[0], v[1], v[2], v[3]) for a, v in rows.items()])
    conn.commit()


def save_round(conn, ts_ms: int, stats: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO hl_far_whale_liq_rounds"
        "(ts_ms, addresses_polled, whales_seen, anomalies_dropped, candidates_new, sweep_mode)"
        " VALUES (?,?,?,?,?,?)",
        (int(ts_ms), stats.get("addresses_polled", 0), stats.get("whales_seen", 0),
         stats.get("anomalies_dropped", 0), stats.get("candidates_new", 0),
         1 if stats.get("sweep_mode") else 0))
    conn.commit()


def save_snapshots(conn, ts_ms: int, rows: list) -> None:
    conn.executemany(
        "INSERT INTO hl_far_whale_liq_position_snapshots"
        "(ts_ms, address, asset, liquidation_price, position_notional, szi)"
        " VALUES (?,?,?,?,?,?)",
        [(int(ts_ms), r["address"], r["asset"], r["liquidation_price"],
          r["position_notional"], r["szi"]) for r in rows])
    conn.commit()


def assign_episode(conn, cand: dict, signal_ts_ms: int) -> tuple:
    """§3.1: same (asset, whale, liq ±25bps) within 60min = same episode. A >25bps liq
    move closes the old episode as liq_price_moved and opens a new one."""
    row = conn.execute(
        "SELECT episode_id, ref_liquidation_price, last_seen_ts_ms FROM hl_far_whale_liq_episodes"
        " WHERE asset=? AND whale_address=? AND status='open'"
        " ORDER BY last_seen_ts_ms DESC LIMIT 1",
        (cand["asset"], cand["whale_address"])).fetchone()
    if row is not None:
        drift_bps = abs(cand["liquidation_price"] - row["ref_liquidation_price"]) \
            / row["ref_liquidation_price"] * 1e4
        gap = signal_ts_ms - row["last_seen_ts_ms"]
        if drift_bps <= EPISODE_LIQ_TOL_BPS and gap <= EPISODE_GAP_MS:
            conn.execute(
                "UPDATE hl_far_whale_liq_episodes SET last_seen_ts_ms=?,"
                " n_candidates=n_candidates+1 WHERE episode_id=?",
                (int(signal_ts_ms), row["episode_id"]))
            conn.commit()
            return row["episode_id"], False
        conn.execute("UPDATE hl_far_whale_liq_episodes SET status=? WHERE episode_id=?",
                     ("liq_price_moved" if drift_bps > EPISODE_LIQ_TOL_BPS else "closed",
                      row["episode_id"]))
    episode_id = f"{cand['asset']}:{cand['whale_address']}:{int(signal_ts_ms)}"
    conn.execute(
        "INSERT INTO hl_far_whale_liq_episodes"
        "(episode_id, asset, whale_address, ref_liquidation_price, first_ts_ms,"
        " last_seen_ts_ms, status, n_candidates) VALUES (?,?,?,?,?,?,'open',1)",
        (episode_id, cand["asset"], cand["whale_address"], cand["liquidation_price"],
         int(signal_ts_ms), int(signal_ts_ms)))
    conn.commit()
    return episode_id, True


def insert_candidate(conn, cand: dict, *, episode_id: str, signal_ts_ms: int,
                     is_primary: int, source_snapshot_hash: str) -> int:
    cur = conn.execute(
        "INSERT INTO hl_far_whale_liq_candidates"
        "(episode_id, study_version, asset, signal_ts_ms, mid_price, liquidation_price,"
        " distance_bps, direction_to_liquidation, position_side, liquidation_type,"
        " whale_address, position_notional, position_size, spread_bps,"
        " source_snapshot_hash, is_primary)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (episode_id, STUDY_VERSION, cand["asset"], int(signal_ts_ms), cand["mid_price"],
         cand["liquidation_price"], cand["distance_bps"], cand["direction_to_liquidation"],
         cand["position_side"], cand["liquidation_type"], cand["whale_address"],
         cand["position_notional"], cand["position_size"], cand.get("spread_bps"),
         source_snapshot_hash, int(is_primary)))
    conn.commit()
    return cur.lastrowid


def open_episodes(conn) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM hl_far_whale_liq_episodes WHERE status='open'").fetchall()]


def update_episode(conn, episode_id: str, *, last_seen_ts_ms=None, status=None) -> None:
    if last_seen_ts_ms is not None:
        conn.execute("UPDATE hl_far_whale_liq_episodes SET last_seen_ts_ms=?"
                     " WHERE episode_id=?", (int(last_seen_ts_ms), episode_id))
    if status is not None:
        conn.execute("UPDATE hl_far_whale_liq_episodes SET status=? WHERE episode_id=?",
                     (status, episode_id))
    conn.commit()


def insert_entry(conn, entry: dict) -> int:
    cur = conn.execute(
        "INSERT INTO hl_far_whale_liq_paper_entries"
        "(candidate_id, episode_id, study_version, asset, direction_to_liquidation,"
        " entry_ts_ms, entry_price, paper_notional, d_bps, target_price,"
        " adverse_stop_price, liquidation_price, spread_bps, is_primary)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (entry["candidate_id"], entry["episode_id"], STUDY_VERSION, entry["asset"],
         entry["direction_to_liquidation"], int(entry["entry_ts_ms"]),
         entry["entry_price"], entry["paper_notional"], entry["d_bps"],
         entry["target_price"], entry["adverse_stop_price"], entry["liquidation_price"],
         entry.get("spread_bps"), int(entry.get("is_primary", 0))))
    conn.commit()
    return cur.lastrowid


def open_entries(conn) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM hl_far_whale_liq_paper_entries WHERE status='open'").fetchall()]


def update_entry_path(conn, entry_id: int, *, mfe_bps: float, mae_bps: float,
                      whale_event=None) -> None:
    conn.execute(
        "UPDATE hl_far_whale_liq_paper_entries SET mfe_bps=?, mae_bps=?,"
        " whale_event=COALESCE(?, whale_event) WHERE entry_id=?",
        (mfe_bps, mae_bps, whale_event, entry_id))
    conn.commit()


def settle_entry(conn, entry_id: int, outcome: dict) -> None:
    conn.execute("UPDATE hl_far_whale_liq_paper_entries SET status='closed' WHERE entry_id=?",
                 (entry_id,))
    conn.execute(
        "INSERT OR REPLACE INTO hl_far_whale_liq_outcomes"
        "(entry_id, exit_ts_ms, exit_price, exit_reason, paper_pnl, mfe_bps, mae_bps,"
        " migrated_toward_liq, hit_liq_level, liq_fired, position_persisted, horizon_ms)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (entry_id, int(outcome["exit_ts_ms"]), outcome["exit_price"],
         outcome["exit_reason"], outcome["paper_pnl"], outcome["mfe_bps"],
         outcome["mae_bps"], int(outcome["migrated_toward_liq"]),
         int(outcome["hit_liq_level"]), outcome.get("liq_fired"),
         outcome.get("position_persisted"), int(outcome["horizon_ms"])))
    conn.commit()


def save_report(conn, *, kind: str, payload: dict, ts_ms: int) -> None:
    conn.execute(
        "INSERT INTO hl_far_whale_liq_reports(ts_ms, study_version, kind, payload_json)"
        " VALUES (?,?,?,?)", (int(ts_ms), STUDY_VERSION, kind, json.dumps(payload)))
    conn.commit()


def latest_report(conn, kind: str) -> dict | None:
    row = conn.execute(
        "SELECT payload_json FROM hl_far_whale_liq_reports WHERE kind=? AND study_version=?"
        " ORDER BY ts_ms DESC, id DESC LIMIT 1", (kind, STUDY_VERSION)).fetchone()
    return json.loads(row["payload_json"]) if row else None


def load_freeze(conn) -> dict | None:
    return latest_report(conn, "freeze")


def read_market_series(conn, asset: str, since_ms: int, until_ms: int) -> list:
    return [(int(r["ts_ms"]), float(r["mid"])) for r in conn.execute(
        "SELECT ts_ms, mid FROM hl_far_whale_liq_market_state"
        " WHERE asset=? AND ts_ms>=? AND ts_ms<=? ORDER BY ts_ms",
        (asset, int(since_ms), int(until_ms))).fetchall()]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_far_whale_store.py tests/test_far_whale_detector.py -q`
Expected: all PASS.

- [ ] **Step 5: Regenerate the brain index (new ensure_* tables) and validate**

Run:
```bash
python3 scripts/brain_index.py generate
python3 scripts/validate_schema_refs.py
```
Expected: index regenerated with the 9 `hl_far_whale_liq_*` tables; validator clean.

- [ ] **Step 6: Commit**

```bash
git add sniper/hyperlive/far_whale_store.py tests/test_far_whale_store.py index/schema_registry.json index/dependency_index.json
git commit -m "feat(fwlm): store — schema, §3.1 episode assignment, freeze/report IO"
```

---

### Task 3: Paper simulator — §7 symbolic exits, pure path replay

**Files:**
- Create: `sniper/hyperlive/far_whale_sim.py`
- Test: `tests/test_far_whale_sim.py`

**Interfaces:**
- Consumes: `far_whale` constants (`TARGET_FRAC`, `ADVERSE_FRAC`, `HORIZON_MS`, `PAPER_NOTIONAL`, `MIGRATE_MIN_FRAC`, `FEE_BPS`).
- Produces (used by Tasks 4, 6, 7):
  - `plan_entry(candidate: dict, signal_ts_ms: int, *, paper_notional=PAPER_NOTIONAL) -> dict` — keys `asset, direction_to_liquidation, entry_ts_ms, entry_price (mid at signal), paper_notional, d_bps, target_price, adverse_stop_price, liquidation_price, spread_bps, mfe_bps, mae_bps`
  - `step_entry(entry: dict, mid: float, ts_ms: int, *, whale_event: str | None = None, horizon_ms=HORIZON_MS) -> dict | None` — mutates `entry["mfe_bps"]/["mae_bps"]`; returns exit dict `{exit_ts_ms, exit_price, exit_reason}` or None. `exit_reason ∈ {"target_hit","adverse_stop","time_stop","target_moved"}`
  - `settle(entry: dict, exit: dict, *, fee_bps=FEE_BPS, position_persisted: int | None = None, liq_fired: int | None = None, horizon_ms=HORIZON_MS) -> dict` — outcome dict matching `far_whale_store.settle_entry`
  - `simulate_path(direction: str, d_bps: float, path: list[tuple[int, float]], *, spread_bps: float, fee_bps=FEE_BPS, horizon_ms=HORIZON_MS) -> dict | None` — pure full replay (controls + diagnostics); `path[0]` is the entry round; None if path empty
- Cost model (pre-registered, stated in code): `paper_pnl_bps = s·(exit_mid − entry_mid)/entry_mid·1e4 − fee_bps − spread_bps_at_signal` (round-trip fee + crossing half the signal-time spread twice). MFE/MAE measured on mids, gross of costs.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_far_whale_sim.py
"""fwlm-v0 paper sim — §7 symbolic exits frozen at signal_ts."""
from sniper.hyperlive import far_whale as FW
from sniper.hyperlive import far_whale_sim as SIM


def _cand(direction="down"):
    liq = 2950.0 if direction == "down" else 3050.0
    side = "long" if direction == "down" else "short"
    return {"asset": "ETH", "whale_address": "0xabc", "liquidation_price": liq,
            "mid_price": 3000.0, "distance_bps": abs(liq - 3000.0) / 3000.0 * 1e4,
            "direction_to_liquidation": direction, "position_side": side,
            "liquidation_type": f"{side}_liquidation", "position_notional": 2e6,
            "position_size": 100.0, "spread_bps": 2.0}


def test_plan_entry_symbolic_levels_down():
    e = SIM.plan_entry(_cand("down"), 1_000_000)
    D = 3000.0 - 2950.0
    assert e["entry_price"] == 3000.0
    assert e["target_price"] == 3000.0 - FW.TARGET_FRAC * D      # 2962.5
    assert e["adverse_stop_price"] == 3000.0 + FW.ADVERSE_FRAC * D  # 3025.0


def test_plan_entry_symbolic_levels_up():
    e = SIM.plan_entry(_cand("up"), 1_000_000)
    D = 3050.0 - 3000.0
    assert e["target_price"] == 3000.0 + FW.TARGET_FRAC * D
    assert e["adverse_stop_price"] == 3000.0 - FW.ADVERSE_FRAC * D


def test_step_target_hit():
    e = SIM.plan_entry(_cand("down"), 1_000_000)
    assert SIM.step_entry(e, 2990.0, 1_180_000) is None
    ex = SIM.step_entry(e, 2960.0, 1_360_000)
    assert ex["exit_reason"] == "target_hit" and ex["exit_price"] == 2960.0
    assert e["mfe_bps"] > 0


def test_step_adverse_stop():
    e = SIM.plan_entry(_cand("down"), 1_000_000)
    ex = SIM.step_entry(e, 3026.0, 1_180_000)
    assert ex["exit_reason"] == "adverse_stop"
    assert e["mae_bps"] < 0


def test_step_time_stop_at_horizon():
    e = SIM.plan_entry(_cand("down"), 1_000_000)
    assert SIM.step_entry(e, 2995.0, 1_000_000 + FW.HORIZON_MS - 1) is None
    ex = SIM.step_entry(e, 2995.0, 1_000_000 + FW.HORIZON_MS)
    assert ex["exit_reason"] == "time_stop"


def test_step_target_moved_exits_immediately():
    e = SIM.plan_entry(_cand("down"), 1_000_000)
    ex = SIM.step_entry(e, 2995.0, 1_180_000, whale_event="liq_price_moved")
    assert ex["exit_reason"] == "target_moved"


def test_settle_pnl_net_of_fee_and_spread():
    e = SIM.plan_entry(_cand("down"), 1_000_000)
    ex = SIM.step_entry(e, 2960.0, 1_360_000)
    out = SIM.settle(e, ex)
    gross_bps = (3000.0 - 2960.0) / 3000.0 * 1e4          # ~133.3 (short wins on drop)
    net_bps = gross_bps - FW.FEE_BPS - 2.0                # fee + signal-time spread
    assert abs(out["paper_pnl"] - net_bps / 1e4 * FW.PAPER_NOTIONAL) < 0.01
    assert out["migrated_toward_liq"] == 1                # mfe >= 25% of D
    assert out["hit_liq_level"] == 0


def test_settle_migrated_label_uses_25pct_of_distance():
    e = SIM.plan_entry(_cand("down"), 1_000_000)
    # move 10% of D toward level, then time-stop: not migrated
    SIM.step_entry(e, 2995.0, 1_180_000)
    ex = SIM.step_entry(e, 2995.0, 1_000_000 + FW.HORIZON_MS)
    out = SIM.settle(e, ex)
    assert out["migrated_toward_liq"] == 0


def test_simulate_path_pure_replay_matches_stepping():
    path = [(1_000_000, 3000.0), (1_180_000, 2990.0), (1_360_000, 2960.0)]
    out = SIM.simulate_path("down", 166.67, path, spread_bps=2.0)
    assert out["exit_reason"] == "target_hit"
    assert out["migrated_toward_liq"] == 1


def test_simulate_path_empty_returns_none():
    assert SIM.simulate_path("down", 166.67, [], spread_bps=2.0) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_far_whale_sim.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Write the implementation**

```python
# sniper/hyperlive/far_whale_sim.py
"""fwlm-v0 paper simulator — §7 exits, all levels FROZEN at signal_ts.

Cost model (pre-registered): pnl_bps = s*(exit_mid-entry_mid)/entry_mid*1e4
- FEE_BPS - spread_bps_at_signal. MFE/MAE on mids, gross. Price path is the
market-state poll series (~180s granularity) — touch detection is
poll-granular, stated honestly in the report (§14)."""
from __future__ import annotations

from sniper.hyperlive.far_whale import (ADVERSE_FRAC, FEE_BPS, HORIZON_MS,
                                        MIGRATE_MIN_FRAC, PAPER_NOTIONAL, TARGET_FRAC)


def _sign(direction: str) -> float:
    return 1.0 if direction == "up" else -1.0


def plan_entry(candidate: dict, signal_ts_ms: int, *,
               paper_notional: float = PAPER_NOTIONAL) -> dict:
    mid = float(candidate["mid_price"])
    liq = float(candidate["liquidation_price"])
    s = _sign(candidate["direction_to_liquidation"])
    d_abs = abs(liq - mid)                     # frozen D in price terms
    return {
        "asset": candidate["asset"],
        "direction_to_liquidation": candidate["direction_to_liquidation"],
        "entry_ts_ms": int(signal_ts_ms),
        "entry_price": mid,
        "paper_notional": float(paper_notional),
        "d_bps": float(candidate["distance_bps"]),
        "target_price": mid + s * TARGET_FRAC * d_abs,
        "adverse_stop_price": mid - s * ADVERSE_FRAC * d_abs,
        "liquidation_price": liq,
        "spread_bps": float(candidate.get("spread_bps") or 0.0),
        "mfe_bps": 0.0,
        "mae_bps": 0.0,
    }


def step_entry(entry: dict, mid: float, ts_ms: int, *, whale_event: str | None = None,
               horizon_ms: int = HORIZON_MS) -> dict | None:
    """One poll round. Mutates mfe/mae; returns exit dict or None. Exit priority:
    target_moved (never re-target mid-trade) > target_hit > adverse_stop > time_stop."""
    s = _sign(entry["direction_to_liquidation"])
    excursion_bps = s * (mid - entry["entry_price"]) / entry["entry_price"] * 1e4
    entry["mfe_bps"] = max(entry["mfe_bps"], excursion_bps)
    entry["mae_bps"] = min(entry["mae_bps"], excursion_bps)
    if whale_event == "liq_price_moved":
        return {"exit_ts_ms": int(ts_ms), "exit_price": float(mid),
                "exit_reason": "target_moved"}
    if s * (mid - entry["target_price"]) >= 0:
        return {"exit_ts_ms": int(ts_ms), "exit_price": float(mid),
                "exit_reason": "target_hit"}
    if s * (mid - entry["adverse_stop_price"]) <= 0:
        return {"exit_ts_ms": int(ts_ms), "exit_price": float(mid),
                "exit_reason": "adverse_stop"}
    if ts_ms - entry["entry_ts_ms"] >= horizon_ms:
        return {"exit_ts_ms": int(ts_ms), "exit_price": float(mid),
                "exit_reason": "time_stop"}
    return None


def settle(entry: dict, exit: dict, *, fee_bps: float = FEE_BPS,
           position_persisted: int | None = None, liq_fired: int | None = None,
           horizon_ms: int = HORIZON_MS) -> dict:
    s = _sign(entry["direction_to_liquidation"])
    gross_bps = s * (exit["exit_price"] - entry["entry_price"]) / entry["entry_price"] * 1e4
    net_bps = gross_bps - fee_bps - entry.get("spread_bps", 0.0)
    liq_excursion_bps = s * (entry["liquidation_price"] - entry["entry_price"]) \
        / entry["entry_price"] * 1e4
    return {
        "exit_ts_ms": exit["exit_ts_ms"],
        "exit_price": exit["exit_price"],
        "exit_reason": exit["exit_reason"],
        "paper_pnl": net_bps / 1e4 * entry["paper_notional"],
        "mfe_bps": entry["mfe_bps"],
        "mae_bps": entry["mae_bps"],
        "migrated_toward_liq": 1 if entry["mfe_bps"] >= MIGRATE_MIN_FRAC * entry["d_bps"] else 0,
        "hit_liq_level": 1 if entry["mfe_bps"] >= liq_excursion_bps else 0,
        "liq_fired": liq_fired,
        "position_persisted": position_persisted,
        "horizon_ms": int(horizon_ms),
    }


def simulate_path(direction: str, d_bps: float, path: list, *, spread_bps: float,
                  fee_bps: float = FEE_BPS, horizon_ms: int = HORIZON_MS) -> dict | None:
    """Pure replay over [(ts_ms, mid), ...]; path[0] is the entry round. Used by the
    control generator (Tasks 6-7) and horizon diagnostics — the same exit engine as
    live stepping, so controls and treatment share one cost/exit model."""
    if not path:
        return None
    t0, mid0 = path[0]
    s = _sign(direction)
    pseudo = {
        "asset": "", "direction_to_liquidation": direction, "entry_ts_ms": int(t0),
        "entry_price": float(mid0), "paper_notional": PAPER_NOTIONAL, "d_bps": float(d_bps),
        "target_price": mid0 + s * TARGET_FRAC * (d_bps / 1e4) * mid0,
        "adverse_stop_price": mid0 - s * ADVERSE_FRAC * (d_bps / 1e4) * mid0,
        "liquidation_price": mid0 + s * (d_bps / 1e4) * mid0,
        "spread_bps": float(spread_bps), "mfe_bps": 0.0, "mae_bps": 0.0,
    }
    exit_ = None
    for ts, mid in path[1:]:
        exit_ = step_entry(pseudo, mid, ts, horizon_ms=horizon_ms)
        if exit_:
            break
    if exit_ is None:      # path ended before horizon — unresolved, caller decides
        return None
    return settle(pseudo, exit_, fee_bps=fee_bps, horizon_ms=horizon_ms)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_far_whale_sim.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/hyperlive/far_whale_sim.py tests/test_far_whale_sim.py
git commit -m "feat(fwlm): paper simulator — §7 symbolic exits frozen at signal_ts, pure path replay"
```

---

### Task 4: Capture runner — harvest → poll → snapshots/candidates/episodes → sim stepping

**Files:**
- Create: `scripts/run_far_whale_capture.py`
- Test: `tests/test_run_far_whale_capture.py`

**Interfaces:**
- Consumes: Tasks 1–3 (`far_whale`, `far_whale_store`, `far_whale_sim`); harvest helpers copied in pattern from `scripts/run_cluster_map.py` (`_harvest_leaderboard_addresses`, `_harvest_ws_addresses`); `metaAndAssetCtxs` for mids/OI/volume and `impactPxs` for spread.
- Produces: `run_round(conn, *, fetch_ctxs, fetch_state, harvest, now_ms, max_assets=20) -> dict` — the testable pure-ish round (all I/O injected); the `main()` loop calls it every `POLL_INTERVAL_S = 180`.
- Round contract:
  1. ctxs → `save_market_state` for ALL top-N assets (the Control-3 pool accrues from round one, candidate or not).
  2. addresses = leaderboard(200) ∪ WS-harvest(20s, cap 700) ∪ **all open-episode whale addresses** (persistence re-poll, Control 4).
  3. threaded `clearinghouseState`; whale rows in band ≥ `SWEEP_FLOOR_NOTIONAL` → `save_snapshots`.
  4. `load_freeze(conn)`: if None → **sweep mode**, no candidates/entries (fail closed). Else `evaluate_candidate` per whale row with the frozen per-asset threshold; count drop reasons; `assign_episode`; `insert_candidate` (`is_primary=1` iff episode is new); on new episode → `plan_entry` + `insert_entry` (`is_primary=1`).
  5. Persistence tracking: for each open episode, classify this round: `persisted` (same whale/level seen), `shrank` (notional < 50% of episode's first candidate notional), `closed` (address polled OK, position gone), `source_disappeared` (address poll failed), `liq_price_moved` (drift > 25 bps — also closes the episode via `assign_episode` semantics or directly). Non-`persisted` states update `hl_far_whale_liq_episodes.status` and feed `whale_event` into open entries.
  6. Step open entries with current mids (`step_entry`), settle exits (`settle` + `settle_entry`) with `position_persisted` = 1 iff the episode stayed `open` through exit; `liq_fired` = 1 iff whale closed while price was within 25 bps of the level (documented approximation), else 0, NULL when the whale outlived the trade unobserved.
  7. `save_round` with the counts; anomaly count printed in the round log line.
- Snapshot hash: `hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()[:16]` of the raw clearinghouseState that produced each candidate.

- [ ] **Step 1: Write the failing tests** — drive `run_round` with fakes; no network.

```python
# tests/test_run_far_whale_capture.py
"""fwlm-v0 capture round — fail-closed before freeze, episode/entry lifecycle, Control 4."""
import importlib.util
import sys
from pathlib import Path

import pytest

from sniper.hyperlive import far_whale_store as S

_spec = importlib.util.spec_from_file_location(
    "run_far_whale_capture",
    Path(__file__).resolve().parent.parent / "scripts" / "run_far_whale_capture.py")
cap = importlib.util.module_from_spec(_spec)
sys.modules["run_far_whale_capture"] = cap
_spec.loader.exec_module(cap)


def _ctxs(mid=3000.0, spread=2.0):
    return {"ETH": {"mid": mid, "oi": 1e6, "day_ntl_vlm": 5e8, "spread_bps": spread}}


def _whale_state(liq=2950.0, szi=700.0, entry=3010.0):
    # 700 * 3010 = $2.107M notional, ~167bps from 3000 -> in band, over $1M
    return {"assetPositions": [{"position": {
        "coin": "ETH", "liquidationPx": str(liq), "szi": str(szi), "entryPx": str(entry)}}]}


@pytest.fixture()
def conn():
    c = S.connect(":memory:")
    S.ensure_schema(c)
    return c


def _freeze(conn, ts=0):
    S.save_report(conn, kind="freeze",
                  payload={"thresholds": {"ETH": 1_000_000.0},
                           "campaign_start_ts_ms": ts}, ts_ms=ts)


def test_sweep_mode_before_freeze_no_candidates(conn):
    stats = cap.run_round(
        conn, fetch_ctxs=lambda: _ctxs(),
        fetch_state=lambda a: _whale_state(),
        harvest=lambda assets: ["0xwhale"], now_ms=1_000_000)
    assert stats["sweep_mode"] is True
    assert conn.execute("SELECT COUNT(*) FROM hl_far_whale_liq_candidates").fetchone()[0] == 0
    # but the sweep pool + control pool still accrue
    assert conn.execute("SELECT COUNT(*) FROM hl_far_whale_liq_position_snapshots").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM hl_far_whale_liq_market_state").fetchone()[0] == 1


def test_frozen_candidate_creates_episode_and_primary_entry(conn):
    _freeze(conn)
    cap.run_round(conn, fetch_ctxs=lambda: _ctxs(),
                  fetch_state=lambda a: _whale_state(),
                  harvest=lambda assets: ["0xwhale"], now_ms=1_000_000)
    cands = conn.execute("SELECT is_primary FROM hl_far_whale_liq_candidates").fetchall()
    entries = conn.execute("SELECT is_primary, status FROM hl_far_whale_liq_paper_entries").fetchall()
    assert [c[0] for c in cands] == [1]
    assert [(e[0], e[1]) for e in entries] == [(1, "open")]


def test_second_round_same_whale_no_second_entry(conn):
    _freeze(conn)
    for ts in (1_000_000, 1_180_000):
        cap.run_round(conn, fetch_ctxs=lambda: _ctxs(),
                      fetch_state=lambda a: _whale_state(),
                      harvest=lambda assets: ["0xwhale"], now_ms=ts)
    assert conn.execute("SELECT COUNT(*) FROM hl_far_whale_liq_paper_entries").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM hl_far_whale_liq_candidates").fetchone()[0] == 2
    assert conn.execute("SELECT n_candidates FROM hl_far_whale_liq_episodes").fetchone()[0] == 2


def test_anomaly_counted_and_dropped(conn):
    _freeze(conn)
    # long with liq ABOVE mid -> §2 anomaly
    stats = cap.run_round(conn, fetch_ctxs=lambda: _ctxs(),
                          fetch_state=lambda a: _whale_state(liq=3050.0, szi=700.0),
                          harvest=lambda assets: ["0xwhale"], now_ms=1_000_000)
    assert stats["anomalies_dropped"] == 1
    assert conn.execute("SELECT COUNT(*) FROM hl_far_whale_liq_candidates").fetchone()[0] == 0


def test_whale_close_sets_episode_closed_and_entry_exits_on_target_moved_only_for_move(conn):
    _freeze(conn)
    cap.run_round(conn, fetch_ctxs=lambda: _ctxs(),
                  fetch_state=lambda a: _whale_state(),
                  harvest=lambda assets: ["0xwhale"], now_ms=1_000_000)
    # whale still polled OK but position gone -> episode closed; entry keeps running (ITT)
    cap.run_round(conn, fetch_ctxs=lambda: _ctxs(mid=2990.0),
                  fetch_state=lambda a: {"assetPositions": []},
                  harvest=lambda assets: [], now_ms=1_180_000)
    ep = conn.execute("SELECT status FROM hl_far_whale_liq_episodes").fetchone()[0]
    entry = conn.execute("SELECT status FROM hl_far_whale_liq_paper_entries").fetchone()[0]
    assert ep == "closed"
    assert entry == "open"          # ITT: evaporation never force-exits the paper trade


def test_target_hit_settles_with_position_persisted_flag(conn):
    _freeze(conn)
    cap.run_round(conn, fetch_ctxs=lambda: _ctxs(),
                  fetch_state=lambda a: _whale_state(),
                  harvest=lambda assets: ["0xwhale"], now_ms=1_000_000)
    cap.run_round(conn, fetch_ctxs=lambda: _ctxs(mid=2960.0),   # past 75% target 2962.5
                  fetch_state=lambda a: _whale_state(),
                  harvest=lambda assets: [], now_ms=1_180_000)
    out = conn.execute("SELECT exit_reason, position_persisted FROM"
                       " hl_far_whale_liq_outcomes").fetchone()
    assert tuple(out) == ("target_hit", 1)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_run_far_whale_capture.py -q`
Expected: FAIL — script missing.

- [ ] **Step 3: Write the implementation**

```python
#!/usr/bin/env python3
# scripts/run_far_whale_capture.py
"""fwlm-v0 far-whale magnet capture — forward-only, paper-only, public data only.

Spec: docs/superpowers/specs/2026-07-04-hl-far-whale-liquidation-magnet-paper-study.md
Per round (local wall clock, self-referential — spec §10):
  market_state for ALL top-N assets (Control-3 pool) -> harvest+poll whales ->
  snapshots (sweep pool) -> [if thresholds frozen] candidates/episodes/entries ->
  persistence tracking (Control 4, diagnostic only — never filters the primary set) ->
  step open paper entries. NO candidates or entries before a freeze row exists."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.hyperlive import far_whale as FW            # noqa: E402
from sniper.hyperlive import far_whale_sim as SIM        # noqa: E402
from sniper.hyperlive import far_whale_store as S        # noqa: E402

HL_INFO = "https://api.hyperliquid.xyz/info"
DEFAULT_DB = "/home/bigpapi/Desktop/Sniper/data/hyperlive_engine.sqlite"
POLL_INTERVAL_S = 180
SHRANK_FRAC = 0.50


def _info(payload: dict):
    import requests
    r = requests.post(HL_INFO, json=payload, timeout=15)
    r.raise_for_status()
    return r.json()


def fetch_ctxs_live(max_assets: int = 20) -> dict:
    """top-N by 24h notional -> {ASSET: {mid, oi, day_ntl_vlm, spread_bps}}.
    spread from impactPxs (impact bid/ask); missing -> None (candidate gate fails closed)."""
    meta, ctxs = _info({"type": "metaAndAssetCtxs"})
    names = [m["name"] for m in meta.get("universe") or []]
    pairs = sorted(zip(names, ctxs or []), key=lambda x: -float(x[1].get("dayNtlVlm") or 0))
    out = {}
    for name, c in pairs[:max_assets]:
        mid = c.get("midPx") or c.get("markPx")
        if not mid:
            continue
        mid = float(mid)
        spread = None
        imp = c.get("impactPxs")
        if isinstance(imp, list) and len(imp) == 2:
            try:
                bid, ask = float(imp[0]), float(imp[1])
                if ask > bid > 0:
                    spread = (ask - bid) / mid * 1e4
            except (TypeError, ValueError):
                pass
        out[name.upper()] = {"mid": mid,
                             "oi": float(c.get("openInterest") or 0) or None,
                             "day_ntl_vlm": float(c.get("dayNtlVlm") or 0) or None,
                             "spread_bps": spread}
    return out


def harvest_live(assets: list) -> list:
    """Leaderboard whales UNION trades-WS actives — same multi-source pattern as
    run_cluster_map (whales first so the cap can't starve them)."""
    from scripts.run_cluster_map import _harvest_addresses  # reuse, don't fork
    return _harvest_addresses(assets, 20, 700, leaderboard_top=200)


def fetch_state_live(address: str) -> dict:
    return _info({"type": "clearinghouseState", "user": address})


def run_round(conn, *, fetch_ctxs, fetch_state, harvest, now_ms: int,
              max_assets: int = 20) -> dict:
    ctxs = fetch_ctxs()
    S.save_market_state(conn, {a: (c["mid"], c.get("oi"), c.get("day_ntl_vlm"),
                                   c.get("spread_bps")) for a, c in ctxs.items()},
                        ts_ms=now_ms)

    episodes = S.open_episodes(conn)
    tracked = {e["whale_address"] for e in episodes}
    addrs = list(dict.fromkeys(list(harvest(list(ctxs))) + sorted(tracked)))

    polled: dict = {}          # address -> (whale_rows | None on poll failure, raw_state)
    def _poll(a):
        try:
            st = fetch_state(a)
            return a, FW.whale_rows_from_clearinghouse(st, a), st
        except Exception as e:  # noqa: BLE001 — poll failure = source_disappeared signal
            print(f"poll fail {a[:10]}: {type(e).__name__}", file=sys.stderr)
            return a, None, None
    with ThreadPoolExecutor(max_workers=24) as ex:
        for a, rows, st in ex.map(_poll, addrs):
            polled[a] = (rows, st)

    in_band = []
    for rows, _st in polled.values():
        for r in rows or []:
            mid = (ctxs.get(r["asset"]) or {}).get("mid")
            if not mid:
                continue
            dist = abs(r["liquidation_price"] - mid) / mid * 1e4
            if FW.DIST_MIN_BPS <= dist <= FW.DIST_MAX_BPS \
                    and r["position_notional"] >= FW.SWEEP_FLOOR_NOTIONAL:
                in_band.append(r)
    S.save_snapshots(conn, now_ms, in_band)

    freeze = S.load_freeze(conn)
    stats = {"addresses_polled": len(addrs), "whales_seen": len(in_band),
             "anomalies_dropped": 0, "candidates_new": 0, "sweep_mode": freeze is None}

    if freeze is not None:
        thresholds = freeze["thresholds"]
        for addr, (rows, st) in polled.items():
            for r in rows or []:
                thr = thresholds.get(r["asset"])
                if thr is None:
                    continue
                c = ctxs.get(r["asset"]) or {}
                cand, reason = FW.evaluate_candidate(
                    r, c.get("mid"), spread_bps=c.get("spread_bps"),
                    staleness_ms=0, threshold_notional=float(thr))
                if reason == "anomaly_side_invariant":
                    stats["anomalies_dropped"] += 1
                if cand is None:
                    continue
                episode_id, is_new = S.assign_episode(conn, cand, now_ms)
                snap_hash = hashlib.sha256(
                    json.dumps(st, sort_keys=True).encode()).hexdigest()[:16]
                cid = S.insert_candidate(conn, cand, episode_id=episode_id,
                                         signal_ts_ms=now_ms, is_primary=int(is_new),
                                         source_snapshot_hash=snap_hash)
                stats["candidates_new"] += 1
                if is_new:
                    entry = SIM.plan_entry(cand, now_ms)
                    entry.update({"candidate_id": cid, "episode_id": episode_id,
                                  "is_primary": 1})
                    S.insert_entry(conn, entry)

    # --- Control 4 persistence tracking (diagnostic; never filters the primary set) ---
    events: dict = {}          # episode_id -> whale_event
    for ep in episodes:
        rows, _ = polled.get(ep["whale_address"], (None, None))
        if rows is None:
            status = "source_disappeared"
        else:
            match = [r for r in rows if r["asset"] == ep["asset"]]
            if not match:
                status = "closed"
            else:
                r = match[0]
                drift = abs(r["liquidation_price"] - ep["ref_liquidation_price"]) \
                    / ep["ref_liquidation_price"] * 1e4
                first_notl = conn.execute(
                    "SELECT position_notional FROM hl_far_whale_liq_candidates"
                    " WHERE episode_id=? ORDER BY candidate_id LIMIT 1",
                    (ep["episode_id"],)).fetchone()
                if drift > FW.EPISODE_LIQ_TOL_BPS:
                    status = "liq_price_moved"
                elif first_notl and r["position_notional"] < SHRANK_FRAC * first_notl[0]:
                    status = "shrank"
                else:
                    status = "persisted"
        if status == "persisted":
            S.update_episode(conn, ep["episode_id"], last_seen_ts_ms=now_ms)
        else:
            S.update_episode(conn, ep["episode_id"], status=status)
            events[ep["episode_id"]] = status

    # --- step open paper entries against current mids ---
    ep_status = {e["episode_id"]: e["status"] for e in
                 (dict(r) for r in conn.execute(
                     "SELECT episode_id, status FROM hl_far_whale_liq_episodes"))}
    for entry in S.open_entries(conn):
        c = ctxs.get(entry["asset"]) or {}
        mid = c.get("mid")
        if not mid:
            continue
        whale_event = events.get(entry["episode_id"]) or entry.get("whale_event")
        exit_ = SIM.step_entry(entry, mid, now_ms,
                               whale_event=("liq_price_moved"
                                            if whale_event == "liq_price_moved" else None))
        S.update_entry_path(conn, entry["entry_id"], mfe_bps=entry["mfe_bps"],
                            mae_bps=entry["mae_bps"], whale_event=whale_event)
        if exit_ is None:
            continue
        status_now = ep_status.get(entry["episode_id"], "open")
        persisted = 1 if status_now in ("open",) else 0
        dist_now = abs(mid - entry["liquidation_price"]) / mid * 1e4
        liq_fired = None
        if status_now in ("closed", "source_disappeared"):
            liq_fired = 1 if dist_now <= 25.0 else 0   # documented approximation
        elif status_now == "open":
            liq_fired = 0
        out = SIM.settle(entry, exit_, position_persisted=persisted, liq_fired=liq_fired)
        S.settle_entry(conn, entry["entry_id"], out)

    S.save_round(conn, now_ms, stats)
    print(f"fwlm round ts={now_ms} addrs={stats['addresses_polled']} "
          f"whales={stats['whales_seen']} anomalies={stats['anomalies_dropped']} "
          f"cands={stats['candidates_new']} sweep_mode={stats['sweep_mode']}", flush=True)
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="fwlm-v0 far-whale capture (paper, read-only)")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--max-assets", type=int, default=20)
    ap.add_argument("--interval-seconds", type=int, default=POLL_INTERVAL_S)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args(argv)
    conn = S.connect(args.db)
    S.ensure_schema(conn)
    try:
        while True:
            try:
                run_round(conn, fetch_ctxs=lambda: fetch_ctxs_live(args.max_assets),
                          fetch_state=fetch_state_live, harvest=harvest_live,
                          now_ms=int(time.time() * 1000), max_assets=args.max_assets)
            except Exception as e:  # noqa: BLE001
                print(f"fwlm round failed: {type(e).__name__}: {e}", file=sys.stderr)
            if args.once:
                return 0
            time.sleep(args.interval_seconds)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_run_far_whale_capture.py -q`
Expected: all PASS. If `from scripts.run_cluster_map import ...` breaks module loading in tests (import side effects), move the import inside `harvest_live` (it already is inside the function — keep it there).

- [ ] **Step 5: Commit**

```bash
git add scripts/run_far_whale_capture.py tests/test_run_far_whale_capture.py
git commit -m "feat(fwlm): capture runner — fail-closed sweep mode, episode/entry lifecycle, Control-4 tracking"
```

---

### Task 5: Threshold sweep + freeze (§4)

**Files:**
- Create: `scripts/far_whale_threshold_sweep.py`
- Test: `tests/test_far_whale_sweep.py`

**Interfaces:**
- Consumes: `far_whale_store` (`connect`, snapshots table, `save_report`), `far_whale.STUDY_VERSION`.
- Produces:
  - `sweep_counts(conn, *, thresholds: list[float], since_ms: int, until_ms: int) -> dict` — `{asset: {threshold: {"episodes_per_day": float, "distinct_whales": int}}}`. Counts **episode-like units** (whale-address × contiguous presence with the §3.1 gap rule applied to snapshot timestamps), NOT raw snapshot rows — otherwise the sweep itself inherits the row-inflation bug.
  - CLI: `python3 scripts/far_whale_threshold_sweep.py --db <path>` prints the `asset × threshold → candidates/day` table (§4 deliverable); `--freeze "BTC=1000000,ETH=1000000,SOL=250000"` writes the freeze report `{thresholds, campaign_start_ts_ms=now}` — the one operator decision this study takes.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_far_whale_sweep.py
"""§4 sweep — episode-unit counts per asset x threshold; freeze writes the report."""
import importlib.util
import sys
from pathlib import Path

from sniper.hyperlive import far_whale_store as S

_spec = importlib.util.spec_from_file_location(
    "far_whale_threshold_sweep",
    Path(__file__).resolve().parent.parent / "scripts" / "far_whale_threshold_sweep.py")
sweep = importlib.util.module_from_spec(_spec)
sys.modules["far_whale_threshold_sweep"] = sweep
_spec.loader.exec_module(sweep)

DAY = 86_400_000


def _snap(conn, ts, addr, notional, liq=2950.0):
    S.save_snapshots(conn, ts, [{"address": addr, "asset": "ETH",
                                 "liquidation_price": liq,
                                 "position_notional": notional, "szi": 100.0}])


def test_sweep_counts_episodes_not_rows():
    conn = S.connect(":memory:")
    S.ensure_schema(conn)
    # one whale seen 10 consecutive rounds = ONE episode-unit, not ten
    for i in range(10):
        _snap(conn, 1_000_000 + i * 180_000, "0xa", 2_000_000.0)
    # another whale, below the 1M threshold
    _snap(conn, 1_000_000, "0xb", 400_000.0)
    out = sweep.sweep_counts(conn, thresholds=[250_000.0, 1_000_000.0],
                             since_ms=0, until_ms=DAY)
    assert out["ETH"][1_000_000.0]["distinct_whales"] == 1
    assert out["ETH"][1_000_000.0]["episodes_per_day"] == 1.0
    assert out["ETH"][250_000.0]["distinct_whales"] == 2


def test_gap_splits_episode_units():
    conn = S.connect(":memory:")
    S.ensure_schema(conn)
    _snap(conn, 1_000_000, "0xa", 2_000_000.0)
    _snap(conn, 1_000_000 + 3_600_001 + 180_000, "0xa", 2_000_000.0)  # > 60min gap
    out = sweep.sweep_counts(conn, thresholds=[1_000_000.0], since_ms=0, until_ms=DAY)
    assert out["ETH"][1_000_000.0]["episodes_per_day"] == 2.0


def test_freeze_writes_report():
    conn = S.connect(":memory:")
    S.ensure_schema(conn)
    sweep.freeze(conn, {"ETH": 1_000_000.0, "SOL": 250_000.0}, now_ms=42_000)
    fz = S.load_freeze(conn)
    assert fz["thresholds"] == {"ETH": 1_000_000.0, "SOL": 250_000.0}
    assert fz["campaign_start_ts_ms"] == 42_000
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_far_whale_sweep.py -q`
Expected: FAIL — script missing.

- [ ] **Step 3: Write the implementation**

```python
#!/usr/bin/env python3
# scripts/far_whale_threshold_sweep.py
"""§4 threshold sweep for fwlm-v0. Reports asset x threshold -> episode-units/day from
the captured snapshot pool, then (operator decision) freezes exactly one threshold per
asset. Counts EPISODE-UNITS (whale x contiguous presence, §3.1 gap rule) — raw snapshot
rows would inflate with poll cadence. Changing a frozen threshold later = new study_version."""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.hyperlive.far_whale import EPISODE_GAP_MS  # noqa: E402
from sniper.hyperlive import far_whale_store as S      # noqa: E402

DEFAULT_DB = "/home/bigpapi/Desktop/Sniper/data/hyperlive_engine.sqlite"
DEFAULT_THRESHOLDS = [250_000.0, 500_000.0, 1_000_000.0, 2_000_000.0]


def sweep_counts(conn, *, thresholds: list, since_ms: int, until_ms: int) -> dict:
    rows = conn.execute(
        "SELECT asset, address, ts_ms, position_notional"
        " FROM hl_far_whale_liq_position_snapshots"
        " WHERE ts_ms>=? AND ts_ms<=? ORDER BY asset, address, ts_ms",
        (int(since_ms), int(until_ms))).fetchall()
    days = max((until_ms - since_ms) / 86_400_000.0, 1e-9)
    out: dict = {}
    for thr in thresholds:
        # episode-unit = contiguous presence (gap <= EPISODE_GAP_MS) of one whale >= thr
        units = defaultdict(int)
        whales = defaultdict(set)
        last_seen: dict = {}
        for r in rows:
            if r["position_notional"] < thr:
                continue
            key = (r["asset"], r["address"])
            prev = last_seen.get(key)
            if prev is None or r["ts_ms"] - prev > EPISODE_GAP_MS:
                units[r["asset"]] += 1
                whales[r["asset"]].add(r["address"])
            last_seen[key] = r["ts_ms"]
        for asset in set(list(units) + list(whales)):
            out.setdefault(asset, {})[thr] = {
                "episodes_per_day": round(units[asset] / days, 2),
                "distinct_whales": len(whales[asset]),
            }
    return out


def freeze(conn, thresholds: dict, *, now_ms: int) -> None:
    S.save_report(conn, kind="freeze",
                  payload={"thresholds": {k: float(v) for k, v in thresholds.items()},
                           "campaign_start_ts_ms": int(now_ms)},
                  ts_ms=int(now_ms))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="fwlm-v0 §4 threshold sweep / freeze")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--freeze", default=None,
                    help='e.g. "BTC=1000000,ETH=1000000,SOL=250000" — writes the freeze report')
    args = ap.parse_args(argv)
    conn = S.connect(args.db)
    S.ensure_schema(conn)
    now_ms = int(time.time() * 1000)
    if args.freeze:
        if S.load_freeze(conn) is not None:
            print("REFUSED: a freeze already exists — changing thresholds requires a new "
                  "study_version (spec §4), not a re-freeze.", file=sys.stderr)
            return 1
        thresholds = dict(kv.split("=") for kv in args.freeze.split(","))
        freeze(conn, {k.upper(): float(v) for k, v in thresholds.items()}, now_ms=now_ms)
        print(f"frozen: {thresholds} campaign_start_ts_ms={now_ms}")
        return 0
    since = now_ms - int(args.days * 86_400_000)
    table = sweep_counts(conn, thresholds=DEFAULT_THRESHOLDS, since_ms=since, until_ms=now_ms)
    print(f"asset x threshold -> episode-units/day (last {args.days:g}d, §3.1 gap rule)")
    for asset in sorted(table):
        for thr in DEFAULT_THRESHOLDS:
            cell = table[asset].get(thr)
            if cell:
                print(f"  {asset:8s} >=${thr/1e6:.2f}M  {cell['episodes_per_day']:6.2f}/day"
                      f"  whales={cell['distinct_whales']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_far_whale_sweep.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/far_whale_threshold_sweep.py tests/test_far_whale_sweep.py
git commit -m "feat(fwlm): §4 threshold sweep (episode-units/day) + one-shot freeze"
```

---

### Task 6: Control generator — shuffle-side + trend-matched (§9 Controls 2–3)

**Files:**
- Create: `sniper/hyperlive/far_whale_eval.py`
- Test: `tests/test_far_whale_eval.py`

**Interfaces:**
- Consumes: `far_whale_store` (series/controls/outcomes IO), `far_whale_sim.simulate_path`, `far_whale` constants.
- Produces (used by Task 7):
  - `features_at(conn, asset: str, ts_ms: int) -> dict | None` — `{ret_30m_bps, vol_30m_bps, spread_bps, oi_delta_30m_pct, hour_of_day}` from the market-state series; None if the 30-min lookback is incomplete
  - `shuffle_control(conn, entry: dict) -> dict | None` — flipped-direction `simulate_path` over the entry's own recorded path
  - `trend_matched_control(conn, entry: dict, *, exclusion_ms: int = 3_600_000) -> dict | None` — nearest z-scored neighbor over same-asset market-state moments ≥ `exclusion_ms` away from ANY candidate `signal_ts_ms` for that asset; inherits the treatment's `direction_to_liquidation` and `d_bps`; simulated with `simulate_path`
  - `generate_controls(conn) -> dict` — for every settled **primary** entry missing controls, write one `shuffle` + one `trend` row into `hl_far_whale_liq_controls`; returns `{"shuffle": n, "trend": n, "unmatched": n}`
- Matching is applied only after treatment outcomes are fixed; the pool exists from round one (capture writes market_state unconditionally) — pre-treatment-blind per §9.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_far_whale_eval.py
"""fwlm-v0 controls — shuffle-side + trend-matched generation (§9 C2-C3)."""
from sniper.hyperlive import far_whale_store as S
from sniper.hyperlive import far_whale_eval as E

R = 180_000   # poll round


def _seed_series(conn, asset="ETH", t0=0, n=40, mid0=3000.0, drift=0.0):
    for i in range(n):
        S.save_market_state(conn, {asset: (mid0 * (1 + drift * i), 1e6, 5e8, 2.0)},
                            ts_ms=t0 + i * R)


def _mk_settled_entry(conn, *, entry_ts, direction="down", d_bps=166.7):
    eid, _ = S.assign_episode(conn, {
        "asset": "ETH", "whale_address": "0xabc", "liquidation_price": 2950.0,
        "mid_price": 3000.0, "distance_bps": d_bps,
        "direction_to_liquidation": direction, "position_side": "long",
        "liquidation_type": "long_liquidation", "position_notional": 2e6,
        "position_size": 100.0, "spread_bps": 2.0}, signal_ts_ms=entry_ts)
    cid = S.insert_candidate(conn, {
        "asset": "ETH", "whale_address": "0xabc", "liquidation_price": 2950.0,
        "mid_price": 3000.0, "distance_bps": d_bps,
        "direction_to_liquidation": direction, "position_side": "long",
        "liquidation_type": "long_liquidation", "position_notional": 2e6,
        "position_size": 100.0, "spread_bps": 2.0},
        episode_id=eid, signal_ts_ms=entry_ts, is_primary=1, source_snapshot_hash="h")
    entry_id = S.insert_entry(conn, {
        "candidate_id": cid, "episode_id": eid, "asset": "ETH",
        "direction_to_liquidation": direction, "entry_ts_ms": entry_ts,
        "entry_price": 3000.0, "paper_notional": 1000.0, "d_bps": d_bps,
        "target_price": 2962.5, "adverse_stop_price": 3025.0,
        "liquidation_price": 2950.0, "spread_bps": 2.0, "is_primary": 1})
    S.settle_entry(conn, entry_id, {
        "exit_ts_ms": entry_ts + 10 * R, "exit_price": 2990.0, "exit_reason": "time_stop",
        "paper_pnl": 0.5, "mfe_bps": 40.0, "mae_bps": -10.0,
        "migrated_toward_liq": 0, "hit_liq_level": 0, "liq_fired": 0,
        "position_persisted": 1, "horizon_ms": 1_800_000})
    return entry_id


def _conn():
    c = S.connect(":memory:")
    S.ensure_schema(c)
    return c


def test_features_at_needs_full_lookback():
    conn = _conn()
    _seed_series(conn, t0=0, n=40)
    assert E.features_at(conn, "ETH", 2 * R) is None          # lookback incomplete
    f = E.features_at(conn, "ETH", 20 * R)
    assert set(f) == {"ret_30m_bps", "vol_30m_bps", "spread_bps",
                      "oi_delta_30m_pct", "hour_of_day"}


def test_shuffle_control_flips_direction_same_path():
    conn = _conn()
    # falling path: down-treatment wins, flipped 'up' control must lose
    for i in range(15):
        S.save_market_state(conn, {"ETH": (3000.0 - i * 5, 1e6, 5e8, 2.0)}, ts_ms=i * R)
    entry_id = _mk_settled_entry(conn, entry_ts=0)
    entry = dict(conn.execute("SELECT * FROM hl_far_whale_liq_paper_entries"
                              " WHERE entry_id=?", (entry_id,)).fetchone())
    ctl = E.shuffle_control(conn, entry)
    assert ctl is not None and ctl["direction_to_liquidation"] == "up"
    assert ctl["paper_pnl"] < 0


def test_trend_match_excludes_candidate_adjacent_moments():
    conn = _conn()
    _seed_series(conn, t0=0, n=200)
    entry_ts = 100 * R
    entry_id = _mk_settled_entry(conn, entry_ts=entry_ts)
    entry = dict(conn.execute("SELECT * FROM hl_far_whale_liq_paper_entries"
                              " WHERE entry_id=?", (entry_id,)).fetchone())
    ctl = E.trend_matched_control(conn, entry)
    assert ctl is not None
    assert abs(ctl["matched_ts_ms"] - entry_ts) >= 3_600_000   # outside exclusion window


def test_generate_controls_writes_both_rows_once():
    conn = _conn()
    _seed_series(conn, t0=0, n=200)
    _mk_settled_entry(conn, entry_ts=100 * R)
    r1 = E.generate_controls(conn)
    r2 = E.generate_controls(conn)          # idempotent — UNIQUE(entry_id, control_type)
    assert r1["shuffle"] == 1 and r1["trend"] == 1
    assert r2["shuffle"] == 0 and r2["trend"] == 0
    n = conn.execute("SELECT COUNT(*) FROM hl_far_whale_liq_controls").fetchone()[0]
    assert n == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_far_whale_eval.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Write the implementation**

```python
# sniper/hyperlive/far_whale_eval.py
"""fwlm-v0 controls + verdict (§9, §12-§13).

Control 2 (shuffle-side): flipped direction over the SAME recorded path — close to a
pure drift test; never sufficient alone. Control 3 (trend-matched): nearest same-asset
non-whale moment from the continuously-captured market_state pool (pre-treatment-blind);
inherits the treatment's direction + D. Both share simulate_path so cost/exit models
cannot diverge between treatment and control."""
from __future__ import annotations

import math
import sqlite3

from sniper.hyperlive.far_whale import HORIZON_MS
from sniper.hyperlive import far_whale_store as S
from sniper.hyperlive.far_whale_sim import simulate_path

ROUND_MS = 180_000
LOOKBACK_MS = 30 * 60_000
FEATURE_KEYS = ("ret_30m_bps", "vol_30m_bps", "spread_bps", "oi_delta_30m_pct")


def features_at(conn: sqlite3.Connection, asset: str, ts_ms: int) -> dict | None:
    rows = conn.execute(
        "SELECT ts_ms, mid, oi, spread_bps FROM hl_far_whale_liq_market_state"
        " WHERE asset=? AND ts_ms>=? AND ts_ms<=? ORDER BY ts_ms",
        (asset, int(ts_ms - LOOKBACK_MS), int(ts_ms))).fetchall()
    if len(rows) < 5 or rows[0]["ts_ms"] > ts_ms - LOOKBACK_MS + 2 * ROUND_MS:
        return None                     # incomplete lookback -> not matchable
    mids = [r["mid"] for r in rows]
    rets = [(mids[i + 1] - mids[i]) / mids[i] * 1e4 for i in range(len(mids) - 1)]
    mean_r = sum(rets) / len(rets)
    vol = math.sqrt(sum((r - mean_r) ** 2 for r in rets) / max(len(rets) - 1, 1))
    oi0, oi1 = rows[0]["oi"], rows[-1]["oi"]
    oi_delta = ((oi1 - oi0) / oi0 * 100.0) if (oi0 and oi1) else 0.0
    return {
        "ret_30m_bps": (mids[-1] - mids[0]) / mids[0] * 1e4,
        "vol_30m_bps": vol,
        "spread_bps": rows[-1]["spread_bps"] or 0.0,
        "oi_delta_30m_pct": oi_delta,
        "hour_of_day": (int(ts_ms) // 3_600_000) % 24,   # local-clock hour, self-consistent
    }


def _entry_path(conn, asset: str, entry_ts_ms: int) -> list:
    return S.read_market_series(conn, asset, entry_ts_ms,
                                entry_ts_ms + HORIZON_MS + 2 * ROUND_MS)


def shuffle_control(conn, entry: dict) -> dict | None:
    flipped = "up" if entry["direction_to_liquidation"] == "down" else "down"
    out = simulate_path(flipped, entry["d_bps"],
                        _entry_path(conn, entry["asset"], entry["entry_ts_ms"]),
                        spread_bps=entry["spread_bps"] or 0.0)
    if out is None:
        return None
    return {"control_type": "shuffle", "matched_asset": entry["asset"],
            "matched_ts_ms": entry["entry_ts_ms"], "direction_to_liquidation": flipped,
            "d_bps": entry["d_bps"], **out}


def trend_matched_control(conn, entry: dict, *, exclusion_ms: int = 3_600_000) -> dict | None:
    target = features_at(conn, entry["asset"], entry["entry_ts_ms"])
    if target is None:
        return None
    cand_ts = [r[0] for r in conn.execute(
        "SELECT signal_ts_ms FROM hl_far_whale_liq_candidates WHERE asset=?",
        (entry["asset"],)).fetchall()]
    pool_ts = [r[0] for r in conn.execute(
        "SELECT ts_ms FROM hl_far_whale_liq_market_state WHERE asset=? ORDER BY ts_ms",
        (entry["asset"],)).fetchall()]
    candidates = [t for t in pool_ts
                  if all(abs(t - c) >= exclusion_ms for c in cand_ts)]
    feats = [(t, features_at(conn, entry["asset"], t)) for t in candidates]
    feats = [(t, f) for t, f in feats if f is not None
             and _entry_path(conn, entry["asset"], t) and
             len(_entry_path(conn, entry["asset"], t)) >= 3]
    if not feats:
        return None
    # z-score each feature over the pool, nearest Euclidean neighbor
    stats = {}
    for k in FEATURE_KEYS:
        vals = [f[k] for _, f in feats]
        mu = sum(vals) / len(vals)
        sd = math.sqrt(sum((v - mu) ** 2 for v in vals) / max(len(vals) - 1, 1)) or 1.0
        stats[k] = (mu, sd)
    def dist(f):
        d = sum(((f[k] - target[k]) / stats[k][1]) ** 2 for k in FEATURE_KEYS)
        d += 0.25 * min(abs(f["hour_of_day"] - target["hour_of_day"]),
                        24 - abs(f["hour_of_day"] - target["hour_of_day"]))
        return d
    t_best, _ = min(feats, key=lambda tf: dist(tf[1]))
    out = simulate_path(entry["direction_to_liquidation"], entry["d_bps"],
                        _entry_path(conn, entry["asset"], t_best),
                        spread_bps=entry["spread_bps"] or 0.0)
    if out is None:
        return None
    return {"control_type": "trend", "matched_asset": entry["asset"],
            "matched_ts_ms": int(t_best),
            "direction_to_liquidation": entry["direction_to_liquidation"],
            "d_bps": entry["d_bps"], **out}


def generate_controls(conn) -> dict:
    done = {(r["entry_id"], r["control_type"]) for r in conn.execute(
        "SELECT entry_id, control_type FROM hl_far_whale_liq_controls")}
    entries = [dict(r) for r in conn.execute(
        "SELECT e.* FROM hl_far_whale_liq_paper_entries e"
        " JOIN hl_far_whale_liq_outcomes o ON o.entry_id=e.entry_id"
        " WHERE e.is_primary=1")]
    counts = {"shuffle": 0, "trend": 0, "unmatched": 0}
    for e in entries:
        for kind, fn in (("shuffle", shuffle_control), ("trend", trend_matched_control)):
            if (e["entry_id"], kind) in done:
                continue
            ctl = fn(conn, e)
            if ctl is None:
                counts["unmatched"] += 1
                continue
            conn.execute(
                "INSERT OR IGNORE INTO hl_far_whale_liq_controls"
                "(entry_id, control_type, matched_asset, matched_ts_ms,"
                " direction_to_liquidation, d_bps, paper_pnl, migrated_toward_liq, exit_reason)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (e["entry_id"], ctl["control_type"], ctl["matched_asset"],
                 ctl["matched_ts_ms"], ctl["direction_to_liquidation"], ctl["d_bps"],
                 ctl["paper_pnl"], ctl["migrated_toward_liq"], ctl["exit_reason"]))
            counts[kind] += 1
    conn.commit()
    return counts
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_far_whale_eval.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/hyperlive/far_whale_eval.py tests/test_far_whale_eval.py
git commit -m "feat(fwlm): control generator — shuffle-side + pre-treatment-blind trend match (§9 C2-C3)"
```

---

### Task 7: Verdict — dose-response, concentration, episode-clustered bootstrap, pass/decayed/kill

**Files:**
- Modify: `sniper/hyperlive/far_whale_eval.py` (append)
- Create: `scripts/run_far_whale_eval.py`
- Test: `tests/test_far_whale_eval.py` (append)

**Interfaces:**
- Consumes: `sniper.algos.polymarket_crypto5m.window_bootstrap.boot_means/pctile` (repo's shared clustered-bootstrap path — do NOT reimplement), Task 6 controls, `far_whale` constants.
- Produces:
  - `FWLM_BOOT_N = 10_000`, `FWLM_BOOT_SEED = 20260704`
  - `episode_ci(by_episode: dict[str, list[float]], *, nb=FWLM_BOOT_N, seed=FWLM_BOOT_SEED) -> tuple[float, float, float] | None` — `(mean, ci_lo_95, ci_hi_95)` from episode-resampled bootstrap
  - `dose_response(rows: list[dict]) -> dict` — migration rate by tercile of `position_notional / distance_bps`; `{"terciles": [...], "exists": bool}` (top tercile > bottom tercile)
  - `concentration(rows: list[dict]) -> dict` — `{"max_pnl_share": float, "max_candidate_share": float, "ok": bool}` (both ≤ `MAX_EVENT_SHARE`; pnl share over positive pnl only)
  - `evaluate(conn, *, now_ms: int) -> dict` — the §12/§13 verdict: counts (episodes/whales/assets), ITT CIs on pnl and migration deltas vs each control, dose-response, concentration, persistence-attrition diagnostic, campaign-cap check. `status ∈ {"no_freeze","insufficient","pass","decayed","kill","sparsity_killed"}` with `reasons: [...]`. **Control-3 failure forces kill regardless of Control 2** (no partial credit).
  - CLI `scripts/run_far_whale_eval.py [--db]` — runs `generate_controls` then `evaluate`, writes a `kind='verdict'` report row, prints the verdict JSON. **Operator-gated; no cron installs it.**

- [ ] **Step 1: Append the failing tests**

```python
# append to tests/test_far_whale_eval.py

def test_episode_ci_deterministic_and_clustered():
    by_ep = {f"e{i}": [10.0] for i in range(30)}
    a = E.episode_ci(by_ep)
    b = E.episode_ci(by_ep)
    assert a == b                       # fixed seed
    assert a[0] == 10.0 and a[1] <= 10.0 <= a[2]
    assert E.episode_ci({}) is None


def test_dose_response_orders_by_notional_over_distance():
    rows = ([{"position_notional": 5e6, "distance_bps": 60.0, "migrated_toward_liq": 1}] * 10
            + [{"position_notional": 1e6, "distance_bps": 150.0, "migrated_toward_liq": 1}] * 5
            + [{"position_notional": 1e6, "distance_bps": 150.0, "migrated_toward_liq": 0}] * 5
            + [{"position_notional": 2e5, "distance_bps": 290.0, "migrated_toward_liq": 0}] * 10)
    dr = E.dose_response(rows)
    assert dr["exists"] is True
    flat = E.dose_response([{"position_notional": 1e6, "distance_bps": 100.0,
                             "migrated_toward_liq": i % 2} for i in range(30)])
    assert flat["exists"] is False


def test_concentration_flags_single_whale_dominance():
    rows = [{"whale_address": "0xa", "paper_pnl": 90.0},
            {"whale_address": "0xb", "paper_pnl": 5.0},
            {"whale_address": "0xc", "paper_pnl": 5.0}]
    c = E.concentration(rows)
    assert c["max_pnl_share"] == 0.9 and c["ok"] is False


def test_evaluate_no_freeze_and_insufficient():
    conn = _conn()
    v = E.evaluate(conn, now_ms=1_000_000)
    assert v["status"] == "no_freeze"
    S.save_report(conn, kind="freeze",
                  payload={"thresholds": {"ETH": 1e6}, "campaign_start_ts_ms": 0},
                  ts_ms=0)
    v = E.evaluate(conn, now_ms=1_000_000)
    assert v["status"] == "insufficient"


def test_evaluate_sparsity_kill_after_campaign_cap():
    conn = _conn()
    S.save_report(conn, kind="freeze",
                  payload={"thresholds": {"ETH": 1e6}, "campaign_start_ts_ms": 0},
                  ts_ms=0)
    v = E.evaluate(conn, now_ms=61 * 86_400_000)     # 61 days, counts unmet
    assert v["status"] == "sparsity_killed"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_far_whale_eval.py -q`
Expected: new tests FAIL — functions missing.

- [ ] **Step 3: Append the implementation to `far_whale_eval.py`**

```python
# append to sniper/hyperlive/far_whale_eval.py
import random

from sniper.algos.polymarket_crypto5m.window_bootstrap import boot_means, pctile
from sniper.hyperlive.far_whale import CAMPAIGN_CAP_DAYS, MAX_EVENT_SHARE

FWLM_BOOT_N = 10_000
FWLM_BOOT_SEED = 20260704
PASS_MIN_EPISODES = 50       # §12 candidate episodes
PASS_MIN_OUTCOMES = 30       # §12 completed ITT primary outcomes
PASS_MIN_WHALES = 10
PASS_MIN_ASSETS = 3


def episode_ci(by_episode: dict, *, nb: int = FWLM_BOOT_N, seed: int = FWLM_BOOT_SEED):
    """Episode-clustered bootstrap 95% CI — resamples EPISODES, never rows (spec §12)."""
    means = boot_means(by_episode, random.Random(seed), nb)
    if not means:
        return None
    grand = sum(sum(v) for v in by_episode.values()) / sum(len(v) for v in by_episode.values())
    return grand, pctile(means, 0.025), pctile(means, 0.975)


def dose_response(rows: list) -> dict:
    """Control 1: migration rate by tercile of notional/distance. exists = top > bottom."""
    scored = sorted(rows, key=lambda r: r["position_notional"] / max(r["distance_bps"], 1e-9))
    n = len(scored)
    if n < 6:
        return {"terciles": [], "exists": False, "n": n}
    k = n // 3
    tiers = [scored[:k], scored[k:n - k], scored[n - k:]]
    rates = [round(sum(r["migrated_toward_liq"] for r in t) / len(t), 3) for t in tiers]
    return {"terciles": rates, "exists": rates[2] > rates[0], "n": n}


def concentration(rows: list) -> dict:
    """Control 5 on positive pnl AND candidate counts, per whale."""
    pos = [r for r in rows if (r.get("paper_pnl") or 0) > 0]
    total_pos = sum(r["paper_pnl"] for r in pos)
    by_whale_pnl: dict = {}
    by_whale_n: dict = {}
    for r in rows:
        w = r["whale_address"]
        by_whale_n[w] = by_whale_n.get(w, 0) + 1
        if (r.get("paper_pnl") or 0) > 0:
            by_whale_pnl[w] = by_whale_pnl.get(w, 0.0) + r["paper_pnl"]
    pnl_share = max((v / total_pos for v in by_whale_pnl.values()), default=0.0) \
        if total_pos > 0 else 0.0
    cand_share = max((v / len(rows) for v in by_whale_n.values()), default=0.0) if rows else 0.0
    return {"max_pnl_share": round(pnl_share, 3), "max_candidate_share": round(cand_share, 3),
            "ok": pnl_share <= MAX_EVENT_SHARE and cand_share <= MAX_EVENT_SHARE}


def _primary_rows(conn) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT e.entry_id, e.episode_id, e.asset, e.d_bps AS distance_bps,"
        " c.whale_address, c.position_notional,"
        " o.paper_pnl, o.migrated_toward_liq, o.position_persisted"
        " FROM hl_far_whale_liq_paper_entries e"
        " JOIN hl_far_whale_liq_outcomes o ON o.entry_id=e.entry_id"
        " JOIN hl_far_whale_liq_candidates c ON c.candidate_id=e.candidate_id"
        " WHERE e.is_primary=1")]


def evaluate(conn, *, now_ms: int) -> dict:
    freeze = S.load_freeze(conn)
    if freeze is None:
        return {"status": "no_freeze",
                "note": "no frozen thresholds — sweep (§4) not completed; no entries exist"}
    rows = _primary_rows(conn)                       # ITT primary set — NEVER persistence-filtered
    n_episodes_total = conn.execute(
        "SELECT COUNT(*) FROM hl_far_whale_liq_episodes").fetchone()[0]
    n_whales = len({r["whale_address"] for r in rows})
    n_assets = len({r["asset"] for r in rows})
    counts = {"candidate_episodes": n_episodes_total, "completed_outcomes": len(rows),
              "distinct_whales": n_whales, "assets": n_assets}
    days_elapsed = (now_ms - freeze["campaign_start_ts_ms"]) / 86_400_000.0
    bar_met = (n_episodes_total >= PASS_MIN_EPISODES and len(rows) >= PASS_MIN_OUTCOMES
               and n_whales >= PASS_MIN_WHALES and n_assets >= PASS_MIN_ASSETS)
    if not bar_met:
        if days_elapsed > CAMPAIGN_CAP_DAYS:
            return {"status": "sparsity_killed", "counts": counts,
                    "days_elapsed": round(days_elapsed, 1),
                    "note": f"§12 counts unmet after CAMPAIGN_CAP_DAYS={CAMPAIGN_CAP_DAYS};"
                            " no silent extension (spec §13)"}
        return {"status": "insufficient", "counts": counts,
                "days_elapsed": round(days_elapsed, 1)}

    ctl = {r["entry_id"]: {} for r in rows}
    for c in conn.execute("SELECT entry_id, control_type, paper_pnl, migrated_toward_liq"
                          " FROM hl_far_whale_liq_controls"):
        if c["entry_id"] in ctl:
            ctl[c["entry_id"]][c["control_type"]] = dict(c)

    pnl_by_ep: dict = {}
    d_shuffle: dict = {}
    d_trend: dict = {}
    for r in rows:
        pnl_by_ep.setdefault(r["episode_id"], []).append(r["paper_pnl"])
        cs, ct = ctl[r["entry_id"]].get("shuffle"), ctl[r["entry_id"]].get("trend")
        if cs:
            d_shuffle.setdefault(r["episode_id"], []).append(
                r["migrated_toward_liq"] - cs["migrated_toward_liq"])
        if ct:
            d_trend.setdefault(r["episode_id"], []).append(
                r["migrated_toward_liq"] - ct["migrated_toward_liq"])

    ci_pnl = episode_ci(pnl_by_ep)
    ci_shuffle = episode_ci(d_shuffle)
    ci_trend = episode_ci(d_trend)
    dr = dose_response(rows)
    conc = concentration(rows)
    attrition = conn.execute(
        "SELECT status, COUNT(*) FROM hl_far_whale_liq_episodes GROUP BY status").fetchall()
    persisted_rate = (sum(r["position_persisted"] or 0 for r in rows) / len(rows))

    reasons = []
    if ci_pnl is None or ci_pnl[1] <= 0:
        reasons.append("net_pnl_ci_not_positive")
    if ci_shuffle is None or ci_shuffle[1] <= 0:
        reasons.append("shuffle_delta_ci_not_positive")
    if ci_trend is None or ci_trend[1] <= 0:
        reasons.append("trend_delta_ci_not_positive")   # C3 fail kills regardless of C2 (§9)
    if not dr["exists"]:
        reasons.append("no_dose_response")
    if not conc["ok"]:
        reasons.append("concentration_breach")

    if not reasons:
        status = "pass"
    elif ci_pnl and ci_pnl[0] > 0 and ci_pnl[1] <= 0 <= ci_pnl[2] and \
            reasons in (["net_pnl_ci_not_positive"],):
        status = "decayed"      # point-positive but CI spans 0 — NOT a pass (§12)
    elif ci_pnl and ci_pnl[2] < 0:
        status = "kill"         # CI upper < 0 — hard kill (§13)
    else:
        status = "kill" if ("trend_delta_ci_not_positive" in reasons
                            or "no_dose_response" in reasons
                            or "concentration_breach" in reasons) else "decayed"

    return {"status": status, "reasons": reasons, "counts": counts,
            "days_elapsed": round(days_elapsed, 1),
            "ci_net_pnl": ci_pnl, "ci_delta_shuffle": ci_shuffle, "ci_delta_trend": ci_trend,
            "dose_response": dr, "concentration": conc,
            "persistence_diagnostic": {
                "itt_n": len(rows), "persisted_rate": round(persisted_rate, 3),
                "episode_attrition": {r[0]: r[1] for r in attrition},
                "note": "persistence-conditioned rate is a diagnostic UPPER BOUND, "
                        "not achievable live (§9 Control 4)"},
            "boot": {"n": FWLM_BOOT_N, "seed": FWLM_BOOT_SEED, "cluster": "episode"}}
```

```python
#!/usr/bin/env python3
# scripts/run_far_whale_eval.py
"""fwlm-v0 evaluator — operator-gated (spec §15 step 8: verdict only at the milestone).
Generates missing controls, computes the §12/§13 verdict, persists it as a report row."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.hyperlive import far_whale_eval as E    # noqa: E402
from sniper.hyperlive import far_whale_store as S   # noqa: E402

DEFAULT_DB = "/home/bigpapi/Desktop/Sniper/data/hyperlive_engine.sqlite"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="fwlm-v0 controls + verdict (operator-gated)")
    ap.add_argument("--db", default=DEFAULT_DB)
    args = ap.parse_args(argv)
    conn = S.connect(args.db)
    S.ensure_schema(conn)
    now_ms = int(time.time() * 1000)
    counts = E.generate_controls(conn)
    verdict = E.evaluate(conn, now_ms=now_ms)
    S.save_report(conn, kind="verdict", payload=verdict, ts_ms=now_ms)
    print(f"controls: {counts}")
    print(json.dumps(verdict, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_far_whale_eval.py tests/test_far_whale_sim.py tests/test_far_whale_store.py tests/test_far_whale_detector.py tests/test_run_far_whale_capture.py tests/test_far_whale_sweep.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/hyperlive/far_whale_eval.py scripts/run_far_whale_eval.py tests/test_far_whale_eval.py
git commit -m "feat(fwlm): verdict — episode-clustered bootstrap CIs, dose-response, concentration, sparsity cap"
```

---

### Task 8: UI lane — sparse-sample-labelled runtime surface (§11)

**Files:**
- Modify: `sniper/routes/api_hyperlive.py` (add `_runtime_far_whale_data`, a lane entry in `_runtime_payload`, a `RUNTIME_PROCESSES` entry, a `RUNTIME_CRONS` entry)
- Test: `tests/test_api_hyperlive.py` (append)

**Interfaces:**
- Consumes: `far_whale_store` read-only (`file:...?mode=ro` pattern used by `_runtime_cascade_data` at `sniper/routes/api_hyperlive.py:211`).
- Produces: `data.far_whale` block + a `far_whale_magnet` lane in `/api/hyperlive/runtime`. Lane `note` MUST contain the exact string `Sparse visible whale sample — NOT full liquidation density.` and MUST NOT contain "liquidation book" or "crowd cluster".

- [ ] **Step 1: Append the failing test**

```python
# append to tests/test_api_hyperlive.py (follow the file's existing client/fixture pattern
# — reuse its app/client construction; shown here with the blueprint factory directly)
def test_runtime_far_whale_lane_sparse_sample_labelling(tmp_path):
    from sniper.hyperlive import far_whale_store as S
    from sniper.routes.api_hyperlive import _runtime_far_whale_data, _runtime_payload
    db = str(tmp_path / "hl.sqlite")
    conn = S.connect(db)
    S.ensure_schema(conn)
    conn.close()
    fw = _runtime_far_whale_data(db)
    assert fw["status"] == "not_started"        # no freeze row yet
    payload = _runtime_payload(db)
    lane = next(l for l in payload["lanes"] if l["id"] == "far_whale_magnet")
    assert "Sparse visible whale sample — NOT full liquidation density." in lane["note"]
    assert "liquidation book" not in lane["note"].lower()
    assert "crowd cluster" not in lane["note"].lower()
    assert payload["data"]["far_whale"]["status"] == "not_started"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_api_hyperlive.py -q -k far_whale`
Expected: FAIL — `_runtime_far_whale_data` missing.

- [ ] **Step 3: Implement**

Add to `sniper/routes/api_hyperlive.py`:

1. `RUNTIME_PROCESSES` — append after the `cascade_audit` entry:
```python
    {
        "id": "far_whale_capture",
        "label": "Far-whale magnet capture",
        "match": "far_whale",
        "cmd_match": "run_far_whale_capture.py",
        "role": "fwlm-v0 paper study — sparse visible-whale liquidation magnet",
        "cadence": "180s",
    },
```
2. `RUNTIME_CRONS` — append:
```python
    {"id": "far_whale_watchdog", "label": "Far-whale watchdog",
     "needle": "far_whale_watchdog.sh", "cadence": "1m"},
```
3. Data helper (next to `_runtime_cascade_data`):
```python
def _runtime_far_whale_data(db_path: str) -> dict:
    out = {"exists": os.path.exists(db_path), "episodes": 0, "candidates": 0,
           "outcomes": 0, "status": "not_started", "verdict": None}
    if not out["exists"]:
        return out
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            out["episodes"] = _table_count(conn, "hl_far_whale_liq_episodes")
            out["candidates"] = _table_count(conn, "hl_far_whale_liq_candidates")
            out["outcomes"] = _table_count(conn, "hl_far_whale_liq_outcomes")
            row = conn.execute(
                "SELECT payload_json FROM hl_far_whale_liq_reports WHERE kind='verdict'"
                " ORDER BY ts_ms DESC, id DESC LIMIT 1").fetchone()
            frozen = conn.execute(
                "SELECT COUNT(*) FROM hl_far_whale_liq_reports WHERE kind='freeze'"
            ).fetchone()[0]
            if row:
                out["verdict"] = json.loads(row["payload_json"])
                out["status"] = out["verdict"].get("status", "accruing")
            elif out["candidates"]:
                out["status"] = "accruing"
            elif frozen:
                out["status"] = "frozen_awaiting_candidates"
            elif out["episodes"] or _table_count(conn, "hl_far_whale_liq_position_snapshots"):
                out["status"] = "sweep_mode"
        finally:
            conn.close()
    except sqlite3.Error:
        out["status"] = "not_started"      # tables absent -> lane dormant
    return out
```
4. In `_runtime_payload`, after `cascade = _runtime_cascade_data(db_path)` add `far_whale = _runtime_far_whale_data(db_path)`; include `"far_whale": far_whale` in the returned `data` dict; and insert this lane after the `cascade_detector` lane:
```python
        {
            "id": "far_whale_magnet",
            "label": "Far-whale liquidation magnet (paper)",
            "status": ("live" if proc_by_id["far_whale_capture"]["state"] == "running"
                       else "built_idle"),
            "runtime": proc_by_id["far_whale_capture"]["state"],
            "data": (f"{far_whale['episodes']} episodes · {far_whale['candidates']} candidates"
                     f" · {far_whale['outcomes']} outcomes · {far_whale['status']}"),
            "note": ("Sparse visible whale sample — NOT full liquidation density. "
                     "fwlm-v0 pre-registered paper study: does a single visible far whale "
                     "liquidation level (50–300bps) pull price? Verdict only at the §12 "
                     "milestone via episode-clustered bootstrap; ITT primary."),
        },
```
(Note: `_table_count` and `json` already exist in the module.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_api_hyperlive.py -q`
Expected: all PASS (new test + no regressions in the existing runtime tests — the lane/process additions change counts; update any existing assertions on lane counts if they are exact).

- [ ] **Step 5: Commit**

```bash
git add sniper/routes/api_hyperlive.py tests/test_api_hyperlive.py
git commit -m "feat(fwlm): runtime lane — sparse-sample-labelled far-whale magnet surface (§11)"
```

---

### Task 9: Watchdog script + final verification (campaign NOT started)

**Files:**
- Create: `scripts/far_whale_watchdog.sh`
- Modify (if stale): `index/schema_registry.json`, `index/dependency_index.json`

**Interfaces:**
- Consumes: the watchdog pattern from `scripts/cascade_capture_watchdog.sh` (PID-file + pgrep reattach + setsid relaunch).
- Produces: an installable-but-NOT-installed watchdog. **Do not add any crontab entry and do not launch capture — spec §15 step 8 is operator-gated.**

- [ ] **Step 1: Write the watchdog**

```bash
#!/usr/bin/env bash
# scripts/far_whale_watchdog.sh
# fwlm-v0 far-whale magnet capture watchdog. Paper-only, public data, no keys/orders.
# NOT installed by default — the forward campaign is operator-gated (spec §15 step 8).
# To start the campaign: crontab entry `* * * * * /home/bigpapi/Desktop/Sniper/scripts/far_whale_watchdog.sh`
set -u

cd /home/bigpapi/Desktop/Sniper || exit 1

PY=/home/bigpapi/.pyenv/versions/3.13.0/bin/python3
DB=/home/bigpapi/Desktop/Sniper/data/hyperlive_engine.sqlite
PIDFILE=/home/bigpapi/Desktop/Sniper/data/far_whale_capture.pid
LOGFILE=logs/far_whale_capture.log
WATCHDOG_LOG=logs/far_whale_watchdog.log
MATCH="run_far_whale_capture.py --db $DB"
LAUNCH="$PY -u scripts/run_far_whale_capture.py --db $DB"

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >> "$WATCHDOG_LOG"; }

pid_matches_runner() {
  local pid="${1:-}" cmd first
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  cmd=$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null || true)
  first=${cmd%% *}
  case "$(basename "$first")" in python|python3) ;; *) return 1 ;; esac
  printf '%s' "$cmd" | grep -Fq "$MATCH"
}

find_existing() {
  local pid
  pgrep -f "$MATCH" | while read -r pid; do
    [ "$pid" != "$$" ] || continue
    pid_matches_runner "$pid" && { echo "$pid"; break; }
  done
}

mkdir -p logs

pid=$(cat "$PIDFILE" 2>/dev/null || true)
if pid_matches_runner "$pid"; then exit 0; fi

existing=$(find_existing || true)
if [ -n "${existing:-}" ]; then echo "$existing" > "$PIDFILE"; log "reattached pid=$existing"; exit 0; fi

setsid nohup $LAUNCH >>"$LOGFILE" 2>&1 </dev/null &
sleep 5
real=$(find_existing || true)
echo "${real:-}" > "$PIDFILE"
if [ -n "${real:-}" ]; then log "RESTARTED pid=$real"; exit 0; fi
log "FAILED_TO_START"
exit 1
```

Run: `chmod +x scripts/far_whale_watchdog.sh && bash -n scripts/far_whale_watchdog.sh`
Expected: syntax-clean, no output.

- [ ] **Step 2: Full focused verification**

```bash
python3 -m pytest tests/test_far_whale_detector.py tests/test_far_whale_store.py \
  tests/test_far_whale_sim.py tests/test_run_far_whale_capture.py \
  tests/test_far_whale_sweep.py tests/test_far_whale_eval.py tests/test_api_hyperlive.py -q
python3 scripts/brain_index.py generate
git diff --stat index/          # if non-empty, stage the regenerated index
python3 scripts/validate_schema_refs.py
```
Expected: all tests pass; index fresh; validator clean.

- [ ] **Step 3: Verify no capture/cron was started**

```bash
pgrep -f run_far_whale_capture.py || echo "OK: capture not running"
crontab -l | grep far_whale || echo "OK: no cron installed"
```
Expected: both `OK` lines — the campaign start is a separate operator decision.

- [ ] **Step 4: Commit**

```bash
git add scripts/far_whale_watchdog.sh index/
git commit -m "feat(fwlm): watchdog script (not installed) — campaign start stays operator-gated"
```

---

## Post-build operating sequence (spec §15 — for the operator, not this plan)

1. Operator installs the watchdog cron → capture runs in **sweep mode** (snapshots + market-state only; no candidates, no entries).
2. After ~5–7 days: `python3 scripts/far_whale_threshold_sweep.py` → review the episode-units/day table → `--freeze "BTC=...,ETH=...,SOL=..."` (one shot; re-freeze refused).
3. Forward campaign accrues; `python3 scripts/run_far_whale_eval.py` only at the §12 milestone (or the 60-day cap).
4. Verdict lands in `hl_far_whale_liq_reports` + the UI lane; knowledge-sync per KSG at branch close.

## Self-Review (done at write time)

- **Spec coverage:** §2 semantics → Task 1; §3/§3.1 gate+episodes → Tasks 1–2; §4 sweep/freeze → Task 5 (+ fail-closed capture in Task 4); §5 entry → Tasks 3–4; §6/§7 horizons+exits → Task 3 (`DIAG_HORIZONS_MS` available to `simulate_path` for the eval's diagnostic readout); §8 labels → Task 3; §9 Controls 1–5 → Tasks 4 (C4), 6 (C2–C3), 7 (C1, C5); §10 tables+wall-clock → Task 2/4; §11 UI → Task 8; §12/§13 verdict+cap → Task 7; §15 gating → Task 9 (no cron, no start); §16 frozen constants → Task 1.
- **Known simplifications (stated, not hidden):** touch detection is poll-granular (~180s) — reported as a §14 limitation; `liq_fired` is a documented approximation; spread from `impactPxs` is an impact-spread proxy, fail-closed when absent; staleness is enforced by skipping candidates on ctx-fetch failure (fresh-fetch-per-round design) rather than a stored age field.
- **Type consistency:** candidate dict shape fixed in Task 1 and consumed verbatim by Tasks 2–4/6; entry dict shape fixed in Task 3 (`plan_entry`) and matches `far_whale_store.insert_entry` columns; outcome dict shape fixed in Task 3 (`settle`) and matches `settle_entry`.
