# Desk narrative / theme seat: tagger, aggregates, researcher, stamp (Lambda, 2026-10-21)

Status: **design for Gamma and Alpha; no code until agreed.** Source: seats v2
(`docs/superpowers/specs/2026-10-21-desk-quant-seats-v2-design.md` §2 seat table, §3 `theme` /
`theme_heat` / `news_heat`, §6), operator rulings 4c930f3a (feeds), 3c38747f (one local model,
qwen3.5:9b, CRAWLER gates), and the CRAWLER precedent
(`docs/superpowers/specs/2026-10-21-desk-crawler-seat-design.md`: causal cache, stamped by U at
emission, version in the cohort policy).

## 1. Pieces

| Piece | Model | Reads | Writes | Cadence |
|---|---|---|---|---|
| **Bulk tagger** | local qwen3.5:9b (Ollama), Haiku nowhere | each U launch's `symbol` + `name` (creator-controlled, fenced) | one `theme` enum per mint, with `known_at` | every launch: about 46/min measured (2,768 in the hour to 16:5xZ) |
| **Copycat flag** | code | U launches: same upper-cased symbol as an earlier launch in the last 24 h | `copycat` bool per mint | every launch |
| **Theme aggregates** | code | tagger output joined with the U tape, the trending board, X producer mention counts, DexScreener metas | per theme: launches, band entries, buy USD, unique buyers over 5/15/60 min; slope = 15 min rate / prior 60 min rate | every 60 s |
| **Narrative researcher** | Haiku 5.5, no tools | the aggregates table (theme ids + numbers) plus, fenced, DexScreener trending meta names and the top X cashtags | `theme_heat` 0-100 per theme, `offchain_heat` 0-100 or null per theme | every 5 min |

A bulk tag never gates a trade. It only moves an aggregate count, so a mislabel (including a
successful injection inside a coin name) shifts one theme's count by one coin.

## 2. Taxonomy `theme-taxonomy-v1` (closed enum)

`ai_agent`, `animal_cat`, `animal_dog`, `animal_other`, `political`, `celebrity`, `crypto_meta`
(BTC/SOL/Pump/exchange references), `tech_brand`, `food`, `sports`, `anime_gaming`, `holiday_event`,
`other`, `unknown`.

`copycat` is not a theme. It is a code flag, because the model cannot know the other coins. A model
answer outside the enum is stored as `unknown` with error `off_enum`, never mapped.

## 3. Local tagger gate (operator 3c38747f, CRAWLER role gates)

- **Blind set:** 200 launch names drawn by `sha256('theme-gate-v1|' + mint)` from one day of U
  launches, labelled by Haiku (the reference) and by qwen, each without seeing the other.
- **Pass:** at least 90 % exact enum agreement, and wall-clock p90 under 1 s per name.
- **Injection rows:** 25 names carrying an instruction. Pass = 25/25 stay inside the enum, so the
  validator never has to fix one, and no output text leaves the enum.
- **Fail:** no local tagger. `theme` is then null (`absent`) for every coin. Haiku cannot carry
  about 66k names a day, and the seat does not fall back to it.
