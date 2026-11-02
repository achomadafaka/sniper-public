# Proof Bar Dashboard — Design Spec

## Goal

Add a persistent proof strip and dedicated Proof Bar tab to the Sniper dashboard that shows real-time go-live readiness against the 5 proof bar criteria, so the operator always knows what's blocking real-money deployment and how fast the system is converging.

## Feature Freeze Exemption

CLAUDE.md prohibits "new features, tabs, or strategy families during the validation freeze." The Proof Bar tab is **operational tooling for the freeze itself** — it visualizes the criteria that define when the freeze ends. It does not change trading behavior, add strategy logic, or introduce new data collection. It is the instrument panel for the validation phase, not a feature.

## Proof Bar Criteria (from CLAUDE.md)

| # | Criterion | Target | Source |
|---|-----------|--------|--------|
| 1 | Closed trades | >= `PROOF_MIN_TRADES` (default 150) at insider >= `PROOF_MIN_INSIDER` (default 60) | `paper_trades` joined with `signal_insider_score` |
| 2 | Win rate | >= `PROOF_MIN_WIN_RATE` (default 60%) on qualifying trades (entry_price < 0.80, per ADR-002) | Same dataset, filtered |
| 3 | Slippage-adjusted P&L | > $0 after `PROOF_SLIPPAGE_PCT` (default 2.5%) one-sided haircut per trade | Same dataset, `roi_percent` with slippage deduction |
| 4 | No losing month | `PROOF_MIN_MONTHS` (default 2) full calendar months, all profitable (slip-adj) | Same dataset, grouped by `closed_at` month |
| 5 | Health checks | All proof-required integrity checks clean | `get_runtime_health()` + data integrity queries |

All threshold constants are configured via `web_settings.py` loaded from `.env`, per project convention. No magic numbers in code.

## Architecture

Layout model C: **persistent proof strip** (visible on every tab) + **dedicated Proof Bar tab** (9th tab).

The strip is a thin status bar below the header. It shows all 5 criteria inline with pass/fail/warn coloring. Clicking any criterion chip scrolls/navigates to its detail section in the Proof Bar tab.

All data comes from existing DB tables via new query helpers in `db.py`. No new tables needed. The tab is read-only — no mutations.

## Component Structure

### 1. Persistent Proof Strip

- Location: below header bar, above tab content, on every tab
- Height: ~28px single row
- Contents: `PROOF` label, then 5 criterion chips separated by dividers, then overall badge

**Chip format per criterion:**
| Criterion | Chip text |
|-----------|-----------|
| Trades | `Trades {n} / {PROOF_MIN_TRADES}` |
| Win rate | `WR {pct}% / {PROOF_MIN_WIN_RATE}%` |
| Slip PnL | `Slip PnL {+/-$x}` |
| Months | `Months {n} / {PROOF_MIN_MONTHS}` |
| Health | `Health {OK/FAIL}` |

**Color rules:**
- Green (pass): criterion met
- Red (fail): criterion not met, blocking
- Amber (warn): criterion-specific thresholds defined below

**Warn thresholds per criterion:**
| Criterion | Warn condition |
|-----------|---------------|
| Trades | >= 90% of target (e.g. 135+ of 150) |
| Win rate | Within 2 percentage points of target (e.g. 58-59.9% when target is 60%) |
| Slip PnL | Positive but < $50 (close to crossing zero) |
| Months | Exactly 1 complete profitable month (halfway there) |
| Health | Any advisory service degraded (proof-required all OK) |

**Overall badge:** "NOT READY" (red) if any criterion fails. "GO-LIVE READY" (green) if all pass.

**Interaction:** Each chip is a link. If already on Proof Bar tab, smooth-scrolls to the criterion's detail section (`id="proof-{criterion-key}"`). If on another tab, navigates to Proof Bar tab and scrolls.

### 2. Proof Bar Tab — Section Layout (top to bottom)

#### 2a. Title + Proof Definition
- `Proof Bar — Go-Live Readiness`
- Subtitle: `Paper trades - insider >= {PROOF_MIN_INSIDER} - slippage haircut {PROOF_SLIPPAGE_PCT}% - full calendar months only`
- Gray, small, always visible — makes the tab self-documenting

#### 2b. Blocking Go-Live (top priority section)
- Red-bordered card, only shown when at least one criterion fails
- Lists each failing/at-risk criterion with:
  - Icon: `x` (fail, red) or `o` (warn, amber)
  - Bold label + current value vs target
  - Fix hint line (amber text, e.g. "At ~2.3 trades/day, earliest completion: ~Apr 23")
- When all criteria pass: this section is replaced with a green "ALL CRITERIA MET — READY FOR GO-LIVE" banner

#### 2c. Five Criteria Cards (grid, 5 columns)
Each card has an `id` attribute (`id="proof-closed-trades"`, etc.) for scroll targeting.

Each card contains:
- Title (uppercase, small)
- Big value (colored by status)
- Target description
- Progress bar (fill colored by status)
- Progress label: `{pct}% of target` (italic, dimmed — not confused with the metric itself)
- Detail line (context, ETA, etc.)
- Status dot (top-right corner, colored)

