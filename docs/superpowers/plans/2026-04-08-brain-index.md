# Brain Index Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a repo-local engineering index that generates machine-readable schema, dependency, and pattern artifacts from the actual codebase, queryable via CLI — so agents never hallucinate column names again.

**Architecture:** Three artifacts generated into `index/` and committed to git. Schema from SQLite `PRAGMA` introspection after `init_db()` on `:memory:`. Dependencies from word-boundary grep of table names in `.py` files. Patterns hand-curated in YAML. Single CLI entry point `scripts/brain_index.py` with `generate`, `schema`, `pattern`, `deps` subcommands.

**Tech Stack:** Python 3, SQLite PRAGMA introspection, PyYAML (already installed), argparse, pytest

---

## File Structure

| Action | File | Purpose |
|--------|------|---------|
| Create | `scripts/brain_index.py` | CLI tool + generation logic (~250 lines) |
| Create | `index/schema_registry.json` | Generated: full SQLite schema from init_db() |
| Create | `index/dependency_index.json` | Generated: file→table dependency mapping |
| Create | `index/pattern_registry.yaml` | Curated: 8 core code patterns with examples |
| Create | `tests/test_brain_index.py` | Tests for generators, formatters, and CLI |
| Modify | `CLAUDE.md` | Reference index/ as authoritative schema source |

---

### Task 1: Schema Registry Generator

**Files:**
- Create: `scripts/brain_index.py` (initial — schema generation only)
- Create: `tests/test_brain_index.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_brain_index.py`:

```python
"""Tests for brain_index — repo-local engineering index."""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))


def test_generate_schema_registry():
    from brain_index import generate_schema_registry

    registry = generate_schema_registry()

    # Must have metadata fields
    assert "generated_at" in registry
    assert "schema_version" in registry
    assert "table_count" in registry
    assert "tables" in registry

    # Must contain core tables
    tables = registry["tables"]
    for expected in [
        "low_buy_trades",
        "user_analysis",
        "position_signals",
        "paper_trades",
        "market_metadata",
        "followed_wallets",
        "paper_limit_orders",
        "signal_queue",
    ]:
        assert expected in tables, f"Missing table: {expected}"

    # Verify column structure for a known table
    cols = {c["name"]: c for c in tables["low_buy_trades"]["columns"]}
    assert "transaction_hash" in cols
    assert cols["transaction_hash"]["pk"] is True
    assert "proxy_wallet" in cols
    assert cols["proxy_wallet"]["type"] == "TEXT"
    assert cols["proxy_wallet"]["notnull"] is True

    # paper_limit_orders — the motivating failure case
    plo_cols = {c["name"]: c for c in tables["paper_limit_orders"]["columns"]}
    assert "order_id" in plo_cols
    assert "strategy_id" in plo_cols
    assert "condition_id" in plo_cols
    assert "bid_price" in plo_cols
    assert "favorite_token_id" in plo_cols
    assert "favorite_token_label" in plo_cols
    assert "tour" in plo_cols
    assert "player1" in plo_cols
    assert "player2" in plo_cols

    # Verify indexes and foreign_keys are lists
    assert isinstance(tables["low_buy_trades"]["indexes"], list)
    assert isinstance(tables["low_buy_trades"]["foreign_keys"], list)


def test_schema_registry_table_count():
    from brain_index import generate_schema_registry

    registry = generate_schema_registry()
    # init_db() creates 40+ tables — verify we capture them all
    assert registry["table_count"] >= 35
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/bigpapi/Desktop/Sniper && python3 -m pytest tests/test_brain_index.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'brain_index'`

- [ ] **Step 3: Write the schema generator**

Create `scripts/brain_index.py`:

```python
#!/usr/bin/env python3
"""Brain Index — repo-local engineering index for Sniper.

Generates machine-readable artifacts from the actual codebase:
- schema_registry.json  — SQLite table schemas via init_db() introspection
- dependency_index.json — heuristic file→table dependency mapping
- pattern_registry.yaml — hand-curated code pattern reference (not generated)

Usage:
    python scripts/brain_index.py generate          # regenerate schema + deps
    python scripts/brain_index.py schema <table>    # lookup table schema
    python scripts/brain_index.py schema --list     # list all tables
    python scripts/brain_index.py pattern <name>    # lookup pattern
    python scripts/brain_index.py pattern --list    # list all patterns
    python scripts/brain_index.py deps <file>       # file dependencies
    python scripts/brain_index.py deps --table <t>  # table readers/writers
"""

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX_DIR = REPO_ROOT / "index"
SCHEMA_FILE = INDEX_DIR / "schema_registry.json"
DEPS_FILE = INDEX_DIR / "dependency_index.json"
PATTERN_FILE = INDEX_DIR / "pattern_registry.yaml"


# ---------------------------------------------------------------------------
# Schema registry generation
# ---------------------------------------------------------------------------


def generate_schema_registry() -> dict:
    """Create in-memory SQLite DB via init_db(), introspect all tables."""
    sys.path.insert(0, str(REPO_ROOT))
    from db import init_db

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)

    schema_version = conn.execute("PRAGMA user_version").fetchone()[0]

    tables = {}
    for row in conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    ):
        table_name = row["name"]

        columns = []
        for col in conn.execute(f"PRAGMA table_info('{table_name}')"):
            columns.append(
                {
                    "name": col["name"],
                    "type": col["type"],
                    "notnull": bool(col["notnull"]),
                    "pk": bool(col["pk"]),
                    "default": col["dflt_value"],
                }
            )

        indexes = []
        for idx in conn.execute(f"PRAGMA index_list('{table_name}')"):
            idx_name = idx["name"]
            idx_cols = [
                c["name"]
                for c in conn.execute(f"PRAGMA index_info('{idx_name}')")
            ]
            indexes.append(
                {
                    "name": idx_name,
                    "unique": bool(idx["unique"]),
                    "columns": idx_cols,
                }
            )

        fks = []
        for fk in conn.execute(f"PRAGMA foreign_key_list('{table_name}')"):
            fks.append(
                {
                    "from_column": fk["from"],
                    "to_table": fk["table"],
                    "to_column": fk["to"],
                }
            )

        tables[table_name] = {
            "columns": columns,
            "indexes": indexes,
            "foreign_keys": fks,
        }

    conn.close()

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "schema_version": schema_version,
        "table_count": len(tables),
        "tables": tables,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/bigpapi/Desktop/Sniper && python3 -m pytest tests/test_brain_index.py -v`
Expected: 2 tests PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/brain_index.py tests/test_brain_index.py
git commit -m "feat: schema registry generator with SQLite introspection"
```

---

### Task 2: Dependency Index Generator

**Files:**
- Modify: `scripts/brain_index.py` (add `generate_dependency_index()`)
- Modify: `tests/test_brain_index.py` (add dependency tests)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_brain_index.py`:

```python
def test_generate_dependency_index():
    from brain_index import generate_dependency_index, generate_schema_registry

    schema = generate_schema_registry()
    deps = generate_dependency_index(schema)

    assert "generated_at" in deps
    assert "file_count" in deps
    assert "files" in deps
    assert "table_readers" in deps
    assert "table_writers" in deps

    # db.py must reference many tables (it defines them all)
    assert "db.py" in deps["files"]
    db_entry = deps["files"]["db.py"]
    total_refs = len(db_entry["reads_tables"]) + len(db_entry["writes_tables"])
    assert total_refs > 10, f"db.py only references {total_refs} tables"

    # paper_trader.py must reference paper_trades
    assert "paper_trader.py" in deps["files"]
    pt = deps["files"]["paper_trader.py"]
    pt_all_tables = set(pt["reads_tables"]) | set(pt["writes_tables"])
    assert "paper_trades" in pt_all_tables

    # Table readers/writers must have entries
    assert len(deps["table_readers"]) > 0
    assert len(deps["table_writers"]) > 0


def test_dependency_index_skips_tests_and_venv():
    from brain_index import generate_dependency_index, generate_schema_registry

    schema = generate_schema_registry()
    deps = generate_dependency_index(schema)

    for file_path in deps["files"]:
        assert not file_path.startswith("tests/"), f"Test file leaked: {file_path}"
        assert ".venv/" not in file_path, f"Venv file leaked: {file_path}"
        assert "__pycache__" not in file_path, f"Cache file leaked: {file_path}"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/bigpapi/Desktop/Sniper && python3 -m pytest tests/test_brain_index.py::test_generate_dependency_index -v`
Expected: FAIL — `ImportError: cannot import name 'generate_dependency_index'`

- [ ] **Step 3: Implement the dependency generator**

Add to `scripts/brain_index.py`, after the schema generator:

