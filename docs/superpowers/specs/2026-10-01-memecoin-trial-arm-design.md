# Memecoin trial arm: a small-clip funded trial for new or re-arm candidates (design)

**Author:** Beta · **Order:** the operator, relayed by Alpha 2026-10-01 ~07:50Z: "a wallet armed at a small clip
for N funded trades, graded on percent return per trade, auto-promoted or disarmed by a rule Gamma
pre-registers; reuse the arming and clip mechanisms". **Rule:** Gamma's pre-registration
`docs/audits/2026-10-01-trial-arm-promotion-preregistration.md` (3a0b141ee). **Status:** PROPOSED, spec only.
Nothing is built. Every value marked *operator* needs the operator's yes before the build.

## 1. What it is

A trial is one (wallet, chain) armed for funded copying at a small fixed clip. Every position it opens
carries a trial marker frozen into its funded contract. Gamma's rule grades the trial's closes on percent
per trade (r = realized_pnl / entry_notional). The trial then ends as PROMOTE, DEMOTE or INSUFFICIENT, or it
continues. A promoted wallet stays armed at the normal clip and is graded by the operator's roster rule
from then on. A demoted wallet is disarmed on that chain.

The trial is a cheap screen, not proof of edge. Gamma's operating characteristics say so plainly: a
zero-edge wallet passes about 3 times in 10. The roster rule on full-clip results remains the protection.

## 2. Reused mechanisms (nothing new where something exists)

| need | existing mechanism |
|---|---|
| arm / disarm one chain | `db_memecoin.set_wallet_chain_armed` (one transaction: flag, chain list, audit row; promote gate overridable with `force` by the operator) |
| cap the entry size | `real_entry(..., size_cap_usd=...)`, already used by the Pons creator-tax test (`_pons_test_arm`) |
| freeze the marker | `build_funded_contract`, called once inside `reserve_entry`'s `BEGIN IMMEDIATE` |
| count against capacity | `reserve_entry`'s capacity snapshot, in the same transaction |
| page the operator | `memecoin_alerts.send_alert` |
| shared-origin check | the shortlist's test 6 (`scripts/research/alpha_wallet_shortlist.py`, `SHARED_S = 30.0`) |
| re-observation clears authority | already true: restoring an excluded wallet sets `real_approved=0` and clears its chains, and pausing does the same |

## 3. The marker (the rule's "trial close")

`build_funded_contract(..., trial=...)` adds one key inside the contract's `source` block, present only
on trial positions:

```json
"source": {"...": "...", "trial": {"trial_id": 17, "rule": "3a0b141ee", "clip_usd": 15.0}}
```

- **In `source`, not at the top level or in `policy`.** `_validated_policy_data` rejects any key it does
  not know, so a policy field would make every pinned prior release raise on these contracts. A rollback
  would then strand open positions with no readable rails. `source` already tolerates unknown keys:
  `copy_arm` and `exit_override` live there for the same reason.
- Contracts are immutable and hash-checked across attempt, pending and position, so the marker cannot be
  edited after reservation. A non-trial contract stays byte-identical to today's.
- A trial close is a position with `is_open=0`, `realized_pnl` not null, `signal_source != 'manual'` and
  `json_extract(funded_contract_json, '$.source.trial.trial_id')` equal to the trial's id.
- Manual EXITS of a trial position count. They put operator selection into r (closing a loser early or a
  winner early), so every verdict page marks each manual exit. When manual exits are more than 20% of a
  trial's closes, the verdict is reported twice, with and without them (Gamma LOW 1).
- A wallet re-armed for a second trial gets a new `trial_id`. Closes never carry over between trials.

## 4. The clip

