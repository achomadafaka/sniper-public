# Memecoin Funded Entry Contract and Deterministic Regime Design

**Date:** 2026-07-18
**Status:** Approved by operator
**Authority:** Funded entry classification, one-way clip reduction, deterministic post-peak refusal,
frozen per-position exit policy, and operator observability. No learned model and no paper-wait gate.

## Goal

Every funded attempt must state what coin and market state we are entering, why the entry is allowed,
what behavior the runner expects, and the exact rails that govern the position. That decision must be
immutable, replayable, chain-correct, and visible in the cockpit.

This design activates deterministic production behavior immediately after pinned deployment. It does
not wait for a paper cohort. Unproven market regimes may reduce risk but may not veto an otherwise valid
entry. Only existing hard safety gates and a separately confirmed post-peak collapse may refuse.

## Relationship to current contracts

This spec narrowly supersedes the funded-authority prohibitions in:

- `2026-07-13-memecoin-adaptive-exit-observation-foundation-design.md`;
- `2026-07-16-memecoin-entry-radar-coin-state-design.md`; and
- `2026-07-17-memecoin-learning-loop-design.md`.

Their identity, clock, attribution, append-only evidence, source-lane separation, no-lookahead, and
fail-closed execution rules remain authoritative. Their paper-only restrictions remain in force for
learned models, holder-driven exit suppression, wallet arming, and automatic parameter optimization.

The implementation reuses the existing entry snapshots, token episodes, quote observations, attempt
traces, reservations, positions, and exit audit. It does not create a second attempt ledger.

## Current defects this design closes

1. `memecoin_entry_snapshots` has 1,951 rows, but every `regime_id` and `wallet_trust_tier` is `NULL`.
2. Real positions do not freeze the settings or expectation that selected their live exit behavior.
3. The UI shows current rails but not the entry type, regime, expectation, invalidation, or confidence.
4. The configured total-exposure cap is disabled, and the funded concurrency accounting is not proven
   across every Solana/EVM reservation and open position.
5. The 1–2M `mid` ladder is defined and unit-tested, but the funded hook only calls
   `assign_micro_policy` when `micro_class` is nano/micro. A real mid fill therefore misses its intended
   `+30%` de-risk / `35%` trail / `-60%` stop policy.
6. `build_snapshot` stamps `decided_at` from host wall time instead of the corrected real-UTC clock.
7. The funded entry path treats a price below the copied trader as a gift even when the local tape shows
   a completed pump and large giveback. There is no exact-quote-confirmed falling-pump refusal.

## Non-negotiable invariants

1. Coin identity is `(network_id, mint)`. Robinhood is `4663`, never generic EVM.
2. `global_feed` and `follow_activity` remain distinct facts and distinct evaluation segments.
3. All decision timestamps use corrected real UTC supplied by the runner. Host wall time is not stored as
   decision time.
4. No new network request is introduced. The classifier reuses the existing market-data response, local
   feed/quote tape, and the exact route quote the executor already obtains.
5. A display or nominal quote can nominate a post-peak refusal but cannot authorize it. The normalized
   exact route quote must confirm the same drawdown before broadcast.
6. Missing regime, lifecycle, liquidity, or momentum data never becomes zero and never vetoes an entry.
7. An unproven regime can only keep or reduce the clip. It cannot increase size, loosen a stop, delay a
   de-risk, widen a trail, suppress an exit, add size, or re-enter.
8. Operator overrides remain explicit, higher-priority, and audited. They never rewrite the original
   funded contract.
9. Existing positions are untouched. Only reservations created at or after the deployment cutover carry
   a funded contract and read frozen contract rails.
10. Reservation, exact-amount approval, write-ahead signature/nonce, no-double-submit, exact-balance fill,
    executable-exit confirmation, kill-switch, daily-loss, and mirror-sell safety remain authoritative.

## Approach decision

Three approaches were considered:

1. **More global `.env` knobs:** simple, but no per-entry record and later config drift silently changes
   open-position behavior.
