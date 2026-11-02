# Memecoin X Adaptive Browser Pacing Design

**Date:** 2026-07-14
**Status:** Implemented and fixture-tested in userscript v1.5; installed live. The bounded HTTP-403/blank-page freeze worked, X recovered without a forced retry, and the fresh parser-v2 24-hour acceptance window is accumulating healthy runs.
**Scope:** Read-only browser-capture pacing and recovery only

## 1. Goal

Replace the current rapid fixed scroll plus 120-240 second reload loop with a lower-load,
DOM-aware scheduler that preserves bounded forward capture. The change must improve page
readiness and sentinel recovery without adding account engagement, unbounded scrolling, hidden
coverage claims, or any dependency on funded execution.

This addendum supersedes the cadence and fixed-scroll defaults in section 5 of
`2026-07-13-memecoin-x-source-graph-design.md`. All other source-graph contracts remain in force.

## 2. Selected Approach

Use an adaptive scheduler plus DOM-aware bounded scrolling.

Rejected alternatives:

- A timer-only slowdown leaves the observed DOM-hydration race unchanged.
- A passive observer without reloads cannot retrieve posts that X never renders in the tab.
- Automated likes, reposts, replies, follows, or other engagement are not capture operations and
  remain forbidden.

The scheduler smooths load and waits for observable DOM progress. It must not claim to imitate a
person or attempt to bypass a challenge, rate limit, or other X control.

## 3. Components

### 3.1 Readiness gate

After initial load or reload, wait up to 30 seconds for both:

1. the Following tab to be selected; and
2. at least one canonical post card to be available.

Challenge, rate-limit, logout, and authenticated wrong-feed states remain immediate operator
states. A page that has not rendered enough DOM to distinguish those states after 30 seconds is
recorded as `page_unready`; it is not mislabeled as `wrong_feed`.

`page_unready` is a diagnostic capture state with zero cards. It backs off for 15 minutes before
one same-page readiness retry. A second consecutive `page_unready` freezes browsing until manual
recovery. It never starts a scroll pass while the gate is unresolved.

### 3.2 User-activity quiet period

Track trusted `wheel`, `pointerdown`, and `keydown` events. A scheduled pass that falls within 90
seconds of such input is postponed until the quiet period expires. Synthetic events do not extend
the quiet period. This prevents the dedicated capture loop from fighting an operator inspecting
the tab.

### 3.3 DOM-aware bounded scroller

Each healthy pass reads the visible cards before scrolling. If the prior sentinel is not visible:

- scroll downward by 450 pixels;
- wait until a new canonical post ID appears or five seconds elapse;
- after progress, require 1.5 seconds without another canonical-ID change before reading again;
- stop as soon as the prior sentinel appears; and
- stop after three scrolls or 50 cards, preserving the existing explicit coverage-gap result.

The scroller never clicks, navigates profiles, opens cards, or scrolls indefinitely. Delivery
remains idempotent by canonical ID plus card fingerprint.

### 3.4 Adaptive scheduler

The next-pass window is selected from the acknowledged result:

| Result | Next window | State transition |
|---|---:|---|
| New cards | 5-8 minutes | Clear recovery-use state when the sentinel is found or no prior sentinel exists |
| No new cards | 10-15 minutes | Clear recovery-use state when the sentinel is found or no prior sentinel exists |
| First consecutive coverage gap | 5-8 minutes | Consume one recovery pass |
| Further coverage gap before recovery | 10-15 minutes | Do not accelerate again |
| First page-unready result | 15 minutes | No scrolling or reload; retry readiness once |
| Second consecutive page-unready result | Frozen | Manual recovery required |
| Challenge, rate limit, logout, or authenticated wrong feed | Frozen | Manual recovery required |
| Transport failure or unmatched acknowledgement | 60 seconds | Retry the same pending payload; no reload or scroll |
| Explicit clock/parser rejection | 60 seconds | Preserve fingerprints, allocate the required fresh run, and do not browse again |

Random selection inside a window exists only to smooth load. It is not described or tested as
human simulation.