Gamma's rule: the chain's median round-trip FIXED fee must be <= 2% of the trial clip. Measured
2026-10-01 with `scripts/research/trial_clip_fees.py` (our own fills' receipts, research endpoints only):

| chain | closes | fixed fee per round trip, median | p90 | 2% floor (median) | 2% floor (p90) |
|---|---|---|---|---|---|
| 4663 | 20 | $0.026 (gas at ETH $2,677) | $0.110 | $1.28 | $5.52 |
| solana | 20 | $0.025 (0.00021 SOL at $119.5) | $0.131 | $1.25 | $6.56 |

- Solana fees are bimodal. Every close since 2026-09-27 pays about 0.0011 SOL (an entry priority fee of
  988,000 lamports), so the current regime is the p90 column, not the median.
- Not fixed, so not counted here: LP fees, price impact and token taxes. They scale with the clip.
- Token-account rent (about 0.002-0.003 SOL per entry) is a DEPOSIT that `memecoin_rent_sweep` returns.
  Token-2022 accounts are not swept (a known gap), so rent is a real cost there.
- 4663 approvals are not linked to fills. The pre-approval table's median is 0.00000093 ETH, about $0.0025.

**Proposed clips (*operator*):** solana **$15**, 4663 **$10**.
- Fee share at the current regime: Solana 0.87% ($0.13 / $15), 4663 1.1% at p90, 0.26% at median.
- Both are above `MEMECOIN_MIN_TRADE_FLOOR_USD` ($5) and well below today's clips (Solana cap $175, 4663
  tiers $100/$100/$170).
- Each verdict report shows the fee share beside r, as the rule asks.

Other chains are not trial chains until their fee is measured with the same script.

## 5. Lifecycle

### 5.1 Start (operator)

`scripts/memecoin_trial_arm.py start <handle> <chain>` is a dry run by default; `--confirm` writes (the
operator runs it).
1. Refuses when the chain has no trial clip configured, the wallet is excluded, or a trial is already
   open for (wallet, chain).
2. Reports delivery: J7 tracking (`j7_tracked_wallets`) and the wallet's signal events on that chain in the
   last 7 days. Zero signals refuse, because the trial would only expire.
3. Inserts the trial row, then calls `set_wallet_chain_armed(..., armed=True, force=True,
   reason="trial 3a0b141ee #<id>")`. If the arm is refused, the trial row is deleted and the refusal
   printed. `force` is needed because several candidates fail the copy-PnL promote gate today; that
   failure is why they are trials.

### 5.2 Entries (runner)

- Entry authority is already read from the ledger per signal: `real_approved_user_ids(conn,
  network_id=chain)` at `memecoin_runner.py:141`. The open-trial lookup is one indexed read on
  `memecoin_trial_arms` at the same point. The build measures its p50/p90; if it shows up, the trial set
  moves to a per-loop cache. If an entry's (source, chain) has an open trial:
  - `size_cap_usd` is the trial clip;
  - the trial is passed to `reserve_entry`, which freezes the marker.
- Inside `reserve_entry`'s existing `BEGIN IMMEDIATE`, two extra refusals, each recorded with its own
  reason so it can be counted:
  - `trial_concurrency`: open trial positions plus unresolved trial reservations would exceed
    `MEMECOIN_REAL_TRIAL_MAX_CONCURRENT` (*operator*, proposed 2, so at most $30 of trial exposure).
    It is counted over the rows the capacity snapshot already reads: open real positions and
    unresolved reservations, bounded by the global concurrency cap, so at most a few rows and never a
    scan of `memecoin_positions`;
  - `trial_full`: the trial already holds 20 positions, open or closed (the rule never needs a 21st).
    A counter `positions` on `memecoin_trial_arms` is incremented when a trial position books. The cap
    is `positions` plus the trial's unresolved reservations (from the same snapshot), read by primary
    key. No json scan runs inside the transaction (Gamma LOW 2).
- Trial positions also count against the global concurrency and exposure caps. A trial gets no extra
  slots.
- Every other gate (chase caps, volume gates, safety, the dip retry list) is unchanged. A trial wallet is
  copied like any armed wallet, only smaller.

### 5.3 Grading (runner maintenance, off the entry path)

