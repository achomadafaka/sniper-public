# Batch Runner — Spec / Plan (rev 2 — methodology-patched, DRAFT)

**Status:** **BUILT** (Becker feed → batch core → Null Calibration Gate; 28 tests; real-data
dry-run + a live persist backtest verified). The conveyor that runs the thesis-extractor
pipeline at volume: candidate feed → profile → enroll → (optional) D4. Cross-ref: the
enrollment contract (`2026-05-30-forward-paper-enrollment-contract.md`), the D4 spec
(`2026-05-30-forward-tracking-job-d4.md`), and the thesis-extractor doctrine
(`2026-05-30-thesis-extractor-multi-lens.md`). Implementation: `becker_feed.py` (Option A,
opened-count, as-of T), `batch_runner.py` (`run_batch` + `null_calibration_rate`). The
`dead_zone` price feed and a live trades enumeration (Option B) remain future work.

**rev 2 (research-methodologist review applied — 3 serious + 3 important + 2 minor):**
(S1) the eligibility floor is now **positions OPENED**, not resolved-closed — "resolved" is a
soft winner-filter (losers held as zombies, winners redeemed faster). (S2) a mandatory
**activity-tier promotion-rate readout** — a rate that *rises* with trade count is a
survivorship signature, not skill. (S3) explicit scope: a D4 promotion is **within-wallet
persistence, NOT a generalizable/deployable edge**. Plus a **Null Calibration Gate** (§1.6 —
within-wallet outcome-shuffle Monte Carlo, the analogue of the MC that killed structural-NO),
unique-wallet gold-board counting (multi-cutoff pseudo-replication), Becker-dropout bias, and
an `entry_ts ≤ as_of` card-input cap.

**The whole point — and the whole risk — is the feed.** Every prior "edge" was
survivorship-contaminated: we picked wallets *because* they won (top PnL / leaderboard),
then "discovered" skill. That is circular. The conveyor only has value if the candidate
feed is selected on a criterion **orthogonal to forward performance**. Get the feed wrong
and this is survivorship-mining with extra steps.

---

## 1. Candidate feed (LOAD-BEARING — designed first)

### 1.1 Anti-survivorship invariants (non-negotiable)
- **No outcome conditioning.** The feed must NOT select on PnL, ROI, win-rate, leaderboard
  rank, "profitable," or anything measured *after* the enrollment cutoff. Selecting on the
  thing D4 later measures is leakage.
- **Activity, not quality.** Wallets are sampled because they *traded*, full stop.
- **Strict PIT.** Enumeration uses only information available at the enrollment cutoff `T`
  (pre-`T` activity). No "wallets active in `(T, T+window]`" — that leaks the forward window.

### 1.2 Eligibility floor (mechanical AND outcome-neutral) [S1]
A wallet is eligible iff it **opened ≥ `ENROLL_MIN_OPENED` positions as-of `T`** (proposed 30).
**Floor on positions OPENED, not resolved-closed** — "resolved" is outcome-correlated (losing
positions are held as unredeemed zombies; winning positions are redeemed promptly), so a
resolved-count floor is a soft winner-filter masquerading as a neutral activity gate. Opening a
position requires no future outcome → outcome-neutral. **No profit/rank filter of any kind.**
Whether a *card* can then run depends on how many of those positions have resolved (its
`min_forward_n`) — that is a downstream card-power question handled honestly by D4
(`awaiting_data`/`inconclusive`), **not** an eligibility gate.
**Data caveat:** if the wallet source only exposes resolved/closed positions (a Data-API
constraint) and an opened-count is unavailable, the floor is unavoidably resolution-correlated —
in that case say so plainly in §1.5 and treat it as an acknowledged limitation, not a clean floor.

