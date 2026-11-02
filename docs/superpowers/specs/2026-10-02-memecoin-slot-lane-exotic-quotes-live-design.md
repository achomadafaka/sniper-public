# Slot lane: exotic-quote buys go live (CPMM, LaunchLab, pump AMM) — design delta

## Amendment, 2026-10-19: LaunchLab packet preparation

Under `MEMECOIN_SLOT_LANE_EXOTIC_REQUOTE=oversize`, initial live LaunchLab preparation
locally checks the full transaction including its S/S+1 guards before publishing the leg.
An exact packet overflow permits one `maxAccounts=20` replacement on the same bounded
worker, with the replacement's price and admission checked again. A provider failure,
another build refusal or a second overflow refuses without another repair.

Retry and publication must remain inside the original 500 ms receipt clock. Signature,
source slot and clip are preserved; no `requote_attempts` age exemption or pending-hold
extension is created. Shadow, disabled and other-venue preparation retain their paths.
No provider call, lock or retry is added to the lane thread. Evidence and runnable check:
[bound incident audit](../../audits/2026-10-19-solana-slot-expiry.md).

2026-10-09 diagnostic amendment: a claimed worker refusal or expiry publishes its terminal
skip reason before waking the on-chain lane. Duplicate or late results cannot replace it.
The existing timing drain persists that explanation; immutable attempt rows remain unchanged.
See `docs/audits/2026-10-09-otto-terminal-reason.md`.

2026-10-17 round-trip amendment: `MEMECOIN_SLOT_LANE_EXOTIC_ROUND_TRIP` defaults on.
Before signing a non-wSOL PumpSwap entry, model the exact approved clip against the source's
post-trade reserves. The existing off-path warmer sizes a reverse ExactIn quote from the retained
buy or verified cached graduation pool state. Forward and reverse evidence must share a fresh
generation, quote mint and clip. The current modeled return must cover the exact reverse input;
use its USDC minimum unchanged and value surplus at zero. USDC pools need no reverse quote.
Missing, stale or invalid evidence, or cost above the frozen position's `entry_roundtrip_cap_pct`,
durably aborts the unsigned reservation. A cold refusal still seeds the warmer for later events.
The aggregate warm HTTP budget stays unchanged; fire adds no quote or RPC request. Disabling the
knob restores the previous path. These models do not establish future fills or same-slot advantage.

2026-10-17 native-exit amendment: the optional `MEMECOIN_SOLANA_NATIVE_EXIT` defaults off.
A bound PumpSwap sell and, when needed, a quote-token-to-USDC tail form one unsigned transaction
that must simulate successfully before entering the existing claim, signing, send and receipt
path. Pool/vault/mint identity and exact amounts are checked; unsupported quote-token extensions
keep the existing route. Only a positive cached divergence hint adds a native build to an existing
Jupiter quote; missing or stale native evidence keeps that route. A missing Jupiter quote still
tries native. Selection requires the simulated executable floor. Native stop holds
require fresh complete reserves, ordered slots and verified pool identity. Unknown movement and
reconnects invalidate that witness; a confirmed operator market-cap stop retains precedence.

Status: PROPOSED, 2026-10-02 (Lambda). Operator GO 12:0xZ via Alpha: "start that build for the alt
tokens the slot lane does not support, and switch Raydium to live". This doc is the design delta that
order needs before code. It extends `2026-09-27-slot-lane-any-venue-design.md` §7 (one source buy, at
most one funded attempt) and §8.1 (quote-residue sweep), and replaces option (c) of
`2026-10-01-memecoin-slot-lane-routed-venues-design.md` §3 for exotic quotes. D1 (quote-token
inventory) stays open and is not built here.

"Exotic quote" means a coin pool quoted in anything but SOL or USDC: xStocks, pre-IPO tokens, ZEC,
launchpad tokens.

## 1. What is missed (7 days to 2026-10-02 ~12Z)

Gamma's coverage (logs and ledger):
- The slot lane made 44% of Solana fills. The misses by notional were:
  - never-saw 21%;
  - CPMM quote unsupported 12%;
  - fallback after a failed send 9%.
- The misses by count were:
  - LaunchLab, 5 fills;
  - a gate disagreement between lanes, 8 fills;
  - an unknown pool, 4;
  - a pump pool quoted in something other than SOL, 2.
- Refusal lines over the same 7 days: `slot_lane_cpmm_quote_not_supported` 150,
  `slot_lane_launchlab_quote_not_supported` 23, `slot_lane_quote_not_sol` 48 (the lane's own pump
  AMM buys) and `pump_quote_not_sol` 10 (routed).
