# X Intel API Capture and Cashtag Alerting — design

**Date:** 2026-07-26
**Status:** approved for implementation
**Scope:** Intel UI, official X API read capture, deterministic cashtag events, and Telegram alerts

## Decision

Move the existing X source ledger out of the Memecoin workspace and make it a dedicated **X**
subtab under Intelligence. Replace the fragile browser/userscript feed capture with a default-OFF,
read-only official X API worker. Observe only active accounts in the curated X roster and only their
cashtag activity. Persist every accepted event before any notification attempt and send bounded,
idempotent Telegram alerts containing the exact X Post link.

This remains an intelligence surface. It does not create entries, recommend trades, assign sizing,
follow accounts, or touch funded execution.

## Goals

1. Add `Geopolitics | Finance | X` navigation under Intelligence and remove `X Sources` from the
   Memecoin workspace without leaving a duplicate surface.
2. Receive near-real-time cashtag Posts from active curated accounts through X Filtered Stream.
3. Repair bounded stream gaps with Recent Search without duplicating Posts or alerts.
4. Distinguish an actor's own cashtag mention from a quote/repost amplification of referenced text.
5. Represent ticker-only evidence honestly; never invent `(network_id, mint)` from a cashtag.
6. Alert Telegram once for each new curated-account cashtag activity and once when a second distinct
   curated account establishes fast consensus.
7. Expose exact capture, alert, health, and source-evidence state in the Intel/X tab.
8. Leave the system ready for an operator-supplied bearer token and explicit activation.

## Non-goals

- No broad memecoin keyword, trend, hashtag, or full-firehose search.
- No accounts outside the active curated roster and no automatic roster expansion.
- No X account or content writes: no follow, unfollow, post, reply, repost, like, bookmark, or DM.
  The only allowed X POST is Filtered Stream rule management for this read-capture project.
- No sentiment or LLM decision in the capture-to-alert path.
- No claim that a cashtag mention is positive promotion.
- No entry generation, wallet scoring, developer graph, trading rule, or funded-runner change.
- No production DB mutation; X evidence remains in the isolated X SQLite store.
- No live X request in tests or fixture smoke.

## Existing boundaries

The current X system already has an isolated SQLite store, forward-evidence clock doctrine, source
graph, read API, deterministic signal builder, Telegram outbox, bounded sender, and fixture tests.
The UI is incorrectly nested under `Memecoin > Wallets > X Sources`, browser capture is the only
live ingestion adapter, and `memecoin_x_signal_events` requires exact `network_id` and `mint`.

The implementation must reuse the isolated store and outbox rather than create a parallel X
product. It must migrate the signal schema so ticker-only events are first-class instead of
fabricating coin identity.

## Architecture

```text
active curated roster
        |
        v
X Filtered Stream ---- reconnect ----> bounded Recent Search repair
        |                                      |
        +------------------+-------------------+
                           v
                  API payload normalizer
                           |
                    one SQLite transaction
                           |
          Post + relationship + cashtag mention + signal + outbox
                           |
                +----------+-----------+
                |                      |
                v                      v
        Intel/X read API        Telegram notifier
```

The stream worker owns X API network reads and the project-scoped Filtered Stream rule set. Rule
management is configuration of the read stream, not authority to act through the watched account.
The normalizer is pure and has no database or network dependency. Persistence owns idempotency and
commits evidence plus outbox work atomically. The notifier is a separate process; it never runs
inside the ingestion transaction.

## 1. Intel/X surface

Add a third Intelligence subtab: `Geopolitics | Finance | X`.

The X view contains:

- a health strip with stream state, last healthy receipt, last recovery, corrected-clock state,
  Post resources read today, local daily cap, and Telegram state;
- recent cashtag activity showing author, curated role, relationship, cashtags, exact excerpt,
  observation age, X-created age, identity state, consensus count, and actor Post link;
- filters for account, role, cashtag, relationship, identity state, and time window;
- existing source-performance, forward-link, suggestion, official-context, and LLM-annotation reads
  below the live activity, with their existing display-only labels.

Remove the Memecoin X subtab and section in the same change. The X renderer becomes an Intel-owned
component rather than a method on the Memecoin board. No X markup contains buy, sell, sizing,
approval, follow, or funded mutation controls.

Canonical read routes move to `/api/intel/x/*`. Keep `/api/memecoin/x/*` aliases only if a current
consumer audit finds a non-UI consumer; otherwise remove the old namespace. Both namespaces, if an
alias is required, must use the same implementation and return the same display-only payloads.

## 2. Curated roster identity and rule compilation

`memecoin_x_accounts` remains the roster authority. Only rows with `active=1` and role
`tier1_trader`, `tier2_source`, or `official_context` enter API rules.

