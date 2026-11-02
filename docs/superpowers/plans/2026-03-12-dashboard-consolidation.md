# Dashboard Consolidation Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Consolidate the Sniper web dashboard from 9 tabs to 7, surface hidden operational state in a new Operator home screen, and merge overlapping wallet/signal views.

**Architecture:** Pure frontend changes — HTML, JS, CSS only. No backend modifications. All data comes from existing API endpoints. New tab modules follow the established IIFE pattern in `static/app.js` (tab module with `load()` + `render()`, data in `state.*`, routing via `router.pushTab()`).

**Known API limitations (frontend-only, no backend changes):**
- `/api/strategies` does NOT return `strategy_family`, `required_feeds`, or `supported_execution_modes` — these live on the Python plugin class but aren't serialized. Use `strategy_type` as a proxy for family.
- `/api/paper-trades` does NOT return `strategy_id`, `strategy_type`, or `instance_tag` — these columns don't exist on the `paper_trades` table. Show static `insider_copy` label for now.
- `/api/followed-wallets/<wallet>/portfolio` returns `avgPrice`, `curPrice`, `currentValue` — NOT `entry_price`, `current_price`, `entry_notional`.

**Tech Stack:** Vanilla JS (ES2020+), Flask Jinja templates, CSS custom properties (green matrix theme)

**Spec:** `docs/superpowers/specs/2026-03-12-dashboard-consolidation-design.md`

---

## Key File Map

| File | Lines | Role |
|------|-------|------|
| `templates/index.html` | 843 | HTML structure — nav, proof strip, tab panels |
| `static/app.js` | 5402 | All JS — tab modules, state, routing, modals, WebSocket |
| `static/styles.css` | 2770 | All CSS — design tokens, components, layout |

### Critical Insertion Points in `static/app.js`

| What | Line | Pattern |
|------|------|---------|
| `VALID_TABS` set | 10 | Add new tab IDs, remove retired ones |
| `state` object | 15+ | Add `state.operator`, `state.wallets` |
| Tab module definitions | 738–5212 | Each module: `const name = { async load() {...}, render() {...} }` |
| `nav.activate()` titles | 4339–4348 | Map tabId → display title |
| `nav.activate()` loaders | 4354–4362 | `if/else` chain calling `module.load()` |
| Default tab fallback | 4768 | `if (!VALID_TABS.has(...)) state.route.tab = 'operator'` |
| Router DEFAULTS | ~4735 | `tab: 'operator'` |

### Critical Insertion Points in `templates/index.html`

| What | Line |
|------|------|
| Nav items | 23–32 |
| Proof strip | 39–75 |
| Tab panels start | 78 |

---

## Chunk 1: Global Shell + Operator Tab

This is the highest-value chunk. Delivers the new Operator home screen and enriches the global header with operational status indicators.

### Task 1: Add Shell Operator Cluster to Header

Add a compact status cluster to the header bar showing alert count, health rollup, and Telegram status. This data loads once on page init and refreshes every 60s.

**Files:**
- Modify: `templates/index.html:34-36` (header-right div)
- Modify: `static/app.js:4849+` (init section)
- Modify: `static/styles.css` (append new styles)

- [ ] **Step 1: Add shell cluster HTML**

In `templates/index.html`, replace the `header-right` div (lines 34-36):

```html
<div class="header-right">
    <div class="shell-cluster" id="shell-cluster">
        <span class="shell-chip" id="shell-alerts" title="Pending alerts">
            <span class="shell-chip-icon">!</span>
            <span class="shell-chip-val">--</span>
        </span>
        <span class="shell-chip" id="shell-health" title="System health">
            <span class="shell-chip-icon">+</span>
            <span class="shell-chip-val">--</span>
        </span>
        <span class="shell-chip" id="shell-telegram" title="Telegram status">
            <span class="shell-chip-icon">T</span>
            <span class="shell-chip-val">--</span>
        </span>
    </div>
    <div class="current-time" id="current-time">--:-- UTC</div>
</div>
```

- [ ] **Step 2: Add shell cluster CSS**

Append to `static/styles.css`:

```css
/* ── Shell Operator Cluster ── */
.shell-cluster {
  display: flex;
  gap: 0.5rem;
  align-items: center;
  margin-right: 1rem;
}
.shell-chip {
  display: inline-flex;
  align-items: center;
  gap: 0.25rem;
  padding: 0.15rem 0.5rem;
  border: 1px solid var(--border);
  border-radius: 4px;
  font-size: 0.7rem;
  font-family: var(--font-mono, 'Courier New', monospace);
  color: var(--text-muted);
  cursor: default;
}
.shell-chip.warn { border-color: var(--state-warning); color: var(--state-warning); }
.shell-chip.fail { border-color: var(--state-negative); color: var(--state-negative); }
.shell-chip.ok   { border-color: var(--state-positive); color: var(--state-positive); }
```

- [ ] **Step 3: Add shell update JS**

In `static/app.js`, add a `shellCluster` object after the `proofBar` module (after line ~4664):

```javascript
// ============================================================================
// SHELL CLUSTER (global header status)
// ============================================================================
const shellCluster = {
  _interval: null,
  async update() {
    const [alertRes, proofRes] = await Promise.allSettled([
      fetchJson('/api/agent/alerts?status=new&limit=100'),
      fetchJson('/api/proof-bar'),
    ]);
    // Alert count
    const alertEl = document.getElementById('shell-alerts');
    if (alertEl && alertRes.status === 'fulfilled') {
      const cnt = alertRes.value.count || 0;
      alertEl.querySelector('.shell-chip-val').textContent = cnt;
      alertEl.className = 'shell-chip' + (cnt > 0 ? ' warn' : ' ok');
    }
    // Health rollup + Telegram (both from proof-bar)
    if (proofRes.status === 'fulfilled' && proofRes.value.health_details) {
      const hd = proofRes.value.health_details;
      const healthEl = document.getElementById('shell-health');
      if (healthEl) {
        const anyFail = Object.values(hd).some(s => s.status === 'fail' && s.proof_required);
        const anyWarn = Object.values(hd).some(s => s.status === 'warn' || (s.status === 'fail' && !s.proof_required));
        healthEl.querySelector('.shell-chip-val').textContent = anyFail ? 'FAIL' : anyWarn ? 'WARN' : 'OK';
        healthEl.className = 'shell-chip' + (anyFail ? ' fail' : anyWarn ? ' warn' : ' ok');
      }
      const tgEl = document.getElementById('shell-telegram');
      if (tgEl && hd.alerter) {
        const connected = hd.alerter.status === 'ok';
        tgEl.querySelector('.shell-chip-val').textContent = connected ? 'ON' : 'OFF';
        tgEl.className = 'shell-chip' + (connected ? ' ok' : ' warn');
      }
    }
    // Log any failures
    if (alertRes.status === 'rejected') console.warn('[shell] alerts fetch failed:', alertRes.reason);
    if (proofRes.status === 'rejected') console.warn('[shell] proof-bar fetch failed:', proofRes.reason);
  },
  start() {
    this.update();
    this._interval = setInterval(() => this.update(), 60000);
  }
};
```

- [ ] **Step 4: Wire shell into init**

In the init section (~line 4849+), add `shellCluster.start();` after the proof strip load.

- [ ] **Step 5: Verify in browser**

