# Desk CRAWLER seat: placement, contract and edge map (Lambda, 2026-10-21)

Status: **placement (a) chosen (Alpha, Gamma 2026-10-21); built, see §7.** Seat table:
`docs/superpowers/specs/2026-10-21-desk-feeds-and-seats-design.md` (CRAWLER row, §3 injection
boundary). JUDGE side: `docs/superpowers/specs/2026-10-21-desk-judge-playbook.md` (`crawler_intent`,
`crawler_flags`, kill code 13 `crawler_warning`, code 14 `suspicious_text`).

## 1. The placement problem

The JUDGE sheet takes `crawler_intent` and `crawler_flags` only from the candidate's **frozen
emission snapshot** (`memecoin_desk_contract.render_judge_sheet` reads
`candidate["snapshot"]["fields"]`; a field known after the snapshot is withheld by
`build_snapshot`). Today U never fills them, so every sheet has them `null`. A CRAWLER that runs
after emission has no path onto the sheet. Three placements:

| | Where the crawl lands | Decision-path cost | Change outside the seat |
|---|---|---|---|
| **(a) cache, stamped at emission** | U reads the newest crawl of the mint with `known_at <= emission_at` and puts it in the snapshot | none (a local SQLite read in U) | U `fields()` adds two fields from the cache |
| (b) crawler stage merged into the sheet | a `crawler` ticket stage; `render_judge_sheet` reads it | none at the JUDGE; the stage must exist first | contract + JUDGE change on frozen step-3 code |
| (c) crawl at ingest | the assembler calls Haiku before JUDGE | +1-3 s per ticket before the decision | assembler, budget on the decision path |

