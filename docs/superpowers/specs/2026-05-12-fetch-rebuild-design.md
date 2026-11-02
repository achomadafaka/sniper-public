# Fetch + Price-History Rebuild — Design

**Date:** 2026-05-12
**Status:** design doc, awaits operator review before /ship
**Severity:** correctness — silent data truncation on every prior wallet run
**Related:** G2 / G5 in `docs/audits/2026-05-10-wallet-analyzer-live-sampling.md`

---

## Empirical findings (2026-05-12)

Two read-only probes against the live Polymarket API on Pestle
`0x241f846866c2de4fb67cdb0ca6b963d85e56ef50`:

### Finding 1: `/closed-positions` truncation was self-inflicted

`sniper/markets/polymarket_api.py:fetch_user_positions` caps at
`page_limit=50 × max_pages=100 = 5000`. Every Pestle run to date has used
this cap.

Live probe with no client `max_pages`:

```text
page  1 offset    0 got 50 cumulative   50
...
page157 offset 7800 got 48 cumulative 7848  ← natural end (page < limit)
page158 offset 7848 got  0 cumulative 7848  ← empty page; stop
```

**Pestle has 7,848 closed positions, not 5,000.** Our cap dropped 2,848
rows (36%). 114 seconds to fetch the full set sequentially with no
backoff or 4xx.

**The dropped slice is the most recent ~5 months**, not the oldest.
The API call uses `sortBy=TIMESTAMP, sortDirection=ASC`
(`polymarket_api.py:814-815`), so offset 0 is the oldest position
(Feb 22 2025 for Pestle) and the cap at row 5,000 stops around Dec 2025.
Pestle's positions span Feb 2025 → May 2026 (today); our capped view
ends at ~Dec 2025. **Every prior analyzer run silently dropped Pestle's
~5 most recent months of trading** — exactly the slice that should
inform any current-edge claim.

This is also why Pestle's negative-PnL honesty floor fires: his
leaderboard lifetime PnL is +$1.9M, but the capped older-only slice
sums to `-$727K` realized. The honesty floor is correctly reacting to
"this wallet, on the data you gave me, is losing money." But the
data we gave it was systematically truncated to his historical losses.
Pestle's recent edge is real and invisible.

Implications for every analyzer run on a high-volume wallet to date:
- `net_pnl` is a partial sum of the oldest 5,000 realized PnLs, not lifetime
- `n_closed_positions` reads `5000` regardless of true volume
- H1 / H2 / H3 / H5 / Position Builder / Exit Attribution all run on the
  oldest-5000 subset; recent edge is invisible by construction
- DW/EW ROI divergence flags compute on a partial denominator
- F2 top-3 concentration can miss the real top 3 if they're in the recent slice
- F3 recent_90d_fraction is biased toward zero (recent positions excluded)
- H7 stationarity / time-split holdout treat "old" as "recent" — testing
  edge on data that is itself ~6 months stale

This is the single biggest correctness gap in the current pipeline.

### Finding 2: `/prices-history` response is bounded by market data, not the window

Live probe against one Pestle activity asset (a recently-resolved market):

| Window | Fidelity | Bars returned | Span actually returned | Elapsed |
|--------|---------:|--------------:|-----------------------:|--------:|
| 1h | 60min | 1 | 0.0h | 1.4s |
| 24h | 60min | 4 | 2.3h | 0.5s |
| 24h | 5min | 33 | 2.7h | 0.5s |
| 168h (7d) | 60min | 4 | 2.3h | 0.5s |
| 168h (7d) | 5min | 37 | 3.0h | 0.5s |

Widening the window from 1h to 168h returned **the same data** — the
market simply doesn't have bars beyond its activity span (it resolved).
This means **one HTTP call per unique market id**, with a generously
wide window, returns the complete bar history for that market. We can
serve N sell-timestamp lookups per market from one HTTP response.

### Finding 3: Position Builder only observes ~1% of positions at fill level

`closed_positions` returns aggregated position summaries (one row per
`(wallet, conditionId, outcome)` with entry/exit/realizedPnl collapsed).
**Position Builder needs fills**, not summaries — it groups `activity_rows`
by `(condition_id, asset)` to reconstruct buy ladders, sell ladders,
partial-exit method, milestone path
(`position_building.py:805-873`).

For Pestle, the report shows:

| Field | Value |
|---|---|
| `n_closed_positions` | 5,000 (capped — actual 7,848) |
| `activity_rows_fetched` | 2,976 |
| `position_observation_window_coverage` | **0.0104 (1.04%)** |
| `n_positions observed by builder` | 5,108 (most are "context positions", no fills) |

Position Builder saw fill-level data for **1.04% of Pestle's positions**.
The other 98.96% are tracked by aggregate only. Reason: `/activity` returns
the most-recent 3,000 fills in a single window. For Pestle's velocity
those 2,976 fills cover roughly 50 of his 5,000+ closed positions; the
other ~4,950 positions' fills predate the API's recent window.

The escape hatch already exists — `polymarket_api.fetch_user_activity`
supports `walk_full_history=True`, which walks `end_ts` backward to
recover up to `max_total_rows=50,000` fills (`polymarket_api.py:855-978`).
But this flag is **off by default** on every analyzer run.

For wallets with significant volume, position-building behavior — how
they laddered in, partial exits, scaling on dips, the entire WHY of
their P&L — is currently invisible by default.

### Finding 4: Stationarity (H7) + time-split holdout operate on the stale capped slice

`run_h7_stationarity` (`held_analyst.py:287-335`) sorts the closed-positions
list chronologically and runs CUSUM to detect a regime change. With the
oldest-5,000 cap:
- Pestle's "regime change" detection sees Feb 2025 → Dec 2025 data
- A regime change at, say, row 3,000 (Aug 2025) would slice to a
  "post-regime" window of Aug 2025 → Dec 2025
- That post-regime window then becomes the input for H1/H2/H3 holdout
- But the wallet's actual current state is May 2026 — **6 months
  newer than the "post-regime" subset**

Time-split holdout (5-fold rolling-origin) is the most-recent fold of a
chronologically-sorted population. Under the cap, "most-recent" means
"the last fold of the oldest 5,000." It's stale by construction. We've
been calling positions "out-of-sample" when they're 6+ months old.

### Finding 5: Activity walking is fragile to single-page API timeouts

A live probe of `fetch_user_activity` on Pestle with `walk_full_history=True`
hit a `Read timed out (20s)` on a single page (offset=1150, end_ts=Oct 2025
window) and entered the retry loop. Each retry: 20s timeout + exponential
backoff (0.66s, 1.31s, 2.62s, ...) up to 6 attempts. A single bad page
can stall a walking fetch for 2-5 minutes.

For a high-volume wallet that walks 50,000 rows = ~1,000 paginated calls,
even a 1% failure rate adds tens of minutes of retry latency on top of
the base ~10 min walk. The default behavior is to retry-then-continue;
the call doesn't fail hard, just stalls.

Implications:
- "Slow" runs are partly retry latency, not just rate-limit pace
- Without per-page progress emission, operator can't distinguish
  "rate-limited" from "stuck retrying" from "actually broken"
- A hard timeout cap on the WALK (vs. per-call) would prevent the
  pathological case where one bad endpoint stalls a 50-wallet cohort

**Batching is essentially free.** Resolved markets are immutable, so
the cache is permanent. The marginal cost of "any timestamp on market X"
is zero after the first lookup.

---

## Complete blindspot audit (11 findings, ranked by impact)

| # | Severity | Surface | Issue | Direction of bias |
|---|---|---|---|---|
| 1 | **CRITICAL** | `/closed-positions` cap + `sortBy=ASC` | Drops 36% of Pestle (2,848 rows) — **the most recent ~5 months**. Lifetime +$1.9M → analyzer net_pnl `-$727K`. | Recent edge invisible |
| 2 | **CRITICAL** | `activity_rows` default `walk_full_history=False` | Position Builder sees fill-level data for **1.04%** of positions. Build mechanism invisible. | Behavioral mechanism invisible |
| 3 | HIGH | H7 stationarity + time-split holdout | Operate on capped + ASC-sorted slice. "Out-of-sample" testing is on 6+ months old data. | Stale OOS |
| 4 | HIGH | F3 recent_90d_fraction | Computes "last 90 days" against the oldest 5K. Biased toward zero. | Self-defeating |
| 5 | HIGH | F2 top-3 concentration | Top-3 by abs P&L computed on capped slice. | Wrong tails |
| 6 | HIGH | `net_pnl` excludes open-position MTM | `sum(realizedPnl for p in closed_positions)`. Wallets with large open books are understated. Negative-PnL honesty floor uses this. | Lifetime understated |
| 7 | MODERATE | Per-call API timeouts under load | `read_timeout=20s` doesn't fire on slow-drip responses. `requests` library has no total-request timeout. Live probe stalled 3+ min on one socket. | "Running" vs "stuck" indistinguishable |
| 8 | MODERATE | Subgraph fallback trigger | Only fires at `len(activity) >= 3000` exactly. Coverage gaps at 2,999 are silent. | Gap detection missing |
| 9 | MODERATE | Capital ledger off by default | Without it, I1 DW/EW divergence ungroundable, M4 accounting tie disabled. | No capital-flow verification |
| 10 | LOW | Open-positions endpoint same `max_pages=100` cap | Most wallets unaffected; structural blindspot for whales. | Inconsistent fix |
| 11 | LOW | Proxy wallet mismatch warn-and-pass | `proxyWallet != wallet` logs WARN but row still returns. Could poison input. | Silent data quality |