Add the stable X user ID to the roster. The returned `author_id`, not the mutable handle, is the
observation identity. A handle change updates display metadata without creating a new source.

Compile deterministic Filtered Stream rules from normalized active handles. Each rule uses a
bounded group of `from:<handle>` terms and `has:cashtags`. Rules carry a versioned tag containing a
roster hash and group number. Compilation respects X's rule-length limit, produces stable ordering,
and fails startup when a roster entry cannot be represented safely. Rule reconciliation adds the
new tagged rule set before deleting the old set so a roster refresh does not create an intentional
coverage gap.

The worker never discovers or adds accounts from X. Suggestions remain review-only evidence.

## 3. API capture contract

The worker uses one Filtered Stream connection and requests only fields needed by the contract:

- Post ID, text, author ID, created time, edit history, entities, public metrics, and references;
- referenced Post ID, author, text, entities, and created time when a quote or repost must be
  classified; and
- user ID, username, name, and public profile metrics only when needed to reconcile the curated
  identity cache.

X `entities.cashtags` is authoritative. A strict text parser may record a diagnostic when the API
omits entities, but it cannot create an eligible event. Normalize cashtags uppercase without `$`.

For each received actor Post:

- cashtag in actor text/entities -> `cashtag_mention`;
- cashtag only in a quoted or reposted referenced Post -> `cashtag_amplification`;
- reply with an actor-text cashtag remains a mention with `relationship='reply'`;
- one Post with several cashtags creates one observation, one Telegram alert, and one normalized
  mention row per cashtag; and
- negative, neutral, warning, joke, or unclear text is still captured and alerted. The message says
  `mention` or `amplification`, never `promotion` unless a later cited annotation says so.

Use corrected real UTC for `observed_at`. X `created_at` is source metadata and may be shown, but it
does not replace the forward-evidence clock.

## 4. Stream recovery and cursor safety

Persist the last committed stream Post frontier and the active roster/rule hash. On disconnect:

1. close the broken connection;
2. reconnect with bounded exponential backoff and jitter;
3. run Recent Search over the exact active roster/cashtag query from the last committed frontier;
4. page in ascending logical order until the gap is complete or an explicit bound is hit; and
5. resume the stream only after every accepted recovery outcome commits.

Never advance the frontier past an uncommitted Post. Stream and recovery delivery share the same
logical identity, so overlap is expected and idempotent.

If recovery hits its page, age, or resource bound, persist a `coverage_gap` with the unresolved
frontier and emit a health event. Do not claim capture is healthy until a later pass closes the gap.
There is no browser-capture fallback after API activation.

## 5. Signal and identity schema

Migrate `memecoin_x_signal_events` to support these additional event types:

- `cashtag_mention`
- `cashtag_amplification`
- `cashtag_update`
- `cashtag_consensus`
- `capture_health`

Add:

- `ticker`
- `identity_state`: `ticker_only`, `exact_coin`, or `not_applicable`
- `post_id`, `post_url`, and `edit_root_id`
- `relationship`: `direct`, `reply`, `quote`, or `repost`
- `cashtag_source`: `actor_text` or `referenced_post`
- `excerpt`
- `x_created_at`
- `consensus_count`
- `details_json` for versioned bounded metadata

Identity constraints:

- `ticker_only` requires a nonempty ticker and `network_id IS NULL AND mint IS NULL`;
- `exact_coin` requires ticker, network, and mint;
- `not_applicable` is allowed only for capture-health events; and
- an existing exact-coin event remains valid without semantic reinterpretation.

Ticker resolution may upgrade a later read model when a contract address or one unambiguous fresh
mapping exists. It never mutates the immutable original event or rewrites a ticker-only alert as if
the exact identity was known at observation time.

Logical idempotency uses the stable actor Post/edit root, actor user ID, relationship, and normalized
cashtag set. An edit that adds a new cashtag creates one `cashtag_update` event and notification for
the added set. Re-delivery of the same edit history and cashtag set creates nothing.

## 6. Telegram contract

Alert every new eligible cashtag activity from an active curated actor. Do not wait for sentiment,
LLM enrichment, contract resolution, market data, or another account.

Each message contains:

```text
X CASHTAG — @handle · DIRECT|REPLY|QUOTE|REPOST

$MEME [$OTHER]
<bounded exact excerpt>

Activity: mentioned in actor text | amplified from referenced Post
Identity: ticker only | <network> <mint>
Consensus: <distinct curated actors in 60m>
Observed: <corrected timestamp or age>

Open Post: https://x.com/<handle>/status/<post_id>
```

The actor Post link is mandatory. A missing referenced Post is rendered as `reference unavailable`;
it does not suppress the actor event.

