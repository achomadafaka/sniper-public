# Hyperliquid Edge Desk Operator Ticket Design

## Goal

Make the Edge Desk answer four questions immediately: is the alt-short paper test running, what rule is being tested, what has happened in the current arm, and why funded promotion remains unavailable.

## Approved layout

```text
ALT-SHORT PAPER TEST                         LIVE

Scanning 174 alts. No qualifying setup yet.
0 / 200 complete closes  ━━━━━━━━━━━━━━━━━━━

0 qualifying     0 open     8h captured

Rule
+30% in 72h → short · skip top-5% funding
15% target · 20% stop · 24h maximum · $40 paper

PROMOTION PAUSED · NO FUNDS · NO ORDERS

Why not promoted ▾
```

## Information hierarchy

The always-visible surface contains only current-arm collection health, close progress, qualifying/open counts, captured regime hours, the exact frozen rule, paper clip, and the funded-authority boundary. Zero events are stated as an active empty state, not as a stopped strategy.

Historical candle censorship, source timing, capacity capture, copy-book totals, concentration, retained rows, and integrity metadata remain available only inside the collapsed audit detail. They cannot compete with current test state.

## Visual system

- Abyss `#08111a`, panel `#0e1b26`, ink `#e7eef5`, slate `#8ca0b3`, live cyan `#5ec8e5`, warning amber `#f2b84b`.
- Inter/system sans for explanations; monospace for state, counts, and the progress rail.
- The signature element is one horizontal evidence rail tied to `complete_net_closes / proof_target`.
- No motion, gradients, new dependency, new API request, or action control.
- Responsive layout collapses to one column below 720px; the audit remains native `<details>`.

## Data and truth rules

- `FORWARD TEST RUNNING` requires `edge_paper.available=true` and `edge_paper.fresh=true`.
- A stale display or collector cannot retain the running visual treatment.
- Progress, counts, hours, universe, policy, clip, and concurrency come from the existing schema-v3 payload.
- Promotion remains explicitly paused and paper-only regardless of collection health.
- Unknown values render as `—`; no missing value is converted into evidence.
- The current API compatibility fallback remains until the restarted server loads the additive `evidence_debt.collection` field.

## Verification

- A UI contract test must fail while the copy-control and four-card runway remain on the primary surface.
- Focused Edge Desk/API tests, `node --check static/app.js`, `git diff --check`, and schema validation must pass.
- Deployment must use the exact commit on `origin/main`, switch `.runtime/web/current`, restart the system service, and verify one process, HTTP 200, served asset hashes, fresh paper/tape heartbeats, and current-arm counts.
- No Playwright is used.