**Phase A in this spec addresses findings #1, #2, #3, #4, #5, #6, #7, #10. Findings #8/#9/#11 are smaller follow-ups; Phase B addresses the price-history cache (independent of any blindspot).**

## What this means for the rebuild

Doctrine: **always fetch the full picture**, no client-side caps, retry transient failures indefinitely with backoff, surface explicit coverage metadata so the operator knows what's being analyzed. No wall-clock deadlines that silently truncate.

---

## Design

### Phase A — the "proper picture" rebuild (one PR, comprehensive)

Phase A is intentionally larger than my original spec. The user's call is:
no caps anywhere, full-history default, never silently truncate. This means
A bundles what was previously A+C+ HTTP-fix into one shippable unit, so a
single re-run of any wallet produces a complete + observable + honest report.

**Scope** — five sub-changes, each in its own file or module, but all in
one PR because they're load-bearing for "proper picture":

#### A.1 — Remove pagination caps + sort DESC (Findings #1, #10)

`sniper/markets/polymarket_api.py:fetch_user_positions`:

- Drop the `max_pages: int = 100` parameter entirely. Paginate until the
  API returns an empty page, a partial-final page, or a 4xx after retries.
- For `closed-positions` endpoint: change `sortDirection=ASC` to **`sortDirection=DESC`**
  (newest first). Reason: if Polymarket ever does refuse past some offset,
  we want the loss to be in the OLD slice, not the recent slice.
- For `positions` (open) endpoint: same — paginate to natural end.
- Record termination reason on a sidecar field: `empty_page` /
  `partial_page` / `api_refused`. Only `api_refused` raises an honest
  "real cap hit" flag.

```python
def fetch_user_positions(
    session: HTTPClient,        # see A.3 — switching to httpx
    wallet: str,
    endpoint: str = "positions",
    page_limit: int = 50,
    progress_emitter: Callable[[int, int], None] | None = None,
) -> tuple[List[Dict[str, Any]], FetchTermination]:
    rows: List[Dict[str, Any]] = []
    offset = 0
    while True:
        params = {"user": wallet, "sizeThreshold": 0,
                  "limit": page_limit, "offset": offset}
        if endpoint == "closed-positions":
            params["sortBy"] = "TIMESTAMP"
            params["sortDirection"] = "DESC"  # newest first
        try:
            data = _request_json(session, f"{DATA_BASE_URL}/{endpoint}",
                                 params=params)
        except httpx.HTTPStatusError as e:
            return rows, FetchTermination("api_refused",
                                          status=e.response.status_code,
                                          offset=offset)
        if not isinstance(data, list) or not data:
            return rows, FetchTermination("empty_page", offset=offset)
        rows.extend(data)
        if progress_emitter:
            progress_emitter(len(rows), offset)
        if len(data) < page_limit:
            return rows, FetchTermination("partial_page", offset=offset)
        offset += page_limit
```

#### A.2 — Default `walk_full_history=True` (Finding #2)

`scripts/run_wallet_protocol.py` and `sniper/analysis/wallet_protocol/fetch.py`:

- Default the wallet-protocol fetch path to `walk_full_history=True`.
- Add a `--bounded-history` opt-OUT flag for the rare case an operator
  explicitly wants the most-recent-3K-only window. Bounded is no longer
  the default.
- Drop the `max_total_rows=50_000` safety cap. Walk to natural exhaustion
  per Polymarket's `end_ts` chain.
- Position Builder's observation coverage now reflects the wallet's real
  shape, not our fetch limit.

#### A.3 — HTTP layer replacement: `httpx` with proper total timeouts (Finding #7)

