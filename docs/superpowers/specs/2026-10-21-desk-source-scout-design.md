# Desk source scout: harvest, selection rule and storage (Lambda, 2026-10-21)

Status: **draft for Gamma and Alpha; the selection rule freezes as `scout-v1` when Gamma passes this
file, before the first harvest.** Scoring, outcomes, arms and retirement are Gamma's
pre-registration (`docs/audits/2026-10-21-desk-source-scout-preregistration.md`, with amendment 1)
and are not restated or changed here. This file fixes only what that document leaves to Lambda: the
candidate harvest, the S selection rule, the storage and the build order.

Authority: operator 4c930f3a (J7's two feeds, chosen X accounts tracked live), 40be676f (the X
producer; custom X slots unlimited), 9115c88c (the desk is U-led). No output reaches a funded
decision; the scout is a JUDGE context field only.

## 1. Inputs

- **X posts:** the X producer's day files, `data/desk/x/<UTC day>.db` (`posts`, `token_meta`).
  `known_at` = `posts.received_at`, our host clock (pre-reg §2). `ai_suggestion` is J7's own guess
  and is never an input to admission, resolution or score.
- **Coins:** the U tape, `data/desk/swaps/<day>.db` and `data/desk/launches`, for mint resolution
  and outcomes.

## 2. Candidate harvest (daily, 00:05Z, over the previous UTC day)

A candidate is an X handle seen in the previous day's posts. **Every handle is lower-cased before any
use** (pool keys, the R hash, tracked/retired sets, eligibility counts), so case variants of one
account never split across arms.

- **A2:** a post's author, and every `@handle` in a post's text or its `replyTo`/`quotedTweet` handles.
- **A3:** a handle with a post in the previous 7 days that named a resolved U coin (§3) whose
  `Y2` = 1, resolved by the harvest's cutoff. Stored with the post and coin; never scored
  (pre-reg §1).
- **A1** (Pump.fun / DexScreener / Axiom socials) is **not built in v1**: the U tape stores no
  socials, and reading them is a new provider read. It is a later `scout_version`.

A handle already tracked, or already retired, is not a candidate again in v1.

**Which frames are posts.** `quoted_tweet` and `reply_tweet` frames carry the quoted/replied
original (its author, an old or 0 `createdAt`; 5 of 5 sampled on 2026-10-21), not the poster. They
are never posts, authors or candidates. The quoter's own post arrives as a `tweet` frame
(`isQuote: true`) whose `quotedTweet` handle enters as a mention. Only the poster's own text resolves
a coin. `tweet_deleted` is excluded, and the first row per `tweet_id` wins over its `tweet_update`s.

**Gap censoring** (same rule as the U tape label u-label-v1): a `worker_start` or `stream:*`
(reconnect) gap row censors only when its measured span, last U swap before it to the first after it
across all mints, exceeds 10 s and overlaps [known_at, t_E + 600]. Any other cause (`ring_overflow`,
frames dropped while the tape kept moving, or a cause not yet known) censors whenever it falls inside
that window. The entry window is [known_at + 5, known_at + 65], shared with the label and runner study.

**Scoring runs** cover days D-1 and D (idempotent), so a post whose 670 s window was open at one run
is scored at the next.

## 3. Coin resolution (the pre-reg §2 rule, in code once)

A post names a coin when its text holds exactly one of:
- a Solana mint address present on the U tape in the previous 24 h, or
- a `$TICKER` matching exactly one U mint that was in-band or pre-band in the previous 24 h.

Zero matches: no coin. Two or more: `ambiguous`, logged, never counted. `token_meta` rows are J7's
link and are stored beside the post for comparison only; they do not resolve a coin in v1, so that
the score never depends on J7's matcher.

## 4. Arms and the S selection rule (`scout-v1`)

