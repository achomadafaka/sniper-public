# Crypto5m Adaptive Paper Cockpit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only adaptive paper cockpit that compares `maker-regime-v1` control against the active adaptive paper challenger.

**Architecture:** Keep control and adaptive DBs separate. Add a new read-only endpoint that opens both DBs, normalizes campaign stats, pairs settled windows by `window_start_ts`, and exposes adaptive profile/reward state. Add a compact UI band to the existing Maker desk that fetches and renders this endpoint without mutating learner policy.

**Tech Stack:** Flask blueprint in `sniper/routes/api_crypto5m.py`, vanilla JS in `static/app.js`, pytest API/UI tests.

## Global Constraints

- No real orders.
- No private keys.
- No production DB.
- No DB merge.
- No browser-side policy mutation.
- No live service restart from the API/UI.
- No retroactive changes to adaptive choices.
- No claim that adaptive is profitable before paired-window evidence exists.
- Control DB default: `data/crypto5m/maker_v0.sqlite`.
- Adaptive DB default: `data/crypto5m/maker_adaptive_v2.sqlite` after the Learner V2 cutover. Override with `CRYPTO5M_ADAPTIVE_MAKER_DB` to inspect V1.

---

### Task 1: Adaptive Cockpit API

**Files:**
- Modify: `sniper/routes/api_crypto5m.py`
- Test: `tests/test_api_crypto5m.py`

**Interfaces:**
- Produces: `GET /api/crypto5m/maker-adaptive`
- Produces helper: `_maker_campaign_snapshot(conn, campaign_hint: str | None) -> dict`
- Produces helper: `_adaptive_current_state(think: dict | None) -> dict`
- Produces helper: `_paired_window_summary(control_rows: list, adaptive_rows: list) -> dict`

- [ ] **Step 1: Write failing API tests**

Add tests to `tests/test_api_crypto5m.py`:

