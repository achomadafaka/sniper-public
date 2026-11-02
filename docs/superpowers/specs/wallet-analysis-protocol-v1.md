---
memory_name: methodology/wallet-analysis-protocol-v1
memory_type: knowledge
memory_description: Pre-registered protocol for per-wallet edge analysis on Polymarket. Fixed hypotheses, inference procedures, stationarity treatment, independence clustering, execution-quality, external validity, and output template. Commit this, then implement analyze_wallet().
memory_tags: [methodology, protocol, wallet, pre-registered, inference, sound]
---

# Wallet Analysis Protocol v1

**Date:** 2026-04-25
**Status:** pre-registered, locked
**Author:** research-methodologist agent
**Implements:** wallet-intelligence-rearchitecture, tasks A4 + execution-profile + B2 consumption
**Supersedes:** ad-hoc wallet dossier notes
**Methodological standards applied:** brain entry `research-12-methodological-standards-2026-04-02`
**Methodology review verdict (2026-04-25):** SOUND, medium trust, 3 minor follow-ups before shipping `analyze_wallet()` (see §13 items 1, 6 + H5 peer_count flag)

This document is the fixed analysis plan. Do not modify hypotheses or inference procedures
after running analysis on a wallet. Changes require a new protocol version with a new date.

---

## 1. Scope and intent

This protocol tells you whether a specific Polymarket wallet has defensible
trading edge, what kind, and how durable it appears to be — or tells you honestly
that the data is insufficient to decide.

It is not a narrative template. It is a checklist of tests with pre-specified
verdicts. The verdict is mechanical: run the tests, record the outputs, apply the
decision rules. No judgment calls in the inference layer.

---

## 2. Data retrieval — mandatory steps before any analysis

These steps are pre-analysis. Do not look at performance numbers before completing
all of them. Looking at results before committing to the analysis plan constitutes
a registered deviation from this protocol.

### 2.1 Closed positions

Fetch `/data-api.polymarket.com/closed-positions?user=W` exhaustively:
- `limit=50` (server cap, verified 2026-04-25; see brain entry
  `research-polymarket-closed-positions-pagination-2026-04-25`)
- `sortBy=TIMESTAMP`, `sortDirection=ASC` for stable pagination
- `max_pages=100`; log how many pages were fetched
- If `n_pages == 100` and last page is full, log "pagination cap hit — lifetime
  history may be truncated"

Never use default sort (DESC by realizedPnl). That returns only the 50 biggest wins,
producing 100% WR on a wallet that is actually a net loser.

### 2.2 Open positions

Fetch `/data-api.polymarket.com/positions?user=W` exhaustively.
- `limit=100`, no sort params (server rejects sortBy on this endpoint)
- For each open position: `unrealized_pnl = (curPrice - avgPrice) * size`
- Count `n_open_dead` = positions where `curPrice <= 0.01` (already-resolved losses
  the wallet never redeemed; these are real losses, not noise)

### 2.3 Activity (behavioral, not performance)

Fetch `/data-api.polymarket.com/activity?user=W&type=TRADE` up to the 3000-row cap.
- Log `n_activity_rows` returned
- If `n_activity_rows == 3000`, flag: "activity covers recent behavior only —
  fill-mechanics hypotheses are invalid for this wallet if it is high-volume"
- Do NOT use activity rows to compute win rates or P&L (they are partial)

### 2.4 Execution profile

Run `scripts/wallet_archetype_extractor.py <wallet> --with-execution`. Store the
`execution_profile` block. This is a falsification instrument — it can rule out
short-horizon entry-timing edge but cannot confirm it.

### 2.5 Tag attribution

Join closed positions and open positions to `market_tags` on `condition_id`.
- Unmatched condition_ids → tag = `"unclassified"`
- For each position, record all tags it maps to (not just the first); the
  alphabetical primary tag is used for within-tag scoring (B1/B2 specs),
  but multi-tag membership is preserved in the raw output for descriptive review

### 2.6 Net P&L computation (mandatory, pre-analysis)

```
net_pnl = sum(closed_realized_pnl) + sum(open_unrealized_pnl)
```

