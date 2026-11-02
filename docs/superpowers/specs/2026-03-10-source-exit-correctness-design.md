# Source Exit Correctness — Design Spec

## Problem

The paper trader conflates "analyzer disqualified the signal" with "source wallet actually sold." When the analyzer reclassifies a wallet (score dropped, bot detected, price > 0.80) it sets `position_signals.is_open = 0`. The paper trader sees `is_open = 0` and closes the paper trade with status `source_exited` — even though the source wallet still holds the exact same position on Polymarket.

**Verified on live VPS data (2026-03-10):** 8 of 59 paper trades (13.6%) were falsely closed as `source_exited`. Four source wallets still hold their positions with significant size ($1,887 to $36,095 notional). Zero sell signals were received for any of them. All closures were triggered by `is_open=0` after analyzer reclassification to `position_low_signal`.

Additionally, `check_sell_signals()` closes on ANY sell trade from the source wallet — including partial reductions — without checking remaining position size.

## Design Principle

**Paper trade stays open as long as the source wallet holds the position.** Analyzer reclassification is metadata, not a close trigger.

## Changes

### 1. `paper_trader.py` — `_check_position_exit()` (lines 834–970)

**Current behavior:** `is_open = 0` → `position_closed_by_source = True` → close trade.

**New behavior:** `is_open = 0` → record disqualification annotation → **keep trade open** → still check actual position via data-api.

Concrete changes at line 876:
```python
# BEFORE:
if signal_row is not None and int(signal_row["is_open"] or 0) == 0:
    position_closed_by_source = True

# AFTER:
if signal_row is not None and int(signal_row["is_open"] or 0) == 0:
    # Analyzer disqualified this signal (score drop, reclassification, etc.)
    # Do NOT close — source wallet may still hold. Record disqualification.
    self._maybe_mark_disqualified(trade_id, "signal_is_open_0")
```

Close with `source_exited` ONLY when:
- Data-api confirms wallet position size < 0.01 (true full exit)
- Market is closed/archived by Polymarket (CLOB `closed` or `archived` flag)

Line 939 remains the close gate but now only fires on confirmed exits:
```python
if source_size is not None and float(source_size) < 0.01:
    # Confirmed: source wallet has fully exited
    ...
```

The `position_closed_by_source` flag from CLOB `closed`/`archived` markets is still valid — those are real market-level events, not analyzer reclassifications.

### 2. `paper_trader.py` — Per-cycle wallet position cache

Add a `_wallet_positions_cache` dict, reset at the start of each `run_once()` cycle. When `_check_position_exit()` or `check_sell_signals()` needs to verify a wallet's actual positions via data-api, check the cache first.

```python
def _fetch_wallet_positions(self, wallet: str) -> list:
    """Fetch wallet positions from data-api, cached per monitoring cycle."""
    if wallet in self._wallet_positions_cache:
        return self._wallet_positions_cache[wallet]
    try:
        r = requests.get(
            'https://data-api.polymarket.com/positions',
            params={'user': wallet, 'sizeThreshold': 0, 'limit': 500},
            timeout=8,
        )
        positions = r.json() if r.ok else []
    except Exception:
        positions = []
    self._wallet_positions_cache[wallet] = positions
    return positions
```

Reset in `run_once()`:
```python
def run_once(self):
    self._wallet_positions_cache = {}
    self.check_sell_signals()
    self.monitor_positions()
    ...
```

### 3. `paper_trader.py` — `check_sell_signals()` (lines 1075–1127)

**Current behavior:** Any sell signal → immediately close paper trade.

**New behavior:** Sell signal received → query data-api for remaining position size (via per-cycle cache) → only close if size < 0.01 → otherwise log partial sell and continue holding.

```python
# After matching sell signal to open trade:
positions = self._fetch_wallet_positions(wallet)
remaining_size = 0.0
for p in positions:
    cid = str(p.get('market', '') or p.get('conditionId', '')).lower()
    if cid == condition_id.lower():
        remaining_size = float(p.get('size', 0))
        break

if remaining_size >= 0.01:
    # Partial sell — source still holds. Do NOT close.
    logger.info(
        f"Partial sell detected for {wallet[:8]}... "
        f"(cond={condition_id[:8]}...) — remaining size={remaining_size:.2f}, keeping open"
    )
    # Mark processed but don't close
    ...
    continue

# Full exit confirmed — close the trade
...
```

### 4. `paper_trader.py` — `_maybe_mark_disqualified()` helper

Records disqualification metadata on paper trades without closing them.

```python
def _maybe_mark_disqualified(self, trade_id: int, reason: str):
    """Record signal disqualification without closing the trade.

    Append-safe: preserves the original disqualified_at timestamp.
    Updates reason only when it actually changes.
    """
    existing = self.conn.execute(
        "SELECT signal_disqualified_at, signal_disqualification_reason FROM paper_trades WHERE trade_id = ?",
        (trade_id,)
    ).fetchone()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    if existing and existing["signal_disqualified_at"]:
        # Already disqualified — update reason only if it changed
        if existing["signal_disqualification_reason"] == reason:
            return
        self.conn.execute(
            "UPDATE paper_trades SET signal_disqualification_reason = ? WHERE trade_id = ?",
            (reason, trade_id),
        )
        self.conn.commit()
        logger.info(f"Signal disqualification reason updated for trade {trade_id}: {reason}")
        return
    self.conn.execute(
        "UPDATE paper_trades SET signal_disqualified_at = ?, signal_disqualification_reason = ? WHERE trade_id = ?",
        (now, reason, trade_id),
    )
    self.conn.commit()
    logger.info(f"Signal disqualified for trade {trade_id}: {reason}")
```

