# Hyperliquid Edge Atlas Design

## Status

Approved by the operator on 2026-07-27 as part of the instruction to inspect and improve the
Hyperliquid UI without further approval checkpoints. This extends the read-only Edge Desk. It does
not authorize funded execution, exchange calls, paper-ledger mutation, or strategy promotion.

## Problem

The Edge Desk reports runtime and provenance, but it does not show the decomposition that found the
alt-short candidate. The useful evidence remains buried in a manually triggered scorecard under a
1,000-row wallet-research surface. Hyperliquid venue navigation also lands on that wallet list
instead of the decision desk.

The historical follow ledger has changed since the original finding. Alt-short remains the only
positive universe/side cell, but its recent generic-follow performance has weakened. The copy book
still preserves more of the result because wallet-directed exits truncate the negative tail. A
static historical study receipt alone therefore overstates how actionable the edge looks today.

## Goals

1. Make the Edge Desk the Hyperliquid venue home.
2. Put the fixed 2x2 edge decomposition on the first screen: core/alt by long/short.
3. Separate historical evidence, recent evidence, and forward confirmation.
4. Show why copy and standalone results diverge: source-directed exits versus time-cap losses.
5. Name the next falsifiable research direction without presenting a trade trigger or sizing rule.
6. Use only local persisted data through SQLite read-only mode. Page load makes no exchange call.

## Non-Goals

- No funded controls, order path, runner, watchdog, cron, schema, or database write.
- No automated wallet selection, coin selection, entry filter, trade advice, sizing, or expected PnL.
- No resurrection of the inactive Market Radar surface or its absent paper ledger.
- No post-hoc search over arbitrary cells, windows, coins, wallets, hours, or thresholds.
- No claim that one bearish episode or one positive cell proves a durable edge.

## Information Architecture

The Hyperliquid venue opens `hl-algo` (Edge Desk). `HL Wallet Research` remains reachable as a
secondary section for audit and manual investigation.

The desk order is:

1. **Decision strip** — runtime, regime, forward sample, promotion gate, funded dry-run truth.
2. **Edge Atlas** — fixed concentration matrix, recent pulse, exit gap, and next falsification.
3. **Sealed study receipt** — provenance and retrospective causal evidence.
4. **Paper ledger** — current lane first; legacy and reconciliation evidence collapsed.

This order answers: What is running? Where did the result live? Is it still present? What must be
tested next? What evidence supports that conclusion?

## Edge Atlas Contract

`edge_atlas` is assembled by a Flask-independent read model from closed rows in
`hl_follow_positions`, closed rows in `hl_copy_book`, and current-arm observations. Symbols are
classified through the existing versioned alt-short universe.

### Fixed concentration matrix

The only cells are the preregistered strategy buckets:

- core / long
- core / short
- alt / long
- alt / short

Each cell shows `n`, mean net bps, median net bps, win rate, total net bps, and a calendar-day block
bootstrap interval. The matrix is descriptive. A positive mean is not labeled validated when the
interval includes zero.

### Temporal pulse

The alt-short follow cell shows:

- full history;
- the last seven corrected-UTC days;
- the latest six non-empty UTC calendar weeks;
- last-close age and data-through timestamp.

Status labels are reporting labels, not a promotion or execution gate:

- `recently_supported`: at least 30 recent closes, positive mean and median, and block interval
  above zero;
- `degraded`: at least 30 recent closes but those conditions do not all hold;
- `insufficient_recent`: fewer than 30 recent closes;
- `no_history`: no closed alt-short follows.

### Exit gap

The Atlas compares alt-short follow rows closed by `mirror_exit` with rows closed by `time_cap` and
shows fixed hold buckets (`<1h`, `1-4h`, `4-12h`, `12-48h`, `48h+`). It also compares the recent
generic-follow result with recent non-cold-start alt-short copy closes.

Hold duration is outcome attribution only; it is never presented as an entry filter because it is
unknown at entry.

### Forward confirmation

Current-arm observation counts show open and closed samples. Zero closes is rendered as no forward
confirmation, even when retrospective evidence is positive.

### Next direction

The primary research question is fixed from the observed failure path:

> Can source-directed wallet exits preserve the alt-short result forward by avoiding the time-cap
> and 12-48 hour loss tail?

The next milestone is descriptive and falsifiable: accrue current-arm closes, compare source-exit
and fallback-exit outcomes, and require multiple distinct regime episodes before promotion. Wallet
and coin cohorts remain audit drill-downs with multiple-testing warnings, not selection rules.

## Error Handling

- Missing follow/copy tables: `edge_atlas.available=false`; the rest of the desk remains usable.
- No closed rows: return explicit empty cells and `no_history`.
- Missing timestamps: include the row in full-history statistics when PnL exists, but exclude it
  from temporal and block-bootstrap views.
- Unknown side or universe: exclude from the matrix and report the excluded count.
- Sparse day blocks: return no interval instead of falling back to an IID trade bootstrap.
- Current-arm unavailable: forward status is `unknown`, never inferred from the newest row.

## Tests

- fixed 2x2 cell membership and net-of-fee field use;
- day-block interval does not expose bootstrap samples;
- seven-day boundary and weekly ordering;
- degraded status under recent loss-tail drift;
- mirror-exit versus time-cap and fixed hold buckets;
- non-cold-start copy comparison;
- current-arm open/closed confirmation counts;
- missing schema degrades only the Atlas;
- Hyperliquid venue home routes to the Edge Desk;
- UI contains the matrix, pulse, exit gap, next test, and explicit research warnings.
