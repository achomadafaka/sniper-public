# db.py God Object Refactor — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the 11,342-line `db.py` into 6 focused modules with zero behavior changes — every function keeps its exact signature and remains importable via `from db import X`.

**Architecture:** Pure code-move refactor. Each module is extracted with its internal helpers. `db.py` re-exports everything via `from db_xxx import *` so 40+ production files and 36 test files that do `from db import X` continue working unchanged. No function signatures change. No logic changes. The test suite is the proof: 1444 tests must pass identically before and after.

**Tech Stack:** Python 3.12, SQLite, pytest

**Critical safety rule:** After EVERY task, run `python3 -m pytest tests/ -q`. If any test fails, STOP and revert that task's changes before proceeding.

---

## Pre-Refactor: Dependency Map

### Module → Functions → Line Ranges

| Module | Functions | Lines | Start–End | Internal deps |
|--------|-----------|-------|-----------|---------------|
| `db_utils.py` | 15 helpers | ~250 | Scattered | None (leaf module) |
| `db_snapshots.py` | 28 functions | ~1,018 | 10324–11342 | `_ensure_column`, `_classify_news_domain` |
| `db_leaderboards.py` | 12 functions | ~790 | 3831–4440 + 7952–8620 | `compute_followability_score`, `_followable_clamp`, `_sort_leaderboard_v2`, `_classify_title_domain` |
| `db_signals.py` | 12 functions | ~620 | 4440–5133 + 5133–5460 | `_coerce_float`, `_coerce_int`, `_extract_signal_identity` |
| `db_followed.py` | 36 functions | ~2,640 | 6212–7950 + 8620–9155 | `_normalize_*`, `_coerce_*`, `_followed_wallet_activity_key` |
| `db_replay.py` | 14 functions | ~570 | 9155–9456 + 10105–10324 | `_ensure_column`, `_current_instance_tag` |

### Cross-Module Dependencies (Critical)

```
db_utils.py (LEAF — no imports from other db_* modules)
    ↑ used by all other db_* modules

db_snapshots.py → imports from db_utils (_ensure_column)
db_leaderboards.py → imports from db_utils (_classify_title_domain, _followable_clamp, compute_followability_score)
db_signals.py → imports from db_utils (_coerce_float, _coerce_int, _extract_signal_identity)
db_followed.py → imports from db_utils (_normalize_*, _coerce_*, _coerce_history_ts)
db_replay.py → imports from db_utils (_ensure_column, _current_instance_tag)

db.py (core) → imports and re-exports ALL of the above
```

**Extraction order matters:** `db_utils.py` MUST be extracted first because every other module depends on it.

### Re-Export Strategy

After extraction, `db.py` will contain:
1. Connection management (get_connection, connection_scope, etc.)
2. Schema initialization (init_db, apply_phase_b_hardening, ensure_* columns)
3. Core CRUD (market_metadata, user_analysis, position handling, etc.)
4. Star-imports that re-export everything:

```python
# At the TOP of db.py, after moving functions out:
from db_utils import *      # noqa: F401,F403
from db_snapshots import *   # noqa: F401,F403
from db_leaderboards import *  # noqa: F401,F403
from db_signals import *     # noqa: F401,F403
from db_followed import *    # noqa: F401,F403
from db_replay import *      # noqa: F401,F403
```

This means `from db import wallet_leaderboard_rows` still works even though the function now lives in `db_leaderboards.py`.

---

## Chunk 1: Extract db_utils.py (leaf module, zero risk)

### Task 1: Establish baseline

**Files:** None modified

- [ ] **Step 1: Record exact test baseline**

```bash
python3 -m pytest tests/ -q 2>&1 | tail -3
```

Record the exact count (expected: `1444 passed, 5 subtests passed`). This number must match after every task.

- [ ] **Step 2: Record exact function count**

```bash
grep -c "^def " db.py
```

Record: should be ~211. After full refactor, `db.py` should have ~130 and the 6 modules should have ~81 combined.

- [ ] **Step 3: Verify all imports resolve**

```bash
python3 -c "from db import *; print('OK:', len(dir()))"
```

---

