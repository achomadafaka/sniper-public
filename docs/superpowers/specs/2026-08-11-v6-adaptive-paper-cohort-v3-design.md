# V6 Adaptive Paper Cohort v3 — Design

**Date:** 2026-08-11
**Status:** proposed
**Scope:** Opportunity Radar paper measurement. Paper only. No funded authority.
**Supersedes:** nothing. `radar-realistic-v2` keeps its rows and its meaning.

---

## 1. Purpose

The Radar measures one exit policy today. That policy is a flat target, a flat stop, and a
flat time cap. This design adds a second, adaptive policy on a fresh clock, and a tighter
entry rule to feed it. The two policies run at the same time on separate cohorts.

The design keeps four result populations apart: Pump, Pons, Bags, and copy-source.

---

## 2. Evidence

### 2.1 What reproduces exactly

The Pump v6 arm has 54 valid results. Query, read-only, on `data/memecoin_opportunity.db`:

```sql
-- schema:external=op_paper_outcomes
-- schema:external=op_paper_fill_requests
SELECT o.result_code, COUNT(*), ROUND(AVG(o.return_bps))
FROM op_paper_outcomes o JOIN op_paper_fill_requests r USING(request_id)
WHERE r.rule_version='pump-confirmation-truth-v6' AND o.status='valid'
GROUP BY 1;
```

| result_code | n | mean return |
|---|---|---|
| `target_first` | 14 | +3,365 bps |
| `stop_first` | 28 | −2,724 bps |
| `time_exit` | 12 | −691 bps |

The exit split is the strong part of the evidence. The stop gives back 81% of what the
target earns, and it fires twice as often.

### 2.2 What does not reproduce

Three claims in the approved contract do not survive re-measurement. The design keeps
them as a prior, not as a result.

1. **Entry age bands.** The contract states −2,487 bps for 10–15 s and +1,106 bps for
   15–23 s. The retained data gives **−1,932 bps (n=12)** and **+241 bps (n=16)**, from
   `op_paper_fill_requests.decision_age_ms`. The direction agrees. The size does not.
2. **Significance.** The difference between the two bands is +2,173 bps at a one-sided
   permutation p of **0.081** on 20,000 shuffles. That is not a significant separation.
3. **Acceleration quartile.** The claim is not testable now. Retention keeps 13 rows in
   `op_forward_alert_features` against 55 v6 requests, so the per-alert
   `capital_acceleration_atomic_per_s2` value is gone for most of the cohort.

### 2.3 The cohort mean

| population | n | mean | median |
|---|---|---|---|
| `pump-confirmation-truth-v6` | 54 | −694 bps | −919 bps |
| `route-control-v2` (matched control) | 100 | −626 bps | −691 bps |

The v6 mean has a bootstrap 95% interval of **[−1,581, +251] bps**. The arm is not
separable from zero, and it is not separable from its own control. **v6 has no measured
edge.** This design is a search, not a deployment of a known edge.

### 2.4 Pons and Bags

Pons has 1 result, and it is censored. Bags has 0 results. Neither venue has evidence.
Section 7 gives the repair that is required before either can produce one.

---

## 3. Decision

Build option 2: a new policy `v6-position-engine-v3` inside the existing engine, with
separate cohorts.

- Option 1 (change v2 in place) breaks the byte-stable `policy_hash` and makes 43,333
  stored decisions unreadable against their own policy. Rejected.
- Option 3 (a second engine) duplicates the rule resolver, the state validator, and the
  fill/unfill fork. Rejected.

---

## 4. Identity and cohort boundaries

### 4.1 New identities

| kind | value | note |
|---|---|---|
| engine policy | `v6-position-engine-v3` | new `V6Policy` in the shared engine |
| lifecycle | `radar-adaptive-v3` | new `op_paper_lifecycle_activations` row |
| Pump arm | `pump-adaptive-v3` | new entry rule |
| Bags arm | `bags-adaptive-v3` | new entry rule |
| Pons arm | `pons-adaptive-v3` | new entry rule |
| matched control | `pump-confirmation-truth-v6` | the current rule, unchanged |

