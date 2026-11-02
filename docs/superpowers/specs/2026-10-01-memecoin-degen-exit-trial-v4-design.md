# Degen exit trial: policy V4 on DegenCapitalLLC's funded Solana entries (spec)

Status: BUILT on lambda/degen-exit-trial, 2026-10-01 (Lambda). Gamma: consistent with her
pre-registration. Operator YES via Alpha. The read is Gamma's pre-registration
`docs/audits/2026-10-01-degen-exit-trial-preregistration.md` (d6c2bd9d1). Default off; the operator
turns it on.

2026-10-18 operator amendment: full-only mirror configuration (`MEMECOIN_REAL_MIRROR_SIZE_MIN_FRACTION=1.0`)
supersedes V4's first-sell rule below. A partial, derisk or unknown-size sell cannot trigger the
funded mirror; full liquidation needs on-chain owner balance proof. V4's other exits remain unchanged.

2026-10-22 reliability amendment (position 74107): an empty owner-index response alone is not
full-liquidation proof. A zero owner total must also agree with a fresh direct read of the known
source token account; missing identity, a stale bank, malformed data or a positive remainder keeps
the result unknown or held. Confirmed partial-sale evidence vetoes weaker native proof for that
same signature in either delivery order, including before a queued unsigned retry. Retry jobs keep
the original signature, owner and mint identity; independent source-failure exits remain separate.
Either a known pre/sold partial ratio or a known positive remainder can veto liquidation when the
other balance field is missing; neither missing field proves a full exit.
A later distinct full-sale signature remains eligible.
The extra source-account read runs on the existing resolver thread, never on the entry path.

Custody token-balance failures remain distinct from a validated zero. A funded exit with no usable
account/decimals defers without marking the bag stuck or signing. Validated-zero alerts report
`EXIT-DEFERRED`; unreadable attempts retain logs and existing infrastructure episode alerts rather
than adding per-attempt pages. `STUCK-BAG` must describe the actual execution failure. A signed `CLOSE-UNKNOWN`
retains its close claim until same-signature receipt recovery settles it. This amendment changes
evidence handling and diagnostics, not mirror thresholds, sizing or exit rails.

## 1. What V4 is, and what exists already

V4 is four exits on the whole bag, in this order per tick:

1. TP +100% on a whole-bag exact quote;
2. a floor at -60%;
3. a mirror on Degen's first sell of the mint after our fill;
4. a cap 800 s after our fill.

Other rails may also close a position; each such close counts and is named by its `exit_reason`.

| V4 need | what already exists | what is missing |
|---|---|---|
| TP +100 | `tp_pct` in the frozen contract, rail `real_tp`, confirmed by `executable_exit_allowed` on an exact whole-bag preview | nothing |
| floor -60 | `catastrophe_pct`, rail `catastrophe` (debounced like the stop, bad-tick guard) | nothing |
| cap 800 s | `max_hold_s`, rail `b_timecap` | nothing; the timecap lift (14,400 s) maps only listed policy ids, and a new id is not in that map |
| mirror on the first sell | `mirror_on_sell=True`; on Solana both mirror paths are size-aware: the J7 path (`_size_aware_mirror`) and the slot-lane `memecoin_source_sell_mirror`. Each holds until the cumulative share sold is ≥ 0.30 | a first-sell rule: any sale by the source, any size, is a full exit |
| one wallet only | `memecoin_wallet_exit_overrides` (TP/stop only), the `.env` wallet lists (`follow-30-50`, `tp-only`) | a policy that names one uid, a chain and a cohort start |
| exact quotes to grade V0 (Gamma constraint 2) | `_keep_route_quote(..., "exit_decision")` on every sweep preview | **none for V4**: `exact_price_relevant` is False for a policy that mirrors, so the sweep never takes an exact quote of a V4 position (only a confirmation when a rail fires) |

## 2. The change

**Settings** (`sniper/settings/memecoin.py`):
- `MEMECOIN_EXIT_TRIAL_V4_WALLETS`: source_user_ids, default empty (off).
- `MEMECOIN_EXIT_TRIAL_V4_FROM`: the activation time (ISO, with offset). Empty or unparseable means
  off, with a warning.
