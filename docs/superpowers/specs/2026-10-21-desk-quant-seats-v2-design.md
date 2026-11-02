# Desk seats v2: the desk as its own quant trader

Status: draft (Lambda, 2026-10-21). This supersedes the H-anchored framing in seats v1 and in
playbook v5-v8.

Operator, 2026-10-21, in Lambda's chat:
- "I want the LLM system to be its own quant trader and to make decisions based on what it
  sees, preferably beating people to the coin they'll later buy... that's why we need a
  narrative researcher, a runners researcher, a news and trending researcher."
- "See where we are going to use the GPT models, where the Claude and where we could integrate
  the local model... all hands on deck."

## 1. What changes

- **The desk leads, it does not follow.** The trigger is the universe (producer U). A roster
  wallet's buy is at most one fact on the sheet ("smart money already in"). It is never an
  anchor or a trigger.
- **The v7/v8 hard-filter rules are withdrawn.** Age < 120 s blocks the very early entries the
  operator wants. The young-chase rule is copy-relative. Neither ships. Playbook v9 removes
  them.
- **Facts measured against the roster** (`since_roster`, `quote_vs_roster_pct`,
  `swaps_since_source_*`) become optional H-only fields. U sheets use market-relative facts
  instead (section 3).

## 2. Seat roster and model per seat

The measured inputs behind the choice, all from 2026-10-21:

| Model | Wall per call | Strengths | Disqualifiers |
|---|---|---|---|
| Claude Haiku 5.5 (subscription, Agent SDK, one CLI spawn per call) | 4.6-6.9 s (p50/p90, time-of-day drift) | Refused 25/25 injected rows. Worked examples right. Strict JSON. | Quota shared with the crew. About 5 s, so it cannot judge every candidate. |
| GPT gpt-5.6-luna (subscription, `codex exec`) | 5.7-8.0 s | Schema-valid. A second, independent provider. | ~22k tokens per call (agent prompt). The read-only sandbox can still read `.env`, so it must never read untrusted text. |
| qwen3.5:9b (local, RTX 4070) | 0.64 / 0.68 s CRAWLER, 2.3 / 2.7 s JUDGE | Free and unmetered. Handles launch volume. Hype/suspicious/rug flag agreement 0.97-0.98. | 56% JUDGE agreement. Missed 5/25 injections. 68% intent agreement. |

| Seat | What it does | Input | Cadence | Model | Why this model |
|---|---|---|---|---|---|
| **Universe scanner** | Every Pump.fun trade: curve price, mcap, band entry, 1 s bars | gRPC tape (built, `lambda/desk-universe`) | every trade | code | deterministic |
| **Runners researcher** | Ranks coins that are taking off: buyer growth, unique makers, buy/sell ratio, 1 s/1 m momentum, new wallets, volume vs mcap | code-computed table of the top ~30 by a code score | every 30-60 s | **GPT luna**, Haiku as fallback | Its input is all numbers built by code, so the GPT sandbox risk does not apply. It is a slow cadence, so 6-8 s is fine. It keeps Claude quota for the JUDGE. |
| **Narrative bulk tagger** | Puts every launch name/symbol into a theme from a fixed taxonomy (e.g. ai_agent, cat, dog, political, celebrity, meme_of_the_day, copycat) | name, symbol, and description when present | every launch (~53/min) | **local qwen3.5:9b** | The only seat at launch volume (~77k/day), which the subscription cannot carry. A wrong label only moves an aggregate count. It never gates a trade, so its 5/25 injection misses cannot place a bet. |
| **Narrative researcher** | From the theme counts and their slopes, names which themes are heating up and how hot | code aggregates of the tagger output: per theme launches/min, buys/min, slope | every 5 min | **Haiku** | It turns aggregates into a judgment. The aggregates are numbers, but theme names come from a fixed list, so this is safe. |
| **News/trends researcher** | What is hot off-chain: X trending/cashtags, news headlines, Axiom trending | **untrusted text** | every 5-15 min | **Haiku only** | It reads untrusted text, so it must be the model that refused 25/25 injections, with no tools. Output is enum theme ids plus heat scores, never raw text (seats v1 §3). |
| **JUDGE (the trader)** | Per candidate: ENTER / WAIT / SKIP plus rails | coin sheet + runners rank + theme heat + news heat (all enums/numbers) | per shortlisted candidate | **Haiku** | Best judgment and injection behaviour measured. The deadline is 5 s. |
| **Checker / post-mortem** | Daily review of fills vs verdicts; proposes playbook edits to the crew as text | ledger + tickets (code-built) | daily | **GPT luna** (batch) | A second provider for an independent view; slow is fine. |
| **Code gates** | Safety kills, 5 s activity gate (H), caps | facts | per candidate | code | deterministic |

