# Office floor watch — slices 1 and 2

Owner Epsilon; Zeta reviews XSS/auth only; Alpha merges/deploys. Ticketless WIP is parked separately.

Design: slate #111824 background, ink #eef2f6, muted #97a6b5; Head blue #70a9df, Judge gold #e2b76a, Scout teal #67c4ae. Local sans-serif prose, monospace times and exact facts. Three compact named seat cards, then a single newest-first timeline. Phone stacks cards, preserving all three near the top. No animation or invented activity.

Host projection: bounded per-actor current-identity event tails; safe fields from host state for planned sleep/park; quota database actor/time index for completed wakes and counted tokens today. Unknowns stay unknown; no inferred zero, process-health or closed-results claim. No network/provider calls, funded DB or .env read.

UI: safe subset markdown built with DOM elements/textContent (bold, bullets, paragraphs; no raw HTML/URL interpretation). Lifecycle/tool results are short expandable chips. Seat and thought/decision filters. Current state and latest thought on every card; coverage/identity below a small disclosure. Keep authenticated idempotent operator DM, drafts, focus and scroll. Raw provider result never becomes a claimed decision.

RED witnesses for host state/metrics, concurrent seat visibility, timeline order/filters, hostile markdown, operator DM identity and phone overflow. Real desktop/phone screenshots. Later slices add the decisions/book strip, mail threads and operator questions inbox; no claims that those exist in slice 1/2.

## Next epoch-open checklist — operator report

- Bind `explore_report.EPOCH`, `operator_view.py`'s fixed epoch/ledger paths, and `office-v2.js`'s report identity check to the same approved epoch in one release. Update `floor_view.py`'s epoch-specific host-state file mapping too. Never fall back to a prior epoch's data.
- Keep operator authentication, read-only Office sources, and the seat/dashboard boundary. No report rows enter seat tools, mail or brain notes.
- Verify old/wrong epoch reports render unknown; partial equity withholds NET and retains unpriced count; source/policy labels remain literal. Run `python3 -m pytest -n 2 tests/office/test_operator_book_ui.py tests/office/test_v2_feed.py -q`.
- Current report refresh is once per minute per visible tab. Add a shared short memo only if measured multi-tab read demand warrants it. Show a priced denominator only when the report actually supplies one; never invent it.
