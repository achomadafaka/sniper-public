# Memecoin Cockpit — PR5 (Wallet × Arm Matrix) Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Turn the "Wallet × Arm" subtab (currently a flat wallet ranking) into a **matrix**: rows = wallets, columns = exit-policy arms, cell = closed copy PnL + n, sorted by total PnL, thin cells flagged — answering "which wallet is +EV under which exit policy."

**Architecture:** New read-only db aggregation `trader_arm_matrix(conn)` (SELECT over `memecoin_positions`, no writer) + a `GET /api/memecoin/traders/arm-matrix` endpoint + a contract test. `_loadTraders` renders the matrix into `#mc-traders`.

**Tech Stack:** Python (`sniper/persistence/db_memecoin.py`, `sniper/routes/api_memecoin.py`), pytest, vanilla JS. **Not static-only** (adds a read-only endpoint) — includes a test + schema validation. Verify: pytest, `node --check`, authed curl, funded-path guard.

**Spec:** `docs/superpowers/specs/2026-07-16-memecoin-operator-cockpit-design.md` §8 (Wallet × Arm). v1 = the core matrix (PnL/n per cell, small-n flagged, total column); chain/source splits + coin overlap deferred (stated).

## Global Constraints

- **Funded path untouched.** New code is a read-only SELECT + a GET route + a renderer. The funded runner never calls it. Guard: `git diff --name-only` lists only `sniper/persistence/db_memecoin.py`, `sniper/routes/api_memecoin.py`, `tests/test_memecoin_arm_matrix.py`, `static/app.js`, `templates/index.html`. Runner healthy on pinned release before/after.
- **Read-only.** No INSERT/UPDATE/DELETE. `execution_mode='paper'`, `is_open=0` only.
- **No fabricated data.** Empty cells render `·`; thin cells (n < min_n) dimmed with a tooltip.
- **Verified columns** (via brain_index): `memecoin_positions.source_user_id, source_handle, strategy, is_open, realized_pnl, execution_mode` — all exist, all nullable.

---

### Task 1: Read-only aggregation `trader_arm_matrix`

**Files:** Modify `sniper/persistence/db_memecoin.py` — add after `trader_performance` (near line 3547).

- [ ] **Step 1: Write the failing test.** Create `tests/test_memecoin_arm_matrix.py`:

```python
import sqlite3
from sniper.persistence import db_memecoin as m


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _pos(c, uid, handle, strategy, realized, is_open=0, mode="paper"):
    c.execute(
        "INSERT INTO memecoin_positions(source_user_id, source_handle, mint, strategy, "
        "execution_mode, is_open, realized_pnl, network_id) VALUES(?,?,?,?,?,?,?,?)",
        (uid, handle, "MINT" + strategy, strategy, mode, is_open, realized, "solana"))


def test_arm_matrix_groups_by_wallet_and_arm():
    c = _conn()
    _pos(c, "u1", "alice", "control", 10.0)
    _pos(c, "u1", "alice", "control", -4.0)
    _pos(c, "u1", "alice", "scalp_c2", -20.0)
    _pos(c, "u2", "bob", "control", 2.0)
    _pos(c, "u2", "bob", "control", 3.0, is_open=1)          # open -> excluded
    _pos(c, "u1", "alice", "control", 99.0, mode="real")      # real -> excluded
    c.commit()
    r = m.trader_arm_matrix(c)
    assert "control" in r["arms"] and "scalp_c2" in r["arms"]
    w = {x["source_user_id"]: x for x in r["wallets"]}
    assert round(w["u1"]["by_arm"]["control"]["pnl"], 2) == 6.0      # 10 - 4
    assert w["u1"]["by_arm"]["control"]["closed"] == 2
    assert w["u1"]["by_arm"]["control"]["wins"] == 1
    assert round(w["u1"]["by_arm"]["scalp_c2"]["pnl"], 2) == -20.0
    assert round(w["u1"]["total_pnl"], 2) == -14.0                    # 6 - 20
    assert w["u2"]["by_arm"]["control"]["closed"] == 1               # open + real excluded
    # sorted by total_pnl desc -> u2 (+2) before u1 (-14)
    assert [x["source_user_id"] for x in r["wallets"]] == ["u2", "u1"]


def test_arm_matrix_never_writes():
    c = _conn()
    _pos(c, "u1", "alice", "control", 5.0)
    c.commit()
    before = c.total_changes
    m.trader_arm_matrix(c)
    assert c.total_changes == before
```

