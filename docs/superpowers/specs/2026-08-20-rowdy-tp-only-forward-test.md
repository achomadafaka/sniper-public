# Rowdy tp-only-v1 forward test — pre-registration (FROZEN, NOT STARTED)

Status: **frozen pre-registration. Nothing here is applied.** It does not authorise clearing the
kill switch, editing `.env`, or restarting the runner. Those are operator actions.

Live state at freeze: kill switch TRIPPED (operator, `web:127.0.0.1`, 2026-08-19 15:49),
`MEMECOIN_MAX_CONCURRENT=1`, one open position (#40296 DJT, −96.7%, holding the only slot),
23 wallets revoked-to-observed, **Rowdy the only armed wallet**.

## Read this first: what this test can and cannot do

It **cannot** establish an edge. At a $25 bankroll and a $25 minimum clip you get one position at
a time and roughly three full stops before the money is gone. No stopping rule reachable with this
capital has the power to separate a real edge from a coin flip.

What it CAN do, and the only reason to run it:

1. Prove the rails fire as designed — that a take-profit, a stop and a hard cap actually execute
   on a live funded position, which **has never been observed** on this path. Every funded exit to
   date has been `manual`, `b_stop` or a mirror.
2. Catch a catastrophic mismatch between the backtest and reality (fills far worse than the mid
   tape, stuck exits, the cap not firing).

Treat the PnL as an observation, not a result. The pre-registration exists so the rails check is
honest, not so a 20-trade number can be called evidence.

## Why Rowdy, and why not the screen's top row

The impulse screen ranked `ovoimballin` first (+20.4 edge, +14.3 retained at 300 s). Its funded
record is **n=10, −$45.53, 3 wins**, and the now-live chase gate does NOT explain it: 9 of those
10 entries were already inside the 30% cap. When the screen and the money disagree, the money has
already paid. `bigwarzeth`'s impulse decays to zero by +300 s. Rowdy has the least decay of the
three (+5.1 at 300 s, +4.6 at 900 s), the largest sample on the screen (n=25, cov 60%), the most
recent activity, and the only clean funded record (n=2, +$17.85) — though n=2 is not a record.

## Why tp-only-v1 and not the live policy

Measured on Rowdy's own 27 priced buys, 6-point round-trip haircut, marks are nominal mids:

| policy | median | mean | win% | worst |
|---|---|---|---|---|
| **live today** (no TP, no stop, 7d hold) | **−18.7** | −28.6 | 22% | **−97.2** |
| TP +20 / stop −50 / cap 20 min | +14.0 | −4.0 | 59% | −56 |
| TP +20 / stop −40 / cap 10 min | +14.0 | −2.8 | 56% | −46 |
| **TP +15 / stop −30 / cap 5 min** | **+9.0** | **−0.6** | **67%** | **−36** |
| trail 30 armed at +20, stop −50 | −10.6 | **+8.1** | 33% | −56 |

The live row is what happened to DJT.

The trail has the best **mean** because it catches the p75 +53% tail. It is rejected anyway: 33%
win rate with a −56% worst case is ruin before the tail arrives on $25. **A positive mean you
cannot survive to realise is not available at this bankroll.** That is a capital constraint, not a
claim that TP +15 is the better policy — at a larger bankroll the trail is probably the right answer
and this choice should be revisited.

Thresholds come from Rowdy's tape: **74%** of his coins reach +15% from our achievable entry (63%
reach +20%, 41% reach +30%); median time to peak **125 s** (p25 39 s, p75 683 s); median trough
−14.4%, 26% breach −30%, **0% ever reach −80%**.

---

# FROZEN — everything below is fixed before the first entry

## Hypothesis

On Rowdy's Solana buys, `tp-only-v1` at TP +15% / stop −30% / cap 300 s executes its three rails
as designed and does not produce an outcome catastrophically worse than the backtest above.

