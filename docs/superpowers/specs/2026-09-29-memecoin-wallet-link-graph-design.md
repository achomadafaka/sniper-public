# Memecoin Wallet Link Graph — design (spec only)

**Date:** 2026-09-29 · **Author:** Lambda · **Status:** PROPOSED, not built · **Order:** operator via Alpha

## Goal

For any armed wallet, answer four questions from evidence we already hold, with the evidence attached:

1. **Who it follows**: whose buys it repeatedly copies, and how fast.
2. **Who follows it**: who repeatedly buys the same coins after it.
3. **Who dumps on it**: who repeatedly sells into its buys.
4. **Its bot and bundle companions**: wallets that buy the same coins in the same slot or bundle, and wallets that share a funder.

The graph describes wallets. It does not admit, arm, size or exit anything. Feeding any edge into
funded admission needs its own spec, its own Rule-5 list and an operator decision.

## Preview (measured before design, read-only, 2026-09-29 21:29Z)

J7 buys (`memecoin_events`, `delivery_lane='j7_wallets'`, `side='buy'`, 14 days, first buy per
wallet per coin):

- 1,047 of 3,323 coins had at least two J7 buyers.
- 511 wallet pairs bought a shared coin within 10 minutes of each other.
- 82 pairs did so on at least 3 coins, and 26 of those 82 kept the same order at least 80% of the
  time.
- Top pair: 11 shared coins, same leader 9 times, lag p50 26.5 s.
- 0 pairs had a median lag of 2 s or less. At J7's receive-time resolution, same-slot co-buying
  does not show up. It needs chain time (§Edge 2).

Volume: about 18k J7 events per week across about 70-80 wallets and 1.4k Solana coins. The follows
computation is a ledger scan with no RPC.

## Data we hold, and the edges each gives

| Source | Where | Gives | Coverage limit |
|---|---|---|---|
| J7 buy/sell events | ledger `memecoin_events` (source_user_id, mint, side, received_at, network_id, fomo_event_id carrying the source tx) | follows, followed-by, dumps-on inside the roster | only wallets on our J7 list (about 80); followers outside it are invisible |
| Our entry attempts | ledger `memecoin_entry_attempts` | which source buys we acted on; our own "we followed" edge | our decisions only |
| On-chain watcher detections | ledger `onchain_wallet_detections` (signature, address, mint, block_time) | exact chain time for armed-wallet buys | armed wallets with a proven address |
| Pool tapes | `data/memecoin_ticks.db` / `data/memecoin_sol_tape.db` `pool_ticks` (chain_id, pool, kind, block_no = slot or block, tx_sig, maker, amounts) | same-slot co-buys, in-slot buyers after the source, dumps-on by ANY wallet, price impact | pools we track (positions and candidates), not every pool of a coin |
| Solana funder resolution | `sniper/services/memecoin_cluster_rpc.py` (first valid funding transfer, research RPC; spec 2026-08-22 cluster admission) | funded-by, shared-funder clusters | Solana only; about 3.5 s per cold wallet; its store `data/memecoin_cluster_gate.db` does not exist yet |
| Helius getTransactionsForAddress | research key (reference memory 2026-09-27: slot filter, ascending, transactionIndex) | the full ordered slot around a source buy: bundle and in-slot followers of any wallet | research key budget; Solana only |
| Jito bundle explorer | public API (finding 2026-09-27: 1 of 67 source buys shared a bundle) | same-bundle membership | Solana only |

**Missing, named, and not guessed:**
- EVM funding links. Native ETH/BNB transfers are not logs, public RPCs expose no trace_filter or
  address history, and no explorer key is configured. EVM funded-by stays "unknown" until an
  explorer API (for example Etherscan v2 multichain) is approved.
- The EVM tape's `maker` is not yet confirmed to be the trader (tx.from) rather than the router.
  It must be checked, as the Ink census check did, before any EVM tape edge is trusted.

## Edge types

