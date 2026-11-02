# RISK actual-fill baseline and apply validation

Operator request: fix RISK's fill-price baseline, require fresh exact-quote and trailing-drawdown
validation before tightening, coordinate with Beta, add regression tests and obtain Gamma review.
This branch is offline only. No push, deployment, restart, provider/model call, funded transaction,
production ledger write or environment write is part of this change.

## Contracts and edge cases

- Candles are SOL/token; the actual entry fill is USD/token. Normalize using the validated local
  universe heartbeat's SOL/USD rate. Heartbeat age is at most 5 seconds, original rate plus heartbeat
  age at most 120 seconds. Missing, stale, future, disconnected, boolean or non-finite data is unknown;
  never fall back to the first candle. This remains a market-tape comparison, not executable PnL.
  Historical SOL candles are repriced at snapshot FX, not reconstructed historical USD highs; the
  model prompt names that limit explicitly.
- Pre-entry and mixed fill-second highs cannot establish a post-entry peak. Unknown fill/conversion
  produces unknown chart ratios. Exact whole-bag PnL remains a separate input; malformed, future or
  non-finite quote inputs do not become a number or crash fact construction.
- Before model or attention tightening, reread the current persisted position and validate the
  sweep's already-computed whole-position quote. Both original quote and observation must be aware timestamps,
  not future and within the existing 30-second exact-mark bound. Identity and current residual size
  must match; the preview's own `input_quantity` supplies the quoted size. The current preview takes
  precedence over buffered history and cannot fall back to it when invalid. Absence records
  `no_current_exact_quote`. Standalone callers without a current-preview argument retain the same
  validation on the latest persisted `exit_decision` quote. No retrieval, retries or provider calls
  are added. Model application runs after the existing preview, before rail nomination; it no longer
  applies during the quote prepass. An explicit model EXIT still uses the shared claim and rail priority.
- Quote timestamps are corrected UTC. Freshness subtracts the learned host offset from the injected
  host time; decision expiry and apply lag retain their existing host-time basis. A missing clock
  offset retains the clock utility's zero-offset behavior; invalid ages refuse tightening.
- Workers capture corrected fact time separately from host time. Ledger holding/quote ages, timecap
  boundaries and chain candle bounds/labels use corrected UTC. Local heartbeat, attention, model TTL,
  cadence and receipt windows retain host time. PumpSwap receipt-window bounds convert the fill time
  into host time, then filter resulting candle timestamps in chain UTC. No historical rows are rewritten.
- Use the funded executable confirmation's SL/TP, partial-fill and trail-arming semantics to refuse
  an already-triggered changed rail. Trailing checks use the current executable peak, never nominal
  highs. Model proposals are all-or-nothing. Attention auto-tightening may apply independently safe
  fields while refusing a crossed or unvalidated field; record `applied_partial` and per-field refusal
  details. No safe field means refusal. Invalid quote/identity/size refuses the whole proposal for
  either source. Refusal never turns tightening into an EXIT.
- Preserve operator precedence, frozen contracts, expiry, source identity, acknowledgements and
  deterministic exits. Quote validation and override/ack writes share a short deferred transaction;
  a read/write conflict rolls back the change. No global state mutation or new entry-path work.
- Both live and shadow fact contracts receive a new version (`desk-risk-live-v7`,
  `desk-risk-shadow-v3`). They must not be pooled with earlier baseline semantics.

This does not select an optimal trail, prohibit tightening losing positions, reconstruct historical
USD candles, prove the 15% counterfactual or change the approved book/clip/loss limits. No schema
migration is needed. Rollback is the preceding code release; existing frozen contracts and applied
overrides remain readable. A later authorized cutover must update watcher and runner together.

## Verification

- Baseline before editing: 36 existing focused tests passed with `-n 2`.
- New baseline/caller/freshness regressions: 30 failed on the old implementation, then passed.
- Additional malformed exact-mark regressions: 9 failed, with 31 controls passing before repair.
- Independent source review found oversized-integer and numeric-ratio overflow edges: 8 regression
  cases failed before the finite-number validation was hardened.
- Apply regressions cover both sources, missing/stale/future/incorrect identities and quantities,
  partial fills, arming, crossed SL/TP/trail, operator priority, idempotency and rollback.
