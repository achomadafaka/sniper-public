# Memecoin Live Radar Visualization Design

**Date:** 2026-07-16
**Status:** Draft — pending operator review
**Authority:** Read-only display. It renders existing telemetry. It does not gate entries, suspend exits, size positions, write any funded or paper table, or issue any external network request. It is a projection, not a control surface.

## Purpose

Surface the entry-radar telemetry that already exists as a live, per-coin picture the operator can watch and drill into. Today that telemetry (`memecoin_token_episodes`, `memecoin_feed_observations_v2`, `memecoin_quote_observations_v2`, `memecoin_radar_ratings`, `memecoin_holder_evidence`) is written but rendered nowhere. The vast majority of captured coin activity is watch-only — signals we saw but never attempted — and there is currently no way to see it in flight, only after a terminal real attempt (of which there are two rows).

This spec adds one embedded **Radar** subtab in the existing Memecoin page: a live episode table, a per-coin timeline drill-down, and a bottom analytics panel. It builds all three in one spec (operator decision, 2026-07-16).

## Relationship to the approved telemetry spec

This consumes, and does not modify, `docs/superpowers/specs/2026-07-16-memecoin-entry-radar-coin-state-design.md`. That spec is authoritative for the writers, the immutable `memecoin_radar_ratings` rule, episode boundaries, holder evidence, and the coin-state vocabulary. Its non-goals explicitly forbid "Telegram, browser, dashboard, LLM, or notification work in the entry path." This spec honors that boundary: **no radar code runs in, before, or alongside the funded entry path.** The radar is a separate read-only Flask route and a UI subtab that polls the local API. It reads the same DB the runner writes; it never writes it.

### The one correction to the original radar-viz notes

The prior notes claimed a live radar "needs a separate immutable watching observation record." It does not. `memecoin_feed_observations_v2` (15.9k immutable per-signal captures), `memecoin_token_episodes` (per-`(network_id, mint)` grouping with consensus and runup basis), and `memecoin_quote_observations_v2` (1.4M executable quote captures) already are that record. `memecoin_radar_ratings` having only two rows is not a gap to fill with a new table — it is the expected shape, because a rating is written only when a real attempt reaches a terminal result. The radar therefore sources live rows from episodes + observations and treats a rating as an optional join. **No new table. No new writer.**

## Non-goals

- No new table, column, writer, or migration on any funded or paper path.
- No write of any kind from radar code. The route opens a read-only connection.
- No new external network request (no DexScreener, no CLOB, no fomo, no RPC). Only the local SQLite DB and the local HTTP API.
- No radar code in the entry path, the exit path, the reservation path, or any runner sweep.
- No mutation of `overall_verdict`, `gate_result`, `real_decision`, `rating`, holder evidence, or any coin-state.
- No pooling of `global_feed` and `follow_activity`. Signal source is always shown separately.
- No new signal, no funded rule, no sizing, no copy advice, no expected-PnL claim.

## Identity and honesty rules (inherited, non-negotiable)

- The unit of identity is `(network_id, mint)`. `memecoin_token_episodes` names these `chain_id` / `token_address`; they carry identical values (`solana`, `8453`, `4663`, `56`, `1`, ...). The radar normalizes both to `(network_id, mint)` at the query layer. A bare `0x` address is never sufficient — the same address on Base (`8453`) and Robinhood (`4663`) is two different coins and must render as two rows.
- Claimed source time is always shown separately from our observed time, and always labeled untrusted. `source_ts` / `source_timestamp_trust` / `lag_s` are display fields, never used as if they were our clock.
- A rating means "no observed v1 warning," not "predicted winner." The word "green" is never rendered as a buy recommendation.
- Missing attribution renders as blank / yellow, never zero-filled. `NULL` mcap is not `$0`; `NULL` drift is not `0%`.
- Clock domain (`clock_domain`) is carried through and shown where two timestamps could be compared across domains.

## Data sources (exact columns consumed)

All read-only. No column below is written by radar code.