Never use `closed_realized_pnl` alone as the wallet's P&L (SemyonMarmeladov flip:
apparent +$17.7M, true -$1.54M).

---

## 3. Pre-registered hypotheses

**These hypotheses are locked. Run all of them. Report all results including
inconclusive and underpowered ones. Do not add or remove hypotheses based on
what you observed in step 2.**

Multiple-testing correction: Benjamini-Hochberg FDR at q=0.10 across all
hypotheses run for a wallet. Bonferroni is too conservative because tests are
correlated. BH at q=0.10 (not 0.05) because test set is small (≤8) and power is
already limited by sample size.

A finding is "supported" only if its BH-adjusted p < 0.10 AND it survives the
null validation in §4 AND it survives the time-split check in §5.

### H1 — Net P&L is positive (gate hypothesis)

**What:** `net_pnl > 0`
**Why gate:** If net P&L is negative, hypotheses H2–H6 may be true but the wallet
is still a net loser. Flag edge claims in the presence of negative net P&L as
"edge candidate in a losing book."
**Test statistic:** Observed `net_pnl` vs. bootstrap distribution of null net P&L (§4.1).
**Falsification:** `bootstrap_CI_lower <= 0` → refuted.
**Note:** Not a per-trade significance test. It is a null-vs-observed test on the total book.

### H2 — Held-to-resolution win rate exceeds market-implied probability

**What:** Wallet's WR on positions held to resolution is higher than mean entry price would imply.
**Held-to-resolution definition (locked):** `curPrice ∈ {0, 1}` (within ε=0.01) AND no SELL
activity events on this condition_id before resolution. Sell-side contamination is
the primary source of inflated WR on Polymarket; this filter is non-negotiable.
**Test statistic:** `WR_held - mean_entry_price_held` (excess WR over implied)
**Null:** Zero excess. Bootstrap: shuffle win/loss labels stratified by entry-price decile.
**CI:** 10,000-sample stratified bootstrap.
**Falsification:** 95% CI includes zero → inconclusive.
**Minimum n:** `n_held_to_resolution >= 20`. Below 20: verdict = "underpowered."
**Entry price filter (ADR-002):** Also report a sub-test restricted to positions with
`avgPrice < 0.80`. Positions entered at >= 0.80 provide almost no signal (certainty-buyers).
Report both full and sub-0.80 numbers.

### H3 — Position size correlates with eventual win (conviction signal)

**What:** Larger positions (USDC notional) win at a higher rate than smaller ones.
**Test statistic:** Spearman ρ between `entry_notional` and `is_win` (binary) across
held-to-resolution positions.
**Null:** Permutation test (shuffle `is_win` labels 10,000 times).
**Falsification:** p_perm > 0.10 → no conviction signal.
**Minimum n:** `n_held_to_resolution >= 30`. Below 30: underpowered.
**Caveat:** If Layer 1 archetype is `two_sided_inventory_trader` or `paired_outcome_arb`,
skip H3 and flag as N/A (hedge wallets trivially show large positions because they run books).

### H4 — Entry price is systematically low (entry-timing edge)

**What:** At entry, fill price is lower than market price 6 hours later (or at resolution
if resolution is within 6h).
**Data source:** `execution_profile.drift_6h` from §2.4. If `execution_profile.status !=
"populated"` or `sample_tier == "unknown"` → "underpowered."
**Test statistic:** `mean(drift_6h)` across qualifying fills (in-play fills excluded).
**Null:** Zero drift. Bootstrap CI.
**Falsification:** 95% CI includes zero → inconclusive. Negative upper bound → refuted (late entry).
**Warning:** v1 execution profile is BUY-only — falsification tool, not discovery.
A drift near zero does NOT mean "no edge" — it means "no short-horizon entry-timing edge."
Longer-duration thematic conviction is not captured here.

### H5 — Tag-level edge concentration (where is the edge?)