2. **Learned/HMM regime selector:** rejected at current funded sample depth. It introduces unstable states,
   opaque authority, and parameters that cannot be validated per chain/source cell.
3. **Deterministic frozen contract:** selected. It is interpretable, versioned, testable end-to-end, and
   keeps all new authority monotonic in risk.

External work supports price/volume thresholding as a practical pump detector and shows that HMM regime
models are normally fit on much larger major-coin time series. AMM execution remains amount-dependent,
so the exact route quote—not a display price—must confirm executable state:

- https://arxiv.org/abs/2503.08692
- https://arxiv.org/abs/2011.03741
- https://developers.uniswap.org/docs/protocols/v2/concepts/pricing

## Architecture

### 1. `EntryClassification`

Create `sniper/services/memecoin_market_state.py` with frozen dataclasses and pure helpers. The public
resolver is:

```python
classify_funded_entry(
    conn,
    *,
    network_id: str,
    mint: str,
    decision_at: str,
    signal_source: str,
    effective_mcap: float | None,
    assumed_micro: bool,
    market_stats: dict,
    base_clip_usd: float | None,
    consensus_n_60m: int | None,
) -> EntryClassification
```

The canonical classification contains:

```text
classification_version = 1
coin_band               = assumed_nano | nano | micro | small | mid | out_of_scope
lifecycle               = fresh | young | mature | revival | unknown
liquidity_class         = thin | balanced | deep | unknown
momentum_phase          = climbing | extended | post_peak | flat | unknown
market_regime           = hot_broad | hot_narrow | normal | thin | unknown
data_quality            = complete | partial | critical_unknown
risk_scale              = 1.00 | 0.75
reason_codes[]
facts{}
```

Classification values are descriptive except `risk_scale` and `post_peak` confirmation. The runner
builds a draft after identity/mcap/market-data resolution. An early terminal gate persists that draft
with any unavailable sizing-dependent facts marked unknown. When reservation is reached, the risk gate
computes the balance-aware base clip, finalizes the liquidity class, and persists the classification once.

### 2. Coin type

The funded bands preserve current thresholds:

| Band | Rule |
|---|---|
| `assumed_nano` | market cap unresolved and the existing assume-micro rule explicitly allowed it |
| `nano` | resolved market cap below `$50,000` |
| `micro` | `$50,000 <= mcap < $250,000` |
| `small` | `$250,000 <= mcap < $1,000,000` |
| `mid` | `$1,000,000 <= mcap < $2,000,000`, after the existing consensus gate |
| `out_of_scope` | unresolved without the explicit assumption, or at/above the live `$2M` ceiling; audit-only and never reservable |

The live `$2M` ceiling remains an entry refusal. Unknown market cap is never presented as a measured
`$0`; `assumed_nano` records the explicit assumption and its reason.

Lifecycle uses existing pair age:

- `fresh`: age at most one hour;
- `young`: over one hour and at most 24 hours;
- `revival`: over 24 hours and current momentum has at least `+20%` runup;
- `mature`: over 24 hours without revival evidence;
- `unknown`: pair age unavailable.

Liquidity is relative to the unscaled base clip and remains descriptive because the exact impact gate
is authoritative. If an attempt terminates before a balance-aware base clip exists, liquidity class is
`unknown` rather than being calculated from a configured placeholder:

- `deep`: liquidity at least `100x` the base clip;
- `balanced`: liquidity at least `20x` but below `100x` the base clip;
- `thin`: liquidity below `20x` the base clip;
- `unknown`: no positive liquidity value.

### 3. Coin momentum and post-peak nomination

Read only positive `memecoin_quote_observations_v2` prices with quality `ok` or `probe_ok` for the exact
coin identity during the 15 minutes before `decision_at`. Include the current market-data price without
duplicating an identical final observation.

A usable tape requires at least three observations spanning at least 30 seconds. Let `first`, `peak`, and
`current` be the first, maximum, and latest positive normalized token/USD prices.

