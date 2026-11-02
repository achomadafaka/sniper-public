# HL Liquidation Tracker Tab — UI Design Spec

**Date:** 2026-03-27
**Status:** Draft
**Context:** Dedicated tab for the Hyperliquid liquidation proximity strategy. Each strategy gets its own tab designed around what it specifically does.

## Core Principle

The tab answers one question: **"Is any whale close to getting liquidated?"**

The closest-to-liquidation position gets the most visual weight. Everything else supports interpreting that number.

## Data Source

`hl_whale_positions` table (populated by `hl_whale_poller.py` every 60s). Currently: 296 positions across 8 wallets.

## Layout: 4 Sections, Top-to-Bottom

### Section 1: Threat Banner (always visible, never scrolled away)

A full-width banner showing the single most critical datapoint.

**When no threat (distance > 15% for all positions):**
```
ALL CLEAR — monitoring 296 positions across 8 wallets
Closest: 92.8% (0xecb63c.. BTC SHORT 20x)
Last poll: 12s ago
```
Background: dark surface, green left border accent.

**When approaching (5% < distance < 15%):**
```
⚠ APPROACHING — 0xecb63c.. BTC SHORT 20x at 8.2% distance
Liq price: $74,200 | Mark: $68,500 | Value: $142,000
```
Background: dark surface, yellow left border, yellow text.

**When imminent (distance < 5%):**
```
🔴 IMMINENT — 0xecb63c.. BTC SHORT 20x at 2.1% distance
Liq price: $70,100 | Mark: $68,500 | Value: $142,000
SIGNAL READY: counter-LONG BTC
```
Background: dark surface, red left border, pulsing red glow, red text.

Implementation: single `div.threat-banner` with CSS classes `.threat--clear`, `.threat--approaching`, `.threat--imminent`.

### Section 2: Position Table (70% of viewport)

Sorted by `distance_pct ASC`. This is the main view.

**Columns:**

| Column | Width | Content |
|--------|-------|---------|
| Wallet | 120px | Truncated address + label from `hl_tracked_wallets` |
| Asset | 60px | BTC, ETH, SOL, etc. |
| Side | 60px | LONG (green) / SHORT (red) |
| Leverage | 50px | e.g., "20x" |
| Value | 90px | Position value in USD |
| Distance | 120px | Percentage + inline color bar (gradient: green→yellow→red) |
| Mark Price | 90px | Current mark from poller |
| Liq Price | 90px | Liquidation price (or "cross" for cross margin) |
| Margin | 70px | "isolated" or "cross" badge |

**Row styling:**
- Default: normal surface color
- distance < 20%: yellow left border (`.pos-row--watch`)
- distance < 10%: yellow background tint (`.pos-row--warn`)
- distance < 5%: red left border, subtle pulse (`.pos-row--danger`)

**Click behavior:** Clicking a row filters the wallet sidebar (Section 4) to that wallet.

**Auto-refresh:** Own `setInterval(hlLiquidation.load, 60000)` that clears on tab exit (same pattern as shellCluster). Does NOT use the global tabPoller (which is hardcoded to 90s and not per-tab configurable). Defers when `document.hidden`.

### Section 3: Strategy Brain Panel

Two sub-panels side by side:

**Left: Gate Checklist (when a position is within threshold)**
```
Strategy: HL Liquidation v1 — State: SCANNING

Entry conditions for nearest signal:
  [x] distance_pct < 5%        actual: 3.2%
  [x] position_value > $100K   actual: $450,000
  [x] funding confirms bearish  rate: -0.05%/8h
  [ ] OI increasing             OI: flat (-0.2%)
  ─────────────────────────────
  3/4 passed — BLOCKED: OI not confirming
```

When no position is near threshold:
```
Strategy: HL Liquidation v1 — State: SCANNING
No positions within 15% of liquidation.
Monitoring 296 positions across 8 wallets.
Next poll: 48s
```

**Right: Decision Log (last 10 evaluations)**
```
21:19:38  SCAN  296 positions checked, 0 within threshold
21:18:38  SCAN  296 positions checked, 0 within threshold
21:17:38  SCAN  295 positions checked, 0 within threshold
```

