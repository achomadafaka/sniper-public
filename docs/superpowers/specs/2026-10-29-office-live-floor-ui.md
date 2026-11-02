# Office live floor UI — 2026-10-29

Owner: Epsilon. Review: Zeta. Deploy: Alpha. Existing authenticated `/office` and GET `/api/office/state`; no new service, dependency, provider call or writer.

| Panel | Read-only source | Presentation / refresh |
|---|---|---|
| Live floor | `data/office/office.db`: agents, turn_runs, heartbeats | Seat/name, role, configured and last served model, generation, recorded task/memo and turn deadline. Running means a recorded running turn, not a provider liveness claim. Five-second visible-page polling. |
| Activity timeline / human logs | Bounded public turns, messages, tickets and decisions | Timestamped plain-English events, actor lanes and explicit references for real handoffs. New events highlight once; reduced-motion respected. Evaluation-derived output stays sealed. No invented Risk/Trader interaction when no receipt exists. |
| Agent / UTC-day sheets | Same bounded turns and spend; decision receipts and paper orders | Observed turns, recorded calls/cost/decisions and linked closed-position outcomes, with source window and denominator. Missing billable usage or actor attribution is unknown, not zero. Day selector filters loaded observations; no claim of complete historical daily totals when truncated. |
| Paper book / marks / curve | Actual `data/office/ledger/ledger.db`: books, orders, marks, cashflows, capital_flows | Exact atomic strings; latest causal per-order marks, age, source and unpriced inventory. Cash and priced liquidation separate. Equity only when loaded inputs are complete and FX/marks fresh. Curve records valid snapshots observed since page opened; gaps stay gaps. Discovery paper is not evaluation evidence. |
| Guard / pauses / providers / budget | Bounded public `data/office-control/health.json`, `guard/guard.json`; public turn refusal reasons, spend and whitelisted runtime limits | Show measured host-time freshness, pause reason, resume eligibility and termination diagnostics. Provider status is recorded outcomes, not an active auth probe. No credential files or token material read. Usage budget remains unknown without measured spend; fixed $800/month labelled separately. |
| Candidate funnel | Bounded `data/desk/intake/{pumpfun,dexscreener,fomo}/<UTC-day>.db:intake_rows`, `data/desk/intake_emitter.db:outcomes`, `data/memecoin_desk.db:desk_tickets` and Office opportunity_log / decisions / orders | Desk intake, recorded seat/filter verdicts, Office candidates, decisions and paper entries shown as separate observed counts. No conversion percentage across unmatched universes. Desk paper positions remain distinct from Office positions. |

## Read and truth contract

Each collection uses a primary-key/rowid tail capped at 200 (+1 truncation sentinel); marks use existing `(order_id, at)` index. At most 32 books. All JSON/text has byte limits. No global COUNT/SUM, JSON table scan, unbounded ledger.equity or progress_frontiers call. Short mode=ro snapshots share a one-second cooperative request deadline, 512-statement cap, 1 MiB accepted input budget and 2 MiB response cap (SQLite rows capped at 64 KiB; at most one refused row is read beyond the accepted-byte budget). SQLite progress deadline is 80 ms per connection and busy timeout 100 ms; an in-flight statement can overshoot the cooperative deadline by that bound. Browser abort does not cancel Flask work. No schema/index creation. Repeated polls remain one in flight per page. Missing table/file, malformed payload, future/stale clock, interrupted query and truncated history have distinct unknown/partial states. Host guard time and corrected Office time are labelled separately. A failed fetch retains the prior view visibly stale and clears live-health claims.

One in-flight request, 2.5 s browser timeout, five-second polling only while visible; manual refresh. Rendering uses textContent and semantic HTML, keyboard controls and a mobile stacked layout. Public DTOs whitelist fields; no raw JSON, private evaluation data, credentials or execution controls.

## Acceptance and limits

RED/GREEN fixtures cover real runtime memo shapes, generation mismatch, absent versus observed zero, bounded reads/truncation, actual ledger path, stale/unpriced marks, exact quantities, guard clock/failure states, attribution and HTML safety. Browser fixture verifies layout and refresh failure behavior. Existing report/integrity tests remain green. Spec/source/tests committed with explicit owned paths; Alpha deploys web.