#### 2d. System Health Panel
- Section heading: `System Health` (`id="proof-health"`)
- Two visually distinct groups:
  - **Proof-required** (affects criterion 5): Stream, Analyzer, Paper Trader, Data Integrity
  - **Advisory** (does not block go-live): DB Size, Alerter — visually dimmed with "(advisory)" label
- Each service tile: status dot (green/amber/red) + label + detail line
- Advisory items are clearly marked so operator doesn't confuse alerter-disabled with a proof-bar failure

**Note on historical health (criterion 5):** For v1, health is evaluated as current-state only (point-in-time check). The proof bar definition says "over the same window," but the system currently has no health history table. v1 treats health as "currently clean" which is sufficient for the dashboard to show real-time status. Historical health tracking (uptime SLA over the proof window) is deferred to a future iteration — it requires a new `health_history` table and periodic health snapshots, which is out of scope for this operational dashboard.

#### 2e. Pace & Accumulation
- 4-card grid:
  - **7-Day Pace**: trades/day over last 7 days
  - **Trades to Go**: remaining trades needed, ETA at current pace
  - **Cumulative Equity**: slip-adjusted all-time P&L
  - **Open Exposure**: total USDC deployed in open positions
- Below: **Cumulative Closed Trades vs Target** chart
  - X-axis: date (daily granularity, one point per day)
  - Y-axis: cumulative closed trades
  - Dashed line at target (150)
  - Solid green line showing actual accumulation
  - Canvas-drawn, matches existing chart style (matrix green on black)

#### 2f. Quality Trend
- **30-Day Slip-Adjusted Equity Curve** (chosen over rolling WR because it captures both WR and sizing quality)
  - X-axis: date (daily granularity)
  - Y-axis: cumulative slip-adj P&L
  - Horizontal dashed line at $0 (the threshold)
  - Canvas-drawn, same style as accumulation chart
- This ensures the screen is not quantity-biased — operator sees quality trajectory alongside trade count

#### 2g. Monthly Breakdown
- Table with columns: Month, Trades, Wins, Losses, Win Rate, P&L (raw), P&L (slip-adj), Status
- Status values: "Profitable" (green check), "Loss" (red x), "In progress" (amber circle)
- Only months with at least 1 trade are shown
- Note below table: "System started {month}. Months count toward proof bar only after they close."

## Data Flow

### New DB helpers needed (all in `db.py`)

```
proof_bar_status(conn) -> dict
```
Single entry point. Returns:
```python
{
    "criteria": {
        "closed_trades": {"value": 49, "target": 150, "status": "fail"},
        "win_rate": {"value": 59.2, "target": 60.0, "status": "warn"},
        "slip_pnl": {"value": 441.0, "target": 0.0, "status": "pass"},
        "no_losing_month": {"value": 0, "target": 2, "status": "fail"},
        "health": {"value": "pass", "target": "pass", "status": "pass"},
    },
    "overall": "not_ready",  # or "ready"
    "pace": {
        "seven_day_rate": 2.3,
        "trades_to_go": 101,
        "eta_date": "2026-04-23",
        "cumulative_equity": 441.0,
        "open_exposure": 225.0,
        "open_positions": 9,
    },
    "monthly": [
        {"month": "2026-03", "trades": 49, "wins": 29, "losses": 20,
         "win_rate": 59.2, "pnl_raw": 515.0, "pnl_slip": 441.0,
         "complete": False, "profitable": True},
    ],
    "health_details": {
        "stream": {"status": "ok", "detail": "Connected, 1817 markets, last trade 2m ago", "proof_required": True},
        "analyzer": {"status": "ok", "detail": "Last run 45s ago", "proof_required": True},
        "paper_trader": {"status": "ok", "detail": "Last cycle 1m ago, 9 open positions", "proof_required": True},
        "data_integrity": {"status": "ok", "detail": "0 orphans, 0 drift, 0 stale", "proof_required": True},
        "db_size": {"status": "ok", "detail": "142 MB, WAL mode", "proof_required": False},
        "alerter": {"status": "warn", "detail": "Disabled (needs Telegram token)", "proof_required": False},
    },
    "charts": {
        "cumulative_trades": [{"date": "2026-03-01", "count": 0}, ...],  # daily granularity
        "equity_curve_30d": [{"date": "2026-03-01", "equity": 0.0}, ...],  # daily granularity
    },
    "data_gaps": {
        "null_roi": 0,        # trades excluded due to NULL roi_percent
        "zero_entry_price": 0  # trades excluded due to entry_price <= 0
    },
}
```

### Configuration constants (via `web_settings.py`)

```python
PROOF_MIN_TRADES = int(os.getenv("PROOF_MIN_TRADES", "150"))
PROOF_MIN_INSIDER = int(os.getenv("PROOF_MIN_INSIDER", "60"))
PROOF_MIN_WIN_RATE = float(os.getenv("PROOF_MIN_WIN_RATE", "60.0"))
PROOF_SLIPPAGE_PCT = float(os.getenv("PROOF_SLIPPAGE_PCT", "0.025"))
PROOF_MIN_MONTHS = int(os.getenv("PROOF_MIN_MONTHS", "2"))
```

