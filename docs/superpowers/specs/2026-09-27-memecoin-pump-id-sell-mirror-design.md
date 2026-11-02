# Mirror-on-sell for the registry owners (the pump:-id wallets included)

Status: **BUILT 2026-09-27 by operator order** ("Build the mirror exits for safety"; then "We
need to sell without waiting for any confirmation, that would be too late"). It was first parked:
the evidence at the end puts a sell mirror at break-even at best, and the operator ordered it built
knowing that. The design as built is the next section; the parked design below it is history.

## 2026-10-18 amendment: full source liquidation only

The operator supersedes the fractional and unknown-fires rules below: mirror only full sells,
never half sells or derisking. `MEMECOIN_REAL_MIRROR_SIZE_MIN_FRACTION=1.0` is now the default.
Values below 1.0 explicitly retain the historical fractional policy for compatibility.

Full-only Solana mirrors require a signed sale plus a complete `getTokenAccountsByOwner` mint
snapshot with zero total atomic balance and a context slot at or after that sale. The existing
bounded background resolver performs this read; neither the entry path nor mirror consumer
performs network reads. Missing, stale, malformed or failed results hold. An emptied ATA, transfer,
decoded inventory, rounded USD zero, cumulative partial sells or V4 first-sell policy cannot prove
liquidation. A newer confirmed observation may strengthen incomplete processed evidence without
duplicating an exit. Existing claims and unsigned retry ownership remain authoritative.

EVM mirrors compare exact positive atomic sold/pre equality and explicit zero post balance, keeping
that proof in the durable pending audit. The watcher balance is block-final, so a source that finishes
selling later in the same block is already flat when this evidence arrives. Price/time/manual exits
remain independent. Paper policies retain their frozen contracts. Activation requires a reviewed
live threshold change from 0.30 to 1.0 and the pinned runtime cutover; no database backup is required.

## Original fractional implementation (historical unless explicitly configured below 1.0)

- **Detection.** The slot lane's processed socket decodes the owner's own sells
  (`memecoin_slot_signal.decode_owner_sells`: pump curve TradeEvent with is_buy=false, PumpSwap
  SellEvent) and hands each to the mirror from the socket thread, which only enqueues. It also taps
  every own buy it decodes, which is the source's known holding. The confirmed watcher writes each
  sell with its exact share to `onchain_wallet_sells`: a backup for sells the socket missed, never a
  gate. Deduplicated by source signature.
- **Share, with no read on the decision path.** PumpSwap: `base_amount_in / user_base_token_reserves`
  (the event's pre-sell balance, verified exact on a real sell). Curve: sold / decoded holding; an
  unknown holding, or one smaller than the sale, is a full dump and FIRES (operator order: the error
  goes toward selling). Watcher: `(pre - post) / pre` from the transaction's balances. A balance
  read runs after the exit, for the audit row only.
- **Rule.** Cumulative since our position opened, `1 - prod(1 - f)`; at or above
  `MEMECOIN_REAL_MIRROR_SIZE_MIN_FRACTION` (0.30) a FULL exit, below it hold.
- **Execution.** Its own thread and connection, so a mirror waits only for the funded exit lock,
  never for a fastlane pass. The position's ordinary exit_fn with reason `mirror_source_sell`:
  claim, exact whole-balance route, sign, send, with none of the price rails' debounce or confirm
  windows. Skipped: tracking-paused, `mirror_disabled`, a close already in flight, a sell dated
  before our position opened.
- **Measurement.** One `memecoin_exit_trigger_audit` row per decision (lane `source_sell_mirror`)
  with the source signature and slot, share and basis, receipt and decision times, lock wait. Send
  time is the position's `close_submitted_at`. One exact exit quote of the sold quantity 600 s
  after each mirror close (`memecoin_post_tp_schedules/results`, horizon 600), measurement only.
- **Flags.** `MEMECOIN_SOURCE_SELL_MIRROR_ENABLED=1` (default, operator order),
  `MEMECOIN_SOURCE_SELL_MIRROR_SHADOW=0`. Heartbeat: `fastlane.source_sell_mirror` counts
  seen / matched / mirrored / held_below / no_position / skipped / errors per path.

## The parked design (history)

Author: Lambda (design, 2026-09-27 03:52Z). Evidence: Gamma (chain reads). Join and verdict:
Lambda and Alpha.

## Question

Alpha asked for a mirror-on-sell path for the wallets whose buys reach us only through the slot
lane and the on-chain watcher (untaxxable, hashbergers, notdecu, TheMisterTurtle: `pump:`-id
sources). The follow-up question: how much earlier would a mirror exit have fired than the stop or
vol_death exits of 2026-09-26/27?

## Why feeding a sell event is not enough

Solana is in `MEMECOIN_REAL_MIRROR_SIZE_CHAINS`, so `real_mirror_on_sell` takes
`_size_aware_mirror`. That function sizes the sell from `m.source_trades_for_coin`, which reads
`memecoin_events` only. These wallets have no events there, because J7 relays none of them. The
history would hold only the sell itself, so base=0, size_unknown, and the mirror HOLDs. A sell has
to carry its own exact sold share.

## Smallest path (design only)

1. **Decode.** `memecoin_slot_signal` already receives these owners' processed logs. Add the
   owner's own SELLs: the pump curve TradeEvent with is_buy=false and user==owner (same
   discriminator as buys), and the PumpSwap SellEvent with user==owner.
2. **Size exactly.**
   - PumpSwap SellEvent carries base_amount_in and user_base_token_reserves, so
     share = base_in / user_base_reserves. It must first be verified on a real sell frame that
     user_base_token_reserves is the PRE-sell balance.
   - The curve TradeEvent carries no user balance. Read the owner's token account once at
     processed, right after the sell: share = sold / (post + sold). That is one read per sell,
     off the signal path.
   - A sell that cannot be sized stays unknown and holds, as today.
3. **Hand over.** The prefetch/handoff thread writes one row per sell to a new small table
   (signature unique; source user, mint, their price, their size in USD, sold share, detected
   time). The curve mint comes from the event; the AMM mint comes from the pool cache, which knows
   every pool we bought on. The on-chain lane worker (it already owns real_hook, 1 s poll) builds
   an `OnchainEv(side='sell', sold_fraction=...)` and calls real_hook, which routes to
   `real_mirror_on_sell`.
4. **One money-path change.** `_size_aware_mirror` uses the event's exact sold_fraction when its
   trade history has no buys for this wallet and coin. The share stays cumulative across sells
   since our fill: 1 - prod(1 - f_i). If the owner BOUGHT that mint again after our fill, the share
   is unknown and the mirror holds. `MEMECOIN_REAL_MIRROR_SIZE_MIN_FRACTION` (0.30) still decides.

## Rule-5 map (as sent)

- **Identity:** the event's user must equal the subscribed owner, the same match as buys.
  `real_mirror_on_sell` then requires an OPEN real position from that source on that mint,
  otherwise it does nothing.
- **Latency:** acting on PROCESSED, about 1-2 s after the sell (prefetch tick up to 0.5 s + lane
  poll up to 1 s), then exit_fn. A processed sell that forks away would exit our bag early; that is
  risk-reducing and rare. Waiting for confirmed would add about 0.5-1 s.
- **Duplicates:** the signature is unique in the table. A later J7 or watcher sell for the same
  mint finds the position closed or claimed and does nothing. Today there is no second source.
- **Partial sells:** the exact per-sell share, cumulative since our fill, against the 0.30 minimum.
  A 20% trim holds; 20% + 20% (36%) exits.
- **Executor sells:** the match is event.user == owner. A router or executor selling the owner's
  tokens is not matched, so no mirror; the rails still guard the position.
- **Contract:** these positions carry `policy.mirror_on_sell=false`, with `mirror_sell` among the
  invalidations. But the Solana size-aware overlay runs BEFORE the contract check (an operator
  overlay on the frozen contract for the listed chains), so for Solana only per-position
  `mirror_disabled` blocks it. The operator should know the overlay is what enables it.
- **Speed:** nothing is added to the buy or fill path. The sell decode is a few microseconds on the
  signal thread; the insert is on the prefetch thread.
- **Absent vs zero:** an unknown share is never 0; it holds. A failed balance read is unknown and
  holds.
- **Rollback:** revert; the table is inert.
- **Not handled:** executor sells; sells by a wallet whose buy we never saw (no position, so moot);
  a buy between two sells (unknown, holds).

## Evidence (2026-09-27, Gamma's chain reads; join by Lambda)

Sample: funded positions from `pump:`-id sources since 2026-09-26 21:00Z. The source's sell was
found by reading the source's own token account for the mint (the earlier owner-address search was
blind for notdecu). "Mirror" is the decoded chain spot 2 s (and 4 s) after the source's sell,
against our entry. "Realized" is our realized PnL over entry notional, fees and impact included.
Our time is `closed_at`, the close finalization; the take-profit send time is
`closed_at - exit_timing_json.total_ms`.

