# Coin Radar — design (2026-07-19)

**Goal:** A real, usable in-app coin radar: discover coins (incl. before our wallets touch them), sort by volume/liquidity/mcap, see which tracked/armed wallets bought a coin, search any coin by name (replacing the Fomo lookup UX), track/rate coins, and get an LLM triage verdict — as inputs to manual or LLM decisions, not a claimed edge.

**Non-goals:** replacing the Fomo *feed* (still drives copy signals); a hardcoded "promising" formula (scoring is measured against outcomes, never asserted); real on-chain holder lists (we use the stronger known-**buyer** proxy).

## Data spine
A coin-centric universe keyed by `(network_id, mint)`, assembled at query time from tables we already have, plus (Stage 2) an external-discovery table.

Sources / columns used (verified 2026-07-19):
- `memecoin_token_episodes` (chain_id, token_address, started_ts, last_signal_ts, consensus_cluster_count, status) — coins with wallet-consensus activity.
- `memecoin_quote_observations_v2` (network_id, mint, price, liquidity, market_cap, volume_24h, observed_at) — latest enrichment.
- `memecoin_feed_observations_v2` (network_id, mint, ticker, source_user_id, observed_at) — buyers + names.
- `memecoin_events` (network_id, mint, ticker, source_user_id, created_at) — buyers (episodes can have 0 feed rows).
- `memecoin_wallets` (source_user_id, real_approved, paused, excluded, handle, label) — armed/tracked wallet set for the known-buyer overlap.
- `memecoin_radar_ratings` (episode_id, network_id, mint, rating) — persisted operator/LLM ratings.
- NEW `memecoin_external_discovery` (Stage 2): chain_id, mint, ticker, source, mcap, liquidity, volume_24h, first_seen, last_seen.

## Stage 1 — usable scanner + search (web-only)
- `sniper/services/memecoin_universe.py`:
  - `universe_rows(conn, *, sort, chain, source, min_liq, min_vol, q, window_hours, limit, now_iso)` — one row per coin with ticker, chain, mcap, liq, vol, age, buyers, **armed_buyers** (count + handles of buyers that are `real_approved` and active), source, rating, real-open flag. Sortable by mcap/liq/vol/buyers/armed/age.
  - `search_coins(conn, q, *, live_fn=None, limit)` — local name/ticker/mint match; optional live DexScreener/GT fallback for coins not in our universe (the Fomo-lookup replacement).
- Endpoints (`api_memecoin.py`): `GET /api/memecoin/universe` (query params → sortable rows), `GET /api/memecoin/coin-search?q=`.
- UI (`app.js`): a "Radar" workboard — sortable table (mcap/liq/vol/buyers/armed/age/chain), a search box, an armed-buyer column (hover → handles), star/rate/track (reuses `memecoin_radar_ratings`).

## Stage 2 — external discovery (worker + cron)
- `sniper/services/memecoin_discovery_external.py`: DexScreener-trending + GT-new-pools pollers → upsert `memecoin_external_discovery` with a `source` tag. Bounded worker, **separate rate budget** (never starves the funded runner's GT single-flight gate — reuse the shared cooldown, low interval). 4663 is GT-only.
- `scripts/memecoin_discovery_worker.py` + a watchdog/cron entry.
- `universe_rows` unions the external table so hot coins appear before our wallets touch them; pruning drops stale/never-traded rows.

## Stage 3 — LLM triage (web/worker)
- `sniper/services/memecoin_radar_triage.py`: over the top-N universe rows, build a compact fact sheet (stats + armed-buyer context + optional X snippet) → LLM → `{verdict: promising|watch|skip, reason}` persisted (reuse `memecoin_radar_ratings` with a `rating_version='llm-triage-v1'`). Bounded to top-N; never auto-trades.
- Endpoint `POST /api/memecoin/radar/triage` + a UI "triage" action; verdicts render in the table for operator confirm.

## Honest constraints
- External ingest adds GT/DexScreener rate pressure; DexScreener is blind to 4663 (GT covers it).
- "Promising" is a measured hypothesis: the triage/score is logged and compared to outcomes before it's trusted.
- Universe needs pruning to stay usable.

## Integrity invariants
- Buyer counts and armed-buyer handles use buy observations only. Feed-v2 rows, legacy event rows, and episode-only coins are all represented without double-counting wallets.
- Coin identity is `(network_id, mint)`; `0x` mints are case-normalized for Radar joins and cache keys. The same address on different networks remains distinct.
- Triage input and output are keyed by `(network_id, mint)`. A mint-only verdict is accepted only when that mint is unambiguous in the bounded batch; malformed model output is ignored.
- Discovery and cache timestamps use corrected real UTC. The worker repairs the initial host-clock-skewed Radar rows, prunes stale discovery rows, and the API ignores quote-cache rows older than 10 minutes.
- The discovery daemon defaults to a 300-second cadence. It must not use the earlier 45-second loop, which exhausted the shared GeckoTerminal budget and competed with funded exit pricing.

## Test plan
Each stage ships with focused tests: `test_memecoin_universe.py` (rows, sort, armed-buyer overlap, search), `test_memecoin_discovery_external.py` (upsert, source tag, rate-guard, dedupe), `test_memecoin_radar_triage.py` (fact-sheet build, verdict persist, top-N bound). Web-layer; no funded-runner deploy for Stages 1/3; Stage 2 adds a worker (not the funded runner).
