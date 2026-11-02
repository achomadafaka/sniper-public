---
memory_name: spec/tag-wallet-pnl-held-only-patch-2026-04-25
memory_type: reference
memory_description: B1 patch — add held-to-resolution win rate to wallet_tag_pnl. Excludes positions closed purely via SELL (sold-on-winners bias). Unblocks B2 wallet_tag_fit consistency component.
memory_tags: [spec, tag-analytics, wallet, pnl, b1, patch, held-only]
---

# B1 patch — `wallet_tag_pnl` held-to-resolution win rate

**Date:** 2026-04-25
**Parent spec:** [B1 tag_wallet_pnl](./2026-04-24-tag-wallet-pnl-design.md)
**Downstream blocker for:** [B2 wallet_tag_fit](./2026-04-24-tag-archetype-fit-design.md) §3.1, §4.2, §4.3
**Status:** READY for `/ship`

## 1. Why

B1's current `win_rate` counts every resolved position regardless of how it
exited — including positions closed entirely via SELL events before
resolution. Sellers cluster on winners (cash out the green, hold the red until
redeem/resolution closes it), so a SELL-heavy wallet's `win_rate` is
inflated by ~5–10pp vs. its true held-to-resolution edge. This is the same
sold-on-winners bias that killed the calibration-mispricing strategy
(brain entry `calibration-analysis-geo-crypto-mispriced-2026-03-31`).

B2's consistency component (Bayesian-shrunk WR) treats `win_rate` as an
unbiased estimator of held-edge. Until that bias is removed, B2 cannot ship.

## 2. What changes

Two new columns on `wallet_tag_pnl`:

| Column | Type | Meaning |
|--------|------|---------|
| `win_rate_held_only` | `REAL` (nullable) | wins / n over positions where at least one exit event is REDEEM **or** the position was closed by residual-at-resolved-price. Positions whose ONLY exits are SELL events are excluded from numerator and denominator. **`NULL` when `n_held_to_resolution = 0`** — distinguishes "no held data" from "0% held WR" (a real outcome for a wallet that held everything and lost every position). |
| `n_held_to_resolution` | `INTEGER NOT NULL` | Denominator of `win_rate_held_only`. Sample size for B2 §4.2. |

`win_rate` and `n_positions` are kept unchanged for backward compatibility
and as the "all resolved exits" view. Downstream B2 reads only the new
fields.

**Sentinel semantics:** `win_rate_held_only IS NULL` ↔ `n_held_to_resolution = 0`.
B2 §5.1 already gates on `n_held_to_resolution >= 3`, but any new consumer
must check `n_held_to_resolution` (or equivalently `win_rate_held_only IS NOT
NULL`) before reading the WR. The `NULL` sentinel makes silent
sentinel-as-data bugs structurally impossible.

## 3. Held-to-resolution rule (precise)

Reuse the existing per-position state in
`sniper/analysis/tag_wallet_pnl.py:reconstruct_positions`. A position is
**held to resolution** iff at least one of:

1. **Residual closed at resolved_price** — the post-events residual size
   exceeded `dust_threshold` and was closed using `market_outcomes` (the
   `if st.size > dust_threshold:` branch at lines ~231–245).
2. **At least one REDEEM activity** was observed for the position.

A position is **NOT held to resolution** iff it was fully closed by SELL
events alone (took the `else` branch at line ~247 with `had_sell_or_redeem=True`
but **only** SELLs, no REDEEMs and no resolved-price residual).

Implementation: split the existing `had_sell_or_redeem: bool` flag and
generalize the defensive exit-check:
- Add `had_redeem: bool` — true iff any REDEEM event was processed.
- Add `closed_at_resolved_price: bool` — true iff the residual branch ran
  for this position.
- **Replace** the existing defensive guard at line ~250 (`if not
  st.had_sell_or_redeem: continue`) with `had_any_exit = had_sell_or_redeem
  or closed_at_resolved_price`. Cleaner intent and avoids a future-bug
  shape where a residual-only close (zero SELLs, zero REDEEMs, residual >
  dust) gets dropped because the old guard never knew about it. The
  current code path can't actually hit that case (residual-close branch
  zeros `st.size` and exits the for-loop body before reaching the
  defensive guard), so this is a clarification, not a behavior change —
  but it documents intent and is forward-safe.

