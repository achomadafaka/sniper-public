# Memecoin per-position exit control — manual overrides + auto templates (design)

Status: **design, for review** · Branch: `worktree-memecoin-exit-control-layer` (off origin/main @ 24f141cf, includes SP#1 foundation) · Date: 2026-07-14

## Problem / intent
Two asks that are one feature:
1. **Operator wants live, per-position manual control** on the Open Real Positions tab — reach into any open real position and move its TP / SL by **percentage or target market cap**, per intuition.
2. **The system should set per-coin strategies automatically** — scalp big-caps (grab +20-30% and leave, replacing the hard mcap ceiling), ride microcaps (wide/patient), trail fast-movers.

Both write the **same per-position exit-control layer**; the exit sweep reads one **effective** policy. This extends the existing `memecoin_exit_targets` (operator-set mcap-band TP, 2026-07-13), which becomes one row-source in the new layer.

## Precedence + authority (the load-bearing rule)
Effective exit for a position = **operator override > system template > default rails**.
- **Operator overrides have FULL authority** — may LOOSEN or tighten (wider SL for room, higher TP to let it run). This is the point of manual control; it is NOT bound by the system's tighten-only invariant. The UI confirms on a *loosening* change so it is never a fat-finger; every change is audited.
- **System templates stay tighten-only** (the SP#1 design invariant): a template may never widen risk beyond the default rails.
- **token_safety is never overridable** — ruin protection, out of scope of this layer.
- **Fail-safe:** the exit sweep reading an override is best-effort — a malformed/None override falls back to the default rails and never crashes the sweep (same discipline as the SP#1 log-only hooks).

## Data model
<!-- schema:external=memecoin_position_exit_overrides -->
New table `memecoin_position_exit_overrides` (one ACTIVE row per (position_id, source); operator row wins):

| column | meaning |
|---|---|
| id | pk |
| position_id | FK memecoin_positions.id |
| source | `operator` \| `scalp` \| `ride` \| `trail` \| `template` |
| tp_pct | take-profit as +% from entry (nullable) |
| tp_target_mcap | take-profit when mcap ≥ this USD (nullable) |
| sl_pct | stop as −% from entry (nullable; negative) |
| sl_target_mcap | stop when mcap ≤ this USD (nullable) |
| trail_pct | optional trailing-stop width (nullable) |
| actor | who set it (operator handle / template name) |
| note | free text |
| active | 1 = in force; superseded/cleared → 0 |
| created_at, resolved_at | audit |

`memecoin_exit_targets` keeps working (read alongside as a legacy `source='operator'` mcap target) — no destructive migration.

Implementation note (2026-07-15 audit): `trail_pct` remains schema-reserved only. The funded sweep
does not yet maintain and act on a real-position high-water mark, so the API rejects this field
instead of falsely reporting that a trailing stop is live.

## Components
- **Persistence** (`db_memecoin.py`): `set_position_exit_override(conn, position_id, source, *, tp_pct, tp_target_mcap, sl_pct, sl_target_mcap, trail_pct, actor, note)` (deactivates prior same-source row, inserts new, audited); `clear_position_exit_override(conn, position_id, source)`; `effective_exit_override(conn, position_id) -> dict|None` (operator row wins, else highest-priority template).
- **Resolver bridge** (`memecoin_policy.py`, SP#1): `apply_overrides(policy: ExitPolicy, override: dict, entry_price, current_mcap) -> ExitPolicy` — folds an override into the ExitPolicy, resolving `*_target_mcap` to an effective % given entry mcap→price scaling. Pure/tested. Keeps ONE code path.
- **API** (`sniper/routes/api_memecoin.py`, guarded by `SNIPER_API_KEY`):
  - `GET /api/memecoin/real/positions` — extend existing payload with each position's `effective_exit` (tp/sl, source, editable flags).
  - `POST /api/memecoin/real/positions/<id>/exit` — body `{tp_pct|tp_target_mcap, sl_pct|sl_target_mcap, trail_pct, clear?}` → `set_position_exit_override(source='operator', actor=<operator>)`. Validates ranges; returns the new effective exit.
- **UI** (`static/app.js` real positions tab): each row gains editable TP/SL inputs with a **%/target-mcap toggle**, a Set and a Clear button, and a live **effective-exit** readout tagged `you` / `scalp` / `ride` / `default`. A loosening change (wider SL / lower TP-below-current) pops a confirm.
- **Exit sweep** (`memecoin_exits.py` real path): before applying default rails to a real position, load `effective_exit_override`; if present, use it (resolve target-mcap to price via the live quote). Deploy-gated (touches real-money exits).
- **Auto templates** (SP#3): `scalp`/`ride`/`trail` are frozen ExitPolicy templates chosen at entry by mcap band and written as `source='<template>'` rows. Scalp replaces the `MEMECOIN_REAL_MAX_MCAP_USD` ceiling: a >$30M coin is no longer refused — it opens with the scalp template (small quick TP, tight SL) instead. Deploy-gated; runs as a paper arm head-to-head first.

## Build order
1. **Additive substrate (build + test now, no live behaviour change):** table + persistence helpers + `apply_overrides` bridge + unit tests.
2. **API + UI (additive):** the endpoint + the row controls; the operator can *set* overrides and see them, but nothing acts until step 3.
3. **Exit sweep honors overrides (DEPLOY-GATED):** the real exit path reads the effective override. This is the first real-money behaviour change — ships via `deploy_memecoin_runtime.sh` after tests + a dry-run.
4. **Auto scalp/ride/trail templates (SP#3, DEPLOY-GATED):** paper arm first, then the winner writes template rows live; the ceiling becomes a scalp-router.

## Out of scope
Overriding token_safety; the co-holder controller (SP#4); changing entry gating other than the ceiling→scalp reroute.

## Verification
Unit tests for the persistence helpers + `apply_overrides` (%, target-mcap, precedence, loosening allowed for operator / blocked for template). API tests (set/clear/validate). A dry-run of the sweep reading an override before any deploy. `validate_schema_refs` + `brain_index generate` after the new table.

## 2026-10-19 amendment: exact saves and executable cap targets

The operator selected executable sell value for custom TP/SL market-cap targets and legacy full-sell cap bands. This supersedes using a supplied market/display cap to confirm these targets, including the display-price allowance in the [rollover contract](2026-07-18-memecoin-exact-balance-rollover-design.md#executable-price-and-peak). Chart marks remain display evidence.

For an exact quote bound to this position, token, chain and actual sell quantity, confirmation uses `expected quote proceeds / quoted token quantity × target supply`. The target supply keeps its existing position basis: pinned FDV supply, otherwise the frozen entry market-cap/price ratio. Missing, invalid or mismatched quantity, supply or quote cannot establish a cap crossing. A custom TP needs that executable cap at or above its target; a legacy band uses its existing lower bound. A custom cap SL uses an executable cap at or below its trigger. Other independently active exits retain authority.

A TP submission must also prove that the signed route's minimum output for the actual sold quantity meets that cap target on the same supply and quote-currency basis. A stale quote, refreshed route or changed sell quantity requires the check again before signing. A route without a provable sufficient minimum cannot submit this TP. This floor is a transaction bound, not a promise that the order will land or that net realized PnL after network fees will equal the displayed cap. SL caps remain triggers: price movement and execution slippage may produce a fill below the stop cap. Do not impose a TP floor on another loss-reduction exit.

The non-atomic Flap two-leg estimate does not prove this TP floor. A custom cap TP also cannot use the unconfirmed route-capacity partial fallback or an ambiguous shared-mint custody quantity; these paths hold that custom TP while independent exits retain authority. Existing signed claims and journal recovery remain intact. Custom targets demand fresh quotes through the existing rail budget. Restored EVM bags may load missing decimals through the existing cached metadata reader on the exit path; a failed read holds the cap trigger.

Single-leg API edits identify `edited_leg` and explicitly supply that leg's percent or cap field (null unsets it; zero disables the percent rail). In one short SQLite write transaction, merge the current untouched TP/SL/trail/de-risk values and preserve their precision. Legacy requests without `edited_leg` retain whole-row replacement semantics. Invalid edit identity or missing selected fields refuses without a write. Once a full-close claim exists, set/reset refuses with 409; it never retargets signed bytes or releases the claim. Reset and FDV conversion/band replacement remain atomic. The editor shows exact editable numbers and names sell-value cap semantics; an active trail still participates when a leg is unset.

This amendment adds no entry-path call, configuration switch, default policy, sizing change or funded-contract rewrite. Deployment and organic fill evidence are separate from the offline tests of these requirements.