- V4's four numbers are module constants, not env knobs. The pre-registration forbids retuning them on
  trial data; a retune is a new stamp.

**Policy** (`memecoin_funded_policy.py`): `exit_trial_v4_policy(coin_band)` builds `exit-trial-v4-v1`
through `_policy`: tp 100, stop 0, trail 0, de-risk 0, catastrophe 60, max_hold 800, mirror on. The
unquotable timeout and stop debounce stay on cfg. Every reservable band is accepted, as for
follow-30-50: no value in V4 depends on the band.

**Applies at reservation, nowhere else** (`memecoin_risk.prepare_funded_contract`, the one seam every
funded Solana entry crosses: main runner, follow thread, slot lane and dip retry). V4 applies when all
three hold:
- the chain is Solana;
- the source is in `MEMECOIN_EXIT_TRIAL_V4_WALLETS`;
- `classification.decision_at` is at or after `MEMECOIN_EXIT_TRIAL_V4_FROM`.

When it applies, V4 replaces whatever `funded_policy_for_origin` returned: band policy, follow-30-50,
copy arm, and the UI TP/stop override. The contract then records no copy arm and no UI override,
because neither was applied. Manual entries (`funded_policy` passed) never get V4.

**Recorded in the contract** (`build_funded_contract(exit_trial=...)`):
- `policy` carries V4's rails under `policy_id='exit-trial-v4-v1'`;
- `source.exit_trial = {"name": "degen-v4", "source_user_id", "cohort_from", "replaced_policy_id",
  "replaced_copy_arm", "replaced_override_id"}`.

It sits in `source` because that block tolerates unknown keys: a pinned prior release still parses the
contract. The new policy id parses on a prior release; a new policy key would not. Gamma pairs on
`policy_id` and `source.exit_trial.cohort_from`.

**The sweep** (`memecoin_real_runner.py`):
- `exact_price_relevant` returns True for the `exit-trial-v4` family. Every sweep then takes an exact
  whole-bag preview of a V4 position, exactly as it does today for every mirror-off Solana policy
  (follow-30-50): TP and floor are nominated on that exact price, and the sweep writes the
  `exit_decision` route quote that grades V0. That answers Gamma constraint 2.
- **First-sell mirror.**
  - J7 path: `real_mirror_on_sell` sends a V4 position to `_size_aware_mirror(..., first_sell=True)`
    on any chain. There a sale of any size, known or unknown, is a full exit (audit code
    `first_sell`).
  - The operator's per-position hold (`mirror_disabled`) and paused tracking still win, unchanged.
- **Slot-lane mirror** (`memecoin_source_sell_mirror`): for a V4 position, `mirror = share > 0`
  instead of `cumulative >= min_fraction`; the audit code is `first_sell`.
- Both paths look the position up by the SELL's source_user_id. Only Degen's own sell can close
  Degen's position (Gamma constraint 4).
- **Volume death: off for V4 (D1, decided NO by Alpha).** The build excludes V4 positions from the
  armed volume-death rail with one condition beside the existing manual exemption.

## 3. Decision D1 (decided: NO, Alpha 2026-10-01; Gamma agrees)

**D1. Should the armed volume-death rail (30 s silence, frozen `voldeath_exit_s`) run on V4 positions?**

My recommendation is no:
- It is a staleness exit, the job the old timecap did, not a safety rail.
- At 30 s of silence it would close most V4 trades long before the 800 s cap, so the trial would
  measure volume death, not V4.
- A dead route is still covered by the unquotable rail and the catastrophe floor.

If Alpha says it should run, I delete one condition; it then counts as a named exit
(`exit_reason=vol_death`).

## 4. What stays as it is (Alpha's list)

- **Positions opened before activation:** their contracts are frozen, so they never change.
- **Other wallets:** byte-identical contracts, with the knob on or off. A test compares the canonical
  JSON.