```python
def test_maker_adaptive_reports_missing_dbs_without_creating_them(tmp_path):
    control = tmp_path / "missing_control.sqlite"
    adaptive = tmp_path / "missing_adaptive.sqlite"

    body = _client("", maker_db_path=str(control), adaptive_maker_db_path=str(adaptive)).get(
        "/api/crypto5m/maker-adaptive"
    ).get_json()

    assert body["available"] is False
    assert body["control"]["available"] is False
    assert body["adaptive"]["available"] is False
    assert "control DB not found" in body["warnings"]
    assert "adaptive DB not found" in body["warnings"]
    assert not control.exists()
    assert not adaptive.exists()


def test_maker_adaptive_pairs_settled_windows_and_surfaces_current_profile(tmp_path):
    import json
    import sqlite3
    import time

    control = tmp_path / "control.sqlite"
    adaptive = tmp_path / "adaptive.sqlite"
    now = int(time.time())
    for path, campaign, pnl in (
        (control, "maker-regime-v1", 10.0),
        (adaptive, "maker-adaptive-paper-v1", 13.5),
    ):
        conn = sqlite3.connect(path)
        conn.executescript("""
          CREATE TABLE maker_state(id INTEGER PRIMARY KEY CHECK(id=1), think_json TEXT, ts INTEGER);
          CREATE TABLE maker_windows(
            condition_id TEXT PRIMARY KEY, window_start_ts INTEGER, up_inv REAL, down_inv REAL, cash REAL,
            realized_pnl REAL, settled INTEGER DEFAULT 0, winning_outcome TEXT, ts INTEGER,
            up_vwap REAL, down_vwap REAL, campaign_id TEXT);
          CREATE TABLE maker_fills(
            id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT, condition_id TEXT, window_start_ts INTEGER,
            token TEXT, side TEXT, price REAL, size REAL, fair REAL, ts INTEGER, reason TEXT, fill_model TEXT,
            profile_id TEXT, fill_profile_id TEXT, selection_mode TEXT, adaptive_context_key TEXT);
        """)
        think = {"campaign": campaign, "engine_version": campaign, "window_start_ts": 1000,
                 "condition_id": "0xcurrent", "seconds_left": 120, "fair_value_up": 0.52}
        if campaign == "maker-adaptive-paper-v1":
            think["adaptive_paper"] = {
                "selector_version": "adaptive-ucb-v1",
                "reward_version": "maker-reward-v1",
                "profiles": {
                    "up": {"profile_id": "quote_tight", "selection_source": "fallback",
                           "exploration": True, "context_key": "mid|mid|calm|flat|healthy|unconfirmed|healthy",
                           "parent_context_used": "global", "n_fills": 0, "mean_reward": 0.0}
                },
            }
        conn.execute("INSERT INTO maker_state(id,think_json,ts) VALUES(1,?,?)", (json.dumps(think), now))
        conn.execute("""INSERT INTO maker_windows(condition_id,window_start_ts,up_inv,down_inv,cash,
                      realized_pnl,settled,winning_outcome,ts,campaign_id)
                      VALUES(?,?,?,?,?,?,?,?,?,?)""",
                     ("0xpair", 700, 0.0, 0.0, 0.0, pnl, 1, "Up", now, campaign))
        conn.commit()
        conn.close()

    conn = sqlite3.connect(adaptive)
    conn.executescript("""
      CREATE TABLE crypto5m_maker_profile_stats(
        campaign_id TEXT, context_key TEXT, profile_id TEXT, n_decisions INTEGER, n_quotes INTEGER,
        n_fills INTEGER, reward_sum REAL, reward_sq_sum REAL, pnl_sum REAL, adverse_sum REAL,
        hard_cuts INTEGER, disabled INTEGER, last_updated_ts INTEGER);
      CREATE TABLE crypto5m_maker_profile_disables(
        id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT, context_key TEXT, profile_id TEXT,
        disabled_at INTEGER, disabled_reason TEXT, selector_version TEXT, reward_version TEXT);
      CREATE TABLE crypto5m_maker_adaptive_rewards(
        id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT, fill_id INTEGER, reward REAL,
        fee_adjusted_pnl REAL, hard_cut_penalty REAL, reward_version TEXT);
    """)
    conn.execute("""INSERT INTO crypto5m_maker_profile_stats VALUES(
                    'maker-adaptive-paper-v1','mid|mid|calm|flat|healthy|unconfirmed|healthy',
                    'quote_tight',5,5,2,1.5,1.25,1.5,0,0,0,?)""", (now,))
    conn.commit()
    conn.close()

    body = _client("", maker_db_path=str(control), adaptive_maker_db_path=str(adaptive)).get(
        "/api/crypto5m/maker-adaptive"
    ).get_json()

    assert body["available"] is True
    assert body["paired_summary"]["n_paired"] == 1
    assert body["paired_summary"]["adaptive_minus_control"] == 3.5
    assert body["current"]["profiles_by_token"]["up"]["profile_id"] == "quote_tight"
    assert body["profile_stats"][0]["mean_reward"] == 0.75
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
python3 -m pytest tests/test_api_crypto5m.py::test_maker_adaptive_reports_missing_dbs_without_creating_them tests/test_api_crypto5m.py::test_maker_adaptive_pairs_settled_windows_and_surfaces_current_profile -q
```

Expected: fail because `_client` has no `adaptive_maker_db_path` argument and `/api/crypto5m/maker-adaptive` does not exist.

- [ ] **Step 3: Implement API helpers and endpoint**

Modify `_client` in tests to pass `adaptive_maker_db_path`.

In `sniper/routes/api_crypto5m.py`, add:

```python
ADAPTIVE_MAKER_DEFAULT_DB = os.path.join(ROOT, "data", "crypto5m", "maker_adaptive_v2.sqlite")
```

Add helper functions:

