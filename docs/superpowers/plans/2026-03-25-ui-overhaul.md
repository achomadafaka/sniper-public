# UI Overhaul Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Professionalize the Sniper dashboard from a developer prototype into a cohesive, terminal-grade trading intelligence interface that matches the matrix-themed world map across all 6 tabs.

**Architecture:** CSS-first overhaul using CSS custom properties for consistent theming, with targeted HTML/JS changes for layout improvements. No framework migration. The world map already has the target aesthetic — extend it to everything else.

**Tech Stack:** CSS custom properties, Inter + JetBrains Mono fonts, TradingView Lightweight Charts (already loaded), D3.js (already loaded), vanilla JS.

---

## Execution Strategy (important)

### Subagent mode recommendation

**Use subagent-driven development, but do NOT run Tasks 4-9 as parallel code-writing workers on the current file structure.**

Current constraint:
- `static/app.js` is still a monolith
- `static/styles.css` is still a monolith
- Most tab tasks touch the same rendering and style surfaces

That means naive parallel execution will create merge collisions and visual regressions.

### Safe execution lanes

**Lane A — isolated / parallel-safe**
- Task 10 (audit harness)
- screenshot review, visual critique, and UX notes
- non-mutating browser validation between tasks

**Lane B — shared frontend core (single writer at a time)**
- Tasks 1-9
- One implementation worker owns `static/app.js`, `static/styles.css`, and `templates/index.html` for any given step

### Recommended order
1. Task 10 can start immediately in parallel because it creates new files and improves verification.
2. Tasks 1-3 must be done first by a single frontend owner.
3. Tasks 4-8 should then run **sequentially**, with review after each task.
4. Task 9 comes after at least Tasks 4-8 are substantially done.

### Parallel review pattern
If subagents are used heavily:
- one writer subagent edits shared frontend files
- one or more review subagents inspect screenshots, compare against the audit manifest, and propose corrections
- do not allow two coding workers to edit `static/app.js` or `static/styles.css` in the same cycle

---

## Current State (from visual audit, March 25 2026)

### What works
- World map on Intel tab — matrix green, scanlines, vignette, monospace labels
- Ticker bar — clean scrolling with live data
- Bottom mobile nav — icons + labels, correct 6-tab layout
- Performance cards on Operator — 2-column on mobile, 6-column on desktop
- Strategy cards — good info density with WR, PnL, open/closed counts

### What doesn't work

**Global issues:**
- No consistent color system — green values are different shades across tabs
- Tables use default browser styling with no row hover, no zebra striping
- Section headings ("PERFORMANCE", "EQUITY CURVE") are plain green text with no visual weight
- No skeleton/loading states — content pops in after "Loading..." text
- Excessive vertical whitespace between sections (40-60px gaps)
- Shell status chips (top right) look disconnected from the nav
- No breadcrumb or context about what you're looking at
- "← scroll →" text hints appear everywhere instead of proper scroll shadows

**Operator tab:**
- Performance cards have no visual hierarchy — all same size, same border
- Equity curve chart floats in a box with no context (no period selector, no summary stats)
- Domain cards ("US Politics $182.6M") are tiny, hard to scan, cut off at edge
- Alert Queue, Mutes, Diagnostics, Strategy Snapshot all use raw HTML tables with no styling
- Strategy table at bottom shows stopped strategies mixed with active — no visual separation

**Signals tab:**
- Signal Snapshots cards at top are good but the main table below is a raw data dump
- 10 column headers with tiny text, hard to scan
- Score column has no color gradient (a score of 100 and 15 look the same)
- "Showing 0 of 0 signals" placeholder is confusing (shows before data loads)
- Convergence signals section is visually indistinct from the main signals

**Positions tab:**
- Stats cards (Total Open, Total PnL, etc.) have inconsistent formatting
- The main positions table is extremely wide — 13+ columns, requires horizontal scroll
- No visual distinction between open (active) and closed (historical) positions
- "Signal Pipeline: 126 ready" stat card is orphaned at bottom left
- Mobile cards work but have no status indicators (no color coding for win/loss)

**Wallets tab:**
- Dense table with 9 columns — works on desktop but overwhelming
- Classification badges ("low_signal", "speculator_bot") use plain text, not styled badges
- "Why Ranked" column has long text that wraps awkwardly
- No visual cue for wallet quality (a pnl-verified wallet looks same as unverified)
- Wallet detail drawer says "Loading..." and feels disconnected

