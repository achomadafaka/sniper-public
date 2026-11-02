# Phase 3B: Copy Viability Scoring + Signal Timestamp Plumbing

**Date:** 2026-03-27
**Status:** Draft
**Scope:** Two independent pieces completing Phase 3

## Piece 1: Signal Timestamp Fixes (3 one-line changes)

### Problem
`strategy_runner.py:1108` uses `int(time.time())` for `signal_ts` in signal_queue inserts. This records when the runner processed the signal, not when the signal was actually generated. For latency measurement, we need the real signal timestamp.

### Changes

**`strategy_runner.py:1108`** — fix signal_ts:
```python
# Before:
signal_ts=int(time.time()),
# After:
signal_ts=signal.analyzed_ts if signal.analyzed_ts > 0 else int(time.time()),
```

**`strategies/insider_copy_v1.py:385`** — add timestamps to entry_meta:
```python
# Before:
entry_meta = {"source_signal_ts": signal.analyzed_ts}
# After:
entry_meta = {
    "source_signal_ts": signal.analyzed_ts,
    "signal_ts": signal.analyzed_ts,
    "entry_ts": int(time.time()),
}
```

### Tests
- Verify signal_queue rows get `signal_ts` from signal, not wall clock
- Verify entry_meta contains `signal_ts` and `entry_ts`

## Piece 2: Copy Viability Scoring

### Data Source
`signal_queue` only. No bootstrap from `paper_trades`. Empty until signals fire — that's correct behavior.

### New File: `copy_viability.py`

```python
def measure_wallet_viability(conn, wallet: str) -> dict:
    """Compute copy viability metrics for a wallet from signal_queue data.

    Returns: {avg_latency_ms, avg_slippage_pct, fill_rate, signal_count, score}
    """
    # Query signal_queue for this wallet's signals (from metadata_json)
    # fill_rate = entered_count / total_count
    # avg_latency_ms = AVG(latency_ms) WHERE status='entered'
    # score = weighted composite (fill_rate * 40 + latency_factor * 30 + volume_factor * 30)

def update_all_viability(conn) -> int:
    """Batch update copy_viability table for all wallets with signal_queue data."""

def get_viability_leaderboard(conn, limit: int = 20) -> list[dict]:
    """Top wallets by viability score, descending."""
```

### New Endpoints in `routes/api_strategies.py`

| Endpoint | Method | Response |
|----------|--------|----------|
| `/api/copy-viability?wallet=0x...` | GET | Single wallet viability dict |
| `/api/copy-viability/leaderboard` | GET | List of wallet viability dicts, sorted by score |

### Tests
1. `measure_wallet_viability` with populated signal_queue data
2. `measure_wallet_viability` with empty data returns zeros
3. `update_all_viability` populates copy_viability table
4. `get_viability_leaderboard` returns sorted results
5. API endpoints return correct JSON

## Files

| File | Change |
|------|--------|
| `strategy_runner.py` | Fix signal_ts (line 1108) |
| `strategies/insider_copy_v1.py` | Add signal_ts + entry_ts to entry_meta (line 385) |
| `copy_viability.py` | New: viability scoring functions |
| `routes/api_strategies.py` | New: /api/copy-viability endpoints |
| `tests/test_copy_viability.py` | New: viability tests |

## Constraints
- signal_queue only — no paper_trades bootstrap
- Backward compatible — existing entry_meta consumers unaffected (additive keys)
- Empty viability scores are valid (score=0, signal_count=0)
