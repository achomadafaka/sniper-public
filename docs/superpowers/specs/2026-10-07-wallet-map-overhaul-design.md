# Wallet Map overhaul: readable evidence, table-first, honest labels, research bridge

2026-10-10 amendment: [armed-wallet-links](2026-10-10-armed-wallet-links.md)
adds saved funding and exact before/after receipt links on every armed chain.
These appear in wallet details and map peers; legacy Solana same-slot ranks keep
their original sample. This research/web change deploys only the web runtime and
does not restart the funded runner or grant related addresses authority.

Operator approval: 2026-10-07 ~10:50Z, the full A-D scope plus the revised D1 below. Four answers:
cover A through D; remove the CRT effect on every tab; open on the table and reach the map in one
click; put the arm buttons below the evidence. Crew work starts when the current queue clears
(Beta's chain-1 F1-F3 and Lambda's 09ac9549/b831e9c1).

Source audit (read-only, Codex, 2026-10-07 07:43-07:56Z):
`/home/bigpapi/Documents/Codex/2026-10-07/wallet-map-user-audit/report.txt`, plus
`frontend-findings.txt` and `backend-findings.txt` in the same folder. Its source refs point at the
served release `b8ebf099b`. Refs in this spec point at main (`f4edfa482`).

## Goal

A researcher opens Wallet Map, sees a ranked, filterable list of wallets with measured relationships,
and opens one wallet. The summary then states in order: the supported observation, its sample and
age, its limitation, the exact supporting coins, our own funded results on those coins, and the next
research step. Live authority controls follow. Nothing on the page implies profitability,
common ownership, causation or a strategy that the evidence does not support.

## Non-goals

- No change to funded authority, admission gates, roster rules, the runner or any send path.
- No new external provider calls. Every read is a local SQLite file or an existing endpoint.
- The legacy publisher quarantine (`docs/audits/memecoin-wallet-research-20261004/publisher-inventory.md`)
  stays in force. `scripts/research/wallet_follow_funnel.py` stays retired. Paper hypotheticals are
  never shown as evidence beside funded results.
- No new graph library, no frontend framework, no new research engine.

## Current state (facts this design rests on)

1. `static/styles.css` `body::before` (4px scanlines at 8% black, z-index 9999) and `body::after`
   (vignette to 60% black, z-index 9998) paint over every tab, including expanded Wallet Map.
2. The inspector is a 320x680px scroll column (`static/wallet_universe.css`). For frankdegods it held
   2,394px of content. Supporting labels are 10-12px. Map zoom inverse-scales labels.
3. Selecting a planet inserts the dossier (counts, provenance warnings, an open Research log) and the
   authority controls ABOVE the relationship facts (`static/wallet_map.js` `drawUniverse`,
   `drawUniverseEvidence`, `dossierHTML`).
4. The Herders table, the best relationship view, sits inside Live roster controls below the map
   and the research coverage table (`views()` key `herders`).
5. Every wheel event over the map zooms (`static/wallet_universe.js` `graph.addEventListener('wheel'`).
   Selection uses `history.replaceState`. Only `wm_wallet`/`wm_chain` survive a reload.
6. `/api/memecoin/wallet-map/universe` reads `data/wallet_universe_<chain>.json`. No such files exist
   because their publisher (`wallet_follow_funnel.py --universe-only`) was retired by the 2026-10-05
   amendment of `docs/superpowers/specs/2026-10-04-memecoin-wallet-execution-research-design.md`.
   The route falls back to roster rows with `paper=None, funded=None, setups=[]` and no edges. That
   is why the evidence panel says "Observed neighborhood · 0 links" and why shared-coin comparison
   is empty.
7. The live link graph `data/memecoin_wallet_graph.db` is refreshed hourly by the `wallet-link-graph`
   cron. Table `memecoin_wallet_edges` held at 10:35Z: solana `co_buy_slot` 432, `slot_follower` 427,
   `follows` 15, `shared_funder` 6, `dumps_on` 1. Each row carries `n_coins`, lags, a null-model p,
   `coverage` and `evidence` (JSON with per-coin mints and times). Solana only.
