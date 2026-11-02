# Intel paper testing — design

**Date:** 2026-07-29
**Status:** design, pending implementation plan
**Audit that motivated it:** `docs/audits/2026-07-29-intel-geopolitics-end-to-end-audit.md`
**Producer already shipped:** `docs/superpowers/plans/2026-07-29-intel-geopolitics-revival.md` (Phases 0–2, merged)

---

## Goal

The LLM reads the news and produces ideas. If a Polymarket market or a Hyperliquid
instrument expresses an idea, open a paper position and score it on price movement.
Show the operator, per position, what it was entered at, where it is now, the thesis,
and two display-only signals: the adversarial reviewer's verdict and which proven
wallets sit on each side.

Nothing gates entry. Every signal is recorded so we can later measure whether it
carries any information, and only then decide whether it should gate anything.

## Non-goals

- No consensus scoring. The holder panel is display-only and affects nothing.
- No reviewer gate on entry. The verdict is a tag, not a door.
- No horizon cap. A position profits from price movement; being right at resolution
  is a bonus, not the objective.
- No live money, no order placement, no strategy-runner lifecycle.

---

## Why nothing gates entry

Two reasons, one practical and one methodological.

Practical: `adversarial_approved` is `NULL` on the one live candidate, and the
reviewer agent is not scheduled. A gate on it yields zero positions forever.

Methodological: if only approved ideas are papered, the reviewer can never be
evaluated — there is no rejected cohort to compare against. Papering everything and
tagging it makes the reviewer itself a testable hypothesis rather than an assumption.
The same argument applies to the holder panel.

This is the `finding-crypto5m-floor-endogeneity-2026-07-07` lesson applied in advance:
decide what a signal is worth from data, not from how sensible it sounds.

---

## Routing

Each candidate resolves to exactly one disposition. Nothing is silently dropped.

| Disposition | Condition | Booked? |
|---|---|---|
| `pm` | `condition_id` resolves to a CLOB `token_id` and a decision price exists | yes |
| `hl` | instrument resolves to a live Hyperliquid symbol | yes |
| `no_instrument` | thesis maps to nothing tradeable on either venue | no, recorded |
| `not_executable` | market unresolvable or no decision price *(already implemented)* | no, recorded |

`no_instrument` is a first-class outcome and a real metric: if most theses cannot be
expressed as a position, the constraint is market availability, not analysis quality.

### Hyperliquid instrument resolution

The main perp universe is crypto-only (232 assets). Equities, indices and commodities
live on **HIP-3 builder-deployed dexes**, addressed as `dex:SYMBOL`. Verified live
2026-07-29 via `POST /info {"type":"perpDexs"}` then `{"type":"meta","dex":"<name>"}`:

| Dex | Assets | Examples |
|---|---:|---|
| `xyz` | 103 | TSLA, NVDA, AAPL, META, COIN, PLTR, INTC, HOOD, GOLD |
| `km`, `mkts` | 23 | US500, USTECH, USOIL, USBOND, SMALL2000, BABA, EUR, SILVER |
| `flx` | 16 | OIL, GAS, GOLD, SILVER, TSLA, NVDA, CRCL |
| `cash` | 17 | USA500, TSLA, NVDA, GOOGL, AMZN, MSFT, META |
| `vntl` | 15 | SPACEX, OPENAI, ANTHROPIC, MAG7, SEMIS, DEFENSE, NUCLEAR, ENERGY |
| `para` | 16 | AVGO, LRCX, NET, TOTAL2, BTCD |

Resolution rule: the LLM proposes a symbol; we accept it **only** if it appears in a
live `meta` response for the main universe or one of the enumerated dexes. The
universe is fetched per run and cached for the run, never hardcoded — dexes are
permissionless and the list will change.

This directly fixes audit F-01, where 5/5 proposed HL instruments (`EIS`, `OIL-USD`,
`OIL-PERP`) did not exist. Note `OIL` **does** exist as `flx:OIL`; the old failure was
a fabricated symbol, not an absent market.

---

## Multi-outcome markets

