# Analyzer Full-Read Engine — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every wallet that enters the system gets one coherent "full read" that distinguishes a fresh information-advantage-shaped account from an established edge wallet, names what the edge is and where it lives, and never overclaims.

**Architecture:** Two engines (existing history-rich thesis + rebuilt fresh-anomaly v2) fused by a pure deterministic `synthesize_read` into `primary_verdict + operational_route + evidence_profile`. A universal analyzer-owned queue + worker funnels every wallet through both engines and appends an immutable read. Fresh anomaly uses calibrated Poisson-binomial surprise over independent event families, an anytime-valid e-process, conservative online-FDR, empirical-Bayes shrinkage, and 5 evidence channels (incl. on-chain funding graph + CLV).

**Tech Stack:** Python 3, SQLite (WAL), pytest. Reuses `winning_wallet_thesis` package, `wallet_protocol/price_history_adapter.py` (CLOB `/prices-history`), `conversions.py` Polygon JSON-RPC plumbing. No new third-party deps.

**Spec:** `docs/superpowers/specs/2026-06-19-analyzer-full-read-engine-design.md`

## Global Constraints

(Every task implicitly includes these. Verbatim from spec / project doctrine.)

- **Never the word "insider"** in any output, identifier, or comment-facing string. Fresh bucket = `fresh_high_surprise`; framing = "fresh account with unusually concentrated, high-surprise results … warrants forward monitoring".
- **E-values, not p-values, are the decision statistic** for the fresh flag (anytime-valid; we re-analyze continuously). Fixed-sample Poisson-binomial p-value is display-only, labelled as such.
- **Immutable reads:** never UPDATE a `wallet_analysis_reads` row; re-analysis appends with `supersedes_read_id`.
- **No legacy reuse** (`user_analysis`, `insider_score`, low-buy, `analysis_runs`) and **no downstream routing eligibility fields** anywhere in the analyzer. `wallet_signal_profiles` is NOT written here.
- **Becker DB is off the critical path.** v1 baseline probability = market price at entry (`entry_vwap`).
- **Anti-hardcoding:** every threshold/constant lives in a `DEFAULTS`/`cfg` dict, overridable (AGENTS.md §Anti-Hardcoding).
- **Fail loud:** geo-block/auth errors raise a distinct surfaced error class; a wallet is only `done` when a canonical `WalletRead` exists (even if `insufficient_evidence`). Never a silent skip, never a false `insufficient_evidence`.
- **Rate limit ≤ 8 RPS** shared across all outbound Polymarket/CLOB/RPC calls; bounded worker concurrency.
- **No copy advice / expected PnL / sizing** in any read field.
- **Pure cores stay pure:** synthesis + fresh-anomaly core classify injected data; they never fetch. Feeds are injected closures.
- Run `python3 scripts/validate_schema_refs.py` and `python3 scripts/brain_index.py generate` after schema changes; commit the regenerated index.

## File Structure

| File | Responsibility | New? |
|---|---|---|
| `sniper/analysis/winning_wallet_thesis/synthesis.py` | `WalletRead` + `synthesize_read` (pure): 7 buckets, precedence, route | new |
| `sniper/analysis/winning_wallet_thesis/stats.py` | pure stats primitives: Poisson-binomial, e-process, EB-shrinkage | new |
| `sniper/analysis/winning_wallet_thesis/fresh_anomaly.py` | fresh engine v2 (pure): clustering, channels A/C/D, alt-explanation, assembles fresh evidence | new (supersedes `fresh_specialist.py`) |
| `sniper/analysis/winning_wallet_thesis/online_fdr.py` | e-value online-FDR allocator + persistent state | new |
| `sniper/analysis/winning_wallet_thesis/clv.py` | Channel B: closing-line value via price-history adapter | new |
| `sniper/analysis/winning_wallet_thesis/funding_graph.py` | Channel E: USDC funder graph + synchronized-entry linkage via Polygon RPC | new |
| `sniper/persistence/db_wallet_reads.py` | `wallet_analysis_reads` + `wallet_analysis_latest` view; append/read | new |
| `sniper/persistence/db_wallet_analysis_queue.py` | `wallet_analysis_queue`: enqueue, atomic lease, heartbeat, complete/fail | new |
| `sniper/analysis/winning_wallet_thesis/orchestrator.py` | one wallet: thesis + fresh + archetype → synthesize → persist | new |
| `scripts/analyze_wallets_worker.py` | the worker loop (lease → orchestrate → done/fail), rate-limited | new |
| `tests/test_analyzer_synthesis.py`, `tests/test_fresh_anomaly_stats.py`, `tests/test_online_fdr.py`, `tests/test_analyzer_feeds.py`, `tests/test_wallet_reads_persistence.py`, `tests/test_wallet_analysis_queue.py`, `tests/test_analyzer_orchestrator.py` | acceptance suite (§10 cases) | new |

`fresh_specialist.py` is retired in P2 (kept as a thin shim re-exporting nothing public, or deleted with its test) — decided in Task 2.6.

## Pinned statistical formulas (the correctness lock)

