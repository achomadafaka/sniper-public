# HL Liquidation Tracker Tab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a dedicated UI tab showing Hyperliquid whale positions sorted by liquidation proximity, with threat banner, position table, and monitoring panel.

**Architecture:** Endpoints added to existing `routes/api_strategies.py` (no new blueprint — follows existing ownership pattern). Real top-level tab on both desktop and mobile. Uses existing `tabPoller` for refresh (90s cadence). Monitoring-only UI — no fake decisioning before Phase 5B strategy exists.

**Tech Stack:** Flask, SQLite, vanilla JS, existing Sniper CSS design system.

**Spec:** `docs/superpowers/specs/2026-03-27-hl-liquidation-tab-design.md`

---

## Architecture Decisions (from review)

1. **No custom setInterval.** Tab uses `tabPoller` (90s, app.js:6009). Same as all other tabs.
2. **Real top-level tab everywhere.** Desktop nav + mobile bottom nav + `VALID_TABS` set + route persistence + `mobileRefresh` injection. No desktop-only compromise.
3. **Endpoints in `routes/api_strategies.py`.** No separate `routes/api_algorithms.py` — one HL monitor surface doesn't justify a new blueprint family. Uses existing `ctx["conn"]` pattern.
4. **Monitoring only.** No "SIGNAL READY" or counter-trade advice. Shows: threat level (distance-based), position table, gate status (what conditions are met/unmet). Brain panel says "Phase 5B strategy not yet built" for the execution section.
5. **Shell wiring tested.** `VALID_TABS` updated, nav activation, route persistence, mobile refresh all verified.

---

## File Map

| File | Responsibility |
|------|---------------|
| `routes/api_strategies.py` | Add 3 HL liquidation endpoints (dashboard, wallet, state) |
| `templates/index.html` | New tab nav item (desktop + mobile) + `<section id="tab-hl-liquidation">` |
| `static/app.js` | Add `hl-liquidation` to `VALID_TABS` + `hlLiquidation` tab handler + nav wiring |
| `static/styles.css` | `.threat-banner`, `.pos-row--*`, gate checklist styles |
| `tests/test_hl_liquidation_api.py` | Flask endpoint tests + shell wiring assertions |

---

### Worker 1: Backend API + Shell Wiring

**Files:**
- Modify: `routes/api_strategies.py` (add 3 endpoints after copy-viability section)
- Modify: `templates/index.html` (add nav items + tab section)
- Modify: `static/app.js` (add `hl-liquidation` to `VALID_TABS` at line 10, add nav wiring)
- Create: `tests/test_hl_liquidation_api.py`

- [ ] **Step 1: Write endpoint tests + shell wiring tests**

Create `tests/test_hl_liquidation_api.py`:

Tests must cover:
- Dashboard endpoint: returns 200, correct response shape (threat_level, positions, counts), sorted by distance, threat_level logic (clear/approaching — NOT imminent/signal_ready)
- Wallet endpoint: returns positions for known wallet, empty for unknown
- State endpoint: returns state=SCANNING (never SIGNAL_DETECTED — Phase 5B)
- Shell wiring: `VALID_TABS` contains `hl-liquidation`

Key assertions for monitoring-only:
- `state` endpoint always returns `"SCANNING"` (no `SIGNAL_DETECTED` until Phase 5B)
- Dashboard `threat_level` uses distance thresholds only: clear (>15%), approaching (5-15%), watch (<5%) — NOT "imminent" with counter-trade advice
- No `signal_ready` or `counter_trade` fields in any response

- [ ] **Step 2: Run tests — verify they fail**

- [ ] **Step 3: Add endpoints to routes/api_strategies.py**

After the copy-viability leaderboard endpoint (~line 1131), add:

```python
        # ------------------------------------------------------------------
        # HL Liquidation Tracker
        # ------------------------------------------------------------------

        @bp.get("/api/algorithms/hl-liquidation/dashboard")
        def hl_liquidation_dashboard():
            try:
                positions = ctx["conn"].execute("""
                    SELECT wp.*, tw.label
                    FROM hl_whale_positions wp
                    LEFT JOIN hl_tracked_wallets tw ON wp.wallet = tw.wallet
                    ORDER BY wp.distance_pct ASC
                    LIMIT 500
                """).fetchall()
                pos_list = [dict(r) for r in positions]
                closest = pos_list[0]["distance_pct"] if pos_list else 100.0
                threat_level = "watch" if closest < 5 else "approaching" if closest < 15 else "clear"
                # ... build response matching spec shape ...
                return jsonify({...})
            except Exception:
                logger.exception("hl_liquidation_dashboard failed")
                return jsonify({"error": "Internal server error"}), 500
```