```python
import re


# ---------------------------------------------------------------------------
# Dependency index generation
# ---------------------------------------------------------------------------

# SQL keywords near a table name that indicate write operations
_WRITE_KW = re.compile(r"\b(?:INSERT|UPDATE|DELETE|REPLACE)\b", re.IGNORECASE)
# SQL keywords near a table name that indicate read operations
_READ_KW = re.compile(r"\b(?:SELECT|FROM|JOIN)\b", re.IGNORECASE)

_SKIP_DIRS = frozenset(
    {"tests", ".venv", "venv", "__pycache__", "index", "node_modules", ".git"}
)


def generate_dependency_index(schema: dict) -> dict:
    """Grep-backed extraction of file→table dependencies.

    For each .py file in the repo (excluding tests/venv), checks which known
    table names appear using word-boundary matching.  Classifies as read or
    write based on surrounding SQL keywords on the same line.
    """
    table_names = set(schema["tables"].keys())
    # Pre-compile per-table regex for performance
    table_patterns = {t: re.compile(rf"\b{re.escape(t)}\b") for t in table_names}

    file_deps: dict[str, dict] = {}
    table_readers: dict[str, list[str]] = {}
    table_writers: dict[str, list[str]] = {}

    for py_file in sorted(REPO_ROOT.rglob("*.py")):
        rel = py_file.relative_to(REPO_ROOT)
        if any(p in _SKIP_DIRS for p in rel.parts):
            continue

        try:
            content = py_file.read_text(errors="replace")
        except OSError:
            continue

        reads: set[str] = set()
        writes: set[str] = set()
        imports: set[str] = set()

        for line in content.splitlines():
            # Track imports from db modules
            m = re.match(r"^(?:from\s+(db\w*)|import\s+(db\w*))", line)
            if m:
                imports.add(m.group(1) or m.group(2))

            # Check each table name against this line
            for table, pattern in table_patterns.items():
                if pattern.search(line):
                    if _WRITE_KW.search(line):
                        writes.add(table)
                    elif _READ_KW.search(line):
                        reads.add(table)
                    else:
                        # Table referenced without SQL keyword — count as read
                        reads.add(table)

        rel_str = str(rel)
        if reads or writes or imports:
            file_deps[rel_str] = {
                "reads_tables": sorted(reads),
                "writes_tables": sorted(writes),
                "imports": sorted(imports),
            }
            for t in reads:
                table_readers.setdefault(t, []).append(rel_str)
            for t in writes:
                table_writers.setdefault(t, []).append(rel_str)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "file_count": len(file_deps),
        "files": file_deps,
        "table_readers": {t: sorted(v) for t, v in table_readers.items()},
        "table_writers": {t: sorted(v) for t, v in table_writers.items()},
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/bigpapi/Desktop/Sniper && python3 -m pytest tests/test_brain_index.py -v`
Expected: 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/brain_index.py tests/test_brain_index.py
git commit -m "feat: dependency index generator with grep-based table extraction"
```

---

### Task 3: Pattern Registry

**Files:**
- Create: `index/pattern_registry.yaml`
- Modify: `tests/test_brain_index.py` (add pattern validation test)

- [ ] **Step 1: Create the index directory**

```bash
mkdir -p /home/bigpapi/Desktop/Sniper/index
```

- [ ] **Step 2: Write the failing test**

Append to `tests/test_brain_index.py`:

```python
import yaml


def test_pattern_registry_valid():
    pattern_file = REPO_ROOT / "index" / "pattern_registry.yaml"
    assert pattern_file.exists(), "pattern_registry.yaml not found"

    with open(pattern_file) as f:
        data = yaml.safe_load(f)

    assert "patterns" in data
    patterns = data["patterns"]
    assert len(patterns) >= 5

    # Every pattern must have required fields
    for p in patterns:
        assert "name" in p, f"Pattern missing 'name': {p}"
        assert "description" in p, f"Pattern '{p.get('name')}' missing 'description'"
        assert "files" in p, f"Pattern '{p['name']}' missing 'files'"
        assert "example" in p, f"Pattern '{p['name']}' missing 'example'"
        assert len(p["files"]) >= 1, f"Pattern '{p['name']}' has empty files list"

    # Check expected core patterns exist
    names = {p["name"] for p in patterns}
    for expected in ["strategy_plugin", "flask_blueprint", "db_module"]:
        assert expected in names, f"Missing expected pattern: {expected}"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd /home/bigpapi/Desktop/Sniper && python3 -m pytest tests/test_brain_index.py::test_pattern_registry_valid -v`
Expected: FAIL — `AssertionError: pattern_registry.yaml not found`

- [ ] **Step 4: Create the pattern registry**

Create `index/pattern_registry.yaml`:

```yaml
# Pattern Registry — hand-curated code patterns for Sniper
# Last updated: 2026-04-08
#
# Purpose: gives agents and developers a quick-reference for how to add new
# components that follow existing conventions.  Each pattern includes the
# files to read, a minimal code example, and notes about gotchas.