- [ ] **Step 2: Run it — expect fail** (`trader_arm_matrix` undefined): `python3 -m pytest tests/test_memecoin_arm_matrix.py -q` → FAIL.

- [ ] **Step 3: Implement** in `db_memecoin.py` after `trader_performance`:

```python
def trader_arm_matrix(conn, mode="paper", min_n=3):
    """Wallet x arm closed-copy matrix (READ-ONLY). Rows = curated wallets, columns = strategy arms;
    each cell = summed realized PnL + closed count + wins over CLOSED paper positions. Answers
    'which wallet is +EV under which exit policy'. Sorted by total closed PnL desc. `min_n` marks
    thin cells for the UI. Scoped to `mode` (default 'paper', the research board)."""
    ARM_ORDER = {"control": 0, "real_shadow": 1, "scalp_c2": 2, "scalp_c": 3,
                 "alpha_b": 4, "momentum_a": 5, "trail_d": 6}
    agg, arms = {}, set()
    for r in conn.execute(
        "SELECT source_user_id, source_handle, strategy, realized_pnl FROM memecoin_positions "
        "WHERE execution_mode=? AND is_open=0 AND realized_pnl IS NOT NULL", (mode,)):
        uid = r["source_user_id"]
        arm = r["strategy"] or "control"
        rp = r["realized_pnl"]
        arms.add(arm)
        a = agg.setdefault(uid, {"source_user_id": uid, "handle": r["source_handle"],
                                 "by_arm": {}, "total_pnl": 0.0, "total_closed": 0})
        if a["handle"] is None:
            a["handle"] = r["source_handle"]
        cell = a["by_arm"].setdefault(arm, {"pnl": 0.0, "closed": 0, "wins": 0})
        cell["pnl"] += rp
        cell["closed"] += 1
        cell["wins"] += 1 if rp > 0 else 0
        a["total_pnl"] += rp
        a["total_closed"] += 1
    wallets = sorted(agg.values(), key=lambda w: w["total_pnl"], reverse=True)
    arms_sorted = sorted(arms, key=lambda x: ARM_ORDER.get(x, 9))
    return {"mode": mode, "min_n": min_n, "arms": arms_sorted, "wallets": wallets}
```

- [ ] **Step 4: Run test — expect pass:** `python3 -m pytest tests/test_memecoin_arm_matrix.py -q` → PASS.
- [ ] **Step 5: Regenerate brain index** (new function touches a table): `python3 scripts/brain_index.py generate` (only if deps index changes; commit if so).
- [ ] **Step 6: Commit.** `git add sniper/persistence/db_memecoin.py tests/test_memecoin_arm_matrix.py && git commit -m "feat(memecoin): read-only trader_arm_matrix aggregation + tests (PR5/1)"`

---

### Task 2: Endpoint `/traders/arm-matrix`

**Files:** Modify `sniper/routes/api_memecoin.py` — add beside the other `traders/*` routes (after `traders_performance`, ~line 934).

- [ ] **Step 1: Add the route:**

```python
    @bp.get("/api/memecoin/traders/arm-matrix")
    def traders_arm_matrix():
        # Wallet x arm closed-copy PnL matrix (read-only). rows=wallets, cols=exit-policy arms.
        return jsonify(m.trader_arm_matrix(conn))
```

- [ ] **Step 2: Smoke it** against the live DB (read-only throwaway app or curl after restart in Task 4). Expect `{arms:[...], wallets:[...], min_n:3}`.
- [ ] **Step 3: Commit.** `git add sniper/routes/api_memecoin.py && git commit -m "feat(memecoin): /traders/arm-matrix read-only endpoint (PR5/2)"`

---

### Task 3: Render the matrix in `_loadTraders`

**Files:** Modify `static/app.js` — replace the `_loadTraders` method body (fetch + table).

- [ ] **Step 1: Replace `_loadTraders`** with the matrix renderer:

```javascript
    async _loadTraders() {
      const el = document.getElementById('mc-traders');
      if (!el) return;
      let d;
      try { d = await fetchJson('/api/memecoin/traders/arm-matrix', { suppressErrorToast: true }); }
      catch (e) { el.innerHTML = `<div class="text-muted">Wallet × arm matrix unavailable: ${esc(String(e && e.message || e))}</div>`; return; }
      const arms = d.arms || [], wallets = d.wallets || [], minN = d.min_n || 3;
      const armLabel = a => ({ real_shadow: 'shadow', momentum_a: 'mom', alpha_b: 'alpha', scalp_c2: 'scalp2', scalp_c: 'scalp', trail_d: 'trail', control: 'ctrl' })[a] || a;
      const cell = c => {
        if (!c || !c.closed) return '<td class="num" style="opacity:.3">·</td>';
        const thin = c.closed < minN;
        return `<td class="num" style="color:${this._col(c.pnl)}${thin ? ';opacity:.45' : ''}" title="${c.closed} closed · ${c.wins} win${c.wins === 1 ? '' : 's'}${thin ? ' · thin sample (<' + minN + ')' : ''}">${this._money(c.pnl)}<span class="text-muted" style="font-size:9px">/${c.closed}</span></td>`;
      };
      const rows = wallets.map(w => `<tr>
        <td title="${esc(w.source_user_id || '')}">${esc(w.handle || w.source_user_id || '—')}</td>
        ${arms.map(a => cell(w.by_arm[a])).join('')}
        <td class="num" style="color:${this._col(w.total_pnl)};font-weight:600">${this._money(w.total_pnl)}<span class="text-muted" style="font-size:9px">/${w.total_closed}</span></td>
      </tr>`).join('');
      el.innerHTML = `<div class="text-muted" style="font-size:11px;margin-bottom:6px">Closed copy PnL by exit-policy arm (paper). Each wallet's arms share the SAME entries and differ only on exit → the cell spread is the exit-policy effect. Thin cells (n&lt;${minN}) dimmed; · = no closes. Sorted by total PnL.</div>
        <div class="table-wrapper" style="overflow-x:auto"><table class="data-table compact">
          <thead><tr><th>Wallet</th>${arms.map(a => `<th class="num" title="${esc(a)}">${esc(armLabel(a))}</th>`).join('')}<th class="num">Total</th></tr></thead>
          <tbody>${rows || `<tr><td colspan="${arms.length + 2}" class="text-muted">No closed copy history yet.</td></tr>`}</tbody></table></div>`;
    },
```

- [ ] **Step 2:** `node --check static/app.js` → OK.

---

### Task 4: Cache-bust + verification gate

**Files:** Modify `templates/index.html` (`app.js?v=168` → `?v=169`).

- [ ] **Step 1:** Bump `app.js?v=168` → `?v=169`; `grep -c 'app.js?v=169' templates/index.html` → 1.
- [ ] **Step 2:** `python3 -m pytest tests/test_memecoin_arm_matrix.py -q` → PASS; `node --check static/app.js` → OK; `python3 scripts/validate_schema_refs.py --files docs/superpowers/plans/2026-07-16-memecoin-cockpit-pr5-wallet-arm.md tests/test_memecoin_arm_matrix.py` → clean.
- [ ] **Step 3: Funded-path guard.** `git diff --name-only origin/main...HEAD` lists only the 5 files above (+ plan/index).
- [ ] **Step 4: Runner healthy.** Single runner instance on pinned `f7cb9463`, `auth_ok=true`.
- [ ] **Step 5: Restart web (by PID), authed-curl verify** `/api/memecoin/traders/arm-matrix` returns `{arms, wallets, min_n}` with wallets>0; served `app.js?v=169` contains `arm-matrix` fetch + `by_arm`; index links v169.
- [ ] **Step 6: Visual (desktop + mobile).** Playwright Wallets › Wallet × Arm: matrix renders (wallet rows × arm cols + Total), colored PnL cells with /n, thin cells dimmed, horizontal scroll on 390px, sorted by Total.

---

## Self-Review

- **Spec coverage (§8):** wallet × arm matrix, cells = realized PnL / n, small-n flagged, sorted — delivered. Chain/source splits + wallet↔coin overlap deferred (stated). This completes the cockpit roadmap (PR0–PR5).
- **Placeholder scan:** db function, endpoint, test, and renderer are complete verbatim.
- **Read-only proof:** `test_arm_matrix_never_writes` asserts `total_changes` unchanged; query is SELECT-only.
- **Consistency:** endpoint mirrors sibling `traders/*` routes; renderer reuses `_col`/`_money`/`esc`/`data-table compact`; ARM_ORDER matches PR4's `_armRank`.
- **Risk:** low-medium — a new read-only endpoint (funded path never calls it) + a renderer swap; covered by a contract + no-write test.