- **R:** `int(sha256("scout:" + lower(handle)).hexdigest(), 16) % 10 == 0` (pre-reg amendment 1). Capped
  at 40, lowest hashes first; every capped-out R handle is logged.
- **S eligibility** (pre-admission evidence only; it never scores):
  - the handle named **at least 2 distinct resolved U coins** in the 7 days before the harvest, by
    `known_at`; and
  - it is not in R.
- **S order:** distinct resolved coins, descending; then the count of those coins whose first
  naming by this handle came **before** the coin's band entry, descending; then
  `sha256("scout-v1|" + handle)` ascending.
- **Fill:** S takes `K − |R tracked|` places, K = 200. Places free up only by retirement.
- Each admission row stores `admitted_at`, arm, path(s), the admitting post id(s) and coin(s), and
  `scout_version`.

Why this rule: it is code-only and cheap, it prefers accounts that name coins on the tape we
trade, and its second key prefers naming before band entry, which is the lead the X-call desk
needs. It uses no follower count and no Haiku label. Its first key uses no outcome. **Its second key
is a past-outcome tiebreak:** resolution includes pre-band coins, so "named before band entry" means
the coin later reached the band after the call. It is pre-admission evidence only (pre-reg §1), it
never scores, and R controls the comparison; S is therefore mildly selected on past early calls.

## 5. Haiku type label

One Haiku call per admitted handle, on its last 20 stored posts' text: the type enum only (kol,
project, bot, news, unknown). Stored, never a score input. Raw text reaches Haiku and nothing else.

## 6. What "tracked" means in v1 (open item for the operator)

J7's custom X-account add is an account action whose API is not yet captured. In v1 the tracked
set is **bookkeeping only**: its handles are scored from whatever J7's firehose already carries for
this account. That biases v1 toward handles J7 already shows; the arm split by hash keeps S vs R
fair within that bias, but recall over handles the firehose never shows is zero. Adding handles
to J7 needs (a) a captured add endpoint and (b) an operator yes, because it changes the account
the funded relay also uses. It is a new `scout_version` when it lands.

## 6b. Reporting obligations

- Every read prints the §6 bias: no recall outside what J7's firehose carries for this account.
- R's scored-post rate is reported weekly from week 1. R is mostly @mentions that never name a coin,
  so 40 R handles may not reach the 100 scored posts the arm test needs in 28 days; then the
  pre-reg's "monitoring list without measured selection" verdict applies. R is never narrowed to
  rescue power: S's eligibility is part of the selection under test (Gamma, c657c7ad0 review).

## 7. Storage

`data/desk/scout/scout.db`, one writer (the scout job), append-only tables:
`pool` (handle, arm, admitted_at, paths, admitting refs, scout_version), `harvests` (one row per
run with counts and the R cap log), `retirements`, `posts_scored` (post_id, handle, known_at,
mint or `ambiguous`, outcome fields per pre-reg §2), `types` (handle, label, model, at).

## 8. Edge map (CLAUDE.md rule 5)

1. Money path: none. A separate job; reads day files, writes its own DB.
2. Fill speed: none.
3. Concurrency: one writer, flock. Reads the producer's WAL day files read-only.
4. Absent vs zero: a day file missing = harvest logged `no_input`, no admissions; a post with no
   resolved coin = no coin, not zero coins named; an unresolvable outcome = NULL (censored).
5. Dependencies: the producer's gaps are its `connection` rows; a harvest over a day with a gap
   records the gap seconds. Haiku failure = type `unknown` with the error, retried next harvest.
6. Units: `known_at` host epoch seconds; U tape block time; never J7's `createdAt`.
7. Sample: posts this J7 account receives (§6). Reported with every result.
8. Rollback: stop the job; the DB is separate.

## 9. Build order

1. Resolution + harvest + admission (no Haiku), dry-run over today's producer data.
2. Forward outcome scorer on the U tape.
3. Haiku type label.
4. J7 custom-handle add, after §6's capture and operator yes.
