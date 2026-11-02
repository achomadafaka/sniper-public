# Memecoin Follow-Sync Design

**Date:** 2026-07-05
**Status:** Spec (approved decisions folded in)
**Depends on:** memecoin copy-mirror (shipped, on main). Adds fomo follow-graph → curated list sync.

## Goal

Keep `memecoin_wallets` in sync with the operator's fomo following graph, without ever losing manual control. Own fomo userId comes from `GET /v2/users/current` (no hardcoding).

## Decisions (operator-approved)

- **Additive + never-delete.** Unfollowing a `follow`-sourced wallet **pauses** it (stops new entries); open positions still exit.
- **Manual wallets are sacrosanct.** Sync never pauses/flips a `manual`-sourced wallet.
- **Manual wins (decision 2).** Manual add-by-handle **upserts** — if the wallet exists as a paused `follow` row, manual add flips it to `source='manual', paused=0`.
- **Preserve mirror-exit (decision 1b).** A paused wallet's open positions must still **mirror-exit** when the trader sells — not only SL/TP. So the sell path processes events from paused wallets that still hold an open position. Buys stay curated-only (paused ⇒ no new entries).
- **Fail-closed.** Reconcile only on a successful, non-empty following fetch and a successful own-id fetch. A fomo error must never pause the whole list.

## Schema

Idempotent (PRAGMA-guarded), mirrors the `exit_reason` migration:
```
memecoin_wallets.source TEXT DEFAULT 'manual'   -- existing 3 rows backfill to 'manual'
```

## DB layer (`db_memecoin`)

- `add_wallet(conn, source_user_id, handle, label="", source="manual")`:
  - `source == "manual"` → **UPSERT**: `INSERT … ON CONFLICT(source_user_id) DO UPDATE SET source='manual', paused=0, handle=excluded.handle` (manual wins + activates).
  - `source == "follow"` → `INSERT OR IGNORE … source='follow'` (never disturbs an existing manual/paused row).
- `open_position_user_ids(conn)` → `{source_user_id}` of `is_open=1` positions (for the mirror-exit sell path).

## Follow-sync service (`sniper/services/memecoin_follow_sync.py`)

`sync_followed(conn, auth, user_fn=None, following_fn=None) -> dict`:
1. `you = user_fn(auth)` (own userId via `/v2/users/current`, defensively parsed). If falsy → `{"skipped": True, "reason": "no_user"}`.
2. `follows = following_fn(auth, you)` → `[{"id","userHandle"}]` (via `/v2/users/{you}/following`, paginated defensively). If empty/error → `{"skipped": True, "reason": "empty_or_error"}` (fail-closed).
3. Reconcile:
   - `F = {id: handle}`; `cur = {w.source_user_id: w for w in list_wallets(conn)}`.
   - For each `(uid, handle)` in F: absent → `add_wallet(uid, handle, source='follow')` (added++); present & `source='follow'` & paused → `set_wallet_paused(uid, False)` (unpaused++); `source='manual'` → leave.
   - For each `source='follow'` wallet not in F and not already paused → `set_wallet_paused(uid, True)` (paused++).
   - Return `{"added","unpaused","paused","followed": len(F)}`.

`user_fn`/`following_fn` injectable for tests (no network).

## Engine (`memecoin_runner.run_once`) — decision 1b

Replace the blanket `filter_curated(events, curated)` with side-aware filtering:
- `buy` event processed only if `source_user_id ∈ curated_user_ids` (paused ⇒ no new entry).
- `sell` event processed if `source_user_id ∈ curated_user_ids ∪ open_position_user_ids` (a paused wallet's open position still mirror-exits).

## Cadence

- Runner: `follow_sync_pass(conn, auth)` on a ~5min timer (own `last_sync`), inside the loop's `try/except` — a sync error can't kill the loop or the bracket pass.
- UI button: `POST /api/memecoin/sync-follows` → `sync_followed(conn, FomoAuth())` → counts; toast "added N · unpaused U · paused M".

## UI

- Source badge per wallet: `⭑ followed` / `✎ manual` (from `list_wallets` `source`).
- "Sync from fomo" button in the Curated-traders header → posts, toasts the counts, reloads wallets.

## Tests (`tests/test_memecoin_follow_sync.py`)

- Migration adds `source`, existing rows = manual.
- `add_wallet` manual upsert flips a paused follow row to manual+active; follow add is INSERT-OR-IGNORE.
- `sync_followed`: new follow added active; unfollowed follow → paused; manual wallet untouched by both add+unfollow; re-follow unpauses; empty/no-user fetch → skipped (fail-closed, nothing paused).
- `run_once`: paused wallet with an open position still mirror-exits on its sell; paused wallet does NOT open a new position on a buy.
- Route: `/api/memecoin/sync-follows` returns counts (injected fns).

Verify: `python3 -m pytest tests/test_memecoin_*.py -q` + `python3 -c "import sniper.web.server"` + live browser.
