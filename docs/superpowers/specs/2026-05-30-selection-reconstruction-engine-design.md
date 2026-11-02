# Counterfactual Selection Engine — analyzer requirements (master design)

**Date:** 2026-05-30 · **Status:** BUILT (rev 2, post Codex review) — all 7 build-order steps shipped on `design/selection-reconstruction-engine`.
**Aka:** Selection-Reconstruction Engine (sre- file prefix retained).
**Build status (2026-05-30):** F0 guards · F1 entity_layers · F2 pit_market_context · F3/F4 matrix · F5–F10 detection · F11 rule_learner · F12 verdict · runner — all implemented under `sniper/analysis/counterfactual_selection/` (43 unit + 2 integration tests). Step 7 wired: `algo_discovery.stage_classify` prefers the CSE verdict when a `MarketContext` is supplied (`--cse-context`), behavioral heuristic as fallback. Data feed: `scripts/build_cse_pit_index.py` (Becker → MarketContext, block→epoch calibrated; trades carry no usable timestamp). Engine validated end-to-end on a planted-edge synthetic universe (recovers the rule → `rebuild_rule`; fabricates nothing on noise) **and** on the real Becker context (40 picks vs 1,986 point-in-time unselected candidates → honest `undetermined`). Remaining = data-ops only: the full 54GB index build + a forward-paper run per route. NO-side-engagement YES-inversion and recurring-candidate de-duplication are documented first-pass simplifications.
**Terminology:** the counterfactual is **"unselected candidate markets"** (markets *available and comparable at T*), NOT "skipped" — we usually cannot know the wallet saw and rejected them.
**Why:** the behavioral classifier conflates execution-automation (laddering) with selection-automation (the edge). To tell human selection from algorithmic selection — and, if algorithmic, to reverse-engineer the rule — the analyzer must organize a wallet's history into **many homogeneous logical groups**, enumerate the **counterfactual** (comparable markets at T it did NOT select), and run a battery of contrast tests within and across those groups. This doc enumerates everything to compute; the per-area specs (this directory) detail each module. **`sre-f0-methodology-guards.md` is mandatory and cross-cutting** — candidate-universe quality, matching balance, leakage/time-scope tags, multiple-testing/FDR, negative controls, and a four-dimension evidence output (not a human/bot label). Read F0 first.

## 0. Core principle

> A wallet's edge is **where its picks differ from the matched counterfactual it skipped** — and its *nature* (human vs algorithmic) is **how exhaustively, how precisely, and on what schedule** that difference is applied. Everything below exists to make those two questions measurable.

Three nested counterfactual questions, weakest → strongest:
1. picks vs the **world** (all markets at T) — rediscovers the obvious; necessary but not sufficient.
2. picks vs the **same type** (matched group) at T — isolates *within-class* selection.
3. picks vs the **same event** / nearest-neighbor markets — isolates the *actual discriminator*.

## 1. Entity layers (reconstruct ALL of these from raw fills)

Today we stop at "position". The engine needs the full ladder so groups can be formed at every grain.

| Layer | Definition | Why it matters |
|---|---|---|
| Fill | one execution | microstructure / maker-taker |
| Clip | a burst of fills = one *add decision* | separates "decisions" from "executions" |
| Position | net per outcome-token | current unit |
| **Engagement** | a wallet's full involvement with one condition (first-entry→adds→exits→resolution) | the unit of *selection* (one market chosen) |
| Outcome-leg | YES vs NO leg within a market | side selection |
| **Event-engagement** | across all outcomes of one event | which outcome of an event was chosen |
| **Basket** | markets entered within a short window / correlated | scanning vs single-shot; co-selection |
| **Session** | a contiguous activity window | "when does it decide" |
| **Campaign** | a thematic run (e.g. a week of NBA underdogs) | regime / strategy episodes |
| Lifetime | the whole wallet | drift / stationarity |

## 2. Grouping / stratification axes (stackable → logical groups)

A "logical group" is any stack of these with ≥ min picked AND ≥ min unpicked members. **Detection happens inside groups.**

