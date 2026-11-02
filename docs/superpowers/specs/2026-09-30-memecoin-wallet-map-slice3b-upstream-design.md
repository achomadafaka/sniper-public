# Memecoin Wallet Map, slice 3b: upstream discovery (pre-registration)

**Date:** 2026-09-30 · **Author:** Lambda · **Status:** PRE-REGISTERED. Gamma CLEAN on round 3
(a6ccd12dd, 2026-09-30). Round 1 (0aa587652) had 5 MEDIUM and 4 LOW; round 2 (86b91af70) had 1
MEDIUM and 3 LOW. All are folded in below and marked "(Gamma M1)", "(Gamma R2)" and so on. The
three decisions below were ruled by Alpha on 2026-09-30; the operator may overrule them. No
upstream read ran, and no wallet's result was looked at, before that. · **Parent spec:**
`docs/superpowers/specs/2026-09-30-memecoin-wallet-map-design.md` (slice 3b outline, operator YES
2026-09-30, priority 8cebdf00) · **Builds on:** slice 3
(`docs/superpowers/specs/2026-09-30-memecoin-wallet-map-slice3-impact-design.md`, built on
`lambda/wallet-map-impact`) · **Order:** Alpha, 2026-09-30: spec first, to Gamma; state the
research-key call budget; nothing on the funded read lane.

## Question

Which wallets, not on our roster, bought a coin shortly BEFORE one of our Solana sources bought
it, and did so on coins that were higher 60 s after our source bought more often than that same
source's other coins were?

The comparison is inside each source and inside launch or not-launch (Gamma M1). A coin's price a
minute after a roster source buys it carries that source's copier crowd, and sources differ, so
"more often than all coins" would reward a wallet for standing in front of a heavily copied
source.

Slices 1 to 3 describe wallets we already follow. Strangers enter those slices only as in-slot
companions, which trade after the source. 3b looks the other way, at the buyers ahead of the
source.

The result is descriptive. A wallet that qualifies is called an **upstream candidate**. It is
shown on the map with its evidence. It never arms, sizes, admits or exits anything, and it never
reaches funded arming from the map: the funded roster changes only on funded results (operator,
2026-09-11).

## What was measured before writing this (read-only, 2026-09-30)

- **Population.** The graph DB holds 2,572 slot reads for the 14-day window, 2,561 of them `ok`.
  They give 759 first buys (one per source and mint), on 660 distinct mints, by 65 sources; 526
  are detection-fed and 233 fill-fed. That is about 54 first buys a day.
- **The Solana tape cannot label winners.** Of the 614 first buys whose mint has a pool in
  `data/memecoin_sol_tape.db`, 526 have no swap row in the hour after the source buy, and only 23
  have 10 or more. The tape follows pools of positions we held. So the win label below comes from
  the same Helius window read that finds the early buyers, not from the tape.
- **The slice-3 smoke read (6 reads):** 3 were launch reads (the mint was 8, 40 and 55 s old at
  the source buy), 2 were USDC pools, 1 was truncated (over 1,000 transactions in 121 s). Slice 3
  could use none of them. 3b can use the USDC pools (a price ratio needs no SOL unit) and needs
  the launch windows most of all.
- **Attribution is the weak point.** `dump_forensics.trade_of` names as trader the largest mover
  of the mint other than the pool. On routed swaps that can be an intermediate account, and it
  has misread trades before (memory `feedback-the-largest-mover-is-not-the-source`). For a rule
  whose output is a list of wallet addresses, that is not good enough, so 3b uses its own rule
  (see "Early buyer").

## Operator decisions (ruled by Alpha, 2026-09-30; the operator may overrule)

**Ruling (Alpha, 2026-09-30, operator may overrule).** The operator had the three questions twice
and did not answer. Lambda's proposal, Gamma's read and Alpha's agree and the job is read-only
research, so Alpha ruled all three as proposed: (1) a winning coin is +15% at 60 s against the
source's fill, with the shrunk per-unit null as registered; (2) launch windows are read, at most
10 calls per launch coin; (3) the forward grade is inside the job, one read at 8 units. Slice 3c
(a paper lane for candidates) is NOT approved. Budget and lane as registered: 150 research-key
calls per run, flat Solana book, nothing on the funded read lane.