- Those are refusals BEFORE the gates. Fills that went to the on-chain lane after the hand-off:
  - CPMM 19 (+4.1%);
  - LaunchLab 5 (-15.2%);
  - pump quote not SOL 2.
- Today's three misses all filled on the on-chain lane at S+3, S+3 and S+4.

**Pump AMM census** (Lambda, research Helius key, flat book, 25 calls;
`~/.cache/lambda/quote_census/`):
- 54 refusals, every one decoded: 48 the lane's own, 6 routed.
- Their pools are all pump.fun migrations (Pool.creator == the pool-authority PDA). So no new
  coin-safety question arises: the coins are pump mints.
- 11 quote mints:

| quote mint | refusals | program | decimals | quote_facts |
|---|---|---|---|---|
| pumpCmXq…pump | 15 | Token-2022 (transferHook, no hook program) | 6 | (6, 0, 0) |
| ZEC A7bdiYdS | 7 | Token | 8 | (8, 0, 0) |
| CARDSccU | 6 | Token | 6 | (6, 0, 0) |
| SKHYhSju | 6 | Token-2022 (xStock extensions) | 6 | (6, 0, 0) |
| SPCXxcqX | 6 | Token-2022 (xStock extensions) | 6 | (6, 0, 0) |
| USDC | 4 | Token | 6 | (6, 0, 0) |
| XspzcW1P | 4 | Token-2022 (xStock extensions) | 8 | (8, 0, 0) |
| Xsc9qvGR | 3 | Token-2022 (xStock extensions) | 8 | (8, 0, 0) |
| XsoCS1Tf, he1ius, Ai66…pump | 1 each | mixed | | all movable |

- Every quote mint passes `raydium_cpmm_buy.quote_facts`, with no transfer fee on any of them.
- USDC is 4 of 54. The rest need a quote leg.

**LaunchLab exotic shadow** (on-demand leg, paper key, 2026-09-27 to 10-02):
- 11 builds simulated: 7 OK and 4 failed.
- All four failures are slippage-class:
  - 2 are LaunchLab ExceededSlippage 6004 at the buy (the price cap held);
  - 2 are Jupiter 6001 at the leg.
- Pump curve buys quoted in non-SOL: 0 refusals in these logs. Out of scope.

## 2. Design: one path for an exotic quote, on any venue

Today an exotic-quote buy is refused `*_quote_not_supported` on the lane thread. It is then offered to
the on-demand leg worker (`raydium_launchlab_buy.OnDemandLegWorker`, ≤ 2 in flight, never the lane
thread). The worker:
- quotes a USDC→quote Jupiter leg on the paper key;
- prices the quote from that leg;
- runs admission with it, builds and simulates once.

Live keeps that shape and adds one hop back to the lane thread:

1. **Lane thread**, unchanged. The buy is refused `*_quote_not_supported` and offered to the worker.
   `offer` never blocks. A venue whose exotic-live knob is off stays shadow, exactly as today.
2. **Worker thread**:
   - If the venue's exotic live cap for this hour is spent, it hands off before any call.
   - Otherwise it quotes the leg on the FUNDED Jupiter seam (role `slot_leg`, the call the lane's
     SOL leg uses), not the paper key: one GET /quote and one POST /swap-instructions.
   - The quote's USD price is the leg's own rate less its price impact:
     `leg USDC / leg outAmount × (1 − priceImpactPct)` (Gamma LOW1). Both rates are logged. The
     pool's LP fee stays in, so the canary reads the gap.
   - On a live venue the worker re-reads the quote mint when its facts are older than 300 s. It
     refuses a mint that `quote_facts` rejects, and any non-zero transfer fee (Gamma LOW2).
   - It runs admission with that quote on its own read-only handle, as the shadow worker does.
   - On a refusal, a leg failure or a 429 it puts an `ExoticRefusal` on the lane queue (§4). On a pass
     it puts `(buy, leg, quote)` there. The worker writes nothing itself; every fallback is
     written on the lane thread, on the routed handoff's own connection.
3. **Lane thread**:
   - `handle` sees the attached leg and runs today's live path:
     - signal age (≤ 0.5 s from receipt, which bounds the whole detour);
     - admission again, authoritative, on the lane's own connection;
     - the venue's exotic cap;
     - the wallet lock (non-blocking);
     - the reservation (USDC funding);
     - `build_fn(…, leg=, quote=)`;
     - sign and send through `slot_execute_fn`.
   - Nothing after the reservation is new.

