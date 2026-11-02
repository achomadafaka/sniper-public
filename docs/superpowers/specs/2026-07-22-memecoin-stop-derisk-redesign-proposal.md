# Proposal: post-cutover funded stop/de-risk re-design (operator decision required)

Exploratory look at the cohort defined by
`docs/superpowers/specs/2026-07-21-memecoin-postcutover-exit-read.md`.
**Proposal only — no knob was changed. No rail on the funded runner has been touched.**

Revised 2026-07-22 after review. Four claims in the first draft were wrong; they are
corrected below and noted in "Corrections".

## Status of this read — the registered branch did NOT fire

The pre-registration gates at **n ≥ 30** (spec line 19). This was read at **n = 29** under
operator authorization. The re-design *condition* matched, but the registered branch did
not fire, and nothing here is decision-grade under the original pre-registration.
`scripts/memecoin_postcutover_exit_read.py` was not modified or bypassed; the identical
computation ran out-of-tree.

Consequence for the escalation path: the spec's "accrue to n=50, re-read once" clause was
conditional on an *ambiguous result at the n=30 gate*. Since the gate never fired, an n=50
look is **a newly frozen confirmation read that must be pre-registered on its own terms**,
not a continuation of the original spec.

## The read (real, closed, `funded_contract_json IS NOT NULL`)

```
n=29  total=-166.65  win_rate=44.8%  avg_win=+16.93  avg_loss=-24.17

by exit_reason:
  b_stop        n=10  total=-313.84  avg=-31.38
  mirror        n= 2  total= -11.78  avg= -5.89
  real_tp       n= 2  total= +39.09  avg=+19.55
  real_trail    n=10  total= +57.77  avg= +5.78
  manual        n= 5  total= +62.11  avg=+12.42

sensitivity: drop-top-1=-206.78   drop-top-2=-237.46
stop-share of gross losses: 81.1%  (stop-class -313.84 of -386.78)
```

Removing the two best fills makes the result worse, not better.

## P0 — the 4663 stop question is an unresolved policy conflict, not a bug

> Later band-specific −60% stops conflict with the earlier chain-default no-stop rationale
> in observed outcomes. **Reconfirm operator intent before changing policy.**

An earlier draft of this section called the divergence a consistency bug and proposed
routing every band through `real_stop_pct_for_chain`. That was wrong. The divergence is a
deliberate sequence of later decisions, and reversing it is a new operator decision.

Chronology, verified in source, history, and tests:

| date | change | evidence |
|---|---|---|
| 2026-07-12 | chain-default *normal* stop disabled for 4663 — *"a fixed stop sells normal volatility"* | `sniper/settings/memecoin.py:382` |
| 2026-07-16 | micro adds a **position-scoped** −60% stop, docstring: *"covers 4663 micros **without touching the chain-wide no-stop decision**"* | `assign_micro_policy`, `memecoin_real_runner.py` |
| 2026-07-16 | nano/micro band split and the 1–2M (mid) band land band-specific −60% stops | `e58157c9`, `7b222dff` |
| 2026-07-18 | funded contracts freeze that behavior | `fe355969` |
| — | tests **require** nano/micro/mid to carry `stop=60.0`, chain-independent | `tests/test_memecoin_funded_policy.py:38-42` |

The micro docstring is explicit that it is overriding the chain default on purpose. So
routing all bands through the per-chain helper would **reverse the newer band policy**, and
would require a new funded policy version and cohort with a pinned deployment — not reuse
of `funded-*-v1`.

### Two data errors in the earlier P0 evidence

1. **"Stop disabled" means the *normal* stop only.** Every contract in the cohort still
   carries the −80% catastrophe floor (`catastrophe_pct = 80.0` on all 29 rows). No funded
   position is unprotected.
