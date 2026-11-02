# Fomo account to on-chain wallet attribution — design

Date: 2026-09-07
Status: DESIGN. No implementation started. Phase 0 is measurement and it can cancel the rest.
Author: Alpha (sniper-9a), at the operator's request.

---

## 1. Why we do this

Fomo refused our account at the application layer on 2026-09-06 at 19:31 UTC. The account keeps a
live JWT and still receives 403. The ban guard refuses every fomo call locally. The funded copy
path saw no signal for 15 hours.

The ban removed the DELIVERY of the signal. It did not remove the VALUE of the signal. The value is
the wallet selection: fomo names traders whose buys are worth a copy. We can keep that selection
and take the delivery from the chain.

**Measured 2026-09-07, and this number decides the design.** We paired every on-chain detection
with the nearest fomo signal for the same trader and the same coin, inside a 600-second window.

| quantity | value |
|---|---|
| matched pairs | 332 |
| median chain lead over fomo | +1.6 s |
| p25 / p75 chain lead | +1.2 s / +2.1 s |
| chain first | 283 of 332 (85%) |

The chain sees the buy BEFORE fomo reports it, and the spread is tight. Our fastest fomo lane
measured a 4.1 s mean lag on the same day. So on-chain attribution is not a fallback for the ban.
It is faster than the lane we lost, and no third party can revoke it.

The `onchain_watch` lane also kept producing entry attempts THROUGH the ban: 6 attempts, the last
at 2026-09-07T00:24:27Z. The lane works. It covers 3 wallets.

## 2. What we need

For each fomo trader we follow, and for each chain that trader uses, we need one on-chain address
that satisfies both conditions:

1. The address is the actual beneficiary of that trader's buys.
2. The address belongs to that trader alone, and not to a shared execution service.

Condition 2 is the hard one and the operator named it first. Fomo executes through common wallets.
An attribution method that ignores this maps many traders to one executor address. Such an address
is worthless as a copy signal, because it fires for every trader at once.

Current coverage, measured 2026-09-07:

| set | count |
|---|---|
| armed wallets | 65 |
| armed wallets with an EVM address | 30 |
| armed wallets with a Solana address | 1 |
| wallets the `onchain_watch` lane covers | 3 |
| armed wallets approved for chain 56 | 0 |

## 3. Methods that already failed — do not repeat them

`memecoin_wallet_addresses.verify_status` records the outcomes. Read it before you propose a
method.

| method | rows | verify_status | why it failed |
|---|---|---|---|
| `holder_intersection` | 3 | REFUTED | It found an address holding 7 or 8 of the 8 coins checked, and the address was still wrong. A shared executor holds every trader's coins, so it intersects with every trader. |
| historical mint-signature paging | — | infeasible | A hot coin does more than 10 transactions per second. A buy 79 minutes old sits more than 60,000 signatures deep. |
| timestamp correlation | — | false positives | A hyper-active account matches any timestamp. |
| `fomoscan` | 2 | confirmed | It works, but it reads a fomo surface. The ban makes any fomo surface unavailable. |

A near-perfect intersection score is NOT evidence. That is the exact trap: the refuted rows scored
7 of 8 and 8 of 8.

## 4. The method we will use

**Fresh-buy attribution, plus a shared-address reject rule.**

1. A signal names a trader and a coin at a known time.
2. Read the chain for buys of that coin inside a short window around that time.
3. Record every address whose balance of that coin increased, and which spent the quote asset.
4. Repeat across many coins and many traders.
5. Propose an address for a trader when the address is the buyer of at least 2 of that trader's
   coins.
6. **REJECT the proposal when the same address is the buyer for more than one trader.** That
   address is a shared executor.

Rule 6 is the addition this design makes. `scripts/research/fresh_buy_resolver.py` already
implements steps 1 to 5 for Solana. Its confirmation rule is rule 5 alone, and rule 5 alone cannot
survive a shared executor, because a shared executor buys at least 2 coins for every trader.

Rule 6 has a consequence for how we run the resolver. The resolver cannot confirm any trader until
it has observed enough traders to prove an address is NOT shared. So it must run across the whole
roster together. It must not run one wallet at a time.

**EVM is easier than Solana for this method.** A swap log names the recipient directly. There is no
signature-paging problem, which is what made Solana hard. Chain 4663 and chain 56 are both EVM.

## 5. Why chain 56 and not chain 8453

Fomo feed events, 14 days to 2026-09-07:

| chain | events | traders | coins |
|---|---|---|---|
| 4663 | 67,185 | 6,968 | 1,719 |
| solana | 23,874 | 4,443 | 1,010 |
| 56 (BSC) | 11,978 | 2,722 | 329 |
| 8453 (Base) | 4,861 | 1,460 | 166 |

Chain 56 carries about 2.5 times the events of chain 8453 and about 1.9 times the traders. The
funded BSC implementation already exists (PancakeSwap V2/V3 rungs, proxy-aware token safety) and it
is inert only because chain 56 is not in `MEMECOIN_EVM_ALLOWLIST`.

