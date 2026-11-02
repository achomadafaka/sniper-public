# Design: memecoin market-data cadence v2

Phase 2 of `docs/superpowers/plans/2026-07-22-memecoin-daytrading-platform-master-prompt.md`.

**Headline: the phase's own gate is not achievable as written, and one of its two targets is
measuring the wrong thing.** Both are corrected here, with the measurements that show why.

## Baseline (measured 2026-07-22, read-only, anchored on `MAX(sweep_ts)`)

Anchor: `2026-07-22T18:26:15Z` (corrected-UTC; host clock runs ~7h fast — Global Constraint 5).

All marks, trailing 48h of data, gap between consecutive marks per `(chain_id, mint)`:

| chain | n gaps | p50 | p90 | p99 | mean |
|---|---|---|---|---|---|
| 4663 | 220,846 | 69.0s | 181.6s | 541.0s | 105.0s |
| solana | 105,326 | 32.0s | 47.0s | 75.7s | 36.8s |
| 56 (BSC) | 12,313 | 32.0s | 46.0s | 69.5s | 35.6s |
| 8453 (Base) | 3,947 | 32.0s | 45.9s | 62.1s | 34.7s |

Restricted to marks inside a **real position's** hold window:

| chain | n gaps | p50 | p90 | p99 |
|---|---|---|---|---|
| solana | 31,681 | 30.1s | 38.8s | 55.2s |
| 4663 | 17,210 | 56.5s | 124.9s | 380.0s |

4663's open-position p50 of 56.5s reproduces the 56.05s measured on position 11984
independently.

## Correction 1 — the Solana number is a recording floor, not a data rate

`MEMECOIN_MARK_WRITE_MIN_INTERVAL_S = 30` (`sniper/settings/memecoin.py:186`) skips any mark
younger than 30s per `(chain, mint)`. It was added 2026-07-17 because the tape grew ~20k
rows/2h, with the explicit rationale: *"Exit decisions read the live price_map, not marks;
marks are telemetry/replay tape, so 30s granularity is safe."*

Gap histogram, trailing 24h, proves the tape is pinned against that floor on Solana and not
on 4663:

| gap | solana (n=65,432) | 4663 (n=153,799) |
|---|---|---|
| <30s | 0.1% | 0.0% |
| 30–35s | **64.8%** | 0.0% |
| 35–45s | 18.9% | 0.1% |
| 45–60s | 11.2% | 2.1% |
| 60–120s | 4.9% | **72.4%** |
| 120+s | 0.1% | 25.5% |

Solana's mode sits in the first bucket the floor permits. 4663 never approaches it. So:

- **Solana marks are dedupe-limited.** The decision loop (bracket pass, ~15s) is already
  faster than anything this tape can show. "Solana open-position staleness ≤5s" is not
  measurable from `memecoin_position_marks` at any implementation quality.
- **4663 marks are source-limited** — GeckoTerminal's 15s min-interval, 300s TTL and shared
  429 cooldown (Global Constraint 8). This is the genuine lever.

This also explains the earlier finding that 4663 rail decisions cannot be audited from the
marks tape: position 12110's executable peak (+36.06%) exceeded every mark mid in its window
(+32.59%) because the rails read a price the tape never recorded.

## Correction 2 — the phase gate is physically unreachable

Phase 2's gate reads: *"measured p50 gap on 4663 open-position marks ≤20s over a 24h window."*
With a 30s write dedupe, the minimum observable p50 is 30s. **The gate cannot go green even
if 4663 quotes arrive every 2s.** Building to it would burn the phase and then fail
acceptance for a reason unrelated to the work.

Restated gate (below) separates the two things the old one conflated: how fresh the *decision*
price is, and how dense the *tape* is.

## Design

Two independent, separately-verifiable units.

### Unit A — `memecoin_fastlane`: open-position quote refresh (new module)

Purpose: keep a fresh executable price for the bounded set of OPEN funded positions
(≤ `MEMECOIN_MAX_CONCURRENT` = 7), independent of the full sweep's coin universe.

- **Interface:** `refresh_open_positions(conn, contexts, *, now_fn, quote_fns) -> dict[(network_id, mint), quote]`.
  Pure dispatch; no exit decisions, no writes to position state.
- **Source: the position's own funded `preview_exit_fn`.** Not a re-implemented quote.

  **Correction (2026-07-22 review, after the first implementation).** The first version built
  its own quote: `dex_v3_quote` over an atomic amount reconstructed from the ledger's *human*
  open quantity. Both halves were wrong.
  - `evm_real_seams.py:501` states the rule outright: *"size the sell from the EXACT raw
    on-chain balance (never a human float: memecoin balances exceed 2^53 and the round-trip
    mis-sizes the sell -> revert/false-stuck or dust)."* The fast lane was doing precisely the
    forbidden thing.
  - The funded exit walks a **V3 → V2 → V4** ladder (`_exit_venue_quote`), deliberately, so a
    token bought through a V4 rung is not stranded. A V3-only quote is not the price the
    position would exit at, so labelling it `quality="exact_route"` was false.

  It now calls `preview_exit_fn(position)` — exact raw balance, full ladder, USD-converted —
  and divides `expected_usdc` by the ledger's open quantity, which is byte-for-byte
  `_executable_unit_price` in `memecoin_real_runner`. One definition of "executable", shared
  by the tape and the rails, so they cannot drift. Decimals handling disappeared entirely with
  the round-trip that needed it.

  A position with no funded context is **skipped**, never priced by a weaker substitute.
