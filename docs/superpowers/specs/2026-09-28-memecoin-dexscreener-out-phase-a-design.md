# DexScreener out, phase A: the consumer switch, E3, E5 and X1

Status: rule-5 spec, no code. Order via Alpha (2026-09-28 ~23:40Z), operator item 2: "DexScreener
out where our own data is as good or better." Scope as ordered: a per-consumer switch, E3 (the
pre-quote display chase), E5 (pair age), X1 (the funded exit sweep's mids), and a measurement of
what E13's market-data GET costs the fill path. E1, E2, E6 and E7 are out of scope. Ids are the
inventory's (`docs/audits/2026-09-28-dexscreener-removal-inventory.md`, 390d27aca); no separate
phase-0 document exists in any branch or the working tree. Author: Lambda.

<!-- schema:external=memecoin_refusal_captures -->

## Measured (2026-09-28 23:38–23:47Z, read-only)

**E3, the pre-quote display chase** (`memecoin_real_runner.py:2937-3017`, reason "coin already ran
X% past their entry"). 42 refusals in 14 days: Solana 24 (nano 11, micro 11, small 2), 4663 11,
56 3, 8453 2, 5042 2. ~~4663 is not DexScreener (its market data is GeckoTerminal and our store;
`MEMECOIN_DEX_BLIND_CHAINS=4663`), so the DexScreener E3 is 31 refusals in 14 days, 2.2 a day.~~
CORRECTED 2026-09-29: 4663 IS DexScreener (chainId `robinhood`, since 2026-08-30), so all 42 are
DexScreener-priced, 3.0 a day. The Solana outcome table below is unchanged.

It is NOT a subset of the executor's exact-quote chase:
- for a nano coin the executor's cap is `max(event cap, MEMECOIN_REAL_NANO_QUOTE_CHASE_MAX_PCT)`,
  35% against the display chase's 30% (`:3080`);
- the display chase has the source-impact extension (`chase_impact_allowance`), 1 admission in 14 days.

What the exact route said about the same coins: 22 of the Solana refusals have an exact-route
refusal capture whose first buy quote came 1-3 s after the refusal, at a $20 clip. On 13 the exact
premium over the source price was inside the cap the executor would have applied (-18% to +31%),
while the display gap read +34% to +123%. On 9 the exact premium was over it as well.
What the coins did next (the capture's sell quotes against its own buy, same instrument):

| group | n | 60 s | 300 s | 780 s |
|---|---|---|---|---|
| exact chase would ADMIT (E3 alone refused) | 13 | median -5.4% (n 11) | -23.0% (n 9) | -16.4% (n 7) |
| exact chase would refuse too | 9 | +17.3% (n 9) | -4.6% (n 6) | +5.3% (n 5) |

Three of the 13 fell 85% to 97%; one rose 160%. The likely mechanism: DexScreener's mid lags
about 25 s behind the chain (finding: DexScreener lag and CDN cache), so "display far above the
source while the route sits near it" reads like "this coin already ran and gave it back". n is small
and the clip is $20, not ours. It is still evidence that dropping E3 is not neutral.

**E5, pair age.** Readers of `facts["pair_age_s"]`:
- the coin-age floor `coin_age_refusal` (`memecoin_risk.py:63`): 120 s on every chain; when the pair
  is younger than the floor it already consults the mint's own evidence (`memecoin_coin_age`);
- the h1 floor pro-rating `_h1_floor_share` (`memecoin_real_runner.py:7720`);
- the lifecycle label (`memecoin_market_state.py:533`), descriptive: `risk_scale` reads the regime only;
- `MEMECOIN_REAL_MAX_PAIR_AGE_S`, 0 (off);
- telemetry `source_token_age_s`.

Coverage on funded fills, 14 days: pair age known on 231 of 317 Solana fills (73%) and 42 of 42 on 56.
Where it is unknown, `MEMECOIN_COIN_AGE_UNKNOWN_PAIR_CHECK` (on) already decides the Solana floor from
the mint just before signing, from a lookup started in the background at classification.

**The h1 pro-rating with an unknown age** gives share 1.0, the full hourly floor. Measured: of 173
fills whose pair was 2 to 60 minutes old (Solana 103, 4663 44, 56 24, 5042 2; 14 days), 0 had an
h1 volume between the pro-rated floor and the full floor. An unknown age would have refused none
of them.

**A free lower bound on mint age.** Our own feed's earliest event for the mint happened after the
mint existed, so `decision time - first feed event` is a lower bound on the mint's age. It clears
the 120 s floor on 114 of 317 Solana fills (36%). The other 203 would need the mint lookup.

**X1, the exit sweep.**
- Open funded positions right now: 0 (flat book). Opened in the last 7 days: 289. Of those, 280
  are exact-policy (`exact_price_relevant`, all `follow-30-50-v2`/`-v4`); the 9 that are not are
  all `manual-unmanaged-v1` on Solana.
- `MEMECOIN_EXACT_QUOTE_ALWAYS` is on, so the demand gate returns `always_exact` before it reads
  the mid (`memecoin_quote_demand.py:98`).
- Where the sweep's prices come from (`scripts/memecoin_runner_loop.py`): `fastlane_poll` passes
  the fast lane's map, which is X2 (the DexScreener batch plus Solana swap marks), and X1's own
  batch runs only when that map is empty; `periodic_sweep` and `hot_pull_fallback` run X1's batch;
  `fastlane_wake` and the focus pass use no DexScreener at all. X2's batch also supplies the quote
  asset's USD price (SOL, USDC) that every Solana swap mark is priced with
  (`memecoin_fastlane.py:687-697`).
- For an exact-policy position the DexScreener mid still feeds, in the sweep:
  1. the WO-4 outcome filter: a failed exact quote becomes an outcome only when `display_mid > 0`
     (`memecoin_real_runner.py:5255`). Without the mid, neither the unsellable miss counter
     (`MEMECOIN_REAL_UNSELLABLE_SWEEPS=4`) nor the per-provider quote-infra Telegram alerts advance;
  2. market cap for operator mcap targets: the provider `market_cap`, `entry_ratio x mid`,
     `fdv_supply x mid`, and the FDV supply pin itself (`pin_fdv_supply`, Solana only,
     `fdv / price` from the snapshot);
  3. display: the nominal peak, `last_quote`, the audit rows' `display_price`;
  4. the 4663 curve fallback and the spike quarantine, which run on the map.
  Rail DECISIONS on exact-policy positions never read it (`decision_mid` is exact, the last exact
  price within 900 s, or none).

**E13, the market-data GET on the fill path** (`market_data_ms`, funded fills, 7 days):

| chain, lane | n | p50 | p90 |
|---|---|---|---|
| Solana J7 | 105 | 234 ms | 516 ms |
| Solana, no lane label | 29 | 218 ms | 281 ms |
| 56 J7 | 21 | 260 ms | 365 ms |
| 4663 J7 (DexScreener `robinhood`; corrected 2026-09-29, first read as GeckoTerminal) | 57 | 286 ms | 1,111 ms |
| Solana slot lane (no GET) | 26 | 2 ms | 6 ms |

E1, E2, E6 and E7 read the same response, so no consumer set in this phase can skip the GET: the
latency saved in phase A is **0 ms**. The table is what a full removal would take off, less
whatever part of the Solana prereads and early quote, which ride the GET's window, then becomes
the critical path. That split is not measured here.

## Design

### The switch

`MEMECOIN_DEXSCREENER_OFF` = comma-separated consumer ids, read at call time
(`cfg.dexscreener_off(consumer, env=None)`); an unknown id warns once. Empty = today, byte-identical.
Phase A recognises `E3`, `E5`, `X1` (and `X2` if Alpha includes it, below).

- An ENTRY consumer (E3, E5) shares the one market-data response with E1, E2, E6 and E7, so its switch
  lives in the consumer: it ignores its field and takes its own-data branch. The GET stays.
- An own-READ consumer (X1) skips its call. F1's funded batch functions gain a `consumer=` tag; a
  call whose consumer is off returns `{}` without HTTP and counts `dexscreener_suppressed[consumer]`.
  A call site the build missed then shows up as a count, never as a silent DexScreener read.
- Every decision records its source (`age_source`, `display_source`), so cohorts never pool across
  a flip.

### E3: switch built, NOT flipped

Off means the display chase does not run and the executor's exact-quote chase decides alone. On the
evidence above that admits about 13 Solana coins in 14 days, whose route price fell a median 23% in
five minutes. **Recommendation: build the switch, keep E3 on, and re-read at n >= 30** (about a
month at this rate), or flip it now with that number accepted. The operator decides.

A replacement that keeps the signal on our own data would be "how far the price moved in the last
~30 s", from our tape, against the source price. The Solana tape binds pools on demand, so its
coverage at decision time is unknown. Measure before proposing it; not in this build.

### E5: the mint's age

Off means `pair_age_s` from DexScreener is ignored everywhere it decides, and the age is the mint's:
1. **Lower bound first, free:** `decision time - earliest memecoin_events row for (network, mint)`,
   the same indexed read the first-seen chase already makes (`first_seen_buy_price`), extended to
   return `created_at`. At or above the floor it admits with no lookup.
2. **Otherwise the mint lookup** (`memecoin_coin_age.warm`), started in the BACKGROUND at
   classification and read from its cache just before signing: the existing unknown-pair presign
   path (`coin_age_presign_check`), which today is Solana-only. The build extends it to EVM, whose
   evidence is our volume tape's earliest tick (an indexed read, no RPC). Without that extension,
   E5 off on 56/8453 would stop the coin-age floor there, because every pair age would read unknown
   and unknown admits.
3. **The h1 pro-rating** reads the same age: the lower bound or a cached mint age when one exists at
   classification, else unknown (full floor). Mint age >= pair age, so the share can only rise and
   the floor only tighten. Measured cost above: 0 of 173 fills.
4. **Lifecycle** and `source_token_age_s` read the same age, with `age_source` = `dex_pair`,
   `feed_first_seen`, `mint` or null.
5. `MEMECOIN_REAL_MAX_PAIR_AGE_S` stays 0; if it is ever set, it reads the mint age under E5 off.

Load, an estimate: Solana attempts that reach classification run about 877 a day (7 days), and 64%
of fills needed the lookup, so about 560 extra `getTransactionsForAddress` calls a day on the watcher
key (`ONCHAIN_WATCH_HELIUS_RPC_URL`), a detection key. Its plan is not recorded here; the Helius free
tier the research key uses is 10 RPS and 1M credits a month. Check the watcher key's budget before the
flip; a 429 leaves the mint age unknown and admits, as today.

### X1: our own marks in the sweep

Off means the sweep's own DexScreener batch is not called. Its map is built from our marks:
- **Solana:** the swap-mark cache's `fresh_price_map`, priced with a quote-asset USD that is not
  DexScreener: USDC-quoted pools at 1.0; WSOL at a Binance SOLUSDT read on a 60 s cache OWNED by the
  swap-mark cache object (it already holds a lock; no module global). A failed read drops the WSOL
  marks for that pass: unknown, never a stale rate.
- **4663:** the tape marks (`memecoin_evm_tape_marks.tape_marks`), display only, anchored on our fill.
- **Anything else** (56, 8453, an unbound Solana position): no mark, so the sweep displays the exact
  preview (`mid = exact_mid`), exactly as it does today when DexScreener returns nothing.

The four DexScreener roles, replaced:
1. **WO-4 outcome filter:** "priceable" becomes `display_mid > 0 OR a resolved exact price within
   _LAST_EXACT_MAX_AGE_S (900 s)`. Without this, X1 off silences the unsellable counter and the
   quote-infra alerts on exactly the chains that lose their mid. A position that never priced stays
   out, as a dead chart does today.
2. **Market cap:** `entry_ratio x (own mark or exact price)`, which is already the sweep's fallback
   (`_mcap_at_price(pos, exact_mid)`). The Solana FDV pin moves to an on-chain total-supply read,
   once per position, in the sweep (off the fill path) through the chain's existing read seam. Until
   a position is pinned, its operator target reads the entry-ratio basis, which sat 20% off on coins
   whose provider cap was circulating (Hogdog, 2026-09-25).
3. **Display:** from the own map or the exact price, labelled `display_source`.
4. **Curve fallback and spike quarantine** run on the own map unchanged.

**X2 is the same decision, and I recommend it in this build.** With X1 off and X2 on, the main
`fastlane_poll` path still hands the sweep DexScreener mids, and the Solana swap marks still price
off DexScreener's SOL. X1 off alone moves only the fallback lanes. The X2 change is the same map:
the fast lane's refresh builds its marks from the swap-mark cache with the Binance-priced quote USD
instead of the DexScreener batch. Alpha decides.

**Manual positions** (`manual-unmanaged-v1`, 9 in 7 days) are the only ones whose decision mid can be
the display mid. Under X1 off it is the own mark, else the exact price, else none. Before the build,
I check whether any rail runs on that policy at all; if none does, this is display only.

## Edge cases (CLAUDE.md rule 5)

1. **Money path: yes.** E3 changes admission (measured above); E5 moves the coin-age floor's and the
   h1 floor's input; X1 moves the WO-4 counters, the quote-infra alerts, the mcap-target input and
   the manual positions' decision mid. Each consumer is its own id, default empty.
2. **Fill speed.** E3 off removes one Jupiter quote from the would-refuse branch only; admitted fills
   are unchanged. E5: the lower bound is the read the entry already makes; the Solana lookup is in
   the background (0 ms wait; 1 of 2 traced evidence reads waited 53 ms, on the old refusal-only
   path); the EVM tape read gets a measured p50/p90 in the build and goes to the background if its
   p90 exceeds 5 ms. X1 is on the exit path only. E13 saved: 0 ms this phase.
3. **Concurrency.** The switch is a call-time environment read. The SOL/USD cache lives on the
   swap-mark cache object under its lock. The mint-age cache is `memecoin_coin_age`'s existing
   locked dict. No new module global.
4. **Absent, zero, unknown.** Unknown pair and mint age admit the coin-age floor (a profitability
   filter, today's contract) and give the full h1 floor. A missing own mark is absent, never 0; the
   exact price stands in. A 0 or negative price is never a mark.
5. **Dependencies.** Helius (watcher key): timeout or 429 leaves the mint age unknown. Tape DB
   unreadable: unknown. Binance: failure drops the WSOL marks for the pass. Total-supply read: failure
   leaves the position unpinned (entry-ratio basis), retried next sweep.
6. **Units and identity.** Mint age >= pair age (a graduated coin's pair is younger). `created_at`
   is our host's receipt time: a lower bound on the mint's age is all it claims. Tape marks are spot
   anchored on our average fill, about 6 points above the exact route on 66384. Swap marks are in
   quote units times the quote asset's USD.
7. **The sample.** A flip changes cohorts; `age_source` and `display_source` go on every decision and
   no read pools across a flip. The E3 numbers are 22 captures at $20.
8. **Rollback.** Remove the id; `.env` changes reach the runner at its next restart through the
   pinned watchdog. Half-applied (a new runner with an old `.env`): nothing changes.

## Check left behind

`tests/test_memecoin_dexscreener_off.py`, each test proven red first:
- the switch parses ids, warns on unknown ones, and an empty value leaves every consumer as today;
- E3 off: an entry whose display gap is over the cap reaches the executor; E3 on: refused as today;
- E5 off, Solana: a pair-age-known young coin with a feed first-seen at or above the floor admits with
  no lookup; one below it with a mint younger than the floor is refused before signing; unknown admits;
- E5 off, EVM: the same through the tape evidence;
- X1 off: the sweep makes no DexScreener call (a batch function that raises); a failed exact quote on a
  position priced within 900 s is still counted by WO-4 and still reaches the quote-infra alert; an
  operator mcap target is reached on the exact price; the suppressed-call counter records a call made
  with the X1 tag.

## Decisions for Alpha (and the operator)

1. E3: keep on and re-read at n >= 30 (recommended), or flip now with the -23% at 300 s number accepted.
2. X2 in the same build (recommended), or X1 alone.
3. The watcher key's budget for about 560 extra lookups a day before E5 flips on Solana.

## As built (2026-09-29, Alpha's decisions on 59f5645ac)

Decisions: E3 stays on (its id exists and is inert until someone names it); X1 and X2 ship together,
default off, activated only after a coverage read; E5 is deferred with the market-data GET itself (it
would save 0 ms and cost about 560 watcher-key lookups a day), so no EVM tape mint-age path.

Built:
- `cfg.dexscreener_off(consumer)`: `MEMECOIN_DEXSCREENER_OFF` ids E3, X1, X2, read at call time. E5 and
  any other id are ignored and named once in a warning.
- F1 `dexscreener_stats_batch_keyed(..., consumer=)`: every call counts under its consumer
  ("untagged" when it names none); a switched-off consumer gets `{}` with no request and counts as
  suppressed. The runner heartbeat carries both counters (`dexscreener`).
- X2 (`memecoin_fastlane._batched_mid_snapshot`): off means no DexScreener mids, and the swap marks'
  quote-asset USD comes from `memecoin_live_marks.fresh_quote_usd` (below: this replaced a Binance
  module before merge). Tagged `consumer="X2"` when on.
- X1 (`memecoin_real_runner._sweep_price_map`): off means the sweep's own map is the swap-mark cache's
  decision map priced in our USD, for the held positions only; every other chain decides and displays
  on the exact preview, as it does today when DexScreener has no row. Tagged `consumer="X1"` when on.
  The 4663 tape marks stay display-only (their own contract) and are not put in the decision map.
- WO-4: with X1 or X2 off, a failed exact quote also counts on a position whose exact price resolved
  within 900 s (`_LAST_EXACT_MAX_AGE_S`). With both switches empty the filter is today's, byte for
  byte, so the build changes nothing until an id is named.
- The FDV supply pin is NOT moved: operator mcap targets numbered 0 created and 0 `mcap_target` exits in
  14 days, and the sweep's existing fallback (entry ratio x price) applies when no pin exists.

Activation read (`scripts/research/own_mark_coverage.py`, 7 days to 2026-09-29 00:00Z; the share of
sweep rows, while a funded position was open, whose recorded price was ours; N = 5 s, the lanes' own
freshness bound):

| chain | positions | sweep rows | ours today | ours with our USD only | DexScreener |
|---|---|---|---|---|---|
| 4663 | 61 | 8,643 | 92.6% | 92.6% | 7.1% |
| 56 | 23 | 2,269 | 94.5% | 94.5% | 5.5% |
| 5042 | 2 | 252 | 90.5% | 90.5% | 9.5% |
| 8453 | 2 | 6,784 | 48.3% | 48.3% | 51.7% |
| solana | 196 | 13,128 | 52.3% | **25.2%** | 47.7% |

On Solana, 82 of the 196 positions (42%) were bound to a pool quoted in a token we cannot price in USD
ourselves (tokenized stocks, JTO and others), and 38 of the 114 WSOL/USDC-quoted ones never had a fresh
swap mark. With X2 off those positions display, and count WO-4 misses, on the exact preview alone.
Exact-policy rails are unaffected either way. Pricing those quote tokens from their own WSOL/USDC pools
would lift the Solana number; it is not built. Rerun the script over a live day before any flip.

### Correction: X1 on 4663 is not a no-op (Beta's R3, 2026-09-29)

DexScreener prices 4663, so the sweep's own map carries 4663 rows today on the X1 lanes (7.1% of real
4663 sweep rows in the coverage read). With `X1:4663` off every 4663 position is absent from that map,
and `_launchpad_curve_price_fallback` (`MEMECOIN_EVM_LAUNCHPAD_CURVE_ENABLED` on) then makes one SERIAL
`mark_price_entry` read on the funded 4663 RPC per 4663 position per X1-lane sweep, on the main loop.
`X1:56` has no such fallback. Recommended to Alpha: activate 56 first; for 4663, skip the curve fallback
on a chain whose X1 is off before switching it (exact-policy decisions never read that price).

### Per chain (Alpha, 2026-09-29, on adc2c45fd)

`MEMECOIN_DEXSCREENER_OFF` entries take a chain: `X1:4663` switches one consumer off on one chain, a
bare `X1` on every chain; chain names are canonicalized (`bsc` = 56, `base` = 8453); an unknown chain
or consumer is ignored and named once in a warning. Every consumer reads its own chain: F1 drops only
the switched-off chains' keys from a request (suppressed counts are `consumer:chain`), the fast lane
and the sweep split their maps per chain, WO-4 widens "priceable" only on a switched-off chain, and E3
reads the event's chain.

**X2 is Solana-only in effect.** The fast lane's provider mid covers Solana alone (`mid_scope`); EVM
positions are priced in that lane by their exact previews. So `X2:4663` and `X2:56` change nothing,
and the DexScreener rows the coverage read found on 4663 and 56 come from X1 (the periodic sweep, the
hot pull fallback, and a fast-lane pass whose map was empty).

Activation (Alpha): X1 and X2 on 4663 and 56 only, after review: `MEMECOIN_DEXSCREENER_OFF=
X1:4663,X2:4663,X1:56,X2:56`. Not 8453 (48.3% own) and not Solana (25.2% with our own USD) until the
Solana quote assets are priced from their own pools (the next spec).

### Gamma's review of adc2c45fd, folded in

- M1 and L1 were fixed in b31533972 on a Binance module, which was then removed (next section); the
  replacement keeps both properties: no I/O on the caller's thread, and a sanity band.
- L2: the consumer counters cover `dexscreener_stats_batch_keyed` only (X1, X2, other batch callers as
  "untagged"). The entry's single-mint GET, the web, the paper lab and the capture reads are not counted;
  the docstring says so.
- INFO, manual-unmanaged-v1: every rail of that policy is zero and mirror is off, so adding any rail
  through an override makes the position exact-policy (`exact_price_relevant` counts a TP or a stop when
  mirror is off). A manual position whose decision mid can be the display mid therefore has no rail that
  can fire; the mid feeds display, the nominal peak and `last_quote`, plus an operator mcap target, of
  which there were 0 in 14 days.
- With `X1:4663,X2:4663,X1:56,X2:56` no Solana consumer is off, so no quote-asset USD is asked for at
  all. A test pins it.

### Quote-asset USD: the live marks' own source replaces Binance (Alpha, option a, 2026-09-29)

I missed an existing source when I built the Binance module: `memecoin_live_marks.onchain_quote_usd()`
(2026-09-27, for the live display marks) returns `{quote_mint: (usd, fetched_at)}` for SOL (the slot
lane's Jupiter SOL->USDC prefetch, every 10 s), USDC (1.0) and every other quote asset (Jupiter on the
capture key, every 10 s, `ExoticQuoteRefresher`). X1 and X2 now read it through
`fresh_quote_usd(quote_mints)`, and `memecoin_own_quote_usd.py` is deleted:
- CACHE ONLY: two reference reads; the writers compute their price first and hold their lock only for
  the swap, so no lock is held across a network call. A test holds both writer locks and the read still
  returns at once; another fails any urlopen.
- FAIL CLOSED PER ASSET: older than `MAX_QUOTE_USD_AGE_S` (60 s), undated (the USDC constant excepted),
  non-finite, not positive, SOL outside 20-2,000 USD, or any other asset at or above 10,000,000 USD:
  absent, and `fresh_price_map` drops that asset's marks.
- NO FUNDED SEAM IS ADDED: the adapter makes no request. Its SOL comes from the slot lane's prefetch,
  which already calls the funded Jupiter path for the lane's own funded work every 10 s (a display copy
  it has published since 2026-09-27); the other assets come from a plain GET on the capture key
  (`jupiter_display_quote`), never the funded seam. The capture key's load is unchanged: the refresher
  runs for the display whatever X1/X2 do.
- SAME PROCESS: the slot lane and the refresher run in the RUNNER (`scripts/memecoin_runner_loop.py`,
  the refresher only while `MEMECOIN_LIVE_MARKS_PUBLISH` is on, as it is), the process that runs X1/X2.
  With the slot lane down SOL ages out within 60 s and SOL-quoted marks go absent; with live marks off,
  the other quote assets are absent.
- This covers the tokenized-stock and JTO quote assets that the "25.2% with our own USD" line excluded,
  so the Solana re-measure is `scripts/research/live_mark_coverage.py`, which samples this same source
  while funded Solana positions are open.

### Before X1 goes off on Solana: pin the FDV supply from the entry's own mint read (Beta's R2, 2026-09-29)

Alpha gave GO on 2026-09-29 (operator decisions delegated; operator mcap targets fired 0 times in 14
days, so the money exposure is small). Built as described; see "As built" at the end of this section.

**The gap.** `pin_fdv_supply` pins a Solana position's total supply once, from a snapshot that carries
DexScreener's `fdv` and `price` (`fdv_supply_from_snapshot`). It runs twice: at entry, after the fill,
on the entry's DexScreener `st`; and in the sweep, on the X1 map row. The X1 row from our own swap
marks (`SolanaSwapMarks.fresh_price_map`) carries only price and source. So with `X1:solana` off, the
sweep never pins, and a position whose entry `st` had no fdv stays on the `entry_ratio` basis for its
whole life. Operator mcap targets on that position then read the provider's circulating basis. That is
the 20% error the pin was built to remove (Hogdog 64977).

**Measured (read-only, 2026-09-29 00:40Z).** The population is the 130 funded Solana positions opened
since 2026-09-25 (all closed). The mint supplies come from one `getMultipleAccounts` pass (2 calls) on
the research RPC, taken with no real Solana position open.
- 102 of 130 are pinned. 28 are not, and all 28 mints read a positive supply today.
- On 61 of the 102, the pinned supply equals today's on-chain mint supply within 1%.
- On the other 41, the pinned supply is 1.1% to 30.8% ABOVE today's mint supply. It is never below.
  99 of the 102 mints now hold under 1e9 tokens, and 34 of the 61 agreeing pins are exactly 1e9. That
  pattern fits holders burning supply after our entry: a burn only lowers supply. It does not show that
  the mint read at entry would disagree with DexScreener.
- NOT measured:
  - how many of the 102 were pinned at entry and how many by the sweep (the ledger keeps no pin time);
  - whether the mint supply AT ENTRY equals DexScreener's fdv/price at entry.

**What the entry already has.** On a Solana entry the prereads read the mint account beside the market
GET: token safety is on, and `MEMECOIN_SOLANA_BAND_MCAP_BASIS=onchain`. A successful read is kept on
the event as `mint_account_read` (`_settle_early_mint_read`, `_onchain_band_mcap`), so the leg's
safety gate judges that same read. A failed, raised or unfinished read is never kept; the gate then
reads fresh and keeps nothing. So in most entries the post-fill pin can use a supply that costs zero
extra calls.

**Proposed change.**
1. Add a pure `fdv_supply_from_mint_read(read)`. It returns `supply / 10**decimals` from a
   `(value, None)` token-safety read, with the same guards as `_onchain_source_mcap`:
   - integer supply > 0;
   - integer, non-bool decimals in 0..18;
   - a finite result.
2. In the entry hook, if `pin_fdv_supply(conn, pos, st)` returns None on an FDV chain, pin from
   `ev.mint_account_read` through the same `m.pin_position_fdv_supply` (`WHERE fdv_supply IS NULL`).
3. DexScreener stays first. Every position that pins today pins to the same number, and only the gap
   rows change.
4. Measurement only: when both sources exist, log one `key=value` line with the position,
   `dex_supply`, `mint_supply` and their ratio. When the mint read is absent, log `mint_read=absent`.
   After 30 Solana entries, the line answers both unmeasured points above.
5. Beta's other idea, one cached `getTokenSupply` off the sweep path for a position with no mint read,
   waits until the absent count says it is needed.

**Edge cases (CLAUDE.md rule 5).**
1. **Money path: yes.** `fdv_supply` sets the mcap an operator target fires on, in both the sweep and
   the exact path (`_mcap_at_price`). Today 28 of 130 positions stay on the entry-ratio basis; with the
   change most would move to the FDV basis. Auto templates do not change: they read `entry_mcap` or the
   provider basis, not the pin.
2. **Fill speed:** no change. The pin runs after the fill and after `set_position_identity`, before the
   entry alert. The fallback reads an attribute already on the event and makes no network call. A gap
   row adds one SQLite UPDATE, the same statement a pinned row already runs today. The canary gives
   p50/p90 for the post-fill stretch before and after.
3. **Concurrency:** `mint_account_read` is written on the entry thread before the leg runs, and read
   once after the fill. The UPDATE is first-writer-wins, so a sweep pin (while X1 is on) and the entry
   pin cannot overwrite each other.
4. **Absent, zero, unknown:** a failed or unfinished read is absent, so no pin (today's basis). A zero
   supply, or decimals out of range or boolean, also means no pin. No path writes a 0.
5. **Dependency failures:** the read's failures are already typed by the safety gate and never kept. A
   wrong answer is possible only if the read is for another mint. `pre.mint_read(ev.mint)` is keyed by
   this event's mint, and the new test pins that.
6. **Units:** raw integer supply over `10**decimals` gives whole tokens. DexScreener's fdv/price is in
   whole tokens too (a pump coin reads 1e9 on both). A test pins a 6-decimal and a 9-decimal mint.
7. **Sample:** the 130 above are our own entries, so the share describes our book, not the market.
   Today's mint supply is read AFTER those entries, and burns bias it low.
8. **Rollback:** revert the commit. Pins already written stay, because a pin is once-only by design,
   so those positions keep the FDV basis. Nothing is half-applied: one process writes the pin.

**Check to leave behind.** A pytest drives the entry hook three ways:
- `st` with no fdv plus a good mint read: `fdv_supply` is pinned to the mint supply.
- `st` with an fdv: the DexScreener supply wins.
- a failed mint read: no pin.
Prove it red by removing the fallback call.

**Not in scope, for the operator.** A pin keeps the ENTRY-time supply. On 41 of 102 pinned coins the
supply has since fallen 1.1% to 30.8%. If GMGN's FDV uses the current supply (unverified), a target
set later from GMGN sits off by that much. That is a property of the existing pin, not of this change.

**As built (2026-09-29).**
- `fdv_supply_from_mint_read(read)`: a pure function with the guards listed above.
- `pin_fdv_supply(conn, pos, st, *, fallback_supply=None)`: `fallback_supply` is pinned only when
  `st` implies no supply. The sweep's call is unchanged.
- `pin_entry_fdv_supply(conn, pos, st, mint_read)`: the post-fill call in `real_entry_hook`, with
  `getattr(ev, "mint_account_read", None)`.
- The mint fallback applies only while `dexscreener_off("X1", chain)`. With X1 on, today's behaviour
  stays exact: the later sweep pin from DexScreener's fdv/price, and no write-once mint pin at entry
  that would pre-empt it (Gamma C1, Alpha 2026-09-29). It turns on with the Solana X1 flip.
- The line is `fdv pin sources at=<host UTC ISO> pos= dex_supply= mint_supply= ratio=
  mint_read=ok|absent|unusable`. It is logged whatever X1 is, at WARNING because the runner keeps no
  INFO handler, and it carries its own stamp because the WARNING lines have none. Read it with `/usr/bin/grep -a "fdv pin
  sources" logs/memecoin_runner.log`.
- Tests in `tests/test_memecoin_fdv_mcap_basis.py`: 11 read cases, DexScreener first, the mint
  fallback, absent, Solana only, and the hook passing the event's read.
- A test asserts no open transaction when `trace.finish` runs (Gamma T1, the 09-25 trace-loss trap).
- Eight mutants all fail:
  - the hook passes no read;
  - the mint read goes first;
  - no decimals guard;
  - the read's failure is ignored;
  - no post-fill commit;
  - the fallback ignores X1;
  - the fallback is never used;
  - no stamp.