- **Poisson-binomial PMF** of `S = Σ Bernoulli(p_i)` by DP: `dp=[1]; for p in ps: dp = [dp[k]*(1-p) + (dp[k-1]*p if k else 0) for k in range(len(dp)+1)]`. Survival `P(S≥obs)=sum(dp[obs:])`. O(n²); n = #families (small).
- **E-process (one-sided "wins > null"):** per trial increment `m(λ)=1+λ·(y−p)`. Grid `Λ` of λ in `[0, λmax]`, `λmax = 0.99/max(p_f)` (keeps `m≥0`). `e_λ = Π m_λ`; mixture `E = mean(e_λ over Λ)`. `anytime_p = min(1, 1/E)`. Default grid: 12 points `linspace(0.05·λmax, λmax, 12)`.
- **Online-FDR (conservative e-value bound, v1):** flag-eligible iff `E_t ≥ 1/(α·γ_t)`, `γ_t = 6/(π²·t²)` (Σγ≤1), `α=0.10`. Bounds expected false discoveries across the whole scan ≤ α (Markov + `E[E_t|H0]≤1`). Conservative (precision over recall — intended). `t` = the wallet's monotone test index from persistent allocator state. (Verify against Wang–Ramdas 2022 e-LOND in review; the §10 FDR test is the lock.)
- **EB-shrinkage (display magnitude only, not the decision):** `shrunk_oe = (W + k·r0)/(E0 + k)` where `W=Σy_f`, `E0=Σp_f`, `r0=1.0` (population obs/exp ≈ calibrated), `k=5.0` pseudo-count (tunable; later fit from survivorship cohorts). Pulls thin-N toward 1.

---

### Task 1: Stats primitives (`stats.py`, pure)

**Files:**
- Create: `sniper/analysis/winning_wallet_thesis/stats.py`
- Test: `tests/test_fresh_anomaly_stats.py`

**Interfaces:**
- Produces:
  - `poisson_binomial_sf(ps: list[float], obs: int) -> float` — `P(S ≥ obs)`.
  - `eprocess_value(trials: list[tuple[float,int]], grid_n: int = 12) -> dict` → `{e_value, anytime_p, lambda_max}` for trials `[(p_f, y_f)]`.
  - `eb_shrunk_oe(W: float, E0: float, k: float = 5.0, r0: float = 1.0) -> float`.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_fresh_anomaly_stats.py
import math
from sniper.analysis.winning_wallet_thesis import stats as ST

def test_poisson_binomial_12_at_020_three_wins_is_ordinary():
    sf = ST.poisson_binomial_sf([0.2]*12, 3)
    assert 0.40 < sf < 0.48          # ~0.44: ordinary variance, NOT surprising

def test_poisson_binomial_three_penny_wins_is_extreme():
    sf = ST.poisson_binomial_sf([0.01, 0.01, 0.01], 3)
    assert sf < 1e-5                 # 0.01^3 = 1e-6

def test_eprocess_high_for_clear_outperformance():
    trials = [(0.05, 1)]*8           # 8 wins each priced 5% -> huge e-value
    out = ST.eprocess_value(trials)
    assert out["e_value"] > 20 and out["anytime_p"] < 0.05

def test_eprocess_near_one_under_null():
    # wins land exactly at expectation: e-value should hover ~1, not significant
    trials = [(0.5, 1), (0.5, 0), (0.5, 1), (0.5, 0)]
    out = ST.eprocess_value(trials)
    assert out["e_value"] < 5 and out["anytime_p"] > 0.2

def test_eprocess_increments_nonnegative_at_lambda_max():
    # m = 1 + lambda*(y-p) must stay >= 0 for a loss at the highest priced trial
    out = ST.eprocess_value([(0.9, 0), (0.9, 0)])
    assert out["e_value"] >= 0

def test_eb_shrinks_thin_extreme_toward_one():
    raw = 3 / 0.3                     # observed/expected = 10x on tiny N
    shrunk = ST.eb_shrunk_oe(W=3, E0=0.3, k=5.0)
    assert 1.0 < shrunk < raw and shrunk < 2.5
```

- [ ] **Step 2: Run, verify fail** — `pytest tests/test_fresh_anomaly_stats.py -q` → FAIL (module missing).

- [ ] **Step 3: Implement `stats.py`**

```python
"""Pure statistical primitives for the fresh-anomaly engine. No I/O, no fetching."""
from __future__ import annotations
import math

def poisson_binomial_sf(ps: list[float], obs: int) -> float:
    """P(S >= obs) where S = sum of independent Bernoulli(p_i). DP over the PMF."""
    ps = [min(1.0, max(0.0, float(p))) for p in ps]
    if obs <= 0:
        return 1.0
    if obs > len(ps):
        return 0.0
    dp = [1.0]
    for p in ps:
        nxt = [0.0] * (len(dp) + 1)
        for k, v in enumerate(dp):
            nxt[k] += v * (1.0 - p)
            nxt[k + 1] += v * p
        dp = nxt
    return min(1.0, max(0.0, sum(dp[obs:])))

