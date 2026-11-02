# Memecoin `(network_id, mint)` keying — hardening design

Date: 2026-07-15
Status: design → plan → implement
Scope owner: memecoin subsystem (funded runner touches this DB — deploy discipline applies)

## Problem

The same `0x` contract address can be deployed on multiple EVM chains (Base / BSC / ETH /
Robinhood-4663). A handful of memecoin tables key coins by `mint` **alone**, with no chain
discriminator column. A lookup / join / dedup on those tables can, in principle, read or write the
wrong chain's row.

This is a **latent** correctness bug, not an active one. Verified against the live DB
(`sniper_tracker.db`, 2026-07-15):

```
SELECT mint, COUNT(DISTINCT network_id) FROM memecoin_events
WHERE network_id NOT IN ('','evm-unknown') GROUP BY mint HAVING COUNT(DISTINCT network_id) > 1;
-- 0 rows
```

**0 mints appear on >1 concrete chain today.** That bounds urgency: this is hardening, done phased
and safe, explicitly protecting the live real-money runner.

## What is already chain-safe (verified — do NOT touch)

The money path already keys by chain everywhere:

- `price_map` is built `(network_id, mint)`-keyed in `memecoin_runner.bracket_pass`
  (`keys = {(p.network_id or 'solana', p.mint) ...}`).
- `memecoin_exits.sweep_exits` / `write_marks` / `_quarantine_spikes` / `quote_for(price_map, mint,
  network_id)` all key by `(chain, mint)`.
- `db_memecoin.resolve_mint_mcap` and `resolve_mint_their_price` already accept `network_id` and
  filter `COALESCE(network_id,'solana')=?` against `memecoin_events` (which has `network_id`).
- All three swap-executor callers pass `network_id`.
- `memecoin_position_marks` (112k rows) has a `chain_id` column; **every** read filters
  `WHERE chain_id=? AND mint=?` (real_runner mcap fallback, api_memecoin chart, api real fallback).
- `memecoin_token_episodes` has `chain_id` (unique `(chain_id, token_address)`).
- `memecoin_entry_snapshots` has a `chain` column and is read only by `upstream_event_id`.

**Enumeration across all discriminator names (`network_id`, `chain_id`, `chain`, `network`):**
the ONLY mint-bearing memecoin tables with no discriminator of any name are exactly four:
`memecoin_quote_ledger`, `memecoin_alert_latency`, `memecoin_exec_latency`, `memecoin_seen_sells`.
(37 other memecoin tables already discriminate chain — leave them alone.)

## Scope — the four ambiguous tables

| Table | Rows | EVM(0x) rows | Verdict |
|---|---|---|---|
| `memecoin_quote_ledger` | ~252,933 | ~55,469 | **MIGRATE** (primary) |
| `memecoin_alert_latency` | 942 | 574 | **MIGRATE** (low tier, cheap, exact backfill) |
| `memecoin_exec_latency` | 36 | 14 | **MIGRATE** (low tier, trivial) |
| `memecoin_seen_sells` | 1,254 | 880 | **SKIP — already chain-safe** |

### Why `memecoin_seen_sells` is skipped (not churned)

Dedup key is `swap_id` (PK, `INSERT OR IGNORE`). A fomo `swap_id` is globally unique per fill on a
specific chain — two chains cannot share one. `mark_sell_classified` counts each sell once by
`swap_id`; `mint` is informational. The only sell-coverage read that filters by mint
(`sell_event_times`) reads `memecoin_events` (which has `network_id`), **not** this table. Adding
`network_id` here changes no result. Documented as safe; no code change.

## Where the ambiguity actually bites

1. **`quote_ledger` writer** — `record_quote_observations` (db_memecoin.py:~1499) receives a
   `(network_id, mint)`-keyed `price_map` but line ~1509 does
   `mint = key[1] if isinstance(key, tuple) ... else key` — it **discards `key[0]` (the chain)**.
   Every quote is written chain-blind.
2. **`quote_ledger` research read** — `memecoin_pattern_study._legacy_quotes` reads
   `WHERE mint=?` and then **hardcodes `item["network_id"] = "solana"`** for every row — actively
   mislabels an EVM mint's legacy quotes as Solana.
3. **`alert_latency` / `exec_latency`** — instrument tables; no chain column, so any future
   mint-join would cross chains. Both have an exact non-mint provenance path to a chain
   (`fomo_event_id → memecoin_events.network_id`; `pending_id → memecoin_pending_entries.network_id`).

## Design

### Convention

