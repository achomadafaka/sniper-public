# Liquidation Cascade Detector + Exploitability Test — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a forward-only Hyperliquid cascade detector and a pre-registered exploitability audit that answers the strategic fork — is the *aftermath* of a detected cascade tradeable net of the 9 bps taker fee, or do we pivot to the copy/info system?

**Architecture:** A dumb capture loop writes raw per-bucket observations (mid, OI, trade count, traded notional) from the HL trades WS + asset-context OI into `hl_cascade_obs`. A pure detector (`sniper/hyperlive/cascade.py`) derives windowed features and emits version-stamped `hl_cascade_events`. A pure stats layer (clustered-t, BH-FDR, negative-control transforms) plus an audit script grid the forward aftermath over magnitude × asset-class × horizon × {continuation, reversion}, apply the canonical gate, and write a verdict the UI surfaces as accruing / edge / no-edge.

**Tech Stack:** Python 3, SQLite (WAL, via `sniper/hyperlive/persistence.py`), `websocket-client`, Flask blueprint (`sniper/routes/api_hyperlive.py`), vanilla JS (`static/app.js`), pytest.

**Spec:** `docs/superpowers/specs/2026-06-25-liquidation-cascade-detector-design.md`

## Global Constraints

Copied verbatim from the spec; every task's requirements implicitly include these.

- **Fee, applied once on the mean:** after-fee = mean − **9.0 bps**. Use `models.ROUND_TRIP_FEE_BPS` (== `2 * 10000 * models.TAKER_FEE`, `TAKER_FEE = 0.00045`). Never subtract the fee per-horizon or per-event — once, on the mean aligned move in the gate.
- **Pre-registered FROZEN detector constants** (change ANY of these → bump `DETECTOR_VERSION`): `W` event window = **60s**; σ lookback = **30 min** trailing realized vol with a min-obs gate; `k` (move threshold) = **3.0** (`|ret_w| ≥ k·σ`); absolute floor = **30 bps** (BOTH `k·σ` AND the floor must be exceeded); `d` (OI drop) = **1.0%** of prior OI; `m` (trade spike) = **3.0×** median windowed trade count; cooldown `C` = **300s** per asset.
- **OI fail-closed:** if OI is missing or stale for an asset at event time, **skip the event** — never assume OI dropped.
- **`clearinghouseState` is reserved for Thesis-1 whale-state sampling ONLY** — never summed across sampled addresses for market-wide OI. Market OI comes only from asset context (`metaAndAssetCtxs` `openInterest`).
- **Canonical exploitability gate (single source of truth, used by detector audit AND decision fork).** An (hypothesis × cell) is an EDGE iff ALL hold: after-fee = mean − 9.0 bps > 0; clustered-t (cluster by **asset × hour**) > 2.0; n ≥ min_n (start **40**); survives **Benjamini-Hochberg FDR** across the whole grid (q = **0.10**); positive in **≥ 2 of 3** chronological thirds; passes the **negative-control battery**. Continuation and reversion are reported separately (mutually exclusive predictions).
- **Negative-control battery (an edge must survive ALL):** direction-flip, asset-shuffle, matched-non-event (same asset, same time-of-day bucket, no cascade), event-time-shift. Each must produce a NULL when the real edge is present.
- **Forward-only — no backfill.** Pre-register the accrual window before reading any verdict.
- **Read-only / selection labels until the gate clears; paper-only after. No live orders in scope.**
- **Storage:** all new tables live in `sniper/hyperlive/persistence.py` (the engine-DB schema owner) and land in the engine DB `data/hyperlive_engine.sqlite`, alongside `hyperlive_cluster_history`.
- **WS:** `websocket-client` (`import websocket`), endpoint `wss://api.hyperliquid.xyz/ws`, subscribe `{"method": "subscribe", "subscription": {"type": "trades", "coin": <ASSET>}}`; trade messages arrive as `msg["channel"] == "trades"`, `msg["data"]` = list of trade records each with `px`, `sz`, `users`.

### Plan-author decisions (NOT in the spec — flagged for the human before execution)

These were under-specified in the spec; the plan pins them. If any is wrong, correct before Task 1.

1. **obs holds RAW per-bucket measurements; the detector derives `ret_w`/`sigma_w`/`oi_delta_w`.** The spec listed those derived columns on `hl_cascade_obs`; the plan stores them on `hl_cascade_events` instead and keeps `hl_cascade_obs` raw (`mid`, `oi`, `n_trades`, `notional`). Rationale: a dumb capture loop + a *pure* detector is far more testable than stateful vol-at-capture, and the spec's event row already lists the derived columns.
2. **Bucket cadence = 10s.** The spec fixed `W = 60s` but left the obs sample rate open. 10s → 6 buckets per window, 180 buckets per 30-min σ lookback.
3. **`magnitude_bucket` bins on `|ret_w|` bps:** `"30-60"`, `"60-120"`, `"120-250"`, `"250+"`.
4. **`asset_class`:** `BTC`/`ETH`/`SOL` → `"major"`, everything else → `"alt"`.
5. **Cascade data lives in the engine DB** (`hyperlive_engine.sqlite`); the cascade Flask route reuses the same `db_path`/`_conn` as the existing `state`/`history` routes (which already read that DB).

### Mid-build review fixes (applied 2026-06-25)

6. **Detector is strictly causal.** σ is computed from returns ending at `base = i − w` (the bucket before the event window) — it EXCLUDES the move window, so a cascade cannot inflate its own σ and there is no look-ahead. The trade-spike baseline median is taken over PRIOR windows only (ending at/before `base`), never the whole series. Events can only fire once enough prior history exists.
7. **Event timestamp = the FIRST bucket where all conditions clear** (real-time/causal). Tests assert this with step-and-hold fixtures (fires at the step bucket); cooldown collapses the held window to a single event.
8. **`hl_cascade_obs` is idempotent** on `UNIQUE(asset, ts_ms)` + `INSERT OR IGNORE` — a capture/watchdog restart re-flushing a bucket cannot double-count.
9. **Thin/empty controls never pass.** A control must have `>= max(20, n/2)` rows to be EVALUABLE; otherwise the cell can't be confirmed and the run is `accruing`, not `edge` — an empty control is not a free null.
10. **Task 9 (`cluster_map`) is research code only.** Do NOT add `run_cluster_map.py` (or the cascade capture/audit) to any watchdog/cron without an explicit, separate operator step. The whale-weight + multi-source harvest changes are unit-tested but not wired to run operationally by this plan.

### Adversarial-review round 2 (applied 2026-06-25, commit 3f40873)

11. **Detector never crashes on a `None` mid.** A transient `metaAndAssetCtxs` failure used to emit `None` mids; `detect_cascades` now treats `None`/0 mids as a 0 return and skips candidate buckets with a `None` mid (regression test `test_detect_cascades_tolerates_none_mid`), and `run_cascade_capture.py` skips writing `None`-mid rows so the obs series is never poisoned.
12. **No forward look-ahead in measurement.** `_nearest_mid(..., after=True)` for the forward point only accepts an obs AT OR AFTER the horizon (never a pre-horizon price); the entry point stays symmetric. Used by `aligned_moves` and the shuffle/shift/matched controls.
13. **The controls-null certification test is no longer vacuous.** It runs on a dense, time-aligned multi-asset grid and asserts a row-floor (`>= 0.6·n`) for every control, so an empty control can never pass via `clustered_t([])==0`.
14. **Matched control excludes ALL detected cascades.** `control_matched(exclude_events=all_events)` so another cell's cascade can't be sampled into the "non-event" pool.

---

## File Structure

| File | Create/Modify | Responsibility |
|---|---|---|
| `sniper/hyperlive/persistence.py` | Modify | Add `hl_cascade_obs`, `hl_cascade_events`, `hl_cascade_audit_runs` DDL + `save_cascade_obs`, `read_cascade_obs`, `save_cascade_event`, `read_cascade_events`, `save_cascade_audit`, `read_latest_cascade_audit` |
| `sniper/hyperlive/cascade.py` | Create | Pure detector + measurement + stats: `DetectorParams`, `detect_cascades`, `aligned_moves`, the four controls, `clustered_t`, `bh_fdr`, `t_to_p`, `magnitude_bucket`, `asset_class`, `thirds_positive`, `cell_passes_gate` |
| `scripts/run_cascade_capture.py` | Create | Trades-WS + asset-ctx OI capture loop → `hl_cascade_obs`; pure `aggregate_buckets` helper |
| `scripts/run_cascade_exploit_audit.py` | Create | Read obs → detect events → grid forward aftermath + controls + FDR + canonical gate → write verdict |
| `sniper/hyperlive/cluster_map.py` | Modify | Thesis-1 magnet fix: whale-weighted cluster (`cluster_signal` `whale_weight`) |
| `scripts/run_cluster_map.py` | Modify | Thesis-1 multi-source de-duplicated address harvest |
| `sniper/routes/api_hyperlive.py` | Modify | `GET /api/hyperlive/cascade` — latest verdict + obs/event counts |
| `static/app.js` | Modify | Cascade forward-test status panel under the hyperlive tab |
| `templates/index.html` | Modify | Cache bump `v=144` → `v=145` + panel container |
| `tests/test_hyperlive_cascade.py` | Create | Detector, measurement, controls, stats, gate |
| `tests/test_hyperlive_cascade_persistence.py` | Create | Schema round-trips + migration idempotency |
| `tests/test_run_cascade_capture.py` | Create | `aggregate_buckets` |
| `tests/test_api_hyperlive.py` | Modify | `/api/hyperlive/cascade` route |

---

## Task 1: Cascade persistence schema

**Files:**
- Modify: `sniper/hyperlive/persistence.py`
- Test: `tests/test_hyperlive_cascade_persistence.py`