### Task 2: Create db_utils.py with utility helpers

**Files:**
- Create: `db_utils.py`
- Modify: `db.py` — remove moved functions, add `from db_utils import *`

**Functions to move (15 functions, ~250 lines):**

| Function | Current line | Dependencies |
|----------|-------------|--------------|
| `_coerce_int` | 5460 | None |
| `_coerce_float` | 5469 | None |
| `_normalize_wallet` | 6212 | None |
| `_normalize_tx_hash` | 6216 | None |
| `_normalize_condition_id` | 6220 | None |
| `_normalize_outcome` | 6224 | None |
| `_coerce_history_ts` | 6228 | None |
| `_followed_wallet_activity_key` | 6254 | `_normalize_wallet`, `_normalize_tx_hash`, `_normalize_condition_id`, `_normalize_outcome` |
| `_extract_signal_identity` | 5478 | None |
| `_build_execution_key` | 5496 | None |
| `_classify_title_domain` | 9002 | None |
| `_classify_news_domain` | 10480 | None |
| `_followable_clamp` | 8238 | None |
| `_followable_short_wallet` | 8296 | None |
| `compute_followability_score` | 8247 | `_followable_clamp` |
| `_current_instance_tag` | 9778 | None |
| `_normalize_alert_status` | 2832 | None |
| `_normalize_alert_severity` | 2837 | None |
| `_normalize_agent_mute_scope` | 2842 | None |
| `_normalize_agent_mute_scope_value` | 2847 | None |

- [ ] **Step 1: Create `db_utils.py`**

```bash
# File starts with:
"""
Utility helpers for the Sniper data layer.
Pure functions: coerce, normalize, classify, score.
No database connections. No side effects.
"""
from typing import Any, Dict
import os
```

Copy each function exactly as-is from db.py. Preserve docstrings, type hints, and default values.

**Critical: `compute_followability_score` imports `_followable_clamp` — both must be in this file.**

**Critical: `_followed_wallet_activity_key` imports `_normalize_*` — all must be in this file.**

- [ ] **Step 2: Add re-export to db.py**

At the TOP of db.py (after the existing imports, before the first function):

```python
from db_utils import (  # noqa: F401
    _coerce_int, _coerce_float,
    _normalize_wallet, _normalize_tx_hash, _normalize_condition_id, _normalize_outcome,
    _coerce_history_ts, _followed_wallet_activity_key,
    _extract_signal_identity, _build_execution_key,
    _classify_title_domain, _classify_news_domain,
    _followable_clamp, _followable_short_wallet, compute_followability_score,
    _current_instance_tag,
    _normalize_alert_status, _normalize_alert_severity,
    _normalize_agent_mute_scope, _normalize_agent_mute_scope_value,
)
```

- [ ] **Step 3: Delete the original function bodies from db.py**

For each function: delete the `def` block. The import above makes it available.

**Do NOT delete them all at once.** Delete 5 at a time, run tests after each batch:

Batch 1: `_coerce_int`, `_coerce_float`, `_normalize_wallet`, `_normalize_tx_hash`, `_normalize_condition_id`

```bash
python3 -m pytest tests/ -q 2>&1 | tail -3
# Must show: 1444 passed
```

Batch 2: `_normalize_outcome`, `_coerce_history_ts`, `_followed_wallet_activity_key`, `_extract_signal_identity`, `_build_execution_key`

```bash
python3 -m pytest tests/ -q 2>&1 | tail -3
```

Batch 3: `_classify_title_domain`, `_classify_news_domain`, `_followable_clamp`, `_followable_short_wallet`, `compute_followability_score`

```bash
python3 -m pytest tests/ -q 2>&1 | tail -3
```

Batch 4: `_current_instance_tag`, `_normalize_alert_status`, `_normalize_alert_severity`, `_normalize_agent_mute_scope`, `_normalize_agent_mute_scope_value`

```bash
python3 -m pytest tests/ -q 2>&1 | tail -3
```

- [ ] **Step 4: Verify import still works from external consumers**

```bash
python3 -c "from db import _coerce_float, compute_followability_score, _classify_title_domain; print('OK')"
python3 -c "from db_utils import _coerce_float, compute_followability_score; print('OK direct')"
```