| # | Question | Proposed | Gamma's read |
|---|---|---|---|
| 1 | What is a winning coin? | The price 60 s after the source buy is at least 15% above the source's own fill price (rule below). It costs no extra call on most reads. A longer horizon needs one more call per coin | Accept 60 s and +15% only with the stratified null (now in the rule) |
| 2 | Launch windows | Read them. Slice 3 skips a coin that is 60 s old or less, by operator decision, because it has no "before" baseline. For 3b the launch window is where the earliest buyers are. Cost: at most 10 calls per launch coin, inside the budget below | Yes; the launch stratum keeps them apart |
| 3 | Forward grading | In this slice the job grades each candidate forward from its own later reads (rule below). Feeding candidates into the paper lane needs the unarmed watcher to follow addresses that are not on Fomo or J7; that is a separate spec (3c) and a separate operator decision | Yes, with a single forward read (now in the rule) |

## The rule (method `upstream-v1:pre60:launch:signer-paid001:win60p15:fxwin:trade01:nofill:m8:strat-shrink10-pb:bh05:k3:d2:fwd8`)

### Unit of observation

One unit per **coin**: among slice 3's units (the first `ok` slot read per source and mint), the
earliest by chain time across all sources for that mint. A coin bought by three of our sources is
one unit, at the first of the three buys.

- `origin` is carried from slice 3: `detection` (the watcher saw the buy; our gates did not select
  it) or `fill` (it reached the graph only because we filled).
- The test and the main numbers use `detection` units only. Fill-fed units are read and reported
  apart, as in slice 3.
- `t0` is the source buy's chain time, as in slice 3.

### Reads (research key, Solana book flat)

Every call goes through `funded_exit_truth.rpc`, which reaches `pick_research_url`; that function
refuses a funded or watcher key. Before every call the job checks `solana_book_open`; an open
book stops the pass for this run and censors nothing. **Nothing here uses the public Solana RPC
(the funded read lane), the funded Jupiter seam, DexScreener or GeckoTerminal.**

1. **Age probe (1 call).** Slice 3's probe; its stored `age_s` is reused when slice 3 already
   read the coin.
2. **Window read (at most 10 calls).** The same call as slice 3, `getTransactionsForAddress(mint)`
   ascending, 100 per page, succeeded only, with:
   - `blockTime in [t0 - 60, t0 + 60]` when the coin is older than 60 s;
   - `blockTime in [first transaction, t0 + 60]` when it is 60 s old or less (a launch window;
     operator decision 2).
3. **End-price read (1 call), only when the window was truncated after the source.** One page of
   100, `sortOrder: "desc"`, with `blockTime in [t0, t0 + 60]`: the last transactions of the
   minute, which the truncated ascending pages did not reach.

The worst case is 12 calls per coin. A read starts only when 12 calls fit the remaining budget.

- **One fetch serves both slices.** When slice 3 reads a coin's window, the same pages are
  decoded for 3b in the same run; that costs no call. A coin slice 3 read before 3b shipped, or
  skipped as a launch, is read under 3b's own budget.
- **Budget: `UPSTREAM_BUDGET = 150` calls per run**, CLI flag `--upstream-budget`, after the
  impact pass and before the funder reads. Every Helius call in a run stays under
  `SLOT_BUDGET_MAX = 2000`. At about 54 first buys a day and half of them launches, a day needs
  about 27 x 10 = 270 launch-window calls; 24 runs x 150 = 3,600 calls a day is the ceiling, so
  the 14-day backlog (660 coins, at most 7,920 calls) drains in about 3 days.
- Pending coins are read detection-fed first, newest `t0` first.

### Early buyer

An early buyer of a unit is an address for which ALL of these hold in one transaction of the
window:

1. the transaction is before the source transaction in chain order (slot, then
   transactionIndex), and its blockTime is `>= t0 - 60` (or it is in the launch window);
2. the address is a **signer** of the transaction;
3. the address's own balance of the mint, summed over its token accounts in
   `preTokenBalances` / `postTokenBalances`, rises in that transaction;
4. the address paid in that transaction (Gamma M4): its SOL balance falls by more than the
   transaction fee PLUS 0.01 SOL, or its own balance of WSOL falls by at least 0.01, or its own
   balance of USDC falls by at least 1.

So the buyer is proven by its own signature and its own balance deltas, not by `trade_of`'s
largest mover and not by the tape's `maker` (the fee payer). Rule 4 keeps out a credit that is
not a buy (an airdrop claim, a distribution to holders): on 2026-09-30 J7 reported such a credit
on 4663 as a buy.

- **Why a floor and not "more than the fee".** A claim the wallet signs also loses SOL beyond the
  fee: a new token account costs 0.00204 SOL in rent, and a Jito tip is a transfer outside
  `meta.fee`. Without the floor a signed credit is a buyer. The job stores, per unit, how many
  signers received the mint and fell under the floor (`n_under_floor`).