## 3. U fact sheet (market-relative)

These replace the roster-relative fields on U candidates:
- `price_vs_band_entry_pct`;
- `buyers_60s` and `buyers_growth_60s` (makers in the last 60 s vs the 60 s before);
- `buy_sell_usd_ratio_60s`;
- `vol_to_mcap_5m`;
- `largest_buyer_share_60s`;
- `runners_rank` (from the runners researcher, 1..30 or null);
- `theme` (enum) and `theme_heat` (0-100);
- `news_heat` (0-100 or null);
- `age_s`;
- `curve_pct`;
- `creator_prior_launches`;
- the safety block.

The roster fields stay on H sheets only. Every field keeps the
`{value, source, known_at, slot, completeness}` contract.

## 4. Volume: why the runners shortlist is mandatory

- Producer U measured about 6 band-entry pools per minute: ~8,600 candidates/day.
- One Haiku JUDGE call takes ~5 s and draws on a quota shared with the crew. Judging all of
  them is impossible.
- Funnel:
  1. code safety kills;
  2. the runners researcher's top-30 list, refreshed every 30-60 s;
  3. JUDGE only for candidates that are on the runners list, or whose theme heat is ≥ X.
- Target: ≤ ~300 JUDGE calls/day. That number is a knob (`MEMECOIN_DESK_SEAT_CALLS_PER_HOUR`,
  plumbing §2.0). Over budget, the result is a logged SKIP `budget`.

## 5. What exists vs what is missing

| Piece | State |
|---|---|
| Universe scanner + 1 s bars + snapshots | built (`lambda/desk-universe`), live smoke running 2026-10-21 |
| Ticket store, validator, renderer, cohort kill | Codex step 3 (`codex/desk-step-three`) |
| Runners table (code) | to build, on producer U's in-memory state (Lambda) |
| Local narrative tagger | to build. Needs a theme taxonomy and an agreement test vs Haiku on ~200 names. Ollama stays a foreground process until the operator decides how it runs. |
| X / news / Axiom feeds | all OFF. X needs an API key and its price checked. Axiom needs the operator's login and carries block risk. News RSS works keyless. Each is a separate operator decision. |
| Playbook v9 (U-led trader) | to write: remove the roster anchor and the v7 rules, add the market-relative fields and the researcher inputs |

## 5a. Operator feed rulings (2026-10-21, Lambda's chat; recorded by Alpha)

| Source | Ruling | Use |
|---|---|---|
| News RSS | no | none |
| Paid X API (official, twitterapi.io) | no | none |
| X posts via **J7's tweet feed** (two J7 accounts) | yes | Live posts from X accounts we choose; the account list comes from the operator. A separate Socket.IO service from J7's wallet relay; not wired today. |
| Grok | no; research only | Its edge is native X search (data access), not model quality. J7's tweet feed gives Claude/GPT the same posts. |
| Axiom | yes | Worker revived behind the operator's logged-in profile. Never cron'd. Halts on the first 403. |
| DexScreener | yes, **desk only** | The copy-bot ban stands. The desk rule follows this table. |
| Pump.fun trending / king of the hill | yes | Reader for Pump's attention lists. |

**DexScreener desk rule (Alpha-confirmed).**
- The desk may read DexScreener for CONTEXT only: trending, boosts/paid promotion, socials
  links, pair metadata, and as a cross-check of age or liquidity.
- Every DexScreener-sourced field carries `source='dexscreener'` and its `known_at` in the
  snapshot, so a read can split on it.
- Never price, fill or mark evidence. Never in any copy-lane or funded-exit path.
- A hard-filter fact whose only source is DexScreener is **null**, not a value.

Build order after the U capture is stable (Alpha):
1. J7 tweet feed as a producer of X posts. Only enums reach the sheet, via the Haiku news
   researcher.
2. Pump.fun trending/KOTH reader.
3. Axiom worker.
4. Additive decoder update for the two unknown Pump.fun discriminators.

## 5b. Departments: parallel desks