- **Solana:** not priced by this lane. Its context has no EVM exit-preview seam, and its tape
  gap is a 30s recording floor rather than a data rate, so there is no freshness to buy.
- **Isolation:** runs in its **own bounded worker**, after the poll's funded hooks — same
  doctrine as the 0x probe worker (Global Constraint 7). Never in front of entry submission.
- **Failure:** any source error yields no quote for that position this tick. Missing is
  missing; the sweep's existing price_map remains the decision input until Unit C flips.

### Unit B — honest tape labelling and a floor that doesn't lie

- Marks written from the fast lane carry `source='fastlane_rpc'` (EVM) / `'fastlane_quote'`
  (Solana) and a distinct `quality`, so research can segment old-cadence from new-cadence eras
  — a hard requirement, since the 4663 paper-slippage convention already changed once at
  `e524ef70` and blended reads were the result.
- The 30s dedupe stays for the broad universe. **Open positions get their own floor**
  (`MEMECOIN_MARK_WRITE_MIN_INTERVAL_OPEN_S`, default 5s). The row-growth problem the dedupe
  solved was thousands of tracked coins; ≤7 open positions at 5s is ~120 rows/position/hour —
  bounded and worth it, because these are the only rows any rail post-mortem ever reads.

### Unit C — the flip (Phase 2.4, operator-notified) — **NOT IMPLEMENTED**

`MEMECOIN_FASTLANE_DECIDES` exists as configuration and documentation only; **nothing reads it
today**, and describing the lane as "ready to flip" would be false. What ships is the shadow
half: `MEMECOIN_FASTLANE_ENABLED` runs the worker and writes `source='fastlane_rpc'` marks.

When Unit C is built, `sweep_exits` would prefer a fast-lane quote when one is fresher than the
sweep's price_map entry for that position — a change to *when* rails evaluate, not what they
say, but still a real-money timing change and therefore announced before flipping.

## Alternatives considered

1. **More GeckoTerminal polling for 4663.** Rejected outright — Global Constraint 8, and the
   429 cooldown is shared, so it would degrade the paper tape for every chain to speed up one.
2. **Lower `MEMECOIN_MARK_WRITE_MIN_INTERVAL_S` globally to 5s.** Rejected: reinstates the
   ~20k rows/2h growth the dedupe was introduced to stop, across thousands of coins, to benefit
   ≤7. Unit B's per-open-position floor gets the same benefit at ~0.3% of the row cost.
3. **WebSocket/stream subscription for pool events.** Deferred, not rejected. Strictly better
   in steady state, but it is a new failure surface with reconnect/backfill semantics, and the
   `eth_call` path reuses primitives already carrying funded money. Revisit once Unit A's
   measured ceiling is known.

## RPC budget — BLOCKED on a quota-backed provider

Steady state, 7 open positions, 5s tick: **~1.4 requests/s ≈ 121k/day** against
`MEMECOIN_EVM_RPC_URL_4663`, plus the balance read inside each preview.

**Operator decision, 2026-07-22: do not run this against the configured endpoint.** It is
Robinhood's public RPC, which publishes no numeric quota and is explicitly documented as
rate-limited and unsuitable for production, high-throughput, or latency-sensitive use
(<https://docs.robinhood.com/chain/connecting/>,
<https://docs.robinhood.com/chain/terms-of-service/>). Robinhood recommends a dedicated
provider such as Alchemy.

**The 24h shadow does not start until a quota-backed endpoint is provisioned.**
`MEMECOIN_FASTLANE_TICK_S` lets the cadence be tuned to whatever that provider allows without
redesign.

## Restated phase gate (replaces the unreachable one)

1. **4663 open-position quote freshness** — p50 age of the newest fast-lane quote at each
   rail evaluation ≤20s, p90 ≤45s, over 24h. Measured from fast-lane telemetry, **not** from
   the dedupe-floored marks tape.

   ⚠ **That telemetry does not exist yet.** No component currently records quote age at rail
   evaluation, so this gate is unmeasurable today. Phase 2.3 is blocked on building the
   instrumentation *and* on a quota-backed RPC — not on wall-clock.
2. **Tape density for open positions** — p50 mark gap ≤10s on 4663 open positions once Unit B
   ships (achievable: the open-position floor is 5s).
3. **Solana** — no cadence target. It is already faster than the tape can express; the
   deliverable is instrumentation parity, not speed.
4. **No GT regression** — `grep -c 429 data/memecoin_runner.log` delta ≈ 0 across the shadow
   window; GT call volume flat.
5. **No funded behavior change during shadow** — identical exit reasons vs the pre-change
   window; runner heartbeat healthy through 48h.

## Testing

- `dex_v3_quote` seam faked; tier cache asserted to cache successes and **not** cache errors
  (07-15 regression).
- Fast lane never runs before funded hooks (ordering test, mirrors the 0x-probe worker test).
- `(network_id, mint)` identity preserved end-to-end (07-15 keying regression).
- Mark labelling: fast-lane rows carry the distinct `source`; the open-position floor applies
  only to open positions.
- Shadow mode asserted inert: with `MEMECOIN_FASTLANE_DECIDES=0`, `sweep_exits` output is
  byte-identical with and without fast-lane quotes present.

## Out of scope

Entry-path latency (Phase 6). Exit policy (Phase 3 — this phase changes *when* rails
evaluate, never *what* they decide). Charting consumption (Phase 5, which benefits
automatically from Unit B).
