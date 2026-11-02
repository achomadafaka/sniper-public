# Flipper-Branch Tag-Grain Inference — Design (v1)

**Date:** 2026-05-15
**Status:** Phase 0 — design only. No code change. No DB writes. No live calls.
**Scope:** close Gap 1 from the 2026-05-15 category-truth audit so flipper-branch tag-grains can produce real `supported` / `inconclusive` / `refuted` verdicts under a flip-appropriate null model.
**Trigger:** `docs/research/wallet_intelligence_workflow/2026-05-15-category-truth-audit-eatpraylove-hwr22e4.md` — eatpraylove's actual edge ($1.5M+ on geopolitics conflict markets, WR 83-91%) is invisible to the current specialist gate because `_build_fh4_grain` returns `inference_not_run` whenever the available peer baseline is held-only win rate.

NOT A BUY OR SELL SIGNAL. Wallet behavior is evidence; this spec designs an inference surface, not a copy mechanism.

---

## 0. Doctrinal framing

The current specialist gate ties to the **held-analyst** branch. For wallets whose mechanism is mixed-mode or flipper-mode, the gate can produce two failure modes:

1. **False rejection.** The wallet's real category-local edge lives in pre-resolution exits (flip ROI), but those grains are stamped `inference_not_run`. The wallet looks like it has no specialist evidence even when concentrated category WR is 90% on n>1,800.

2. **False promotion.** The held-to-resolution remainder is a small subset, and tag mass on it can lock onto a category the wallet doesn't actually specialize in. eatpraylove's `nav/crypto` n=20 supported grain is exactly this — 2.5% of net PnL drives the machine label.

Both modes were observed in the audit. Closing the gap is a precondition for specialist_evidence to mean what its name implies.

### Non-goals

- Not a new hypothesis. FH4 already exists as a per-grain hypothesis with the right scope; this spec defines the **null model and baseline data** it has been suppressed without, since 2026-04-30 (commit comment on `_build_fh4_grain`).
- Not a copy mechanism. The output is `verdict ∈ {supported, inconclusive, refuted, underpowered, descriptive_only}` on `TagGrainResult`, consumed by the specialist consensus surface — same as held grains.
- Not a refactor of FH1–FH3, FH5–FH6. Their inference paths already exist and pass tests; FH4 alone is suppressed.
- Not a tag-taxonomy redesign. The grain enumeration (`enumerate_tag_grains`) and `leaf` / `nav` distinction stay.

---

## 1. Problem statement

`sniper/analysis/wallet_protocol/flipper.py:_build_fh4_grain` returns `verdict="inference_not_run"` for every flipper-branch tag grain. The reason is a unit mismatch:

- **FH4 docstring contract** (line 15 of `flipper.py`): `peer_baseline_fn(grain, tag, wallet) -> (mean_flip_roi, peer_count)`.
- **Available implementation** (`db.peer_baseline_for_grain` and its successors under `sniper/persistence/`): returns `(mean_held_win_rate, peer_count)`. The mean here is a binary-outcome rate, not a continuous flip-ROI mean.

`_build_fh4_grain` cannot tell, at call time, whether the baseline fn it received was the held-WR or flip-ROI variant. The conservative choice — explicitly chosen in the existing comment ("Bug 1: db.peer_baseline_for_grain returns held-only win_rate, not flip ROI. Suppress FH4 until a flip-specific peer aggregate exists.") — is to suppress all FH4 verdicts.

This is pinned by `tests/test_wallet_protocol_fh4_unit_mismatch.py`, which verifies that under a held-WR baseline FH4 returns `inference_not_run` even at high peer counts. The pin must survive any change made by this design: the *wrong baseline* must continue producing `inference_not_run`. What changes is that the **right baseline** finally exists.

### What "edge in the flipper branch" means

For a flip (BUY+SELL pair, FIFO-matched):

- `flip_roi = (sell_price - buy_price) / buy_price` (per-flip continuous, can be negative)
- `flip_pnl = (sell_price - buy_price) × size` (per-flip dollars)
- `hold_duration_seconds` (per-flip)

A category-local "specialist" claim in the flipper branch reduces to: *this wallet's mean flip ROI in category C exceeds the peer mean flip ROI in category C, statistically*. That requires a peer baseline of mean flip ROI per (grain, tag) — which does not currently exist as a queryable surface.

---

## 2. Current state inventory

What exists, what doesn't, what blocks.

### 2.1 Code that exists and stays

