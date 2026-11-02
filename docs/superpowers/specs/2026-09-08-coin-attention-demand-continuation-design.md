# Coin attention, trader anticipation, and demand continuation

Status: revised design direction. No funded implementation, activation, or live-policy change occurred.

This document supersedes the stop and holding-time rules in `2026-09-08-turnover-source-funded-setup-design.md`.

Proposed identity: `attention_demand_continuation_v1`.

## User direction

Identify the coin setup before a prominent trader buys. The trader may select the same market conditions that our system identifies.

Use a meaningful source buy as confirmation. Hold while demand supports continuation. Exit when that demand fails.

The design has no percentage stop and no maximum holding period. Do not retain a catastrophe price stop under another name.

Whether +25% causes an automatic take-profit remains a separate user choice. It remains an outcome measurement in either case.

## Two hypotheses, tested separately

**Selection hypothesis:** these traders find coins with favorable conditions that already exist before their buys.

**Influence hypothesis:** their buys attract additional buyers and create a further response.

The selection hypothesis does not require the influence hypothesis to be true. Follower count alone proves neither.

Degen may be a useful confirmation source because the wallet selects this setup repeatedly. This remains a hypothesis about the observed results.

The system should first predict eligible coins. It should not require a correct prediction of the exact trader who buys next.

## Coin-first sequence

1. Observe coins through the existing approved data sources before the qualifying source buy.
2. Record each coin's attention state and timestamp before its future outcome is known.
3. Rank coins by the measured conditions associated with later qualified source buys.
4. Prepare cached identity and route metadata within existing resource limits.
5. On an eligible source buy, recheck the current coin state and the executable entry.
6. Apply the existing wallet authority, custody, route, size, and reservation checks.
7. Manage the new position with the revised demand-continuation policy once that policy is explicitly activated.

Preselection does not authorize an anticipatory funded buy. It prepares the system to evaluate the confirming event promptly.

Do not reserve funds, sign, or submit an order because a coin enters the attention list.

A coin may leave the attention list without any selected trader buying it. Keep that observation in the research population.

## Attention state

Attention must have separate activity, participation, pressure, and liquidity measures.

| Measure | Required meaning |
| --- | --- |
| Raw volume and turnover | Trading activity relative to the selected market-cap basis |
| New buyer arrival | New observed buyer addresses per event-time window |
| Buyer breadth | Distinct observed buyers, with concentration reported separately |
| Seller participation | Distinct observed sellers and their executed sell notional |
| Net trade flow | Executed buy notional minus executed sell notional, with consistent units |
| Liquidity and route depth | Actual executable conditions on the relevant route |
| Source buy size | Amount relative to recent activity, route depth, and available prior trader history |
| Price expansion | The move that occurred before our executable entry |

Distinct addresses are not necessarily distinct people. Report known router, creator, and related-address exclusions without claiming complete entity resolution.

Count each economic swap once when several logs or routes describe it. Preserve chain, venue, event identity, and unit provenance.

Do not mix cumulative buyer counts with buyer-arrival rates. An old coin with many historical buyers can have no current attention.

Do not mix m5 volume with a short-window floor without matching the units and window definitions.

## Entry evidence retained from the prior study

The raw turnover band of 10–50% and source-buy range of $1k–$10k remain candidate entry features.

The study found 39 matches, 38 coins, 18 traders, and +$695.41 in recorded historical PnL.

Those outcomes used +25% take-profit and −35% stop thresholds. Most also had a four-hour maximum hold.

They do not estimate the return of this revised exit policy. Do not attach +$695.41 to a policy without those exits.

The study also starts after a source buy exists. It does not prove that the system could identify the coin before that buy.

The revised design therefore requires both pre-buy coin observations and separate exit evidence.

Do not add a nano requirement or a profitable-wallet requirement solely from the previous pooled tables.

## Demand death

Total volume is not sufficient. High sell volume can accompany a rapid decline while total activity remains high.

The proposed exit detects insufficient buying demand relative to sell pressure and executable liquidity.