2. **The small-band 4663 subgroup contains one `b_stop`, not zero.** Position 9380
   (−$31.86) stopped under an explicit **operator per-position override**
   (`memecoin_position_exit_overrides` id=81, `source=operator`, `sl_pct=-60.0`), whose note
   reads: *"cap downside — 4663 has no default stop; floor −60% (2026-07-19)."* The claim
   "every stop-class loss comes from a band ignoring the decision" is therefore **false** —
   this one came from a band that honored the chain default plus an operator who chose to
   add a floor anyway. That note is direct evidence of operator intent pointing the
   *opposite* way from the earlier draft's proposal.

Corrected subgroup rows:

| 4663 small-band | n | total | stops |
|---|---|---|---|
| no effective normal stop | 5 | +$69.24 | 0 |
| operator `sl_pct` override | 1 | −$31.86 | 1 |
| combined | 6 | +$37.39 | 1 |

### What the subgroup is worth

Suggestive, not a natural experiment. It compares different mcap bands, different trail
activation (`trail_arm=15`, `requires_derisk=false` vs `arm=0`, `requires_derisk=true`),
different entry selection, and includes one operator override. n=5 on the stopless side.

The honest statement is the blockquote at the top of this section: the two policies
disagree, the outcomes so far lean one way, and only the operator can decide which
intent governs.

## Supporting decomposition

### Chain

| chain | n | total | stops | stop PnL |
|---|---|---|---|---|
| 4663 | 19 | −$203.96 | 10 | −$313.84 |
| solana | 10 | **+$37.31** | **0** | $0.00 |

Non-stop 4663 closes total +$109.88 — 4663 entries are not uniformly bad.

### Band

| policy | n | total | avg | stops | derisk | trail | trail_arm | requires_derisk |
|---|---|---|---|---|---|---|---|---|
| funded-nano-v1 | 8 | −$135.58 | −$16.95 | 5 | 40 | 40 | 0 | yes |
| funded-micro-v1 | 8 | −$110.84 | −$13.85 | 4 | 50 | 50 | 0 | yes |
| funded-small-v1 | 11 | +$66.88 | +$6.08 | 1 | 40 | 30 | **15** | **no** |
| funded-mid-v1 | 2 | +$12.89 | +$6.45 | 0 | **30** | **35** | 0 | yes |

Both positive bands, not one. `funded-mid-v1` is positive *with* `trail_requires_derisk`,
so "de-risk-gated trail" cannot be the sole explanation for the losing bands — its de-risk
threshold is also the lowest of the gated bands (+30%). n=2; treat as directional only.

### Peak distribution of the 10 stopped positions

| exec peak | count |
|---|---|
| ≥ +30% | 2 (11984 +48.3%, 12110 +36.1%) |
| +13% … +22% | 4 (+20.2, +15.4, +13.9, +13.5) |
| ≤ +4.4% | 4 (+4.4, +0.05, and two that never traded above entry) |

A rail at **+20% reaches 3 of 10** stopped positions. Reaching 6 of 10 requires a rail at
≤ +13.5%. Four of ten had no profitable exit at any rail — that is entry quality, not exit
policy.

### Counterfactual rail sweep (all 29 closes)

Full-sell at rail X whenever executable peak ≥ X, actual outcome otherwise. Gross —
assumes a fill *at* the rail, ignores exit slippage and the 56s mark cadence. Every number
is optimistic.

| X | cohort total | vs actual | converted | capped |
|---|---|---|---|---|
| 10% | −$35.15 | +$131.50 | 12 | 11 |
| 15% | −$62.67 | +$103.98 | 10 | 11 |
| 20% | −$90.94 | +$75.71 | 7 | 10 |
| 25% | −$123.25 | +$43.40 | 5 | 8 |
| 30% | −$93.87 | +$72.78 | 5 | 8 |
| 40% | −$99.62 | +$67.03 | 6 | 5 |
| 50% | −$103.77 | +$62.88 | 6 | 3 |

- Every level beats the status quo, so the signal is "the first profit rail sits too high",
  not "X = 10 is optimal". Taking the argmax at n=29 with peak-fill assumptions is
  overfitting.