`sniper/markets/polymarket_api.py:_request_json` is rewritten on top of
`httpx.Client`:

```python
client = httpx.Client(
    timeout=httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=5.0),
    # Total wall-clock per request — kills slow-drip responses that
    # current `requests` lib lets hang indefinitely
    transport=httpx.HTTPTransport(retries=0),  # we own retries explicitly
)
```

Wrap each call with explicit per-request budget. **No wall-clock cap on
retries** (per operator decision: don't cut short). Retries continue
indefinitely with exponential backoff up to a per-attempt sleep cap of
60s, so a permanently broken endpoint just slows the run instead of
artificially failing it.

Session reset hook: if 3 consecutive timeouts on the same `httpx.Client`,
discard and rebuild. Targets the "sticky-keepalive" pathology I observed
in the live probe (3+ min hang on one socket).

#### A.4 — PnL composition + open-position breakdown (Finding #6)

New fields on `ProtocolReport`:

```python
realized_pnl: float                  # sum(realizedPnl for p in closed_positions)
unrealized_pnl: float                # sum((curPrice - avgPrice) * size for p in open_positions)
combined_pnl: float                  # realized + unrealized

realized_roi: float | None           # realized / sum(closed cost basis)
unrealized_roi: float | None         # unrealized / sum(open cost basis at avgPrice)
combined_roi: float | None           # combined / (closed + open cost basis)

open_position_breakdown: dict        # see below
```

Backward compatibility: `net_pnl` stays on the report and continues to
equal `realized_pnl` exactly. No existing reader breaks.

`open_position_breakdown` shape (per operator's 3 chosen cuts):

```python
{
    "n_open_total": int,
    "n_open_dead": int,              # curPrice <= 0.01
    "n_open_in_money": int,          # curPrice > avgPrice
    "n_open_underwater": int,        # curPrice <= avgPrice (excludes dead)
    "dead_capital_usdc": float,      # sum of avgPrice*size for dead positions
    "in_money_capital_usdc": float,
    "underwater_capital_usdc": float,
    "in_money_unrealized_pnl": float,
    "underwater_unrealized_pnl": float,
    "time_in_position": {
        "median_days": float,
        "p25_days": float,
        "p75_days": float,
        "p95_days": float,
        "n_stale_90d": int,          # held > 90 days
        "n_stale_365d": int,
    },
}
```

#### A.5 — Honesty floor uses BOTH realized and combined signals (Finding #6)

Update `_apply_negative_pnl_honesty_floor` in
`sniper/analysis/wallet_protocol/analyze_wallet.py`:

```python
def _apply_negative_pnl_honesty_floor(report):
    if report.overall_verdict != "edge_detected":
        return report
    # Downgrade only when BOTH realized AND combined are non-positive
    pnl_floor_satisfied = (report.realized_pnl > 0) or (report.combined_pnl > 0)
    coverage_floor_satisfied = (...)  # unchanged
    if pnl_floor_satisfied or coverage_floor_satisfied:
        # Special caveat when realized < 0 but combined > 0 (depends on opens)
        if report.realized_pnl <= 0 and report.combined_pnl > 0:
            report.data_quality_flags.append(
                "EDGE_DEPENDS_ON_OPEN_POSITIONS: "
                f"realized={report.realized_pnl:.0f}, "
                f"unrealized={report.unrealized_pnl:.0f}"
            )
        return report
    # Both negative: existing downgrade behavior
    report.data_quality_flags.append(NEGATIVE_PNL_EDGE_UNEXPLAINED)
    ...
```

Wallets with strong unrealized are flagged but not downgraded — the
operator can see "edge currently depends on open positions" without the
verdict being silently demoted.

#### A.6 — Per-page progress emitters (Finding #7 observability)

`sniper/analysis/wallet_protocol/run_events.py:JsonlEmitter` gains three
new event types:

| Event | Payload | Emitted by |
|---|---|---|
| `fetch_progress` | `{endpoint, n_rows, offset, page, elapsed_s}` | `_request_json` after each successful page |
| `fetch_window_complete` | `{endpoint, window_end_ts, n_rows_window, total_rows}` | `fetch_user_activity` after each `end_ts` window when `walk_full_history` |
| `fetch_terminated` | `{endpoint, reason, n_rows, offset, status}` | once per endpoint at termination |

Operator running with `--run-log-dir` sees each page land as a single
JSONL line. Distinguishes "running but slow" from "stuck" trivially.

#### A.7 — Time-coverage metadata in every report

New fields on `ProtocolReport`:

```python
time_coverage: dict = {
    "first_position_ts": int,          # min(timestamp) over fetched positions
    "last_position_ts": int,           # max(timestamp) over fetched positions
    "duration_days": int,
    "n_positions_per_month": float,    # rough velocity
    "first_fill_ts": int,              # from activity_rows
    "last_fill_ts": int,
    "fetch_termination": {             # one entry per endpoint
        "closed_positions": "empty_page" | "partial_page" | "api_refused",
        "open_positions": ...,
        "activity": ...,
    },
}
```

Every analyzer report carries explicit metadata about what slice of
history it analyzed. No more silent truncation; coverage is always
machine-readable.

### Phase B (follow-up) — persistent SQLite price-history cache + batched-window adapter

**Scope:**
- New: `sniper/markets/price_history_cache.py` (SQLite-backed cache)
- Rewrite: `sniper/analysis/wallet_protocol/price_history_adapter.py`
- Touch: `sniper/analysis/wallet_protocol/held_analyst.py` (H6) and
  `sniper/analysis/wallet_protocol/flipper.py` (FH6) only at the
  call-shape level if needed

**Cache schema:**

```sql
CREATE TABLE IF NOT EXISTS price_history_bars (
    market_id TEXT NOT NULL,
    ts INTEGER NOT NULL,          -- bar start timestamp
    fidelity INTEGER NOT NULL,    -- 5, 60, etc. (minutes per bar)
    price REAL,                   -- mid price; NULL when bar exists but has no fill
    fetched_at INTEGER NOT NULL,  -- unix epoch of fetch
    PRIMARY KEY (market_id, ts, fidelity)
);

CREATE TABLE IF NOT EXISTS price_history_fetches (
    market_id TEXT NOT NULL,
    fidelity INTEGER NOT NULL,
    start_ts INTEGER NOT NULL,
    end_ts INTEGER NOT NULL,
    fetched_at INTEGER NOT NULL,
    n_bars INTEGER NOT NULL,
    PRIMARY KEY (market_id, fidelity, start_ts, end_ts)
);
```

`price_history_fetches` tracks which `(market, fidelity, window)` tuples
we've already requested, so a re-run never re-fetches the same window.

**Adapter shape:**

```python
def build_price_history_fn(
    session: requests.Session | None = None,
    fidelity: int = 60,
    cache_db: str = "sniper_price_cache.db",
    timeout: float = 10.0,
    progress_emitter: Callable[[str, int, int], None] | None = None,
) -> Callable[[str, int], float | None]:
    """Returns a closure that resolves (market_id, ts) → price using a
    persistent cache. First call for a market triggers a full-lifetime
    fetch; subsequent calls for that market are O(1) cache lookups.
    """
    conn = _open_cache(cache_db)
    fetched_markets: set[str] = set()  # in-session marker; cache db is source of truth

    def _ensure_market_fetched(market_id: str) -> None:
        if market_id in fetched_markets:
            return
        if _market_fully_cached(conn, market_id, fidelity):
            fetched_markets.add(market_id)
            return
        # One call per market — wide window covers entire market lifetime
        bars = _fetch_market_bars(
            session, market_id,
            fidelity=fidelity,
            start_ts=0,  # market inception
            end_ts=int(time.time()),
            timeout=timeout,
        )
        _store_bars(conn, market_id, fidelity, bars)
        fetched_markets.add(market_id)

    def _price_history_fn(market_id: str, ts: int) -> float | None:
        if not market_id or not ts:
            return None
        _ensure_market_fetched(market_id)
        return _lookup_first_bar_at_or_after(conn, market_id, fidelity, ts)

    return _price_history_fn
```

**Key properties:**
- One HTTP call per **unique market_id**, not per (market_id, ts) pair
- Cache persists across runs, across wallets, across cohort passes
- Resolved markets cached forever (data is immutable)
- Active markets: a re-fetch happens when `(end_ts - latest_cached_bar_ts) > active_market_ttl_seconds` (configurable; default 24h)
- Empty windows (market has no bars) cached as a single null sentinel so we don't refetch

**For Pestle** (~2,500 sells across ~1,500 unique markets):
- First run: ~1,500 HTTP calls × ~0.5s = ~12 min wall time
- Second run on same wallet: 0 HTTP calls
- Subsequent runs on OTHER wallets touching the same markets: 0 HTTP calls for shared markets
- Once cache is warm across the population, H6/FH6 are effectively free

### Phase C (follow-up) — REST endpoint cache to SQLite (lower priority)

Currently `_request_json` caches data-API responses in-process for
15-30 seconds. Moving this to a SQLite-backed cache with per-endpoint
TTL would let multi-wallet cohort passes share recent fetches. Lower
priority because the speedup is small (TTLs are short and most calls
are wallet-specific).

### Phase D (follow-up) — Findings #8, #9, #11

- Finding #8: subgraph fallback trigger broaden (fire on coverage gap
  signals, not exact-cap)
- Finding #9: capital ledger on by default
- Finding #11: proxy wallet mismatch — drop the row, don't pass it through

---

## Hard scope guards

The implementation must NOT:

- Change any hypothesis-test mathematics (H1-H6, FH1-FH6, time-split,
  BH-FDR, bootstrap CIs). Phase A returns more data; verdicts may
  change because they see the full population — that's correctness,
  not regression.
- Mutate `gate_result` or `primary_edge_type` logic. Changes are
  bounded to `overall_verdict` (only via the existing honesty floors,
  now using realized+combined as the gate per A.5) and the new
  `data_quality_flags` / `applicable_limitations` entries.
- Change persistence semantics for `analyzer_runs`, `paper_trades`, or
  any production-DB writer.
- Emit copy / trigger / simulator / forward-prediction strings.
- Touch the analyzer pipeline's branch logic (held / flipper / mode
  router). These layers receive richer input via the new fetch but
  their semantics are unchanged.