patterns:
  - name: strategy_plugin
    description: How to write a new strategy plugin extending StrategyPlugin
    files:
      - strategies/base.py
      - strategies/insider_copy_v1.py
      - strategies/registry.py
    example: |
      from strategies.base import (
          StrategyPlugin, StrategySignal, StrategyEntryDecision,
          StrategyExitDecision, StrategyManageState, StrategyReport,
      )

      class MyStrategyV1(StrategyPlugin):
          name = "my_strategy_v1"
          version = "1.0"

          def evaluate_signal(self, signal: StrategySignal) -> StrategyEntryDecision:
              # Return should_enter=True with size_usdc to open a position
              ...

          def manage_position(self, position, state: StrategyManageState) -> StrategyExitDecision:
              # Return should_exit=True with reason to close
              ...

          def report(self) -> StrategyReport:
              return StrategyReport(summary={"status": "ok"})
    notes: |
      Register in strategies/registry.py STRATEGY_PLUGINS dict.
      All strategies run through strategy_runner.py / strategy_service.py.
      Use execution_mode="paper" until validated.
      Dataclasses use slots=True — no dynamic attributes.

  - name: flask_blueprint
    description: How Flask route blueprints are structured in routes/
    files:
      - routes/api_core.py
      - routes/api_followed.py
      - routes/api_strategies.py
      - web_server.py
    example: |
      from flask import Blueprint, jsonify, request
      from db import get_readonly_connection

      bp = Blueprint("api_myfeature", __name__)

      @bp.route("/api/my-endpoint")
      def my_endpoint():
          conn = get_readonly_connection()
          try:
              rows = my_db_function(conn)
              return jsonify(rows)
          finally:
              conn.close()
    notes: |
      Register blueprint in web_server.py: app.register_blueprint(bp).
      Use get_readonly_connection() for reads, get_connection() for writes.
      All DB access through db.py functions — no raw SQL in routes.
      Serialization through serializers.py — don't format in routes.

  - name: db_module
    description: How db helper modules are organized (db_*.py split)
    files:
      - db.py
      - db_utils.py
      - db_signals.py
      - db_followed.py
      - db_leaderboards.py
      - db_snapshots.py
      - db_replay.py
      - db_hyperliquid.py
    example: |
      # db_mymodule.py
      import sqlite3

      def my_query(conn: sqlite3.Connection, wallet: str) -> list[dict]:
          rows = conn.execute(
              "SELECT * FROM my_table WHERE wallet = ?", (wallet,)
          ).fetchall()
          return [dict(r) for r in rows]

      def upsert_my_record(conn: sqlite3.Connection, wallet: str, value: float) -> None:
          conn.execute(
              "INSERT OR REPLACE INTO my_table (wallet, value) VALUES (?, ?)",
              (wallet, value),
          )
          conn.commit()

      # db.py — add re-export for backward compatibility:
      # from db_mymodule import *  # noqa: F401,F403
    notes: |
      All DB functions take conn as first argument.
      New modules must be re-exported from db.py for backward compat.
      Use parameterized queries (?) — never f-strings with user input.
      conn.row_factory = sqlite3.Row is set by get_connection/get_readonly_connection.

  - name: feed_adapter
    description: How data feed adapters are structured in feeds/
    files:
      - feeds/registry.py
      - feeds/polymarket.py
      - feeds/hyperliquid.py
    example: |
      from feeds.base import FeedAdapter

      class MyFeedAdapter(FeedAdapter):
          feed_id = "my_feed"

          def fetch_snapshot(self) -> dict:
              # Return data dict consumed by strategy plugins
              ...

      # Register in feeds/registry.py:
      # FEED_ADAPTERS["my_feed"] = MyFeedAdapter
    notes: |
      Feed adapters provide data to strategy plugins via strategy_runner.py.
      Must be registered in feeds/registry.py FEED_ADAPTERS dict.
      fetch_snapshot() is called per strategy run cycle.

  - name: ensure_schema
    description: Idempotent schema migration pattern for new tables
    files:
      - db.py
      - db_hyperliquid.py
      - db_replay.py
    example: |
      def ensure_my_tables(conn: sqlite3.Connection) -> None:
          """Idempotent — safe to call on every startup."""
          conn.executescript("""
              CREATE TABLE IF NOT EXISTS my_table (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  wallet TEXT NOT NULL,
                  value REAL NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
              );
              CREATE INDEX IF NOT EXISTS idx_my_table_wallet
                  ON my_table(wallet);
          """)

      # Then add call in init_db() in db.py:
      #     ensure_my_tables(conn)
    notes: |
      Always use IF NOT EXISTS for tables and indexes.
      No destructive DDL (no DROP, no column removal).
      Called from init_db() so it runs on every service startup.
      For adding columns to existing tables, use _ensure_column().
      After adding: run `python scripts/brain_index.py generate` to update schema registry.

  - name: mcp_tool
    description: How MCP server tools are structured in mcp_tools/
    files:
      - mcp_tools/read_tools.py
      - mcp_tools/write_tools.py
      - mcp_tools/analyze_tools.py
      - mcp_tools/replay_tools.py
      - sniper_mcp_server.py
    example: |
      from db import get_readonly_connection

      def get_my_data(param: str) -> dict:
          """One-line description shown in MCP tool listing."""
          conn = get_readonly_connection()
          try:
              rows = my_db_query(conn, param)
              return {"status": "ok", "data": rows}
          finally:
              conn.close()

      # Register in sniper_mcp_server.py @server.tool decorator
    notes: |
      Read tools use get_readonly_connection().
      Write tools use get_connection() and call insert_mcp_audit_log().
      Tools return dicts — JSON-serialized by MCP framework.
      Register with @server.tool() decorator in sniper_mcp_server.py.

  - name: systemd_service
    description: How VPS systemd user services are configured
    files:
      - CLAUDE.md
    example: |
      # ~/.config/systemd/user/sniper-myservice.service
      [Unit]
      Description=Sniper My Service
      After=sniper-web.service

      [Service]
      Type=simple
      WorkingDirectory=/home/sniper/sniper
      ExecStart=/home/sniper/sniper/.venv/bin/python3 my_service.py
      Restart=on-failure
      RestartSec=10
      Environment=PYTHONUNBUFFERED=1

      [Install]
      WantedBy=default.target
    notes: |
      Services run as user units: systemctl --user start/stop/restart sniper-<name>.
      Startup order: web first, then stream, analyzer, paper, alerter.
      After code changes: systemctl --user restart sniper-<name>.
      Unit files live on VPS at ~/.config/systemd/user/ (not in repo).
      View logs: journalctl --user -u sniper-<name> -f.

  - name: test_fixture
    description: How tests use conftest.py for env pinning and local in-memory DB helpers
    files:
      - tests/conftest.py
      - tests/test_fresh_account.py
      - tests/test_brain_index.py
    example: |
      import pytest
      import sqlite3
      from db import init_db

      # Tests typically create their own in-memory DB helper:
      def _in_memory_conn():
          conn = sqlite3.connect(":memory:")
          conn.row_factory = sqlite3.Row
          init_db(conn)
          return conn

      def test_my_feature():
          conn = _in_memory_conn()
          conn.execute(
              "INSERT INTO my_table (wallet, value) VALUES (?, ?)",
              ("0xABC", 42.0),
          )
          conn.commit()
          try:
              result = my_function(conn)
              assert result["value"] == 42.0
          finally:
              conn.close()
    notes: |
      conftest.py pins all scoring env vars to canonical defaults.
      conftest.py does not currently provide a shared fresh_db fixture.
      Most tests use file-local helpers like _in_memory_conn() or _fresh_db().
      Do NOT assume a shared DB fixture exists unless this project adds one.
      Tests run with: python3 -m pytest tests/ -q
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd /home/bigpapi/Desktop/Sniper && python3 -m pytest tests/test_brain_index.py::test_pattern_registry_valid -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add index/pattern_registry.yaml tests/test_brain_index.py
git commit -m "feat: hand-curated pattern registry with 8 core patterns"
```

---

### Task 4: CLI Query Tool

**Files:**
- Modify: `scripts/brain_index.py` (add formatting functions + argparse CLI)
- Modify: `tests/test_brain_index.py` (add formatter + CLI tests)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_brain_index.py`:

```python
def test_format_schema_table():
    from brain_index import format_schema_table, generate_schema_registry

    registry = generate_schema_registry()
    output = format_schema_table(registry, "low_buy_trades")

    assert "low_buy_trades" in output
    assert "transaction_hash" in output
    assert "TEXT" in output
    assert "PK" in output
    assert "proxy_wallet" in output


def test_format_schema_table_missing():
    from brain_index import format_schema_table, generate_schema_registry

    registry = generate_schema_registry()
    output = format_schema_table(registry, "nonexistent_table")
    assert "not found" in output.lower()


def test_format_schema_list():
    from brain_index import format_schema_list, generate_schema_registry

    registry = generate_schema_registry()
    output = format_schema_list(registry)

    assert "low_buy_trades" in output
    assert "paper_limit_orders" in output
    # Should show column counts
    assert "col" in output.lower()


def test_format_deps_file():
    from brain_index import (
        format_deps_file,
        generate_dependency_index,
        generate_schema_registry,
    )

    schema = generate_schema_registry()
    deps = generate_dependency_index(schema)
    output = format_deps_file(deps, "paper_trader.py")

    assert "paper_trader.py" in output
    assert "paper_trades" in output


def test_format_deps_table():
    from brain_index import (
        format_deps_table,
        generate_dependency_index,
        generate_schema_registry,
    )

    schema = generate_schema_registry()
    deps = generate_dependency_index(schema)
    output = format_deps_table(deps, "low_buy_trades")

    assert "low_buy_trades" in output
    # db.py should show as a reader/writer
    assert "db.py" in output


def test_format_pattern():
    from brain_index import format_pattern

    pattern_file = REPO_ROOT / "index" / "pattern_registry.yaml"
    with open(pattern_file) as f:
        data = yaml.safe_load(f)

    output = format_pattern(data, "strategy_plugin")
    assert "strategy_plugin" in output
    assert "StrategyPlugin" in output
    assert "strategies/base.py" in output


def test_format_pattern_missing():
    from brain_index import format_pattern

    pattern_file = REPO_ROOT / "index" / "pattern_registry.yaml"
    with open(pattern_file) as f:
        data = yaml.safe_load(f)

    output = format_pattern(data, "nonexistent_pattern")
    assert "not found" in output.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/bigpapi/Desktop/Sniper && python3 -m pytest tests/test_brain_index.py::test_format_schema_table -v`
