# Memecoin Copy Mirror v0 — Design

**Date:** 2026-07-05
**Status:** Draft for review (brainstorming → spec)
**Author:** operator + Claude
**Scope:** NEW subsystem. Solana memecoin copy-mirroring driven by the fomo.family feed. Separate from the Polymarket/Hyperliquid stack; shares nothing with existing ledgers. Paper-first per repo doctrine (validate before real money).

---

## 1. Goal

Mirror the entries of a curated set of fomo.family traders into a **paper ledger**, visualize each position + token stats/graph in a new Sniper dashboard tab, and measure whether instant-copying these traders is +EV **before any real money**.

**v0 success metric:** per-trader copy PnL on **honest, slippage-included** paper fills, plus the entry-lag distribution. The question v0 answers: *is front-running the fomo notification herd on these traders +EV after memecoin slippage?*

**Explicit non-goals for v0:** real trade execution, take-profit/stop-loss algorithms beyond mirroring source exits, copying non-fomo external wallets, catching sub-threshold small entries (see §3).

---

## 2. Research findings that shape this design

Full detail: `~/.claude/.../memory/research-fomo-family-ws-feasibility.md` (live-inspected + measured 2026-07-05).

1. **fomo has no websocket.** The feed is REST polling of `GET https://prod-api.fomo.family/feed`. Latency **measured sub-second** (median 0.34s catch at 500ms polling; feed publish is ~real-time, min 0.02s). Latency is **not** the blocker; our catch latency is bounded by our poll interval, and we beat the mobile push.
2. **fomo executes fomo-users' trades from its own wallets** (`isOffPlatform:false`, `provider:"DFLOW"`). Proven: a fomo trader's swap `address` has **zero on-chain existence** (`getAccountInfo`=null, 0 txs, 0 tokens). ⇒ **Fomo-native traders CANNOT be copied by watching their wallet on-chain.** fomo's API is the only signal for them. (`isOffPlatform:true` = an external wallet fomo merely observed → that one is on-chain; out of v0 scope.)
3. **`/feed` is global + large-trades-only; `/swaps` is token-scoped.** `/feed` carries a threshold-gated stream of large trades across all traders (filter to our curated set by `userHandle`/`userId`). `/v2/users/{id}/swaps` requires `?tokenAddress=`, so it can't discover a trader's entry into an *unknown* token. ⇒ **`/feed` is the only real-time discovery surface, and it only carries large trades.** Small sub-threshold entries are invisible to the web API (mobile-push only). v0 accepts large-only coverage.
4. **Auth = Privy access-token JWT** (`Authorization: Bearer`), ~1h expiry, refreshed via `POST auth.privy.io/api/v1/sessions` with `{refresh_token}` + headers `privy-app-id`/`privy-client`/`privy-client-id`/`privy-ca-id`; refresh **rotates** the refresh token. fomo's edge **403s** requests lacking browser headers (`origin`, `referer`, `user-agent`, `x-supported-chains`) — a WAF. Both handled by `scripts/fomo_latency_probe.py` (the working reference client).
5. **The edge is adversarial, not alpha:** copying fomo-native traders = front-running the fomo notification herd (post-fill). This is what the paper study must price against slippage.

---

## 3. Architecture

```
fomo /feed (Privy-authed poll, browser headers, ~250-500ms)
      │  normalize + filter-to-curated + dedup
      ▼
 CopyEvent  ──►  Copy engine  ──►  Paper ledger (SQLite)
                    │                    ▲
                    │ honest fill        │ marks
                    ▼                    │
             Jupiter quote        DexScreener poller
                    │                    │
                    ▼                    ▼
             Dry-run executor      UI tab (Flask blueprint + dashboard)
             (armed=false)
```

The **feed is an isolated adapter** behind a single interface (`poll() -> [CopyEvent]`), so a Solana Tracker / Helius source can replace or supplement fomo later (e.g. for `isOffPlatform:true` external wallets). Everything downstream is feed-agnostic.

### Component map (proposed paths)

