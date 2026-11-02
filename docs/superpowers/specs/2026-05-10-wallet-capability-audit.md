# Wallet Capability Audit — What ProtocolReport Can Actually Claim

**Date:** 2026-05-10  
**Status:** read-only audit, fixture replay only  
**Base context:** Timing Surface v1 + semantic followups through R9.5  
**No live API calls:** yes — all sampled reports used committed calibration fixtures  
**No DB writes / service restarts:** yes

---

## 1. Surface Inventory

| Surface | Honest capability statement | Claim type | Confirms | Cannot distinguish |
|---|---|---|---|---|
| Stage 1 gates | Rejects obvious non-candidates before inference: hedge archetype, insufficient sample, dormant wallet. Also surfaces caution flags like open-dead positions. | factual gate | Whether the report should run deep analysis at all. | Whether a passing wallet is copyable or profitable for us. |
| Capital efficiency | Reports dollar-weighted ROI, equal-weighted ROI, and DW/EW divergence over closed positions with usable cost basis. | factual + descriptive | Whether dollars performed differently from the average position. | Why the divergence exists: timing, sizing, liquidity, or luck. |
| Capital ledger | Reports SPLIT/MERGE/REDEMPTION onchain flow and open-mark parity diagnostics. | factual + reconciliation diagnostic | CTF flow totals, redemption counts, whether current portfolio value roughly matches open marks. | Lifetime equity, external funding, true bankroll ROI. |
| Execution quality | Passes through skew/drift summaries from `execution_profile` when populated. | descriptive | Whether captured fills were followed by favorable/unfavorable short-horizon moves. | Whether the wallet caused the move, beat the book after slippage, or can be copied at the same price. |
| Edge Surface | Breaks closed positions into YES/NO/OTHER buckets with WR, ROI, PnL, top markets, per-bucket attribution slices, and tag-grain descriptive financials when tag data exists. | descriptive | Where realized PnL came from by outcome side, tag grain, and market concentration. | Statistical edge, causality, slippage-adjusted copyability. |
| Exit Attribution | Categorizes closed positions into held-to-resolution wins/losses, pre-resolution profitable exits/loss cuts, unresolved, and insufficient data. | descriptive | Whether captured history shows prediction-style holding or trading-style exits. | Partial sell ladders, missed sells outside capture, exact accumulation/exit construction. |
| Completeness Surface | Reports capture state: activity cap, subgraph fallback, unmapped fills, outside-window/insufficient attribution counts, and caveats. | factual caveat layer | Whether other surfaces are well-covered enough to trust. | Missing behavior itself; it flags opacity but does not fill it. |
| Timing Surface v1 | Classifies captured-travel behavior as holding-driven, trading-driven, mixed, unclear, or underpowered using Exit Attribution categories. | descriptive classification | Whether realized PnL in captured rows came mainly from holding to resolution or pre-resolution exits. | Strict market repricing, future price movement, latency/slippage, per-fill accumulation. |
| Held analyst H1 | Tests net realized PnL location after winsorized/bootstrap handling. | statistical inference | Whether closed-position PnL is statistically positive/refuted/inconclusive. | Source of profit. H1 alone is not a primary edge test. |
| Held analyst H2 / H2_sub080 | Tests held-to-resolution win rate versus entry-implied price, including sub-0.80 subset. | statistical inference | Whether buy-and-hold prediction results beat entry-implied baseline. | Whether good entries came from timing, information, liquidity, or selection bias. |
| Held analyst H3 | Tests size conviction: relationship between position size and win outcome. | statistical inference | Whether bigger held positions were more likely to win. | Whether sizing was intentional, capital-constrained, or copied from market liquidity. |
| Held analyst H4 | Tests execution-profile entry timing when execution profile is present. | statistical inference | Whether drift_6h supports entry timing. | Anything when `execution_profile` is absent; fixture replay often underpowers it. |
| Held analyst H5 | Tests tag-grain held WR versus peer baselines when tags and peer baselines exist. | statistical inference | Tag specialism relative to peers. | Anything under fixture runs without tag/peer surfaces. |
| Held analyst H6 | Tests sell/exit quality when `price_history_fn` exists. | statistical inference | Whether sells avoided worse future price movement. | Anything when price history is absent; currently underpowered in sampled fixture runs. |
| Stationarity / time split | Applies CUSUM and rolling-origin holdout checks to supported/refuted claims. | validation | Whether a signal survived out-of-sample folds or shifted regimes. | New signal source; it validates or weakens existing hypotheses. |
| Flipper FH1-FH6 | Equivalent flipper branch tests over reconstructed BUY/SELL flip pairs. | statistical inference | Flip ROI, size conviction, spread capture, tag flip ROI, hold-duration regime, directional skill when data exists. | Anything for held-only wallets; no branch runs when flip_rate routes to held_analyst_only. |
| Overall verdict | Compresses branch verdicts into `edge_detected`, `no_edge`, `underpowered`, `operator_review`, or `data_insufficient`. | decision label | Whether primary statistical tests found an edge under current routing and data. | Copyability. A non-null `primary_edge_type` is not enough without `overall_verdict=edge_detected`. |