- `memecoin_token_episodes`: `episode_id`, `chain_id`→network_id, `token_address`→mint, `started_ts`, `ended_ts`, `first_signal_id`, `consensus_cluster_count`, `status`, `last_signal_ts`, `first_source_price`, `first_executable_price`.
- `memecoin_feed_observations_v2`: `upstream_event_id`, `normalized_side`, `source_user_id`, `source_handle`, `network_id`, `mint`, `ticker`, `their_price`, `their_market_cap`, `their_size_usd`, `source_ts`, `observed_at`, `lag_s`, `was_curated`, `was_real_approved`, `signal_source` (via join to events), `clock_domain`.
- `memecoin_quote_observations_v2`: `network_id`, `mint`, `observed_at`, `price`, `liquidity`, `market_cap`, `volume_24h`, `quality`, `dex_id`, `pair_address`. Latest-per-coin uses index `idx_mc_quotes_v2_coin_time`.
- `memecoin_radar_ratings`: `attempt_id`, `episode_id`, `network_id`, `mint`, `signal_source`, `rating`, `reason_codes_json`, `facts_json`, `decided_at`, `created_at`. Authoritative attempt rating only.
- `memecoin_holder_evidence`: `network_id`, `mint`, `source_user_id`, `evidence_state`, `remaining_usd`, `observed_at`, `confirmation_state`, `invalidated_at`, `invalidation_reason`.
- `memecoin_events`: `mint`, `network_id`, `signal_source`, `action` (paper decision), `real_decision`, `real_decision_reason`, `real_decision_at`, `source_handle`, `their_price`, `their_market_cap`, `created_at`, `received_at_normalized`.
- `memecoin_positions` (paper + real ledger): `network_id`, `mint`, `strategy` (arm), `execution_mode`, `is_open`, `entry_price`, `entry_notional`, `opened_at`, `closed_at`, `realized_pnl`, `exit_reason`, `peak_price`, `max_gain_pct`, `origin_event_id`, `attempt_id`, `quote_price`, `source_drift_pct`, `execution_slippage_pct`, `total_copy_drift_pct`, `price_attribution`, `quote_id`, `mirror_disabled`, `derisked_at`, `stop_hits`.
- `memecoin_strategy_candidates`: `candidate_id`, `name`, `status`, `gates_json`, `execution_mode`, `alpha_alloc`, `born_at`.
- `memecoin_candidate_positions`: `candidate_id`, `episode_key`, `network_id`, `mint`, `evidence_stage`, `control_for_signal_id`, `status`, `net_pnl`, `opened_at`, `closed_at`.

## Architecture

Three read-only endpoints in `sniper/routes/api_memecoin.py` (same blueprint and read-connection pattern as `/api/memecoin/events/recent`), plus one UI subtab in `static/app.js` rendered into the existing `#mc-subtabs` strip.

### Endpoint 1 — `GET /api/memecoin/radar`

Returns recent episodes as radar rows, newest activity first, bounded to a recent window (default last 24h of `last_signal_ts`, capped at N rows) with query filters: `?chain=`, `?source=` (`global_feed`|`follow_activity`), `?rating=`, `?fast=1` (claimed-lag < 60s), `?phase=`.

Each row:

| Field | Derivation |
|---|---|
| `network_id`, `mint`, `ticker` | episode identity + latest known ticker from feed obs |
| `chain_label` | display map: `solana`→SOL, `8453`→Base, `4663`→Robinhood, `56`→BSC, `1`→ETH; unknown passes through raw |
| `phase` | authoritative coin-state (below) |
| `age_s` | now − `started_ts` (episode clock) |
| `buyers` | distinct `source_user_id` in feed obs within the episode window; `consensus_cluster_count` shown alongside |
| `source_quote_drift_pct` | (`first_executable_price` − `first_source_price`) / `first_source_price`, `NULL` if either missing |
| `runup_pct` | (latest quote `price` − `first_executable_price`) / `first_executable_price`, `NULL` if unpriced |
| `mcap` | latest `memecoin_quote_observations_v2.market_cap` for the coin; `NULL` → blank |
| `attempt_rating` | `memecoin_radar_ratings.rating` when a terminal attempt exists for this episode/coin; else `null` |
| `advisory_rating` | display-only, computed live (below); labeled advisory, never persisted |
| `signal_source` | from events; separate global vs follow |

**Phase** (authoritative, mapped 1:1 to the approved telemetry coin-state vocabulary, emitting only states backed by real state):
- `watching` — episode `status` active, no real position and no terminal attempt for `(network_id, mint)`. (= approved `watching`.)
- `owned_unconfirmed` — real attempt reserved/submitted, position not yet confirmed open. (= approved `owned_unconfirmed`.)
- `owned` — confirmed real position open (`memecoin_positions` real, `is_open=1`). This is a display label for the approved `protected_hold_shadow` state (confirmed funded position; holder evidence remains observation-only). Rendered as "owned" for operator clarity; no behavior attaches.
- `cooldown` — episode closed (`ended_ts` set / `status` closed) with no open position. (= approved `cooldown`.)