- [ ] **Step 5: Commit**

```bash
git add db.py db_utils.py
git commit -m "refactor: extract db_utils.py — 20 pure utility functions from db.py

Moved: _coerce_float, _coerce_int, _normalize_*, _classify_*_domain,
compute_followability_score, _followable_clamp, _extract_signal_identity,
_build_execution_key, _current_instance_tag, _normalize_alert_*.

All re-exported from db.py — zero import changes needed.
1444 tests passing."
```

---

## Chunk 2: Extract db_snapshots.py (clean boundary, lines 10324–end)

### Task 3: Create db_snapshots.py

**Files:**
- Create: `db_snapshots.py`
- Modify: `db.py` — remove lines 10324–11342, add `from db_snapshots import *`

**Functions to move (28 functions, ~1,018 lines):**

All functions from line 10324 to end of file. These are self-contained snapshot table handlers that follow a consistent pattern: `ensure_*_table()` + `insert_*()` + `recent_*()`.

| Group | Functions |
|-------|-----------|
| Weather | `ensure_weather_snapshots_table`, `insert_weather_snapshot`, `recent_weather_snapshots` |
| Commodity | `ensure_commodity_snapshots_table`, `insert_commodity_snapshot`, `recent_commodity_snapshots`, `commodity_price_history` |
| News | `ensure_news_snapshots_table`, `insert_news_snapshot`, `recent_news_snapshots`, `find_affected_markets`, `recent_news_for_market` |
| Odds | `ensure_odds_snapshots_table`, `insert_odds_snapshot`, `recent_odds_events`, `odds_event_history`, `find_matching_polymarket_h2h_markets`, `_fetch_clob_outcomes`, `polymarket_market_outcomes` |
| Edge | `ensure_edge_observations_table`, `insert_edge_observation` |
| Poly price | `ensure_poly_price_snapshots_table`, `insert_poly_price_snapshot`, `poly_price_history` |
| Analysis | `market_price_series`, `insider_convergence_check`, `wallet_sports_entry_edge` |

**Internal dependencies:**
- `_classify_news_domain` → already in `db_utils.py`
- `_ensure_column` → stays in `db.py` (used by init_db)

- [ ] **Step 1: Create `db_snapshots.py`**

```python
"""
Snapshot table handlers for external data feeds.
Weather, commodities, news, odds, edge observations, Polymarket prices.
"""
import json
import logging
import os
import sqlite3
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from db_utils import _classify_news_domain

logger = logging.getLogger(__name__)
```

Copy all 28 functions from db.py lines 10324–end.

**Critical:** `_fetch_clob_outcomes` (line 11041) uses `urllib.request` — this import must be in `db_snapshots.py`.

**Critical:** Some functions reference `_ensure_column` which stays in db.py. These functions need `from db import _ensure_column` — but that creates a circular import. Solution: pass `_ensure_column` as needed or inline the ALTER TABLE logic for these 2-3 calls.

Actually, checking the code: `_ensure_column` is only used in `ensure_replay_tables` (replay module, not snapshots). The snapshot `ensure_*_table` functions use `CREATE TABLE IF NOT EXISTS` directly, not `_ensure_column`. So **no circular dependency** — clean extraction.

- [ ] **Step 2: Add re-export to db.py**

```python
from db_snapshots import *  # noqa: F401,F403
```

- [ ] **Step 3: Delete lines 10324–end from db.py**

- [ ] **Step 4: Run tests**

```bash
python3 -m pytest tests/ -q 2>&1 | tail -3
# Must show: 1444 passed
```

- [ ] **Step 5: Verify direct imports work**

```bash
python3 -c "from db import ensure_weather_snapshots_table, insert_odds_snapshot, poly_price_history; print('OK')"
python3 -c "from db_snapshots import ensure_weather_snapshots_table; print('OK direct')"
```

- [ ] **Step 6: Commit**

```bash
git add db.py db_snapshots.py
git commit -m "refactor: extract db_snapshots.py — 28 snapshot handlers (~1018 lines)

Weather, commodity, news, odds, edge, poly_price snapshot tables.
All re-exported from db.py. 1444 tests passing."
```