### 4.2 Frozen identities

`v6-position-engine-v2` keeps policy hash
`a11a0d279d9cd1c29974faac5ae6df7799af39b6b28acbbbfa182080f3cb9902`. A test pins this
exact string. If the refactor changes it, the refactor is wrong.

`radar-realistic-v2` keeps its activation row, its 217 outcomes, and its exit rules.

### 4.3 The comparison

`op_paper_outcomes` is unique on `(request_id, lifecycle_version)`. One filled request can
therefore hold one outcome per lifecycle. The design uses this to separate two effects:

1. **Exit effect.** The baseline arm `pump-confirmation-truth-v6` runs under BOTH
   lifecycles. Same entries, two exit policies. The difference is the exit change alone.
2. **Entry effect.** The new arm `pump-adaptive-v3` runs under `radar-adaptive-v3` only.
   Against the baseline arm on the same lifecycle, the difference is the entry change
   alone.

No result pools the two lifecycles, and no result pools two venues.

### 4.4 Origin separation

Every `radar-adaptive-v3` result row stores `origin_kind`. The permitted values are
`radar_pump`, `radar_bags`, `radar_pons`. The copy-source population lives in
`data/memecoin_v6_paper.sqlite` under `signal_source='global_feed'` and never enters an
`op_*` table. A report that pools two `origin_kind` values is a defect.

---

## 5. Entry contract

### 5.1 What already exists

`evaluate_venue_confirmation` in `sniper/research/memecoin_opportunity/directional.py`
already implements most of the approved entry design. It is applied to Bags and Pons only.
It is NOT applied to Pump. Pump uses `evaluate_confirmation_v6`, which has no age window,
no sell-pressure limit, no concentration check, and no creator-sale check.

The v3 entry rule is that predicate, applied to all three venues, with five changes.

### 5.2 The v3 predicate

The rule needs a prior state observation and a current state observation. Both come from
`op_state_snapshots`. The rule accepts a launch when every condition below is true.

| # | condition | source | status |
|---|---|---|---|
| 1 | two distinct state observations | `prior is not None` | exists |
| 2 | `15_000 < launch_age_ms <= 35_000` | interval feature | **changed** from `10_000 < x <= 60_000` |
| 3 | `effective_buyer_count > prior` | interval feature | exists |
| 4 | `capital_velocity_atomic_per_s > prior` | interval feature | exists |
| 5 | `effective_buyer_count >= 10` | interval feature | exists |
| 6 | `new_eligible_buyers >= 2` | interval feature | exists |
| 7 | `effective_buyer_hhi_bps <= prior` | interval feature | exists |
| 8 | `creator_sale_since_prior` is false | interval feature | exists |
| 9 | `sell_pressure_bps <= 2_500` | interval feature | exists |
| 10 | `liquidity_removed_fraction_bps < 500` | **new** interval feature | **new** |
| 11 | `capital_acceleration_atomic_per_s2 <= frenzy ceiling` | interval feature | **new gate** |
| 12 | `local_exit_exists` and `remote_entry_quote_ok` and `remote_sell_simulation_ok` | interval feature | exists |
| 13 | `observed_pair_return_bps >= -500` | interval feature | exists |
| 14 | `quote_age_ms < 2_000` | decision-site value | **new gate** |

### 5.3 The new inputs

**`liquidity_removed_fraction_bps`.** The interval builder computes it from the reserve
field of the venue. Solana uses `real_quote_reserves`. EVM uses `v3_liquidity`. The value
is `max(0, (prior - current) * 10_000 // prior)`. If the prior reserve is absent or is
zero, the feature is `None`, and condition 10 fails closed.

**The frenzy ceiling.** The ceiling is a function of venue and launch age, not one
constant. It is the p75 of `capital_acceleration_atomic_per_s2` over the same venue, over
the last 200 eligible snapshots in the same 5-second age bucket. If fewer than 40 samples
exist for the bucket, the feature is `None`, and condition 11 fails closed.

> **Caution:** the ceiling is a live percentile over recent launches. It moves. Every
> result row must store the ceiling value that the decision used, or the decision is not
> reconstructable.

