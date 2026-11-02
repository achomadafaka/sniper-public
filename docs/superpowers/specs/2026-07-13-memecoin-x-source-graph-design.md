# Memecoin X Source Graph and Forward Lead Evidence - Design

**Date:** 2026-07-13
**Status:** Approved design; Phases 1-4 and the optional tool-free LLM extension are implemented and fixture-tested. Adaptive userscript v1.5 is installed with cross-tab leader election; parser v2 adds localized attribution/truncation and promoted-card quarantine. X recovered after the bounded blank-page freeze, and the fresh parser-v2 acceptance window is healthy so far; the cadence-compatible 24-hour/90-run minimum, human audit, and operator enablement gates remain pending.
**Scope:** Read-only browser capture, source discovery, and display-only lead evidence

## 1. Goal

Use a dedicated, logged-in X account to observe a small curated Following feed and learn
which public accounts repeatedly precede or are amplified by proven Fomo traders.

The system produces two outputs:

1. a forward-honest source graph built from posts that Sniper actually observed; and
2. display-only source suggestions, notifications, and draft thesis evidence.

It does not trade, size, veto, follow accounts, or mutate the funded memecoin runner.

## 2. Decisions

- Do not use the X API.
- Do not scrape complete following lists or exhaustively scroll trader timelines.
- Use one dedicated burner account's Following feed as the live observation surface.
- Use a dedicated burner browser session with a read-only userscript limited to `x.com/home`.
  The userscript sends bounded card batches and pass health to a receiver bound only to
  `127.0.0.1`; it never receives credentials or performs account actions.
- Lock the initial roster at 27 Tier-1 trader accounts plus eight Tier-2/context accounts.
- Rank sources primarily by distinct Tier-1 amplifiers over rolling 24-hour, 7-day, and
  30-day windows. Keep relation counts separate; do not collapse them into a composite score.
- Treat X `posted_at` as source metadata, never as proof that Sniper knew a post at that time.
- Use Sniper's corrected real-UTC `observed_at` for every forward lead claim.
- Keep retrospective backfill structurally quarantined from forward evaluation.
- Capture non-amplified source-post/buy coincidences as a separate weak evidence lane.
- Capture official Robinhood accounts as context, but exclude them from coin-caller ranking.
- New accounts are suggestions for manual review. The system never follows them automatically.
- Keep deterministic capture, correlation, and notifications independent of any LLM. Optional
  LLM enrichment is asynchronous, display-only, evidence-cited, and auditable.
- Never claim the browser feed is complete.

## 3. Initial Roster

### 3.1 Tier 1: proven Fomo traders

These 27 accounts form the base feed. The X handle may differ from the Fomo handle.

| Fomo identity | X handle |
|---|---|
| `zinceth` | `@zinceth` |
| `RuneCrypto_` | `@RuneCrypto_` |
| `Thokani` | `@thokani` |
| `saivSOL` | `@saivSOL` |
| `PaikCapital` | `@PaikCapital` |
| `Aurelius0121` | `@Aurelius0121` |
| `Fiboguy` | `@ErisScalp` |
| `techquant` | `@Dprisav` |
| `0xJiggy` | `@0xJiggy` |
| `00CTG` | `@00CTG` |
| `Farmercharts` | `@Farmercharts` |
| `memekingdom` | `@memekingdom` |
| `_OHT_` | `@_OHT_` |
| `airtightfish` | `@airtightfish` |
| `badmonft` | `@badmonft` |
| `theveeman` | `@theveeman` |
| `real_y22` | `@real_y22` |
| `chimpfone` | `@chimpfone` |
| `brezscales` | `@brezscales` |
| `change` | `@changefomo` |
| `asta` | `@astaso1` |
| `Binkieee` | `@Binkieeefomo` |
| `shillcapital` | `@shillcapital` |
| `goyim` | `@goyimpnl` |
| `careful` | `@careful` |
| `Hiraethh` | `@WoahWurdz` |
| `jester` | `@dddddddddddd100` |

The mapping is configuration, not identity truth forever. Handle changes require operator
review. X handles are canonicalized without `@` and in lowercase for identity/deduplication;
display spelling may be retained separately. Historical captures retain the canonical handle
and roster role observed at capture time.

### 3.2 Tier 2: upstream candidates

- `@poptyedev`
- `@blknoiz06`
- `@iruletrenches`
- `@ProofMarkets`
- `@Noxa_Fi`
- `@hooddotfun`

These accounts are eligible for source-post and coin-caller statistics. Their presence in
the initial list is a discovery hypothesis, not proof of lead or profitability.

### 3.3 Official context

- `@vladtenev`
- `@RobinhoodApp`

These accounts provide official narrative and ecosystem context. Their posts may appear in
notifications or a draft thesis, but they are excluded from source-call denominators,
amplification rankings, and coin-caller leaderboards.

### 3.4 Out of scope roster