---

## Chunk 3: Extract db_leaderboards.py

### Task 4: Create db_leaderboards.py

**Files:**
- Create: `db_leaderboards.py`
- Modify: `db.py` — remove functions, add re-export

**Functions to move (12 functions, ~790 lines across 2 ranges):**

Range 1 (lines 3831–4440):
- `high_winrate_wallets` (3831)
- `wallet_leaderboard_rows` (3899) — 383 lines, largest function
- `get_wallet_leaderboard_rows` (4282)
- `emerging_insider_wallets` (4300)
- `speculator_flow_wallets` (4366)

Range 2 (lines 7952–8620):
- `wallet_leaderboard_v2_rows` (7952)
- `_sort_leaderboard_v2` (8161) — internal helper
- `_leaderboard_v2_meta` (8201) — internal helper
- `wallet_followable_leaderboard_rows` (8302) — 318 lines

**Internal dependencies:** `compute_followability_score`, `_followable_clamp`, `_followable_short_wallet`, `_classify_title_domain` → all already in `db_utils.py`.

- [ ] **Step 1: Create `db_leaderboards.py`**

```python
"""
Wallet leaderboard and ranking queries.
"""
import logging
import sqlite3
from typing import Any, Dict, List, Optional

from db_utils import (
    compute_followability_score,
    _followable_clamp,
    _followable_short_wallet,
    _classify_title_domain,
)

logger = logging.getLogger(__name__)
```

- [ ] **Step 2: Move Range 1 functions (lines 3831–4440)**

Cut these 5 functions from db.py, paste into db_leaderboards.py.

```bash
python3 -m pytest tests/ -q 2>&1 | tail -3
```

- [ ] **Step 3: Move Range 2 functions (lines 7952–8620)**

Cut these 4 functions from db.py, paste into db_leaderboards.py.

**Gotcha:** After moving Range 1, the line numbers for Range 2 will have shifted. Use function names to locate them, not line numbers.

```bash
python3 -m pytest tests/ -q 2>&1 | tail -3
```

- [ ] **Step 4: Add re-export to db.py**

```python
from db_leaderboards import *  # noqa: F401,F403
```

- [ ] **Step 5: Commit**

```bash
git add db.py db_leaderboards.py
git commit -m "refactor: extract db_leaderboards.py — 12 ranking functions (~790 lines)

wallet_leaderboard_rows, wallet_followable_leaderboard_rows,
high_winrate_wallets, emerging_insider_wallets, etc.
All re-exported from db.py. 1444 tests passing."
```

---

## Chunk 4: Extract db_signals.py

### Task 5: Create db_signals.py

**Files:**
- Create: `db_signals.py`
- Modify: `db.py`

**Functions to move (12 functions, ~620 lines, range 4440–5460):**

- `upsert_position_signal` (4440) — 254 lines
- `mark_wallet_position_signals_open_state` (4694)
- `position_signals` (4749)
- `wallet_resolved_outcome_stats` (4810)
- `thesis_signal_rows` (4930)
- `thesis_candidate_rows` (5033)
- `position_signal_summary_for_wallets` (5083)
- `market_convergence_signals` (5133)
- `market_convergence_detail` (5177)
- `convergence_from_history` (5227)
- `market_intelligence_top_markets` (5351)
- `market_intelligence_domain_summary` (5395)

**Internal deps:** `_coerce_float`, `_coerce_int`, `_extract_signal_identity`, `_classify_title_domain` → all in `db_utils.py`.

- [ ] **Step 1–5:** Same pattern as Tasks 3-4. Create file, move functions, re-export, test, commit.

---

## Chunk 5: Extract db_followed.py (largest, most dependencies)

### Task 6: Create db_followed.py

**Files:**
- Create: `db_followed.py`
- Modify: `db.py`

**Functions to move (36 functions, ~2,640 lines, range 6281–9155 minus leaderboard range):**

