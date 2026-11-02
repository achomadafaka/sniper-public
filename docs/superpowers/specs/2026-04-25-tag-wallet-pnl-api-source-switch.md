---
memory_name: spec/tag-wallet-pnl-api-source-switch-2026-04-25
memory_type: reference
memory_description: B1 source switch — replace reconstruct_positions FIFO accounting with Polymarket /closed-positions API. Eliminates 3 reconstruction bugs (outcome casing, REDEEM-empty-outcome, MERGE/SPLIT/CONVERSION ignored) by trusting Polymarket's settled realizedPnl. Build → buy.
memory_tags: [spec, tag-analytics, wallet, pnl, b1, source-switch, api, ctf]
---

# B1 source switch — `/closed-positions` API replaces FIFO reconstruction

**Date:** 2026-04-25
**Parent spec:** [B1 tag_wallet_pnl](./2026-04-24-tag-wallet-pnl-design.md) (now superseded by this for the source-of-truth question)
**Supersedes (in part):** [B1 held-only patch](./2026-04-25-tag-wallet-pnl-held-only-patch.md) — `held_to_resolution` is now derived from API, not from event-level state machine
**Triggered by:** brain finding `finding/tag-wallet-pnl-reconstruction-bugs-2026-04-25` (3 reconstruction bugs on real data)
**Status:** READY for `/ship`

## 1. Why

B1's `reconstruct_positions` rebuilds Polymarket's CTF (Conditional Token Framework) accounting from raw `followed_wallet_activity` events. This was a build-not-buy choice, and on real data it has three correctness bugs:

1. **Outcome casing mismatch** — `followed_wallet_activity.outcome` uses display casing ('Yes', 'Celtics'), `market_outcomes.outcome` uses lowercase. Cross-table join misses on every held position. Drops them silently.
2. **REDEEM with empty outcome** — production REDEEM rows use `outcome=''`, `price=0`, `size=N` (the Polymarket per-cid sweep shape). Current per-`(cid, outcome)` state-keying puts them in an empty bucket; the `st.size <= 0` defensive guard ignores them. `had_redeem` is never set; 473 events silently lost.
3. **MERGE/SPLIT/CONVERSION ignored** — these CTF mechanics adjust share counts (USDC ↔ matched outcome-token pairs). 11 of 21 wallets (52%) have at least one such event; our highest-volume wallets have 36–54 MERGE events. Token counts are mathematically wrong (e.g. wallet `0x7b02b2bac2a3` shows net YES held = -25,278 from BUY/SELL alone — physically impossible).

Polymarket's Data API endpoint `/closed-positions` returns per-position settled `realizedPnl` directly, with CTF accounting handled by their backend. **We already use this endpoint elsewhere in the codebase** (`polymarket_api.py:777-811`, called from `sync.py:142-143`). Switching B1's source to the API:

- Eliminates all three reconstruction bugs.
- Removes the open-ended scope of reimplementing CTF semantics for 8 activity types.
- Keeps the same schema and downstream contract (`wallet_tag_pnl` columns unchanged).
- Reduces module size by ~250 lines.

Trade-offs accepted:

- We trust Polymarket's accounting. If their API has bugs we inherit them. (Counter: their accounting is the on-chain settlement value; if it's wrong, the wallet's actual PnL is wrong.)
- We make a network call per wallet during backfill. Already mitigated via `USER_POSITIONS_CACHE_TTL_SECONDS` cache and existing pagination.
- We lose per-event introspection. Counter: the spec's non-goals already exclude per-event analytics.

## 2. Non-goals

- Not changing the `wallet_tag_pnl` table schema (columns stay the same).
- Not changing B2's contract (`wallet_tag_fit` consumes `wallet_tag_pnl` exactly as before).
- Not adding fee/slip accounting (still out of scope per parent B1 spec).
- Not deprecating `followed_wallet_activity` ingestion (other consumers still use it; this is a source switch for B1 only).
- Not retroactively backfilling open positions (still PnL = unrealized; only `/closed-positions` feeds B1).

## 3. Data source

`polymarket_api.fetch_user_positions(session, wallet, endpoint='closed-positions')` returns a list of dicts with these fields (sampled 2026-04-25 against wallet `0x1cc16713196d456f86fa9c7387dd326a7f73b8df`):