A separate **display-only** boolean `giveback` is set when the episode is active/held and the latest quote is ≥ a configured percent below the episode's peak executable price. This is a rendering flag, explicitly not the reserved telemetry `decaying` coin-state, and it carries no behavior. It satisfies the "DECAYING" cue in the mock without overloading a reserved term or inventing hidden state.

**Advisory rating** (display-only): reuse `sniper/services/memecoin_entry_radar.py:rate_entry_facts` read-only against live episode + latest-quote facts to produce green/yellow/red for watch-only coins that never had an attempt. It is returned as `advisory_rating` and rendered dimmed with a `~` marker so it is visually distinct from the immutable `attempt_rating`. It is never written to `memecoin_radar_ratings`. This keeps the immutable-outcome ledger clean while still giving the operator a warning cue on coins we only watched.

### Endpoint 2 — `GET /api/memecoin/radar/<network_id>/<path:mint>`

Timeline for one coin (accepts `?episode_id=` to disambiguate multiple episodes of the same coin). Returns an ordered event list plus the price path:

- Each armed-wallet buy / partial sell / full sell from `memecoin_feed_observations_v2` + `memecoin_holder_evidence`, each row carrying claimed `source_ts` and observed `observed_at` separately, `lag_s`, and holder `evidence_state` / `invalidation_reason`.
- First source price, first executable quote, quote age, `quote_id`, and the three drift measures (`source_drift_pct`, `execution_slippage_pct`, `total_copy_drift_pct`) from `memecoin_positions` where an attempt occurred; quote path from `memecoin_quote_observations_v2`.
- Consensus appearing/disappearing over time (`consensus_cluster_count` progression via feed obs ordering; holder-evidence invalidations).
- Real decision and terminal reason (`memecoin_events.real_decision` / `real_decision_reason`; `memecoin_radar_ratings` terminal status + reason codes when present).
- Paper-arm exits overlaid on the price path: closed `memecoin_positions` for the coin, per `strategy`, with `entry_price`, `exit_price`, `peak_price`, `realized_pnl`, `exit_reason`.
- Peak, giveback, mirror decision (`mirror_disabled`), stop decision (`stop_hits`, `derisked_at`), terminal reason.

Price path is the `memecoin_quote_observations_v2` series for the coin across the episode window, downsampled server-side to a bounded point count.

### Endpoint 3 — `GET /api/memecoin/radar/summary`

Bottom-panel aggregates over the same recent window:

- **Outcomes by chain and source**: green/yellow/red counts. Immutable outcomes count `memecoin_radar_ratings` only (honest: two rows today, grows with real attempts). Advisory-rating counts are reported in a separate, clearly labeled tally so the two are never conflated.
- **Paper-arm PnL on matched entries**: sum `realized_pnl` from closed `memecoin_positions`, grouped by `strategy`, restricted to positions whose `(network_id, mint)` matches an episode in the window. Reported per arm; `real_shadow` labeled as the live-rails mirror.
- **Drop-top-two-winners sensitivity**: the same per-arm PnL recomputed after removing that arm's two largest `realized_pnl` winners, exposing tail dependence.
- **Complete attribution percentage**: share of attempts/positions in the window with full attribution — `price_attribution` present, `quote_id` present, both `source_drift_pct` and `execution_slippage_pct` non-null.
- **Candidate-strategy progress vs promotion gates**: for each `memecoin_strategy_candidates` row, closed `memecoin_candidate_positions` counts per chain against the graduation gate ("30–50 paired forward closes per chain" from the telemetry spec), read from `gates_json`. Shows progress (e.g. `22 / 30–50 paired closes`), never auto-promotes.

### UI — `Radar` subtab

- Add a `<button class="mc-subtab" data-subtab="radar">Radar</button>` to the strip at `static/app.js:2523`, and register `radar: '_loadRadar'` in the loader dispatch (`static/app.js:2696`), following the exact pattern of `_loadDecisions` / `_loadEvents`.
- Filter chips row: `GLOBAL` / `FOLLOW` / `SOL` / `BASE` / `4663` / `<60s` / rating (G/Y/R), reusing the existing chain-chip helper (`chainFilterChips`, `data-mc-chain`).
- Episode table with the Endpoint-1 columns; a row click opens the Endpoint-2 timeline in a drawer/expander showing the ordered events, price path with paper-arm exit markers, and claimed-vs-observed time columns.
- Bottom panel renders Endpoint-3 summary.
- Polling: the subtab refreshes on a bounded interval (default 8s) only while it is the active subtab; it stops polling when another subtab is shown. A short server-side cache (5–10s TTL) fronts Endpoint-1/3 so repeated polls do not re-scan the 1.4M-row quote table.

