# Memecoin capture coverage — blind-chain probe sweeps + candidate cohorts

**Date:** 2026-07-16 · **Status:** approved for build (operator standing full-autonomy)
**Motivation:** `docs/research/2026-07-16-memecoin-corpus-pattern-study.md` — its #1 lever. Chain 4663 has
545,103/545,103 `missing_pair` rows in `memecoin_quote_observations_v2` (DexScreener does not index the
Robinhood chain), and non-entered coins get no forward capture unless a ≥$min_usd buy opens an outcome —
so lifecycle/consensus datasets are survivorship-biased and 63% of coins have stale anchors.

## Problems (verified in code)

1. **4663 is unpriceable by the v2 sweep.** `capture_v2_pass` prices via `dexscreener_stats_batch_keyed`;
   `FOMO_TO_DEX` (sniper/execution/solana_quote.py:25) has no 4663 entry → every sweep writes
   `missing_pair` and burns a budget slot. `create_outcome_v2` (memecoin_capture_v2.py:118) rejects
   4663 outright → zero v2 outcomes/horizon marks for the top-volume chain.
2. **Non-entered coins are invisible.** `register_active_capture_cohorts` registers only `position` and
   `signal` contexts. The `candidate` context exists in `CONTEXTS` but nothing registers it. Coins whose
   feed events are below `MEMECOIN_DISCOVERY_MIN_USD` (or curated-small) never get swept.
3. **Latency constraint:** `capture_v2_pass` runs synchronously on the funded runner's poll loop
   (scripts/memecoin_runner_loop.py:354). 0x probes cost ~1s each — they must NOT ride the main sweep
   (entry-speed doctrine, follow fast-lane 07-16).

## Design

### A. Blind-chain probe capture — new background worker, off the money path

New pure function in `sniper/services/memecoin_capture_v2.py`:

```
capture_blind_pass(conn, now_iso, *, price_batch_fn=None, max_mints=None, sweep_id=None, period_s=None)
```

- Selects due cohorts exactly like the main sweep but `WHERE network_id IN MEMECOIN_DEX_BLIND_CHAINS`,
  `ORDER BY priority DESC, next_due_at ASC` , `LIMIT MEMECOIN_CAPTURE_BLIND_MAX_MINTS` (default 8).
- Prices via existing `sniper/services/memecoin_evm_price.py::evm_price_batch` bound to
  `build_evm_price_for()` / `build_evm_mcap_for()` (0x sell-quote for one token; attempt-based rotation,
  fail-parking, never a 0.0 tick — all already battle-tested for position marks). Pass `cap=len(selected)`;
  external budget owns selection.
- Writes `memecoin_quote_observations_v2` rows: `dex_id='zerox'`, synthetic stable
  `pair_address='zerox:<network_id>'`, `liquidity=NULL` (never fabricated), `market_cap` from totalSupply,
  and a **probe-distinct quality vocabulary**:
  - `probe_ok` — positive finite price, ratio vs cohort `last_good_price` within [1e-4, 1e3]
  - `probe_ratio_outlier` — price present but ratio guard tripped (bad-tick protection; not used for marks)
  - `probe_unpriced` — selected but 0x could not price it (explicit coverage gap, matching v2 philosophy)
- Updates cohort `last_good_price` on `probe_ok`, `next_due_at = now + MEMECOIN_CAPTURE_BLIND_PERIOD_S`
  for all selected, and inserts a `memecoin_quote_sweeps_v2` row for the pass.
- **Never `ok`:** probe prices stay opt-in for research; dashboards/queries filtering `quality='ok'`
  are unaffected.

Worker thread (pattern: follow fast-lane thread in `scripts/memecoin_runner_loop.py`): own SQLite
connection, loop every `MEMECOIN_CAPTURE_BLIND_PERIOD_S` (default 60s), calls `capture_blind_pass`,
logs + survives exceptions. Starts only when `MEMECOIN_CAPTURE_V2_ENABLED` and
`MEMECOIN_CAPTURE_BLIND_ENABLED` and blind chains configured. Zero copy-path latency impact.

Main sweep change: `capture_v2_sweep` excludes blind-chain cohorts from eligibility (they can never
price on DexScreener) — ends the missing_pair spam and frees budget slots. `register_active_capture_cohorts`
keeps registering them (positions/signals/candidates on 4663 all flow to the blind worker).

Horizon marks: `assign_due_horizons` quote filter widens from `quality='ok'` to
`quality IN ('ok','probe_ok')`. On DexScreener chains nothing changes (no probe rows exist there);
on 4663 outcomes become measurable.