**Interfaces:**
- Consumes: existing `connect(path)`, `ensure_schema(conn)` in `persistence.py`.
- Produces: `save_cascade_obs(conn, rows, ts_ms)`, `read_cascade_obs(conn, asset=None, since_ms=None) -> list[dict]`, `save_cascade_event(conn, event)`, `read_cascade_events(conn, detector_version=None) -> list[dict]`, `save_cascade_audit(conn, verdict, ts_ms)`, `read_latest_cascade_audit(conn) -> dict | None`. `rows` is `{asset: (mid, oi, n_trades, notional)}`. An `event` is the dict produced by `cascade.detect_cascades`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_hyperlive_cascade_persistence.py
import json, os
from sniper.hyperlive import persistence as P


def _conn(tmp_path):
    c = P.connect(os.path.join(str(tmp_path), "engine.sqlite"))
    P.ensure_schema(c)
    return c


def test_obs_roundtrip_ascending(tmp_path):
    c = _conn(tmp_path)
    P.save_cascade_obs(c, {"BTC": (100.0, 5e8, 12, 250000.0)}, ts_ms=1000)
    P.save_cascade_obs(c, {"BTC": (101.0, 4.9e8, 30, 900000.0),
                           "ETH": (50.0, 2e8, 5, 40000.0)}, ts_ms=2000)
    btc = P.read_cascade_obs(c, asset="BTC")
    assert [r["ts_ms"] for r in btc] == [1000, 2000]
    assert btc[1]["mid"] == 101.0 and btc[1]["n_trades"] == 30
    assert len(P.read_cascade_obs(c)) == 3
    assert len(P.read_cascade_obs(c, since_ms=1500)) == 2


def test_event_roundtrip_and_version_filter(tmp_path):
    c = _conn(tmp_path)
    ev = {"event_id": "BTC:2000:v1", "asset": "BTC", "ts_ms": 2000, "direction": 1,
          "ret_w": 80.0, "sigma_w": 12.0, "oi_delta_w": -2.0, "trade_count_w": 40,
          "traded_notional_w": 9e5, "magnitude_bucket": "60-120", "detector_version": "v1"}
    P.save_cascade_event(c, ev)
    P.save_cascade_event(c, ev)  # same event_id -> idempotent, still one row
    rows = P.read_cascade_events(c)
    assert len(rows) == 1 and rows[0]["direction"] == 1 and rows[0]["magnitude_bucket"] == "60-120"
    assert P.read_cascade_events(c, detector_version="v1") == rows
    assert P.read_cascade_events(c, detector_version="v2") == []


def test_audit_latest(tmp_path):
    c = _conn(tmp_path)
    assert P.read_latest_cascade_audit(c) is None
    P.save_cascade_audit(c, {"status": "accruing", "n_events": 3}, ts_ms=1000)
    P.save_cascade_audit(c, {"status": "no-edge", "n_events": 60}, ts_ms=2000)
    latest = P.read_latest_cascade_audit(c)
    assert latest["ts_ms"] == 2000 and latest["verdict"]["status"] == "no-edge"


def test_migration_idempotent(tmp_path):
    c = _conn(tmp_path)
    P.ensure_schema(c)  # second call must not raise
    P.save_cascade_obs(c, {"BTC": (100.0, 5e8, 1, 1.0)}, ts_ms=1)
    assert len(P.read_cascade_obs(c)) == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `cd /home/bigpapi/Desktop/Sniper-leadlag && python3 -m pytest tests/test_hyperlive_cascade_persistence.py -q`
Expected: FAIL — `AttributeError: module 'sniper.hyperlive.persistence' has no attribute 'save_cascade_obs'`.

- [ ] **Step 3: Add DDL + functions**

In `ensure_schema(conn)`, after the existing `CREATE TABLE IF NOT EXISTS hyperlive_cluster_history ...` / index block, add:

```python
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS hl_cascade_obs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asset TEXT NOT NULL, ts_ms INTEGER NOT NULL,
        mid REAL, oi REAL, n_trades INTEGER, notional REAL,
        UNIQUE(asset, ts_ms)              -- idempotent: a re-flushed bucket on restart is ignored
    );
    CREATE INDEX IF NOT EXISTS idx_cascade_obs_asset_ts ON hl_cascade_obs(asset, ts_ms);

    CREATE TABLE IF NOT EXISTS hl_cascade_events (
        event_id TEXT PRIMARY KEY,
        asset TEXT NOT NULL, ts_ms INTEGER NOT NULL, direction INTEGER,
        ret_w REAL, sigma_w REAL, oi_delta_w REAL,
        trade_count_w INTEGER, traded_notional_w REAL,
        magnitude_bucket TEXT, detector_version TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_cascade_events_ver_ts ON hl_cascade_events(detector_version, ts_ms);

    CREATE TABLE IF NOT EXISTS hl_cascade_audit_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_ms INTEGER NOT NULL, verdict_json TEXT NOT NULL
    );
    """)
    conn.commit()
```

Add the module-level functions (near `save_cluster_history`):

```python
def save_cascade_obs(conn, rows, ts_ms):
    """rows: {asset: (mid, oi, n_trades, notional)}. Idempotent on (asset, ts_ms) so a
    capture/watchdog restart can't double-count a bucket."""
    conn.executemany(
        "INSERT OR IGNORE INTO hl_cascade_obs(asset, ts_ms, mid, oi, n_trades, notional) "
        "VALUES (?,?,?,?,?,?)",
        [(a, ts_ms, v[0], v[1], v[2], v[3]) for a, v in rows.items()])
    conn.commit()


def read_cascade_obs(conn, asset=None, since_ms=None):
    q = "SELECT asset, ts_ms, mid, oi, n_trades, notional FROM hl_cascade_obs"
    cond, args = [], []
    if asset is not None:
        cond.append("asset=?"); args.append(asset)
    if since_ms is not None:
        cond.append("ts_ms>=?"); args.append(since_ms)
    if cond:
        q += " WHERE " + " AND ".join(cond)
    q += " ORDER BY ts_ms ASC, id ASC"
    cur = conn.execute(q, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def save_cascade_event(conn, event):
    conn.execute(
        """INSERT OR IGNORE INTO hl_cascade_events
           (event_id, asset, ts_ms, direction, ret_w, sigma_w, oi_delta_w,
            trade_count_w, traded_notional_w, magnitude_bucket, detector_version)
           VALUES (:event_id,:asset,:ts_ms,:direction,:ret_w,:sigma_w,:oi_delta_w,
                   :trade_count_w,:traded_notional_w,:magnitude_bucket,:detector_version)""",
        event)
    conn.commit()


def read_cascade_events(conn, detector_version=None):
    q = ("SELECT event_id, asset, ts_ms, direction, ret_w, sigma_w, oi_delta_w, "
         "trade_count_w, traded_notional_w, magnitude_bucket, detector_version FROM hl_cascade_events")
    args = []
    if detector_version is not None:
        q += " WHERE detector_version=?"; args.append(detector_version)
    q += " ORDER BY ts_ms ASC"
    cur = conn.execute(q, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def save_cascade_audit(conn, verdict, ts_ms):
    import json as _json
    conn.execute("INSERT INTO hl_cascade_audit_runs(ts_ms, verdict_json) VALUES (?,?)",
                 (ts_ms, _json.dumps(verdict)))
    conn.commit()


def read_latest_cascade_audit(conn):
    import json as _json
    cur = conn.execute("SELECT ts_ms, verdict_json FROM hl_cascade_audit_runs ORDER BY ts_ms DESC LIMIT 1")
    row = cur.fetchone()
    if not row:
        return None
    return {"ts_ms": row[0], "verdict": _json.loads(row[1])}
```

- [ ] **Step 4: Run to verify pass**

Run: `cd /home/bigpapi/Desktop/Sniper-leadlag && python3 -m pytest tests/test_hyperlive_cascade_persistence.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add sniper/hyperlive/persistence.py tests/test_hyperlive_cascade_persistence.py
git commit -m "feat(hl-cascade): obs/events/audit schema + persistence round-trips"
```

---

## Task 2: Detector parameters + `detect_cascades`

**Files:**
- Create: `sniper/hyperlive/cascade.py`
- Test: `tests/test_hyperlive_cascade.py`

**Interfaces:**
- Produces: `DETECTOR_VERSION` (str), `DetectorParams` (frozen dataclass with the pre-registered constants), `magnitude_bucket(ret_bps) -> str`, `asset_class(coin) -> str`, `detect_cascades(series, params=DetectorParams()) -> list[dict]`. `series` is a list of obs dicts (`asset`, `ts_ms`, `mid`, `oi`, `n_trades`, `notional`) ascending in `ts_ms` for ONE asset. Each event dict matches the `save_cascade_event` shape.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_hyperlive_cascade.py
from sniper.hyperlive import cascade as C


def _flat_series(asset, n, *, start_ts=0, bucket_ms=10_000, mid=100.0, oi=1e9, n_tr=10, notl=1e5):
    """A calm baseline: tiny ±2bps oscillation, steady OI, steady volume."""
    out = []
    for i in range(n):
        m = mid * (1 + (0.0002 if i % 2 else -0.0002))
        out.append({"asset": asset, "ts_ms": start_ts + i * bucket_ms,
                    "mid": m, "oi": oi, "n_trades": n_tr, "notional": notl})
    return out


def test_magnitude_bucket_and_asset_class():
    assert C.magnitude_bucket(45) == "30-60"
    assert C.magnitude_bucket(90) == "60-120"
    assert C.magnitude_bucket(200) == "120-250"
    assert C.magnitude_bucket(900) == "250+"
    assert C.asset_class("BTC") == "major" and C.asset_class("ETH") == "major"
    assert C.asset_class("WIF") == "alt"