### 5.4 Fill

A v3 acceptance produces one `op_paper_fill_requests` row and one `op_paper_fills`
receipt, through the existing adapter. The adapter version stays
`radar-paper-adapter-v1`. The fill model stays `route-floor-v1`. Nothing about the entry
fill changes.

---

## 6. Exit contract

### 6.1 Shape of the change

The engine module holds its parameters as module constants today. The change turns them
into a frozen `V6Policy` object. `step` takes a policy argument and defaults to the v2
policy. `policy_document()` becomes `policy.document()`. The v2 document must serialize to
the identical bytes it does now.

### 6.2 The v3 rules

Rules run in the order below. `resolve_rule_results` already prefers `EXIT_FULL`, then
priority, then fraction. The table gives priority values that respect that order.

| rule | trigger | action | fraction | priority |
|---|---|---|---|---|
| `lp_loss` | removed fraction at or above 30% | EXIT_FULL | 100% | 100 |
| `creator_sale` | creator sale observed after entry | EXIT_FULL | 100% | 95 |
| `exact_stop` | executable return at or below −1,000 bps, on two distinct healthy quotes | EXIT_FULL | 100% | 90 |
| `route_loss` | route absent for 4 checks and 60 s | EXIT_FULL | 100% | 80 |
| `time_cap` | held 120 s, and demand is not strong | EXIT_FULL | 100% | 70 |
| `time_cap` | held 300 s | EXIT_FULL | 100% | 70 |
| `trail` | executable price at or below 92% of the executable high-water, after the take-profit tranche filled | EXIT_FULL | 100% | 40 |
| `take_profit` | executable return at or above +1,200 bps | DERISK | 50% | 30 |
| `staged_break` | second confirmed negative-flow break | EXIT_FULL | 100% | 20 |
| `staged_break` | first confirmed negative-flow break | DERISK | 50% | 10 |

### 6.3 Exact definitions

**Executable price only.** Every price in section 6.2 is a route quote for the exact open
quantity. No local curve price and no mark price can trigger any rule.

**Two distinct healthy quotes.** The stop needs two observations with different
`evidence.quote` identities, both with `quote_status='healthy'`, both at or below
−1,000 bps. One quote cannot confirm itself. The engine already holds
`catastrophe_confirmations` and `catastrophe_candidate_price` for this shape; v3 reuses
them with the v3 threshold.

**Strong demand.** The 120-second cap does not fire when all three are true on the last
structural observation:

1. `is_break` is false;
2. `net > 0`;
3. the executable return is at or above 0 bps.

The 300-second cap has no exception.

**Tranche budget.** `MAX_TRANCHES` is 2 for v3. The take-profit tranche consumes tranche
1. A later break therefore exits the remainder in full. This is deliberate: a position
that already took profit does not stage a second partial exit.

**Trail arming.** The trail is inert until the take-profit tranche fills. Before that, the
stop and the break rules own the downside. This prevents an 8% trail from closing a
position that never went up.

### 6.4 Insufficient data

Missing required evidence produces `HOLD` with sorted `data_insufficient:<field>` reason
codes. This is the existing behaviour and it does not change. A provider failure sets
`quote_status` to `provider_unavailable`. That is not a token failure, it never advances
the stop confirmations, and it never triggers `route_loss`.

### 6.5 Post-exit follow-up

After a terminal exit the lifecycle schedules follow-up quotes at 30, 120, 300, and 900
seconds. These rows are measurement only. No follow-up value can enter any v3 decision,
any v3 state, or any v3 result return. `EXIT_FOLLOWUP_HORIZONS_S` in
`sniper/persistence/db_memecoin_v6_paper.py` already holds this horizon set; the Radar
uses the same four values.

---

## 7. Pons quote binding repair

### 7.1 The defect

`OpportunityRadarEngine.observe_remote_completion` in
`sniper/research/memecoin_opportunity/engine.py:691` discards a completed EVM quote:

```python
or (
    result.provider in {"bags_curve_eth_call_v2", "pons_v3_quoter_v1"}
    and (result.quote_mint != state.quote_mint
         or result.state_fingerprint != point.state_fingerprint)
)
```

`point` is `self.last_observations[launch_id]`, which is the LATEST observation. The quote
was requested against an earlier snapshot. `OpportunityEvmProbe` returns
`EVM_QUOTE_PENDING` and completes the call on a background thread. Any normal trade that
arrives in the meantime advances `point`. The completed quote then fails the fingerprint
comparison and returns `None`.

Line 724 does the same thing with time: `if _parse(completed) < _parse(point.observed_at):
return None`.

This explains the counts. Pons has 2 executable quotes and 10 `EVM_QUOTE_PENDING` rows,
and no valid result.

### 7.2 The repair

1. The probe stores the requesting snapshot identity and the requesting block identity
   with the task.
2. The completion carries both back.
3. `observe_remote_completion` compares the completion against the snapshot it was
   requested for, not against the latest snapshot.
4. A later normal trade does not reject the completion.

### 7.3 What still rejects

The completion is still rejected when any of the following is true between the requested
snapshot and the completion:

- a creator sale occurred;
- the route changed;
- liquidity fell by the material fraction or more;
- the quote age exceeds `remote_completion_max_age_ms`.

Fail closed on all four. A stale quote is not evidence.

---

## 8. Persistence

### 8.1 Schema

`SCHEMA_VERSION` in `sniper/research/memecoin_opportunity/store.py` moves from 13 to 14.
`_migrate_schema` accepts version 13 and replays the new statements.

<!-- schema:external=op_paper_position_actions -->
<!-- schema:external=op_paper_position_states -->
<!-- schema:external=op_paper_outcomes -->

**New table `op_paper_position_actions`** — one append-only row per filled engine action.

```sql
CREATE TABLE IF NOT EXISTS op_paper_position_actions (
    action_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    lifecycle_version TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('DERISK','EXIT_FULL')),
    fraction_ppm INTEGER NOT NULL,
    quantity_atomic TEXT NOT NULL,
    expected_out_atomic TEXT NOT NULL,
    scored_out_atomic TEXT NOT NULL,
    reason_codes_json TEXT NOT NULL,
    policy_hash TEXT NOT NULL,
    quote_evidence_id TEXT,
    market_evidence_id TEXT,
    observed_at TEXT NOT NULL,
    row_fingerprint TEXT NOT NULL,
    inserted_at TEXT NOT NULL,
    UNIQUE(request_id, lifecycle_version, sequence)
);
```

**New table `op_paper_position_states`** — one append-only row per decision.

```sql
CREATE TABLE IF NOT EXISTS op_paper_position_states (
    state_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    lifecycle_version TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    policy_hash TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    row_fingerprint TEXT NOT NULL,
    inserted_at TEXT NOT NULL,
    UNIQUE(request_id, lifecycle_version, sequence)
);
```

Both tables sit outside the retention delete set, like `op_paper_fill_requests`. A state
row that retention can delete is not a state row.

**Changed table `op_paper_outcomes`** — four added columns, all nullable so the 217
existing rows stay valid:

| column | meaning |
|---|---|
| `origin_kind` | `radar_pump`, `radar_bags`, or `radar_pons` |
| `policy_hash` | the engine policy that decided the exit |
| `decision_facts_json` | the entry gate values, with the frenzy ceiling used |
| `exit_reason_codes_json` | the reason codes of the terminal action |

`result_code` gains three values for v3: `adaptive_exit`, `trail_exit`, `creator_exit`.
The existing four values keep their meaning.

### 8.2 Return calculation

The v3 outcome return is the sum of the scored proceeds of every filled tranche, against
the entry `quote_in_atomic`.

```
return_bps = (SUM(scored_out_atomic) - quote_in_atomic) * 10_000 / quote_in_atomic
```

The measured execution haircut of 122 bps per leg applies to every tranche. This is the
`radar-realistic-v2` rule and it does not change.

---

## 9. Reports