- **Control on known buys, before any verdict (Gamma M4).** The source transaction of every unit
  is a known buy. The same four rules (without "before the source") must name the source's own
  address as the buyer in its own transaction. The job reports `control_named / control_total`
  over `ok` units each run. **Pass mark: 90%.** Below it, no wallet gets a verdict
  (`stamp.upstream.control_failed`), because a rule that misses known buys would also miss
  early buyers. A source that buys through a relayer that signs for it fails the control on
  that unit; the share of such units is the share of early buyers the rule cannot see.
- **The control comes first and needs no wallet result (Gamma R2).** It reads only the source
  transactions. The first run reports it (named / total, and of the failures how many are "the
  source did not sign" against "under the floor") BEFORE any verdict is computed. A failed
  control is answered by a pre-registered amendment of the rule, never by lowering the mark
  after the number is seen.
- **Unnamed receivers (Gamma LOW 1).** Per unit the job stores `n_unnamed`: transactions before
  the source in which some owner's balance of the mint rose and the rule named no buyer. It is
  shown beside the unit, so an undercount is visible instead of silent.

- A buyer counts once per unit, at its first such transaction. `lead_s = t0 - blockTime`.
- A wallet that buys through a relayer that signs for it is missed. That undercounts; it cannot
  name a wrong wallet.
- Excluded, never stored as a buyer: our own addresses (`MEMECOIN_OWN_ADDRESSES`); every
  verified address and `pump:<address>` id of a roster source (`solana_owners`); the mint's
  creator when the launch event names one.
- Excluded from the test, but stored: a wallet that is a `slot_follower` companion of any source
  in the current graph (a copy bot of our sources), and a wallet whose first funder is a hub
  (`shared_funder` evidence with `funder_degree` over the hub bound). Hub status needs a funder
  read; only wallets that reach the test are funder-read, inside the existing
  `FUNDER_BUDGET`. A wallet whose funder is unread is `unknown`, not a candidate.

### Winning coin (operator decision 1)

- `p0` = the source's own fill price: the quote it paid divided by the tokens it received, from
  the source signer's own balance deltas in the source transaction. The quote is the pool's
  (SOL or USDC).
- `p_end` = the median price of the LAST 3 decoded trades in the same quote with
  `blockTime in (t0, t0 + 60]`, after the source in chain order. On a truncated window they come
  from the end-price read.
- `ret_60 = p_end / p0 - 1`. **The coin is a winner when `ret_60 >= 0.15`.**
- Fewer than 3 such trades in a fully read minute: the coin did not trade enough for anyone to
  sell into a gain. It is **not a winner**; that is a known outcome, not an unknown one.
- The mark carries a tolerance of 1e-9 (build note): in binary `p_end / p0 - 1` of a nominal
  +15.0% is 0.1499999999999999, and no ratio gives exactly 0.15.
- The label is unknown, and the unit is `no_label`, when: the source's own deltas give no price;
  the end of the minute was not read (truncated, and the end-price read failed or gave fewer
  than 3 decodable same-quote trades); or the trades after the source are in another quote
  than `p0`.
- `+15%` is a judgement, not a measurement: it has to sit above what a copier pays to get in and
  out. The job stores `ret_60` itself, so the share of winners at `+5%` and `+30%` is reported
  beside the verdicts as a sensitivity line. Only `+15%` is tested.
- The label reads the price AFTER the source's own buy and after whoever copies it. So it says
  "the coin was higher 60 s after our source bought", not "the coin rose on its own" (Gamma LOW 2);
  the stratified null below is what keeps that crowd out of the claim.
- **Our own fills (Gamma M5).** A detection-fed unit means the watcher saw the buy, not that we
  did not fill it. Our entry lands seconds after the source and our exit often inside the
  minute, and on a nano coin our clip moves the price by a few percent. A unit whose
  `(t0, t0 + 60]` holds one of our fill signatures (`our_fill_sigs`, entries and exits) is
  flagged `our_fill`. It is kept out of the test and out of the null, and the win rate of those
  units is reported beside the rest.

### Status (one per unit; the first rule that applies wins)

| Status | Meaning | In the test |
|---|---|---|
| `unread` | the book was open, the budget ran out, or a call failed | no, retried |
| `probe_empty` | the age probe returned an empty page 3 runs in a row | no |
| `truncated_before` | the 10 pages ended before the source transaction | no |
| `not_found` / `clock_mismatch` | the source transaction is not in the window, or its blockTime is not `t0` | no |
| `no_label` | early buyers are complete but the win label is unknown | no |
| `our_fill` | complete and labelled, but one of our fills is inside the label minute (Gamma M5) | no, reported apart |
| `ok` | early buyers complete, the label known, none of our fills in the minute | yes |

### The null for one unit (Gamma M1, R2)

Every detection-fed `ok` unit `i` gets its own null probability `p_i` of being a winner:

    p_i = (w + 10 x r) / (n + 10)

- `n` and `w` are the count and the winners of the OTHER `ok` units of the same source and the
  same launch flag (unit `i` left out);
- `r` is the win rate of all other `ok` units with the same launch flag, any source;
- when that launch-flag set has fewer than 10 units, the unit has no `p_i` and is not used.

So a wallet is compared with what the same source's other coins did, launches with launches, and
a source with few units is pulled toward the launch-flag rate with a fixed prior weight of 10.

- **Why shrink (Gamma R2).** A raw rate over 10 units has a standard error near 0.15, and the
  test reads `p_i` as exact. A stratum with no winner would give `p_i = 0`, and one win there
  would make the tail exactly 0 and the wallet a candidate whatever else it did. With the
  formula, `n = 0` gives `r`, and `p_i` is never 0 or 1 unless `r` is.
- **Small strata are shown.** Per wallet the job stores `m_small`: how many of its `m` units
  have `n < 10`. Those units are compared mostly with other sources' coins, which is the
  confound the stratum exists to remove; the hover shows `m_small`.

### Per wallet (detection-fed `ok` units that have a `p_i`)

For a wallet `w` that is not excluded:

- `m` = the number of units (distinct coins) where `w` is an early buyer;
- `k` = the number of those that are winners;
- `expected` = the sum of `p_i` over those units;
- `days` = the number of distinct UTC days among its winning units;
- `lead_median_s` = the median `lead_s` over its units.

### The test (shown on the map as "this run only")

- **The family (Gamma M2):** every non-excluded wallet with `m >= 8`. Nothing about the outcome
  decides membership.
- `p_value` = the Poisson-binomial upper tail `P(X >= k)`, where `X` is the sum of independent
  Bernoulli(`p_i`) over the wallet's `m` units (exact, by the usual O(m^2) recursion).
- Benjamini-Hochberg at q = 0.05 over the family.
- Verdict:
  - `candidate`: q <= 0.05 AND `k >= 3` AND `days >= 2`. One coin, or one day, never makes a
    candidate;
  - `not_distinguished`: in the family and not a candidate;
  - `untested`: `m < 8`;
  - `unknown`: the funder is unread, or the control failed.
- The null is "this wallet's early coins win as often as the same sources' other coins of the
  same kind". A bot that buys every launch in front of one source sits at that source's launch
  rate and is not a candidate.

**Limits.** The Poisson-binomial treats the units as independent. Units of one source on one
day share a market and a crowd, so they are not. `days >= 2` softens that; it does not remove it.

**What a candidate is not.** It is a wallet that was early on coins that beat the same sources'
other coins more often than chance, in this window. It may be a trader our sources copy, a
sniper with a good filter, or part of a group that makes the coin move. The map says "early on
winners", never "smart money" or "insider".

**Power and repeated looks (Gamma LOW 4).** About 54 first buys a day, some censored. A wallet
needs 8 of them in 14 days, so only a very active wallet can be tested at all. No candidate in
the first weeks is a likely and valid result. The test also runs every hour on a moving window,
so a wallet with no edge has many chances to be a `candidate` in SOME run. The hover says "this
run only". The only claim this slice makes about a wallet is the single forward read below.

### Forward grade (operator decision 3; Gamma M3)

- The first run in which a wallet is a `candidate` stamps `first_candidate_at`. The stamp never
  moves.
- Forward units are the wallet's units with `t0 > first_candidate_at` that are `ok` and have a
  `p_i`, in time order.
- **One read, at exactly 8 forward units.** When the 8th exists, the job computes the
  Poisson-binomial tail of the wallet's forward winners against those 8 units' own `p_i` (each
  `p_i` as it stood in the run that makes the read), stores `fwd_k`, `fwd_expected`, `fwd_p` and
  `fwd_verdict`, and never computes them again.
- `fwd_verdict`: `confirmed` when `fwd_p <= 0.05`, else `not_confirmed`; `pending` below 8 units.
- The forward reads are not corrected across candidates: with several candidates, some
  `confirmed` by chance is expected, and the map says how many forward reads have been made.
- Nothing is attached to `confirmed` except its label on the map. Following a confirmed wallet on
  paper is slice 3c.
- Forward units live in the 14-day window (build note, Gamma 2026-09-30). A candidate that gathers
  forward units slower than 8 in 14 days stays `pending`, and its "first eight" are the earliest
  eight still stored. That depends on time, not on outcomes.

## Storage (graph DB, `data/memecoin_wallet_graph.db` under the STATE ROOT)

<!-- schema:external=memecoin_wallet_upstream_reads -->
| column | note |
|---|---|
| `signature` PK | the unit's source transaction |
| `mint`, `source_user_id`, `origin`, `t0`, `age_s`, `launch` | from the unit |
| `status`, `label_reason` | the table above; why a `no_label` unit has no label (`source_price`, `end_unread`, `end_thin`, `other_quote`) |
| `n_before_tx`, `n_early` | transactions before the source in the window; early buyers kept |
| `n_unnamed`, `n_under_floor` | receivers the rule named no buyer for; signers under the payment floor |
| `n_before_buy_tx`, `n_before_sell_tx`, `n_before_nomove_tx` | POST HOC counters: before-transactions with a signer buy, a signer sell, no move of the mint |
| `control_named`, `control_fail` | 1 when the rule names the source as buyer in its own transaction, else 0 with the reason (`not_signer`, `under_floor`, `no_rise`) |
| `p0`, `p_end`, `quote`, `fx_rate`, `fx_legs`, `fx_rejected`, `ret_60`, `win` | the label; `win` is 1, 0 or NULL; `fx_rate` is the window's USDC per SOL (amendment A1), `fx_legs` the pool legs behind it, `fx_rejected` a rate outside the run's 5% band |
| `n_end`, `n_end_other` | priced trades after the source in the minute; trades in a quote that could not be compared |
| `our_fill` | 1 when one of our fills is inside the label minute |
| `p_null` | the unit's `p_i` in the last run; NULL when it has none |
| `empty_probes`, `calls`, `method_version`, `read_at` | |

<!-- schema:external=memecoin_wallet_early_buys -->
| column | note |
|---|---|
| `signature`, `trader` PK | one row per early buyer per unit |
| `lead_s`, `slot`, `tx_index` | its first buy before the source |
| `tokens` | the rise of its own balance |

<!-- schema:external=memecoin_wallet_upstream -->
| column | note |
|---|---|
| `trader`, `method_version` PK | |
| `m`, `m_small`, `k`, `expected`, `days`, `lead_median_s`, `p_value`, `q_value`, `verdict`, `verdict_reason` | the test |
| `coins` | JSON, at most 10 units: mint, t0, lead_s, ret_60, win, p_null |
| `first_candidate_at`, `fwd_m`, `fwd_k`, `fwd_expected`, `fwd_p`, `fwd_verdict`, `fwd_read_at` | the forward grade; written once |
| `excluded` | `copy_bot`, `hub_funded` or NULL |
| `window_from`, `computed_at` | |

- `memecoin_wallet_upstream` is rewritten each run, except `first_candidate_at` and the forward
  columns, which are carried.
- Size: at most 1,000 early-buy rows per unit (10 pages); the 14-day window is about 760 units.
  Rows older than the window are deleted by the run.

## Map changes

- The payload gains `stamp.upstream` (`method_version`, `computed_at`, `candidates`, `family`,
  the win rate by launch flag, the win rate of `our_fill` units, `control_named / control_total`,
  `forward_reads`, and the `+5%` / `+30%` sensitivity shares) and a node kind
  `upstream_candidate` for verdict `candidate` only.
- A candidate node's hover: `m` (and `m_small`), `k` against `expected`, `q`, "this run only", `lead_median_s`,
  its coins, and its forward grade (`fwd_k` of 8 against `fwd_expected`, or "pending n of 8").
- An edge type `upstream_of` from a candidate to each source whose buy it preceded, with the
  number of coins.
- A knob "upstream candidates", on by default. "Hide strangers" does not hide candidates.
- Wording: "early on coins that were higher 60 s after our source bought (+15%), against the
  same source's other coins". Never "smart money", "insider" or "leader".
- A missing table is `stamp.upstream = null`, not an error, as in slice 3.

## Edge cases (CLAUDE.md rule 5)

1. **Money path:** none. The job is the research script on the research key; the map reads only.
   No funded import (`evm_real_seams`, `solana_real_seams`, Jupiter, price helpers). The map's
   import test covers the new code.
2. **Speed on the fill path:** nothing runs there. The job is a cron; its calls are paced 1.1 s
   apart on the research key and stop when the Solana book is open. 150 calls add about 2.75
   minutes to a run. The web payload reads two more small tables; its build is measured again
   (slice 3: p50 43.1 ms, p90 49.5 ms).
3. **Concurrency:** the graph job is one process; the web opens the graph DB read-only. A run
   rewrites the per-wallet table in one transaction.
4. **Absent, zero, unknown:** no early buyer on a unit is a known zero (`n_early = 0`), and the
   unit still counts in the other units' `p_i`. A unit without a label is `no_label` and counts
   nowhere. A unit with no `p_i` (a launch-flag set under 10 units) is not used. A wallet with an
   unread funder is `unknown`. `win` is NULL, never 0, when the label is unknown.
5. **Dependencies:** a failed or rate-limited call leaves the unit `unread` and ends the pass; an
   empty probe is retried 3 runs, then `probe_empty`. A truncated window is used only when the
   source transaction is inside the pages read. A decode failure on one transaction loses that
   buyer, never the unit.
6. **Units and identity:** prices are quote per token inside ONE pool quote per unit; a unit
   whose end trades are in another quote has no label. Times are chain blockTime; order is slot
   then transactionIndex. A buyer is a signer address, never a token account and never the fee
   payer alone.
7. **The sample:** a unit exists only because one of OUR sources bought the coin, and
   detection-fed units exist only while the watcher ran. So every rate here is about coins our
   sources buy, not the market, and a candidate is "early on coins our sources bought".
   Truncated-before units are the busiest coins; they drop out, and the stamp reports how many.
   A roster buy whose slot read is not `ok`, or that the watcher missed, is not a unit: the unit
   then starts at a LATER roster buy, and the copy bots of the earlier source can appear as
   early buyers. The `slot_follower` exclusion covers part of that.
8. **Rollback:** revert the code; the three tables can stay and nothing else reads them. The
   map falls back to slices 1 to 3 when `stamp.upstream` is null.

## Checks left behind (each proven red on a mutant)

- an early buyer needs its own signature, its own balance rise and its own payment over the
  floor: a fee payer that receives nothing, a largest mover that did not sign, and a signer that
  only receives a credit and pays account rent plus a tip under the floor are not buyers (the
  fixture carries the rent);
- the control: the rule names the source in its own transaction; under 90% named, no wallet gets
  a verdict; `n_unnamed` and `n_under_floor` are counted;
- a unit with one of our fill signatures inside `(t0, t0+60]` is `our_fill`: out of the test and
  out of every `p_i`;
- a transaction at or after the source in chain order is never early, inside the source's second
  too;
- our own addresses, roster addresses and the creator are never stored; a slot-follower and a
  hub-funded wallet are stored and never tested;
- the launch window starts at the first transaction; a coin older than 60 s uses `t0 - 60`;
- the label: `p0` from the source's own deltas; `p_end` is the median of the last 3 same-quote
  trades after the source; fewer than 3 in a fully read minute is not a winner; another quote,
  or an unread end of the minute, is `no_label`;
- a truncated window is `ok` for buyers only when the source is inside it, and then makes the
  descending end-price read;
- one unit per coin: the earliest source buy; a second source's buy of the same coin adds nothing;
- `p_i = (w + 10 r) / (n + 10)` computed by hand on one stratum, the unit left out; `n = 0`
  gives `r`; no `p_i` when the launch-flag set is under 10;
- a wallet with 3 wins on units whose own-source stratum has 0 winners in 10 is not a candidate
  by that alone; a wallet in front of one heavily copied source is not a candidate when that
  source's other coins win as often;
- `m_small` counts the wallet's units with `n < 10`;
- the control is reported before any verdict, with its two failure reasons;
- the family is `m >= 8` whatever `k` is; `k >= 3` and `days >= 2` only gate `candidate`; the
  Poisson-binomial tail and BH by hand;
- fill-fed units never enter a `p_i` or the test;
- `first_candidate_at` never moves; the forward read happens once, at exactly 8 forward units,
  and a ninth unit changes nothing;
- the worst-case budget: a read starts only when 12 calls fit; an open book stops the pass;
- the map: `upstream_candidate` nodes only for `candidate`; the wording; a missing table.

## Out of scope

- Following candidates on paper (slice 3c) and anything funded.
- A horizon longer than 60 s, and a peak-return label (both need more calls per coin).
- EVM chains. Ink source discovery (task 8e39ba39) stays paused by the operator; this method is
  its template when it resumes.
- Re-decoding slice 3's impact numbers; slice 3's method and reads are unchanged.

## Amendments before any window read (2026-09-30, with the build; for Gamma's review)

Both come from the control sample below. They change how a price is read, not who is a buyer, what
a winner is, the null or the test. No window was read and no wallet result was looked at before
they were written. The method string carries them (`fxwin`, `trade01`).

### The control sample (the instrument, not a result)

The source transactions of the 40 newest detection-fed units were read on the research key
(40 `getTransaction` calls, flat Solana book, 2026-09-30). These are our own sources' buys.

- The buyer rule names the source in its own transaction on **40 of 40**. 25 of the 40 are
  co-signed by a relayer that pays the fee and receives nothing; the rule never names it.
- **27 of 40 sources paid in USDC, 13 in SOL.** Our sources are Fomo users and the app pays USDC
  through a route. As registered, `p0` is what the source paid per token and the end trades must
  be "in the same quote", so a USDC source followed by SOL traders would be `no_label`.
- The pool-leg decoder (`dump_forensics.trade_of`) does no better: it returned nothing on 7 of
  the 40 and the wrong side on 2, all coins whose pool is quoted in a third token (a tokenized
  stock, another memecoin), where neither SOL nor USDC sits on the mint's pool.
- 28 of the 40 transactions carry a SOL/USDC pool leg of their own route. The rate it implies
  ran from 117.9 to 121.7 USDC per SOL over the days sampled.
- On the 6 curve buys both decoders read, the signer's own price was 1.0267 times the curve's
  event price on every one: the curve fee. It is the price a copier pays.

### A1. A trade in the other quote is converted with the window's own SOL/USDC rate

- The window rate is the median, over the window's transactions, of `|USDC| / |WSOL|` for
  accounts that did not sign, whose own WSOL and USDC moved in opposite directions, with at
  least 0.01 WSOL moved (a SOL/USDC pool leg of a routed swap).
- When the window holds a rate, every price in the label (`p0` and the end trades) is in SOL:
  a USDC amount is divided by the rate. `quote` is then `SOL` and `fx_rate` is stored.
- When it holds none, the registered rule applies unchanged: only trades in the source's own
  quote price the end of the minute, and other-quote trades make the unit `no_label`
  (`label_reason = other_quote`).
- An error in the rate moves `p0` and the end trades together when they are in the same quote,
  and by the rate's own error when they are not; inside one minute that is well under the 15%
  mark.
- **Gamma's two additions (accepted with A1, 2026-09-30, before the smoke read).** The number of
  pool legs behind the rate is stored (`fx_legs`). A window rate more than 5% off the median
  window rate of the same run's reads is treated as no rate: the unit takes the label it has
  under the registered same-quote rule, and the rate it had is kept in `fx_rejected`. One odd
  leg would otherwise move every price of that unit. A run with one rated window has no median
  to compare with and keeps its rate. While the 14-day backlog drains, one run reads coins of
  several days, so a real move of SOL over those days can put a good rate outside the band; the
  run note counts them (`upstream_fx_rejected`).

### A2. A trade prices the end of the minute only when it is at least 0.1 SOL or 10 USDC

- Every price here is what the signer paid or received, net of the fee it paid as fee payer.
  That includes the rent of a new token account (0.00204 SOL) and any tip. On a 0.02 SOL buy the
  rent alone reads 10% high.
- So a trade under 0.1 SOL (or 10 USDC) does not count among "the last 3 decoded trades". The
  rent then moves a price by at most 2%. `p0` has no floor: it is the source's own fill.
- "Fewer than 3 such trades in a fully read minute" keeps its registered meaning: not a winner.
  Each run reports how many `ok` units are not winners by that rule alone (`n_end < 3`), as
  `upstream_thin_losers` in the run note, so its share is visible (Gamma, 2026-09-30).
- Known and accepted: a buy reads the pool price plus fees and a sell reads it minus fees, so a
  source buy followed by sells reads a few percent lower than by buys. That is the round trip a
  copier would pay.

## Build notes (2026-09-30)

- Code: `scripts/research/wallet_upstream.py` (the rule, the null, the test, the storage; pure)
  and the reads and the pass in `scripts/research/wallet_link_graph.py` (`upstream_pass`). Map:
  `sniper/services/memecoin_wallet_map.py`, `static/wallet_map.js`.
- **Verdicts are off until the control has been read.** The job reads, labels and stores early
  buyers every run and writes the control (named / total, and the failure reasons) into the run
  note. It computes wallet verdicts only with `--upstream-verdicts`, which is not on the cron
  line. Turning it on is a step after Gamma has read the control and the status mix.
- **Not built: "one fetch serves both slices".** 3b reads its own window for every coin. At
  about 27 coins a day older than 60 s and 1 to 3 pages each, that is 30 to 80 calls a day the
  shared fetch would have saved, against a ceiling of 3,600. Sharing the pages means holding up
  to 27 windows of 1,000 transactions in memory on the funded host, or decoding 3b inside slice
  3's read. Build it when the research key's quota matters.
- Slice 3's stored age is reused, so a coin slice 3 already probed costs no probe.
- `our_fill` has two sources: one of our fill signatures inside the pages read, or a fill of
  ours on that mint in the ledger between `t0 - 10 s` and `t0 + 70 s` (ledger time against chain
  time). The second one covers a window cut before our fill.
- The label minute is `t0 < blockTime <= t0 + 60` and after the source in chain order, as
  registered: a trade in the source's own second does not price the end.
- The run drops the reads of units that left the window, except when the unit list is empty (a
  slot-read table that answered nothing): then nothing is dropped and the run note says
  `upstream_pruned=skipped_no_units`.
- A per-wallet row exists for a wallet with `m >= 8` and for a wallet that was ever a candidate.
  A wallet with fewer units is `untested` and has no row.
- `n_unnamed` counts a transaction before the source where a non-signer's balance of the mint
  rose, no signer's fell, and no signer's rose: a receiver the rule cannot name. A sell (the
  pool's balance rises) is not counted.
- The forward read needs 8 forward units inside the 14-day window: units older than the window
  are dropped, so a wallet that takes longer than 14 days to reach 8 stays `pending`.

## POST HOC instrument addition (2026-09-30, after the smoke read; Gamma's go)

The smoke read (3 coins, 28 calls, status and counters only) showed a launch with 978 transactions
before the source and 24 early buyers, 0 unnamed. The counters could not say what the other
transactions were. Three per-unit counters were added AFTER that read: `n_before_buy_tx`
(before-transactions in which a signer's balance of the mint rose), `n_before_sell_tx` (a signer's
fell) and `n_before_nomove_tx` (no balance of the mint moved). They feed no rule, no label and no
verdict, and no wallet result was read to decide them.

## Amendment A1b: the FX band compares each window with windows near it in time (Gamma, stamped 2026-10-01, Alpha's order)

**Why.** Gamma's A1 addition rejects a window SOL/USDC rate more than 5% off the median rate of the
same run's reads. The A1 text already warned that a run reading coins from several days could reject
good rates. That is now happening. The reader reaches back up to 14 days (runs 41 and 42 read coins
spread over 11.6 and 13.9 days), and SOL really moved by about 20% over that time: the window rates
are about 101 on 2026-09-17, about 121 on 09-26/27 and about 118 on 10-01. So a good rate is
rejected for being from a different day, not for being a bad pool leg. In run 42, three 10-01 coins
at 117.6 were rejected because older coins pulled that run's median down to about 111.

**Measured before stamping (rates and dates only, no outcome column).**

- 396 stored windows have a rate (`fx_rate` or `fx_rejected`).
- Under A1, 12 are rejected, all in runs 41 and 42.
- Under A1b, 0 are rejected. 5 keep their rate because fewer than 3 other windows lie within 12 h
  of them. All 12 A1 rejections would keep their rate.

**The rule (A1b).** A unit's window rate is compared with the median window rate of the OTHER
stored units whose `t0` lies within 12 hours of its own `t0` (either side). The comparison uses
every stored window rate, kept or rejected, from any run. If at least 3 such windows exist and the
rate is more than 5% off their median, it is treated as no rate, exactly as under A1: the unit takes
its label under the registered same-quote rule, and the rate is kept in `fx_rejected`. If fewer
than 3 such windows exist, the rate is kept. A1b replaces A1's same-run median; nothing else in A1
changes.

**Scope and recompute.**

- A1b applies to every unit, stored and future.
- Stored units are relabelled once under A1b. If a stored row cannot rebuild the A1b label from
  what is stored, that unit is re-read once on the research key, under the flat-book gate, with its
  calls counted.
- Lambda changes `apply_rate_band` in `scripts/research/wallet_upstream.py`. The method string
  gains a marker for A1b. Gamma recomputes independently.

**Reporting.**

- Slice 3b and the KOL-early Read 2 (`docs/audits/2026-10-01-wallet-map-kol-early-preregistration.md`)
  are reported under A1b as the primary result.
- Each is also reported under A1 as a labelled sensitivity line, with its rejection count.
- Read 2's "at least 30 KOL-preceded units" is counted under A1b.

**Disclosure (verbatim, required by Alpha).** While tracing the rejections, Gamma printed 3 rejected
rows that included their `win` values (1, NULL, 1). 2 of those coins are in Read 2. The amendment
was built from rates and dates only; no outcome was used to choose it.

**Stamped:** 2026-10-01T15:41:42Z (the commit time of this text is the stamp).