When signals are evaluated:
```
21:19:38  EVAL  0xecb63c.. BTC SHORT — dist=3.2%, BLOCKED(oi_flat)
21:18:38  SCAN  296 positions checked, 1 within threshold
```

Color coding: SCAN = dim gray, EVAL = yellow, ENTER = green, BLOCKED = amber, ERROR = red.

### Section 4: Wallet Sidebar (expandable, right side)

Clicking a wallet row in the position table opens a right sidebar showing:

- **Wallet header:** Full address, label, source, tracked since
- **Account health bar:** Visual bar showing `account_value` vs `crossMaintenanceMarginUsed` (cross margin wallets)
- **All positions:** Full list of this wallet's positions with same columns as main table
- **Close button** to collapse sidebar

For v1: the sidebar can be a simple expandable panel below the table (no need for a CSS grid right panel). Simpler to build, same information.

## New API Endpoints

| Endpoint | Returns |
|----------|---------|
| `GET /api/algorithms/hl-liquidation/dashboard` | See response shape below |
| `GET /api/algorithms/hl-liquidation/wallet/<addr>` | All positions + account health for one whale |
| `GET /api/algorithms/hl-liquidation/state` | Strategy state + gate checklist + last 10 decision log entries |

**Dashboard response shape:**
```json
{
  "threat_level": "clear",
  "closest_distance_pct": 92.8,
  "closest_position": {"wallet": "0xecb63c..", "asset": "BTC", "side": "short", "leverage": 20},
  "wallet_count": 8,
  "position_count": 296,
  "long_count": 132,
  "short_count": 164,
  "danger_count": 0,
  "last_polled_at": "2026-03-27T21:19:38Z",
  "positions": [
    {"wallet": "...", "label": "...", "asset": "BTC", "side": "short", "leverage": 20,
     "position_value_usd": 142000, "distance_pct": 92.8, "mark_price": 68500,
     "liquidation_price": 74200, "margin_type": "cross", "entry_price": 67000,
     "unrealized_pnl": -1500}
  ]
}
```

All endpoints read from `hl_whale_positions` and `hl_tracked_wallets` tables. `added_at` column maps to "tracked since" in the wallet detail view.

## Navigation

Add a new nav item "HL Liq" between "Strategies" and "Intel" in `templates/index.html`. Tab ID: `hl-liquidation`.

**Mobile nav:** Do NOT add a 7th mobile nav item (will overflow on 320px screens). Instead, add HL Liq to the top nav only. On mobile, accessible via hamburger menu or horizontal scroll. Add `overflow-x: auto` to `.mob-nav` if not already present.

Each future strategy tab follows the same pattern — its own nav item, its own section, its own API endpoints. No generic "Algorithms" tab needed.

## Files

| File | Change |
|------|--------|
| `templates/index.html` | New tab section `tab-hl-liquidation` + nav item |
| `static/app.js` | New `hlLiquidation` tab handler: load, render, auto-refresh |
| `static/styles.css` | `.threat-banner`, `.pos-row--watch/warn/danger`, gate checklist styles |
| `routes/api_algorithms.py` | New blueprint with 3 HL liquidation endpoints |
| `web_server.py` | Register algorithms blueprint |
| ~~`routes/contexts.py`~~ | Not needed — no shared contexts module exists. Blueprint uses inline `ctx` dict. |

## Design Tokens (matching existing UI)

- Surface: `var(--surface-1)` / `var(--surface-2)`
- Text: `var(--text-strong)` (matrix green), `var(--text-dim)` (dark green)
- Data values: `JetBrains Mono`, tabular-nums
- Cards: `.op-card` pattern with accent border
- Section headings: uppercase, 0.85rem, `var(--text-muted)`
- Positive/negative: `var(--state-positive)` / `var(--state-negative)`
- Warning: `var(--state-warning)` (#ccff00)

## Constraints

- Read-only tab — no actions (no manual entry/exit buttons). Strategy plugin (Phase 5B) handles execution.
- 60s refresh matches poller cadence. No sub-second updates needed for paper mode.
- Position table limited to 500 rows (currently 296, plenty of headroom).
- Mobile: position table scrolls horizontally. Threat banner and brain panel stack vertically.
