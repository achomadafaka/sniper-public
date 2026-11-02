# Golden Lane — Independent Elite-Wallet Paper Lane

**Date:** 2026-08-22. **Status:** APPROVED DESIGN, not yet implemented.
**Origin:** Coverage audit 2026-08-22. The scanner admission path blocks Golden
observation: newest-30 discovery covers ~59 s of launches (448 creates in 20 min),
busy scans take up to 286 s, the A/B/C profile and the $250k/vol gates run before
Golden, 13.5% of signals lose their quote slot, and the 100-trade buyer tail
truncated 18 of 27 Golden tails. Golden must leave the admission path.

## Goal

Observe every buy by the elite roster (32 wallets, `data/runner_golden_wallets.json`,
tier=elite) within seconds, paper it, and label it — independent of the runner
scanner's discovery, profiles, gates, and capacity.

## Design

1. **Watcher.** A dedicated process follows the elite wallets with the confirmed
   on-chain watcher pattern (`scripts/research/onchain_wallet_watch.py` precedent,
   1-3 s detection). It writes to its OWN paper queue. It never touches the funded
   detection queue, the funded RPC keys, or any funded table.
2. **Quote first, enrich later.** On each elite buy: one exact Jupiter entry quote
   immediately, then a paper row. Transient quote failures retry at 2 s and 5 s.
   A terminal quote failure records a typed result and opens no paper arms.
   Candles, holders, clusters, and metadata fetch asynchronously afterward. They
   become labels and never become admission conditions.