| Field | Type | Use |
|-------|------|-----|
| `proxyWallet` | str (0x…) | wallet address (== `user` query param) |
| `conditionId` | str (0x…) | join key for `market_tags` and `market_outcomes` |
| `outcome` | str | outcome label ('Yes', 'No', 'Celtics', 'Carlos Ulberg', …) |
| `outcomeIndex` | int | 0 or 1 for binary; not used here |
| `realizedPnl` | float (USDC) | **the settled P&L for this position** — primary input to aggregation |
| `curPrice` | float [0, 1] | resolution-time price; `∈ {0.0, 1.0}` ⇔ held to resolution |
| `avgPrice` | float [0, 1] | volume-weighted entry price |
| `totalBought` | float (USDC) | gross USDC flow into position (per ADR-004, inflated; only for diagnostics) |
| `timestamp` | int (unix epoch) | last activity timestamp on this position (used for B2 recency) |
| `endDate` | str (ISO 8601) | market resolution deadline (informational only) |
| `slug`, `eventSlug`, `title`, `icon` | str | metadata; not consumed by B1 |
| `asset`, `oppositeAsset`, `oppositeOutcome` | str | CTF token IDs; not consumed by B1 |

We continue to use `market_tags` for primary-tag attribution (alphabetically-first slug per condition_id, else `unclassified`) — same as the original B1 spec §5.

## 4. Per-position logic

For each wallet, fetch `/closed-positions` and treat each row as one resolved Position. No FIFO reconstruction.

```python
@dataclass
class Position:
    wallet: str
    condition_id: str
    outcome: str
    realized_pnl: float          # api: realizedPnl
    avg_entry_price: float       # api: avgPrice
    total_bought_usdc: float     # api: totalBought (diagnostic only)
    cur_price: float | None      # api: curPrice (defensive None handling)
    last_ts: int                 # api: timestamp
    held_to_resolution: bool     # derived — see §4.1
```

### 4.1 `held_to_resolution` derivation (CRITICAL — market-state vs wallet-action)

**Naïve rule (REJECTED):** `held_to_resolution = (cur_price == 0.0) or (cur_price == 1.0)`. This is wrong because `curPrice` reflects MARKET resolution state, not WALLET action. A wallet that fully SELL-exited before resolution on a market that LATER resolved to {0,1} will have `curPrice ∈ {0,1}` in the API response and be falsely classified as held — re-introducing the exact sold-on-winners contamination the held-only patch was built to remove. Per architect review 2026-04-25, this would silently undo B2 §4.3's bias correction.

**Correct rule (LOCKED):**

```
held_to_resolution = (cur_price is not None)
                  AND (cur_price <= EPS OR cur_price >= 1.0 - EPS)
                  AND NOT (had_any_sell AND NOT had_any_redeem)
```

where:
- `EPS = 0.01` — matches `sync.py`'s `position_settlement_price_eps`. Defends against float rounding (e.g. `cur_price = 0.9998` on a market that resolved YES).
- `had_any_sell` = at least one row in `followed_wallet_activity` for `(wallet, cid)` with `activity_type='TRADE' AND side='SELL'`.
- `had_any_redeem` = at least one row in `followed_wallet_activity` for `(wallet, cid)` with `activity_type='REDEEM' AND size > 0`.

**Decision tree** (for documentation):
- only BUYs (no SELL, no REDEEM) → `held = True` if `cur_price` near {0,1}; else `False`.
- BUYs + REDEEM (any SELLs or not) → `held = True` (REDEEM is proof-of-redemption regardless of partial sells).
- BUYs + SELLs (no REDEEM) → `held = False` (sold-before-resolution; market may have later resolved but the WALLET exited).
- no BUYs at all → position should not appear (defensive: `held = False`).

