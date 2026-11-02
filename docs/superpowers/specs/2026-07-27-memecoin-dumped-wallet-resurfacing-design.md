# Memecoin dumped-wallet re-surfacing (graveyard revival) — design

**Date:** 2026-07-27
**Status:** approved direction; not approved for activation
**Scope:** read-only operator surface + optional operator-gated re-observe; no auto-arming, no funded path

## Problem

Today `excluded=1` is a permanent burial: an excluded wallet leaves `curated_user_ids`, so it stops
generating paper positions; follow-sync deliberately never re-adds it; and after ~30 days its stale
paper evidence ages out of the recommender window. So a dumped wallet **can never demonstrate
improvement** — the exclusion removes the very evidence loop that would prove it. Operators expect
"if a dumped wallet gets good again it should reappear in discovery"; that behavior does not exist.

Two things already close most of the gap and are prerequisites, not part of this build:
- Grade with the honest fomo-excluded alpha_b metric everywhere (already true in-system).
- Dump via **revoke-to-observed**, reserving `excluded` for confirmed trash. Observed wallets are
  continuously re-graded by the recommender and auto-resurface on the promote list — no feature
  needed. This spec covers only the residual: wallets that ARE hard-excluded and later improve.

## Decision

Add a **read-only "Reconsider" surface** that flags hard-excluded wallets showing fresh, independent
promising signals, so the operator can choose to re-observe them. No automatic un-exclude, no
automatic arming. Confirmed trash with no fresh activity stays buried and generates zero churn.

## Signals (all from data captured feed-wide, independent of curation)

Excluded wallets keep appearing in the feed even though we stop simulating them. Rank reconsideration
by signals we DO have for everyone:

1. **Fresh feed activity** — count/size of large buys in `memecoin_events`/`memecoin_firehose` in the
   last N days (default 7). Zero fresh activity → never surface (no churn, stays buried).
2. **Consensus with armed wallets** — the excluded wallet is co-buying the same `(network_id, mint)`
   that currently-armed wallets are buying (a weak but independent "still running with the pack"
   proxy). Uses the existing consensus/co-buy read.
3. **External leaderboard trend** — if `external_discovery`/leaderboard PnL is tracked for the wallet
   and has turned up since exclusion.

None of these is copy-PnL (that requires re-observing). The surface is explicitly a *reconsider
prompt*, not a re-grade. Honest label: "fresh activity, unproven under our exits."

## Surface

Extend the existing discovery/recommendations area (read-only) with a **Reconsider** section:
- excluded wallets meeting `fresh_buys >= MIN_FRESH_BUYS` in the window, ranked by a composite of the
  three signals;
- each row shows: handle, why-excluded (last audit reason + honest PnL/closes at exclusion), fresh
  buys (N/window), armed-consensus overlap, external trend if known, and days-since-exclusion;
- one operator action: **Re-observe** → `set_wallet_excluded(uid, False)` (→ observed, NOT armed).
  From there the normal loop takes over: paper evidence regenerates, recommender grades over ≥5
  closes, and it earns promote on its own merits.

## Non-goals / guardrails

- No auto un-exclude, no auto-arm, no funded mutation.
- No re-surfacing of wallets with no fresh feed activity (keeps the graveyard quiet / churn-free).
- Re-observing regenerates evidence from scratch; the surface never claims a copy-PnL the wallet
  hasn't earned post-revival.
- Confirmed trash the operator wants gone forever stays excluded until they choose otherwise.

## Alternative considered: time-boxed auto-revival

Auto un-exclude any excluded wallet after N days if it shows fresh activity, give it a probation
window, re-cut if it fails. Rejected as default: it re-adds churn from confirmed losers and mutates
the roster without operator intent. The operator-gated read-only surface gets the same visibility
without the churn. Could be a later opt-in flag.

## Acceptance criteria

1. Excluded wallets with fresh feed activity appear in the Reconsider surface, ranked by the composite.
2. Excluded wallets with no fresh activity never appear (no churn).
3. The surface performs no mutation; the only action is operator Re-observe → observed (never armed).
4. A re-observed wallet regenerates paper evidence and is graded by the existing recommender.
5. All reads use the honest fomo-excluded conventions; no contaminated all-fills metric anywhere.

## Testing

RED-first focused tests: fresh-activity ranking, zero-activity suppression, read-only guarantee
(no writes from the report path), Re-observe sets excluded=0/paused=0 and leaves real_approved=0,
and a re-observed wallet reappears in the recommender once it has ≥5 honest closes.
