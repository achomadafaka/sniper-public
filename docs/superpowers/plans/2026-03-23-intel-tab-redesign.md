# Intel Tab Redesign — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Redesign the Intel tab from a scrolling wall of event cards into a professional intelligence dashboard with actionable trade ideas prominently placed, filterable events, and a clean mobile experience.

**Architecture:** Pure frontend changes (HTML, CSS, JS). No new API endpoints. All data comes from existing `/api/world-context`, `/api/trade-ideas`, `/api/news-feed`, `/api/ticker` endpoints.

**Tech Stack:** Vanilla JS, CSS Grid/Flexbox, existing lightweight-charts library

**Design Principles (from Bloomberg research):**
- Surface what users need to know, WHEN they need it, WHY it's relevant
- Actionable content first (trade ideas), context second (events), reference last (news)
- High information density but clear visual hierarchy
- Dark theme with contrast — not everything the same shade of green

---

## New Layout: Intel Tab

### Desktop (1280px+)

```
┌─────────────────────────────────────────────────────────────────┐
│ [Geopolitics] [Finance]            ↻ Refresh  15:20:30 EDT     │
├─────────────────────────────────────────────────────────────────┤
│ SCROLLING TICKER BAR                                            │
├──────────────────────┬──────────────────────────────────────────┤
│                      │                                          │
│  TRADE IDEAS         │  WORLD MAP                               │
│  (left panel, 35%)   │  (right panel, 65%)                      │
│                      │  - D3 map with event pins                │
│  7 open              │  - Tooltip on hover                      │
│                      │  - Zoom/pan controls                     │
│  ┌────────────────┐  │                                          │
│  │ BTC $80K March  │  │                                          │
│  │ BUY YES @ 13¢  │  │                                          │
│  │ MEDIUM  Execute │  │                                          │
│  └────────────────┘  │                                          │
│  ┌────────────────┐  │                                          │
│  │ Iran ceasefire  │  │                                          │
│  │ BUY YES @ 18¢  │  │                                          │
│  │ MEDIUM  Execute │  │                                          │
│  └────────────────┘  │                                          │
│  ...                 │                                          │
│                      │                                          │
├──────────────────────┴──────────────────────────────────────────┤
│                                                                  │
│  WORLD EVENTS (with filters)                                     │
│  [All] [Active] [Escalating] [Developing] [Monitoring]          │
│  [Middle East] [Europe] [Americas] [Asia] [Africa]              │
│                                                                  │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐                        │
│  │ Event 1  │ │ Event 2  │ │ Event 3  │  (3-column grid)       │
│  │ collapsed│ │ collapsed│ │ collapsed│                         │
│  └──────────┘ └──────────┘ └──────────┘                        │
│                                                                  │
├──────────────────────────────────────────────────────────────────┤
│  RECENT NEWS (grouped by domain, expandable)                     │
│  ▸ Geopolitics (12 items)                                        │
│  ▸ Finance (8 items)                                             │
│  ▸ Crypto (5 items)                                              │
└──────────────────────────────────────────────────────────────────┘
```

### Mobile (390px)

```
┌─────────────────────┐
│ [Geopolitics] [Fin] │
├─────────────────────┤
│ TRADE IDEAS (top!)  │
│ Compact cards       │
│ Swipe to see more   │
├─────────────────────┤
│ MAP (compressed)    │
│ 150px height        │
│ Tap to expand full  │
├─────────────────────┤
│ EVENT FILTERS       │
│ Horizontal scroll   │
├─────────────────────┤
│ EVENTS              │
│ Collapsed by default│
│ Tap to expand       │
├─────────────────────┤
│ NEWS                │
│ Grouped + collapsed │
└─────────────────────┘
```

---

## Chunk 1: Restructure Intel Layout (trade ideas → top)

### Task 1: Move trade ideas ABOVE the map, add split panel on desktop

**Files:**
- Modify: `templates/index.html` — reorder Intel sections
- Modify: `static/app.js` — update rendering order
- Modify: `static/styles.css` — add split panel CSS

The trade ideas section moves from the bottom (below 30 event cards) to a LEFT PANEL next to the map. On mobile, it goes ABOVE the map.

- [ ] **Step 1:** In index.html, restructure the `#intel-geo-view` div:
  - Create a `.intel-top-row` flex container (desktop: `flex-direction: row`, mobile: `column`)
  - Left panel (35%): trade ideas container
  - Right panel (65%): world map
  - Below: events grid + news

- [ ] **Step 2:** CSS for split panel layout

```css
.intel-top-row {
    display: flex;
    gap: 12px;
    margin-bottom: 16px;
}
.intel-ideas-panel {
    flex: 0 0 35%;
    max-height: 500px;
    overflow-y: auto;
}
.intel-map-panel {
    flex: 1;
    min-height: 350px;
}
@media (max-width: 768px) {
    .intel-top-row { flex-direction: column; }
    .intel-ideas-panel { flex: none; max-height: 300px; }
    .intel-map-panel { min-height: 200px; }
}
```

- [ ] **Step 3:** Commit

---

## Chunk 2: Event Filters (status + region)

### Task 2: Add filter chips above event cards

**Files:**
- Modify: `static/app.js` — add filter state and rendering
- Modify: `templates/index.html` — add filter chip HTML
- Modify: `static/styles.css` — filter chip styling

- [ ] **Step 1:** Add filter chip HTML before the events grid:

```html
<div class="intel-event-filters" style="display:flex;gap:6px;flex-wrap:wrap;margin:12px 0">
    <button class="intel-filter active" data-filter="all">All</button>
    <button class="intel-filter" data-filter="active">Active</button>
    <button class="intel-filter" data-filter="escalating">Escalating</button>
    <button class="intel-filter" data-filter="developing">Developing</button>
    <button class="intel-filter" data-filter="monitoring">Monitoring</button>
    <span style="width:1px;background:#00ff4122;margin:0 4px"></span>
    <button class="intel-filter" data-region="middle_east">Middle East</button>
    <button class="intel-filter" data-region="europe">Europe</button>
    <button class="intel-filter" data-region="north_america">Americas</button>
    <button class="intel-filter" data-region="east_asia">Asia</button>
    <button class="intel-filter" data-region="africa">Africa</button>
</div>
```

- [ ] **Step 2:** JS filter logic in intelligence.loadWorldEvents():
  - Track `_eventStatusFilter` and `_eventRegionFilter`
  - Click handler toggles filter, re-renders event cards
  - Filter: if status filter active, only show matching status. If region filter active, only show matching region. Both can be combined.

- [ ] **Step 3:** Commit

---

## Chunk 3: Collapsed Event Cards (tap to expand)

### Task 3: Make event cards collapsed by default with expand on click

**Files:**
- Modify: `static/app.js` — change event card rendering
- Modify: `static/styles.css` — collapsed/expanded styles

Currently event cards show full analysis text, making the page very long. Change to:
- **Collapsed (default):** title, status badge, trajectory, age. 2 lines max.
- **Expanded (on click):** full analysis text, developments, linked events, news citations.

- [ ] **Step 1:** Update event card rendering in app.js

```javascript
// Each event card:
<div class="intel-event-card collapsed" onclick="this.classList.toggle('collapsed')">
    <div class="intel-event-header">
        <h3>${title}</h3>
        <span class="expand-icon">▸</span>
    </div>
    <div class="intel-event-meta">${statusBadge} ${trajectory} ${age}</div>
    <div class="intel-event-body">
        ${fullAnalysisText}
        ${developments}
        ${linkedEvents}
    </div>
</div>
```

- [ ] **Step 2:** CSS:

```css
.intel-event-card.collapsed .intel-event-body { display: none; }
.intel-event-card.collapsed .expand-icon { transform: rotate(0deg); }
.intel-event-card:not(.collapsed) .expand-icon { transform: rotate(90deg); }
```

- [ ] **Step 3:** Commit

---

## Chunk 4: Trade Idea Cards — Compact with Quick Action

### Task 4: Redesign trade idea cards for the side panel

**Files:**
- Modify: `static/app.js` — compact idea card rendering
- Modify: `static/styles.css` — idea card styles

Current trade ideas show full analysis text (200+ words each). In the side panel, they need to be compact:

```
┌─────────────────────────────┐
│ ■ intel  BTC $80K March?    │
│ BUY YES @ ¢13    MEDIUM    │
│ Kelly edge ~67%             │
│ [Execute] [Dismiss] [▸ More]│
└─────────────────────────────┘
```

- [ ] **Step 1:** Compact card render with expandable reasoning
- [ ] **Step 2:** CSS for compact cards in side panel
- [ ] **Step 3:** Commit

---

## Chunk 5: Mobile Optimizations

### Task 5: Mobile-specific Intel improvements

**Files:**
- Modify: `static/styles.css` — mobile overrides
- Modify: `static/app.js` — mobile behavior

- [ ] **Step 1:** Map height reduced to 200px on mobile with "Tap to expand" overlay
- [ ] **Step 2:** Sub-tab buttons larger (44px min touch target)
- [ ] **Step 3:** Trade ideas as horizontal scroll cards above map
- [ ] **Step 4:** Event cards collapsed by default on mobile (same as desktop after Chunk 3)
- [ ] **Step 5:** Commit

---

## Chunk 6: News Section — Grouped by Domain

### Task 6: Group news by domain with collapse/expand

**Files:**
- Modify: `static/app.js` — grouped news rendering
- Modify: `static/styles.css` — news group styles

Instead of a flat list of headlines, group by domain:
```
▸ Geopolitics (12) — latest: 1h ago
▸ Finance & Macro (8) — latest: 2h ago
▸ Crypto (5) — latest: 30min ago
```

Click to expand shows headlines with source tags. Each group shows freshness indicator.

- [ ] **Step 1:** Group news items by source → domain mapping
- [ ] **Step 2:** Render as collapsible `<details>` elements
- [ ] **Step 3:** Commit

---

## Chunk 7: Visual Polish

### Task 7: Final typography, colors, spacing

- [ ] **Step 1:** Event status badges: use distinct colors per status
  - ACTIVE: red, ESCALATING: orange, DEVELOPING: yellow, MONITORING: blue
- [ ] **Step 2:** Trade idea cards: profit color for positive edge, warning for risky
- [ ] **Step 3:** Map container: subtle glow border on hover
- [ ] **Step 4:** Consistent spacing: 12px gaps, 6px internal padding
- [ ] **Step 5:** Bump cache versions
- [ ] **Step 6:** Commit

---

## Verification

After all chunks:
1. Run the UI audit prompt (`docs/prompts/ui-audit.md`)
2. Test at 1440px, 1280px, 768px, 390px viewports
3. Verify all interactive elements work (filters, expand/collapse, execute buttons)
4. Check console for JS errors
5. Push and deploy