Each edge is directed or symmetric as stated, and scoped to one chain. Wallet identity is the chain
address (Solana base58 exact, EVM lowercase). `source_user_id` is attached when a proof maps it.

### 1. follows (leader → follower), directed

- **Rule:** first buys of the same coin by A then B, B within W = 10 min of A.
- **Edge criteria:** at least 3 shared coins, and A first in at least 80% of them.
- **Must beat a null:** the pair's shared-coin count must exceed the 99th percentile of 200
  permutations. Each permutation shuffles B's first-buy times across coins within the same UTC day,
  so popular coins do not manufacture followers.
- **Time:** J7 received_at gives the order, with lag reported in seconds. Chain time replaces it
  where the source tx is in the tape or in the watcher.
- **Rule 5:**
  - money path: none;
  - speed: offline;
  - concurrency: one writer job;
  - absent: no shared coin means "no evidence", never "not following";
  - failure: a missing J7 day is excluded, with the gap recorded on the edge;
  - units: seconds; J7's corrected-clock domain (finding 2026-09-27) is used consistently;
  - sample: the J7 roster only (selection bias, stated on every edge);
  - rollback: drop the rows.
- **Cost:** 0 RPC; one indexed ledger scan per run.

### 2. co_buy_slot / co_bundle (symmetric)

- **Rule:** A and B buy the same coin in the same slot (Solana) or block (EVM). co_bundle requires
  the same Jito bundle.
- **Edge criteria:** at least 3 coins.
- **Also recorded, directed:** `slot_follower`, where B's buy lands after A's buy in the same slot
  (higher transactionIndex). Gamma's CARDANO case: a $3.5k buyer at index 449.
- **Source:** the tape first (0 RPC). Helius getTransactionsForAddress per (coin, slot) around an
  armed wallet's buy, only when the tape does not cover the pool.
- **Rule 5:**
  - money path: none;
  - speed: offline;
  - failure: a Helius 429 or timeout leaves that slot "unread" (censored), never "no companions";
  - units: slot versus block height (finding 2026-08-25: never compare them);
  - sample: armed-wallet buys only, since the slot is read around OUR sources;
  - rollback: drop the rows.
- **Cost:** tape 0 RPC. Helius about 1 call per uncovered armed buy (about 5.9k Solana J7 buys per
  week; armed and uncovered is a subset). Budget-capped per run, and never on the funded read lane.

### 3. funded_by (funder → wallet), directed; shared_funder (symmetric)

- **Rule:** the first valid funding transfer into the wallet, via the cluster-admission funder
  resolution.
- **shared_funder:** two wallets with the same funder.
- **Rule 5:**
  - money path: none (the gate itself is separate and unbuilt);
  - speed: about 3.5 s per cold wallet, cached forever per wallet;
  - failure: unresolved is "unknown", never "self-funded";
  - sample: armed wallets plus their graph neighbours (bounded, about 300 wallets);
  - EVM: missing (see above).
- **Cost:** about 20 min cold for about 300 Solana wallets on the research key, then incremental.

### 4. dumps_on (seller → buyer), directed

- **Rule:** B sells coin M within N = 120 s after A's buy of M, where B held M before A's buy. That
  is B's position, not a round trip inside A's block.
- **Edge criteria:** at least 3 coins.
- **Sources:**
  - the tape, for any seller in tracked pools, with the side from the signed amounts;
  - J7 sells, for roster sellers.
- **Rule 5:**
  - money path: none;
  - sample: tracked pools, so the edge undercounts, and the edge says so;
  - absent: a sell we did not see is unknown;
  - units: V4 tape rows store into-pool amounts since 05293598e, and rows written before that deploy
    keep the swapper sign (finding 2026-09-29), so the writer reads the sign per row's store version;
  - rollback: drop the rows.
- **Cost:** 0 RPC; a tape scan bounded by armed buys (index on chain_id, pool, block_no).

### 5. price_impact (wallet attribute, not an edge)

