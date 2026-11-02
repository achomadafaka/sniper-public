# Frozen win/loss evidence study — 2026-09-08

Status: observation only. The first deployed release with these labels defines the code identity.
The first natural version-2 shadow defines the corrected UTC start boundary. Historical reports cannot move this boundary.
The saved study-start receipt records the release and the first trace.

## Fixed candidates

| Label | Population and fixed input | Recorded result |
| --- | --- | --- |
| `turnover_10_50_v1` | Nonmanual attempt; frozen raw m5 volume divided by positive effective market cap | `would_allow` when `0.10 <= ratio < 0.50` |
| `nano_prior_profitable_v1` | Nonmanual attempt; positive effective market cap below $50,000 | `would_allow` after at least five eligible prior closes with positive total PnL |
| `robinhood_organic_m5_lt_1000_v1` | Nonmanual chain-4663 attempt; `max(0, raw_m5_volume - source_size_usd)` | Existing label: `would_block` below $1,000 |

The nano prior uses the same source wallet and chain. It excludes the target mint and the current position.
Prior rows must be closed real positions with finite PnL and positive entry notional.
The frozen funded contract must classify each prior row as copy. Missing contracts and manual contracts do not qualify.
The recorded close instant must precede or equal the decision instant. The comparison requires timezone-aware times.
The label stores count, PnL, notional, ROI, decision time, and population identity.
Absent target identity or a failed read produces `unknown`. Fewer than five eligible closes produces `insufficient_history`.
The existing `prior_5_negative_v1` retains its legacy nonmanual population. It is a separate diagnostic.

## Collection and comparison

The existing immutable entry shadow stores these labels. No label changes an entry, exit, wallet, or position size.
Every attempt that reaches the existing terminal shadow can enter the study. Unknown inputs remain unknown.
The funded contract defines copy/manual/unknown outcome populations. The analysis keeps these populations separate.
The collector does not use a proposed label to select or discard its control observations.

The existing refusal collector continues its Solana-only quote schedule and capacity limits.
It now recognizes `m5_turnover_under_big_floor`. It retains unfinished attempts and pages by insertion order.
It keeps exact origin identifiers. New quote records retain atomic quantities as decimal TEXT, token decimals, quote output, and an available output floor.
Records retain observation time, failures, and capacity exclusions. Historical rows keep absent quantities unknown.
The source-buy-floor family remains outside quote capture. No provider budget increases.
A restart starts a fresh observation window. It cannot recover historical quotes or completed observations from the outage.
This collector is incomplete outside its existing scope. Missing refusal outcomes prevent a complete executable gate-value claim.

## Fixed first read

The first read occurs 14 days after the release boundary. This document creates no scheduled task.
Use one chain/origin observation per candidate comparison. Show repeated origins and missing-origin rows separately.
Keep chains, funded policy cohorts, and configuration periods separate. Do not fit new thresholds during this window.
Show all admitted open positions and all censored outcomes beside closed-position results.
Show unknown and insufficient-history groups. Report refusal coverage against every eligible origin.
Do not compare admitted realized PnL with refused sampled price maxima.

For each candidate, report distinct coins, wallets, days, wins, losses, median return, total ledger PnL, and entry notional.
Report results without the best positive coin and without the best positive trader on each chain.
For EACH fixed candidate, remove its largest positive wallet contribution separately within each chain and recorded state.
Keep `would_allow`, `would_block`, `unknown`, `insufficient_history`, `not_applicable`, and `not_recorded` separate.
Rank wallets by total ledger PnL in that candidate, chain, and state. Use ascending source ID to resolve equal PnL.
Remove all positions from that wallet in that group. If no wallet contributes positive PnL, remove none.
Show the removed wallet, original PnL, remaining positions, valid outcomes, wallets, coins, entry notional, PnL, and weighted return.
The subtraction is descriptive. It produces no automatic rule or trader verdict.
The closed-position report supplies this subtraction. It does not replace the required origin, open-position, and censored-outcome checks.
Compare prior-count groups `0`, `1–4`, `5–19`, and `20+`, with target-mint exclusion and strict copy identity.
Keep prior PnL sign as a separate description within these fixed groups. Do not turn the old negative-prior label into a veto.

Below 100 distinct coins or 10 wallets on a chain, report descriptive results only.
Above both counts, report coin-cluster and wallet-cluster confidence intervals. Retain the less favorable interval.
Apply Holm correction across the three fixed candidate hypotheses. These counts do not establish statistical power or permission to trade.
A later period must reproduce any result before a new policy review. No result automatically changes funded authority.

## Exit evidence and costs

The report uses exact route observations only between the recorded entry and close times.
A close time can precede final chain confirmation. The report names that limit and does not append later price peaks.
An exit-policy comparison requires exact event times, quantities, route availability, operator changes, and full open/censored counts.
Known quote floors and expected output remain separate. Missing fees or gas keep total account return unknown.
Historical ledger PnL remains a separate reported measure. The inventory audit explains the two shared-sale records without a ledger rewrite.
