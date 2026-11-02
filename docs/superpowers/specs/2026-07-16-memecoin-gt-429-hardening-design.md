# Memecoin GeckoTerminal 429 Hardening Design

**Date:** 2026-07-16
**Status:** approved for implementation by operator instruction to close the live 429 gap
**Extends:** `2026-07-16-memecoin-gt-pricing-design.md`

## Evidence

After the 2026-07-16 runtime restart, GeckoTerminal returned three HTTP 429 responses for chunk
sizes 30, 14, and 1. The client treated each failure independently, continued later chunks after
the first refusal, and had no shared cooldown. Coverage recovered afterward: the blind worker
priced 8/8 candidates each minute and the position sweep wrote 51-53 chain-4663 marks per minute.
The defect is therefore retry amplification during a provider rate-limit window, not a persistent
coverage outage.

The separate full-suite failure is a stale UI assertion. The Performance tab now consumes
`/api/memecoin/traders/arm-matrix` and renders `wallet.by_arm`; it no longer consumes the legacy
`trader_performance` response. Production UI behavior is current; its test still names `ab_pnl`
and `ab_closed` from the retired view.

## Decision

Use a process-wide, nonblocking request gate in `geckoterminal_quote.py`.

- Cache hits never acquire the gate.
- Only one GT HTTP request may be in flight in the process. A competing caller returns missing and
  retries on its next scheduled sweep; it never waits in the entry or exit loop.
- HTTP 429 starts a shared cooldown. Honor a positive numeric `Retry-After` when available;
  otherwise use `MEMECOIN_GT_429_COOLDOWN_S=60`. Clamp provider values to 3-300 seconds.
- The batch caller stops all remaining chunks after the first 429. It retains any earlier chunk
  results from that call.
- During cooldown, batch and single-key misses make zero HTTP requests. Fresh cache hits remain
  available.
- Log one concise warning when cooldown starts. Do not print a traceback for an expected 429.
- Other HTTP failures remain fail-closed and do not invent prices.

Update the stale UI test to assert the current Wallet x Arm contract: arm-matrix endpoint,
`by_arm`, and the explicit `alpha_b` column mapping. Do not change UI production code for that
test-only mismatch.

## Alternatives rejected

- Sleep between chunks: blocks the money-adjacent sweep and weakens exit cadence.
- Lower the capture worker cadence: does not coordinate the independent position-mark caller.
- Add another provider: adds an unmeasured network dependency and violates the existing pricing
  design.

## Safety and acceptance

- No entry, exit, reservation, sizing, stop, take-profit, or token-safety behavior changes.
- No new network request; 429 windows produce fewer calls.
- A 65-address batch receiving 429 on its first chunk performs exactly one request, not three.
- Calls during cooldown perform zero requests; calls resume after cooldown.
- Concurrent callers cannot issue overlapping GT requests.
- Full memecoin suite no longer has the stale Performance-tab failure.
