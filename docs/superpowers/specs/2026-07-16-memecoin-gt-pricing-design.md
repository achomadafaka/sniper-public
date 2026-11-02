# Memecoin blind-chain pricing via GeckoTerminal — kill the 0x pricing dependency

**Date:** 2026-07-16 · **Status:** ready for implementation (operator approved: no 0x plan upgrade)
**Depends on:** `e4df3c00` (blind-chain probe capture + candidate cohorts — merged, not yet deployed)
**Companion spec (do NOT implement here):** `2026-07-16-memecoin-direct-swap-4663-design.md` (execution)

## Why (incident context — verified 2026-07-16)

- The 0x API key is dead: `api.0x.org` returns **403 "You cannot consume this service"** on every
  chain/endpoint/taker (plan cap; operator declines upgrade). Live impact: chain-4663 `zerox`
  position marks stopped 2026-07-16T04:29:57Z; 4663 exit rails price-blind; the new blind capture
  worker would only write `probe_unpriced` rows. Brain: `finding/memecoin-0x-key-outage-2026-07-16`.
- 0x was our only price source for DexScreener-blind chains (4663). It burned ~23k calls/day for
  marks alone — the cap failure mode is structural, not accidental.
- **Verified 2026-07-16 (live calls):** GeckoTerminal's public API indexes the Robinhood chain as
  network slug `robinhood`, free, no key. Batch endpoint returned USD prices for our real 4663
  mints — including `0x39dbed3a2bd333467115de45665cc57f813c4571`, which the 0x probe could NOT
  price — plus pool reserve (`Index / WETH 1%`, $1.4M):
  `GET https://api.geckoterminal.com/api/v2/simple/networks/robinhood/token_price/{addr1,addr2}?include_market_cap=true&include_24hr_vol=true`
  → `{"data":{"attributes":{"token_prices":{"0x39db…":"0.00832…","0x5691…":"0.01929…"},…}}}`
  Public rate limit ~30 calls/min; up to 30 addresses per call. Prices are **already USD** — no
  WETH conversion needed (the 0x path needed a live ETH mark; this one doesn't).

## Goal

Blind-chain (4663) pricing for capture AND position marks flows from GeckoTerminal, free, batched,
with the 0x probe demoted to optional fallback. No behavior change on any DexScreener-priced chain
and none on Solana.

## Design

### 1. New client module: `sniper/execution/geckoterminal_quote.py`

Mirror the DexScreener client conventions in `sniper/execution/solana_quote.py` (browser UA — GT
may 403 the default Python UA like DexScreener/Jupiter do; stdlib urllib; no new deps).

- `GT_NETWORK = {"4663": "robinhood"}` — module constant. ONLY the verified slug. Do not guess
  slugs for other chains; extending the map is a one-line change when a chain is verified.
- `gt_stats_batch_keyed(keys, http=None) -> {(network_id, mint): stats}` where stats =
  `{"price": float, "liquidity": None, "mcap": float|None, "source": "geckoterminal"}` — the SAME
  shape `evm_price_batch` returns, so it drops into every existing seam.
  - Group keys by network; skip networks not in `GT_NETWORK`; chunk ≤30 addresses per HTTP call.
  - Parse `token_prices` (string → float; omit non-positive/non-finite — NEVER emit a 0.0 tick,
    same contract as `memecoin_evm_price`). `market_cap_usd` observed as `"0.0"`/`null` for young
    coins → treat ≤0/null as None (the RPC totalSupply mcap path stays the real mcap source).
  - `liquidity: None` always in v1. (`include_total_reserve_in_usd` may exist on this endpoint —
    if the implementer verifies it live, wire it; otherwise None is the honest default and the
    slippage model already treats unknown liquidity pessimistically.)
- `gt_price_for(network_id, mint, http=None) -> float|None` — single-key convenience wrapper over
  the same cache (below) for the `price_for(chain, mint)` seam.
- **Shared TTL cache + rate guard (module-level, like `_last_attempt` in memecoin_evm_price):**
  cache entries ~`MEMECOIN_GT_TTL_S` (default 15s); a min-interval guard keeping total GT HTTP
  ≤ ~20 req/min. The blind capture worker's batch call warms the cache, so the exit sweep's
  per-mint `gt_price_for` lookups are mostly cache hits. Provide `reset_gt_state()` test seam.
- Every HTTP failure → missing key in the result, never an exception to the caller.

### 2. Wire into the blind capture worker (`sniper/services/memecoin_capture_v2.py`)

`_default_blind_price_batch_fn(blind)` becomes: GT batch first; for keys GT returned nothing,
fall through to the existing 0x `evm_price_batch` binding (it self-parks failing mints and fails
closed while the key is dead — keep it as a free second chance, do not remove it).
- The blind pass currently hardcodes `dex_id='zerox'` / `pair_address='zerox:<network>'`. Change
  to derive from `stats["source"]`: `dex_id=<source>`, `pair_address=f"{source}:{network_id}"`.
  Quality vocabulary (`probe_ok` / `probe_ratio_outlier` / `probe_unpriced`) and the ratio guard
  are UNCHANGED — the guard runs on one `last_good_price` chain regardless of source, which is
  exactly what protects against a source flip-flop spike.
- Update the affected assertions in `tests/test_memecoin_capture_blind.py` (they pin `zerox`
  strings for injected stats with `source: "zerox"` — injected GT-shaped stats must produce
  `geckoterminal:`-tagged rows).

### 3. Wire into the exit-sweep / position-marks seam (`sniper/services/memecoin_evm_price.py`)

Inside `build_evm_price_for(...)`'s returned `price_for(chain, mint)`: after the existing
blind-chain gate (`chain not in allowed → None` — this gate embodies the RUSSELL +$785.66 phantom
lesson: NEVER price a DexScreener-covered chain from a secondary source; GT inherits the same
rule), try `gt_price_for(chain, mint)` FIRST; on None, fall through to the existing 0x quote path.
- Interface unchanged → `scripts/memecoin_runner_loop.py:172-173` (`build_evm_price_for()`),
  the bracket-path merge in `sniper/services/memecoin_runner.py` (~line 373:
  `evm_price_batch(unpriced, evm_price_for, mcap_for=evm_mcap_for)`), and the real exit sweep all
  pick it up with zero call-site changes.
- `evm_price_batch` stamps `source: "zerox"` on its output today — make the source honest: have
  `price_for` return price only (unchanged signature) but let `evm_price_batch` accept an optional
  source hint, OR simplest honest option: `gt_price_for` results flow through with source
  `"geckoterminal"` by extending `evm_price_batch`'s stats to take `(price, source)` from a
  `price_for` that returns a tuple — pick ONE, keep it small, and make sure `memecoin_position_marks.source`
  and the quote ledger record the true source (verify where marks copy `source` from stats before
  claiming done; the exit sweep's spike-quarantine log prints it).
- `build_evm_mcap_for` (RPC totalSupply × price) is UNCHANGED — it is free and more reliable than
  GT's often-null mcap.

### 4. Settings (`sniper/settings/memecoin.py`)

| var | default | meaning |
|---|---|---|
| MEMECOIN_GT_ENABLED | 1 | GT pricing on/off (off → pure 0x behavior, i.e. today's code) |
| MEMECOIN_GT_TTL_S | 15 | shared price-cache TTL |
| MEMECOIN_GT_MIN_INTERVAL_S | 3 | min spacing between GT HTTP calls (≤20/min) |

### 5. Explicitly out of scope

Swap execution (companion spec). Solana pricing. DexScreener-chain pricing. GT for any chain not
in `GT_NETWORK`. Removing the 0x code path (it stays as fallback behind its key check). mcap
sourcing changes. New DB columns (existing TEXT columns absorb the new source strings).

## What could go wrong → mitigation

- **GT price staleness/pool-mid semantics** — same class as DexScreener, which already prices every
  other chain's rails. Marks/rails only; the fill simulator and slippage model are untouched.
- **Rate limit (429)** — min-interval guard + TTL cache + ≤30-address batching; on 429 treat as
  missing (fail-closed), never retry-storm.
- **GT returns a price for a dead/rugged coin** — ratio guard vs `last_good_price` in the blind
  pass and the exit sweep's existing spike quarantine both still apply.
- **Source flip-flop GT↔0x** — single `last_good_price` chain per cohort; ratio guard is
  source-agnostic; `source` column records provenance per row.

## Verification (implementer must run, in order)

1. `python3 -m pytest tests/test_memecoin_capture_blind.py tests/test_memecoin_evm_price.py tests/test_memecoin_capture_v2_quotes.py -q` — plus new GT client tests (parse, cache TTL, rate guard, ≤30 chunking, non-positive-price omission, 403/429 → missing).
2. Full `python3 -m pytest tests/ -q -k memecoin`.
3. Live smoke (read-only, like the e4df3c00 smoke): in-memory DB + real GT call for the two mints
   above → expect `probe_ok` rows with `dex_id='geckoterminal'` and positive prices.
4. `python3 scripts/validate_schema_refs.py` before committing.
5. Post-deploy (standard pinned-runtime gates): within 30 min, 4663 rows with
   `quality='probe_ok'` appear in quote observations; 4663 position marks resume with
   `source='geckoterminal'`; zero 0x HTTP attempts needed for pricing (log-grep).

<!-- schema:external=memecoin_quote_observations_v2 -->
<!-- schema:external=memecoin_position_marks -->
<!-- schema:external=memecoin_capture_cohorts_v2 -->
