# Memecoin Copy Mirror — Native UI Integration + Improvements

**Date:** 2026-07-05
**Status:** Spec for a build agent (hand-off)
**Depends on:** `docs/superpowers/specs/2026-07-05-memecoin-copy-mirror-design.md` (v0, SHIPPED). Backend + a standalone `/memecoin` page already exist and run live.

---

## Context (what exists today)

The memecoin copy-mirror v0 is built and running:
- **Backend** (all live): `sniper/routes/api_memecoin.py` blueprint (registered in `sniper/web/server.py`), `db_memecoin`, copy engine, DexScreener marks, `scripts/memecoin_runner_loop.py` (kept alive by `scripts/memecoin_watchdog.sh` cron).
- **UI today:** a **standalone self-contained page** served at `GET /memecoin` (inline HTML/JS inside `api_memecoin.py`, constant `_PAGE`). It is NOT part of the main dashboard.
- **Existing endpoints:** `GET/POST/DELETE /api/memecoin/wallets`, `POST /api/memecoin/wallets/<uid>/pause`, `GET /api/memecoin/positions/open` (`{positions, marks}`), `GET /api/memecoin/positions/closed` (`{positions, stats}`), `POST /api/memecoin/positions/<pid>/close`.

## Goal

1. **Part A — Integrate** the memecoin board as a **native tab** in the main Sniper dashboard (under a new "Solana" venue), matching existing tab architecture. Retire the standalone `_PAGE`.
2. **Part B — Improve** the board so the operator can (a) build the curated wallet list **by fomo handle** (not raw userId), (b) see whether the system is actually entering/copying correctly, and (c) judge per-trader copy quality.

Keep it read-mostly + paper. No live execution. Reuse existing shared UI helpers/classes — do not introduce a new framework or CSS system.

---

## Part A — Native tab integration

The dashboard groups flat tabs under **venues**. Router lives in `static/app.js`; markup in `templates/index.html`. Tabs are DOM sections filtered by venue.

### A1. `templates/index.html`
- Add a venue item next to the others (after `data-venue="hyperliquid"`, line ~29):
  ```html
  <a href="#" class="venue-item" data-venue="solana"><span class="venue-label">Solana</span></a>
  ```
- Add a nav item in `#section-nav` (with the other `.nav-item`s, ~line 61):
  ```html
  <a href="#" class="nav-item" data-tab="memecoin" data-venue="solana"><span class="nav-label">Memecoin Copy</span></a>
  ```
- Add the panel section (sibling of the other `<section id="tab-*" class="tab-panel">`):
  ```html
  <section id="tab-memecoin" class="tab-panel">
    <div class="panel-header">
      <h1 class="panel-title">Memecoin Copy <span class="pill">paper</span></h1>
      <p class="panel-subtitle">Mirrors curated fomo traders' large entries into a paper ledger — honest slippage-included fills. Front-runs the notification herd (post-fill), large-trades-only. Forward-test, not advice.</p>
    </div>
    <div id="memecoin-board"></div>
  </section>
  ```

### A2. `static/app.js`
- Add to `VALID_TABS` (line 10): `'memecoin'`.
- Add to `TAB_VENUE` (line ~17): `memecoin: 'solana',`.
- Add to `VENUE_HOME` (line ~22): `solana: 'memecoin',`.
- Create a `memecoin` controller object **mirroring `liveSignals` (app.js:1021)** — same shape: private `_timer`, `load()`, row renderers, `_act()` for mutations. It MUST:
  - Render into `document.getElementById('memecoin-board')`.
  - Fetch via the shared `fetchJson(...)` helper; escape with `esc(...)`; format with `fmt.usd/fmt.pct/fmt.addr`; notify with `toast(...)`.
  - Use existing classes: `card`, `table-wrapper`, `data-table compact`, `btn btn-sm`, `btn-danger`, `text-muted`, `num`, `pill`, `analyzer-input`.
  - Self-clearing poll (copy the pattern at app.js:1098-1101): `setInterval` that calls `load()` only while `state.route.tab === 'memecoin'`, else clears itself. Use **4000ms** (positions move fast).
- Wire activation: find where tabs load on activation (the router calls `applyVenueChrome(tabId)` at app.js:5754 and dispatches per-tab loads nearby — follow how `signals` triggers `liveSignals.load()` at app.js:1974). Add the equivalent `case 'memecoin': return memecoin.load();`.

### A3. Retire the standalone page
- Remove the `_PAGE` constant and the `GET /memecoin` route from `api_memecoin.py` **after** the tab works (keep it until then for parity). Keep all `/api/memecoin/*` JSON endpoints.

**Acceptance:** clicking Solana → Memecoin Copy shows the board inside the dashboard chrome; it polls every 4s; leaving the tab stops polling; no standalone page needed.

---

## Part B — Improvements (prioritized)

### P0 — Add wallets by handle (the operator's main workflow)
Today `POST /api/memecoin/wallets` needs a raw fomo `userId`. The operator wants to type a **handle**.
- **Backend:** add `GET /api/memecoin/resolve/<handle>` → calls fomo `GET /v2/users/userHandle/<handle>` (use `fomo_feed.fomo_get` with a shared `FomoAuth`) and returns `{source_user_id, handle, display_name, found: bool}`. Reuse the runner's auth/token store so it stays authed.
- **UI:** the "add" input takes a handle; on submit, resolve → confirm the display name → POST the wallet. Show a clear error if not found.
- **Also:** show each wallet's live copy stats inline (positions open, realized PnL, win rate) pulled from a new `GET /api/memecoin/wallets/stats` (join wallets × positions). This is how the operator decides who to keep.