```text
runup_pct   = (peak / first - 1) * 100
giveback_pct = (1 - current / peak) * 100
```

- `post_peak`: `runup_pct >= 20` and `giveback_pct >= 30`;
- `extended`: current is at least `+100%` over first and within `10%` of peak;
- `climbing`: current is at least `+10%` over first and within `10%` of peak;
- `flat`: usable tape without the prior conditions;
- `unknown`: unusable tape.

`post_peak` is only a nomination. The executor refuses before signing/broadcast only when its existing
normalized exact buy quote is also at least `30%` below the nominated peak. A missing, non-positive,
stale, or unit-incomparable exact price fails open to existing safety gates. The refusal releases the
reservation with terminal reason `post_peak_confirmed` and writes the exact/display facts to the attempt
trace. Tests must prove this on both Solana and `4663`.

### 4. Memecoin-native market regime

The regime uses `memecoin_feed_observations_v2` only; it does not pretend BTC/SOL direction is a complete
memecoin regime. It reads buys observed before `decision_at` and compares the current 15-minute window
with the preceding 23 complete 15-minute buckets (5h45m).

Each bucket records buy count, distinct `(network_id, mint)` count, and distinct buyer count. A baseline
is usable only with at least 30 buys and eight non-empty completed buckets. The latest feed observation
must be no older than 180 seconds on corrected UTC.

Let `coin_med` and `buyer_med` be the medians of the completed buckets:

- `hot_broad`: current distinct coins are at least `max(5, 1.75 * coin_med)` and buyers are at least
  `max(3, 1.50 * buyer_med)`;
- `hot_narrow`: not broad, and either exact-coin consensus is at least two prior buyers or usable coin
  momentum has at least `+20%` runup;
- `thin`: current coins and buyers are both at most `0.50` of their non-zero baseline medians;
- `normal`: usable/fresh baseline without the prior conditions;
- `unknown`: stale or insufficient market tape.

The source lane remains a contract fact. Performance reports may not pool global-feed and
follow-activity outcomes merely because both saw the same market regime.

### 5. Risk authority

`hot_broad` and `normal` use `risk_scale=1.00`. `hot_narrow`, `thin`, and `unknown` use `0.75`.

The reservation gate computes its current balance-aware base clip first, then applies the scale:

```text
approved_clip = min(base_clip, max(MEMECOIN_MIN_TRADE_FLOOR_USD, base_clip * risk_scale))
```

The minimum floor may neutralize a haircut on a small bankroll, but it may never increase the clip above
`base_clip`. A regime alone therefore cannot turn an otherwise allowed entry into a rejection.

### 6. Frozen `FundedEntryContract`

Add version/hash/JSON columns to `memecoin_entry_attempts`, `memecoin_pending_entries`, and
`memecoin_positions`. Do not add a parallel attempt table.

The attempt receives immutable classification JSON at its terminal pre-reservation gate or inside the
allowed reservation transaction. A second write is idempotent only when the hash matches; a different
hash is an error and leaves the first record unchanged.

For an allowed reservation, `reserve_entry` constructs the full contract inside the same transaction that
inserts the pending row. The contract includes:

- exact identity, source lane, corrected decision time, and origin event;
- classification, reason codes, input facts, missing-data flags, and risk scale;
- original balance-aware base clip and approved clip;
- policy ID/version and every selected exit value;
- an expectation object and invalidation reason list;
- canonical JSON and SHA-256 hash.

Solana/EVM executors copy the pending contract byte-for-byte to the position on confirmed balance-delta
fill. Crash recovery uses the same pending payload. A filled position without a matching contract hash is
a deployment-blocking invariant failure for the new cutover cohort.

`build_snapshot` receives the caller's corrected `now_iso` and stores it as `decided_at`; it no longer
samples host wall time internally. Snapshot failures remain best-effort, while funded attempt/contract
persistence is authoritative for funded behavior.

