# V6 Cockpit Load Remediation

Status: approved for implementation. V6 remains paper-only. Funded execution, V6 policy,
observation cadence, and provider quote cadence are unchanged.

## Problem

The Real tab refreshes every four seconds. Its V6 cockpit currently fetches the full V6 research
summary with every refresh. That summary loads the complete observation, decision, intent, and fill
history. On the current 405 MB ledger, one uncached build took 3.65 seconds and 869 MB peak RSS.

The browser poll does not drive the V6 engine, but the report work makes the cockpit appear stalled
and adds avoidable CPU, memory, SQLite reads, JSON serialization, and local transfer.

## Design

- Keep the existing full summary endpoint for explicit research use.
- Add the small operational fields used by the cockpit to a bounded cockpit snapshot. Compute them
  with bounded SQL aggregates and the existing admission summary; do not materialize history.
- Stop the cockpit from calling the full summary endpoint.
- Reuse the dashboard's native `EventSource` pattern for one V6 stream while the Real tab is visible.
  Gate emissions on a cheap V6 position/decision/admission revision, cap checks at two per second,
  and emit the bounded snapshot only when that revision changes. Market-event and latency-only DB
  writes must not trigger a render.
- Coalesce pushed snapshots into one animation-frame render. Close the stream when the tab is hidden
  or the operator leaves Real. Native reconnect plus a 30-second fallback poll covers disconnects.
- Render an explicit idle explanation from current admission state instead of looking broken.
- Preserve the isolated V6 database, read-only GET routes, paper-only authority, and existing censor
  control.

## Verification

- A regression proves the cockpit snapshot provides operational state without calling the full
  research summary.
- Stream tests prove unrelated market-event/latency writes do not emit a new snapshot and a V6
  decision/admission change does.
- UI tests prove the Real tab uses one visible-only stream, coalesces renders, and keeps the fallback
  poll without calling the summary endpoint.
- Focused V6 API/UI/report tests, JavaScript syntax validation, and `git diff --check` must pass.

## Non-goals

- No V6 policy, wallet admission, Solana admission, quote routing, provider pacing, persistence, or
  funded-path changes.
- No new WebSocket broker, worker-to-web callback, dependency, or shared dashboard stream coupling.
- No historical data rewrite, deletion, service restart, deployment, or production DB mutation.