- **The catastrophe rail:** still runs, at V4's 60.
- **Unsellable:** the unquotable timeout and the stuck-bag paths are untouched.
- **Write-offs:** the write-off script is untouched.
- **Dip-retry entries:** get V4, because they reserve through the same `prepare_funded_contract`.
- **Fill latency:** no read, lock or call is added to the entry path. Measured
  (`prepare_funded_contract`, 400 calls each, two rounds):
  - `exit_trial_v4_applies` costs 0.30 / 0.34 µs (p50 / p90) for another wallet, and 2.25 /
    2.35 µs for Degen;
  - the contract build for another wallet is unchanged within noise (knob off 309-314 µs p50,
    knob on 307-317 µs);
  - a Degen V4 build costs +15 to +32 µs p50, for the second policy object.

## 5. Edge cases (CLAUDE.md rule 5)

1. **Money path: yes.** It changes the exit rails of one wallet's future funded Solana positions:
   - TP moves from 25 to 100;
   - the stop of 35 is removed;
   - the floor moves from 80 to 60;
   - the cap moves from 14,400 s to 800 s;
   - the mirror becomes first-sell;
   - volume death is off (D1).

   A V4 position with no Degen sell and no price move exits at 800 s.
2. **Speed on the fill path.** The entry path gains an O(1) check and nothing else.
   - The sweep gains one exact preview per sweep per open V4 position. That is the same load as each
     open follow-30-50 position today. Degen opened 17 funded Solana positions in 14 days.
   - The mirror decision adds a policy parse per decided sell (microseconds, no I/O).
3. **Concurrency.** No module state changes. The knobs are read at import; the policy is frozen per
   position at reservation. The mirror thread and the sweep each read the position's own contract.
4. **Absent versus zero versus unknown.**
   - A sell of unknown size is a full exit under V4, on purpose: it is a sell.
   - An unparseable cohort time switches the trial off, never on.
   - A contract that fails to parse is the existing `CONTRACT_UNPARSEABLE` path, unchanged.
   - `stop_pct=0` is not "no downside": the conjunction invariant sees catastrophe 60 > 0.
5. **Dependencies.**
   - The account path counts ANY drop, a transfer out included (operator decision 2026-09-27). Under
     V4 a dust transfer out of Degen's coin account is a full exit.
   - The audit row's `detail.path` says which path decided:
     - `account_processed` is a balance drop, which may be a sale or a transfer (that path cannot
       tell them apart);
     - `slot_processed` and `watcher_confirmed` are decoded sales;
     - the J7 path's lane is `mirror_on_sell`, a J7 sell event.
   - A J7 sell that never arrives (the account-2 gap) leaves the slot-lane path. The pre-registration
     flags such fills; they are not censored.
6. **Units and identity.**
   - Percentages are pnl percent on entry, as in every rail.
   - `max_hold_s` counts from `opened_at`, as today's timecap does. The pre-registration says "after
     our fill"; `opened_at` is decision start, about 2 s earlier, which is conservative by that much.
   - The cohort time compares as aware datetimes, never as strings.
7. **The sample.** Every funded Solana entry of one uid from `cohort_from`. The read is Gamma's, at 20
   graded pairs.
8. **Rollback.**
   - Empty the wallet knob: new entries go back to today's policy, and open V4 positions keep V4,
     because their contracts are frozen.
   - On a pinned prior release, a V4 contract still parses. That release reverts to the 30% mirror
     size rule and quotes the position only on a rail nomination. That is a degraded V4, not an
     outage. The volume-death exemption is also lost there, so the rail would run.

## 6. Checks to leave behind

All on the production paths (`real_exit_sweep`, `real_mirror_on_sell`, `SourceSellMirror.decide`,
`prepare_funded_contract`), with contexts built the production way:

- V4 contract: TP at +100 exits and +99 does not; the floor at -60 exits; the cap exits at 801 s and
  not at 799 s.
- Volume death does not fire on V4 (D1).
- An exact preview is taken every sweep, and an `exit_decision` route quote is recorded.
- A Degen sell of 5% closes a V4 position (both mirror paths). The same sell holds a non-trial
  position.
- A different wallet's sell never closes a V4 position.
- Another wallet's contract is byte-identical with the knob on and off. Degen's contract is
  byte-identical with the knob off.
- Degen's contract is unchanged before the cohort time and is V4 after it.
- An unparseable cohort time means off.
- The stored contract carries `exit_trial` and the V4 rails, and a dip-retry entry gets the same
  contract.
- The operator's `mirror_disabled` still holds a V4 position.