Private Telegram sources (`arcanegems`, `zincalpha`, `y22_trades`, `humblest`, and
`poptyedev`) are a separate access and capture problem. This design does not read, merge,
or score Telegram content.

## 4. Evidence Modes

### 4.1 Forward evidence

Forward evidence means:

1. Sniper observed an X feed item at `x_observed_at` on a healthy corrected clock;
2. the feed item contained an unambiguous coin identity;
3. Sniper later observed an eligible Tier-1 Fomo buy at `fomo_observed_at`; and
4. `x_observed_at < fomo_observed_at`.

Only this mode may contribute to lead-time metrics or a future evaluation gate. The
conservative lead is:

```text
lead_seconds = fomo_observed_at - x_observed_at
```

The X post may have been published earlier, but `posted_at` is not used in this subtraction.
Browser polling latency makes `x_observed_at` later than the unknowable first availability
time, so the measured lead is conservative.

An eligible Fomo buy is a `memecoin_feed_observations_v2` row with
`normalized_side='buy'`, `was_real_approved=1`, `capture_version=2`, and
`clock_domain='observed_real_utc'`. Forward links materialize only when the buy arrives no
more than 24 hours after the X observation. The primary source-flow horizon is six hours;
one-hour and 24-hour cuts are secondary diagnostics. Older matches remain searchable context
but are excluded with reason `lead_window_expired`.

### 4.2 Retrospective evidence

Timeline or search backfills may answer: "Who historically posted this coin before a trader
bought it?" They use X `posted_at` and are useful only for candidate generation.

Retrospective records:

- write to separate backfill tables;
- carry `evidence_mode='retrospective'`;
- cannot be copied into forward tables;
- cannot contribute to lead time, source precision, alerts, or evaluation samples; and
- may only suggest an account for manual addition to the forward roster.

This is structural quarantine, not a query convention.

## 5. Browser Capture Contract

The operator owns one logged-in burner tab on X's Following feed. A userscript limited to
`https://x.com/home` performs DOM reads, fixed bounded scrolls, and timed reloads only. It sends
card fragments and capture metadata to a local receiver bound to `127.0.0.1`. The receiver owns
clock reconciliation, sanitization, normalization, persistence, and run audit. Neither side
automates credentials, login challenges, two-factor prompts, session recovery, or account
actions.

Playwright persistent-profile, CDP, extension, and `channel=chrome` capture were attempted and
could not attach to the authenticated operator session reliably. The userscript/receiver split
is the selected adapter, not a weaker evidence mode: every coverage, clock, idempotency, and
diagnostic requirement below still applies.

Each bounded pass:

1. verifies that the tab is authenticated and still on the Following feed;
2. allocates a client run ID and sends a heartbeat even when no cards are new;
3. refreshes with a load-smoothing adaptive cadence;
4. reads visible cards and scrolls only until it reaches a previously seen sentinel or a
   configured card/scroll limit;
5. retains the batch in a pending state until the receiver acknowledges a committed run;
6. persists normalized facts idempotently behind one receiver clock snapshot; and
7. records whether the prior sentinel was found.

Initial operating defaults, superseded by
`2026-07-14-memecoin-x-adaptive-browser-pacing-design.md`:

- refresh interval: adaptive 5-15 minutes, with explicit readiness and operator-state backoff;
- maximum cards per pass: 50;
- maximum bounded scrolls per pass: 3;
- scroll progress: 450 pixels followed by canonical-ID progress/stability waits;
- no likes, replies, reposts, follows, profile navigation, or other account actions.

The userscript does not mark a card fingerprint delivered until a 2xx receiver response. Failed
or non-2xx deliveries remain pending and retry without inventing an earlier observation time.
The client run ID is stable across retries, so a lost response cannot duplicate a committed run.

If the sentinel is not found, the run records a coverage gap and stops. It does not scroll
indefinitely. If X presents a login, challenge, or rate-limit screen, capture sends a diagnostic
heartbeat, pauses card persistence, and alerts the operator; it never attempts to bypass the
control. Browser/tab death is detected as a missing heartbeat by the dry-run report and becomes
an explicit gap when capture resumes.

The receiver stores normalized text and links, not cookies, browser storage, credentials, or
browser profiles. The userscript may retain bounded post fingerprints and an unacknowledged batch
inside its own tab session; those values never enter SQLite or logs. Raw HTML and screenshots are
off by default outside the in-flight localhost request. Fixture capture stores the smallest
sanitized DOM fragment that reproduces a card and removes profile/session data before the fixture
enters Git.

## 6. Normalized Data Model

Names below are the implementation contract. Schema validation must run before the plan or
implementation names existing columns.

### 6.1 `memecoin_x_accounts`

One current registry row per normalized X handle:

- normalized handle and profile URL;
- `role`: `tier1_trader`, `tier2_source`, `official_context`, or `suggested`;
- optional Fomo source user ID and Fomo handle;
- active/manual-review state;
- provenance and operator timestamps.