There are no non-binary markets. A Polymarket **event** holds N **markets**, each of
which is binary. "Presidential Election Winner 2028" is 128 separate markets — one per
candidate — each with its own `conditionId`, exactly two `clobTokenIds`, `outcomes`
`["Yes","No"]`, and a `groupItemTitle` naming the outcome ("JD Vance").

So a "multi-outcome" idea is a *selection* problem, not a pricing one. The audit's
undefined `buy_yes` on "Which party will control the Senate after 2026?" was the LLM
naming an **event** rather than a **market**.

Two consequences:

1. **Identity comes from `condition_id`, always.** The producer already feeds the LLM
   market-level rows with conditionIds and requires an exact match, so identity is
   pinned by construction. An idea we cannot pin to exactly one `condition_id` is
   `no_instrument`, never a guess.
2. **Display needs three labels, not one.** Rendering "Presidential Election Winner
   2028 → YES" is meaningless. The correct rendering is
   *event title → outcome (`groupItemTitle`) → side → price*.

## Opening a position — one call

`GET gamma-api.polymarket.com/markets?condition_ids=<cid>` returns everything needed
to open and to render, in a single request: `question`, `groupItemTitle`,
`events[].title`, `outcomes`, `outcomePrices`, **both `clobTokenIds`**, `bestBid`,
`bestAsk`, `endDateIso`, `liquidityNum`, `volume24hr`, `negRisk`, `closed`.

This supersedes the CLOB `/markets/<cid>` round-trip in `pm_resolver.resolve_token`:
the token ids arrive with the metadata. Opening is one HTTP call.

For Hyperliquid, opening needs the instrument to appear in a cached `meta` response
plus its mid from `allMids` — both already fetched per cycle, so opening is zero
extra calls.

## Pricing and refresh

The constraint: the box already runs many per-minute watchdogs. Marking must not
scale with book size and must never run inside a web request.

**Marks are O(1) HTTP calls, independent of position count:**

| Venue | Call | Returns |
|---|---|---|
| Polymarket | `POST clob/prices` with all open token ids | bid + ask per token, one response |
| Hyperliquid main | `POST info {"type":"allMids"}` | 941 mids |
| Hyperliquid dex | `POST info {"type":"allMids","dex":"<name>"}` | that dex's mids |

A full cycle is roughly three requests total. Compared with the existing
every-minute watchdogs this is negligible.

**A `*/5` cron writes marks; the UI reads only SQLite.** No external call ever happens
in a request path. The cron is not an optimisation — the mark series *is* the
measurement, so it must accrue whether or not anyone is looking. Lazy
refresh-on-view was rejected for exactly that reason: it would leave gaps wherever
nobody happened to open the tab.

**Freshness is shown, not assumed.** Each row renders its mark age. A manual refresh
button triggers the same one-shot cycle for operators who want it now. Marks older
than 15 minutes render as stale rather than being presented as current — audit F-02.

Using `/prices` rather than `/midpoints` costs nothing extra and yields bid/ask, so
PnL can be marked conservatively (a long marks at the bid) instead of at the mid.
This does not constitute an execution-cost model — see Open questions.

## Storage

One table pair, both venues. Rejected alternatives and why:

- **`strategy_positions`** — right shape for Polymarket, but `condition_id NOT NULL`
  and `outcome NOT NULL` cannot represent an HL perp without fabricating a market
  identity, and it drags in the strategy-runner lifecycle we do not need.
- **`hl_paper_positions`** — perp-shaped, but welded to the alt-short study
  (`arm_id`, `obs_id`, clip-vs-ref pricing, funding bps). Intel rows would populate
  ~6 of 35 columns and contaminate an active experiment's queries.

Keeping one cohort in one table also means the two UI surfaces are filtered views of
the same data, so measurement never has to union across books.

### `intel_paper_positions`

<!-- schema:external=intel_paper_positions -->