**Market-intrinsic:** category · subcategory/league/tag · event_family · market_type (binary/multi/scalar) · outcome_role (favorite/underdog/named/yes/no) · favorite_rank (1st/2nd/… favorite) · resolution_source/oracle · market_template (who-wins / over-under / will-X-happen / range).
**Price / probability:** entry-price decile · semantic band (longshot <0.15 · underdog 0.15–0.45 · tossup 0.45–0.55 · favorite 0.55–0.85 · near-cert >0.85) · extremity = |price−0.5|.
**Time:** time-to-resolution bucket (intraday/days/weeks/months) · market-age-at-entry (fresh<1h/day/mature/stale) · creation-cohort · entry hour-of-day · entry day-of-week · pre/in/post scheduled-event (sports).
**Liquidity / crowd:** volume tier · liquidity/depth tier · #prior-trades-in-market (crowdedness) · #other-wallets-already-in.
**Wallet-relative:** nth-market-entered (career stage) · size band vs own distribution · conviction percentile.
**Outcome (label only — never a selection feature):** won/lost · ROI band · hold duration.

## 3. Counterfactual scopes (picks vs unpicked — tightening)

| Scope | Counterfactual = unpicked markets that, at her entry time T, were… | Strength |
|---|---|---|
| C0 | …anything tradeable | weak (rediscovers the obvious) |
| C1 | …same category | low |
| C2 | …same (category × market_type × outcome_role) | medium |
| C3 | …C2 + same price band | high |
| C4 | …**other outcomes of the SAME event** she skipped | very high |
| C5 | …same creation-cohort (opened alongside the ones she took) | high |
| C6 | …same volume/liquidity tier | medium |
| C7 | …**nearest-neighbor** unpicked market on all intrinsic features (propensity match) | highest |

C4 and C7 are where "why these and not those" actually lives.

## 4. Per-engagement features the analyzer must compute

**Price-path:** entry price · price at −1h/−6h/−24h (pre-entry drift) · pre-entry velocity & acceleration · entered-on-dip/spike/reversal/trend flag · pre-entry volatility · distance from market min/max & from session VWAP.
**Timing:** latency from market creation → her first entry · time-to-resolution at entry · fraction-of-market-life elapsed at entry · NEW-market entry hour/day · inter-new-market gap.
**Microstructure / execution (reveals automation, not selection):** fills-per-engagement · clip cadence · accumulation duration · ladder shape (uniform/escalating/de-escalating) · adds-on-adverse vs adds-on-favorable · maker/taker · aggressive/passive.
**Sizing:** entry size · total size · size vs own median (conviction) · size as a function of features.
**Relational:** co-entered markets (basket membership) · order-of-entry within an event · entry-of-A→entry-of-B lift.
**Post-hoc (label only):** resolved win/loss · ROI · **resolution of the picked vs the unpicked same-group** (the edge).

## 5. The test battery (per area spec)