The candidate evidence includes:

- New buyer arrival falls and buyer participation becomes concentrated.
- Buy notional weakens relative to sell notional.
- Net demand weakens relative to executable depth on the current route.
- Repeated healthy observations confirm the change.

These are candidate inputs, not calibrated exit thresholds. The current study does not supply a defensible numeric continuation floor.

There is no universal volume level that guarantees a +25% move. The same volume can produce different outcomes across liquidity, direction, and venue.

AMM price impact changes with available liquidity and swap size. See [Uniswap's price-impact explanation](https://developers.uniswap.org/docs/get-started/concepts/traders/swaps#price-impact).

The research must estimate whether comparable demand states continue to an executable +25%, stall, or deteriorate.

An executable quote describes the present route. It does not prove that future buying will arrive.

The observation window measures recent activity. It is not a maximum position holding time.

## Exit scope

| Condition | Proposed treatment |
| --- | --- |
| Percentage loss alone | No automatic exit |
| Position age alone | No automatic exit |
| Confirmed demand death | Request an exact full-position exit through the existing executor |
| Confirmed swap silence | Evidence for demand death when the relevant venue is covered and healthy |
| Missing or disconnected feed | Unknown; do not label the coin dead |
| Missing route or failed quote | Record inability to execute; do not invent a sale or a price |
| Operator close | Preserve the manual action and mark the result as operator-modified |
| +25% executable return | Always record the target touch; automatic sale awaits the user choice |

No fixed unquotable-age deadline may act as a hidden holding-time exit in this proposed policy.

Transaction expiry, confirmation timeouts, and retry rules retain their execution meaning. They do not force a position close because it is old.

Missing data can leave a position open. High activity can also continue during a large loss; measure these outcomes rather than conceal them.

Existing live positions retain their current contracts. This document does not remove their stops, time limits, or other exits.

## What the app already has

`sniper/services/memecoin_volume_death.py` measures confirmed swap silence. It also checks coverage, stream health, and venue-state evidence.

That rule does not calculate whether ongoing buy flow is sufficient for another 25% move.

Reuse its coverage and health logic. Do not rename the silence threshold as a demand floor.

The existing rule can veto an exit when executable prices move materially. A demand rule requires separate validation of that veto's meaning.

Price movement can reflect heavy selling. It must not automatically prove healthy buying demand.

The Radar state contains buyer-address sets for its supported venues. Those sets are not entity-resolved and do not cover every funded coin.

That evidence does not authorize a new Radar-to-funded or Radar-to-V6 admission path. Preserve the existing ownership boundaries.

Provider transaction counts remain transaction counts. They cannot replace unique buyer or seller data.

## Required proof

1. Build the candidate population before observing the future trader buy.
2. Retain coins that receive no qualifying trader buy, with the same observation and coverage rules.
3. Measure later trader selection against candidate rank, using a forecast window fixed before evaluation.
4. Compare pre-buy coin states within the same chain, period, cap range, and liquidity range.
5. Compare states with a qualifying source buy against contemporaneous states that do not yet have one.
6. Use risk-set comparisons so a future trader buy cannot create false earlier predictive power.
7. Measure exact-quantity target touches, demand-exit proceeds, route failures, and open inventory.
8. Test demand thresholds on a later period and remove the largest trader and coin contributions.

The forecast window measures prediction quality. It does not impose a position exit deadline.

Research-window closure leaves unresolved positions open or censored. It must not fabricate an exit or exclude open losses from the result.

The absence of a stop makes full-book and open-inventory evidence necessary. Closed winners alone cannot establish policy performance.

## Current boundary

The immediate deliverable is the revised design and a data-coverage check. Numerical demand thresholds remain uncalibrated.

Implementation must use a new explicit policy identity and a new observation cohort. It must not mutate the earlier frozen study.

No automatic promotion follows from trader popularity, the 39 historical matches, or a candidate's attention rank.

No backup, service restart, funded setting change, or production write occurred during this revision.
