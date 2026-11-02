# Memecoin Gate Audit Corrections Design

**Date:** 2026-07-17
**Status:** Approved by operator directive (verify and tackle all actionable findings)
**Scope:** Base funded-route readiness, blind-chain capture coverage, startup configuration truth, and corrected audit clocks

## Evidence

The audit window is `2026-07-16T12:00:00Z` through `2026-07-17T06:41:00Z`, anchored to corrected observed UTC.

- Base `8453` produced 117 consecutive `unroutable_chain` decisions after the runtime environment stopped selecting a funded venue. Code contains canonical Base SwapRouter02 and QuoterV2 addresses, but the current executor only quotes a single-hop `USDC -> token` path.
- An initial read-only live probe of 20 recent Base coins below $2M found `0/20` single-hop routes. A scratch `USDC -> WETH -> token` probe appeared to find one two-way route, but the post-implementation acceptance probe did not reproduce it: 29 recent mints produced two buy quotes and zero executable reverse quotes. Base therefore remains funded-disabled.
- The Base wallet has about 0.01084 native ETH and $4.39 USDC. The existing $5 minimum-trade floor therefore remains authoritative and prevents funded entries until the wallet is funded further.
- Legacy `memecoin_signal_outcomes` has `0/367` marked 4663 decisions in the window. Capture v2 is not absent: `314/367` decisions have v2 outcomes and `203/367` have at least one observed v2 horizon. The blind worker is starved: 96 cohorts were eligible while only eight were probed per minute.
- `memecoin_position_marks` already held a corrected-UTC GeckoTerminal tape for 82 4663 mints. Reusing that exact tape can expand v2 coverage without another network request.
- Approval and exit-override helpers default to host `datetime.now(timezone.utc)` even though the host wall clock is about 25,205 seconds ahead. Their audit rows are therefore mixed with corrected observed timestamps.
- `was_real_approved` in `memecoin_feed_observations_v2` was false for every `not_approved` decision in the window. The 134 4663 rows belonging to wallets armed now were armed later; no armed-wallet read race is proven.
- Consensus and first-seen chase samples remain below the pre-registered per-chain evidence threshold. Their funded behavior does not change in this work.

## Decisions

### 1. Base direct route

Add a Uniswap V3 exact-input route for configured immutable connector tokens. Base configures canonical WETH as its connector and the canonical USDC/WETH 500 fee tier. The token-side fee is scanned across the existing fee-tier list. Direct and connector candidates are quoted in one JSON-RPC batch and the deepest executable output wins; providers that reject batch payloads retain the existing single-call fallback. One transient transport failure gets one bounded retry. No quote or mutable route ranking is cached.

The fallback returns the existing executor quote contract and uses SwapRouter02 `exactInput((bytes,address,uint256,uint256))`. Entry, token-safety sell checks, previews, and exits consume the same quote function, preserving route symmetry. Quotes, balances, safety verdicts, capital, and kill-switch state are never cached.

The live environment may select `MEMECOIN_EVM_EXEC_VENUE_8453=direct` only after focused tests and a fresh two-way read-only quote pass. That acceptance check currently fails, so `8453` must be removed from the funded allowlist until a current two-way route is proven. The unchanged $5 floor also blocks the currently underfunded Base wallet. A token with no sell path remains refused by token safety.

### 2. Startup configuration truth

When funded trading is enabled, compare the explicit EVM allowlist with the contexts actually built. Log and asynchronously alert once at startup for every missing chain. This is outside the entry hot path and adds no per-signal work.

The diagnostic does not guess why a context is absent. The chain list is deterministic; readiness details remain available from the existing status endpoint.

### 3. 4663 outcome coverage

Keep capture v1 and v2 separate. Do not add 4663 writes to legacy `memecoin_signal_outcomes`.

Before the blind worker spends its eight-probe network budget, load the newest safe `memecoin_position_marks` row per eligible blind-chain coin. A reusable mark must:

- match exact `(network_id, mint)` identity;
- use corrected observed UTC;
- be no newer than the pass and no older than two blind-capture periods;
- have positive finite price and `quality='live'`;
- preserve its original `sweep_ts`, source, liquidity, and market cap.

Persist reused marks into the v2 quote ledger as `probe_ok` with `observed_at=source_ts=position_mark.sweep_ts` and `pair_address='position_mark:<network>'`. Never stamp the worker's current time onto an older quote. Apply the existing ratio guard. Only cohorts without a reusable mark consume the bounded GeckoTerminal probe budget.

Sweep counts include reused plus probed cohorts; budget-skipped counts include only cohorts that had neither a reusable mark nor a probe slot. This makes the coverage gain visible while keeping network use bounded.

### 4. Clock correctness

Default `memecoin_wallet_approvals.created_at` and `memecoin_position_exit_overrides.created_at/resolved_at` to `real_utc_now_iso()`. Explicit caller-supplied timestamps remain unchanged. This is audit correction only; it does not change arming or exit policy.

## Explicit Non-Changes

- No consensus, first-seen chase, market-cap, impact, allowlist, re-entry, TP, SL, trail, or mirror threshold changes.
- No change to atomic reservation, one-position-per-coin, duplicate-submit protection, token safety, kill switch, sizing, capital accounting, or paper strategies.
- No legacy outcome backfill or pooling with capture v2.
- No funded Base transaction or smoke trade in this change.
- No additional hot-path provider.

## Acceptance

- Base direct and connector quotes preserve exact chain identity and return symmetric executable buy/sell calldata when a route exists.
- A missing Base sell path remains a deterministic safety refusal.
- Startup identifies an allowlisted EVM chain with no built context once, outside the entry loop.
- Fresh shared marks cover eligible 4663 cohorts before the eight-mint network budget is applied.
- Reused observation timestamps equal the source mark timestamps; no fake zero or current-time stamp is written.
- Approval and override default timestamps use corrected real UTC.
- Existing gate behavior and funded safety tests remain green.