Load the dashboard. Confirm the header-right area shows three status chips (alerts, health, Telegram) with values populated from the API.

- [ ] **Step 6: Run tests**

Run: `python3 -m pytest tests/ -x -q --tb=line`
Expected: All pass (no backend changes)

- [ ] **Step 7: Commit**

```bash
git add templates/index.html static/app.js static/styles.css
git commit -m "feat(dashboard): add shell operator cluster to header"
```

---

### Task 2: Add Operator Tab Skeleton

Create the new Operator tab as the default landing page. Initially empty sections that will be populated in subsequent tasks.

**Files:**
- Modify: `templates/index.html:23-32` (nav), `templates/index.html:78+` (add tab panel)
- Modify: `static/app.js:10` (VALID_TABS), `static/app.js:4339-4362` (nav.activate), `static/app.js:4768` (default tab)

- [ ] **Step 1: Add Operator nav item and panel HTML**

In `templates/index.html`, replace the nav section (lines 23-32) to put Operator first:

```html
<nav class="topnav">
    <a href="#" class="nav-item active" data-tab="operator"><span class="nav-label">Operator</span></a>
    <a href="#" class="nav-item" data-tab="signals"><span class="nav-label">Signals</span></a>
    <a href="#" class="nav-item" data-tab="markets"><span class="nav-label">Markets</span></a>
    <a href="#" class="nav-item" data-tab="positions"><span class="nav-label">Positions</span></a>
    <a href="#" class="nav-item" data-tab="wallets"><span class="nav-label">Wallets</span></a>
    <a href="#" class="nav-item" data-tab="strategies"><span class="nav-label">Strategies</span></a>
    <a href="#" class="nav-item" data-tab="research"><span class="nav-label">Research</span></a>
</nav>
```

Note: `thesis`, `leaderboard`, `followed`, `proof-bar` removed from nav. Old tab panels remain in HTML (dead but not deleted yet — cleanup in Task 15).

Add the Operator tab panel right after `<div class="content-area">` (line 78), before the signals tab:

```html
<!-- OPERATOR TAB -->
<section id="tab-operator" class="tab-panel active">
    <div class="panel-header">
        <h1 class="panel-title">Operator</h1>
        <p class="panel-subtitle">System readiness, health, alerts, and diagnostics</p>
    </div>

    <!-- 1a: Proof & Readiness -->
    <div id="op-proof" class="op-section">
        <h2 class="section-heading">Proof &amp; Readiness</h2>
        <div id="op-proof-content" class="op-section-body">Loading...</div>
    </div>

    <!-- 1b: Alerts -->
    <div id="op-alerts" class="op-section">
        <h2 class="section-heading">Alert Queue</h2>
        <div id="op-alerts-content" class="op-section-body">Loading...</div>
    </div>

    <!-- 1c: Mutes -->
    <div id="op-mutes" class="op-section">
        <h2 class="section-heading">Active Mutes</h2>
        <div id="op-mutes-content" class="op-section-body">Loading...</div>
    </div>

    <!-- 1d: Classification Diagnostics -->
    <div id="op-diag" class="op-section">
        <h2 class="section-heading">Classification Diagnostics</h2>
        <div id="op-diag-content" class="op-section-body">Loading...</div>
    </div>

    <!-- 1e: Strategy / Replay Snapshot -->
    <div id="op-strats" class="op-section">
        <h2 class="section-heading">Strategy &amp; Replay Snapshot</h2>
        <div id="op-strats-content" class="op-section-body">Loading...</div>
    </div>
</section>
```

Remove `active` class from the old `tab-signals` panel since Operator is now the default.

- [ ] **Step 2: Add Operator section CSS**

Append to `static/styles.css`:

```css
/* ── Operator Tab ── */
.op-section {
  margin-bottom: 1.5rem;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 1rem;
  background: var(--surface-1);
}
.op-section .section-heading {
  font-size: 0.85rem;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--text-muted);
  margin: 0 0 0.75rem 0;
  padding-bottom: 0.5rem;
  border-bottom: 1px solid var(--border);
}
.op-section-body { min-height: 3rem; }
.op-cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 0.75rem;
}
.op-card {
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 0.75rem;
}
.op-card-label {
  font-size: 0.7rem;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--text-muted);
  margin-bottom: 0.25rem;
}
.op-card-value {
  font-size: 1.4rem;
  font-weight: 700;
  color: var(--text-strong);
  font-family: var(--font-mono, 'Courier New', monospace);
}
.op-card-value.pass { color: var(--state-positive); }
.op-card-value.fail { color: var(--state-negative); }
.op-card-value.warn { color: var(--state-warning); }
```

- [ ] **Step 3: Register Operator in JS**

In `static/app.js`:

1. Update `VALID_TABS` (line 10):
```javascript
const VALID_TABS = new Set(['operator', 'signals', 'markets', 'positions', 'wallets', 'strategies', 'research']);
```

2. Add to `nav.activate()` titles (line 4339):
```javascript
const titles = {
  operator: 'Operator',
  signals: 'Signals',
  markets: 'Markets',
  positions: 'Positions',
  wallets: 'Wallets',
  strategies: 'Strategies',
  research: 'Research',
  // Legacy (panels still in DOM, just hidden from nav)
  thesis: 'Thesis',
  leaderboard: 'Leaderboard',
  followed: 'Followed Wallets',
  'proof-bar': 'Proof Bar'
};
```

3. Add to `nav.activate()` loader chain (line 4354):
```javascript
if (tabId === 'operator') operator.load();
else if (tabId === 'signals') signals.load().then(() => ws.subscribeVisible());
// ... rest unchanged
```

4. Update `state.route.tab` initial value (line 33):
```javascript
// Change from 'signals' to 'operator'
tab: 'operator',
```

5. Update default tab fallback (line 4768):
```javascript
if (!VALID_TABS.has(state.route.tab)) state.route.tab = 'operator';
```

6. Update router DEFAULTS (~line 4735):
```javascript
DEFAULTS: { tab: 'operator', ... }
```

6. Add empty operator module stub (before `shellCluster`):
```javascript
// ============================================================================
// OPERATOR TAB
// ============================================================================
const operator = {
  async load() {
    // Sections loaded in subsequent tasks
  },
  render() {}
};
```

- [ ] **Step 4: Verify in browser**

Load dashboard. Confirm: Operator tab is default, shows 5 section skeletons with "Loading..." placeholders. Old tabs still work if navigated to directly via URL `?tab=thesis`.

- [ ] **Step 5: Commit**

```bash
git add templates/index.html static/app.js static/styles.css
git commit -m "feat(dashboard): add Operator tab skeleton as default landing page"
```

---

### Task 3: Operator — Proof & Readiness Section

Move the proof bar content into the Operator tab's first section. Reuse the existing `proofBar` module's rendering logic.

**Files:**
- Modify: `templates/index.html` (op-proof-content panel)
- Modify: `static/app.js` (operator.load, operator.renderProof)

- [ ] **Step 1: Add proof section HTML structure**

In `templates/index.html`, replace the `op-proof-content` div content with structured containers that the JS will populate:

```html
<div id="op-proof-content" class="op-section-body">
    <div id="op-proof-cards" class="op-cards"></div>
    <div id="op-proof-monthly" style="margin-top:1rem"></div>
    <div id="op-proof-pace" style="margin-top:0.75rem"></div>
    <div id="op-proof-health" style="margin-top:0.75rem"></div>
</div>
```