| Symbol | File | Role |
|---|---|---|
| `Flip` dataclass | `flip_reconstruction.py` | FIFO-matched flip pair; has `flip_roi`, `buy_notional`, `condition_id`, etc. |
| `enumerate_tag_grains(flip_dicts, leafs_by_cid, nav_lookup)` | (helper) | Maps each flip to its leaf and nav tag membership. Already used in `run_flipper_branch`. |
| `_build_fh4_grain(grain, tag, indices, flips, peer_baseline_fn, n_iter, seed, wallet)` | `flipper.py:293` | Constructs `TagGrainResult` per grain. **Currently always returns `inference_not_run`.** This is where the new logic lands. |
| `_attach_flipper_grain_financials(result, indices, flips)` | `flipper.py` (called from `_build_fh4_grain`) | Edge Surface v1 financials — display-only, attached regardless. **Stays.** |
| `PeerBaselineFn = Callable[[str, str, str], tuple[float, int] | None]` | `flipper.py:102` | Existing type. **Reused** under §3.1 with explicit unit naming. |
| `BH FDR pooling` for `{FH1, FH2, FH3, FH4 per-grain pooled, FH6}` | `run_flipper_branch` orchestrator | Already expects FH4 grain results to enter the BH pool. **No change required.** |
| `classify_peer_confidence(peer_count)` | (helper used in `_build_fh4_grain`) | Maps peer_count → `ok` / `low_population` / etc. **Stays.** |

### 2.2 Data that does not exist

The blocker is a *queryable flip-ROI peer aggregate per (grain, tag)*. There is no table, view, or function on either SQLite or Supabase that returns `mean(flip_roi)` aggregated over peers per category.

### 2.3 Existing held-side mirror (reference only — NOT to be reused)

`db.peer_baseline_for_grain(grain, tag, wallet) → (mean_held_win_rate, peer_count)` is the held-analyst contract. Same signature shape, different *unit and population*. The held population is "wallets that held to resolution in this category"; the flipper population must be "wallets that flipped (BUY+SELL pre-resolution) in this category."

These two populations are not identical. Some wallets only hold; some only flip; many do both. The peer set for FH4 must be **the subset of wallets that have ≥M flips in (grain, tag)** for some threshold M (see §3.2 design choices).

---

## 3. Design

### 3.1 Two-baseline-fn architecture (Recommendation)

Introduce a second baseline fn parameter on `run_flipper_branch` and `_build_fh4_grain`, type-distinct from the existing held-side fn:

```python
# In sniper/analysis/wallet_protocol/flipper.py
FlipRoiBaselineFn = Callable[[str, str, str], tuple[float, int] | None]
# Returns (mean_flip_roi, peer_count) or None if no peer aggregate available.

def run_flipper_branch(
    flips: Sequence[Flip],
    *,
    wallet: str = "",
    # ... existing args ...
    peer_baseline_fn: PeerBaselineFn | None = None,        # held-WR baseline — kept for any caller still passing it; ignored by FH4
    flip_roi_baseline_fn: FlipRoiBaselineFn | None = None, # NEW — used by FH4
    # ... rest ...
)
```

`_build_fh4_grain` reads `flip_roi_baseline_fn` (not `peer_baseline_fn`). If `flip_roi_baseline_fn is None`, returns `inference_not_run`. If it returns `None` for the (grain, tag) lookup, returns `inference_not_run`. Otherwise runs the bootstrap defined in §3.3.

**Why two fns rather than a tagged unit:** the held-side fn already exists, is consumed by `held_analyst.py`, and has its own pinning tests. A unit tag would force a contract change on the held side for no held-side benefit. Two fns keep the change additive.

**Backward-compat path for the existing pinning tests:** the existing tests in `test_wallet_protocol_fh4_unit_mismatch.py` pass `_wr_baseline_fn` as the `peer_baseline_fn` positional — that arg now gets *ignored* by `_build_fh4_grain` (it only reads `flip_roi_baseline_fn`). With no `flip_roi_baseline_fn`, the path still returns `inference_not_run`, satisfying the existing assertions. The tests should be amended to reflect the new contract explicitly: "with no flip-ROI baseline, FH4 returns `inference_not_run`" — a stronger pin than the unit-mismatch framing, surviving any future refactor.

### 3.2 Peer-set definition (the substantive design call)

**Population:** for grain (`leaf` | `nav`) and tag T, the peer set is the universe of wallets W such that:

- W has been classified by the analyzer at least once (so we have a flip dataset for W), AND
- W has executed ≥ `MIN_PEER_FLIPS_IN_GRAIN` flips in (grain, T), AND
- W ≠ the wallet under analysis (self-contamination guard — same as held-side per `held_analyst.py:14`)

