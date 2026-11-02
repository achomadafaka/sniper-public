---
memory_name: methodology/wallet-protocol-v2-gaps-from-eatpraylove
memory_type: knowledge
memory_description: Gap analysis for wallet-analysis-protocol-v2 from eatpraylove case (1383 positions, +$449K realized, classified underpowered). Five amendments enumerated, ship-as-v3 recommended, literature pointers included.
memory_tags: [methodology, protocol, wallet, gap-analysis, v2, v3-candidates]
---

# Wallet Protocol v2 — Gap Analysis (from eatpraylove)

**Date:** 2026-04-25
**Wallet:** `0xc02147dee42356b7a4edbb1c35ac4ffa95f61fa8` (eatpraylove)
**Case facts:** 1383 closed positions, +$449K realized, classified `underpowered`. Full-sample H2 REFUTED (-30pp). H2_sub080 SUPPORTED (+2.7pp) but time-split failed. H1 CI [-$294K, $1.4M] straddles zero.

---

## Section 1: H2_sub080 gap — confirmed

Your read is correct. The gap is real and is both a spec gap and a code gap:

**Spec gap.** v2 §11 defines "supported" as `BH-adjusted p < 0.10 for at least one of {H2, H3, H5}`. H2_sub080 is not in that set. The verdict assembly in `analyze_wallet.py:43` (`HELD_PRIMARY_TESTS = ("H2", "H3", "H5")`) is faithful to the spec, making this a protocol defect, not an implementation defect. Fixing only the code tuple while leaving §11 unpatched puts code and spec out of sync — both must change together in v3.

**Code gap (secondary).** `held_analyst.py:720-724` runs the time-split loop for `H2` and `H3` only. H2_sub080 has no time-split path. If H2_sub080 is promoted to primary, the time-split loop must be extended to include it, otherwise it will always skip the OOS gate when time-split is eligible.

**Recommended fix:** Two-step rule in v3 §11 and §6.3.
1. When full-sample H2 is REFUTED and H2_sub080 has n >= H2_MIN_N: treat H2_sub080 as the canonical H2. Add to primary pool. Label as `entry_timing_sub080` edge type.
2. Add to report flag: `CERTAINTY_BUYER_DETECTED` — "Full-sample H2 refuted; sub-0.80 sample used per ADR-002. If H2_sub080 also refuted, wallet is neither an informed trader nor a certainty-buyer with edge."

Do not introduce a bespoke "certainty_buyer_with_thin_edge" label — it requires a new inference tier. The two-step rule is cheaper and follows directly from ADR-002.

---

## Section 2: Missing tests

**A. H1 robust statistic (highest priority)**
*What it tests:* Whether mean per-position EV is positive, using a statistic that degrades gracefully under kurtosis-918 tail distributions.
*Eatpraylove justification:* H1's bootstrap-on-sums over 1383 positions produces CI [-$294K, $1.4M] on +$449K because 3-5 fat-tail positions dominate each resample. This is not a power problem — it is a known failure mode of percentile bootstrap under heavy tails. H1 staying `inconclusive` blocks `edge_detected` in §11 even if H2_sub080 is promoted.
*Implementation sketch:* In `held_analyst.py:run_h1_net_pnl`, add a second statistic: Winsorized mean (5th/95th percentile clip). Bootstrap the Winsorized mean separately. Report both. Gate on the Winsorized statistic, label it H1_winsorized. Alternatively replace the default with a studentized bootstrap (Hall 1992). Primary change in `statistics.py:bootstrap_ci`.
*Literature:* Hall (1992) ch. 3-4; Wilcox (2012) ch. 4 (Winsorized bootstrap); Politis & Romano (1999) for subsampling under heavy tails.

**B. F2 → gate (not flag)**
*What it tests:* Whether the H1 bootstrap is representatively sampling the wallet's positions or is dominated by 1-3 outlier bets.
*Eatpraylove justification:* F2 (top-3 > 70% of PnL) currently fires as a caution flag with no procedural consequence. If eatpraylove's top-3 positions dominate, the H1 bootstrap is fundamentally unrepresentative — every resample includes or excludes that bet at 50/50, not at the true frequency.
*Implementation sketch:* In `stage1_gates.py` or at the top of `run_h1_net_pnl`: if F2 flag is active, split H1 into H1_all and H1_sub_top3 (exclude the top-3 PnL positions). If H1_sub_top3 CI excludes zero (positive) and H1_all is inconclusive, flag `EDGE_CONDITIONAL_ON_TOP3_EXCLUSION`. This does not change the primary verdict but prevents one outsized bet from masking a genuine small-position edge — or vice versa.
*Literature:* Mitts & Ofir SSRN §4.2 on outlier-trade contamination in prediction-market wallet analysis.

