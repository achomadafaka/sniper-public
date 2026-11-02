# HL Standalone Exhaustion Forward Paper Arm — Design

Date: 2026-07-26
Status: implemented and active in paper at corrected UTC 2026-07-26T18:24:26.627Z

## Decision

Forward-test `standalone_exhaustion` separately from `wallet_copy_alt_short`.
The active wallet-copy arm cannot validate a public-price entry rule. This arm remains
paper-only and cannot authorize funding, an allowlist override, or real orders.

## Frozen policy

- Universe: current HL perpetuals excluding BTC, ETH, and SOL; record survivorship bias.
- Eligibility: at least 30 calendar days of hourly history and 80% hourly coverage.
- Decision: after an hourly bar closes, compute
  `runup_72 = close[t] / min(close[t-72:t]) - 1`.
- Trigger: the first upward crossing from `<30%` to `>=30%`; a level does not retrigger.
- Causal gate: trailing 72-hour equal-weight alt basket return `<= -500 bps`, with
  at least 80% universe coverage. `neutral`, `bullish`, and `unknown` are logged but
  stand down.
- Entry: first observed mark in the next UTC hour, never the decision close.
- Exit: sampled-mark stop at `entry*1.20`, take-profit at `entry*0.85`, or the first
  observed mark at/after eight hours. Stop wins a same-sample tie.
- Costs: 9 bps round-trip fee plus 30 bps spread/slippage haircut; funding is required.
- Portfolio: eight equal $125 slots on $1,000. Coin order breaks simultaneous-entry ties.
- Benchmark: equal-weight short of all other eligible alts over the same entry/exit
  interval, with the same 39 bps cost and recorded hourly funding; minimum 80% coverage.

The live mark tape is sampled, not exchange OHLC. Each UTC hour stores first, high, low,
and latest observed marks plus funding. Historical cache may seed market history only;
it never creates a signal, position, arm observation, or pre-activation decision.

## Persistence

Reuse `hl_altshort_arms` for immutable identity. Add:

<!-- schema:external=hl_altshort_signal_observations -->

`hl_altshort_signal_observations` stores decision, causal features, pending next-hour
entry, fixed exit, funding, matched basket, latest mark, and provenance. Identity is
unique on `(arm_id, coin, decision_ts_ms)`. No row may have `decision_ts_ms` before the
arm epoch.

`hl_altshort_regime_marks` gains `open_mark`, `high_mark`, and `low_mark`. Existing
`mark` remains the latest observation and is backward-compatible.

## Runtime

`run_hl_altshort_signal_paper.py` reads the paper DB only. The existing copy loop owns
public mark capture. The standalone runner submits no HTTP request and no order.

The watchdog identity includes DB path and manifest path. Heartbeat includes code SHA,
dirty flag, arm ID, epoch, policy hash, last processed closed-hour bucket, and position
counts. Existing wallet-copy and real processes are independent.

## Verdict

The reporter stays `paper_continue` until at least 30 valid closes across 20 UTC entry
days. It includes all opens at fresh MTM, fixed-slot PnL, exact matched-beta residual,
funding/benchmark coverage, concentration, and provenance failures. Promotion is still
paper-only and requires the existing causal/robustness gates.

## Activation

1. Pin clean runtime code and immutable manifest.
2. Back up `hl_intel.sqlite`.
3. Seed only hashed hourly market/funding history from the sealed cache.
4. Create the arm at corrected UTC.
5. Launch one exact standalone paper process.
6. Verify no pre-epoch decision rows, heartbeat identity, and one full scan.

Activation completed as `standalone-exhaustion-forward-v1-20260726`, pinned to
`99ebc028d5c5eefda91e672888c61577d550baad`. The initial verdict is
`paper_continue` with zero observations; funded deployment remains unauthorized.
