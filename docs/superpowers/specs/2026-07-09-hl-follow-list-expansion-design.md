# HL Follow-List Expansion — Design

**Date:** 2026-07-09
**Status:** design (approved direction; pending written-spec review)
**Files:** `sniper/hyperlive/watch_lifecycle.py`, `sniper/hyperlive/intel_store.py`,
`scripts/screen_hl_smart_money.py`, `scripts/run_hl_copy.py`
**Data:** `data/hl_intel.sqlite` (schema defined in `intel_store.py:ensure_schema`; **not** the
brain-indexed `sniper_tracker.db` — columns below verified via live `PRAGMA table_info`, not `brain_index`).

---

## Problem

The HL smart-money screen finds good wallets but the follow list is strangled downstream. Of 255
screened wallets, 81 are archetype-pass (70 `watch` + 11 `copyable`), yet only **5** are active copy
targets — and they are the wrong 5 (4 unproven `watch`; only 1 of 11 forward-proven wallets is
followed, the weakest of them). Three mechanisms cause this:

1. **Hard cap** `max_watch_targets=5` in `watch_lifecycle.maintain_watchlist`.
2. **Staleness cull forgets good wallets.** The screen deep-screens only ~40 leaderboard candidates
   per 6h run. A wallet that rotates out of the top-40 is never re-screened, ages past
   `WATCH_STALE_MS` (7d), and is culled by `_cull_reason` — for staleness, not performance. All 11
   proven wallets were last screened 13–17 days ago; the two best ($657k and $95k realized) are
   culled on staleness alone.
3. **Incumbent-biased ranking.** `_score()` adds `+2,000,000` (copy) / `+1,000,000` (follow) to
   wallets that already have paper positions, so a fresh high-quality wallet cannot outrank a
   mediocre incumbent for a slot.

Live-path note: the lifecycle `proven` tier is gated on `copyability_phase2(follow_stats)` — i.e. the
old **hl_follow lead-lag** forward data, which is net-negative — so `proven` is empty and the live
follow path is the `watch_fallback` basket. **Expansion targets that basket.** The phase-2 `proven`
gate and its lead-lag dependency are explicitly **out of scope** here.

## Goal