## Performance

- Latest-quote-per-coin uses `idx_mc_quotes_v2_coin_time`; the list query resolves the latest quote per `(network_id, mint)` via a correlated max on `observed_at`, bounded to episodes in the window (≤ ~200 episodes/day), not a full scan.
- Endpoint-1 and Endpoint-3 responses are cached server-side with a short TTL. Endpoint-2 is per-coin and uncached but bounded to one episode window with a downsampled price path.
- The route is registered on the existing web server (`sniper/web/server.py`), which is a read process separate from the funded runner. No runner change, no restart of the funded runner is required or implied.

## Error handling

- The radar route opens its own connection with `PRAGMA query_only=1` where supported, and never issues DML. Any query failure returns an error payload the subtab renders as "Radar unavailable: …" — it never raises into any writer and cannot affect the runner.
- Missing/`NULL` facts propagate as `null` to the client and render blank/yellow, never zero.
- Multiple episodes for one coin are disambiguated by `episode_id`; the list shows the most recent and the timeline can target a specific one.

## Testing (acceptance)

- **No-write guarantee**: a test asserting the radar handlers execute zero DML (connection is query-only / mock connection rejects writes).
- **Identity separation**: the same `0x` mint on `8453` and `4663` yields two distinct radar rows and two distinct timelines.
- **Honest ratings**: `attempt_rating` is populated only from `memecoin_radar_ratings`; `advisory_rating` is never written back; the outcomes tally counts immutable ratings separately from advisory ones.
- **Missing attribution**: a coin with `NULL` mcap/drift renders `null`, and the attribution-completeness percentage excludes it — never counts it as complete or zero.
- **Source separation**: `global_feed` and `follow_activity` rows are never pooled in list or summary.
- **Claimed vs observed**: the timeline returns `source_ts` and `observed_at` as distinct fields with `lag_s`; claimed time is labeled untrusted.
- **Phase correctness**: watching/owned_unconfirmed/owned/cooldown derive from real position + episode status; `giveback` is a display flag, not a coin-state write.
- **Sensitivity math**: drop-top-two-winners removes exactly the two largest `realized_pnl` winners per arm and recomputes.
- **Candidate gate**: progress reads `gates_json` + closed `memecoin_candidate_positions` per chain; never mutates candidate status.
- **Window/filters**: chain, source, rating, and `<60s` filters constrain both list and summary consistently.

## Out of scope for this build

- Any funded-path use of radar facts (gated behind the telemetry spec's graduation gate; a separate spec required).
- Telegram/browser/notification surfacing (this is dashboard-only, polled from the local API).
- New signal sources, new capture, new external calls.

## Revision 2026-07-16b — legibility pass (operator: "hardly understand anything")

The first cut rendered faithful telemetry as an unreadable debug table (jargon headers,
a column that was ~90% identical advisory-yellow dots, raw truncated addresses, an
un-framed research dump). Reworked the **presentation** (same read-only data model, no
new tables, still nothing in the entry path) so the tab answers one question: *what are
curated/armed wallets buying right now, and did we act?*

- `radar_rows` gains plain-language fields: `us_status` (`hold`/`entered`/`exited`/`passed`/`watching`),
  `us_detail` (hold PnL/peak, humanized skip reason), `heat` (`hot`/`cooling`), `last_activity_age_s`.
  Skip reasons humanized: `skipped_mcap`→"mcap gate", `unroutable_chain`→"chain not supported",
  `unsafe`→"safety check", `rejected`→"already held". `not_approved`/`disarmed` are **watching**
  (the buyer isn't armed), not passes. `buyers` now = `max(feed, events)` distinct — episodes with
  zero feed-obs rows still show their event buyers.
- UI: card-per-coin board (name · chain · "N wallets in · +X% since 1st buy · $mcap · Nm ago" ·
  us-line), one status badge (WE HOLD / HOT / COOLING / PASSED / EXITED), our positions sorted first.
  Rating de-emphasized (moved off the headline). Bottom panel collapsed to a one-line strip +
  `<details>` for the arm/attribution/candidate research.