A USDC-quoted pump AMM pool needs no leg. It takes the CPMM USDC path on the lane thread: the clip
in USDC is spent straight from the wallet's USDC account, at a quote USD of 1.0.

**How the price cap converts** (the existing `capped_min_out`, unchanged):
- The source's post-trade price in quote per coin comes from the pool event, as today. It is
  multiplied by the leg's quote USD (1.0 for USDC) to give `source_price_usd`.
- The ticket cost is the leg's USDC input (the clip; for USDC, the clip itself).
- price floor = coins that cost buys at `source_price_usd × (1 + cap/100)`.
- output floor = expected out × (1 − cap/100). The expected out is computed from the quote amount
  actually spent (the leg's MINIMUM out) on the event's post-trade reserves.
- min_out = the larger of the two. A floor above expected refuses before the reservation.
- The leg and the buy are one atomic transaction. If either fails its minimum, both fail and only
  the fee is lost.

**What the pump AMM builder gains** (`pumpswap_buy.py`):
- Any quote mint whose `quote_facts` pass, on that quote's token program (Token or Token-2022; the
  26-account list already takes the quote program per pool).
- The spend is the leg's minimum out (or the USDC clip), paid from the user's quote ATA. The leg's
  setup instructions create that ATA.
- There is no wSOL wrap or close, because the quote ATA is not closed. Residue (leg out above its
  minimum, ≤ the leg slippage) stays there for the residue sweep.
- The SOL path is unchanged.
- Admission (`amm_market_facts`) prices any quote as above. Its migration and supply rules are
  unchanged; every census pool passes them.

**Residue:** every filled exotic buy calls `memecoin_quote_residue.record_fill_residue`.
`MEMECOIN_QUOTE_RESIDUE_SWEEP=1` is already in `.env`. The sweep sells only residue it recorded,
never a holding.

## 3. Knobs and caps (each default off)

### 2026-10-06 amendment: reuse the pool learner's fresh quote-mint read

MultiStonks (`11fc12692ad249c1bf17c89ef2d48f52`) spent 382 ms preparing its exotic quote leg.
The first live exotic quote redundantly read its mint after CPMM pool learning had already read
that same mint in the metadata batch. CPMM and LaunchLab now publish that raw mint value together
with the monotonic **start** of its read. The shared worker validates the value from that exact
published snapshot and reuses it only within the existing 300-second quote-safety window. It never
pairs an older validation tuple with a newer snapshot's timestamp. Missing, expired, future,
non-numeric or non-finite timestamps keep the existing RPC read; an absent or unsafe raw value
refuses. Pump AMM continues its existing mint re-read because its cache does not publish these
validated raw facts. No new synchronous read, lock, retry, funding asset, or price-cap change.

The failed receipt showed 1,457,615,073,234 available atomic tokens versus the signed minimum
1,667,203,006,781. That floor correctly enforced the 30% source-price cap: the full ticket would
have paid about 49% above its reference. The entry reverted atomically and paid 993,000 lamports
in fees. Removing the duplicate read improves setup time; it does not promise that this historical
buy would have landed within its cap. In eight bounded quote-safety-step samples on the observation
RPC, old p50/p90 were 25.19/53.05 ms, while fresh reuse was 0.0052/0.0143 ms with zero duplicate
reads. These are isolated step timings, not organic fill latency. Rollback is the prior pinned
release; no persisted state or schema migration is introduced.

- `MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES`: a comma list of `raydium_cpmm`, `raydium_launchlab` and
  `pump_amm`.
  - A venue on it sends exotic-quote buys live through the funded leg.
  - Empty means today: shadow on the paper key.
  - It needs the venue's own mode live (`MEMECOIN_SLOT_LANE_CPMM` / `_LAUNCHLAB` / the lane itself).
- **USDC-quoted pump AMM** is live with `pump_amm` on that list too. It is one knob per venue, not
  per quote.