The one-recovery-pass flag is scoped to the current unresolved sentinel. A successful
`sentinel_found` result or a healthy initial pass without a prior sentinel resets it. Reload
occurs only after a healthy, acknowledged pass. Operator states and readiness failures never
reload automatically.

### 3.5 Cross-tab leader lease

All `x.com/home` tabs in the dedicated browser profile share one bounded `localStorage` leader
lease. Each tab keeps a stable `sessionStorage` identity across reloads. Only the current leader
may inspect the feed, scroll, deliver, or reload; followers remain passive and retry leadership
after the lease expires. The leader renews every 45 seconds against a 120-second lease and stops
renewing when an operator state freezes capture. This prevents duplicate tabs from multiplying
browser load while preserving automatic takeover after a tab closes.

## 4. Data Flow

1. Acquire or renew the shared profile lease; passive tabs perform no feed work.
2. Wait for page readiness and operator quiet time.
3. Inspect the authenticated Following-feed state.
4. Read visible canonical cards.
5. Use DOM-aware bounded scroll only when a prior sentinel requires catch-up.
6. Submit the immutable batch through the existing pending-delivery controller.
7. On matching committed acknowledgement, persist the newest sentinel and schedule from the
   acknowledged result.
8. On delivery failure, retain the exact pending run and retry locally without browsing again.

The receiver remains the authority for corrected observation time, parsing, normalization,
persistence, and capture-run audit.

## 5. Diagnostics and Audit

Every capture run or local scheduler decision must expose enough evidence to explain its load and
coverage behavior:

- cadence bucket selected;
- bounded scroll count;
- cumulative DOM-wait milliseconds;
- reload reason;
- `page_unready` occurrence;
- whether the one recovery pass was consumed; and
- whether operator activity postponed the pass.

These are capture diagnostics only. They do not affect source ranking, signal conviction,
notifications, or trading.

## 6. Failure Contract

- Blank or partially hydrated pages do not become wrong-feed evidence.
- Challenges, rate limits, and logouts freeze browsing actions; no watchdog loops through them.
- A missing sentinel remains a declared coverage gap.
- A receiver outage retries delivery, not page interaction.
- A tab or browser death remains a missing-heartbeat gap in the acceptance report.
- Any unsupported state fails closed to zero captured cards and no account action.

## 7. Testing Contract

Use Node's built-in test runner with fake clocks, deterministic random draws, mocked DOM mutation,
and mocked trusted user events. Tests must prove:

1. no scroll occurs before readiness;
2. a blank page becomes `page_unready`, not `wrong_feed`;
3. trusted operator input postpones a pass by the remaining 90-second quiet period;
4. each scroll is exactly 450 pixels and at most three scrolls occur;
5. the scroller waits for canonical-ID progress and 1.5 seconds of stability;
6. sentinel discovery stops immediately;
7. schedule windows match the table for boundary random draws;
8. only one accelerated recovery pass occurs per unresolved sentinel;
9. delivery failure retries the same payload without another scroll or reload;
10. operator states freeze reload and scroll behavior;
11. duplicate tabs elect exactly one collector and preserve the same tab identity across reloads;
    and
12. static guards reject click, like, follow, post, reply, repost, and arbitrary navigation calls.

Focused receiver and report tests must cover the new readiness and pacing diagnostics. The full
memecoin-X focused suite, schema validator, source guard, and `git diff --check` must pass before
the browser script version is promoted.

## 8. Rollout

1. Implement and verify the scheduler against fixtures only.
2. Update userscript v1.5 and reinstall/update it in the dedicated Tampermonkey profile.
3. Confirm one live healthy pass, one no-new pass, and no forbidden action.
4. Restart the 24-hour acceptance window after one verified v1.5 healthy pass because pacing and
   cross-tab ownership semantics changed. No v1.4 row counts toward the new window.
5. Phase 2 remains incomplete until the new 24-hour report and manual observation audit pass.

The rollout does not restart trading services, read position tables, or alter funded execution.