This is the largest extraction. It includes:
- Watchlist operations (6281–6445)
- Followed trade operations (6445–6868)
- Followed wallet activity (6868–7037)
- Strategy research notes (7037–7153)
- Auto-follow and sync (7136–7321)
- Real trades summary (7321–7850) — the `real_trades_positions_summary` (337 lines)
- Followed wallet management (7658–7952)
- Position snapshots (8620–9002)
- Research queries (9002–9155)

**Internal deps (39 references):** `_normalize_wallet`, `_normalize_tx_hash`, `_normalize_condition_id`, `_normalize_outcome`, `_coerce_float`, `_coerce_int`, `_coerce_history_ts`, `_followed_wallet_activity_key` → all in `db_utils.py`.

**Gotcha:** `wallet_has_open_signals` (8884) queries `position_signals` table — it doesn't depend on `upsert_position_signal`, just reads the table directly. Safe to move.

- [ ] **Step 1: Create `db_followed.py`**

```python
"""
Followed wallets, watchlists, position snapshots, and research queries.
"""
import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from db_utils import (
    _normalize_wallet, _normalize_tx_hash,
    _normalize_condition_id, _normalize_outcome,
    _coerce_float, _coerce_int, _coerce_history_ts,
    _followed_wallet_activity_key, _classify_title_domain,
)

logger = logging.getLogger(__name__)
```

- [ ] **Step 2: Move in 4 batches (test after each)**

Batch A: Watchlist functions (6281–6445) — 7 functions

```bash
python3 -m pytest tests/ -q 2>&1 | tail -3
```

Batch B: Followed trade + position functions (6445–7037) — 10 functions

```bash
python3 -m pytest tests/ -q 2>&1 | tail -3
```

Batch C: Research + sync + real trades (7037–7950) — 12 functions

```bash
python3 -m pytest tests/ -q 2>&1 | tail -3
```

Batch D: Position snapshots + research queries (8620–9155) — 10 functions

**Gotcha:** After extracting Range 1 in Task 4, the leaderboard functions (7952–8620) are already gone. So lines 8620+ have shifted. Use function names.

```bash
python3 -m pytest tests/ -q 2>&1 | tail -3
```

- [ ] **Step 3: Re-export + commit**

---

## Chunk 6: Extract db_replay.py + Final verification

### Task 7: Create db_replay.py

**Files:**
- Create: `db_replay.py`
- Modify: `db.py`

**Functions to move (14 functions, ~570 lines):**

Range 1 (analyzer helpers):
- `analyzer_paper_trades` (9155)
- `analyzer_signal_outcomes` (9194)
- `analyzer_fwa_trades` (9231)
- `closed_signals_for_wallet` (9280)
- `final_score_for_wallet` (9313)
- `insert_signal_history` (9332)
- `insert_score_history` (9391)

Range 2 (replay engine):
- `replay_curated_signals` (10105)
- `save_replay_config` (10185)
- `get_replay_config` (10197)
- `ensure_replay_tables` (10206) — uses `_ensure_column`
- `list_replay_configs` (10253)
- `save_replay_run` (10262)
- `get_replay_run` (10288)
- `list_replay_runs` (10297)

**Internal deps:** `_ensure_column` (stays in db.py), `_current_instance_tag` (in db_utils.py).