def eprocess_value(trials: list[tuple[float, int]], grid_n: int = 12) -> dict:
    """Anytime-valid e-value for the one-sided alternative 'wins more than null'.
    trials = [(p_f, y_f)]. m(lambda)=1+lambda*(y-p); mixture over a lambda grid."""
    pys = [(min(1.0 - 1e-9, max(1e-9, float(p))), int(y)) for p, y in trials]
    if not pys:
        return {"e_value": 1.0, "anytime_p": 1.0, "lambda_max": 0.0}
    pmax = max(p for p, _ in pys)
    lam_max = 0.99 / pmax
    grid = [lam_max * (0.05 + (1.0 - 0.05) * i / (grid_n - 1)) for i in range(grid_n)] if grid_n > 1 else [lam_max]
    es = []
    for lam in grid:
        prod = 1.0
        for p, y in pys:
            prod *= 1.0 + lam * (y - p)
            if prod <= 0.0:
                prod = 0.0
                break
        es.append(prod)
    e = sum(es) / len(es)
    return {"e_value": e, "anytime_p": min(1.0, 1.0 / e) if e > 0 else 1.0, "lambda_max": lam_max}

def eb_shrunk_oe(W: float, E0: float, k: float = 5.0, r0: float = 1.0) -> float:
    """Empirical-Bayes shrunk observed/expected ratio; pulls thin-N toward r0(=1)."""
    if E0 + k <= 0:
        return r0
    return (W + k * r0) / (E0 + k)
```

- [ ] **Step 4: Run, verify pass** — `pytest tests/test_fresh_anomaly_stats.py -q` → PASS.

- [ ] **Step 5: Commit** — `git add sniper/analysis/winning_wallet_thesis/stats.py tests/test_fresh_anomaly_stats.py && git commit -m "feat(analyzer): pure stats primitives (Poisson-binomial, e-process, EB-shrinkage)"`

---

### Task 2: Fresh-anomaly engine v2 — clustering + channels A/C/D + alt-explanation (`fresh_anomaly.py`, pure)

**Files:**
- Create: `sniper/analysis/winning_wallet_thesis/fresh_anomaly.py`
- Test: `tests/test_fresh_anomaly_engine.py`
- Reference for reuse: `fresh_specialist.py` (excludes + `_area` logic), `types.py` (`PositionBook`, `PositionEpisode`).

**Interfaces:**
- Consumes: `stats.poisson_binomial_sf`, `stats.eprocess_value`, `stats.eb_shrunk_oe`; `PositionBook`.
- Produces: `assess_fresh_anomaly(book, *, clv_fn=None, funding=None, now_ts=None, cfg=None) -> FreshAssessment` where `FreshAssessment` is a dataclass with: `assessable: bool`, `history_thin: bool`, `n_independent_events: int`, `effective_sample_size: float`, `e_value: float`, `anytime_p: float`, `obs_over_expected: float`, `fixed_p_value: float`, `shrunk_oe: float`, `ex_ante_level: str`, `ex_post_level: str`, `coordination_level: str`, `channels: dict[str,dict]` (A..E → `{level, reason}`), `channels_strong: int`, `alt_explanation: str|None`, `excludes: str|None`, `reasons: list[str]`, `data_gaps: list[str]`, `sample_warning: str`.

**Key logic to implement (pin in code):**
- Reuse hard excludes from `fresh_specialist.py` (paired-outcome structural; churn/flip) verbatim → set `excludes` and return early (`assessable=False` for the surprise math, but still report ex-ante channels where meaningful).
- **Event-family clustering:** group `resolved_directional()` by `event_slug` (fallback `condition_id`); per family `p_f = Σ(buy_cost·entry_vwap)/Σbuy_cost`, `y_f = 1 if Σ(buy_cost·won)/Σbuy_cost ≥ 0.5 else 0`. `n_independent_events=len(families)`; `effective_sample_size = (Σw)²/Σ(w²)` (Kish) with `w=family exposure`.
- **Channel A** (ex-post): `eprocess_value(trials)`, `fixed_p_value=poisson_binomial_sf([p_f],Σy_f)`, `shrunk_oe`. Level: strong if `e_value ≥ 20`, moderate if `≥ 5`, weak if `≥ 2`, else none. (FDR gate applied later in synthesis/orchestrator, not here.)
- **Channel C** (ex-ante): top-area share + HHI (reuse `_area`). strong ≥0.7 share, moderate ≥0.5.
- **Channel D** (ex-ante): max bet ≥ `very_large_bet_usdc` strong; ≥ `large_bet_usdc` moderate. `data_gap` "no market-depth feed — size-vs-depth not evaluated".
- **Channel B** (ex-ante): if `clv_fn` given, exposure-weighted fraction of bets that beat the closing line; strong ≥0.7, moderate ≥0.55; else `data_gap` "no CLV feed".
- **Channel E** (ex-ante): from injected `funding` dict (`{linked_wallets, synchronized_entries, funder_age_days}`); strong if shared-funder AND synchronized entries; else proportionate; missing → `data_gap`.
- **alt_explanation:** `public_truth_stale` if any large family's entry ts > its `resolution_ts`/closed time (needs episode `first_buy_ts` vs `resolution_ts`); `single_event` if `n_independent_events ≤ 1`. Set and suppress strong-surprise when present.
- `channels_strong` = count of channels at level "strong". `ex_post_level`=Channel A level; `ex_ante_level`=max(B,C,D); `coordination_level`=Channel E level.
- `sample_warning` = the existing `_SAMPLE_WARNING` text. Never "insider".

- [ ] **Step 1: Write failing tests** (cases 1–3, 9, 10 + excludes)

```python
# tests/test_fresh_anomaly_engine.py  (excerpt — full cases in suite)
from sniper.analysis.winning_wallet_thesis import fresh_anomaly as FA
from tests.helpers_thesis import make_book   # tiny builder over PositionEpisode