- **Measure:** the median absolute price move of the wallet's buys in tracked pools. EVM V3 uses
  sqrt_price in the tape. Solana CP vaults overstate reserves (finding 2026-09-26), so the Solana
  impact is marked "approximate" until measured against swap marks.
- **Cost:** 0 RPC.

## Storage

### Local: `memecoin_wallet_edges` in the funded ledger (proposal)

The funded ledger is chosen so the web and brain read one place.

<!-- schema:external=memecoin_wallet_edges -->

| Column | Type | Meaning |
|---|---|---|
| `id` | INTEGER PK | |
| `network_id` | TEXT | chain id (`solana`, `4663`, `56`, `8453`, `57073`) |
| `edge_type` | TEXT | follows, slot_follower, co_buy_slot, co_bundle, funded_by, shared_funder, dumps_on |
| `src` / `dst` | TEXT | addresses; for symmetric types `src < dst` |
| `src_user_id` / `dst_user_id` | TEXT NULL | our source_user_id when a proof maps the address |
| `n_coins` | INTEGER | distinct coins supporting the edge |
| `n_consistent` | INTEGER | coins in the declared direction (directed types) |
| `lag_p50_s` / `lag_p90_s` | REAL NULL | directed types |
| `null_p99` | REAL NULL | the permutation null's 99th percentile of shared coins (follows) |
| `first_seen` / `last_seen` | TEXT | ISO UTC of the first and last supporting coin |
| `coverage` | TEXT | JSON: sources used, censored slots, gaps |
| `evidence` | TEXT | JSON: up to 10 supporting (mint, src tx, dst tx, times) |
| `method_version` | TEXT | e.g. `wallet-graph-v1` |
| `computed_at` | TEXT | writer run time |

- **Key:** UNIQUE (network_id, edge_type, src, dst, method_version).
- **Writer:** one research job, `scripts/research/wallet_link_graph.py`. It reads the ledger and
  tapes read-only and writes in one short BEGIN IMMEDIATE per edge type. It is not a funded
  process, and it never holds a writable ledger handle between runs.
- **Cadence:** follows and dumps_on hourly (0 RPC); co_buy and slot edges hourly within a Helius
  budget; funded_by daily and incremental.
- **Rolling window:** 14 days. An edge not re-supported ages out, and last_seen says when.

### Brain projection

- Each wallet is a `brain_entities` row of type `wallet`: name = address, aliases = handle or
  source_user_id.
- Each qualifying edge becomes ONE `brain_facts` row of kind `measurement`:
  - statement, e.g. "A follows B on solana: A first on 9 of 11 shared coins, lag p50 26.5 s,
    14 d, J7 roster only";
  - entity_ids [A, B];
  - detail = the edge JSON;
  - status `unverified`.
- A recompute supersedes the fact. `brain_links.rel` has no follows-type relation, so edges live in
  facts, not links.
- Only edges past their criteria are projected, capped per wallet (top 10 per type), so the brain is
  not flooded.

## The per-wallet answer

One read, for armed wallet X on chain C:

- follows: `edge_type='follows' AND dst=X`, leaders by n_consistent;
- followed by: `edge_type='follows' AND src=X`;
- dumped on by: `edge_type='dumps_on' AND dst=X`;
- companions: `edge_type IN ('co_buy_slot','co_bundle','shared_funder','slot_follower')` with X on
  either side.

Every row carries its coverage and sample note. The web view shows "unknown" where a source is
missing, never "none".

## Open decisions for Alpha and the operator

1. W (10 min), N (120 s), minimum 3 coins, 80% order and the permutation null: confirm, or set now,
   before the first run.
2. Ledger table versus a separate `data/memecoin_wallet_graph.db`. The ledger is simpler for web
   reads; a separate DB removes all writer contention.
3. The Helius budget per run for slot reads.
4. EVM funding: approve an explorer API, or leave it unknown.
5. Whether the web shows the graph (Beta or C-lane owner) or only the brain.
