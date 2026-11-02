# Memecoin Wallet Map overhaul: herders, their herds, and who buys before them

**Date:** 2026-09-30 · **Author:** Lambda · **Status:** DRAFT 3, Gamma's review of dd5001f7c applied · **Order:**
operator, 2026-09-30 16:5xZ to 17:1xZ, five messages relayed by Alpha · **Replaces the views of**
`2026-09-30-memecoin-wallet-map-design.md` (its data rules stay).

## What the operator asked for, in his words

- "We need to find wallets that actively buy before the price impact accounts (herders); we need
  to see if the herders interact with each other; that's about it."
- "We want the copy bot link, that way we know how much of a herd the herders have."
- "We will use the herd members to identify if they form part of other herds from other herders,
  then see if the herders follow a pattern or a wallet that can predict their entry; see how we
  can use the copy bots to identify other good herders."
- "Identify the best herder (biggest herd) and then best herd member, most profitable, and see if
  we can get some alpha or edge from them; is it only Fomo accounts that have this type of
  motion? Maybe other KOL from Axiom, GMGN and such can also create herds or identify alpha
  before the herders."

The page describes wallets. It never arms, sizes, admits or exits anything, and the web process
makes no chain, price or funded call. Arming stays a funded-results decision by the operator.

## Words, each with its denominator