def test_12_bets_three_cheap_wins_not_strong():           # case 1
    book = make_book([dict(area="nba", entry=0.2, cost=600, won=w, slug=f"g{i}")
                      for i, w in enumerate([1,1,1,0,0,0,0,0,0,0,0,0])])
    a = FA.assess_fresh_anomaly(book)
    assert a.channels["A"]["level"] in ("none", "weak")
    assert a.ex_post_level in ("none", "weak")

def test_three_independent_penny_wins_strong():           # case 2
    book = make_book([dict(area="pol", entry=0.01, cost=8000, won=1, slug=f"e{i}") for i in range(3)])
    a = FA.assess_fresh_anomaly(book)
    assert a.channels["A"]["level"] == "strong"

def test_three_cheap_wins_same_event_collapse():          # case 3
    book = make_book([dict(area="pol", entry=0.2, cost=8000, won=1, slug="same") for _ in range(3)])
    a = FA.assess_fresh_anomaly(book)
    assert a.n_independent_events == 1
    assert a.alt_explanation == "single_event"

def test_public_truth_stale_suppresses_flag():            # case 9
    book = make_book([dict(area="pol", entry=0.1, cost=9000, won=1, slug="x",
                           first_buy_ts=1000, resolution_ts=500)])
    a = FA.assess_fresh_anomaly(book)
    assert a.alt_explanation == "public_truth_stale"

def test_structural_excluded():
    book = make_book([dict(area="x", entry=0.5, cost=5000, won=1, slug="m", cid="c1", oidx=0),
                      dict(area="x", entry=0.5, cost=5000, won=0, slug="m", cid="c1", oidx=1)])
    a = FA.assess_fresh_anomaly(book)
    assert a.excludes == "structural"
```

- [ ] **Step 2: Add `tests/helpers_thesis.py`** — a `make_book(rows) -> PositionBook` builder constructing `PositionEpisode`s with the fields used (entry via buy_cost/buy_shares, won, resolved=True, condition_id, outcome_index, event_slug, category, first_buy_ts, resolution_ts). Run tests → FAIL.

- [ ] **Step 3: Implement `fresh_anomaly.py`** per the key-logic spec above (excludes reused from `fresh_specialist.py`; clustering; channels; alt-explanation; `FreshAssessment` dataclass).

- [ ] **Step 4: Run → PASS.** `pytest tests/test_fresh_anomaly_engine.py -q`

- [ ] **Step 5: Retire `fresh_specialist.py`** — delete it and `tests/test_fresh_specialist.py` (superseded); grep to confirm no production import (`grep -rn fresh_specialist sniper/ scripts/`). Run `pytest tests/ -k fresh -q`.

- [ ] **Step 6: Commit** — `git add -A && git commit -m "feat(analyzer): fresh-anomaly engine v2 (Poisson-binomial surprise, event clustering, channels A/C/D, alt-explanation); retire fresh_specialist"`

---

### Task 3: Online-FDR allocator (`online_fdr.py`)

**Files:**
- Create: `sniper/analysis/winning_wallet_thesis/online_fdr.py`
- Test: `tests/test_online_fdr.py`

**Interfaces:**
- Produces: `fdr_threshold(t: int, alpha: float = 0.10) -> float` → `1/(alpha·γ_t)`, `γ_t=6/(π²t²)`; `is_flag_eligible(e_value: float, t: int, alpha: float = 0.10) -> bool`.
- Persistent state lives in the reads/queue layer (the wallet's monotone test index `t`); the allocator itself is a pure function of `(e_value, t, alpha)`.

- [ ] **Step 1: Failing tests** (cases 11–12)

```python
# tests/test_online_fdr.py
import random
from sniper.analysis.winning_wallet_thesis import online_fdr as F
from sniper.analysis.winning_wallet_thesis import stats as ST

def test_threshold_grows_with_index():
    assert F.fdr_threshold(100) > F.fdr_threshold(1)

def test_false_discoveries_bounded_on_null_stream():     # case 12
    rng = random.Random(7)
    false_disc = 0
    for t in range(1, 3001):
        # null wallet: 6 fair-ish trials, outcomes drawn at the null prob
        trials = [(p, 1 if rng.random() < p else 0) for p in [rng.uniform(0.1, 0.6) for _ in range(6)]]
        e = ST.eprocess_value(trials)["e_value"]
        if F.is_flag_eligible(e, t):
            false_disc += 1
    assert false_disc <= 3            # expected <= alpha=0.1 across the stream; allow slack

def test_peeking_does_not_inflate_alerts():              # case 11
    # same null wallet re-analyzed 50x (growing e-process) stays sub-threshold in expectation
    rng = random.Random(3)
    flips = 0
    for trial_run in range(200):
        e = ST.eprocess_value([(0.5, 1 if rng.random() < 0.5 else 0) for _ in range(10)])["e_value"]
        if F.is_flag_eligible(e, t=trial_run + 1):
            flips += 1
    assert flips <= 5
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement**