This requires one new helper `_fetch_sell_and_redeem_cids(conn, wallet) -> tuple[set[cid], set[cid]]` (per-call, cached for the wallet's `compute_wallet_tag_pnl` invocation).

### 4.2 `avg_hold_days` — column is dropped

The original B1 spec computed `avg_hold_days` from raw activity timestamps. With the API source, the only available timestamp is `position.timestamp` (last activity per Polymarket's accounting), which conflates the close timestamp with later non-BUY activity (REWARD, YIELD).

Cross-source computation `(last_api_ts - first_buy_ts_from_fwa)` is a kludge — `last_api_ts` is not the close time. Architect review confirmed no current downstream consumer reads `avg_hold_days` (`wallet_tag_fit.py` doesn't reference it; `brain_index.py deps --table wallet_tag_pnl` shows zero readers). **Drop the column** via `ALTER TABLE wallet_tag_pnl DROP COLUMN avg_hold_days` (SQLite ≥ 3.35) inside `ensure_wallet_tag_pnl_table`'s migration block. Idempotent. Update `TagPnlRow` dataclass and `upsert_wallet_tag_pnl` to remove the field. Add to brain index regen step.

If a future consumer needs hold duration, derive from `followed_wallet_activity` directly with proper close-time semantics in a follow-up.

## 5. Tag assignment (unchanged)

Same as parent B1 spec §5: alphabetically-first slug from `market_tags` for the cid, else `unclassified`. No change.

## 6. Aggregation per (wallet, tag) — B1 patch fields preserved

For each wallet:

```
positions = fetch_user_positions(wallet, endpoint='closed-positions')
for each position p, attach primary_tag(p.condition_id)
group by primary_tag:
    if len(group) < MIN_SAMPLE: skip
    n_positions            = len(group)
    realized_pnl           = sum(p.realized_pnl for p in group)
    wins                   = count(p where p.realized_pnl > 0)
    win_rate               = wins / n_positions
    held_group             = [p for p in group if p.held_to_resolution]
    n_held_to_resolution   = len(held_group)
    held_wins              = count(p in held_group where p.realized_pnl > 0)
    win_rate_held_only     = (held_wins / n_held_to_resolution) if n_held_to_resolution > 0 else None
    conviction_concentration = sum(|p.realized_pnl| for p in group) / sum(|p.realized_pnl| across all of wallet's groups)
    computed_at            = now()
```

Schema goes from 10 columns to 9 (`avg_hold_days` dropped per §4.2). PK + indexes unchanged. B2 schema unaffected.

## 7. Module changes

### 7.1 `polymarket_api.py` — typed wrapper added

Per architect review: network calls belong in `polymarket_api.py` (module convention), not in the analytics layer.

**Add:**
- `fetch_closed_positions(session, wallet) -> list[dict]` — thin typed wrapper around `fetch_user_positions(session, wallet, endpoint='closed-positions')`. Asserts `row['proxyWallet'].lower() == wallet.lower()` for each returned row; emits WARN via `logger.warning` on mismatch (catches future Safe-multisig delegation cases). Does NOT cast to `Position` — keeps the API layer dict-typed.

### 7.2 `sniper/analysis/tag_wallet_pnl.py`

**Replace (delete):**
- `_fetch_activities` — no longer used for position reconstruction (still kept inline if needed for §4.1's sell/redeem cid lookup; OR replace with the dedicated helper below).
- `_fetch_market_outcomes` — no longer needed.
- `reconstruct_positions` and the nested `_State` dataclass — no longer needed.
- The held-only flag logic (`had_redeem`, `closed_at_resolved_price`, `had_sell_or_redeem`).
- The module docstring (lines 1–11) referencing "FIFO/proportional cost-basis accounting" — REWRITE to describe the API-sourced shape.
- `Position` field deletions: `entry_price`, `exit_price`, `size`, `hold_days`, `first_ts`, `side`.
- `TagPnlRow.avg_hold_days` field — drop entirely per §4.2.

**Add:**
- `_fetch_sell_and_redeem_cids(conn, wallet) -> tuple[set[str], set[str]]` — single SQL query returning `(sell_cids, redeem_cids)` for the wallet from `followed_wallet_activity`. Used by §4.1 held_to_resolution rule.
- `Position` field additions: `avg_entry_price: float`, `total_bought_usdc: float`, `cur_price: float | None`, `last_ts: int`. `held_to_resolution: bool` retained, derivation per §4.1.
- A new orchestrator helper that takes a `requests.Session` (default: `requests.Session()`) and the SQLite conn, calls `polymarket_api.fetch_closed_positions`, then aggregates. `compute_wallet_tag_pnl` signature gains an optional `session` parameter; default behavior creates one transparently.

**Keep (unchanged):**
- `_primary_tag_for_cids` — unchanged.
- `compute_wallet_tag_pnl` body — simplified to consume API rows, attach tag, derive held flag per §4.1, aggregate per §6.
- `ensure_wallet_tag_pnl_table` — adds the `DROP COLUMN avg_hold_days` migration; otherwise unchanged.
- `upsert_wallet_tag_pnl` — drops the `avg_hold_days` column from the INSERT.

### 7.3 `scripts/build_wallet_tag_pnl.py`

No CLI flag changes (decision: drop the proposed `--http-cache-ttl` flag — adds surface area without a stated operator need; per architect feedback). Adds one env var read: `POLYMARKET_API_TIMEOUT_SECONDS` (default 30, already exists in `polymarket_api.py`'s session config).

### 7.4 `db.py`

- `wallet_tag_pnl_rows` reader — drop `avg_hold_days` from the SELECT and the row dict mapping.
- `init_db` DDL for `wallet_tag_pnl` (lines ~2079–2102): drop `avg_hold_days` column from the `CREATE TABLE`. Comment-mark removal date.

### 7.5 `index/schema_registry.json`

Regen required (`avg_hold_days` removed). One commit: `chore(index): regen after avg_hold_days drop`.

## 8. Tests (`tests/test_tag_wallet_pnl.py`)

**Hard rule (new, per `finding/tag-wallet-pnl-reconstruction-bugs-2026-04-25`):**

> Test fixtures MUST cover production data shape across all data sources the module joins. For B1: a mock of `/closed-positions` API response (the position itself) AND `followed_wallet_activity` rows used to compute `had_any_sell` / `had_any_redeem` for the §4.1 cross-source rule. Cross-source casing, empty fields, and missing rows MUST appear in at least one fixture.

### 8.1 Replace existing `reconstruct_positions` tests

The existing 23 tests under `tests/test_tag_wallet_pnl.py` all use raw activity fixtures fed into `reconstruct_positions`. Most lose meaning when reconstruction is removed.

**Strategy:** delete reconstruction-internal tests, keep tag-aggregation tests (with rewritten Position-shape fixtures). Counts pinned in §8.3.

### 8.2 New tests

- **API fixture round-trip:** mock `fetch_closed_positions` to return a list of dicts mirroring real API shape (pulled from a real wallet sample, sanitized, committed under `tests/fixtures/polymarket_closed_positions_sample.json`). Run `compute_wallet_tag_pnl` end-to-end. Assert correct aggregation.
- **`held_to_resolution` derivation — naïve cases:** `curPrice == 1.0`, no SELLs in fwa → True; `curPrice == 0.0`, no SELLs → True; `curPrice == 0.685` → False; `curPrice is None` → False (defensive).
- **`held_to_resolution` derivation — SOLD-BEFORE-RESOLUTION trap:** `curPrice == 1.0` (market resolved YES) BUT wallet has SELL events for (wallet, cid) AND zero REDEEMs → **held = False**. This is the regression-prevention test for the architect's blocking finding. Without the cross-source check, this would falsely return True.
- **`held_to_resolution` derivation — SELL + REDEEM:** `curPrice == 1.0`, wallet has both SELL and REDEEM events → **held = True** (REDEEM is proof-of-redemption regardless of partial sells).
- **`held_to_resolution` derivation — epsilon:** `curPrice == 0.9998` with no SELLs → True (within EPS=0.01).
- **`win_rate_held_only` NULL sentinel preserved:** all positions sold-before-resolution → `n_held_to_resolution = 0`, `win_rate_held_only = None`.
- **API empty response:** `fetch_closed_positions` returns `[]` → no rows, no crash.
- **API error handling:** `fetch_closed_positions` raises `requests.RequestException` → propagated with `logger.exception`; no partial DB writes.
- **`avg_hold_days` migration:** existing `wallet_tag_pnl` row with the column populated → after `ensure_wallet_tag_pnl_table` runs, the column is removed; existing data preserved on other columns.
- **Reader compat:** `db.wallet_tag_pnl_rows` no longer returns `avg_hold_days`; B2 `wallet_tag_fit.compute_wallet_tag_fit` does not crash on the missing key (it doesn't read `avg_hold_days` per §0 verification).
- **`proxyWallet` mismatch WARN:** mock API returns a row with `proxyWallet` differing from the queried wallet → `fetch_closed_positions` logs a WARN; row still returned (no exception).

### 8.3 Deletion / keep / rewrite list (PINNED — finalize before /ship Phase 1, not during)

Test-writer must walk every existing test in `tests/test_tag_wallet_pnl.py` and assign exactly one of: DELETE, KEEP, REWRITE-FIXTURE. No tests defer.

| Bucket | Pattern | Rationale |
|--------|---------|-----------|
| DELETE | `test_reconstruct_positions_*` (FIFO, dust, state-machine internals) | Module function removed |
| DELETE | `test_held_flag_pure_sell_exit_is_false`, `test_held_flag_redeem_only_exit_is_true`, `test_held_flag_sell_then_redeem_is_true`, `test_held_flag_partial_sell_then_residual_close_is_true`, `test_held_flag_buy_only_residual_close_is_true` | Replaced by §8.2 API-derived held tests |
| DELETE | `test_ensure_table_migrates_legacy_schema_idempotently` (in current shape) | Migration target changed; rewritten under §8.2 with new ALTER (DROP COLUMN avg_hold_days) |
| KEEP | `test_unclassified_tag_assignment` | Tag attribution unchanged |
| KEEP | `test_min_sample_filter` (after fixture rewrite to API rows) | Aggregation logic unchanged |
| KEEP | All `test_upsert_*` and `test_db_wallet_tag_pnl_rows_*` | Persistence + reader unchanged |
| REWRITE-FIXTURE | `test_compute_wallet_tag_pnl_aggregation` and the held-only canonical aggregation test | Logic unchanged but fixture must use Position dict shape, not raw activity events |
| KEEP | `test_backward_compat_win_rate_and_n_positions_unchanged` | Invariant still holds; fixture rewritten |

Test-writer must enumerate all tests and assign a bucket BEFORE writing new tests. If any test resists classification, halt and flag — do not guess.

## 9. Acceptance criteria

- [ ] `reconstruct_positions`, `_State`, `_fetch_activities`, `_fetch_market_outcomes` all removed from `sniper/analysis/tag_wallet_pnl.py`.
- [ ] `polymarket_api.fetch_closed_positions` added with proxyWallet WARN check.
- [ ] `_fetch_sell_and_redeem_cids` helper added in `tag_wallet_pnl.py`.
- [ ] `compute_wallet_tag_pnl` uses the new path; `held_to_resolution` derived per §4.1 cross-source rule (NOT naïve `curPrice ∈ {0,1}`).
- [ ] Schema migration drops `avg_hold_days`; idempotent ALTER; `init_db` DDL updated.
- [ ] `tests/fixtures/polymarket_closed_positions_sample.json` checked in.
- [ ] All §8.2 tests green; full suite (`python3 -m pytest -q`) green.
- [ ] Schema validator clean: `python3 scripts/validate_schema_refs.py`.
- [ ] Brain index regenerated: `python3 scripts/brain_index.py generate`.
- [ ] CLI `python3 scripts/build_wallet_tag_pnl.py --all` runs clean against the live SQLite DB; spot-check confirms wallet `0x1cc16713196d…` shows the `~$264K` Iran-Israel position attributed to the `geopolitics` tag.
- [ ] B2 `python3 scripts/build_wallet_tag_fit.py --all` produces non-zero rows on the new B1 data (population-bound; absolute count not asserted).
- [ ] Brain finding `finding/tag-wallet-pnl-reconstruction-bugs-2026-04-25` set `status='superseded'`, `superseded_by='spec/tag-wallet-pnl-api-source-switch-2026-04-25'`.
- [ ] Brain memory `spec/tag-wallet-pnl-held-only-patch-2026-04-25` set `status='superseded'`, same `superseded_by`.
- [ ] Brain memory `spec/tag-wallet-pnl-api-source-switch-2026-04-25` saved as `status='active'` with `valid_as_of_commit` = post-merge commit hash.

## 10. Risks

- **API rate limits / outages.** Mitigation: existing cache layer + retry logic in `polymarket_api.py`; CLI is idempotent so re-runs are cheap.
- **API field contract drift.** Polymarket may rename `realizedPnl` or change `curPrice` semantics. Mitigation: pin the field names in `fetch_closed_positions` and emit a structured log line on every backfill counting positions by `held_to_resolution` band — so drift becomes visible.
- **Closed-only position scope.** `/closed-positions` does NOT include open (still-resolving) positions. B1 has always been resolved-only by design; this is consistent.
- **Loss of audit trail.** We can no longer reconstruct B1's numbers from raw activity. Mitigation: `inputs_json` style audit field is NOT added to `wallet_tag_pnl` (B2 already has one); but the `/closed-positions` row hash + timestamp can be cached in a separate audit table if downstream demands it later. Out of scope for v1.
- **Population still small.** This source switch fixes correctness, not population. The 21-wallet population in `followed_wallet_activity` remains the universe; broader population is a separate spec.

## 11. What this does NOT solve

The B2 §8 acceptance criterion ("≥10 rows with `confidence_flag='ok'`" requires `peer_count >= 30 per tag`) is **NOT met by this spec.** With ~21 wallets, no tag has 30 qualified peers. Resolving that requires a separate spec to expand B1's wallet universe.

A4 makes the table CORRECT; population growth makes it OPERATIONALLY USABLE. Both are required for `wallet_tag_fit` to drive production decisions.

### 11.1 Follow-up specs (stubbed here, drafted separately)

- **`B1-population-expansion`** — expand B1's wallet universe beyond `followed_wallet_activity` (~21 wallets). Candidates: union with `candidate_users` filtered by minimum trade volume, or a dedicated `discovery → wallet_universe` pipeline. Required before B2 §8 verification can pass. Stub planned for next session.
- **`hold-duration-revival`** (only if a downstream consumer asks) — re-derive a properly-defined hold-duration metric from `followed_wallet_activity` close timestamps if any consumer of `wallet_tag_pnl` requests it. Currently: zero readers (verified via `brain_index.py deps`).