Note: `threat_level` values are `clear`, `approaching`, `watch` — NOT `imminent`. No counter-trade advice. No `signal_ready`.

- [ ] **Step 4: Add tab to index.html (both desktop + mobile nav)**

Desktop nav (~line 33): add `<a class="nav-item" data-tab="hl-liquidation">` after strategies, before intelligence.

Mobile nav (~line 729): add `<a class="mob-nav-item" data-tab="hl-liquidation">` after strategies, before intelligence. Add `overflow-x: auto` to `.mob-nav` in CSS if needed for 7-item overflow.

Tab section: add `<section id="tab-hl-liquidation" class="tab-panel">` with panel header, threat banner placeholder, position table wrapper, monitoring panel.

- [ ] **Step 5: Update VALID_TABS in app.js**

Line 10: add `'hl-liquidation'` to the Set:
```javascript
const VALID_TABS = new Set(['operator', 'signals', 'positions', 'wallets', 'strategies', 'hl-liquidation', 'intelligence']);
```

Wire into `nav.activate()` dispatch and `mobileRefresh` tab list.

- [ ] **Step 6: Run tests — verify they pass**

- [ ] **Step 7: Run full suite + commit**

```bash
git add routes/api_strategies.py templates/index.html static/app.js tests/test_hl_liquidation_api.py
git commit -m "feat: HL Liquidation Tracker — API endpoints + shell wiring

3 monitoring-only endpoints in api_strategies.py (dashboard, wallet, state).
Tab added to VALID_TABS, desktop + mobile nav, route persistence.
No fake decisioning — monitoring only until Phase 5B strategy exists.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Worker 2: Frontend Render + Styling

**Files:**
- Modify: `static/app.js` (add `hlLiquidation` tab handler with load/render)
- Modify: `static/styles.css` (threat-banner, position table, monitoring panel styles)

**Depends on:** Worker 1 committed.

- [ ] **Step 1: Add CSS styles to styles.css**

At end of file, add:
- `.threat-banner` with `.threat--clear`, `.threat--approaching`, `.threat--watch` variants
- `.hl-pos-table` with `.pos-row--watch`, `.pos-row--warn` row highlights
- `.dist-bar` inline color bar for distance visualization
- `.gate-list` with `.gate-pass` / `.gate-fail` items
- No `.threat--imminent` or pulse animation (monitoring only)

- [ ] **Step 2: Add hlLiquidation handler in app.js**

Tab handler with:
- `load()`: fetches `/api/algorithms/hl-liquidation/dashboard`, renders all sections
- `_renderThreat(data)`: banner with clear/approaching/watch states. NO "SIGNAL READY" or counter-trade text.
- `_renderTable(positions)`: sorted position table with distance color bars
- `_renderMonitoring(data)`: shows monitoring stats + gate conditions (met/unmet). Explicitly says "Strategy plugin: Phase 5B (not yet built)" in the gate section.

Plugs into `tabPoller` via standard `load()` pattern — no custom setInterval.

Wire into `nav.activate()`:
```javascript
case 'hl-liquidation': hlLiquidation.load(); break;
```

- [ ] **Step 3: Verify tab renders in browser**

Open dashboard → click "HL Liq" tab → threat banner + position table + monitoring panel render with live data.

- [ ] **Step 4: Run full suite + commit**

```bash
git add static/app.js static/styles.css
git commit -m "feat: HL Liquidation Tracker — threat banner, position table, monitoring panel

Color-coded threat states (clear/approaching/watch).
Position table sorted by liquidation proximity with distance bars.
Monitoring panel shows gate conditions — no fake decisioning.
Uses tabPoller for refresh (standard 90s cadence).

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Final Verification

- [ ] `python3 -m pytest tests/test_hl_liquidation_api.py -v` — all pass
- [ ] `python3 -m pytest tests/ -q` — full suite green
- [ ] Browser: click "HL Liq" tab on desktop → renders
- [ ] Browser: click "HL Liq" on mobile bottom nav → renders
- [ ] Route persistence: refresh page with `?tab=hl-liquidation` → tab loads
- [ ] Code review via superpowers:requesting-code-review
