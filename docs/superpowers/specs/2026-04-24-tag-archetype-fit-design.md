---
memory_name: spec/tag-archetype-fit-2026-04-24
memory_type: reference
memory_description: B2 spec — per-(wallet, tag) fit score in [0,1] over realized PnL/behavior. v1 ships hand-set equal-weight; empirical weight-fitting deferred to v2 pending labeled-data threshold. Methodology-reviewed 2026-04-25.
memory_tags: [spec, tag-analytics, wallet, archetype, b2]
---

# B2 — `wallet_tag_fit` — per-(wallet, tag) fit score

**Status:** READY for `/ship` once B1 patch (§3.1) lands.
**Date:** 2026-04-24 (revised 2026-04-25)
**Parent plan:** [wallet-intelligence-rearchitecture](../plans/2026-04-24-wallet-intelligence-rearchitecture.md) task B2
**Upstream blocker:** [B1 tag_wallet_pnl](./2026-04-24-tag-wallet-pnl-design.md) + [B1 patch (held-to-resolution)](./2026-04-25-tag-wallet-pnl-held-only-patch.md)
**Methodology review:** 2026-04-25, research-methodologist — verdict `needs-revision` → addressed (this revision).

## 0. Audit trail

This spec was revised 2026-04-25 in response to:
- methodology review (6 ambiguities → locked decisions, see §9)
- B1 sell-side audit (`win_rate` is sold-before-resolution-inflated → B1 patch required, see §3.1)
- `wallet_memberships` UI audit (operator sees aggregate WR/PnL but **not** per-tag breakdown → v2 empirical fitting methodologically valid; deferred only on sample size, see §4.6)

The §3.3 nomenclature collision in `tag-analytics.md` is resolved by renaming this deliverable from `tag_archetype_fit` to **`wallet_tag_fit`** (grain = per-wallet-per-tag). The aggregate per-(tag, archetype) deliverable in `tag-analytics.md §3.3` retains the `tag_archetype_*` namespace and should be renamed to `tag_archetype_cooccurrence` in a follow-up doc edit.

## 1. Goal

Project the `wallet_tag_pnl` matrix onto a normalized fit scalar in `[0, 1]` per (wallet, tag) so downstream consumers (review queue, strategy-derivation scorer) can answer "where does this wallet rank among peers on this tag, given realized edge, sample, consistency, and recency?" without re-running PnL reconstruction.

The score is a **within-tag percentile rank** — not portable across tags (see §4.4).

## 2. Non-goals

