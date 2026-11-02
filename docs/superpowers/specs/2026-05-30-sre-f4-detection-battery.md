# SRE-F5–F10 — Detection battery

**Parent:** `2026-05-30-selection-reconstruction-engine-design.md`
**Purpose:** the independent tests run on `sre_decision_matrix`. Each emits a statistic + an interpretable verdict fragment that F12 synthesizes. Tests fan out (no shared state).

## Selection-vs-skip tests (within group)
- **T1 within-group discriminator** — per (group, feature): distribution of picked vs skipped (Mann-Whitney / KS / standardized mean diff + bootstrap CI). Output: ranked features that separate her picks. *Pattern:* "within underdog-NBA she takes lower-price + recent-dip members."
- **T1b directional** — sign & monotonicity of each separating feature (cheap-side? recent-drop? high-volume?). Distinguishes *fade* vs *follow*, *contrarian* vs *momentum*.
- **T2 selection-edge** — do her picks **out-resolve** the same-group skipped? Compare realized win-rate/ROI of picked vs a matched skipped sample (resolution known for both). *Crux:* if equal → no within-group edge (the group is the edge, or noise → likely human-class or survivorship); if picked ≫ skipped → real selection signal worth reconstructing.
- **T2b edge-decomposition** — is the edge in *which group* she's in (class selection) vs *which members within* (pick selection)? Variance decomposition across group vs within-group.

## Mechanism (bot vs human)
- **T3 coverage/exhaustiveness** — for each candidate predicate, `entered / matching` within the group. High (→1) + stable → mechanical; low/selective → judgment. Report the coverage curve over predicate tightness.
- **T4 threshold precision** — histogram of the separating feature at the decision boundary; sharp cliff (entries stop exactly at price X) → algorithmic; smooth → human. Quantify with boundary-sharpness / entropy.
- **T5 selection schedule** — hour-of-day & day-of-week of NEW-market entries (F1 decision grain); 24/7 uniform + entries in 2–6am local → automated; concentrated waking-hours + gaps → human. Metrics: hour-entropy, dead-hours fraction, weekend ratio.
- **T5b creation-latency** — `first_entry − created_at` distribution; sub-minute & tight → creation-sniper bot; hours & broad → human.
- **T4c determinism** — given the learned predicate (F11), fraction of entries satisfying it (precision) AND fraction of predicate-matches entered (coverage); both →1 = deterministic algorithm.

## Structure & dependency
- **T6 sequential dependency** — does entry of A predict entry of B (lift / Markov over event_family, template, basket)? Structured stream → bot or feed-driven.
- **T6b basket geometry** — within a basket, are members related (same event, same league, correlated outcomes)? co-entry graph + community detection.
- **T8 sizing-as-signal** — regress entry_size on the §F3 features; a feature that predicts her size is a *conviction variable* = a selection feature she acts on (even if she enters many, she bets big on the ones matching her real signal).

## Stability & externality
- **T7 stationarity** — does the discriminating feature/predicate hold across time windows (split-half, rolling)? Stable → algorithmic; regime-switching/drifting → adaptive/discretionary. (Reuse the protocol's CUSUM where possible.)
- **T9 external alignment** — cross-correlate NEW-market entry times with news/odds/sports-feed event times (F2 live-time for recent wallets); a tight lag spike → external-intel (flag, can't replay). Report lag, strength, and which feed.
- **T10 cross-wallet herding** — do other wallets enter the same markets within a short lag? Build the co-entry graph; leader/follower lag, shared-signal cluster. Distinguishes original signal vs copying vs common feed.

## Output
`sre_detection_results`: one row per (test, group) with statistic, CI, interpretable fragment, and a `confidence` + `data_fidelity` (from F2 tiers). Every fragment names the group and the scope it was computed at.

## Acceptance
1. On SwissMiss: T5 (new-market schedule) and T3 (selectivity) are computed at the *decision* grain and disagree with the old fill-grain classifier.
2. T2 returns "no within-group edge" vs "picked out-resolve skipped" with a CI, not a point estimate.
3. Every result carries its cf_scope + fidelity tier; sparse-tier results are flagged low-confidence.
