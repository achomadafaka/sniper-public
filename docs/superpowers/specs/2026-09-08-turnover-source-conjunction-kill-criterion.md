# Kill criterion: the turnover x source-size entry conjunction

Locked at: 2026-09-08T19:31:00Z (fill exact time when this ships). Author: Beta, at Alpha's
request. Supersedes the wait-for-a-shadow plan in
`2026-09-08-turnover-source-conjunction-preregistration.md`.

Status: pre-committed control on a gate that goes live directly, no observation period. The
operator's reasoning: on the historical population, the conjunction earned +$782.96 on $3,606
stake (n=44, +21.72%) against the refused population's +$39.40 on $24,078 stake (n=326, +0.16%).
If the conjunction is pure search artefact and refused trades perform like the accepted ones on
average, the expected loss from skipping observation is small: the accepted trades would earn
roughly what everything else earns, not less. A shadow period does not avoid a loss here; it
delays a probable gain. That argument only holds if a real kill switch exists and someone checks
it. This document is that switch.

**This document sets the numbers now, before the gate is live and before any forward close
exists. Do not edit the numbers after a forward close exists.** A later change is a new kill
criterion, not an amendment, and must say so and say why.

## 1. The gate this controls

Two independent existing gate families, both currently inert (`0` = off), become the conjunction
when both are set to their studied values:

- `MEMECOIN_REAL_INBAND_MAX_TURNOVER_M5` (paired with the existing
  `MEMECOIN_REAL_INBAND_MIN_TURNOVER_M5`): raw five-minute turnover in `[0.10, 0.50)`.
- `MEMECOIN_REAL_MIN_SOURCE_BUY_USD` / `MEMECOIN_REAL_MAX_SOURCE_BUY_USD`: source buy in
  `[$1,000, $10,000)`. Adds to the existing market-cap-ratio floor; does not replace it.

Both pairs land inert in the code (Gamma, `memecoin_real_runner.py` and `memecoin_settings.py`,
in flight at lock time). The operator sets the four numeric values separately. **Activation is the
moment all four are set to their studied values in the live environment, not the moment the code
merges.** Record that moment in section 6 before anyone reads a result under section 3.

## 2. The population this measures

Once live, every admitted `follow_activity` entry on `solana` or `4663` satisfies the conjunction
by construction — there is no separate shadow lookup to join, because the gate itself is the
filter. The kill-criterion population is:

    closed, execution_mode='real', signal_source='follow_activity',
    network_id IN ('solana','4663'), opened_at >= <activation timestamp from section 6>

Reuse the same population definition Alpha priced the decision on: `execution_mode='real'` and
`signal_source='follow_activity'`, not a broader or narrower set. A different population answers a
different question.

## 3. The kill triggers, fixed now

Compute, on the population in section 2:

    n_closes                   = COUNT(*)
    total_pnl                  = SUM(realized_pnl)
    total_stake                = SUM(entry_notional)
    stake_weighted_return_pct  = total_pnl / total_stake * 100.0

**KILL (turn every one of the four settings in section 1 back to `0`) the moment ANY of these
three conditions is true. Check after every new close, not on a fixed schedule — a kill condition
does not wait for the next scheduled check.**

1. **Fast floor.** `n_closes >= 10` AND `stake_weighted_return_pct <= -15.0`. Ten trades running
   15 points net negative is a large, real loss, not the variance of a handful of trades — the
   historical population's own worst single trade lost 80.54%, so ten trades can swing hard on
   noise alone, but a SUSTAINED 15-point deficit across ten trades is not that. This tier exists to
   catch a gate that is actively wrong (backwards logic, a stale value, a wrong field) fast, before
   it compounds.
2. **Standard floor.** `n_closes >= 20` AND `stake_weighted_return_pct < 0.0`. The operator's own
   argument for skipping the shadow period was "the downside is bounded near zero... we do not
   lose." Any net loss after 20 closes falsifies that argument on its own terms. This tier does not
   require a large deficit — zero is the line, because the premise that justified skipping
   observation was zero, not a smaller number chosen after the fact.
