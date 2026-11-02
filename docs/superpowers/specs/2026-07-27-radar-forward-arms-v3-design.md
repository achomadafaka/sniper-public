# Radar Forward Arms v3 Design

**Status:** Approved for implementation on 2026-07-27 after live-ledger verification.

## Goal

Turn the Radar's broad route detector into forward-testable paper hypotheses without hiding dry periods or changing funded execution. Preserve the current permissive route observation as a control, but reserve CENTER and operator-facing alerts for explicit Pump.fun ignition or confirmation arms.

## Verified evidence snapshot

The immutable insight ledger contained 367 exact scored alerts at the analysis cutoff:

- zero target hits inside 20 seconds;
- three +2% net executable hits inside 60 seconds;
- one +12% hit inside 60 seconds;
- 94 alerts with remote round-trip drag below -12%;
- 30 alerts with positive remote round-trip return; neither invalid-cost group produced a 60-second hit;
- the existing async remote-completion path promoted route success directly to CENTER without rerunning the round-trip runway rule;
- requiring Pump progress >=20% and remote drag in [-1200, 0] bps retained all three 60-second +2% hits in 95 historical alerts;
- a two-branch retrospective screen retained all three hits in 70 alerts: one early ignition and two buyer-confirmed continuations.

The earlier claim that 42% of alerts were exploitable later is **not verified**. Post-60-second MFE can be stamped by the timeout sweep using the last captured state. V3 must collect a real 120/300-second first-passage path before treating delayed continuation as an edge.

## Forward-only arms

All thresholds below apply only to `pumpfun_solana`. Bags venues continue capture and enrichment but cannot inherit Pump thresholds.

### Route control

`route-control-v1` records the first fresh acquired-quantity round-trip route per launch that the existing async adapter completes. One independent launch-level control avoids overweighting busy launches. It creates an immutable paper entry and outcome path but never moves a dot to CENTER and never appears as an actionable alert.

### Ignition

`pump-ignition-v1` requires all existing Level-1 momentum, concentration, creator, and route checks plus:

- launch age <=10,000 ms;
- curve progress >=2,000 bps;
- capital velocity >=500,000,000 quote atomic units per second;
- remote round-trip return between -1,200 and 0 bps inclusive.

### Buyer confirmation

`pump-confirmation-v1` requires the same shared checks plus:

- launch age >10,000 ms and <=60,000 ms;
- curve progress >=2,000 bps;
- effective buyer count >=5;
- remote round-trip return between -1,200 and 0 bps inclusive.

The candidate arms are mutually exclusive because the first qualifying arm per launch emits one immutable forward alert and entry. Thresholds are exploratory hypotheses, not funded gates or claims of edge.

### Forward probes added after the first retained comparison

The first retained forward comparison did not support the original arms: ignition reached +12% within 60 seconds in 1/13 samples, confirmation in 0/5, and the permissive executable-route control in 10/74. The result rejects route success, ignition, or buyer breadth alone as the current answer.

Two narrow patterns in the still-hot route-control cohort are now forward probes, not retrospective wins:

- `pump-clean-start-v1`: <=10% curve progress, <=10 seconds old, 2–4 effective buyers, effective buyer HHI <=4,000 bps, improving capital/buyer velocity and concentration, no creator-linked sale, exact entry and sell routes, and remote round-trip drag from -500 through 0 bps. The observed hot slice was 2/2 at +12% within 60 seconds.
- `pump-curve-break-v1`: 20–40% curve progress, <=10 seconds old, exactly two effective buyers, improving capital/buyer velocity and concentration, no creator-linked sale, exact entry and sell routes, and remote round-trip drag from -3,500 through -1,201 bps. The observed hot slice was 2/4 at +12% within 60 seconds.

Those tiny, selected slices only justify prospective capture. They do not establish an edge or authorize funded entry.

Each worker process freezes its startup timestamp as the minimum launch time for new v3 receipts. Restored launches from before that boundary may keep moving on the board, but cannot seed candidate or control evidence after a restart.

## Remote quote correctness

Async completion must reconstruct current point-in-time features and rerun candidate evaluation. Route existence alone cannot create CENTER. Positive round-trip return is treated as an inconsistent sequential quote pair, and drag below -1,200 bps is uneconomic for these exploratory arms. The route control still records both groups so the exclusion remains measurable.

## Extended evidence

Candidate launches remain event-captured through 300 seconds; ordinary launches keep the 120-second limit. Candidate entries continue receiving executable observations after their immutable 60-second competing outcome until the first observation at or after 300 seconds. Diagnostics add 120,000 ms and 300,000 ms horizons and retain the worst executable return observed before each target or terminal failure.

Hot retention increases only enough to project the targeted 300-second path. Existing storage pressure controls remain authoritative and may censor evidence rather than exceed the hard cap.

## Operator surface

- `FIRE NOW — IGNITION`: rare early capital shock with >=20% progress.
- `FIRE NOW — CONFIRMED`: broader demand after 10 seconds with >=20% progress.
- `FIRE NOW — CLEAN START`: low-friction independent demand below 10% progress.
- `FIRE NOW — CURVE BREAK`: fast two-buyer traversal through 20–40% progress despite higher entry cost.
- `ROUTE CONTROL`: research evidence only; never actionable.
- `WAIT`: no forward arm qualifies.

Every row shows arm, quote drag, age, progress, effective buyers, and forward result in plain language. The global Radar status remains exploratory until forward receipts show otherwise.

## Safety and scope

- Paper/read-only only.
- No signing, transaction construction, funded balances, funded runners, positions, X, or wallet-follow signals.
- Existing historical rows remain immutable and keep their original rule versions.
- No cross-venue pooling.

## Acceptance

- Production-shaped tests prove remote route success cannot bypass candidate rules.
- Control and candidate entries are immutable and independently scored.
- Decode/data gaps remain fail-closed.
- Candidate-only 300-second capture is bounded and retention-safe.
- UI distinguishes candidate arms from controls.
- Focused Radar tests, compile checks, JavaScript syntax, and live read-only health checks pass.