### Slippage calculation

Slippage is **one-sided** (entry only). Rationale: paper trades don't execute on-chain, so there's no real exit slippage to model. The haircut simulates the cost of getting filled at a worse price than the signal's entry_price.

Per-trade slippage = `size_usdc * PROOF_SLIPPAGE_PCT`.
Slip-adjusted P&L per trade = `(roi_percent / 100) * size_usdc - slippage`.
Cumulative = sum across all qualifying closed trades.

Trades with NULL `roi_percent` are excluded from P&L calculation (treated as data gaps, not zero). They still count toward the trade count if they have a valid `status`.

### Qualifying trades filter

Two populations exist:

**Trade count population** (criterion 1):
```sql
WHERE signal_insider_score >= {PROOF_MIN_INSIDER}
  AND status IN ('resolution_win', 'resolution_loss', 'early_exit', 'manual')
```
This intentionally excludes `source_exited` and `stale` statuses. Those are valid terminal states but represent inconclusive outcomes (the tracked wallet left or the position timed out) — they don't demonstrate signal quality. The proof bar should only count trades with clean resolution paths.

This is the count that must reach 150.

**Win rate population** (criterion 2):
```sql
WHERE signal_insider_score >= {PROOF_MIN_INSIDER}
  AND status IN ('resolution_win', 'resolution_loss', 'early_exit', 'manual')
  AND entry_price < 0.80
```
This is the denominator for win rate, per ADR-002. Win rate is computed over this subset only. The entry_price < 0.80 filter excludes certainty-buyers (see ADR-002). This means the WR denominator may be smaller than the trade count.

**Win definition:** A win is a trade where `pnl_usdc > 0`. This matches the paper trader's existing WR calculation.

**P&L population** (criterion 3):
Same as trade count population (all qualifying closed trades, regardless of entry_price). Slippage is applied to all trades, not just the WR-qualifying subset.

### Monthly grouping

Group by `strftime('%Y-%m', closed_at)`. Trades with NULL `closed_at` are excluded from monthly grouping (this would be a data integrity bug — the proof bar will not silently absorb it).

A month is "complete" only if current UTC date is past the last day of that month. Incomplete months are shown but not counted toward criterion 4.

Guard: `entry_price > 0` before any division. Trades with `entry_price <= 0` are excluded.

## API Endpoint

```
GET /api/proof-bar
```

Registered in `routes/api_core.py` (alongside other dashboard-level endpoints like `/api/market-intelligence`).

Returns the `proof_bar_status()` dict wrapped in standard JSON response. No parameters needed. Called by the frontend on tab load and on a 60-second auto-refresh interval. Refresh only fires when the page is visible (`document.visibilitychange` check) to avoid unnecessary queries on the 2vCPU/2GB VPS.

Also exposed as MCP read tool `get_proof_bar_status` in `mcp_tools/read_tools.py`, following existing tool patterns (no parameters, returns `make_envelope(proof_bar_status(conn), data_source="proof_bar")`).

## Frontend

### Files modified
- `templates/index.html`: Add proof strip HTML below header, add Proof Bar tab to nav, add tab content container
- `static/app.js`: Add `loadProofBar()` function, proof strip rendering, chart drawing, auto-refresh with visibility check

### Chart rendering
Canvas-drawn, same pattern as existing equity curve in Strategies tab:
- Black background, green (#00ff41) data line, dashed gray target/threshold line
- Axis labels in monospace green
- No external charting library

### Proof strip rendering
- Injected once on page load, updated on every `loadProofBar()` call
- Strip is outside the tab-content switcher so it persists across tab changes
- Click handlers on each chip: `showTab('proof-bar'); scrollToSection('proof-{criterion-key}')`

## Error Handling

- If `proof_bar_status()` DB query fails, strip shows "PROOF DATA UNAVAILABLE" in amber
- Individual criteria that can't be computed show "N/A" instead of a value
- Health checks that timeout show "UNKNOWN" with amber status
- Frontend gracefully degrades: if API returns error, last-known values persist with a "stale" indicator
- Trades with NULL `roi_percent` or `entry_price <= 0` are silently excluded (counted in a `data_gaps` field in the response for transparency)

## Testing

- Unit tests for `proof_bar_status()` with seeded paper_trades data
- Test monthly grouping edge cases (0 trades, 1 month, month boundary, NULL closed_at)
- Test slippage calculation (one-sided, correct formula)
- Test status thresholds (pass/warn/fail boundaries for all 5 criteria)
- Test overall readiness logic (all pass = ready, any fail = not ready)
- Test health advisory vs proof-required distinction
- Test WR population vs trade count population are correctly separated
- Test NULL roi_percent and entry_price <= 0 exclusion
- Integration test: API endpoint returns valid envelope via `routes/api_core.py`

## Scope Exclusions

- No write operations (proof bar is read-only)
- No historical health tracking (v1 is point-in-time; deferred)
- No Telegram integration (separate subsystem)
- No strategy tuning (separate subsystem)
- No position management (separate subsystem)
- Strip does not show on mobile — desktop-only dashboard
