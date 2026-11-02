# Wallet Promotion — Pre-Registration

**Status:** Pre-registration, written 2026-08-28 BEFORE the first forward paper entry. Frozen
inputs below. Phase 0 (policy clone) must complete before ARMS is restored; this document does not
authorise that.

**STUDY LIVE 2026-08-29 09:24:06Z:** the operator ordered the flip ("flip"). Phase 0 complete,
fitting addendum + A12 ruling registered, 112 artifacts committed (b2b0f90c).
`MEMECOIN_ARMS=funded_clone,profile_matched_v1`; the forward window opens at the runner restart
that loaded them (release 4365d40b). Wallets armed after the 08-28 freeze (picadura,
ClearCyanBoar, ResellCalendar, fivesevenslatt) accrue as the SECOND cohort per section 0.

This registers what will be measured, how, and what would falsify it — before any row exists. It
also records what CANNOT be answered, because several of the questions people expect this study to
settle are not answerable with the instruments we have.

## 0. The frozen cohort

The roster moved 17 -> 21 wallets on 2026-08-28 alone, so it is frozen here by content hash. A
wallet armed after this timestamp joins a SECOND cohort or waits; it does not enter this one.

```text
frozen_at      2026-08-28T13:41:10+00:00
roster_sha256  6aee7f91a9fcd8819fa6f2ecacf9f95195709d2b0adcb34065e970b063c82bde
n_wallets      21   (all solana)
```

ser_albert, (unnamed 43dba179), bigwarzeth, LevisNFT, fmpumpguy, s4if, 0xdedrater, sunnydog06,
hopa, llonchain, CryoPalmar, loganlim_x, omotrades, frankdegods, yayayayayey, KaminaSOL,
smol_intern, Thokani, CryptoTalkMan, DegenCapitalLLC, theveeman.

**Identity rule (load-bearing).** Every read in this study keys on `source_user_id`. It must never
key on `source_handle`. The executor opens a real position before it knows who signalled it, so it
writes the placeholder handle `(real)` and `set_position_identity` backfills the true handle later.
When that backfill does not happen, the row keeps `(real)` and a handle-keyed query drops it. 19
funded closes across 17 wallets carry `(real)` today. A handle-keyed read also POOLS: 4 closes
belonging to two other wallets carry an empty handle, so an empty-handle wallet inherits them.

Recomputed across the frozen roster both ways, one wallet's exclusion verdict depends on the key:

| wallet | by `source_user_id` (correct) | by `source_handle` (wrong) |
|---|---|---|
| `yayayayayey` | 15 closes, **+$107.77**, not excluded | 14 closes, -$27.34, excluded |

The missing row is position 39937 (Intismeran, closed 2026-08-19T12:12Z, +$135.11), which still
carries the `(real)` placeholder. `hopa` (23 vs 22) and `smol_intern` (14 vs 13) also read short by
handle, but neither changes verdict.

**There is therefore no governance exception in this cohort.** `yayayayayey` is clean on the funded
ledger and the freeze admits it on merit. The earlier -$27.34 citation was a handle-keyed artefact
and is withdrawn. The paper-gate figure (-$183/6) is a separate instrument and is unaffected by
this correction; it is not the standing funded exclusion line.

**Cohort attrition after the freeze (2026-08-28).** `KaminaSOL` was disarmed after the freeze
timestamp. The freeze holds — the roster and its hash do not change — but a disarmed wallet accrues
no forward rows, so the cohort is 21 wallets on paper and at most 20 that can produce data. The
live configuration shows 20 Solana wallets with funded authority. Record every further disarm here.
A cohort that silently shrinks is a cohort whose denominator stops matching its roster.

Attrition ledger (append-only):
- 2026-08-28 (pre-registration text above): `KaminaSOL` disarmed. Producing cohort <= 20.
- 2026-08-29 ~02:00Z (Alpha): `CryptoTalkMan` disarmed (operator "disarm losers"; funded robot
  -$71.03 on 10 closes). Producing cohort <= 19. Also for the record: `notarandomuser`
  (d47559fc) was armed 2026-08-28 ~18:55Z and disarmed ~02:00Z — both AFTER the freeze, so it
  was never in this cohort. `picadura` (43dba179) re-armed after the freeze: second-cohort
  candidate, not this cohort.
- 2026-08-29 ~07:4xZ (Alpha): `ClearCyanBoar` (8a4f6ef0) blind-armed by operator order after the
  freeze — never in this cohort; second-cohort candidate. Zero funded history at arm time.
- 2026-08-29 ~13:xxZ (Alpha): `ResellCalendar` (ff03cead) armed by operator order after the
  freeze — not in this cohort; second-cohort candidate. Funded robot +$70.82/7 at arm time.
- 2026-08-29 ~11:0xZ (Alpha): `leyten` (956aa99e, gate PASSED clean: 6 closes +$483.49),
  `prevaile` (22140383, forced: no evidence), `wealthmaxxer` (77e21536, forced: 8 closes
  -$127.58) armed by operator order after the freeze — second-cohort candidates.

**Open item for the operator, not a study input:** the roster entry `43dba179` has an empty handle,
is `real_approved=1` on solana, and has **zero** funded closes. It is armed with no evidence at all.
It stays in the frozen cohort and lands in the no-evidence stratum of §3.

## 1. The measurement unit

**One observation per admission-eligible ORIGIN. Never per source buy.**

This is not a detail. The wallet-profile spread quoted earlier as 250x was event-weighted and
mixed-chain; under the admission-eligible unit the same quantity is **1.7x** ($899K against $1.5M).
The 250x was Thokani's repeat buys counted individually. Every profile derivation, every stratum,
and every paired difference in this study uses the admission-eligible origin as the unit.

## 2. Arms

| arm | policy | purpose |
|---|---|---|
| `funded_clone` | resolves its rails through the SAME function the funded contract builder uses | the baseline |
| `profile_matched_v1` | chain-specific prior-events profile, frozen pre-window | the hypothesis |