**What:** For each tag with `n_held_to_resolution >= 10`, test whether wallet's held WR
exceeds the tag's population mean WR.
**Test statistic:** `WR_held_tag - pop_mean_WR_tag`
**Null:** Bootstrap from peer distribution's `win_rate_held_only` for that tag.
**CI:** 10,000 samples.
**Falsification:** CI includes zero → no tag-level edge.
**Multiple testing within H5:** Apply BH within per-tag tests for this wallet.
**Minimum n per tag:** 10. Below 10: descriptive only.
**Population baseline caveat (mandatory):** If peer population for a tag has fewer
than 10 wallets with `n_held_to_resolution >= 3`, baseline too thin. Flag
`confidence_flag=low_population` and DO NOT claim H5 supported. (Mirrors B2's pattern.)

### H6 — Exit quality (does holding add value?)

**What:** When wallet holds rather than selling early, does subsequent price favor them?
**Operationalized:** For positions where wallet has SELL events, compare sell price to
price 24h after the sell. If `sell_price > price_t+24h`: early exit was correct.
**Data dependency:** Activity rows + `clob.polymarket.com/prices-history?fidelity=60`.
If `n_sell_events_available < 20`: "underpowered."
**Test statistic:** Fraction of early exits where holding 24h would have been better.
**Null:** 0.50. Binomial test.
**Falsification:** p > 0.10 → inconclusive.

### H7 — Performance stationarity (is the edge recent?)

**What:** Per-trade P&L has not structurally changed.
**Test statistic:** CUSUM on cumulative-return series. Standard tabular CUSUM `h=5, k=0.5σ`.
**⚠ Calibration follow-up (§13 item 6):** These params are manufacturing-derived for Gaussian
noise. P&L is heavy-tailed; false-alarm rate not validated. Recalibrate before shipping.
**Outcome:** If CUSUM signals, split history at the regime-change point and report regimes
separately. Do not suppress pre-change.
**When stationarity fails:** Run downstream hypotheses on POST-change regime only if `n_post >= 20`.
**When stationarity passes:** Analyze all positions together.

### 3.8 Power table

At BH q=0.10, stratified bootstrap with 10,000 samples:

| n (held-to-resolution) | Min detectable excess WR (H2) | Min detectable Spearman r (H3) |
|------------------------|-------------------------------|--------------------------------|
| 20                     | ~18pp                         | ~0.45                          |
| 50                     | ~11pp                         | ~0.28                          |
| 100                    | ~8pp                          | ~0.20                          |
| 300                    | ~5pp                          | ~0.12                          |
| 500                    | ~4pp                          | ~0.09                          |

Approximate. Recalculate per-wallet via simulation if distribution is heavy-tailed.
"Inconclusive" is the correct verdict for most wallets at typical sample sizes.

---

## 4. Null / baseline construction

### 4.1 Wallet-level null

For H1, null is: this wallet's per-trade P&L distribution is the same as population.
1. Empirical per-position P&L distribution across all wallets in DB
2. Draw `n` samples (matching wallet's n_positions) with replacement 10,000 times
3. p-value = fraction of null draws >= observed net_pnl

**Population baseline caveat:** local followed_wallet_activity covers 21 wallets — biased
by our discovery pipeline (speculator_bot over-represented). State this in every report.
Until B1 population expansion, treat 21-wallet baseline as "illustrative peer comparison"
not "Polymarket population."

**Alternative for H2:** Polymarket is efficiently priced on held-to-resolution positions, so
`E[WR_held] = mean_entry_price` is the natural null and tighter than bootstrap. Use both,
report the more conservative.

### 4.2 Tag-level null

For H5, null is the within-tag peer distribution of `win_rate_held_only` from `wallet_tag_pnl`.
If fewer than 10 peers have data for that tag, flag `confidence_flag=low_population` and switch
to market-implied null (§4.1).

---

## 5. Temporal separation (external validity)

### 5.1 Time-split design

For every wallet with `n_closed_positions >= 60`:
- Sort positions by `timestamp` (ASC)
- Training: first 70%; test: last 30%
- Re-run H2 and H3 on training. If supported (BH p < 0.10), re-run on test.
- If not supported on test: verdict = "training artifact." Downgrade to inconclusive.
- If supported on both: flag "survived time-split."

For wallets with n < 60: no time-split. Flag "insufficient history for OOS validation."

### 5.2 Tag-time interaction

For H5, if a tag's positions are clustered in a narrow time window (>70% within 90-day window),
flag "concentrated period — tag edge may not generalize." Common for event-driven wallets
(e.g., Iran crisis positions clustering in 2025).

---

## 6. Independence handling

Polymarket positions on the same market event are correlated. Treating each as independent
inflates effective sample size.

**Clustering rule (heuristic, locked for v1):**
Two positions are in the same cluster if:
1. Same `eventSlug` from `/closed-positions`, OR
2. `timestamp` values within 24h AND same primary tag

**Cluster-as-observation reduction:**
For H2 and H3, also report results with each cluster treated as a single observation
(cluster mean entry price, sum notional). If cluster-reduced result differs materially
from position-level, flag the discrepancy.

**Wallet flag:** If >50% of positions fall into clusters of size ≥ 3, flag "concentrated
portfolio — independence assumption violated; cluster-reduced sample size = {n_clusters}."

**⚠ Validation follow-up (§13 item 1):** This rule is unvalidated. Pick 5 wallets with
known event structure and verify the rule correctly groups correlated bets before promoting
findings based on cluster-reduced inference.

---

## 7. Execution quality measures (H4 supplement)

Provided by shipped `wallet-execution-profile` tool. Protocol consumes output, does not duplicate.

**What v1 measures:**
- `skew_vs_preceding_bar`: fill_price minus preceding 1-min bar close. Positive = buying after rise.
- `drift_1h`, `drift_6h`, `drift_24h`: price at T+H minus fill price. Positive = well-timed.
- `tape_midpoint`: cross-reference vs other fills in 5-min pre-window.

**What v1 cannot measure (state in every execution-quality section):**
- Sell-side execution
- True slippage (no order book; cannot compare fill to NBBO)
- Maker vs taker distinction
- Fill quality on thin markets (tape_n < 3 fills are excluded)

**Interpretation rule:** If `mean(drift_6h)` 95% CI includes zero, wallet does NOT
demonstrate short-horizon entry-timing edge. This does NOT mean no edge — means this
particular measurement cannot detect it. Don't conflate "no evidence of X" with
"evidence of no X" unless adequately powered.

---

## 8. Multi-tag intersection

Primary-tag (alphabetical first slug) loses information when a market maps to multiple tags.

**v1 approach:**
- Record all tags for each position
- Descriptive output: list all tags wallet has any exposure to, with position counts and PnL
- Inference (H5): use primary-tag only. Don't run H5 on secondary tags (would double-count
  in multiple peer distributions).
- Flag wallets where top-3 tags by position count all have primary-tag assignment, since
  secondary-tag exposure is hidden from inference

**Known limitation:** Multi-tag inference not solved in v1. A "crypto + politics" specialist
looks like a generalist in both. Flagging this is required output; no fix attempted.

---

## 9. Stationarity and regime detection

Use CUSUM on cumulative realized P&L:
1. `cusum_t = max(0, cusum_{t-1} + (pnl_t - μ - k))` where `μ` is in-sample mean P&L,
   `k = 0.5 * σ`
2. Signal when `cusum_t > h`, `h = 5 * σ` (standard tabular CUSUM, ARL 370)
3. Record signal-point as regime boundary

**When CUSUM signals:**
- Run H2, H3, H5 on post-signal regime only if `n_post >= 20`
- Report pre-signal regime descriptively: "pre-regime data retained for context;
  inference uses post-regime only"
- If `n_post < 20`: "regime change detected but post-regime sample too small for inference"

**When CUSUM does not signal:** Analyze all positions together, report "no structural break."

---

## 10. What this protocol cannot conclude

State in every wallet report. These are methodological facts, not caveats.

1. **Survivorship in observation set.** We see wallets that traded enough to surface. Edge
   estimates are upward-biased relative to true Polymarket distribution.
2. **Closed-positions self-selection.** `/closed-positions` returns only redeemed/sold positions.
   Unredeemed dead positions only visible via `/positions`. Even with the A4 net-P&L correction,
   the closed-positions sample used for WR analysis still misses unredeemed positions. Wallets
   with high `n_open_dead` have WR estimates that overstate true resolution rate.
3. **Activity 3000-cap.** For wallets with >3000 lifetime trades, `/activity` covers recent
   behavior only. H4/H6 reflect current style, not lifetime.
4. **No order-book reconstruction.** True slippage uncomputable. Fill quality approximated
   by `skew_vs_preceding_bar`, coarser than NBBO-vs-fill.
5. **Tag baseline with n=21.** Population baseline is 21 wallets. Percentile estimates have
   wide CIs. Treat within-tag rank as "illustrative peer comparison" until B1 population
   expansion.
6. **Independence assumption is heuristic.** Event-slug + 24h rule unvalidated. Effective
   sample sizes may be smaller than reported.
7. **No causal inference.** Supported H3 (size ~ win) does not mean private information.
   Consistent with several non-informational explanations.
8. **Zero-fee venue effects.** ~89.6% maker fills are zero-fee on Polymarket. Edge thresholds
   don't need fee correction for most wallets. But taker fills (10.4%) may matter for wallets
   that frequently use market orders. Maker/taker not exposed by Data API.

---

## 11. Output report template

Every wallet report must include these sections in this order. Do not omit. Mark missing as
"N/A — [reason]."

```
## Wallet Report: {wallet_address}
**Protocol version:** wallet-analysis-protocol-v1
**Analysis date:** {ISO 8601}
**n_closed_positions:** {n}
**n_held_to_resolution:** {n}
**n_open_positions:** {n}
**n_open_dead:** {n}
**net_pnl:** {$ value}
**activity_rows_fetched:** {n} {"(CAP HIT)" if n==3000}
**archetype (Layer 1):** {label}
**regime change detected:** yes | no

### Data quality flags
{List all mandatory flags from §2 that triggered, or "None."}

### H1 — Net P&L gate
- observed: {$}
- bootstrap 95% CI: [{lower}, {upper}]
- verdict: supported | refuted | inconclusive

### H2 — Held-to-resolution WR vs market-implied
- n_held_to_resolution: {n}
- WR_held: {%}
- mean_entry_price_held: {%}
- excess WR: {pp}
- 95% CI on excess: [{lower}pp, {upper}pp]
- BH-adjusted p: {value}
- sub-0.80: WR={%}, excess={pp}, CI=[{lower}, {upper}]
- verdict: supported | refuted | inconclusive | underpowered

### H3 — Size-win correlation
- n: {n}
- Spearman r: {value}
- permutation p: {value}
- BH-adjusted p: {value}
- verdict: supported | refuted | inconclusive | underpowered | N/A (archetype)

### H4 — Entry timing (execution profile)
- execution_profile.status: {status}
- sample_tier: {unknown | medium | high}
- mean drift_6h: {value}
- 95% CI: [{lower}, {upper}]
- verdict: supported | refuted | inconclusive | underpowered

### H5 — Tag-level edge concentration
For each tag with n_held >= 10:
  - tag: {slug}
  - peer_count: {n}                       ← required
  - confidence_flag: ok | low_population  ← required
  - n_held: {n}
  - WR_held: {%}
  - pop_mean_WR: {%}
  - 95% CI on excess: [{lower}pp, {upper}pp]
  - BH-adjusted p (within H5): {value}
  - verdict: supported | refuted | inconclusive | underpowered | baseline_underpowered

If confidence_flag=low_population: verdict = "inference_not_run"

Tags with n_held < 10: {list with counts, "descriptive only"}

### H6 — Exit quality
- n_sell_events_available: {n}
- fraction where holding 24h would improve: {%}
- binomial p (null=0.50): {value}
- BH-adjusted p: {value}
- verdict: supported | refuted | inconclusive | underpowered

### H7 — Stationarity
- CUSUM result: no break | break at position {k} on {date}
- pre-regime: n={n}, WR={%}, mean_pnl={$}
- post-regime: n={n}, WR={%}, mean_pnl={$}
- inference basis: full history | post-regime only | insufficient post-regime

### Time-split validation
- n >= 60: yes | no (no time-split attempted)
- If yes: H2 training p={value}, test p={value}, holdout: survived | failed | underpowered
- H3 training p={value}, test p={value}, holdout: survived | failed | underpowered

### Independence check
- n_clusters (eventSlug + 24h rule): {n}
- fraction in clusters of size >= 3: {%}
- cluster-reduced H2: excess={pp}, CI=[{lower},{upper}]
- flag: concentrated | normal

### Execution quality
- BUY fills analyzed: {n}
- skew_vs_preceding_bar mean: {value}
- drift_6h mean: {value}
- tape confidence: {n_high_confidence_fills}/{n_total_fills}
- short-horizon entry-timing edge: ruled out | not ruled out | underpowered
- v1 limits: [list from §7]

### Multi-tag notes
- tags with any exposure: {list with position counts}
- top-3 tags by position count: {list}
- secondary-tag exposure hidden from inference: yes | no

### Overall verdict
Decision rule: "edge detected" requires AT LEAST ONE of {H2, H3, H5} to be "supported"
after BH correction AND survive time-split. H1 must not be "refuted." H4/H6 supplementary.

- Overall: edge_detected | no_edge | underpowered | data_insufficient
- Primary edge type (if detected): {entry_timing | size_conviction | tag_specialist | unknown}
- Copyability note: {one sentence, based on archetype + edge type}
- Open questions: {list what would change the verdict}

### Limitations applicable to this wallet
{From §10 — mark which limitations specifically apply, one line each.}
```

---

## 12. Operationalization notes

```python
def analyze_wallet(wallet: str) -> ProtocolReport:
    ...
```

`ProtocolReport` is a dataclass with one field per output-template section. Fields that
cannot be computed store sentinel `{"verdict": "underpowered", "reason": "..."}`.

**Key dependencies:**
- `scripts/wallet_archetype_extractor.py` — Layer 1 archetype + execution profile
- `polymarket_api.fetch_closed_positions(wallet)` — ASC/TIMESTAMP, limit=50, max_pages=100
- `polymarket_api.fetch_open_positions(wallet)` — for `n_open_dead`
- `db.wallet_tag_pnl_rows(conn, wallet)` — for H5 peer distribution
- `clob.polymarket.com/prices-history` — H4/H6 reference prices
- `scipy.stats.spearmanr` — H3
- `statsmodels.stats.multitest.multipletests(method='fdr_bh')` — BH correction

**CUSUM:** `ruptures` library or implement directly. `h=5, k=0.5` σ-units (recalibrate per §13).

**Bootstrap:** One loop, 10,000 iterations, `np.random.seed(42)` for reproducibility.

**Session guard:** Store `protocol_version` in report. Re-running with version mismatch =
results not comparable.

---

## 13. Needs-follow-up empirical work

These are honest gaps. Do not paper over them.

1. **Independence clustering rule.** eventSlug + 24h heuristic unvalidated. Pick 5 wallets
   with known event structure (e.g., Iran escalation bets), verify rule groups correlated
   bets correctly.
2. **Population baseline.** 21-wallet population not the Polymarket distribution. B1
   population expansion to candidate_users tier needed. Until then, all within-tag
   percentile claims are illustrative peer comparisons.
3. **Power table calibration.** §3.8 power table is approximate. Simulate under empirical
   P&L distribution (heavy-tailed) and report actual power.
4. **Multi-tag inference.** H5 restricted to primary-tag. Multi-tag exposure model
   (regression with tag dummies) needed; validate against wallets with known specializations.
5. **Exit skill (H6) via Subgraph.** For wallets with /activity capped at 3000 rows, H6
   often underpowered. Polymarket Subgraph (TheGraph) may provide lifetime on-chain trade
   history. Validate Subgraph access; add as optional data source in §2.3.
6. **CUSUM parameterization.** Standard `h=5, k=0.5` is for Gaussian process control. P&L
   series are heavy-tailed. Simulate CUSUM under empirical distribution to verify ARL,
   calibrate `h` for Polymarket context. ⚠ Methodology review: do this BEFORE shipping
   `analyze_wallet()` to avoid spurious regime detections truncating samples.