- Add wall-clock caps that silently truncate runs. Retries are
  unbounded with backoff; the only natural termination signals are
  empty-page, partial-page, and 4xx-after-retries.

### Scope DOES include (new fields on ProtocolReport)

Per operator decision, schema additions are accepted:
- `realized_pnl: float` (alias for current `net_pnl` for backward compat)
- `unrealized_pnl: float`
- `combined_pnl: float`
- `realized_roi: float | None`
- `unrealized_roi: float | None`
- `combined_roi: float | None`
- `open_position_breakdown: dict` (see A.4 shape)
- `time_coverage: dict` (see A.7 shape)

`net_pnl` remains exactly `realized_pnl`. No reader breaks.

---

## Migration / rollout strategy

**Order:** A → B → C → D. Each phase is independently shippable and
reversible.

### Ship Phase A first (smallest unit of real correctness)

PR scope:
- `polymarket_api.py:fetch_user_positions` — remove `max_pages`,
  add termination-reason tracking
- `wallet_protocol/fetch.py` — set `closed_positions_cap_hit` only on
  `api_refused`, never on natural end
- `wallet_protocol/types.py` — if needed, surface `closed_positions_cap_hit`
  on `ProtocolReport` (preferred: piggyback on existing
  `data_quality_flags` list with a new prefix, no schema change)
