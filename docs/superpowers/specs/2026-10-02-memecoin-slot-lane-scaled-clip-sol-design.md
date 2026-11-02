# Slot lane: fund a regime-scaled clip in native SOL instead of refusing it

Date: 2026-10-02. Author: Lambda. Requested by Alpha (item 3, "make the lane size the 8").
Status: spec only. This is a money-path change (entry funding and sizing), so the edge-case
answers in §5 go to the operator through Alpha before any code is written.

### 2026-10-03 amendment: warm cash legs cover both regime clips

The native funding path and subsequent on-demand cash-leg work are implemented. Position 69045
exposed a remaining delay: its coin scale was 1.0 while the warm leg used 0.75. Native funding was
unavailable; the on-demand USDC→wSOL leg took 352.1 ms before the lane could reserve and send.

With `MEMECOIN_SLOT_LANE_SCALED_SOL` enabled, the prefetch thread now keeps two independently
quoted Pump cash legs: the market clip and the other configured regime clip. Both are floored
to cents with `scaled_funded_clip`, rather than enlarging a leg past its reservation. Each leg
retains its own refresh cadence and 3-second freshness limit. The curve leg unwraps SOL; the
AMM leg retains wSOL. Immutable snapshots are replaced whole; the entry thread only reads them.

A matching warm leg may cover a scaled USDC reservation when native SOL is short. Reservation
and builder checks still enforce the final approved clip and the existing 90–100% spend bound.
This changes §3 step 5's original blanket prohibition on a scaled USDC leg. No swap instruction
is resized. Cold, stale or differently sized clips retain their existing refusal/on-demand path.
Arbitrary trial sizes and exotic quote assets are not prefetched by this amendment.

The extra work is bounded to one additional GET quote + POST instructions per Pump builder per
refresh interval (live interval 1.5 s), off the entry thread. Provider failures retain old stamps;
they never mark stale legs fresh. Disabling the scaled path restores single-leg quote load.
Rollback is the prior pinned release; funded reservation identities and policies do not change.

## 1. The 8 fills, re-read

Gamma's `sizing_disagree_8.csv` lists the Solana fills (2026-09-25 .. 10-02) where the slot lane
saw the source buy, refused it on sizing, and another lane filled the same signature. All 8 were
filled by `onchain_watch`.

| Refusal | Fills | Cause | Fix |
|---|---|---|---|
| `skipped_unsized` | 66303 FIM, 67628 DUCK, 67708 Minecraft, 68119 OCTO | The lane's own refusal was `slot_lane_quote_not_sol`. The pools are quoted in PUMP (`pumpCmXq…`), hSOL (`he1ius…`) and two xStocks (`XspzcW1…`, `XsoCS1Tf…`). The handoff could not value the source buy, so it wrote no row; the watcher's own row filled. | None new. E2 (`pump_amm` in `MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES`) builds these through the on-demand leg. Unsure whether the two xStock quotes pass E1's fee-bearing-quote refusal (Token-2022 extensions); the first live attempt answers it. |
| `slot_lane_scaled_clip` | 65891 PAUL (pump curve), 67012 Float (pump AMM), 67943 FILLD (pump AMM), 66799 CURVECAT (routed CPMM) | The coin classified at a risk scale other than the one the USDC→SOL leg was prefetched for (`leg_plan`). The lane refuses before reserving, because the leg would build `leg_over_clip` / `leg_under_clip`. | §3: when the clip can be paid in native SOL, no leg is used, so the scale mismatch does not matter. |

Context only, not a gate: realized PnL on the watcher lane was −$66.5 for the 4 scaled-clip fills
and −$1.4 for the 4 unsized ones. The case for the change is the lane's speed (an S+1 fill instead
of a watcher fill), not these 8 outcomes.