## 6. Phase 0 — the replay that decides everything

**This phase writes no production code and changes no runtime behaviour. It answers three
questions at once, and a bad answer cancels phases 1 to 3.**

We hold 47 EVM addresses in `memecoin_evm_wallet_addresses`. A peer scraped them from fomo profile
pages. They cover 47 traders and 47 distinct addresses, and no address repeats. But a
profile-displayed address is NOT proven to execute the trades. Nobody has checked it against the
chain.

Take those 47 addresses. For each trader, select their chain-4663 buy events from
`memecoin_events`. Read chain 4663 swap logs around each event time. Then answer:

1. **Does the profile address appear as the buyer of that trader's coins?** If yes, fomo profile
   scraping already solved attribution for EVM and phases 1 and 2 shrink to a scrape plus a
   verification.
2. **Does any address appear as the buyer for more than one trader?** That names the shared
   executors, and it measures how large the problem the operator raised actually is.
3. **Can we read an EVM buyer at all from the logs we can reach?** This is the feasibility gate for
   the whole design on EVM.

Report the three answers with the sample size for each. Do not proceed on a hunch.

### Phase 0 constraints

- **Read-only.** No writes to `sniper_tracker.db` except a new research table this phase owns.
- **Never use the custody RPC.** Chain 4663 research reads use the chain-owner endpoint
  `https://rpc.mainnet.chain.robinhood.com`. Never QuickNode. A research loop burned about 7,000
  requests of funded quota on 2026-08-21, and a funded lane must never queue behind research.
- **There is no stored swap tape.** No table in `sniper_tracker.db` holds swap senders or
  recipients, so this phase must read logs from an RPC. Bound the request count and state it before
  the run.
- Endpoint capability is not uniform. Chain 4663 has 3 configured endpoints and only one serves
  `getLogs`; the other two answer 400 and 403. Verify the endpoint answers the method before you
  measure anything with it.

## 7. Phase 1 — the EVM resolver

Extend fresh-buy attribution to EVM, with rule 6 enforced. Write results to
`memecoin_wallet_addresses`, which already carries `network_id`, `method`, `verify_status` and
`verify_detail`, so it needs no migration. Use a new `method` value so the rows are distinguishable
from the refuted `holder_intersection` rows.

`onchain_wallet_detections` already carries `network_id`. It needs no migration to hold EVM rows.

## 8. Phase 2 — chain 56

Point the phase 1 resolver at chain 56 and its 2,722 traders. Chain 56 needs its own `getLogs`
endpoint check first; we have not verified which BSC endpoint serves it, and we must not assume the
chain-4663 answer transfers.

## 9. Phase 3 — the capture we are missing

We discard the swap recipient today. Capture it, keyed by `(network_id, mint)` as every other
memecoin surface is. This turns attribution from a batch job into a continuous one, and it is also
the data the operator's side-funding question needs. Do not start this phase before phase 0
reports.

## 10. On Bubblemaps

The operator proposed Bubblemaps to link a trader to the wallets that side-fund their positions.

It fits the FUNDING question and not the ATTRIBUTION question. It answers "which addresses are
connected". It does not answer "which address bought this coin first". So use it to CONFIRM a
candidate cluster, never to generate one.

Two cautions. It is a third-party service with rate limits and a possible cost, and we have not
read its terms; do not assume access. And the funding edges it draws are derivable from the chain
directly, by a trace of native transfers into a candidate address. After the 2026-09-06 ban, we
prefer a dependency we own. Bubblemaps is acceptable as a research aid. It must never sit in the
funded path.

## 11. What stops this design

- Phase 0 shows we cannot read an EVM buyer from a reachable endpoint. Then EVM attribution fails
  and the design narrows to Solana.
- Phase 0 shows most candidate addresses are shared across traders. Then per-trader attribution is
  not possible from buys alone, and the design needs the funding graph of phase 3 before it can
  continue.
- The chain lead measured in section 1 does not hold per chain. The +1.6 s figure comes from Solana
  data only, because all 3 detected addresses are Solana. Chain 4663 has a known 40 to 60 second
  log-index lag on fresh pools. Measure the lead separately for every chain before that chain's
  attribution feeds a funded entry.

## 12. Open questions

1. Which BSC endpoint serves `getLogs`? Not verified.
2. Is a fomo profile address the executing address? Phase 0 question 1.
3. Does the +1.6 s chain lead hold on chain 4663 and chain 56? Not measured.
4. `fresh_buy_observations` holds one observation for `frankdegods` with buyer `58osDYARtvC5...`,
   while `memecoin_wallet_addresses` holds `498g1rVnFc...` for the same trader from `fomoscan`. The
   two disagree. One of them may be a shared executor. Resolve this before you trust either method.

## 13. Scope guards

- This design changes NO funded behaviour. No resolved address feeds a funded entry until the
  operator approves it separately.
- Arming a wallet stays the operator's decision.
- Chain 56 stays out of `MEMECOIN_EVM_ALLOWLIST` until the operator funds it.
- No phase here revives the legacy low-buy or insider-score pipelines.