### 7. Frozen funded exit policy

Create one pure resolver in `sniper/services/memecoin_funded_policy.py`. It freezes current effective
production behavior at reservation time:

- assumed/nano: no full TP, `+40%` de-risk, `40%` post-de-risk trail, `-60%` scoped stop;
- micro: no full TP, `+50%` de-risk, `50%` post-de-risk trail, `-60%` scoped stop;
- small: deployed rollover defaults (`TP=0`, `+40%` de-risk, `30%` trail, `+15%` arm), chain-specific
  normal stop, `-80%` catastrophe floor;
- mid: no full TP, `+30%` de-risk, `35%` post-de-risk trail, `-60%` scoped stop;
- all: current stop debounce, max hold, unquotable guard, mirror behavior, catastrophe floor, and exact
  partial-exit semantics.

This closes the current mid-band wiring defect and removes post-fill system-policy assignment as a failure
point for new positions. Existing `micro`/`ride` rows remain readable for historical positions.

For contract-cohort positions, nomination and executable confirmation read the same frozen values. The
effective operator override is then overlaid at evaluation time. Existing positions without a contract
continue through the deployed legacy/rollover cohort logic unchanged.

The expectation object is descriptive, not an expected-PnL claim:

```text
intent              = recover_principal_then_capture_trend | capture_trend
principal_recovery  = exact configured de-risk trigger or null
trail_activation    = exact arm rule
trail_giveback      = exact percent
normal_stop         = exact percent or disabled
catastrophe_stop    = exact percent
max_hold_s          = exact value
unquotable_exit_s   = exact value
invalidations       = post_peak_confirmed, mirror_sell, stop, catastrophe, trail, timecap,
                      unquotable, manual_override
```

### 8. Cross-venue portfolio envelope

The funded risk gate must count all open real positions plus all unresolved reservations across Solana and
every EVM chain. Network identity is still recorded for diagnostics, but no venue bypasses the book cap.

Deployment settings:

```text
MEMECOIN_MAX_CONCURRENT=7
MEMECOIN_MAX_TOTAL_EXPOSURE_USD=700
```

Total exposure is entry cost/notional for open positions plus reserved notional for pending entries. It
does not use display mark value. The current daily-loss, minimum-trade, gas, balance, and micro-capacity
rules remain additional gates.

### 9. API and cockpit

The real-position API returns a structured `entry_contract` and never asks the browser to reconstruct
policy from current environment values. It also returns `effective_exit_policy` separately so an operator
override is visibly distinct from the original contract.

The dense cockpit shows compact badges and a detail block:

```text
TYPE      small · young · balanced
STATE     hot_narrow · climbing · partial data
WHY       approved wallet · consensus 2 · exact route · impact 3.1%
EXPECT    derisk +40 · arm +15 · trail 30 · stop -60 · catastrophe -80 · max 7d
INVALID   post-peak · mirror sell · stop/trail · unquotable 24h
SIZE      base $X · regime 75% · filled $Y
POLICY    funded-small-v1 · frozen <hash-prefix> · operator override none|active
```

Unknowns render as `unknown`, never zero. Existing pre-contract positions render `legacy policy` with
their current effective rails.

## Error handling

- Classification-read failure produces `market_regime=unknown`, `risk_scale=0.75`, explicit reason code,
  and otherwise continues through existing gates.
- Contract canonicalization or validation failure occurs before reservation and rejects with
  `invalid_funded_contract`; no transaction is submitted.
- Pending/position contract hash mismatch blocks startup/deployment reconciliation for the new cohort.
- Post-peak exact confirmation failure releases the reservation before signing/broadcast.
- API/UI parsing failure renders the raw policy ID and hash; it cannot change runner behavior.
- All numeric settings reject booleans, NaN, infinity, negative values, invalid ratios, and unordered band
  thresholds during funded-runner startup.

## TDD acceptance