Working rule: factual observations are safe to state directly. Descriptive
surfaces are useful for mechanism hypotheses. Statistical tests are the only
surfaces that can support `edge_detected`. Interpretations must cite both the
surface and its coverage caveat.

---

## 2. Per-Wallet Walkthroughs

Fixture command shape for every sampled wallet:

```bash
python scripts/run_wallet_protocol.py <wallet> \
  --from-fixture tests/calibration/fixtures/<wallet>_2026-04-30.json \
  --seed 42 --n-iter 500 --json --no-persist
```

### 2.1 Summary Table

| Wallet | Verdict | Primary edge type | Mode | Closed / attributed | Timing v1 | H2 | H3 | Main honest claim |
|---|---:|---|---|---:|---|---|---|---|
| `0xdbade4c8...9c95` SwissMiss | underpowered | none | held | 975 / 235 | underpowered | inconclusive +2.2pp | inconclusive | Profitable, NO-heavy, OTHER bucket drives gains; current attribution coverage is too low for timing classification. |
| `0x1cc16713...b8df` eatpraylove | underpowered | entry_timing | held | 1014 / 27 | underpowered | supported +3.6pp | inconclusive | H2 supports prediction edge, but holdout/routing result still underpowered; attribution coverage is almost absent. |
| `0x6a72f618...33ee` exec-bot | edge_detected | size_conviction | held | 3931 / 52 | underpowered | inconclusive | supported rho=0.0669 | Strongest statistical claim: larger held positions correlate with wins. Timing/exit mechanism remains opaque. |
| `0x22e4248b...ef1c` HWR | underpowered | none | held | 1083 / 70 | underpowered | inconclusive | inconclusive | High WR and PnL are observable, but current protocol cannot substantiate a primary edge. |
| `0x5bffcf56...ffbe` YatSen | underpowered | entry_timing | held | 860 / 106 | underpowered | supported +2.7pp | refuted | Outcome buckets show profitable YES/NO/OTHER activity; size conviction is negative and timing is undercovered. |
| `0x84933747...5366` small insider | underpowered | none | held | 272 / 272 | unclear, high confidence | refuted -8.4pp | inconclusive | Full attribution says behavior is mixed/unclear, not edge-like; H1/H2 refute profitability/WR claims. |
| `0x1eb06435...d25b` speculator | edge_detected | entry_timing | held | 762 / 87 | underpowered | supported +7.2pp | inconclusive | H2 supports held prediction edge; copy mechanism is still opaque because attribution coverage is low. |
| `0xb3b50ef6...bef4` HWR medium | edge_detected | size_conviction | held | 809 / 257 | underpowered | inconclusive | supported rho=0.3351 | H3 supports size conviction; dollar-weighted ROI is negative despite positive equal-weighted ROI. |