**Strategies tab:**
- Left sidebar list is cramped — strategy names truncated to "P...", "World...", "Finan..."
- Right panel shows "Select a strategy" placeholder with no visual guidance
- "AU" badges on strategy cards are unexplained (what does AU mean?)
- Sort dropdown looks like a form input, not a filter control
- No visual PnL sparkline or trend indicator on strategy cards

**Intel tab:**
- Best-designed tab overall (world map carries it)
- Trade ideas panel has good Execute/Dismiss/More actions
- Event cards are well-structured with status badges
- News grouping by domain works
- Issue: "5 open" trade ideas count is hard to see against the panel background

**Mobile-specific:**
- Tables overflow horizontally with "← scroll →" hints everywhere
- Positions mobile cards lack color coding for PnL (red/green)
- Strategies tab is unusable on mobile — side panel doesn't adapt
- No pull-to-refresh gesture
- Touch targets on filter chips are too small (36px, should be 44px)

---

## Design System (apply before any tab work)

### Task 1: CSS Custom Properties + Typography Foundation

**Files:**
- Modify: `static/styles.css` (top of file)
- Modify: `templates/index.html` (font imports)

- [ ] **Step 1: Define the complete color system as CSS custom properties**

```css
:root {
  /* Matrix greens */
  --matrix-primary: #00ff41;
  --matrix-dim: #00ff4188;
  --matrix-faint: #00ff4133;
  --matrix-bg: #00ff4108;

  /* Semantic colors */
  --color-win: #00ff41;
  --color-loss: #ff3333;
  --color-warn: #ffaa00;
  --color-neutral: #556677;
  --color-info: #3b82f6;

  /* Surface hierarchy */
  --surface-0: #020a02;      /* deepest background */
  --surface-1: #0a1a0a;      /* card background */
  --surface-2: #0d2a0d;      /* elevated surface */
  --surface-3: #102a10;      /* hover/active state */

  /* Borders */
  --border-subtle: #00ff4115;
  --border-default: #00ff4125;
  --border-strong: #00ff4140;

  /* Text */
  --text-primary: #e0ffe0;
  --text-secondary: #88bb88;
  --text-muted: #446644;
  --text-bright: #ffffff;

  /* Spacing */
  --space-xs: 4px;
  --space-sm: 8px;
  --space-md: 16px;
  --space-lg: 24px;
  --space-xl: 32px;

  /* Radii */
  --radius-sm: 4px;
  --radius-md: 6px;
  --radius-lg: 8px;
}
```

- [ ] **Step 2: Add JetBrains Mono for data values**

In `templates/index.html`, add after Inter font import:
```html
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
```

- [ ] **Step 3: Define typography classes**

```css
/* Data values — always monospace */
.mono { font-family: 'JetBrains Mono', 'Courier New', monospace; }
.stat-value { font-family: 'JetBrains Mono', monospace; font-weight: 600; letter-spacing: -0.5px; }
.stat-label { font-family: 'Inter', sans-serif; font-size: 0.7rem; font-weight: 500; text-transform: uppercase; letter-spacing: 1px; color: var(--text-muted); }

/* Section headings */
.section-heading {
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.75rem;
  font-weight: 700;
  letter-spacing: 2px;
  text-transform: uppercase;
  color: var(--matrix-primary);
  border-bottom: 1px solid var(--border-subtle);
  padding-bottom: var(--space-sm);
  margin-bottom: var(--space-md);
}
```

- [ ] **Step 4: Run tests and commit**

```bash
node --check static/app.js
# Visual check: open dashboard, verify fonts load
git add static/styles.css templates/index.html
git commit -m "design: CSS custom properties + JetBrains Mono typography system"
```

---

### Task 2: Table Styling System

**Files:**
- Modify: `static/styles.css`

- [ ] **Step 1: Create reusable table styles**