```python
def _maker_campaign_snapshot(conn, campaign_hint):
    srow = conn.execute("SELECT think_json, ts FROM maker_state WHERE id=1").fetchone()
    think = json.loads(srow["think_json"]) if srow and srow["think_json"] else None
    campaign = (think or {}).get("campaign") or campaign_hint
    rows = conn.execute(
        "SELECT * FROM maker_windows WHERE campaign_id=? ORDER BY window_start_ts DESC",
        (campaign,),
    ).fetchall() if campaign and _table_exists(conn, "maker_windows") else []
    settled = [r for r in rows if r["settled"] and r["realized_pnl"] is not None]
    return {
        "available": True,
        "campaign": campaign,
        "engine_age_s": max(0, int(time.time()) - int(srow["ts"] or 0)) if srow else None,
        "think": think,
        "n_settled": len(settled),
        "realized_pnl": round(sum(float(r["realized_pnl"] or 0.0) for r in settled), 2),
        "n_open": sum(1 for r in rows if not r["settled"]),
        "n_pending": sum(1 for r in rows if r["settled"] and r["winning_outcome"] == "unresolved"),
        "latest_window_start_ts": (think or {}).get("window_start_ts"),
        "latest_condition_id": (think or {}).get("condition_id"),
        "settled_windows": settled,
    }
```

Add endpoint using read-only connections and returning warnings instead of creating missing DBs.

- [ ] **Step 4: Run API tests**

Run:

```bash
python3 -m pytest tests/test_api_crypto5m.py::test_maker_adaptive_reports_missing_dbs_without_creating_them tests/test_api_crypto5m.py::test_maker_adaptive_pairs_settled_windows_and_surfaces_current_profile -q
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add sniper/routes/api_crypto5m.py tests/test_api_crypto5m.py
git commit -m "feat(crypto5m): add adaptive maker cockpit API"
```

### Task 2: Adaptive Cockpit UI

**Files:**
- Modify: `static/app.js`
- Test: `tests/test_ui_algo_tabs.py`

**Interfaces:**
- Consumes: `GET /api/crypto5m/maker-adaptive`
- Produces JS method: `_adaptiveCockpitHtml(report)`

- [ ] **Step 1: Write failing UI test**

Add assertions to `tests/test_ui_algo_tabs.py`:

```python
def test_crypto5m_maker_ui_fetches_adaptive_cockpit():
    js = Path("static/app.js").read_text()
    assert "fetchJson('/api/crypto5m/maker-adaptive'" in js
    assert "Adaptive Challenger" in js
    assert "_adaptiveCockpitHtml" in js
```

- [ ] **Step 2: Run test to verify failure**

Run:

```bash
python3 -m pytest tests/test_ui_algo_tabs.py::test_crypto5m_maker_ui_fetches_adaptive_cockpit -q
```

Expected: fail because no adaptive cockpit fetch/render method exists.

- [ ] **Step 3: Implement compact renderer**

In `static/app.js`, add `_adaptiveCockpitHtml(report)` near `_regimeEvalHtml(report)`.

Render:

```text
Adaptive Challenger
control vs adaptive settled PnL
paired delta
current profiles by token
selection source
top profile stats
recent fills
warnings
```

In `crypto5m.load()`, fetch `/api/crypto5m/maker-adaptive` after `/api/crypto5m/maker`, suppressing errors:

```javascript
let adaptive = null;
try { adaptive = await fetchJson('/api/crypto5m/maker-adaptive', { suppressErrorToast: true }); } catch (e2) {}
```

Insert:

```javascript
const adaptiveHtml = this._adaptiveCockpitHtml(adaptive);
```

Then place `${adaptiveHtml}` under `${accountingHtml}`.

- [ ] **Step 4: Run UI test**

Run:

```bash
python3 -m pytest tests/test_ui_algo_tabs.py::test_crypto5m_maker_ui_fetches_adaptive_cockpit -q
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add static/app.js tests/test_ui_algo_tabs.py
git commit -m "feat(crypto5m): show adaptive paper cockpit"
```

### Task 3: Adaptive Watchdog Stanza

**Files:**
- Modify: `scripts/crypto5m_watchdog.sh`
- Test: `tests/test_crypto5m_watchdog_config.py`

**Interfaces:**
- Produces watchdog process name: `crypto5m_adaptive_maker`
- Produces PID file: `data/crypto5m/maker_adaptive_v1.pid`
- Produces DB: `data/crypto5m/maker_adaptive_v1.sqlite`

