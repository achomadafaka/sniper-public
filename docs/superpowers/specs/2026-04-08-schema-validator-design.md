# Schema Reference Validator

## Problem

Agents write code against hallucinated column names. The brain index (`index/schema_registry.json`) provides authoritative schema facts, but nothing enforces that code actually matches those facts. Plans, test fixtures, and SQL snippets ship with fake columns — caught only when a human spots them or tests crash at runtime.

A second failure mode: `schema_registry.json` itself drifts. Someone adds a column via `_ensure_column()` or a new `ensure_*_tables()` function, commits the code, but doesn't regenerate the index. The validator then checks references against stale truth.

Both are the same root cause: no structural enforcement that the brain index is authoritative.

## Solution

One script (`scripts/validate_schema_refs.py`) that scans files for SQL column references and validates them against `index/schema_registry.json`. One shared helper module (`scripts/sql_ref_utils.py`) for SQL/string extraction logic reused by both the validator and brain index. One CI workflow (`.github/workflows/schema-validate.yml`) that regenerates the index, fails if stale, then runs the validator. One optional pre-commit hook for local convenience.

## CI Workflow

**File:** `.github/workflows/schema-validate.yml`

**Triggers:** push to `main`, pull requests targeting `main`.

**Single job, three steps in order:**

1. **Regenerate** — `python3 scripts/brain_index.py generate`
2. **Staleness check** — `git diff --exit-code index/schema_registry.json index/dependency_index.json`. If diff is non-empty, fail with message: "Generated index artifacts are stale. Run `python3 scripts/brain_index.py generate` and commit."
3. **Reference validation** — `python3 scripts/validate_schema_refs.py`. Exit 1 if any violations found.

Does NOT touch `index/pattern_registry.yaml` — that file is hand-curated.

## Validator Script

**File:** `scripts/validate_schema_refs.py`

### Input

Reads `index/schema_registry.json` for the set of known tables and their columns.

### Scan Scope

| Scope | Files | Method |
|-------|-------|--------|
| Test fixtures | `tests/**/*.py` | AST-parse, extract SQL string literals |
| Plans | `docs/superpowers/plans/*.md` | Extract fenced Python/SQL code blocks, parse SQL |
| Specs | `docs/superpowers/specs/*.md` | Extract fenced Python/SQL code blocks, parse SQL |
| Application code | `*.py` in repo root + subdirs (excl. tests, venv, archive, __pycache__, index) | AST-parse, extract SQL string literals |

### SQL Patterns Detected

For each SQL string literal found, extract table-column pairs:

| Pattern | Extraction |
|---------|-----------|
| `INSERT INTO <table> (<col1>, <col2>, ...)` | table + column list |
| `SELECT <col1>, <col2> FROM <table>` | table + column list (skip `*`, expressions, aliases) |
| `UPDATE <table> SET <col> = ...` | table + column |
| `ORDER BY <col>` (when FROM table is known) | table + column |
| `WHERE <col> ...` (when FROM table is known) | table + column |

Skipped:
- `CREATE TABLE` / `CREATE INDEX` — definitions, not references
- `ALTER TABLE` — schema changes, not references
- Function calls in SELECT (e.g., `COUNT(*)`, `MAX(col)`) — extract inner column only
- Subqueries — not parsed in v1; only the outermost FROM table is tracked
- `SELECT *` — no column list to validate
- Multi-table JOINs — skipped entirely in v1; only single-table queries are validated

### What It Does NOT Catch (v1)

- Python dict access (`row["column_name"]`)
- JavaScript property access (`o.column_name`)
- Natural language column mentions in markdown prose
- SQL in non-Python, non-markdown files
- Multi-table JOINs with ambiguous column references

### Output

```
FAIL: tests/test_stink_bid_api.py:164
  INSERT INTO paper_limit_orders: unknown column 'side'
FAIL: tests/test_stink_bid_api.py:164
  INSERT INTO paper_limit_orders: unknown column 'match_slug'
FAIL: docs/superpowers/plans/2026-04-08-stink-bid-deploy.md:298
  SELECT FROM paper_limit_fills: unknown column 'filled_at'

3 schema violations found
```

Exit code: 0 if clean, 1 if violations found.

### Markdown Code Block Extraction

For `.md` files, extract content from fenced code blocks tagged as `python`, `sql`, or `py`:

<!-- schema:external=table -->
````
```python
conn.execute("INSERT INTO table (col1, col2) VALUES (?, ?)", ...)
```
````

For fenced `sql` blocks, parse the block content directly as SQL text.

For fenced `python` / `py` blocks:
- first try AST-based string literal extraction, matching the `.py` file path
- if AST parse fails but the block contains SQL-like text (`SELECT`, `INSERT`, `UPDATE`, `DELETE`, `REPLACE`), fall back to a simpler SQL-string regex extractor over the raw block
- if AST parse fails and no SQL-like text is present, skip the block

Unfenced prose is ignored entirely.

## Pre-Commit Hook

**Optional local convenience.** Not required — CI is the enforcement point.

A shell script at `scripts/pre-commit-schema-check.sh` that:
1. Gets list of staged `.py` and `.md` files
2. Runs `python3 scripts/validate_schema_refs.py --files <staged-files>` (validator accepts an optional `--files` flag to limit scan scope)
3. Exits non-zero if violations found

Installation: `cp scripts/pre-commit-schema-check.sh .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit`

Documented in CLAUDE.md. Not auto-installed.

## Shared Utilities

Move the shared SQL/string extraction helpers into `scripts/sql_ref_utils.py`:
- `sql_string_literals(content)` — AST-based string literal extraction (skips docstrings)
- `extract_markdown_code_blocks(content)` — fenced block extraction for `python`, `py`, `sql`
- `looks_like_sql(text)` — lightweight SQL-text detector for fallback mode

Both `brain_index.py` and `validate_schema_refs.py` import from this module. This keeps the validator from depending on private helpers inside another CLI script.

## Files

| Action | File | Purpose |
|--------|------|---------|
| Create | `scripts/validate_schema_refs.py` | Validator script (~200 lines) |
| Create | `scripts/sql_ref_utils.py` | Shared SQL/string extraction helpers |
| Create | `scripts/pre-commit-schema-check.sh` | Optional pre-commit hook |
| Create | `.github/workflows/schema-validate.yml` | CI workflow |
| Create | `tests/test_validate_schema_refs.py` | Tests for the validator |
| Modify | `scripts/brain_index.py` | Import shared extraction helpers instead of local copy |
| Modify | `CLAUDE.md` | Document the validator and hook |

## Success Condition

The stink bid deploy plan (with its original 11 wrong columns) would have failed CI before any agent could execute it. "Brain index done" means this check is in the path.