```css
/* Terminal-grade tables */
.data-table {
  width: 100%;
  border-collapse: collapse;
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.78rem;
}
.data-table th {
  font-size: 0.68rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: var(--text-muted);
  padding: var(--space-sm) var(--space-md);
  border-bottom: 1px solid var(--border-default);
  text-align: left;
  position: sticky;
  top: 0;
  background: var(--surface-1);
  z-index: 1;
}
.data-table td {
  padding: var(--space-sm) var(--space-md);
  border-bottom: 1px solid var(--border-subtle);
  color: var(--text-primary);
}
.data-table tr:hover td {
  background: var(--surface-2);
}
/* Zebra striping */
.data-table tbody tr:nth-child(even) td {
  background: var(--surface-0);
}
.data-table tbody tr:nth-child(even):hover td {
  background: var(--surface-2);
}
/* Value coloring */
.data-table .val-positive { color: var(--color-win); }
.data-table .val-negative { color: var(--color-loss); }
.data-table .val-neutral { color: var(--text-muted); }
```

- [ ] **Step 2: Style sort indicators**

```css
.data-table th[data-sortable] {
  cursor: pointer;
  user-select: none;
}
.data-table th[data-sortable]:hover {
  color: var(--matrix-primary);
}
.data-table th .sort-arrow {
  opacity: 0.4;
  margin-left: 4px;
}
.data-table th.sorted .sort-arrow {
  opacity: 1;
  color: var(--matrix-primary);
}
```

- [ ] **Step 3: Commit**

```bash
git add static/styles.css
git commit -m "design: terminal-grade table styling system"
```

---

### Task 3: Card and Badge System

**Files:**
- Modify: `static/styles.css`

- [ ] **Step 1: Create stat card component**

```css
.stat-card {
  background: var(--surface-1);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  padding: var(--space-md);
  position: relative;
  overflow: hidden;
}
.stat-card::before {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 2px;
  background: var(--matrix-primary);
  opacity: 0.3;
}
.stat-card--positive::before { background: var(--color-win); opacity: 0.6; }
.stat-card--negative::before { background: var(--color-loss); opacity: 0.6; }
.stat-card--warn::before { background: var(--color-warn); opacity: 0.6; }
```

- [ ] **Step 2: Create badge system**

```css
.badge {
  display: inline-block;
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.65rem;
  font-weight: 600;
  letter-spacing: 0.5px;
  padding: 2px 8px;
  border-radius: 3px;
  text-transform: uppercase;
}
.badge--active { color: var(--color-win); border: 1px solid var(--color-win); background: rgba(0,255,65,0.08); }
.badge--paused { color: var(--color-warn); border: 1px solid var(--color-warn); background: rgba(255,170,0,0.08); }
.badge--stopped { color: var(--text-muted); border: 1px solid var(--text-muted); background: rgba(85,102,119,0.08); }
.badge--win { color: var(--color-win); background: rgba(0,255,65,0.12); }
.badge--loss { color: var(--color-loss); background: rgba(255,51,51,0.12); }
.badge--pnl-verified { color: var(--color-info); border: 1px solid var(--color-info); background: rgba(59,130,246,0.08); }
.badge--insider { color: var(--matrix-primary); border: 1px solid var(--matrix-dim); background: var(--matrix-bg); }

/* Score indicator — color gradient by value */
.score-indicator {
  font-family: 'JetBrains Mono', monospace;
  font-weight: 700;
  font-size: 0.82rem;
}
.score-indicator--high { color: var(--color-win); } /* 70+ */
.score-indicator--mid { color: var(--color-warn); } /* 40-69 */
.score-indicator--low { color: var(--color-loss); } /* <40 */
```

- [ ] **Step 3: Create skeleton loading states**

```css
@keyframes skeleton-pulse {
  0%, 100% { opacity: 0.15; }
  50% { opacity: 0.3; }
}
.skeleton {
  background: var(--matrix-faint);
  border-radius: var(--radius-sm);
  animation: skeleton-pulse 1.5s ease-in-out infinite;
}
.skeleton-text { height: 12px; width: 80%; margin: 6px 0; }
.skeleton-card { height: 80px; width: 100%; }
.skeleton-row { height: 40px; width: 100%; margin: 2px 0; }
```

- [ ] **Step 4: Commit**

```bash
git add static/styles.css
git commit -m "design: card, badge, score indicator, and skeleton loading components"
```

---

### Task 4: Operator Tab Overhaul

**Files:**
- Modify: `static/app.js` (operator section rendering)
- Modify: `static/styles.css`

- [ ] **Step 1: Add equity curve period selector and summary**

Add buttons above the chart for 7d / 30d / All time views. Add a one-line summary showing period return.

- [ ] **Step 2: Style Operator section tables with data-table class**

