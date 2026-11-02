# Memecoin Operator Cockpit — Design Spec

**Date:** 2026-07-16
**Scope:** Memecoin dashboard surface only (pilot). Establishes reusable design tokens + UI-state
conventions that later Sniper tabs can adopt, but this spec redesigns **only** the memecoin subtabs.
**Status:** Design — awaiting operator review before an implementation plan is written.

---

## 1. Problem

The memecoin surface is the only funded, real-money surface in Sniper, and it is the least legible.
Audit (2026-07-16, browser + code) found:

- **Real open positions are unreadable.** An 18-column table (`Venue · Chain · Token · Trader · Size ·
  Value · Entry · Now · PnL · Peak · MCap · Liq · Vol · Hold · Flags · TP · SL · ⋯`) rendered at
  `static/app.js:3043`. On desktop the price sub-values overlap; on mobile the table clips. The most
  operationally important facts (copy latency, fill drift, quote age, who else still holds) are hidden
  in `title=` hover text a phone can't show.
- **Three conflicting color systems.** Leftover Matrix-green (`#3fb950`-family) dominates as a base UI
  color rather than a semantic "gain" color, competing with an ad-hoc status palette and inline hex
  literals scattered through `app.js`. Gain/loss/warning have no consistent token.
- **11 peer-level subtabs, no hierarchy** (`Real-Money · Positions · Discovery · Traders · Theses ·
  Activity · Decisions · Radar · X Sources · Performance · Closed`). Live money ops, research, and
  closed history all compete at the same level.
- **Duplicate status.** Live arm state and accounting state are shown twice and can visibly disagree.
- **Paper-arm rows duplicate data** instead of explaining where `control` / `real_shadow` / `scalp_c2`
  diverged on the same coin.
- **`static/app.js` + `static/styles.css` mix layout, styling, data-fetching, and rendering** in one
  monolith, so every change is high-blast-radius.

Every element the operator needs already has a backing read-only endpoint (see §7 data contracts).
This is a **presentation** problem, not a data problem.

## 2. Goals / Non-Goals

**Goals**
- A **Real Position Cockpit**: one readable card per real position answering, without hover, "what do
  we hold, what's it worth, who did we copy, how late/off were we, who else still holds, and what will
  make us exit."
- A **Paper Strategy Lab**: one card per coin episode comparing `control` / `real_shadow` / `scalp_c2`
  side-by-side and showing where their outcomes diverged.
- A **Wallet × Arm** performance view: compares arms and exposes wallet/coin overlap and sample size,
  not just a wallet ranking.
- A restrained, **tokenized visual system** (navy/slate surfaces + semantic gain/loss/warn/neutral)
  replacing the three ad-hoc color systems and inline hex.
- A **4-workspace information architecture** replacing the 11 flat subtabs.
- Radar (already read-only, shipped) **restyled** to the new tokens — not rebuilt.
- Mobile-legible: no clipped tables, reachable navigation.

**Non-Goals**
- No change to the funded entry/exit path, sizing, rails, or any money-moving logic.
- No new strategy, no scoring change, no arming change.
- No redesign of non-memecoin tabs (Polymarket / Hyperliquid / Intel). Tokens are built to be reusable,
  but adoption elsewhere is out of scope here.
- No new DB table and no new writer in the funded path. New *read-only* projection endpoints are allowed
  only if an element can't be served by an existing one (§7 shows none are required for v1).

## 3. Global Constraints (every task inherits these — copied verbatim into the plan)

1. **Funded path untouched.** No file under the funded entry/exit path is modified: not the runner, not
   `sniper/settings/memecoin.py` rails, not the exit sweep, not order construction. This is a
   `static/` + `sniper/routes/` + `sniper/services/` (read-only) change. Verification step in every PR:
   `git diff --name-only` must not list any funded-runtime file, and the runner heartbeat
   (`data/memecoin_runner.heartbeat`, `auth_ok=true`, age < 30s) is unchanged before/after.
2. **Identity is `(network_id, mint)`.** Never key a card, chart, or lookup on bare mint or bare address.
   Robinhood chain **4663** is rendered as its own explicit chain label, never folded into "EVM".