- **Caps:** exotic sends count per venue under their own cap key `<venue>:exotic` in
  `MEMECOIN_SLOT_LANE_VENUE_LIVE_MAX_PER_HOUR_BY_VENUE`. The default is 2 an hour (Gamma's canary),
  so an exotic canary cannot starve the venue's SOL/USDC sends.
- **Rollback:** take the venue off the list and restart. Exotic buys go back to shadow and the
  routed row is written first again. No schema change.

## 4. One source buy, at most one funded attempt, never zero (Gamma M1/M2)

- **PendingLegs** (`memecoin_slot_lane`): an in-process registry, signature → (buy, deadline).
  - The deadline is the buy's receipt + `MAX_SIGNAL_AGE_S` (0.5 s).
  - While an entry is held, `memecoin_onchain_lane.slot_lane_outcome` answers `in_flight` (no
    attempt row exists until the lane reserves). The on-chain lane therefore defers instead of
    racing the reservation, and waits at most the deadline.
- **Exactly one party resolves an entry**, by claiming it under the registry lock:
  - the worker's refusal (`ExoticRefusal` → the lane writes the fallback);
  - the lane's expiry sweep, every 50 ms between queue items;
  - the lane thread after the re-entry has run, by which time the attempt row exists.
  - Each one sets the on-chain lane's wake.
- **A release with no attempt row** (Gamma MED-1) is recorded for 120 s, the outcome lookback.
  `slot_lane_outcome` answers `failed` for it, so a detection the on-chain lane deferred on the hold
  gets its ONE fallback at once. Without the marker it waited until `slot_fallback_expired`, with
  zero attempts. An attempt row still decides first. An exception inside the re-entry writes the
  fallback before the release.
- **Each detection deferred on a hold** logs `slot_lane_exotic_hold sig=… hold_left_ms=…` on the
  on-chain lane's line, so the canary reads how often the watcher's row lands inside the hold.
- **A full worker or a failed offer** writes the routed row at once.
- **An item that expired while still queued** reaches the lane with no claim left. It sends
  nothing, and it is counted `aged_out`.
- **A restart** drops the registry. The watcher's own later row still reaches the on-chain lane.

- **Routed buys** (CPMM, LaunchLab, routed pump):
  - When the venue is exotic-live and the conversion names an exotic quote, `route_routed_buy`
    writes no routed row (as for a live SOL/USDC CPMM buy).
  - Every refusal before the reservation writes the routed row then (`_routed_fallback`).
  - That covers the worker's refusals too: cap, leg failure, 429, admission, a stale signal on
    re-entry.
- **The lane's own pump AMM buys** (no routed row) are seen by the on-chain watcher and J7
  independently, as today. A refusal hands nothing off, exactly as the `slot_lane_quote_not_sol`
  refusal does today.
- From the reservation on, the attempt row decides through `slot_lane_outcome`. The on-chain lane
  defers to `slotlane:<sig>` and one-pending-reservation-per-coin holds.
- **Race:** the on-chain lane may reserve the coin first while the leg is out.
  - The lane's reservation then refuses as a duplicate, so the leg's two calls are wasted and no
    second attempt is made.
  - The watcher's row normally lands after confirmation (S+1 or later), past the leg (p90 228-325
    ms).

## 5. Edge cases (CLAUDE.md rule 5)

1. **Money path: yes.**
   - New funded sends on three venues and a new funding shape for pump AMM.
   - Every lane gate, the reservation, the funded contract, the chase cap and the exit rails are
     unchanged.
   - Exotic quote tokens are held only as leg residue (≤ the leg slippage of a clip), and the
     sweep returns it.
2. **Speed: nothing new for any buy the lane sends today.**
   - SOL and USDC paths are unchanged. The worker never runs on the lane thread. The re-entry is
     one queue item.
   - The added latency is the leg, on the buys that go to the on-chain lane today, at S+3/S+4.
     - Paper key, measured from the shadow lines: LaunchLab p50 141 / p90 228 ms (n=21); CPMM
       p50 159 / p90 325 ms (n=142).
     - Signal to leg done: LaunchLab p50 217 / p90 340 ms.
   - The funded key's leg time is measured live (`leg_ms` on each row) and read per venue against
     the on-chain fallback it replaces.
   - A routed buy refused after its leg reaches the on-chain lane late by that leg time. The 0.5 s
     signal-age gate bounds it, as §7 of the any-venue spec bounds every lane refusal.