- Tests: new fixture + unit test for paginate-until-empty + 4xx-stops
- Manual smoke: re-run Pestle, confirm `n_closed_positions=7848`

**Expected analyzer-output diff for Pestle after Phase A:**
- `n_closed_positions`: 5000 → 7848
- `net_pnl`: changes by whatever the 2,848 additional positions sum to
- H1/H2/H3 verdicts: may flip in either direction; report on first run
- Tag-grain `politics/nav` for SwissMiss-like cases: may flip
- Honesty floors continue to fire correctly (they operate on
  whatever the analyzer outputs)

### Phase B can ship 1-2 weeks later

Bigger surface; needs persistent cache, schema, migration, and a
batch-fetch retrofit of the H6/FH6 call sites. Worth a clean spec
of its own once Phase A is in.

### Phase C ships alongside A or B (it's small)

Per-page / per-market emitter additions to existing JsonlEmitter
contract. Cosmetic but high operator value during long runs.

### Phase D is opportunistic

If cohort runs benefit from shared REST cache across wallets, do it.
Otherwise defer.

---

## Acceptance criteria (Phase A — for /ship)

**Pagination + sort (A.1):**
1. `fetch_user_positions(endpoint="closed-positions")` paginates until
   empty / partial / 4xx. No `max_pages` parameter survives. Returns
   `(rows, FetchTermination)`.
2. `closed-positions` uses `sortDirection=DESC` (newest first).
3. `fetch_user_positions(endpoint="positions")` likewise paginates to
   exhaustion.