- Every 60 s the runner applies a pure function `trial_verdict(closes, started_at, now)` to each open
  trial. It implements the rule exactly, with r as a fraction:
  - early DEMOTE at 5 closes when the mean is < -0.30;
  - at 10: DEMOTE when the mean is < -0.10; PROMOTE when the mean is > +0.10 and the mean without the
    best coin is > 0 and the best coin's share of the positive sum is <= 0.50; otherwise CONTINUE;
  - at 20: PROMOTE when the mean is > +0.05 and the same two conditions hold; otherwise DEMOTE;
  - INSUFFICIENT when fewer than 10 closes exist 14 days after the start;
  - SHADOW when >= 50% of the closes were also bought by a wallet armed on that chain within 30 s.
    SHADOW overrides PROMOTE.
- A close with a null or zero `entry_notional` has no r. It is listed and excluded, never counted as 0.
- A verdict is evaluated only when the close count reaches 5, 10 or 20, or at day 14. It is written once
  to `memecoin_trial_verdicts`. The page lists every close (position, mint, r, exit reason, fee share),
  the mean, the median and the 90% bootstrap CI by mint (4,000 draws, seed 20260930).

### 5.4 Actions (*operator*: which ones are automatic)

`MEMECOIN_REAL_TRIAL_AUTO` decides which actions are automatic; it is read at call time.

| verdict | `off` | `demote` (proposed default) | `both` |
|---|---|---|---|
| DEMOTE | page; trial stops taking entries | end trial, disarm the chain, page | same as `demote` |
| INSUFFICIENT, SHADOW | page; trial stops taking entries | end trial, disarm the chain, page | same |
| PROMOTE | page; trial stops taking entries | page; trial stops taking entries | end trial, chain stays armed at the full clip, page |
| CONTINUE | nothing | nothing | nothing |

- **The conflict.** The operator's order says auto-promote AND auto-disarm. Gamma's pre-registration says
  every promotion or demotion is a recommendation. `demote` is proposed as the default for two reasons:
  - removing a clear loser fast is what the screen is good at (a -10% wallet is demoted 95%+ of the time);
  - an automatic promotion would put 3 in 10 zero-edge wallets on the full clip with no human look.
  `both` is one env line away if the operator wants it.
- "Stops taking entries" means the trial row gets `stopped_at`. The runner then refuses that wallet's
  entries on that chain with reason `trial_stopped`. It never falls back to the full clip.
- Every disarm goes through `set_wallet_chain_armed(..., armed=False, actor="trial_rule")`, so the audit
  row names the rule.

### 5.5 The existing auto-demote

The runner already revokes a chain on its own:
- when a wallet's cohort loses `MEMECOIN_REAL_DEMOTE_LOSS_USD` ($100 in `.env`);
- or after `MEMECOIN_REAL_DEMOTE_CONSEC` consecutive losses (4 in `.env`).

This is `actor="auto_demote"` in `memecoin_real_runner.py`. Left on, it would end a trial after 4 straight
losses, before the rule's first check at 5 closes, and change the rule's operating characteristics. At
31-36% per-close SD, 4 losses in a row happen often even to a wallet with a positive edge.

**Proposed (*operator*):** while a trial is open, its marked positions are left out of the auto-demote
cohort for that (wallet, chain). The trial rule is the demotion rule. Once the trial ends, the
auto-demote applies again from a fresh cohort, as it does after any re-arm.

### 5.6 End by other means

- **Approval cleared** (pause, exclusion, re-observation, or a roster disarm): the next grading pass sees
  the chain unarmed and ends the trial with `approval_cleared`. A re-arm needs a new trial.
- **Operator end:** `scripts/memecoin_trial_arm.py end <handle> <chain> --keep-armed|--disarm --confirm`.
  One of the two flags is required, so ending a trial never silently leaves the wallet on the full clip.

## 6. Data (proposed tables, in `db_memecoin`)

