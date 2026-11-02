# Hyperliquid Edge Desk And UI Truth Design

## Status

Approved 2026-07-26. This specification covers the read-only Hyperliquid UI and API work only.
The paper-runner coin-key comparison defect is a separate TDD change on
`fix/hl-copy-coin-key-normalization`. Neither change authorizes a runner restart or funded
activation.

## Problem

The dashboard no longer matches the deployed Hyperliquid research state:

- the top-level Hyperliquid page still says no lane is active;
- `/api/hyperlive/runtime` describes copy as paused or offline while the alt-short copy runner and
  standalone exhaustion paper scanner are running;
- the analyzer sends and renders the complete screened-wallet store on every load;
- every `hl_copy_book.status='open'` row is presented as one active book even when its source wallet
  is inactive, its source state is stale, or reconciliation is blocked;
- today's v3 study, current regime, forward-arm sample, episode gate, and real-leg dry-run state are
  absent from the UI.

Live counts, wallet addresses, and affected symbols are observations, not configuration. The UI
must derive every count and lane membership at request time.

## Goals

1. Replace the obsolete Hyperliquid placeholder with a truthful, read-only `HL Edge Desk`.
2. Show current research, paper-runtime, regime, provenance, promotion-gate, and dry-run state.
3. Classify open copy rows by reproducible rules instead of age, a fixed row list, or a fixed count.
4. Make the analyzer usable without putting the whole wallet store in the response or DOM.
5. Preserve all paper evidence. No UI action in this scope closes, rewrites, archives, or deletes a
   copy-book row.
6. Keep funded execution unchanged and visibly fail closed as `DRY_RUN` or unavailable.

## Non-Goals

- No exchange or price-history call from a page load.
- No real order, SDK, RPC, funded database, runner, watchdog, cron, or service change.
- No copy-target selection, sizing, trade recommendation, or expected-PnL language.
- No historical row deletion or fabricated close.
- No claim that one bearish episode proves regime-independent edge.
- No paper-runner restart. The separate coin-key fix remains inactive until an operator explicitly
  restarts `run_hl_copy.py`.

## Change Isolation

### UI truth change

Branch: `feature/hl-edge-desk-ui-truth`.

Allowed surfaces:

- `sniper/hyperlive/edge_desk.py`, a Flask-independent read model;
- `sniper/routes/api_hyperlive.py`;
- `templates/index.html`;
- `static/app.js` and `static/styles.css`;
- focused Hyperliquid API and UI tests;
- this specification and its implementation plan.

The endpoint is read-only and the web service is not restarted by this work.

### Paper-runner defect

Branch: `fix/hl-copy-coin-key-normalization`.

Allowed surfaces:

- `sniper/hyperlive/copy_sim.py`;
- focused pure-function tests;
- a short defect note if required by branch-close knowledge sync.

This change normalizes keys for comparison only. Returned symbols retain the canonical spelling
already supplied by the target or stored paper book. It never sends an uppercased synthetic symbol
to Hyperliquid. It is committed and verified separately, with no process restart.

## Edge Desk Information Architecture

The existing `hl-algo` navigation slot becomes `HL Edge Desk`. The separate `HL Analyzer` remains
available for wallet inspection.

### Status strip

The first screen shows compact, independently sourced status cells:

- standalone paper arm: arm ID, process/heartbeat freshness, decisions, opened, closed, rejected;
- wallet-copy arm: arm ID, process/heartbeat freshness, open and closed observations;
- regime: causal label, 72-hour basket return, decision timestamp, and freshness;
- promotion gate: `paper_continue`, episode progress, forward closes, and incomplete/failed gates;
- real leg: enabled flag, `DRY_RUN`, open intents, pending intents, and heartbeat freshness.

Missing, malformed, stale, or inconsistent input is rendered `UNKNOWN` or `STALE`, never inferred
as healthy. Raw host time is not used for market/runtime age. Corrected UTC is the API time source.

### Evidence receipt

The v3 research card reads the sealed report named by the standalone arm manifest and verifies its
SHA-256 before displaying numbers. It shows:

- signal, matched-alt basket, and residual means;
- day-block confidence interval;
- episode count and minimum episode residual;
- hourly-close bridge and its limitation;
- `paper_continue` plus the explicit warning that one bearish episode is not replication.

The passive full-period short result is not promoted to a headline because its capital profile is
not comparable to the intermittent arm.

### Open paper book

The desk shows counts for every state and expands rows only on demand. The default visible state is
the current strategy lane; legacy and uncertain states are collapsed but never omitted from totals.

Each row exposes strategy provenance, wallet lifecycle, source-sync time, mark availability, entry,
current mark when available, unrealized bps when computable, cold-start state, and the reason for its
classification.

## Dynamic Position Classification

Classification uses normalized comparison keys, while every displayed and returned symbol retains
its stored spelling. `comparison_key(symbol)` is case-insensitive and has no exchange-side effect.

Inputs per open row:

- copy-book row and cold-start flag;
- current wallet-copy arm and matching `hl_altshort_observations` provenance;
- target active state;
- latest `hl_copy_trader_state.positions_json` and sync timestamp;
- latest locally persisted regime mark, addressed by comparison key;
- copy-runner heartbeat freshness.

The authoritative current arm ID is the arm ID in a fresh heartbeat, provided it matches the
corresponding checked-in manifest and persisted arm row. A missing, stale, or inconsistent
heartbeat makes current-arm provenance unavailable; the API does not guess from the newest DB row.
Paper heartbeat and source-sync freshness use a five-minute threshold and a 60-second future-time
tolerance against corrected UTC.

Derived facts:

- `source_fresh`: source sync is present, not in the future beyond tolerance, and within the stated
  five-minute freshness threshold;
- `source_matches`: the fresh source book contains the same comparison key and side;
- `mark_available`: a positive persisted mark exists for the comparison key and the copy-runner
  heartbeat proving the mark sweep is fresh;
- `current_arm`: the copy row has an observation for the active wallet-copy arm;
- `target_active`: the target is currently active in `hl_copy_targets`.

Priority-ordered states:

1. `source_unverified`: source state is missing, stale, future-dated beyond tolerance, or invalid.
2. `reconcile_blocked`: source is fresh and no longer matches, but no fresh usable mark exists.
3. `reconcile_ready`: source is fresh and no longer matches, and a fresh usable mark exists. This
   names a runner/write-path discrepancy; the read-only UI does not close it.
4. `current_lane`: source matches and the row has current-arm provenance.
5. `draining_legacy`: source matches but the row lacks current-arm provenance or its target is
   inactive.

If current-arm provenance and inactive-target state coexist, `draining_legacy` wins and provenance
remains visible. Counts come from these rules on each request. No fixed total, symbol list, wallet
list, or activation-date guess is allowed.

## Analyzer Query Contract

`GET /api/hyperlive/analyzer` becomes a bounded query endpoint:

- query parameters: `view`, `q`, `verdict`, `page`, `page_size`;
- `page_size` defaults to 25 and has a hard upper bound;
- `view=attention` is the default and includes current copy targets, effective proven wallets, and
  active lifecycle selections;
- `view=archive` exposes rejected, culled, inactive, and other screened rows;
- exact wallet search can find a row regardless of view;
- the response includes `items`, `page`, `page_size`, `total`, `has_more`, and unpaginated summary
  counts;
- lifecycle, effective-verdict, copy status, and follow/copy aggregates retain their existing
  honesty semantics.

The browser renders one page only. Changing search, view, verdict, or page triggers a new request.
The complete screened store is never embedded in the default response or DOM.

## API Architecture

Add a focused Flask-independent read model in `sniper/hyperlive/edge_desk.py`, exposed through
`api_hyperlive.py` as:

- `GET /api/hyperlive/edge-desk` for local runtime, evidence, gate, and classified-position state;
- bounded `GET /api/hyperlive/analyzer` for wallet results;
- existing mutation endpoints remain unchanged and outside the Edge Desk.

`edge-desk` reads only:

- `data/hl_intel.sqlite` in SQLite read-only mode;
- the two arm manifests;
- the manifest-referenced sealed v3 report;
- `data/hl_copy.heartbeat`;
- `data/hl_altshort_signal.heartbeat`;
- `data/hl_real.heartbeat`.