4. Live smoke: Pestle (`0x241f...ef50`) reports `n_closed_positions >= 7848`
   AND `time_coverage.last_position_ts` reflects today's date.
5. Live smoke: a low-volume wallet (≤100 closed) returns the same count
   as before, no regression.

**Activity walking (A.2):**
6. Default analyzer path runs with `walk_full_history=True`. The
   `--bounded-history` opt-out flag exists and reverts to the old
   3K-window behavior when set.
7. `max_total_rows` cap removed from `fetch_user_activity`.
8. Pestle live smoke: `activity_rows_fetched` >> 3000 (likely 15K-30K).
9. `position_observation_window_coverage` on Pestle ≥ 0.50 (up from 0.01).

**HTTP layer (A.3):**
10. `_request_json` is rebuilt on `httpx.Client` with
    `httpx.Timeout(connect=10, read=20, write=10, pool=5)`.
11. No call hangs beyond the configured per-stage timeout. Probe-style
    test: simulate a slow-drip server response; assert the call
    terminates in ≤ 25 seconds.
12. Session reset hook fires after 3 consecutive timeouts on the same
    client.
13. Retry budget is unbounded. Per-attempt sleep is capped at 60s.

**PnL composition (A.4):**
14. `ProtocolReport.realized_pnl == ProtocolReport.net_pnl` (backward compat).
15. `ProtocolReport.unrealized_pnl` populated when `open_positions` non-empty.
16. `ProtocolReport.combined_pnl == realized_pnl + unrealized_pnl`.
17. `ProtocolReport.open_position_breakdown` carries the 8 fields per A.4
    (n_total, n_dead, n_in_money, n_underwater, capital cuts, unrealized
    cuts, time_in_position).

**Honesty floor (A.5):**
18. Floor downgrades ONLY when `realized_pnl <= 0 AND combined_pnl <= 0`
    AND no coverage. Wallets with `realized < 0` but `combined > 0` get
    a new `EDGE_DEPENDS_ON_OPEN_POSITIONS:` flag instead of downgrade.
19. Existing 34 honesty-floor tests still pass; new tests cover the
    realized-negative-but-combined-positive case.

**Observability (A.6):**
20. Each successful page emits a `fetch_progress` event via JsonlEmitter
    when `--run-log-dir` is set.
21. Each `walk_full_history` window emits `fetch_window_complete`.
22. Termination emits `fetch_terminated`.

**Coverage metadata (A.7):**
23. Every report includes `time_coverage` with the 7 fields per A.7.
24. `fetch_termination` per endpoint is populated.

**Doctrine:**
25. No copy/trigger/simulator/forward-prediction strings introduced.
26. `gate_result` and `primary_edge_type` logic unchanged.
27. Full pytest suite passes. Hypothesis test values may change because
    the input population is now complete — that's correctness.

**Calibration v3 (bundled in Phase A rollout):**
28. v2 fixture-replay tests pass on the merged code (deterministic
    replay is preserved through the migration).
29. Within 24h post-merge: each of the 10 v2 wallets is re-recorded
    against the new fetcher. New fixtures land at
    `tests/calibration/fixtures/<wallet>_2026-05-12.json`.
30. `tests/calibration/labels.yaml` updated to `calibration_version: "3"`.
    `expected` values regenerated. `operator_prior` preserved. Evidence
    source paths point at v3 fixtures.
31. v2 fixture files deleted from `tests/calibration/fixtures/`.
32. Calibration runbook + audit doc updated with v3 baseline citation.

---

## Risk surface

### Correctness risks (good)

- **All prior analyzer outputs on high-volume wallets are partial.**
  Re-running the full cohort post-Phase-A will produce different
  numbers. This is the rebuild's whole point — but worth flagging that
  prior calibration baselines (v2 fixtures) used the truncated data
  and will need re-baselining. Calibration v2 → v3.

### Operational risks (manageable)

- **Run time grows with wallet size.** Pestle was 72s for 5K; ~115s for
  7.8K. Other wallets may be 30s, 200s, or 500s. No hard ceiling. The
  user already accepted "let it run instead of cutting it short"; this
  is the cost.
- **Rate limit pressure.** At 8 RPS, a 10K-position wallet is ~21 min
  for closed-positions alone. If we run cohorts of 10-50 wallets, we
  need to budget time or raise RPS. `PM_REQUEST_RPS_LIMIT` env var
  already exists; tunable per environment.

### Phase-B risks (deferred)

