# Pixel Office tab implementation plan

> Execute inline using superpowers:executing-plans. Zeta reviews the completed exact branch.

**Goal:** Replace the dashboard Desk lead with an original animated Office while preserving its evidence panels.
**Architecture:** Browser-only canvas scene plus accessible DOM boards, one read-only API fetch through the existing tab poller. Keep the legacy renderer in a lazy details section.
**Tech stack:** Vanilla JS, CSS, canvas, existing pytest/Node checks and Playwright screenshots.
**Spec:** docs/superpowers/specs/2026-10-29-pixel-office-tab-design.md

## Constraints

No money/env/provider/runtime changes; no dependencies, random motion, legacy deletion or changes to `/office`. Alpha approved preservation/routing plan via message84d25414. Unknown is stationary, reduced motion disables animation. Original self-drawn assets only.

## Implementation

- [x] Add `tests/test_office_floor_ui.py`: Node checks for state precedence, typed event gating, first-read suppression and identity/unknown rules; integration assertions for legacy panel and poller. Run RED.
- [x] Implement `static/office-floor.js` state projection, canvas drawing, DOM boards and bounded/cancellable read lifecycle. Implement scoped `static/office-floor.css` responsive layout. No new interval.
- [x] Update `templates/index.html` Office label/panel and retain legacy Desk details; `static/app.js` Office loader and 5s active Office poller; `sniper/web/server.py` static asset version entries.
- [x] Run focused tests and JS syntax checks. Render actual dashboard template against local fixture API; use Playwright desktop/mobile, paused/unknown/reduced-motion checks and screenshots. Fix findings.
- [ ] Commit explicit owned paths through crew_commit.sh; send screenshot paths and exact SHA to Alpha/Zeta. Record brain task and episode. Alpha handles merge/deploy.

## Verification record

38 focused tests PASS (two-worker cap); initial missing asset/tab RED, unknown guard RED and bubble-wrap RED observed before implementation. Node syntax checks and diff whitespace pass. Local fixture screenshots at `/home/bigpapi/.cache/codex/epsilon-pixel-office-proof/`: desktop-running, desktop-paused, desktop-unknown, desktop-reduced-motion, mobile-running, desktop-full-router. Pixel-frame checks show running motion and paused/unknown/reduced static; text-node injection remains literal; min5s request gate and tab-switch abort verified. Actual dashboard router loads the Office panel with mocked APIs; expected legacy WebSocket/EventSource mock transport errors are separate from Office behavior. Standalone fixture panel has zero console errors. No production API/model/provider/deploy proof claimed.

Alpha reviewed screenshots before Zeta and requested wrap/plates/static furniture/lamp, then removing bottom paused overlay and enlarging lamp label. Both passes implemented. No stale worktree removal.