### 1.3 Concrete source — recommendation
| Option | Source | PIT/backtest | Verdict |
|---|---|---|---|
| **A (recommended first)** | **Becker trades dataset** — enumerate wallets with ≥ K trades in `[T-L, T]`, by activity | clean PIT; enables an immediate **backtest** (enroll as-of `T`, D4 measures the already-elapsed `(T, now]`) | have the data; truly broad; validates the whole conveyor now without waiting weeks |
| B (**BUILT**) | recent BUY trades in active markets (Data API `/trades?market=` → `proxyWallet` buyers) | current wallets; enables a **live** forward test | `live_feed.py` (`live_candidate_feed` + `build_live_providers`); hash-ordered, outcome-neutral (openers only), appearance-proxy `opened` (definitive floor still the producer `min_n` downstream); live-verified |
| C (REJECTED) | leaderboard / top-PnL | — | survivorship — the exact failure mode |

**Recommendation:** build **Option A** first — a Becker-derived broad activity enumeration at
a past cutoff `T` so the first run is a **backtest** that exercises the entire conveyor end to
end and produces *real, already-resolved* verdicts (no multi-week wait). Then Option B for the
ongoing live conveyor.

### 1.4 Feed contract
```
CandidateFeed(as_of_ts: int, *, min_closed: int, limit: int) -> Iterable[str]   # wallet addrs
```
Yields wallet addresses, deterministically ordered (e.g. by address hash, NOT by any
performance metric), selected by pre-`T` activity + the eligibility floor. Injectable so the
batch is testable with a fake feed and so Option A/B are swappable.

### 1.5 Residual biases (document, surface — do not hide)
- **Activity survivorship [S2]:** the floor selects wallets active enough to open `N` positions —
  i.e. wallets that *survived* long enough. The eligible set over-represents persistent traders,
  who may have a higher base rate of real edges, inflating the D4 promotion rate above the true
  out-of-population rate. **Not just a readout concern — it contaminates the promoted rate.**
  Mitigation: §5 reports promotion rate by **activity tier** (floor→2×floor vs >2×floor opened);
  a promotion rate that *rises* with trade count is the **signature of survivorship inflation**,
  not a genuine skill gradient — investigate before claiming edge.
- **Within-wallet persistence is NOT generalizable edge [S3]:** D4 builds the card from a wallet's
  pre-`T` behavior and measures the **same wallet's** post-`T` behavior. A stable idiosyncratic
  style persists forward whether or not it is a real market edge, and the two confirming batches
  are **correlated** (style autocorrelation), so the α² independence claim is optimistic. **A D4
  promotion is evidence of within-wallet behavioral persistence — NOT, by itself, evidence of a
  cross-wallet generalizable or deployable market edge.** Establishing a deployable edge needs a
  second-stage test (the card design replicated across a held-out cohort of same-type wallets),
  which is **out of scope** for this conveyor.
- **Becker-dropout bias [I5]:** wallets enumerated from the frozen Becker window (~Feb 2026) but
  producing no forward positions are the dropout population; their D4 outcome is structurally
  `inconclusive`. The promoted rate is **conditional on continued post-`T` activity** and cannot
  be extrapolated to the full Becker-eligible population (dropouts may differ systematically —
  banned bots, strategies that stopped working).
- **Becker freeze:** Option A enumerates as-of Becker's frozen window, not the present; fine for a
  backtest, but the live conveyor needs Option B.
- The batch reports the eligibility-rejection count so the funnel from "scanned" to "eligible" is
  visible.

### 1.6 Null Calibration Gate (MANDATORY before any gold-board claim) [I6]
The conveyor has **no falsifiability** without a null. Before interpreting *any* gold-board
result, run a **within-wallet outcome-shuffle Monte Carlo**: for each eligible wallet, permute the
win/loss labels on its forward positions (preserving position count and timing), run the full D4
pipeline on the shuffled labels, and record the **empirical null promotion rate**. This is the
direct analogue of the Monte Carlo that killed structural-NO and batch-1 volume-filtered-NO.
**Rule:** a real-data promotion rate **≤ 2× the Monte Carlo null rate is NOT reportable** as
evidence of edge. The null rate is inflated above the theoretical α² by the activity floor and
within-wallet autocorrelation, which is exactly why it must be measured empirically, not assumed.

## 2. Per-wallet pipeline