3. **Single-trade fragility.** `n_closes >= 20` AND removing the single largest-PnL closed
   position from the population flips `stake_weighted_return_pct` negative. Mirrors the
   sensitivity check the original audit already ran (removing the largest trader) and the check
   the superseded pre-registration required before a GO. A result that depends on one trade is not
   a result; the historical population's own return survived removing its largest trader
   (+$695.41 to +$524.43, still strongly positive) and this bar asks the forward population to do
   the same.

**No kill trigger requires waiting past `n_closes = 20`.** If none of the three conditions is true
at n=20, continue running and keep checking after every close; there is no separate re-approval
step and no upper bound on continued operation while the gate stays clean.

## 4. The check is a query, not a judgment call

```sql
WITH pop AS (
  SELECT realized_pnl, entry_notional
  FROM memecoin_positions
  WHERE execution_mode = 'real'
    AND is_open = 0
    AND signal_source = 'follow_activity'
    AND network_id IN ('solana', '4663')
    AND opened_at >= :activation_timestamp
    AND realized_pnl IS NOT NULL
    AND entry_notional IS NOT NULL AND entry_notional > 0
)
SELECT
  COUNT(*)                                        AS n_closes,
  SUM(realized_pnl)                                AS total_pnl,
  SUM(entry_notional)                              AS total_stake,
  100.0 * SUM(realized_pnl) / SUM(entry_notional)  AS stake_weighted_return_pct,
  (SELECT 100.0 * (SUM(realized_pnl) - MAX(realized_pnl))
     / NULLIF(SUM(entry_notional) - (SELECT entry_notional FROM pop
              ORDER BY realized_pnl DESC LIMIT 1), 0)
   FROM pop)                                       AS return_pct_ex_largest_trade
FROM pop;
```

`:activation_timestamp` is the value recorded in section 6. Reading `n_closes`, comparing
`stake_weighted_return_pct` and `return_pct_ex_largest_trade` against section 3's thresholds is
the entire check. It requires no interpretation of the numbers, no chart, and no comparison to a
backtest figure not written down here.

`scripts/research/turnover_source_kill_check.py` runs this query against the live database and
prints a `RUNNING` or `KILL` verdict with the reason. Build it before activation, not after the
first close, so the check exists before anyone needs it.

## 5. What this control does not do

- **It does not bound the loss before the fast floor's ten trades close.** Ten trades at the
  historical worst-case single-trade loss (-80.54%) on a full-size clip could exceed the fast
  floor's dollar-equivalent before `n_closes` even reaches 10, since the fast floor is
  percentage-based, not per-trade-capped. This document sets the STATISTICAL kill line; it is not
  a substitute for the runner's own per-position stop and catastrophe rails, which remain active
  and unchanged.
- **It does not answer whether the freed capital should be redeployed.** That is a separate,
  larger question — see the operator's own framing in the request this document responds to.
  Nothing here changes `MEMECOIN_MAX_CONCURRENT`, bankroll sizing, or clip ceilings.
- **It does not distinguish "the rule is fake" from "the rule was real but conditions changed."**
  A kill under section 3 is a instruction to turn the gate off, not a claim about which of those
  two is true. Re-activating after a kill needs a new decision, not an automatic retry.
- **A position closed by an operator override, not by the runner's own exit policy, still counts.**
  This control does not distinguish an operator-modified close from a policy-driven close. If the
  operator manually closes several conjunction positions early during the observation window, that
  action feeds this same query and can trigger a kill or avoid one; know that before acting on
  positions inside the observation population.

## 6. Activation record

Fill in when all four settings in section 1 are set to their studied live values.

`MEMECOIN_REAL_INBAND_MAX_TURNOVER_M5` value set:
`MEMECOIN_REAL_MIN_SOURCE_BUY_USD` value set:
`MEMECOIN_REAL_MAX_SOURCE_BUY_USD` value set:
Activation timestamp (UTC), for `opened_at >=` above:
Release SHA at activation:
Activated by:

Before this section holds values, section 2's population has no start time and section 4's query
cannot run.