Expected: FAIL — `ImportError: cannot import name 'format_schema_table'`

- [ ] **Step 3: Implement formatting functions**

Add to `scripts/brain_index.py`, after the generators:

```python
import yaml


# ---------------------------------------------------------------------------
# Formatting functions (for CLI output)
# ---------------------------------------------------------------------------


def format_schema_table(registry: dict, table_name: str) -> str:
    """Format a single table's schema for terminal display."""
    tables = registry.get("tables", {})
    if table_name not in tables:
        return f"Table '{table_name}' not found. Run: python scripts/brain_index.py schema --list"

    t = tables[table_name]
    lines = [f"── {table_name} ──", ""]

    # Columns
    lines.append("Columns:")
    for col in t["columns"]:
        flags = []
        if col["pk"]:
            flags.append("PK")
        if col["notnull"]:
            flags.append("NOT NULL")
        if col["default"] is not None:
            flags.append(f"DEFAULT {col['default']}")
        flag_str = f"  ({', '.join(flags)})" if flags else ""
        lines.append(f"  {col['name']:40s} {col['type']:10s}{flag_str}")

    # Indexes
    if t["indexes"]:
        lines.append("")
        lines.append("Indexes:")
        for idx in t["indexes"]:
            uniq = "UNIQUE " if idx["unique"] else ""
            lines.append(f"  {idx['name']:50s} {uniq}({', '.join(idx['columns'])})")

    # Foreign keys
    if t["foreign_keys"]:
        lines.append("")
        lines.append("Foreign keys:")
        for fk in t["foreign_keys"]:
            lines.append(f"  {fk['from_column']} → {fk['to_table']}.{fk['to_column']}")

    return "\n".join(lines)


def format_schema_list(registry: dict) -> str:
    """List all tables with column counts."""
    lines = [f"Tables ({registry['table_count']}):", ""]
    for name, t in sorted(registry["tables"].items()):
        col_count = len(t["columns"])
        idx_count = len(t["indexes"])
        lines.append(f"  {name:45s} {col_count:3d} cols, {idx_count:2d} indexes")
    return "\n".join(lines)


def format_deps_file(deps: dict, file_path: str) -> str:
    """Format dependency info for a single file."""
    files = deps.get("files", {})
    if file_path not in files:
        return f"File '{file_path}' not found in dependency index."

    entry = files[file_path]
    lines = [f"── {file_path} ──", ""]

    if entry["imports"]:
        lines.append(f"Imports: {', '.join(entry['imports'])}")
    if entry["reads_tables"]:
        lines.append(f"Reads tables: {', '.join(entry['reads_tables'])}")
    if entry["writes_tables"]:
        lines.append(f"Writes tables: {', '.join(entry['writes_tables'])}")

    return "\n".join(lines)


def format_deps_table(deps: dict, table_name: str) -> str:
    """Format which files read/write a specific table."""
    readers = deps.get("table_readers", {}).get(table_name, [])
    writers = deps.get("table_writers", {}).get(table_name, [])

    if not readers and not writers:
        return f"Table '{table_name}' not found in dependency index."

    lines = [f"── {table_name} ──", ""]

    if readers:
        lines.append(f"Read by ({len(readers)}):")
        for f in readers:
            lines.append(f"  {f}")

    if writers:
        lines.append(f"Written by ({len(writers)}):")
        for f in writers:
            lines.append(f"  {f}")

    return "\n".join(lines)


def format_pattern(data: dict, pattern_name: str) -> str:
    """Format a single pattern for terminal display."""
    patterns = data.get("patterns", [])
    match = None
    for p in patterns:
        if p["name"] == pattern_name:
            match = p
            break

    if match is None:
        names = [p["name"] for p in patterns]
        return f"Pattern '{pattern_name}' not found. Available: {', '.join(names)}"

    lines = [f"── {match['name']} ──", ""]
    lines.append(match["description"])
    lines.append("")
    lines.append("Files:")
    for f in match["files"]:
        lines.append(f"  {f}")
    lines.append("")
    lines.append("Example:")
    lines.append(match["example"].rstrip())
    if match.get("notes"):
        lines.append("")
        lines.append("Notes:")
        lines.append(match["notes"].rstrip())

    return "\n".join(lines)


def format_pattern_list(data: dict) -> str:
    """List all patterns with descriptions."""
    patterns = data.get("patterns", [])
    lines = [f"Patterns ({len(patterns)}):", ""]
    for p in patterns:
        lines.append(f"  {p['name']:25s} {p['description']}")
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/bigpapi/Desktop/Sniper && python3 -m pytest tests/test_brain_index.py -v`
Expected: All tests PASS (12 tests)