Role changes do not rewrite historical observations.

### 6.2 `memecoin_x_capture_runs`

One row per bounded browser pass:

- run ID, parser version, started/finished timestamps;
- corrected-clock offset and offset age at start;
- `clock_domain`;
- cards delivered, total visible cards scanned, bounded-pass stop reason, new canonical posts,
  new relationships, and parse failures;
- prior sentinel, newest sentinel, and `sentinel_found`;
- status: `ok`, `coverage_gap`, `logged_out`, `challenge`, `rate_limited`,
  `clock_unhealthy`, or `parser_error`;
- concise error detail with no browser secrets.

An in-flight run owns a short receiver lease. A duplicate request cannot reset an active run;
only a stale `running` row can be retried. `cards_scanned` counts unique visible cards before
delivered-fingerprint filtering, while `cards_seen` remains the raw cards sent to the parser.

### 6.3 `memecoin_x_forward_posts`

One row per canonical X post ID first seen through the live Following feed:

- canonical post ID and URL;
- author handle and role at first capture;
- full visible text;
- whether the visible text was complete or truncated;
- X `posted_at_raw` and parsed `posted_at` when present;
- `first_observed_at` and `last_observed_at`;
- capture run ID, parser version, and `clock_domain`;
- extracted URLs and the raw card from the first canonical-post capture.

This row is a deduplicated read model. Its first-seen raw card is not the evidence authority for
facts discovered on later deliveries; that authority is `memecoin_x_card_observations`.

A canonical post ID is required for eligible forward evidence. A card without one may be
retained as an ineligible diagnostic fingerprint, but it cannot enter the source graph.

### 6.4 `memecoin_x_card_observations`

One immutable row per card delivery within a bounded capture run:

- capture run ID and canonical post ID;
- canonical author, feed actor, and relationship;
- our `observed_at` and `clock_domain`;
- parser version;
- full visible text and completeness flag;
- posted-time metadata and extracted URLs; and
- the exact raw card used by normalization.

The idempotency key is `(capture_run_id, canonical_post_id, actor_handle, relationship)`. A
relationship or coin mention must reference the observation that produced it. Fact-level lead
time always comes from that observation, never from the canonical post's earlier first-seen time.

### 6.5 `memecoin_x_relationships`

One point-in-time relationship between a feed actor and a canonical post:

- canonical post ID;
- actor handle;
- actor role and Tier-1 membership at observation time;
- relationship: `direct`, `repost`, `quote`, or `reply`;
- relationship `observed_at`, capture run ID, and `clock_domain`;
- referenced/quoted author and post ID when visible.
- source observation ID for replaying the captured relationship.

For `direct`, canonical author and feed actor must be the same normalized handle. A mismatch is
diagnostic-only and cannot enter the forward graph.

The unique identity is `(canonical_post_id, actor_handle, relationship)`. Re-delivery retains
the first captured relationship observation and advances the canonical post's last-seen timestamp
without inventing another amplification. Replay is ordered by §7.3 before persistence.

### 6.6 `memecoin_x_coin_mentions`

Zero or more normalized coin facts per canonical post:

- canonical post ID;
- network ID when independently identifiable from captured X content or a captured URL;
- normalized mint/contract address;
- ticker;
- extraction method: `contract_text`, `dex_url`, `launchpad_url`, or `ticker_only`;
- ambiguity and eligibility flags;
- parser version; and
- source observation ID containing the raw text/URL evidence and authoritative observation time.

Forward correlation requires an exact normalized mint and network. A ticker-only mention is
hypothesis evidence because tickers collide. A contract with unknown network remains visible
but is excluded from the forward lead gate. Network must not be inferred from a later buy.
Mentions extracted from incomplete/truncated text are diagnostic and ineligible. A later complete
observation may promote the same exact mention only at that later observation time; a later
incomplete delivery cannot demote or replace earlier complete evidence.

DexScreener and DEXTools pair-page path IDs are pool/pair identifiers, not token mints. Those
URLs may establish network context, but they do not establish the token mint unless the captured
card also contains an exact contract address. Raw Solana-like base58 strings are extracted only
from explicit contract context (`CA:`, `contract:`) or a recognized token launchpad URL; arbitrary
long base58 prose is not a coin mention.

EVM addresses normalize to lowercase. Solana addresses preserve base58 case. Matching always
includes network so the same EVM address on multiple chains cannot collide.

Text extraction accepts only standalone, exact EVM `0x` plus 40-hex tokens. Solana extraction
uses a case-insensitive `CA`/`contract` label but a case-sensitive Base58 alphabet; every extracted
address passes the same strict validator used for launchpad URLs.

Mention identity is NULL-safe. Re-observing the same ticker-only or unknown-network mention must
not create another row merely because SQLite treats `NULL` values as distinct in a normal UNIQUE
constraint.