Apply `.data-table` class to Alert Queue, Active Mutes, Classification Diagnostics, and Strategy Snapshot tables.

- [ ] **Step 3: Add status color to Performance cards**

Positive PnL card gets green top accent, negative gets red. Win Rate gets color based on value (>60% green, <50% red).

- [ ] **Step 4: Improve domain cards layout**

Make domain cards horizontally scrollable on mobile with snap points instead of wrapping to multiple rows.

- [ ] **Step 5: Visually separate active vs stopped strategies in snapshot table**

Stopped strategies get reduced opacity. Active strategies get green left border.

- [ ] **Step 6: Test and commit**

```bash
node --check static/app.js
git add static/app.js static/styles.css
git commit -m "design: Operator tab overhaul — charts, tables, cards, domain layout"
```

---

### Task 5: Signals Tab Overhaul

**Files:**
- Modify: `static/app.js` (signals rendering)
- Modify: `static/styles.css`

- [ ] **Step 1: Add score color gradient to Signals table**

Score values get colored: 90+ bright green, 70-89 green, 50-69 amber, <50 red.

- [ ] **Step 2: Reduce table density without hiding critical truth by default**

Prefer responsive priority and compact stacked cells over removing important columns outright. If a toggle is added, default desktop view must still preserve the primary truth-bearing fields needed to understand why a signal is ranked.

- [ ] **Step 3: Style Convergence Signals section distinctly**

Add a subtle background color and border to visually separate from the main signals table.

- [ ] **Step 4: Replace "Showing 0 of 0" with skeleton loading**

Show skeleton rows instead of "0 of 0" text while data loads.

- [ ] **Step 5: Test and commit**

```bash
node --check static/app.js
git add static/app.js static/styles.css
git commit -m "design: Signals tab — score colors, column management, loading skeletons"
```

---

### Task 6: Positions Tab Overhaul

**Files:**
- Modify: `static/app.js` (positions rendering)
- Modify: `static/styles.css`

- [ ] **Step 1: Color-code PnL and ROI values**

Green for positive, red for negative, across both table and mobile cards.

- [ ] **Step 2: Add visual separator between Open and Closed sections**

A clear heading with count badge: "Open Positions (55)" and "Closed Trades (501)" with different background tints.

- [ ] **Step 3: Reduce table width — group related columns**

Combine "Entry" + "Entry Lag" + "Current" into a single "Price" column with stacked layout. Combine "PnL" + "ROI" into one column.

- [ ] **Step 4: Add win/loss color coding to mobile position cards**

Green left border for winning positions, red for losing, neutral for open.

- [ ] **Step 5: Move Signal Pipeline card into a header bar**

Instead of orphaned card at bottom left, put "126 ready" in the sub-tab header next to "Paper Trades".

- [ ] **Step 6: Test and commit**

```bash
node --check static/app.js
git add static/app.js static/styles.css
git commit -m "design: Positions tab — PnL colors, section separators, compact columns"
```

---

### Task 7: Wallets Tab Overhaul

**Files:**
- Modify: `static/app.js` (wallets rendering)
- Modify: `static/styles.css`

- [ ] **Step 1: Style classification as colored badges**

`low_signal` → grey badge, `speculator_bot` → amber badge, `high_winrate_review` → green badge, `arb_fund` → red badge, `followed` → blue badge.

- [ ] **Step 2: Add score color indicator**

Same as Signals tab — color gradient based on insider score value.

- [ ] **Step 3: Style confidence column with badges**

`pnl-verified` → blue badge with checkmark icon, `metric-qualified` → grey badge, `reviewed` → green badge.

- [ ] **Step 4: Truncate "Why Ranked" to tooltip**

Show first reason as text, rest on hover tooltip. Prevents text wrapping.

- [ ] **Step 5: Improve wallet detail drawer**

Add smooth slide-in animation, show skeleton while loading, style with card system.

- [ ] **Step 6: Test and commit**

```bash
node --check static/app.js
git add static/app.js static/styles.css
git commit -m "design: Wallets tab — classification badges, score colors, drawer animation"
```

---

### Task 8: Strategies Tab Overhaul

**Files:**
- Modify: `static/app.js` (strategies rendering)
- Modify: `static/styles.css`

- [ ] **Step 1: Fix sidebar truncation**

Show full strategy names — wrap text instead of truncating to "P...", "World...".

- [ ] **Step 2: Add PnL sparkline to strategy cards**

