# Hyperliquid Watch Lifecycle and Fallback Copy Design

Date: 2026-07-08
Status: implementation spec

## Goal

Keep the Hyperliquid analyzer useful after the far-whale lane was retired: continuously discover wallets,
retain the useful watch cohort, cull unwanted wallets, and run paper mirror-copy from `proven` wallets or,
when none exist, a bounded `watch_fallback` basket.

## Non-Goals

- No real orders, keys, exchange signing, or production execution.
- No claim that `watch_fallback` wallets are proven profitable.
- No hidden cohort-following surface. Copy targets must have an explicit tier and reason.
- No revival of far-whale liquidation logic.

## Data Model

`hl_tracked_traders` keeps the research cohort and gains lifecycle fields:

- `lifecycle_status`: `active` or `culled`.
- `lifecycle_reason`: current inclusion or cull reason.
- `lifecycle_updated_ts_ms`: last lifecycle decision.
- `last_seen_ts_ms`: last screen time where the wallet was archetype-pass.
- `culled_ts_ms`: cull time, null for active wallets.

`hl_copy_targets` gains execution metadata:

- `copy_tier`: `proven` or `watch_fallback`.
- `reason`: why this target is active or inactive.
- `updated_ts_ms`: last target update.

`hl_copy_book` stores `copy_tier` at open time. New rows inherit the active target tier; legacy rows migrate
to `unknown` so old paper results remain usable without pretending they had tier attribution.

Existing DBs must migrate additively. Old rows default to `active` tracked state, `proven` target tier, and
`unknown` copy-book tier.

## Lifecycle Rules

The lifecycle manager runs after each persisted screen and can also run standalone.

1. Mark tracked wallets `proven` if phase-2 forward-follow passes existing `copyability_phase2`.
2. Cull wallets when the latest screen now rejects them.
3. Cull wallets with at least 30 closed follows and non-positive mean net bps.
4. Cull wallets with at least 10 closed follows and mean net bps <= `-25`.
5. Cull wallets with at least 20 closed paper copies and non-positive mean copy bps.
6. Cull wallets with at least 5 closed paper copies and mean copy bps <= `-25`.
7. Cull stale wallets not seen for 7 days if they have no open follows.
8. Keep remaining archetype-pass wallets as `active watch`.

Cull means deactivating any copy target. Open paper copies are not deleted; the copy runner closes them at
current mark on the next cycle. Self-disqualification uses all closed paper-copy rows for that wallet, while
the stats also expose a `by_tier` breakdown for attribution.

## Copy Selection

Selection is explicit and deterministic.

- If any `proven` wallet exists, activate only `proven` copy targets, capped by score.
- If no `proven` wallet exists, activate a bounded `watch_fallback` basket from the best active watch wallets.
- The fallback basket default cap is 5 wallets.
- Watch ranking prefers own paper-copy evidence first, then forward-follow evidence, then historical quality:
  - positive closed-copy count and mean copy bps;
  - positive follow count and mean net bps;
  - win rate;
  - profit factor capped at 50;
  - month ROI.

The copy runner may open new paper copies only for executable targets:

- `copy_tier='proven'` and effective verdict `proven`; or
- `copy_tier='watch_fallback'`, no proven wallet exists, lifecycle status is `active`, and effective verdict is `watch`.

## Funnel

`screen_hl_smart_money.py --persist` remains the wallet funnel. After persisting a screen it runs the lifecycle
manager unless disabled with `--no-maintain-lifecycle`.

`scripts/maintain_hl_watchlist.py` provides a standalone lifecycle pass for operators and watchdogs.

## API and UI

The API exposes active and blocked copy targets with:

- `copy_tier`
- `copy_status`
- `lifecycle_status`
- `lifecycle_reason`
- copy-book rows with tier-at-open attribution

The UI must distinguish:

- `watch`: phase-1 research, not copied;
- `watch_fallback`: paper copied only because no proven wallets exist;
- `proven`: phase-2 paper proof passed;
- `culled`: disposed from follow/copy selection.

The HL Analyzer tab includes a paper signal tape showing open mirrors and recent closed paper copies with
state, coin, side, tier, target, entry/exit marks, net bps, and age.

Manual `/api/hyperlive/copy/set` stays conservative: it can only activate `proven` targets. The fallback basket is
managed by lifecycle code, not by a one-click manual override.

## Edge Cases

- Legacy `copyable` rows display as `watch` and backfill `archetype_pass=1`.
- A stale active copy target with no proof is deactivated.
- A fallback target becomes ineligible as soon as any proven wallet appears.
- A culled wallet with an open copy is not fetched for new positions; the runner closes its open copy at mark.
- A screen API/fill error does not cull a wallet by itself; culling only uses persisted evidence.
- A wallet missing from the current top-N screen is stale only after 7 days, not immediately.
- Open follows protect a stale wallet from culling until the paper follow resolves.
- Open copies do not protect a wallet from culling; they are closed by the copy runner.
- Negative follow evidence culls even if historical fill metrics are strong.
- Negative paper-copy evidence culls even if historical fill metrics and follow history are strong.
- Existing DB files migrate without destructive rebuilds.