`realistic_result_summary` gains a lifecycle argument. The API payload holds one block per
lifecycle, and inside each block one entry per venue and one per arm. The function never
returns a pooled mean across venues, and never across lifecycles.

`sniper/routes/api_memecoin_opportunity.py` serves both blocks. The UI shows them as two
labeled panels.

---

## 10. Guards

1. V6 stays paper. The v3 work adds no funded authority, no signing, no reservation, and
   no live approval.
2. The funded runner, `sniper_tracker.db`, and `.runtime/memecoin/current` are not
   touched.
3. `radar-realistic-v2` outcome rows are never updated and never deleted.
4. The v2 policy hash is pinned by test.
5. Forward results count. Retrospective results do not count, and no v3 outcome row is
   backfilled from a historical request.

---

## 11. Test contract

| # | test | asserts |
|---|---|---|
| 1 | v2 policy hash | equals `a11a0d27…9902` after the policy refactor |
| 2 | v2 decisions replay | a stored v2 decision reproduces byte-for-byte |
| 3 | v3 entry age window | 14.9 s rejects, 15.1 s accepts, 34.9 s accepts, 35.1 s rejects |
| 4 | v3 entry fails closed | absent LP feature rejects; absent ceiling rejects; quote age 2,001 ms rejects |
| 5 | take profit | +1,200 bps gives DERISK at 500,000 ppm |
| 6 | trail is inert | an 8% fall before the take-profit tranche gives HOLD |
| 7 | trail arms | after the tranche fills, 92% of high-water gives EXIT_FULL |
| 8 | stop needs two quotes | one −1,000 bps quote gives HOLD; a second distinct one gives EXIT_FULL |
| 9 | provider failure | `provider_unavailable` never advances the stop confirmations |
| 10 | time cap | 120 s with weak demand exits; 120 s with strong demand holds; 300 s always exits |
| 11 | creator sale | gives EXIT_FULL at priority 95 |
| 12 | tranche budget | a break after the take-profit tranche exits the remainder in full |
| 13 | Pons binding | a completion whose snapshot advanced is accepted |
| 14 | Pons fail-closed | a completion after a creator sale, a route change, an LP loss, or an age breach is rejected |
| 15 | outcome sum | a two-tranche exit sums both scored proceeds |
| 16 | cohort separation | the summary never pools two venues and never pools two lifecycles |
| 17 | follow-up isolation | a follow-up quote changes no state, no decision, and no return |
| 18 | migration | a version 13 database opens at version 14, and its 217 outcomes stay readable |

---

## 12. Open risks

1. **The entry thresholds rest on 54 results with no significant separation.** The 15–35 s
   window is a prior. It can be wrong. The matched control is the only thing that can
   settle it, and it needs about 30 to 50 paired results per venue before it is readable.
2. **The frenzy ceiling is endogenous.** It is a percentile over recent launches on the
   same venue. A regime change moves the gate and the arm at the same time. The stored
   per-decision ceiling is what makes this auditable later.
3. **Pons and Bags have no evidence at all.** The section 7 repair is necessary. It is not
   sufficient. Neither venue can be read until it produces its own results.
4. **The exit leg haircut is still unmeasured.** Every chain exit row in `memecoin_fills`
   carries a null `execution_slippage_pct`. v3 applies the entry-leg distribution to both
   legs, exactly as `radar-realistic-v2` does, because no other measurement exists.
5. **A tighter entry cuts the sample rate.** v6 fires about 11 times a day. Conditions 2,
   10, 11, and 14 will cut that. If the v3 arm falls below about 3 fires a day, the
   readable date moves out past a month, and that is a reason to widen the window before
   the clock starts.

---

## 13. Knowledge sync

| question | answer |
|---|---|
| New doctrine? | Yes. Radar exit policy is now per-lifecycle, and a lifecycle is a cohort boundary. |
| Superseded doc? | None. `2026-08-06-radar-owned-realistic-paper-lifecycle-design.md` stays correct for v2. |
| Brain entry? | Yes, after the first 30 paired results, not before. |
| AGENTS.md? | Add one line: `radar-adaptive-v3` is paper only and never pools with `radar-realistic-v2`. |