8. `/api/memecoin/wallet-map/wallet` (`build_wallet_view`) already reads that graph plus the ledger
   for one Solana wallet. When the graph open fails it connects `:memory:` and returns an empty view.
   That empty view reads as "no relationships": the swallowed-error-as-empty trap.
9. `real_stats_by_wallet(conn, chain)` (`sniper/persistence/db_memecoin.py`) already returns the
   funded record per wallet on one chain: robot-only, `WALLET_GRADE_SQL`, all-time plus 24h/7d/30d,
   with `roi_pct` = realized PnL / entry notional. The Armed board uses it.
10. The wallet link graph's brain projection refuses the same 6 `shared_funder` edges every hour
    ("detail must be <= 4096 chars"): `detail_for` in `scripts/research/wallet_link_graph.py` drops
    `coins` and `per_seed` but the text can still exceed the gate's 4,096 bound.

## Design

Ten slices. W slices are frontend, D slices are backend. Each slice is its own branch, its own Gamma
review and its own merge. Runner and web deploy together on every merge (standing rule).

### W1. Readability and layout (Lambda)

- Delete the `body::before` and `body::after` CRT rules from `static/styles.css`. Every tab changes.
  The universe sky illustration stays.
- The Wallet Map section becomes two columns at >= 1100px: list or map on the left, the wallet
  summary pane on the right at `clamp(480px, 45%, 720px)`. Below 1100px the summary stacks under the
  list or map at full width. The summary pane is no longer a fixed-height inner scroller; the page
  scrolls.
- Type scale: evidence text and table cells 14px, labels 13px minimum, numbers in headline facts
  18px. Map labels keep a 12px floor and stop inverse-scaling below 1x zoom.
- Expanded mode on narrow screens keeps a "Details" button that opens the summary as an overlay
  with focus moved into it, and Escape returns focus to the map node.

### W2. Table first (Lambda)

- Wallet Map opens on the relationship table, today's Herders rows, moved out of Live roster
  controls to the top of the section. One toggle, `Table | Map`, sits above it. The Live roster
  controls keep their own Armed/Watch-only tables, unchanged.
- Columns: wallet, chain coverage, read coins (n), repeat buyers, same-slot share after it,
  timing rank (or "unranked: n < 10"), link count by edge type (from D1), funded closes and
  funded return % on the selected chain (from D1, blank when none), freshness.
- Filters: minimum read coins, timing ranked yes/no, measured vs unmeasured, armed/watch-only,
  has funded closes. Sort by any column. Filters live in the URL (W4).
- A row click opens that wallet's summary in the right pane without leaving the table.
- W2 can merge before D1: the link-count and funded columns then read "pending D1", never 0.

### W3. Summary order and wording (Lambda)

The wallet summary renders in this fixed order:

1. **Observation**: one sentence built from measured fields only, for example "15 repeat buyers
   across 59 read coins; another decoded buyer landed after it in the same slot on 43/59."
