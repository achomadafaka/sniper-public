# 4663 hourly dollar floor scaled with the coin (design + rule 5)

Status: BUILT behind a knob, default off. Operator direction via Alpha (2026-09-28): "the 4663 h1
floor is too high for small coins; it must scale with the coin." Option (b1), chosen by Alpha after
the mcap finding below. Author: Lambda.

## 2026-10-03 operator amendment: remove the Robinhood hourly dollar veto

The operator explicitly removed this blocker after SPORE attempt `530c00dd` refused against
$3,000 with unknown effective market cap and provider h1 volume zero. The selected provider pool
had $3.23 liquidity; the on-chain tape reported `blind_source_tx_absent`. These inputs do not
establish complete zero activity for the traded venue.

`MEMECOIN_REAL_MIN_VOLUME_H1_USD_4663` now overrides the global dollar floor on chain 4663.
Unset or blank inherits the prior rule below. Explicit `0` disables this dollar leg, including
when market cap is unknown; positive values remain eligible for mcap scaling and age pro-rating.
The funded startup validator rejects negative and non-finite values. Other chains, h1 turnover,
m5 gates, classification, route and safety controls retain their policies. The live activation
sets this override to `0`; the existing global h1 turnover floor is also `0`.

This selector uses only in-memory settings and arithmetic: no provider call, lock, retry, or
mutation of shared state. A 5,000-call local microbenchmark measured p50 0.001251 ms and
p90 0.001297 ms; this is not an end-to-end entry latency measurement. Roll back by removing the
override and restarting the pinned runner. Missing venue coverage and missing market-cap
classification remain unresolved by this policy change. No old trade is replayed or submitted.

Regression: `tests/test_memecoin_robinhood_h1_floor_override.py` reproduces the exact hourly
refusal when unset, advances beyond it at zero, retains the subsequent unclassified-band refusal,
and reaches the executor for a classified low-volume coin. It also retains the m5 gate and other
chain floors. The zero-override test failed against the previous implementation before the fix.

## Measurement (Gamma)

4663 `under_abs_floor` refusals since 09-14: n=28. The nano median organic h1 is $1 (dead coins).
There are 5 near misses at $1,000 or more.

## Rule

    floor = min(MEMECOIN_REAL_MIN_VOLUME_H1_USD, r x effective mcap)
    r     = MEMECOIN_REAL_MIN_VOLUME_H1_MCAP_RATIO_4663   (default 0 = today's flat $3,000)

- Implemented once, in `volume_gate_args` via `h1_abs_floor_for(network_id, mcap)`. It is
  therefore shared by the entry hook, the revival watch and the promotion lane.
- Only a chain with a `..._MCAP_RATIO_<chain>` setting scales; only 4663 has one.
- The pair-age pro-rating (`_h1_floor_share`) applies on top, unchanged.
- The refusal reason names the basis, e.g. "h1 organic volume $1 under floor $282
  (under_abs_floor, 5.00% of mcap $5635)". The volume_gate substage records
  `h1_abs_floor_basis` (None = flat).

## The finding that shaped (b1)

The two J7 cards named for testing (28211dc9, 392bebc1) carry NO market cap on the J7 path:
their_market_cap is 0 on 4663 (27 of 51 fills since 09-21), and the copy path's live-mcap fallback
reads DexScreener, which 4663 is barred from by the 08-30 band-source pin. The rule keeps the flat
floor for an unknown mcap, so those cards stay refused on J7.

The on-chain lane carries an mcap for the same buy (99043164: $28,360). With 98f1f641's tape bound
($2,171) and r=0.05 (floor $1,418), it admits the buy. J7 is being retired; making J7 admit these
((b2): an mcap for the h1 scaling from source price x supply) was declined.

## Rule 5

1. **Money path.** Yes: admission. It only ever lowers the floor on a coin with a known mcap below
   flat / r. It never raises one: a $1M coin keeps $3,000.
2. **Speed.** Arithmetic on values already in hand. No read.
3. **Concurrency.** Pure. The knob is read per call from the settings module.
4. **Absent vs zero vs unknown.**
   - An absent mcap and a 0 mcap both keep the flat floor. A 0 is a missing read, not a tiny coin,
     and must never scale the floor to $0. Negative and non-finite values keep it too.
   - r = 0 (the default) is byte-identical to today: `h1_abs_floor_for` returns the flat floor
     and basis None, so the volume_gate substage and the reason text are unchanged.
   - Non-4663 chains are byte-identical: no other chain has a ratio setting.
5. **Failure modes.** A wrong mcap scales the floor wrongly. The mcap is the same
   classification.facts.effective_mcap that the band and the ceiling already trust.
6. **Units and identity.** r is a fraction of USD market cap per hour; the floor is USD.
7. **Sample.** After activation, count 4663 `under_abs_floor` refusals whose basis is scaled, and
   the admissions they became. Refused-coin outcomes come from the refusal shadow and captures.
8. **Rollback.** Set r to 0 and restart the runner.

## Check left behind

`tests/test_memecoin_h1_floor_mcap_scaled.py`:
- the real card is admitted at r=0.05 and refused at r=0;
- an unknown mcap stays flat, and so does a $1M coin;
- other chains are untouched;
- pro-rating applies on top;
- the basis string, and the reason through the real hook.
