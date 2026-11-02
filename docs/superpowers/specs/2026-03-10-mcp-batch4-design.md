# MCP Batch 4: Write Tools + Batch 3 Fixes

## Summary

Batch 4 adds MCP write tools for strategy creation/activation, fixes two open
Batch 3 issues (multi-outcome collapse in `get_market_context()` and missing
`freshness_seconds` across 7 tools), and introduces a shared strategy service
layer so validation logic is not duplicated between Flask routes and MCP tools.

## Phase 1: Fix Batch 3 Remnants

### 1a. `get_market_context()` multi-outcome fix

**Problem:** `market_context()` in `mcp_tools/read_tools.py` fetches all
convergence signals, filters by `condition_id`, then takes `convergence[0]` —
silently dropping other outcomes. A market with YES-side and NO-side wallets
reports only the first outcome found.

**Fix:**
- Stop fetching all convergence signals. Query `position_signals` directly for
  the requested `condition_id` with `is_open=1`, grouped by outcome.
- Return a list of per-outcome signal summaries instead of a single dict.

**Before:**
```python
"active_signals": {"wallet_count": 5, "avg_insider_score": 78.2, "total_capital": 50000}
```

**After:**
```python
"active_signals": [
    {"outcome": "Yes", "wallet_count": 5, "avg_insider_score": 78.2, "total_capital": 50000.0},
    {"outcome": "No",  "wallet_count": 3, "avg_insider_score": 65.1, "total_capital": 20000.0}
]
```

**Breaking change:** `active_signals` changes from dict to list. Any MCP client
reading this field must handle the new shape. Since the MCP server is new and
has no external consumers yet, this is acceptable.

### 1b. Real `freshness_seconds` for all tools

**Problem:** 7 of 9 MCP tools pass no `last_write_ts` to `make_envelope()`,
resulting in `freshness_seconds: 0.0` — semantically misleading.

**Fix:** Each tool queries the most relevant timestamp from its source table and
passes it to `make_envelope()`.

| Tool | Freshness source |
|------|-----------------|
| `wallet_profile` | `analyzed_ts` from the returned `user_analysis` row |
| `market_context` | `MAX(analyzed_ts)` from `position_signals` for that `condition_id` |
| `paper_performance` | `MAX(closed_at)` from `paper_trades` (parsed to epoch) |
| `followable_leaderboard` | `MAX(analyzed_ts)` from `user_analysis` |
| `signal_history` | `MAX(analyzed_ts)` from the returned result set |
| `analyze_historical_outcomes` | `MAX(closed_at)` from `paper_trades` |
| `sweep_filters` | same as `analyze_historical_outcomes` |

For `market_context` specifically: use the newest of `MAX(position_signals.analyzed_ts)`
for that condition_id as the freshness source. This is more precise than DB file
mtime and reflects when the signal data was last computed.

## Phase 2: Shared Strategy Service Layer

### New file: `strategy_service.py`

A small module that both `routes/api_strategies.py` and `mcp_tools/write_tools.py`
import. Contains only what both callers need.

**Contents:**

```python
import re
import json
import sqlite3
from strategies.registry import list_strategy_plugins

WALLET_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
VALID_STRATEGY_TYPES = {
    "mirror_all", "selective", "signal_only", "fade",
    *(p.plugin_id for p in list_strategy_plugins()),
}
VALID_STRATEGY_STATUSES = {"active", "paused", "stopped"}


def normalize_wallet(wallet: str | None) -> str:
    return str(wallet or "").strip().lower()


def validate_wallet(wallet: str) -> str:
    """Normalize and validate. Returns normalized wallet or raises ValueError."""
    w = normalize_wallet(wallet)
    if not WALLET_RE.fullmatch(w):
        raise ValueError("wallet must be a valid 0x address")
    return w


def validate_strategy_type(strategy_type: str) -> str:
    """Normalize and validate. Returns lowered type or raises ValueError."""
    st = str(strategy_type or "").strip().lower()
    if st not in VALID_STRATEGY_TYPES:
        raise ValueError(f"strategy_type must be one of: {', '.join(sorted(VALID_STRATEGY_TYPES))}")
    return st


def validate_parameters(parameters) -> dict:
    """Ensure parameters is a dict."""
    if isinstance(parameters, dict):
        return parameters
    if isinstance(parameters, str) and parameters:
        try:
            parsed = json.loads(parameters)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return {}


def create_strategy(
    conn: sqlite3.Connection,
    *,
    wallet: str,
    label: str,
    strategy_type: str,
    parameters: dict,
) -> dict:
    """Validate inputs, create strategy in paused state, return row dict.

    Raises ValueError on invalid inputs.
    """
    wallet = validate_wallet(wallet)
    strategy_type = validate_strategy_type(strategy_type)
    parameters = validate_parameters(parameters)
    label = str(label or "").strip()

    from db import insert_strategy
    strategy_id = insert_strategy(
        conn,
        wallet=wallet,
        label=label,
        strategy_type=strategy_type,
        parameters=parameters,
        status="paused",
    )

    row = conn.execute("SELECT * FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
    return dict(row) if row else {"id": strategy_id}


def activate_strategy(
    conn: sqlite3.Connection,
    strategy_id: int,
) -> dict:
    """Activate a paused strategy. Raises ValueError if not paused or not found."""
    row = conn.execute("SELECT * FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
    if not row:
        raise ValueError(f"strategy {strategy_id} not found")
    if row["status"] != "paused":
        raise ValueError(
            f"strategy {strategy_id} status is '{row['status']}', must be 'paused' to activate"
        )

    from db import update_strategy_status
    update_strategy_status(conn, strategy_id, "active")

    row = conn.execute("SELECT * FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
    return dict(row) if row else {"id": strategy_id, "status": "active"}
```

