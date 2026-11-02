# Office Network Room Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put a read-only wallet/developer/trader/funder network room inside the Office, with evidence and wallet/GMGN click-through.

**Architecture:** Epsilon owns `static/office-network.js`, the two Office/dashboard script tags, the Office mount, independent asset versions in `office_page`/`_asset_versions`, and UI tests. Lambda explicitly cleared the `office_page` edit; its network route stays untouched. Reuse `WalletUniverse.fitPoints`, `zoomAt`, `panBy` from the existing Wallet Map; never load `wallet_map.js` or mount its authority controls. Lambda owns the graph store/API and agreed `146b763f3`: GET `/api/office/network?limit=200`, optional `seed`, `hops=1|2`, `types=csv`. No writes, provider calls, funded-record fallback, arming or trial actions.

**Tech stack:** Existing JavaScript/DOM/SVG and Wallet Map camera helpers; Flask static assets; pytest/Playwright. No new dependencies.

**Contract:** Preserve `state/reason/as_of/truncated/note`, `nodes[{address,labels[{label,source,known_at}]}]`, full typed/provenance/time/proof/stale `edges`, and address/check/state/reason/time/fetch `coverage`. A missing edge is not no relationship; unknown times and unverified/imported edges stay labelled. Missing store is unavailable, not an empty graph. Role colors use stored labels; neutral fallback is not an ownership claim.

**Bounds and failures:** 200 requested edges (server maximum500), at most1000 displayed nodes/500 edges, fixed same-origin GET, 2.5s abort, manual refresh/room opening only. Untrusted labels use text nodes; only validated Solana addresses enter fixed wallet/GMGN URLs. Late requests cannot replace a newer query. Static SVG, no force simulation or animation. No entry/exit/sizing/signing path or shared server state. Rollback removes the room script/mount; API remains Lambda-owned. No whole-graph completeness or relationship discovery is promised.

- [x] Write failing Chromium tests: room nesting, reused camera math, role labels/edges, click-through, source/proof/stale/null-time/coverage, missing store, request race/failure, phone width and no mutation requests.
- [x] Implement the single read-only room module and script/mount wiring; keep stored-role/relationship semantics.
- [ ] Run focused tests with `-n 2`, capture desktop/phone with real read-only Lambda data, and review exact source with Zeta.
- [ ] Commit owned paths with `crew_commit.sh`; send reviewed SHA to Alpha. Alpha merges and publishes. Record source and runtime evidence separately.