```python
"""e-value online false-discovery control. Conservative bound: reject (flag-eligible)
iff E_t >= 1/(alpha*gamma_t), gamma_t = 6/(pi^2 t^2) (sum<=1). Bounds expected false
discoveries across the scan <= alpha (Markov + E[E_t|H0]<=1). Precision over recall."""
from __future__ import annotations
import math

def gamma(t: int) -> float:
    return 6.0 / (math.pi ** 2 * (max(1, int(t)) ** 2))

def fdr_threshold(t: int, alpha: float = 0.10) -> float:
    return 1.0 / (alpha * gamma(t))

def is_flag_eligible(e_value: float, t: int, alpha: float = 0.10) -> bool:
    return float(e_value) >= fdr_threshold(t, alpha)
```

- [ ] **Step 4: Run → PASS.**

- [ ] **Step 5: Commit** — `git commit -am "feat(analyzer): conservative e-value online-FDR allocator"`

---

### Task 4: Synthesis + verdict model (`synthesis.py`, pure)

**Files:**
- Create: `sniper/analysis/winning_wallet_thesis/synthesis.py`
- Test: `tests/test_analyzer_synthesis.py`

**Interfaces:**
- Consumes: a thesis view, a `FreshAssessment` (Task 2), an archetype view, a quality view (all light dataclasses/dicts — the orchestrator adapts the real `ThesisProfile`/archetype output into these in Task 8).
- Produces: `WalletRead` dataclass (fields per spec §5.1) + `synthesize_read(thesis, fresh, archetype, quality) -> WalletRead` + `derive_route(verdict, recent_anomaly_level, data_fresh) -> str`.
- Input adapters (defined here, used by orchestrator): `ThesisView`, `ArchetypeView`, `QualityView` dataclasses with the exact fields the precedence reads: `ThesisView{assessable, established_edge_supported, emerging_positive, negative_evidence, directional_residual_supported, obs_over_expected, p_value, e_value, history_depth, n_independent_events, edge_domains, loss_centers, entry_shape, recent_anomaly_level, regime_shift_candidate, realized_pnl}`; `ArchetypeView{archetype, intent, structural_share_high, structural_share}`; `QualityView{data_fresh, analyzer_version, rule_version, data_as_of, source_snapshot_hash, fdr_pass}`.

- [ ] **Step 1: Failing tests** (cases 5–8 + precedence collisions)

