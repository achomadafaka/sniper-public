# SRE-F1 — Entity layering

**Parent:** `2026-05-30-selection-reconstruction-engine-design.md`
**Purpose:** reconstruct, from raw fills, every unit of analysis the engine groups on. Pure compute on data we already fetch (`activity_rows` + `closed_positions`); no new fetch. Each layer becomes a table.

## Why
A *selection decision* is "enter a new market" — an **engagement**, not a fill. Today we only have positions. Without the layers below, every cadence/coverage signal is computed at the wrong grain (the SwissMiss mislabel: fill-cadence read as selection-cadence).

## Layers & how to build them

| Table | Built from | Key derivation | Edge cases |
|---|---|---|---|
| `sre_clip` | consecutive same-(condition,asset,side) fills | new clip when inter-fill gap > `CLIP_GAP_S` (default 120s) | single-fill clip is valid; clock jitter |
| `sre_engagement` | all fills for one (wallet, conditionId, outcome-token) | first_entry_ts = min BUY ts; n_clips, n_fills, accumulation_span; exit_ts; exit_mode | re-entry after full exit = same engagement (note `n_reentries`) vs new — flag both |
| `sre_event_engagement` | engagements grouped by `event_family`/eventSlug | which outcome(s) of the event taken; outcomes_available_in_event vs taken | event_family missing → singleton |
| `sre_basket` | new-market first-entries clustered in time | new basket when gap between *first-entries* > `BASKET_GAP_S` (default 600s); members = engagements in cluster | a basket of 1 = single-shot |
| `sre_session` | all activity clustered in time | new session when activity gap > `SESSION_GAP_S` (default 3600s); n_new_markets, n_fills, hours_active | overnight bot sessions never break |
| `sre_campaign` | engagements clustered by (time-window × dominant group) | run of ≥ `CAMPAIGN_MIN` engagements sharing a tight group within a rolling window | overlapping campaigns allowed (tagged) |

## Columns each engagement carries (feeds F3/F4)
`wallet, conditionId, outcome_token, event_family, first_entry_ts, first_entry_block, n_clips, n_fills, accumulation_span_s, ladder_shape, entry_price, total_size_usdc, exit_ts, exit_mode, resolved_outcome, realized_roi, basket_id, session_id, campaign_id, nth_market_for_wallet`.

## Derived "decision-grain" signals (the fix for the mislabel)
- **selection cadence** = inter-`first_entry_ts` gaps across engagements (NOT inter-fill).
- **selection schedule** = hour-of-day / day-of-week histogram of `first_entry_ts` (NOT fills).
- **execution intensity** = fills_per_engagement, clip cadence (kept SEPARATE — execution, not selection).
- **latency-from-creation** = `first_entry_ts − market.created_at` (needs F2).

## Outputs
Six tables above + an `sre_engagement` view that is the join key for everything downstream. All deterministic, idempotent, keyed by (wallet, conditionId, outcome_token).

## Acceptance
1. Σ fills over engagements == raw BUY/SELL fill count.
2. SwissMiss: selection-cadence (engagement grain) ≫ execution-cadence (fill grain) — reproduces the probe.
3. Basket/session boundaries stable under ±10% gap-threshold perturbation (report sensitivity).