**C. Time-segmented H1 (per-regime P&L split)**
*What it tests:* Whether the edge was concentrated in an early regime (e.g. great in 2024, mediocre in 2025) that CUSUM at h=25 misses due to its intentionally wide control limit.
*Eatpraylove justification:* H7 CUSUM is calibrated to avoid false alarms on heavy-tail P&L. v2 §13 limitation #16 explicitly admits this leaves gradual strategy drift undetectable. The 70/30 time-split for H2/H3 partly addresses this, but H1 (the gate) has no time-split at all.
*Implementation sketch:* In `held_analyst.py:run_held_branch`, after time_split computation, also run H1 on the last-30% test subset. Report `H1_test_split` as a diagnostic (not a primary). If H1_test_split CI straddles zero while H1_full is supported, add flag `EDGE_MAY_BE_FRONT_LOADED`.
*Literature:* Aue & Horváth (2013) §3 on structural-break power under fat tails; directly applicable to why h=25 misses slow drift.

**D. Dollar-weighted vs equal-weighted P&L diagnostic**
*What it tests:* Whether the wallet's edge scales with capital deployed (smart allocation) or is noise-driven by a handful of lucky bets.
*Eatpraylove justification:* A wallet with one $200K bet and 1000 $50 bets looks different from a wallet with 1000 $300 bets, but H1's equal-weighted bootstrap treats them identically. Dollar-weighted IRR normalizes by capital actually at risk.
*Implementation sketch:* Compute dollar-weighted IRR from `(realized_pnl / entry_notional)` per position, then weight the bootstrap by entry_notional. Report as `H1_dollar_weighted` diagnostic. Does not replace H1 — it complements. Primary change: add `dollar_weighted_roi_bootstrap` helper in `statistics.py`.
*Literature:* Dichev (2007) AER 97(1) "What are stock investors' actual historical returns?" — the definitive treatment of dollar-weighted vs time-weighted for evaluating investor skill.

**E. Entry-price-bucket baseline for high certainty-buyer wallets (spec-only)**
*What it tests:* Whether a wallet buying at 0.85+ is beating a true bucket baseline (not just the market-implied probability, which is the same as the price they paid).
*Eatpraylove justification:* H2's current null is `mean(is_win - avgPrice)` — sensible for a diversified trader, but degenerate for a wallet that exclusively buys at 0.85-0.95. The null is trivially close to zero because `avgPrice ≈ 0.87` means the null expects 87% WR, and most certainty positions DO resolve at 100%. The test is answered by the entry price distribution, not the trader's skill.
*Implementation sketch:* Spec change only for v3. If `CERTAINTY_BUYER_DETECTED` and fraction of held_positions with entry_price >= 0.85 > 0.50: replace H2 null with the empirical population WR for positions in the [0.85, 1.0) entry-price bucket from B1 peers. This is the correct baseline — does this wallet beat the average person who also bought at 0.87?
*Literature:* Wolfers & Zitzewitz (2004) JEP 18(2) §3 on calibration at high probabilities; Manski (2006) on certainty-equivalent prediction.

---

## Section 3: Ship-as-v3 vs separate diagnostic layer

Ship as v3. The "extended diagnostics" framing is an escape hatch that breaks the pre-registration discipline that gives this protocol its value. If v2 returns `underpowered` on a 1383-position +$449K wallet, v2 is wrong. The fix belongs in the canonical protocol.

Ordering for v3 scope: A (H1 robust statistic) and B (F2 → gate) are the highest-leverage changes and are small. C (H1 time-split) is a one-function addition. D (dollar-weighted) is a diagnostic addition, safe to ship alongside. E (certainty-buyer bucket baseline) is a spec-only change requiring no new data. All five fit in one revision.

One discipline constraint: v3 must update both the spec document (§11, §6.1 H2, §3.2 F2) AND the code (`analyze_wallet.py`, `held_analyst.py`, `stage1_gates.py`, `statistics.py`). Amendments that touch only one layer will be found out on the next anomalous wallet.

---

## Section 4: Literature pointers

| Amendment | Primary citation |
|-----------|-----------------|
| H1 Winsorized/studentized bootstrap | Hall (1992) *Bootstrap and Edgeworth Expansion* ch. 3-4; Wilcox (2012) *Robust Estimation* ch. 4 |
| H1 subsampling for heavy tails | Politis & Romano (1999) *Subsampling* §2 |
| F2 → gate (outlier-trade contamination) | Mitts & Ofir SSRN §4.2 |
| Time-segmented H1 (h=25 slow-drift blind spot) | Aue & Horváth (2013) *J. Time Series Anal.* 34(1) §3 |
| Dollar-weighted IRR | Dichev (2007) *AER* 97(1) |
| Certainty-buyer bucket baseline | Wolfers & Zitzewitz (2004) *JEP* 18(2) §3 |