## Configuration, exactly

    MEMECOIN_REAL_TP_ONLY_WALLETS=Rowdy:solana
    MEMECOIN_REAL_TP_ONLY_TP_PCT=15
    MEMECOIN_REAL_TP_ONLY_STOP_PCT=30
    MEMECOIN_REAL_TP_ONLY_MAX_HOLD_S=300

Verified to build: `tp_only_applies('Rowdy','solana')` is True and `('bigslime','solana')` is
False; the policy resolves identically across nano/micro/small/mid as
`tp=15.0 stop=30.0 hold=300.0 trail=0.0 derisk=0.0 mirror=False`.

`tp-only-v1` is applied in `memecoin_risk.py:354`, not in the resolver, because only that scope
knows whose signal it is. The `follow3050_applies` branch tested immediately before it reads a
DIFFERENT allowlist (`MEMECOIN_FOLLOW3050_WALLETS`, currently empty), so
`MEMECOIN_FOLLOW3050_ALL_WALLETS=1` does not shadow it — that flag changes the resolver default
only. This was checked because a flag hiding behind a trip is how the chase gate stayed dead.

**Two inherited defaults the operator must accept or change before starting:**

- `catastrophe_pct = 0.0`. `_tp_only_policy` leaves the catastrophe floor on the cfg default by
  design, and the cfg default is currently OFF. With no mirror-on-sell there is no wallet sell to
  bail the position out, so the −30% stop is the only loss rail. If it fails — and **18.6% of exit
  attempts (72/388) currently come back `stuck`** — nothing catches the position.
- `unquotable_exit_s = 86400`. A 24-hour unquotable timeout under a 5-minute thesis. A coin that
  stops quoting sits for a day.

## Cohort

- **Start instant:** the first funded Solana entry attempt from Rowdy with `observed_at` strictly
  after the operator applies the configuration above. Record the exact ISO instant here.
- **Eligibility:** `execution_mode='real'`, `network_id='solana'`, `source_user_id` =
  `e89522ff-73b5-52f3-b1e3-ef8f60e90775`, `origin_event_id IS NOT NULL`, and a funded contract
  whose `policy_id` is `tp-only-v1`. A position that entered under any other policy is not in the
  cohort and is reported separately.
- **One row per eligible attempt, including refusals.** A chase-refused or gate-refused attempt is
  recorded with outcome `blocked` and PnL 0.00. Recording only fills would score the policy on the
  trades it allowed.

## Stop rules

- **Enrollment:** 20 eligible attempts, or bankroll exhaustion (free USDC below the $25 minimum
  clip), whichever comes first. 20 is what the money allows and is stated as such — it is not a
  powered sample.
- **Terminal:** readable only when every enrolled attempt is `blocked` or `closed`. An open
  position blocks the read. Backstop 24 hours after the last enrollment, given a 300 s hold — a
  position still open a day after a five-minute policy is itself a finding, recorded as `censored`.
- **Hard abort:** stop the test immediately and report, without waiting for the stop count, if any
  of these occur — each falsifies the rails-fire hypothesis directly:
  - a position exceeds `max_hold_s + 120 s` while still open (the cap did not fire),
  - a position marks below −45% at any point (the stop did not fire, with slack for a wick),
  - two consecutive exits return `stuck`.

## Primary observation

Per-attempt realized PnL, blocked entries at 0.00, and the **exit-reason histogram** — which rail
actually fired. The histogram is the point. A cohort where every exit is `manual` has falsified the
hypothesis regardless of PnL.

## Secondary

Realized vs backtested per-trade PnL (the backtest says median +9.0 after a 6-point haircut; a
much worse realized median means the haircut was wrong); time-to-exit against the 125 s median
time-to-peak; stuck-exit rate; and the fill gap between the TP trigger price and the executed price.

## Pre-declared outcome

If the rails fire and the realized median is not catastrophically below the backtest, the result is
"the policy executes; expectancy remains unestablished." If any hard-abort condition fires, the
result is "the rails do not execute as designed on the funded path," which is a more valuable
finding than any PnL number here and blocks further funded testing until fixed.

No outcome of this test authorises raising the bankroll, re-arming wallets, or claiming an edge.
