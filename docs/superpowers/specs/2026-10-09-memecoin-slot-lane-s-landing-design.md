# Landing slot-lane buys in the source's own slot S, per opted-in wallet (frankdegods first)

**2026-10-14 evidence correction:** the local head and its notification age are a feed-head proxy.
A delayed stream can show S-1 after bank S has closed. Every inference below that those observations
identify "early" bank arrival, sends a fee can move, or a feasible fee-only go/no-go subset is withdrawn.
The <=80 ms bins remain descriptive notification timing. M1 now labels that proxy explicitly and
censors bank feasibility and fee movability; an independent bank-phase witness is required.
Recorded canonical landing positions and conditional reserve/fee arithmetic remain separate evidence.
Same-slot superiority over competing bots remains unproven.

Status: SPEC ONLY, for Gamma and the operator. Author: Beta, 2026-10-09. Order: Alpha (operator: "continue
with everything"). No runtime code; M1 is a research script. This changes the 2026-07-25 free-direct submission design
(`docs/superpowers/specs/2026-07-25-memecoin-free-direct-submission-design.md`: no tips; sender/jito/
fast_fanout fail startup) for a per-wallet opt-in list only, so it is the operator's decision.

## 1. The problem, measured

frankdegods (`source_user_id` aefe2ddd…, Solana wallet 498g1rVn…) made 10 slot-lane SENDS since 2026-09-26.
- 7 failed on max price (`BuySlippageBelowMinBaseAmountOut` 5, `ExceededSlippage` 1, Jupiter `0x1771` 1).
- 3 filled.
- We landed at S+1 to S+3 every time, never S.

Frank's funded Solana record (`memecoin_positions`, real, closed):
- 30 closes, +$477, so +$15.91 per close at an average notional of $110;
- 11 of them closed in under 60 s, +$414, so +$37.6 per close.

**Where the price went (M1, recorded reserves).**
- **Method.** Signer-window tape (`data/research_signal_windows_solana.db` `pool_swaps`). Our $110 clip is
  bought into the pool's recorded reserves at each position in S. The reference is the source's
  post-trade SPOT (the reserves after its swap), which is the slot lane's chase reference.
- **Why not `price`.** The tape's `price` column is each swap's own execution price, never a spot
  (Gamma). The first draft's table used it, so it overstated premiums against the spot, and its
  index gaps were an ordering of all the block's transactions, not a count of copiers.
- **Coverage.** 5 of the 7 failed sends have a tape row.

| send (UTC) | venue | our average vs the source spot, landing right after the source / after its 1st later swap / 2nd / at S's end | inside +30% |
|---|---|---|---|
| 10-02 17:09 | raydium_cpmm | +1.3% / +1.4% / +0.6% / +0.6% | every position |
| 10-04 06:38 | pump_amm | +2.2% / +107.2% / +119.7% / +130.0% | only right after the source |
| 10-04 06:42 | pump_amm | +2.8% / +232.9% / +247.8% / +247.8% | only right after the source |
| 10-05 21:36 | pump_amm | +81.1% / +90.4% / +102.6% / +106.3% | none (see the bias note) |
| 10-09 01:27 | pump_amm | +1.7% / +21.7% / +23.8% / +48.1% | up to the 2nd later swap |

- **Bias.** A PumpSwap pool's vault reserves omit its virtual quote. On pump_amm rows our own impact
  and the price moves therefore read LARGER than they are; 10-05's +81% at the first position is that.
- **The cap evidence (Gamma's grade, corrected):** caps of 35% and 50% fill 0 of 7; a 60% cap fills 1
  of 7; an uncapped fill at our real landing was positive at +60 s.
- **So landing in S rescues these buys only very early in S.** That means right after the source on two
  of them, which is ahead of every other buyer in the slot.

**Where we are today, measured.**
- **Our own landings** (tape, every Solana signal with our landing since 2026-10-01, n=143):

  | slot | landings |
  |---|---|
  | S | 3 |
  | S+1 | 57 |
  | S+2 | 32 |
  | S+3 | 14 |
  | S+4 | 16 |
  | S+5 or later | 21 |

- **Detection is late relative to S's leader for most sends (Gamma).**
  - The first draft read `slot_receipt.head_at_recv_minus_s == 0` (138 of 180 sends) as "we detect
    inside S". That reading is near-tautological: a processed stream from one node delivers S's
    transactions while that node is still replaying S. The age is also node-relative, because the
    slot notification fires when the node creates S's bank, after turbine lag.
  - The usable signal is receipt age into node-S against our landing slot:

    | receipt age into node-S | landed in S | S+1 | S+2+ |
    |---|---|---|---|
    | ≤ 30 ms | 4 | 21 | 1 |
    | 30–80 ms | 1 | 18 | 6 |
    | 80–150 ms | 0 | 23 | 7 |
    | > 150 ms | 0 | 37 | 20 |

  - All 5 S landings arrived ≤ 80 ms into node-S, and they fell at S%4 of 0 and 2.
  - About 81% of sends arrive past 30 ms, and even the earliest landed in S only 4 times in 26.
  - **A priority fee can help only the early-arriving sends.** M2 reports its S share inside the
    ≤ 30 ms and ≤ 80 ms bins.
- **What we pay.** Priority is a CU price of 3.8M µL/CU over 182k-400k CU, which is 0.0007-0.0015 SOL.
  The Sender A/B arm B adds a 5,000-lamport Helius Sender tip.
  - Lambda's A/B read since 10-02 (brain d71ef09e): Sender cut late S+2+ landings from 27% to 11%. The
    pre-registered score is S+0, and it is not expected to adopt.

## 2. Who lands in S, and what they pay (measured)

Source: `scripts/research/s_landing_census.py` (this branch). It ran 2026-10-09 on the research Helius key,
flat-book held, 41 calls: every transaction on the mint in S..S+1 for Frank's 10 sends, fee facts per
buyer, tips by recipient, and Jito's public explorer for bundle membership of every S buyer.

| | buyers after the source in S | buyers in S+1 |
|---|---|---|
| buys | 60 (43 signers; about 6 per send) | 161 (102 signers) |
| paid any known tip | 18 | 117 |
| tips by service | nozomi 10, helius_sender 4, jito 3, gmgn 2, astralane 2 | astralane 50, gmgn 36, helius_sender 33, nozomi 12, jito 8 |
| in a Jito bundle | 4 | (not asked) |
| paid per buy (priority + tips), p50 | 0.010 SOL ($1.10 at $110) | 0.0028 SOL ($0.30) |
| paid per buy, p90 | 0.150 SOL ($16.50) | 0.040 SOL ($4.40) |

- **The leader orders by fee PER CU, not by total lamports (Gamma):**

  | | CU price p50 | CU price p90 | CU limit p50 |
  |---|---|---|---|
  | S landers | 55.9M µL/CU (14.7× ours) | 588M | 150k |
  | S+1 landers | 4.0M µL/CU (about ours) | 100M | 229k |
  | ours | 3.8M µL/CU | | 182k-400k |

  The per-CU bid is what separates S landers from S+1 landers.
- **S landers mostly pay a high priority fee and no tip.** 42 of 60 tipped nothing.
- **What this does NOT show is causality.** An S lander's arrival path (a staked SWQoS connection, its own
  validator, colocation) is invisible on chain. FbJBKx landing in S 4 of 10 times with no tip does not
  show that priority alone moved it there.
- **Tip counts.** The eighth Jito tip account is 3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT, per Jito's
  getTipAccounts (Gamma). `copier_crowd_and_fees.py` carried a one-letter typo, fixed on this branch.
  Earlier tip counts that used it (incl. brain f8120a16) missed that account; this census did not.
- **The repeat S landers:**

  | signer | S landings | priority per buy | tip |
  |---|---|---|---|
  | FbJBKxCR… | 4 of 10 sends | about 0.012-0.013 SOL | none |
  | H4N4nXT7… | 3 of 10 | 0.010 SOL | none |
  | CiSEJeH8…, 77vDJ2vq…, CC3oFqcs…, 87sBaED2…, DeX37c6Y…, 3Um4qsYQ… | 2 each | | |

- **Top-level programs of S landers:**
  - PumpSwap direct, 39;
  - Jupiter, 8;
  - b1oomGGq…, 7 (Bloom's router, going by the vanity prefix);
  - akbotssn…, 3 (unidentified).
- **The extreme:** a 40 SOL buyer (5zouka…) paid an 11.9 SOL Jito tip in a bundle on 09-29.
- **Bundles land later.** This matches the 2026-09-27 finding (brain f8120a16) that bundled copiers land
  later than unbundled ones.

What this says about S-landing:
- The S landers do not mainly pay tips. They bid about 15 times our per-CU price and, by an arrival
  path we cannot see, reach S's leader early.
- Our detection is late for about 81% of sends. So a fee can only be tested on the early arrivals.
- The operator's 2026-10-08 decision (brain 073a87da: no Solana early-detection feed; "no option fills
  in-slot misses from this host") stands until the measurement in section 6 says otherwise.

## 3. What S-landing needs (the options, cheapest first)

| option | what it is | cost per landed buy | what is still unknown |
|---|---|---|---|
| A. Priority tier on today's path | the listed wallet's slot-lane buy signs a higher CU price; the same free-direct transport (custody RPC, official endpoints, local TPU sidecar) | the priority fee only, charged on every LANDED tx, failed ones included. At the S landers' p50 of 55.9M µL/CU over our 150-260k CU that is 0.008-0.015 SOL ($0.92-1.60); capped by a knob | whether an unstaked path reaches S's leader in time, and only for sends that arrive ≤ 80 ms into node-S; measured by M2 |
| B. Staked or leader-direct sender | Helius Sender (SWQoS, already behind the A/B), Nozomi/Temporal, Astralane, NextBlock: one tip transfer inside the same signed tx, the same bytes also to today's fan-out | the tip (Sender's minimum 5,000 lamports; S landers' nozomi tips 0.001-0.41 SOL) plus the priority fee; no subscription for Sender; others per their docs (to be read before any build) | Sender alone gave no S+0; whether the rest do is unmeasured |
| C. Jito bundle | the buy plus a tip, sent to the block engine for a Jito leader | the tip; a dropped bundle costs nothing | 4 of 60 S landers; bundled copiers land later (f8120a16); the auction tick may miss early S |
| D. Faster detection | Jito ShredStream (an approved auth key, a public UDP endpoint on this host) or a paid geyser stream (Helius LaserStream on Professional $999/mo) | a subscription, not per buy | only if M2 shows that timing, not priority, is the limit; this reopens the operator's 10-08 decision |

- The tip always rides INSIDE the buy transaction (one SystemProgram transfer, as the Sender A/B does). A
  failed transaction reverts it, so a tip is paid only when the buy lands and succeeds.
- The priority fee is charged on every landed transaction, failed ones included.

## 4. Scope and latency

- **Per wallet, opt-in only.** A new knob `MEMECOIN_SLOT_LANE_S_LANDING_WALLETS` (source_user_id list,
  empty by default) names the wallets. The arm and the tier are chosen per wallet. The funded runner never
  applies them globally.
- **Bounded spend.** Per-buy priority and tip have hard caps (`MEMECOIN_SLOT_LANE_S_LANDING_MAX_LAMPORTS`).
  A build over the cap keeps today's fee, never a higher one.
- **No added latency for wallets not on the list.** The choice is one set-membership check on the build
  path, beside the existing Sender arm assignment (`solana_sender_ab.assign`). A listed wallet's extra
  path (a sender connection, a bundle client) is warmed on its own thread. Today's path is unchanged byte
  for byte for everyone else.
- **Listed wallets:** the same identical signed bytes go to today's fan-out AND to the S-landing path
  (option B or C), so the S-landing path can only add a chance, never remove today's.

## 5. Failure modes (under the existing submitted_unknown rules)

- **Identity first.** The signature and signed bytes are persisted before any network I/O, as today. Every
  path broadcasts the same bytes, so one signature identifies the buy on every path, and a double landing
  is impossible.
- **A dropped bundle, or a sender that does not answer:** no on-chain effect. The send is ambiguous until
  the blockhash's last valid height, and it is recorded `submitted_unknown` exactly as the free-direct spec
  rules. Never released as safe before expiry.
- **A tip paid without landing:** impossible by construction, because the tip is inside the buy and
  reverts with it.
- **A landed but failed buy (max price):** pays the priority fee, about $1.10 at the p50 tier. The 7 failures
  in 10 sends are exactly this case. It is counted per arm as a cost.
- **A provider 429, quarantine or health failure:** the free-direct per-provider quarantine applies. The
  buy still has today's fan-out.
- **A provider that rejects a state-dependent send:** ambiguous, never a fabricated "not sent".

## 6. Measurement before any money

### M1, zero cost: run 2026-10-09 (shadow replay on recorded data)

`scripts/research/s_landing_replay.py` (local tape reads only: no RPC, no money, one job, reads by index;
`--selftest` checks the pool math, the break-even and the early-arrival flag). For every slot-lane send
since 2026-09-26 with a tape row:
1. Take S's recorded swaps on the source's pool (`pool_swaps`, reserves after each swap).
2. Buy our $110 clip into the recorded reserves at each position: right after the source (k0), after the
   k-th later swap in S (k1..k3), at S's end, and at EVERY swap boundary in S. Fees are charged on both legs.
3. Judge each fill against the live chase cap: average at most 30% over the source's post-trade spot. A
   refused fill pays its fee.
4. Exit into the same pool's last recorded state at or before +30 s and +60 s after the source's block.
5. Net each position of each cost tier: our 0.0015 SOL, the census p50 0.010 SOL, the p90 0.150 SOL
   ($110/SOL).
6. Two baselines on the same sends, under the same exit rule:
   - TODAY: our own landed transaction bought into the state before it. A refused send pays our fee.
   - LATE UNCAPPED (Gamma): a fill at our real landing slot with no cap.

**"Anywhere in S" is priced at the worst boundary (Gamma).** Prices inside S are not monotone: copier
sells and back-runs put some middle positions below S's end. On 23 of 118 graded sends an earlier
position in S was worse than S's end.
- The go line uses **any-S worst**: per send, the minimum over every swap boundary in S.
- **any-S mean** (the uniform average over those boundaries) and **S's end** are reported variants.

**Early arrivals (Gamma).** A fee can move only sends whose receipt reached us at most 80 ms into node-S, or
before its slot notification. That is the trace's `slot_receipt` `head_at_recv_minus_s` and
`head_at_recv_age_ms`. Every line is also read on those sends alone.

**Tape completeness.** A send is censored (`tape_gap`) unless the signal-window tape read S densely and
every slot to +60 s at a sample width of at most 12 slots (`coverage`). No graded send was censored this
way. A sampled 5-slot bucket can still hide its last ~2 s on a busy pool.

**2026-10-10 measurement correction.** Dense slot coverage alone no longer permits grading. `s_landing_replay.py` censors a missing source block clock as `source_clock_unknown`. Every paired horizon needs a same-pool exit observation timestamped within `[source block time, source block time + horizon]`, at most `max_exit_age_s` old at that horizon (default 60 seconds). An absent exit is `horizon_exit_unknown:<horizon>`; an old exit is `stale_horizon_exit:<horizon>`. The model never substitutes its entry state for a missing exit. The numeric results recorded below predate this correction and remain historical; the corrected denominator requires a new replay before reusing them as current evidence. This amendment does not reconstruct missing clocks or authorize entry/exit policy changes.

**Bias against S-landing.** The k-positions and the late arm sell into recorded reserves that lack our own
buy, so their proceeds read low. A filled send's own swap is on the tape, so TODAY has no such gap. The
PumpSwap vault bias of section 1 also applies.

**M1 is a screen, not a gate (Gamma).** Every number below is a replay of a handful of sends.

**Frank alone first** (8 of 10 sends graded; 2 have no tape row):

| position, exit | net at 0.0015 | net at 0.010 | net at 0.150 |
|---|---|---|---|
| k0, +30 s | +$573 | +$565 | +$442 |
| k0, +60 s | +$640 | +$632 | +$509 |
| S's end, +30 s | +$191 | +$183 | +$60 |
| S's end, +60 s | +$163 | +$156 | +$33 |

- TODAY, replayed under the same exit (6 sends): -$22 at +30 s, -$34 at +60 s.
- LATE UNCAPPED (5 sends): +$52 at +30 s, +$24 at +60 s, 3 of 5 up.
- k0 is concentrated: the top 2 sends are $334 of k0's $565 at +30 s; 6 of 8 are up.
- TODAY here is not Frank's funded record (+$15.91 per close). It is only these slot-lane sends, exited
  by the replay's rule.
- Arrival: of the 6 base sends, 3 were early, 1 late and 2 unknown (no receipt head in the trace).

**The go/no-go line for the M2 A/B (option A on Frank).** q* = dfee / (G − B + dfee). G is the position's
net per send, B is today's, and dfee is the tier's extra fee over ours, all on the same sends. Option A at
0.010 SOL beats doing nothing on Frank only if the higher fee moves these shares of his sends beyond today's:

| sends | to k0 (+30 s / +60 s) | to anywhere in S, worst (+30 s / +60 s) | any-S mean | S's end |
|---|---|---|---|---|
| all 6 | 1.1% / 0.9% | 4.0% / 4.2% | 2.1% / 1.9% | 3.8% / 4.0% |
| early 3 | 1.4% / 1.1% | never / never | | |

- **On the sends a fee can move, landing anywhere in S does not pay at the worst price.** Probably because early
  arrivals already land early in S+1, so S's worst boundary is no better than today. Only k0 pays there. The
  "early" row is 3 sends.
- **So the operator's line is k0 among early arrivals:** about 1.4% of Frank's early sends must newly land
  before S's first later swap.
- **Today's k0 share is 0 of 143.** Our 3 S landings since 10-01 came 6, 10 and 6 same-pool swaps after
  the source.
- **The front of S is not bought by fee alone.** On Frank's 8 sends with an S buyer, the first buyer after
  the source bid between 0.2M and 2,000M µL/CU. 3 of the 8 were Jito-bundled, one paying 11.9 SOL to Jito tip accounts (as recorded),
  and one bid 0.2M µL/CU, below our 3.8M. Arrival decides the front, and we cannot see the arrival path
  (section 2).
- At 0.150 SOL: 18.5% / 15.9% to k0 and 70.6% to anywhere in S (worst). **0.150 SOL is no-go on Frank.**

**The other armed Solana wallets with at least 5 slot-lane sends, each alone** (net at 0.010 SOL, +30 s / +60 s):

| wallet | graded | k0 | S's end | today replayed | late uncapped (n, up at +30 s) | early / late / unknown | q* k0 | q* any-S worst | q* any-S worst, early only |
|---|---|---|---|---|---|---|---|---|---|
| DegenCapitalLLC | 34 of 49 | +$854 / +$772 | +$141 / +$116 | +$90 / +$25 | -$58 / -$104 (20, 11 up) | 9 / 19 / 6 | 4.0% / 4.1% | 56% / 46% | never |
| ResellCalendar | 29 of 35 | +$1,301 / +$1,288 | +$364 / +$390 | +$116 / +$133 | +$179 / +$160 (8, 7 up) | 7 / 15 / 7 | 2.2% / 2.3% | 19% / 21% | never |
| probablyinsider | 15 of 29 | +$218 / +$207 | +$122 / +$108 | -$159 / -$236 | -$123 / -$204 (14, 8 up) | 7 / 5 / 3 | 3.6% / 3.1% | 5.0% / 4.1% | 5.9% / 5.9% |
| OmakaseOnly | 9 of 14 | +$91 / +$267 | +$85 / +$260 | -$113 / +$64 | -$115 / +$61 (7, 2 up) | 3 / 1 / 5 | 4.0% / 4.0% | 4.3% / 4.4% | never |
| jpegkite | 11 of 11 | +$125 / +$62 | +$95 / +$35 | -$21 / -$95 | -$8 / -$71 (11, 8 up) | 2 / 4 / 4 | 6.2% / 6.3% | 8.4% / 8.3% | never |
| redemptionjizzy | 6 of 10 | +$81 / +$58 | +$46 / +$26 | -$87 / -$103 | -$58 / -$78 (5, 4 up) | 1 / 1 / 1 | 2.2% / 2.2% | 2.6% / 2.6% | never |
| fmpumpguy | 6 of 7 | +$192 / +$198 | -$21 / +$7 | -$16 / +$12 | -$15 / +$13 (2, 1 up) | 2 / 2 / 2 | 2.6% / 2.9% | never | never |

- The early/late/unknown counts are over each wallet's base sends (those whose TODAY replays).
- On early sends, worst-case any-S never pays on 6 of these 7 wallets. probablyinsider is the exception
  (n=7). Per wallet the early subsets are 1-9 sends.
- At 0.150 SOL, k0 needs 38-71% on every other wallet and more than 100% on jpegkite. Landing anywhere in S
  needs more than 44% on every wallet. So **0.150 SOL is no-go on every wallet.**
- Censoring is heavy on some wallets (Degen 15 of 49 and probablyinsider 12 of 29 have no tape row). Their
  rows describe the taped sends only.

**The cheaper experiment the late arm points to: a per-wallet cap lift plus a fast exit.**
- Uncapped fills at our real landing were up on Frank (+$52 / +$24, 3 of 5) and ResellCalendar (+$179 /
  +$160, 7 of 8), at no added fee.
- They were down on Degen, probablyinsider, jpegkite and redemptionjizzy. So a cap lift is per wallet or
  nothing.
- A cap lift is a money-path change, and a fast exit is an exit change. Both go to paper first
  (operator rule: exit changes go to paper before live). Both are operator decisions, outside this spec.

### M2, small and live, only on the operator's go: priority tiers on today's path (option A)

- **Arms.** sha256(attempt_id), assigned within each listed wallet: today's CU price, and the S landers'
  p50 per-CU price (55.9M µL/CU). No 0.150 SOL arm (M1: no-go everywhere).
- **Arm B pays its fee only on early arrivals (Gamma).** Receipt age is known at receipt, before the build.
  The higher CU price applies only when the receipt is early (≤ 80 ms into node-S). A late or unknown
  receipt signs today's price in both arms, because late sends do not reach S (0 of 87 late sends landed
  in S). This cuts arm B's cost by about 63%, and the primary score is unchanged. dfee and q* are per
  early send: Frank's line is the early row of the M1 table (1.4% / 1.1% to k0).
- **Scores.**
  - **Primary: k0 share among early arrivals** (≤ 80 ms into node-S), per arm. That is our landing before
    the first later same-pool swap in S, from our in-slot index on the tape.
  - **Secondary:** k0 share in the ≤ 30 ms bin; S share in the ≤ 80 ms and ≤ 30 ms bins and overall.
    The ≤ 80 ms bin is the only primary bin.
- **PnL, fill rate and every go/no-go are per wallet, using that wallet's own M1 q*.**
- **Pooled S share (Gamma's conditions).** The S share may pool across listed wallets as a TRANSPORT
  diagnostic ("does this fee reach S from our path"), and only as the q estimate:
  - it is stratified by receipt-age bin;
  - per-wallet and per-venue S counts with exact binomial CIs are printed beside it;
  - a wallet whose own arm-B count contradicts the pooled share (its upper bound below it) is judged on
    its own count.
  This keeps a fee lift on easy coins from being credited to Frank's crowd (588M µL/CU at p90).
- **Pre-registered asymmetric rule (Gamma).** Per wallet and score, with the time cap:
  - NO-GO as soon as arm B's 95% upper bound falls below arm A's share + q*;
  - GO only when arm B's 95% lower bound exceeds arm A's share + q*;
  - otherwise "undetermined" at the time cap.
- **Power.**
  - **A NO-GO takes many sends.** With 0 S landings in n, the bound is 3/n.
  - **Any-S** (2.1% today against about 6%): a GO at one-sided α = 0.05 and 80% power needs about 330 sends
    per arm (Gamma; checked: 328).
  - **k0 among early arrivals** (0 today, 1.4% line for Frank): a NO-GO needs about 214 early sends with no
    k0 landing, that is about 580 sends per arm at the 37% early share.
  - A GO can come early only if the fee really buys the front often.
- **List and time cap.**
  - List every wallet whose M1 worst-case any-S line at 0.010 is under 10%: Frank, probablyinsider,
    OmakaseOnly, jpegkite, redemptionjizzy. These made 74 slot-lane sends in 13 days, about 2.8 per day
    per arm.
  - Time cap: 30 days, about 85 arm-B sends, of which about 31 are early and pay the higher fee: about
    $30-72 of extra fee ($0.94-2.29 per early send by signed limit).
- **The operator's decision, stated plainly: M2 on option A will most likely end "undetermined".**
  - The 30-day cap gives about 31 early arm-B sends; a k0 no-go needs about 214.
  - The first S buyer bidding 0.2M µL/CU, under our 3.8M, says the fee is not the only gate. Path or
    stake decides the front (options B and D), and the Sender A/B showed no S+0.
  - So the choice is about $30-72 of fee for a likely "undetermined", against skipping to the cap-lift
    paper test, or to option B/D, which are operator decisions on paid paths.
- **CU limit first (Gamma).** The leader ranks by µL/CU, and the priority fee is that price × our SIGNED
  limit. So the same rank costs less at a right-sized limit. Measured since 10-02 (121 slot-lane sends
  with a recorded limit):
  - limits: the lane default on 67 sends (400k now, 260k before 10-07), the per-class limits of 182-215k
    on 51, and a raised exotic-leg limit on 3;
  - consumed: at most 130k on pump_amm at the 400k default (n=13); signed/consumed is 1.89× over 72 sends
    with both;
  - arm B's fee per buy: 0.010 SOL at 182k, 0.0145 SOL at 260k, 0.022 SOL at 400k.
  - Right-sizing is a separate money-path change, not part of this spec (Gamma). A too-low limit lands,
    fails and still pays the fee. It would extend the existing per-class rule (`SLOT_LANE_CU_CLASSES`: max
    successful consumed × 1.15 at ≥ 10 successful sends per class) to the default-limit builds. It ships
    as its own reviewed branch, with its own edge cases (ATA creation, Token-2022 hooks, exotic routes).
    Until then arm B signs today's limits.
  - No simulateTransaction on the send path: it would add a provider call to the fill path (rule 5,
    item 2).
  - dfee is recorded per send from the signed limit. It is never assumed flat.
- **Pre-registered with Gamma before the first send:** the list, n and the time cap, the bins, the
  per-wallet q* lines from M1, and the asymmetric rule.

### M3, only if M2 shows the leader is reached too late at any fee

- Option B or C on the same arms, with the tip schedule pre-registered.
- Option D (detection) is an operator decision on a subscription.

## 7. Edge cases (CLAUDE.md rule 5)

1. **Money path: yes.** Entry submission and spend per buy. Default off, per wallet, capped per buy.
   Every existing gate (caps, reservation, safety) is unchanged.
2. **Fill path speed.** Unlisted wallets add one set lookup and nothing else. A listed wallet's added
   send runs beside today's on its own connection. Its latency is measured per arm (receipt to send
   return, p50/p90) before any claim.
3. **Concurrency.** The arm is a pure function of attempt_id. Provider connections are per-process,
   thread-owned objects, and nothing global is mutated.
4. **Absent, zero, unknown.** No tape row means a send is not graded in M1, never "0 copiers". An
   unknown landing slot is censored, and an unanswered status is `submitted_unknown`.
5. **Dependency failures.** A sender or block engine that is down, rate-limited or rejecting falls back
   to today's fan-out (section 5). A tip-account list change is a refusal to send that path, never a
   guess.
6. **Units.** Priority lamports = CU price (µL/CU) × CU limit / 1e6. Version-1 transactions carry the fee
   in `transactionConfig` (copier_crowd_and_fees.fee_facts). Tips are lamports. USD uses the SOL/USD
   at measurement ($110 here).
7. **Sample.** Frank: n=10 sends, 8 graded in M1. Other wallets: graded counts in section 6, with heavy
   censoring on some. The copier census (section 2) is Frank's coins only; who lands in S on other
   wallets' coins is unmeasured. M1's TODAY baseline is our own landing, so it describes us, not the market.
8. **Rollback.** Empty the wallet list and the arms stop. Today's path never changed.

## 8. Not handled

- Option D's host requirements: ShredStream needs inbound UDP, and this host's NAT is unchecked.
- Provider tip schedules other than Helius Sender's, which must be read from each provider before M3.
- The copier census on wallets other than frankdegods (M1 ran on them; the census did not).
- The per-wallet cap lift and fast exit (section 6). They are a paper experiment for the operator, outside this spec.