Follow **every** archetype-pass wallet that is not culled by real evidence (no cap — "if it's good,
it's good"), keep the pool fresh so good wallets are never forgotten, and rank by wallet quality
rather than incumbency.

---

## Design

### Change 1 — Re-screen tracked wallets every run (kills staleness at the source)

`scripts/screen_hl_smart_money.py`: add `--refresh-tracked`. When set, after
`rows = HL.fetch_leaderboard()` and building the top-N prefiltered candidates, also include the
rows whose wallet is a **tracked archetype-pass wallet** (`IS.get_tracked(conn)` filtered to
`archetype_pass`), pulled from the full `rows` set — bypassing the top-40 prefilter cut but reusing
their real leaderboard fields (account_value, windows, month_roi). Union with the prefiltered
candidates, dedup by wallet. Each run then re-screens them, refreshing `ts_ms` / `last_seen_ts_ms`.

- **Composes with the parallel WIP** (`--skip-existing`, `--fill-retries`, `--fill-sleep`,
  `--fill-delay`): `--skip-existing` filters the *leaderboard* portion (widen to new wallets);
  `--refresh-tracked` *appends* known tracked wallets (refresh). If both are set, tracked wallets are
  still included (refresh wins over skip). Touches the candidate-assembly block only; must not
  modify the WIP's fetch/throttle additions.
- **Edge:** a tracked wallet that has dropped out of the 40k leaderboard entirely won't be in `rows`.
  Fallback: screen it from a synthetic `{"wallet": w}` candidate (deep-screen only needs `wallet`;
  persisted account_value/month_roi fall back to the wallet's last stored screened values). Rare
  (these are high-value accounts); the implementation plan will resolve the exact fallback.
- **Cost:** ~40 leaderboard + ~70 tracked ≈ 110 fill fetches/run every 6h. Mitigated by the WIP's
  `--fill-delay 0.5 --fill-retries 2` in the cron. ~35 req/min-equivalent — gentle for the HL API.

### Change 2 — Remove the cap

`sniper/hyperlive/watch_lifecycle.py`: `maintain_watchlist(..., max_watch_targets=None)`. `None`
means **no cap** — every eligible archetype-pass wallet is activated as a copy target. The parameter
is retained (not deleted) so a numeric ceiling can be re-imposed later if the paper book needs
bounding. When `None`, skip the `[:max_watch_targets]` slice in both the `proven` and
`watch_fallback` branches. `scripts/screen_hl_smart_money.py` gains `--max-watch-targets`
(default: unset → `None` → no cap) passed through to `maintain_watchlist`.

The "good enough to follow" bar is therefore the existing **archetype-pass gate**
(`trader_intel.copyability`: directional, taker ≥ 0.5, n_closes ≥ 20, PF ≥ 1.1, not market-maker,
not degen-leverage), minus wallets removed by forward-evidence culls.

### Change 3 — Stale cull exempts archetype-pass wallets

`sniper/hyperlive/watch_lifecycle.py:_cull_reason`: the stale branch
(`now_ms - last_seen > WATCH_STALE_MS`) applies **only to non-archetype-pass wallets**. An
archetype-pass wallet is removed only by:
- a re-screen that downgrades its verdict (the existing `latest screen rejected` branch), or
- negative forward evidence (the existing mature/early follow-close and
  `copy_disqualification_reason` culls).

This decouples "we haven't re-screened it lately" from "this wallet is bad." With Change 1 the
re-screen window is ≤6h anyway; the exemption is the safety net for transient fetch failures.

### Change 4 — Quality-led ranking, evidence as tiebreak

`sniper/hyperlive/watch_lifecycle.py:_score()`: replace the `+2M/+1M` incumbency bonuses with a
**wallet-quality composite** as the primary term, forward evidence demoted to a bounded tiebreak:

```
quality = realized                      # lead term (USD realized PnL, from hl_tracked_traders.realized)
        + min(profit_factor, 50) * <w_pf>
        + month_roi * <w_roi>
        + win_rate * <w_wr>
evidence_tiebreak = small bounded +/-   # e.g. +ε if copy_mean_bps>0, +ε/2 if follow_mean_bps>0
score = quality + evidence_tiebreak
```

Exact weights fixed in the plan; the invariant to test is **ordering by quality, not incumbency**:
a fresh high-realized wallet with zero forward history must outrank a low-realized incumbent with a
positive copy book. With no cap, `_score` governs ordering/display only (nothing is cut), but it
stays correct for any future re-cap and for throttled fetch prioritization.

### Change 5 — Mirror fetch throttle (operational, enables no-cap)

`scripts/run_hl_copy.py`: add `--fetch-delay` (default `0.0`; cron sets ~`0.3`). At ~70 targets the
mirror otherwise bursts ~70 `fetch_clearinghouse_state` calls at the top of each 120s cycle. A small
inter-target delay spreads the burst. Sustained rate (~35 req/min) is well within HL limits; only
burst-spreading is needed.

---

## Non-goals

- **Not** touching the phase-2 `proven` gate or its hl_follow lead-lag dependency. `proven` stays
  empty; expansion lives in the `watch_fallback` basket. (Migrating `proven` onto the mirror-copy
  book is a separate, later decision.)
- **Not** killing the losing hl_follow lead-lag runner here (separate operator decision).
- **No** change to the archetype-pass gate thresholds in `trader_intel.py` (`MIN_CLOSES`,
  `MAKER_FRAC_MM`, `TAKER_FRAC_MIN`, `MIN_PROFIT_FACTOR`, concentration limits). The follow bar is
  the existing gate; we stop discarding wallets that pass it.

## Risks

- **Many phase-1-only wallets followed.** No-cap follows ~70+ wallets whose archetype-pass is
  "unproven until forward-follow." Accepted: it is a **paper** study; breadth is the point, and the
  forward closes are exactly what later separates winners. Per-wallet attribution relies on that
  accruing forward data.
- **Mirror load** at ~70 targets: mitigated by Change 5.
- **Study reset:** expanding targets changes the ~17h-old mirror book's composition. Cheap now
  (n=10 closed, pre-gate). Flagged, not blocking.

## Testing (TDD)

`tests/` (extend `test_screen_hl_smart_money_lifecycle.py` and/or a lifecycle test module):

1. **No cap** — `maintain_watchlist(max_watch_targets=None)` activates *all* eligible archetype-pass
   wallets, not a top-N.
2. **Cap still works** — a numeric `max_watch_targets` still truncates (regression guard).
3. **Quality-led ranking** — given two archetype-pass wallets, higher `realized` ranks above a
   lower-realized incumbent that has a positive copy book.
4. **Stale exemption** — an archetype-pass wallet past `WATCH_STALE_MS` is **not** culled; a
   non-archetype-pass wallet past the window **is** culled.
5. **Re-screen tracked** — with `--refresh-tracked`, tracked archetype-pass wallets are added to the
   screened candidate set (monkeypatch leaderboard + fills); composes with `--skip-existing`.
6. **WIP regression** — the existing WIP tests
   (`test_fill_screen_retries_transient_fetch_error`,
   `test_screen_can_skip_existing_wallets_and_throttle_between_candidates`,
   `test_persisted_screen_runs_watch_lifecycle_and_activates_fallback`) stay green.

## Verification

- `python3 -m pytest tests/test_screen_hl_smart_money_lifecycle.py -q` (+ any lifecycle test module).
- Dry run: `python3 scripts/screen_hl_smart_money.py --refresh-tracked --fill-delay 0.5
  --fill-retries 2 --intel-db data/hl_intel.sqlite` (persist to a **copy** of the DB first), then
  confirm active copy-target count jumps from 5 to the full eligible set and the previously-culled
  proven wallets are re-activated.
- Operator step (not auto-applied): update the screen cron to add `--refresh-tracked --fill-delay
  0.5 --fill-retries 2`; add `--fetch-delay 0.3` to the mirror.