Within a canonical post, a higher parser version replaces lower-version mention state before its
new result is persisted. Lower parser input cannot restore superseded eligibility. Parser upgrades
must run from a complete card or an explicit replay of a stored observation; a truncated higher-
version live card is not an authorized reparse.

### 6.7 `memecoin_x_forward_links`

Before links are built, `memecoin_x_fomo_buyer_snapshots` pins each eligible Fomo event's
stable source-user ID, source handle as observed, mapped X handle, Tier-1 flag, and Fomo
observation time. The initial mapping is written when the v2 buy first reaches the correlator,
even when no X mention currently matches. Replays must reuse that row; current roster edits must
not reinterpret an already-seen buy. A later Fomo handle rename may reuse a prior snapshot only
through the same stable `source_user_id`. `was_real_approved=1` remains the buy-time approval
fact; the current X roster is not used to restamp historical membership.

If no X identity mapping exists on first observation, the snapshot is still written with
`buyer_x_handle_at_obs=NULL`. Automatic replay must keep it unresolved even if the mutable roster
later changes. Assigning an X identity then requires an explicit, audited operator correction.

One auditable X-post/Fomo-buy pair:

- canonical post and coin-mention IDs;
- Fomo upstream event ID and buying source user ID;
- normalized network and mint;
- mention-observation and Fomo observed timestamps and clock domains;
- conservative lead seconds;
- evidence lane: `amplified` or `coincidence`;
- buying trader's qualifying relationship when amplified;
- point-in-time Tier-1 and source-role flags;
- eligibility and explicit exclusion reason.

Rows are append-only facts. Read models derive rolling summaries. Phase 1 currently enforces
`(coin_mention_id, fomo_upstream_event_id, buying_source_user_id)`, but `coin_mention_id` can
change after a parser correction. Before Phase 3 writes links, migrate idempotency to the stable
logical identity `(canonical_post_id, fomo_upstream_event_id, buying_source_user_id, network_id,
mint)`, preserving old rows and proving duplicate collapse with fixtures.

### 6.8 `memecoin_x_backfill_candidates`

Retrospective-only candidate evidence:

- historical post identity, author, X `posted_at`, coin facts, and discovery query;
- related historical Fomo event when applicable;
- `evidence_mode='retrospective'` enforced by a check constraint;
- no foreign key or promotion path into `memecoin_x_forward_links`.

### 6.9 `memecoin_x_capture_diagnostics`

Phase-2 browser/parser evidence that cannot enter the forward graph:

- capture run ID, observed timestamp, clock domain, and parser version;
- bounded diagnostic code and optional visible-card index;
- canonical post ID when one was safely extracted;
- sanitized article fingerprint, structured details, and parser card facts when useful; and
- no raw HTML, browser profile path, cookies, storage, authorization data, or credentials.

Diagnostics are operational evidence, not coin mentions. They cannot create forward links,
source counts, alerts, or LLM jobs. Unknown diagnostic codes are rejected rather than silently
invented at write time.

## 7. Clock Contract

Clock reconciliation is mandatory.

### 7.1 X observation clock

The browser reader loads one clock-offset snapshot, confirms that its offset and host-clock
`at` value are present and finite, confirms the offset is within the existing two-day sanity
bound and no more than 15 minutes old, then derives `observed_at` from that same snapshot. It
must not validate one read and call `real_utc_now_iso()` for a second read: a missing or changed
file between reads could silently fall back to the host clock. Capture runs store the checked
offset and its age.

If clock health fails:

- store the capture run and diagnostic cards if useful;
- mark their domain `legacy_unknown`;
- emit an operator warning; and
- exclude every affected post and relationship from forward correlation.

The helper's silent host-clock fallback is not sufficient for this feature.

### 7.2 Fomo observation clock

The authority for new forward evaluation is:

```text
memecoin_feed_observations_v2.observed_at
```

Eligible rows require `capture_version=2` and `clock_domain='observed_real_utc'`.
`memecoin_events.created_at` and X `posted_at` are never used for lead subtraction.

Legacy `memecoin_events` rows may be displayed or used for retrospective candidate work.
They may enter a normalized historical analysis only when `received_at_normalized` is present
and `clock_domain` was explicitly classified. They remain excluded from the forward X eval.

### 7.3 Ordering

Forward replay orders by:

1. `observed_at`;
2. capture run ID; and
3. canonical post or upstream event ID.

Strictly positive lead is required. Equal or unknown timestamps are ineligible.

## 8. Relationship Lanes

### 8.1 Amplified

For a given Tier-1 buy, `amplified` means the buying trader publicly reposted, quoted, or
replied to the matched canonical source post, and Sniper observed that relationship before
the buy. Amplification by a different trader remains part of the source graph but does not
convert this buyer's link into an amplified link.

### 8.2 Coincidence

For a given Tier-1 buy, `coincidence` means:

- Sniper directly observed an eligible source post containing the exact network and mint;
- the trader later bought that coin; and
- no qualifying pre-buy public amplification by that buying trader was observed.

This captures the plausible but unprovable path where the trader saw the source in a private
home feed or elsewhere. It is weaker than amplification and stays separate in every report.
It is never labeled causal.

### 8.3 Direct trader posts

A Tier-1 trader's own post is a direct seed signal, not an upstream-source edge. It may drive
a display-only notification or thesis fact, but self-post/self-buy pairs do not improve an
upstream author's ranking.

## 9. Read Models and Ranking

The source graph reports raw, interpretable counts for 24-hour, 7-day, and 30-day windows:

- distinct Tier-1 amplifiers;
- direct, repost, quote, and reply counts;
- exact-coin posts;
- amplified forward links;
- coincidence forward links;
- distinct buying traders and coins;
- median and distribution of conservative lead seconds;
- excluded and ambiguous observations;
- capture coverage gaps affecting the window.

No composite score is emitted in v0.

An account that posts many coins must not look strong merely by volume. Reports therefore
show both:

- follow-through precision: exact-coin posts followed by at least one Tier-1 buy within the
  six-hour primary horizon divided by all eligible exact-coin posts, with one-hour and
  24-hour secondary cuts; and
- buy coverage: Tier-1 buys preceded by an eligible post from the source divided by all
  eligible Tier-1 buys in the same captured window.

Metric denominators use only continuous intervals with healthy X and Fomo clocks and no
recorded browser coverage gap. Gapped intervals remain visible in the coverage report but do
not silently penalize source precision or buy coverage.

These metrics establish information-flow hypotheses, not profitability. Any proposal to use
X evidence for entry, veto, sizing, or capital allocation requires a separate approved spec
and forward outcome comparison against the existing Fomo-only baseline, net of costs.

## 10. Suggestions, Notifications, and Draft Theses

### 10.1 Follow suggestions

An untracked author surfaced through Tier-1 amplification becomes a suggestion. The operator
sees its evidence rows and may manually add it to the burner and registry. The application
does not click Follow or mutate the X account.

### 10.2 Notifications

Notifications are informational and link to captured evidence. Initial eligible events are:

- a Tier-1 direct post containing an exact network and mint;
- a Tier-2 source post containing an exact network and mint; or
- a fast-consensus event: the second distinct point-in-time Tier-1 actor mentioning the same
  exact `(network, mint)` within 60 minutes.

Notifications label the lane, capture time, coin-match quality, and coverage health. Official
context is labeled separately. Send failures do not affect capture. The 60-minute window is
only the fast notification trigger. The source graph and forward evaluation retain the six-hour
primary measurement horizon from §§4.1 and 9; no metric is silently shortened to 60 minutes.

### 10.3 Draft theses

An optional downstream agent may create a draft thesis containing only captured evidence IDs,
a concise interpretation, and explicit uncertainty. Drafts are display-only and never become
executable signals. The capture and source graph do not depend on an LLM.

### 10.4 LLM invocation model

The deterministic pipeline always publishes its eligible tag or notification before an LLM
job is queued. LLM latency, failure, invalid output, provider outage, or budget exhaustion cannot
delay, suppress, upgrade, or rewrite deterministic facts.

Invocation has four bounded modes:

1. **Event enrichment:** asynchronously queue one job for a fast-consensus event or a Tier-2
   scout event. A scout event is an exact `(network, mint)` post from a registered Tier-2
   upstream candidate. The logical event key deduplicates retries.
2. **Scheduled digest:** process only eligible observations newer than a committed digest
   watermark. If there are no new eligible observations, do not call the model.
3. **On-request analysis:** answer an operator query from a persisted `as_of` evidence snapshot.
   It never reads the live browser page or imports facts captured after that snapshot.
4. **Deterministic-only degradation:** if the provider is unavailable, output is invalid, or a
   request/token/window budget is exhausted, capture, correlation, alerts, and raw read models
   continue unchanged.

There is no per-card LLM call. Raw capture tags are deterministic and their latency is described
as observed polling latency, never "instant" or a promise of lead.

### 10.5 LLM evidence and audit contract

The model receives structured evidence rows plus the stored text from the cited card observations.
It never receives live DOM, raw HTML, browser profile data, cookies, credentials, arbitrary fetched
URLs, execution state, or later observations. Every job records:

- logical trigger key, mode, status, attempt, queue time, start time, finish time, and `as_of`;
- exact observation/relationship/forward-link IDs and a canonical input hash;
- provider, model ID, prompt version, output-schema version, and generation parameters;
- input/output token counts, budget decision, validation result, and bounded error reason; and
- the raw structured response plus the accepted display annotation, with secrets excluded.