1. Coin/lifecycle/liquidity/momentum/regime boundaries and every unknown state are pure-tested.
2. Market-state calculations use only prior observations; a decision-time or future row cannot leak in.
3. `global_feed` and `follow_activity` stay explicit in contract and reports.
4. A nominal post-peak tape cannot block without exact-quote confirmation.
5. Exact confirmation refuses before broadcast and releases the reservation on Solana and `4663`.
6. Missing exact price fails open to existing safety gates.
7. Regime scaling never raises size and never rejects solely because the scaled clip is below the floor.
8. Attempt classification is immutable; pending and filled hashes match byte-for-byte.
9. A restart/config change cannot alter a contract-cohort position's nomination or confirmation rails.
10. Operator override changes effective rails without changing the original contract.
11. Full funded-hook tests prove nano, micro, small, and mid fills receive their intended policy; the mid
    test must fail against the pre-fix hook.
12. Global concurrency/exposure includes Solana, EVM, and reservations exactly once.
13. Existing positions without contracts retain their deployed cohort behavior.
14. API/UI show type, state, expectation, invalidation, frozen policy, effective override, and unknowns.
15. Schema validation, Python compilation, JavaScript syntax, focused funded tests, complete memecoin test
    suite, and `git diff --check` pass.

## Deployment and rollback

1. Build on an isolated branch from current `origin/main`; do not mix the unrelated cockpit UI branch.
2. Merge only after local verification, CI, and the Knowledge Sync Gate.
3. Use the deploy script's coordinated activation to stop the old runner, recheck zero reservations,
   then atomically write the exposure cap, classifier defaults, and corrected-UTC contract cutover. Never
   hand-write a future cutoff while old code can still reserve a contractless entry.
4. Deploy only through `scripts/deploy_memecoin_runtime.sh <sha> --activate-rollover`; the mutable
   checkout never runs funded code.
5. Reconcile reservations, positions, balances, contract hashes, and current open-bag exclusions before
   switching `.runtime/memecoin/current`.
6. Restart the funded runner, verify its exact pinned SHA/process start time, inspect startup validation,
   and probe authenticated memecoin endpoints.
7. New eligible funded trades use the contract immediately. There is no paper observation delay.
8. Rollback restores the prior pinned release. Contract columns are additive; prior code ignores them.
   New contract positions must not be managed by prior code without an explicit operator rollback decision,
   because prior code would fall back to mutable environment rails.

## Knowledge delta

This is a funded-runner doctrine and behavior change. The implementation branch must update `AGENTS.md`
and `CLAUDE.md` with the frozen-contract, no-regime-veto, exact-post-peak-confirmation, and cross-venue
exposure invariants. The PR must include the full Knowledge Delta Report. A Supabase memory write remains a
separate explicitly authorized Knowledge Sync invocation under repository policy.

---

## Amendment 2026-07-21 — reconciliation severity split (outage class fix)

The 2026-07-21 funded outage: a CLOSED manual position (id 9920) and its CONSUMED reservation
(pending 172) were missing contracts (the manual `/real/buy` path pre-`e321934a` never stamped
them). The startup reconciliation treated them like live risk and refused every armed relaunch
after a reboot — 26 minutes of crash-loop guarding rows whose money outcome was already settled.

Amended contract (implemented in `reconcile_severity`):

- **Deploy reconciliation is unchanged and strict**: every post-cutover violation — live or
  terminal — fails `scripts/validate_memecoin_funded_contracts.py`. Deploys are attended; audit
  debt gets repaired deliberately, never carried forward.
- **Armed startup blocks only on live risk**: an OPEN real position or an UNRESOLVED reservation
  (status not in `confirmed`/`failed`) with a missing/malformed/mismatched contract still refuses
  startup, exactly as before.
- **Terminal audit debt pages, never blocks startup**: the same violations on a closed position,
  a confirmed/failed reservation, or an attempt journal row produce a loud startup log line plus
  a Telegram page, and the runner starts. Rows with an unparseable time field remain blocking
  (unknown state fails closed).