For each candidate wallet (checkpointed individually):
1. **Fetch** the wallet's closed positions (reuse the wallet fetch).
2. **Map** to card-input rows (reuse the sizing-row mapping from `forward_window_provider`).
   Enrollment uses full pre-`T` history **capped at `entry_ts ≤ as_of_ts`** [I7] — if the
   wallet fetch returns positions after `T` (the live Data API returns recent positions without a
   date cap), those MUST be excluded from the card input or the in-sample card silently ingests
   forward data. On current data only sizing (and timing if a price feed existed) is real;
   exit/discovery degrade honestly.
3. **Assemble** `assemble_profile(wallet, positions)`.
4. **Enroll** `enroll_profile(profile, store=...)`. `store=None`/dry in dry-run; the Supabase
   store under `--persist`. In **backtest** mode set `enrolled_at = T` so D4's forward window
   is `(T, now]` (build dep §6).
5. **(optional) D4** `run_forward_tracking` over this wallet's rows using the **negRisk-enriched**
   provider (build the negRisk set from the wallet's forward condition_ids, §negrisk_lookup).
6. **Checkpoint** the wallet: status + counts.

## 3. Modes & flags

- `--as-of <ts>` — enrollment PIT cutoff (default: now = live mode; a past `ts` = backtest).
- `--persist` — write enrollments to Supabase (default **dry-run / no persist**).
- `--run-forward-tracking` — run a D4 pass after enrollment (default off).
- `--max-wallets <n>` — bounded batch size (rate-limit guard).
- `--batch-run-id <id>` — names the run + its checkpoint (default: derived from `as_of` + feed
  params, deterministic).
- `--min-closed <n>` — eligibility floor (default 30).

## 4. Idempotency & checkpointing

- One **`batch_run_id`** per run.
- **Per-wallet status** persisted as the batch proceeds: a checkpoint record per wallet
  `{wallet, status ∈ done|failed|skipped, counts, error}` in `runs/batch/<batch_run_id>.jsonl`
  (default; a Supabase `paper_batch_status` table is the durable/multi-host option, deferred).
- **Rerun semantics:** load the checkpoint; **skip `done`**, **retry `failed`/missing**. The
  enrollment idempotency key (`wallet|card_id|card_version|profile_run_id`) independently
  prevents duplicate rows even if a wallet is reprocessed.
- **Checkpoint every wallet** (not at the end) so a killed run resumes cleanly.

## 5. Output (counts only)

A summary dict + per-wallet checkpoint lines. No verdict prose, no PnL claims:
`wallets_scanned, eligible, ineligible, profiles_built, cards_admitted, cards_rejected,
enrollments_queued, d4_advanced{by new_state}, failed`.

Additionally required for any gold-board interpretation:
- **`promotion_rate_by_activity_tier`** [S2] — promoted/eligible split by opened-count tier
  (floor→2×floor vs >2×floor). A rate that rises with the tier is a survivorship signal.
- **`unique_wallets_promoted`** [I4] — the gold board counts **unique wallets**, never total
  promotions, so a wallet enrolled at multiple cutoffs can't pseudo-replicate itself.
- **`null_promotion_rate`** [I6] — from the §1.6 Monte Carlo; the real rate is only reportable
  if it exceeds 2× this.

## 6. Build dependencies (land before/with the batch)

- **`enrolled_at` write:** `enrollment_store._row` currently omits `enrolled_at` (relies on DB
  `default now()`). Backtest mode must send `enrolled_at = T`; add `enrolled_at` to the insert
  payload when the `PaperEnrollment` carries it (None → DB default, unchanged for live mode).
- **Sizing-row mapping reuse:** factor the `_to_sizing_row` mapping so both the forward provider
  and the batch enrollment-mapping share it (no second copy).
- **negRisk set per wallet:** the D4 pass builds `negrisk_conditions` from the wallet's forward
  condition_ids via `negrisk_lookup` (already built).

## 7. Invariants (hard)

- **No verdict mutation outside `paper_card_enrollments`.** The batch writes only enrollments
  (`--persist`) + its checkpoint file. It never touches `user_analysis`, verdict surfaces, or
  any analyzer table.
- **No PnL/rank filter anywhere** in feed or eligibility (§1).
- **No live orders, no sizing output.** Paper hypotheses only.
- **Dry-run is the default**; persistence is explicit.
- **Deterministic**: same feed + same `as_of` + same data ⇒ same enrollments + same counts.

## 8. Required tests (acceptance)

1. `feed_selects_by_activity_not_pnl` — a fake feed with mixed-PnL wallets; the batch applies no
   profit/rank filter; eligibility is purely the opened-position floor.
2. `eligibility_floor_is_opened_not_resolved` [S1] — floor counts positions OPENED; a thick
   **losing** wallet (many opened, mostly lost/zombie) is eligible; a thin wallet excluded.
3. `card_input_capped_at_as_of` [I7] — positions with `entry_ts > as_of` are excluded from the
   card input (no forward leakage into the in-sample card).
4. `null_calibration_mc_produces_null_rate` [I6] — the within-wallet outcome-shuffle MC runs the
   pipeline on permuted labels and returns an empirical null promotion rate.
5. `gold_board_counts_unique_wallets` [I4] — a wallet promoted from two cutoffs counts once.
6. `promotion_rate_reported_by_activity_tier` [S2] — output splits promoted/eligible by opened tier.
7. `dry_run_does_not_persist` — default run makes zero store writes.
8. `persist_writes_enrollments` — `--persist` upserts to the (fake) store.
9. `rerun_skips_done_retries_failed` — checkpoint drives skip/retry.
10. `backtest_sets_enrolled_at_to_as_of` — `--as-of T` ⇒ enrollment `enrolled_at == T`.
11. `forward_tracking_pass_advances_rows` — `--run-forward-tracking` advances enrolled rows via
    the provider (fake fetch).
12. `counts_output_is_accurate` — the summary tallies match the fakes.
13. `no_writes_outside_paper_card_enrollments` — structural: only the enrollment store + checkpoint
    are written.
14. `max_wallets_bounds_the_scan` — `--max-wallets` caps the candidates processed.

## 9. Open decisions (resolve in review)

- **D-feed:** Option A (Becker backtest) first vs jumping to Option B (live). Recommendation: A.
- **Multi-cutoff [I4 — RESOLVED]:** one enrollment per wallet per cutoff is allowed (different
  `as_of` → different `profile_run_id`), but the gold board counts **unique wallets promoted**,
  not total promotions, so overlapping-history enrollments can't pseudo-replicate.
- **Eligibility floor [S1 — RESOLVED]:** opened-position count, not resolved-closed (§1.2).
- **Status store [I8]:** JSONL checkpoint is sufficient for bounded single-host runs; the Supabase
  `paper_batch_status` table becomes **required before batches exceeding `max_wallets=100`**
  (otherwise a re-run from another host re-fetches every wallet).
- **Backtest window:** for Option A, the `(T, now]` forward window length / whether to cap it.
- **Timing card in the batch:** stays `BLOCKED`/`awaiting_data` until a price feed exists — confirm
  we run the batch sizing-only for now (don't wait on timing).

---

**Cross-ref:** the conveyor is `positions → producers → admission → profile → enroll →
paper_card_enrollments → D4 ← provider`. The batch runner is the orchestrator that drives it at
volume over the §1 feed. Methodology review: research-methodologist `needs-revision`/serious,
3 serious + 3 important + 2 minor, all applied in rev 2 (opened-not-resolved floor, activity-tier
readout, within-wallet-persistence scope limit, Null Calibration Gate, unique-wallet counting,
Becker-dropout bias, `entry_ts ≤ as_of` cap, checkpoint-durability bound).

After this spec is approved: land the §6 build deps, build the batch core + the §8 tests against
fakes, **including the §1.6 Null Calibration Gate**, then one bounded dry-run + one small
`--persist` backtest. **No gold-board claim until the real promotion rate clears 2× the MC null.**
