# Brain Index: Repo-Local Engineering Index

## Problem

AI agents hallucinate schema details — column names, table structures, function signatures. Example: an explore agent reported `paper_limit_orders` had columns that don't exist, causing a broken implementation plan that wasted an entire implementation cycle.

Current mitigations (hand-written table lists in CLAUDE.md) don't scale and go stale. The 40+ tables and 200+ columns in Sniper's SQLite schema change regularly. When agents infer schema from reading `db.py` text, they guess wrong.

**Root cause:** agents use heuristic inference instead of precise fact sources.

## Solution

Three machine-readable artifacts generated from the actual codebase, plus a CLI query tool. Design philosophy from research:

- **Precise where facts matter** — schema from SQLite introspection, not text parsing
- **Curated where conventions matter** — patterns hand-written, not auto-extracted
- **Heuristic where convenience is enough** — dependencies from grep, not compiler analysis
- **Git is the source of truth** — artifacts committed to repo, not stored only in Supabase

### 1. Schema Registry (generated)

**Source:** SQLite `PRAGMA` introspection on in-memory DB created by `init_db()`.

**Output:** `index/schema_registry.json`

**Contents per table:**
- Column name, type, notnull, pk, default value
- Indexes (name, unique, columns)
- Foreign keys (from_column, to_table, to_column)

**Why init_db() introspection, not regex parsing of db.py:**
- `init_db()` calls `ensure_wallet_directory_schema()`, `ensure_all_hl_tables()`, `ensure_paper_limit_order_tables()`, and `apply_phase_b_hardening()` — so all tables are captured in one call
- `PRAGMA table_info` returns the actual schema, not what we think the schema is
- Immune to SQL formatting variations, multi-line strings, or conditional DDL

### 2. Pattern Registry (curated)

**Source:** Hand-written YAML.

**Output:** `index/pattern_registry.yaml`

**Contents:** 8 core patterns:

| Pattern | Purpose |
|---------|---------|
| `strategy_plugin` | How to extend `StrategyPlugin` from `strategies/base.py` |
| `flask_blueprint` | How route blueprints are structured in `routes/` |
| `db_module` | How `db_*.py` modules are organized and re-exported |
| `feed_adapter` | How `FeedAdapter` subclasses work in `feeds/` |
| `ensure_schema` | Idempotent migration pattern for new tables |
| `mcp_tool` | How MCP server tools are structured in `mcp_tools/` |
| `systemd_service` | How VPS systemd units are configured |
| `test_fixture` | How tests use `conftest.py` env pinning plus local in-memory DB helpers |

Each pattern includes: name, description, reference files, code example, notes.

**Why hand-curated, not auto-extracted:**
- Auto-extracting conventions is where hallucinations creep back in
- 8 patterns is small enough to maintain manually
- The value is in the _notes_ (gotchas, registration steps) which can't be auto-generated

### 3. Dependency Index (heuristic)

**Source:** Word-boundary grep of table names across all `.py` files, classified by SQL keyword context.

**Output:** `index/dependency_index.json`

**Contents:**
- `files`: per-file → `{reads_tables, writes_tables, imports}`
- `table_readers`: per-table → list of files that SELECT from it
- `table_writers`: per-table → list of files that INSERT/UPDATE/DELETE into it

**Classification heuristic:**
- Line contains `INSERT|UPDATE|DELETE|REPLACE` near table name → write
- Line contains `SELECT|FROM|JOIN` near table name → read
- Table name appears without SQL keyword → read (conservative default)

**Why grep-based, not AST/compiler:**
- All SQL in Sniper goes through `db.py` modules — table names appear as string literals
- Word-boundary matching on known table names has low false-positive rate
- Good enough for "who touches this table?" — the primary use case
- No external tooling (tree-sitter, CodeQL) required for v1

### 4. CLI Query Tool

**File:** `scripts/brain_index.py`

```
python scripts/brain_index.py generate          # regenerate all artifacts
python scripts/brain_index.py schema <table>    # lookup table schema
python scripts/brain_index.py schema --list     # list all tables
python scripts/brain_index.py pattern <name>    # lookup pattern
python scripts/brain_index.py pattern --list    # list all patterns
python scripts/brain_index.py deps <file>       # file dependencies
python scripts/brain_index.py deps --table <t>  # table readers/writers
```

Query commands read from pre-generated JSON/YAML files (fast). The `generate` command regenerates all artifacts. If files don't exist, query commands print an error with the generate command.

## Integration Points

1. **CLAUDE.md** — reference `index/` as authoritative schema source; agents should read `index/schema_registry.json` before writing code that touches DB tables
2. **Subagent prompts** — when dispatching explore/architect agents, include "read index/schema_registry.json for exact table schemas"
3. **Regeneration** — run `python scripts/brain_index.py generate` after any schema change (new table, new column, new ensure_* function)

## What This Is NOT

- **Not an embedding store** — no vectors, no semantic search
- **Not a semantic code graph** — no CodeQL, no full AST analysis
- **Not a Supabase-only store** — Git is canonical; Supabase sync is a future v3 option
- **Not a replacement for reading code** — it's a fast lookup for facts that agents get wrong

## Future Versions

**v2:** Tree-sitter extraction for Python function signatures and route definitions. Better file-to-table dependency extraction via AST analysis of `db.py` function bodies.

**v3:** Optional sync of generated artifacts into Supabase `memories` table for remote agent lookup. Git-versioned artifacts remain canonical.

## Files

| File | Type | Purpose |
|------|------|---------|
| `scripts/brain_index.py` | New | CLI tool + all generation logic |
| `index/schema_registry.json` | Generated | Full SQLite schema |
| `index/dependency_index.json` | Generated | File→table dependency map |
| `index/pattern_registry.yaml` | Curated | Core code patterns |
| `tests/test_brain_index.py` | New | Tests for generation + queries |
