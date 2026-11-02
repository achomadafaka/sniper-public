# Far-Whale Liquidation Magnet — Paper Study v0 (pre-registered design)

**Status:** design / pre-registration. Spec only. No implementation until approved.
**Date:** 2026-07-04
**Scope guards:** paper-only · public data only · no live orders · no private keys · no production DB writes · no service restarts · no changes to the live A/B policy.
**Supersedes framing of:** the Thesis-1 "near liquidation cluster" magnet lane (`cluster_map.py` / `cluster_audit.py`). This is NOT a crowd-liquidation-density study.

---

## 0. Why this exists — what the data forced

The original Thesis-1 design assumed we could reconstruct a dense *near-price* liquidation book from public per-account sampling and trade the last push (≤50 bps) into it. A live smoke test refuted that assumption:

| Sample | Addresses polled | Cluster signals | NEAR (≤50 bps) + BIG (≥$1M) |
|---|---|---|---|
| A | 300 | 1 (ETH `short` 180 bps $4.1M) | 0 |
| B | 711 | 2 (ETH `short` 170 bps $4.75M; BTC `short` 270 bps $63k) | 0 |

Two facts:
1. **Cheap public sampling cannot reconstruct dense near-price liquidation density.** Even at 711 addresses, zero NEAR+BIG clusters appeared. The near design was self-defeating: rare in-sample *and* a tiny (<50 bps) move that barely clears the ~9 bps taker cost.
2. **It *can* catch individual large visible whale positions sitting far away.** ETH showed a consistent ~$4.7M liquidation level ~170 bps from mid across *both* independent samples — a real position, not sampling jitter.

Conclusion: pivot from *crowd density near price* to *single visible whale magnet far from price*. The move is large (50–300 bps ≈ 6–33× the fee), the signal is observable, and the method is honest about being a sparse sample.

```text
Old idea:  near crowd liquidation density        (unmeasurable, tiny move)
New idea:  large single-whale liquidation magnet (observable, large move)
```

---

## 1. Hypothesis (pre-registered, one sentence)

> A large visible single-whale liquidation level sitting 50–300 bps from mid raises the probability that price migrates **toward** that level over the next 10–60 minutes by enough to clear modeled taker cost, **relative to matched controls** — for a large enough whale.

This is a **paper-only** measurement of a price-migration *association*, not a causal claim.

---

## 2. Side semantics — READ FIRST (this is where bugs hide)

The existing code's `side` field is a **naming trap** and must be remapped in implementation. In `cluster_map.py:66`, `side = "long" if center > mid else "short"` — i.e. `side` labels *where the cluster sits* (magnet direction), which is the **inverse** of the liquidating trader's position side. "short cluster below mid" in the current code actually means *long*-liquidations below mid. Do not carry that field name forward.

Canonical fields for this study (define once, use everywhere):

| Field | Values | Definition |
|---|---|---|
| `liquidation_price` | float | The whale position's on-chain `liquidationPx`. |
| `mid_price` | float | Current asset mid. |
| `distance_bps_from_mid` | float ≥ 0 | `abs(liquidation_price - mid) / mid * 1e4`. |
| `direction_to_liquidation` | `up` / `down` | `up` if `liquidation_price > mid`, else `down`. |
| `position_side` | `long` / `short` | The **liquidating trader's** side (`long` if `szi > 0`). |
| `liquidation_type` | `long_liquidation` / `short_liquidation` | `long_liquidation` if the level is a long being liquidated. |

**Invariants (assert in code, fail-closed if violated):**
- A **long** position liquidates **below** entry → `liquidation_price < mid` → `direction_to_liquidation = down` → `liquidation_type = long_liquidation`.
- A **short** position liquidates **above** entry → `liquidation_price > mid` → `direction_to_liquidation = up` → `liquidation_type = short_liquidation`.
- Therefore: `long_liquidation ⇔ direction down`; `short_liquidation ⇔ direction up`.
- If a polled position's `szi` sign disagrees with `sign(liquidation_price - mid)` (e.g. a long whose liq is *above* mid), **flag it as a source/schema anomaly and drop the candidate** — do not silently coerce. Log the count of dropped anomalies per round.