```python
# tests/test_analyzer_synthesis.py
from sniper.analysis.winning_wallet_thesis import synthesis as SY
from sniper.analysis.winning_wallet_thesis.synthesis import ThesisView, ArchetypeView, QualityView
from tests.helpers_thesis import fresh_view   # builds a FreshAssessment-like stub

def _q(**kw): return QualityView(data_fresh=True, analyzer_version=1, rule_version=1,
                                 data_as_of=0, source_snapshot_hash="h", fdr_pass=kw.get("fdr_pass", True))

def test_arb_huge_pnl_is_structural():                    # case 5
    r = SY.synthesize_read(
        ThesisView(assessable=True, established_edge_supported=False, emerging_positive=False,
                   negative_evidence=False, directional_residual_supported=False, obs_over_expected=1.0,
                   p_value=1.0, e_value=1.0, history_depth="deep", n_independent_events=200,
                   edge_domains=[], loss_centers=[], entry_shape={}, recent_anomaly_level="none",
                   regime_shift_candidate=False, realized_pnl=5_000_000),
        fresh_view(assessable=False), ArchetypeView("paired_outcome_arb", "structural_bias_exploiter",
                   structural_share_high=True, structural_share=0.95), _q())
    assert r.primary_verdict == "structural_non_directional"
    assert r.operational_route == "ignore"

def test_arb_with_directional_residual_retains_evidence():  # case 6
    r = SY.synthesize_read(
        ThesisView(assessable=True, established_edge_supported=True, emerging_positive=False,
                   negative_evidence=False, directional_residual_supported=True, obs_over_expected=2.4,
                   p_value=0.001, e_value=50, history_depth="deep", n_independent_events=120,
                   edge_domains=["sports"], loss_centers=[], entry_shape={}, recent_anomaly_level="none",
                   regime_shift_candidate=False, realized_pnl=1_000_000),
        fresh_view(assessable=False), ArchetypeView("two_sided_inventory_trader", "structural_bias_exploiter",
                   structural_share_high=True, structural_share=0.8), _q())
    assert r.primary_verdict == "structural_non_directional"
    assert "sports" in r.edge_location["domains"]          # secondary directional evidence retained

def test_established_with_recent_burst_keeps_verdict_changes_route():  # case 7
    r = SY.synthesize_read(
        ThesisView(assessable=True, established_edge_supported=True, emerging_positive=False,
                   negative_evidence=False, directional_residual_supported=True, obs_over_expected=2.0,
                   p_value=0.004, e_value=40, history_depth="deep", n_independent_events=300,
                   edge_domains=["geopolitics"], loss_centers=[], entry_shape={}, recent_anomaly_level="strong",
                   regime_shift_candidate=True, realized_pnl=22_000_000),
        fresh_view(assessable=False), ArchetypeView("directional_discretionary", "thesis_specialist",
                   structural_share_high=False, structural_share=0.1), _q())
    assert r.primary_verdict == "established_directional_edge"
    assert r.operational_route == "expedited_review"
    assert r.evidence_profile["recent_anomaly_level"] == "strong"

def test_high_wr_negative_pnl_is_antipattern():           # case 8
    r = SY.synthesize_read(
        ThesisView(assessable=True, established_edge_supported=False, emerging_positive=False,
                   negative_evidence=True, directional_residual_supported=False, obs_over_expected=0.8,
                   p_value=0.9, e_value=0.5, history_depth="deep", n_independent_events=150,
                   edge_domains=[], loss_centers=["crypto"], entry_shape={}, recent_anomaly_level="none",
                   regime_shift_candidate=False, realized_pnl=-40_000),
        fresh_view(assessable=False), ArchetypeView("directional_discretionary", "noise",
                   structural_share_high=False, structural_share=0.1), _q())
    assert r.primary_verdict == "negative_edge_or_antipattern"

def test_fresh_high_surprise_needs_two_channels_and_fdr():
    r = SY.synthesize_read(
        ThesisView(assessable=False, established_edge_supported=False, emerging_positive=False,
                   negative_evidence=False, directional_residual_supported=False, obs_over_expected=3.0,
                   p_value=0.01, e_value=80, history_depth="thin", n_independent_events=4,
                   edge_domains=["pol"], loss_centers=[], entry_shape={}, recent_anomaly_level="none",
                   regime_shift_candidate=False, realized_pnl=50_000),
        fresh_view(assessable=True, history_thin=True, channels_strong=2, alt_explanation=None,
                   e_value=80, ex_post_level="strong"),
        ArchetypeView("directional_discretionary", "directional_unknown", False, 0.1), _q(fdr_pass=True))
    assert r.primary_verdict == "fresh_high_surprise"
    assert r.operational_route == "expedited_review"
    assert "insider" not in (r.headline + " ".join(r.warnings)).lower()

def test_fresh_blocked_when_fdr_fails():
    r = SY.synthesize_read(
        ThesisView(assessable=False, established_edge_supported=False, emerging_positive=False,
                   negative_evidence=False, directional_residual_supported=False, obs_over_expected=3.0,
                   p_value=0.2, e_value=4, history_depth="thin", n_independent_events=2, edge_domains=[],
                   loss_centers=[], entry_shape={}, recent_anomaly_level="none", regime_shift_candidate=False,
                   realized_pnl=1000),
        fresh_view(assessable=True, history_thin=True, channels_strong=2, alt_explanation=None,
                   e_value=4, ex_post_level="moderate"),
        ArchetypeView("directional_discretionary", "directional_unknown", False, 0.1), _q(fdr_pass=False))
    assert r.primary_verdict != "fresh_high_surprise"
```

- [ ] **Step 2: Add `fresh_view` helper to `tests/helpers_thesis.py`** (a stub with the `FreshAssessment` fields the synthesis reads). Run → FAIL.

- [ ] **Step 3: Implement `synthesis.py`** — `WalletRead`, the three input views, `derive_route`, and `synthesize_read` with the precedence from spec §5.3. `fresh_high_surprise` requires `fresh.assessable and fresh.history_thin and fresh.channels_strong>=2 and not fresh.alt_explanation and quality.fdr_pass`. `established_directional_edge` requires `thesis.established_edge_supported` (which itself encodes "enough independent history" — set in orchestrator). On `established_*`, copy `recent_anomaly_level`/`regime_shift_candidate` into evidence and upgrade route. Always populate `edge_location` from thesis (domains/loss_centers/entry_shape). Headline = one sentence; warnings include fresh `sample_warning`; assert-free guarantee no "insider" token (lowercase scan in a unit test).

- [ ] **Step 4: Run → PASS.** `pytest tests/test_analyzer_synthesis.py -q`

- [ ] **Step 5: Commit** — `git commit -am "feat(analyzer): synthesis verdict model (7 buckets, precedence, route, evidence)"`

---

### Task 5: CLV feed (`clv.py`, Channel B)

**Files:**
- Create: `sniper/analysis/winning_wallet_thesis/clv.py`
- Test: `tests/test_analyzer_feeds.py` (CLV section)
- Reuse: `wallet_protocol/price_history_adapter.build_price_history_fn`; `feeds/goldsky_fills.asset_to_condition_from_activity` (invert for condition+oidx→asset).

**Interfaces:**
- Produces: `build_clv_fn(raw_activity, *, price_history_fn=None) -> fn(episode) -> float|None` returning signed closing-line value for the held side (`close_price - entry_price`, sign-corrected per outcome); `None` when price history unavailable (caller treats as gap, not zero).

- [ ] **Step 1: Failing test** — mock `price_history_fn` returning a higher close than entry → positive CLV; returning `None` → `None`. Build the asset map from a tiny `raw_activity` fixture.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** — map `(condition_id, outcome_index) → asset id` from `raw_activity`; for an episode, `entry=episode.entry_vwap`, `close=price_history_fn(asset_id, episode.resolution_ts - 60)`; CLV signed for the held outcome. Memoize via the adapter cache. Rate-limit handled by the shared limiter in the worker (Task 9).
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** — `git commit -am "feat(analyzer): Channel B CLV feed via price-history adapter"`