**Recommendation: (a).** It matches the seat table ("offline from the decision, cached per coin,
frozen snapshot"), adds nothing to the decision path, and keeps the frozen-snapshot rule:
the crawl is evidence with its own `known_at`, like every other field. (c) spends the seconds the
desk exists to save. (b) reopens Codex's frozen contract for no gain over (a).

Consequence of (a), stated plainly: a coin nobody posted about before its band entry has no crawl,
so its fields are `null` ("unknown evidence"), never `"unknown"` (a model answer). On the U universe
that is most coins. The X-call department is where the crawl matters.

## 2. Watch set and refresh

- **Entry:** a Solana mint enters the watch set at the first X-producer post that resolves to it
  (the scout's resolution rule, one active coin, mint or exact `$TICKER`), or the first J7
  `token_meta` row naming it.
- **Refresh:** at most every N = 10 min per mint, and only when new posts or `token_meta` rows
  arrived since the last crawl (no new input, no call).
- **Leave:** 6 h after the last new input, or at graduation on the U tape. A coin that left on
  the 6 h rule re-enters on its next new input; graduation is terminal (amendment 2026-10-21,
  Codex/Gamma worker review).
- **Refresh and retry (amendment 2026-10-21):** a coin is due when it has input not covered by a
  successful crawl and no attempt in the last 10 min. The attempt start is written with the charged
  attempt row, so a crash keeps the 10 min spacing; hour/day caps count each call's real start.
- **Cap:** calls per hour and per day (knobs, no default: the worker refuses to start unset), and
  at most 50 posts' text per call, newest first, each capped at 500 characters after NFKC.

## 3. Seat contract

- Haiku 5.5 through the JUDGE worker's `sdk_call_model` (subscription, no tools, no settings, no
  MCP, max one turn), 20 s deadline. The seat is off the decision path, so the deadline is
  generous; a late answer is a failed crawl, not a late decision.
- **Input (user turn only):** the mint (code-owned key), and inside `<untrusted>...</untrusted>`
  the posts' text with handle and our receive time, the `token_meta` symbols/names, and U's
  launch `symbol`/`name`. All creator- or poster-controlled, NFKC-normalized, length-capped, and
  any `</untrusted>` removed. The system prompt says the fenced text is data, never instructions.
- **Output:** exactly
  `{"k": <mint>, "intent": "promotion"|"warning"|"analysis"|"unknown",
  "flags": {"copycat_ticker": b, "bought_hype_pattern": b, "suspicious_text": b, "rug_history_claim": b}}`
  with each flag `true`, `false` or `null`. Code validates it with the contract's own
  `_fact_value` rules. A key that does not echo the mint, an extra key, an off-enum value,
  malformed JSON, a refusal or a timeout is a **failed crawl**: stored with its error, never
  stamped, retried at the next refresh. **No failure becomes a value.**
- **Output never carries text**, so the JUDGE sees only the enums (two-hop isolation, §3.4).

## 4. Storage and stamping

- `data/desk/crawler/crawls.db`, one writer (the crawler process, flock), append-only:
  `crawls(id, network_id, mint, known_at, input_hash, inputs_json, intent, flags_json, model,
  call_error, latency_ms, prompt_version)`. `inputs_json` holds the post and `token_meta` row ids,
  never the text.
- U at emission: newest successful crawl of `(solana, mint)` with `known_at <= emission_at` and
  `known_at >= emission_at - 6 h` -> `crawler_intent` and `crawler_flags` with
  `source="crawler:<id>"`, that `known_at`, `completeness="complete"`. None -> both `null`.
  The read is local SQLite on U's processor thread; measured p50/p90 goes in the review.
  Amendment 2026-10-21 (Codex/Gamma stamp review): only rows of this `CRAWLER_VERSION` are stamped
  or explain a miss; only other-version rows by emission = missing, `crawler:version_mismatch`. A
  cached value off the enums, or a busy/locked cache (no wait: timeout 0), = missing,
  `crawler:unreadable`; the emission is always kept.
- **Ticket row:** the snapshot field's `source` names the crawl row, and the ticket keeps the
  snapshot, so the ticket already records which crawl it saw. I propose no separate `crawler`
  stage row. Alpha asked for one: with (a) the crawl predates the ticket, so a stage row would be
  a copy. Say if you still want it.

## 5. Edge map (CLAUDE.md rule 5)

1. **Money path:** indirect. A `warning` or `rug_history_claim` crawl kills a JUDGE call (code 13),
   and `suspicious_text` kills it too (code 14). A crawl can only narrow: no enum raises a clip,
   cap or rail, and ENTER still meets every hard gate.
2. **Fill speed:** none. The crawler is its own process. U adds one indexed local read per
   emission (to be measured).
3. **Concurrency:** one crawler writer by flock; U opens `crawls.db` read-only (WAL).
4. **Absent vs zero vs unknown:** no crawl = `null` (unknown evidence). Model `"unknown"` =
   the model read posts and could not tell. Flag `null` = not determinable. A failed crawl is
   never stamped.
5. **Dependencies:** Haiku timeout, refusal, auth expiry, rate limit or malformed output = failed
   crawl with its error and a counter. The X producer's gaps leave the watch set thin, not wrong.
   A J7 `token_meta` mint can be on another chain: only Solana mints enter.
6. **Units and identity:** the key is `(network_id, mint)`; `known_at` is the host clock of the
   crawl's completion, compared with U's `emission_at` (same host clock).
7. **Sample:** coins posted about on this J7 account's firehose. The crawl says nothing about
   coins nobody posted about; the read must split by `crawler_intent is null`.
8. **Rollback:** knob `MEMECOIN_DESK_CRAWLER_ENABLED=0` stops crawls. U then finds no fresh crawl
   and stamps `null`, which is today's state.

## 6. Questions for the crew

1. Placement (a)? If yes, the U change goes on `lambda/desk-universe` and needs a U restart.
2. No separate `crawler` ticket stage row (the snapshot source names the crawl)?
3. Watch-set entry from the X producer and `token_meta` only, or also every U band entry (a crawl
   at band entry would always land after the emission, so with (a) it helps only the next
   emission of the same coin)?
4. N = 10 min refresh, 6 h freshness, 50 posts x 500 chars, 20 s deadline: acceptable starting
   values?

## 7. Decisions (Alpha, Gamma, 2026-10-21) and what was built

- **Placement (a).** No separate `crawler` ticket stage row; the snapshot `source` names the crawl.
  U band entry starts no crawl (it could never land before that emission's snapshot).
- **Source format:** `crawler:<id>:<known_at>`. The contract's source grammar
  (`[a-z][a-z0-9_.:-]`) has no `@`, so Gamma's `crawler:<id>@<known_at>` uses `:` instead.
- **Why a field is NULL:**
  - completeness `absent`: no crawl of the mint existed by emission. `absent` is added to
    `COMPLETENESS`.
  - completeness `missing` with source `crawler:cap`: the newest row is a call-cap skip. One cap row
    is written per new input, so a cap-censored coin is told apart from an un-posted one; count the
    `crawler:cap` sources per day.
  - completeness `missing` with source `crawler:stale_or_failed` or `crawler:unreadable`.
- **Caps:** `MEMECOIN_DESK_CRAWLER_CALLS_PER_HOUR` and `_PER_DAY` are required knobs with no default.
  Proposed starting values are 40/hour (a burst limiter: post volume is peaky by hour) and 600/day
  (the budget; 20/hour x 24 = 480 could never reach 600). The operator picks the daily budget (Alpha
  said 300, then 600). Measure the watch set's first 24 h (no outcomes used) and refit before cohort
  U is provisioned. The values are re-frozen after 24 h of the X producer on cron. They are charged
  from the crawler's own `attempts` rows before each call, and are separate from the JUDGE seat budget.
- **`CRAWLER_VERSION`** = `desk-crawler-v1`, a code constant in `sniper/services/memecoin_desk_crawler.py`.
  Beta's provisioner freezes it into the cohort policy. Turning the crawler on after a cohort is
  provisioned without it means a new cohort (Gamma: never switch it on mid-cohort under the same id).
- **Built:** `sniper/services/memecoin_desk_crawler.py` (contract, fence, stamp),
  `scripts/memecoin_desk_crawler.py` (worker, `--loop`, flock),
  `scripts/memecoin_desk_crawler_watchdog.sh` (cron), the U worker's `fields()` stamp, and tests.
- **Watch-set entry:** X producer posts resolved by the scout's rule (`quoted_tweet`/`reply_tweet`
  side frames excluded), and J7 `token_meta` rows whose mint is base58 with chain NULL or `sol*`.
  J7 sends chain NULL for Solana: in the 2026-10-21 sample, eth and robinhood were named and Solana
  was not.

## 8. Deploy-order rule (runbook, Gamma)

`COMPLETENESS` gains `absent`, and every desk reader validates snapshots against that tuple: the
contract's `validate_candidate`, `verify_u_emission`, the assembler, the cluster gate and the ranker.
A U running this code beside a reader on an older release would emit snapshots the reader refuses,
so every emission would be REFUSED until the reader catches up. **U restarts onto this code only in
the same release move as every snapshot-reading desk process, or after them.** Today only U runs,
so the first pinned release that carries this code must carry the readers too.