<!-- schema:external=memecoin_trial_arms -->
<!-- schema:external=memecoin_trial_verdicts -->
```sql
CREATE TABLE IF NOT EXISTS memecoin_trial_arms (
  id INTEGER PRIMARY KEY, source_user_id TEXT NOT NULL, chain TEXT NOT NULL,
  clip_usd REAL NOT NULL, rule TEXT NOT NULL, started_at TEXT NOT NULL, actor TEXT NOT NULL,
  positions INTEGER NOT NULL DEFAULT 0,
  stopped_at TEXT, ended_at TEXT, end_reason TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS idx_trial_open ON memecoin_trial_arms(source_user_id, chain)
  WHERE ended_at IS NULL;
CREATE TABLE IF NOT EXISTS memecoin_trial_verdicts (
  id INTEGER PRIMARY KEY, trial_id INTEGER NOT NULL, checked_at TEXT NOT NULL, closes INTEGER NOT NULL,
  mean_r REAL, verdict TEXT NOT NULL, action TEXT, detail_json TEXT NOT NULL);
```

`clip_usd` is copied into the row at start, so a later env change never re-sizes a running trial.

## 7. Edge cases (CLAUDE.md rule 5)

1. **Money path:** yes, for entry size, arming and disarming. Every item below applies. The automatic
   disarm is the only action taken without a human, and the default allows it only for demotion.
2. **Fill speed:** one indexed read on `memecoin_trial_arms` beside the per-signal authority read
   (§5.2). Inside `reserve_entry` the trial checks use the capacity snapshot's rows plus one primary-key
   read, in a transaction that already runs.
   - The build measures p50/p90 of `reserve_entry` and of the lookup, with and without a trial.
   - **Never on a copy of the ledger:** a 9 GB copy OOM-killed the funded runner on 2026-09-27, because
     /tmp is RAM on this host. Measure on a generated DB: the production schema (`init_db`), realistic
     row counts (`memecoin_positions` about 70k, the trial tables, every index), with and without a
     trial (Gamma MEDIUM).
3. **Concurrency:** the runner thread and the follow-activity thread both reserve. The trial caps are
   checked inside the same `BEGIN IMMEDIATE` as global capacity, so two threads cannot both take the last
   trial slot. The in-memory trial set is replaced by assignment, never mutated in place.
4. **Absent vs zero:**
   - no trial clip for a chain means no trial on that chain, never the full clip;
   - a trial row with `stopped_at` set refuses entries, never falls back;
   - a null or zero `entry_notional` excludes the close, never r = 0;
   - a missing contract marker means not a trial close.
5. **Dependency failures:**
   - a grading exception means no verdict this pass, logged with the trial id; the next pass retries;
   - a refused disarm pages "DISARM FAILED", and the trial stays stopped;
   - the page is best-effort and never blocks the action.
   - Partial start: the trial row without an armed chain is deleted. An arm without a row cannot happen,
     because the row is written first.
6. **Units and identity:** r is a fraction and the thresholds are written as fractions (-0.30, not -30).
   Chains are canonical ids (`solana`, `4663`), via `canonical_chain_id`. The trial key is
   (source_user_id, chain), never the handle.
7. **The sample:** a trial's denominator is exactly its marked closes, and our own exits shape r: a
   trial copied at a $15 clip with our rails is not the wallet's own return.
   - The small-clip bias is stated, not corrected: fees are a larger share, impact is smaller.
   - Refusals (chase cap, volume gate, `trial_concurrency`) remove trades from the sample; the verdict
     reports their counts beside the closes.
8. **Rollback:**
   - `MEMECOIN_REAL_TRIAL_AUTO=off` stops all automatic actions;
   - ending every trial with `--disarm` returns the roster to its pre-trial state;
   - code rollback leaves trial contracts readable, because the marker sits in `source`, the block
     prior releases already tolerate (the `copy_arm` precedent). The build must test that
     `frozen_policy_from_position` and `_verified_contract` accept a contract with `source.trial`.

## 8. Candidates (from the shortlist, Gamma 0a4fe062b; *operator* picks)