### 2.2 Wallet Notes

**SwissMiss `0xdbade4c8...9c95`**

- Factual: 975 closed, 950 held to resolution, net PnL about $556.8K, 40 open.
- Descriptive: YES bucket is profitable, NO bucket has high WR but negative PnL, OTHER bucket supplies most positive PnL.
- Inference: H2 and H3 are inconclusive; verdict is underpowered.
- Opaque: 740/975 positions are insufficient for exit attribution. Timing v1 cannot say if profits are holding-driven or trading-driven.

**eatpraylove `0x1cc16713...b8df`**

- Factual: 1014 closed, 541 held, net positive; DW ROI 6.6%.
- Descriptive: both YES and NO buckets are profitable; only 27/1014 positions are attributed.
- Inference: H2 is supported, H3 inconclusive, H4/H6 underpowered.
- Opaque: With 987 insufficient-attribution rows, the mechanism cannot be separated from missing exit history.

**exec-bot `0x6a72f618...33ee`**

- Factual: 3931 closed, 3870 held, huge OTHER-bucket PnL.
- Descriptive: only 52/3931 positions are attributed, so exit/timing surfaces are not representative.
- Inference: H3 is supported and overall verdict is `edge_detected` with `size_conviction`.
- Opaque: Whether the size-conviction signal is copyable depends on position-building and fill accessibility, which current surfaces do not model.

**HWR `0x22e4248b...ef1c`**

- Factual: 1083 closed, 534 held, positive PnL in all outcome buckets.
- Descriptive: pre-resolution and held categories both appear in the small attributed slice.
- Inference: H2/H3 inconclusive, verdict underpowered.
- Opaque: Cannot explain the high legacy win rate. Needs either better attribution coverage, tag baselines, or position-building detail.

**YatSen `0x5bffcf56...ffbe`**

- Factual: 860 closed, 682 held, large YES/NO profits, DW/EW divergence flagged.
- Descriptive: YES bucket PnL about $2.13M, NO bucket about $997K, OTHER about $183K.
- Inference: H2 supported, H3 refuted, verdict underpowered.
- Opaque: The protocol sees prediction signal but not enough validated primary/out-of-sample structure to make a copyable claim.

**Small insider `0x84933747...5366`**

- Factual: 272 closed, full attribution coverage, negative PnL.
- Descriptive: Timing v1 is `unclear` with high confidence; held and pre-resolution behavior both exist.
- Inference: H1 refuted, H2 refuted, H3 inconclusive, verdict underpowered.
- Opaque: This is the control case where opacity is low and the answer is simply weak/no evidence.

**Speculator `0x1eb06435...d25b`**

- Factual: 762 closed, 727 held, H2 supported.
- Descriptive: OTHER bucket dominates closed activity and PnL.
- Inference: `edge_detected`, primary edge type `entry_timing` through held-branch primary support.
- Opaque: Only 87/762 attributed rows. We can say the held prediction test supports edge; we cannot say the copy path is mechanically feasible.

**HWR medium `0xb3b50ef6...bef4`**

- Factual: 809 closed, 674 held, 257 attributed.
- Descriptive: NO bucket has very high WR but negative total PnL; OTHER is also negative.
- Inference: H3 strongly supported, `edge_detected`, `size_conviction`.
- Opaque: Positive equal-weighted ROI but negative dollar-weighted ROI means the copyability story depends on knowing position sizing and accumulation sequence.

---

## 3. The Five Questions

### Q1. Given a full ProtocolReport, what can we honestly claim?

1. Factual observations: counts, PnL, ROI, bucket totals, ledger flows, open marks, activity coverage, and hypothesis sample sizes.
2. Descriptive surfaces: outcome buckets, tag-grain financials, exit attribution, timing classification, drift/skew summaries, and completeness caveats.
3. Statistical inference: only branch hypotheses with verdicts, BH-adjusted p-values, CIs/permutation results, stationarity, and time-split status.
4. Interpretation: allowed only as a bounded inference. Example: "H3 supported, so larger held positions were more likely to win in this fixture replay" is valid. "Wallet is copyable" is not.