- **Running Ollama as a service is an operator decision** (seats v2 §5: "Ollama stays a
  foreground process until the operator decides how it runs"). The tagger worker calls the local
  HTTP API, and never starts, stops or configures Ollama.

## 4. Causality and the stamp

Same pattern as the CRAWLER. Every row is append-only with `known_at` = when it was computed, in
`data/desk/narrative/narrative.db` (one writer per table, flock).

At emission, U stamps for the coin:
- `theme`: the coin's tag row with `known_at <= emission_at`; else null with completeness `absent`
  (not tagged yet) or `missing` (tagger failed or is down);
- `theme_heat`: the newest researcher row with `known_at <= emission_at` and at most 15 min old,
  for that theme; else null `missing`;
- `news_heat`: that row's `offchain_heat` for the theme, or null.

Each source is `narrative:<row id>:<known_at>`. A coin tagged after its emission never reaches that
snapshot.

## 5. HARD ORDERING RULE (contract)

`memecoin_desk_contract.validate_candidate` refuses any snapshot field outside its fixed `_FIELDS`
set (`snapshot_shape`), and `theme`, `theme_heat` and `news_heat` are not in it today. A U that
stamps them without the matching contract change would get **every emission REFUSED**. So:

1. The contract change (Codex's frozen code) adds the three fields with their value rules: `theme`
   in the taxonomy enum, `theme_heat` and `news_heat` integers 0-100.
2. The playbook is bumped (a new `prompt_version`), since the JUDGE sheet gains fields.
3. Both ship in the same release as, or before, the U stamp.
4. Gamma's rule: `NARRATIVE_VERSION` (taxonomy + tagger + researcher prompt) goes into the cohort
   policy before provisioning; switching it on later is a new cohort.

Until then the seat runs and stores rows for the narrative department and the studies, and U
stamps nothing. That is step 1 of the build.

## 6. Caps and fencing

- **Haiku:** one researcher call per 5 min = 288/day. Required knob
  `MEMECOIN_DESK_NARRATIVE_CALLS_PER_HOUR`, no default, charged before the call. Separate from the
  JUDGE and CRAWLER budgets.
- **Local:** no quota, one call at a time; the backlog is bounded to the last 10 min of launches. A
  backlog older than that is tagged `unknown`/`backlog_skip` and counted, so the share of untagged
  coins is visible.
- **Fencing:** names, symbols, meta names and cashtags go only inside `<untrusted>`. They are NFKC,
  capped at 64 characters per name, and stripped of fence tokens. The researcher's answer must be
  exactly `{"v": NARRATIVE_VERSION, "heat": {theme: int|null for every taxonomy id},
  "offchain": {...same...}}`. Anything else is a failed row, never stamped.

## 7. Edge map (CLAUDE.md rule 5)

1. **Money path:** indirect, once stamped. `theme_heat` reaches the JUDGE sheet only after the §5
   steps. It can narrow (the JUDGE is still bounded by every hard gate) and cannot raise a clip,
   cap or rail. Until §5, none.
2. **Fill speed:** none. Separate processes. U adds one indexed read per emission, as with the
   CRAWLER (measured ~0.09 ms there).
3. **Concurrency:** one tagger, one aggregator, one researcher, each with a flock; U reads
   read-only.
4. **Absent vs zero vs unknown:**
   - not tagged yet = `absent`;
   - tagger failed = `missing`;
   - model answer `unknown` = a real label meaning "cannot tell";
   - a theme with no coins this hour = count 0, which is a real zero;
   - no researcher row in 15 min = heat null, never the last value reused.
5. **Dependencies:**
   - Ollama down or slow: tags `missing`, counted, backlog bounded.
   - Haiku timeout, refusal or off-contract answer: failed row, heat null.
   - The X producer or DexScreener down: `offchain_heat` null, while `theme_heat` from the tape
     still works.
6. **Units:** host clock for every `known_at`, the same clock as U's `emission_at`. Heat is 0-100
   per theme, not per coin.
7. **Sample:** themes describe this tape's launches. The tag is only as good as the gate
   agreement, which is reported with every read.
8. **Rollback:** knob off per piece (`MEMECOIN_DESK_NARRATIVE_*_ENABLED`). The stamp falls back to
   null; the contract keeps accepting null fields.

## 8. Build order

1. Taxonomy + tagger + gate harness. Run the gate; report agreement, p90 and injection results
   before the worker goes on.
2. Code aggregates + copycat flag (no model).
3. Researcher (Haiku).
4. Only after Codex's contract change, the playbook bump and the cohort-policy version: the U stamp.

## 9. Questions

1. Is the taxonomy (§2) right for the narrative department, or are there themes to add?
2. Is a gate on 200 names plus 25 injected ones enough? Haiku as the reference is itself a
   model, so agreement is with Haiku, not with truth.
3. Should the stamp wait for a playbook version written for theme fields (v9, the U-led trader)?
4. Does `offchain_heat` belong in this seat, or in the separate news/trends seat the seats table
   lists?

## 10. Decisions after the gate run (Alpha, Gamma, 2026-10-21)

- **No local model in the tagging path.** The first cold load of qwen3.5:9b stalled the funded loop for about
  13 s (heartbeat 0.55 s -> 4.99 s). The gate run was void, and the Claude CLI login had also expired. Tagging
  is now `theme-rules-v1`: code rules in `sniper/services/memecoin_desk_theme_rules.py`, frozen with two
  named misses, zero model calls, about 82 us per tag. 24.7 % of launches get a specific theme on 2026-10-21.
  Any read of per-theme heat covers only that quarter.
- **`offchain_heat` moves to Beta's off-chain seat.** This seat stays on the tape plus names.
- **Emergent tokens (new):** every closed UTC hour, the top matched rule words and the top raw name tokens
  (NFKC, lower-case, letters only, at least 3 characters, stop-words dropped), counted once per launch, with
  the previous hour's count and the change. Its own table, separate from the 14 themes, so a "kimchi" day
  shows without being forced into food. Collected and reported only: never a gate or a sheet field until a
  pre-registered read.
- **Stamp and cohort:** unchanged (§5). Cohort U provisions without narrative; narrative arrives later as a
  new cohort.