Use a tiny inline SVG sparkline (last 10 trade PnLs) on each strategy card.

- [ ] **Step 3: Replace "AU" badge with an explicit strategy type label**

Use short plain-English labels or labeled badges with tooltip help. Do not replace an ambiguous code with another ambiguous icon-only code.

- [ ] **Step 4: Style sort dropdown as filter chips**

Replace HTML select with styled chip buttons matching the Active/Paused/Stopped filters.

- [ ] **Step 5: Improve empty state for right panel**

Instead of "Select a strategy" text, show a summary dashboard with aggregate stats across all active strategies.

- [ ] **Step 6: Make strategies tab mobile-responsive**

On mobile, show list only. Tapping a strategy navigates to a full-screen detail view with back button.

- [ ] **Step 7: Test and commit**

```bash
node --check static/app.js
git add static/app.js static/styles.css
git commit -m "design: Strategies tab — full names, sparklines, icons, mobile responsive"
```

---

### Task 9: Mobile Polish

**Files:**
- Modify: `static/styles.css`
- Modify: `static/app.js`

- [ ] **Step 1: Remove "← scroll →" text hints**

Replace with CSS scroll shadow indicators (gradient fade on edges).

- [ ] **Step 2: Increase touch targets to 44px minimum**

Filter chips, buttons, table rows on mobile.

- [ ] **Step 3: Fix Strategies tab mobile layout**

Hide sidebar, show list as full-width cards. Detail view as separate screen.

- [ ] **Step 4: Improve refresh affordance without implying unsupported gestures**

Do not imply pull-to-refresh unless it actually refreshes data. Prefer a clear visible refresh affordance, stale-data timestamp, or sticky refresh action on mobile.

- [ ] **Step 5: Test on 390x844 viewport and commit**

```bash
node --check static/app.js
git add static/app.js static/styles.css
git commit -m "design: mobile polish — scroll shadows, touch targets, strategies responsive"
```

---

### Task 10: GPT Audit Integration

**Files:**
- Create: `config/ui_live_audit_manifest.yaml`
- Create: `scripts/ui_live_audit.py`

- [ ] **Step 1: Create audit manifest with truth mappings per tab**

Define required elements, API sources, and backing truth for each tab as specified in GPT's audit plan.

- [ ] **Step 2: Create minimal audit runner**

Non-mutating script that fetches APIs, captures screenshots, and compares visible values against API payloads.

- [ ] **Step 3: Generate discrepancy report**

Output Markdown report + JSON ledger with classification per GPT's spec.

- [ ] **Step 4: Test and commit**

```bash
python3 scripts/ui_live_audit.py --tab operator --dry-run
git add config/ scripts/ui_live_audit.py
git commit -m "feat: UI live audit runner with truth verification"
```

---

## Execution Notes

- Tasks 1-3 (design system) MUST be done first — all subsequent tasks depend on the CSS custom properties and component classes.
- Task 10 (audit) can be done independently at any time and is the only truly parallel-safe coding track in the current structure.
- Tasks 4-8 should be treated as sequential shared-frontend tasks unless `static/app.js` and `static/styles.css` are first split into isolated per-tab modules.
- Task 9 (mobile) should be done after Tasks 4-8 are substantially complete, because it depends on the final desktop information architecture.
- Each task should be verified visually on both desktop (1280x800) and mobile (390x844) before committing.
- Cache version in `templates/index.html` must be bumped with each CSS/JS change.
- Every task that changes UI rendering should be rechecked against live data truth, not screenshots alone. Task 10's manifest and discrepancy output become the acceptance gate for Tasks 4-9.

Sources:
- [Bloomberg Terminal UX](https://www.bloomberg.com/company/stories/how-bloomberg-terminal-ux-designers-conceal-complexity/)
- [Dark Trading Dashboard Patterns](https://www.pinterest.com/ideas/dark-mode-trading-dashboard-design-patterns-2025/938052841262/)
- [Figma Trading UI Kit](https://www.figma.com/community/file/1359230717010633204/trading-stock-market-dashboard-platform-ui-kit)
- [Color Theory in Finance Dashboards](https://medium.com/@extej/the-role-of-color-theory-in-finance-dashboard-design-d2942aec9fff)
- [Dashboard UX Best Practices](https://www.pencilandpaper.io/articles/ux-pattern-analysis-data-dashboards)
