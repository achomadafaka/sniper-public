# Animated Office dashboard tab

Owner Epsilon; reviewer Zeta; web deployment Alpha. Operator GO relayed by Alpha 2026-10-29.

## Scope and preservation

Replace the Solana Desk navigation label/panel lead with Office. Retain `data-tab="desk"` and `tab-desk` so saved navigation and the existing dashboard tab controller still work. Office reads only `GET /api/office/state`. Existing `/office` remains unchanged.

The old Desk is not disposable: `static/desk.js` renders desk PAPER positions, per-cohort caps, funnel/ranker, worker status, postmortem/checker reports, feeds and real copy-lane diagnostics. Preserve it unchanged inside a collapsed **Desk paper cohorts and diagnostics** section under Office, loaded only when expanded. These numbers keep their existing cohort labels and evidence limits; never add them to Office equity. Existing report links remain accessible.

## Visual design

Original top-down pixel-art office, self-drawn on a small canvas with nearest-neighbor scaling. No borrowed game imagery, names, audio, fonts or assets. No image generation or dependency needed. Palette: ink `#182f36`, slate `#44645b`, sage `#a8c58d`, parchment `#e6e8b2`, amber `#d5a76a`, muted plum `#7b6d8d`. System monospace labels sit outside the canvas for legibility; the canvas carries the scene, not essential text.

```
Office                     State cut / freshness / full evidence link
+-------------------pixel office room-------------------+
| windows / notice board / floor lights                  |
| desk + seat       desk + seat       desk + seat        |
| label/status      label/status      label/status       |
|                central aisle                          |
| desk + seat       desk + seat       desk + seat        |
+-------------------------------------------------------+
Seat cards with recorded state and public message bubbles
Equity / positions / tickets boards, exact published values
Desk paper cohorts and diagnostics [collapsed]
```

Room reflows within the available width; canvas scrolls only if required, while HTML seat cards and boards stack on mobile. Clear keyboard focus and a text equivalent for every seat/status/message. Canvas is decorative (`aria-hidden`); the accessible live region reports request/guard state rather than every animation frame.

## State and motion contract

- Each published seat gets one stationary desk sprite; at most 32 drawn seats, with explicit loaded/drawn counts if truncated. All state comes from published API fields, not prose classification or assumed work.
- Fresh heartbeat + `turn_state=running`: keyboard/typing frames. Fresh explicit waiting state: stationary waiting pose. Explicit paused/retired state, stale heartbeat, unknown heartbeat/clock or request failure: stationary, visibly labeled. Idle animation is stationary, not random roaming.
- A short walk is only an illustration of a newly observed, recent, typed `runtime_handoffs` row with exact known endpoints and real event/reference IDs. No inference from a shared coin, timestamps or message text. Never replay historical events on first load or on refresh. No animation when either endpoint is stale/unknown/paused or the guard pauses the floor. It represents a recorded handoff, not physical location or a new task assignment.
- Speech bubbles use bounded public `direct_messages` and explicitly timestamped floor `messages` with exact active-seat identity. Render using text nodes, never HTML. Show authors and stored message/receipt IDs; a message is data, not operator authority. No invented dialogue or recipient.
- Guard `state=paused` dims room lights and shows a persistent banner with the published reason. Unknown/stale guard state is labeled unknown and cannot clear a known pause. Failed polling keeps previous data labeled stale and freezes motion.
- Respect `prefers-reduced-motion` (including changes while open); render static frames and no walks. Stop requestAnimationFrame when hidden/off-tab/reduced-motion; cap scene updates at 12 fps. No sound.

## Evidence boards

Show published Office equity books, positions and tickets in separate bounded HTML lists/tables. Preserve NULL as unknown and literal zero as zero. Include source coverage/limited state. No invented aggregate PnL, no pooling books, no trade controls. Typed runtime calls supply optional last model/result labels; basis must distinguish echoed, requested-pinned and unknown. Do not substitute configured model for served identity.

## Polling and integration

Use the existing dashboard tab load/refresh controller, with one Office fetch in flight, a 2.5 second deadline, and a minimum five seconds between requests. No polling while hidden or outside the Desk/Office tab. Request failure freezes movement and labels the prior read. Visibility/tab changes cancel in-flight fetch/animation. Legacy Desk requests occur only on expanding its section or explicit refresh while expanded. No new background interval alongside the dashboard poller.

Add versioned static assets to the existing server asset-version map. Keep the legacy Desk renderer and `/office` page working. No server/provider/execution/DB schema changes.

## Edge cases, limits and rollback

Money path: display only; no entry/exit/sizing/signing code or provider calls. No additional fill-path work; p50/p90 fill latency not applicable. Concurrency: browser-local state only; no module/env/DB mutation. Unknown/zero: explicit mapping above. Failures: HTTP error, abort, timeout, missing/partial/malformed sections and future timestamps freeze affected motion and retain labeled unknowns. Units: corrected epoch seconds for API events, browser monotonic milliseconds only for animation/rate limiting. Identity: exact actor IDs/generations and typed event IDs, never name proximity. Denominator: loaded/drawn rows only, not total office history. Rollback: revert the web-only commit; additive assets can remain unused, old Desk renderer is retained.

Not handled: physical-location tracking, inferred workflow, external messages, new collectors, new model calls, historical replay, trading actions, strategy promotion or changes to `/office`. Guard repair/restarts and missing producer facts stay with their current owners.

## Checks

Focused Node assertions invoked by pytest: status precedence, NULL/zero formatting, paused/stale/unknown freeze, fresh typed-only handoff gating, deduplication, first-load suppression, reduced motion, escaped speech, request cancellation/rate limits and preserved legacy panel. Prove a RED witness before implementation. Browser screenshots on local fixture-only dashboard at desktop/mobile, running/waiting/paused/unknown and reduced motion; verify console errors and no overflow. Keep screenshots labeled fixture proof; no live-runtime or provider effectiveness claim. Reviewer Zeta checks exact commit; Alpha deploys.