The **paper entry direction is always `direction_to_liquidation`** (bet price migrates toward the level). Never phrase anything as "short cluster below mid."

---

## 3. Candidate definition (pre-registered gate)

A candidate signal exists at a poll timestamp when **all** hold:

```text
visible single-whale liquidation notional  >= asset-specific threshold   (§4)
50 <= distance_bps_from_mid <= 300
position still open / visible at signal_ts  (whale_weight: the single largest position, not a crowd sum)
asset in the tradeable universe (top-N by 24h notional)
spread_bps <= MAX_SPREAD_BPS
book/mid data fresh (age <= MAX_STALENESS_MS)
direction_to_liquidation is unambiguous (§2 invariants hold)
```

**Whale-weighted, not crowd-summed.** "Whale-weighted" means the size signal is the *single position's* notional — never an aggregate/crowd sum. It is **not** a top-1-per-asset cap: candidates are generated for **every** whale meeting this gate (the `bins_max` ranking in existing code is display ordering only). We are not estimating density; we are catching fish big enough that their own liquidation is a market event. This is what makes the study robust to under-sampling.

### 3.1 Episode definition (pre-registered — the unit of observation)

A whale parked at a level for hours would otherwise regenerate a "candidate" every poll round, making row counts meaningless.

```text
episode := (asset, whale_address, liquidation_price ± 25 bps)
  - repeated qualifying polls of the same episode collapse into ONE candidate
  - a new episode for the same whale starts only if: the level exits the gate for
    >= 60 min (price moved away / position closed / threshold not met), OR
    liquidation_price moves > 25 bps from the episode's frozen reference
  - PRIMARY paper entry = the FIRST valid entry per episode.
    Re-entries after exit within the same episode are logged as diagnostics only
    and are excluded from the primary outcome set.
```

The pass-bar counts (§12) are **episode counts**, not row counts. Reports show both `row_n` and `episode_n`.

---

## 4. Thresholds — sweep first, then freeze one

Do **not** hardcode a threshold. First run a **candidate-count sweep** over recent capture and report, per asset, how many candidates each threshold produces:

Initial thresholds to evaluate (report counts, do not assume):

```text
BTC:            >= $1M
ETH:            >= $1M
SOL / midcaps:  >= $250k – $500k
```

Deliverable before forward run: a table of `asset × threshold → candidate_count / day`. Then **pre-register exactly one threshold per asset** (recorded in `hl_far_whale_liq_reports` with a frozen `study_version`). Changing a threshold later starts a new `study_version` — never re-tune mid-run.

---

## 5. Entry logic (v0, deterministic, no discretion)

```text
On a valid far-whale candidate:
  enter paper in direction_to_liquidation (up -> long exposure; down -> short exposure)
  fixed paper notional (PAPER_NOTIONAL, e.g. $1,000)
  taker fill against the current visible book (modeled spread + slippage)
  fee = modeled HL round-trip taker (~9 bps; ROUND_TRIP_FEE_BPS), reported explicitly
```

- No discretionary entry, no post-hoc filtering after seeing the outcome.
- One open paper position per (asset, whale, liquidation_price) at a time — do not stack the same magnet.
- Record the `source_snapshot_hash` of the clearinghouseState that produced the candidate (provenance / reproducibility).

---

## 6. Horizon

Short microstructure horizons are wrong for a 50–300 bps target. Test fixed horizons **10 / 20 / 30 / 60 min**. **Primary (pre-registered): 30 min.** Others are diagnostics only.

---

## 7. Exit logic (v0, deterministic, pick one primary before forward run)

All references are **frozen at signal_ts** (symbolic, so no two implementers derive different formulas):