| Column | Type | Notes |
|---|---|---|
| `position_id` | INTEGER PK | |
| `idea_id` | INTEGER NOT NULL | FK → `trade_ideas.id` |
| `venue` | TEXT NOT NULL | `pm` \| `hl` |
| `instrument` | TEXT NOT NULL | PM: `condition_id`; HL: `dex:SYMBOL` or bare symbol |
| `token_id` | TEXT | PM only — the CLOB asset id for the chosen side |
| `event_title` | TEXT | PM: parent event, e.g. "Presidential Election Winner 2028" |
| `outcome_label` | TEXT | PM: `groupItemTitle`, e.g. "JD Vance" — the outcome within the event |
| `direction` | TEXT NOT NULL | PM: `buy_yes`/`buy_no`; HL: `long`/`short` |
| `entry_price` | REAL NOT NULL | PM: outcome-token price (ADR-003); HL: mark |
| `entry_ts` | TEXT NOT NULL | ISO-8601 UTC |
| `size_usdc` | REAL NOT NULL | fixed notional, no sizing model |
| `status` | TEXT NOT NULL | `open` \| `closed` |
| `close_price` | REAL | |
| `close_ts` | TEXT | |
| `close_reason` | TEXT | `resolved` \| `expired` \| `manual` |
| `reviewer_verdict` | TEXT | `approved` \| `rejected` \| `pending` at entry |
| `reviewer_score` | INTEGER | adversarial score at entry |
| `holder_snapshot` | TEXT | JSON, display-only (see below) |

### `intel_paper_marks`

<!-- schema:external=intel_paper_marks -->

| Column | Type | Notes |
|---|---|---|
| `position_id` | INTEGER NOT NULL | FK |
| `ts` | TEXT NOT NULL | ISO-8601 UTC |
| `price` | REAL NOT NULL | same convention as `entry_price` |
| `pnl_pct` | REAL NOT NULL | signed, relative to `entry_price` and `direction` |

PK `(position_id, ts)`. Marked on a schedule; the mark series **is** the result.

Both tables are additive. No existing table is modified.

---

## Confidence panel — display only

For a Polymarket candidate, at entry:

1. `GET data-api.polymarket.com/holders?market=<conditionId>` → top holders per
   outcome token, with proxy wallet, display name and size. Verified live: the
   parameter is the **conditionId**, not the token id.
2. For each holder, run the existing wallet protocol. It reads
   `/closed-positions`, `/positions` and `/activity` directly from Polymarket's
   public API (`sniper/analysis/wallet_protocol/fetch.py`), so it needs no local
   wallet database — which matters, because `user_analysis` is currently empty.
3. Keep only wallets that clear **H5** — tag-level edge concentration: for a tag with
   `n_held_to_resolution >= 10`, the wallet's held-to-resolution win rate exceeds the
   tag population mean. Use `exit_attribution` to confirm the edge is held-to-resolution
   prediction rather than pre-resolution trading, and `independence` (eventSlug
   clustering) so correlated positions are not counted as independent wins.

The panel renders: how many qualifying wallets sit on our side, how many opposite,
and their sizes. It is stored in `holder_snapshot` and **changes no decision**.

Why display-only, beyond the operator's preference: `conviction_add_paradox`
(n=292 resolved) found the opposite of the intuition — 0 wallets buying since entry
gave 80.0% WR / +$222, while 6+ gave 46.7% WR / −$796, concluding *"the smart-money
signal is most valuable when it is LONELY."* That is about flow after entry rather
than stock at entry, so it does not settle the question — which is exactly why the
panel records rather than decides. Recording it lets us settle it later.

Cost control: the protocol is a multi-request walk per wallet. Cap at the top 5
holders per side, cache verdicts per wallet for 7 days, and compute off the entry
path so a slow or failing lookup never blocks a position from opening.

---

## UI placement

| Cohort | Location | Status |
|---|---|---|
| Polymarket | Polymarket → Operations → Positions → **Paper Trades** sub-tab | exists (`#paper-table`, `data-subtab="paper"`) |
| Hyperliquid | Intel → Geopolitics → new **Paper** fold | new |