When a second distinct curated account mentions or amplifies the same normalized ticker within 60
minutes, enqueue one `cashtag_consensus` escalation. More actors update the dashboard count but do
not create repeated consensus alerts for the same rolling cluster.

Notification behavior:

- the event and outbox row commit atomically;
- the notifier is a separate default-OFF persistent worker;
- successful sends are never sent again;
- failures retry at most three times and retain bounded, secret-free errors;
- recovered Posts no older than 15 minutes alert normally;
- older recovered Posts are grouped into one catch-up digest;
- one actor producing more than five eligible Posts in ten minutes triggers one burst warning and
  digest mode for that actor until the window clears; all evidence remains in the X tab; and
- API auth failure, exhausted local budget, stale stream, or unresolved recovery gap sends one
  cooldown-bounded health alert and one recovery notice.

## 7. Cost and credential controls

The worker starts disabled. Enabling requires:

- `X_INTEL_API_ENABLED=1`
- a bearer token supplied through a dedicated untracked runtime environment file;
- a healthy corrected clock;
- a nonempty valid active roster;
- a positive local daily Post-resource cap; and
- a writable isolated X database distinct from funded and tape databases.

The bearer token never enters SQLite, logs, health payloads, exceptions, process arguments, fixture
files, or Telegram. The API client redacts authorization material from every error path.

Default `X_INTEL_MAX_POST_READS_PER_DAY=500`. Count accepted stream resources, recovery resources,
and explicit referenced-Post hydration once per resource ID per corrected UTC day. At the cap,
disconnect cleanly, preserve the frontier, record budget exhaustion, and resume after the next UTC
day. The operator must also configure the X Developer Console spending limit; the local counter is
defense in depth, not billing authority.

Telegram delivery has a separate gate:

- `X_INTEL_TELEGRAM_ENABLED=1`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

API capture can run with Telegram disabled. Telegram configuration cannot enable API capture.

## 8. Runtime and failure behavior

Add separate user services for API capture and Telegram notification. Neither is part of the funded
memecoin runner, its watchdog, or its pinned runtime. Installation and enablement are explicit.

Startup fails before network access for malformed settings, missing token, invalid DB identity,
unhealthy clock, empty roster, or an impossible resource cap. Fixture smoke requires no credentials
and opens no network socket.

Operational outcomes are explicit:

- `healthy`
- `reconnecting`
- `recovering`
- `coverage_gap`
- `clock_unhealthy`
- `auth_failed`
- `budget_exhausted`
- `database_unavailable`
- `stopped`

The UI never converts an unhealthy state into an empty healthy feed.

## 9. Testing

All implementation follows RED-GREEN TDD with injectable HTTP/stream boundaries and complete X API
fixtures. Tests cover:

- rule compilation, stable ordering, length bounds, roster hash, and inactive-account exclusion;
- direct, reply, quote, and repost payloads;
- actor-text versus referenced-Post cashtags;
- multiple, repeated, and case-variant cashtags;
- negative/neutral text without classifier suppression;
- ticker-only identity and exact contract identity without cross-chain guessing;
- stable X user identity across handle changes;
- edits adding/removing cashtags and stream/search overlap;
- disconnect recovery, pagination bounds, frontier non-advancement, and stale catch-up digest;
- missing/deleted referenced Posts;
- daily resource cap, UTC rollover, auth failure, redaction, and health transitions;
- atomic event/outbox persistence, retry bounds, burst grouping, and consensus deduplication;
- Intel/X API/UI placement, filters, empty/error states, responsive behavior, and escaped content;
- removal of the Memecoin X section;
- default-OFF startup and fixture-only smoke; and
- structural absence of funded imports, funded routes, execution controls, and production DB writes.

Focused verification includes the complete existing memecoin-X suite, Intel UI contract tests,
schema-reference validation, Python compilation, JavaScript syntax, and `git diff --check`.

## 10. Activation and rollback

Activation is separate from implementation:

1. merge and deploy the code with API and Telegram disabled;
2. create the dedicated bearer-token environment file with owner-only permissions;
3. run fixture smoke;
4. run one bounded API dry-capture pass with Telegram disabled;
5. review stored Posts, cashtag classification, cursor, resource count, and exact notification
   previews;
6. explicitly enable API capture;
7. observe stream and recovery health;
8. explicitly enable Telegram and send one operator-approved test message; and
9. retire the browser/userscript service only after the API path is healthy.

Rollback disables Telegram first, then API capture. It leaves the isolated evidence database and
outbox intact. Re-enabling resumes from the last committed frontier and must not replay sent alerts.

No step changes funded runtime, reservations, positions, entry policy, exit policy, or execution
credentials.