```text
D            = |liquidation_price@signal_ts - mid@signal_ts|        (frozen for the trade)
s            = +1 if direction_to_liquidation == up, else -1

target_price = mid@signal_ts + s * TARGET_FRAC  * D    (TARGET_FRAC  = 0.75)
adverse_stop = entry_price   - s * ADVERSE_FRAC * D    (ADVERSE_FRAC = 0.50)

target_hit:   price reaches liquidation_price@signal_ts OR target_price
time_stop:    exit at the primary horizon (30 min) if neither stop hit
adverse_stop: price reaches adverse_stop (moves 0.50·D against the entry)
target_moved: if the tracked liquidationPx drifts > 25 bps from the frozen level
              (whale adds margin / resizes), exit immediately, exit_reason=target_moved —
              never silently re-target mid-trade
```

Primary exit = `target_hit (75%) OR time_stop(30m) OR adverse_stop(50%) OR target_moved`. Do not optimize the exit after seeing results; a changed exit = new `study_version`.

---

## 8. Outcome labels (per signal)

```text
migrated_toward_liq : price moved toward the level by >= MIGRATE_MIN (bps or % of distance)
hit_liq_level       : price reached liquidation_price
liq_fired           : a liquidation event actually occurred, if observable
paper_pnl           : paper result net of modeled taker cost + slippage
mfe_bps             : max favorable excursion toward the level
mae_bps             : max adverse excursion away from the level
position_persisted  : whale position still open long enough to be relevant (see Control 4)
```

The **primary endpoint is price migration** (`migrated_toward_liq` / `paper_pnl`), **not** whether the liquidation fires. A whale can act as a magnet even if it closes before liquidation — but that is tracked separately (`liq_fired`, `position_persisted`), never conflated.

`MIGRATE_MIN` (25% of distance) is **deliberately looser** than `TARGET_FRAC` (75%): the label measures whether any real directional progress happened, independent of whether the trade closed at target. Not an inconsistency.

---

## 9. Controls (load-bearing — without these it is trend-following in a costume)

**Control 1 — dose-response (size × distance).** Migration/hit rate must rise with larger notional and closer distance (and with the notional/distance ratio). If migration is flat across size/distance, it is not a magnet.

**Control 2 — shuffle-side baseline.** Randomize/flip `direction_to_liquidation` while preserving asset, timestamp, |distance|, notional bucket, and volatility regime. A real magnet must beat this. **Scope honestly:** since direction is deterministic from the whale's side, this is close to a pure drift test — it cannot distinguish "magnet" from "coincident trend-alignment" on its own. Only Controls 2 AND 3 passing jointly is evidence; **passing Control 2 while failing Control 3 reads as null — no partial credit.**

**Control 3 — trend-matched baseline.** Match each signal to non-whale moments with similar recent return, volatility, spread, volume, OI change, asset, and time-of-day. A real magnet must beat the trend-matched baseline, not only the random one. **Capture mechanism (pre-registered):** the control pool is built from a **continuous background stream** — the detector records the market-state vector at *every* poll round (candidate or not), so qualifying non-whale moments exist in storage before any outcome is known. Matching (nearest-neighbor on the frozen field list above) runs only after treatment outcomes are fixed; which control instances *exist* is never conditioned on treatment results. A reactively/post-hoc sampled control pool would reintroduce exactly the post-hoc filtering §5 forbids.

**Control 4 — persistence (diagnostic, NOT a primary-set filter).** Track each whale per round: persisted / shrank / closed / liq-price-moved / source-disappeared. **The primary endpoint is intent-to-treat (ITT):** every valid candidate at `signal_ts` counts, regardless of what the whale does afterward — a live trader cannot know at entry whether the position will persist, so conditioning the primary set on persistence is survivorship look-ahead that inflates the hit rate. Report **two** numbers: (a) ITT migration/PnL over all valid candidates — the pass bar (§12) is judged on this; (b) the persistence-conditioned rate as a **diagnostic upper bound**, explicitly labelled not-achievable-live. High evaporation attrition is a kill signal (§13), not a reason to shrink the denominator.