def test_planted_cascade_fires_one_event_correct_direction():
    s = _flat_series("BTC", 250)                      # 250 buckets of calm
    # plant a down-cascade at i=200: -150bps over the 60s window, OI -2%, 5x volume
    base = s[194]["mid"]
    for j, i in enumerate(range(195, 201)):
        s[i]["mid"] = base * (1 - 0.0025 * (j + 1))   # ~ -150bps across 6 buckets
        s[i]["oi"] = 1e9 * (1 - 0.004 * (j + 1))      # OI dropping
        s[i]["n_trades"] = 60                          # spike
        s[i]["notional"] = 6e5
    evs = C.detect_cascades(s)
    assert len(evs) == 1
    e = evs[0]
    assert e["asset"] == "BTC" and e["direction"] == -1
    assert e["ts_ms"] == s[200]["ts_ms"]
    assert e["ret_w"] < -100 and e["oi_delta_w"] < 0
    assert e["detector_version"] == C.DETECTOR_VERSION
    assert e["event_id"] == f"BTC:{s[200]['ts_ms']}:{C.DETECTOR_VERSION}"


def test_slow_drift_fires_none():
    s = _flat_series("ETH", 300)
    for i in range(300):                               # +200bps over the WHOLE series = slow, sub-k drift
        s[i]["mid"] = 100.0 * (1 + 0.00007 * i)
    assert C.detect_cascades(s) == []


def test_min_move_floor_suppresses_low_sigma_trigger():
    # Dead-calm σ≈0, then a 20bps blip (< 30bps floor) with OI drop + volume spike.
    s = _flat_series("SOL", 250, n_tr=10)
    for i in range(250):
        s[i]["mid"] = 100.0                            # perfectly flat -> σ ~ 0
    base = s[194]["mid"]
    for j, i in enumerate(range(195, 201)):
        s[i]["mid"] = base * (1 - 0.0002 * (j + 1) / 6)   # ~ -20bps total, under the floor
        s[i]["oi"] = 1e9 * (1 - 0.004 * (j + 1))
        s[i]["n_trades"] = 60
    assert C.detect_cascades(s) == []


def test_cooldown_suppresses_second_event_in_window():
    s = _flat_series("BTC", 300)
    def plant(start, sign):
        base = s[start - 1]["mid"]
        for j, i in enumerate(range(start, start + 6)):
            s[i]["mid"] = base * (1 + sign * 0.0025 * (j + 1))
            s[i]["oi"] = 1e9 * (1 - 0.004 * (j + 1))
            s[i]["n_trades"] = 60
    plant(200, -1)
    plant(210, -1)        # only 100s later (< 300s cooldown) -> must be suppressed
    evs = C.detect_cascades(s)
    assert len(evs) == 1 and evs[0]["ts_ms"] == s[200]["ts_ms"]


def test_oi_missing_skips_event_fail_closed():
    s = _flat_series("BTC", 250)
    base = s[194]["mid"]
    for j, i in enumerate(range(195, 201)):
        s[i]["mid"] = base * (1 - 0.0025 * (j + 1))
        s[i]["oi"] = None                              # OI unavailable
        s[i]["n_trades"] = 60
    assert C.detect_cascades(s) == []
```

- [ ] **Step 2: Run to verify failure**

Run: `cd /home/bigpapi/Desktop/Sniper-leadlag && python3 -m pytest tests/test_hyperlive_cascade.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'sniper.hyperlive.cascade'`.

- [ ] **Step 3: Create `sniper/hyperlive/cascade.py`**

```python
"""Pure Hyperliquid cascade detector + exploitability primitives. No I/O.

Thesis 2 (primary): detect a realized cascade from its public footprint (fast adverse move +
OI drop + trade-count spike) and test whether the AFTERMATH is tradeable net of the 9 bps taker
fee. All detector constants are pre-registered and frozen; any change must bump DETECTOR_VERSION.
See docs/superpowers/specs/2026-06-25-liquidation-cascade-detector-design.md."""
from __future__ import annotations

import math
from dataclasses import dataclass

from .models import ROUND_TRIP_FEE_BPS

DETECTOR_VERSION = "v1"

_MAJORS = {"BTC", "ETH", "SOL"}


@dataclass(frozen=True)
class DetectorParams:
    W_sec: int = 60
    bucket_sec: int = 10
    k: float = 3.0
    min_move_bps: float = 30.0
    d_oi_frac: float = 0.01          # OI must drop >= 1% over the window
    m_trade: float = 3.0             # trade_count_w >= m * median(trade_count_w)
    sigma_lookback_sec: int = 1800   # 30 min
    min_sigma_obs: int = 30
    cooldown_sec: int = 300
    version: str = DETECTOR_VERSION


def magnitude_bucket(ret_bps: float) -> str:
    a = abs(ret_bps)
    if a < 60:
        return "30-60"
    if a < 120:
        return "60-120"
    if a < 250:
        return "120-250"
    return "250+"


def asset_class(coin: str) -> str:
    return "major" if (coin or "").upper() in _MAJORS else "alt"