- **No level makes the cohort positive.** The optimistic ceiling still loses $35. Exits are
  worth ~$40–130 of a $167 loss; the rest is entry selection and 4663 execution.

Chain-split — a global knob damages the working chain:

| X | 4663 delta | solana delta |
|---|---|---|
| 15% | +$93.68 | +$10.30 |
| 20% | +$82.04 | **−$6.33** |
| 25% | +$37.23 | +$6.17 |
| 30% | +$54.10 | +$18.67 |
| 40% | +$37.75 | +$29.28 |

## Proposals (operator decides; none implemented)

**P0 — reconfirm operator intent on 4663 normal stops. No code change proposed.** The
07-12 chain-default no-stop rationale and the 07-16/07-18 band-specific −60% stops
disagree, and the outcomes so far favor the former. If the operator now wants no normal
stop across all 4663 bands, that is a **new funded policy version and cohort with a pinned
deployment**, not a reuse of `funded-*-v1` and not a per-chain-helper reroute. Note the
countervailing signal: on 2026-07-19 the operator manually added a −60% floor to a 4663
position *because* the chain had no default stop.

**P1 — partial de-risk instead of an all-or-nothing first rail.** For nano/micro, sell a
fraction (~40–50%) at ~+20% executable and leave the remainder under the trail. Converts
part of the +13–48% peak cluster without truncating the tail that produced +$30.69 /
+$28.95 / +$19.61 / +$18.82 from peaks of +68% to +171%. Note honestly: a +20% trigger
touches **3 of 10** stopped positions.

**P2 — if nano/micro adopt an early trail, two fields must move together.** They freeze
`trail_arm_pct = 0.0` **and** `trail_requires_derisk = true`
(`sniper/services/memecoin_funded_policy.py:82`). Clearing only the flag arms the trail
immediately from entry, giving back on the first tick. The empirically positive
configuration is the small band's `trail_requires_derisk = false` **with**
`trail_arm_pct = 15` — the working value is 15, not 20, and there is no evidence for 20
specifically.

**P3 — chain-conditioned policy or reduced 4663 exposure.** 4663 carries 100% of stop-class
losses and 19 of 29 closes. Cheapest first: smaller 4663 clip; 4663-specific rails; pause
4663 funded entries. This is an entry/exposure lever, not an exit knob. If P0 ships, much
of P3 is already covered.

**P4 — position 9003 is an unknown-confirm accounting defect, not a rail outcome.** Detail
below; resolve before trusting the cohort total or the `real_trail` decomposition.

## P4 — RESOLVED on-chain 2026-07-22: the de-risk never landed

Forensic via Solana RPC `getTransaction` on all three signatures:

| tx | slot | result |
|---|---|---|
| entry `34Kwaya…` | 433795444 | USDC 101.332982 → 51.332982 (**−$50.00**); token 0 → **292581.799277** |
| de-risk `3HDs2W9…` | — | **null — not found on chain** |
| exit `1Z3jq4no…` | 433796764 | token **292581.799277 → 0**; USDC 51.332982 → 68.632906 (**+$17.299924**) |

The null lookup alone would be ambiguous (RPC retention). The token ledger is not: at exit
the wallet still held **exactly the full entry quantity**. A 65.053% de-risk cannot have
landed. The exit's USDC delta also matches the booked fill notional to the cent.

**Consequences:**

- **Realized −$32.70 is correct** ($50.00 spent, $17.30 returned). The cohort total
  −$166.65 stands and is **not** understated. The `real_trail` decomposition is sound.
- **Branch B confirmed:** the trail armed without a completed de-risk.

### Why it armed — a conflation of two different questions

`solana_exit_executor.py:116` sets `derisked_at` on an **unknown** confirm on purpose: the
sell may have landed, and re-firing a maybe-landed sell risks a double-sell. That is right.