Hyperliquid intel positions have no natural home — the HL venue sections
(`hl-algo`, `hl-analyzer`) belong to the HL algo fleet. Placing them under
Geopolitics keeps each thesis next to the position it produced.

The split is deliberate but costs comparability, so each view shows the other's
open count with a link, and both read the same table.

Per row: instrument, thesis, direction, entry price, current price, PnL %, age,
reviewer verdict badge, and the holder panel. Stale marks are labelled with their
age rather than being presented as current — audit F-02.

---

## Testing

- Routing: a candidate with a resolvable PM market books `pm`; one naming a real
  HL symbol books `hl`; one naming `EIS` books `no_instrument` and opens nothing.
- HL universe resolution is checked against a mocked `meta`/`perpDexs` response —
  never against the live API in tests.
- A rejected-by-reviewer candidate still opens a position, tagged `rejected`.
- Marks compute signed PnL correctly for all four direction/venue combinations.
- A failing holder lookup leaves the position open with an empty panel.
- Regression: the ADR-003 convention holds — a `buy_no` position stores the NO price.

## Closing

**A position closes manually, or when its market resolves. Nothing else closes it.**
There is no time-based rule and no `expires_at` close — an idea's expiry governs the
*idea*, not the position taken on it.

- **Resolution** — the mark cycle batches one Gamma call covering every open PM market
  (repeated `condition_ids` params; a comma-joined value silently returns zero rows),
  so resolution detection stays O(1) calls. When `closed` is true the position closes
  at *our side's* final `outcomePrices` value, `close_reason='resolved'`.
- **Manual** — `POST /api/intel-paper/positions/<id>/close` closes at the last mark
  and refuses a position that has none: closing without an observed price would invent
  one. A Hyperliquid perp never resolves, so this is its only exit.

## Decided, not deferred

- **No execution-cost model, by decision.** Marks are taken at the bid, which is
  conservative, and that is where it stops. Fees, slippage and depth are not modelled
  and will not be. Read results as directional signal, not as profitability.
- **Holder / expert-consensus panel is blocked, not scheduled.** It requires a
  professional-trader cohort analysed and registered per topic domain, which does not
  exist — `user_analysis` is empty (audit §1). The `holder_snapshot` column stays so
  adding it later is a write rather than a migration, but no work happens until that
  cohort exists. The mechanism is proven feasible (`/holders?market=<conditionId>`
  returns both sides, and the wallet protocol's H5 runs off Polymarket's public API
  with no local database) — it is the *inputs* that are missing, not the method.

## negRisk and sibling independence

**negRisk needs no special pricing.** Verified live 2026-07-29 across the top 300
markets by volume: 27/100 are negRisk, and every sampled one has exactly 2 outcomes,
2 `clobTokenIds`, and `YES + NO == 1.0000`. The CLAUDE.md warning about "synthetic
shorts violating price assumptions" applies to *event-level* reasoning (the sum of
YES across an event's markets, and NO-conversion mechanics), not to per-market binary
pricing. A test pins the assumption so a venue change surfaces as a failure rather
than as wrong PnL.

**The real hazard is independence, and it is not specific to negRisk.** Sibling
outcomes of one event are mechanically linked — mutually exclusive in a negRisk
event, or nested ("no ceasefire by Aug 31" implies "none by Jul 31"). N such
positions are **one** observation, not N.

This is live already: both current positions are outcomes of the same event, so the
book is n=1 episode despite showing two rows. Left unmarked it would silently inflate
any future sample count — the cluster-aware-uncertainty requirement the wallet
protocol already models via `eventSlug` clustering.

Accounted for by recording rather than by inference:

- `neg_risk` is stored per position (`NULL` on rows written before the column existed
  — unknown, not fabricated as 0).
- `open_positions()` returns `event_position_count`, the number of open positions
  sharing an `event_title`. A NULL event title never clusters, so Hyperliquid
  positions do not collapse into one bucket.
- The UI badges both: `· 2-way event` and `· negRisk`.

No de-duplication is applied. The correlation is made visible so that any later
analysis can cluster on `event_title`; silently collapsing rows would discard
information the operator may want.