---

### Task 6: Funding graph (`funding_graph.py`, Channel E)

**Files:**
- Create: `sniper/analysis/winning_wallet_thesis/funding_graph.py`
- Test: `tests/test_analyzer_feeds.py` (funding section)
- Reuse: `conversions.py` `_polygon_rpc_url`, JSON-RPC POST pattern.

**Interfaces:**
- Produces: `build_funding(wallet, *, rpc_url=None, log_fetch=None, peers=None) -> dict` → `{funder, funded_ts, funder_age_days, linked_wallets, synchronized_entries, data_gaps}`. `log_fetch` injectable (tests pass a fake returning USDC Transfer logs); real default uses `eth_getLogs` over USDC `0x2791…84174` `Transfer` with `topics=[TRANSFER_SIG, None, pad(wallet)]`.

- [ ] **Step 1: Failing tests** — (a) injected logs with one funder → returns funder + age; (b) two wallets share a funder and enter the same `condition_id` within Δt → `synchronized_entries>0`; (c) `rpc_url=None` and no env → `data_gaps=["funding graph unavailable (no RPC)"]`, no coordination claim.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** — parse inbound Transfer logs, earliest non-exchange = funder; `funder_age_days` from `funded_ts` vs `now`; linkage from shared funder among `peers`; synchronized entries by comparing entry timestamps on shared `condition_id`s. Graceful+loud degrade. Bounded block range (cfg). Reuse RPC POST/backoff from `conversions.py`.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** — `git commit -am "feat(analyzer): Channel E on-chain funding graph + synchronized-entry linkage"`

---

### Task 7: Immutable reads persistence (`db_wallet_reads.py`)

**Files:**
- Create: `sniper/persistence/db_wallet_reads.py`
- Test: `tests/test_wallet_reads_persistence.py`

**Interfaces:**
- Produces: `ensure_wallet_reads_tables(conn)`; `append_read(conn, read: WalletRead) -> int` (returns read_id, sets `supersedes_read_id` to prior latest, idempotent on `(wallet, analyzer_version, data_as_of)`); `latest_read(conn, wallet) -> dict|None`; the `wallet_analysis_latest` view.

- [ ] **Step 1: Failing tests** — append two reads for a wallet → second `supersedes` the first; `latest_read` returns the second; appending the same `(wallet, analyzer_version, data_as_of)` twice is a no-op (idempotent); a read is never mutated (no UPDATE path).
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** — DDL from spec §7.1 (`wallet_analysis_reads` + `wallet_analysis_latest` view = `SELECT * ... WHERE created_ts = (SELECT MAX(created_ts) ...)`). Append-only.
- [ ] **Step 4: Run → PASS.** Then `python3 scripts/brain_index.py generate` + stage `index/`.
- [ ] **Step 5: Commit** — `git commit -am "feat(analyzer): immutable wallet_analysis_reads + latest view"`

---

### Task 8: Queue persistence (`db_wallet_analysis_queue.py`)

**Files:**
- Create: `sniper/persistence/db_wallet_analysis_queue.py`
- Test: `tests/test_wallet_analysis_queue.py`

**Interfaces:**
- Produces: `ensure_queue_tables(conn)`; `enqueue(conn, wallet, source, *, source_ref="", reason="", priority=100) -> bool` (idempotent: no-op if a live row exists); `lease(conn, owner, *, now, ttl_s=600) -> dict|None` (atomic, respects `next_retry_ts`, reclaims expired leases); `heartbeat(conn, id, now)`; `complete(conn, id)`; `fail(conn, id, err, *, now, backoff_s, max_attempts=5)`.

- [ ] **Step 1: Failing tests** — enqueue dedup (second enqueue while live → False); `lease` returns one row and flips it to `leased`; a second `lease` by another owner returns a different row (or None); expired lease is reclaimable; `fail` sets `next_retry_ts` and bumps `attempt_count`; after `max_attempts` stays `failed` and is not re-leased until `next_retry_ts`.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** — DDL from spec §6.1 (partial unique index on live rows). Atomic lease via `UPDATE ... WHERE id=(SELECT ... LIMIT 1)` then read-back, or a transaction with `BEGIN IMMEDIATE`.
- [ ] **Step 4: Run → PASS.** `brain_index generate` + stage `index/`.
- [ ] **Step 5: Commit** — `git commit -am "feat(analyzer): wallet_analysis_queue with atomic lease/heartbeat/retry"`

---

### Task 9: Orchestrator + worker (end-to-end)

**Files:**
- Create: `sniper/analysis/winning_wallet_thesis/orchestrator.py`
- Create: `scripts/analyze_wallets_worker.py`
- Test: `tests/test_analyzer_orchestrator.py`
- Reuse: `cli.run_extractor` (thesis), `scripts/wallet_archetype_extractor` (archetype), Tasks 2/4/5/6/7/8.

