# Memecoin Wallet Map — design

**Date:** 2026-09-30 · **Author:** Alpha (crew input: Beta, Lambda, Gamma) · **Status:** APPROVED direction, slices 1–2 specified in full · **Order:** operator, 2026-09-30

## Goal

A "Wallet Map" section in the dashboard, in the spirit of Bubblemaps: wallets as bubbles, links as
lines, per chain and per source, showing who leads, who follows, who dumps, which wallets act as
one operator, and who moves the market. Two views with a toggle: the **universe** (our source
wallets and the wallets linked to them) and a **coin** (the buyers of one coin we touched).

The map describes wallets. It never arms, sizes, admits or exits anything. Every figure on it is
read from stored data; the web process makes no chain, price or funded-seam call.

## Operator decisions (2026-09-30)

| Question | Answer |
|---|---|
| Main view | Universe and coin, with a toggle |
| Coin scope | Coins we touched now; any-coin lookup later |
| Default bubble size | "Moves the market" (slice 3); other metrics behind a size-by knob |
| Discovery | Slice 3b: upstream candidates (early on several winning coins), paper-graded, never auto-armed |
| Approach | A: hourly job computes, read-only GET endpoints, d3 force layout in `static/wallet_map.js` |
| Process | Agile: thin slices, each reviewed by Gamma and deployed on its own; no mockups |

## Honesty rules (crew input, binding on every slice)

1. **Roster-centred, not a market map.** Every view carries a stamp: `observed set: N wallets ·
   chain · window · graph method <method_version> · computed <computed_at>`. Strangers appear only
   where they shared a slot with one of our sources.
2. **Direction only from chain order.** A directed arrow is drawn only when the edge's own clock is
   chain order (slot + transactionIndex) or the stored method already tested direction against its
   null. Same-slot pairs are undirected.
