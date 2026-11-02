---
memory_name: spec/tag-wallet-pnl-2026-04-24
memory_type: reference
memory_description: B1 spec — per-(wallet, tag) realized-PnL table + pure-function builder + CLI. Required for curation decisions (copy/ignore/derive per tag) and strategy derivation candidate surfacing.
memory_tags: [spec, tag-analytics, wallet, pnl, b1]
---

# B1 — `tag_wallet_pnl` — per-(wallet, tag) realized-PnL matrix

**Date:** 2026-04-24
**Parent plan:** [wallet-intelligence-rearchitecture](../plans/2026-04-24-wallet-intelligence-rearchitecture.md) task B1
**Parent tag-analytics spec:** [2026-04-24-tag-analytics.md §3.2](./2026-04-24-tag-analytics.md)

## 1. Goal

For each (wallet, tag) pair where the wallet has ≥ N resolved positions on that tag, compute realized-PnL + win-rate + hold-duration + concentration. This is the load-bearing primitive for the review-queue dossier and for strategy-derivation candidate selection — "copy their politics fills, skip their sports fills" becomes quantitative.

## 2. Non-goals

- Not predicting future PnL. This is retrospective attribution only.
- Not classifying tags as "good"/"bad". Just the per-tag realized numbers; interpretation is downstream.
- Not computing fees or slippage — realized PnL in this module is fill-to-resolution, not net. Fee adjustment is a separate concern.
- Not persisting sub-tag hierarchies. Tags stay flat opaque strings per migration spec §3.1.

## 3. Data sources

| Table | Use |
|-------|-----|
| `followed_wallet_activity` | Wallet-level buy / sell / redeem events (wallet, condition_id, activity_type, side, outcome, price, size, usdc_size, activity_ts) |
| `market_tags` | condition_id → tag (opaque gamma slugs) |
| `market_outcomes` | (condition_id, outcome) → resolved_price, resolved_at |

Schema-validated 2026-04-24 via `brain_index.py schema`.

## 4. Per-position logic

For each wallet, reconstruct resolved positions:

1. Group `followed_wallet_activity` rows by (wallet, condition_id, outcome).
2. Build FIFO fills: each BUY increments size + cost basis; each SELL decrements with proportional cost-basis release (accumulated realized-PnL += sell_price × size - cost_basis_freed). Each REDEEM at resolution closes the remaining position (accumulated realized-PnL += resolved_price × size - cost_basis_remaining).
3. A position is **resolved** when `market_outcomes` has a row for (condition_id, outcome) AND the wallet's net size is zero or reduced to zero via redemption.
4. Per resolved position we have: `entry_price` (cost_basis / size), `exit_price` (weighted avg of sells + final redemption), `size`, `realized_pnl`, `hold_days` (activity_ts of last buy → resolved_at).

Handle partial sells — compute realized PnL proportionally. Handle NO-side positions — price math is the same (outcome-native per ADR-003); realized PnL is positive when entry_price < exit_price on the side you hold.

## 5. Tag assignment (primary_tag)

Use the existing `_primary_tag_for_cid(condition_id, tag_map)` convention from `wallet_archetype_extractor.py`: **alphabetically first tag slug**, or `"unclassified"` if none. Deterministic, opaque, no hardcoded taxonomies. The tag_wallet_pnl module must not import from or hardcode any specific slug values.

## 6. Aggregation per (wallet, tag)

```
tag_wallet_pnl(wallet, primary_tag):
    positions = [p for p in resolved_positions(wallet) if primary_tag_for(p.condition_id) == primary_tag]
    if len(positions) < MIN_SAMPLE:   # default 3
        skip
    n_positions            = len(positions)
    realized_pnl           = sum(p.realized_pnl for p in positions)
    wins                   = sum(1 for p in positions if p.realized_pnl > 0)
    win_rate               = wins / n_positions
    avg_hold_days          = mean(p.hold_days for p in positions)
    conviction_concentration = (
        sum(abs(p.realized_pnl) for p in positions)
        / sum(abs(p.realized_pnl) for p in all_resolved_positions(wallet))
    )
```