**Recommended threshold:** `MIN_PEER_FLIPS_IN_GRAIN = 30`. Justification: smaller-than-30 per-peer flip samples produce peer-mean estimates that are themselves noisy; pooling across peers each contributing <30 flips just propagates noise upward. 30 is the standard small-sample threshold for normal-approximation arguments. This is a tunable knob, locked in `web_settings.py` rather than hard-coded.

**Aggregation choice — per-peer-mean vs per-flip-pool (the load-bearing decision):**

| Option | What it computes | Pros | Cons |
|---|---|---|---|
| A: per-peer mean | For each peer, compute mean(flip_roi) in (grain, T); then mean across peers. | Each peer counts equally regardless of activity. Robust to one heavy flipper dominating. | Throws away information about flip-count distribution among peers. |
| B: per-flip pool | Pool all peer flips in (grain, T); compute mean(flip_roi) over the pool. | Uses every flip, no information loss. | One heavy peer with 10K flips can swamp the baseline. |
| C: weighted (per-peer mean, weighted by sqrt(peer_flip_count)) | Hybrid — middleweight against heavy peers without ignoring sample-size signal. | Compromise of A and B. | One more knob; less defensible. |

**Recommendation: A (per-peer mean).** The specialist-gate question is "does *this wallet's* flip-ROI mean stand out among *peers*." Weighting peers by their own activity counts mixes two distinct signals. A is the cleanest answer to the specialist question and matches the held-side convention (each peer's win-rate enters the baseline equally).

The implementation should record `peer_count` (number of peers contributing — not peer-flip-count) on the `TagGrainResult` to keep the existing `classify_peer_confidence` signature intact.

### 3.3 Null model and inference path

For wallet W with `n` flips in (grain, T) and observed mean `m_W`, against peer baseline `(m_peer, peer_count)`:

1. **Effect size:** `effect = m_W − m_peer` (continuous, signed; in flip-ROI units, not pp).
2. **Bootstrap CI on `m_W`:** pair-resample W's flips in (grain, T) with replacement, B=2000 iterations, seed=1 (same conventions as FH1). Compute 95% CI for `m_W − m_peer` by subtracting the constant `m_peer` from each bootstrap mean.
3. **Empirical p-value:** under H0 `m_W = m_peer`, center W's flips at `m_peer` and resample; p = fraction of resamples with `|mean − m_peer| ≥ |m_W − m_peer|`. This matches the empirical-p convention introduced by M1 (`finding/wallet-protocol/m1-empirical-bootstrap-p-2026-04-26`).
4. **Verdict mapping** (mirrors held-side `_verdict_after_pool` semantics):
   - `supported` if (BH-rejected) AND (CI excludes 0 in the direction of `m_W − m_peer`).
   - `refuted` if (BH-rejected) AND (CI excludes 0 in the opposite direction).
   - `inconclusive` otherwise.
   - `underpowered` if `n < MIN_FLIPS_FOR_INFERENCE` (recommend 30 — same threshold) OR `peer_count < MIN_PEERS_FOR_INFERENCE` (recommend 10 — held-side convention).
5. **Pool entry:** FH4 grain results enter the BH pool alongside FH1/FH2/FH3/FH6 per the existing `run_flipper_branch` BH logic.

### 3.4 What `TagGrainResult` fields gain meaning

Currently `_build_fh4_grain` leaves these as `None` because no inference runs:

| Field | New meaning under §3.3 |
|---|---|
| `metric_value` | `m_W` — wallet's mean flip ROI in (grain, tag) |
| `pop_baseline` | `m_peer` — peer mean flip ROI |
| `excess_pp` | **Renamed semantically to `excess` (continuous, not pp).** Or kept as `excess_pp` for schema stability — pick at implementation time. Concretely the value is `(m_W − m_peer)`. Unit-naming is a doc concern; the schema field stays the same. |
| `ci_lower` / `ci_upper` | 95% bootstrap CI on `m_W − m_peer` |
| `p_value_bh_adjusted` | BH-adjusted empirical p from §3.3.3 |
| `verdict` | One of `supported` / `refuted` / `inconclusive` / `underpowered` per §3.3.4 |
| `underpowered_reason` | New typed value `no_flip_roi_peer_baseline` when `flip_roi_baseline_fn is None` OR returns None; or `flip_count_below_floor`; or `peer_count_below_floor`. |
| `grain_total_pnl`, `grain_roi`, `grain_win_rate`, `grain_avg_win`, `grain_avg_loss`, `grain_max_realized_loss` | **Unchanged.** Edge Surface v1 financials remain attached display-only regardless of verdict. |

### 3.5 Where the baseline fn comes from at call site

`run_wallet_protocol.py` and the replay harness build the baseline fns. The new fn must:

- Be safe for read-only replay against a fixture (return `None` when no peer aggregate is available).
- Be safe for live protocol runs (read from the actual peer aggregate store).

A reasonable initial implementation reads from a new Supabase view (or computed table) `peer_flip_roi_aggregates` keyed by `(grain, tag)`, populated from the `wallet_analysis_results` and the wallet ledger surface — but **defining that aggregate's source schema is out of scope for this spec** and belongs to the implementation plan. Phase 0 ships only the design.

If the aggregate doesn't exist yet, the fn returns `None`, and every flipper grain stays `inference_not_run` — same observable behavior as today. No regression.

---

## 4. Test pinning

### 4.1 Existing tests — amend, do not delete

`tests/test_wallet_protocol_fh4_unit_mismatch.py` has three tests:

1. `test_fh4_grain_returns_inference_not_run_under_held_wr_baseline` — passes a held-WR baseline as `peer_baseline_fn` and asserts `inference_not_run`.
2. `test_fh4_grain_inference_not_run_even_when_peer_count_is_high` — same with high peer_count.
3. `test_fh4_grain_inference_not_run_with_no_baseline_fn` — passes `None` baseline.

Under the two-fn architecture (§3.1), `_build_fh4_grain` ignores `peer_baseline_fn` entirely. The existing tests pass with no signature change. Recommend amending the test docstrings to reflect the new semantics: "the *wrong-unit* baseline does not enable FH4 — only the dedicated flip-ROI baseline does."

### 4.2 New tests required

Add `tests/test_wallet_protocol_fh4_flip_roi_inference.py`:

| Test | Scenario | Expected verdict |
|---|---|---|
| `test_fh4_supported_when_wallet_mean_well_above_peer_mean` | `n=50`, `m_W=0.20`, baseline `(m_peer=0.05, peer_count=20)`, tight bootstrap | `supported` |
| `test_fh4_refuted_when_wallet_mean_well_below_peer_mean` | `n=50`, `m_W=-0.10`, baseline `(0.05, 20)` | `refuted` |
| `test_fh4_inconclusive_when_ci_crosses_zero` | `n=50`, `m_W` close to `m_peer`, wide variance | `inconclusive` |
| `test_fh4_underpowered_when_n_below_floor` | `n=20`, baseline ok | `underpowered`, reason `flip_count_below_floor` |
| `test_fh4_underpowered_when_peer_count_below_floor` | `n=50`, baseline `(0.05, 5)` | `underpowered`, reason `peer_count_below_floor` |
| `test_fh4_inference_not_run_when_no_flip_roi_baseline` | `n=50`, `flip_roi_baseline_fn=None` | `inference_not_run`, reason `no_flip_roi_peer_baseline` |
| `test_fh4_self_excluded_from_peer_set` | Baseline fn receives `wallet` arg and returns `None` if W is the only peer | `inference_not_run`, reason `no_flip_roi_peer_baseline` (caller's responsibility — fn contract documented) |
| `test_fh4_grain_financials_attached_regardless_of_verdict` | Any of the above | `grain_total_pnl` / `grain_roi` / `grain_win_rate` populated regardless |

### 4.3 Calibration impact

After Phase 1 implementation lands, the v3 calibration fixtures may produce *different* verdicts for at least eatpraylove and hwr 22e4 — that's the point. The v3 labels.yaml will need a v4 re-baseline. The calibration loop is the natural place to confirm the new path does not regress wallets where the held-branch is already supported (SwissMiss `leaf/geopolitics` — held-only specialist; her flipper-branch fraction is 11%, so adding flip inference should not displace her held verdict).

Re-baseline must produce *justified diffs only*: any wallet whose verdict moves should be traceable to a specific new flip-ROI peer-baseline result on a specific grain. Unmotivated moves are regressions.

---

## 5. Phased implementation

| Phase | Deliverable | Risk |
|---|---|---|
| 0 | This spec | none |
| 1 | Define peer aggregate source schema + Supabase view / table contract. **Separate design doc.** | low — design only |
| 2 | Implement `flip_roi_baseline_fn` in flipper.py + new `FlipRoiBaselineFn` type alias. Wire through `run_flipper_branch`. **No FH4 logic change yet — just plumbing.** All existing tests still pass; new fn always returns `None` because the source aggregate doesn't exist yet. | low — additive only |
| 3 | Build the peer flip-ROI aggregate (the actual data). Backfill from existing wallet ledger / analysis results. | medium — DB write surface |
| 4 | Replace `_build_fh4_grain` body: read `flip_roi_baseline_fn`, run bootstrap (§3.3), return real verdict. Amend `test_wallet_protocol_fh4_unit_mismatch.py` docstrings; add `test_wallet_protocol_fh4_flip_roi_inference.py`. | medium — verdict-producing code change, calibration impact |
| 5 | V4 calibration re-baseline. Investigate any moved verdicts; only justified diffs accepted. | medium — same shape as v3 rebaseline (PR #38) |
| 6 | Specialist registry consumers (Specialist Consensus Surface) start reading flipper-branch supported grains alongside held-branch supported grains. **Separate spec change** for the consensus-surface side. | low — additive |

Each phase has its own go/no-go.

---

## 6. Risks

1. **Peer aggregate becomes a privacy / leakage surface.** Aggregating per-(grain, tag) ROI across all analyzed wallets could leak individual wallets' positioning if the peer set is small. Mitigation: enforce `MIN_PEERS_FOR_INFERENCE = 10` at the lookup site, return `None` below that. Same threshold as held-side.

2. **Heavy-tailed flip-ROI distribution.** A small number of wallets with extreme positive ROI on a niche tag (Iran $470K on n=1,823 at WR=91% is the canonical example) can make the peer mean look high enough to falsely refute lesser specialists. Mitigation: per-peer-mean aggregation (§3.2 option A) — each peer counts once, regardless of how extreme their concentration is.

3. **Backward-compatibility on the held-side fn.** Callers that still pass `peer_baseline_fn` (held WR) to `run_flipper_branch` continue to do so; FH4 just ignores it now. The held-side branch is unaffected. Risk is zero if §3.1 is implemented additively.

4. **Calibration churn.** Phase 4 will produce verdict changes — by design. Without the v4 re-baseline (Phase 5), the calibration test suite breaks. Phases 4 and 5 must land together (or 5 immediately after 4 within the same PR sequence).

5. **Re-anchoring the specialist registry.** Once eatpraylove flips to `supported` on `leaf/geopolitics` / `leaf/iran`, the prior thesis-card classification (`research_only`) and the prior Supabase finding (`finding/wallet-protocol/capability-surfaces-audit-2026-05-02`) need re-anchoring. Standard supersession via KSG Phase 4+ (when it exists) — interim, manual UPDATE to the `memories` row.

---

## 7. Open questions

These are explicitly parked for Phase 1+ design, not for Phase 0.

1. **Source of the peer flip-ROI aggregate.** Is it a Supabase materialized view derived from `wallet_analysis_results`, a query against the wallet ledger, or a new computed table refreshed by a cron? Decision deferred to the Phase 1 schema-source spec.

2. **Recency window for peer aggregation.** Should the peer mean use all-time flip ROI, or a rolling window (e.g. last 90 days)? Held-side currently uses all-time; consistency argues for the same here, but flipper edges may decay faster, arguing for rolling. Phase 1 question.

3. **Should FH4 fold into a single per-wallet held+flip surface for mixed-mode wallets?** eatpraylove has held-branch grains AND would have flipper-branch grains after this lands. Two parallel specialist claims on the same wallet might confuse the consensus surface. Possible answer: keep them separate at the grain level; let the consensus surface decide whether to merge. Out of scope for Phase 0.

4. **Does the `excess_pp` field need renaming to `excess` (unit-neutral)?** Held side uses excess in percentage-points (win-rate units). Flipper side will use it in flip-ROI units (continuous, can be 0.05, 0.30, etc., not 5pp or 30pp). Cosmetic; defer to implementation review.

---

## 8. Cross-references

- `docs/research/wallet_intelligence_workflow/2026-05-15-category-truth-audit-eatpraylove-hwr22e4.md` — the audit that surfaced Gap 1
- `docs/research/wallet_intelligence_workflow/2026-05-15-phase1-manual-thesis-cards.md` — the prior thesis cards
- `docs/superpowers/specs/2026-05-15-wallet-intelligence-workflow-phase1-specialist-consensus.md` — the consensus surface this gate feeds
- `sniper/analysis/wallet_protocol/flipper.py:293` — `_build_fh4_grain`, current suppression site
- `sniper/analysis/wallet_protocol/flipper.py:102` — `PeerBaselineFn` type alias
- `sniper/analysis/wallet_protocol/held_analyst.py:14` — self-contamination guard (peer fn must exclude wallet under analysis)
- `tests/test_wallet_protocol_fh4_unit_mismatch.py` — current pin; must continue passing after Phase 4
- `finding/wallet-protocol/m1-empirical-bootstrap-p-2026-04-26` (Supabase) — empirical-p convention referenced in §3.3.3
