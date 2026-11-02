# V6 Live-Paper Remediation Design

Status: approved for implementation on the isolated V6 paper book. Funded execution and Coin Radar are out of scope.

## Evidence contract

- A terminal price loss needs two distinct healthy quote identities. An adverse quote more than 10x below the last accepted healthy quote is quarantined and cannot ratchet high-water or trigger catastrophe.
- LP removal is classified from measured removal fraction. Below 5% is minor/HOLD, 5%-30% is material/DERISK, and at least 30% is catastrophic/EXIT_FULL. Missing magnitude is insufficient evidence, not a full cut.
- Quote failures use the shared TOKEN-versus-INFRA classifier. Durable token/route failures advance route absence; provider failures do not.
- Structural pool prices are compared only in their own quote asset. A Jupiter USDC exit price cannot substitute for a WSOL pool price.
- Broken feed continuity still produces a persisted data-insufficient HOLD so time and capture failures remain observable.

## Policy contract

- `early` remains a registered feature but cannot by itself promote a break to full exit. Full exit requires at least two non-time severity points after wallet adjustment; lesser breaks use the staged DERISK path.
- The wallet prior is computed causally from prior closed paper lifecycles, shrunk through wallet-chain, chain, and global levels with kappa 20. No usable history is represented as null and reported as insufficient.
- A suppressed action intent never consumes a tranche.
- One live policy implementation exists. The divergent replay policy and replay entrypoint are retired.
- Every change above changes the policy hash. New entries form a fresh cohort; existing immutable evidence stays attributable to its original hash.

## Reporting contract

- An action is unfilled only when its intent records a failed quote attempt, not when the state fork is first persisted.
- Quote-asset PnL is always reported by asset. WETH lifecycles get a labeled entry-FX USD mark derived from entry notional and entry WETH; mixed or unavailable conversion never silently disappears.
- Max drawdown is the maximum over the full observation path. Quarantined quotes are excluded.
- Duplicate reason codes are removed while preserving order.

## Runtime and operations

- Explicit SQLite migrations validate every required column.
- Quote pacing reserves call slots under lock but never holds the lock during network I/O.
- Event receive timestamps are bounded per position.
- Chain volatility uses the causal trailing hour and refreshes per observation.
- Runtime deployment validates before making a backup and uses corrected project time for backup identities.
- The funded runner is the sole TPU sidecar owner; web contexts never resolve/rebind the socket and
  use the existing RPC fallback path.
- Hyperliquid current/proven targets keep the fast lane; legacy fallback targets use a bounded slower lane. Heartbeats are written after each cycle with cycle metrics.
- 4663 deep-catchup gaps are persisted and visible instead of only logged.