`network_id TEXT` nullable. Values are the existing feed convention: `'solana'`, `'8453'` (Base),
`'4663'` (Robinhood), etc. **`NULL` means unknown — never coerce unknown to a default chain.**
`'evm-unknown'` is a distinct known-EVM-but-unattributed sentinel already used elsewhere; the
backfill leaves genuinely unresolvable `0x` rows `NULL` (not `evm-unknown`) so they are counted, not
silently defaulted.

### Phase 1 — `memecoin_quote_ledger` (primary)

- **Schema (idempotent, in `db_memecoin` init/ensure path):** `ALTER TABLE ... ADD COLUMN
  network_id TEXT` guarded by a column-existence check; add covering index
  `idx_mc_quotes_net_mint_ts (network_id, mint, ts)`. Keep the existing `UNIQUE(mint, ts_minute)`
  and `idx_mc_quotes_mint_ts (mint, ts)` untouched — **no table rebuild**, so no long write-lock
  against the live 15 s sweep.
- **Writer:** record `network_id = key[0]` when the key is a `(net, mint)` tuple, else `None`. No
  caller changes (the price_map is already tuple-keyed in production; the bare-mint test/pump path
  correctly yields `NULL`).
- **Read:** `_legacy_quotes` selects `network_id`, uses the stored value, and for legacy `NULL` rows
  infers `'solana'` only for non-`0x` mints (Solana-native, unambiguous) — never coerces a `0x` mint
  to solana. Accepts an optional `network_id` scope filter.

**Residual (documented):** the `UNIQUE(mint, ts_minute)` constraint stays mint-scoped (changing it
needs a full-table rebuild, which the guardrails forbid against the live sweep). If the same mint
ever traded on two chains in the same UTC minute, `INSERT OR IGNORE` would drop the second chain's
minute-bucket. Impossible today (0 cross-chain mints) and observability-only (the ledger is not in
the exit/mark money path — the sweep prices from on-chain / dexscreener directly). Acceptable; noted.

### Phase 2 — `memecoin_alert_latency` + `memecoin_exec_latency` (low tier)

- **Schema (idempotent):** add `network_id TEXT` to each. No new index (tiny, instrument-only).
- **Writers (additive, fail-open):** `record_alert` reads `fields.get("network_id")` (no signature
  change); `record_exec_latency` gains a `network_id=None` kwarg, passed by the real-runner caller
  where the chain is already in scope. Absent → `NULL`.

### Phase 3 — Backfill (one-time ops script, batched, behind a backup)

`scripts/research/memecoin_backfill_network_id.py` — NOT in `init_db` (never run a 252k UPDATE on
every boot). Mirrors the prior-art `memecoin_backfill_evm_unknown.py` (commit b45523e1).

Resolution rule per `NULL`-network row, **unambiguous-only** (like
`resolve_network_from_events`):

- `quote_ledger`: non-`0x` mint → `'solana'`; `0x` mint → `resolve_network_from_events(mint)` (single
  concrete chain) or leave `NULL`.
- `alert_latency`: exact — `fomo_event_id → memecoin_events.network_id`; else `NULL`.
- `exec_latency`: exact — `pending_id → memecoin_pending_entries.network_id`; fallback
  `resolve_network_from_events(mint)` unambiguous; else `NULL`.

Batched (5,000 rows/txn, commit between, real-UTC timing log) so each write-lock is short vs the live
sweep (WAL: readers never block). Reports resolved / left-NULL counts.

### Phase 4 — Verify & deploy

Backup → migrate live DB → run backfill → `brain_index.py generate` → `validate_schema_refs.py` →
focused then broad pytest → funded runner redeploy via `scripts/deploy_memecoin_runtime.sh <sha>`
after the release test gate, with zero pending reservations and a fresh SQLite backup → heartbeat
health check.

## Testing (TDD — failing test first for each behavior)

- Writer preserves `key[0]` on tuple keys; writes `NULL` on bare-mint keys.
- Same `0x` mint on two chains writes two distinguishable ledger rows (net A vs net B).
- Backfill resolver: non-`0x` → solana; unambiguous `0x` → that chain; conflicting `0x` → `NULL`;
  `alert_latency`/`exec_latency` exact-path resolution.
- `_legacy_quotes` no longer hardcodes solana for a `0x` mint.
- Regression: existing memecoin + real-deploy suites stay green.

## Guardrails honored

Real-money DB (backup before mutation; funded path never broken — the ledger is observability-only).
No raw SQL outside `db.py` for app code (backfill is a one-time ops script, consistent with prior
art). `None` ≠ default chain. Real-UTC timestamps, not host clock. Schema validator + brain index
regenerated before commit.

## Non-goals

No table rewrites, no `UNIQUE`-constraint rebuilds, no trading/exit/threshold changes. `seen_sells`
is not migrated (justified above). `position_marks` / `token_episodes` / `entry_snapshots` are
already chain-safe.