Accepted output is schema-constrained and limited to a concise summary, an explicitly labeled
intent interpretation, uncertainty, risk concerns, and cited evidence IDs. Every factual claim
must resolve to an input evidence ID. Coin identities must exactly match an input `(network, mint)`;
unknown or unsupported identities and claims are rejected, not repaired. Model output may report
a risk concern but cannot determine token safety, emit a rug verdict, forecast price/returns,
recommend an entry/exit, or read or affect execution state.

Post text, handles, tickers, URLs, and quoted content are all adversarial data. Prompt delimiting is
defense-in-depth, not the control boundary. The hard controls are a tool-free worker, no browser or
execution credentials, a closed output schema, evidence-ID validation, budgets, idempotent jobs,
and display-only persistence.

## 11. Failure Handling and Edge-Case Contract

This is the known v1 register. New observed failure modes are added with a fixture and explicit
eligibility decision; they are never patched only in selectors or operator folklore.

### 11.1 Capture and browser session

| Case | Required behavior |
|---|---|
| Stale/cached or reordered feed | Deduplicate by canonical identity, compare the prior sentinel, record no-new state, and never infer continuous coverage from unchanged cards. |
| Missing sentinel or bounded-scroll/card cutoff | Mark `coverage_gap` with the stopping reason and scanned counts; never scroll indefinitely or claim full coverage. |
| Login, challenge, or rate-limit screen | Pause and alert. Do not bypass, submit credentials, accelerate retries, or restart-loop. |
| Dead browser, closed tab, or host sleep | Detect the missing heartbeat in the coverage report, record the missed interval on recovery, and alert for manual recovery. |
| Concurrent tab/pass or duplicate delivery | Use one browser-profile lease with a stable per-tab identity and stable client run ID. Only the elected tab reads or delivers; the receiver returns the previously committed result for a retry. |
| Wrong tab/feed (`For you`, search, profile) | Refuse capture unless authenticated and visibly on Following. |
| DOM experiment, locale/layout change, or selector drift | Mark `parser_error`, retain sanitized diagnostics, and stop before emitting eligible facts. |
| Virtualized/partial/truncated card | Preserve the observation but set incomplete/diagnostic eligibility. A later complete card gets its own observation time. |
| Promoted card or untracked direct author | Label separately. It cannot count as Tier-1 consensus; an author visible through Tier-1 amplification may still become a manual follow suggestion. |
| Receiver timeout, non-2xx, or lost response | Keep the batch pending, retry the same run ID, and mark fingerprints delivered only after a committed 2xx response. |
| Duplicate or overlapping polls | Enforce a shared browser-profile lease plus an in-tab pass lease and idempotent run/card keys. |
| Network timeout or crash mid-pass | Finish or recover the run as partial/error; no successful-coverage claim. Card persistence and run finalization are independently recoverable. |
| Deleted or edited post | Keep immutable delivery observations. Never rewrite earlier text; new facts use the later observation time. |
| Browser/session secret exposure | Reject fixtures/log fields containing cookies, storage, authorization headers, or profile paths with secrets. |

### 11.2 Extraction and identity

| Case | Required behavior |
|---|---|
| CA appears only in image/video | Record `media_without_text_identity`; no OCR and no eligible identity in v1. |
| Homoglyph, zero-width, spaced, or otherwise obfuscated CA | Record diagnostic text only. Do not strip/reconstruct characters into an eligible address. |
| Ticker-only mention or ticker collision | Hypothesis-only; never eligible for a forward link. |
| Bare EVM address without chain context | Retain normalized address as ambiguous; never infer Base, Robinhood `4663`, Ethereum, or another chain from a later buy. |
| Conflicting chain markers/URLs | Mark network ambiguous and ineligible. |
| Pair/pool URL | May establish network context but never promotes the pair ID to a token mint. |
| Shortened or arbitrary URL | Store as text; do not follow it server-side. Without captured chain/address evidence, identity remains unknown. |
| Reply asking for a CA | Attribute authored text to the answerer. The asker does not become the caller unless their own captured text contains the identity. |
| Quote/repost with nested authors | Preserve actor, canonical author, referenced author, and referenced post separately; do not flatten them into one source. |
| Multiple exact CAs in one card | Emit separate mention facts. Do not pick a winner; each must have independent chain context. |
| Same `0x` address on multiple chains | Match only the exact `(network, normalized_address)` tuple. Robinhood `4663` is never pooled into generic EVM. |
| Invalid Base58/EVM boundaries or Unicode controls | Fail strict validation and retain diagnostic-only text. |
| Later card adds a previously truncated CA | Create evidence at the later card-observation time; never borrow the canonical post's first-seen time. |

### 11.3 Correlation and causality