`memecoin_real_runner.py:178` then arms the trail with `armed = bool(pos.get("derisked_at"))`.
So "we must not re-fire the de-risk" silently became "principal has been recovered". They
are not the same claim, and only the second one licenses a trail.

### Why recovery never caught it — a race, not a missing feature

`recover_unverified_derisks` is correctly designed: `not_found` re-arms only after 3 spaced
reads **and** a stamp older than `DERISK_REARM_MIN_AGE_S` (20 min), so ambiguity never
causes a double-sell. On 9003 it accumulated **20** `not_found` reads — but the position
closed **371 s** after the de-risk stamp, i.e. **829 s before** `not_found` became decisive.
The trail (armed by the conflation above) closed the position out from under its own
recovery pass.

`unverified_derisks()` then filtered `is_open=1`, so the close stranded the signature
permanently.

### Fixed (shipped)

The recovery queue no longer drops closed positions
(`sniper/persistence/db_memecoin.py`), and the state machine branches on `is_open`: a closed
row settles its audit debt but never emits "rail re-armed" language or alerts, and repeated
`not_found` alone is decisive for it (the age gate guards a double-sell hazard that a closed
position no longer has). Four regression tests in `tests/test_memecoin_derisk_recovery.py`.

Once this reaches the pinned runner, 9003's own record self-heals: three `not_found` reads
clear the false `derisked_at`. **No manual DB surgery is proposed** — nothing needs booking,
because nothing landed.

### Still open — requires an operator checkpoint (real-exit behavior)

Trail arming should require **booked de-risk proceeds**
(`derisk_proceeds(conn, id) > 0`), not the `derisked_at` double-sell flag. This is a
correctness fix, and it is strictly less trigger-happy — a position with an unconfirmed
de-risk would fall back to stop/timecap/mirror instead of trailing on an imaginary recovered
principal. It still changes what real dollars do, so under Global Constraint 14 it ships
with the Checkpoint A/B cutover rather than as an audit fix.

## P4 original detail (superseded by the forensic above)

Facts from the ledger (`memecoin_positions`, `memecoin_fills`):

- De-risk approved, `fraction = 0.65053`, expected proceeds $76.86.
- `derisked_at = 2026-07-19T00:53:04Z`, `derisk_sig = 3HDs2W9rkMu5kM1mmPj4kw7XZ64Ezu2SgVXqKewxP9LGEvA41w1CZcq1CCGYy3i4j1LpRWcbpSKvvCY6ABaCZ4Kq`.
- **No `kind='derisk'` fill was ever booked.** Only two fills exist: entry (qty 292581.799277,
  $50.00) and `bracket_exit` (qty 292581.799277, $17.30, sig `1Z3jq4no…`).
- `derisk_recheck_hits = 20` — the recovery pass tried 20 times and never verified.
- `derisk_proceeds()` sums only `kind='derisk'` fills, so the trail confirmation ran with
  `prior_proceeds = 0`.
- **Root cause of the permanent stranding:** `unverified_derisks()`
  (`sniper/persistence/db_memecoin.py:3241`) filters `p.is_open=1`. Closing the position
  removed it from the recovery queue forever, with the signature still unverified.

Two branches, both requiring an on-chain check:

- De-risk landed → realized PnL is understated by the recovered principal, and the cohort
  total is wrong.
- De-risk failed → the trail armed without a completed de-risk, i.e. the
  `trail_requires_derisk` guard was satisfied by `derisked_at` alone rather than by booked
  proceeds.

Check **both** signatures, not just the de-risk one: the exit sold the full ledger quantity
(identical to the entry quantity). That quantity is ledger-derived (`open_qty`), so it does
not by itself prove the de-risk failed — but if the de-risk had landed, the exit transaction
could not have moved a full bag. The two transactions together resolve the branch.

Related defect regardless of outcome: `unverified_derisks()` should not drop rows on close.
A closed position with `derisk_sig` and no derisk fill is unresolved audit debt.

