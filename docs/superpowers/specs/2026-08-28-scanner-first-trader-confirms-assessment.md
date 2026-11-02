# "Scanner-first, trader-confirms" — pre-registration assessment

Written 2026-08-28. Routed to this desk as a named prereg candidate. **The verdict is split: one
half is registrable and one half is not.** The distinctive half — the trader-confirmation trigger —
does not survive measurement, for reasons that are structural rather than statistical.

All figures below come from joining `data/runner_signals.db` (scanner detections and features) to
`memecoin_events` (trader buys) on the mint, with the top 8 wallets by funded honest PnL as the
roster.

## 1. What reproduces exactly

The overlap cell is real. 77 coins appear in both the scanner's detections and the top 8's buys.
Coverage is good and censoring is NOT the problem here: 89.8% of the confirmed cell and 91.2% of
the qualifying population carry a tracked outcome.

## 2. What does not hold

**It is not 77 coins where the scanner was first. It is 49.** In 28 of the 77 the trader bought
BEFORE the scanner detected. The scanner led in 64% of the overlap, not 100%.

**The 20-minute lead is a median with no useful shape.** Among the 49, the lead runs p25 4.8
minutes, median 22.8, p75 205.8, and a maximum of 4305 minutes. A quantity spanning three orders of
magnitude cannot be used as a scheduled trigger.

**The ~$30k market cap component adds nothing.** It is a property of the selected 77, not a
predictor. Conditioning on it makes the confirmation rate WORSE:

| feature at detection | n | later confirmed | rate |
|---|---|---|---|
| all detections (base rate) | 14400 | 49 | 0.34% |
| golden buyer >= 1 | 1133 | 17 | 1.50% |
| conviction >= 9 | 1072 | 15 | 1.40% |
| golden OR conviction >= 9 | 1807 | 27 | 1.49% |
| **the proposed bundle, + mcap 25-35k** | 232 | 3 | **1.29%** |
| golden OR conviction, + mcap >= 20k | 1099 | 24 | 2.18% |

## 3. Why the confirmation trigger fails

**It fires on 1.3% to 2.2% of positions.** The features give a genuine 4x to 6x lift over the base
rate, and that lift is real. But the absolute rate means that **98% of feature-bundle entries never
receive confirmation**. An instrument that stays silent on 98% of the population is not a trigger.

**When it does fire, the peak has usually already happened.** This is the finding that closes it:

- median peak time after detection: **10 minutes**
- median trader arrival after detection: **23 minutes**
- peak occurred BEFORE the trader arrived: **23 of 44 (52%)**

The trader's arrival lands a coin-flip after the top. As a scale-in or hold-extension trigger it
would add size at or after the peak half the time. **Arrival is not a leading indicator of anything
we can still act on.** The 20-minute lag that made the idea attractive is precisely what disqualifies
it: our own peak arrives at 10 minutes.

## 4. What IS registrable, and it does not need the traders

The scanner's features predict the outcome DIRECTLY, with no conditioning on any trader:

| population | reached +50% (of tracked) |
|---|---|
| all detections | 25% |
| golden OR conviction >= 9 | **38%** |

That is a real lift, measured forward from detection, on 1648 tracked qualifying rows. It requires
no future event to define its cohort and it is not vulnerable to the reverse causation that the 84%
figure carries.

**This is already the scalp study's population.** The registrable content of "scanner-first" is the
feature bundle, and the feature bundle is what
`docs/superpowers/specs/2026-08-28-scanner-scalp-viability-prereg.md` already tests. It does not
need a second pre-registration; it needs the arms that study is still blocked on.

## 5. The honest summary

The original framing was that the system had found its own native edge, with the copy roster as a
confirming instrument. Half of that is supported. The scanner does see things early and its features
do carry forward signal.

But the confirming instrument does not confirm. It speaks on 2% of cases, it arrives a median 13
minutes after the peak, and it is right about which way to lean only about as often as a coin.

**Recommendation: do not pre-register the confirmation leg.** Register nothing new for the feature
bundle either, because the scalp study already covers it. If the trader-arrival relationship is to
be pursued at all, the question worth asking is the inverse one this data actually supports: whether
the scanner's detections can be used to ANTICIPATE the roster's buys — which is a signal about the
roster, not a trading rule.

## 6. What would change this verdict

1. A trader-arrival measurement taken at the roster's DECISION time rather than its fill time, if
   the two differ materially. Arrival is measured here from `memecoin_events.created_at`.
2. A peak definition at a horizon shorter than the full track, if the 10-minute median is an
   artifact of tracking coins that die quickly.
3. A confirmation rate computed over a roster larger than 8 wallets. A wider roster would fire more
   often, though it would also dilute the "best traders" premise the design rests on.