| handle | chain | shortlist read |
|---|---|---|
| redemptionjizzy | solana | n 20, mean +9.7%, CI [+0.7, +18.9], 7-day mean -4.3% (n 8) |
| DexGemsReal | solana | n 17, +7.6%, CI [+0.6, +14.2], 7-day -6.5% (n 4) |
| JarlBalgruuf__ | solana | n 17, +7.0%, CI [+1.2, +16.9], 7-day -8.0% (n 2) |
| onmycheck | 4663 | n 5, +14.9%, CI [-9.6, +35.0]; fails A3 (not positive without the best coin) |
| probablyinsider | 4663 | n 7, +5.2%, CI [-11.4, +20.8]; fails A3 |

All five are J7-unarmed today. At start, the delivery check (5.1 step 2) decides whether each one can
trade at all.

## 9. Build plan (after the operator's yes)

1. TDD on the production rail:
   - `trial_verdict` (pure), against every branch of the rule and the operating-characteristic
     examples;
   - the `reserve_entry` caps under two threads;
   - contract round-trip with the marker;
   - the runner wiring (size cap plus marker on the same entry).
   - the `positions` counter increments once per booked position, also on a retried or recovered
     booking: a test re-books and recovers the same position and the count stays 1 (Gamma, CLEAN note).
2. Switch: no trial rows means no behaviour change, so the build lands inert. Its first effect is the
   operator's first `start --confirm`.
3. Guards: the source-name guard tests and the covering set for `memecoin_risk`, `memecoin_funded_policy`
   and the runner's entry path.
4. Measured `reserve_entry` latency, p50 and p90, with and without a trial, on a generated DB (§7.2),
   never a ledger copy.
5. Gamma reviews; Alpha merges and deploys.

## 10. Operator decisions needed

If the operator keeps the existing auto-demote on trials (decision 5 = no), Gamma re-simulates the
operating-characteristics table with the 4-consecutive-loss cut before the first trial starts, and that
table applies instead of the one in 3a0b141ee.


1. Clips: solana $15, 4663 $10 (proposed).
2. `MEMECOIN_REAL_TRIAL_MAX_CONCURRENT`: 2 (proposed).
3. `MEMECOIN_REAL_TRIAL_AUTO`: `demote` (proposed), or `both` per the original order, or `off`.
4. Which candidates start, and on which chain.
5. Exempt open trials from the existing auto-demote cohort (§5.5): proposed yes.

## 11. Build (Beta, branch beta/trial-arm-build, 2026-10-01)

**Operator decisions (relayed by Alpha, 2026-10-01 ~15:5xZ):** clips $20 on solana and 4663 (decision
1). Decisions 2-5 were not answered, so the proposed defaults are built: concurrency 2,
`MEMECOIN_REAL_TRIAL_AUTO=demote`, the five candidates of section 8, and trials exempt from the
existing auto-demote. The operator can override any of them.

**Every knob is default off in code.** Activation is `.env` plus a runner restart, then the operator's
`start --confirm`:
- `MEMECOIN_REAL_TRIAL_CLIP_USD_SOLANA=20` and `MEMECOIN_REAL_TRIAL_CLIP_USD_4663=20`. Unset means no
  trial on that chain. Only the start script reads them, at call time.
- `MEMECOIN_REAL_TRIAL_AUTO=demote`. The code default is `off`: every verdict pages and stops the
  trial's entries.
- `MEMECOIN_REAL_TRIAL_MAX_CONCURRENT`, code default 2.

**Changes from sections 5-6, all in the build's tests:**
- **The counter.** `positions` is maintained from a link table, `memecoin_trial_positions(position_id
  PRIMARY KEY, trial_id)`, written by `open_position` in the booking's own transaction. A retried or
  recovered booking of the same position cannot count twice.
- **Write-once verdicts.** `memecoin_trial_verdicts` gains `checkpoint` ('5', '10', '20', 'day14')
  with a unique index on (trial_id, checkpoint).