- **Active-market cache invalidation.** Markets that are still trading
  produce new bars. The TTL strategy (24h refetch for active markets)
  is a design call we can iterate after Phase B ships.
- **Cache file size.** 1M markets × 100 bars/market × ~100 bytes/bar =
  10 GB. Realistic Polymarket universe is much smaller; estimate ~50K
  markets ever × 200 bars/market = 1 GB. Acceptable. SQLite handles it.

---

## Out of scope (not in this rebuild)

- **Tag table** (`market_tags`). G4 from the audit. Separate work.
- **Order book depth / slippage model**. SPA data-gap on
  `liquidity_opportunist_needs_data_v1`. Separate work.
- **Mutating the verdict-resolution logic** based on cap flags. The
  honesty floors already exist; this rebuild just makes them operate
  on complete data. If a new "INCOMPLETE_DATA_DUE_TO_CAP" verdict-level
  floor is wanted, that's a follow-up.
- **Hypothesis-test rate-of-convergence improvements.** Bigger N from
  Phase A may shift CIs; that's expected, not a redesign trigger.
- **Re-baselining calibration v2.** Once Phase A ships, the v2 fixtures
  capture truncated wallet state. Re-recording them to v3 is a separate
  follow-up; the fixture-replay tests stay green because they test the
  protocol's determinism on captured input, not the input's completeness.

---

## Operator decisions locked into this spec

1. **Sort DESC, no client caps.** Both confirmed.
2. **Default `walk_full_history=True`.** No upper bound on row count.
3. **PnL composition.** Multiple fields; floor uses BOTH realized and
   combined signals.
4. **Open-position breakdown cuts.** ROI+dead split, in_money/underwater
   split, time-in-position distribution. NO top-3 (rejected as point-snapshot noise).
5. **No wall-clock caps anywhere.** Retries unbounded with backoff.
6. **HTTP layer:** `httpx` replacement for proper total-request timeouts.
7. **Schema additions accepted on `ProtocolReport`** (realized_pnl,
   unrealized_pnl, combined_pnl, ROIs, open_position_breakdown,
   time_coverage). `net_pnl` retained for backward compat.

## What I'd want operator review on before /ship

All operator decisions locked. No remaining review items before /ship.

### `httpx` migration scope (locked: hot paths only)

Phase A migrates the analyzer-critical external-API hot paths:

- **Polymarket data API** (`gamma-api.polymarket.com`, `data-api.polymarket.com`):
  closed-positions, positions, activity, user-value endpoints.
- **Polymarket CLOB** (`clob.polymarket.com`): prices-history, market data.
- **Goldsky subgraph** (Polymarket activity subgraph): OrderFilled events,
  capital-ledger queries.

Phase A does NOT migrate:
- Telegram bot HTTP (rare, short responses, separate concern)
- News scrapers and ancillary agent paths
- Polygon RPC calls (low-volume)

**Policy codified in CLAUDE.md:** future external-API hot paths use
`httpx`, not `requests`. Prevents drift back to silent-hang behavior.

### Calibration v3 re-baseline — bundled in Phase A rollout

No "v2 stays indefinitely" caveat. Phase A's PR merges, then immediately:

1. Re-run the new fetcher against each of the 10 calibration v2 wallets
   (`tests/calibration/labels.yaml`).
2. Check in the new fixture JSONs as `tests/calibration/fixtures/<wallet>_2026-05-12.json`.
3. Update `tests/calibration/labels.yaml`:
   - Replace `expected` values with new deterministic protocol outputs.
   - Preserve `operator_prior` field (W6.3 reputational label) unchanged.
   - Update `evidence` source paths to v3 fixtures.
   - Bump `calibration_version: "3"`.
4. Delete `tests/calibration/fixtures/<wallet>_2026-04-30.json` v2 files.
5. Update calibration runbook + audit docs with the v3 baseline.

This is operational post-merge work, not extra code in the PR. But it's
**not optional** — Phase A is incomplete until v3 lands. Spec marks v3
as part of Phase A's definition-of-done.
   flag if you prefer the explicit field.
3. **Default `--full-history` to True?** Currently it's opt-in via
   flag. Phase A doesn't touch this — but if we're rebuilding for
   honest data, default-on is the consistent move. Worth a separate
   call.
4. **Calibration v2 re-baseline plan.** Once Phase A ships, v2 fixtures
   become "snapshot of truncated state." Either re-record to v3 or
   document that v2 baselines pre-date the cap removal.