3. **Independent lanes, three classes.** Every paper row records its class:
   `golden_only`, `candidate_only`, or `golden_candidate_confluence` (the runner
   scanner's candidate state joins by mint). The read tests whether Golden adds
   conviction over the candidate rule.
4. **Conviction upgrades.** Detection events append; the mint keeps a highest-
   conviction state. A stale or standard hit never suppresses a later elite hit.
   (Shipped in the scanner 2026-08-22 as an interim fix; the lane inherits the rule.)
5. **Durable discovery cursor.** The lane does not sample newest-30. It processes
   each elite-wallet transaction once. Each wallet has a persisted signature
   cursor. The lane replays each fetched page from oldest to newest. It advances
   the cursor after the queue insert commits. It never discards a backlog because
   newer events arrived.
6. **Capacity.** The lane has its own quote budget. The runner scanner's
   `capacity_skip` fix (last_fire only after admission) shipped 2026-08-22.
7. **Paired exits from ONE entry (review 2026-08-22).** Every eligible Golden entry
   opens three paper arms that share the same exact entry quote:
   - `golden_hold_control` — the current hold policy (stop -35%, trail arms 1.5x,
     25% give-back from the peak quote, 30 min cap).
   - `golden_scalp_v1` — TP +25%, stop -35%, 120 s cap.
   - `golden_ladder_v1` — partial exits into strength (sell a third at +20%, a
     third at +40%, trail the remainder with the hold rule).
   The engine quotes the exact sell of the remaining quantity every 2 s for the
   first minute, then at the hold cadence. Route loss is an unknown result
   (`route_censored`), never a loss. Cluster state is a LABEL on each row, not a
   routing rule. The read compares the three arms on identical entries — that is
   the monetization question the whole lane exists to answer.
8. **One cohort per roster and mint.** The first valid quote for `(roster_id, mint)`
   creates the cohort. Later elite buys append evidence to that cohort. They do
   not create new entries or reset an exit arm.
9. **Point-in-time confluence.** A Golden cohort gets
   `golden_candidate_confluence` only when Candidate evidence exists before the
   Golden entry quote. Later Candidate evidence stays an appended label. It does
   not change the entry class. The existing scanner cohort supplies the separate
   `candidate_only` comparison.
10. **Research storage.** The lane owns `data/golden_lane.db`. The queue, wallet
    cursors, cohorts, exact quotes, labels, and arm events stay in this database.
    Token quantities use decimal `TEXT` in atomic units. One database transaction
    creates all three arms from the same entry quote.

## Boundaries

- Paper and pages only. No funded authority, no funded RPC keys, no funded tables.
- The sealed candidate rule, the A500 cell, and the ACTION-M100 shadow are untouched.
- The elite roster refresh policy stays as written in the roster file; the lane
  reloads the roster on each refresh and stamps `roster_id` on every decision.
- RPC: a dedicated research key with backoff (finding 2026-08-21); WebSocket
  subscription preferred over polling.

## Read

Grade the three classes at >= 30 closed papers per class or 7 days: peak-class
rates, quote-based PnL under all three arms, and time-from-buy-to-detection. Promotion
of any class needs its own read; this document grants none.

## Amendment 2026-08-22 — dedicated normalized store (approved before tests)

The lane uses its OWN database file (`data/golden_lane.db`, WAL, gitignored runtime
data; observers open it read-only). An event-only store complicates recovery and the
scanner database breaks lane independence. Contract:

<!-- schema:external=golden_wallet_cursors -->
<!-- schema:external=golden_detections -->
<!-- schema:external=golden_cohorts -->
<!-- schema:external=golden_cohort_evidence -->
<!-- schema:external=golden_quotes -->
<!-- schema:external=golden_arms -->
<!-- schema:external=golden_arm_events -->
<!-- schema:external=golden_labels -->

| table | role |
|---|---|
| `golden_wallet_cursors` | one committed signature cursor per elite wallet |
| `golden_detections` | immutable transaction evidence (one row per observed buy) |
| `golden_cohorts` | unique `(roster_id, mint)` entries |
| `golden_cohort_evidence` | later elite buys and Candidate evidence for the mint |
| `golden_quotes` | typed entry and exit quote results (ok / transient / no_route / malformed) |
| `golden_arms` | current state of the three arms per cohort |
| `golden_arm_events` | immutable partial exits, stops, trails, censorship |
| `golden_labels` | cluster and enrichment evidence (labels, never admission) |

Rules: all token quantities are decimal atomic `TEXT` (u64-safe); the entry notional
stays $100; the ladder assigns `floor(qty/3)` to each target and the remainder stays in
the trail arm; exit quotes run every 2 s through 60 s, then every 10 s; route loss is
an unknown result. Public modules: `GoldenLaneStore`, `GoldenLane`, `GoldenExitEngine`.
Baseline at approval: 13,632 passed / 56 skipped (`2ed7c67d`, `09845165`, `be9d9f0b`).

## Amendment 2026-08-22 — operational closure after implementation review

The first implementation review found that the library shell could not run the lane.
This amendment defines the missing runtime and recovery contract.

1. The lane uses roster `defined_at` as `roster_id` and as the cohort cutoff.
   It stores older detections as `pre_cutoff`. It creates no quote or arm for them.
2. One transaction inserts a detection and advances its wallet cursor. Each detection
   has a durable `pending`, `processing`, `complete`, or `terminal` state. A lease
   returns abandoned `processing` rows to `pending`. A duplicate never skips pending
   work.
3. The wallet walker preserves provider order. It uses slot and transaction index for
   chain order. It continues backward until it reaches the prior cursor or the roster
   cutoff. It records an incomplete frontier and does not claim complete coverage when
   a page limit stops the walk.
4. An entry quote is valid only when all conditions are true: input mint is USDC,
   output mint is the detected token, requested input is `100000000` atomic USDC,
   output quantity is a positive decimal integer, and response age is at most five
   seconds. The row stores request time, response time, both mints, both exact amounts,
   provider, route identity, and typed status.
5. An exit quote reverses the mints. Its requested token quantity must equal the arm's
   exact remaining quantity. Its USDC output must be a positive decimal integer. Each
   arm action links to the exact quote for the sold quantity.
6. Each arm stores policy version, peak executable USDC, target state, realized USDC,
   remaining quantity, and terminal state. One transaction stores the action event,
   exact quote link, quantity change, proceeds, and arm state.
7. `golden_hold_control` stops at 0.65x, arms its trail at 1.50x, exits after a 25%
   peak give-back, and closes at 1,800 seconds. `golden_scalp_v1` takes profit at
   1.25x, stops at 0.65x, and closes at 120 seconds.
8. `golden_ladder_v1` sells `floor(entry_quantity/3)` at 1.20x and the same quantity
   at 1.40x. It trails the remainder after 1.50x with a 25% give-back. The stop and
   1,800-second cap close the exact remainder. A partial action uses a new exact quote
   for that partial quantity.
9. The exit worker uses a bounded quote budget. It records a typed deferral when the
   budget is full. Terminal arms leave the open-arm set.
10. `scripts/research/golden_lane_worker.py` is the executable paper worker. It reads
    only `MEMECOIN_RESEARCH_HELIUS_RPC_URL`, reloads the elite roster, backfills before
    live observation, processes pending detections, runs exits, and writes
    `data/golden_lane.heartbeat`. It has a deterministic `--once` mode for tests.
11. Enrichment uses a durable queue and a separate connection. It cannot block wallet
    detection, entry quotes, or exit quotes.

## Amendment 2026-08-22 — live-path identity and load closure

The second implementation review found differences between the test adapter and the
worker adapter. These rules apply to the worker and the library.

1. The canonical USDC mint is
   `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v`. Tests and workers use this value.
2. A buy requires a successful transaction, a positive token balance change, and a
   verified wallet SOL or USDC decrease. A transfer or failed transaction is not a
   buy. One transaction can create more than one token-buy detection.
3. Confirmed WebSocket logs are the primary live source. The signature poll repairs
   gaps. The poll checks summaries against the cursor before it requests transaction
   details. It decodes only new signatures.
4. An incomplete walk resumes from its stored frontier. It does not replace the
   complete cursor until it reaches the prior cursor or roster cutoff.
5. Same-slot order preserves provider order. A page-array index is not a transaction
   index and must not claim that name.
6. Exit targets compare executable unit price with entry unit price. The calculation
   uses integer cross multiplication. A partial sale cannot lower the price ratio only
   because fewer tokens remain.
7. If no route exists at an arm's time cap, the engine closes that arm as
   `route_censored`. It records unknown PnL and does not record a loss.
8. A unique `(cohort_id, kind, signature)` constraint makes Golden evidence idempotent.
   Detection and enrichment claims use owner-qualified leases and recover after a
   crash.
9. One sweep budget includes monitor quotes and partial-action quotes. A deferral moves
   `next_quote_at`. A durable fair cursor prevents the same arms from always winning.
10. An action stores the exact quote output as its proceeds. The store rejects a
    caller-provided amount that differs from the quote.
11. The worker reads Candidate evidence from the research scanner database before the
    entry quote. It runs enrichment from the durable queue and sends one Golden page
    after a valid cohort starts.
12. The worker resolves its database, roster, and heartbeat below `SNIPER_STATE_ROOT`.
    An RPC error records a failure heartbeat and uses bounded backoff. Transaction
    reads use confirmed commitment and retry the short confirmation race.

## Amendment 2026-08-22 — rug roster (label + page demote)

Mirror study of the golden rule: buyers present in >= 6 signaled coins with >= 95% dead
outcomes (peak < 1.25x). Day-1 roster tested blind on day-2: tagged coins 82% dead vs 67%
base and 3% winners vs 9% base; golden coins that also carry a rug wallet win 19% vs 37%
without (n=26). The roster is versioned in the same file (`rug_wallets`, disjoint from the
golden set by construction). The scanner records `rug_buyers` on every signal and demotes a
golden page to the digest when a rug wallet is in the tail. The lane inherits the rule as a
LABEL on `golden_labels` and a page demote — never an admission block; the arms still open so
the demote grades itself forward.

## Amendment 2026-08-22 — conviction score (13 stable bands, scored not gated)

Calibration on day-1 signals (quintile bands per at-signal feature; positive move = peak >=
+50% after signal), kept only where the day-2 win rate is >= 1.25x the day-2 base. Thirteen
bands survive (top-1/top-5 share, vol60/vol300, mcap, mom60/mom300, prior run, range, buyers,
top-buyer volume share, age 75-163 s, off-high -16..0). Seven do not (uniform, fresh, crew,
bundle, socials, rugcheck, sniper — the week's trap list). On the unseen day: score >= 9 ->
55% reach +50% and 48% reach $40k (base 24% / 9%, n=77); 6-8 -> 42% / 27%; < 6 -> 18% / 2%.
Bands versioned in `data/runner_conviction_bands.json`, which is TRACKED (force-added past
the `*.json` ignore): the file is the instrument, and a missing one used to leave every signal
silently scoring 0. `_load_conviction` now prints a loud line and returns no bands, and
`conviction_score` returns None — never 0 — so an unscored signal cannot look like a scored
one that hit nothing.

Read the three buckets, not the point score: <6, 6-8, and >=9 rise in order, but the full
0-13 range does not (win dips at 10-13, $40k dips at 8). The page cut is >= 11, measured on
17.0 h of unseen-day tape: >= 10 pages 5.5 mints/h at 61% reaching $40k, >= 11 pages 1.8/h at
77%. A rug tell or rug wallet still demotes to the digest.

The keep rule reads day-2 outcomes, so it is feature SCREENING, not a clean out-of-sample test
of the composite. Control: re-selecting bands on day-1 ONLY and scoring day-2 gives 56% win /
51% $40k in the top 5%, against 55% / 48% shipped — the day-2 keep step did not inflate the
result. The seven dropped features are the value of the split: all cleared day-1 (socials
1.80x, bundle 1.36x, uniform 1.32x, fresh 1.30x, crew 1.29x, sniper 1.27x) and collapsed to
about 1.0x on day-2.

Conviction predicts PEAK HEIGHT, not survival: 99.7% of score<6 coins end 'died', and so do
97.7% of score 10-13. Never read a high score as a durable hold.

Scope: the bands were calibrated on coins under 3 minutes old, so they page only on the
age-bound profiles (`CONVICTION_PAGE_PROFILES = "ABC"`). A profile-D coin can never hit the
75-163 s age band, and 8 of the other 12 bands shift on older tape, so D records its score and
opens its paper arm — earning evidence forward — but does not spend an operator page until the
D recalibration exists. The lane stores conviction as a label and the read splits every arm by
band. Not validated yet (no day-1 coverage): holders_total, tweet_likes, replies,
dev_prior_mints, goplus — graded when a full day exists.