**Control 5 — single-event concentration.** Report PnL by asset, by whale, by event/cluster. **No single whale/event may contribute more than a capped share (`MAX_EVENT_SHARE = 40%`) of total positive result.** The ETH whale must not define the study.

---

## 10. Persistence (proposed new tables — SQLite `hyperlive_engine.sqlite`, forward-accruing)

Proposed logical tables (new; not yet in the schema registry — implementation adds `ensure_*` + `brain_index.py generate`):

- `hl_far_whale_liq_candidates` — one row per detected candidate signal.
- `hl_far_whale_liq_position_snapshots` — per-round persistence tracking of each tracked whale position.
- `hl_far_whale_liq_paper_entries` — simulated entries.
- `hl_far_whale_liq_outcomes` — resolved outcomes per entry.
- `hl_far_whale_liq_market_state` — continuous background market-state vector per (asset, poll round), candidate or not — the pre-treatment-blind pool Control 3 matches from (§9).
- `hl_far_whale_liq_controls` — matched control rows (shuffle-side + trend-matched) per candidate.
- `hl_far_whale_liq_reports` — frozen `study_version`, thresholds, pass/kill readouts.

Required fields (distributed across the tables above):

```text
candidate_id · study_version · asset · signal_ts · mid_price · liquidation_price
distance_bps · direction_to_liquidation · position_side · liquidation_type
whale_address · position_notional · position_size · source · source_snapshot_hash
position_persisted · entry_price · paper_notional · fee_assumption
exit_reason · exit_price · horizon · mfe_bps · mae_bps
hit_liq_level · liq_fired · paper_pnl · control_group_id
```

Stamp timestamps with the local wall clock consistently (this lane is a self-referential time series — no exchange-vs-local comparison, so the +7h host skew cancels; see the cascade-capture lesson for when it does NOT cancel).

---

## 11. UI — must not imply a full book

Surface on the Hyperliquid runtime tab as a **live lane**, labelled honestly:

```text
Sparse visible whale sample — NOT full liquidation density.
```

Display: largest visible whale liquidation · `direction_to_liquidation` · `distance_bps` · `notional` · position persistence · source freshness · candidate status · control-comparison status.

**Never** label it "liquidation book" or "crowd cluster." The `/api/hyperlive/runtime` lane note must carry the sparse-sample caveat.

---

## 12. Pass bar (do not read a verdict before this)

All counts are **episodes** (§3.1), not rows:

```text
>= 50 valid forward candidate episodes
>= 30 completed episode outcomes (ITT primary entries)
>= 10 distinct whale addresses
>= 3 independent assets (or an explicit asset-specific, not pooled, label)
```

**Pre-registered statistical test (the decision rule — no eyeballing point estimates):**

```text
statistic   : mean net paper_pnl, and migration rate differenced against each control
CI method   : episode-clustered bootstrap (resample EPISODES, never i.i.d. outcome rows),
              10,000 draws — same discipline as the crypto5m lane harness
PASS        : 95% CI lower bound > 0 for BOTH
                (a) net paper_pnl after modeled costs
                (b) migration-rate delta vs EACH of Controls 2 and 3
DECAYED     : point estimate > 0 but CI spans 0 at the milestone → labelled
              "indeterminate/decayed", NOT a pass, NOT silently extended
```

Pass additionally requires **all** (evaluated on the ITT primary set):

```text
dose-response by notional/distance exists             (Control 1)
Control 3 pass is mandatory — Control 2 alone is never sufficient (§9)
no single whale/event exceeds MAX_EVENT_SHARE (40%)   (Control 5, and the same
  concentration check applied to CANDIDATE counts, not just positive PnL)
evaporation attrition (Control 4 diagnostic) is tolerable
```

## 13. Kill / revise bar