- [ ] **Step 1: Write failing watchdog test**

Add to `tests/test_crypto5m_watchdog_config.py`:

```python
def test_watchdog_has_adaptive_paper_challenger_stanza():
    script = Path("scripts/crypto5m_watchdog.sh").read_text()
    assert "crypto5m_adaptive_maker" in script
    assert "maker_adaptive_v1.sqlite" in script
    assert "maker-adaptive-paper-v1" in script
    assert "--maker-adaptive-paper" in script
    assert "maker_v0.pid" in script
```

- [ ] **Step 2: Run test to verify failure**

Run:

```bash
python3 -m pytest tests/test_crypto5m_watchdog_config.py::test_watchdog_has_adaptive_paper_challenger_stanza -q
```

Expected: fail because watchdog does not manage adaptive challenger.

- [ ] **Step 3: Add separate adaptive watchdog stanza**

Add below the control maker stanza:

```bash
# Adaptive paper challenger ENABLED 2026-07-05. Separate DB because maker_windows is keyed by condition_id.
# Paper only, no orders. This process is intentionally not governed by the control campaign guard.
ensure "crypto5m_adaptive_maker" "data/crypto5m/maker_adaptive_v1.pid" \
  "run_polymarket_crypto5m_maker.py --paper-db data/crypto5m/maker_adaptive_v1.sqlite" \
  "$PY -u scripts/run_polymarket_crypto5m_maker.py --paper-db data/crypto5m/maker_adaptive_v1.sqlite --pid data/crypto5m/maker_adaptive_v1.pid --campaign maker-adaptive-paper-v1 --continuous --poll 1.0 --maker-adaptive-paper --adaptive-control-campaign maker-regime-v1 --adaptive-epsilon 0.10 --adaptive-seed 20260705"
```

Do not alter the existing `crypto5m_maker` stanza.

- [ ] **Step 4: Run watchdog test**

Run:

```bash
python3 -m pytest tests/test_crypto5m_watchdog_config.py::test_watchdog_has_adaptive_paper_challenger_stanza -q
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/crypto5m_watchdog.sh tests/test_crypto5m_watchdog_config.py
git commit -m "chore(crypto5m): watchdog adaptive paper challenger"
```

### Task 4: Final Verification

**Files:**
- No new files.

**Interfaces:**
- Verifies `/api/crypto5m/maker`
- Verifies `/api/crypto5m/maker-adaptive`
- Verifies adaptive process remains separate.

- [ ] **Step 1: Run focused tests**

Run:

```bash
python3 -m pytest tests/test_api_crypto5m.py tests/test_ui_algo_tabs.py tests/test_crypto5m_watchdog_config.py -q
```

Expected: all pass.

- [ ] **Step 2: Run broader crypto5m tests**

Run:

```bash
python3 -m pytest tests/test_crypto5m*.py -q
```

Expected: all pass.

- [ ] **Step 3: Check live adaptive process**

Run:

```bash
cat data/crypto5m/maker_adaptive_v1.pid
ps -p "$(cat data/crypto5m/maker_adaptive_v1.pid)" -o pid,ppid,etime,cmd
```

Expected: process is alive and command contains `--maker-adaptive-paper`.

- [ ] **Step 4: Check DB separation**

Run:

```bash
python3 - <<'PY'
import sqlite3
for p in ["data/crypto5m/maker_v0.sqlite", "data/crypto5m/maker_adaptive_v1.sqlite"]:
    c = sqlite3.connect(p)
    print(p, c.execute("SELECT COUNT(*) FROM maker_fills WHERE campaign_id='maker-adaptive-paper-v1'").fetchone()[0])
    c.close()
PY
```

Expected:

```text
data/crypto5m/maker_v0.sqlite 0
data/crypto5m/maker_adaptive_v1.sqlite >0
```

- [ ] **Step 5: Commit if any final fixes are needed**

If no final fixes are needed, do not create an empty commit.