File parsing is bounded. SHA verification precedes report rendering. A missing optional source
produces a source-specific unavailable block while the rest of the desk still renders.

The obsolete generic runtime doctrine is removed from the top-level Hyperliquid UI. Existing
runtime API compatibility may remain for other callers, but the new desk does not consume its
stale lane labels.

## UI Behavior

- Keep the existing dark operator-console language and type scale.
- Use one compact status strip, one evidence receipt, and one position ledger rather than a grid of
  interchangeable cards.
- Make `paper`, `DRY_RUN`, `STALE`, `UNKNOWN`, and `one episode` visually explicit.
- Default legacy and archive sections closed; their counts remain visible.
- Provide no close, purge, promote, fund, restart, or override button.
- On mobile, status cells stack and the position ledger becomes compact expandable rows.
- Empty state means no rows for that classification, not that a runner or feed is healthy.

## Error Handling

- Database absent or unreadable: return `available=false` for the database source and continue with
  file-backed runtime evidence.
- Table or column missing: report `schema_missing`; do not migrate from this endpoint.
- Heartbeat missing/malformed/stale/future: render the affected runner unavailable or stale.
- Report missing or SHA mismatch: suppress all study numbers and render `integrity_failed`.
- Malformed position JSON: classify affected rows `source_unverified`.
- Normalized-key collision: classify affected rows `source_unverified`; never choose one silently.
- Missing mark: keep unrealized PnL null and use `reconcile_blocked` where applicable.
- UI fetch failure: retain the last successful render with a stale banner and timestamp.

## Separate Coin-Key Fix Contract

The pure `mirror_actions(target_positions, our_book, marks, fee_bps)` function must compare all
three mappings through a case-insensitive index:

- target to book matching;
- book to target matching;
- mark lookup for opens and closes.

Outputs preserve canonical spelling:

- open and hold actions use the target position's original symbol;
- close actions use the paper book's original symbol;
- no mapping passed by the caller is mutated;
- ambiguous normalized collisions fail closed and emit no unsafe action.

The runner fix does not change eligibility, the alt-short universe, costs, entries, exits, or DB
schema. It takes effect only after a separately authorized runner restart.

## Tests

### API and classification

- current, draining, source-unverified, reconcile-blocked, and reconcile-ready fixtures;
- current-arm provenance versus inactive target precedence;
- mixed-case comparison without changing returned spelling;
- dynamic counts after rows are added between requests;
- stale, future, missing, and malformed source state;
- missing mark and normalized-key collision;
- sealed-report success, missing report, and SHA mismatch;
- corrected-clock heartbeat freshness;
- endpoint performs no schema migration or write.

### Analyzer

- default attention view excludes archive rows;
- exact search crosses the view boundary;
- verdict/search filters compose;
- page size default, maximum, page metadata, and stable order;
- a large fixture returns one bounded page, not the whole store.

### Browser and static UI

- obsolete empty-state copy is absent;
- Edge Desk consumes `/api/hyperlive/edge-desk`;
- no destructive or funded action appears;
- analyzer navigation requests bounded pages;
- desktop and mobile screenshots verify hierarchy, overflow, collapsed legacy state, and stale/error
  treatment.

### Separate runner fix

- mixed-case target/book match holds without duplicate open;
- mixed-case mark lookup allows open and close;
- side flip closes with stored spelling and opens with target spelling;
- uppercase native ticker remains unchanged;
- inputs remain unchanged;
- normalized collision fails closed;
- existing copy-simulation and runner-focused tests remain green.

## Rollout And Safety

The UI branch and runner-fix branch are committed separately and must not be combined into one PR.
Verification uses fixture databases and a development web process, not the production database or
service. No branch is pushed or merged without operator instruction.

Completing either branch does not authorize:

- restarting the production web service;
- restarting `run_hl_copy.py`;
- closing the currently wedged rows;
- enabling the real leg beyond its current dry-run state;
- funding an account or overriding the episode gate.

The final handoff must state that the coin-key fix is dormant until restart and that current live
counts may differ from every count observed during design.
