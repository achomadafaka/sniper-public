# Desk narrative guesser: design (Lambda, 2026-10-27)

Status: **implemented shadow collector; GPT arm remains default OFF.** Sections 9–10 and the forward preregistration supersede the original build proposal below. Ordered by Alpha 2026-10-27. Gamma reviews the
measurement (§6). Paper only first. Builds on `2026-10-21-desk-narrative-seat-design.md` (tape-driven
`theme_heat`, `theme-taxonomy-v1`) and `2026-10-21-desk-offchain-heat-seat-design.md` (off-tape heat,
paid never blended with organic). This spec adds the forward-looking half: guess the coming theme and
its likely ticker/name BEFORE launches appear, then pre-load matching new launches into the watch
list.

## 1. Question and non-goals

Question: does a launch whose name/symbol matches a pre-registered narrative guess have a better
forward outcome (after costs, on Gamma's frozen grid) than a matched launch that does not?

Non-goals: no entry, no sizing, no seat bypass. A pre-loaded coin still passes every hard filter, the
JUDGE and the RISK seat. **J7 is context only, never a wallet signal and never a route for
candidates** (operator 10-02). The guesser never touches the money path; this spec adds no code on
the fill path.

## 2. Inputs (all already collected; no new provider call in v1)

| Input | Producer | Use |
|---|---|---|
| X posts, cashtags, `token_meta` mint links, `ai_suggestion` / `ai_suggestion_update` frames (J7's own tweet-to-ticker guess) | `memecoin_desk_x_producer.py` (J7 feed) | what is being talked about now, per account; J7's own guess is one more labelled input, not truth |
| News / headlines | existing news feed (`sniper/feeds/`) | exogenous events (people, brands, animals, memes) |
| Tape `theme_heat` | narrative seat | which themes already trade; saturation |
| Offchain heat (organic vs paid) | offchain seat | exclude paid-boost noise |
| Launch stream (name, symbol, creator, mint, first-seen `known_at`) | universe producer | the matching target |

## 3. Components

1. **Guesser (model, off the critical path, every 10 min).** Reads the last 60 min of §2 and writes
   at most N=5 `guess` rows: `theme_id` (taxonomy v1 or `new:<slug>`), `rationale` (short), up to 8
   `ticker_candidates` and 8 `name_candidates` (strings, lowercase-normalised), `confidence` 0-100,
   `expires_at` (default now+2 h), and the `evidence` ids it read. Append-only, never edited.
2. **Matcher (code, no model).** For each new launch: normalise name/symbol (case, punctuation,
   repeated letters, common suffixes `coin`, `inu`, `ai`), exact and edit-distance <=1 match against
   live guesses; also token overlap on `name_candidates`. Output `match_kind` in
   `{exact, fuzzy, theme_only, none}` plus the guess id. Pure function of (launch row, guess rows
   with `known_at` < launch `first_seen`).
3. **Watch-list preload (paper).** A matched launch is added to the desk watch list with
   `source='narrative_guess:<guess id>'`. It only ENTERS the existing watch/trigger pipeline; it
   gets no extra seat share, no filter relief, no floor/band relief. v1 caps preloads at 10/h so a
   bad guess cannot flood the 40/h JUDGE seat.

All rows land in `data/desk/narrative/narrative.db`, one writer per table (flock), `known_at` =
write time on the corrected clock, `source` tag on every row.

## 4. Anti-leakage rules (the core of the design)

- A guess is valid for a launch only if guess `known_at` < launch `first_seen_known_at`. A guess
  written after a coin exists can never match it (this kills "fit the winner after the fact").
- The guesser never receives the launch stream, prices, or any outcome. Its inputs are §2 minus the
  launch table. Theme saturation comes from tape heat computed before the guess time.
- The matcher is frozen code with a version hash; the normaliser list is part of the hash.
- Prompt, model id, N, expiry and thresholds are cohort-frozen (`narrative-guess-v1`); any change
  is a new cohort, not a mixed series.

## 5. Edge cases (CLAUDE.md rule 5)

1. **Money path.** None in v1: preload only feeds the watch list; no entry, exit, sizing or signing
   code is touched. Any later live use is a separate operator-approved spec.
2. **Speed on the fill path.** Guesser and matcher run in their own worker. The matcher is
   in-memory string work; the desk reads the preload flag from the watch row it already reads. No
   synchronous call is added to any entry path. Measured p50/p90 of matcher latency is a
   precondition for the build, not an estimate.
3. **Concurrency / global state.** Single writer per table; the matcher gets a copy of the live
   guess set per launch batch; no module attribute or env var mutated.
4. **Absent vs zero vs unknown.** No guess live = `match_kind='none'` with `guesser_state='ok'`.
   Guesser down / stale (>25 min since last successful run) = `guesser_state='stale'`; matched
   rows from before staleness are kept, new launches get `match_kind=NULL` (unknown), never
   `none`. Analyses treat NULL as censored, not as a control.
5. **Dependency failures.** Model timeout, rejection, rate limit, empty or malformed JSON: no guess
   row written, a `guesser_runs` row with the error class; an empty answer is never stored as "no
   theme". A feed down in the window is `missing` per input, and the guess row records which inputs
   were present.
6. **Units / identity.** Mint + network id are the identity; name/symbol are only match keys. Times
   are epoch seconds on the corrected clock (J7 transit skew: do not diff J7 `created_at` against
   host stamps). Ticker strings are lowercased once, in the matcher.
7. **The sample.** Denominator = all launches with `first_seen` inside a window where the guesser
   was `ok`. A launch that matches only because the guesser saw it trade (leakage) is impossible by
   §4. Preloaded coins are selected BY the guess, so their outcomes describe the guess plus our
   funnel; the comparison is vs matched controls (§6), not vs the desk's own book.
8. **Rollback.** One knob turns the preload off; guesses and matches keep being recorded
   (measurement continues). Half-applied state is harmless: preloaded rows are only extra watch
   candidates that die at their normal watch expiry.

Adverse-selection risk (explicit): a viral theme also attracts copy-cat and rug launches that copy
the name. Matching raises the share of squatters. The hard filters (farm/wash, rug, creator history
as a negative screen) are NOT relaxed for matched coins, and the read splits `exact` vs `fuzzy`
vs `theme_only`.

## 6. Measurement (Gamma reviews; preregistered before any read)

- **Unit:** one launch = one row (not a coin-day, not a trade).
- **Treated:** launches with `match_kind in (exact, fuzzy)` and the guess confidence stratum.
- **Controls:** for each treated launch, up to 3 non-matching launches from the same hour, same
  venue, same first-seen market-cap band, passing the same hard filters.
- **Outcomes:** (a) Gamma's frozen label P(touch +U before -D within H) on the 1 s grid, after
  costs, from the first watch-eligible moment; (b) guess precision = fraction of guesses with a
  later matching launch that reached the market-cap band at all; (c) coverage = share of the day's
  top launches (by 1 h peak) that a live guess matched ahead of time.
- **Tests:** treated minus control on (a), paired by stratum, bootstrap by launch; report n, CI,
  and censored share. No pooling across `match_kind`. No read before >= 30 treated launches per
  `match_kind` (same discipline as the other eval gates).
- **Kill / continue:** pre-registered with Gamma. Default: stop if (a) treated <= control at the
  point estimate after 100 treated launches, or if coverage (c) < 10% (the guess is not finding
  the winners).
- **Cost:** model spend per day and per treated launch is reported next to the effect; a positive
  effect that costs more than it earns is a no.

## 7. Open questions for Gamma / Alpha

1. Which model for the guesser (Haiku 5.5 as the other seats, or a stronger model once per hour
   for the theme and Haiku for candidates)? Proposal: cohort v1 = one model, compare in v2.
2. Is `theme_only` worth preloading at all, or is it recorded for measurement only? Proposal:
   `theme_only` is recorded, not preloaded, in v1.
3. Expiry 2 h: too long for fast memes? Proposal: keep 2 h, read precision by guess age.
4. Do the J7 `ai_suggestion` frames count as an independent guesser (compare ours vs J7's own)? Proposal:
   yes, record J7's guesses as `source='j7_ai_suggestion'` in the same table and score them with
   the same matcher; it is a free baseline.

## 8. Build order (after approval, not now)

1. Schema + matcher + frozen normaliser with unit tests (pure, offline).
2. Replay: run the guesser offline on the stored 10-22..10-26 feed windows, matcher on the stored
   launch tape, score vs Gamma's labels. This is the one-look read before any live worker.
3. Live worker, paper only, preload cap 10/h, knob default ON per the ship-with-kill-knob rule once
   the replay shows coverage above the floor.


## 9. Operator amendment: speed-first GPT model (2026-10-27)
The shadow model caller now targets `gpt-6-luna` with reasoning `low` through the isolated
`codex_cli` subscription adapter, which selects the bundled Codex CLI 0.164 route. Its new model cohort is `narrative-guess-gpt6-luna-low-v2`;
old Opus rows stay immutable and cannot supply new GPT matches or clean controls. The J7 baseline
keeps its original identity. The prompt and causal cutoffs are unchanged; the answer is constrained
by the existing field bounds in a strict JSON schema. See Amendment 3 of
`docs/audits/2026-10-27-narrative-guesser-forward-preregistration.md` for frozen counting and failure
rules. This is a shadow-only caller change: default OFF and `--no-model` remain authoritative,
with no preload or runtime activation in this amendment.

## 10. Current implementation status — 2026-10-27

The reviewed implementation is merged at `04e67e6e2928cf0844a734cc046bb874267ec0de`. Model selection for this arm is resolved by section 9. The older replay-before-live, preload and default-ON proposals in sections 7–8 are historical; the current contract is the outcome-blind **forward** preregistration at `docs/audits/2026-10-27-narrative-guesser-forward-preregistration.md`. There is no watch-list preload. Existing model slots never reroll, and historical Opus or foreign-cohort guesses cannot become current GPT matches or clean controls.

The J7-only worker was separately activated from immutable collector release `e92d1f2b3` with `--no-model`, a fresh error-free heartbeat and external state root. That launch cannot enable GPT through a configuration change. The new GPT caller has code/test and fixed-probe evidence only; enabling its model arm requires a separately authorized launch. J7 baseline identity and existing rows remain unchanged. No replay outcome, model skill, production latency percentile or trading claim is established here.