| # | Test | Question | Bot/human or edge signal |
|---|---|---|---|
| T1 | Within-group discriminator | which feature separates picked from same-group unpicked? | reveals what she selects ON |
| T2 | **Selection-edge** | do her picks out-resolve the same-group unpicked? | if NOT → the group is the edge / it's noise; if YES → real selection signal worth reconstructing |
| T3 | Coverage / exhaustiveness | what fraction of a rule-matching group did she enter? | high → mechanical bot; low/selective → judgment |
| T4 | Threshold precision | are entry conditions sharp (price ≤ X to the cent) or fuzzy? | sharp → algorithmic |
| T5 | Selection schedule | are NEW-market entries 24/7 + sub-second-from-creation, or waking-hours + irregular? | 24/7+fast → automated selection; waking+irregular → human |
| T6 | Sequential dependency | does one entry predict the next (basket logic / following a stream)? | structured stream → bot/feed |
| T7 | Stationarity | does the discriminating rule persist across time, or drift/regime-switch? | stable rule → algorithmic; drifting → discretionary/adaptive |
| T8 | Sizing-as-signal | does she size up on a specific computable feature? | reveals the conviction variable = a selection feature |
| T9 | External alignment | do entry times cross-correlate with news/odds/cross-venue moves? | aligned → external-intel (flag, can't replay) |
| T10 | Cross-wallet herding | do other wallets hit the same markets at the same time? | shared signal / copy network |

## 6. Logical patterns we're trying to surface (concrete)

These are the *outputs* the battery should be able to state plainly:
- "Within NBA underdogs 0.20–0.35 resolving <48h, she takes the ones that **dropped >5% in the last 6h**" → dip-buy selection rule (algorithmic, replicable).
- "Within an event she **always takes the 2nd-favorite**, never favorite/longshot" → structural rule.
- "She enters **~90%** of all [politics · price<0.10 · age<1h] markets" → exhaustive longshot-sniper bot.
- "Her picks resolve **58% vs 41%** for same-group unpicked" → real selection edge (reconstruct it).
- "New-market entries cluster **9am–11pm ET, none 2–6am**, irregular gaps" → human selection.
- "Entries **within 3s of market creation, 24/7**" → creation-sniper bot.
- "Entries spike **within 10 min of ESPN score updates**" → live-event reactive (external-intel).
- "She **co-enters all 3 related markets within 60s**" → correlated-basket bot.
- "No feature separates picks from same-group unpicked, low coverage, waking-hours" → **human judgment, copy don't rebuild**.

## 7. Engine modules (one spec per area, this directory)

| Module | Spec | Produces |
|---|---|---|
| **F0 methodology guards** | `…-f0-methodology-guards.md` | **MANDATORY** — candidate-quality, matching balance, leakage tags, FDR/multiple-testing, negative controls, 4-dim evidence output, report guard |
| F1 Entity layering | `…-f1-entity-layering.md` | clips/engagements/event-engagements/baskets/sessions/campaigns tables |
| F2 Point-in-time market context | `…-f2-pit-market-context.md` | candidate-universe-at-T + per-market state (price/age/volume) — the counterfactual foundation |
| F3 Stratification engine | `…-f3-stratification-counterfactual.md` | logical groups (§2) with picked+unpicked members (§3 scopes) |
| F4 Engagement features | `…-f3-stratification-counterfactual.md` | the §4 feature columns per engagement + per unpicked market |
| F5–F10 Detection battery | `…-f4-detection-battery.md` | T1–T10 results |
| F11 Selection-rule learner | `…-f5-rule-learner-and-verdict.md` | the predicate(s) separating picks from non-picks within strata, w/ precision+coverage |
| F12 Bot-vs-human verdict | `…-f5-rule-learner-and-verdict.md` | calibrated, explained verdict (human-selection / algorithmic-selection{self-contained,external-intel} / undetermined) + route |

## 8. Data dependencies

- F2 (point-in-time universe) is the gating dependency — needs the block→time index + token→condition map + per-market created_at/end_date + nearest-trade price (the 2026-05-29 feasibility study: PARTIAL-GO with this preprocessing). Without F2 there is no counterfactual, so the whole engine collapses to descriptive self-analysis (today's grains).
- F9/T9 (external alignment) needs the news/odds/sports feed snapshots already ingested.
- T10 (herding) needs other wallets' activity (followed_wallet_activity / Becker).

## 9. Honest ceiling

This yields **strong, accumulating evidence**, not certainty. Output is **four independent evidence dimensions** (F0/G7: selection_edge · rule_extractability · automation_evidence · human_discretion_evidence), each with confidence + data-fidelity — the human-vs-bot sentence is a *downstream qualitative note*, never the product.

Two limits are explicit and load-bearing:
- **Observed-only (G8):** matching balances only *observed* features, so "no separating predicate" is ambiguous. If a real selection **edge** exists but no observed rule explains it → `algorithmic_unobserved_signal` (private/external data we can't see → flag, can't replicate), NOT "human". Only edge-absent + no-rule + irregular schedule + selective leans human.
- **Intent is unprovable (G9):** a bot can mimic human irregularity and a human can run scripts. The report never claims "proved human/bot/the true algorithm". Final arbiter remains forward paper.

## 10. Build order

1. **F1 + F2** entity layering (cheap) + point-in-time market context (the gate) — in parallel.
2. **F0 candidate-quality + matching-balance gates (G1/G2)** on top of F2 — prove the counterfactual is valid before trusting any comparison.
3. **F3/F4** stratification + counterfactual + feature matrix (with G3 leakage time-scope tags from the start).
4. **F0 leakage (G3), multiple-testing (G4), negative-controls (G5)** infrastructure — must exist and PASS on a placebo dataset before any rule is trusted.
5. **F5–F10** detection battery (fan out; every result carries candidate-quality + FDR + placebo status).
6. **F11/F12** rule learner + verdict — **HARD-GATED**: do not build until G2/G3/G4/G5 pass. Promotion (discovery→thesis) requires min-sample + effect-size + FDR + temporal OOS (G4).
7. Wire the four-dimension verdict into algo_discovery's classify→route (replaces the execution-only classifier).