- Not a forward-PnL forecast.
- Not a global wallet quality score (that's `wallet_quality_score`).
- Not an archetype label (that's `wallet_archetype_extractor.py` → C1's `wallet_archetypes` table).
- Not portable across tags (fit=0.9 on `politics` ≠ fit=0.9 on `sports`).

## 3. Data sources

| Table | Use |
|-------|-----|
| `wallet_tag_pnl` (B1, post-patch) | (wallet, tag, n_positions, realized_pnl, win_rate, **win_rate_held_only**, **n_held_to_resolution**, avg_hold_days, conviction_concentration, computed_at) |
| `market_tags` | per-tag universe size (peer-count denominator) |
| `wallet_memberships` | v2 ONLY — operator accept/reject labels for empirical weight-fitting (deferred) |

<!-- schema:validate-columns-for=wallet_tag_pnl -->

Schema validated via `python3 scripts/brain_index.py schema wallet_tag_pnl`.

### 3.1 Hard upstream blocker — B1 patch required

B1's current `win_rate` is computed over **all resolved positions including those closed via SELL/REDEEM events** (sniper/analysis/tag_wallet_pnl.py:325-374). This inflates WR ~5-10pp because sellers cluster on winners (same mechanism as the calibration-mispricing dead-end documented in brain entry `calibration-analysis-geo-crypto-mispriced-2026-03-31`).

**B2 must NOT consume B1's `win_rate` field.** B1 must first ship the patch defined in `docs/superpowers/specs/2026-04-25-tag-wallet-pnl-held-only-patch.md` adding:
- `win_rate_held_only REAL NOT NULL` — wins / n where n excludes positions closed purely via SELL (positions resolved via REDEEM or residual-at-resolved-price are kept)
- `n_held_to_resolution INTEGER NOT NULL` — sample size for `win_rate_held_only`

B2's consistency component (§4.3) reads only `win_rate_held_only`.

## 4. Scoring inputs (LOCKED — v1)

**v1 weighting:** equal-weight hand-set across the four active components below, after each is z-scored within-tag and rank-transformed to [0,1]. Concentration is computed but weight = 0; raw value persisted in `inputs_json` for v2 evaluation.

**v2 weighting:** empirical regression against `wallet_memberships` accept/reject decisions. **Blocked** until labeled set ≥100 decisions with ≥30 rejects (10 events-per-variable × 5 components — standard logistic-regression power). Current labeled set: 12 followed + 36 watchlist ≈ 48 positives. Revisit when threshold is met.

### 4.1 Realized edge

Per-tag percentile rank of `realized_pnl / n_positions` (avg $/position) within the tag's peer distribution.

### 4.2 Sample strength

`min(sqrt(n_held_to_resolution) / sqrt(100), 1.0)` — sqrt-scale, capped at n=100.

Reads `n_held_to_resolution`, NOT `n_positions`. Held-to-resolution is the unbiased sample size.

### 4.3 Consistency (Bayesian-shrunk WR)

Beta prior `Beta(μ·k, (1-μ)·k)` where:
- `μ` = peer-mean `win_rate_held_only` for the tag (computed across all wallets with ≥3 held-to-resolution positions on that tag)
- `k` = `max(5, median peer n_held_to_resolution)` — prior strength

Posterior mean: `(wins + μ·k) / (n_held_to_resolution + k)`.

Then percentile-rank within tag.

**Reason:** baseline WR varies wildly by tag (geopolitical NO ~76%, sports ~50%); uniform 0.5 prior under-shrinks outliers on biased tags.

### 4.4 Recency

Exponential decay on `last_activity_ts` (max activity_ts of the wallet's positions on this tag) with **half-life = 90 days**.

`recency_weight = 0.5 ** (days_since_last / 90)`.

Then percentile-rank within tag.

**Reason:** 90 days matches Polymarket wallet turnover observed in followed-wallet activity. Documented choice — change requires spec edit.

### 4.5 Concentration (weight = 0 in v1)

Raw `conviction_concentration` from B1 is computed and persisted into `inputs_json` for auditability and future evaluation, but contributes 0 to v1 fit_score.

**Reason:** direction undetermined. High concentration could indicate edge (specialist) or survivorship bias (one lucky market). Direction must be settled empirically before activation:

> Within peer strata (same tag, same n_held_to_resolution decile), regress operator-accept on concentration. Sign with |t| > 2 locks the direction. Until that test runs in v2, weight stays at 0.

### 4.6 Cross-tag portability

**Not supported.** `fit_score = X` on tag A is **not comparable** to `fit_score = X` on tag B. The score is a within-tag percentile rank with no tag-edge-variance correction.

Documented in the API surface (§6), the validation tests (§7), and the column comment.

## 5. Output schema (LOCKED)

```sql
CREATE TABLE IF NOT EXISTS wallet_tag_fit (
    wallet              TEXT    NOT NULL,
    tag                 TEXT    NOT NULL,
    fit_score           REAL    NOT NULL,  -- [0, 1] within-tag percentile; NOT cross-tag portable
    fit_rank            INTEGER NOT NULL,  -- 1-based rank within tag
    peer_count          INTEGER NOT NULL,  -- wallets scored against same tag
    confidence_flag     TEXT    NOT NULL,  -- 'ok' if peer_count >= 30, else 'low_confidence'
    computed_at         INTEGER NOT NULL,
    inputs_json         TEXT    NOT NULL,  -- {edge, sample, consistency, recency, concentration_raw, components_z, weights, version}
    PRIMARY KEY (wallet, tag)
);
CREATE INDEX IF NOT EXISTS idx_wtf_tag_rank ON wallet_tag_fit (tag, fit_rank);
CREATE INDEX IF NOT EXISTS idx_wtf_wallet  ON wallet_tag_fit (wallet);
```

<!-- schema:validate-columns-for=wallet_tag_fit -->

### 5.1 Reporting floor

- **Compute** for any (wallet, tag) where `peer_count >= 10` AND wallet has `n_held_to_resolution >= 3` on the tag.
- **Flag** `confidence_flag = 'low_confidence'` when `peer_count < 30`.
- **Consumers** (review queue, derivation scorer) MUST filter on `confidence_flag = 'ok'` for production decisions; low-confidence rows are stored for monitoring only.

**Reason:** percentile ranks below ~30 peers are too noisy for production use, but storing the row preserves the signal for tag growth tracking.

### 5.2 inputs_json shape

```json
{
  "version": "v1",
  "components": {
    "edge_pct": 0.92,
    "sample_pct": 0.61,
    "consistency_pct": 0.78,
    "recency_pct": 0.85
  },
  "raw": {
    "realized_pnl_per_position": 1240.5,
    "n_held_to_resolution": 18,
    "win_rate_held_only": 0.722,
    "wr_posterior_mean": 0.689,
    "days_since_last": 12,
    "concentration_raw": 0.41
  },
  "peer_stats": {
    "tag": "politics",
    "peer_count": 47,
    "peer_mean_wr_held": 0.612,
    "peer_median_n_held": 8,
    "k_prior_strength": 8
  },
  "weights": {"edge": 0.25, "sample": 0.25, "consistency": 0.25, "recency": 0.25, "concentration": 0.0}
}
```

## 6. Module + CLI

Mirror B1 structure:

- **Module:** `sniper/analysis/wallet_tag_fit.py` — pure functions:
  - `compute_peer_stats(conn) -> dict[tag, PeerStats]` — one pass over `wallet_tag_pnl` to build per-tag distributions
  - `compute_wallet_tag_fit(wallet_tag_pnl_row, peer_stats) -> FitRow` — pure scoring
  - `compute_all_fits(conn, min_peers=10) -> Iterable[FitRow]`
- **CLI:** `scripts/build_wallet_tag_fit.py` with flags `--wallet`, `--all`, `--tag`, `--min-peers` (default 10), `--db`. Mirrors `build_wallet_tag_pnl.py`.
- **Reader:** `db.wallet_tag_fit_rows(conn, wallet=None, tag=None, confidence_only=False, limit=None)` — `confidence_only=True` filters `confidence_flag = 'ok'`. No raw SQL outside `db.py` per CLAUDE.md.

## 7. Validation

### 7.1 Unit tests (`tests/test_wallet_tag_fit.py`)

- **Fixture: 5 wallets × 2 tags.** Known winner ranks #1 on each tag.
- **Within-tag scaling:** all four component percentiles in [0,1]; fit_score in [0,1].
- **Bayesian shrinkage:** wallet with 1W/0L on a tag with peer_mean_wr=0.6 lands posterior_mean ∈ [0.55, 0.75], NOT 1.0.
- **Recency decay:** wallet with `days_since_last = 90` gets recency_weight = 0.5 ± 0.01.
- **Concentration zeroed:** changing concentration_raw does NOT change fit_score in v1.
- **Cross-tag NOT comparable:** assert that ranking the full universe ignoring tag produces tag-clustered top-N (test exposes the documented non-portability).
- **Reporting floor:** `peer_count = 25` → row written with `confidence_flag = 'low_confidence'`; `peer_count = 35` → `'ok'`.
- **Min sample:** wallet with `n_held_to_resolution = 2` → excluded.
- **Determinism:** same input → same output across runs.
- **Empty data:** no positions → no rows, no crash.

### 7.2 Out-of-sample regression (replaces the original §7 circular test)

Cut historical `wallet_tag_pnl` at T = 60 days ago. Compute `wallet_tag_fit` using only pre-T data. Then correlate `fit_score` (pre-T) with `realized_pnl_post_T / n_post_T` per (wallet, tag) on positions resolved AFTER T.

**Pass:** Spearman ρ > 0.3, p < 0.05 on tags with ≥10 wallets having both pre-T fit and post-T positions.
**Fail:** ρ ≤ 0 or p ≥ 0.10 — fit score has no forward signal; investigate before shipping.

This is the only test that validates fit_score is informative, not just well-formed.

### 7.3 Operator sanity check

Sample 10 (wallet, tag) pairs at fit_score deciles {0.1, 0.3, 0.5, 0.7, 0.9} × 2. Manually inspect against the wallet dossier. Document findings in a one-page memo before /ship merge.

## 8. Acceptance criteria

- [ ] B1 patch (§3.1) shipped — `win_rate_held_only` + `n_held_to_resolution` populated.
- [ ] Table `wallet_tag_fit` exists per §5; `brain_index.py generate` refresh clean.
- [ ] Module + CLI per §6; module is pure (no DB writes outside the persistence helper).
- [ ] All §7.1 unit tests green; full suite stays green (`python3 -m pytest tests/ -q`).
- [ ] §7.2 out-of-sample regression run on real DB; result attached to PR; ρ > 0.3 OR explicit go-anyway justification with monitoring plan.
- [ ] §7.3 operator memo committed to `docs/audits/`.
- [ ] CLI `--all --min-peers 10` runs clean against live DB; produces ≥50 rows; ≥10 with `confidence_flag = 'ok'`.
- [ ] Schema validator clean: `python3 scripts/validate_schema_refs.py`.
- [ ] Brain memory `methodology-review/wallet-tag-fit-b2-design` recorded with the methodologist's findings + this revision.

## 9. Decisions log (resolved ambiguities)

| # | Question | Decision | Reason |
|---|----------|----------|--------|
| 1 | Nomenclature collision (`tag_archetype_fit` per-(wallet,tag) vs per-(tag,archetype)) | Rename this deliverable to `wallet_tag_fit`. Aggregate stats keep `tag_archetype_*` namespace; rename §3.3 to `tag_archetype_cooccurrence`. | Table names should encode PK grain. |
| 2 | Concentration direction | Compute, persist raw value, weight = 0 in v1. Activate in v2 only after within-strata regression locks the sign. | Survivorship-vs-edge ambiguity unresolved; structural-NO research showed "obvious" priors are often backwards. |
| 3 | WR Bayesian prior | Tag-conditional Beta with `μ` = peer-mean `win_rate_held_only`, `k = max(5, median peer n_held_to_resolution)` | Per-tag baseline WR varies wildly; uniform 0.5 under-shrinks on biased tags. |
| 4 | Within-tag vs cross-tag | Within-tag percentile rank only. Documented in column comment, README, and §7.1 test. | No tag-edge-variance correction in this design; cross-tag is undefended. |
| 5 | Empirical weight-fitting | Deferred to v2; v1 ships equal-weight hand-set. | Labeled set (~48 positives, mostly accepts) is below the 100-decision/30-reject threshold for 5-component logistic regression. |
| 6 | Freshness | Daily batch, same cron as B1. | No sub-daily downstream consumer; consumer should never be upstream of B1's cadence. |
| EXTRA | Recency half-life | 90 days. | Matches Polymarket wallet turnover; documented free parameter. |
| EXTRA | Reporting floor | min_peers=10 to compute, but `confidence_flag='ok'` requires ≥30. Production consumers filter. | Percentile ranks below 30 peers are too noisy for decisions; >10 is fine for monitoring. |
| EXTRA | WR source field | `win_rate_held_only` (not `win_rate`) | Sell-side contamination in B1's `win_rate`; held-only is unbiased. |

## 10. Risks / known gaps

- **v2 fitting may stay blocked indefinitely** if `wallet_memberships` doesn't reach the 100/30 threshold. v1 hand-set is the production answer until then.
- **Aggregate WR/PnL is visible in the operator UI** at decision time (`/api/wallet-followable-leaderboard` per `static/app.js:2390+`). Per-tag PnL is NOT visible. So per-tag features add net-new information for v2 fitting — methodologically valid but power-limited by the small labeled set.
- **Tag taxonomy churn:** `market_tags` is gamma-assigned and can change. `computed_at` lets us detect staleness; daily recompute mitigates. Document any tag-rename events in brain memory.
- **NO-side / fractional-share dust:** inherited correctness from B1 (commit `09a5044`). Add a §7.1 test for a NO-only wallet on a tag.

## 11. Next after B2

- **C1** (`wallet_archetypes` table) composes `wallet_tag_fit` rows across tags + `wallet_archetype_extractor.py` envelope to produce a wallet-level archetype classification.
- **C3** (curation CLI) reads `wallet_tag_fit` filtered to `confidence_flag = 'ok'` to surface review-queue candidates ranked by tag × fit.
- **v2 empirical fitting** revisits §4 weights once labeled set crosses the 100/30 threshold.