This UI cannot reconstruct unpersisted API usage, historical equity or causal per-agent PnL. It displays the requested sheet columns with explicit unknown reasons; exact historical totals need separately owned indexed producer summaries. No production writes, service actions, money, signing, model/auth probes or env edits.

## Producer gaps for runtime-owner assignment (proposed contracts; no migration in this UI)

| Missing metric | Producing module | Proposed table / columns | Required index |
|---|---|---|---|
| Per-call usage and cost, including failures | `sniper/office/runtime_steps.py` at the models.call result boundary; usage from `sniper/office/models.py` | `data/office/office.db:model_calls`: call_id, actor_id, generation, turn_id, decision_id, requested_provider/model, served_provider/model, started_at, ended_at, outcome, reason, input_tokens, output_tokens, cached_tokens, cost_usd, usage_state, cost_basis. Unknown cost remains NULL. Idempotent call_id. Never prompt/auth contents. | UNIQUE(call_id); (actor_id, ended_at, call_id); (ended_at, call_id) |
| Explicit causal handoffs | `sniper/office/runtime.py` memo/action commit and `sniper/office/broker.py` action boundary; ledger decision receipt | `data/office/office.db:handoff_events`: event_id, at, from_actor_id, to_actor_id, from_record_kind/id, to_record_kind/id, decision_id, order_id, kind, public_summary. A shared coin or adjacent time is not an edge. | UNIQUE(event_id); (at,event_id); (from_actor_id,at); (to_actor_id,at) |
| Exact per-agent/day summary | Proposed new `scripts/office_public_reporting.py`, consuming runtime call/action records and `sniper/office/ledger.py` decision_receipts | `data/office/office.db:agent_daily`: utc_day, actor_id, generation, turns_started/completed/partial, calls, measured_cost_usd, decisions, linked_closed_n, wins_n, realized_paper_pnl_usd, open_n, source_heads, covered_through, missing_n. Include linkage/denominator and completeness, do not sum overlapping credits. | UNIQUE(utc_day,actor_id,generation); (actor_id,utc_day) |
| Historical equity curve | Proposed new `scripts/office_public_reporting.py` after ledger marks/flows; reuse `ledger.equity` off the web request (existing `equity_snapshot.py` is an authenticated evaluated as-of reader, not a public time-series producer) | `data/office/ledger/ledger.db:equity_snapshots`: book_id, at, cash_usd, known_liq_usd, equity_usd nullable, priced_n, unpriced_n, fx_at, source_heads, state, fill_model_version. Unknown snapshots produce gaps. | UNIQUE(book_id,at); (at,book_id) |

Until those contracts exist, the UI shows observed-window counters, explicit unknown cells and only client-observed equity samples. Runtime owners must approve schema/retention and causal linkage before writing. No producer table is silently created by GET.


## Review clarifications

Verified released sealed-view integrity and Head reference gates remain unchanged. Nested `evaluation_derived` or blind markers suppress the entire public turn/message/ticket and any derived activity/statistics. Unknown/corrupt release proof never becomes commentary or economics. Existing integrity tests are retained.

One corrected-UTC read cut governs Office events, causal marks (`at <= cut`, age <=60s) and FX (`fx_at <= cut`, age <=120s; inferred settled-fill FX <=60s). Exact lamport sell marks are net of modeled exit network cost once. Missing FX makes inventory unknown; unknown inventory is never zero. Cash is shown only if all bounded capital/cashflow/order inputs fit. Truncated, omitted or future inputs invalidate complete equity. Closed-position outcomes use the close UTC day, exact receipt-to-order actor linkage and net stored exit minus entry minus costs; credit remains descriptive/non-additive. Guard freshness uses separately labelled host UTC.

The footer shows the actual served code-root SHA only for a content-addressed web release; mutable source is unknown. Runtime SHA is declared public configuration, not proof of an installed PID. Agent generation, recorded lease generation, turn deadline and heartbeat freshness are separate. Neither a running turn nor ready guard establishes organic readiness.

## Validation evidence

