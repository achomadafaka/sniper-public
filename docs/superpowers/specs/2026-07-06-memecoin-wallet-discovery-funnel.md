# Memecoin Wallet-Discovery Funnel — Design Spec

**Date:** 2026-07-06
**Status:** approved (operator asked for "a funnel to find more wallets to copy")
**Related:** [[project-fomo-account-analyzer]], [[research-fomo-family-ws-feasibility]]

## 1. Problem

The curated list (21 traders) produces few copy signals — they go quiet for long
stretches. Meanwhile `run_once` pulls the whole fomo `/feed` (hundreds of large buys/hour)
and **discards every non-curated event** (`memecoin_runner.py:98`). Validated live: a ~6s,
3-poll sample had **22 buys, all 22 non-curated, from 19 distinct traders** (BinkBinkBink
$51k, orcaxocean $47k, …). We firehose past the exact thing we lack: candidate wallets.

The bottleneck is **curation width**, not copy mechanics or the analyzer. The funnel widens
the top: capture the discarded large-buy firehose, aggregate it per trader, screen the
active ones with the existing analyzer, and surface a ranked candidate list the operator
promotes with one click.

## 2. The funnel

```
fomo /feed firehose ─► capture non-curated large buys ─► per-trader aggregate
   ─► analyzer screen (top-N) ─► ranked Discovery table ─► operator promotes ─► curated
```

**Stance (matches the analyzer):** the funnel RANKS by feed activity (sighting frequency ×
size) to find *front-runnable* candidates; it does NOT decide they're profitable. Skill comes
only from the analyzer screen (realized PnL / coverage). "Active + large" ≠ "good" — the UI
shows both and never conflates them. Promotion is **operator-gated** (no auto-add).

## 3. Data model

New aggregate table (one row per non-curated trader; O(1) per read, bounded):

<!-- schema:external=memecoin_candidates -->
`memecoin_candidates`: `source_user_id` PK, `handle`, `sighting_count`, `total_buy_usd`,
`max_buy_usd`, `first_seen`, `last_seen`, `last_ticker`, `last_mint`, `status`
(`new`|`ignored`|`promoted`, default `new`).

**Capture (dedup-free):** in `run_once`, for each feed event that is a **buy**, from a
**non-curated** trader, with `their_size_usd >= MEMECOIN_DISCOVERY_MIN_USD`: insert it into
`memecoin_events` (existing `insert_event` OR-IGNORE dedup) and, only when that returns a NEW
row, `upsert_candidate` (increment counters). Tag the event `action='candidate'`. The curated
copy loop is unchanged and disjoint (curated ∩ non-curated = ∅).

**Anti-pollution:** `list_recent_events` (the "Recent activity" UI) filters OUT
`action='candidate'` so the firehose never spams the copy feed.

## 4. Screen

`memecoin_candidates` LEFT JOIN `memecoin_trader_analytics` on `source_user_id`. The analyzer
(`scripts/analyze_fomo_accounts.py`) gains a `--candidates N` mode: pull the top-N `new`
candidates by `total_buy_usd`, analyze each (reuses everything), populate `trader_analytics`.
Rate-limited exactly like `--all` (shares fomo's WAF budget; 429 backoff).

## 5. Surfaces

- `GET /api/memecoin/candidates?min_sightings=&min_usd=` → `{rows}` (aggregate LEFT JOIN
  analytics, `status='new'`, sorted by `total_buy_usd` desc).
- `POST /api/memecoin/candidates/<uid>/ignore` → `status='ignored'` (dismiss).
- `POST /api/memecoin/candidates/<uid>/promote` → add to curated (`source='manual'`) +
  `status='promoted'`.
- UI: a "Discovery — non-curated large buyers" card in the memecoin tab: ranked rows with
  `sightings`, `total$`, `max$`, `last_ticker`, and analyzer columns (`n_closed`,
  `realized_pnl`, `median%`, `coverage`, `unmatched`) when screened, plus **Add** / **Ignore**
  / chart. A "screen top N" hint (analyzer is CLI/cron, not per-request, for rate safety).

## 6. Config (`sniper/settings/memecoin.py`)
- `MEMECOIN_DISCOVERY_ENABLED` (default True) — capture kill switch.
- `MEMECOIN_DISCOVERY_MIN_USD` (default 1000) — large-buy floor for candidacy (front-run
  edge is large-trades-only; keeps noise out of the aggregate).

## 7. Honesty rails
- Candidate rank = feed activity = a **popularity/liquidity** signal, not skill. The card
  header says so. Analyzer columns (skill) sit beside it; a high-sighting trader with bad/thin
  analyzer metrics is a herd-magnet, not a copy target.
- Promotion is operator-only. Ignored candidates stay ignored (status preserved on re-sighting).

## 8. Testing (TDD)
- db: upsert increments/maxes correctly; status preserved on conflict; list filters + joins.
- run_once: non-curated large buy → candidate; curated → not; sub-threshold → not; re-delivery
  doesn't double-count; capture doesn't disturb the copy loop.
- endpoints: list shape, ignore, promote (adds to curated + marks promoted).
- recent-activity excludes `action='candidate'`.
- CLI `--candidates` resolves top-N and persists analytics.

## 9. Out of scope (v0)
`distinct_mints` per candidate (needs a sightings sub-table); auto-screen cron; auto-promote.