3. **Read-only display.** The cockpit reuses existing operator-triggered POST routes for actions
   (`/real/positions/<pid>/exit`, `/real/positions/<pid>/mirror`, `/real/buy`, `/real/kill`,
   `/positions/<pid>/sell`). It adds **no** new money-path writer.
4. **Honesty rails.** Unknown ≠ zero. Every value renders one of three states: **known**, **stale**
   (served but past its freshness window — labeled with age), **unknown** (no data — labeled "—", never
   a fabricated 0). Per-holder PnL is **unknown** in v1 (the holder table exposes remaining size + state,
   not entry price) and must render as such, not invented.
5. **No inline hex in new code.** New markup uses CSS custom properties from the token sheet (§4). Green
   is a *gain* token only, never a base UI color.
6. **Poll cadence preserved.** The 4s UI poll stays; the balance/accounting RPC reads stay on their
   ≥60s cache (`app.js:2955`). No cockpit element triggers an RPC on the 4s cadence.

## 4. Design Foundation (tokens)

A single stylesheet `static/css/mc-tokens.css`, loaded before the memecoin renderers. Values are
illustrative; final hex tuned during implementation, but the **token names are the contract**.

**Surfaces** (dark, restrained navy/slate — kills the Matrix-green base):
`--surface-0` page, `--surface-1` card, `--surface-2` raised/inset, `--border-subtle`, `--border-strong`.

**Semantic status** (the only place saturated color is allowed):
`--gain` (green), `--loss` (red), `--warn` (amber), `--info` (blue), `--neutral` (slate).
Each has a `-bg` variant (~12% alpha) for chips/fills.

**Text:** `--text-1` primary, `--text-2` secondary, `--text-3` muted/labels.

**Freshness** (drives the honesty rail visually): `--fresh` (<window), `--stale` (past window, amber),
`--unknown` (muted, dashed). A shared `.freshness[data-state]` class maps these.

**Typography scale:** 4 sizes only — `--fs-lg` (headline metric), `--fs-md` (body), `--fs-sm` (label),
`--fs-xs` (annotation). Numeric values use `font-variant-numeric: tabular-nums`.

**Spacing scale:** `--sp-1..--sp-5` (4/8/12/16/24px). Cards use `--sp-3` internal padding, `--sp-2`
between blocks.

**Accessibility:** gain/loss never encoded by color alone — always paired with a sign (`+`/`−`) or an
arrow glyph, so it survives greyscale/colorblind viewing. Min contrast AA on text tokens.