Focused RED witnesses reproduced before implementation: runtime memo projection, real ledger path/unbounded-helper refusal, truncated daily coverage, public guard reader, nested blind payloads, request budget/identity and dangling handoff references. Focused report/view/integrity/ledger regressions pass with two workers. Playwright local fixture at desktop1440x1000 and mobile390x844 verifies seats/arrows, day and equity selectors, contained table overflow/navigation, and retained visibly stale state after an injected503. Only fixture503 browser errors are expected; no JavaScript exception observed. Screenshots remain local under `output/playwright/` (not deployed/test fixtures).

One read-only live-data projection on2026-10-29:28 SQL statements,71791 accepted bytes,6.4ms;Office/ledger known,4 agents,26 events,4 stats rows;all intake/filter windows correctly marked capped. This is one observation, not a latency distribution or runtime-readiness claim. No model/auth/provider call, production DB write, service action, money or env action.

## Hard-filter panel integration

Reader dependency: reviewed `desk_funnel.py` at97026e862 (Beta author PASS, Zeta PASS), merged by Alpha in4632b19c5. The API reuses the existing `funnel` key and the same `_ro`/`_charge` request budget; the previous generic desk-stage count query is replaced, not duplicated. Intake sightings, emitter eligibility outcomes and Office receipt counts remain separate windows. No conversion ratio or Office admission is inferred.

`hard_filter_state` and nullable `hard_filter_count` describe only the hard-filter subset of the **last 200 tickets, all stages**. Known empty means “0 in the last 200,” never no candidates or a complete hourly zero. Future/invalid/naive times are omitted; incomplete observations stay partial/unknown. The panel displays bounded UTC-hour verdict/reason groups, first recorded blockers and at most50 ticket details with per-check status/reason. Missing checks remain not observed; missing/truncated evidence stays explicit. All content uses textContent, and open ticket details/focus survive polling. The viewer creates no tables, indexes, receipts or actions.

### Written seat messages and task boards

The read-only floor adds pair-selected DM threads and an open/blocked/done board per seat. It consumes Codex's typed `office_dm_events`, `office_task_events` and `office_task_heads` contract (`fea6ac1b83c47194a02c6b54f29a13b9ade8bfe6`). It never sends messages or changes tasks. Text is recorded data, not operator authority.

Reads share the existing whole-response query/byte/time budget: at most 65 DM rows by descending indexed seq, 65 task heads by rowid and 64 unique event-ID lookups. Only 64 records are projected per collection. Missing heads support a labelled 64-event compatibility tail; other projection failures stay unknown. An empty head projection with existing events is unknown. Current actor generations must match both DM endpoints and task owners; future or invalid records are withheld. Sealed seats are excluded. No DM/task contents enter outcome statistics.

Task heads retain current state when terminal history prunes: a done head may show its title/status with note and receipt unavailable; open/blocked heads without their event are withheld. No older open event replaces a malformed latest event. Window limits and withheld records remain visible. Graph edges use the persisted DM ID and receipt, never inferred interaction. Task completion is never inferred from a running turn. Pair/task selection and thread scroll survive polling; all text uses textContent.

Not handled: full inbox history, unread/read state, reference expansion, task editing, or cross-generation ownership. Missing producer tables remain unavailable until their runtime/schema release lands. Persistent `agent_daily` and `equity_snapshots` readers are a separate adapter; existing recent-window statistics and page-local equity samples retain their labels.

### Persistent daily sheets and equity history

Reader contract pinned to Lambda reporting `a1dc8436c` (supersedes `4fa952561`): `office.db:agent_daily`, `ledger/ledger.db:equity_snapshots`. `agent_daily_late` is internal and never read. No producer invocation, schema creation or persistence occurs in the web handler.

Daily rows keep actor, generation, UTC day and nullable epoch identity separate. Generation -1 is unattributed. Known epoch rows leave turn/call/handoff usage unknown; those facts live in “not epoch-attributed” rows and are never summed into an epoch. Exact public discovery epochs are allowed individually. Legacy/no-epoch rows and NULL-epoch rows require a complete, causal, entirely discovery/public epoch universe; mixed/evaluated/unknown scope is withheld. Evaluated metrics require a future scoped release adapter and remain unavailable here. `source_heads.release_sha` is 40 lowercase hex or null, and remains producer provenance rather than installed-runtime proof.

