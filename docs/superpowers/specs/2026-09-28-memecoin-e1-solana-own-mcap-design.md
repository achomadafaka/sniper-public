# E1 on Solana: our own mcap when the on-chain basis cannot price a coin (rule-5 spec)

Date: 2026-09-28. Author: Beta. Order: operator item 2 via Alpha ("no dex anywhere"); inventory row E1
of docs/audits/2026-09-28-dexscreener-removal-inventory.md. Solana only. No code before Alpha's go.

## What the ledger says (read-only, 2026-09-28 23:3xZ)

Solana classifications since the on-chain band basis went live (first `onchain_source_fill`
2026-09-27T09:00:54Z), by `classification_json.facts.mcap_basis` (written by
`classify_funded_entry` from the runner's stage):

- 1,340 classifications, 551 with a basis: history 265, event 177, onchain_source_fill 105,
  **dexscreener_fallback 3**, route 1, **assumed 0**. The other 789 carry no basis (the slot lane
  classifies from its own curve facts, and refusals decided before the mcap stages).
- `history` and `event` are the feed's own mcap (Fomo/J7 events and same-mint history), not
  DexScreener, so they are outside E1.
- The 3 DexScreener fallbacks:
  - 2 onchain_watch buys with no source price (09-27 11:38Z, mcap $54,018, micro);
  - 1 follow_activity buy at $118.195 a token whose on-chain read did not price it (09-28 15:33Z,
    mcap $1.44B, refused over the ceiling).
- The "21 of 540" figure counted 18 plain `dexscreener` rows from 04:48-05:22Z on 09-27, before the
  on-chain basis was live.
- No `assumed_nano` promotion has happened since the flag, so the fail-open corner is empty today.

## The stage order today (memecoin_real_runner.py ~:9380-9520, Solana)

1. The event's mcap (`event`); 2. same-mint feed history (`history`); 3. the DexScreener GET
(`stats_fn`) with `MEMECOIN_SOLANA_BAND_MCAP_BASIS=onchain`: source fill x on-chain supply
(`_onchain_band_mcap`, `onchain_source_fill`), else DexScreener's `market_cap`
(`dexscreener_fallback`); 4. LAST RESORT: `executable_mcap` (our Jupiter entry quote for
`MEMECOIN_TRADE_SIZE_USD` x `getTokenSupply`, `route`), reached only when stage 3 produced nothing;
5. `assumed_nano` (`MEMECOIN_REAL_MCAP_ASSUME_MICRO=1`): the $250k ceiling and the floor do not apply.

## Change

1. `MEMECOIN_SOLANA_DEX_MCAP_FALLBACK` (default `1` = today). At `0`, stage 3 on Solana never reads
   DexScreener's `market_cap`: when the on-chain basis fails, the code goes straight to stage 4,
   which already exists. No new stage, no new provider.
   It lives in the existing `dex_mcap_allowed(chain)` (today False on the DexScreener-blind chains),
   so one function still decides where DexScreener may set a band.
2. `executable_mcap` gains `supply=(supply, decimals)`. The runner passes the supply from the mint read
   the prereads already started beside the GET (`pre.mint_read`, the token-safety read), so the route
   stage makes ONE call, the Jupiter entry quote, instead of getTokenSupply plus the quote. With no
   finished read it reads the supply itself, as today.
3. `MEMECOIN_SOLANA_MCAP_UNRESOLVED` = `assume` (default, today) | `refuse`. At `refuse`, a Solana coin
   that every own stage failed to price is refused "mcap_unresolved" instead of banding
   `assumed_nano`. THIS is what keeps the removal from widening admission (edge case 4).

Activation, Alpha: `MEMECOIN_SOLANA_DEX_MCAP_FALLBACK=0` and `MEMECOIN_SOLANA_MCAP_UNRESOLVED=refuse`
together, then a runner restart.

Not in scope: the DexScreener GET itself still runs, because E2 (volume), pair age, liquidity and
names read it. E1 removal saves no time until those move too.

## Edge cases (CLAUDE.md rule 5)

1. **Money path: yes.** The band decides admission (ceiling, floor, reservable bands) and the exit
   policy.
2. **Fill speed.** Only the coins the on-chain basis cannot price reach the route stage: 3 of 551
   basis-carrying classifications (0.5%) since the flag, plus the 1 that already did. They pay one
   Jupiter entry quote on the fill path. The proxy, measured on 106 funded Solana entry quotes since
   09-27 09:00Z (`memecoin_entry_attempts.quote_ms`, the same `jupiter_entry_quote`): **p50 288 ms,
   p90 521 ms**, max 1,516 ms. Change 2 removes the getTokenSupply read from that stage. Every other
   classification pays nothing new.
   - Not the early quote: it does not start for an event with no mcap (`EQ.stats()["skipped"]
     == {"mcap_unknown": ...}`), which is exactly this cohort.
3. **Concurrency.** `executable_mcap` runs on the runner's own entry thread through the funded quote
   function it uses today; no global is added. The mint read is the prereads' own future, read only
   once it has finished.
4. **Absent vs zero vs unknown. Never widen.**
   - Today a coin the on-chain basis cannot price, but DexScreener can, gets a ceiling check. With
     the fallback off, the route stage must price it. If the route stage also fails, the knob decides:
     `assume` would band it `assumed_nano` and skip the ceiling, which is WIDER than today. So
     `refuse` ships with the fallback removal.
   - `refuse` is narrower than today only for a coin nothing prices at all, which today bands
     `assumed_nano`. That happened 0 times in 1,340 Solana classifications since the flag.
   - `executable_mcap` returns None on any failure (no supply, a quote without outAmount, an
     exception), never 0. An mcap of exactly 0 cannot occur (supply > 0 and tokens > 0 are required).
5. **Dependencies.** Jupiter 429, 401 or no route gives None, which refuses under `refuse`. The route
   stage uses the funded quote seam and so counts against the funded Jupiter budget, as it does
   today; at 0.5% of classifications that is a few calls a day.
6. **Units.** Supply and decimals come from ONE read (the mint read's `supply` and `decimals`), so the
   division cannot mix sources. USD = the clip in USDC (6 decimals) / tokens out. Calibrated in
   2026-08: `executable_mcap` / DexScreener 0.909-1.045 over 6 coins, so the $250k ceiling
   transfers to within about $12k.
7. **The sample.** n=3 fallbacks in 38 h; 2 of them lacked a source price (onchain_watch rows the
   watcher could not value). The forward read is the count of `route`, `mcap_unresolved` and
   `assumed` per day after activation, beside the band each coin would have had.
8. **Rollback.** `MEMECOIN_SOLANA_DEX_MCAP_FALLBACK=1` and `MEMECOIN_SOLANA_MCAP_UNRESOLVED=assume`,
   plus a restart. Half-applied: the fallback off with `assume` is the widening case, so the runner
   logs a warning at start when it sees that pair.

## Check left behind

`tests/test_memecoin_e1_solana_own_mcap.py`, red first:
- fallback 0: a coin with a DexScreener mcap and no on-chain price bands from the route stage, never
  from `market_cap`;
- fallback 0 + `refuse`: route None refuses "mcap_unresolved"; + `assume`: it bands `assumed_nano`;
- fallback 1: byte-identical to today (the same basis and band for the same inputs);
- the route stage takes supply and decimals from a finished mint read and makes no getTokenSupply
  call; with no finished read it makes exactly one;
- a mint read and a quote whose decimals disagree cannot occur: one read feeds both numbers.

## Review fixes (Gamma, Alpha's order, 2026-09-28)

- **M1.** A J7 shadow decision quotes nothing: `_route_band_mcap` returns before any quote, and the
  pre-existing `executable_mcap` last resort is skipped for it too (the funded Jupiter seam; a 429
  there cools the funded hosts). Shadow therefore records `mcap_unresolved` / `assumed` on the ~0.5% no
  other stage prices. This also applies with the knobs at their defaults.
- **L1.** Only an early quote bound to THIS trace skips the route stage. A retry's leftover object
  from an earlier attempt no longer forces a second quote.
- **L2.** The new stage records `mcap_basis = "route_clip"`; `route` stays the executable_mcap price,
  so the forward read can split them.
- **L3.** `MEMECOIN_SOLANA_DEX_MCAP_FALLBACK` turns off only on an explicit 0/false/no/off; a typo or
  an empty value keeps today's behaviour.
- **Residual, a later item:** a MANUAL Solana entry still bands from DexScreener's market_cap
  (`_manual_classification_context`, via `dex_mcap_allowed`). The check stays out of that function
  on purpose, because it also routes manual entries to the DEX-blind resolver.