**Migration of `routes/api_strategies.py`:**
- Remove inline `VALID_STRATEGY_TYPES` definition
- Import `validate_wallet`, `validate_strategy_type`, `validate_parameters`,
  `VALID_STRATEGY_TYPES` from `strategy_service`
- Replace inline validation in `api_strategies_create` and `api_strategies_update`
  with calls to the shared validators
- Keep Flask-specific concerns (request parsing, jsonify, HTTP status codes) in
  the route

## Phase 3: MCP Write Tools

### New file: `mcp_tools/write_tools.py`

Two tools, thin adapters over `strategy_service`.

**`create_paper_strategy`:**
- Params: `wallet: str`, `strategy_type: str`, `label: str = ""`, `parameters: str = "{}"`
- Calls `strategy_service.create_strategy()` (always creates paused)
- Returns envelope with strategy row
- Logs to `mcp_audit_log`

**`activate_paper_strategy`:**
- Params: `strategy_id: int`
- Calls `strategy_service.activate_strategy()` (refuses unless paused)
- Returns envelope with updated strategy row
- Logs to `mcp_audit_log`

Both tools catch `ValueError` from the service layer and return it in the
envelope's error field rather than crashing.

### Registration in `sniper_mcp.py`

```python
from mcp_tools.write_tools import register_write_tools
register_write_tools(mcp)
```

## Phase 4: MCP Audit Log

### New table: `mcp_audit_log`

Every MCP write operation gets logged for traceability.

```sql
CREATE TABLE IF NOT EXISTS mcp_audit_log (
    log_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_name TEXT NOT NULL,
    params    TEXT NOT NULL,          -- JSON of input params
    result    TEXT,                   -- JSON of output (truncated if large)
    success   INTEGER NOT NULL,       -- 1 = ok, 0 = error
    error_msg TEXT,
    ts        TEXT NOT NULL           -- ISO 8601 UTC
);
CREATE INDEX IF NOT EXISTS idx_mcp_audit_ts ON mcp_audit_log (ts DESC);
```

DB helper in `db.py`: `insert_mcp_audit_log(conn, tool_name, params, result, success, error_msg)`.

Write tools call this after every create/activate, regardless of success/failure.

## Phase 5: Smoke Test

Verify MCP writes work against the real local DB:

1. Call `create_paper_strategy` with a known insider wallet + `insider_copy_v1`
2. Confirm strategy appears in `strategies` table with status `paused`
3. Call `activate_paper_strategy` with the returned ID
4. Confirm status is now `active`
5. Call `activate_paper_strategy` again — confirm it fails (already active)
6. Verify `mcp_audit_log` has 3 entries

## Out of Scope

- `save_research_note`, `capture_snapshots` — no immediate caller
- Autonomous agent runner — deferred until write tools are proven
- Research session/findings tables — not needed yet
- Generic `update_strategy()` — MCP only needs create + activate

## Files Changed

| File | Change |
|------|--------|
| `strategy_service.py` | **New** — shared validation + create/activate |
| `mcp_tools/write_tools.py` | **New** — 2 MCP write tools |
| `mcp_tools/read_tools.py` | Fix multi-outcome + add freshness to 5 tools |
| `mcp_tools/analyze_tools.py` | Add freshness to 2 tools |
| `routes/api_strategies.py` | Import from strategy_service, remove inline validation |
| `sniper_mcp.py` | Register write_tools |
| `db.py` | Add `mcp_audit_log` table + insert helper |
| `tests/test_mcp_tools.py` | Tests for all changes |