The handler checks query plans before executing: a range SEARCH without a temporary sort is required. Daily reads project at most 201 rows by the day-key index, accepting 200. Snapshot reads use `(at,book_id)` with the same 201/200 bound, then bind exact discovery book IDs from the loaded book metadata. No global aggregate or per-book fanout. Both readers charge existing response work/byte/query limits; missing indexes/tables, malformed/future cuts or exhausted budget yield unknown. Reporting timestamps use one host-UTC cut, distinct from the corrected runtime clock.

Sheets expose nullable metrics, cost_unknown_n, pnl_unknown_n, missing_n, overlap_n, late_after_close, source heads, covered_through and publication freshness (stale after 900s). Partial measured cost is never total spend. PnL is descriptive and non-additive. Historic snapshots keep NULL gaps and book separation, stored cash versus known inventory versus complete equity, priced/unpriced counts, SOL/USD and FX observation time, fill-model version and source heads. Priced inventory with unknown/future/stale FX cannot become complete equity. The stored producer's per-mark timestamps are not available and are labelled unavailable; the reader does not reconstruct marks. Each point retains its own cut rather than being repriced with today's FX.

Unavailable producers retain the explicitly labelled recent-window/page-local compatibility view. Persistent history is not extended by client polling or backfilled. No changes to reporting cadence, producer authority, runtime or model calls.

### Served-model identity basis

The seat and provider panels project typed `turn_runs.value.served_model_basis`: `echoed`, `requested_pinned`, or `unknown`. They label the discovery-only fallback “requested model, pin-verified; not echoed,” rather than implying a provider echo. Missing/legacy/invalid basis remains unknown; sealed output suppresses both model and basis. This display does not grant evaluated/default authority or change admission. Direct `model_calls.served_model_basis` consumption belongs to the separate producer reader adapter.

## Amendment — Office v2 operator surface, 2026-10-30

The original v1 state projection and task/message panels remain read-only. Office v2 adds a separate bounded `GET /api/office/feed` projection and an authenticated `POST /api/office/dm`; the page as a whole therefore has a mail write surface. These implement the approved `beta/office-v2-spec` persistent-seats design, not funded authority.

The host publishes `office_v2_events` and `office_v2_roster`; events must match the current epoch, actor generation and incarnation. Mail additionally requires `office_v2_mail_bind`. Unbound historical mail is withheld, never assigned to an epoch by time. Current v2 tasks come from the latest observed host-bound workspace manifest, not epoch-less legacy task heads. Event and mail cursors are independent decimal sequence strings. Reads use the existing read-only connection deadline and capped indexed tails; unavailable sources and truncated windows stay labelled.

Operator DMs require explicit dashboard credentials/session or a verified loopback gateway, even when the ordinary global write guard is disabled. Same-origin JSON and a bounded body are required. The host assigns sender `operator`, resolves the target generation and binds the epoch/incarnation. Stable client IDs make ambiguous retries idempotent. A stored message is not an acknowledged seat receipt. Seat text, file names, notebook content and browser host/path render as literal text; no workspace file or URL is opened by the web process. Only emitted thoughts are shown.

Beta owns producer schema, mail persistence and retention; Epsilon owns the projection, routes and UI; Zeta reviews. No funded imports, calls, locks, service actions or `.env` changes are introduced. Rollback removes the new route/asset wiring together; persisted mail is retained under the producer retention contract.

## Amendment — compact Office v2 feed, 2026-10-30

Operator-approved presentation fix after the deployed feed reached about 22,000 px: mount persistent-seat work before the legacy floor, put the DM form before the work panels, and keep the live feed in a keyboard-focusable scroll region capped at `min(50vh,32rem)`. Tool results use native `details`/`summary`, initially closed. No seat text becomes HTML. Expanded results, feed scroll, result keyboard focus and the DM draft survive polling; switching seats resets the feed scroll. Existing source states, read limits, producer ownership, auth, idempotency and funded boundaries remain unchanged.

Validation: real Chromium fixture with 64 long hostile tool results reproduced the unreachable DM before the fix; keyboard-focus loss during polling was separately observed RED. Both pass after the fix; 33 focused UI/feed/route tests pass with `-n 2`, Node syntax and diff checks pass. No live DM or deployment was performed. Rollback reverts the JS asset with its normal web asset-version update.

Knowledge Sync audit: Q4 yes (real browser regression); Q6 yes (this presentation amendment); all other questions no new delta beyond the existing v2 contract. No brain/schema/producer change.