- [ ] **Step 2: Implement operator.renderProof()**

In `static/app.js`, expand the `operator` module:

```javascript
const operator = {
  async load() {
    try {
      const proofData = await fetchJson('/api/proof-bar');
      state.operator = state.operator || {};
      state.operator.proof = proofData;
      this.renderProof();
      // Also update the global proof strip
      proofBar.renderStrip(proofData);
    } catch (err) { console.error('[operator] proof load failed:', err); }
  },
  renderProof() {
    const d = state.operator?.proof;
    if (!d) return;
    const c = d.criteria || {};
    const statusClass = (s) => s === 'pass' ? 'pass' : s === 'warn' ? 'warn' : 'fail';

    // Criteria cards
    const cardsEl = document.getElementById('op-proof-cards');
    if (cardsEl) {
      cardsEl.innerHTML = `
        <div class="op-card">
          <div class="op-card-label">Closed Trades</div>
          <div class="op-card-value ${statusClass(c.closed_trades?.status)}">${c.closed_trades?.value ?? '--'} / ${c.closed_trades?.target ?? 150}</div>
        </div>
        <div class="op-card">
          <div class="op-card-label">Win Rate</div>
          <div class="op-card-value ${statusClass(c.win_rate?.status)}">${c.win_rate?.value != null ? c.win_rate.value.toFixed(1) + '%' : '--%'}</div>
        </div>
        <div class="op-card">
          <div class="op-card-label">Slip-Adj PnL</div>
          <div class="op-card-value ${statusClass(c.slip_pnl?.status)}">$${c.slip_pnl?.value?.toFixed(2) ?? '--'}</div>
        </div>
        <div class="op-card">
          <div class="op-card-label">No Losing Month</div>
          <div class="op-card-value ${statusClass(c.no_losing_month?.status)}">${c.no_losing_month?.value ?? '--'} / ${c.no_losing_month?.target ?? 2}</div>
        </div>
        <div class="op-card">
          <div class="op-card-label">Health</div>
          <div class="op-card-value ${statusClass(c.health?.status)}">${c.health?.value ?? '--'}</div>
        </div>
      `;
    }

    // Monthly breakdown
    const monthEl = document.getElementById('op-proof-monthly');
    if (monthEl && d.monthly?.length) {
      monthEl.innerHTML = `
        <div class="table-wrapper"><table class="data-table compact">
          <thead><tr><th>Month</th><th>Trades</th><th>WR</th><th>Raw PnL</th><th>Slip PnL</th><th>Status</th></tr></thead>
          <tbody>${d.monthly.map(m => `<tr>
            <td>${m.month}</td><td>${m.trades}</td>
            <td>${m.win_rate?.toFixed(1)}%</td>
            <td>$${m.pnl_raw?.toFixed(2)}</td>
            <td>$${m.pnl_slip?.toFixed(2)}</td>
            <td><span class="badge ${m.profitable ? 'pass' : 'fail'}">${m.profitable ? 'PROFIT' : 'LOSS'}${m.complete ? '' : ' (partial)'}</span></td>
          </tr>`).join('')}</tbody>
        </table></div>`;
    }

    // Pace
    const paceEl = document.getElementById('op-proof-pace');
    if (paceEl && d.pace) {
      const p = d.pace;
      paceEl.innerHTML = `<div class="op-cards">
        <div class="op-card"><div class="op-card-label">7d Rate</div><div class="op-card-value">${p.seven_day_rate?.toFixed(1) ?? '--'}/wk</div></div>
        <div class="op-card"><div class="op-card-label">To Go</div><div class="op-card-value">${p.trades_to_go ?? '--'}</div></div>
        <div class="op-card"><div class="op-card-label">ETA</div><div class="op-card-value">${p.eta_date ?? 'N/A'}</div></div>
        <div class="op-card"><div class="op-card-label">Open Positions</div><div class="op-card-value">${p.open_positions ?? '--'}</div></div>
        <div class="op-card"><div class="op-card-label">Equity</div><div class="op-card-value">$${p.cumulative_equity?.toFixed(2) ?? '--'}</div></div>
      </div>`;
    }

    // Health details
    const healthEl = document.getElementById('op-proof-health');
    if (healthEl && d.health_details) {
      const hd = d.health_details;
      healthEl.innerHTML = `<div class="table-wrapper"><table class="data-table compact">
        <thead><tr><th>Service</th><th>Status</th><th>Detail</th><th>Required</th></tr></thead>
        <tbody>${Object.entries(hd).map(([svc, s]) => `<tr>
          <td>${svc}</td>
          <td><span class="badge ${s.status === 'ok' ? 'pass' : s.status === 'warn' ? 'warn' : 'fail'}">${s.status.toUpperCase()}</span></td>
          <td class="text-muted">${s.detail || ''}</td>
          <td>${s.proof_required ? 'Yes' : 'No'}</td>
        </tr>`).join('')}</tbody>
      </table></div>`;
    }
  },
  render() {
    this.renderProof();
  }
};
```

- [ ] **Step 3: Verify in browser**

Navigate to Operator tab. Confirm: criteria cards show with pass/fail coloring, monthly table renders, pace cards show, health details table shows service statuses.

- [ ] **Step 4: Commit**

```bash
git add templates/index.html static/app.js
git commit -m "feat(dashboard): implement Operator proof & readiness section"
```

---

### Task 4: Operator — Alert Queue Section

Surface the agent alert system in the browser for the first time. Shows recent alerts with ack and bulk-ack actions.

**Files:**
- Modify: `static/app.js` (operator module — add renderAlerts)
- Modify: `static/styles.css` (alert table action styles)

- [ ] **Step 1: Implement operator.renderAlerts()**

Add to `operator.load()`:

```javascript
async load() {
  const results = await Promise.allSettled([
    fetchJson('/api/proof-bar'),
    fetchJson('/api/agent/alerts?status=new,sent&limit=50'),
    fetchJson('/api/agent/mutes?limit=100'),
    fetchJson('/api/classification-diagnostics'),
    fetchJson('/api/strategies'),
  ]);
  const [proofRes, alertRes, muteRes, diagRes, stratRes] = results;
  results.forEach((r, i) => { if (r.status === 'rejected') console.error(`[operator] fetch ${i} failed:`, r.reason); });
  state.operator = {
    proof: proofRes.status === 'fulfilled' ? proofRes.value : null,
    alerts: alertRes.status === 'fulfilled' ? alertRes.value : null,
    mutes: muteRes.status === 'fulfilled' ? muteRes.value : null,
    diag: diagRes.status === 'fulfilled' ? diagRes.value : null,
    strategies: stratRes.status === 'fulfilled' ? stratRes.value : null,
  };
  this.render();
  if (state.operator.proof) proofBar.renderStrip(state.operator.proof);
},
```

Add `renderAlerts()` method:

```javascript
renderAlerts() {
  const d = state.operator?.alerts;
  const el = document.getElementById('op-alerts-content');
  if (!el) return;
  if (!d?.items?.length) {
    el.innerHTML = '<div class="empty-state">No pending alerts</div>';
    return;
  }
  el.innerHTML = `
    <div style="margin-bottom:0.5rem;display:flex;justify-content:space-between;align-items:center">
      <span class="text-muted">${d.count} alert${d.count !== 1 ? 's' : ''}</span>
      <button class="btn btn-sm btn-outline" id="op-ack-all-btn">Ack All Visible</button>
    </div>
    <div class="table-wrapper"><table class="data-table compact" id="op-alerts-table">
      <thead><tr><th>Time</th><th>Type</th><th>Wallet</th><th>Title</th><th>Status</th><th>Action</th></tr></thead>
      <tbody>${d.items.map(a => `<tr data-alert-id="${a.id}">
        <td title="${a.last_seen_at}">${fmt.age(a.last_seen_ts)}</td>
        <td><span class="badge">${a.alert_type || '--'}</span></td>
        <td title="${a.wallet}">${a.username || a.pseudonym || fmt.addr(a.wallet)}</td>
        <td>${a.title || a.message || '--'}</td>
        <td><span class="badge ${a.status === 'new' ? 'warn' : ''}">${a.status}</span></td>
        <td>${a.status !== 'acknowledged' ? `<button class="btn btn-xs btn-ghost op-ack-btn" data-id="${a.id}">Ack</button>` : '<span class="text-dim">Done</span>'}</td>
      </tr>`).join('')}</tbody>
    </table></div>`;

  // Wire ack buttons
  el.querySelectorAll('.op-ack-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      await mutate(btn, () => fetchJson(`/api/agent/alerts/${btn.dataset.id}/ack`, { method: 'POST' }));
      operator.load();
    });
  });

  // Wire bulk ack
  document.getElementById('op-ack-all-btn')?.addEventListener('click', async function() {
    const ids = d.items.filter(a => a.status !== 'acknowledged').map(a => a.id);
    if (!ids.length) return;
    await mutate(this, () => fetchJson('/api/agent/alerts/ack-bulk', {
      method: 'POST', body: JSON.stringify({ ids })
    }));
    operator.load();
  });
},
```

Update `render()`:

```javascript
render() {
  this.renderProof();
  this.renderAlerts();
},
```

- [ ] **Step 2: Add compact button CSS**

Append to `static/styles.css`:

```css
.btn-xs { padding: 0.1rem 0.4rem; font-size: 0.65rem; }
.empty-state { padding: 1rem; text-align: center; color: var(--text-dim); font-style: italic; }
```

- [ ] **Step 3: Verify in browser**

Navigate to Operator tab. Confirm: Alert Queue section shows alerts from the agent API. Ack button acknowledges a single alert and refreshes. Bulk Ack button works. If no alerts exist, shows "No pending alerts" empty state.

- [ ] **Step 4: Commit**

```bash
git add static/app.js static/styles.css
git commit -m "feat(dashboard): add alert queue section to Operator tab"
```

---

### Task 5: Operator — Mute Management Section

Show active mutes with add and delete actions.

**Files:**
- Modify: `static/app.js` (operator module — add renderMutes)

- [ ] **Step 1: Implement operator.renderMutes()**

```javascript
renderMutes() {
  const d = state.operator?.mutes;
  const el = document.getElementById('op-mutes-content');
  if (!el) return;
  if (!d?.items?.length) {
    el.innerHTML = '<div class="empty-state">No active mutes</div>';
    return;
  }
  el.innerHTML = `
    <div style="margin-bottom:0.75rem;display:flex;gap:0.5rem;align-items:center">
      <input type="text" class="search-input" id="op-mute-scope" placeholder="Wallet address or condition_id:outcome" style="flex:1;font-size:0.75rem">
      <input type="text" class="search-input" id="op-mute-reason" placeholder="Reason (optional)" style="width:150px;font-size:0.75rem">
      <button class="btn btn-sm btn-outline" id="op-mute-add-btn">Add Mute</button>
    </div>
    <div class="table-wrapper"><table class="data-table compact">
      <thead><tr><th>Scope</th><th>Value</th><th>Reason</th><th>Expires</th><th>Action</th></tr></thead>
      <tbody>${d.items.map(m => `<tr>
        <td>${m.scope_type}</td>
        <td title="${m.scope_value}">${fmt.addr(m.scope_value)}</td>
        <td class="text-muted">${m.reason || '--'}</td>
        <td>${m.expires_at || 'Never'}</td>
        <td><button class="btn btn-xs btn-ghost op-mute-del" data-id="${m.id}">Remove</button></td>
      </tr>`).join('')}</tbody>
    </table></div>`;

  el.querySelectorAll('.op-mute-del').forEach(btn => {
    btn.addEventListener('click', async () => {
      await mutate(btn, () => fetchJson(`/api/agent/mutes/${btn.dataset.id}`, { method: 'DELETE' }));
      operator.load();
    });
  });

  // Wire add-mute form
  document.getElementById('op-mute-add-btn')?.addEventListener('click', async function() {
    const scope = document.getElementById('op-mute-scope')?.value?.trim();
    const reason = document.getElementById('op-mute-reason')?.value?.trim();
    if (!scope) return;
    const [scope_type, scope_value] = scope.includes(':') ? ['position', scope] : ['wallet', scope];
    await mutate(this, () => fetchJson('/api/agent/mutes', {
      method: 'POST',
      body: JSON.stringify({ scope_type, scope_value, reason, ttl_seconds: 86400 * 7 })
    }));
    document.getElementById('op-mute-scope').value = '';
    document.getElementById('op-mute-reason').value = '';
    operator.load();
  });
},
```

Update `render()`:

```javascript
render() {
  this.renderProof();
  this.renderAlerts();
  this.renderMutes();
},
```

- [ ] **Step 2: Verify in browser**

Mute section shows active mutes. Remove button deletes a mute and refreshes. Empty state shows if no mutes exist.

- [ ] **Step 3: Commit**

```bash
git add static/app.js
git commit -m "feat(dashboard): add mute management section to Operator tab"
```

---

### Task 6: Operator — Classification Diagnostics Section

Show the pipeline drought summary: why wallets are or aren't qualifying.

**Files:**
- Modify: `static/app.js` (operator module — add renderDiagnostics)

- [ ] **Step 1: Implement operator.renderDiagnostics()**

```javascript
renderDiagnostics() {
  const d = state.operator?.diag;
  const el = document.getElementById('op-diag-content');
  if (!el) return;
  if (!d?.summary) {
    el.innerHTML = '<div class="empty-state">No diagnostics available</div>';
    return;
  }
  const s = d.summary;
  el.innerHTML = `
    <div class="op-cards" style="margin-bottom:1rem">
      <div class="op-card">
        <div class="op-card-label">Above Threshold</div>
        <div class="op-card-value">${s.wallets_above_threshold ?? '--'}</div>
      </div>
      <div class="op-card">
        <div class="op-card-label">Insider-Like</div>
        <div class="op-card-value">${s.insider_like_wallets ?? '--'}</div>
      </div>
      <div class="op-card">
        <div class="op-card-label">Open Signals</div>
        <div class="op-card-value">${s.open_signals_meeting_insider_threshold ?? '--'}</div>
      </div>
      <div class="op-card">
        <div class="op-card-label">Max Open Score</div>
        <div class="op-card-value">${s.max_open_signal_insider_score?.toFixed(0) ?? '--'}</div>
      </div>
      <div class="op-card">
        <div class="op-card-label">Low Signal</div>
        <div class="op-card-value warn">${s.low_signal_wallets ?? '--'}</div>
      </div>
    </div>
    ${d.top_failed_checks?.length ? `
    <div class="table-wrapper"><table class="data-table compact">
      <thead><tr><th>Failed Check</th><th>Count</th><th>% of Low-Signal</th></tr></thead>
      <tbody>${d.top_failed_checks.map(f => `<tr>
        <td>${f.check}</td>
        <td>${f.count}</td>
        <td>${f.pct_of_low_signal?.toFixed(1)}%</td>
      </tr>`).join('')}</tbody>
    </table></div>` : ''}`;
},
```