- [ ] **Step 5: Implement argparse CLI**

Add to the bottom of `scripts/brain_index.py`:

```python
# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        print(f"File not found: {path}")
        print("Run first: python scripts/brain_index.py generate")
        return None
    with open(path) as f:
        return json.load(f)


def _load_yaml(path: Path) -> dict | None:
    if not path.exists():
        print(f"File not found: {path}")
        return None
    with open(path) as f:
        return yaml.safe_load(f)


def cmd_generate(_args) -> None:
    """Regenerate schema_registry.json and dependency_index.json."""
    INDEX_DIR.mkdir(exist_ok=True)

    print("Generating schema registry...")
    schema = generate_schema_registry()
    with open(SCHEMA_FILE, "w") as f:
        json.dump(schema, f, indent=2)
    print(f"  → {SCHEMA_FILE} ({schema['table_count']} tables)")

    print("Generating dependency index...")
    deps = generate_dependency_index(schema)
    with open(DEPS_FILE, "w") as f:
        json.dump(deps, f, indent=2)
    print(f"  → {DEPS_FILE} ({deps['file_count']} files)")

    if PATTERN_FILE.exists():
        print(f"  ✓ {PATTERN_FILE} (hand-curated, not regenerated)")
    else:
        print(f"  ⚠ {PATTERN_FILE} not found — create manually")

    print("Done.")


def cmd_schema(args) -> None:
    """Query schema registry."""
    registry = _load_json(SCHEMA_FILE)
    if registry is None:
        return

    if args.list:
        print(format_schema_list(registry))
    elif args.table:
        print(format_schema_table(registry, args.table))
    else:
        print("Usage: brain_index.py schema <table> | --list")


def cmd_pattern(args) -> None:
    """Query pattern registry."""
    data = _load_yaml(PATTERN_FILE)
    if data is None:
        return

    if args.list:
        print(format_pattern_list(data))
    elif args.name:
        print(format_pattern(data, args.name))
    else:
        print("Usage: brain_index.py pattern <name> | --list")


def cmd_deps(args) -> None:
    """Query dependency index."""
    deps = _load_json(DEPS_FILE)
    if deps is None:
        return

    if args.table:
        print(format_deps_table(deps, args.table))
    elif args.file:
        print(format_deps_file(deps, args.file))
    else:
        print("Usage: brain_index.py deps <file> | --table <table>")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Brain Index — repo-local engineering index for Sniper"
    )
    sub = parser.add_subparsers(dest="command")

    # generate
    sub.add_parser("generate", help="Regenerate schema + dependency artifacts")

    # schema
    p_schema = sub.add_parser("schema", help="Query table schemas")
    p_schema.add_argument("table", nargs="?", help="Table name to look up")
    p_schema.add_argument("--list", action="store_true", help="List all tables")

    # pattern
    p_pattern = sub.add_parser("pattern", help="Query code patterns")
    p_pattern.add_argument("name", nargs="?", help="Pattern name to look up")
    p_pattern.add_argument("--list", action="store_true", help="List all patterns")

    # deps
    p_deps = sub.add_parser("deps", help="Query file/table dependencies")
    p_deps.add_argument("file", nargs="?", help="File path to look up")
    p_deps.add_argument("--table", help="Table name to look up readers/writers")

    args = parser.parse_args()

    commands = {
        "generate": cmd_generate,
        "schema": cmd_schema,
        "pattern": cmd_pattern,
        "deps": cmd_deps,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    import argparse

    main()
```