3. **Concurrency.**
   - The worker holds no ledger connection that writes. Admission runs on its read-only handle,
     and the authoritative admission and the reservation run on the lane thread.
   - The leg dict is built on the worker and only read after the hand-over. The lane thread never
     mutates it.
   - Cap counters live on the lane thread only. The worker's pre-leg cap check reads a snapshot
     (an over-count wastes at most 2 calls; the lane's own check refuses).
4. **Absent versus zero versus unknown.**
   - A leg with outAmount 0, a missing minimum or no USD rate refuses.
   - `quote_facts` None (an unmovable quote) refuses.
   - Unknown quote decimals refuse.
   - A USDC balance unread is the existing stale-input refusal.
5. **Dependencies.**
   - **A 429 or 401 on the funded Jupiter seam.** The seam's own handling cools its hosts. The
     worker also stops legs for `LEG_RATE_LIMIT_BACKOFF_S` and hands the buy off. Load is about
     66 calls/day: the 7-day exotic refusal count of 231 is about 33 a day, at 2 calls each. That
     is about 0.08% of the SOL leg's ~1 call/s.
   - **A leg timeout** hands off.
   - **A send that fails on slippage** costs its fee. Gamma's bar (§6) caps how many may fail.
6. **Units and identity.**
   - Quote atoms use the quote's own decimals (6 or 8 in the census; ZEC 8).
   - Token-2022 quote transfer fees come from `quote_facts` (0 for all 11 census mints, still
     applied).
   - The leg's output mint must equal the pool's quote mint (`leg_wrong_quote`, as CPMM).
   - The quote ATA uses the quote's own token program.
7. **The sample.** The counts are refusals under today's roster and gates, so the share moves with
   the roster. The per-venue read uses only units after each venue's go-live.
8. **Rollback.**
   - Per venue, as §3.
   - A restart mid-attempt finishes under the reservation and pending rules.
   - Residue already held stays recorded for the sweep.

**Gamma LOW3, declined for E1.** The price-free gates (kill switch, armed) already run before the
leg. Cooldown and one-per-coin need the reservation. The mcap ceiling and the source floors need a
price, and a routed-average pre-check would be a second, slightly different gate. The cost is about
16 legs a day spent on later refusals, 32 funded calls. Revisit if the funded 429 count rises.

**The funded seam (Gamma M3).**
- `_jup_call` takes `_INFLIGHT` (a Condition) only to bump a counter, for microseconds and never
  across HTTP. Funded calls never wait on each other, and paper calls yield to funded.
- No slot or semaphore sits on that path.
- A shadow venue keeps the paper key, and a test pins it.

## 6. Pre-registration (Gamma)

Gamma's B2 bar applies per venue: a 2 an hour canary, the five stops, and the read at 10 lane units
or 14 days. Gamma adds two conditions before go-live:
- (a) Of live SENDS, at least 80% land filled rather than failing the price ceiling.
- (b) Each live fill's fill-timing row shows S+1 or S+2, judged by percent return per wallet, never
  pooled dollars.

## 6b. Lifting the exotic cap (Alpha, 2026-10-02)

Per venue, `<venue>:exotic` goes from 2 an hour to the venue's normal cap after Gamma's read, when
all of these hold:
- **5 clean live exotic sends** (the existing CANARY_SENDS; exotic sends get their own five canary
  lines). Each one landed, has a source slot and a landing slot, and its receipt fill and
  accounting read without anomaly.
- **At least 4 of the 5 filled** rather than failing the price ceiling (Gamma's 80%).
- **No ordering violation.** No second funded attempt (reservation or send) for the same source
  signature or coin by any lane while the exotic attempt was pending or in flight. That is, one
  `slotlane:<sig>` per origin, and the on-chain lane's outcome deferred.
- **No residue strand.** Every filled exotic send wrote its `memecoin_quote_residue` row, each row
  was swept or sits under the sweep minimum within 24 h, and no `foreign_balance` block appears on
  those mints.
- **Aged-out re-entries are ≤ 10%** of offered exotic buys. Each re-entry logs its signal age, its
  queue wait and the leg time, and the heartbeat carries `slot_lane.exotic`.
- If any one fails, the cap stays at 2 an hour and the failed condition is reported.

Measured before live, on the paper key (receipt to leg and build done, before the requeue):
- CPMM: n=142, p50 163 / p90 333 / max 504 ms. 1 is over 500 ms and 4 are over 400 ms.
- LaunchLab: n=21, p50 218 / p90 340 / max 501 ms. 1 is over 500 ms.

## 7. Build order (each a commit, default off, then Gamma)

1. **E1.** The worker's live mode and the lane re-entry, wired for CPMM and LaunchLab exotic quotes.
   This includes the funded seam, the per-venue `:exotic` cap, the routed fallback on every worker
   refusal, residue recording at the fill, `leg_ms` on the trace, and tests.
2. **E2.** pump AMM any quote: the USDC direct path, and exotic quotes through E1's worker, routed
   and lane-own. Builder and admission tests on captured frames of the census pools.

**E2 as built** (lambda/exotic-e2):
- **Admission.** `amm_market_facts` prices a non-SOL pool only with `pump_amm` listed and the lane
  live. A USDC pool prices at $1 in the quote's own decimals. Any other quote prices with the
  leg's `(decimals, USD)`; without a leg the buy refuses `slot_lane_pump_quote_not_supported`,
  which is the worker's cue. Off the list the refusal is today's `slot_lane_quote_not_sol`.
- **Pool cache.** It keeps `quote_decimals`; an unreadable mint leaves it unknown.
- **Builder.**
  - USDC: the clip is spent from the USDC account, with no leg and no wSOL close.
  - Any other quote: the leg's minimum, from the quote's own (Token or Token-2022) account.
  - The builder refuses native SOL funding, a wrong-quote leg, a stale leg and unknown quote
    decimals.
- **Lane.**
  - A lane-own PumpSwap buy takes its pool's quote mint after admission.
  - Every non-SOL pump buy, USDC included, counts and logs canary lines under `pump_amm:exotic`.
  - It never reserves native SOL. The lane's SOL-buffer preference, if applied, would have
    reserved SOL for a USDC pool; the builder refused that send, and a test pins the fix.
- **Simulated on chain state** (research key, flat book; from the funded wallet's public key,
  sigVerify off, never sent):
  - A USDC pool: no error, 107,786 CU, 1,052 bytes.
  - An SPCX pool (Token-2022 xStock quote) with a real paper-key Jupiter leg: the leg ran, and
    the buy failed only on `BuySlippageBelowMinBaseAmountOut` (6040). That is the price cap
    holding against a source price hours old, and it shows the accounts and the quote transfer are
    right.

**E3 — Meteora DAMM v2, any quote** (lambda/exotic-e3; operator yes 2026-10-02, "fill regardless
of USDC or SOL quote"):
- **Pricing.** DAMM prices from the source's routed average whatever the pool's quote, so
  admission needs no quote price. The min-out stays the price floor at that average, with the
  cost being the clip (USDC) or the leg's USDC (exotic).
- **Funding.** USDC spends the clip from the USDC account. Any other quote spends the on-demand
  leg's minimum from its own Token or Token-2022 account. A non-SOL pool closes no wSOL.
- **Pool cache.** It keeps the quote's facts and program from the mint read it already makes.
- **Gate.** "meteora_damm_v2" must be listed, then DAMM's own mode applies. DAMM is shadow today,
  so E3 builds and simulates in shadow (exotic quotes through the paper-key worker) until DAMM
  goes live, which stays gated on the B2 shadow read and a per-venue cap of 2.
- **Cap and canary.** Non-SOL DAMM sends count under `meteora_damm_v2:exotic`.
- **USDC funding (Alpha).** The reservation never approves more than spendable USDC
  (`real_trade_size`), so a low balance shrinks the clip and the build spends exactly the approved
  amount. No balance, or an unread one, refuses before any reservation. A DAMM build refuses
  native SOL funding outright. The executor books the USDC actually spent from the receipt's
  balance delta (`receipt_entry_fill`), as for CPMM USDC.
- **Not yet done:** a chain simulation of a real non-SOL DAMM pool. The book was open at build
  time, so it waits for a flat book.

Out of scope here: never-saw launchpads, gate parity, the unknown-pool miss and D1. Those are
Alpha's items 2 and 3, each with its own delta.


## Amendment 2026-10-09: Alpha handoff completion

Every PendingLegs release owned by SlotLane uses the lane monotonic clock: exotic requeue, graduation hand-back, rejected offer and worker refusal. Hold and release TTL stay in the same domain; claim-once and policy/fallback distinctions are unchanged.

Evidence and limits: `docs/audits/2026-10-09-codex-alpha-handoff-closeout.md`.


## Amendment 2026-10-11: bounded preparation before first use

The [sub200 entry contract](2026-10-11-solana-sub200-entry-design.md) adds bounded preparation from verified streamed or miss-learned pool facts. Pump AMM now publishes parsed quote-mint evidence with its original read-start timestamp, permitting the existing safety verdict to reuse that exact fresh snapshot. Successful unsafe evidence invalidates an older passing cache; no synchronous read is added to an entry.

The warmer keeps at most two exact candidates and four actual Jupiter HTTP requests per1.5seconds on its own client. Cached legs bind payer, pool/program identity, exact clip, query, slippage and original quote age. Missing or mismatched evidence keeps the existing cold/refusal path. Preparation inventory comes from the existing sizing expressions; it grants no admission or sizing authority. Activation requires verified provider headroom.
