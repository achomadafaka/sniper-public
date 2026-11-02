# Office exploratory reader

Owner Codex: only sniper/office/explore_report.py, focused tests and this plan.
Producer dependency: Beta adf747561 (unmerged); field list Gamma066b14ee4.
No installer overlap. Handover blockers interrupt this work.

Use supplied SQLite connections only; no files, network, settings, service or database writes.
Reuse ledger.equity, costs.epoch_cost and seat_host.proof_report. Read exact producer
schemas. All scans have row/byte bounds; partial reads never establish absence.
Labels join actual submitting decision hash. Windows are event-time [start,end).
Cost is full touched UTC days including today; adjacent windows may double-count.
Override entries make cost/measurement definitions unknown pending a structured basis.

Edges: no money/fill-path calls; no shared connection/global mutations; missing,
zero and unknown remain distinct; malformed JSON/schema/read/cap fails typed;
identities pinned to explore-1; future rows excluded; segment holes unknown;
partial equity withholds NET; output capped 64 KiB. No historical repair.
Not handled: absent producer data (feed paths/null fill, complete refusal telemetry,
brain audit, wall counters, watcher heartbeat) stays explicitly unknown. No new
simulator, producer schema, optional backend or live reads.

1. RED: real-schema disposable fixtures, eight Gamma witnesses plus read-only,
   malformed/read/cap, half-open bounds, cost and override checks.
2. Build smallest bounded projections, no verdict or inferred zero.
3. Focused tests -n 2; source review Gamma/Beta before Alpha merge.

## Delivered slice and limitations
Gamma confirmed touched-day C including today and explicit unknown for unavailable
brain/null inputs. Beta adf747561 confirmed pre-ledger refusals are not persisted.
This reader exposes lower-bound ledger refusal counts and unknown self attempts.
No null calculation is attempted without complete reusable feed/path/fill inputs;
no competing fill simulator. Equity FX comes only from causal ledger cashflows,
so stale/absent FX withholds current book NET rather than fetching another source.
Missing snapshot/manifest/reading labels mark recorded_fields_state unknown.
Physical SQLite execution time remains controlled by the owner of each supplied
read-only connection; the reader does not replace its global progress handler.
The reader caps results, queries and returned bytes without mutating connections.
No multi-database atomic snapshot is claimed. Source review before integration.

## Beta producer review child
Producer53fcb326e adds book_policy_sha256 to labels. Count by actual identity and
authorization (stored, prior override, unrecorded, unknown); a later override
cannot authorize an earlier admission. Invalid/missing hashes stay unknown.
Recorded field markers are per-field states: unknown, absent, absent_by_manifest
and known. Aggregate is unknown for unknown fields, partial for declared absence.
Eight RED witnesses precede the child; no producer/runtime edits. Rebase onto
Beta66801b139 only after Alpha selects its integration tip.

## Gamma follow-ups 1 and 2
Open window end=now keeps proof_report window_closed false unless epoch closed.
Refusal/non-order counts use actual office_v2_events tool_result rows for the
three paper tools, not legacy receipts (producer SeatTools._call). Malformed,
truncated or capped receipts refuse; counts explicitly scope persisted results.
Self refusals count self_sheet_unavailable and candidate_not_registered paper
order results. Earlier pre-ledger unknown/lower-bound note is superseded by this
verified producer path. Missing wake types stay unknown; Gamma supplies the
offline feed null comparison separately. No new producer or outside I/O.

## Poll history and release rollup

Read account poll rows on the supplied quota connection only; never call the
usage endpoint. Counts are persisted rows in the window. A failed empty-window
poll does not refresh newest_reading_age_s; a row with windows is data presence,
not account-guard clearance. Print the 30-day pruning caveat and partial/lower
bounds if the interval reaches past retention. Missing/capped/malformed input is
unknown. Group existing segments by release_sha and sum orders/fills/wakes/parks;
unknown segment assignment or metric stays unknown, never a zero. No relogging,
producer change or inference. Seven REDs precede this child; focused -n2 checks.

Historical windows also show age_at_window_end_s at their fixed end cut, while
newest_reading_age_s remains measured to now. Open windows or no reading return
None for the close-age field; no guard clearance or zero age is inferred.

## Read-only market observations

Optional host-supplied market_conn=None; missing source stays unknown. Caller
opens mode=ro; report never opens a file or calls MarketData.counts/_conn/fetch.
Read calls via the shared row/query/byte budget (10,000 rows). A row cap refuses
whole block. Preserve NULL outcome as outcome_unknown. Counts describe recorded
rows/reservations, not requests confirmed received by the provider. Non-dial
logging remains best effort, so the block is labelled partial/lower_bound even
with authoritative dial flags. Zero means 'no calls recorded', not no activity.

Prefer dial and stop_until columns when present. Old/null dial flags use Gamma's
explicit PROXY grouping: cache_hit, paced, budget_exhausted, provider_backoff are
non-dial; everything else is a dial proxy, including no_matching_pair and unknown.
Report inferred row count and mixed basis. A 200/no-matching-pair follows a dial;
its cached later answer is cache_hit. Actual stop_until derives response start via
STOP_429; absent old stop stamps use a reservation-time lower-bound label. At most
100 windows returned, with stops_truncated and the exact omitted count (only from
a complete bounded read). Print nominal retention30days; protected epochs may
retain longer, older windows are not assumed complete. Funded breaker unknown;
no marker or money-path changes. Producer source/clock/shape repairs merged in
d7cabfa51; newer authoritative columns in a4379d463/e8a499c40 read when available.
Nine RED witnesses precede implementation, including current and legacy schemas.