Note: move the `import argparse` to the top of the file (with the other imports) and remove the one inside `__main__`. The `import argparse` at the top of the file was already listed in the initial creation — add it there.

- [ ] **Step 6: Run full test suite**

Run: `cd /home/bigpapi/Desktop/Sniper && python3 -m pytest tests/test_brain_index.py -v`
Expected: All 12 tests PASS

- [ ] **Step 7: Commit**

```bash
git add scripts/brain_index.py tests/test_brain_index.py
git commit -m "feat: CLI query tool with schema/pattern/deps subcommands"
```

---

### Task 5: Generate Artifacts + Integration

**Files:**
- Modify: `CLAUDE.md` (add index/ reference)
- Generate: `index/schema_registry.json` (via CLI)
- Generate: `index/dependency_index.json` (via CLI)

- [ ] **Step 1: Run the generator**

```bash
cd /home/bigpapi/Desktop/Sniper && python3 scripts/brain_index.py generate
```

Expected output:
```
Generating schema registry...
  → /home/bigpapi/Desktop/Sniper/index/schema_registry.json (40+ tables)
Generating dependency index...
  → /home/bigpapi/Desktop/Sniper/index/dependency_index.json (50+ files)
  ✓ /home/bigpapi/Desktop/Sniper/index/pattern_registry.yaml (hand-curated, not regenerated)
Done.
```

- [ ] **Step 2: Smoke-test the CLI**

```bash
cd /home/bigpapi/Desktop/Sniper

# Schema lookup — the motivating case
python3 scripts/brain_index.py schema paper_limit_orders
# Should print exact columns: order_id, strategy_id, condition_id, bid_price, ...

# Schema list
python3 scripts/brain_index.py schema --list
# Should print 40+ tables with column counts

# Pattern lookup
python3 scripts/brain_index.py pattern strategy_plugin
# Should print StrategyPlugin example code

# Dependency lookup
python3 scripts/brain_index.py deps paper_trader.py
# Should show reads/writes including paper_trades

# Reverse lookup
python3 scripts/brain_index.py deps --table paper_limit_orders
# Should show which files reference this table
```

- [ ] **Step 3: Add CLAUDE.md reference**

Add this section to `CLAUDE.md`, after the "### Core DB tables" section:

```markdown
### Brain Index — authoritative schema/pattern/dependency lookup

The `index/` directory contains machine-generated artifacts for exact codebase facts.
**Agents must read these before writing code that touches DB tables.**

```bash
# Lookup exact columns for a table (prevents hallucinated column names)
python3 scripts/brain_index.py schema paper_limit_orders

# List all tables with column counts
python3 scripts/brain_index.py schema --list

# Find which files read/write a table
python3 scripts/brain_index.py deps --table paper_trades

# Find what tables a file touches
python3 scripts/brain_index.py deps paper_trader.py

# Lookup code patterns (strategy plugin, blueprint, db module, etc.)
python3 scripts/brain_index.py pattern strategy_plugin
python3 scripts/brain_index.py pattern --list

# Regenerate after schema changes (new table, new column, new ensure_*)
python3 scripts/brain_index.py generate
```

Source files: `index/schema_registry.json` (generated), `index/dependency_index.json` (generated), `index/pattern_registry.yaml` (hand-curated).
```

- [ ] **Step 4: Run full project test suite**

```bash
cd /home/bigpapi/Desktop/Sniper && python3 -m pytest tests/ -q
```

Expected: All existing tests pass + new brain_index tests pass.

- [ ] **Step 5: Commit everything**

```bash
git add index/schema_registry.json index/dependency_index.json index/pattern_registry.yaml CLAUDE.md
git commit -m "feat: brain index — initial generation + CLAUDE.md integration"
```

- [ ] **Step 6: Run final verification**

```bash
cd /home/bigpapi/Desktop/Sniper
# Verify the motivating case: paper_limit_orders columns are now queryable
python3 scripts/brain_index.py schema paper_limit_orders | head -20
# Verify table count matches reality
python3 -c "
import json
with open('index/schema_registry.json') as f:
    d = json.load(f)
print(f'Tables: {d[\"table_count\"]}')
print(f'Schema version: {d[\"schema_version\"]}')
"
```