**Responsive:** cards are the layout primitive (they reflow; tables don't). Breakpoint `--bp-narrow`
(≈720px): multi-column card bodies collapse to single column; the workspace nav becomes a horizontal
scroll strip with the active workspace pinned left.

## 5. Information Architecture

Collapse 11 flat subtabs into **4 workspaces**, each with secondary views. Nothing is deleted — every
current subtab maps into the new tree:

| Workspace | Secondary views | Absorbs today's subtabs |
|---|---|---|
| **Live** (funded ops) | Cockpit (real positions) · Accounting · Venues/Route health | `Real-Money` |
| **Paper** (research A/B) | Strategy Lab (per-coin arms) · Closed | `Positions`, `Closed` |
| **Radar** (live telemetry) | Board · Coin timeline | `Radar`, `Activity`, `Decisions` |
| **Wallets** (who we copy) | Wallet × Arm performance · Discovery · Traders · Theses · X Sources | `Performance`, `Discovery`, `Traders`, `Theses`, `X Sources` |

Default landing = **Live › Cockpit**. Closed history moves out of the live view (audit finding:
closed rows dilute the live cockpit). Workspace switch is one control; secondary view is a sub-nav
within the workspace. This is a nav re-parenting of existing `mc-subtab`/`mc-section` machinery
(`app.js:2523`), not new routing.

## 6. Real Position Cockpit (the core deliverable)

One **card** per real open position, replacing the table at `app.js:3043`. Sort order: needs-action
first (stuck bag / stale quote / orphaned), then worst freshness, then largest deployed size. Layout
(single card, blocks stack on narrow):

```
┌─ HEADER ────────────────────────────────────────────────┐
│ $TICKER   ● Robinhood 4663 / Solana / Base   held 21h    │  ← chain explicit, never "EVM"
│ decision tape: entered 4s after signal · mirror ON       │
├─ VALUE (MTM) ───────────────┬─ PROVENANCE ───────────────┤
│ entry 0.0030 → now 0.0026   │ copied @Wiley  their $50    │
│ value $43 of $50   −$7 −14% │ copy lag 4s · fill +2.1%    │  ← drift = we paid up vs their price
│ peak +11% · giveback 25%    │ quote age 4s (fresh)        │
│ mcap $2.6M · liq n/a · vol —│                             │
├─ MINI PRICE GRAPH ──────────────────────────────────────┤
│ ▁▂▃▅▇▆▅▃  (local 0x marks; "collecting" if empty)        │
├─ HOLDER CONSENSUS ──────────────────────────────────────┤
│ 3 armed wallets in · @Wiley SOLD (full) · @Cap holds $30 │  ← source-sold-but-others-hold, explicit
│ @Mag holds $12 (partial) · evidence 2m ago (fresh)       │  ← remaining size + partial/full + freshness
├─ EXIT CONTROLS ─────────────────────────────────────────┤
│ TP +60%  ·  SL −60%  ·  target mcap $5.5–6.5M  ·  route ✓ │
│ [ Sell 50% ]  [ Sell all ]  [ toggle mirror ]            │  ← confirmation gate on every funded action
└──────────────────────────────────────────────────────────┘
```

**Blocks and their honesty behavior**
- **Header** — `$ticker` (fallback: mint prefix), explicit chain label from `network_id` (4663 →
  "Robinhood 4663"), venue, `held_s` humanized.
- **Decision tape** — entry latency (`entry_lag_s`) + current mirror state (`mirror_disabled`).
- **Value/MTM** — `entry_price` → `last_quote_price` (already net-of-exit-slippage), `entry_notional`
  → derived value, `pnl_pct` ($ and %), peak (`max_gain_pct`/`peak_price`) + giveback (peak − current),
  `market_cap`/`liquidity`/`volume_24h`. Each carries a freshness state from `last_quote_ts`; unknown
  liquidity/volume renders "—", not 0.
- **Provenance** — `source_handle`, `their_size_usd`, `entry_lag_s` (copy latency), `fill_gap_pct`
  (fill drift vs `their_price`), `entry_liquidity`. This is the audit's "exact source wallet, copy
  latency, source/quote/fill drift."
- **Mini price graph** — `/api/memecoin/marks/<chain>/<mint>` (`points[{t,mid,mcap}]`). Empty series →
  "collecting marks" (honest), never an error. Labeled "local 0x marks," not candles. This is the
  **only** price history for 4663 (DexScreener has no 4663 pair).
- **Holder consensus** — from `coin_timeline.holders` (`memecoin_holder_evidence`): each armed/followed
  wallet's `remaining_usd`, `evidence_state`/`confirmation_state`, `observed_at` (freshness),
  `invalidated_at`. Renders: count in, **which source sold vs who still holds**, remaining size,
  **partial** (`remaining_usd` reduced) vs **full exit** (invalidated), evidence age. Per-holder up/down
  PnL is **unknown in v1** and shown as such (not fabricated).
- **Exit controls** — current effective TP/SL/target-mcap (`effective_exit`, `exit_target`,
  `default_tp_pct`/`stop_pct`/`derisk_pct` defaults), mirror toggle, and **manual partial + full sell**.
  Sell-route readiness from `/real/status` venue card fields (RPC set, keypair present, 0x key set →
  "route ✓" / "route not ready"). Every funded action (sell, mirror toggle, kill) goes through a
  **confirmation gate** (§9) — no single-click money moves.

## 7. Data Contracts (no new backend required for v1)

| Cockpit element | Endpoint | Fields |
|---|---|---|
| Real card header/MTM/provenance/exit | `GET /api/memecoin/real/positions` | `network_id, mint, ticker, venue, entry_price, entry_notional, last_quote_price, pnl_pct, market_cap, liquidity, volume_24h, last_quote_ts, source_handle, their_size_usd, entry_lag_s, their_price, fill_gap_pct, entry_liquidity, peak_price, max_gain_pct, held_s, timecap_s, stop_pct, default_tp_pct, derisk_pct, mirror_disabled, effective_exit, exit_target, stuck_bag` |
| Arm/accounting banner + venue route health | `GET /api/memecoin/real/status`, `/real/accounting` | `envelope, daily, reservations, real_open, venues{...balances,route readiness}, knobs, kill_tripped, armed` |
| Mini price graph | `GET /api/memecoin/marks/<chain>/<mint>` | `points[{t,mid,mcap}], last, lo, hi, change_pct` |
| Holder consensus + coin timeline | `GET /api/memecoin/radar/<network_id>/<mint>` (`coin_timeline`) | `holders[{source_handle,evidence_state,remaining_usd,observed_at,confirmation_state,invalidated_at}], signals, decisions, arm_positions, price_path` |
| Paper arms (Strategy Lab) | `coin_timeline.arm_positions` + `GET /api/memecoin/positions/open` `/positions/closed` | `strategy, execution_mode, entry_price, last_quote_price, peak_price, max_gain_pct, realized_pnl, exit_reason, opened_at, closed_at, is_open` |
| Wallet × Arm perf | `GET /api/memecoin/traders/performance` `/traders/combined`, `radar/summary` (`paper_arms`) | per-wallet + per-arm aggregates |
| Actions (reused, operator-triggered) | `POST /real/positions/<pid>/exit`, `/real/positions/<pid>/mirror`, `/real/buy`, `/real/kill`, `/positions/<pid>/sell` | existing |

**One optional read-only add (deferred unless the Lab needs it):** a `GET
/api/memecoin/coin/<network_id>/<mint>/arms` convenience projection that returns the three arms already
present in `coin_timeline.arm_positions`, pre-aligned for side-by-side. Only build it if the client-side
join proves awkward; it is a pure read wrapper, no writer.

## 8. Paper Strategy Lab & Wallet × Arm

**Strategy Lab** — one card per coin episode, three columns (`control` / `real_shadow` / `scalp_c2`)
from `coin_timeline.arm_positions`: entry, current/realized PnL, peak, exit reason, open/closed. A
**divergence line** highlights where arms differ (e.g. "control still open +40%, scalp_c2 exited −12%
on timecap") — this is the whole point, replacing today's duplicated rows. Coin/wallet overlap: the
card shows the shared source wallet(s) so overlap is visible per episode.

**Wallet × Arm matrix** — rows = wallets, columns = arms; cells = realized PnL / n. Exposes **sample
size** (small-n cells flagged, not hidden), chain/source splits, and wallet↔coin overlap so the
operator sees *where* arms agreed or diverged, not just a leaderboard. Honesty: any cell under a
minimum-n threshold is labeled "thin," never presented as a verdict.

## 9. Shared UI States

A small shared module `static/memecoin/ui_states.js` (or exported helpers) so every card renders these
identically:
- **Freshness** — `known` / `stale (age)` / `unknown (—)`, driven by a per-value timestamp + window.
- **Loading** — skeleton card, never a spinner that hides last-good data (keep last value dimmed +
  "updating").
- **Error** — inline on the card, preserves last-good values, names the failing endpoint.
- **Empty** — honest ("no real positions open", "collecting marks"), never a fake zero row.
- **Funded-action confirmation** — a required confirm step for sell / mirror-toggle / kill, showing the
  concrete effect ("Sell all of $KITTY on Solana — ~$29 at current mark?") before the POST fires. No
  single-click money movement. Uses the existing exit/sell/kill routes.

## 10. Phase-Zero Cleanup (ships first, de-risks everything)

A standalone first PR, safe and independently valuable, before any new cards:
1. Remove the duplicate live-vs-accounting status (single source).
2. Introduce `mc-tokens.css` and replace inline hex in the memecoin renderers with tokens (mechanical,
   no layout change) — this alone kills the Matrix-green-as-base problem.
3. Fix mobile nav: subtab bar becomes a reachable horizontal scroll strip (already `overflow-x:auto` at
   `app.js:2523`; make the active tab pin + be reachable on narrow).
4. Simplify labels (drop cryptic `Src→Q`, `Phase`-style headers already removed from radar; apply the
   same to real/paper).
5. Move `Closed` out of the live cockpit into the Paper workspace.

Phase-zero touches only `static/` and changes no data. It is shippable and reviewable on its own.

## 11. File Structure

`static/app.js` is a monolith IIFE; a full rewrite is out of scope and risky. Pragmatic, memecoin-scoped
split (follows the "files that change together live together" principle without unilateral app-wide
refactor):

- **Create** `static/css/mc-tokens.css` — the token sheet (§4). Loaded by `index.html` before `app.js`.
- **Create** `static/memecoin/cockpit.js` — real-position card renderer (§6) + `ui_states.js` helpers
  (§9). Plain module exposing render functions the existing `_loadReal` calls.
- **Create** `static/memecoin/paper_lab.js` — Strategy Lab renderer (§8).
- **Modify** `static/app.js` — `_loadReal` (`:2935`) and `_loadOpen` delegate rendering to the new
  modules; nav re-parenting (§5) in the `mc-subtab`/`mc-section` block (`:2500`–`:2716`). No behavior
  change to fetching/polling.
- **Modify** `sniper/routes/api_memecoin.py` — only if the deferred `/coin/.../arms` read wrapper (§7)
  is built. Otherwise untouched.
- **Radar** (`app.js` `_loadRadar` `:3691`, `memecoin_radar_view.py`) — restyle to tokens only; data
  layer unchanged.

The rest of `app.js` (Polymarket, HL, Intel renderers) is not touched.

## 12. Testing Strategy

Frontend-heavy, so verification is contract + visual + honesty:
- **JS sanity:** `node --check` on every new/modified `.js`.
- **Endpoint contracts:** extend the existing `tests/test_memecoin_radar_*.py` pattern with assertions
  that `coin_timeline` returns `holders`/`arm_positions` shapes the cockpit relies on, and that
  `/real/positions` includes the drift/peak/exit fields (guards against a backend field rename silently
  blanking a card).
- **Honesty-rail units:** any new read-only projection (if §7 wrapper is built) gets a "never writes"
  SQL-spy test (same pattern as `test_radar_view_never_writes`) and an unknown-vs-zero test.
- **Visual:** Playwright desktop (≥1280) + mobile (≈390) screenshots of Live/Paper/Radar per PR, read
  back and compared to the audit baselines under `.playwright-cli/`. Acceptance: no clipped content, no
  horizontal-scroll body, price values non-overlapping.
- **Funded-path guard (every PR):** `git diff --name-only` asserts no funded-runtime file changed;
  runner heartbeat `auth_ok=true` & fresh before and after.

## 13. Rollout (phased, each independently shippable)

- **PR0 — Phase-zero cleanup** (§10). Tokens introduced, dup status killed, mobile nav fixed, Closed
  moved. No new cards.
- **PR1 — IA + workspaces** (§5). Re-parent 11 subtabs into 4 workspaces.
- **PR2 — Real Position Cockpit** (§6). The core deliverable. Table → cards, with confirmation gates.
- **PR3 — Paper Strategy Lab** (§8).
- **PR4 — Wallet × Arm** (§8).
- **PR5 — Radar restyle** (§7 radar row). Tokens only.

Each PR carries the funded-path guard and the KSG block. PR0–PR2 deliver the bulk of the legibility win;
PR3–PR5 are additive and can be scheduled independently.

## 14. Open Questions (resolve in planning, not blocking design)

1. Solana mini-graph source: `/marks` is sparse for Solana (0x-mark writer targets DexScreener-blind
   chains). v1 uses `coin_timeline.price_path` (quote observations) for Solana and `/marks` for 4663;
   a dedicated Solana mark writer is out of scope and flagged, not faked.
2. Whether the `/coin/.../arms` read wrapper (§7) is worth building vs a client-side join — decided in
   the plan based on payload shape.