- **The guard runs first.** It sits inside `reserve_entry` / `reserve_entry_evm`'s BEGIN IMMEDIATE,
  before the risk context. The clip comes only from a marker rebuilt from the trial row. A manual or
  override entry is never a trial entry. The guard is keyed on the caller's `enforce_concurrency`,
  because the risk modules are policy-blind (`test_memecoin_manual_gate_bypass`).
  - Refusals: `trial_unmarked`, `trial_stopped`, `trial_ended`, `trial_no_contract`,
    `trial_concurrency`, `trial_full`.
- **Paths that cannot carry the marker.** Any reservation path without the trial clip and marker is
  refused for a trial wallet (`trial_unmarked`), with nothing held.
- **The slot lane carries a trial itself with SOL or a matching async USDC leg** (Alpha on
  92646580c; cash-leg amendment 2026-10-03, operator's calico/f99189cb incident). A trial
  graded on the on-chain fallback (p50 723 ms decide to send, against 6 ms on the lane) would be
  graded under a handicap a promoted wallet does not have.
  - SOL funding needs no prefetched USDC leg, so the trial clip fits.
  - The SOL buffer is judged on the trial clip, not the full clip.
  - Without a SOL path, a configured live leg worker can quote the trial clip off the lane thread.
    Re-entry requires a matching leg and carries the original trial marker; reservation validates
    it again, including stop/end while queued. The existing signal deadline and single fallback hold.
  - Without a matching leg or available worker, the lane hands off with nothing held.
    `slot_lane_outcome` reads that refusal as `failed`, so the on-chain lane gives the signature one
    ordinary attempt at the trial clip.
  - Funding handoffs remain visible on the dashboard and do not page as final refusals. The
    resulting fill, refusal or unresolved submission has its own notification.
  - The lane pays one open-trial read (about 3 µs) per entry.
  - The share of trial entries that go through the slow fallback is a reportable bias: Gamma should
    state it beside each verdict.
- **The auto-demote.** `wallet_real_record` leaves trial-marked closes out always. During a trial the
  rule is the only demotion. After it, the cohort is the non-trial closes.
- **The fee share** on the page is the chain's measured p90 fixed fee over the clip (section 4).
  Per-close fees are not read.
- **Order on a terminal verdict.** The action is applied, then the verdict is written. A crash between
  the two leaves the trial stopped or ended without its verdict row. The next pass does not grade a
  stopped or ended trial, so that page is lost and the action stands.

**Latency.** Generated ledger (production schema, 70,000 positions, 4,500 pending, 51 trials), never a
copy:

| step | p50 | p90 |
|---|---|---|
| hook lookup, no trial | 3.2 µs | 3.3 µs |
| hook lookup, open trial | 6.7 µs | 6.8 µs |
| guard in the reservation, no trial | 4.3 µs | 4.5 µs |
| guard in the reservation, trial (all checks) | 19.7 µs | 20.1 µs |
| `reserve_entry`, no trial | 603 µs | 641 µs |
| `reserve_entry`, trial | 629 µs | 679 µs |

- An entry with no trial pays about 7.5 µs.
- An entry with a trial pays about 27 µs, which is under 5% of `reserve_entry`.

**Actions, as built (Gamma MEDIUM on 92646580c; Alpha's call (a), 2026-10-01).** This replaces the
5.4 table:
- Only DEMOTE disarms on its own, under `demote` or `both`.
- PROMOTE ends the trial with the chain kept armed only under `both`.
- INSUFFICIENT and SHADOW are reports in rule 3a0b141ee. Under every setting they stop the trial and
  page, and never disarm: the operator's roster rule disarms on funded results only.
- A stopped trial keeps refusing the wallet's entries on that chain until the operator ends it with
  `--keep-armed` or `--disarm`.

**Not handled:**
- A position booked by a path that bypasses `open_position` is not counted. No such path exists
  today.
- The SHADOW check uses wallets armed NOW, not at the time of the buy (the shortlist does the same).