Update `render()`:

```javascript
render() {
  this.renderProof();
  this.renderAlerts();
  this.renderMutes();
  this.renderDiagnostics();
},
```

- [ ] **Step 2: Verify in browser**

Diagnostics section shows metric cards and a failed-checks table explaining why wallets aren't qualifying.

- [ ] **Step 3: Commit**

```bash
git add static/app.js
git commit -m "feat(dashboard): add classification diagnostics section to Operator tab"
```

---

### Task 7: Operator — Strategy & Replay Snapshot

Compact summary of active strategies and recent replay runs.

**Files:**
- Modify: `static/app.js` (operator module — add renderStrategySnapshot)

- [ ] **Step 1: Implement operator.renderStrategySnapshot()**

```javascript
renderStrategySnapshot() {
  const strats = state.operator?.strategies;
  const el = document.getElementById('op-strats-content');
  if (!el) return;

  const items = strats?.items || strats || [];
  if (!items.length) {
    el.innerHTML = '<div class="empty-state">No strategies configured</div>';
    return;
  }

  const byStatus = {};
  items.forEach(s => {
    const st = s.status || 'unknown';
    byStatus[st] = (byStatus[st] || 0) + 1;
  });

  el.innerHTML = `
    <div class="op-cards">
      ${Object.entries(byStatus).map(([st, cnt]) => `
        <div class="op-card">
          <div class="op-card-label">${st}</div>
          <div class="op-card-value">${cnt}</div>
        </div>
      `).join('')}
      <div class="op-card">
        <div class="op-card-label">Total Strategies</div>
        <div class="op-card-value">${items.length}</div>
      </div>
    </div>
    <div class="table-wrapper" style="margin-top:0.75rem"><table class="data-table compact">
      <thead><tr><th>Name</th><th>Type</th><th>Status</th></tr></thead>
      <tbody>${items.slice(0, 10).map(s => `<tr>
        <td>${s.label || s.name || '--'}</td>
        <td><span class="badge" style="font-size:0.6rem">${s.strategy_type || '--'}</span></td>
        <td><span class="badge">${s.status || '--'}</span></td>
      </tr>`).join('')}</tbody>
    </table></div>`;
},
```

Update `render()`:

```javascript
render() {
  this.renderProof();
  this.renderAlerts();
  this.renderMutes();
  this.renderDiagnostics();
  this.renderStrategySnapshot();
},
```

- [ ] **Step 2: Verify in browser**

Strategy section shows status cards + strategy table with name, type badge, and status.

- [ ] **Step 3: Commit**

```bash
git add static/app.js
git commit -m "feat(dashboard): add strategy snapshot section to Operator tab"
```

---

## Chunk 2: Signals Consolidation

Merge Thesis into the Signals tab. Add gate transparency labels. Remove the separate Thesis tab from navigation.

### Task 8: Merge Thesis into Signals Tab

Add a "Thesis Signals" sub-section within the existing Signals tab, populated from the thesis API endpoints.

**Files:**
- Modify: `templates/index.html` (tab-signals panel — add thesis section)
- Modify: `static/app.js` (signals module — load thesis data, render thesis section)

- [ ] **Step 1: Add thesis section HTML to Signals tab**

In `templates/index.html`, inside the `tab-signals` panel, after the existing convergence section, add:

```html
<!-- Thesis signals (merged from former Thesis tab) -->
<div id="signals-thesis-section" style="margin-top:1.5rem">
    <h2 class="section-heading">Thesis Signals <span class="badge" style="font-size:0.65rem">HARD-GATED</span></h2>
    <div id="signals-thesis-content"></div>
</div>
```

- [ ] **Step 2: Extend signals.load() to fetch thesis data**

In `static/app.js`, modify the `signals` module's `load()` to also fetch thesis data:

```javascript
// Inside signals.load(), after fetching open-position-signals:
const thesisData = await fetchJson('/api/thesis-signals');
state.thesisInSignals = thesisData.items || [];
this.renderThesis();
```

- [ ] **Step 3: Add signals.renderThesis()**

```javascript
renderThesis() {
  const items = state.thesisInSignals || [];
  const el = document.getElementById('signals-thesis-content');
  if (!el) return;
  if (!items.length) {
    el.innerHTML = '<div class="empty-state">No thesis-qualifying signals</div>';
    return;
  }
  el.innerHTML = `
    <div class="table-wrapper"><table class="data-table compact">
      <thead><tr><th>Wallet</th><th>Market</th><th>Insider</th><th>Price</th><th>Notional</th><th>Status</th></tr></thead>
      <tbody>${items.map(s => `<tr>
        <td title="${s.wallet}">${s.username || s.pseudonym || fmt.addr(s.wallet)}</td>
        <td>${s.title || s.condition_id || '--'}</td>
        <td>${s.position_insider_score?.toFixed(0) ?? s.insider_score?.toFixed(0) ?? '--'}</td>
        <td>${fmt.price(s.entry_price)}</td>
        <td>${fmt.usd(s.entry_notional)}</td>
        <td><span class="badge ${s.is_open ? 'pass' : ''}">${s.is_open ? 'OPEN' : 'CLOSED'}</span></td>
      </tr>`).join('')}</tbody>
    </table></div>`;
},
```

- [ ] **Step 4: Verify in browser**

Signals tab now shows the main signals table, convergence section, AND a new "Thesis Signals" section at the bottom with hard-gated signals.

- [ ] **Step 5: Commit**

```bash
git add templates/index.html static/app.js
git commit -m "feat(dashboard): merge thesis signals into Signals tab"
```

---

### Task 9: Gate Transparency Labels

Add visible gate/skip reason labels to signal rows so the operator can see why signals are or aren't actionable.

**Files:**
- Modify: `static/app.js` (signals.render — add gate status column)
- Modify: `static/styles.css` (gate label styles)

- [ ] **Step 1: Add gate status column to signals table**

In the signals module's `render()` function, add a "Gate" column after the existing columns. The open-position-signals endpoint already returns fields like `position_classification`, `position_insider_score`, and `position_speculator_score`. Use these to compute gate status:

```javascript
// Helper function inside signals module:
_gateLabel(row) {
  const ins = row.position_insider_score ?? row.insider_score ?? 0;
  const spec = row.position_speculator_score ?? row.speculator_score ?? 0;
  const price = row.entry_price ?? 1;
  if (ins >= 60 && spec < 70 && price <= 0.70) return { label: 'QUALIFYING', cls: 'pass' };
  if (ins >= 55 && spec < 70 && price <= 0.70) return { label: 'NEAR-MISS', cls: 'warn' };
  const reasons = [];
  if (ins < 55) reasons.push('insider<55');
  if (spec >= 70) reasons.push('spec>=70');
  if (price > 0.70) reasons.push('price>0.70');
  return { label: reasons.join(', ') || 'LOW', cls: 'fail' };
},
```