| Case | Required behavior |
|---|---|
| X `posted_at` before buy but X `observed_at` after buy | Ineligible for forward evidence. |
| Equal, unknown, stale, or mixed-domain clocks | Ineligible; strictly positive lead on reconciled `observed_real_utc` clocks is required. |
| Repost/quote/reply observed after buy | Does not create a pre-buy amplified lane. |
| Fomo duplicate/replay or non-v2/unknown-clock event | Deduplicate; exclude from forward evaluation unless it meets the exact v2 receipt contract. |
| Network mismatch, including Robinhood vs another EVM chain | No link, even if address text is identical. |
| Trader may have learned through Telegram/private feed/common source | Keep the separate `coincidence` lane and label it non-causal. |
| Trader buys then posts | Direct seed context only; it does not improve upstream lead ranking. |
| Same actor repeats or changes relationship type | Distinct-actor consensus counts the actor once; relation counts remain separate. |
| Coordinated actors always move together | Report raw co-occurrence and actor-set concentration separately. Do not down-weight or create a composite score in v0. |
| Roster role or handle changes later | Use point-in-time stamped role/handle; require operator reconciliation for future captures. |
| Official Robinhood/context account | Keep separate from coin-caller and consensus denominators. |
| Coverage gap intersects a metric window | Exclude the interval from eligible denominators and show the gap; do not interpret missing posts as negative evidence. |
| Multiple source posts match one buy | Keep auditable source-post links while enforcing a stable logical identity; do not inflate distinct-buyer or distinct-coin counts. |
| Common external cause creates apparent lead | Report observed correlation only; public sequence is not proof of information transfer. |

### 11.4 Alerts and LLM jobs

| Case | Required behavior |
|---|---|
| Repeated signal, process retry, or notification retry | Deduplicate inside the X lane with a stable logical event key and committed watermark. Never read real-position tables. |
| Alert provider failure | Persist bounded failure state and continue capture/correlation. Retry cannot duplicate a delivered event. |
| Gap during an interesting interval | Alert on degraded coverage and label any signal incomplete. |
| LLM completes after the deterministic alert | Attach a later annotation; never hold, replace, retract, or silently upgrade the original alert. |
| Prompt injection in text, handle, ticker, quote, or URL | Treat every field as data. Worker has no tools/credentials; validate the closed output schema and evidence citations. |
| Hallucinated CA or unsupported factual claim | Reject the annotation or unsupported field; never repair it from outside the frozen input snapshot. |
| Invalid JSON/schema, timeout, rate limit, provider outage | Record bounded error and degrade to deterministic-only. |
| Cost spike or context overflow | Enforce request/token/window budgets and deterministic input truncation that preserves evidence IDs; skip rather than silently omit identity facts. |
| Duplicate/concurrent job | Acquire by logical trigger key and input hash; one accepted result per model/prompt/output-schema version. |
| Stale completion after newer evidence | Preserve its original `as_of` and label it stale; do not merge later facts into the old annotation. |
| Model or prompt changes | Pin and persist versions. Compare changes on frozen fixtures before enabling a new version. |
| Slang, irony, multilingual text, or unclear intent | Return `unclear` with uncertainty. Do not force an intent class. |
| Media-only content | LLM receives the media diagnostic only; it does not infer unseen image/video content. |
| On-request prompt asks for a trade, safety verdict, or forecast | Return cited evidence and uncertainty only; no execution recommendation or token-safety determination. |

### 11.5 Adversarial and operational integrity

| Case | Required behavior |
|---|---|
| Malicious/rug/honeypot contract is posted | Capture is not endorsement. LLM may state cited risk concerns but cannot declare safety or veto/authorize execution. |
| Bot-amplified or copied cashtag activity | Report measured actor/post concentration and duplication; make no unsupported prevalence claim or automatic trading inference. |
| Handle impersonation, rename, or compromised roster account | Preserve the captured identity, flag anomaly for operator review, and make no automatic roster mutation. |
| Malicious link | Store text only; never auto-open or fetch arbitrary destinations. |
| Disk full, DB locked, schema mismatch, or parser downgrade | Fail the pass visibly, keep the coverage gap, and never acknowledge persistence that did not commit. |
| Clock offset becomes stale during a pass | Use the single accepted run snapshot for internal consistency; the next run rechecks health. |
| Scheduler overlap or backlog | Single-pass lease, bounded queue, explicit dropped/deferred counts, and no silent time compression. |

Coverage reporting includes run count, successful intervals, gap intervals, parser failures,
login/rate-limit downtime, cards seen, scan cutoffs, missing heartbeats, LLM degradation, and the
fraction of eligible time with a healthy clock.

## 12. Security and Account Safety

- Browser credentials, cookies, tokens, and local-storage values never enter SQLite or logs.
- The userscript is installed only in the dedicated burner session, is limited to `x.com/home`,
  and sends only to the loopback receiver. Browser session state is never supplied to an LLM.
- The reader is read-only. It never posts, likes, replies, reposts, messages, follows, or
  unfollows.
- The initial 35 follows are manual, human-paced account setup outside the application.
- Raw text is untrusted input. It is never interpolated into shell commands or SQL. For LLM
  enrichment it comes only from stored observations, is framed as untrusted data, and remains
  subordinate to tool isolation and closed-schema output validation.