Several strategy desks run side by side. They share producers (U, H) and researchers (runners,
narrative, news/trends), and each owns:
- its own **cohort** (Codex's store isolates per cohort);
- its own **playbook** and prompt_version;
- its own **budget**: clip, max open and daily-loss halt. These sit inside the operator's desk
  totals (5 open / $200 daily / −$200 cohort) until the operator splits budgets;
- its own **SR arm**, so each department is read against its own random-admission baseline.

| Department | Scope | Trigger it reacts to | Distinct edge it tests |
|---|---|---|---|
| Early-launch | coins under ~10 min old, curve < ~50% | band entry + runners rank in the first minutes | being first, before roster wallets and copiers |
| Runner momentum | coins already moving: buyers and volume growth | runners top-30 | riding confirmed flow, not prediction |
| Narrative/theme | coins in themes whose heat is rising | theme_heat slope | buying the theme before the crowd rotates in |
| X-call / KOL | coins named by tracked X accounts (J7 tweet feed) | a post on a tracked account mentioning a coin | speed between a post and the market |

**Shared rules.**
- **Dedupe.** One coin can be a candidate for several departments. The ticket store keeps one
  open position per coin across the desk, so departments compete for it; the first ENTER wins
  and the others log `held_by_other_department`.
- **Researchers run once,** and every department reads the same snapshot.
- **A department is retired** if its 30-close read does not beat its own SR arm.

## 5c. Learning loop (PROPOSAL, not a build; Alpha takes it to the operator)

Operator direction, 2026-10-21: the desk loop should improve itself.

1. **Blinded post-mortem per closed ticket.**
   - The CHECKER seat (GPT, batch, daily) gets the frozen sheet, the verdict and the outcome
     path.
   - It never sees the playbook version or cohort, which is why the post-mortem is "blinded".
   - It writes a typed post-mortem: what the sheet showed, what happened, which rule helped or
     hurt, and a proposed rule text.
2. **Two pools come out of the post-mortems plus Gamma's labels.**
   - Few-shot pool: worked examples with outcomes, curated by code (balanced
     winners/losers/skips, never the newest week only).
   - Amendment queue: proposed playbook edits as text for the crew and the operator.
3. **Every playbook version is its own cohort arm.**
   - It runs at the $25 clip with its own −$200 kill, and the prior version runs as control.
   - Candidates are split by a fixed hash rule, never by the model.
4. **Promotion by a pre-registered weekly read.**
   - The new version replaces the control only if it beats it on full-book PnL. The read is
     frozen before the week starts.
   - Otherwise the version retires, with its post-mortems kept.
5. **The model never edits its own playbook live.** Amendments go through a commit, a new
   prompt_version and a new cohort, with the operator's yes for a funded arm.
6. **The code ranker (runners) is refit daily** on U labels, with the newest day held out. A
   refit ships only if it beats the current ranker on the held-out day. Every refit is versioned
   like a playbook.
7. **Guards.**
   - Small n is reported with n.
   - The fit set never overlaps the test set (the v7 lesson: rules tuned on rows whose labels
     were seen).
   - The few-shot pool never contains the test week.

## 5d. Verdict schema room for conditional entries (operator direction 2026-10-21)

- An entry mode with closed parameters is reserved now, so the caller needs no migration
  later: `entry_mode` ∈ {now, pullback, breakout}, default now. Bounds: `pullback_pct`,
  `breakout_pct`, `expiry_s`, validated like rails (outside the bounds = SKIP, never clamped).
- **Caller v1 executes `now` only.** pullback and breakout are recorded, then sized SKIP
  `entry_mode_unavailable` until the entry-trigger watcher exists. Beta builds that after the
  LP branch, on the step-5 candles.

## 6. Rule-5 notes for the new seats

- **Money path.** Only the JUDGE's verdict reaches the engine, and it passes the existing hard
  filter, rails bounds and cohort kill first. Researchers produce context fields only.
- **Speed.** Researchers run on their own cadence, off the decision path. The JUDGE reads their
  latest output with its `known_at`. Stale beyond a knob means the field is null, never reused
  silently.
- **Injection.** Only Haiku reads untrusted text, with no tools, and its output is enums. GPT
  and the local model read code-built numbers or names. A local mislabel only shifts an
  aggregate.
- **Unknown.** A researcher that is down gives null fields with completeness `missing`. The
  JUDGE plays on without them. That is not a reason to enter.
- **Sample.** Every researcher output is persisted with its inputs hash, so the 30-close read
  can ask whether runners_rank or theme_heat predicted anything.
