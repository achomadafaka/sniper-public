# HL Analyzer Cleanup + Copy/Info Honesty Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Clean the HL Analyzer tab's verified defects and make "copyable" mean *survived the forward-follow net-of-fee test*, not just archetype — without breaking the live forward-follow data feed.

**Architecture:** Persist a stable `archetype_pass` boolean (phase-1 gate) independent of the display verdict. The follow runner selects on `archetype_pass`, not the verdict string, so renaming `copyable→watch` can't silently stop data collection. A read-time phase-2 gate over per-wallet forward-follow stats promotes `watch → proven`. The UI shows the two-tier verdict and only offers the 24/7 copy action for `proven`.

**Tech Stack:** Python 3.13, SQLite (WAL), Flask blueprint, vanilla JS (`static/app.js`), pytest.

## Global Constraints

- Worktree: `/home/bigpapi/Desktop/Sniper-leadlag`, branch `research/hl-measurement-audit-leadlag`. All paths below are relative to this worktree.
- Intel DB: `/home/bigpapi/Desktop/Sniper/data/hl_intel.sqlite` (note: the leadlag code reads/writes the **main** worktree's `data/` dir — see `api_hyperlive.py:11-12`).
- The forward-follow runner `scripts/run_hl_follow.py` is **live** (watchdog cron `* * * * *`). Phase-2 (Tasks 6-11) must not change its selection except via Task 5's `archetype_pass` switch.
- Phase-2 constants are **frozen** (Task 7): `PHASE2_MIN_CLOSES=30`, `PHASE2_MIN_T=2.0`, `PHASE2_MAX_CONCENTRATION=0.40`, `PHASE2_STALE_DAYS=14`, cluster dimension = **asset×day**.
- Pure functions over fill/row dicts; never `except: pass`; `datetime.now(timezone.utc)` not `utcnow()`.
- Paper/research surface only — never copy-trade advice or a live trigger.
- Run focused tests: `python3 -m pytest tests/test_hyperlive_*.py tests/test_api_hyperlive.py -q`.

---

## File Structure

| File | Responsibility | Phase |
|---|---|---|
| `static/app.js` (hlAnalyzer ~5499-5596) | listener-leak fix, signal race, two-tier verdict render, copy-button gating | 1, 4 |
| `tests/test_api_hyperlive.py` | `/trader/<wallet>` route test; analyzer two-tier payload | 1, 4 |
| `sniper/hyperlive/intel_store.py` | `archetype_pass` column + migration; `follow_stats_by_wallet`; `get_screened` ordering | 2, 3 |
| `scripts/run_hl_follow.py` | select followables by `archetype_pass` (the data-feed fix) | 2 |
| `sniper/hyperlive/trader_intel.py` | phase-1 `archetype_pass` flag + rename; `copyability_phase2`; `effective_verdict` | 3, 3 |
| `sniper/routes/api_hyperlive.py` | join per-wallet follow stats; emit two-tier verdict | 4 |
| `tests/test_hyperlive_intel_store.py`, `tests/test_hyperlive_trader_intel.py` | unit coverage for the above | 2-4 |

**Phase order (the user's `C-cleanup → …` sequencing):**
- **Phase 1 — Tasks 1-2:** mechanical JS cleanup + endpoint test gap (independent, low-risk).
- **Phase 2 — Tasks 3-5:** `archetype_pass` column + migration + follow-selection fix. **These land together and BEFORE any verdict rename**, or forward-follow collection stops.
- **Phase 3 — Tasks 6-11:** phase-2 honesty gate, per-wallet stats, rename, route wiring, UI truthfulness. Code lands now; promotion is data-gated at runtime (no `proven` until a wallet has ≥30 closes).

---

## Phase 1 — JS cleanup + endpoint test gap

### Task 1: Fix the hlAnalyzer listener leak + signal race (defects 1-3)

**Files:**
- Modify: `static/app.js:5541-5548` (`_bind`), `static/app.js:5588-5594` (`toggleCopy`), `static/app.js:5550-5561` (`detail` — thread the signal)

**Interfaces:**
- Produces: `hlAnalyzer._bind()` attaches exactly one delegated listener; `toggleCopy(wallet, active, sig)` honors an abort signal.

- [ ] **Step 1: Replace `_bind()` with event delegation (one listener, guarded)**

Replace `static/app.js:5541-5548` with:

```javascript
    _bind() {
      const list = document.getElementById('hl-analyzer-list');
      if (list && !list._b) {                       // delegate once; survives re-render
        list._b = 1;
        list.addEventListener('click', (e) => {
          const item = e.target.closest('.analyzer-report-item');
          if (item) this.detail(item.getAttribute('data-wallet'));
        });
      }
      const btn = document.getElementById('hl-analyzer-btn');
      const inp = document.getElementById('hl-analyzer-input');
      if (btn && !btn._b) { btn._b = 1; btn.addEventListener('click', () => { if (inp.value.trim()) this.detail(inp.value.trim()); }); }
      if (inp && !inp._b) { inp._b = 1; inp.addEventListener('keydown', e => { if (e.key === 'Enter' && inp.value.trim()) this.detail(inp.value.trim()); }); }
    },
```

- [ ] **Step 2: Thread the abort signal through `toggleCopy` (fix the stale-render race)**

Replace `static/app.js:5588-5594` (`toggleCopy`) with:

```javascript
    async toggleCopy(wallet, active, sig) {
      try {
        await fetchJson('/api/hyperlive/copy/set', { method: 'POST', body: JSON.stringify({ wallet, active: !active }), signal: sig });
        toast(active ? 'Stopped mirroring' : 'Now mirroring 24/7 (paper)', 'success');
        await Promise.allSettled([this.load(sig), this.detail(wallet, sig)]);
      } catch (e) { if (e.name !== 'AbortError') toast('Copy toggle failed', 'error'); }
    },
```

Then in `detail` (line ~5560) update the copy-button binding to pass the signal:

```javascript
      const cb = document.getElementById('hl-copy-btn');
      if (cb) cb.addEventListener('click', () => this.toggleCopy(d.wallet, cb.getAttribute('data-active') === '1', sig));
```

And change the `detail` signature (line 5550) to accept `sig`: `async detail(wallet, sig) {` and pass `sig` into its `fetchJson('/api/hyperlive/trader/...', { signal: sig, suppressErrorToast: true })` call (line 5555).

- [ ] **Step 3: Manual verification (no JS test harness in repo)**

Run the web server from the leadlag worktree:
`SNIPER_DISABLE_SEASON_REFRESH=1 python3 -m sniper.web.server` (port 5000).
Open the HL Analyzer tab, DevTools → Elements → select `#hl-analyzer-list` → Event Listeners: confirm a single `click` listener after switching tabs 5×. Confirm clicking a row still opens the detail panel.
Expected: one listener, not N.

- [ ] **Step 4: Commit**

```bash
git add static/app.js
git commit -m "fix(hl-analyzer): delegate list clicks (no listener leak) + signal-safe toggleCopy"
```

### Task 2: Add the missing `/api/hyperlive/trader/<wallet>` route test

**Files:**
- Modify: `tests/test_api_hyperlive.py`

**Interfaces:**
- Consumes: `create_api_hyperlive_blueprint`, monkeypatched `hyperliquid_api.fetch_user_fills` + `trader_intel.assess_trader`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_api_hyperlive.py`:

```python
def test_trader_route_returns_assessment(monkeypatch):
    from sniper.routes import api_hyperlive as AH
    from sniper.markets import hyperliquid_api as HL
    fills = [{"coin": "BTC", "dir": "Close Long", "closedPnl": "12.0", "fee": "0.1", "crossed": True}]
    monkeypatch.setattr(HL, "fetch_user_fills", lambda w: fills)
    app = _make_app(AH.create_api_hyperlive_blueprint(db_path="/nonexistent.sqlite"))
    r = app.test_client().get("/api/hyperlive/trader/0xabc")
    assert r.status_code == 200
    body = r.get_json()
    assert body["available"] is True
    assert body["wallet"] == "0xabc"
    assert "verdict" in body and isinstance(body["breakdown"], list)


def test_trader_route_handles_fetch_error(monkeypatch):
    from sniper.routes import api_hyperlive as AH
    from sniper.markets import hyperliquid_api as HL
    def boom(w): raise TimeoutError("slow")
    monkeypatch.setattr(HL, "fetch_user_fills", boom)
    app = _make_app(AH.create_api_hyperlive_blueprint(db_path="/nonexistent.sqlite"))
    r = app.test_client().get("/api/hyperlive/trader/0xabc")
    assert r.get_json()["available"] is False
    assert r.get_json()["reason"] == "TimeoutError"
```

If `_make_app` does not already exist in the file, add this helper near the top:

```python
def _make_app(bp):
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(bp)
    return app
```

- [ ] **Step 2: Run to verify it passes (route already exists; this closes the coverage gap)**

Run: `python3 -m pytest tests/test_api_hyperlive.py -q -k trader_route`
Expected: PASS (the route at `api_hyperlive.py:161` already works; this test pins its contract).

- [ ] **Step 3: Commit**

```bash
git add tests/test_api_hyperlive.py
git commit -m "test(hl): cover /api/hyperlive/trader/<wallet> success + fetch-error paths"
```

---

## Phase 2 — archetype_pass column + follow-selection fix (PROTECT THE DATA FEED)

### Task 3: Add `archetype_pass` column + migration + backfill to intel_store

**Files:**
- Modify: `sniper/hyperlive/intel_store.py` (`ensure_schema`, `upsert_tracked`, `upsert_screened`, `get_screened`)
- Test: `tests/test_hyperlive_intel_store.py`

**Interfaces:**
- Produces: `hl_tracked_traders.archetype_pass`, `hl_screened_traders.archetype_pass` (INTEGER 0/1); `upsert_tracked`/`upsert_screened` persist `info["archetype_pass"]`; `_migrate(conn)` is idempotent.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_hyperlive_intel_store.py`:

```python
def test_archetype_pass_column_and_backfill(tmp_path):
    import sniper.hyperlive.intel_store as IS
    db = str(tmp_path / "intel.sqlite")
    conn = IS.connect(db)
    # simulate a PRE-migration row written by the old code path (no archetype_pass)
    IS.ensure_schema(conn)
    conn.execute("INSERT INTO hl_tracked_traders(wallet, verdict, archetype, added_ts_ms, updated_ts_ms) "
                 "VALUES ('0xold', 'copyable', 'directional', 1, 1)")
    conn.commit()
    IS._migrate(conn)                         # idempotent; backfills legacy copyable -> archetype_pass=1
    row = conn.execute("SELECT archetype_pass FROM hl_tracked_traders WHERE wallet='0xold'").fetchone()
    assert row["archetype_pass"] == 1
    IS._migrate(conn)                         # second call must not throw (idempotent)


def test_upsert_persists_archetype_pass(tmp_path):
    import sniper.hyperlive.intel_store as IS
    conn = IS.connect(str(tmp_path / "intel.sqlite"))
    IS.ensure_schema(conn)
    IS.upsert_tracked(conn, "0xnew", {"verdict": "watch", "archetype": "directional",
                                      "archetype_pass": 1}, ts_ms=10)
    row = conn.execute("SELECT archetype_pass FROM hl_tracked_traders WHERE wallet='0xnew'").fetchone()
    assert row["archetype_pass"] == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_hyperlive_intel_store.py -q -k archetype_pass`
Expected: FAIL (`no such column: archetype_pass` / `_migrate` not defined).

- [ ] **Step 3: Implement migration + column writes**

In `sniper/hyperlive/intel_store.py`, add `_migrate` and call it from `ensure_schema` (after the `executescript`, before/around `conn.commit()`):

```python
def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent additive migrations. SQLite has no ADD COLUMN IF NOT EXISTS, so probe first."""
    for table in ("hl_tracked_traders", "hl_screened_traders"):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if "archetype_pass" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN archetype_pass INTEGER DEFAULT 0")
            # backfill: legacy phase-1 'copyable' rows ARE archetype-pass (not yet proven)
            conn.execute(f"UPDATE {table} SET archetype_pass=1 WHERE verdict='copyable'")
    conn.commit()
```

Add `_migrate(conn)` at the end of `ensure_schema`:

```python
    conn.commit()
    _migrate(conn)
```

Update `upsert_tracked` — add `archetype_pass` to `cols` and to the INSERT:

```python
    cols = ("verdict", "archetype", "taker_frac", "profit_factor", "win_rate", "n_closes",
            "month_roi", "realized", "label", "archetype_pass")
```

and in the INSERT branch add the column + value:

```python
        conn.execute(
            "INSERT INTO hl_tracked_traders(wallet, label, verdict, archetype, taker_frac, "
            "profit_factor, win_rate, n_closes, month_roi, realized, archetype_pass, added_ts_ms, updated_ts_ms) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (wallet, info.get("label"), info.get("verdict"), info.get("archetype"),
             info.get("taker_frac"), info.get("profit_factor"), info.get("win_rate"),
             info.get("n_closes"), info.get("month_roi"), info.get("realized"),
             int(bool(info.get("archetype_pass"))), ts_ms, ts_ms))
```

Update `upsert_screened` similarly — add `archetype_pass` to the column list, the `VALUES` tuple (one more `?`), the `ON CONFLICT … DO UPDATE SET` list (`archetype_pass=excluded.archetype_pass`), and pass `int(bool(info.get("archetype_pass")))`.

Update `get_screened` ordering so proven/watch sort above rejects independent of the soon-to-be-renamed verdict string:

```python
def get_screened(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM hl_screened_traders ORDER BY archetype_pass DESC, month_roi DESC").fetchall()]
```

- [ ] **Step 4: Run to verify it passes**

Run: `python3 -m pytest tests/test_hyperlive_intel_store.py -q`
Expected: PASS (all, including the existing roundtrip tests).

- [ ] **Step 5: Apply the migration to the live DB (one-time)**

```bash
python3 -c "import sniper.hyperlive.intel_store as IS; c=IS.connect('/home/bigpapi/Desktop/Sniper/data/hl_intel.sqlite'); IS.ensure_schema(c); print('migrated', [r[1] for r in c.execute('PRAGMA table_info(hl_tracked_traders)')])"
```
Expected: column list includes `archetype_pass`.

- [ ] **Step 6: Commit**

```bash
git add sniper/hyperlive/intel_store.py tests/test_hyperlive_intel_store.py
git commit -m "feat(hl-intel): archetype_pass column + idempotent migration/backfill; sort screened by it"
```

### Task 4: Compute `archetype_pass` on write (trader_intel)

**Files:**
- Modify: `sniper/hyperlive/trader_intel.py` (`copyability`, `assess_trader`)
- Test: `tests/test_hyperlive_trader_intel.py`

**Interfaces:**
- Produces: `copyability(...)` dict gains `"archetype_pass": bool` (True only for the directional phase-1 pass). `assess_trader` passes it through. **No verdict rename yet** (that is Task 8) — keeps this task isolated and reviewable.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_hyperlive_trader_intel.py`:

```python
def test_copyability_sets_archetype_pass_for_directional():
    import sniper.hyperlive.trader_intel as TI
    m = {"n_closes": 40, "profit_factor": 1.5, "top_coin_pnl_frac": 0.3, "single_fill_frac": 0.2}
    arch = {"archetype": "directional", "taker_frac": 0.8, "maker_frac": 0.2}
    out = TI.copyability(m, arch)
    assert out["archetype_pass"] is True

def test_copyability_archetype_pass_false_for_market_maker():
    import sniper.hyperlive.trader_intel as TI
    m = {"n_closes": 40, "profit_factor": 1.5, "top_coin_pnl_frac": 0.3, "single_fill_frac": 0.2}
    arch = {"archetype": "market_maker", "taker_frac": 0.2, "maker_frac": 0.8}
    assert TI.copyability(m, arch)["archetype_pass"] is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_hyperlive_trader_intel.py -q -k archetype_pass`
Expected: FAIL (`KeyError: 'archetype_pass'`).

- [ ] **Step 3: Implement — add `archetype_pass` to every `copyability` return**

In `sniper/hyperlive/trader_intel.py`, add `"archetype_pass": False` to each non-directional return dict and `"archetype_pass": True` to the directional return. E.g. the directional branch becomes:

```python
    if arch["archetype"] == "directional":
        return {"verdict": "copyable", "copyable": True, "archetype_pass": True,
                "reason": f"directional, taker {arch['taker_frac']:.0%}, PF {metrics['profit_factor']}, "
                          f"n={metrics['n_closes']} (phase-1: pre forward-follow proof)"}
```

and every other return (low_sample, market_maker, degen_leverage, unprofitable, mixed) gains `"archetype_pass": False`.

- [ ] **Step 4: Run to verify it passes**

Run: `python3 -m pytest tests/test_hyperlive_trader_intel.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/hyperlive/trader_intel.py tests/test_hyperlive_trader_intel.py
git commit -m "feat(hl-intel): copyability emits archetype_pass (phase-1 directional gate)"
```

### Task 5: Follow runner selects by `archetype_pass`, not `verdict=="copyable"` (THE data-feed fix)

**Files:**
- Modify: `scripts/run_hl_follow.py:43-44`
- Test: `tests/test_run_hl_follow_selection.py` (new)

**Interfaces:**
- Produces: `select_followable(tracked: list[dict]) -> list[dict]` — pure, returns rows with `archetype_pass==1` (back-compat: also accepts legacy `verdict=="copyable"`). `_cycle` calls it.

- [ ] **Step 1: Write the failing test**

Create `tests/test_run_hl_follow_selection.py`:

```python
import importlib

def test_select_followable_uses_archetype_pass():
    mod = importlib.import_module("scripts.run_hl_follow")
    tracked = [
        {"wallet": "0xa", "verdict": "watch", "archetype_pass": 1},      # renamed, still followable
        {"wallet": "0xb", "verdict": "market_maker", "archetype_pass": 0},
        {"wallet": "0xc", "verdict": "copyable", "archetype_pass": 1},    # pre-rename legacy
    ]
    got = {t["wallet"] for t in mod.select_followable(tracked)}
    assert got == {"0xa", "0xc"}


def test_select_followable_legacy_copyable_without_column():
    mod = importlib.import_module("scripts.run_hl_follow")
    # a row written before the migration has no archetype_pass key at all
    tracked = [{"wallet": "0xd", "verdict": "copyable"}]
    assert [t["wallet"] for t in mod.select_followable(tracked)] == ["0xd"]
```

(If `scripts/` is not importable as a package, the test runner already imports other `scripts.*` modules — confirm with `python3 -c "import scripts.run_hl_follow"`. If it fails, add an empty `scripts/__init__.py` in a preceding step and commit it with this task.)

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_run_hl_follow_selection.py -q`
Expected: FAIL (`select_followable` not defined).

- [ ] **Step 3: Implement the helper + use it**

In `scripts/run_hl_follow.py`, add above `_cycle`:

```python
def select_followable(tracked: list[dict]) -> list[dict]:
    """Follow the phase-1 archetype-pass cohort (watch ∪ proven) — NOT the display verdict.
    Keying off verdict=='copyable' would silently stop collection once we rename to 'watch'.
    Legacy rows (pre-migration, no archetype_pass) fall back to the old verdict."""
    return [t for t in tracked
            if t.get("archetype_pass") == 1 or t.get("verdict") == "copyable"]
```

Replace the selection line (currently `tracked = [t for t in IS.get_tracked(conn) if t.get("verdict") == "copyable"]`) with:

```python
    tracked = select_followable(IS.get_tracked(conn))
```

- [ ] **Step 4: Run to verify it passes**

Run: `python3 -m pytest tests/test_run_hl_follow_selection.py -q`
Expected: PASS.

- [ ] **Step 5: Verify the live runner still selects the current cohort**

```bash
python3 -c "import sniper.hyperlive.intel_store as IS, scripts.run_hl_follow as R; c=IS.connect('/home/bigpapi/Desktop/Sniper/data/hl_intel.sqlite'); print('followable:', len(R.select_followable(IS.get_tracked(c))))"
```
Expected: a non-zero count matching the current copyable cohort (proves the rename won't starve the feed).

- [ ] **Step 6: Commit**

```bash
git add scripts/run_hl_follow.py tests/test_run_hl_follow_selection.py
git commit -m "fix(hl-follow): select followables by archetype_pass (rename-safe data feed)"
```

> **Checkpoint:** Phase 2 complete. The live follow feed is now rename-safe. Safe to pause here and move to Spec A if desired; Phase 3 can land later without further follow-feed risk.

---

## Phase 3 — Phase-2 honesty gate, per-wallet stats, rename, wiring, UI

### Task 6: `follow_stats_by_wallet` in intel_store

**Files:**
- Modify: `sniper/hyperlive/intel_store.py`
- Test: `tests/test_hyperlive_intel_store.py`

**Interfaces:**
- Produces: `follow_stats_by_wallet(conn) -> dict[str, dict]` keyed by wallet, each value `{n_closed, mean_net_bps, top_asset_pnl_frac, top_asset_count_frac, last_close_ts, by_cluster}` where `by_cluster` maps `"<coin>|<utc_day>" -> mean_net_bps` (for the asset×day clustered-t computed in Task 7).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_hyperlive_intel_store.py`:

```python
def test_follow_stats_by_wallet(tmp_path):
    import sniper.hyperlive.intel_store as IS
    conn = IS.connect(str(tmp_path / "intel.sqlite"))
    IS.ensure_schema(conn)
    day = 86_400_000
    rows = [("0xa", "BTC", 50.0, day + 1), ("0xa", "BTC", -10.0, day + 2),
            ("0xa", "ETH", 30.0, 2 * day + 1), ("0xb", "SOL", 5.0, day + 1)]
    for w, coin, net, ts in rows:
        fid = IS.open_follow(conn, {"wallet": w, "coin": coin, "side": "long", "entry_mark": 1.0}, ts_ms=ts)
        IS.close_follow(conn, fid, exit_mark=1.0, net_bps=net, reason="mirror", ts_ms=ts)
    stats = IS.follow_stats_by_wallet(conn)
    assert stats["0xa"]["n_closed"] == 3
    assert abs(stats["0xa"]["mean_net_bps"] - round((50 - 10 + 30) / 3, 3)) < 1e-9
    assert stats["0xa"]["last_close_ts"] == 2 * day + 1
    # BTC contributes 50+(-10)=40 of 70 positive pnl -> top_asset_pnl_frac ~ 40/80
    assert 0.0 <= stats["0xa"]["top_asset_pnl_frac"] <= 1.0
    assert set(stats["0xa"]["by_cluster"]) == {"BTC|1", "ETH|2"}
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_hyperlive_intel_store.py -q -k follow_stats_by_wallet`
Expected: FAIL (`follow_stats_by_wallet` not defined).

- [ ] **Step 3: Implement**

Add to `sniper/hyperlive/intel_store.py`:

```python
def follow_stats_by_wallet(conn) -> dict:
    """Per-wallet closed-follow stats for the phase-2 gate. Clusters net_bps by coin×UTC-day."""
    from collections import defaultdict
    rows = conn.execute(
        "SELECT wallet, coin, net_bps, exit_ts_ms FROM hl_follow_positions "
        "WHERE status='closed' AND net_bps IS NOT NULL").fetchall()
    agg = defaultdict(lambda: {"nets": [], "by_coin_pnl": defaultdict(float),
                               "by_coin_cnt": defaultdict(int),
                               "by_cluster_sum": defaultdict(float),
                               "by_cluster_cnt": defaultdict(int), "last_close_ts": 0})
    for r in rows:
        a = agg[r["wallet"]]
        net = float(r["net_bps"])
        a["nets"].append(net)
        a["by_coin_pnl"][r["coin"]] += net
        a["by_coin_cnt"][r["coin"]] += 1
        ck = f"{r['coin']}|{int(r['exit_ts_ms']) // 86_400_000}"
        a["by_cluster_sum"][ck] += net
        a["by_cluster_cnt"][ck] += 1
        a["last_close_ts"] = max(a["last_close_ts"], int(r["exit_ts_ms"]))
    out = {}
    for w, a in agg.items():
        n = len(a["nets"])
        pos_total = sum(v for v in a["by_coin_pnl"].values() if v > 0) or 1e-9
        top_pnl = max((v for v in a["by_coin_pnl"].values()), default=0.0)
        top_cnt = max(a["by_coin_cnt"].values(), default=0)
        by_cluster = {k: a["by_cluster_sum"][k] / a["by_cluster_cnt"][k] for k in a["by_cluster_sum"]}
        out[w] = {
            "n_closed": n,
            "mean_net_bps": round(sum(a["nets"]) / n, 3) if n else 0.0,
            "top_asset_pnl_frac": round(max(0.0, top_pnl) / pos_total, 3),
            "top_asset_count_frac": round(top_cnt / n, 3) if n else 0.0,
            "last_close_ts": a["last_close_ts"],
            "by_cluster": by_cluster,
        }
    return out
```

- [ ] **Step 4: Run to verify it passes**

Run: `python3 -m pytest tests/test_hyperlive_intel_store.py -q -k follow_stats_by_wallet`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/hyperlive/intel_store.py tests/test_hyperlive_intel_store.py
git commit -m "feat(hl-intel): follow_stats_by_wallet (per-wallet net + coin×day clusters)"
```

### Task 7: `copyability_phase2` + `effective_verdict` (the honesty gate)

**Files:**
- Modify: `sniper/hyperlive/trader_intel.py`
- Test: `tests/test_hyperlive_trader_intel.py`

**Interfaces:**
- Consumes: a per-wallet stats dict from `follow_stats_by_wallet`.
- Produces:
  - `copyability_phase2(follow_stats: dict | None) -> dict` → `{"phase2": "proven"|"watch", "reason": str, "clustered_t": float, "n_closed": int}`.
  - `effective_verdict(stored_verdict: str, archetype_pass, follow_stats: dict | None) -> str` → `"proven"` / `"watch"` / the stored reject verdict.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_hyperlive_trader_intel.py`:

```python
def test_phase2_proven_when_significant_positive():
    import sniper.hyperlive.trader_intel as TI
    # 32 positive closes spread across many coin×day clusters -> significant, low concentration
    by_cluster = {f"C{i}|{i}": 40.0 for i in range(32)}
    stats = {"n_closed": 32, "mean_net_bps": 40.0, "top_asset_pnl_frac": 0.1,
             "top_asset_count_frac": 0.1, "last_close_ts": 9_999_999_999_999, "by_cluster": by_cluster}
    out = TI.copyability_phase2(stats)
    assert out["phase2"] == "proven"
    assert out["clustered_t"] > TI.PHASE2_MIN_T

def test_phase2_watch_when_thin():
    import sniper.hyperlive.trader_intel as TI
    stats = {"n_closed": 5, "mean_net_bps": 40.0, "top_asset_pnl_frac": 0.1,
             "top_asset_count_frac": 0.1, "last_close_ts": 9_999_999_999_999, "by_cluster": {"A|1": 40.0}}
    assert TI.copyability_phase2(stats)["phase2"] == "watch"

def test_phase2_watch_when_over_concentrated():
    import sniper.hyperlive.trader_intel as TI
    by_cluster = {f"C{i}|{i}": 40.0 for i in range(40)}
    stats = {"n_closed": 40, "mean_net_bps": 40.0, "top_asset_pnl_frac": 0.9,   # one coin carries it
             "top_asset_count_frac": 0.2, "last_close_ts": 9_999_999_999_999, "by_cluster": by_cluster}
    assert TI.copyability_phase2(stats)["phase2"] == "watch"

def test_effective_verdict_combines():
    import sniper.hyperlive.trader_intel as TI
    by_cluster = {f"C{i}|{i}": 40.0 for i in range(32)}
    proven_stats = {"n_closed": 32, "mean_net_bps": 40.0, "top_asset_pnl_frac": 0.1,
                    "top_asset_count_frac": 0.1, "last_close_ts": 9_999_999_999_999, "by_cluster": by_cluster}
    assert TI.effective_verdict("copyable", 1, proven_stats) == "proven"
    assert TI.effective_verdict("copyable", 1, None) == "watch"
    assert TI.effective_verdict("market_maker", 0, None) == "market_maker"
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_hyperlive_trader_intel.py -q -k phase2`
Expected: FAIL (`copyability_phase2` not defined).

- [ ] **Step 3: Implement**

Add to `sniper/hyperlive/trader_intel.py` (constants near the top with the others):

```python
# Phase-2 (forward-follow proof) — frozen.
PHASE2_MIN_CLOSES = 30
PHASE2_MIN_T = 2.0
PHASE2_MAX_CONCENTRATION = 0.40   # max share of PnL OR of closes from one asset
PHASE2_STALE_DAYS = 14
```

and the functions:

```python
import math


def _clustered_t(by_cluster: dict) -> float:
    """One-sample t over cluster (coin×day) mean net_bps; H0: mean <= 0."""
    vals = list(by_cluster.values())
    n = len(vals)
    if n < 2:
        return 0.0
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / (n - 1)
    if var <= 0:
        return 0.0 if mean <= 0 else 999.0
    return mean / math.sqrt(var / n)


def copyability_phase2(follow_stats: dict | None, *, now_ms: int | None = None) -> dict:
    """Promote archetype-pass -> proven only on a real forward-follow record."""
    s = follow_stats or {}
    n = int(s.get("n_closed", 0))
    if n < PHASE2_MIN_CLOSES:
        return {"phase2": "watch", "n_closed": n, "clustered_t": 0.0,
                "reason": f"{n} closes < {PHASE2_MIN_CLOSES}"}
    if max(s.get("top_asset_pnl_frac", 1.0), s.get("top_asset_count_frac", 1.0)) > PHASE2_MAX_CONCENTRATION:
        return {"phase2": "watch", "n_closed": n, "clustered_t": 0.0,
                "reason": "single-asset concentration > 40%"}
    t = _clustered_t(s.get("by_cluster", {}))
    if s.get("mean_net_bps", 0.0) <= 0 or t <= PHASE2_MIN_T:
        return {"phase2": "watch", "n_closed": n, "clustered_t": round(t, 2),
                "reason": f"net {s.get('mean_net_bps')}bps, t={t:.2f} (need >{PHASE2_MIN_T})"}
    if now_ms is not None and (now_ms - int(s.get("last_close_ts", 0))) > PHASE2_STALE_DAYS * 86_400_000:
        return {"phase2": "watch", "n_closed": n, "clustered_t": round(t, 2), "reason": "stale (>14d)"}
    return {"phase2": "proven", "n_closed": n, "clustered_t": round(t, 2),
            "reason": f"forward net +{s.get('mean_net_bps')}bps, t={t:.2f}, n={n}"}


def effective_verdict(stored_verdict: str, archetype_pass, follow_stats: dict | None,
                      *, now_ms: int | None = None) -> str:
    """Two-tier display verdict. archetype-pass -> proven|watch; else the stored reject verdict."""
    if archetype_pass in (1, True) or stored_verdict == "copyable":
        return copyability_phase2(follow_stats, now_ms=now_ms)["phase2"]
    return stored_verdict
```

- [ ] **Step 4: Run to verify it passes**

Run: `python3 -m pytest tests/test_hyperlive_trader_intel.py -q -k phase2`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/hyperlive/trader_intel.py tests/test_hyperlive_trader_intel.py
git commit -m "feat(hl-intel): copyability_phase2 + effective_verdict (forward-follow honesty gate)"
```

### Task 8: Rename phase-1 verdict `copyable → watch`

**Files:**
- Modify: `sniper/hyperlive/trader_intel.py` (the directional branch of `copyability`)
- Test: `tests/test_hyperlive_trader_intel.py`

**Interfaces:**
- Produces: `copyability(...)` directional branch returns `verdict="watch"` (archetype_pass stays True). `"copyable"` is no longer emitted by new writes; `effective_verdict` (Task 7) and the migration backfill (Task 3) handle legacy.

- [ ] **Step 1: Update the test from Task 4 to expect `watch`**

In `tests/test_hyperlive_trader_intel.py`, change `test_copyability_sets_archetype_pass_for_directional` to also assert the new verdict:

```python
    assert out["archetype_pass"] is True
    assert out["verdict"] == "watch"          # phase-1 directional is 'watch', not 'copyable'
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_hyperlive_trader_intel.py -q -k archetype_pass`
Expected: FAIL (`assert 'copyable' == 'watch'`).

- [ ] **Step 3: Implement the rename**

In `copyability`, the directional branch `verdict` becomes `"watch"`:

```python
    if arch["archetype"] == "directional":
        return {"verdict": "watch", "copyable": False, "archetype_pass": True,
                "reason": f"directional, taker {arch['taker_frac']:.0%}, PF {metrics['profit_factor']}, "
                          f"n={metrics['n_closes']} (phase-1 archetype-pass; unproven until forward-follow)"}
```

(`copyable: False` because phase-1 is no longer a copy recommendation; proof is phase-2.)

- [ ] **Step 4: Run to verify it passes**

Run: `python3 -m pytest tests/test_hyperlive_trader_intel.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/hyperlive/trader_intel.py tests/test_hyperlive_trader_intel.py
git commit -m "feat(hl-intel): rename phase-1 directional verdict copyable->watch (proof is phase-2)"
```

### Task 9: Wire two-tier verdict + per-wallet follow stats into the routes

**Files:**
- Modify: `sniper/routes/api_hyperlive.py` (`analyzer`, `intel`, `trader`)
- Test: `tests/test_api_hyperlive.py`

**Interfaces:**
- Consumes: `IS.follow_stats_by_wallet`, `TI.effective_verdict`.
- Produces: `/api/hyperlive/analyzer` traders each gain `effective_verdict` + `follow` (per-wallet stats or null). `/api/hyperlive/trader/<wallet>` gains `effective_verdict` + `follow`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_api_hyperlive.py`:

```python
def test_analyzer_emits_two_tier_verdict(tmp_path, monkeypatch):
    import sniper.hyperlive.intel_store as IS
    from sniper.routes import api_hyperlive as AH
    db = str(tmp_path / "hl_intel.sqlite")
    conn = IS.connect(db); IS.ensure_schema(conn)
    IS.upsert_screened(conn, "0xa", {"verdict": "watch", "archetype": "directional",
                                     "archetype_pass": 1, "month_roi": 0.5}, ts_ms=1)
    monkeypatch.setattr(AH, "INTEL_DB", db)
    app = _make_app(AH.create_api_hyperlive_blueprint(db_path="/nonexistent.sqlite"))
    body = app.test_client().get("/api/hyperlive/analyzer").get_json()
    t = [x for x in body["traders"] if x["wallet"] == "0xa"][0]
    assert t["effective_verdict"] == "watch"        # archetype-pass but no follow proof yet
    assert "follow" in t                              # per-wallet stats key present (null/empty ok)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_api_hyperlive.py -q -k two_tier`
Expected: FAIL (`KeyError: 'effective_verdict'`).

- [ ] **Step 3: Implement — enrich the analyzer payload**

In `api_hyperlive.py`, the `analyzer()` handler, replace the `traders` assembly:

```python
        import time
        from sniper.hyperlive import trader_intel as TI
        traders = IS.get_screened(conn)
        fstats = IS.follow_stats_by_wallet(conn)
        now_ms = int(time.time() * 1000)
        for t in traders:
            fs = fstats.get(t["wallet"])
            t["follow"] = fs
            t["effective_verdict"] = TI.effective_verdict(
                t.get("verdict"), t.get("archetype_pass"), fs, now_ms=now_ms)
        return jsonify({"available": True, "traders": traders,
                        "screen": IS.latest_screen_run(conn),
                        "copy_targets": [t["wallet"] for t in IS.get_copy_targets(conn)],
                        "copy": IS.copy_summary(conn)})
```

In `trader()` (the live single-wallet route), after `v = TI.assess_trader(fills)` add per-wallet follow stats + effective verdict (best-effort; the intel DB may be absent):

```python
        v["available"] = True
        v["wallet"] = wallet
        v["breakdown"] = _trader_breakdown(fills)
        try:
            import time
            from sniper.hyperlive import intel_store as IS
            if os.path.exists(INTEL_DB):
                ic = IS.connect(INTEL_DB)
                try:
                    fs = IS.follow_stats_by_wallet(ic).get(wallet)
                finally:
                    ic.close()
                v["follow"] = fs
                v["effective_verdict"] = TI.effective_verdict(
                    v.get("verdict"), v.get("archetype_pass"), fs, now_ms=int(time.time() * 1000))
        except Exception:  # noqa: BLE001 — enrichment is optional, never break the route
            v.setdefault("effective_verdict", v.get("verdict"))
        return jsonify(v)
```

In `intel()`, enrich `out["cohort"]` the same way as `analyzer` (loop adding `follow` + `effective_verdict`).

- [ ] **Step 4: Run to verify it passes**

Run: `python3 -m pytest tests/test_api_hyperlive.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/routes/api_hyperlive.py tests/test_api_hyperlive.py
git commit -m "feat(hl-api): emit effective_verdict (two-tier) + per-wallet follow stats"
```

### Task 10: UI — two-tier verdict + copy-action gated to `proven`

**Files:**
- Modify: `static/app.js` (`_vCol`, `_screenHeader`, `_list`, `_detail`)

**Interfaces:**
- Consumes: `t.effective_verdict`, `t.follow` (from Task 9).

- [ ] **Step 1: Use `effective_verdict` everywhere a verdict is shown + add `proven`/`watch` colors**

In `_vCol` (line ~5513):

```javascript
    _vCol(v) {
      return v === 'proven' ? '#3fb950' : (v === 'watch' ? '#58a6ff'
        : (v === 'market_maker' ? '#d29922' : (v === 'degen_leverage' ? '#f85149' : '#8b949e')));
    },
```

In `_list` (line ~5530), use `effective_verdict` and surface the follow proof:

```javascript
      return rows.map(t => {
        const v = t.effective_verdict || t.verdict;
        const isCopy = this._copyTargets.includes(t.wallet);
        const f = t.follow;
        const proof = f && f.n_closed ? ` · follow ${(f.mean_net_bps>=0?'+':'')+f.mean_net_bps}bps/n${f.n_closed}` : '';
        return `<div class="analyzer-report-item" data-wallet="${esc(t.wallet)}" style="cursor:pointer;padding:9px 10px;border-bottom:1px solid #21262d">
          <div style="display:flex;justify-content:space-between;align-items:center;gap:8px">
            <span style="font-family:monospace;font-size:12px">${esc(t.wallet.slice(0, 12))}…${isCopy ? ' <span style="color:#bc8cff">●copying</span>' : ''}</span>
            <span class="badge" style="background:${this._vCol(v)}22;color:${this._vCol(v)};font-size:10px">${esc(v)}</span>
          </div>
          <div class="text-muted" style="font-size:11px;margin-top:3px">${esc(t.archetype || '')} · PF ${t.profit_factor ?? '–'} · WR ${t.win_rate != null ? Math.round(t.win_rate * 100) + '%' : '–'} · n ${t.n_closes ?? '–'}${proof}</div>
        </div>`;
      }).join('');
```

In `_screenHeader` (line ~5520), count `proven`/`watch` instead of `copyable` (read from the enriched cohort isn't available here; keep the screen tally but relabel the copyable count as "archetype-pass"):

```javascript
      const cop = v.copyable || 0, rej = Object.keys(v).filter(k => k !== 'copyable').reduce((a, k) => a + v[k], 0);
      // 'copyable' in the stored screen tally == phase-1 archetype-pass; label honestly
```
and change the rendered label `${cop} copyable` → `${cop} archetype-pass`.

- [ ] **Step 2: Gate the copy button to `proven` in `_detail` (line ~5569)**

```javascript
      const v = d.effective_verdict || d.verdict;
      const copyBtn = v === 'proven'
        ? `<button id="hl-copy-btn" class="btn ${isCopy ? 'btn-secondary' : 'btn-primary'}" data-active="${isCopy ? '1' : '0'}" style="margin-top:4px">${isCopy ? 'Stop mirroring' : 'Copy this wallet 24/7'}</button>`
        : (v === 'watch'
          ? `<div class="text-muted" style="font-size:11px;margin-top:4px">Track (unproven) — archetype-pass; forward-follow accruing${d.follow && d.follow.n_closed ? ` (n=${d.follow.n_closed}, ${(d.follow.mean_net_bps>=0?'+':'')+d.follow.mean_net_bps}bps)` : ''}. No copy action until proven.</div>`
          : `<div class="text-muted" style="font-size:11px;margin-top:4px">Not copyable — ${esc(d.reason || '')}.</div>`);
```

Also update the badge in `_detail` to use `v` (the effective verdict) instead of `d.verdict`, and update the footnote (line ~5582) to: `"proven" = positive forward-follow net (n≥30, t>2); "watch" = archetype-pass, unproven.`

- [ ] **Step 3: Bump the cache version**

Find the asset cache-buster (per memory, `cache v=…`, currently 127) in `templates/index.html` and increment it (e.g. `v=128`) so clients pull the new JS.

- [ ] **Step 4: Manual verification**

Restart the leadlag web server, open HL Analyzer: confirm `watch` rows show blue + "Track (unproven)", no `proven` rows yet (none have ≥30 follows), and pasting a wallet shows the honest footnote. No "Copy this wallet 24/7" button on `watch`.

- [ ] **Step 5: Commit**

```bash
git add static/app.js templates/index.html
git commit -m "feat(hl-analyzer-ui): two-tier verdict (proven/watch) + copy action gated to proven"
```

### Task 11: Full focused suite + screen-writer archetype_pass check

**Files:**
- Verify: `scripts/screen_hl_smart_money.py` passes `archetype_pass` through to `upsert_*`
- Test: full focused run

- [ ] **Step 1: Confirm the screen writer forwards `archetype_pass`**

Run: `grep -n "archetype_pass\|upsert_tracked\|upsert_screened\|copyability\|assess_trader" scripts/screen_hl_smart_money.py`
If it builds the `info` dict by cherry-picking keys (not passing the full `assess_trader`/`copyability` dict), add `"archetype_pass": v.get("archetype_pass")` to that dict. If it passes the full dict, no change. Commit only if changed.

- [ ] **Step 2: Run the full focused suite**

Run: `python3 -m pytest tests/test_hyperlive_intel_store.py tests/test_hyperlive_trader_intel.py tests/test_api_hyperlive.py tests/test_run_hl_follow_selection.py -q`
Expected: all PASS.

- [ ] **Step 3: Schema-ref validator (CI parity)**

Run: `python3 scripts/validate_schema_refs.py` (from main worktree if that's where the validator lives; otherwise skip — this branch's CI runs it).
Expected: no violations.

- [ ] **Step 4: Final commit (if Step 1 changed the writer)**

```bash
git add scripts/screen_hl_smart_money.py
git commit -m "fix(hl-screen): forward archetype_pass to the intel store"
```

---

## Self-Review

**Spec coverage:** §2 defects 1-3 → Task 1; test gaps → Tasks 2, 9, 11. §3 D1 archetype_pass + follow-fix → Tasks 3-5. D1b migration → Task 3. D2 UI/copy-gating → Task 10. D3 per-wallet stats + phase-2 constants → Tasks 6-7. Rename → Task 8. Defect 4 (dedupe) intentionally deferred per spec ("only if cheap"). ✅
**Placeholder scan:** no TBD/TODO; every code step has complete code. ✅
**Type consistency:** `archetype_pass` int 0/1 in DB, bool in trader_intel, accepted as `in (1, True)` by `effective_verdict`; `follow_stats_by_wallet` keys match `copyability_phase2` reads (`n_closed`, `mean_net_bps`, `top_asset_pnl_frac`, `top_asset_count_frac`, `last_close_ts`, `by_cluster`); `effective_verdict`/`select_followable` names consistent across tasks. ✅