Add this column to each signal row's `<tr>`:

```html
<td><span class="badge gate-${gate.cls}">${gate.label}</span></td>
```

- [ ] **Step 2: Add gate label CSS**

```css
.gate-pass { background: rgba(0,255,65,0.15); color: var(--state-positive); }
.gate-warn { background: rgba(204,255,0,0.15); color: var(--state-warning); }
.gate-fail { background: rgba(255,51,51,0.1); color: var(--text-dim); font-size: 0.65rem; }
```

- [ ] **Step 3: Verify in browser**

Signal rows now show a Gate column with QUALIFYING (green), NEAR-MISS (yellow), or specific gate failure reasons (dim red).

- [ ] **Step 4: Commit**

```bash
git add static/app.js static/styles.css
git commit -m "feat(dashboard): add gate transparency labels to Signals tab"
```

---

## Chunk 3: Wallets Consolidation

Merge Leaderboard and Followed into a unified Wallets tab with filters and a wallet detail drawer.

### Task 10: Create Unified Wallets Tab

Replace the separate Leaderboard and Followed tabs with a single Wallets tab that shows a unified wallet table.

**Files:**
- Modify: `templates/index.html` (add `tab-wallets` panel)
- Modify: `static/app.js` (add `wallets` module)
- Modify: `static/styles.css` (wallet tab styles)

- [ ] **Step 1: Add Wallets tab HTML**

In `templates/index.html`, add a new tab panel (after `tab-positions`, before `tab-strategies`):

```html
<!-- WALLETS TAB -->
<section id="tab-wallets" class="tab-panel">
    <div class="panel-header">
        <h1 class="panel-title">Wallets</h1>
        <p class="panel-subtitle">Intelligence on tracked wallets — rankings, classifications, activity</p>
    </div>
    <div class="filter-bar" id="wallets-filters">
        <input type="text" class="search-input" id="wallets-search" placeholder="Search wallet, username...">
        <div class="badge-filter">
            <span class="badge-chip active" data-wallets-filter="all">All</span>
            <span class="badge-chip" data-wallets-filter="followed">Followed</span>
            <span class="badge-chip" data-wallets-filter="emerging">Emerging</span>
            <span class="badge-chip" data-wallets-filter="high-wr">High WR</span>
            <span class="badge-chip" data-wallets-filter="flow">Spec Flow</span>
        </div>
    </div>
    <div class="table-wrapper">
        <table class="data-table" id="wallets-table">
            <thead><tr>
                <th data-sort-key="wallet">Wallet</th>
                <th data-sort-key="insider_score">Insider</th>
                <th data-sort-key="win_rate">WR</th>
                <th data-sort-key="classification">Class</th>
                <th data-sort-key="confidence_label">Confidence</th>
                <th data-sort-key="followability_score">Follow Score</th>
                <th>Followed</th>
                <th>Why Ranked</th>
                <th data-sort-key="analysis_updated_at">Updated</th>
            </tr></thead>
            <tbody id="wallets-tbody"></tbody>
        </table>
    </div>
    <!-- Wallet detail drawer -->
    <div class="drawer-overlay" id="wallet-drawer-overlay"></div>
    <aside class="drawer" id="wallet-drawer">
        <div class="drawer-header">
            <h2 class="drawer-title" id="wallet-drawer-title">Wallet Detail</h2>
            <button class="btn btn-ghost drawer-close" id="wallet-drawer-close">&times;</button>
        </div>
        <div class="drawer-body" id="wallet-drawer-body">Loading...</div>
    </aside>
</section>
```

- [ ] **Step 2: Add wallets module JS**

In `static/app.js`, add the `wallets` module:

```javascript
// ============================================================================
// WALLETS TAB (merged Leaderboard + Followed)
// ============================================================================
const wallets = {
  async load() {
    try {
      const [lbData, followedData] = await Promise.allSettled([
        fetchJson('/api/wallet-followable-leaderboard?limit=500'),
        fetchJson('/api/followed-wallets'),
      ]);
      const lbItems = lbData.status === 'fulfilled' ? (lbData.value.items || []) : [];
      const followedItems = followedData.status === 'fulfilled' ? (followedData.value.items || followedData.value || []) : [];

      // Build lookup of leaderboard wallets
      const lbMap = new Map(lbItems.map(w => [(w.wallet || '').toLowerCase(), w]));

      // Merge: start with leaderboard items, mark followed state
      const merged = lbItems.map(w => ({
        ...w,
        _followed: false, // will be set below
        _source: 'leaderboard',
      }));

      // Add followed-only wallets not already in leaderboard
      const followedSet = new Set();
      for (const fw of followedItems) {
        const key = (fw.wallet || '').toLowerCase();
        followedSet.add(key);
        if (!lbMap.has(key)) {
          merged.push({
            wallet: fw.wallet,
            username: fw.username,
            pseudonym: fw.pseudonym,
            insider_score: fw.insider_score,
            win_rate: fw.win_rate,
            classification: fw.classification || 'followed',
            confidence_label: '',
            followability_score: null,
            speculator_score: fw.speculator_score,
            analysis_updated_at: fw.last_synced_at || fw.added_at || '',
            why_ranked: [],
            _followed: true,
            _source: 'followed',
          });
        }
      }

      // Mark followed state on leaderboard items
      merged.forEach(w => {
        if (followedSet.has((w.wallet || '').toLowerCase())) w._followed = true;
      });

      state.wallets = merged;
      this.render();
    } catch (err) { console.error('[wallets] load failed:', err); }
  },

  _filter() {
    const f = state.route.wallets_filter || 'all';
    const search = (state.route.wallets_search || '').toLowerCase();
    let items = state.wallets || [];
    if (f === 'followed') items = items.filter(w => w._followed);
    else if (f === 'emerging') items = items.filter(w => w.classification === 'emerging_insider');
    else if (f === 'high-wr') items = items.filter(w => (w.win_rate || 0) >= 60 && (w.closed_sample_size || 0) >= 10);
    else if (f === 'flow') items = items.filter(w => (w.speculator_score || 0) >= 50);
    if (search) items = items.filter(w =>
      (w.wallet || '').toLowerCase().includes(search) ||
      (w.username || '').toLowerCase().includes(search) ||
      (w.pseudonym || '').toLowerCase().includes(search) ||
      (w.display_name || '').toLowerCase().includes(search)
    );
    return items;
  },

  render() {
    const items = this._filter();
    const tbody = document.getElementById('wallets-tbody');
    if (!tbody) return;
    tbody.innerHTML = items.map(w => `<tr class="wallet-row" data-wallet="${w.wallet}">
      <td title="${w.wallet}">${w.display_name || w.username || w.pseudonym || fmt.addr(w.wallet)}</td>
      <td>${w.insider_score?.toFixed(0) ?? '--'}</td>
      <td>${w.win_rate != null ? w.win_rate.toFixed(1) + '%' : '--'}</td>
      <td><span class="badge">${w.classification || '--'}</span></td>
      <td>${w.confidence_label || '--'}</td>
      <td>${w.followability_score?.toFixed(1) ?? '--'}</td>
      <td>${w._followed ? '<span class="badge pass">YES</span>' : ''}</td>
      <td class="text-muted" style="max-width:150px;overflow:hidden;text-overflow:ellipsis">${(w.why_ranked || []).join(', ') || '--'}</td>
      <td>${w.analysis_updated_at || '--'}</td>
    </tr>`).join('');

    wireDataSort('wallets-table', 'wallets_sort', 'wallets_dir', () => this.render());
    updateSortIndicators('wallets-table', 'wallets_sort', 'wallets_dir');

    // Wire row click → drawer
    tbody.querySelectorAll('.wallet-row').forEach(row => {
      row.addEventListener('click', () => this.openDrawer(row.dataset.wallet));
    });
  },

  async openDrawer(wallet) {
    const overlay = document.getElementById('wallet-drawer-overlay');
    const drawer = document.getElementById('wallet-drawer');
    const body = document.getElementById('wallet-drawer-body');
    const title = document.getElementById('wallet-drawer-title');
    if (!drawer || !body) return;

    overlay.classList.add('open');
    drawer.classList.add('open');
    title.textContent = fmt.addr(wallet);
    body.innerHTML = '<div class="empty-state">Loading wallet detail...</div>';

    try {
      const [portfolio, snapshots, activity] = await Promise.all([
        fetchJson(`/api/followed-wallets/${wallet}/portfolio`).catch(() => null),
        fetchJson(`/api/research/snapshots?wallet=${wallet}&limit=10`).catch(() => null),
        fetchJson(`/api/research/activity?wallet=${wallet}&limit=20`).catch(() => null),
      ]);

      // Find wallet in state for scoring data
      const w = (state.wallets || []).find(x => x.wallet?.toLowerCase() === wallet.toLowerCase()) || {};

      title.textContent = w.username || w.pseudonym || fmt.addr(wallet);
      body.innerHTML = this._renderDrawerContent(w, portfolio, snapshots, activity);
    } catch (err) {
      body.innerHTML = `<div class="empty-state">Failed to load: ${err.message || err}</div>`;
    }
  },

  _renderDrawerContent(w, portfolio, snapshots, activity) {
    const sections = [];

    // Classification summary
    sections.push(`<div class="op-section">
      <h3 class="section-heading">Classification</h3>
      <div class="op-cards">
        <div class="op-card"><div class="op-card-label">Insider</div><div class="op-card-value">${w.insider_score?.toFixed(0) ?? '--'}</div></div>
        <div class="op-card"><div class="op-card-label">Speculator</div><div class="op-card-value">${w.speculator_score?.toFixed(0) ?? '--'}</div></div>
        <div class="op-card"><div class="op-card-label">Class</div><div class="op-card-value" style="font-size:0.9rem">${w.classification || '--'}</div></div>
        <div class="op-card"><div class="op-card-label">Confidence</div><div class="op-card-value" style="font-size:0.9rem">${w.confidence_label || '--'}</div></div>
        <div class="op-card"><div class="op-card-label">Win Rate</div><div class="op-card-value">${w.win_rate != null ? w.win_rate.toFixed(1) + '%' : '--'}</div></div>
        <div class="op-card"><div class="op-card-label">Sample</div><div class="op-card-value">${w.closed_sample_size ?? '--'} / ${w.total_closed_count ?? '--'}</div></div>
      </div>
      ${w.reason ? `<div class="text-muted" style="margin-top:0.5rem;font-size:0.75rem">${w.reason}</div>` : ''}
    </div>`);

    // Strategy info
    if (w.strategy_group || w.strategy_note) {
      sections.push(`<div class="op-section">
        <h3 class="section-heading">Strategy Profile</h3>
        <div class="op-cards">
          <div class="op-card"><div class="op-card-label">Group</div><div class="op-card-value" style="font-size:0.9rem">${w.strategy_group || '--'}</div></div>
          <div class="op-card"><div class="op-card-label">Confidence</div><div class="op-card-value">${w.strategy_confidence?.toFixed(2) ?? '--'}</div></div>
        </div>
        ${w.strategy_note ? `<div class="text-muted" style="margin-top:0.5rem;font-size:0.75rem">${w.strategy_note}</div>` : ''}
      </div>`);
    }

    // Portfolio (if available)
    const positions = portfolio?.items || portfolio?.positions || [];
    if (positions.length) {
      sections.push(`<div class="op-section">
        <h3 class="section-heading">Open Positions (${positions.length})</h3>
        <div class="table-wrapper"><table class="data-table compact">
          <thead><tr><th>Market</th><th>Outcome</th><th>Entry</th><th>Current</th><th>Value</th></tr></thead>
          <tbody>${positions.slice(0, 15).map(p => `<tr>
            <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis">${p.title || p.condition_id || '--'}</td>
            <td>${p.outcome || '--'}</td>
            <td>${fmt.price(p.avgPrice)}</td>
            <td>${fmt.price(p.curPrice)}</td>
            <td>${fmt.usd(p.currentValue)}</td>
          </tr>`).join('')}</tbody>
        </table></div>
      </div>`);
    }

    // Recent activity (if available)
    const acts = activity?.items || [];
    if (acts.length) {
      sections.push(`<div class="op-section">
        <h3 class="section-heading">Recent Activity (${acts.length})</h3>
        <div class="table-wrapper"><table class="data-table compact">
          <thead><tr><th>Time</th><th>Type</th><th>Market</th><th>Size</th></tr></thead>
          <tbody>${acts.slice(0, 15).map(a => `<tr>
            <td>${fmt.age(a.timestamp || a.created_ts)}</td>
            <td>${a.activity_type || a.side || '--'}</td>
            <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis">${a.title || a.condition_id || '--'}</td>
            <td>${fmt.usd(a.notional || a.size)}</td>
          </tr>`).join('')}</tbody>
        </table></div>
      </div>`);
    }

    return sections.join('') || '<div class="empty-state">No detail data available</div>';
  },

  closeDrawer() {
    document.getElementById('wallet-drawer-overlay')?.classList.remove('open');
    document.getElementById('wallet-drawer')?.classList.remove('open');
  }
};
```

- [ ] **Step 3: Add drawer CSS**

Append to `static/styles.css`:

```css
/* ── Wallet Detail Drawer ── */
.drawer-overlay {
  position: fixed; inset: 0;
  background: rgba(0,0,0,0.5);
  z-index: 900;
  opacity: 0; pointer-events: none;
  transition: opacity 0.2s;
}
.drawer-overlay.open { opacity: 1; pointer-events: auto; }
.drawer {
  position: fixed; top: 0; right: 0;
  width: min(560px, 90vw);
  height: 100vh;
  background: var(--surface-1);
  border-left: 1px solid var(--border);
  z-index: 901;
  transform: translateX(100%);
  transition: transform 0.25s ease;
  display: flex; flex-direction: column;
  overflow: hidden;
}
.drawer.open { transform: translateX(0); }
.drawer-header {
  display: flex; justify-content: space-between; align-items: center;
  padding: 1rem;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.drawer-title {
  font-size: 1rem;
  color: var(--text-strong);
  font-family: var(--font-mono, 'Courier New', monospace);
}
.drawer-close {
  font-size: 1.5rem;
  line-height: 1;
  padding: 0.25rem 0.5rem;
}
.drawer-body {
  flex: 1;
  overflow-y: auto;
  padding: 1rem;
}
.drawer-body .op-section { margin-bottom: 1rem; }
.wallet-row { cursor: pointer; }
.wallet-row:hover { background: rgba(0,255,65,0.06); }
```