| Word | Meaning on the page | Measured by |
|---|---|---|
| Herder | a wallet whose buys are followed by other people's buying | two measures, shown side by side: its herd (below), and slice 3's flow change (other wallets' net SOL into the coin in the 60 s after its buy against the 60 s before) |
| Unit read | the ONE read that stands for a (herder, coin): its earliest read at or before the window's first buy (a coverage read, or a read first buy that has since left the window), else the read of its first-buy transaction (pre-registration A) | `wallet_probes.unit_reads` over ALL reads; the page and Gamma's freeze call the same function |
| Read coin | a coin whose unit read is `ok` and not truncated | one per (herder, coin) |
| Herd member | a wallet that bought the same coin in the herder's own buy slot, later in the slot, in the unit read | a companion whose transaction index is higher than the herder's |
| Regular | a member behind the herder on k >= 3 of its n read coins and k/n >= 10%; none named when n < 10 | `wallet_probes.regulars` (Gamma's ruling) |
| Probe bot | a regular of two or more herders | the regulars, across herders (pre-registration C) |
| Early buyer | a wallet that bought a coin before the herder did, up to 60 s before | slice 3b's window read |

A herd number is always "k of n": J2QQwDNY is behind probablyinsider on 6 of probablyinsider's
45 read coins, not "6 of 6".

**One read per (herder, coin).** A pair often has several reads (338 of 791 read pairs had more
than one on 2026-09-30, 2,247 of the 2,700 reads). Counting a member behind "in any read of the
coin" names more regulars than the first-buy read does (Gamma: 82 against 66 on her window, 9 of
24 herders with n >= 10 changed), and it lets a later buy stand in for the first. So k, n,
coverage and every herd figure use the unit read only. When a pair's read first buy leaves the
14 days and it rebuys inside them, the old read stays its unit read, as coverage_pending holds the
pair done; a read of the rebuy never stands in. A pair whose reads are all of later buys is
"not read at its first buy". A unit read that is truncated, `not_found`, `no_slot` or
`no_signature` is out of both k and n. Each of these is counted per herder in the details
(41 truncated `ok` reads today), never shown as 0.

**What a slot read is and is not.** One read = one buy by one of our wallets, and the list of
wallets that traded that coin in the same slot. Until the coverage backfill, a buy was read only
if we filled a copy of it or the on-chain watcher saw it (section 0). 65 wallets have reads
(2,700 reads, 41 truncated). A wallet without reads is "not read", never "no herd". Only the same slot (about 0.4 s) is covered; buyers in later slots
are in slice 3's 60 s count, not here. Swaps routed through an aggregator are often not decoded,
so every herd is a lower bound.

**The stored test is not used for herd counts.** A `slot_follower` edge passes when the wallet is
behind this source more often than its rate across all our reads (binomial, p <= 0.01). That asks
"is it concentrated on one source", and a bot behind every herder has a high base rate, which is
the bot the operator wants to see. The page counts from the unit reads (k of n).

## 0. Coverage: read every buy, not the ones we chose

**What picks a buy for a slot read today** (`scripts/research/wallet_link_graph.py`): first the
source transactions of our own funded Solana fills, then `solana_detections`, the buys the
on-chain watcher saw. The watcher follows only wallets armed on Solana. So a wallet's buys are
read only while it is armed there, plus the buys we copied. The slot pass has 300 calls per run:
of the 19 runs before 2026-09-30 17:35Z, 9 stopped on that budget, 9 finished and 1 hit an RPC
error; the limit is what gets picked. The job also waits while a funded Solana position is open:
the 17:35Z run read nothing for that reason, and Gamma measured the book flat at :35 on 136 of the
last 168 hours (81%), so research throughput is about four runs in five, a little less when a
position opens mid-run.

**Measured, 2026-09-16T07:28Z to 09-30T16:28Z.**

- J7 delivered 3,352 Solana wallet-coin buys by 76 wallets, 233 a day. 597 of them (18%) were
  read, for 56 of the 76 wallets.
- The reads hold 791 wallet-coins in all. The other 194 are buys J7 never delivered: notdecu,
  untaxxable and Aurelius0121 are watched on chain and are not on J7. (791 over 3,352 is not a
  coverage share; the two counts are different populations.)
- 331 of the 692 read coins are coins our funded book also bought.
- Per wallet, coins read out of coins we know it bought: frankdegods 50 of 54, probablyinsider
  45 of 55, DegenCapitalLLC 40 of 63, cap 26 of 69, sunnydog06 22 of 104, bigwarzeth 19 of 53,
  prevaile 1 of 377, badabeepp 5 of 322, 0xsneakky 3 of 177.

The operator is right: the herd table describes the wallets we armed, and the wallets with the
most buys are barely read.

**New rule.** One read per (wallet, coin), at the wallet's first buy of that coin, for every
Solana buy by every tracked wallet, from three intakes: our fills, the watcher's detections, and
J7 events whose id carries the transaction signature. A J7 signature's slot comes from
`getSignatureStatuses`, 256 signatures per call. J7's `swap:<uuid>` report carries no
transaction. 778 of the 3,352 wallet-coin buys (23%) have only that id and cannot be read unless
the watcher also saw them. That is the ceiling of this rule, and the page states it.

**Cost, research key.**

- A read cost 1.04 calls on the 16:35Z run (106 calls for 102 reads); the worst case is 3.
- Steady state: about 245 calls a day (at most 700), plus 1 status call per 256 signatures.
- Backfill of the 14-day window: 2,105 unread wallet-coin buys with a signature. That is about
  2,200 calls (at most 6,300) plus 9 status calls: 8 runs at the slot budget of 300 (21 at worst).
- The job's per-run budgets today are slot 300, flow 300, upstream 150 and funders 50: 800 of the
  2,000 cap.

**On the page.** Every herder row prints its coverage ("read 45 of 51 first buys") and, beside
it, how many of its first buys can never be read ("2 uuid only"), so the reader can tell buys not
yet read from buys that cannot be. A herd figure whose coverage is under 80% carries
"undercounted: N% of its buys read". The stamp gives the overall share and the uuid ceiling:
766 of 3,548 first buys are uuid only (21.6%), so the ceiling is 78.4%, under the 80% line, and
most herders will carry the label. The bot trail (section 4) and the Axiom seeds (section 6)
widen the set past our list; this section only stops the list from being chosen by our own
trading.

## What the stored data says today (no new call)

Unit reads only, computed 2026-09-30 ~18:45Z on a copy of the graph DB; the window is the job's 14
days, and the stored reads run from 2026-09-16T19:17Z to 09-30T16:28Z. The window slides, so the
total differs a little from Gamma's 66 regulars on her window (60 here); the rule is the same.
"Drew a follower" = read coins with at least one later buyer in the slot.

| Herder | First buys (units) | uuid only | Read coins n | Regulars | Drew a follower | Members ever | Out of n: not first / truncated | Armed on Solana now |
|---|---|---|---|---|---|---|---|---|
| DegenCapitalLLC | 64 | 2 | 35 | 11 | 25 | 58 | 4 / 0 | yes |
| frankdegods | 51 | 0 | 45 | 8 | 32 | 111 | 0 / 2 | yes |
| untaxxable | 10 | 0 | 10 | 8 | 7 | 19 | 0 / 0 | no |
| cap | 69 | 0 | 26 | 7 | 12 | 39 | 0 / 0 | no |
| notdecu | 66 | 0 | 63 | 6 | 37 | 114 | 1 / 2 | no |
| bigwarzeth | 51 | 9 | 15 | 4 | 9 | 23 | 4 / 0 | no |
| probablyinsider | 56 | 0 | 45 | 3 | 19 | 33 | 0 / 0 | yes |
| MemeCooksx | 97 | 45 | 18 | 3 | 13 | 17 | 4 / 0 | no |
| picadura | 91 | 7 | 45 | 2 | 12 | 33 | 2 / 0 | no |
| sunnydog06 | 107 | 29 | 16 | 2 | 8 | 7 | 0 / 0 | no |
| Roveryaps | 78 | 37 | 34 | 0 | 6 | 11 | 0 / 0 | yes |
| OmakaseOnly | 63 | 30 | 16 | 0 | 7 | 17 | 1 / 0 | yes |

The rest of a herder's first buys are not read yet (the backfill). Roveryaps' 0 regulars of 34
read coins is a measured zero; a herder under 10 read coins shows "too few read coins to name
regulars", never 0.

Probe bots (regulars of 2 or more herders; none is on our roster):

| Bot | Herds | Behind whom (k of n read coins) |
|---|---|---|
| J2QQwDNY | 6 | probablyinsider 6 of 45, MemeCooksx 5 of 18, DegenCapitalLLC 4 of 35, redemptionjizzy 4 of 24, sunnydog06 4 of 16, DopamineFeenFr1 3 of 10 |
| 2CQgjcdN | 5 | notdecu 7 of 63, frankdegods 5 of 45, DegenCapitalLLC 5 of 35, cap 3 of 26, bigwarzeth 3 of 15 |
| DiJF3L37 | 2 | untaxxable 4 of 10, cap 3 of 26 |
| 2WezGgRW | 2 | DegenCapitalLLC 4 of 35, bigwarzeth 4 of 15 |

26 herder pairs share at least one regular; the most is bigwarzeth and DegenCapitalLLC with 2.

**Do herders interact? Today, only through shared regulars.** No `follows`, `dumps_on`,
`shared_funder` or same-slot edge joins two wallets of the herder set. The 6 `follows` and 3
`dumps_on` edges in the graph are between other roster wallets (0xsneakky, badabeepp, Roveryaps,
prevaile and the wallets that buy after them). Those edges are read **src = the wallet that buys
first** for `follows` and **src = the seller** for `dumps_on`
(`scripts/research/wallet_link_graph.py:186`); the first mock had `follows` reversed.

**Does anyone land before a herder inside its own slot? Almost never.** One herder has such
wallets on 3+ coins: untaxxable (6GTY24Q4 on 6 of 10, FwhaiTn2 on 3 of 10). Whoever is before the
herders is in the seconds before, which is slice 3b's window, not in the slot.

**Flow change (slice 3): not measured yet.** The job went live with the 17:05Z deploy and its first
run is 17:35Z. A wallet gets a verdict at 20 reads; about 8 wallets can reach that, and the test
compares a wallet with the rest of our roster, with low power. Day one shows "not measured yet".

## Page one: Herders

A table, one row per wallet with slot reads. No graph.

| Column | Plain words | State |
|---|---|---|
| Herder | handle | exists |
| Coverage | "read 45 of 51 first buys, 0 uuid only", and "undercounted" under 80% | exists (unit reads, J7 events, detections, fills) |
| Herd | "8 regulars, 32 of 45 read coins drew a follower"; "too few read coins to name regulars" under 10 | exists (unit reads) |
| Flow after its buy | median SOL change in the 60 s after, with n, and "moves more than our other wallets" only on a `mover` verdict; otherwise "not measured yet" or "not told apart at this n" | slice 3, from the first runs |
| Shares regulars with | the herders it shares the most regulars with, with the count | exists (unit reads) |
| Links to other herders | buys-after, sells-into, same funder, same slot, between herders only | exists; empty today |
| Buys before it | wallets that bought its coins before it, k of n, median seconds | slice 3b rows regrouped (section 2) |
| Do we follow it | "Real money on Solana" / "Paper only" / "Not watched", with the last approvals-ledger action when not armed | exists |

**Sort:** by regulars, then by the share of buys that drew a follower. Reason: it is measured
today for every read wallet and it is a count with its denominator. The flow verdict exists for a
handful of wallets, flips between hourly runs by design, and compares only inside our roster; the
column is sortable, and a `mover` verdict is marked, but it does not order the page. A wallet
with fewer than 20 read coins carries the label "few reads".

Under the table: the herder-by-herder shared-regular table (pairs with 2 or more shared
regulars), and the probe list (regulars of 2 or more herders, each with its herds as k of n).

One stamp line, in words: "Solana only. Herds come from the first-buy slots of our wallets, <first
read> to <last read>. A wallet we never read is not on this page." The two dates are the unit
reads' earliest and latest times, computed per request, never typed.

## Page two: one herder in time

Three columns, left to right, no lines:

```
  BUY BEFORE IT                THE HERDER                    BUY AFTER IT (its herd)
  early buyers, k of n         probablyinsider               J2QQwDNY   6 of 45  (a regular of 5 others)
  coins, median seconds        armed on Solana               ... regulars first
  (slice 3b; "not              45 read coins                 + the members under the regular rule,
  measured yet" until then)    19 drew a follower              as one count
                               flow after: not measured yet
  SELLS INTO IT (red)                                        IT SELLS INTO (red)
```

Below: the pattern table (section 3), then a details toggle, closed by default, with the stored
edge tests, method version, flags, truncated reads and the undecoded share. No test vocabulary
outside the toggle.

## 1. Best herder

Ranking as above. Measured: herd size, regulars, draw share (slot reads), flow change once slice 3
has verdicts. Not measured: how much of the flow change the herd accounts for. Slice 3's window
read holds every buyer of the 60 s after; attributing that flow to named herd members is a
regrouping of stored pages, and it needs Gamma's rule before it is shown. Cost: 0 new calls.

## 2. Who buys before the herders (the wallets to follow)

Slice 3b already stores, per coin, the early buyers before our first source's buy
(`memecoin_wallet_early_buys`) and the service already groups them per source. Keyed to herders,
three things change, and that is a new method version, not an edit of `upstream-v1`:

1. **Unit.** Today: one unit per coin, at the first buy by any of our sources. Wanted: one unit
   per (herder, coin), at the herder's own first buy. When the herder was the first source the
   stored read is the unit (0 calls). When another source bought first, the herder's window may
   lie outside the stored pages: a new read, at most 12 calls, inside 3b's 150 per run.
2. **Statistic and null.** Today: early on winning coins, against early buyers of the same coins'
   non-winning windows. Wanted: early before herder H on k of H's n coins, whatever the coin did.
   A launch sniper is early on everything; the null must be the wallet's own rate of being early
   across all read coins, so it names wallets concentrated on herders, not wallets that buy
   every launch.
3. **Who counts.** Today roster wallets are excluded. The operator wants inside and outside the
   roster.

The herder set has to be frozen before the read, or the selection feeds the test. Proposal: the
wallets with at least one regular on the day Gamma registers the rule. Gamma rules on the
amendment before any read. Measured today: nothing (3b's first run is 17:35Z). Cost: 0 calls for
the regrouping; at most 12 per (herder, coin) where the herder was not the first source; the
share of such units is not known until the first runs.

## 3. What predicts a herder's entry: pattern table

Per herder, from stored rows only, 0 calls: market cap at entry and buy size
(`memecoin_events.their_market_cap`, `their_size_usd`; frankdegods: 137 of 137 buys carry both),
hour of day (`created_at`), delivery lane (`delivery_lane`), and coin age at the herder's buy from
slice 3's age probe only, with its own n. `memecoin_positions.entry_token_age_s` is the age at OUR
paper entry, a later clock; it is not pooled with the herder's and is left out. Shown as medians
with quartiles and n. Descriptive; nothing is tested.

## 4. Bot trail: using the bots to find herders we do not know

A probe bot is a regular of two or more herders. Its other buys are read, and the wallet that bought
the same coin just before it (same slot by transaction index, or the seconds before) is noted. A
wallet outside our roster that several independent probes buy behind, on several coins, is a
candidate herder. The trail is blind to platform: a candidate can be a Fomo account or not, and
the report states what share of candidates is on our roster.

Pre-registered by Gamma (section C of the pre-registration): probes are regulars of 2+ frozen
herders (4 by the unit reads today; the list is frozen after the coverage backfill); sampling,
the candidate rule, and the exclusions (our roster's addresses, the probes, the coin's creator and
launch bundle) are hers.

Cost, research key: a bot's transactions come 100 per call and a bot can trade thousands of times
a day, so the read is a sample. Proposal: per probe per day, 2 list calls (its newest 200
transactions) and 10 sampled buys at up to 3 calls each (the coin's transactions in the slot and
the 10 s before): at most 32 calls per probe per day, 640 for 20 probes. Cap: 150 calls per
hourly run, the same as 3b; every run stays under the job's 2,000. Measured today: the probe set.
Not measured: everything else.

## 5. Best herd member

From the same list pages, a probe bot's own buys and sells: SOL paid and received per coin from
its own balance changes in each transaction, fees included, failed transactions counted (so the
read takes failed transactions too). Per bot: coins, share of coins closed at a gain, median
hold, and whether it sells before the herder's herd arrives or into it. No display feed.

A round trip needs both ends, so the read is a contiguous stretch of history, not scattered
transactions: proposal, the last 24 h per probe, at most 10 calls (1,000 transactions); when the
cap cuts the stretch the page says how many hours it covers, and a coin still held at the end is
"open", not a win or a loss. Gamma pre-registers what "profitable" means and the n. Cost: at most
10 calls per probe per day, 200 for 20 probes, inside the bot-trail cap. Measured today: nothing.

## 6. Beyond Fomo

What we hold:

- Roster: 553 wallets (`follow` 296, `manual` 222, `manual-arm` 10, `robin-discovery` 9, `pump` 9,
  `robin-intake` 5, `axiom_watch` 1, `arm-matrix` 1); 83 have a verified Solana address.
- Axiom, one capture on 2026-09-16 (717,608 raw messages, 07:39Z to 12:03Z; it has not run
  since). Its smart-money directory gives handle, Solana address, 24 h PnL, trades, volume and
  followers. `axiom_trader_directory` holds 346 distinct Solana addresses (351 handles) and 100
  EVM addresses. 33 of the 346 are wallets we already track; 313 are not. `axiom_kol_callouts`
  (41,201 rows) names callers by handle, not by wallet.
- Beta's read of the 313 (2026-09-30; the counts marked * re-checked by Lambda):
  - the directory has three 7-day lists: kol 126, pump 97, fomo 80, kol and pump 8, fomo and
    pump 1, none 1. The 33 we track are mostly the fomo list (26);
  - followers are known for 186* (p50 1,148, max 577,895);
  - the raw payloads hold PnL and rank per window (24 h, 7 d, 30 d, all). All 134 kol-list
    wallets have a 7-day rank;
  - 18* share an X handle with a roster handle. Their addresses are not registered, so the
    identity is unproven;
  - Axiom trades with a transaction: 23 events from 10 wallets, all in the 09-16 capture;
  - 4* appear in our slot reads, always ahead of our source by 161 to 1,183 transactions. That
    is not a bot follow, and no graph edge touches them.
- GMGN: no wallet list. The GMGN code here reads token stats; the Axiom callouts that come from
  GMGN (1,166 rows) carry handles only. A GMGN KOL list with addresses would have to be exported
  by the operator; nothing here scrapes a site or creates an account.

The herd test for any wallet with a known Solana address is the same slot read and needs no
watcher. Our slot reads cover only coins our own wallets bought, so a KOL's herd on other coins
is invisible until its OWN buys are read: list its recent transactions (100 per call), then one
slot read per coin (1 to 3 calls). To 20 read coins that is about 21 to 63 calls per wallet,
once. The 313 are 6,600 to 19,700 calls, several days inside the job's cap. Proposal: start
with the 134 kol-list wallets in 7-day rank order (followers are missing for 127 of the 313).
This is a fourth intake, beside section 0's three. The list is two weeks old: addresses do not
expire, the ranking does.

## Approved (operator, 2026-09-30 ~17:4xZ, relayed by Alpha): order and caps

Pre-registered by Gamma at 17:40Z, before any read:
`docs/audits/2026-09-30-wallet-map-reads-preregistration.md` (A coverage, B who buys before the
herders, C bot trail, D best herd member, E flow attribution, F Axiom). Where this spec and that
file differ, that file rules: the coverage unit is the FIRST buy across all intakes with no
substitution (a uuid-only first buy is `no_signature`), and a regular needs n >= 10 read coins.

"Sure, do all of that with your team." In this order, each read pre-registered by Gamma before it
runs, every call on the research key through `funded_exit_truth.rpc`, only while the Solana book
is flat, and every 429 counted in the run notes:

1. Coverage (section 0), with the 14-day backfill.
2. Bot trail and best herd member (sections 4 and 5); probes are regular multi-herder bots.
3. A one-off chain read of the 134 Axiom kol-list wallets (kol 126, kol and pump 8) to test
   whether they have herds; the pump-list wallets only if that pays.

The page build follows this spec's CLEAN.

**Caps, research-key calls per hourly run.** Slices 3 and 3b keep their budgets and run first in
every run. The new passes run after them, and a 429 stops the new passes for the rest of that
run.

| Pass | Cap per run | Existing? | Day ceiling |
|---|---|---|---|
| slice 3 flow | 300 | yes | 7,200 |
| slice 3b upstream | 150 | yes | 3,600 |
| slot reads: coverage and backfill, status lookups included | 300 | yes (same budget) | 7,200 |
| funders | 50 | yes | 1,200 |
| bot trail and best herd member | 150; per probe per day at most 32 + 10 | new | 3,600 |
| Axiom kol-list read (one-off) | 150, until the 134 are done | new | 3,600 |
| **total** | **1,100** of the job's 2,000 | | **26,400** |

The Axiom read is about 2,800 to 8,400 calls, so 19 to 56 runs at its cap. Whether the research
key's monthly quota covers 26,400 a day is not known (see the last section); the run notes report
calls and 429s per pass, so the first day of runs answers it.

## Open decision: slice 3c (not approved)

A candidate herder found by the bot trail, or an Axiom wallet with a herd, gets the three
measures (flow, herd, who buys before it) from batch reads as above. Following it live on paper
needs a watcher on its address: measured on the armed watcher (13 addresses, 1.90 h), about 98
`getTransaction` calls per address-hour, **at least 2,340 research-key calls per address per
day**, plus 8 to 30 exact captures per address per day. That stays the operator's decision.

## What is deleted, what stays

Deleted: the d3 force view (`static/wallet_map.js`, its knobs, the 31 `.wm-` rules, the canvas
block in `templates/index.html`, the rendering tests) and `GET /api/memecoin/wallet-map`, whose
only reader it is. `static/vendor/d3.min.js` stays (the world map uses it). Shared-funder hubs and
edges between wallets that are not herders leave the page. Out of scope: coin view, EVM, the
"what it paid" number (Gamma's later read), slice 3c.

Kept: the graph job and database, and the first spec's data rules (sample stated on the page,
our own addresses excluded, numbers with denominators, missing / unreadable / empty as three
states). `sniper/services/memecoin_wallet_map.py` keeps its readers and gains the herd shaper.

## API (read-only, per-request read-only connections)

- `GET /api/memecoin/wallet-map/herders?chain=solana` returns `{stamp, herders, shared_bots,
  probes}`.
- `GET /api/memecoin/wallet-map/wallet?chain=solana&id=<source_user_id or address>` returns
  `{stamp, wallet, before, herd, sells_into_it, it_sells_into, patterns, details}`.

Herd counts are computed per request from `memecoin_wallet_slot_reads` (2,700 rows today). If
that read passes 50 ms p90 on the live file, the hourly job writes a summary table instead; the
measured p50 and p90 go in the slice-1 review. HTTP 200 with `stamp.error` when the graph file is
missing or unreadable. A chain other than Solana returns an empty list and says so.

## Build order (each slice reviewed by Gamma, merged by Alpha)

1. Herders table with its coverage column, shared-regular table, probe list; delete the force view
   and its route. 0 calls.
2. Coverage intake in the graph job (section 0): J7 signatures plus a status lookup, one read
   per (wallet, coin). Research key, inside the slot budget.
3. One-herder view with the pattern table. 0 calls.
4. Flow column and early-buyer column, once slices 3 and 3b have run and Gamma has ruled on
   section 2's amendment.
5. Bot trail and best herd member: pre-registration, then the job's new pass, then the page.
6. Non-Fomo seed intake.

The build uses the frontend-design skill; both views are plain tables and a three-column grid.

## Edge cases (CLAUDE.md rule 5)

1. **Money path:** none. Read-only pages; the new job passes are research reads on the research
   key through `funded_exit_truth.rpc`, which refuses a funded or watcher key, and they stop while
   a Solana position is open, as slices 3 and 3b do. A test pins the web import list.
2. **Fill-path speed:** none; web and job are separate processes. The job's added calls are
   capped per run and spaced as today.
3. **Concurrency:** per-request read-only connections; one writer (the hourly job).
4. **Absent, zero, unknown:** no reads is "not read", never "no herd". "Not measured yet" is a
   state, never 0. A herd of 0 regulars with 44 read coins (Roveryaps) is a measured zero and is
   shown as one. A coin a bot still holds is "open".
5. **Dependencies:** the graph job can stop (stamp shows age, stale after 3 h). Slice 3 and 3b
   tables can be absent. A truncated or undecoded read lowers a herd; the details show both counts.
6. **Units and identity:** `follows` src buys first; `dumps_on` src sells. A herd number is k of
   the herder's read coins. A member is an address; an address the registry maps to a roster
   wallet is shown as that wallet. Armed is per chain. Slot order is chain order; seconds between
   roster wallets come from the relay's clock.
7. **The sample:** until section 0 ships, herds exist only for buys we copied or the watcher saw,
   so the herder list is drawn from wallets we chose; after it, from every tracked wallet's buys
   that carry a transaction (a 77% ceiling today). More reads make more regulars by a fixed
   count, hence the share rule and the "few reads" label. Herd members are bots that buy in the
   same slot; they say a wallet is copied fast, not that copying it pays.
8. **Rollback:** revert the commit; stop a job pass with its budget flag at 0.

**Check left behind:** a test builds reads for herder A (10 read coins; bot X behind it on 4 in
the first-buy reads, bot Y on 1, bot Z ahead of it on 4, and a second, LATER read of one coin with
bot LATE behind), herder B (10 read coins, bot X on 3) and herder C (5 read coins, X on 3). It
asserts: A has 1 regular at "4 of 10"; LATE is not a member (only the unit read counts); Y is a
member and not a regular; Z is not in the herd; C shows "too few read coins to name regulars";
X is a probe with two herds (A and B, not C); A and B share one regular. Proven red by counting
companions regardless of transaction index, by counting any read of the coin, by naming regulars
under 10 read coins, and by dividing by the member's coins instead of the herder's read coins
(the "8 of 8" reading).

## Rulings

Gamma ruled on the regular rule, section 2's amendment, the bot trail, "profitable" and the flow
attribution in `docs/audits/2026-09-30-wallet-map-reads-preregistration.md` (17:40Z) and its
amendments. This draft applies her review of dd5001f7c: one unit read per (herder, coin), probes
and shared counts built from regulars, a third state under 10 read coins, truncated and
`not_found` out of k and n, one clock for coin age, derived stamp dates, and the uuid-only count
per herder.

## Not read for this draft

The research key's remaining monthly quota and its credit price per call. Every cost above is in
calls.

## Amendment, 2026-10-02

The 2026-10-02 wallet-universe delivery overlays the complete current non-excluded roster, including wallets absent from saved lists, and adds chain-specific setup/provenance views. Unavailable measurement caches do not disable separately verified live authority; unavailable authority disables money controls.

## Amendment, 2026-10-05: research table and authority separation

The default funnel endpoint now builds live roster controls without reading the
historical performance cache. Candidate and rotation rankings are retired from
that panel. The coverage table reuses the universe's owned research artifact;
no additional polling or provider requests are added. Historical sampled returns
require an explicitly opened forensic archive, with no operator controls or
ranking. The October 4 execution-research spec's October 5 table amendment governs
its denominators and missingness.