Important caveat: `primary_edge_type` can be non-null on reports whose
`overall_verdict` is not `edge_detected`. Treat it as explanatory metadata,
not as a standalone approval signal.

### Q2. Why is a profitable wallet profitable?

| Cause | Can confirm | Can describe | Cannot currently distinguish |
|---|---|---|---|
| Timing edge | H4 when execution_profile exists; FH6/H6 when price history exists. | Execution quality drift/skew; Timing v1 captured-travel class. | Live book availability, latency, and whether copier gets the same entry. |
| Size conviction | H3 / FH2 supported, with time-split context. | DW/EW divergence and position-size concentration. | Whether size was intentional or liquidity-constrained. |
| Tag specialism | H5 / FH4 when tag and peer baselines exist. | Edge Surface tag financials. | Missing tags, peer baseline staleness, causal tag selection. |
| Market repricing | H6 / FH6 with `price_history_fn`. | Timing v1 says future repricing is unavailable in v1. | Strict repricing until price-history validation exists. |
| Holding through resolution | H2 and Exit Attribution held buckets. | Timing v1 holding-driven if coverage supports it. | Whether holding was skill or passive exposure to favorable markets. |
| Liquidity opportunity | Execution profile skew/drift may hint. | Fill fragmentation and bucket PnL. | Order-book depth, slippage, queue priority, copy fill probability. |
| Luck | Refuted/inconclusive tests, failed time-splits, high variance flags. | DW/EW divergence and concentration caveats. | True luck vs unmodeled causal factor. |

### Q3. What patterns are copyable today?

Copyable enough to study, not auto-copy:

- Held prediction edge with supported H2 and acceptable completeness. This still needs entry-price, market, and sizing gates.
- Size-conviction wallets where H3 is supported, but only if position-building data shows we can observe the early accumulation before the edge is gone.
- Tag specialist behavior only when H5/FH4 is populated from tags and peer baselines; fixture-only runs often cannot ground this.
- Holding-driven wallets only when Timing v1 has enough attribution coverage. In the sample, most high-interest wallets are underpowered here.

Not copyable from current surfaces alone:

- Multi-fill accumulation patterns.
- Thin-book entry quality.
- Latency-sensitive flips.
- Slippage-sensitive positions.
- Order-book-depth dependent opportunities.
- Any strategy whose evidence is mostly in rows marked `insufficient_data`.

### Q4. What is opaque?

1. Per-fill position building: when accumulation starts, whether we can see the first actionable fill, whether later fills are worse, and whether size ramps predict conviction.
2. Slippage and market depth: current reports do not know whether a copier can enter with comparable price/size.
3. Strict future repricing: Timing v1 explicitly does not use price history; H6/FH6 require a validated price-history adapter.
4. Partial exits: Exit Attribution treats each position as a simplified BUY/SELL/resolution flow and does not model sell ladders.
5. Tag and peer coverage: H5/FH4 are only as good as tag lookup and peer baseline snapshots.
6. Activity history gaps: many fixture reports have low attribution coverage, making Timing v1 underpowered.
7. External capital flows: current CTF ledger is not wallet lifetime equity.
8. Causality: profitable market selection is observable; why the wallet selected it is not.

### Q5. Which workstream closes which gaps?

