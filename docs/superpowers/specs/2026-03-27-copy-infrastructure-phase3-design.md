# Phase 3: Copy Infrastructure — WS Reconciliation Design

**Date:** 2026-03-27
**Status:** Implemented (4 commits: b0c395a, 477815f, b681991, e499f57)
**Scope:** Phase 3A only — WS reconciliation. Phase 3B (copy viability, signal_ts plumbing) is a separate spec.
**Context:** Multi-engine strategy platform, Phase 3 of 7

## Problem

The Polymarket WS stream sees trades in real-time but lacks transaction hashes. The `followed_wallet_activity` table uses a composite key that includes `transaction_hash`. We need real-time detection without REST lookups, then later reconciliation when sync fetches the real tx hash.

## Design

### Synthetic WS Transaction Hash

**Two-step process:**

1. `generate_ws_synthetic_hash(wallet, condition_id, outcome, side, activity_type, raw_timestamp_ms, price, size)` — computes `ws:<sha1(...)>` using the **raw millisecond timestamp** directly. This bypasses `_coerce_history_ts()` which collapses to seconds. Millisecond precision prevents collisions in burst trades.

2. The resulting `ws:...` string is then passed as the `transaction_hash` argument to `_followed_wallet_activity_key(wallet, synthetic_hash, condition_id, ...)`. The key function uses its own second-resolution `activity_ts` (from `_coerce_history_ts()`) for the remaining key fields. The sub-second precision lives only inside the synthetic hash, not in the activity_key's other components.

### Detection Helper

`is_synthetic_ws_hash(tx_hash) -> bool` — checks `startswith("ws:")`

### Rejection at Write Time

WS events with `price <= 0` or `size <= 0` are rejected before insert. No zero-price synthetic rows enter the table.

### Activity Key Flow

- WS rows: `activity_key = _followed_wallet_activity_key(wallet, synthetic_hash, ...)`
- Canonical rows: `activity_key = _followed_wallet_activity_key(wallet, real_hash, ...)`
- The two keys will always differ for the same trade (different tx_hash input). Reconciliation bridges this gap.

### Reconciliation (in `upsert_followed_wallet_activity`)

**Architecture change:** The existing `executemany` batch path is retained for WS rows (all synthetic hashes — fast, no reads needed). Real-hash rows are processed in a per-row read-modify-write loop within the same transaction. This is necessary because reconciliation requires SELECT queries before deciding INSERT vs UPDATE vs DELETE.

When an incoming row has a real `transactionHash`:

1. Compute canonical `activity_key` from real tx hash
2. If canonical row already exists → upsert into it normally (idempotent). Then separately query for synthetic rows matching step-3 criteria. If exactly one synthetic candidate matches, delete it. If multiple match, log warning `ambiguous_followed_wallet_activity_reconcile_cleanup` and leave them (same ambiguity rule as step 5).
3. If canonical row does NOT exist → search for a reconcilable synthetic candidate:
   - `transaction_hash LIKE 'ws:%'`
   - Same wallet, condition_id, normalized outcome, side, activity_type
   - `activity_ts` within +/-2 seconds
   - `round(price, 6)` match
   - `round(size, 6)` match
   - `usdc_size` comparison: only if BOTH rows have `usdc_size > 0`, require `round(usdc_size, 4)` match. If either is 0, skip this check (symmetric guard).
4. Exactly one match → update that row in place (activity_key, transaction_hash, enrich all fields). Post-merge `activity_ts` is the REST row's value (REST is authoritative, resolves +/-1s rounding).
5. Multiple matches → do NOT auto-merge; insert canonical row as new; log warning `ambiguous_followed_wallet_activity_reconcile`
6. No match → insert normally

### WS Reconnect Dedup

A WS reconnect delivering the same trade event produces the same synthetic hash (deterministic). The `ON CONFLICT(activity_key) DO UPDATE` in the existing upsert handles this — no second synthetic row is created. This is inherent to the key design, not a special case.

### Merge Fields

On successful reconciliation, update: `activity_key`, `transaction_hash`, `title`, `slug`, `event_slug`, `price`, `size`, `usdc_size`, `activity_ts`, `source_endpoint`, `raw_json`

### Reconciliation Index

Add a composite index in the canonical schema path (`db.py:1553`, alongside existing `followed_wallet_activity` indexes — NOT in `db_followed.py`):

```sql
CREATE INDEX IF NOT EXISTS idx_fwa_reconcile
    ON followed_wallet_activity(wallet, condition_id, outcome, activity_type, side, activity_ts);
```

Includes `outcome` so the candidate lookup does not scan YES/NO siblings in the same market. The `transaction_hash LIKE 'ws:%'` predicate is a post-filter on the narrowed set (acceptable at current scale).

### Streamer Responsibility

The streamer only: detects, normalizes, writes fast. No REST lookups, no reconciliation logic. DB layer owns merge.

## Files (Phase 3A only)

| File | Change |
|------|--------|
| `db_utils.py` | `generate_ws_synthetic_hash()`, `is_synthetic_ws_hash()` |
| `db_followed.py` | Reconciliation logic in `upsert_followed_wallet_activity()` |
| `db.py` | Add `idx_fwa_reconcile` index in canonical schema (line ~1553) |
| `stream_low_buys.py` | Followed-wallet detection in WS handler, writes to `followed_wallet_activity` via `db_followed.py` |

**NOT in this spec (Phase 3B — separate):**
- `strategies/insider_copy_v1.py` (signal_ts metadata)
- `strategy_runner.py` (signal_ts fix)
- `copy_viability.py` (viability scoring)
- `routes/api_strategies.py` (copy-viability endpoints)

## Tests

1. WS synthetic row inserts with `ws:` tx hash
2. Later real-hash sync row merges, leaving exactly 1 row
3. After merge, stored activity_key is canonical real-hash key (not synthetic)
4. Second real-hash upsert is idempotent
5. No-match real-hash row inserts as new row
6. Ambiguous synthetic candidates do not auto-merge
7. Existing pure real-hash behavior unchanged
8. WS reconnect duplicate (same trade again) does not create second synthetic row
9. Zero-price/zero-size WS events rejected before insert
10. Canonical row already exists + one synthetic duplicate → synthetic deleted after cleanup
11. Canonical row already exists + multiple synthetic duplicates → none deleted, warning logged

## Constraints

- No REST lookup in WS path
- `low_buy_trades` semantics unchanged
- `followed_wallet_activity` remains canonical sink
- Additive and backward-compatible
- Strict matching preferred over aggressive matching
- `executemany` retained for WS batch inserts; per-row loop for real-hash reconciliation