Outcome gate: `create_outcome_v2` accepts `network in set(FOMO_TO_DEX) | MEMECOIN_DEX_BLIND_CHAINS`.
`FOMO_TO_DEX` itself is untouched (it encodes "chains DexScreener can price" — adding 4663 there would
lie to `resolve_evm_chain`/slug logic).

### B. Candidate cohorts — sweep every concrete-chain coin the feed shows us

Hook in `sniper/services/memecoin_runner.py::run_once` after the current poll's event loop has sent
every eligible event through the funded hook (the same function still sees EVERY event, both sides,
no size floor). Candidate research writes must not precede on-chain submission:

- For each **buy** event in the poll with `network_id in set(FOMO_TO_DEX) | MEMECOIN_DEX_BLIND_CHAINS`
  (i.e. concrete chains only — `evm-unknown` stays out until identity healing, separate concern):
  `register_capture_cohort(conn, network_id, mint, context='candidate', observed_at=now,
  expires_at=now + MEMECOIN_CAPTURE_CANDIDATE_TTL_S, priority=50, commit=False)`, one commit per poll.
- Best-effort try/except — capture must never break the copy path (same contract as firehose retention).
  The cohort timestamp reuses the poll's immutable normalized receipt time; it does not read another
  clock after execution.
- Gate: `MEMECOIN_CAPTURE_CANDIDATES_ENABLED` (default 1).
- Priority 50 < signal 100 < position 400: candidates only consume leftover sweep budget; positions and
  live outcomes are never starved. Starvation is observable via `memecoin_quote_sweeps_v2.budget_skipped_count`.
- `register_capture_cohort` upserts and merges contexts, so repeat events are cheap and an entered coin's
  cohort simply gains the `position` context later.

### C. Settings (sniper/settings/memecoin.py — all env-overridable, fail-safe defaults)

| var | default | meaning |
|---|---|---|
| MEMECOIN_CAPTURE_BLIND_ENABLED | 1 | blind-chain probe worker on/off |
| MEMECOIN_CAPTURE_BLIND_PERIOD_S | 60 | probe worker cadence |
| MEMECOIN_CAPTURE_BLIND_MAX_MINTS | 8 | 0x probes per pass (≈0.13 rps; full 40-coin rotation ≈ 5 min) |
| MEMECOIN_CAPTURE_CANDIDATES_ENABLED | 1 | candidate-cohort registration on/off |
| MEMECOIN_CAPTURE_CANDIDATE_TTL_S | 86400 | candidate capture lifetime (matches 24h max horizon) |

`MEMECOIN_CAPTURE_V2_MAX_MINTS` stays 120 (4 DexScreener batch requests/sweep); blind-slot recovery plus
priority ordering absorbs candidate volume (~50-100 concurrent at current feed rates). Raise via env if
`budget_skipped_count` shows sustained starvation.

<!-- schema:external=memecoin_quote_observations_v2 -->
<!-- schema:external=memecoin_quote_sweeps_v2 -->
<!-- schema:external=memecoin_capture_cohorts_v2 -->
No schema changes: new `quality` values and `dex_id='zerox'` fit existing TEXT columns; `contexts`
already merges `candidate`.

## What could go wrong → mitigation

- **0x probe on illiquid coin ≠ reliable mid** → distinct `probe_ok` quality (never `ok`), ratio guard vs
  `last_good_price`, liquidity NULL (pessimistic slippage downstream), park-on-fail rotation. Lesson
  already encoded from the RUSSELL +$785 phantom (memecoin_evm_price.py:131-136): probes ONLY on chains
  with no alternative.
- **SQLite contention from a second writer thread** → WAL + short transactions + same busy timeout the
  fast-lane worker uses; capture writes are small batches.
- **Cohort growth unbounded** → TTL expiry already enforced by the sweep's `expires_at>=now` filter.
- **Copy-path regression** → the only run_once addition is an OR-IGNORE-style upsert loop behind
  try/except after current submissions; capture_v2_pass main-sweep query shrinks (blind exclusion).
  No new HTTP or candidate DB work precedes the funded hook.

## Verification

- `python3 -m pytest tests/test_memecoin_capture_v2_quotes.py tests/test_memecoin_capture_v2_horizons.py tests/test_memecoin_evm_price.py tests/test_memecoin_capture_blind.py -q` (new file for A+B).
- Post-deploy (pinned runtime, standard gates): within 30 min, `memecoin_quote_observations_v2` shows
  4663 rows with `quality='probe_ok'` and `dex_id='zerox'`; `memecoin_capture_cohorts_v2` shows
  `contexts LIKE '%candidate%'` rows; main-sweep missing_pair rate for 4663 drops to zero (excluded).
- Success metric (study re-run gate): ≥300 reliable-anchor coins before re-running the lifecycle study.