- [ ] **Step 4: Wire wallets filter chips and drawer close**

In the init section of `static/app.js`:

```javascript
// Wallets filter chips
document.querySelectorAll('[data-wallets-filter]').forEach(chip => {
  chip.addEventListener('click', () => {
    document.querySelectorAll('[data-wallets-filter]').forEach(c => c.classList.remove('active'));
    chip.classList.add('active');
    router.replaceParam('wallets_filter', chip.dataset.walletsFilter);
    wallets.render();
  });
});

// Wallets search
document.getElementById('wallets-search')?.addEventListener('input', function() {
  router.replaceParam('wallets_search', this.value);
  wallets.render();
});

// Drawer close
document.getElementById('wallet-drawer-close')?.addEventListener('click', () => wallets.closeDrawer());
document.getElementById('wallet-drawer-overlay')?.addEventListener('click', () => wallets.closeDrawer());
```

Add to router DEFAULTS:
```javascript
wallets_filter: 'all', wallets_search: '', wallets_sort: 'followability_score', wallets_dir: 'desc',
```

Add to `nav.activate()` loader chain:
```javascript
else if (tabId === 'wallets') wallets.load();
```

- [ ] **Step 5: Verify in browser**

Navigate to Wallets tab. Confirm: unified table loads with wallet data, filter chips work (All, Followed, Emerging, High WR, Spec Flow), search filters live, clicking a row opens the detail drawer with classification + portfolio + activity sections.

- [ ] **Step 6: Commit**

```bash
git add templates/index.html static/app.js static/styles.css
git commit -m "feat(dashboard): add unified Wallets tab with detail drawer"
```

---

## Chunk 4: Attribution + Navigation Cleanup

Add strategy family and instance tag badges to Strategies and Positions tabs. Then do the final navigation cleanup to arrive at the target 7-tab IA.

### Task 11: Strategy Type Badges in Strategies Tab

Add `strategy_type` badges to strategy cards. Note: `strategy_family`, `required_feeds`, and `supported_execution_modes` are NOT in the `/api/strategies` response (they live on the Python plugin class but aren't serialized to DB or API). Use `strategy_type` as the proxy for now.

**Files:**
- Modify: `static/app.js` (strategies module — enrich card/row rendering)

- [ ] **Step 1: Add type badge to strategy cards**

In the `strategies` module's render function, find where strategy cards or table rows are generated. Add the type badge after the name:

```javascript
// In the strategy card/row template, add after the name:
<span class="badge" style="font-size:0.6rem">${s.strategy_type || '--'}</span>
```

- [ ] **Step 2: Verify in browser**

Strategies tab shows family badges and feed/mode columns. Instance tags appear where present.

- [ ] **Step 3: Commit**

```bash
git add static/app.js
git commit -m "feat(dashboard): add strategy family and instance badges to Strategies tab"
```

---

### Task 12: Position Attribution in Positions Tab

Add a Strategy column to paper trades. Note: `paper_trades` does NOT have `strategy_id`, `strategy_type`, or `instance_tag` columns — these don't exist in the DB schema. For now, show a static `insider_copy` label since all current paper trades come from that strategy. This becomes dynamic when the backend adds strategy attribution to paper_trades.

**Files:**
- Modify: `static/app.js` (positions module — add attribution column)

- [ ] **Step 1: Add strategy column to paper trades table**

In the `positions` module's paper trade render function, add a column:

```javascript
// In the paper trade table header, add:
<th>Strategy</th>

// In each paper trade row, add:
<td><span class="badge" style="font-size:0.6rem">insider_copy</span></td>
```

- [ ] **Step 2: Verify in browser**

Paper trades table shows a Strategy column with `insider_copy` on every row.

- [ ] **Step 3: Commit**

```bash
git add static/app.js
git commit -m "feat(dashboard): add strategy attribution column to Positions tab"
```

---

### Task 13: Navigation Cleanup — Final 7-Tab Shell

Remove retired tabs (Thesis, Leaderboard, Followed, Proof Bar) from the DOM. Redirect any deep links to the new locations.

**Files:**
- Modify: `templates/index.html` (remove retired tab panels)
- Modify: `static/app.js` (clean up VALID_TABS, add redirects, remove dead module references)

- [ ] **Step 1: Remove retired tab panels from HTML**

In `templates/index.html`, delete (or comment out) the following `<section>` elements:
- `tab-thesis` panel
- `tab-leaderboard` panel
- `tab-followed` panel
- `tab-proof-bar` panel

These were already hidden from nav in Task 2. Now remove the HTML entirely.

- [ ] **Step 2: Add legacy tab redirects in JS**

In `static/app.js`, update the router `_normalize()` to redirect old tab names:

```javascript
_normalize() {
  // Legacy tab redirects
  const redirects = { thesis: 'signals', leaderboard: 'wallets', followed: 'wallets', 'proof-bar': 'operator' };
  if (redirects[state.route.tab]) state.route.tab = redirects[state.route.tab];

  if (!VALID_TABS.has(state.route.tab)) state.route.tab = 'operator';
  // ... rest of normalize
}
```

- [ ] **Step 3: Clean up dead code**

Remove the `thesis` module's `load()` call from `nav.activate()` (it was already unreachable since thesis isn't in VALID_TABS). Same for `leaderboard` and `followed` loader calls. Leave the module code itself for now (it can be removed in a future cleanup pass — not worth the risk of breaking shared helpers).

Remove the `leaderboard_dir`, `followed_wallet_dir`, `followed_position_dir` from router DEFAULTS (replaced by `wallets_*`).

Update proof strip chip click handlers in init (~line 4877) to navigate to `'operator'` instead of `'proof-bar'`.

- [ ] **Step 4: Verify in browser**

- Load `?tab=thesis` → redirects to Signals
- Load `?tab=leaderboard` → redirects to Wallets
- Load `?tab=followed` → redirects to Wallets
- Load `?tab=proof-bar` → redirects to Operator
- Proof strip chip clicks navigate to Operator tab (not the retired Proof Bar)
- Navigation shows exactly 7 tabs: Operator, Signals, Markets, Positions, Wallets, Strategies, Research
- All 7 tabs load and render correctly

- [ ] **Step 5: Run full test suite**

Run: `python3 -m pytest tests/ -x -q --tb=line`
Expected: All pass (no backend changes made)

- [ ] **Step 6: Commit**

```bash
git add templates/index.html static/app.js
git commit -m "feat(dashboard): finalize 7-tab navigation, remove retired tabs"
```

---

## Verification Checklist

After all chunks are complete, verify:

- [ ] Default landing page is Operator tab
- [ ] Operator tab shows: proof cards, monthly table, pace, health, alerts, mutes, diagnostics, strategy snapshot
- [ ] Shell header shows: alert count, health status, Telegram status (refreshes every 60s)
- [ ] Proof strip still works across all tabs
- [ ] Signals tab includes thesis signals section + gate transparency labels
- [ ] Wallets tab shows unified leaderboard + followed state + filter chips + search
- [ ] Wallet row click opens detail drawer with classification, portfolio, activity
- [ ] Positions tab shows strategy and instance columns
- [ ] Strategies tab shows family, feeds, execution mode columns
- [ ] Legacy URLs redirect correctly (thesis→signals, leaderboard→wallets, etc.)
- [ ] Full pytest suite passes
- [ ] No new backend endpoints or DB changes required
