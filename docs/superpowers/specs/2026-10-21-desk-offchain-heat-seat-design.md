# Desk offchain-heat researcher seat: design (Beta, 2026-10-21)

Status: **design only, for Gamma and Alpha.** This splits the off-tape half out of Lambda's narrative
seat (`lambda/desk-narrative`, `docs/superpowers/specs/2026-10-21-desk-narrative-seat-design.md` §1, the
researcher's `offchain_heat`), per Gamma. The narrative seat keeps tape-driven `theme_heat`. This seat
owns attention measured **off the tape**, and keeps it in its own rows with its own source tag.

## 1. Inputs (all already collected; this seat adds no provider call)

| Source | Producer | What it carries | Paid or organic |
|---|---|---|---|
| DexScreener `/token-boosts/top`, `/token-boosts/latest` | `memecoin_desk_dexscreener.py`, desk context only | boost amount per token | **paid**: a boost is bought |
| DexScreener `/ads/latest` | same | ad placements | **paid** |
| DexScreener `/token-profiles/latest` | same | an enhanced token profile was listed | **paid**: the profile is a paid listing |
| DexScreener `/community-takeovers/latest` | same | a community takeover claim | **listing**: a listing fee may apply, so never counted as organic |
| DexScreener `/metas/trending` | same | trending meta (narrative) names and their stats | **organic**: DexScreener's own aggregate, not bought per token |
| X posts and cashtag / `token_meta` mint links | `memecoin_desk_x_producer.py` (J7 feed) | mentions per mint and per cashtag, per account | organic, except posts from accounts on a promoter list (§3) |
| CRAWLER intent per coin | `memecoin_desk_crawler.py` | `promotion` / `warning` / `analysis` / `unknown` + flags | `promotion` is promotion, the rest is organic |

The DexScreener rule stands (operator 4c930f3a): desk context only. It is never a price, fill, mark,
hard-filter fact or study label.

## 2. Outputs

Two per-row families, append-only, in `data/desk/offchain/offchain.db`, one writer per table (flock),
every row with `known_at` = when it was computed and `source = 'offchain:<row id>:<known_at>'`:

- **Per coin (code only, no model):** for each mint seen in any input within 60 min:
  - `paid_boost_usd` (active boost total) and `paid_ads`, `paid_profile`, `listing_takeover` booleans;
  - `x_mentions_15m`, `x_accounts_15m` (distinct accounts), `x_promoter_mentions_15m`;
  - `crawler_intent` at that time.
  Counts that are known to be 0 are 0; a source that was down in the window is null with completeness
  `missing`.
- **Per theme (Haiku 5.5):** `organic_heat` 0-100 and `paid_heat` 0-100 per `theme-taxonomy-v1` id,
  every 5 min, plus `meta_theme_map` (which DexScreener meta names map to which theme id).

**Paid is never organic.** The two numbers are never summed, averaged or blended. A coin with heavy
boosts and no organic mentions reads `paid_heat` high and `organic_heat` low. The sheet field the
JUDGE may later see is `offchain_heat` = `organic_heat`, with `paid_promo` (per-coin booleans and
`paid_boost_usd`) as a separate field. That way the JUDGE can never read a bought boost as
attention.

## 3. Telling paid from organic, concretely

1. **By endpoint (code):** boosts, ads and profiles are paid by construction; takeovers are a listing.
   They feed only `paid_*` fields.
2. **By account (code):** a promoter list of X accounts whose posts are paid calls is kept as data
   (`data/desk/offchain/promoters.json`, versioned, operator-curated), and their mentions go to
   `x_promoter_mentions_15m`, not organic. Seed it from the CRAWLER's `bought_hype_pattern` flag hits
   and from accounts that post a mint within N s of its boost. The list's version is in the seat
   version.
3. **By timing (code, reported, not used as a gate in v1):** for each mint, X mentions in the
   15 min after a boost start vs before it. A spike that starts at the boost is tagged
   `post_boost`, so a read can separate attention that followed money from attention that came
   first.
4. **By the model, only as a second opinion:** the researcher gets the paid and organic counts as
   separate numbers and the instruction that boosts, ads and profiles are bought. Its `paid_heat`
   and `organic_heat` are validated as integers 0-100. It can never move a count between the two
   families.

## 4. The researcher call

- Haiku 5.5 through the JUDGE's Agent SDK adapter (subscription, `tools=[]`, no settings or MCP),
  with the API key popped.
- One call per 5 min (288 a day). Required knob `MEMECOIN_DESK_OFFCHAIN_CALLS_PER_HOUR`, no default,
  charged before the call from its own rows. This budget is separate from the JUDGE, CRAWLER and
  narrative budgets.
- **Input:** per theme, the code-built counts, numbers only. Then, **fenced** as untrusted text, at
  most 50 DexScreener meta names and the top 50 cashtags, each NFKC-normalised, at most 40
  characters, with fence tokens stripped. No coin names or post text. The output must be exactly
  `{themes: {<theme id>: {organic_heat, paid_heat}}, meta_theme_map: {<meta>: <theme id>}}` with
  ids in the closed enum. Anything else is a failed call, stored with its error and never stamped.
- **Causality:** every input row has `received_at <= decided_at`. An output row is usable for an
  emission only when `known_at <= emission_at` and it is at most 15 min old. Otherwise the field is
  null: `absent` before the first row, `missing` when the seat or a source failed.

## 5. Contract and cohort order (the CRAWLER / narrative rule)

- `offchain_heat` and `paid_promo` are not in `memecoin_desk_contract._FIELDS`. A U that stamped them
  today would get every emission refused. The contract change (Codex's frozen code) comes first,
  and the playbook is bumped in the same release.
- `OFFCHAIN_VERSION` covers the input set, the promoter-list version, the researcher prompt and the
  taxonomy version. It goes into the cohort policy before provisioning, read from code like
  `crawler_version`. Switching it on later is a new cohort.
- **Coordination with Lambda:** Lambda's narrative design names a `news_heat` field taken from its
  researcher's `offchain_heat`. With this split, `news_heat` becomes this seat's `offchain_heat`
  (organic), plus `paid_promo`. Lambda's researcher drops its `offchain_heat` output. Both seats use
  `theme-taxonomy-v1` exactly (14 ids including `other` and `unknown`); a taxonomy change is a new
  version for both.
- Until the contract change lands, the seat runs and stores rows for the narrative department and
  the studies, and U stamps nothing.

## 6. Edge map (CLAUDE.md rule 5)

1. **Money path: none.** No entry, exit, sizing or signing. It changes the JUDGE's inputs only once
   stamped, and that is gated by §5.
2. **Fill speed:** its own process. The U stamp is one indexed read on U's processor thread (the
   CRAWLER stamp measured p90 0.094 ms); this one is measured the same way before merge.
3. **Concurrency:** one writer per table, flock singleton. It reads the producers' day files
   read-only (DexScreener, X, crawler) with short transactions.
4. **Absent vs zero vs unknown:** a 0 count needs a healthy source over the whole window; a source
   that was down (DexScreener 429 or error poll rows, an X producer gap, the crawler down) makes the
   field null `missing`. Paid fields are never 0-filled from an empty poll.
5. **Dependencies:**
   - DexScreener 429 or error: its poll rows say so, and the paid fields go `missing`.
   - Model timeout, dead login, off-enum output: a failed row, no stamp.
   - Promoter-list file missing: refuse to run. Without it, promoter posts would count as organic.
6. **Units and identity:**
   - boost amounts in DexScreener's units, stored raw with the unit named;
   - mints as base58 plus the chain id, with DexScreener `chainId` `solana` only;
   - seconds and UTC throughout.
7. **The sample:** heat describes attention we observed, through J7's account list and DexScreener's
   endpoints. It is not the market's whole attention, and every read must say so. Boosts are
   self-selected by the payer, so `paid_heat` is never read as a quality signal.
8. **Rollback:** `MEMECOIN_DESK_OFFCHAIN_ENABLED=0` stops it. Without fresh rows the U stamp goes
   `missing`. Removing the fields from the sheet is a new contract plus a new cohort.

## 7. Phasing (Gamma, on 0e8c35699)

- **v1 collects and reports only.** Neither `paid_promo` nor the model-made heat scores reach the
  JUDGE sheet. Both are unmeasured. A paid-promotion number in front of the JUDGE risks teaching it
  to chase paid pumps before anyone knows what they do.
- **Per-coin code part first.** It needs no model and no theme tagger. After an outcome-free
  collection week, the per-coin counts (boost USD, ads, X mentions, accounts, promoter mentions) are
  read against the U tape label in a pre-registered read. Only then is it decided what reaches the
  sheet, and that is a new cohort or prompt version.
- **The per-theme part waits.** It needs coins assigned to themes. The local qwen tagger is on hold
  after its cold-load stall of the funded loop (Lambda, 16:56Z) until the operator decides. It also
  needs the Claude CLI login, which has expired, renewed by the operator.
- **Promoter list:** the operator owns it as `promoter-list-vN`. Any edit is a version change, and an
  edit mid-cohort is a regime change if the list ever feeds the sheet.

## 8. Decisions for the operator

1. Approve the split: this seat owns `offchain_heat` (organic) and `paid_promo`; Lambda's seat keeps
   `theme_heat`.
2. The promoter list: Gamma recommends the operator owns it (versioned). Approve the seed rule.
3. Its own Haiku budget (proposal: 12 per hour, one per 5 min).
4. `paid_promo` and the heat scores on the sheet: Gamma recommends not in v1 (§7). Collect, read, then
   decide as a new cohort.
5. Re-login of the Claude CLI and the tagger decision, both needed for the per-theme part.