Over the full runner logs since 2026-09-27 the lane wrote 21 `slot_lane_scaled_clip` lines
(13 in `memecoin_runner.log-20260929T001701`, 8 in the current log): notdecu 8, cpmm-shadow
duplicates 3, OmakaseOnly 3, DegenCapitalLLC 2, untaxxable 2, hashbergers 1, SossaDotKek 1,
probablyinsider 1. The 4 above are the ones another lane then filled.

## 2. Today's code

`SlotLane._reserve_and_execute` (`sniper/services/memecoin_slot_lane.py`):

1. `classify_funded_entry(...)` gives `classification.risk_scale`.
2. If `classification.risk_scale != leg_scale_fn()`, return `slot_lane_scaled_clip`. Nothing is
   reserved or signed.
3. Otherwise compute `prefer_sol`: venue is `pump_curve` or `pump_amm`, the cap key is not
   `:exotic`, `0 < clip_guess <= MEMECOIN_SOL_EXEC_BUFFER_CAP_USD` (live value 175), and the SOL
   balance after the gas reserve covers `clip_guess`. `clip_guess` is the trial clip, or else
   `shadow_clip_fn()`, which is the roster clip at `leg_scale`.
4. With `prefer_sol`, `reserve_entry(..., funding_asset="sol")`. On "SOL buffer insufficient" it
   falls back to the USDC reservation (which then uses the prefetched leg).

A SOL-funded entry never touches the USDC leg: `pump_curve_buy.build` and `pumpswap_buy.build`
spend `funding_lamports` directly. So step 2 refuses entries that step 4 could fund without a leg.

## 3. Change