**Phase 0 is BUILT (`1df6283d`), and this blocker is cleared.** The legacy paper policy differs from
funded on every rail (TP 0 / stop 0 / de-risk / 10-min / mirror, against 25 / 35 / 4h / none). A
baseline carrying the legacy policy measures POLICY DIFFERENCE and reports it as translation.

The delivered `funded_clone` arm is STRONGER than the exact clone this section originally demanded,
and the difference matters. A clone is a snapshot and can go stale the moment funded changes.
`funded_clone` instead RESOLVES its rails through the same function the funded contract builder
uses, so it carries whatever a funded entry from the same origin would be frozen under — band
default, per-wallet override, copy arm, mirror-on-sell and all. It tracks funded rather than
copying it, so the baseline cannot silently drift away from the thing it is the baseline for.

`profile_matched_v1` rules, frozen: chain-specific, profile built from prior events only and frozen
before the window opens, default-policy fallback where the profile is thin, and **no hold-derived
timecap in v1** — its coverage is 1.4-14.5%, which is not a parameter, it is a guess with error
bars. Paired differences by wallet; **no per-wallet verdicts**. Scouts are a separate cohort.

## 3. Primary read

**Total realised PnL per dollar deployed, `profile_matched_v1` MINUS control, paired by wallet.**

**Sign convention, fixed here and used everywhere in this document: treatment minus control. A
POSITIVE number means `profile_matched_v1` beat the funded baseline. A NEGATIVE number means it lost
to it.** An earlier draft of this line read "control minus `profile_matched_v1`", which is the
opposite of the direction the kill rule in section 6 uses. A primary read and a stop rule pointing
in opposite directions would have fired the stop on a winning arm, so the convention is now stated
once and every signed quantity below follows it.

The historical funded-versus-paper figures in the next paragraph are a DIFFERENT comparison and are
not on this axis. They are labelled where they appear.

Not win rate. Not per-rung capture. Not a per-wallet ranking.