## What must NOT be concluded

- Not "the branch fired". It did not; the gate is n ≥ 30 and this is n = 29.
- Not "graduated exits are proven". Direction only, at n=29, with optimistic fills.
- Not "the rails are broken". Solana is net-positive with zero stops.
- Not "fixing exits makes this profitable". The optimistic ceiling is still negative.

## Corrections to the second draft (`003132bb`)

5. P0 was framed as a consistency bug ("three bands silently bypass the operator's
   decision") with a proposal to route every band through `real_stop_pct_for_chain`. Wrong
   — the band-specific stops are deliberate later decisions (07-16/07-18), documented in a
   docstring that names the chain-wide decision it is overriding, and pinned by tests.
   Implementing it as written would have silently reversed intentional policy.
6. "Stop disabled" was stated without qualification; it applies to the **normal** stop only.
   The −80% catastrophe floor is present on all 29 contracts.
7. The small-band 4663 subgroup was reported as **0 stops**; it has **1** (position 9380,
   −$31.86, operator `sl_pct=-60` override). This contradicted the band table in the same
   document, which had already reported one small-band stop.

## Corrections to the first draft (`ee35b093`)

1. "Branch fired" → the condition matched during an exploratory n=29 look; the registered
   branch did not fire, and the n=50 path must be newly pre-registered.
2. `funded-mid-v1` de-risk is **30**, not 35 (its trail is 35), and mid is **also positive**
   — the "only positive band" claim was wrong and self-contradictory.
3. Peak bins were 2/3/5; actual is **2/4/4**. A +20% rail reaches **3** of 10 stopped
   positions, not 5.
4. P2 originally named one field. It requires **two**: `trail_requires_derisk = false` *and*
   a non-zero `trail_arm_pct` (working value 15).

## Verification path before any knob moves

1. Resolve 9003 on-chain first — the cohort total and `real_trail` decomposition depend on it.
2. Pre-register an n=50 confirmation read on its own terms, including P0's chain-split
   comparison as a named hypothesis.
3. Implement P1/P2 in the **paper** arm first, matched-pair against the live control on the
   same entries, with the sample size pre-registered (the tp-vs-derisk A/B died for lack of n).
4. Any funded knob change goes through `scripts/deploy_memecoin_runtime.sh` with the full
   backup / reservation / reconcile gate. No hand-edited env on a live runner.

Artifacts: read + sweep scripts in the session scratchpad; cohort queried read-only from
`sniper_tracker.db` on 2026-07-22.

## 2026-07-23 — P1/P2 parameter study + combo_f paper arm (pre-registered)

Full-tape counterfactual on the same 29 closes (every position has a complete executable
mark tape, median 76 marks): `scripts/research/memecoin_partial_derisk_counterfactual.py`.
Three structures replayed per position (3% fill haircut; P-configs approximate the
remainder's fate as unchanged; trail replays run on the full executable tape):

| structure | best config | cohort total | Δ vs actual | tail cost (top-4 winners) |
|---|---|---|---|---|
| P — partial only | P(25%, f=0.5) | −$95.15 | +$71.50 | −13.7 / +0.4 / 0 / −5.9 |
| T — early full-bag trail (P2 shape) | T(arm 20%, trail 25%) | −$114.71 | +$51.94 | 0 / −18.5 / 0 / −4.4 |
| **C — partial + remainder trail** | **C(20%, f=0.5, trail 25–30%)** | **−$74.97 … −$82.07** | **+$84.57 … +$91.68** | −16.2 / −8.8…−11.7 / 0 / −8.6…−10.5 |
| C, 4663-only | C(20%, 0.5, 30) | −$84.86 | +$81.79 | solana untouched |

Findings: the all-or-nothing tail-truncation fear applies to full-bag trails (T hits winner
#2 for −18 to −30), NOT to partials — the remainder keeps riding, bounding tail cost at
f×(tail−rail). The combination dominates both pure structures across the whole
neighborhood; the region is flat (trail 25 vs 30 ≈ $7 at n=29 — noise), so parameters are
picked from the region + P2's empirical evidence (small band's working trail values), not
argmax. Honest ceiling unchanged: best config still leaves the cohort ≈ −$80; the rest is
entry selection (4/10 stopped never had a profitable exit at any rail).

### Shipped: `combo_f` paper challenger (per §next-steps "paper arm first")

Partial 50% at first +20% crossing (`_combo_partial`, one-shot via `derisked_at`), remainder
trails 30% off the running peak (trail arms ONLY after the partial), catastrophe stop −60%
always live, mirror ON (like control), no own timecap (arm-agnostic 14d backstop applies).
Knobs `MEMECOIN_COMBO_F_*`; enrolled via `MEMECOIN_ARMS`. Paper mid-mark convention, same as
every arm — comparable matched-pair.

### Pre-registered read (fixed now, before any data)

Read when `combo_f` has **≥30 closes**. Metrics, matched-pair vs `control` on shared
episodes (same mint + entry event):
1. Paired total PnL delta (combo_f − control), with sign and bootstrap CI.
2. Stop-class share of gross losses (combo_f vs control).
3. Tail retention: per-episode delta restricted to episodes where control's peak ≥ +40% —
   the study predicts a bounded negative here; a large negative kills the config.

Decision rule: graduate to a **funded** policy proposal (new policy version + cohort
cutover, Checkpoint B) only if (1) is positive and (3) is not materially negative. The
funded rail is deliberately NOT implemented yet — paper first. No peeking before n=30;
this section is the pre-registration.

---

## 2026-07-23 — EVIDENCE RETRACTION (review P0/P1): the counterfactuals are invalid

An adversarial review found that **every counterfactual number in this document that claims
to be an "executable-route" study is not one.** Both study scripts read
`memecoin_position_marks.executable_exit_value`, which is a **reserved alias written EQUAL to
`mid`** — confirmed at full scale: **1,077,115 / 1,077,115 rows have `executable_exit_value ==
mid`, zero exceptions** (`memecoin_exits.py` documents the alias). There is no true executable
tape until the fast lane goes live. So:

- **`scripts/research/memecoin_partial_derisk_counterfactual.py`** — the **+$85–92** partial /
  combination figures behind funded **v2** are a **mid-tape replay with a flat 3% haircut**, NOT
  executable-route outcomes.
- **`scripts/research/memecoin_structure_exit_counterfactual.py`** (untracked) — the **+$14.46**
  structure result is the same mid-relabeled read. It also **claims higher-highs but implements
  only swing/higher-lows** (`swing_lows` + break-of-most-recent-confirmed-low; no higher-high
  confirmation). **Do not cite its results.**

**Consequences, on the record:**

1. The **+$85–92** number did **not** justify the +20% / 50% parameters of funded **v2**. The v2
   *runtime* uses exact whole-balance quotes correctly (verified) — but the *evidence* that
   selected its knobs is void. v2 shipped ahead of, and now without, valid supporting evidence.
2. **Do not stage funded v3** on the structure counterfactual. It is invalid twice over
   (mid-relabeled tape + unimplemented higher-high claim).
3. **What still stands:** the *paper* reads (`combo_f`, and any `structure_g` arm) are
   **unaffected** — they book real paper fills, not the mislabeled tape. The disciplined path
   (paper-proves-then-funded, pre-registered read, matched-pair vs control) is the only valid
   route to a funded v3, and it is unchanged by this retraction.

The lesson is narrow and important: `executable_exit_value` is a **placeholder equal to `mid`**.
No study may treat it as an executable price until the fast lane populates it with real
whole-balance route quotes. Any counterfactual that needs executable pricing must wait for that
tape, or state explicitly that it is a mid-plus-haircut model (and not call it "executable").
