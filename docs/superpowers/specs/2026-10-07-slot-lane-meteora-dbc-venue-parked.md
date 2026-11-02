# Slot lane: Meteora DBC venue (UN-PARKED and built LIVE, 2026-10-07; DLMM stays parked)

Status: DBC UN-PARKED by operator order (2026-10-07, via Alpha: "address this slot lane failure";
then "we need that live no time for shadows"). Built live-capable behind `MEMECOIN_SLOT_LANE_DBC`
(default off) as `sniper/execution/meteora_dbc_buy.py`; see "The build" at the end. The tally below
is the original parking evidence. Original status: PARKED by the bar Alpha set (build the exotic-quote DBC venue only if xStock-quoted, no-hook
DBC buys that our gates would admit run at about 1 a day or more). Measured below that. Author
Lambda. Trigger: operator flag on position 71651 ELON (redemptionjizzy, 19:05:03Z), a routed buy
the slot lane handed off (`no_slot_venue`) that the on-chain lane filled ~2 s later at +19.3% vs
the source (mirror exit -$13.79).

## The tally (read-only)

Every routed slot-lane hand-off since `memecoin_slot_lane_skips` began (2026-10-02 12:06Z) to
2026-10-07 19:05Z: 110 rows (109 `no_slot_venue`, 1 `pump_quote_not_sol`), 5.3 days. Each source tx
read once (`getTransaction`, jsonParsed, `maxSupportedTransactionVersion: 1`, most are v1) on
MEMECOIN_RESEARCH_HELIUS_RPC_URL at 2/s; 110 of 110 read. A DBC coin leg = a DBC instruction with
at least 15 accounts. Outcome = the on-chain lane's attempts on the same source signature.

| DBC coin leg (35 of 110) | n | on-chain lane outcome |
|---|---|---|
| SOL-quoted, transfer-hook base (Swap2WithTransferHook) | 29 | 24 mcap over the $250k ceiling, 4 launchpad-gated (co-sign hook), 1 coin_too_young |
| SOL-quoted, no hook | 3 | 1 filled (67BHnptu, 10-02 20:07Z), 1 re-entry cooldown, 1 under the source-buy floor |
| exotic-quoted, no hook | 3 | 1 filled (ELON 2VbsYwA6, quote Xs3oZwbH), 1 exact quote +85% over the 35% cap (4NYezY9y, quote HiMSSzzw), 1 no route then +82% past entry (59j9ZS7N, quote wdysfTqU) |

Admitted by our gates (filled): 2 in 5.3 days, 1 of them exotic-quoted. Exotic-quoted no-hook DBC
buys our gates admit: ~0.2 a day. Below the 1-a-day bar, so no build.

The 29 transfer-hook coins are refused by our transfer-hook rule on both lanes (and most by other
gates first). That is deliberate and stays (Alpha).

## What a build would need (verified from real transactions)

- Program `dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN`. Instructions seen from aggregators:
  `swap` (`f8c69e91e17587c8`), `swap2` (`414b3f4ceb5b5b88`, args amount_0 u64, amount_1 u64,
  swap_mode u8; 0 = exact in, amount_1 = minimum out) and `swap2_with_transfer_hook`
  (`b75d992818e6c297`, the hook coins, extra hook accounts after the 15).
- The 15 accounts, in order: pool_authority `FhVo3mqL8PW5pH5U2CN4XE33DokiyZnUwuGpH2hmHLuM`
  (PDA), config, pool, input_token_account, output_token_account, base_vault, quote_vault,
  base_mint, quote_mint, payer, token_base_program, token_quote_program, referral (the program id
  for none), event_authority `8Ks12pbrD6PXxfty1hVQiE9sc289zgU1zHkvXhrSdriF` (PDA), program.
- The pool and config come from the frame's own `EvtSwap` (self-CPI;
  `sniper/research/memecoin_opportunity/bags_solana.py` decodes it: pool, config, direction,
  amounts, next_sqrt_price, fees).