### 5. `db.py` — Schema evolution for new columns

Add `ensure_paper_trades_disqualification_columns()` following the existing pattern (`_ensure_column`), called from `init_db()`.

```python
def ensure_paper_trades_disqualification_columns(conn: sqlite3.Connection) -> None:
    """Ensure disqualification tracking columns exist on paper_trades."""
    with with_db_init_lock(conn):
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_trades' LIMIT 1"
        ).fetchone()
        if table is None:
            return
        _ensure_column(conn, "paper_trades", "signal_disqualified_at", "TEXT DEFAULT NULL")
        _ensure_column(conn, "paper_trades", "signal_disqualification_reason", "TEXT DEFAULT NULL")
        conn.commit()
```

### 6. `_check_position_exit()` — Data-api verification when signal missing

When the signal is not in the pre-fetched live list AND `is_open = 0`, the current code falls through to CLOB and data-api lookups. This flow stays, but:
- CLOB `closed`/`archived` → still sets `position_closed_by_source = True` (real market event)
- Data-api size < 0.01 → still sets `position_closed_by_source = True` (real full exit)
- `is_open = 0` alone → disqualification annotation only, no close

The data-api call should use the new `_fetch_wallet_positions()` cache.

### 7. One-off repair script: `scripts/repair_false_source_exits.py`

Reopens falsely closed trades. Run once after deploy.

**Default mode: dry-run.** Requires explicit `--apply` flag to mutate the DB.

```bash
# Dry-run (default) — shows what would be reopened
python3 scripts/repair_false_source_exits.py

# Apply — actually reopens trades
python3 scripts/repair_false_source_exits.py --apply
```

**Criteria for reopening:**
1. `exit_reason = 'source_exited'` (or `status = 'early_exit'` with `exit_reason = 'source_exited'`)
2. No matching row in `paper_sell_signals` for that `(wallet, condition_id)`
3. Source wallet still holds the position (data-api size > 0.01)
4. Market not resolved/archived (CLOB check)
5. No duplicate open trade for the same `(wallet, condition_id, outcome)`

**Actions (apply mode only):**
- Set `status = 'open'`, clear `exit_price`, `exit_reason`, `closed_at`, `pnl_usdc`
- Set `signal_disqualified_at` and `signal_disqualification_reason` to record the original false closure reason
- Print repair report with affected trade_ids

**Dry-run output:** For each candidate trade, print trade_id, wallet, condition_id, current source size, market status, and whether it would be reopened or skipped (with skip reason).

### 8. Tests

New test file: `tests/test_source_exit_correctness.py`

| Test | Verifies |
|------|----------|
| `test_is_open_zero_does_not_close` | `is_open=0` alone does NOT trigger closure |
| `test_is_open_zero_records_disqualification` | Disqualification metadata is written |
| `test_confirmed_full_exit_closes` | Data-api size < 0.01 DOES close with `source_exited` |
| `test_partial_sell_does_not_close` | Sell signal + remaining size > 0.01 keeps trade open |
| `test_full_sell_closes` | Sell signal + remaining size < 0.01 closes trade |
| `test_market_closed_closes` | CLOB `closed=true` triggers closure |
| `test_wallet_cache_per_cycle` | Cache is hit on second call, reset between cycles |
| `test_disqualification_preserves_first_timestamp` | Second disqualification keeps original `disqualified_at` |
| `test_disqualification_updates_reason_on_change` | New reason updates, same reason is no-op |

### 9. UI — Disqualification badge

On the Proof Bar tab and paper trades display, show a small badge/indicator for open trades that have `signal_disqualified_at` set. Non-blocking, informational only. Something like:

```
Trade #42 | 0xc8ab... | ETH > $5k | Open | ⚠ signal disqualified
```

This is a display-only change — disqualified trades still count as open and will be closed normally when the source actually exits.

## What does NOT change

- Resolution win/loss logic (current_price >= 0.90 or <= 0.05)
- Stale timeout logic (14 days + no activity still valid close)
- Entry gates (all thresholds unchanged)
- Proof bar criteria (only counts truly closed trades)
- `position_signals.is_open` semantics in the analyzer (unchanged — analyzer still sets it for its own purposes)

## Affected files

| File | Change |
|------|--------|
| `paper_trader.py` | Core fix: `_check_position_exit()`, `check_sell_signals()`, new cache + helper |
| `db.py` | `ensure_paper_trades_disqualification_columns()`, called from `init_db()` |
| `scripts/repair_false_source_exits.py` | New one-off repair script |
| `tests/test_source_exit_correctness.py` | New test file, 8+ tests |
| `templates/index.html` | Disqualification badge in paper trades display |
| `static/app.js` | Render disqualification badge |

## Risk assessment

- **Low risk:** Schema change is additive (nullable columns), backward compatible
- **Medium risk:** Data-api calls per cycle could be rate-limited under heavy load; mitigated by per-cycle cache
- **Low risk:** Repair script is idempotent and checks 5 criteria before reopening
- **No risk to proof bar:** Reopened trades resume normal tracking; proof bar only counts closed trades