The `Position` dataclass gains a single derived bool `held_to_resolution =
had_redeem or closed_at_resolved_price`, set when the Position is appended.

REDEEM events occurring after a position has been fully sold (rare —
post-resolution redemption sweeps with size 0 or already-zero `st.size`) hit
the `if size <= 0 or st.size <= 0` defensive branch at line ~204 and are
ignored. That's fine: such events do not affect realized PnL and the
`held_to_resolution` flag is only set when an event with non-zero close-size
is processed. Document this in a code comment.

## 4. Aggregation rule

In `compute_wallet_tag_pnl`, after grouping positions by tag:

```python
held_positions = [p for p in plist if p.held_to_resolution]
n_held = len(held_positions)
held_wins = sum(1 for p in held_positions if p.realized_pnl > 0.0)
win_rate_held_only = (held_wins / n_held) if n_held > 0 else None
```

`min_sample` continues to gate on `len(plist)` (all resolved positions), not
on `n_held`. Tags with `n_held = 0` are still emitted with
`win_rate_held_only = NULL` and `n_held_to_resolution = 0`. B2 §5.1 already
imposes `n_held_to_resolution >= 3` as a hard floor for fit_score
computation.

**Note for B2 author:** the held-share ratio `n_held_to_resolution /
n_positions` is a useful derived signal (tells you what fraction of the
wallet's book actually held to resolution vs. flipped). B2 can compute
it in-memory; no new schema field needed.

## 5. Schema migration (additive, idempotent)

`ensure_wallet_tag_pnl_table` extends the `CREATE TABLE` to include the new
columns. For existing deployments, add an `ALTER TABLE … ADD COLUMN`
fallback inside `ensure_wallet_tag_pnl_table` guarded by a `PRAGMA
table_info` check — same pattern as other ensure-helpers in `db.py`.

```sql
ALTER TABLE wallet_tag_pnl ADD COLUMN win_rate_held_only REAL;            -- nullable; NULL == no held data
ALTER TABLE wallet_tag_pnl ADD COLUMN n_held_to_resolution INTEGER NOT NULL DEFAULT 0;
```

`win_rate_held_only` is nullable (no `DEFAULT`, no `NOT NULL`). Pre-patch
rows land with `NULL` until the next CLI run repopulates them.
`n_held_to_resolution` is `NOT NULL DEFAULT 0` (count column; sentinel `0`
means "not yet computed or zero held positions"; combine with the
`win_rate_held_only IS NULL` check to disambiguate).

A one-shot full refresh (`scripts/build_wallet_tag_pnl.py --all`) is
acceptable; no row-level backfill script needed.

## 6. Module + CLI changes

**`sniper/analysis/tag_wallet_pnl.py`**
- `Position` dataclass: add `held_to_resolution: bool`.
- `_State` dataclass: add `had_redeem: bool = False`, `closed_at_resolved_price: bool = False`.
- `reconstruct_positions`:
  - In the `elif atype in ("SELL", "REDEEM"):` branch, also set
    `st.had_redeem = True` when `atype == "REDEEM"`.
  - In the residual-close branch, set `st.closed_at_resolved_price = True`.
  - When constructing `Position`, set
    `held_to_resolution = st.had_redeem or st.closed_at_resolved_price`.
- `TagPnlRow`: add `win_rate_held_only: float | None`, `n_held_to_resolution: int`.
- `compute_wallet_tag_pnl`: compute new fields per §4.
- `ensure_wallet_tag_pnl_table`: schema migration per §5.
- `upsert_wallet_tag_pnl`: include new columns in the INSERT/UPDATE.

**`scripts/build_wallet_tag_pnl.py`** — no CLI flag changes required. The
`--all` run repopulates all rows with the new columns.

**`db.py`** — `wallet_tag_pnl_rows` SELECT must include
`win_rate_held_only, n_held_to_resolution`. Update return-row mapping.

**`index/schema_registry.json`** — regenerate via `python3
scripts/brain_index.py generate` after migration runs once locally.

## 7. Tests (`tests/test_tag_wallet_pnl.py` extension)

Add to the existing test file. Existing tests must remain green.

### 7.1 Held-to-resolution flag coverage
- **Pure-SELL exit** → `held_to_resolution = False`, position counted in
  `n_positions` but NOT in `n_held_to_resolution`.
- **REDEEM-only exit** → `held_to_resolution = True`.
- **SELL then REDEEM** → `held_to_resolution = True` (REDEEM observed).
- **BUY then partial-SELL then resolved-price residual close** →
  `held_to_resolution = True` (residual branch ran).
- **All BUYs, no SELL/REDEEM, residual closed at resolved_price** →
  `held_to_resolution = True`.

### 7.2 Aggregation correctness
- Fixture: 5 resolved positions on tag `t1` — 3 held-to-resolution (2 wins, 1
  loss), 2 pure-SELL (both wins). Expected:
  - `n_positions = 5`
  - `win_rate = 4/5 = 0.8` (sold-side biased)
  - `n_held_to_resolution = 3`
  - `win_rate_held_only = 2/3 ≈ 0.667`
- Sentinel: tag with all positions pure-SELL → `n_held_to_resolution = 0`,
  `win_rate_held_only IS NULL`, row still emitted (gated by `min_sample` on
  `n_positions`, not `n_held`).
- True 0% held WR (held everything, lost everything): `n_held_to_resolution
  >= 1`, `win_rate_held_only = 0.0` (NOT NULL — distinguishable from the
  no-data sentinel above).

### 7.3 Persistence + reader
- `upsert_wallet_tag_pnl` writes new columns; `db.wallet_tag_pnl_rows`
  returns them.
- Re-running on existing DB (with old schema) triggers the ALTER and
  repopulates correctly.

### 7.4 Backward compatibility
- Existing assertions on `win_rate` and `n_positions` unchanged. The bias
  IS preserved in `win_rate` — that's the point. Only new field is unbiased.

## 8. Acceptance criteria

- [ ] `Position` and `TagPnlRow` extended; `reconstruct_positions` and
  `compute_wallet_tag_pnl` emit `held_to_resolution` and the two new
  aggregate fields.
- [ ] Schema migration idempotent — running `ensure_wallet_tag_pnl_table`
  on a pre-patch DB adds the columns; running on a post-patch DB is a
  no-op.
- [ ] `db.wallet_tag_pnl_rows` returns the new columns; brain_index
  schema lookup shows them.
- [ ] All §7 tests pass; existing `tests/test_tag_wallet_pnl.py` tests
  remain green; full suite `python3 -m pytest tests/ -q` stays green.
- [ ] Schema validator clean: `python3 scripts/validate_schema_refs.py`.
- [ ] CLI `python3 scripts/build_wallet_tag_pnl.py --all` runs clean against
  the live SQLite DB; spot-check confirms `n_held_to_resolution <=
  n_positions` and `win_rate_held_only != win_rate` for at least one row
  with mixed exits.
- [ ] Brain index regenerated: `python3 scripts/brain_index.py generate`.

## 9. Out of scope

- Recomputing B2 fit scores (B2 ships separately after this patch lands).
- Held-only versions of `realized_pnl`, `avg_hold_days`, or
  `conviction_concentration`. Only WR is biased by sold-on-winners; the
  others are amount-weighted, not count-weighted, and dollar-PnL is
  unaffected by the path taken to realize it. Spec author can add held-only
  variants later if a downstream consumer demonstrates need.
- Backfill script. The `--all` CLI run is sufficient.

## 10. Risks

- **Position-level edge cases:** if a wallet has zero REDEEM events and
  zero residual closes (pure day-trader who always sells before
  resolution), `n_held_to_resolution = 0` for every tag. B2 §5.1 already
  filters `n_held_to_resolution >= 3`, so the wallet drops out of fit
  scoring — correct behavior (we have no held-edge evidence for them).
- **Polymarket post-resolution redemption sweeps:** described in §3 — the
  defensive `if size <= 0 or st.size <= 0` filter at line ~204 already
  ignores these. Verified by reading existing code before writing this
  spec.