Losers (our stop or vol_death):

| Position | Source | Our exit | Source sold after its buy | Our exit vs source sell | Realized | Spot +2 s | Mirror saves |
|---|---|---|---|---|---|---|---|
| 65891 | untaxxable | b_stop | 31 s, 100% | +4 s | -35.6% | -33.8% | $2.36 |
| 65922 | untaxxable | b_stop | 3 s, 100% | +87 s | -35.6% | -28.2% | $10.99 |
| 65929 | untaxxable | b_stop | 39 s, 100% | +12 s | -37.4% | -20.4% | $24.35 |
| 65860 | themisterturtle | vol_death | 8 s, 100% | +32 min | -25.5% | -16.7% | $14.67 |
| 65923 | hashbergers | vol_death | 51 s, 100% | +2.7 min | -11.6% | -10.5% | $1.29 |
| 65904 | hashbergers | vol_death | 656 s | source sold AFTER our close | -30.6% | n/a | $0 |
| 65906 | hashbergers | vol_death | 388 s | source sold AFTER our close | -15.9% | n/a | $0 |

Winners (one full `real_tp` each):

| Position | Source | Realized | Source sell vs our TP send | Mirror effect |
|---|---|---|---|---|
| 65813 | notdecu | +23.1% | sold 60%, 47.9 s AFTER our TP | none |
| 65826 | untaxxable | +28.8% | sold 64%, 4.5 s AFTER our TP send | none |
| 65862 | untaxxable | +23.0% | sold 100% 15 s after its buy, 72 s BEFORE our TP; spot +2 s -22.0%, +4 s -5.5% | turns +$40.29 into -$38.5 / -$9.6: costs $78.8 / $49.9 |
| 65924 | notdecu | +37.9% | sold 100%, 253 s AFTER our TP | none |

Net over the 6 positions where the source sold before our exit send (65860, 65862, 65891, 65922,
65923, 65929): mirror minus realized = **-$25.1 at +2 s, +$4.8 at +4 s**. Per position at +2 s:
+15.2, -78.8, +2.4, +11.0, +1.4, +23.7.

## Verdict

Break-even at best. One reversal (65862) erases every loser's saving. A rule that mirrors only
while our position is under water does not rescue it: 65862 was at -22% spot when its source
sold. The estimate is biased toward the mirror (chain spot with no exit fee or impact, against a
realized figure that includes ours), so the true figure is worse than shown. n is small: 6
decision rows.

Closed after parking (Gamma, 2026-09-27): all 7 loser sells were re-read with the token-account
search and gave identical signatures and spots, so the net stands. The non-`pump:`-id sources
(Roveryaps 65900 and 65675, 0xdetweiler 65856) never sold during our hold or for 10 minutes after,
so a mirror would not have fired on them. 65568 was not reported. Reopen only on a larger sample
with executable (quoted) exit prices, not spot.