- Repeatable integrated gate:

  ```bash
  python3 -m pytest -n 2 \
    tests/test_desk_risk_fill_baseline.py tests/test_desk_risk_tighten_quote_guard.py \
    tests/test_memecoin_desk_risk_live.py tests/test_memecoin_desk_risk_shadow.py \
    tests/test_memecoin_desk_risk_watcher.py tests/test_memecoin_desk_risk_structure_feed.py \
    tests/test_desk_risk_exact_pnl.py tests/test_memecoin_desk_risk_apply.py \
    tests/test_memecoin_desk_attention.py tests/test_desk_attention_signals.py \
    tests/test_desk_attention_worker.py tests/test_memecoin_desk_attention_watchdog.py \
    tests/test_memecoin_real_rails_v2.py tests/test_memecoin_real_peak_tracking.py \
    tests/test_memecoin_real_exit_override.py tests/test_memecoin_exit_overrides.py \
    tests/test_memecoin_exit_preview_quantity.py tests/test_memecoin_exit_sweep_timing.py \
    tests/test_memecoin_funded_policy.py tests/test_memecoin_exit_intent.py \
    tests/test_memecoin_real_runner.py tests/test_memecoin_desk_risk_watcher_watchdog.py \
    tests/test_desk_ps_tape.py tests/test_desk_structure_tape.py -q --tb=short
  ```
- Schema references, whitespace, relevant neighboring funded-exit regressions and Gamma's exact-tree
  review are required before declaring this branch ready. No live improvement claim follows.

Gamma reviewed initial commit `8b62fb20d38a7cb28f44253aa5432f1385424404`, independently passed 232
tests and accepted the baseline/quote checks. The review found that whole-proposal refusal could
discard attention's valid defensive stop when its proposed minimum trail was already crossed.
The configured trail minimum is 8%; an empty-settings review shell does not represent that config.
The follow-up preserves the safe attention stop, explicitly refuses the trail, and retains whole
model-proposal refusal. This addresses the review without adding exit authority or tuning thresholds.
With an empty rail-bounds setting, `trail_min` is `None`: attention does not propose a trail and
can still apply its independently valid stop.

Source review also proved the route-series writer buffers the current sweep's quote after attention
application. A 30-second-old-enough retained quote could therefore approve a rail already crossed by
the current preview. Both apply sources now use the current preview directly, with original time and
quoted quantity intact. Regressions reproduced the old acceptance and missing-current cases before
the repair. Fieldwise attention regressions failed 6 cases before repair; the current-preview additions
failed 15 cases, quoted-size checks failed 2, and explicit missing-current reasons failed before repair.

## Local evidence

The initial 22-file combined gate passed **506 tests**, `-n 2`, in 43.32 seconds. The expanded
current-preview gate passed **531 tests**, in 43.69 seconds. Final missing-current and fieldwise checks
passed a **145-test** focused gate. After merging local main `65b91f0875810c6f807400e0fdff9105da0563d3`,
the **24-file integrated gate passed 571 tests in 44.06 seconds**. Beta's PumpSwap bars and the validated
FX injection are both preserved, with version `v7`. Schema reference and whitespace checks passed.
No full repository suite was run. Gamma's new exact-pin review is the remaining external review gate.

The final source audit found a clock-domain mismatch: corrected quote timestamps were compared
against host time. Twelve offset regressions (host 7 hours fast/slow; fresh, stale and future quotes)
produced **8 failures and 4 passing controls** before repair. After aligning quote freshness, the
**103-test quote guard file passed**. Existing model TTLs and acknowledgement lag remain host-based.
The related live/shadow fact and curve/PumpSwap worker seam regressions produced **12 failures before
repair**, then **130 tests passed across five focused files**. They cover host offsets of both signs,
midnight partitions, current PnL/holding/candle inputs, pre-entry/future PumpSwap rows, and unchanged
heartbeat/attention/TTL domains.

After both clock repairs, the final **24-file integrated gate passed 595 tests in 45.10 seconds**
with `-n 2`. The clock changes add no entry-path work, providers, global state mutation or production
writes. Existing corrected historical timestamps and host-time decision rows retain their meanings.

The initial apply guard's offline in-memory benchmark (1,000 iterations after 50 warmups) measured
p50 **541 microseconds**, p90 **566 microseconds**, including the deferred snapshot, position/policy,
retained quote/size reads and SL/trail validation. This is fixture CPU/SQLite cost, not live latency;
the entry path has no added calls. The baseline regression includes a real temporary candle DB and
heartbeat with differing SOL/USD units, a pre-fill candle and a mixed fill-second high.
