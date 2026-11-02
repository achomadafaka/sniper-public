# SRE-F0 — Methodology guards (mandatory, cross-cutting)

**Parent:** `2026-05-30-selection-reconstruction-engine-design.md`
**Origin:** integrates the Codex design review (2026-05-30). These guards are NOT optional — the rule learner (F11) and verdict (F12) MUST NOT be built or promote any result until every guard here is enforced. Without them the engine becomes a false-discovery generator.

## G0 — Terminology
Use **"unselected candidate markets"**, never "skipped". "Skipped" wrongly implies the wallet saw and rejected them; we usually cannot know she saw them, that they existed at the same time, or that they were liquid/in-domain. The counterfactual set is strictly: *markets available at the same point in time and comparable under the selected scope.*

## G1 — Candidate-universe quality (per counterfactual scope, every test)
Emit with every comparison; if weak, output `counterfactual_underpowered` instead of a rule:
`n_picked · n_unselected_candidates · coverage_ratio · candidate_source · point_in_time_verified(bool) · price_available_share · liquidity_available_share · resolution_available_share · scope_quality{ok|sparse|invalid}`.

## G2 — Matching balance diagnostics (C7 nearest-neighbor)
Matching reduces, never eliminates, confounding — and only over OBSERVED features. Emit:
`standardized_mean_diff_before/after · matching_distance_dist · n_unmatched_picks · n_reused_controls · caliper_pass(bool)`.
If balance poor → `nearest_neighbor_scope_invalid`; never claim "selection edge" on unbalanced groups.

## G3 — Leakage / lookahead discipline
Every feature carries `time_scope ∈ {pre_engagement, at_engagement, post_engagement_label_only, forbidden_lookahead}`. **Only `pre_engagement` + `at_engagement` may inform a selection rule.** `post_engagement_label_only` (resolution, ROI, post-entry price move) is for the edge test (T2) ONLY. Forbidden examples to actively block: future volume, final outcome, post-entry price movement, price/volume rank computed from future prices, category/tag assigned at resolution, liquidity-after-pick. Apply purging/embargo when label windows overlap candidate windows.

## G4 — Multiple-testing / false-discovery control
The battery runs many (test × feature × scope × group × window) combinations → false positives by luck. Mandatory per run: `n_tests · test_families · raw_p · BH_adjusted_q (FDR) · effect_size · min_sample_met(bool)`. **Promotion gate (discovery → thesis):** a rule is promoted ONLY if it clears min-sample AND an effect-size floor AND FDR correction AND a **temporal out-of-sample** confirmation (fit on earlier window, confirm on later). No exceptions.

## G5 — Negative controls / placebo (must pass or the battery is too permissive)
Run alongside every real test: (a) shuffle wallet pick/skip labels; (b) shuffle picked/unselected within a time block; (c) inject a future-impossible feature and verify G3 BLOCKS it; (d) run the discovered rule on random wallets; (e) run it on an adjacent unrelated category. If "edge"/"rule" appears in any placebo at the same strength → flag `battery_too_permissive` and suppress promotions.

## G6 — Microstructure / trade-direction sourcing
Trade direction MUST come from on-chain OrderFilled / asset-id (which our `fetch.py` already does), NOT order-book inference (public-feed direction agrees with ground truth only slightly above chance). Any order-book-derived direction/spread feature is `diagnostic_only`, never a rule input.

## G7 — Output is four evidence dimensions, not a label
Primary product (each independently scored, with confidence + data_fidelity):
- `selection_edge` ∈ {supported, weak, contradicted, underpowered} — picks out-resolve matched unselected? (T2)
- `rule_extractability` ∈ {high, medium, low, underpowered} — does a stable, OOS-confirmed predicate separate picks? (F11)
- `automation_evidence` ∈ {high, medium, low, underpowered} — coverage/schedule/creation-latency/threshold (T3/T4/T5)
- `human_discretion_evidence` ∈ {high, medium, low, underpowered} — selectivity/irregular schedule/drift/no-separator
The human-vs-bot sentence is a **downstream qualitative note** derived from these, never the headline.

## G8 — Observed vs unobserved (disambiguating "no rule")
Matching balances only OBSERVED features, so "no separating observed predicate" is ambiguous. Disambiguate with the edge test:
- selection_edge **supported** + rule_extractability **low** → `algorithmic_unobserved_signal`: a real edge driven by data we can't see (private/external/cross-venue). Route `flag_human_ai`; do NOT call it human, and do NOT claim we have the rule.
- selection_edge **contradicted/weak** + rule_extractability **low** + irregular schedule + selective → `human_or_noise` (lean human judgment; copy_mirror + forward paper to separate skill from survivorship).
T9 (external alignment) further splits `algorithmic_unobserved_signal` into external-intel vs unknown-private.

## G9 — Report language guard (hard rule)
The report must NEVER say "we proved this is a human / a bot / the true algorithm." It says: *evidence supports a stable selection rule / automation evidence is high / human-discretion evidence is medium / requires forward paper validation.* Data cannot prove intent; a bot can mimic human irregularity and a human can run scripts. Confidence is always capped and the strongest counter-evidence is always reported.

## Build-order gate
Build F1 + F2 + the G1/G2 candidate-quality gates first. Do NOT build F11 (rule learner) or F12 (verdict) until G2 (matching balance), G3 (leakage), G4 (multiple-testing), and G5 (negative controls) exist and pass on a placebo dataset.