**Interfaces:**
- Produces: `analyze_one(conn, wallet, *, fetchers=None, now_ts=None, analyzer_version, rule_version) -> WalletRead` — runs thesis (`run_extractor`), archetype, fresh (`assess_fresh_anomaly` with CLV + funding feeds injected), maps to `ThesisView`/`ArchetypeView`/`QualityView` (incl. `fdr_pass = online_fdr.is_flag_eligible(fresh.e_value, t)` where `t` is the wallet's monotone test index), `synthesize_read`, `append_read`. Pure-ish (fetchers injectable for tests). The worker `main()` loops `lease → analyze_one → complete`/`fail`, with the shared rate limiter + a distinct `GeoBlockedError` that surfaces (not swallowed into `insufficient_evidence`).

**Key mapping rules (thesis → ThesisView):**
- `established_edge_supported = (history_depth in {moderate,deep}) and (shuffle p_value < 0.05 or e_value clears) and realized_pnl > 0 and n_independent_events >= MIN_INDEP (cfg, default 30)`.
- `history_depth`: thin if `total_resolved <= 40`, deep if `>= 150`, else moderate (reuse fresh thresholds).
- `recent_anomaly_level`/`regime_shift_candidate` from the fresh assessment on the recent-window subset.
- `directional_residual_supported`: a directional edge cluster survives after removing structural legs (from `ThesisProfile.edge_clusters`).

- [ ] **Step 1: Failing tests** — (a) case 4: fresh wallet, no resolved bets, extreme injected CLV → `operational_route="monitor"`, not established; (b) end-to-end with injected fetchers returning a deep, shuffle-significant book → `established_directional_edge` appended to reads; (c) worker fail-loud: a fetcher raising `GeoBlockedError` → queue row `failed` with surfaced error, NO read row written, NOT `insufficient_evidence`; (d) idempotent: running `analyze_one` twice with same data_as_of appends one read.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** `orchestrator.analyze_one` + `scripts/analyze_wallets_worker.py` (lease loop, `RateLimiter` ≤8 RPS shared across injected fetchers, `GeoBlockedError`). Intake helpers: `enqueue` adapters for discovery/followed/watchlist/manual/favorite (thin wrappers).
- [ ] **Step 4: Run → PASS.** Full focused run: `pytest tests/test_analyzer_synthesis.py tests/test_fresh_anomaly_engine.py tests/test_fresh_anomaly_stats.py tests/test_online_fdr.py tests/test_analyzer_feeds.py tests/test_wallet_reads_persistence.py tests/test_wallet_analysis_queue.py tests/test_analyzer_orchestrator.py -q`
- [ ] **Step 5: Commit** — `git commit -am "feat(analyzer): orchestrator + worker (lease loop, rate limit, fail-loud); wire thesis+fresh+archetype+synthesis end-to-end"`

---

### Task 10: Adversarial review + fixes + push

- [ ] **Step 1:** Dispatch `research-methodologist` (statistical validity: Poisson-binomial, e-process construction + λ-grid safety, conservative online-FDR bound vs claimed guarantee, EB-shrinkage, event-independence, alt-explanation completeness) and `platform-reviewer` + `feature-dev:code-reviewer` (code correctness, lease atomicity, fail-loud, immutability, no-"insider"/no-routing-field/no-legacy-reuse compliance) — in parallel.
- [ ] **Step 2:** Triage findings (no yes-man — reject with reasons where wrong); fix all real ones with regression tests.
- [ ] **Step 3:** Green gate — focused suite + `python3 scripts/validate_schema_refs.py` + brain-index freshness (`brain_index.py generate` clean) + `grep -rn "insider" sniper/analysis/winning_wallet_thesis/{synthesis,fresh_anomaly,orchestrator}.py` empty.
- [ ] **Step 4:** Update `MEMORY.md`/HANDOFF + a memory file for the analyzer engine; KSG block.
- [ ] **Step 5:** `git pull --rebase origin main` then push. Commit trailers + KSG per project rules.

---

## Self-Review

**Spec coverage:** §3 units U1–U7 → Tasks 7/8/9 (queue,worker,reads), 4 (synthesis), 2 (fresh), 1/3 (stats,fdr); engines U3/U5 reused via orchestrator (Task 9). §4 channels A/C/D → Task 2; B → Task 5; E → Task 6. §4.4 e-process → Task 1. §4.5 online-FDR → Task 3. §4.7 EB → Task 1. §5 synthesis → Task 4. §6 funnel → Tasks 8/9. §7 persistence → Task 7. §9 honesty → enforced in Tasks 2/4 + Task 10 grep gate. §10 all 12 cases mapped (1–3,9,10→T2; 4,end-to-end→T9; 5–8→T4; 11–12→T3). §11 phasing preserved.

**Placeholder scan:** none — every code step has concrete code; the only deferred items (exact e-LOND constant relaxation, EB `k` cohort-fit) are explicitly scoped as conservative v1 defaults with the §10 tests as the lock, not TODOs.

**Type consistency:** `FreshAssessment` fields (Task 2) match what `synthesize_read` reads (Task 4 `fresh_view` stub + real in Task 9). `WalletRead` produced in Task 4, consumed in Task 7 `append_read`. `eprocess_value` return dict (`e_value/anytime_p`) consistent across Tasks 1/3/9. `is_flag_eligible(e_value,t,alpha)` signature consistent T3/T9.