2. **Sample and age**: population, n, window, graph run time, and stale flag (graph > 3 h).
3. **Limitation**: the one that matters for this observation ("sampled same-slot order; ownership
   and causation unproved").
4. **Supporting evidence**: the exact coins (D2) and our funded results on them (D3).
5. **Next step**: actions that exist today: open coins, compare with a linked wallet, save a
   research question (existing dossier note POST), see missing data (D4).
6. **Execution research** (existing dossier counts and codes, with one plain sentence each).
7. **Live authority**: the existing Trial / Arm / Disarm / Pause / Retire controls in a section
   titled "Live authority · changes real money". Handlers, endpoints, confirmations, CSRF and dry
   runs are unchanged; only their DOM position moves.
8. Collapsed by default: research notes (draft text still preserved), raw JSON, legacy diagnostics,
   the forensic archive.

Wording: one term, "repeat buyers", replaces moons, satellites, herders and "Following" in
user-facing text (code identifiers stay). "Probe bots" becomes "unverified high-overlap addresses".
The header "See who leads, who follows, and who connects them." becomes "Repeat buyers and
same-slot order from our sampled first buys. Not ownership, not profit."

### W4. Navigation and state (Lambda)

- The URL carries: chain, view (table/map), wallet, satellite, size metric, table filters and sort,
  open sections, comparison set. Reload and deep links restore all of them.
- A wallet step uses `history.pushState`; browser Back returns to the previous wallet. Sorted-list
  previous/next stays a separate control labelled "Next in list".
- Same-wallet redraws keep open sections, summary scroll position and keyboard focus. Loading shows
  "loading", never an absence claim.
- Wheel over the map scrolls the page. Zoom needs ctrl/cmd+wheel, the +/- buttons, or a click on the
  map, which arms wheel zoom until the pointer leaves. Touch keeps pinch zoom; one-finger vertical
  drag scrolls the page.
- A Refresh button reloads universe, herders and the open wallet from the existing endpoints and
  keeps state. The status line shows age ("graph 12 min old"), ticking.

### W5. Availability and honest encoding (Lambda, with D1 fields)

- A per-chain strip above the table: global roster N, addresses proven on this chain, relationship
  graph measured yes/no (Solana only today), fresh capital n/N. A size mode that the chain cannot
  measure is disabled with the reason in its tooltip.
- Unknown is drawn as a dashed outline with no fill. A measured zero is a solid minimum-size planet.
  The legend shows both.
- Every headline number carries its population inline ("of 59 read coins", "of 49 usable coins").
- "Shared repeat buyers with" shows all links with a total, not the first three.
- Comparison shows a tray with its members and the 6-wallet cap where the Add button is. A chain
  change asks before clearing it.

### D1. Live relationship source (Beta)

- `/api/memecoin/wallet-map/universe` stops reading `data/wallet_universe_<chain>.json`. Wallets
  come from the live roster as today. Edges come from `memecoin_wallet_edges` for the chain at the
  current method versions, read-only, bounded (per-wallet cap and the existing `UNIVERSE_MAX_BYTES`),
  with `graph_state` = `ok` / `stale` / `not_measured` (non-Solana) / `unavailable` (open failed).
- Each wallet row gains `funded` from `real_stats_by_wallet(conn, chain)` for that chain. No paper
  field is served as evidence; the hypothesis export says "not available: legacy setups retired".
- `build_wallet_view` and the universe route never substitute an empty `:memory:` graph on failure.
  A failed open returns `graph_state='unavailable'` and the UI says so.
- Fix the brain projection bound: after `detail_for` drops `coins` and `per_seed`, any text still
  over 4,096 chars caps the largest remaining field with an explicit `"…(truncated, N chars)"`
  marker so the edge projects instead of being refused every hour.

#### D1 response contract (Beta, branch `beta/wm-d1-live-links`)

`GET /api/memecoin/wallet-map/universe?chain=<alias>`. An unknown chain is still HTTP 400
`{"error": "unknown_chain"}`. Every other answer is HTTP 200. These fields are added or changed;
the others (`stamp`, `clips`, `chains`, `selection_gates`, `execution_research`, `measurements`,
per-wallet `coverage`, `armed`, `effective`, `paused`, `metrics`, `strict_policy`) are unchanged.

| Field | Values |
|---|---|
| `graph_state` | `ok` / `stale` (run finished > 3 h ago; edges still served) / `not_measured` (the graph covers Solana only) / `unavailable` |
| `graph.state` | same as `graph_state` |
| `graph.reason` | `null` when ok/stale; `chain_not_measured`, `absent` (no graph file), `open_failed`, `unreadable` (corrupt, locked, malformed row), `no_finished_run`, `oversize` (edges JSON over `UNIVERSE_MAX_BYTES`) |
| `graph.run_finished_at`, `graph.age_s`, `graph.stale_after_s` | the latest finished `memecoin_wallet_graph_runs` row; `stale_after_s` = 10800 |
| `graph.method_version` | that run's version. Edges served = that version plus its `:`-suffixed variants (the slot method's `:cov1`); older versions are never served |
| `graph.computed_at` | newest `computed_at` among the served edges |
| `graph.edges_total`, `graph.edges_served`, `graph.omitted_by_wallet` | all edges of the family after roster exclusions; served after the per-wallet cap (`UNIVERSE_WALLET_EDGE_CAP` = 120 edges touching a wallet, in or out; strongest first by `null_p`, then `n_coins`; an edge is served while neither endpoint holds the cap, D3 amendment after Gamma's D1 note); `{wallet or address: n}` edges left out that touch it, counted on both endpoints. Live 2026-10-07: 881 / 881 / `{}` |
| `generated_at` | `graph.run_finished_at` (was the snapshot file's time) |
| `edges[]` | `kind` (edge_type), `direction` (`ordered`, or `association` for `co_buy_slot` and `shared_funder`), `src`, `dst` (graph identities), `src_uid` / `dst_uid` (roster id, else the raw address), `src_in_roster` / `dst_in_roster`, `n_coins`, `n_consistent`, `lag_p50_s`, `lag_p90_s`, `null_p`, `null_p99`, `flag`, `first_seen`, `last_seen`, `coins` (mints, at most 10 per edge as the job stores them), `coins_omitted` (`n_coins - len(coins)`, or `null` for a type without coins: `shared_funder`), `coverage_ref` (a key of `coverage_by_ref`), `provenance` `{method_version, computed_at}`. No edge carries its own `coverage` |
| `coverage_by_ref` | `{ref: coverage}`, each distinct coverage once. The job writes one coverage per type per run, so the ref is the type (`co_buy_slot`); a coverage that differs within a type gets `<type>#2`, `<type>#3` |
| `wallets[].links` | `{state, by_type}`; `state` = `graph_state`; `by_type` = `{follows, dumps_on, shared_funder, co_buy_slot, slot_follower}` counts over the whole family (before the cap) when ok/stale, `null` otherwise. A measured zero is `0`, never `null` |
| `wallets[].funded` | `{state: "ok", ...}` with `real_stats_by_wallet(conn, chain)` unchanged (`open_count`, `closed_count`, `realized_pnl`, `notional`, `roi_pct`, `win_rate`, `windows.{24h,7d,30d}.{closed,pnl,notional,roi_pct}`, `last_entry`); `{state: "none", open_count: 0, closed_count: 0}` when the wallet has no funded row on the chain; `{state: "unavailable"}` when the ledger read failed |
| `wallets[].paper` | `null` always: no paper field is evidence |
| `funded_scope` | one sentence: robot entries, `execution_mode='real'`, grade exclusions applied, only armed wallets have rows (the selection effect) |
| `hypothesis_export` | `{state: "not_available", reason: "legacy setups retired"}` |
| removed | `measurement_state` (the snapshot file's state) |

Edge direction by type: `follows` src = leader, dst = the wallet that bought after; `dumps_on`
src = seller, dst = the buyer it sold into; `slot_follower` src = the roster wallet whose slot was
read, dst = a buyer that landed after it in that slot; `co_buy_slot` src = the roster wallet, dst =
a buyer in the same slot, no order; `shared_funder` symmetric.

`/api/memecoin/wallet-map/herders` and `/api/memecoin/wallet-map/wallet` keep their payloads. Their
`stamp` gains `graph_state` (`ok` / `stale` / `unavailable`; `not_measured` on a non-Solana herders
request). A graph open failure is `stamp.error = "graph_unavailable"`, never a `:memory:` stand-in.
The wallet route's non-Solana 400 stays until D2.

Measured on the live graph (2026-10-07, read-only): the edge read is p50 25.2 ms / p90 27.1 ms (15
reads); edges plus `coverage_by_ref` are 0.90 MB (5 refs, 5.8 KB), down from 1.45 MB with per-edge
coverage. After the bound fix all 881 edge details are <= 4,096 chars;
the 6 `shared_funder` details carry the truncation marker.

### D2. Supporting coins (Beta)

- `/api/memecoin/wallet-map/wallet?id=<w>&chain=solana` adds, per coin, the source first-buy
  signature, slot and time, and the repeat buyers that landed after it in that slot (address,
  signature, slot index). Bounded to the 200 most recent coins; the response says how many were
  omitted.
- `&with=<other>` returns the coins both wallets share under each edge type in
  `memecoin_wallet_edges.evidence`, with per-coin times and lags. No shared coins returns an empty
  list plus the edge coverage, never a bare zero.
- Non-Solana chains return `{"graph_state": "not_measured"}` with HTTP 200, not a 400, so the UI can
  render availability rather than an error. This is a contract change: update any test that
  pins the old `solana_only` 400.

#### D2 response contract (Beta, branch `beta/wm-d2-supporting-coins`, stacked on D1)

`GET /api/memecoin/wallet-map/wallet?id=<w>&chain=<alias>[&with=<other>]`. No `id` is still 400
`id_required`; an unresolvable chain is 400 `unknown_chain`. Everything else is HTTP 200. The
existing payload (`stamp`, `wallet`, `before`, `herd`, `sells_into_it`, `it_sells_into`, `patterns`,
`details`) is unchanged; these fields are added:

| Field | Values |
|---|---|
| `graph_state` | the stamp's `graph_state`: `ok` / `stale` / `unavailable`; `not_measured` for a non-Solana chain, which returns the empty view shape (`wallet: null`, `coins: []`, ...) with HTTP 200 |
| `coins_total`, `coins_omitted` | the wallet's unit reads (one per sampled first buy), and how many fell outside the newest `COINS_MAX` = 200 |
| `coins[]` | existing `mint`, `at`, `followers`, `regulars`, plus `source` `{signature, slot, index, at}` (the source first-buy read) and `after[]` `{trader (roster id when the address is proven, else the address), address, index, signature, repeat_buyer}` sorted by slot index. When the source read has no slot index the order is unknown: `after` and `followers` are `null`, never `[]`/`0` |
| `notes.after_signature` | `"not stored by the slot read"`: the slot read keeps each later buyer's address and index only, so `after[].signature` is always `null` |
| `with` (only when `&with=` is given) | `{other, state, measured_link, edges[], shared_coins[], coverage_by_ref}`. `state` ok / stale / unavailable (graph absent or unreadable). `edges[]` = every live-family edge between the two wallets in either direction, in the D1 edge shape with `coins` replaced by the stored per-coin evidence objects (`follows` `{mint, leader_at, lag_s}`, `dumps_on` `{mint, buy_at, sell_lag_s}`, slot types `{mint, slot, src_index, dst_index}`; at most 10 per edge, `coins_omitted` counts the rest). `shared_coins` = the distinct mints across those edges. No edge: `measured_link: false`, `edges: []`, `shared_coins: []`, and `coverage_by_ref` of every measured type, so the empty list carries its scope |

Measured on the live graph and ledger (2026-10-07, read-only, wallet 22140383 with 405 coins):
the view takes 145-221 ms (3 runs, including the herders state it already computed) and is 92 KB
for its newest 200 coins; a `with` comparison takes 11 ms and is 1.6 KB.

### D3. Our funded results on those coins (Beta)

- For each supporting coin, attach our funded positions for the same `source_user_id`, `mint` and
  chain: `execution_mode='real'`, the robot-only and `WALLET_GRADE_SQL` filters that
  `real_stats_by_wallet` uses. Fields: opened_at, entry_notional, entry_lag_s, fill_gap_pct,
  total_copy_drift_pct, realized_pnl, return % (realized / notional), exit_reason, is_open.
- Coins we never entered say so: "not entered" plus the refusal reason when the attempt trace has
  one (existing capture data, read-only), else "no attempt recorded".
- The wallet-level funded block reuses `real_stats_by_wallet` output unchanged: all-time and 7d
  return %, closes, open count. Return % is the headline, dollars second (operator rule: judge
  wallets by percent return).

#### D3 response contract (Beta, branch `beta/wm-d3-funded-coins`, stacked on D2)

`GET /api/memecoin/wallet-map/wallet?id=<w>&chain=solana` adds:

| Field | Values |
|---|---|
| `funded` | the wallet's block, `real_stats_by_wallet(ledger, 'solana')[w]` unchanged as `{state: "ok", ...}` (headline `roi_pct`, then `windows.7d.roi_pct`, `closed_count`, `open_count`, dollars after); `{state: "none", open_count: 0, closed_count: 0}`; `{state: "unavailable"}` when the ledger read failed |
| `funded_scope` | robot entries, `execution_mode='real'`, grade exclusions applied, only armed wallets have rows (the selection effect) |
| `coins[].funded` | `{state: "entered", positions[], other_positions_same_coin[]}`. Candidates are the funded positions on the same wallet, mint and chain under `real_stats_by_wallet`'s filters (one read, `real_positions_for_wallet`, on `idx_mc_pos_wallet_mode_open`); a paper, manual, other-chain or grade-excluded row never appears anywhere. EXACT-EVENT ATTRIBUTION: a position is in `positions` (this source event's entry) only when its `origin_event_id`, else its attempt's `origin_event_id` (via `attempt_id`), carries the coin's source signature as a whole `:` token. Every other candidate goes in `other_positions_same_coin`, never counted as entered and never the coin's return |
| `linkage` (each position) | `origin_event_id` / `attempt_id` (how an entered row matched); `other_event` (the first id that carries a signature carries another one); `unknown` (neither id carries a signature: NULL, `act-<uuid>`, a hex id) |
| `positions[]` | `position_id`, `opened_at`, `closed_at`, `entry_notional`, `entry_lag_s`, `fill_gap_pct`, `total_copy_drift_pct`, `realized_pnl`, `return_pct` (realized / entry notional x 100 on a closed row with a positive notional; an open row, a missing PnL or a 0/NULL notional is `null`), `exit_reason`, `is_open`, `opened_vs_source_s` (seconds from the coin's source first buy; negative = an older trade on the same coin) |
| `coins[].funded` (no entered position) | `{state: "not_entered", other_positions_same_coin[], attempts, reason, attempt}`. `attempt` is the latest `memecoin_entry_attempts` row (one read, `entry_attempts_for_coins`, on `idx_mc_entry_attempt_coin`) whose `origin_event_id` carries this coin's source first-buy signature as a whole `:`-separated token; `reason` is its `terminal_reason`. With no such row: `attempts: 0`, `attempt: null`, `reason: "no attempt recorded"`. No reason is ever inferred |
| `coins[].funded` (ledger failed) | `{state: "unavailable"}` on every coin, never `not_entered` |

Measured on the live ledger (2026-10-07, read-only, 10 runs each): wallet 22140383 (newest 200 of 405
coins) D3 adds p50 6.0 / p90 6.7 ms, view p50 145 / p90 233 ms, 184 KB, 200 `not_entered`; its one
same-coin row (Aug 30, origin `act-<uuid>`) is `unknown` linkage, no longer `entered`. DegenCapitalLLC
fb0c112f (104 coins): D3 p50 5.8 / p90 6.1 ms, view p50 143 / p90 157 ms, 31 `entered` (all by
`origin_event_id`), 73 `not_entered`, 5 `other_event` and 2 `unknown` rows beside them.

### D4. Missing data and next step (Beta API, Lambda UI)

- The summary lists each gap with what would fill it, from fields D1-D3 already return:
  graph not measured on this chain; sample under 10 usable coins; graph stale; no funded closes;
  supporting coin without our attempt; dossier admission evidence missing.
- "Save as research question" posts the observation sentence plus the selected gap to the existing
  dossier note endpoint. No new workflow, no new table.

#### D4 response contract (Beta, branch `beta/wm-d4-gaps`, stacked on D3)

`GET /api/memecoin/wallet-map/wallet` adds `gaps[]`, in this fixed order, each
`{key, what, fill[, n][, since]}` (`what` = the gap in one sentence, `fill` = what would fill it).
Keys are stable for the page:

| key | when | extra |
|---|---|---|
| `graph_not_measured` | `graph_state = not_measured` (non-Solana); the only gap | |
| `graph_unavailable` | `graph_state = unavailable` (absent, unreadable, no finished run); the only gap | |
| `graph_stale` | `graph_state = stale` | `since` = the graph run time |
| `small_sample` | the wallet's read first-buy coins `n` < 10 (repeat buyers are named from 10) | `n` |
| `funded_unavailable` | the funded read failed; no later funded gap is claimed | |
| `no_funded_closes` | `funded.closed_count` is 0 or `funded.state = none` | |
| `coins_without_attempt` | listed coins `not_entered` with `attempts = 0` | `n` |

Not in this payload: "dossier admission evidence missing". The page reads it from the existing
`GET /api/memecoin/wallet-map/dossier?wallet=&chain=` (`entries[].codes`), because computing it
here would replay dossier episodes on every wallet view. "Save as research question" uses the
existing `POST /api/memecoin/wallet-map/dossier?wallet=&chain=` with `{"text": ...}` (1-4,000 chars,
CSRF header): the observation sentence plus the selected gap's `what` and `fill`. No backend change.

Live (2026-10-07, read-only): 22140383 and DegenCapitalLLC have no gap; 93b8c6e5 has
`coins_without_attempt` n=1.

## Edge cases (CLAUDE.md rule 5)

1. **Money path.** W3 moves the authority controls in the DOM only. A test proves every control still
   calls the same endpoint with the same confirmation text and CSRF header. D1-D3 are read-only.
   No slice touches the runner, signing or sizing. Web and runner still deploy together.
2. **Speed on the fill path.** None: no runner code changes. Web route latency is measured p50/p90
   before and after D1-D3 on the live DB (Gamma), and each route stays bounded.
3. **Concurrency and global state.** Flask serves threads. Every new read opens its own read-only
   handle (`open_graph_readonly`, `get_readonly_connection`) and closes it in `finally`. No module
   dict caches are added.
4. **Absent vs zero vs unknown.** Non-Solana graph = `not_measured`; graph open failure =
   `unavailable`; zero edges on a measured chain = 0. No funded closes = "none" with n=0; an
   unreadable ledger = unknown. The map draws unknown dashed and zero solid.
5. **Dependency failures.** Graph DB missing, locked or corrupt; ledger locked; oversize payload.
   Each returns a named state. The `:memory:` substitution is removed (fact 8).
6. **Units and identity.** Graph `network_id` is `solana`; funded rows use `network_id`; chain params
   go through `canonical_chain_id`. Wallet identity is `source_user_id`, which may be a UUID or a
   `pump:<address>` alias; edges carry both `src` and `src_user_id`. Return % is realized / entry
   notional on closed rows. Times are UTC ISO; graph times use the J7 corrected clock.
7. **The sample.** Relationship counts come from our stored first buys of roster wallets, so they
   describe our coverage, and the summary says so. Funded results exist only for wallets we chose to
   arm: a selection effect, labelled beside the number.
8. **Rollback.** Each slice is one merge commit; revert it and redeploy the previous SHA. No data
   migration, no new cron, no new file under `data/`.

What this design does NOT handle: relationships on EVM chains (the graph is Solana-only), proven
ownership or funding links beyond the 6 `shared_funder` edges, any causal claim, and a validation
workflow that runs a hypothesis forward. Those stay listed as missing data.

## Acceptance (from the audit's section 7)

Gamma checks each item in the built-in browser at 1366x768 and 664x652 after the slice that owns it
deploys:

1. A new reader explains size, radius, line style and unknown from the page alone. (W3, W5)
2. At 1366x768 the selected wallet's observation and next step are readable without scrolling an
   inner column. (W1, W3)
3. Each headline number names its population, n and age. (W5)
4. Two linked wallets can be compared, their shared coins inspected, and Back returns to the prior
   wallet. (W4, D2)
5. Ordinary wheel scrolling over the map reaches the content below; zoom is deliberate. (W4)
6. A metric change or Refresh keeps the open section and focus. (W4)
7. Reload and a pasted deep link restore the investigation. (W4)
8. Ethereum shows "relationships not measured", not a measured zero. (W5, D1)
9. In narrow expanded mode the details are reachable. (W1)
10. No element can be read as a validated strategy, ownership or profit claim. (W3)
11. Authority controls behave exactly as before (test in W3). Runner untouched (diff check).

## Crew, order, branches

- **Lambda**: W1 -> W2 -> W3 -> W4 -> W5, branches `lambda/wm-w1-readability` etc. W1 does not depend
  on D1. W5 waits on D1's fields.
- **Beta**: D1 -> D2 -> D3 -> D4 API, branches `beta/wm-d1-live-links` etc., after chain-1 F1-F3.
  D1 also publishes the response contract (field names and states) in this spec before Lambda wires it.
- **Gamma**: reviews each branch; measures route p50/p90 for D1-D3; runs the acceptance list.
- **Alpha**: merges in order, deploys runner+web together at a flat book, keeps this spec current.

Files touched (no others without an amendment here): `static/styles.css`, `static/wallet_universe.css`,
`static/wallet_universe.js`, `static/wallet_map.js`, `templates/index.html` (asset versions),
`sniper/routes/api_memecoin.py`, `sniper/services/memecoin_wallet_map.py`,
`scripts/research/wallet_link_graph.py` (D1 bound only), `sniper/persistence/db_memecoin.py` (D3 amendment: two read-only helpers, `real_positions_for_wallet` and `entry_attempts_for_coins`, beside `real_stats_by_wallet` so the funded filters live in one place), tests below.

## Verification

Existing suites stay green:

```bash
node --test tests/wallet_universe_camera.test.mjs
python3 -m pytest tests/test_memecoin_wallet_map.py tests/test_memecoin_wallet_map_ui.py tests/test_wallet_map_actions.py tests/test_wallet_map_research_boundary.py tests/test_wallet_universe.py tests/test_wallet_universe_navigation.py tests/test_wallet_dossier.py tests/test_wallet_link_graph_brain.py tests/test_web_server_asset_paths.py -q
```

New checks, one per risk, each shown to fail before its fix:

- W1: no `body::before`/`body::after` overlay rule remains in `static/styles.css`.
- W3: each authority control keeps its endpoint, confirmation text and CSRF header after the move.
- W4: a wheel event without ctrl/cmd over an unarmed map does not change zoom; URL round-trip
  restores the full state object.
- D1: a graph open failure yields `graph_state='unavailable'`, never an empty edge list; a
  non-Solana chain yields `not_measured`; the universe route never opens
  `wallet_universe_<chain>.json`.
- D1 bound: a `shared_funder` edge with oversized coverage projects with a truncation marker.
- D3: a paper position on the same wallet and coin never appears in the funded block; return % is
  realized / notional on closed rows only.