`conviction_concentration ∈ [0, 1]` — fraction of the wallet's absolute realized-PnL carried by this tag. Tells you whether the wallet is tag-specialized or spread thin.

## 7. Persistence

### Table: `wallet_tag_pnl`

```sql
CREATE TABLE IF NOT EXISTS wallet_tag_pnl (
    wallet                    TEXT    NOT NULL,
    tag                       TEXT    NOT NULL,
    n_positions               INTEGER NOT NULL,
    realized_pnl              REAL    NOT NULL,
    win_rate                  REAL    NOT NULL,
    avg_hold_days             REAL    NOT NULL,
    conviction_concentration  REAL    NOT NULL,
    computed_at               INTEGER NOT NULL,
    PRIMARY KEY (wallet, tag)
);
CREATE INDEX IF NOT EXISTS idx_wallet_tag_pnl_wallet ON wallet_tag_pnl (wallet);
CREATE INDEX IF NOT EXISTS idx_wallet_tag_pnl_tag ON wallet_tag_pnl (tag);
```

<!-- schema:validate-columns-for=wallet_tag_pnl -->

Writer upserts on (wallet, tag). `computed_at` is an integer unix epoch for freshness checks.

### Compute pass

`scripts/build_wallet_tag_pnl.py`:
- `--wallet <addr>` — recompute for one wallet
- `--all` — recompute all wallets with ≥ 1 resolved position in followed_wallet_activity
- Idempotent. Writes are transactional per wallet.

## 8. Implementation surface

1. `sniper/analysis/tag_wallet_pnl.py` — pure functions:
   - `reconstruct_positions(conn, wallet) -> list[Position]`
   - `compute_wallet_tag_pnl(conn, wallet, min_sample=3) -> dict[tag, TagPnlRow]`
2. `scripts/build_wallet_tag_pnl.py` — CLI builder (reads → writes `wallet_tag_pnl`).
3. Migration under `migrations/` or `init_db()` bootstrap with schema above.
4. `db.py` additions: `wallet_tag_pnl_rows(conn, wallet=None)` reader. No raw SQL outside `db.py` per CLAUDE.md.
5. `tests/test_tag_wallet_pnl.py`:
   - Happy path: 5 positions across 2 tags → correct aggregation
   - Min-sample filter: 2 positions on a tag → excluded
   - Only-resolved: un-resolved market → excluded
   - Partial sell: proportional realized-PnL
   - Multi-outcome market: non-binary outcome → handled or excluded explicitly (decide in impl)
   - NO-side position: price math correct
   - No activity: empty result, no crash
   - conviction_concentration math: sums to 1.0 across a wallet's reported tags

## 9. Opaque-tag discipline

- No slug values hardcoded anywhere in the module or tests.
- Tests use synthetic tag slugs (`"alpha"`, `"beta"`, `"some-future-slug"`) to enforce this.
- The primary_tag helper is the only tag-resolution surface — no parallel implementation.

## 10. Open questions

1. Multi-outcome markets (basketball team winners, etc.): include in resolved-position set, or skip? Recommend include — realized PnL math still works per outcome-native convention. Add a test.
2. Positions with negative size (shorting via NO side): already handled by outcome-native convention, but confirm via test.
3. Freshness: should `wallet_tag_pnl` auto-invalidate when `followed_wallet_activity` gains new rows? Recommend: batch recompute daily via cron, `computed_at` serves as staleness check. Hot-path consumers (dossier endpoint) check staleness before reading.

## 11. Acceptance criteria

- New table `wallet_tag_pnl` exists + indexed
- Pure builder in `sniper/analysis/tag_wallet_pnl.py` with tests
- CLI writer in `scripts/build_wallet_tag_pnl.py`
- `db.py` reader function
- Test file `tests/test_tag_wallet_pnl.py` with the 7+ cases listed above
- All tests green
- `brain_index.py generate` refresh clean (new table indexed)
- A smoke-run against real DB produces sensible numbers for at least one known wallet (e.g. SwissMiss)

## 12. Next after B1

B2 (`tag_archetype_fit`) depends on this output and is a small follow-on. Then C1 (`wallet_archetypes` table) and C3 (curation CLI) unblock the review-queue workflow.