### P0 — Is the system actually entering/copying? (health + activity)
The operator explicitly wants to verify entries fire. Add:
- **Health endpoint** `GET /api/memecoin/health` → `{runner_alive, last_event_at, last_event_age_s, events_last_hour, open_count, curated_count, feed_lag_median_s, auth_ok}`. `runner_alive` = a heartbeat: have the runner loop write `data/memecoin_runner.heartbeat` (mtime) each cycle; health reads its age. `auth_ok` = last feed poll status.
- **UI status strip** at the top of the board: green/amber/red dot for runner alive, "last entry N s ago", events/hr, feed lag. This is the at-a-glance "is it working".
- **Recent activity feed** `GET /api/memecoin/events/recent?limit=50` (from `memecoin_events`) → show the last N feed hits for curated wallets with outcome (opened / skipped-dup / mirror-exit / ignored). So the operator sees copies happening in real time. (Engine must record the outcome per event — add an `action` column to `memecoin_events` or a small `memecoin_actions` log; nullable, backfill-safe.)

### P1 — Honest-fill quality + richer position rows
- Surface `entry_fill_source` per position with a badge: **jupiter** (best), **dexscreener** (ok), **fomo** (⚠ fallback — no live quote, treat PnL as soft). The operator must know when a fill is unreliable.
- Add columns: **% PnL** (not just $), **entry age**, **liquidity at entry**, **is_first_buy** flag, **chain badge** (SOL / EVM from mint prefix). `their_size_usd` already present (conviction).
- Sort open positions by uPnL; let the operator filter by trader.

### P1 — Manual actions parity
The v0 spec promised manual buy / partial-sell; only manual close exists.
- **Backend:** `POST /api/memecoin/positions/<pid>/sell` `{fraction}` (partial mirror-exit at a DexScreener fill, proportional realized PnL, leaves the rest open — needs the engine to support partial closes: split into `reduce_position` + a `partial_exit` fill kind). `POST /api/memecoin/manual-buy` `{source_user_id, mint, ticker}` (opens a paper position at a live fill, kind `manual_buy`).
- **UI:** per-row sell-25/50/100% + a manual-buy control.

### P2 — Per-trader performance panel
A table: trader → copied entries, closed PnL, win rate, avg entry lag, median hold. This is the payoff — it tells the operator which curated wallets are actually +EV to copy (the whole point of the study). Back it with `GET /api/memecoin/traders/performance`.

### P2 — Sizing + config visibility
Show the current `MEMECOIN_COPY_SIZE_USD` and poll interval; note fills are paper + slippage-included. A read-only settings line (no live editing needed for v0).

---

## Backend additions summary (new endpoints)

| Endpoint | Returns / does |
|---|---|
| `GET /api/memecoin/resolve/<handle>` | handle → `{source_user_id, display_name, found}` via fomo `/v2/users/userHandle/<handle>` |
| `GET /api/memecoin/wallets/stats` | wallets × positions rollup (open, realized, win rate per wallet) |
| `GET /api/memecoin/health` | runner heartbeat, last-event age, events/hr, feed lag, auth_ok |
| `GET /api/memecoin/events/recent?limit=` | recent `memecoin_events` + per-event action outcome |
| `GET /api/memecoin/traders/performance` | per-trader closed-PnL / WR / lag / hold |
| `POST /api/memecoin/positions/<pid>/sell` | partial mirror-exit `{fraction}` |
| `POST /api/memecoin/manual-buy` | manual paper entry `{source_user_id, mint, ticker}` |

Engine/db changes: (a) record per-event action (new `memecoin_events.action` column or `memecoin_actions` table); (b) partial-exit support (`reduce_position` + `partial_exit` fill kind, keep position open with reduced qty); (c) runner writes a heartbeat file each cycle. All schema changes idempotent; run `scripts/brain_index.py generate` + `scripts/validate_schema_refs.py`.

---

## Testing

- Follow the existing `tests/test_memecoin_*.py` style (stdlib sqlite `:memory:`, Flask `test_client`, injected `http`/`fill_fn`).
- New tests: resolve endpoint (mock fomo `http`), health shape, recent-events shape, wallets/stats rollup math, partial-sell PnL math (sell 50% realizes half, leaves half open), manual-buy opens a position, traders/performance rollup.
- Front-end: no framework; keep JS testable-by-inspection. Verify the tab registers (nav → section shows) and polling self-clears off-tab.
- Run `python3 -m pytest tests/test_memecoin_*.py -q` and `python3 -c "import sniper.web.server"` (import smoke) before hand-off.

## Out of scope (v0 UI)

Live execution / `armed=true`; editing sizing from the UI; small-trade coverage (feed is large-only); external-wallet (`isOffPlatform:true`) direct-chain copying; charting beyond the embedded DexScreener iframe.

## Notes for the build agent

- Auth/token gotchas are documented in the memory finding `research-fomo-family-ws-feasibility.md` and the v0 spec: fomo API + auth.privy.io need browser headers; the app-scoped `token` field is the bearer; refresh rotates → persisted to `.fomo_tokens.json`; DexScreener/Jupiter need a browser UA; feed is multichain. Reuse `sniper/feeds/fomo_feed.FomoAuth` and `sniper/execution/solana_quote` — don't re-implement.
- The DexScreener chart embed URL is `https://dexscreener.com/solana/<mint>?embed=1&theme=dark` for SOL; for EVM tokens use the correct chain slug (e.g. `/base/<mint>`), derivable from the mint prefix + DexScreener `chainId` in the marks response.
- Keep the honest framing in the UI copy: post-fill, herd-front-running, large-only, forward-test — do not imply guaranteed profit.