def detect_cascades(series, params: DetectorParams = DetectorParams()):
    """series: obs dicts for ONE asset ascending in ts_ms. Returns version-stamped event dicts."""
    n = len(series)
    if n < 2:
        return []
    asset = series[0]["asset"]
    w = max(1, round(params.W_sec / params.bucket_sec))
    cooldown_ms = params.cooldown_sec * 1000

    mids = [r.get("mid") for r in series]
    # per-bucket simple returns (for σ)
    rets = [0.0]
    for i in range(1, n):
        p = mids[i - 1]
        rets.append((mids[i] - p) / p if p else 0.0)

    # current-window trade counts; the spike baseline uses PRIOR windows only (causal — no
    # look-ahead into the event/future, per review).
    tcw = [None] * n
    for i in range(n):
        if i >= w:
            tcw[i] = sum(series[j]["n_trades"] or 0 for j in range(i - w + 1, i + 1))

    lookback = round(params.sigma_lookback_sec / params.bucket_sec)
    events = []
    last_ts = None
    for i in range(w, n):
        base = i - w                         # last bucket BEFORE the event window (causal cutoff)
        # prior-only σ: vol of returns ending at `base`; EXCLUDES the move window so the move
        # cannot inflate its own σ and there is no look-ahead.
        lo = max(1, base - lookback + 1)
        wr = rets[lo:base + 1]
        if len(wr) < params.min_sigma_obs:
            continue
        mu = sum(wr) / len(wr)
        var = sum((x - mu) ** 2 for x in wr) / (len(wr) - 1)
        sigma = math.sqrt(var) if var > 0 else 0.0

        mid_now, mid_prev = mids[i], mids[i - w]
        if not mid_prev:
            continue
        ret_w = (mid_now - mid_prev) / mid_prev
        ret_w_bps = ret_w * 10000
        if abs(ret_w_bps) < params.min_move_bps:
            continue
        if sigma <= 0 or abs(ret_w) < params.k * sigma:
            continue

        oi_now, oi_prev = series[i].get("oi"), series[i - w].get("oi")
        if not oi_now or not oi_prev or oi_prev <= 0:      # fail-closed on missing/stale OI
            continue
        oi_delta = (oi_now - oi_prev) / oi_prev
        if oi_delta > -params.d_oi_frac:
            continue

        # prior-only baseline median trade count: windows ending at/<= `base` (never the future).
        lo2 = max(w, base - lookback + 1)
        prior_counts = [tcw[j] for j in range(lo2, base + 1) if tcw[j] is not None]
        if not prior_counts:
            continue
        med_prior = sorted(prior_counts)[len(prior_counts) // 2]
        if med_prior <= 0 or (tcw[i] or 0) < params.m_trade * med_prior:
            continue

        ts = series[i]["ts_ms"]            # event ts = FIRST bucket where ALL conditions clear
        if last_ts is not None and ts - last_ts < cooldown_ms:
            continue

        direction = 1 if ret_w > 0 else -1
        events.append({
            "event_id": f"{asset}:{ts}:{params.version}",
            "asset": asset, "ts_ms": ts, "direction": direction,
            "ret_w": ret_w_bps, "sigma_w": sigma * 10000, "oi_delta_w": oi_delta * 100,
            "trade_count_w": tcw[i], "traded_notional_w": sum(
                series[j]["notional"] or 0 for j in range(i - w + 1, i + 1)),
            "magnitude_bucket": magnitude_bucket(ret_w_bps),
            "detector_version": params.version,
        })
        last_ts = ts
    return events
```

- [ ] **Step 4: Run to verify pass**

Run: `cd /home/bigpapi/Desktop/Sniper-leadlag && python3 -m pytest tests/test_hyperlive_cascade.py -q`
Expected: PASS (the 7 detector tests above).

- [ ] **Step 5: Commit**

```bash
git add sniper/hyperlive/cascade.py tests/test_hyperlive_cascade.py
git commit -m "feat(hl-cascade): pure detect_cascades + frozen DetectorParams"
```

---

## Task 3: Forward aftermath measurement (`aligned_moves`)

**Files:**
- Modify: `sniper/hyperlive/cascade.py`
- Test: `tests/test_hyperlive_cascade.py`

**Interfaces:**
- Consumes: event dicts from `detect_cascades`; `series_by_asset` = `{asset: [obs dicts ascending]}`.
- Produces: `aligned_moves(events, series_by_asset, horizon_ms, mode, *, tol_ms=15_000) -> list[dict]`. `mode` ∈ `{"continuation", "reversion"}`. Each result dict: `{"asset", "ts_ms", "bps"}` where `bps` is the **pre-fee** aligned forward move (fee is applied once later, in the gate). Events whose forward mid is unavailable within `tol_ms` are dropped.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_hyperlive_cascade.py
from sniper.hyperlive import cascade as C


def _obs(asset, ts, mid):
    return {"asset": asset, "ts_ms": ts, "mid": mid, "oi": 1e9, "n_trades": 1, "notional": 1.0}


def test_aligned_moves_continuation_vs_reversion_signs():
    # down-cascade event at t=0 (direction -1); price continues DOWN by 50bps over 60s.
    series = {"BTC": [_obs("BTC", 0, 100.0), _obs("BTC", 60_000, 99.5)]}
    ev = [{"asset": "BTC", "ts_ms": 0, "direction": -1}]
    cont = C.aligned_moves(ev, series, 60_000, "continuation")
    rev = C.aligned_moves(ev, series, 60_000, "reversion")
    # fwd_ret = -50bps; continuation aligned = dir * fwd = (-1)*(-50) = +50
    assert abs(cont[0]["bps"] - 50.0) < 1e-6
    assert abs(rev[0]["bps"] + 50.0) < 1e-6
    assert cont[0]["asset"] == "BTC" and cont[0]["ts_ms"] == 0


def test_aligned_moves_drops_event_without_forward_obs():
    series = {"BTC": [_obs("BTC", 0, 100.0)]}           # no obs near t+horizon
    ev = [{"asset": "BTC", "ts_ms": 0, "direction": -1}]
    assert C.aligned_moves(ev, series, 60_000, "continuation") == []
```

- [ ] **Step 2: Run to verify failure**

Run: `cd /home/bigpapi/Desktop/Sniper-leadlag && python3 -m pytest tests/test_hyperlive_cascade.py -k aligned_moves -q`
Expected: FAIL — `AttributeError: module 'sniper.hyperlive.cascade' has no attribute 'aligned_moves'`.

- [ ] **Step 3: Add `_fwd_ret_bps` + `aligned_moves` to `cascade.py`**

```python
def _nearest_mid(series, target_ms, tol_ms):
    """Mid of the first obs at/after target_ms within tol_ms; else the closest within tol_ms."""
    best, best_d = None, None
    for r in series:
        d = abs(r["ts_ms"] - target_ms)
        if d <= tol_ms and (best_d is None or d < best_d):
            best, best_d = r.get("mid"), d
    return best


def aligned_moves(events, series_by_asset, horizon_ms, mode, *, tol_ms=15_000):
    if mode not in ("continuation", "reversion"):
        raise ValueError(f"bad mode {mode!r}")
    out = []
    for e in events:
        series = series_by_asset.get(e["asset"]) or []
        m0 = _nearest_mid(series, e["ts_ms"], tol_ms)
        m1 = _nearest_mid(series, e["ts_ms"] + horizon_ms, tol_ms)
        if not m0 or m1 is None or m0 <= 0:
            continue
        fwd_bps = (m1 - m0) / m0 * 10000
        aligned = e["direction"] * fwd_bps
        if mode == "reversion":
            aligned = -aligned
        out.append({"asset": e["asset"], "ts_ms": e["ts_ms"], "bps": aligned})
    return out
```

- [ ] **Step 4: Run to verify pass**

Run: `cd /home/bigpapi/Desktop/Sniper-leadlag && python3 -m pytest tests/test_hyperlive_cascade.py -q`
Expected: PASS (all cascade tests so far).

- [ ] **Step 5: Commit**

```bash
git add sniper/hyperlive/cascade.py tests/test_hyperlive_cascade.py
git commit -m "feat(hl-cascade): aligned_moves forward aftermath (continuation/reversion, pre-fee)"
```

---

## Task 4: Negative-control battery

**Files:**
- Modify: `sniper/hyperlive/cascade.py`
- Test: `tests/test_hyperlive_cascade.py`

**Interfaces:**
- Produces (all return the same `[{"asset","ts_ms","bps"}]` shape as `aligned_moves`):
  - `control_flip(events, series_by_asset, horizon_ms, mode, **kw)` — flip each event's direction.
  - `control_shuffle(events, series_by_asset, horizon_ms, mode, *, rng, **kw)` — measure each event's forward move against a random OTHER asset's series.
  - `control_matched(events, series_by_asset, horizon_ms, mode, *, params=DetectorParams(), rng, **kw)` — same asset, same time-of-day hour bucket, NON-event windows.
  - `control_shift(events, series_by_asset, horizon_ms, mode, *, shift_ms=1_800_000, **kw)` — measure starting at `ts + shift_ms`.
- Determinism: `rng` is a `random.Random` passed in (no global RNG).

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_hyperlive_cascade.py
import random
from sniper.hyperlive import cascade as C


def _ramp(asset, n, slope_bps_per_bucket, start_ts=0, bucket_ms=10_000, mid0=100.0):
    out, mid = [], mid0
    for i in range(n):
        mid = mid0 * (1 + slope_bps_per_bucket / 10000.0 * i)
        out.append({"asset": asset, "ts_ms": start_ts + i * bucket_ms,
                    "mid": mid, "oi": 1e9, "n_trades": 1, "notional": 1.0})
    return out


def test_control_flip_negates_aligned():
    series = {"BTC": [{"asset": "BTC", "ts_ms": 0, "mid": 100.0, "oi": 1e9, "n_trades": 1, "notional": 1.0},
                      {"asset": "BTC", "ts_ms": 60_000, "mid": 99.5, "oi": 1e9, "n_trades": 1, "notional": 1.0}]}
    ev = [{"asset": "BTC", "ts_ms": 0, "direction": -1}]
    real = C.aligned_moves(ev, series, 60_000, "continuation")[0]["bps"]
    flip = C.control_flip(ev, series, 60_000, "continuation")[0]["bps"]
    assert abs(flip + real) < 1e-6


def test_control_shuffle_uses_other_asset():
    # BTC continues down (real edge); ETH is flat. Shuffle must read ETH's ~0 forward move.
    series = {
        "BTC": [{"asset": "BTC", "ts_ms": 0, "mid": 100.0, "oi": 1e9, "n_trades": 1, "notional": 1.0},
                {"asset": "BTC", "ts_ms": 60_000, "mid": 99.0, "oi": 1e9, "n_trades": 1, "notional": 1.0}],
        "ETH": [{"asset": "ETH", "ts_ms": 0, "mid": 50.0, "oi": 1e9, "n_trades": 1, "notional": 1.0},
                {"asset": "ETH", "ts_ms": 60_000, "mid": 50.0, "oi": 1e9, "n_trades": 1, "notional": 1.0}],
    }
    ev = [{"asset": "BTC", "ts_ms": 0, "direction": -1}]
    out = C.control_shuffle(ev, series, 60_000, "continuation", rng=random.Random(1))
    assert len(out) == 1 and abs(out[0]["bps"]) < 1e-6      # measured ETH (flat), not BTC


def test_control_shift_reads_a_shifted_window():
    # Forward move only happens in [0,60s]; after the shift the window is flat -> ~0.
    series = {"BTC": [
        {"asset": "BTC", "ts_ms": 0, "mid": 100.0, "oi": 1e9, "n_trades": 1, "notional": 1.0},
        {"asset": "BTC", "ts_ms": 60_000, "mid": 99.0, "oi": 1e9, "n_trades": 1, "notional": 1.0},
        {"asset": "BTC", "ts_ms": 1_860_000, "mid": 99.0, "oi": 1e9, "n_trades": 1, "notional": 1.0},
        {"asset": "BTC", "ts_ms": 1_920_000, "mid": 99.0, "oi": 1e9, "n_trades": 1, "notional": 1.0}]}
    ev = [{"asset": "BTC", "ts_ms": 0, "direction": -1}]
    out = C.control_shift(ev, series, 60_000, "continuation", shift_ms=1_800_000)
    assert len(out) == 1 and abs(out[0]["bps"]) < 1e-6


def test_control_matched_picks_nonevent_windows():
    # One asset, long flat history with a single planted event; matched control samples flat windows.
    s = _ramp("BTC", 400, 0.0)                         # perfectly flat -> any window ~0
    series = {"BTC": s}
    ev = [{"asset": "BTC", "ts_ms": s[200]["ts_ms"], "direction": -1}]
    out = C.control_matched(ev, series, 60_000, "continuation", rng=random.Random(7))
    assert out and all(abs(r["bps"]) < 1.0 for r in out)
```

- [ ] **Step 2: Run to verify failure**

Run: `cd /home/bigpapi/Desktop/Sniper-leadlag && python3 -m pytest tests/test_hyperlive_cascade.py -k control -q`
Expected: FAIL — controls not defined.

- [ ] **Step 3: Add controls to `cascade.py`**

```python
def control_flip(events, series_by_asset, horizon_ms, mode, *, tol_ms=15_000, **_):
    flipped = [{**e, "direction": -e["direction"]} for e in events]
    return aligned_moves(flipped, series_by_asset, horizon_ms, mode, tol_ms=tol_ms)


def control_shuffle(events, series_by_asset, horizon_ms, mode, *, rng, tol_ms=15_000, **_):
    assets = list(series_by_asset)
    out = []
    for e in events:
        others = [a for a in assets if a != e["asset"]]
        if not others:
            continue
        sub = rng.choice(others)
        m0 = _nearest_mid(series_by_asset[sub], e["ts_ms"], tol_ms)
        m1 = _nearest_mid(series_by_asset[sub], e["ts_ms"] + horizon_ms, tol_ms)
        if not m0 or m1 is None or m0 <= 0:
            continue
        fwd = (m1 - m0) / m0 * 10000
        aligned = e["direction"] * fwd
        if mode == "reversion":
            aligned = -aligned
        out.append({"asset": e["asset"], "ts_ms": e["ts_ms"], "bps": aligned})
    return out


def control_shift(events, series_by_asset, horizon_ms, mode, *, shift_ms=1_800_000, tol_ms=15_000, **_):
    shifted = [{**e, "ts_ms": e["ts_ms"] + shift_ms} for e in events]
    res = aligned_moves(shifted, series_by_asset, horizon_ms, mode, tol_ms=tol_ms)
    # restore original ts on the result rows (clustering uses the event's true time)
    for r, e in zip(res, [x for x in shifted]):
        r["ts_ms"] = r["ts_ms"] - shift_ms
    return res


def _hour_bucket(ts_ms):
    return (ts_ms // 3_600_000) % 24


def control_matched(events, series_by_asset, horizon_ms, mode, *, params: DetectorParams = DetectorParams(),
                    rng, tol_ms=15_000, n_per_event=1, **_):
    """Same asset, same hour-of-day bucket, windows that are NOT within cooldown of any real event."""
    cooldown_ms = params.cooldown_sec * 1000
    by_asset_evts = {}
    for e in events:
        by_asset_evts.setdefault(e["asset"], []).append(e["ts_ms"])
    out = []
    for e in events:
        series = series_by_asset.get(e["asset"]) or []
        evt_ts = by_asset_evts.get(e["asset"], [])
        cands = [r["ts_ms"] for r in series
                 if _hour_bucket(r["ts_ms"]) == _hour_bucket(e["ts_ms"])
                 and all(abs(r["ts_ms"] - t) >= cooldown_ms for t in evt_ts)]
        rng.shuffle(cands)
        for t0 in cands[:n_per_event]:
            m0 = _nearest_mid(series, t0, tol_ms)
            m1 = _nearest_mid(series, t0 + horizon_ms, tol_ms)
            if not m0 or m1 is None or m0 <= 0:
                continue
            fwd = (m1 - m0) / m0 * 10000
            aligned = e["direction"] * fwd
            if mode == "reversion":
                aligned = -aligned
            out.append({"asset": e["asset"], "ts_ms": t0, "bps": aligned})
    return out
```

- [ ] **Step 4: Run to verify pass**

Run: `cd /home/bigpapi/Desktop/Sniper-leadlag && python3 -m pytest tests/test_hyperlive_cascade.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/hyperlive/cascade.py tests/test_hyperlive_cascade.py
git commit -m "feat(hl-cascade): negative-control battery (flip/shuffle/matched/shift)"
```

---

## Task 5: Exploitability statistics (clustered-t, BH-FDR, thirds)

**Files:**
- Modify: `sniper/hyperlive/cascade.py`
- Test: `tests/test_hyperlive_cascade.py`

**Interfaces:**
- Produces:
  - `clustered_t(rows) -> float` — one-sample t over **asset × hour** cluster means of `bps`; H0: mean ≤ 0. `rows` = `[{"asset","ts_ms","bps"}]`. Mirrors `trader_intel._clustered_t` but clusters by `(asset, ts_ms // 3_600_000)`.
  - `t_to_p(t, df) -> float` — one-sided p (normal approx via `math.erfc`); `p = 0.5 * erfc(t / sqrt(2))`, clamped to `[0, 1]`.
  - `bh_fdr(pairs, q=0.10) -> set` — Benjamini-Hochberg survivors; mirror of `crypto5m_lane_eval.bh_fdr`.
  - `thirds_positive(rows) -> int` — count of chronological thirds (split by `ts_ms`) whose mean `bps` > 0.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_hyperlive_cascade.py
from sniper.hyperlive import cascade as C


def test_clustered_t_positive_for_consistent_edge():
    # 8 clusters (4 assets × 2 hours), each strongly positive -> large t.
    rows = []
    for a in ("BTC", "ETH", "SOL", "WIF"):
        for h in (0, 1):
            rows.append({"asset": a, "ts_ms": h * 3_600_000, "bps": 30.0})
    assert C.clustered_t(rows) > 2.0


def test_clustered_t_zero_for_noise_around_zero():
    rows = [{"asset": "BTC", "ts_ms": h * 3_600_000, "bps": (10.0 if h % 2 else -10.0)} for h in range(8)]
    assert abs(C.clustered_t(rows)) < 2.0


def test_bh_fdr_keeps_true_screens_nulls():
    pairs = [("real", 0.001)] + [(f"null{i}", 0.6) for i in range(20)]
    survivors = C.bh_fdr(pairs, q=0.10)
    assert "real" in survivors and not any(k.startswith("null") for k in survivors)


def test_thirds_positive_counts_chronological_thirds():
    rows = ([{"asset": "BTC", "ts_ms": i, "bps": 5.0} for i in range(10)] +
            [{"asset": "BTC", "ts_ms": 100 + i, "bps": 5.0} for i in range(10)] +
            [{"asset": "BTC", "ts_ms": 200 + i, "bps": -1.0} for i in range(10)])
    assert C.thirds_positive(rows) == 2
```

- [ ] **Step 2: Run to verify failure**

Run: `cd /home/bigpapi/Desktop/Sniper-leadlag && python3 -m pytest tests/test_hyperlive_cascade.py -k "clustered_t or bh_fdr or thirds" -q`
Expected: FAIL — functions not defined.

- [ ] **Step 3: Add stats to `cascade.py`**

```python
def _cluster_means(rows):
    buckets = {}
    for r in rows:
        key = (r["asset"], r["ts_ms"] // 3_600_000)
        buckets.setdefault(key, []).append(r["bps"])
    return [sum(v) / len(v) for v in buckets.values()]


def clustered_t(rows) -> float:
    """One-sample t over (asset × hour) cluster mean bps; H0: mean <= 0."""
    vals = _cluster_means(rows)
    n = len(vals)
    if n < 2:
        return 0.0
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / (n - 1)
    if var <= 0:
        return 0.0 if mean <= 0 else 999.0
    return mean / math.sqrt(var / n)


def t_to_p(t, df) -> float:
    """One-sided p-value, normal approximation (df unused; approx is acceptable per FDR docstring)."""
    p = 0.5 * math.erfc(t / math.sqrt(2))
    return min(1.0, max(0.0, p))


def bh_fdr(pairs, q=0.10) -> set:
    """Benjamini-Hochberg survivors. pairs=[(key,p)]. APPROXIMATE (cells are non-independent)."""
    valid = sorted(((k, p) for k, p in pairs if p is not None), key=lambda x: x[1])
    m = len(valid)
    if m == 0:
        return set()
    rank = 0
    for i, (_k, p) in enumerate(valid, 1):
        if p <= (i / m) * q:
            rank = i
    return {k for i, (k, _p) in enumerate(valid, 1) if i <= rank}


def thirds_positive(rows) -> int:
    rows = sorted(rows, key=lambda r: r["ts_ms"])
    n = len(rows)
    if n == 0:
        return 0
    cut = max(1, n // 3)
    thirds = [rows[:cut], rows[cut:2 * cut], rows[2 * cut:]]
    out = 0
    for t in thirds:
        if t and sum(r["bps"] for r in t) / len(t) > 0:
            out += 1
    return out
```

- [ ] **Step 4: Run to verify pass**

Run: `cd /home/bigpapi/Desktop/Sniper-leadlag && python3 -m pytest tests/test_hyperlive_cascade.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/hyperlive/cascade.py tests/test_hyperlive_cascade.py
git commit -m "feat(hl-cascade): clustered-t + BH-FDR + thirds-positive stats"
```

---

## Task 6: Canonical gate (`cell_passes_gate`) + end-to-end falsification test

**Files:**
- Modify: `sniper/hyperlive/cascade.py`
- Test: `tests/test_hyperlive_cascade.py`

**Interfaces:**
- Produces: `cell_passes_gate(cell, *, fdr_survivors, min_n=40, fee_bps=ROUND_TRIP_FEE_BPS) -> bool`. `cell` is a dict: `{"key", "n", "mean_bps", "t", "thirds_pos", "controls_pass"}`. Returns True iff: `n >= min_n` AND `mean_bps - fee_bps > 0` AND `t > 2.0` AND `thirds_pos >= 2` AND `controls_pass is True` AND `cell["key"] in fdr_survivors`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_hyperlive_cascade.py
import random
from sniper.hyperlive import cascade as C


def test_cell_passes_gate_all_conditions():
    cell = {"key": "k1", "n": 50, "mean_bps": 20.0, "t": 3.0, "thirds_pos": 3, "controls_pass": True}
    assert C.cell_passes_gate(cell, fdr_survivors={"k1"}) is True
    assert C.cell_passes_gate(cell, fdr_survivors=set()) is False           # FDR screened
    assert C.cell_passes_gate({**cell, "n": 10}, fdr_survivors={"k1"}) is False        # low n
    assert C.cell_passes_gate({**cell, "mean_bps": 8.0}, fdr_survivors={"k1"}) is False  # < fee
    assert C.cell_passes_gate({**cell, "t": 1.0}, fdr_survivors={"k1"}) is False         # weak t
    assert C.cell_passes_gate({**cell, "thirds_pos": 1}, fdr_survivors={"k1"}) is False  # unstable
    assert C.cell_passes_gate({**cell, "controls_pass": False}, fdr_survivors={"k1"}) is False


def test_negative_controls_null_on_planted_true_edge():
    """Synthetic: a genuine down-cascade continuation edge. Real measurement is strong;
    EVERY control must null. Proves the controls actually kill artifacts."""
    rng = random.Random(0)
    # Build 60 events across 4 assets; after each, the asset continues in the event direction by 40bps.
    series_by_asset = {a: [] for a in ("BTC", "ETH", "SOL", "WIF")}
    events = []
    bucket = 10_000
    for n in range(60):
        a = ("BTC", "ETH", "SOL", "WIF")[n % 4]
        t0 = 3_600_000 * (n // 4) + (n % 4) * 120_000     # spread across hours
        s = series_by_asset[a]
        s.append({"asset": a, "ts_ms": t0, "mid": 100.0, "oi": 1e9, "n_trades": 1, "notional": 1.0})
        s.append({"asset": a, "ts_ms": t0 + 60_000, "mid": 99.6, "oi": 1e9, "n_trades": 1, "notional": 1.0})  # -40bps
        # add neutral filler windows for the matched control to sample
        for f in range(1, 4):
            s.append({"asset": a, "ts_ms": t0 + 600_000 * f, "mid": 100.0, "oi": 1e9, "n_trades": 1, "notional": 1.0})
        events.append({"asset": a, "ts_ms": t0, "direction": -1})
    for a in series_by_asset:
        series_by_asset[a].sort(key=lambda r: r["ts_ms"])

    real = C.aligned_moves(events, series_by_asset, 60_000, "continuation")
    assert C.clustered_t(real) > 2.0 and sum(r["bps"] for r in real) / len(real) > 30

    flip = C.control_flip(events, series_by_asset, 60_000, "continuation")
    shuf = C.control_shuffle(events, series_by_asset, 60_000, "continuation", rng=rng)
    matc = C.control_matched(events, series_by_asset, 60_000, "continuation", rng=rng)
    shift = C.control_shift(events, series_by_asset, 60_000, "continuation", shift_ms=600_000)
    # every control must be non-significant (t <= 2)
    for ctrl in (flip, shuf, matc, shift):
        assert C.clustered_t(ctrl) <= 2.0
```

- [ ] **Step 2: Run to verify failure**

Run: `cd /home/bigpapi/Desktop/Sniper-leadlag && python3 -m pytest tests/test_hyperlive_cascade.py -k "gate or controls_null" -q`
Expected: FAIL — `cell_passes_gate` not defined.

- [ ] **Step 3: Add `cell_passes_gate` to `cascade.py`**

```python
def cell_passes_gate(cell, *, fdr_survivors, min_n=40, fee_bps=ROUND_TRIP_FEE_BPS) -> bool:
    return bool(
        cell["n"] >= min_n
        and (cell["mean_bps"] - fee_bps) > 0
        and cell["t"] > 2.0
        and cell["thirds_pos"] >= 2
        and cell["controls_pass"]
        and cell["key"] in fdr_survivors
    )
```

- [ ] **Step 4: Run to verify pass**

Run: `cd /home/bigpapi/Desktop/Sniper-leadlag && python3 -m pytest tests/test_hyperlive_cascade.py -q`
Expected: PASS (full cascade module suite).

- [ ] **Step 5: Commit**

```bash
git add sniper/hyperlive/cascade.py tests/test_hyperlive_cascade.py
git commit -m "feat(hl-cascade): canonical gate + planted-edge falsification of controls"
```

---

## Task 7: Capture loop (`run_cascade_capture.py`)

**Files:**
- Create: `scripts/run_cascade_capture.py`
- Test: `tests/test_run_cascade_capture.py`

**Interfaces:**
- Produces: `aggregate_buckets(trades, bucket_ms, *, now_ms) -> dict` — pure: groups a list of raw WS trade records (`{"coin","px","sz",...}`) into `{coin: (n_trades, traded_notional)}` for the bucket ending at `now_ms` (i.e., trades with `now_ms - bucket_ms <= time < now_ms`). The live `main()` loop is thin: subscribe trades WS for the top-N assets, poll `metaAndAssetCtxs` for OI + mid each bucket, call `aggregate_buckets`, write one `hl_cascade_obs` batch per bucket via `persistence.save_cascade_obs`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_run_cascade_capture.py
import importlib.util, os

spec = importlib.util.spec_from_file_location(
    "run_cascade_capture",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", "run_cascade_capture.py"))
rcc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rcc)


def test_aggregate_buckets_counts_and_notional_in_window():
    trades = [
        {"coin": "BTC", "px": "100", "sz": "2", "time": 9_000},     # in window [0,10000)
        {"coin": "BTC", "px": "101", "sz": "1", "time": 9_500},
        {"coin": "ETH", "px": "50",  "sz": "4", "time": 8_000},
        {"coin": "BTC", "px": "100", "sz": "5", "time": 20_000},    # outside window (>= now)
        {"coin": "BTC", "px": "100", "sz": "5", "time": -1},        # before window
    ]
    out = rcc.aggregate_buckets(trades, 10_000, now_ms=10_000)
    assert out["BTC"] == (2, 100 * 2 + 101 * 1)
    assert out["ETH"] == (1, 50 * 4)
    assert "BTC" in out and out["BTC"][0] == 2          # the 20000 and -1 trades excluded
```

- [ ] **Step 2: Run to verify failure**

Run: `cd /home/bigpapi/Desktop/Sniper-leadlag && python3 -m pytest tests/test_run_cascade_capture.py -q`
Expected: FAIL — `aggregate_buckets` not defined.

- [ ] **Step 3: Create `scripts/run_cascade_capture.py`**

```python
#!/usr/bin/env python3
"""Forward-only Hyperliquid cascade-observation capture.

Subscribes to the HL trades WS for the top-N assets, polls metaAndAssetCtxs for market mid + OI
each bucket, and writes raw per-bucket rows to hl_cascade_obs (the detector derives ret/sigma/oi
deltas downstream). No backfill. Read-only w.r.t. exchange. clearinghouseState is NOT used here
(market OI comes only from asset context). See the cascade detector design + plan."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.hyperlive import persistence as P  # noqa: E402

HL_WS = "wss://api.hyperliquid.xyz/ws"
HL_INFO = "https://api.hyperliquid.xyz/info"
DEFAULT_DB = "/home/bigpapi/Desktop/Sniper/data/hyperlive_engine.sqlite"


def aggregate_buckets(trades, bucket_ms, *, now_ms):
    """Pure: {coin: (n_trades, traded_notional)} for trades in [now_ms - bucket_ms, now_ms)."""
    lo = now_ms - bucket_ms
    out = {}
    for tr in trades:
        t = int(tr.get("time") or 0)
        if t < lo or t >= now_ms:
            continue
        coin = tr.get("coin")
        if not coin:
            continue
        try:
            notl = float(tr.get("px") or 0) * float(tr.get("sz") or 0)
        except (TypeError, ValueError):
            continue
        n, s = out.get(coin, (0, 0.0))
        out[coin] = (n + 1, s + notl)
    return out


def _info(payload):
    import requests
    r = requests.post(HL_INFO, json=payload, timeout=15)
    r.raise_for_status()
    return r.json()


def _assets_mid_oi(max_assets):
    """Returns (assets, {coin: (mid, oi)}) from metaAndAssetCtxs, top-N by 24h notional volume."""
    data = _info({"type": "metaAndAssetCtxs"})
    meta, ctxs = data[0], data[1]
    names = [m["name"] for m in meta.get("universe") or []]
    pairs = sorted(zip(names, ctxs or []), key=lambda x: -float(x[1].get("dayNtlVlm") or 0))
    pairs = pairs[:max_assets]
    assets = [a for a, _ in pairs]
    mid_oi = {}
    for a, c in pairs:
        mid = c.get("midPx") or c.get("markPx")
        oi = c.get("openInterest")
        mid_oi[a.upper()] = (float(mid) if mid else None, float(oi) if oi else None)
    return assets, mid_oi


def main(argv=None):
    ap = argparse.ArgumentParser(description="HL cascade observation capture")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--max-assets", type=int, default=20)
    ap.add_argument("--bucket-seconds", type=int, default=10)
    args = ap.parse_args(argv)

    import websocket  # websocket-client
    conn = P.connect(args.db)
    P.ensure_schema(conn)
    bucket_ms = args.bucket_seconds * 1000

    assets, _ = _assets_mid_oi(args.max_assets)
    ws = websocket.create_connection(HL_WS, timeout=15)
    for a in assets:
        ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "trades", "coin": a}}))
    ws.settimeout(1)

    buf = []
    next_flush = (int(time.time() * 1000) // bucket_ms + 1) * bucket_ms
    print(f"capturing {len(assets)} assets, bucket={args.bucket_seconds}s -> {args.db}", flush=True)
    while True:
        try:
            msg = json.loads(ws.recv())
            if msg.get("channel") == "trades":
                buf.extend(msg.get("data") or [])
        except websocket.WebSocketTimeoutException:
            pass
        except Exception as e:  # noqa: BLE001
            print(f"ws error: {type(e).__name__}: {e}", flush=True)
            time.sleep(1)
            try:
                ws = websocket.create_connection(HL_WS, timeout=15)
                for a in assets:
                    ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "trades", "coin": a}}))
                ws.settimeout(1)
            except Exception:
                pass
            continue

        now = int(time.time() * 1000)
        if now >= next_flush:
            agg = aggregate_buckets(buf, bucket_ms, now_ms=next_flush)
            try:
                _, mid_oi = _assets_mid_oi(args.max_assets)
            except Exception as e:  # noqa: BLE001
                print(f"ctx fetch error: {type(e).__name__}", flush=True)
                mid_oi = {}
            rows = {}
            for a in assets:
                au = a.upper()
                mid, oi = mid_oi.get(au, (None, None))
                n, notl = agg.get(a, (0, 0.0))
                rows[au] = (mid, oi, n, notl)
            P.save_cascade_obs(conn, rows, ts_ms=next_flush)
            buf = [t for t in buf if int(t.get("time") or 0) >= next_flush]
            next_flush += bucket_ms


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run to verify pass**

Run: `cd /home/bigpapi/Desktop/Sniper-leadlag && python3 -m pytest tests/test_run_cascade_capture.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/run_cascade_capture.py tests/test_run_cascade_capture.py
git commit -m "feat(hl-cascade): trades-WS + asset-ctx OI capture -> hl_cascade_obs"
```

---

## Task 8: Exploitability audit (`run_cascade_exploit_audit.py`)

**Files:**
- Create: `scripts/run_cascade_exploit_audit.py`
- Test: `tests/test_hyperlive_cascade.py` (add an end-to-end `build_grid` test against an in-memory series)

**Interfaces:**
- Produces: `run_audit(conn, *, params=DetectorParams(), horizons_ms=(60_000, 180_000, 600_000), min_n=40, rng=None) -> dict`. Reads `hl_cascade_obs`, runs `detect_cascades` per asset, writes events via `save_cascade_event`, builds the grid (magnitude_bucket × asset_class × horizon × {continuation, reversion}), evaluates each cell (mean, clustered-t, thirds, controls), applies BH-FDR across all cells, then the canonical gate, and returns `{"status": "accruing"|"edge"|"no-edge", "n_events", "cells": [...], "edges": [...]}`. `main()` opens the engine DB, calls `run_audit`, and persists the verdict via `save_cascade_audit`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_hyperlive_cascade.py
import importlib.util, os, random
from sniper.hyperlive import persistence as P


def _load_audit():
    spec = importlib.util.spec_from_file_location(
        "run_cascade_exploit_audit",
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", "run_cascade_exploit_audit.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_run_audit_accruing_when_few_events(tmp_path):
    audit = _load_audit()
    conn = P.connect(os.path.join(str(tmp_path), "engine.sqlite"))
    P.ensure_schema(conn)
    # write a calm series -> no events -> accruing
    for i in range(50):
        P.save_cascade_obs(conn, {"BTC": (100.0, 1e9, 5, 1e5)}, ts_ms=i * 10_000)
    res = audit.run_audit(conn, min_n=40, rng=random.Random(0))
    assert res["status"] == "accruing" and res["n_events"] == 0


def test_run_audit_writes_events_and_returns_status(tmp_path):
    audit = _load_audit()
    conn = P.connect(os.path.join(str(tmp_path), "engine.sqlite"))
    P.ensure_schema(conn)
    # one planted down-cascade in an otherwise calm BTC series
    mid = 100.0
    for i in range(250):
        m = mid * (1 + (0.0002 if i % 2 else -0.0002))
        oi, n, notl = 1e9, 10, 1e5
        if 195 <= i <= 200:
            m = 100.0 * (1 - 0.0025 * (i - 194)); oi = 1e9 * (1 - 0.004 * (i - 194)); n = 60
        P.save_cascade_obs(conn, {"BTC": (m, oi, n, notl)}, ts_ms=i * 10_000)
    res = audit.run_audit(conn, min_n=40, rng=random.Random(0))
    assert res["n_events"] >= 1
    assert res["status"] in ("accruing", "no-edge", "edge")
    assert len(P.read_cascade_events(conn)) == res["n_events"]
```

- [ ] **Step 2: Run to verify failure**

Run: `cd /home/bigpapi/Desktop/Sniper-leadlag && python3 -m pytest tests/test_hyperlive_cascade.py -k run_audit -q`
Expected: FAIL — script/`run_audit` not defined.

- [ ] **Step 3: Create `scripts/run_cascade_exploit_audit.py`**

```python
#!/usr/bin/env python3
"""Pre-registered Hyperliquid cascade exploitability audit.

Reads hl_cascade_obs, detects events (versioned, persisted), grids the forward aftermath over
magnitude × asset-class × horizon × {continuation, reversion}, runs the negative-control battery
and BH-FDR, and applies the single canonical gate. Writes a verdict (accruing/edge/no-edge).
Read-only / selection only; no orders. See the cascade detector design + plan."""
from __future__ import annotations

import argparse
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.hyperlive import cascade as C  # noqa: E402
from sniper.hyperlive import persistence as P  # noqa: E402

DEFAULT_DB = "/home/bigpapi/Desktop/Sniper/data/hyperlive_engine.sqlite"


def _series_by_asset(conn):
    by = {}
    for r in P.read_cascade_obs(conn):
        by.setdefault(r["asset"], []).append(r)
    for a in by:
        by[a].sort(key=lambda x: x["ts_ms"])
    return by


def run_audit(conn, *, params: C.DetectorParams = C.DetectorParams(),
              horizons_ms=(60_000, 180_000, 600_000), min_n=40, rng=None):
    rng = rng or random.Random(1234)
    series_by_asset = _series_by_asset(conn)

    all_events = []
    for asset, series in series_by_asset.items():
        evs = C.detect_cascades(series, params)
        for e in evs:
            P.save_cascade_event(conn, e)
        all_events.extend(evs)

    n_events = len(all_events)
    if n_events < min_n:
        return {"status": "accruing", "n_events": n_events, "min_n": min_n, "cells": [], "edges": []}

    # group events by (magnitude_bucket, asset_class)
    groups = {}
    for e in all_events:
        groups.setdefault((e["magnitude_bucket"], C.asset_class(e["asset"])), []).append(e)

    cells = []
    for (mag, klass), evs in groups.items():
        for h in horizons_ms:
            for mode in ("continuation", "reversion"):
                real = C.aligned_moves(evs, series_by_asset, h, mode)
                if len(real) < min_n:
                    continue
                mean_bps = sum(r["bps"] for r in real) / len(real)
                t = C.clustered_t(real)
                thirds = C.thirds_positive(real)
                # controls: each must be non-significant
                ctrls = {
                    "flip": C.control_flip(evs, series_by_asset, h, mode),
                    "shuffle": C.control_shuffle(evs, series_by_asset, h, mode, rng=rng),
                    "matched": C.control_matched(evs, series_by_asset, h, mode, params=params, rng=rng),
                    "shift": C.control_shift(evs, series_by_asset, h, mode),
                }
                # A thin/empty control is NOT a vacuous null (per review): require >= max(20, n/2)
                # rows for a control to be EVALUABLE; otherwise the cell can't be confirmed.
                min_ctrl = max(20, len(real) // 2)
                controls_evaluable = all(len(v) >= min_ctrl for v in ctrls.values())
                controls_pass = controls_evaluable and all(C.clustered_t(v) <= 2.0 for v in ctrls.values())
                key = f"{mag}|{klass}|{h}|{mode}"
                cells.append({
                    "key": key, "magnitude_bucket": mag, "asset_class": klass,
                    "horizon_ms": h, "mode": mode, "n": len(real),
                    "mean_bps": round(mean_bps, 3), "after_fee_bps": round(mean_bps - C.ROUND_TRIP_FEE_BPS, 3),
                    "t": round(t, 3), "thirds_pos": thirds,
                    "controls_evaluable": controls_evaluable, "controls_pass": controls_pass,
                    "p": C.t_to_p(t, len(real) - 1),
                })

    fdr_survivors = C.bh_fdr([(c["key"], c["p"]) for c in cells], q=0.10)
    edges, pending = [], []
    for c in cells:
        c["fdr_survivor"] = c["key"] in fdr_survivors
        c["edge"] = C.cell_passes_gate(c, fdr_survivors=fdr_survivors, min_n=min_n)
        if c["edge"]:
            edges.append(c["key"])
        elif (c["after_fee_bps"] > 0 and c["t"] > 2.0 and c["thirds_pos"] >= 2
              and c["fdr_survivor"] and not c["controls_evaluable"]):
            pending.append(c["key"])     # passes all but controls aren't trustworthy -> accruing

    status = "edge" if edges else ("accruing" if pending else "no-edge")
    return {"status": status, "n_events": n_events, "min_n": min_n,
            "cells": cells, "edges": edges, "pending": pending}


def main(argv=None):
    ap = argparse.ArgumentParser(description="HL cascade exploitability audit")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--min-n", type=int, default=40)
    ap.add_argument("--persist", action="store_true", help="write the verdict to hl_cascade_audit_runs")
    args = ap.parse_args(argv)

    conn = P.connect(args.db)
    P.ensure_schema(conn)
    res = run_audit(conn, min_n=args.min_n)
    print(f"status={res['status']} n_events={res['n_events']} edges={res['edges']}", flush=True)
    for c in sorted(res["cells"], key=lambda x: -x["t"])[:12]:
        print(f"  {c['key']:32} n={c['n']:4} mean={c['mean_bps']:+7.2f} after_fee={c['after_fee_bps']:+7.2f} "
              f"t={c['t']:+5.2f} thirds={c['thirds_pos']} ctrl={'ok' if c['controls_pass'] else 'FAIL'} "
              f"fdr={'y' if c['fdr_survivor'] else 'n'} edge={'YES' if c['edge'] else '-'}")
    if args.persist:
        P.save_cascade_audit(conn, res, ts_ms=int(time.time() * 1000))
        print("verdict persisted", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run to verify pass**

Run: `cd /home/bigpapi/Desktop/Sniper-leadlag && python3 -m pytest tests/test_hyperlive_cascade.py -q`
Expected: PASS (full cascade suite incl. the two `run_audit` tests).

- [ ] **Step 5: Commit**

```bash
git add scripts/run_cascade_exploit_audit.py tests/test_hyperlive_cascade.py
git commit -m "feat(hl-cascade): exploitability audit — grid + controls + FDR + canonical gate"
```

---

## Task 9: Thesis-1 magnet fix — whale-weighted clusters

**Files:**
- Modify: `sniper/hyperlive/cluster_map.py:36-58` (`cluster_signal`)
- Modify: `scripts/run_cluster_map.py` (multi-source de-duplicated harvest)
- Test: `tests/test_hyperlive_cluster_map.py` (create if absent)

**Interfaces:**
- Consumes: existing `cluster_signal(levels, mid, *, min_notional=0.0, max_distance_bps=300.0)`.
- Produces: `cluster_signal(levels, mid, *, min_notional=0.0, max_distance_bps=300.0, whale_weight=True)`. When `whale_weight=True`, a cluster's strength is dominated by its single largest position notional (not equal-weight across tiny accounts): rank candidate price bins by `max_position_notional` first, then total notional. Return shape unchanged: `(side, distance_bps, notional)` where `notional` remains the bin's total notional.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_hyperlive_cluster_map.py
from sniper.hyperlive import cluster_map as CM


def test_whale_weight_prefers_single_large_position():
    mid = 100.0
    # Bin A (just above): one whale of 8M. Bin B (also above, slightly farther): 10 dust of 0.5M (5M total).
    levels = [(100.5, 8_000_000.0)] + [(101.0, 500_000.0)] * 10
    side, dist, notl = CM.cluster_signal(levels, mid, min_notional=1_000_000.0, whale_weight=True)
    assert side == "long"                       # cluster above price -> price pulled up -> long bias
    assert abs(dist - 50.0) < 5.0               # picked the 100.5 whale bin (~50bps away)
    assert notl >= 8_000_000.0


def test_equal_weight_still_available():
    mid = 100.0
    levels = [(100.5, 8_000_000.0), (101.0, 500_000.0)]
    out = CM.cluster_signal(levels, mid, min_notional=1_000_000.0, whale_weight=False)
    assert out is not None and out[0] == "long"
```

- [ ] **Step 2: Run to verify failure**

Run: `cd /home/bigpapi/Desktop/Sniper-leadlag && python3 -m pytest tests/test_hyperlive_cluster_map.py -q`
Expected: FAIL — `cluster_signal() got an unexpected keyword argument 'whale_weight'`.

- [ ] **Step 3: Add `whale_weight` to `cluster_signal`**

Read the current `cluster_signal` body (`sniper/hyperlive/cluster_map.py:36-58`) first, then thread `whale_weight`: when binning price levels, track each bin's `max_position_notional` alongside `total_notional`; rank bins by `(max_position_notional, total_notional)` when `whale_weight=True`, else by `total_notional` (current behavior). Keep the returned tuple `(side, distance_bps, total_notional)` unchanged so all existing callers and `cluster_audit` keep working. Default `whale_weight=True` per the spec (whale-weighting is the fix), but the equal-weight path must remain reachable via `whale_weight=False`.

In `scripts/run_cluster_map.py`, extend `_harvest_addresses` to a **multi-source de-duplicated** harvest: union of (a) the existing trades-WS address set, (b) leaderboard top-N by account value (`HL.fetch_leaderboard()` wallets, reuse the import already in `screen_hl_smart_money.py`), de-duplicated into one `set`, capped at `--max-addresses`. This widens and de-time-biases the Thesis-1 sample. Keep `clearinghouseState` polling unchanged (it is the legitimate Thesis-1 whale-state source).

- [ ] **Step 4: Run to verify pass**

Run: `cd /home/bigpapi/Desktop/Sniper-leadlag && python3 -m pytest tests/test_hyperlive_cluster_map.py tests/test_hyperlive_cluster_audit.py -q`
Expected: PASS (new whale tests + existing cluster-audit tests still green).

- [ ] **Step 5: Commit**

```bash
git add sniper/hyperlive/cluster_map.py scripts/run_cluster_map.py tests/test_hyperlive_cluster_map.py
git commit -m "feat(hl-cascade): Thesis-1 magnet — whale-weighted clusters + multi-source harvest"
```

---

## Task 10: Cascade status route + UI panel

**Files:**
- Modify: `sniper/routes/api_hyperlive.py` (add `GET /api/hyperlive/cascade`)
- Modify: `static/app.js` (cascade status panel)
- Modify: `templates/index.html` (cache `v=144` → `v=145` + panel container)
- Test: `tests/test_api_hyperlive.py`

**Interfaces:**
- Consumes: `persistence.read_latest_cascade_audit`, `read_cascade_obs`, `read_cascade_events`.
- Produces: `GET /api/hyperlive/cascade` → `{"available": bool, "status": "accruing"|"edge"|"no-edge"|"none", "n_obs": int, "n_events": int, "verdict": {...}|None}`. Reuses the same engine `db_path`/`_conn` as the existing `state`/`history` routes.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_api_hyperlive.py — match the existing app/client fixture style in this file
def test_cascade_route_reports_status(tmp_path):
    import sqlite3
    from sniper.hyperlive import persistence as P
    from sniper.routes.api_hyperlive import create_api_hyperlive_blueprint
    from flask import Flask

    db = str(tmp_path / "engine.sqlite")
    conn = P.connect(db); P.ensure_schema(conn)
    P.save_cascade_obs(conn, {"BTC": (100.0, 1e9, 5, 1e5)}, ts_ms=1000)
    P.save_cascade_audit(conn, {"status": "no-edge", "n_events": 60, "edges": []}, ts_ms=2000)
    conn.close()

    app = Flask(__name__)
    app.register_blueprint(create_api_hyperlive_blueprint(db_path=db))
    c = app.test_client()
    r = c.get("/api/hyperlive/cascade")
    assert r.status_code == 200
    j = r.get_json()
    assert j["available"] is True and j["status"] == "no-edge"
    assert j["n_obs"] == 1 and j["verdict"]["n_events"] == 60


def test_cascade_route_no_db_is_graceful(tmp_path):
    from sniper.routes.api_hyperlive import create_api_hyperlive_blueprint
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(create_api_hyperlive_blueprint(db_path=str(tmp_path / "missing.sqlite")))
    r = app.test_client().get("/api/hyperlive/cascade")
    assert r.status_code == 200 and r.get_json()["available"] is False
```

- [ ] **Step 2: Run to verify failure**

Run: `cd /home/bigpapi/Desktop/Sniper-leadlag && python3 -m pytest tests/test_api_hyperlive.py -k cascade -q`
Expected: FAIL — 404 (route absent).

- [ ] **Step 3: Add the route**

In `sniper/routes/api_hyperlive.py`, mirror the existing `state` route. The engine-DB routes already guard on `os.path.exists(db_path)`; reuse that and the `_conn()` helper:

```python
    @bp.route("/api/hyperlive/cascade")
    def cascade():
        if not os.path.exists(db_path):
            return jsonify({"available": False, "status": "none", "n_obs": 0, "n_events": 0, "verdict": None})
        from sniper.hyperlive import persistence as P
        conn = _conn()
        try:
            latest = P.read_latest_cascade_audit(conn)
            n_obs = conn.execute("SELECT COUNT(*) FROM hl_cascade_obs").fetchone()[0]
            n_events = conn.execute("SELECT COUNT(*) FROM hl_cascade_events").fetchone()[0]
        finally:
            conn.close()
        return jsonify({
            "available": True,
            "status": (latest["verdict"]["status"] if latest else "none"),
            "n_obs": n_obs, "n_events": n_events,
            "verdict": (latest["verdict"] if latest else None),
        })
```

Note: `_conn()` opens `mode=ro`; `read_latest_cascade_audit`/the COUNTs are read-only, so this is safe. If `hl_cascade_obs` doesn't exist yet (older engine DB), wrap the COUNTs in try/except returning 0 — the engine DB only gains these tables once `ensure_schema` runs from capture/audit.

- [ ] **Step 4: Add the UI panel**

In `static/app.js`, add a `cascade` loader near `hlAnalyzer` that fetches `/api/hyperlive/cascade` and renders a status chip — `accruing` (grey), `no-edge` (muted), `edge` (#3fb950) — plus `n_obs`/`n_events` and, when present, the top edge cells from `verdict.cells`. Follow the existing `fetchJson(..., {suppressErrorToast:true})` + `innerHTML` pattern. In `templates/index.html`, add a `<div id="hl-cascade-panel">` container under the hyperlive tab and bump the cache version:

```html
    <script src="/static/app.js?v=145" defer></script>
```

- [ ] **Step 5: Run to verify pass + commit**

Run: `cd /home/bigpapi/Desktop/Sniper-leadlag && python3 -m pytest tests/test_api_hyperlive.py -q`
Expected: PASS.

```bash
git add sniper/routes/api_hyperlive.py static/app.js templates/index.html tests/test_api_hyperlive.py
git commit -m "feat(hl-cascade): /api/hyperlive/cascade status route + UI panel (cache v145)"
```

---

## Final verification (after all tasks)

```bash
cd /home/bigpapi/Desktop/Sniper-leadlag
python3 -m pytest tests/test_hyperlive_cascade.py tests/test_hyperlive_cascade_persistence.py \
  tests/test_run_cascade_capture.py tests/test_hyperlive_cluster_map.py \
  tests/test_hyperlive_cluster_audit.py tests/test_api_hyperlive.py -q
python3 scripts/validate_schema_refs.py
python3 scripts/brain_index.py generate && git status --short index/   # must be empty (index fresh)
```

All green + index fresh → ready for `superpowers:finishing-a-development-branch`.

**Operational follow-up (NOT part of TDD tasks — operator-gated, out of scope for the implementer):** add `run_cascade_capture.py` + a periodic `run_cascade_exploit_audit.py --persist` to the HL watchdog/cron once the schema lands, then accrue forward to the pre-registered milestone before reading any verdict. Capture is forward-only; the verdict is not immediate.

---

## Self-Review

**1. Spec coverage.**
- §2 two theses pre-registered → Thesis 2 detector (Tasks 2-8) + Thesis 1 whale-weight magnet (Task 9). ✓
- §3 collection (WS-derived trade count/notional, asset-ctx OI, no clearinghouseState for market OI) → Task 7. ✓
- §3 frozen constants + detector_version → `DetectorParams` (Task 2), Global Constraints. ✓
- §3 event persistence (versioned) → Tasks 1, 8. ✓
- §3 forward measurement at 60/180/600s × {continuation, reversion} → Task 3 + grid in Task 8. ✓
- §3 negative-control battery → Task 4 + wired in Task 8. ✓
- §3 canonical gate (single source) → Task 6 `cell_passes_gate`, used by Task 8. ✓
- §4 components table → all files mapped. ✓
- §6 testing (planted event fires one; slow drift none; cooldown; min-move floor; forward signs; fee once; FDR; controls null on planted edge; whale dominates) → Tasks 2-6, 9. ✓
- §7 decision fork (accruing/edge/no-edge verdict) → Task 8 `run_audit` status + Task 10 surface. ✓
- §8 risks (fail-closed OI, forward-only, paper-only) → Global Constraints + Task 2 OI skip + Task 7 no-backfill. ✓

**2. Placeholder scan.** No TBD/TODO; every code step has real code; commands have expected output. ✓

**3. Type consistency.** `aligned_moves` and all four controls return `[{"asset","ts_ms","bps"}]`; `clustered_t`/`thirds_positive` consume that exact shape; `cell` dict keys (`key`,`n`,`mean_bps`,`t`,`thirds_pos`,`controls_pass`) are produced in Task 8 and consumed by `cell_passes_gate` (Task 6); event dict shape from `detect_cascades` (Task 2) matches `save_cascade_event` (Task 1) and `read_cascade_events`. `DetectorParams` constants match the Global Constraints values. ✓