Bootstrap **clustered by wallet AND day**, never over independent closes. This is load-bearing: on
the historical paired data (funded MINUS paper, a separate comparison from this study's contrast)
the naive SE gave mean +7.85/close with CI +1.75..+13.95 (resolvable), and the same data clustered
gives +8.69 with CI **-1.01..+18.28** (not resolvable). Closes are not
independent within a wallet or within a day, and treating them as such manufactures significance.

## 4. Censoring, stated before the data

- `HONEST_COPY_SQL` AND `measurable_outcome_sql()` — both, always. Fomo fills are a dishonest ENTRY;
  all-non-executable exits are an unobserved EXIT. Applying only the first gives n=27 where the
  canonical pair gives n=21.
- **On FUNDED rows only** (`execution_mode='real'`): a position with no `executable_peak_price` is
  INCLUDED as never-touched, never dropped. There the column is missing-not-at-random — absent on
  roughly a third of funded closes and those rows are overwhelmingly losers — so dropping them
  conditions every statistic on the position having gone green.

  **This clause must NEVER be applied to paper rows, and the scoping is load-bearing.** On paper the
  column is not missing-at-random, it is missing UNIVERSALLY: **0 of 20,454 paper closes carry one**,
  against 459 of 710 funded (64.7%). Its only writer runs on the funded exit path; the paper sweep
  has no call path to it. So absence on a paper row carries no information about that position, and
  a paper-side read inheriting this clause would file EVERY paper row as never-touched — turning a
  structural gap in the instrument into a finding about the market.

  Closing the gap is not affordable: it would cost on the order of 115,200 quotes/day (Beta's
  estimate), two orders beyond budget. **Paper peaks therefore stay chart-space and must be labelled
  as such wherever they are used.**

  **If a paper-side ROUTE peak is ever needed, the registered shape is post-hoc reconstruction from
  an existing tape** — the continuation-capture pattern — **never live sampling.** Live sampling is
  what makes the cost unaffordable, and a reconstruction reads a tape that is already being written.
- **The ledger writes a FABRICATED KNOWN ZERO for unpriceable paper closes, across TWO almost
  disjoint naming families.** A suffix cut (`..._noquote`) finds 1,860 rows; a prefix cut
  (`unquotable...`) finds 2,146; **the overlap is 57**. The union is roughly **3,950 paper closes
  carrying ~2,819 fabricated zeros and not one NULL**. Either cut alone understates the phenomenon by
  about a third. The position was never quoted after entry (`entry_price == peak_price`), so zero is
  not a measured outcome — it is an unknown recorded as a known.

  **The primary read is protected by BELT AND BRACES, verified rather than assumed.**
  `measurable_outcome_sql()` carries both a structured fill clause
  (`fill_source NOT IN ('noquote','cleanup')`) and an explicit `substr(exit_reason,-8) <> '_noquote'`
  term. All 2,078 bare-`unquotable` rows carry a `cleanup` fill, and **none closes without a fill
  row at all**, so the "no exit legs" path cannot admit them. The 71 that do survive the censor are
  partial-leg rows holding REAL PnL — zero fabricated zeros among them, none with
  `entry_price == peak_price`, averaging +45.20. The estimand is clean.

  **But the protection is by PREDICATE, not by value.** Any reader summing or filtering without it
  absorbs ~2,819 unpriceable outcomes as break-even, which is most ad-hoc queries. That is the
  argument for fixing the writer — not that this study needs it, but that the next reader does.

  Ruled 2026-08-29: the writer records NULL. The reader audit, run over the UNION, says the change is
  safe — `SUM`, `COUNT(*)` and `realized_pnl > 0` counts are all UNCHANGED, because a 0.0 and a NULL
  both contribute nothing to a sum and both fail a positivity test. Only `AVG` moves, **+1.9934 to
  +2.2994 (+15.4%)**, in the correct direction. **Forward only — no backfill**, since a silent one
  shifts every historical mean by 15.4% and reads as a market change rather than a definition change.
  Any announcement must quote the union's magnitude, never a single family's.

- **MONITORED: the fabricated-close rate per arm, and BROKEN PAIRS as the binding quantity.**
  Fabrication today is symmetric — 6 of 79 closes in each arm, **7.6% both** — so it cancels in the
  paired difference, and section 4's censor keeps it out of the estimand regardless. Neither is a
  guarantee going forward: nothing constrains the two rates to stay matched, and a divergence turns
  fabrication into a DIFFERENTIAL bias in an estimand whose entire claim is that the arms differ
  only in policy.

  **But the rate is not the quantity that matters, and monitoring it alone would miss the failure.**
  A paired difference needs BOTH halves. Two arms can fabricate at identical rates and still destroy
  the estimand if they fabricate on DIFFERENT origins — every such case is a pair with one half
  missing and a difference that is undefined. **The binding quantity is the count of pairs where
  exactly one arm fabricated.**

  **THE RATE IS WINDOW-DEPENDENT AND ALL SIX FABRICATIONS ARE POST-CUTOFF.** The same six rows per
  arm give 7.5% over all 80 pairs the arms have ever closed and **31.6% over the 19 post-cutoff
  pairs** — and the second is the honest figure for a forward estimand, because the forward cohort
  is what the study is read on. Both are reported with their windows named; the all-history figure
  alone would understate by 4x.

  Measured 2026-08-29 **on the post-cutoff cohort, which is the population this check governs**
  (an earlier version of this clause computed it over all history, where the phenomenon does not
  live):

  | | post-cutoff pairs | all-history pairs |
  |---|---|---|
  | both arms fabricated (intact, censored together) | 5 | 5 |
  | **exactly one fabricated (PAIR BROKEN)** | **2** | 2 |
  | neither (pair usable) | **12** | 73 |
  | total complete pairs | 19 | 80 |

  **Fabrication is CORRELATED within pairs on both windows.** On the post-cutoff cohort, independence
  at 31.6% would break 8.2 of 19 pairs; **2 broke.** It is largely a property of the COIN — both arms
  meet the same unquotable token — rather than an independent per-arm event, so the paired design
  survives.

  **The number a reader needs is 12 USABLE PAIRS, not 19 and not 80.** A reader seeing the pair count
  will otherwise assume the study is far further along than it is; at a 150-pair target this is
  roughly 8% of the way, and it is the same halving the accrual section records by a different cut.

  **Check before publishing any paired read**: the broken-pair count, not the rate divergence. A
  rising broken-pair share means fabrication has decoupled from the coin and started tracking
  something arm-specific, which is the condition that makes it a differential bias. This is a reason
  to LOOK, never a reason to stop the study.

- Unknown outcomes are censored, not imputed. A rotated-out scout is `CENSORED_AT_ROTATION`, never
  deleted and never backfilled — dropping a scout rotated out for being bad is selection running in
  the flattering direction.
- Priceability is a **stratum, not a filter**, and is measured **KEYED from the first forward row**.

## 5. What this study cannot answer, recorded so nobody assumes otherwise

- **The historical 95% unpriceable figure will never be decomposed.** The keyless baseline hours
  were waived (correctly — the skip-reason split shipped before any key, so attribution lives in
  each row). Forward rows are keyed from day one. The old number stays an unresolved mixture of
  rate-limit and genuinely-unroutable, and any document implying it will later be split must stop.
- **Best-of-N cannot be solved by a threshold at scouting scale.** Per-close funded return sd is
  49.0%. Under the null that every scout is the same population, 50 scouts at >=20 closes needs
  mean **>= +46.9%/close** to hold family-wise false promotion at 5%; the best wallet in the book is
  hopa at +24.0%, and only five wallets have >=20 funded closes at all. At 10 scouts and >=40 closes
  the bar is +22.7%/close. **State the accepted false-promote rate explicitly; at 50 scouts and any
  reachable bar it is not 5%.**
- **Time to first promotion** under a rule that controls best-of-N is ~40 closes per wallet, which
  at current signal rates is roughly 12 weeks. There is no promotion inside a quarter.
- **The translation gap is not established.** Direction is consistently positive across all three
  reads on the funded-minus-paper axis (+7.85 naive, +8.69 clustered, median +4.89), so "funded
  beats paper per close" is a live
  hypothesis. It is carried as a **BOUND, not a haircut** — a haircut invites subtraction, and there
  is nothing resolvable to subtract.

## 6. Kill criteria, pre-registered

1. Stop if `profile_matched_v1` minus control is worse than **-3% per dollar** at n=150 paired
   observations. This is on the section 3 axis: negative means the treatment lost to the baseline.
   origins.
2. Stop if the residual `rate_limited` share under the key exceeds **10%** of quote attempts,
   **counted on SOLANA ONLY** — the capacity check is invalid above that and the study is measuring
   our own limit.

   **The denominator is Solana-scoped because the rule is about Jupiter, and Jupiter quotes Solana.**
   An EVM attempt cannot be Jupiter-rate-limited, so pooling chains dilutes the share with a
   population that structurally cannot exhibit the condition. It is not a conservative choice, it is
   a wrong one: all-chain unpriceable reads **26.1%** (4,976 of 19,047) while Solana alone reads
   **6.4%** (502 of 7,799), and the gap is chain SUPPORT rather than capacity — chain 56 is 95.1%
   unpriceable, chain 1 is 85.7%, chain 8453 is 66.3%, against Solana 6.4% and 4663 9.9%.

   (Those chains are not wholly unable to quote, as an earlier framing had it: they carry 113, 82
   and 324 opens respectively. They quote BADLY, which is enough to dominate a pooled denominator.)
3. Stop if `funded_clone`'s resolved policy is found to differ from live funded at any point in the
   window. The baseline's whole validity rests on it. Because the arm resolves through the funded
   builder's own function rather than restating it, this criterion is checkable by comparing
   resolved contracts for the same origin, not by reading two policy tables and hoping they match.

### Effective accrual is HALF the apparent rate

Measured on the forward cohort 2026-08-29: of 16 closed rows, **8 close `unquotable_noquote`** —
censored by section 4, carrying no information. **50%.**

That reason matches BOTH naming families above and is one of the 57 overlap rows, so the forward
halving is not driven by a family either cut misses. All 8 are censored; all 8 `real_tp` rows
survive.

A count of closed positions is therefore not a count of informative ones, and the 150-paired target
needs on the order of **300 opened pairs** at the current unpriceable rate. Registered so nobody
reads "16 closed" as 16 informative. The rate rests on 8 pairs and should be re-measured as the
cohort grows; if unpriceability moves with market conditions, so does the read date.

## 7. Read date

Fixed before the window opens, and no peeking. The read is a single pre-registered analysis run by
a committed script with the cutoff, seed, censor predicate and dataset hash recorded in this
document before the first forward row.

### 7a. REGISTERED RETROSPECTIVELY on 2026-09-22T17:16:24Z (Gamma, observed SHA 9143c535)

**This section was empty until now, and the window opened 2026-08-29. The registration is
therefore RETROSPECTIVE, and that weakens it. It is recorded as what it is.** The estimand (section 3),
the arms (section 2), the unit (section 1), the censor (section 4) and the kill rules (section 6) were fixed
before the window and are NOT changed here. Only the read date, the cutoff, the seed and the
dataset hash are new, and each is fixed below before the read runs.

**What was already seen, by whom, before this registration.** Gamma, in
`docs/audits/2026-09-22-paper-lanes-quality-and-spend.md`, read and published: per-arm opened and
closed counts, per-arm total realized PnL on closes (uncensored), the exit-reason distribution, the
placeholder-exit rate, the per-chain split, the 17 closes above +300%, and the last-24 h closed
totals. Gamma did NOT compute the section 3 estimand, the paired population, or any bootstrap. A reader
who believes that peek could have shaped the choices below should discount this read accordingly.

- **Read date:** 2026-09-22T17:16:24Z. One read. A second read of the same estimand on the same cohort is a new
  registration, not a repeat.
- **Cutoff:** positions with `opened_at < 2026-09-22T17:16:24Z`. Closed rows only.
- **Population:** `execution_mode='paper'`, `strategy IN ('funded_clone','profile_matched_v1')`,
  `experiment_status='eligible'`, and the section 4 censor exactly as code:
  `measurable_outcome_sql('memecoin_positions')`, which already carries `HONEST_COPY_SQL`. The
  paper-peak clause of section 4 is NOT applied, per that section's own scoping.
- **Matching:** one observation per `origin_event_id` where BOTH arms carry a censor-surviving
  closed row. An origin with one surviving arm is dropped, because the difference is undefined.
- **Estimand:** section 3 as written. Realized PnL per dollar deployed, `profile_matched_v1` MINUS
  `funded_clone`, reported overall and per chain, Solana first.
- **Uncertainty:** bootstrap over clusters, resampling `(wallet, UTC day)` cells, 10,000 draws,
  **seed 20260922**, percentile interval at 95%.
- **Dataset hash:** sha256 over the sorted matched rows
  `(origin_event_id, network_id, source_user_id, utc_day, pnl_clone, notional_clone, pnl_profile,
  notional_profile)`, printed by the read script and recorded in the read report.
- **Script:** `scripts/paper_promotion_read.py`, committed with this section. `--dry-run` prints
  row counts and the dataset hash and NO estimand.
- **Outliers:** reported both ways, with and without closes above +300%. No row is deleted.

## 8. Cost model

Paper entry and final exit are ROUTE quotes (Jupiter on solana), with **one shared entry quote
across arms** — arms differ in policy, not in what they paid to learn the price. Intermediate marks
are not route quotes. This matters for the quote budget and for the residual `rate_limited` read in
kill criterion 2.

## 9. Consumers that must exist before the read

- A baseline script that counts `skipped_quote_<reason>@<host>` labels. The split records them; no
  consumer reads them yet. Named here so the read cannot silently have no input.
- The paper lane must announce `configured=<bool>` for its Jupiter key at process start. A key set
  in `.env` but absent from a process behaves identically to no key, and kill criterion 2 cannot
  distinguish the two without it.

## 11. Scanner coverage as a profile feature — measured, never gated

Routed here from the scanner-first assessment. The proposal was a per-wallet **anticipation rate**:
how often our own detector saw a wallet's coin before the wallet bought it. The reasoning was that a
wallet we consistently front-run adds no information we lack, while a wallet who beats our detector
carries real information.

**The reasoning is sound and the proposed quantity does not carry it.** Measured over the roster,
across all buys since the scanner's first row on 2026-08-21:

| quantity | pooled | dispersion vs binomial | verdict |
|---|---|---|---|
| anticipation (saw it first, GIVEN we detected) | 148/206 = 71.8% | chi2 ratio 0.89, bootstrap p = 0.67 | **no per-wallet signal** |
| coverage (did we detect the coin at all) | 206/855 = 24.1% | chi2 ratio 3.25, bootstrap p < 0.0001 | **real per-wallet axis** |

Anticipation is indistinguishable from a single common rate shared by every wallet. The wallets
showing 100% are carrying n of 1, 2 and 5. Anticipation measures OUR DETECTOR'S LATENCY, not the
wallet, so it cannot be a wallet-quality feature.

**Coverage is a real axis and is registered as a measured profile feature.** Restricted to wallets
with at least 20 buys:

| wallet | coverage | z under pooled |
|---|---|---|
| LevisNFT | 54% | +4.3 |
| KaminaSOL | 47% | +4.3 |
| CryptoTalkMan | 29% | +0.9 |
| DegenCapitalLLC | 28% | +0.8 |
| yayayayayey | 23% | -0.1 |
| hopa | 22% | -0.3 |
| frankdegods | 15% | -2.1 |
| sunnydog06 | 0% | -2.7 |

**The reading, and the tension in it.** High coverage means the wallet trades inside the universe our
scanner already sees, so its marginal information is low and our own detector is a candidate
substitute for it. Low coverage means the wallet trades where we are blind, which is genuinely
additive — **and simultaneously means we cannot validate it with our own instrument.** Additive and
unverifiable are the same reading of the same number. Neither end is straightforwardly "better", so
this feature ENTERS THE PROFILE AND GATES NOTHING.

It is recorded per wallet at the cohort cutoff and reported beside the primary read. It does not
enter `profile_matched_v1`'s rules in this study, and no promotion or demotion may cite it. If it
earns more, that is a later study with its own pre-registration.

Note for interpretation: `KaminaSOL`, at 47% coverage, is the wallet disarmed after the freeze
(section 0). That is consistent with the substitution reading and is not evidence for it — one
wallet is an anecdote.

## 12. RULING: the cohort RESTARTS. The pre-incident rows are discarded, not censored.

**Incident, 2026-08-29.** From the 09:24:06Z flip until the ~10:19Z pause, the study's arms priced
through a funded Jupiter budget running about **40% rate-limited**. Kill criterion 2 names 10% and
says the study is then measuring our own limit; it fired at four times threshold and the pause
honoured it.

**Ruling: restart at the clean re-flip. The pre-incident rows do not enter this study in any form.**

### Why not censored

**Censoring is the wrong instrument, and the reason is worth stating precisely.** A censored
observation is one whose OUTCOME is unobserved or partially observed — that is what section 4's
`HONEST_COPY_SQL` and `measurable_outcome_sql()` handle, and what a missing `executable_peak_price`
is. These rows have fully observed outcomes. What differs is the DATA-GENERATING PROCESS that
produced their fills.

That is a **regime change**, not censoring. You do not censor a regime; you bound the window and say
where the boundary is.

### Why not kept as a labelled stratum

Both arms shared the degraded key, so the artifact plausibly cancels in a paired difference. **That
is an assumption, not a measurement**, and this document does not need to make it. The whole cohort
to date is 13 pairs — first open 09:29:31Z, last 10:18:26Z, every row inside the degraded window —
against a target of 150 paired observations. Discarding 9% of the target is cheaper than carrying an
unverified symmetry claim through every later read.

This matches the ruling already made on the scalp probe: excluded, not a pilot stratum, with one
line recording that it existed.

### The boundary

The new cohort cutoff is the **first qualifying row after the per-call key split is verified live**,
recorded here as a UTC timestamp when it happens, not chosen afterwards:

    RESTART_CUTOFF = 2026-08-29T13:42:20Z          <- the VERIFICATION instant, exclusive
    first cohort members: positions 45135 (funded_clone) / 45136 (profile_matched_v1),
                          opened_at 2026-08-29T13:45:31.613731+00:00

**The cutoff is the verification instant, NOT the first row's timestamp, and the distinction is not
cosmetic.** Setting it to a value that a row EQUALS makes that row's membership depend on string
formatting, because `opened_at` is stored with a `+00:00` suffix and these comparisons are string
comparisons:

    opened_at '...13:45:31.613731+00:00' > cutoff '...13:45:31.613731'         -> INCLUDED
    opened_at '...13:45:31.613731+00:00' > cutoff '...13:45:31.613731+00:00'   -> EXCLUDED

Same intent, opposite result, decided by whether whoever wrote the cutoff copied the timezone
suffix. **No row sits at 13:42:20Z**, so the verification instant is a clean exclusive bound under
any formatting, and the first pair is named by ID above so membership never has to be re-derived
from a boundary at all.

**The measurement that cleared the gate**, on the section 6 partition, Solana, window 10:19Z to
13:42:20Z:

    denominator (quote attempted)   50      floor met exactly
      priced (cannot hide a limit)  49
      attributed skip (no_route)     1
      UNATTRIBUTED (U)               0
      rate_limited (R)               0
    bound [R, R+U] = [0.00%, 0.00%]   PASS (< 10%)

**Verified independently before stamping, and it reproduces.** Two properties of the verification
belong in the record.

**The share condition is ROBUST to the window boundary; only the count is marginal.** U = 0 and
R = 0 at every candidate start tested — 10:19Z gives a denominator of 50, 11:00Z gives 47, and the
current release's deploy at 13:36:11Z gives 3 — so the bound is [0.00%, 0.00%] in all of them. The
boundary changes only whether the n >= 50 floor is met, never the share. That matters because a
result that flipped with the window would not be a measurement.

**CAVEAT, AND IT IS NOT A BLOCKER BUT MUST NOT BE LOST.** The 50-row window largely predates the
current release: the runner deployed at **13:36:11Z**, six minutes before the verification, so 47 of
the 50 attempts ran on the previous build. More importantly the window ran with **ARMS off**, and
ARMS-off is what remedied the incident by cutting total IP volume rather than by unpooling a key.

**So U = 0 was measured at reduced load, and the resuming study restores that load.** The gate passes
on the registered condition — which asks about the regime after the remedy, and this is that regime
— but the evidence is weaker than the same numbers gathered at full load would be.

**Registered re-check, therefore:** once the resumed cohort has produced 50 qualifying Solana
attempts AT FULL LOAD, re-run the section 6 partition. If the bound rises above 10% there, kill
criterion 2 fires again and section 13's resumption rule applies from the start. This is not a new
criterion; it is the existing one measured on the population it was written for.

### RE-CHECK SATISFIED, 2026-08-29 — and it is strictly stronger than the resumption read

Measured post-cutoff (`decided_at > 2026-08-29T13:42:20Z`), Solana, on the section 6 partition, and
reproduced independently by two desks:

    denominator (quote attempted)   50      floor met exactly
      priced (cannot hide a limit)  50
      attributed skips               0
      UNATTRIBUTED (U)               0
      rate_limited (R)               0
    bound [R, R+U] = [0.00%, 0.00%]   PASS

**Criterion 2 does not fire. Section 13 does not apply. The study continues uninterrupted.**

**Why this is stronger than the read that resumed the study, rather than merely equal to it.** Two
reasons, and the first is the one worth keeping:

1. **The denominator is now PURE.** The resumption read was 49 priced plus 1 attributed `no_route`,
   so its bound rested partly on that single skip having been labelled correctly — it needed the
   label to be right. **This read has 50 priced and zero attributed: there is no labelling question
   to trust at all.** A bound that depends on no label is a different quality of evidence from one
   that depends on one.
2. **It was gathered at full load**, with the arms actually running, which is the condition the
   criterion was written for. The resumption evidence was collected with ARMS OFF at reduced IP
   volume, and this document registered at the time that the same zeros under load would be stronger.
   They are.

That closes the loop this morning's kill-criterion fire opened: **paused by measurement, resumed by
measurement, re-verified under the load the pause was about.** It is the first end-to-end exercise of
sections 6 and 13, and both held.

### The bound is on LOST DECISIONS, not on 429s — a third instrument makes this explicit

A third instrument, the in-process `by_role` counter, was read on the same condition and **agrees on
the verdict while disagreeing on the count**:

| instrument | reading | verdict |
|---|---|---|
| durable table, pre-cutoff | 49 priced + 1 attributed `no_route` | PASS, bound [0.00%, 0.00%] |
| durable table, post-cutoff | 50 priced, 0 attributed | PASS, bound [0.00%, 0.00%] |
| in-process `by_role` counter | 51 quotes, **1 rate_limited** = 2.0% | PASS, upper95 8.5% |

**They measure different EVENTS and both are right.** Confirmed in code at
`memecoin_v6_paper_quotes.py:146-169`: the lane tries a second host when the first fails,
`_note_role_rate_limited` fires **per 429 per host** and then `continue`s, while the recorded action
is the **LAST host's** reason. So a 429 recovered by host failover is +1 on the counter and a
**priced** row in the table.

**Therefore: `rate_limited = 0` in the table does NOT mean no 429 occurred. One did.** The bound of
section 6 is on rate limits that ENDED A DECISION, not on rate-limit events, and it must not be
quoted as the latter.

**That is the correct event for this criterion** — the rule exists because rate limiting can
invalidate the study, and a 429 that recovered invalidated nothing; the decision was made on a real
quote. So the durable table remains the right gate instrument, for reproducibility AND for measuring
the right thing.

**But the counter is the better EARLY WARNING**, precisely because it sees pressure before pressure
becomes a lost decision. Registered as a watch surface, not as a gate.

**One asymmetry to watch, explicitly NOT a claim.** The counter currently reads **paper 1 of 51
rate-limited against funded 0 of 156**. That is the shape predicted by the per-IP contention
mechanism, which until now has only ever been inferred. **At a single rate-limit event it is not
evidence of anything** and must not be cited as confirmation. It is worth watching now that the
counter partitions by role honestly.

**The cutoff is applied to `opened_at`, never to `closed_at`, and that distinction is
load-bearing.** As of 2026-08-29 the arms hold **108 stuck open positions** (54 per arm, opened
09:29:31Z to 13:06:40Z) which will close on the next runner deploy. Their CLOSE timestamps will land
AFTER `RESTART_CUTOFF` even though every one of them opened before it, so a filter keyed on
`closed_at` admits the entire pre-restart cohort while a filter keyed on `opened_at` excludes it.

Nothing OPENED before the cutoff enters, including the 26 positions of 2026-08-29T09:29:31Z to
10:18:26Z and the 108 above.

### The discarded rows are still evidence, for a different question

They are useless for the wallet question and clean for an infrastructure one: the same two arms ran
before and after the key split, so the pair is a natural before/after on whether the split changes
fill quality. Keep them queryable and labelled. Do not delete them, and do not let them back into
this study.

### Outcome, 2026-08-29: the marker survived an activation race

The closure branch shipped with an empty-default activation stamp — the right default, since it
cannot misfire on later deploys — and the deploy went out before the knob was armed. **78 of the 108
closed on rails at four-hour ages before the marker was live**, and were relabelled afterwards; the
remaining 30 closed labelled. All 108 now carry `exit_reason='sweep_closure'` with
`realized_pnl IS NULL`, verified in the ledger.

**The repair was clean because the ruling PREDATED the event.** Its predicate came from section 12,
not from judgement after the fact — and that distinction is the whole value. A repair decided after
seeing an outcome is indistinguishable from a repair FITTED to that outcome, even when it is
correct. Deciding the rule first is what makes the fix auditable rather than merely defensible.

**The two groups remain separable, which was not designed and is worth knowing.** Their close
timestamps differ: 78 at `13:18:34` and 30 at `13:20:12`. So a later reader who needs to distinguish
rail-closed-then-relabelled from swept-live still can, even though both carry one label.

**What the relabel cost, recorded rather than glossed.** The original rail reason for those 78 is in
no column — `exit_reason` and `close_exit_reason` both read `sweep_closure` — and there are ZERO
`memecoin_exit_trigger_audit` rows for any of the 108. So "which rail closes a stuck position at four
hours" is unrecoverable for this cohort. The loss is small, because all 78 share a single close
second and were therefore one batch decision rather than 78 independent ones, but it is a real
overwrite on a ledger whose doctrine is that nothing is hidden.

The `opened_at` pin held throughout as the backstop, which is what it was for: the study's exclusion
never depended on the marker landing.

## 13. Every kill criterion needs a resumption rule

Section 6 says STOP three times and never says how to START AGAIN. A fired criterion therefore left
the study in an undefined state, and the ruling above had to be made after the fact rather than
read off the document. That is a defect in section 6, not in the incident.

Registered generally, for all three criteria:

1. A fired criterion pauses the study. It does not end it.
2. Resumption requires the criterion's condition to be **verified false by measurement**, not
   assumed fixed by a deploy. For criterion 2 that means an observed `rate_limited` share under 10%
   on the study's own key after the split, Solana-scoped, with the attempt COUNT reported beside the
   share and at least 50 attempts.

   **AND THE INSTRUMENT MUST HAVE A POSITIVE CONTROL.** A zero from a counter that has never
   recorded a positive is not a measurement of the condition; it is a measurement of the counter.
   As of 2026-08-29 `memecoin_entry_snapshots` holds **zero `rate_limited` rows in 19,047 all-time
   attempts**, including **zero across the incident window itself** — 31 attempts between 09:24 and
   10:19 on a lane Alpha measured at 40% rate-limited, and not one label. The only
   `skipped_quote_*` row ever written is a `no_route` at 11:07:21.

   So "0 rate_limited in 478 Solana attempts" does not clear this gate. An instrument that returned
   zero through a 40% hour cannot distinguish 0% from 40%, and reading its zero as a pass is the
   verification-that-cannot-fail pattern. Before the gate can be satisfied, the label must be shown
   to land: a test that writes a `rate_limited` row and reads it back, or a cross-check against a
   source that DID observe the incident's 429s.

   **THE DENOMINATOR, REGISTERED.** It is every Solana decision where a quote was ACTUALLY
   ATTEMPTED: all actioned Solana rows MINUS the pre-quote refusals. `skipped_base_asset` is logged
   at `memecoin_copy_trader.py:525`, before the quote call at :533, so it can never be rate-limited
   and its inclusion only dilutes. This is the same principle that scoped the rule to Solana — the
   denominator is the population that could exhibit the condition.

   **Note the safety direction.** A larger denominator makes the share SMALLER and so trips this
   kill rule LESS often. Padding it is the PERMISSIVE choice, not the conservative one, which is why
   it is registered rather than left to judgement.

   **THE GATE CLEARS ON THE UPPER BOUND, NOT ON THE POINT ESTIMATE.** Partition the denominator:

   | class | can it hide a rate limit? |
   |---|---|
   | a price came back (`opened`, `skipped_chase`, `skipped_dump`) | no — the quote succeeded |
   | attributed skip (`skipped_quote_<reason>@<host>`) | only if the reason IS rate_limited |
   | everything else attempted (`skipped_no_executable_quote`, `ignored`) | **yes — unattributed** |

   With R the measured rate_limited share and U the unattributed share, the true share lies in
   **[R, R+U]**, and the gate is cleared only when **R + U < 10%**.

   **This dissolves the positive-control problem rather than working around it.** A broken
   `rate_limited` label can only move counts from R into U, and the bound spans both — so the gate
   never requires trusting a label that has never fired. It requires only that the unattributed
   residual is small enough that 10% is excluded either way. The positive control remains worth
   building as an instrument check; it is **not** a precondition for resumption.

   **Measured 2026-08-29, Solana post-split:** denominator 43 — 42 rows where a price came back, 1
   attributed skip (`no_route`), and **U = 0**. The bound is [0.00%, 0.00%], a point rather than an
   interval, because every attempt is accounted for. **The share condition is satisfied; the gate is
   blocked only on the n >= 50 floor, at 43.** All-time Solana for contrast: denominator 7,782 with
   U = 6.45%, so the bound is [0%, 6.45%] and clears as well — but all-time spans the incident era
   the restart exists to exclude, so it cannot be the resumption evidence.
3. Every resumption sets a new cutoff and **discards rows generated while the criterion was true**,
   for the reason in section 12: a fired criterion means the data-generating process was not the one
   registered.
4. The pause window and its reason are recorded in this document at the time, never reconstructed
   later.

---

## 14. Lane A — the DISARMED-wallet promotion read (registered 2026-09-23 14:5xZ, before any row)

Registered before Lane A accrues its first row. Lane A papers every disarmed wallet the J7 relay
delivers as if it were armed: full gate stack, funded clip, frozen exit policy, a real Solana quote,
and no paper position at all when a gate would have refused the signal. This section registers only
the READ; the build is Beta's.

**HOW THIS ESTIMAND DIFFERS FROM SECTION 3, so nobody sums them:** section 3 contrasts TWO PAPER ARMS
inside the ARMED cohort (`profile_matched_v1` minus `funded_clone`, paired by wallet, per dollar
deployed) and answers a POLICY question; section 14 asks whether a single DISARMED wallet × chain
clears zero against an execution haircut and answers a ROSTER question. Different population,
different unit, different contrast, one-sided against zero rather than paired between arms. They are
never added, averaged or reported in one column.

Sections 0 (identity rule), 1 and 4 (censoring) apply here unchanged.

### 14.1 What a PASS authorises

**Paper nominates. Paper never arms.** The operator's standing rule (2026-09-11) is that roster
changes use funded results only. A Lane A pass produces one thing: a candidate for a **bounded funded
trial** with its own pre-registered chain, clip, stop and close count. That trial's funded record is
what arms or retires the wallet. Settled by Alpha 2026-09-23 14:5xZ after the collision was raised.

A future reading in which a paper pass arms directly supersedes the 2026-09-11 rule and must be
recorded here as an explicit operator decision with its date. It is not implied by a pass.

### 14.2 The cohort, pinned

Three sessions counted "disarmed" on 2026-09-23 and got three denominators, all defensible readings:

| reading | wallets | closes | funded PnL |
|---|---:|---:|---:|
| wallet-level arm state, ignoring `real_chains` | 93 | 734 | −$2,799.70 |
| **wallet × chain arm state (THIS SECTION)** | **126** | **1,029** | **−$3,568.01** |
| the J7-relayed subset | 46 | 444 | −$2,508 |

**THE UNIT IS THE WALLET × CHAIN PAIR at arm state as of the readout.** Promotion is a per-chain
decision, so a close on a chain the wallet is no longer armed on belongs to the disarmed side even
when the wallet is armed elsewhere; the wallet-level reading pools those closes into the armed side
and flatters it. Source of truth is `memecoin_wallets`, exactly these four conditions, all of which
must hold for ARMED ON THAT CHAIN:

```text
real_approved = 1
COALESCE(excluded, 0) = 0
COALESCE(paused, 0)   = 0
canonical_chain_id(<chain>) ∈ {canonical_chain_id(x) for x in split(real_chains, ',')}
```

⚠️ `real_chains` carries chain IDs while `funded_chain_keys()` returns NAMES. Never intersect them
without `canonical_chain_id()` (`sniper/services/memecoin_chains.py:48`). Doing so once put twelve
armed wallets in "armed nowhere".

The armed side under this definition is 37 wallets over 60 wallet-chain pairs, 410 closes, +$5,576.52.

### 14.3 The unit of evidence is the distinct coin

**One coin is one bet.** Several paper rows on one mint for one wallet are ONE observation. Every
count, median and minimum below is over distinct mints per wallet per chain.

This is the difference between a finding and an artefact. The gate-less paper lane's 2026-09-23
headline was disarmed wallets at +$1,901 over 256 would-pass rows, mean +$7.43 against the armed
set's +$2.10. Under row counting the top five rows are 83% of that sum and hold **three** distinct
coins; under coin counting the headline does not exist.

**ONE ARM ONLY.** Every signal writes both `funded_clone` and `profile_matched_v1`, so pooling doubles
n and adds nothing. Name one as primary and report the other as a replication, never summed.

**NEVER A COHORT VERDICT.** Promotion is per wallet per chain; the cohort mean is the statistic that
produced the mirage above and carries no promotion meaning at any n.

### 14.4 Entry gate

At least **20 distinct coins**, over at least **14 days**, spanning at least **10 distinct UTC days**.
The day spread exists because one session or one regime can supply twenty coins — three of the eight
wallets behind the 2026-09-23 headline had n=1, all losses, which a coin minimum alone does not stop.

**REACHABILITY IS NOT ESTABLISHED.** n = 20 is chosen so a sign test has any power at all, not derived
from Lane A's arrival rate, which nobody has stated. When Beta reports coins per wallet per week,
recompute whether 20 is reachable inside 14 days and record the answer here. If it is not, this gate
is wrong and must change BEFORE any verdict is read, never after.

### 14.5 The statistic

**Primary: the median per-coin paper PnL for that wallet × chain, minus a measured execution haircut,
with a sign test on the per-coin distribution.** One-sided against zero.

Median, not mean. On the 2026-09-23 numbers the two disagree in direction — by median the armed set
leads (+$8.58 against +$6.57), by mean the disarmed set leads. The mean is moved by one wallet
supplying 74% of the sum; the median is not.

**THE HAIRCUT MUST BE MEASURED, NEVER ASSUMED.** Paper pays no impact and no route failure, so Lane A
is a **CEILING**. The gap is already measured and it is the whole story: disarmed wallets' coins are
neutral in paper (median +$0.08 over 1,275 `not_approved` signals) while the same wallets' real money
is −$3,568.01. Coins neutral, money negative — the difference is entirely execution. The haircut is
the **median of (paper-modelled PnL − real PnL) over coins where we hold BOTH**, per chain and per
clip band; 54% of the disarmed paper PnL sits on coins the armed set also traded, and that overlap is
the matched set that makes it computable. **Until the haircut exists for a chain, no wallet can PASS
on that chain.**

### 14.6 Verdicts

**PASS** requires all four:

1. haircut-adjusted median per-coin PnL > 0 with the sign test significant at the registered level;
2. the verdict survives **leave-one-coin-out** for every coin;
3. coin-level win rate at or above the armed set's on the same gate stack and chain;
4. the wallet's funded record on that chain is non-negative, or the adjusted median covers the
   recorded deficit at that wallet's historical close count.

Condition 4 matters most. The wallet supplying 74% of the paper headline, `prevaile`, is **−$222.69
over 14 funded closes** and is the worst wallet on the disarmed side. Conditions 2 and 4 each reject
it independently.

**FAIL:** n ≥ 20 coins with adjusted median ≤ 0. No re-test for 30 days, so a wallet cannot be
re-rolled until a window happens to favour it. Resumption follows section 13.

**INSUFFICIENT:** the verdict flips under leave-one-coin-out; n short on coins, window or distinct
days; or more than 25% of coins still open (censored per section 4, never counted as zero).

60 of the 76 disarmed wallets with funded closes are negative. The prior this rule must overturn is
strong and the bar is set to match.

### 14.7 Two contamination rules

**THE COMPARISON SET MUST USE THE SAME GATE STACK.** Lane A papers only signals a gate would have
passed, so its denominator is a function of our own gates. A wallet-versus-wallet comparison is valid
only when the armed comparison set is filtered identically; otherwise the share describes our gate
stack, not the wallets.

**COIN COUNTING DOES NOT FIX NON-INDEPENDENCE.** With 54% of disarmed paper PnL on coins the armed set
also traded, the groups share market moves. For any armed-versus-disarmed contrast, **pair on the
coin and test the paired difference**; never compare two marginal distributions.

### 14.8 A registered SECONDARY, which is not part of any promotion decision

`memecoin_shadow_signals` already rates disarmed signals `candidate` / `watch` / `reject` and joins
Lane A on `origin_event_id`. **Cardinality verified 2026-09-23: 3,305 rows over the last 7 days and
3,305 distinct `origin_event_id`, so the join is 1:1** — re-verify before any read, because the
table's own uniqueness is on `episode_id` and on `(network_id, mint, episode_id)`, not on
`origin_event_id`, so nothing in the schema prevents that from changing. `wallet_ab_pnl` and
`wallet_ab_closes` are populated on 100% of those rows; whether they are per chain is UNVERIFIED and
must be checked before they stand in for condition 4.

The secondary question is whether a `candidate` rating predicts the paper outcome, measured on the
same per-coin unit. **It is descriptive only.** A rating never enters PASS, FAIL or INSUFFICIENT
unless it is separately validated and registered here with a date — an unvalidated input must not
reach a decision that ends in funded money.

### 14.9 What section 14 cannot answer

- Whether a passing wallet is profitable when funded. Only the bounded funded trial answers that.
- Anything about route failure or impact, which paper does not model.
- Whether the original disarm was correct. A FAIL is consistent both with a correct disarm and with a
  wallet that has since changed; this read does not separate them.

### 14.10 Amendment rule

Every change to 14.2 through 14.7 after the first Lane A row exists is recorded here with its date,
its reason, and whether any verdict was already read under the old text. A threshold moved after
seeing data is not a pre-registration.