| Component | Path | Purpose |
|---|---|---|
| Feed adapter | `sniper/feeds/fomo_feed.py` | Privy-authed `/feed` poller → normalized `CopyEvent`; dedup; refresh loop; filter to curated |
| Copy engine | `sniper/services/memecoin_copy_trader.py` | Entry/exit logic, honest fills, position lifecycle |
| Dry-run executor | `sniper/execution/solana_swap_executor.py` | Builds the Jupiter swap tx behind `armed=false`; logs, never signs |
| Marks poller | part of the copy engine service or `sniper/feeds/dexscreener.py` | DexScreener price/mcap/liq/vol on open mints |
| Persistence | `db.py` layer → `sniper/persistence/db_memecoin.py` | 4 new tables (§6) |
| UI blueprint | `sniper/routes/api_memecoin.py` | CRUD + board + manual actions |
| UI tab | `sniper/web/` static + template | New dashboard tab |

---

## 4. Feed adapter (`fomo_feed.py`)

- **Auth:** reuse the proven flow. Refresh token + optional access token from env / a gitignored store (never committed; `.fomo.env` pattern). On startup and on 401/403, `POST auth.privy.io/api/v1/sessions` → fresh access JWT + rotated refresh token (persist the rotation). Send browser headers on every fomo call (`origin`, `referer`, `user-agent`, `x-supported-chains`) or the WAF 403s.
- **Poll:** `GET /feed?limit=50&feedTypes=large_buy&feedTypes=large_sell&...` at a configurable interval (default 400–500ms; measured sub-second catch). Read the response `Date` header as the **skew-free clock** (this box's local clock is +7h off — never trust it for lag).
- **Normalize** each event to:
  ```
  CopyEvent {
    source_user_id, source_handle,
    mint, ticker, side (buy/sell),
    their_price, their_market_cap, their_size_usd, is_first_buy,
    fomo_event_id, created_at (fomo), received_at (our poll's server Date), lag_s
  }
  ```
- **Filter** to events whose `source_user_id`/`source_handle` ∈ the curated set.
- **Dedup** by `fomo_event_id`.
- **Fail-closed:** unknown shapes logged + dropped; never fabricate a fill on a malformed event.
- **Scope note in code:** `/feed` is large-only; document that small entries are not observable here.

---

## 5. Copy engine (`memecoin_copy_trader.py`)

- **Entry:** a curated trader's `large_buy` → open a paper position sized **fixed $N** (from `web_settings.py` / `.env`), storing `their_size_usd` as a conviction flag.
- **Honest fill:** at signal time, request a live **Jupiter quote** for $N of the mint → real fill price incl. slippage/price-impact (memecoin mids lie). Fallbacks, each flagged: DexScreener price → the fomo event `their_price` (last resort). Record which source was used.
- **Position key:** `(source_user_id, mint)` — two traders in the same token = two attributable positions.
- **Exit — mirror proportionally:** track the source's position from observed buys; when they sell X% of it, close X% of ours (Jupiter quote for the exit). If their prior holdings are unknown (they bought before we followed / below threshold), the first observed sell = full close. Manual UI override always available (buy / partial-sell / close).
- **Dry-run executor:** build the real Jupiter swap tx behind `armed=false`; log the would-be transaction; never sign. Going live later = config flip + key setup (a separate, gated step).
- **Latency accounting:** persist `lag_s` per fill (skew-free) — this is the v0 evidence.

---

## 6. Persistence (4 new SQLite tables via the db.py layer)

New tables (not yet in the registry — proposed):

<!-- schema:external=memecoin_wallets -->
<!-- schema:external=memecoin_events -->
<!-- schema:external=memecoin_positions -->
<!-- schema:external=memecoin_fills -->

```sql
-- curated trader list (fomo-native identity; these traders have no copyable on-chain wallet)
CREATE TABLE IF NOT EXISTS memecoin_wallets (
  id INTEGER PRIMARY KEY,
  source_user_id TEXT UNIQUE,     -- fomo userId
  handle TEXT,
  label TEXT,
  paused INTEGER DEFAULT 0,
  added_at TEXT
);

-- append-only raw feed observations (audit + research)
CREATE TABLE IF NOT EXISTS memecoin_events (
  id INTEGER PRIMARY KEY,
  fomo_event_id TEXT UNIQUE,
  source_user_id TEXT, source_handle TEXT,
  mint TEXT, ticker TEXT, side TEXT,
  their_price REAL, their_market_cap REAL, their_size_usd REAL, is_first_buy INTEGER,
  created_at TEXT,          -- fomo timestamp
  received_at TEXT,         -- our skew-free receipt (server Date)
  lag_s REAL
);

-- paper positions
CREATE TABLE IF NOT EXISTS memecoin_positions (
  id INTEGER PRIMARY KEY,
  source_user_id TEXT, source_handle TEXT,
  mint TEXT, ticker TEXT,
  entry_price REAL, entry_notional REAL, entry_fill_source TEXT,  -- jupiter|dexscreener|fomo
  entry_lag_s REAL, their_size_usd REAL,
  entry_liquidity REAL, entry_token_age_s REAL,   -- snapshot for "what works" analysis
  is_open INTEGER DEFAULT 1,
  opened_at TEXT, closed_at TEXT,
  realized_pnl REAL
);

-- fills incl. manual UI actions
CREATE TABLE IF NOT EXISTS memecoin_fills (
  id INTEGER PRIMARY KEY,
  position_id INTEGER,
  kind TEXT,               -- entry|mirror_exit|manual_buy|manual_sell|manual_close
  price REAL, qty REAL, notional REAL, fill_source TEXT,
  fraction REAL,           -- for proportional exits
  created_at TEXT
);
```

Run `python3 scripts/brain_index.py generate` after creating the tables so CI passes.

---

## 7. UI (new dashboard tab)

Blueprint `sniper/routes/api_memecoin.py`:
- **Wallet CRUD** — add/pause/remove curated traders by fomo handle/userId, with label.
- **Open board** — token, source trader, their size vs our $N, entry vs current price, unrealized PnL, position age, liquidity, **entry lag**.
- **Expand a position** — embedded DexScreener chart iframe + full token stats (price/mcap/liq/vol/age).
- **Manual actions** — paper buy / partial-sell / close.
- **Closed table + stats** — win rate, total PnL, **PnL per trader**, **lag distribution**.

Follows the existing Signals-tab pattern (Flask blueprint + a tab in the dashboard).

---

## 8. Ops, testing, risks

- **Ops:** no always-on service; a watchdog cron keeps the copy engine alive (same pattern as the crypto5m paper engines). The Privy refresh loop runs inside the adapter.
- **Testing:** fixture-`CopyEvent` tests for normalization, dedup, entry/exit lifecycle, proportional-exit math, honest-fill fallback ordering, lag calc (skew-free). Endpoint tests for the blueprint. Reuse a real captured `/feed` payload as a fixture.
- **Honest risks (carry into the paper study, don't bury):**
  1. **Adversarial edge** — copying fomo-native traders front-runs the notification herd; post-fill; +EV only if the herd's follow-on flow moves price more than our slippage. Unproven — that's the point of v0.
  2. **Large-only coverage** — sub-threshold entries are invisible; we systematically miss a trader's small adds.
  3. **ToS/fragility** — fomo API is unofficial; ToS bans automated access; the WAF + Privy flow can change on any fomo update. The adapter abstraction is the mitigation; a Solana Tracker/Helius source is the fallback for `isOffPlatform:true` external wallets.
  4. **Slippage realism** — Jupiter quotes make paper fills honest, but real fills also face MEV/priority-fee dynamics not modeled in paper.

---

## 9. Open questions / future (out of v0)

- Confirm whether `/swaps` is meaningfully earlier than `/feed` (this run didn't corroborate the one-sample ~200ms lead; `/swaps` is token-scoped so probably not usable for discovery).
- Coverage study: what fraction of a curated trader's entries clear the `/feed` large threshold.
- `isOffPlatform:true` external-wallet copying via direct-chain (Helius/Solana Tracker) — a separate, ToS-safe, faster path for non-fomo wallets.
- Live executor (`armed=true`): key management, priority fees, MEV, gated behind operator approval and a proven paper edge.
