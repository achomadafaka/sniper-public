# Native float drift panel

Operator order 2026-10-08 ~16:35Z, via Alpha: "make it so I can see the live price moves of our
non-USDC tokens, make it easy to see and nice". Context: the chain-1 wallet "lost $10" purely
because ETH fell about 9% since 10-06 (0.0402 ETH, no transaction). The idle float is about 0.169
ETH across 4663/8453/1 (native + WETH), 0.334 BNB on 56 (BNB + WBNB), and the Solana SOL buffer and
gas reserve. A 1% ETH move is about $4.1 and a 1% BNB move about $2.6.

## What the panel shows

In the Real tab, beside accounting:

- A headline, "native price drift": the USD change in the idle native float caused by price alone,
  over the selected window. It is kept separate from trading PnL.
- Per asset (ETH, BNB, SOL): units held, split per chain into native and wrapped; USD value now;
  the live price; the change in $ and % over 1h / 24h / 7d; a sparkline.
- A stale flag when the snapshot is more than 3 min old or a price source failed. "Price
  unavailable" when Binance is unreachable. An absent value is never shown as $0, and an old price
  is never shown as live.
- No baseline: the operator removed the baseline reset on 2026-10-08. The windows are 1h, 24h and
  7d only. A `data/native_float_baseline.json` left on disk is not read.

## Sources (Alpha approved 2026-10-08)

No new read on the funded path. No read from a web or display thread into a funded seam: no funded
Jupiter, no custody RPC, no DexScreener. The web reads only the sampler's own files, read-only.

- 4663 and 56 balances: the EVM gas top-up pass already reads native and WETH/WBNB raw balances every
  15 s (`memecoin_evm_leg` gas seams). A recorder keeps the last good value per (chain, kind) in
  memory. No extra read.
- Solana SOL balance: the slot-lane prefetch already reads it every 2 s; the same recorder keeps it.
  WSOL is not counted (it is not part of the idle float).
- 8453 and 1 balances: one batched JSON-RPC POST per chain (eth_chainId proof, eth_getBalance,
  WETH balanceOf) to the chain's independent public read endpoint (`memecoin_evm_holdings._public_post`;
  for chain 1 the configured observer endpoint), every 5 min, on the sampler thread. The same
  public read is the fallback for 4663/56 when the recorder holds nothing newer than 10 min.
- Prices: Binance public `ticker/price` for ETHUSDT, BNBUSDT and SOLUSDT in one request, once a
  minute (the source `evm_wallet.asset_usd_price` already uses; no key, no FMP fallback). At startup,
  `klines` 1h x 168 per symbol (3 requests) backfills 7 days of prices.

## Storage and publication

One daemon thread in the runner (`MEMECOIN_NATIVE_FLOAT_SAMPLER`, default on, 0 = off), once a
minute:

- `data/native_float.db`: its own SQLite file, never the ledger. Table `native_float_prices(ts,
  asset, price_usd, source)` and `native_float_units(ts, chain, asset, native_units, wrapped_units,
  source)`. Retention 30 days.
- `data/native_float.json`: the latest snapshot, written as tmp + rename. It holds per asset units,
  value, price and price age; window changes; sparkline points; source health.

Drift math: over a window, the sum over consecutive price samples of units held x (price_i -
price_{i-1}), using the units known at the earlier sample. A balance change from our own trades or
top-ups is therefore not counted as drift. Before the sampler's first unit sample (the backfilled
days), the earliest known units are used, and the panel says so ("balances recorded from <time>;
drift before then assumes today's balances").

Units per sample (fixed 2026-10-08): each chain's last read is carried forward up to 15 min, because
8453 and 1 are read every 5 min and 4663 every minute. While any chain of an asset has no read in
that time, including before its first read, the asset's units are unknown and any window with such a
step has unknown drift, never a smaller sum. The same holds when the whole asset writes no row for
more than 15 min. The headline names an asset whose drift is unknown ("partial: BNB not counted").

## Edge cases (CLAUDE.md rule 5)

1. Money path: no. Display only. The recorder hooks sit in two funded readers (the gas pass seams
   and the slot-lane prefetch) and only copy a value they already hold.
2. Fill path: the recorder is a dict assignment under try/except (no lock, no I/O). The sampler runs
   on its own daemon thread, takes no lock the fill path takes, opens its own SQLite file, and never
   raises into the runner.
3. Concurrency: the recorder dict is written by the gas pass thread and the prefetch thread and read
   by the sampler; each key is replaced as one tuple. The web reads files written by rename.
4. Absent, zero, unknown: a failed read is None, never 0. A chain whose units are unknown shows as
   unknown and is left out of the totals, which then say "partial". A zero balance is shown as 0.
   A price older than 3 min, or a failed price fetch, shows as "price unavailable".
5. Dependencies: Binance down gives "price unavailable" plus stale; a public RPC down leaves that
   chain unknown (the last units are kept with their age; past 15 min they are unknown); a 429 is
   not retried in the same minute; every HTTP call has a 6 s timeout; a sampler exception is logged
   and the next minute runs.
6. Units: raw balances divided by 18 decimals (ETH, BNB, WETH, WBNB; read from CHAIN_QUOTE) and 9
   (SOL). Prices are USDT pairs, read as USD. Times are UTC, ISO and epoch seconds.
7. Sample: the float is what the sources see (custody wallet only). Open-position tokens are not
   native float. USDC is excluded by definition.
8. Rollback: `MEMECOIN_NATIVE_FLOAT_SAMPLER=0` and a restart; the panel then shows stale. The files
   can be deleted; no other reader depends on them. Reverting the observation-time change
   (68c8f1e07) ALSO needs `data/native_float.db` deleted or
   `ALTER TABLE native_float_units DROP COLUMN observed_at`: the older code inserts six positional
   values into what is now a seven-column table, and the panel goes dead.

## Fixes after review (2026-10-08 engineering review SF07, SF09)

- Each balance row keeps when it was READ (`observed_at`) apart from the sample that stored it; age,
  the 15 min carry and drift's stall guard all use the read time. A copied old reading never looks new.
- The first sample after a start waits up to 30 s for the slot-lane SOL recorder (no public fallback).
- The page ages a snapshot by the time it arrived; past 3 min, or with no arrival time, it shows no
  live price, value, change or headline, and a failed refresh is always shown.
- Klines give each candle's open price at its open time (Binance's close belongs to the close time).

## Funded-only recovery (2026-10-13)

The display sampler remains enabled in funded-only mode, subject to its existing opt-out. It is
funded-wallet telemetry; research workers and the optional equity recorder retain their separate
enablement. No provider call or wait is added to order filling.

Startup refreshes public price history when the last price is stale, even if older history already
covers seven days. A single current price cannot establish a window percentage. Dollar drift
requires fresh balance observations throughout the measured price interval, including gaps between
sparse prices and the final carry; copied stale observations do not extend coverage. The documented
assumption before the first balance observation remains explicit.

The panel's file-only refresh runs independently of cockpit status and positions requests, retaining
the existing 20-second throttle and singleflight. Missing historical observations remain unknown.