**Gotcha:** `ensure_replay_tables` calls `_ensure_column`. Since `_ensure_column` stays in db.py and db_replay.py imports from db_utils.py, we need to either:
- (a) Move `_ensure_column` to db_utils (but it's tightly coupled with init_db)
- (b) Import it from db.py (creates circular import db.py ↔ db_replay.py)
- (c) Inline the 2 ALTER TABLE calls in `ensure_replay_tables`

**Decision: Option (c)** — `ensure_replay_tables` only calls `_ensure_column` twice (for `instance_tag` on 2 tables). Inline as:

```python
try:
    conn.execute("ALTER TABLE replay_configs ADD COLUMN instance_tag TEXT")
except Exception:
    pass
try:
    conn.execute("ALTER TABLE replay_runs ADD COLUMN instance_tag TEXT")
except Exception:
    pass
```

This removes the cross-dependency entirely.

- [ ] **Step 1–5:** Same pattern. Create file, move, re-export, test, commit.

---

### Task 8: Final verification and cleanup

**Files:**
- Modify: `db.py` — verify final state
- Modify: `CLAUDE.md` — update architecture table

- [ ] **Step 1: Verify line counts**

```bash
wc -l db.py db_utils.py db_snapshots.py db_leaderboards.py db_signals.py db_followed.py db_replay.py
```

Expected:
| File | Lines |
|------|-------|
| `db.py` | ~4,800–5,200 |
| `db_utils.py` | ~250 |
| `db_snapshots.py` | ~1,018 |
| `db_leaderboards.py` | ~790 |
| `db_signals.py` | ~620 |
| `db_followed.py` | ~2,640 |
| `db_replay.py` | ~570 |
| **Total** | ~11,342 (unchanged) |

- [ ] **Step 2: Verify ALL re-exports work**

```bash
python3 -c "
from db import (
    # utils
    _coerce_float, compute_followability_score, _classify_title_domain,
    # snapshots
    ensure_weather_snapshots_table, insert_odds_snapshot, poly_price_history,
    # leaderboards
    wallet_leaderboard_rows, wallet_followable_leaderboard_rows, high_winrate_wallets,
    # signals
    upsert_position_signal, market_convergence_signals, convergence_from_history,
    # followed
    watchlist_wallets, upsert_followed_trade, capture_position_snapshots,
    # replay
    analyzer_paper_trades, replay_curated_signals, save_replay_config,
    # core (still in db.py)
    init_db, get_connection, upsert_user_analysis,
)
print('ALL 18 spot-check imports OK')
"
```

- [ ] **Step 3: Run full test suite**

```bash
python3 -m pytest tests/ -q
```

Must show: `1444 passed, 5 subtests passed`

- [ ] **Step 4: Run all production files to check imports**

```bash
for f in alerter.py analyze_users.py paper_trader.py stream_low_buys.py sync.py web_server.py strategy_runner.py; do
    python3 -c "import importlib.util; spec = importlib.util.spec_from_file_location('mod', '$f'); mod = importlib.util.module_from_spec(spec)" 2>&1 | grep -i error
done
echo "All production imports OK"
```

- [ ] **Step 5: Update CLAUDE.md architecture table**

Add to the component table:

```markdown
| Data layer   | `db.py` + `db_*.py` | Core CRUD + 6 focused modules (utils, snapshots, leaderboards, signals, followed, replay) |
```

- [ ] **Step 6: Final commit**

```bash
git add -A
git commit -m "refactor: complete db.py split — 11,342 → ~5,000 lines + 6 modules

db.py core: connections, schema init, market/user/position CRUD
db_utils.py: coerce, normalize, classify, score (leaf, no DB)
db_snapshots.py: weather, commodity, news, odds, edge, poly_price
db_leaderboards.py: wallet rankings, followability, v2 leaderboard
db_signals.py: position signals, convergence, market intelligence
db_followed.py: watchlist, followed wallets, snapshots, research
db_replay.py: retrospective analysis, replay configs/runs

All functions re-exported from db.py — zero import changes.
1444 tests passing identically."
```

---

## Risk Mitigation

### If tests fail after a move:

1. Check if the moved function references a module-level variable (e.g., `logger`, `DB_PATH`)
2. Check if it references another function that wasn't moved (import it from db_utils or db)
3. Check if a test imports the function directly from db.py (the re-export should handle this)
4. If stuck: `git stash` to revert, diagnose, re-apply

### Circular import prevention:

- `db_utils.py` imports NOTHING from other `db_*` modules (leaf)
- `db_snapshots.py` imports from `db_utils` only
- `db_leaderboards.py` imports from `db_utils` only
- `db_signals.py` imports from `db_utils` only
- `db_followed.py` imports from `db_utils` only
- `db_replay.py` imports from `db_utils` only (with inlined `_ensure_column`)
- `db.py` imports from ALL of the above (one-way)

### Execution order is load-bearing:

**Task 2 (utils) → Task 3 (snapshots) → Task 4 (leaderboards) → Task 5 (signals) → Task 6 (followed) → Task 7 (replay)**

Do NOT skip or reorder. Each task depends on the previous one being complete.