3. **Our own wallets and fills never appear as edges or volume.** Otherwise we draw ourselves as
   every armed source's biggest follower. Our custody addresses come from a new public-keys-only
   setting `MEMECOIN_OWN_ADDRESSES` (comma list; the web never loads a keypair; setting it is an
   .env change on the operator's yes). The endpoint drops every edge touching one. Measured
   2026-09-30: 0 of 298 edges touch the Solana custody address today, so this is a guard, not a fix.
4. **Solid / dashed / absent.** Solid = the edge passed its null and spans at least 5 distinct
   coins. Dashed grey = it exists in the graph DB but spans fewer than 5 coins (weak). No line is
   only ever "no edge in the graph DB"; the stamp's coverage note says what "no edge" can mean.
5. **Knobs change what is drawn, never the test.** A knob can hide edges; it can never show an edge
   the graph job did not emit, or lower the method's own floor.
6. **A number always carries its denominator.** Hover shows n shared coins, the null p, the first
   and last seen time; a PnL is never shown without its trade count and window.

## Data (read-only)

| Source | Location | Used for |
|---|---|---|
| Graph edges | `memecoin_wallet_edges` in `data/memecoin_wallet_graph.db` under the STATE ROOT (the web runs from `.runtime/web/current`; never resolve relative to the release) | links, their evidence, `method_version`, `computed_at` |
| Graph runs | `memecoin_wallet_graph_runs` (same DB) | stamp freshness |
| Funders | `memecoin_wallet_funders` (same DB) | hub detection for shared-funder edges |
| Wallet labels | ledger `memecoin_wallets` (`source_user_id`, `handle`, `label`, `paused`, `excluded`, `real_approved`, `real_chains`) | node label, armed per chain |
| Address proofs | ledger `memecoin_address_registry` (`status='verified'` only) | map chain addresses to our sources |
| Activity | ledger `memecoin_events` (bounded window, by `network_id` + `source_user_id`) | slice-1 bubble size: distinct coins per wallet |

The graph DB opens as `sqlite3.connect("file:...?mode=ro", uri=True)` per request (2.2 MB, hourly
writes). The ledger is read through `get_readonly_connection()`; never the blueprint's shared
writable `ctx["conn"]`.

Edge types present today (Solana only, 298 edges at 09:35Z): `follows` 6, `dumps_on` 3,
`shared_funder` 5, `co_buy_slot` 144, `slot_follower` 140.

## Slice 1 — universe map, Solana, existing links

**API.** `GET /api/memecoin/wallet-map?chain=solana&days=14` in `sniper/routes/api_memecoin.py`.
Returns `{stamp, nodes, edges}`:

- `stamp`: observed set size, chain, window, `method_version`, latest `computed_at`, the edge
  source's `coverage` note, and `stale: true` when the latest graph run finished more than 3 h ago.
- `nodes`: one per wallet id that appears in an edge or is one of our sources on the chain.
  Fields: `id`, `kind` (`source` = in `memecoin_wallets`; `stranger` = chain address only),
  `label`/`handle`, `armed` (chain in `real_chains` and `real_approved=1`), `paused`, `excluded`,
  `activity` (distinct coins bought in the window; `null` for strangers = unknown, never 0).
- `edges`: `src`, `dst`, `type`, `directed`, `strength` (`solid`/`weak` per rule 4), `n_coins`,
  `null_p`, `lag_p50_s`, `first_seen`, `last_seen`, `hub_funder` (shared_funder only).

Edge rendering class per type:

| type | directed | drawn as |
|---|---|---|
| `follows` | yes (method-tested) | arrow leader → follower |
| `dumps_on` | yes (method-tested) | red arrow dumper → victim |
| `co_buy_slot` | no | thin dashed, labelled "same slot (bot crowd, not identity)" |
| `slot_follower` | yes (chain order) | thin arrow |
| `shared_funder` | no | dotted; hidden when the funder funds more than 10 of the wallets in the funder table (hub) |

**UI.** New tab `data-tab="wallet-map"` in `templates/index.html`; all logic in a new
`static/wallet_map.js` (not `app.js`). d3 force layout (`d3.forceSimulation`, already vendored at
`static/vendor/d3.min.js`). Bubble size = activity; colour = armed (per chain) / unarmed source /
stranger. Hover card per node and per edge (rule 6). Stamp line above the canvas. Loads on tab
open; refresh button; no polling faster than 60 s.

## Slice 2 — knobs

Client-side filters over the slice-1 payload (no new query): chain, window (7/14/30 d, refetch),
edge types (checkboxes), minimum shared coins (slider, floor = the method's own minimum), armed
only, hide strangers, size-by (activity; later impact), colour-by (armed / kind). Click a node =
focus: dim everything beyond 2 hops. Search box by handle or address.

## Slices 3–5 and later (outline; each gets its own spec delta before build)

- **3 · Market impact.** The hourly `scripts/research/wallet_link_graph.py` also computes, per
  source wallet and chain, other wallets' net buy flow in the 60 s after the source's buy against
  the same coin's pre-signal baseline, our fills excluded, from the stored Solana tapes. The rule is
  pre-registered and reviewed by Gamma before the first read; stored in the graph DB; becomes the
  default size-by. Spec delta: `docs/superpowers/specs/2026-09-30-memecoin-wallet-map-slice3-impact-design.md`.
- **3b · Upstream discovery (operator YES 2026-09-30).** The map's slices 1–3 describe the wallets
  we already have; strangers enter only as in-slot companions, which are mostly copy bots trading
  after the call. 3b looks the other way: for each coin a source of ours bought that later won
  (win defined in the pre-registration, e.g. peak return over a fixed horizon from the source's
  fill), read the coin's buyers BEFORE the source buy (Helius `getTransactionsForAddress`, slot +
  transactionIndex order, research key, inside the existing hourly job's call budget). A wallet
  becomes an `upstream_candidate` only when it was early on at least N distinct winning coins and
  beats a null that draws early buyers from the same coins' non-winning windows; FDR across wallets;
  one lucky coin never qualifies. Our own wallets, known copy-bot clusters and hub-funded wallets
  are excluded. Candidates appear on the map as their own node kind with their evidence (coins,
  lead time, n) and feed the existing paper lane for forward grading. They never reach funded
  arming from the map: the funded roster changes only on funded results (standing operator rule,
  2026-09-11). Pre-registration (N, horizon, win rule, null, FDR level) is written and reviewed by
  Gamma before the first read. Same method is the template for Ink source discovery (task
  8e39ba39).
- **4 · Coin view.** Toggle to a coin we touched: its buyers as bubbles (tapes, J7 events, slot
  reads), clustered by funder and same slot, our fills marked.
- **5 · EVM.** 4663/56/8453: J7 co-buys inside our roster and same-block buys among watched
  addresses (`onchain_wallet_detections`), zero RPC; tape makers labelled "unconfirmed" until tx.from
  is read.
- **Later.** Any-coin lookup (research key budget), EVM funding (an indexer; possibly
  base.blockscout.com for 8453, untested).

## Edge cases (CLAUDE.md rule 5)

1. **Money path:** none. The endpoint and page read only; no funded import (`evm_real_seams`,
   `solana_real_seams`, Jupiter, price helpers). A test pins the import list.
2. **Fill-path speed:** none; the web is a separate process. The ledger query is bounded by window
   and chain and uses existing indexes; measured p50/p90 goes in the slice-1 PR.
3. **Concurrency:** per-request read-only connections; no module-level caches in slice 1.
4. **Absent / zero / unknown:** a missing graph DB, an empty edge table and a failed read are three
   different states. Missing DB or failed read → HTTP 200 with `stamp.error` and no edges, shown as
   "graph unavailable", never an empty map that reads as "no links". Stranger activity is `null`.
5. **Dependencies:** the graph cron can stop; `stale` flags it. A graph-DB schema change is caught
   by a test that reads the columns the endpoint selects.
6. **Units and identity:** wallet ids are `source_user_id` for our sources and chain addresses for
   strangers (Solana base58 exact); an address that the registry maps (verified) to a source is
   merged into that source's node. `lag_*` in seconds.
7. **Sample:** the stamp states the observed set; nothing on the map is a market-wide claim.
8. **Rollback:** revert the commit; the tab and endpoint disappear; nothing else reads them.

**Check left behind:** a test that builds a graph DB fixture with one solid, one weak, one hub
shared-funder and one edge touching our own wallet, and asserts the payload's `strength`, the hub
hiding flag, our-wallet exclusion, and the missing-DB error state; proven red on a mutant for each.

## Amendment, 2026-10-02

The 2026-10-02 wallet-universe design adds an operator-authorized arm action through the existing shared writer and verified authority readback. The original no-arming scope applies to its analytical graph jobs. Separate configured authority from effective live authority; cached measurements never decide either.