| Rank | Workstream | Load-bearing impact | Gaps closed | Does not close | Notes |
|---:|---|---:|---|---|---|
| 1 | Workstream 2 — position-builder / per-fill accumulation design | 5 | Accumulation start, fill sequence, size ramp, partial exits, copy-entry feasibility. | Future repricing, peer tags. | Required before simulator. Directly addresses the biggest opacity behind copyability. |
| 2 | Workstream 4 — clean intake / capture quality | 4 | Activity coverage, missing sells, attribution underpowering, fixture completeness. | Causal mechanism, strict repricing. | Many Timing v1 surfaces are blocked by insufficient attribution; cleaner intake raises every downstream surface. |
| 3 | Workstream 5 — strict repricing / v1.5 / H6 design | 4 | Future repricing, H6/FH6, market movement after exits, strict timing claims. | Copy fill feasibility, accumulation. | High value, but should follow adapter design and validation. |
| 4 | Workstream 3 — simulator | 3 | Copy PnL under explicit rules once position-building data exists. | Data quality and missing fill reconstruction. | Blocked by Workstream 2; otherwise it simulates on weak assumptions. |
| 5 | More broad wallet re-runs | 2 | More examples. | Structural opacity. | Do after capability gaps are reduced or a focused wallet question exists. |

Recommendation: **Workstream 2 next — position-builder / per-fill accumulation design.**

Reason: the current protocol can often say "this wallet made money" and
sometimes "H2 or H3 supports an edge." It usually cannot say whether a copier
can observe and enter the same position at comparable price/size. That is the
load-bearing gap between statistical wallet evidence and actionable copy rules.
It is also the explicit prerequisite for the simulator.

---

## 4. Gap Matrix

| Gap | Evidence from sampled reports | Blocks | Candidate closer | Priority |
|---|---|---|---|---:|
| Low attribution coverage | 7/8 sampled wallets have Timing v1 underpowered; examples: SwissMiss 235/975, exec-bot 52/3931, eatpraylove 27/1014. | Timing classification, exit-vs-hold claims. | Workstream 4 plus Workstream 2. | 5 |
| No per-fill accumulation model | H3-supported wallets still do not show when size ramp became visible. | Copyability of size conviction. | Workstream 2. | 5 |
| No slippage/order-book model | No surface records available book depth at copy time. | Realistic copy rules. | Workstream 2 first, then simulator. | 5 |
| No strict future repricing in Timing v1 | Timing Surface always reports `future_repricing_available=false`. H6/FH6 underpowered in sampled reports. | Market-repricing timing claims. | Workstream 5. | 4 |
| Partial sell ladders not modeled | Exit Attribution caveat: simplified position flow. | Trading-driven classification accuracy. | Workstream 2. | 4 |
| Tag/peer inference absent in fixture-only runs | H5/FH4 generally unavailable unless tag/peer surfaces exist. | Tag specialist claims. | Clean intake / replay surface capture. | 3 |
| Capital ledger is not lifetime equity | Accounting flags explicitly say portfolio value is current holdings, not lifetime equity. | Bankroll ROI claims. | Separate external funding ledger, not currently one of the five. | 3 |
| Overall verdict hides mechanism nuance | Edge wallets can be size-conviction or H2-driven while Timing is underpowered. | Operator interpretation. | Capability docs + report UX, not core infra. | 2 |

---

## 5. Final Recommendation

Pick **Workstream 2 — position-builder / per-fill accumulation design**.

Why it beats the alternatives:

- vs Workstream 3 simulator: simulator is downstream. Without accumulation and fill-sequence facts, it will encode assumptions as results.
- vs Workstream 4 clean intake: intake improves coverage, but it does not answer whether a profitable position was copyable at the moment we observed it.
- vs Workstream 5 strict repricing: repricing is important for H6/FH6, but most sampled wallets are held-heavy and copyability is blocked earlier by position construction and slippage.
- vs broad re-runs: more reports will repeat the same opacity unless the per-fill gap is closed.

Acceptance bar for Workstream 2 design:

- Define a position-builder surface that reconstructs per-position accumulation from fills.
- Emit first-fill, size-ramp, average-entry path, final size, time-to-final-size, and available copy windows.
- Represent partial sells and sell ladders without collapsing them into one SELL.
- State which fields are factual, which are inferred, and which are unavailable.
- Produce a fixture-replay validation plan before simulator work begins.