- URLs are recorded as text. The agent does not automatically open arbitrary links.
- Contract addresses are data, not authorization to trade.
- X capture failure cannot call or import funded execution paths.

## 13. Interfaces and Boundaries

The feature is split into isolated units:

1. **Userscript adapter:** obtains visible feed-card facts and pass health; knows nothing about
   Fomo or trading.
2. **Normalizer:** converts card facts into canonical posts, relationships, and coin mentions.
3. **Forward store:** persists point-in-time X facts and capture health.
4. **Correlator:** joins eligible X mentions to later eligible Fomo v2 buy observations.
5. **Read models:** computes source graph and raw rolling metrics.
6. **Output adapter:** emits display-only suggestions, notifications, and draft-thesis inputs.
7. **Backfill tool:** writes only the structurally separate retrospective candidate store.
8. **LLM job store:** records idempotent triggers, frozen evidence snapshots, versions, budgets,
   outcomes, and validation state without importing browser or execution code.
9. **LLM worker:** consumes persisted jobs asynchronously, has no browser/trading tools, and
   produces evidence-cited display annotations through a closed schema.

The userscript adapter can change when X changes its DOM without changing correlation rules. The
correlator can be tested entirely from fixtures without launching a browser.

## 14. Testing Contract

Use TDD during implementation. Required focused coverage:

- parse direct, repost, quote, and reply fixture cards;
- canonical post and relationship deduplication;
- role and Tier-1 flags remain point-in-time after roster changes;
- official-context capture but exclusion from caller rankings;
- exact EVM and Solana address normalization with network separation;
- ticker-only and unknown-network exclusion;
- healthy, missing, stale, and malformed clock-offset cases;
- X `posted_at` predates a buy but `observed_at` follows it: ineligible;
- X `observed_at` predates eligible Fomo v2 `observed_at`: eligible with correct lead;
- Fomo legacy/unknown-clock row: ineligible for forward evaluation;
- amplified lane requires the buying trader's own pre-buy relationship;
- non-amplified exact-coin coincidence remains separate and weaker;
- retrospective records cannot enter forward links or forward read models;
- sentinel miss and parser failure produce visible coverage gaps;
- notification/thesis failures do not break capture;
- duplicate-delivery, wrong-feed, login/challenge, virtualized-card, and bounded-scan fixtures;
- Robinhood `4663` remains separate from every other EVM chain in extraction and correlation;
- obfuscated/homoglyph/spaced addresses remain diagnostic-only;
- the 60-minute fast-alert boundary does not change the six-hour measurement horizon;
- deterministic notification precedes and survives delayed, failed, invalid, or over-budget LLM work;
- prompt injection, unsupported identity/claim, invalid schema, stale completion, duplicate job,
  context overflow, and version-change fixtures;
- every accepted LLM factual claim resolves to an input evidence ID and exact coin tuple;
- on-request analysis is frozen to its persisted `as_of` snapshot;
- browser secrets never appear in persistence fixtures or logs; and
- the X feature has no funded-execution imports or calls.

## 15. Rollout

1. Manually follow the locked 35 accounts on the burner at a human pace.
2. Install the read-only userscript in the dedicated burner session after manual login, start the
   loopback receiver, and capture sanitized direct/repost/quote/reply DOM fixtures without account
   actions.
3. Build the parser against those real fixtures and retain synthetic boundary fixtures for
   failures that cannot be safely induced on X.
4. Run bounded browser passes in dry/read-only mode and inspect parser/coverage diagnostics.
5. Start forward persistence with suggestions, notifications, and theses disabled.
6. Verify clock domains, sentinel coverage, deduplication, and exact-coin extraction during a
   24-hour read-only dry run.
7. Enable display-only source graph and raw rolling counts.
8. Enable deterministic informational notifications.
9. Enable event LLM enrichment, scheduled digest, and on-request analysis independently only
   after their audit, injection, schema, budget, and dry-run gates pass.
10. Collect forward evidence. Do not add trading behavior under this spec.

Every deployment remains outside `.runtime/memecoin/current`; this feature does not alter or
redeploy the funded memecoin runtime.

## 16. Explicit Non-Goals

- X API integration.
- Full following-list intersection.
- Exhaustive historical timeline scraping.
- Reading another user's private home feed.
- Automated account following or engagement.
- Telegram capture.
- Sentiment scoring or a composite conviction score.
- Destructive normalization that reconstructs obfuscated contract addresses.
- OCR or interpretation of contract addresses visible only inside images or video.
- Claiming public amplification proves private causality.
- Treating retrospective timestamps as tradeable evidence.
- Real or paper entries, exits, sizing, vetoes, or capital movement.
- LLM token-safety verdicts, price/return forecasts, or execution recommendations.
- Reading real-position or funded-runner tables to suppress or alter X alerts.
- Any change to existing memecoin strategy verdicts or funded execution.
