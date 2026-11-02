# Memecoin Research workspace → a promotion screen

Status: approved 2026-09-23, operator. Supersedes the Research workspace's three subtabs.

## Why

The Research workspace answers a question nobody asked. Its three subtabs — `Paper results`,
`Paper positions`, `Traders (paper)` — are all built on `memecoin_positions` rows written by the
gate-less paper lanes, and measured on 2026-09-23 those lanes opened a position for **2,242 of
2,346** signals the funded path had REFUSED. So every per-wallet cell in the arm matrix answers
"were this wallet's coins good" while the operator reads it as "would we have made this money".

The two answers disagree by the whole result. The published example: a disarmed cohort showed
+$1,901 of paper PnL over 256 would-pass rows, 74% of it one wallet whose real record is **−$223
over 14 funded closes**, the top 5 rows were 83% of the sum across only 3 distinct coins, and the
median said the opposite of the mean.

`memecoin_promotion_lane` (Lane A, shipped 2026-09-23 as `fe947fd8` / `9a7c73fb`) writes
gate-faithful rows: it replays the reproducible funded gates through the funded path's own
evaluators and admits a modelled position only when they all pass. This spec rebuilds the workspace
around those rows.

## Decisions taken

1. **The gate-less paper data is retired from the UI entirely.** The lanes keep writing, and
   `scripts/paper_promotion_read.py` keeps consuming them for the registered 2026-08-28 prereg, but
   no screen shows them. Rationale: two columns per wallet, one honest and one misleading, is the
   failure mode this work exists to remove.
2. **Ship before the data arrives.** Lane A has no rows until the operator arms the knob. Each panel
   renders an empty state that names what would fill it. A layout problem is cheaper to find now.
3. **A backfill was considered and refused.** Gate inputs are decision-time quantities; the market
   has moved. The worker already refuses signals older than 900s for the same reason.

## The screen

One workspace, one subtab, one row per wallet, read left to right as a funnel.

| # | column | source | note |
|---|---|---|---|
| 1 | wallet, chain | `source_handle`, `network_id` | |
| 2 | signals seen | `COUNT(*)` | the denominator, before any of our behaviour |
| 3 | would have taken | `verdict='admitted'` n and % | the number that did not exist before Lane A |
| 4 | refused — top family | `refused_family` mode | why we would have passed |
| 5 | unreadable | `verdict='unreadable'` | NEVER folded into refused |
| 6 | outcome on admitted | n closed, **median**, sum, win%, censored | median leads; a mean here is a tail lottery |
| 7 | funded record | `memecoin_positions` `execution_mode='real'` | the reconciliation |
| 8 | promotion state | Gamma's rule | eligible / below n floor / failed |

Expanding a wallet lists its individual signals: stamp, coin, verdict, refusing family, and for
admitted rows the modelled outcome and `censored_by`.

A second panel pools every wallet into the lane's own funnel — refusals by family — so the operator
can see whether a gate is doing work or sitting inert.

### The header states the standing caveats once

Never per row, never omitted:

- admission is **looser than funded**: the chase families cannot be reproduced after the fact and
  are recorded `unknown_lagged`;
- paper pays no impact and no route failure, so every number is a **ceiling**;
- exits walk a **one-minute** series against a rail that sweeps in seconds, so both the stop and the
  take-profit **under-trigger**;
- every row carries `eval_lag_s`, and the header shows its p50/p90 for the window on screen.

## Data

New read-only endpoint `GET /api/memecoin/promotion` in `sniper/routes/api_memecoin.py`, returning
per-wallet aggregates and the pooled family funnel from `memecoin_promotion_lane`, plus each
wallet's funded record from `memecoin_positions`. Window and chain are query parameters, matching
the workspace's existing chain-filter behaviour.

Aggregation rules, which are the spec and not an implementation detail:

- the denominator is every evaluated row, so a share can never describe our own retention;
- `unreadable` is its own bucket at every level;
- a censored row (`exit_reason='open_at_horizon'`) contributes to n and to the censored count, never
  to a closed statistic, and carries **no** `exit_price` by design;
- the median is computed over admitted closed rows; the mean is shown beside it, never alone;
- a wallet under the n floor renders its numbers dimmed with the floor named.

## What is deleted

`static/app.js`: the `research` subtab buttons for `closed`, `positions`, `performance`; the
`mc-section` blocks for those three; the renderers behind `mc-stats`, `mc-closed`, `mc-open`,
`mc-paper-summary`, `mc-chart`, `mc-traders`; and the now-unreferenced calls to
`/api/memecoin/traders/arm-matrix`, `/api/memecoin/traders/combined` and `/api/memecoin/paper-report`
from this workspace.

**Verified before approval:** the arm matrix carries a live per-chain control
(`data-action="chainarm"`, plus `wexempt` for gate exemptions), not a badge. `Funded authority`
(`data-section="armed"`, `/api/memecoin/armed`) already carries the identical controls, so the
deletion removes no ability to arm, disarm or exempt a wallet. Any endpoint still consumed by
another workspace stays.

## Testing

- The endpoint's aggregation, against a fixture table: an unreadable row never counts as refused; a
  censored row never enters a closed statistic; the denominator is every evaluated row.
- Empty state: zero rows renders the "what would fill this" panel, not an error and not a zero that
  reads as a measurement.
- A UI test asserting the three retired subtab buttons are gone and that `Funded authority` still
  renders a `chainarm` control, so the deletion cannot silently take the arming surface with it.

## Out of scope

Lane B (gate and filter experiments on the armed roster) is parked by Alpha and gets its own
surface. Gamma's Lane A section of the 2026-08-28 prereg supplies the promotion rule; this screen
renders whatever that rule decides and does not define it.

## Open

The promotion-state column is inert until Gamma's rule lands. Until then it renders `—` with a
tooltip naming the prereg, rather than a placeholder verdict an operator could act on.