- `VirtualPool` (424 bytes, discriminator `d5e005d16245775c`): config at offset 72, base_mint at
  136, base_vault at 168, quote_vault at 200 (checked on ELON's pool ETaY7dvA). `PoolConfig`
  (1048 bytes, `1a6c0e7b74e6812b`): quote_mint at offset 8. The only off-path reads would be the
  config (quote mint, curve) and the two mints (program, extensions).
- Exotic quotes (xStocks, Token-2022) need the USDC -> quote leg through the existing
  OnDemandLegWorker (the raydium_cpmm / launchlab ondemand interface:
  `build(buy, clip, snapshot, leg=, quote=)`), and min_out through `capped_min_out` as CPMM does.
- Refuse any transfer-hook base mint (covers the co-sign hooks `Bf7ecqFi` / `DMohCzuY` seen as
  launchpad-gated, and `COSIGN_HOOKS` in scripts/research/dbc_cosign_pricing.py).
- A curve that completes mid-flight reverts; it needs its own refusal before graduation handling.

## Re-open when

The tally reaches about 1 admitted exotic-quoted no-hook DBC buy a day. Re-run the same read over
the newer skips rows; the script shape is in the episode for this spec (2026-10-07).

## DLMM: parked by the same bar (Alpha, 2026-10-07)

The same 110 hand-offs, by coin leg (the program whose transfer paid the coin to the owner; the
owner is the wallet whose USDC fell and whose coin rose, since the fee payer is often a co-signer):

| coin leg | n | admitted by our gates (filled) | refusals |
|---|---|---|---|
| Meteora DBC | 34 | 1 | 25 mcap ceiling, 4 launchpad-gated, 1 ran past entry, 1 re-entry, 1 too young, 1 exact quote over cap |
| Meteora DLMM (alone or split) | 30 | 0 | 29 mcap ceiling, 1 h1 organic volume |
| DAMM v2 (alone) | 18 | 3 | 5 mcap, 5 price impact, 2 exact quote, 1 source floor, 1 slippage revert, 1 live mint authority |
| proVF4 router hop | 14 | 3 | 9 mcap, 1 exact quote, 1 too young |
| JUP hop | 6 | 0 | 6 mcap |
| Raydium LaunchLab | 5 | 1 | 2 source floor, 1 re-entry, 1 mcap |
| others (Tessera, BisonFi, CLMM+Whirlpool) | 3 | 0 | 3 mcap |

DLMM admitted 0 of 30 in 5.3 days, so no DLMM map or build. Across all 110 hand-offs the gates
refused 102: mcap over the $250k ceiling 78 (71%), price impact 5, exact quote over the cap 4,
launchpad-gated 4, source floor 3, then 1-2 each. 8 filled on the on-chain lane (~1.5 a day across
every venue): DAMM v2 3 (first-sight learning, live since 17:24Z, now covers these), proVF4 hops 3,
DBC 1, LaunchLab 1. A routed hand-off that our gates would admit is rare; the slot lane's missing
venues are not the main lever.

## The build (2026-10-07, Alpha GO on the edge map)

Scope: SOL-, USDC- and exotic-quoted DBC curves (Alpha (a)). Knobs: `MEMECOIN_SLOT_LANE_DBC=off|live`
(no shadow mode) plus `meteora_dbc` on `MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES` for USDC and exotic
quotes (Alpha (b)). Caps: `meteora_dbc` and `meteora_dbc:exotic` under
`MEMECOIN_SLOT_LANE_VENUE_LIVE_MAX_PER_HOUR_BY_VENUE`.

- Every DBC routed buy is learned by mint on the MissLearner's DBC thread, inside its 250 ms
  budget, on `ONCHAIN_WATCH_HELIUS_RPC_URL` (never the funded key). Reads: `getProgramAccounts`
  (dataSize 424, memcmp base_mint@136), then one `getMultipleAccounts` of [config, coin, quote
  vault]. The quote mint lives in the config, so the vault names it (cross-checked) and its program.
  An exotic quote's mint costs one more read, kept for QUOTE_RECHECK_S. Measured n=13 (research
  key): p50 112 / p90 153 / max 175 ms for the first two reads.
- The buy carries the learned facts (`SlotBuy.venue_event`, a `DbcPool`); no cache across buys,
  so each buy is judged on a read at most `FACTS_MAX_AGE_S` (1 s) old.
- The branch runs after pump, CPMM, LaunchLab and DAMM v2 in `_route_routed_buy`: zero added time
  on those paths. The frame marker is one `startswith` scan, only with the knob on.
- Refused: `TransferHookPool` curves and `ConfigWithTransferHook` configs (found on 8V2jTnUb: hooked
  coins use their own 424-byte pool type), any coin extension the builds do not cover, migrated,
  complete (`quote_reserve >= migration_quote_threshold`), rate-limiter pools (base fee mode 2), and
  at build a curve our amount plus the source's own estimate would complete (ExactIn past the end
  reverts with AmountLeftIsNotZero; the read may predate the source's buy).
- `swap2` ExactIn (mode 0), 15 accounts in the order above, referral = program id. amount_in = the
  leg's guaranteed quote (its min out); minimum out = the source-average price floor at the chase
  cap on the USDC the leg spends. The leg and swap are one transaction, so a revert costs the fee.
- Mainnet simulation (sigVerify off, nothing signed): the SOL curve C6W7GUJo err None, 43,871 CU;
  ELON on Xs3o (ETaY7dvA) err None, 39,723 CU. Negative control: an impossible floor reverts with
  ExceededSlippage (6002).
- Rollback: knob off. No builder, no marker, no learner; DBC buys hand off as before.