```text
no dose-response by size/distance
shuffle-side performs the same or better
trend-matched baseline explains the result   (kills regardless of Control 2 — no partial credit)
most positions close before price migrates   (high Control-4 evaporation attrition)
paper PnL loses after costs (95% CI upper bound < 0 → hard kill; CI spans 0 → decayed)
result depends on one ETH whale (or episode/candidate concentration breaches MAX_EVENT_SHARE)
SPARSITY TIME CAP: pass-bar counts (§12) not reached within CAMPAIGN_CAP_DAYS = 60
  calendar days of forward capture → close as sparsity-killed. No silent extension —
  extending requires a new study_version. (The crypto5m lane's milestone evals never
  ran for 9 days before anyone noticed; this cap exists so that cannot repeat here.)
```

---

## 14. Honest limitations (state these in the report and UI)

- Public sampling does **not** reconstruct full liquidation density; this tracks *visible large positions only*.
- A liquidation level is **not necessarily causal**; trend and volatility can mimic magnetism (that is what Controls 2–3 exist for).
- Whale positions can be **closed or defended** before price arrives.
- Result is **paper-only** until forward validation clears the pass bar.
- **Maker vs taker** execution assumptions are reported separately. v0 is taker; a later maker "fade-the-overshoot" variant (be the resting bid that absorbs the forced fill) is a distinct study, not folded in here.

---

## 15. Build path (each step gated; do not skip to strategy changes)

1. **Spec only** (this document) — approve before any code.
2. **Candidate detector** — remap `side` → canonical semantics (§2), whale-weighted, §3 gate, threshold sweep (§4).
3. **Position-persistence tracker** — re-poll tracked whales each round (Control 4).
4. **Paper entry simulator** — §5 entry, §7 exit, modeled cost.
5. **Control generator** — shuffle-side + trend-matched (Controls 2–3).
6. **Outcome evaluator** — §8 labels, dose-response (Control 1), concentration (Control 5).
7. **UI / report** — §11, sparse-sample-labelled live lane.
8. **Forward paper campaign** — watchdog + cron like the cascade lane; read verdict only at the §12 milestone.

No active strategy changes, no live-A/B edits, until the spec is approved and each build step is verified.

---

## 16. Frozen v0 parameters (to confirm at spec approval, then locked under `study_version = fwlm-v0`)

```text
universe          = top-20 HL assets by 24h notional
distance band     = [50, 300] bps
whale threshold   = per-asset, set AFTER the §4 sweep (BTC/ETH ~$1M, SOL/mid ~$250–500k to review)
candidate scope   = EVERY whale meeting the §3 gate (no top-1-per-asset cap)
episode rule      = §3.1 — (asset, whale, liq_price ±25bps); first entry per episode is
                    primary; 60-min gap or >25bps liq-price move starts a new episode
primary analysis  = intent-to-treat over all valid candidates (persistence = diagnostic only)
primary horizon   = 30 min   (diagnostics: 10/20/60)
entry             = taker, direction_to_liquidation, PAPER_NOTIONAL = $1,000
exit              = target 75%·D OR 30m time-stop OR 50%·D adverse-stop OR target_moved
                    (D and liquidation_price frozen at signal_ts — §7 symbolic forms)
fee               = ROUND_TRIP_FEE_BPS (~9 bps taker), reported explicitly
MAX_SPREAD_BPS    = 5 bps      (recommended; liquid majors are tighter — skip candidate if wider)
MAX_STALENESS_MS  = 5000 ms    (recommended; mid/book must be fresh within 5s)
MIGRATE_MIN       = 25% of signal-time distance toward the level (directional-progress label,
                                separate from paper_pnl; deliberately looser than TARGET_FRAC)
MAX_EVENT_SHARE   = 40%        (Control 5 cap — applied to positive PnL AND candidate counts)
decision rule     = episode-clustered bootstrap, 95% CI lower bound > 0 on net PnL and on
                    the delta vs each control (§12); CI-spans-0 = decayed, not pass
CAMPAIGN_CAP_DAYS = 60         (sparsity kill if §12 counts unmet — no silent extension)
pass bar          = §12   kill bar = §13
```

Any change to a frozen parameter starts a new `study_version`. The pre-registration is the contract.