Behind a new knob `MEMECOIN_SLOT_LANE_SCALED_SOL` (default `0`; `0` is exactly today's behaviour):

1. Keep the classification read. Remember `scaled = classification is not None and
   risk_scale != leg_scale`.
2. When `scaled` and the knob is on, `clip_guess` for a non-trial entry becomes
   `scaled_funded_clip(base_clip_usd=shadow_clip / leg_scale, risk_scale=classification.risk_scale,
   floor_usd=cfg.MEMECOIN_MIN_TRADE_FLOOR_USD)`. This is pure arithmetic: no new read. A trial keeps
   its own clip, as today.
3. Compute `prefer_sol` exactly as today, with that guess.
4. If `scaled` and not (`knob on` and `prefer_sol`): return `slot_lane_scaled_clip`, as today.
5. If `scaled` and `prefer_sol`: reserve with `funding_asset="sol"`. If that refuses with
   "SOL buffer insufficient", return `slot_lane_scaled_clip`. **Never fall back to the USDC
   reservation on a scaled coin**, because the leg does not fit its clip.
6. Everything from the reservation onward is unchanged. `reserve_entry` sizes the clip from the
   classification as it does for every entry. The builder's funding-mismatch guard and the
   target-mint guard stay.
7. Log one substage on the trace, `slot_scaled_sol` with `risk_scale`, `leg_scale` and `clip_guess`,
   so the forward read can count these entries.

Not in scope:
- **CPMM, LaunchLab, DAMM v2.** These have no native funding: `raydium_cpmm_buy.build` returns
  `native_funding_not_supported_on_cpmm`. 66799 stays refused. The fix for those venues would be
  an on-demand leg quoted at the scaled clip (the E1 worker), which adds a Jupiter quote before the
  build. Build it only if scaled CPMM refusals recur; there was 1 in 7 days.
- **Clips above the SOL buffer cap ($175 live).** These stay refused. The 4 fills were $40–$131.

## 4. Why not re-quote the USDC leg

The re-quote option would quote an on-demand USDC→SOL leg at the scaled clip off the lane thread
(E1's `OnDemandLegWorker`) and requeue. It covers every venue, but it puts a Jupiter round trip
in front of every scaled fill (E1 paper key, CPMM leg p50 159 / p90 325 ms, n=142) and adds Jupiter load. The SOL path costs nothing
on the fill path and covers 3 of the 4 fills. Recommendation: build the SOL path; revisit the leg
re-quote only on CPMM evidence.

## 5. Edge cases (CLAUDE.md rule 5)

1. **Money path: yes.** The change decides whether a funded Solana entry is sent and which asset
   funds it. It does not change how big the clip is: `reserve_entry` sizes it from the
   classification, as it does for every entry today. Operator answer needed before the edit.
2. **Fill-path speed.** No new I/O, lock or retry. The guess is arithmetic on the cached roster
   clip. The SOL reservation is the existing `prefer_sol` path, which already runs for unscaled
   curve and AMM buys. A refused buy becomes no slower. Measured p50/p90: the refusal today returns
   at `ms=1.9–4.5` (lane lines); the SOL path's reservation and build costs are the existing
   `prefer_sol` traces, unchanged by this spec.
3. **Concurrency / global state.** Lane thread only. `shadow_clip_fn` and `leg_scale_fn` read the
   lane's cached roster dict (written by the prefetch thread). A torn read can pair a new clip with
   an old scale; the outcome is only a wrong guess, and `reserve_entry` decides the real clip and
   refuses if SOL cannot fund it. No module attribute, env var or shared connection is written.
4. **Absent vs zero vs unknown.**
   - `classification is None`: not scaled, today's path.
   - `leg_scale <= 0`, `None` or not finite: cannot derive a base; return `slot_lane_scaled_clip`.
   - `shadow_clip_fn` is `None` or returns `None`/≤0: no guess, `prefer_sol` is false, refuse.
   - `risk_scale` outside (0, 1]: `scaled_funded_clip` raises; catch it and refuse
     `slot_lane_scaled_clip`. Never send on an unreadable scale.
   - `snap.sol_balance` or `snap.sol_usd` is `None`: `prefer_sol` is false, refuse, as today.
5. **Dependency failures.** SOL short, or a competing reservation taking native funds, gives
   "SOL buffer insufficient", which refuses (no USDC fallback). A build failure releases the
   reservation, as today. A send failure is the existing executor's ambiguity handling.
6. **Units.** The clip is USD. `funding_lamports = floor(clip / sol_usd × 1e9)` happens inside
   `reserve_entry`, as today. The pump AMM native ATA rent overhead (2×) is already applied on the
   `prefer_sol` path.
7. **The sample.** A forward count is entries with the `slot_scaled_sol` substage. The denominator
   is our own refusals turned into sends, so it describes the lane, not the market. No PnL claim
   at n=4; the read is coverage (refusal → slot fill) and land-S against the watcher's fills on
   the same wallets.
8. **Rollback.** `MEMECOIN_SLOT_LANE_SCALED_SOL=0`, then a runner restart. While half-applied
   (code deployed, knob off) the behaviour is exactly today's.

## 6. Tests the build must carry (Gamma's bar)

1. Scaled coin, knob on, SOL covers the scaled guess: `reserve_entry` gets `funding_asset="sol"`
   and the guess is `scaled_funded_clip(shadow/leg_scale, risk_scale, floor)`.
2. Scaled coin, knob on, SOL reservation refuses "SOL buffer insufficient": the result is
   `slot_lane_scaled_clip` and there is no second (USDC) `reserve_entry` call.
3. Knob off: a scaled coin is refused `slot_lane_scaled_clip` before any reservation (today).
4. Scaled coin on an `:exotic` cap key, or on CPMM: refused `slot_lane_scaled_clip`.
5. `leg_scale` 0 / `None`, and `risk_scale` outside (0, 1]: refused, nothing reserved.
6. Unscaled coin: today's behaviour is byte-identical, including the USDC fallback.
7. Mutation check: dropping the "no USDC fallback on a scaled coin" guard must fail test 2.

## 7. Lift criterion

Turn the knob on after Gamma PASS and the operator's yes. Read again after 10 `slot_scaled_sol`
sends: land-S and fill rate against the same wallets' watcher fills. Keep the knob on if the
median land-S beats the watcher's and nothing lands outside its reservation. Otherwise turn it off.
