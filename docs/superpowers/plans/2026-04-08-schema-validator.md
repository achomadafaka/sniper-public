# Schema Reference Validator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enforce that all SQL column references in tests, plans, and code match the brain index schema registry — with CI as the authority and pre-commit as optional convenience.

**Architecture:** One shared helper module (`scripts/sql_ref_utils.py`) for AST-based string extraction and markdown code block parsing. One validator script (`scripts/validate_schema_refs.py`) that loads `index/schema_registry.json`, scans files for SQL patterns, and exits non-zero on unknown columns. One CI workflow that regenerates the index, fails if stale, then runs the validator.

**Tech Stack:** Python 3, `ast` module, regex, GitHub Actions, PyYAML

---

## File Structure

| Action | File | Purpose |
|--------|------|---------|
| Create | `scripts/sql_ref_utils.py` | Shared SQL/string extraction helpers |
| Create | `scripts/validate_schema_refs.py` | Schema reference validator (~250 lines) |
| Create | `tests/test_sql_ref_utils.py` | Tests for shared helpers |
| Create | `tests/test_validate_schema_refs.py` | Tests for validator |
| Create | `.github/workflows/schema-validate.yml` | CI workflow |
| Create | `scripts/pre-commit-schema-check.sh` | Optional pre-commit hook |
| Modify | `scripts/brain_index.py` | Import from sql_ref_utils instead of local functions |
| Modify | `CLAUDE.md` | Document validator and hook |

---

### Task 1: Shared Helper Module

**Files:**
- Create: `scripts/sql_ref_utils.py`
- Create: `tests/test_sql_ref_utils.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_sql_ref_utils.py`:

<!-- schema-validator: ignore-next-block -->
```python
"""Tests for sql_ref_utils — shared SQL/string extraction helpers."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


def test_sql_string_literals_extracts_strings():
    from sql_ref_utils import sql_string_literals

    code = '''
x = "SELECT * FROM foo"
conn.execute("INSERT INTO bar (a, b) VALUES (?, ?)")
'''
    result = sql_string_literals(code)
    texts = [text for text, _lineno in result]
    assert "SELECT * FROM foo" in texts
    assert "INSERT INTO bar (a, b) VALUES (?, ?)" in texts


def test_sql_string_literals_skips_docstrings():
    from sql_ref_utils import sql_string_literals

    code = '''
def foo():
    """INSERT INTO fake_table (col) VALUES (1)"""
    return "SELECT * FROM real_table"
'''
    result = sql_string_literals(code)
    texts = [text for text, _lineno in result]
    assert "SELECT * FROM real_table" in texts
    assert not any("fake_table" in t for t in texts)


def test_sql_string_literals_includes_line_numbers():
    from sql_ref_utils import sql_string_literals

    code = 'x = "hello"\ny = "world"\n'
    result = sql_string_literals(code)
    assert len(result) >= 2
    for text, lineno in result:
        assert isinstance(lineno, int)
        assert lineno >= 1


def test_sql_string_literals_handles_fstrings():
    from sql_ref_utils import sql_string_literals

    code = 'x = f"SELECT {col} FROM my_table WHERE id = {val}"\n'
    result = sql_string_literals(code)
    texts = [text for text, _lineno in result]
    assert any("my_table" in t for t in texts)


def test_sql_string_literals_handles_syntax_error():
    from sql_ref_utils import sql_string_literals

    result = sql_string_literals("def broken(:\n")
    assert result == []


def test_extract_markdown_code_blocks_python():
    from sql_ref_utils import extract_markdown_code_blocks

    md = '''# Heading

Some text.

```python
conn.execute("INSERT INTO foo (a) VALUES (?)")
```

More text.

```javascript
console.log("ignored");
```
'''
    blocks = extract_markdown_code_blocks(md)
    assert len(blocks) == 1
    assert blocks[0][0] == "python"
    assert "INSERT INTO foo" in blocks[0][1]


def test_extract_markdown_code_blocks_sql():
    from sql_ref_utils import extract_markdown_code_blocks

    md = '''```sql
INSERT INTO bar (x, y) VALUES (1, 2);
```
'''
    blocks = extract_markdown_code_blocks(md)
    assert len(blocks) == 1
    assert blocks[0][0] == "sql"
    assert "INSERT INTO bar" in blocks[0][1]


def test_extract_markdown_code_blocks_with_line_offset():
    from sql_ref_utils import extract_markdown_code_blocks

    md = '''line1
line2
```python
code here
```
'''
    blocks = extract_markdown_code_blocks(md)
    assert len(blocks) == 1
    lang, content, start_line = blocks[0]
    assert start_line == 4  # code starts after the ``` line


def test_looks_like_sql_positive():
    from sql_ref_utils import looks_like_sql

    assert looks_like_sql("INSERT INTO foo (a) VALUES (1)")
    assert looks_like_sql("SELECT x FROM bar")
    assert looks_like_sql("UPDATE baz SET x = 1")
    assert looks_like_sql("delete from qux where id = 1")


def test_looks_like_sql_negative():
    from sql_ref_utils import looks_like_sql

    assert not looks_like_sql("just a regular string")
    assert not looks_like_sql("x = 42")
    assert not looks_like_sql("")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/bigpapi/Desktop/Sniper && python3 -m pytest tests/test_sql_ref_utils.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sql_ref_utils'`

- [ ] **Step 3: Implement sql_ref_utils.py**

Create `scripts/sql_ref_utils.py`:

```python
"""Shared SQL/string extraction helpers for brain index and schema validator.

Used by:
- scripts/brain_index.py (dependency extraction)
- scripts/validate_schema_refs.py (column reference validation)
"""

from __future__ import annotations

import ast
import re


def sql_string_literals(content: str) -> list[tuple[str, int]]:
    """AST-parse Python source, return non-docstring string literals with line numbers.

    Returns list of (literal_text, lineno) tuples.  Skips docstrings at the
    start of modules, classes, and functions.  Handles f-strings by extracting
    the static text portions.
    """
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return []

    docstring_ids: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body:
            first = body[0]
            if isinstance(first, ast.Expr):
                value = first.value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    docstring_ids.add(id(value))
                elif isinstance(value, ast.JoinedStr):
                    docstring_ids.add(id(value))

    literals: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if id(node) in docstring_ids:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            literals.append((node.value, getattr(node, "lineno", 0)))
        elif isinstance(node, ast.JoinedStr):
            static_text = "".join(
                part.value
                for part in node.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            )
            if static_text:
                literals.append((static_text, getattr(node, "lineno", 0)))
    return literals


def extract_markdown_code_blocks(content: str) -> list[tuple[str, str, int]]:
    """Extract fenced code blocks from markdown.

    Returns list of (language, code_content, start_line) tuples.
    Only extracts blocks tagged as python, py, or sql.
    start_line is 1-indexed and points to the first line of code
    (the line after the opening fence).
    """
    blocks: list[tuple[str, str, int]] = []
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```"):
            lang = stripped[3:].strip().lower()
            if lang in ("python", "py", "sql"):
                code_lines: list[str] = []
                code_start = i + 2  # 1-indexed, line after fence
                i += 1
                while i < len(lines) and not lines[i].strip().startswith("```"):
                    code_lines.append(lines[i])
                    i += 1
                blocks.append((lang, "\n".join(code_lines), code_start))
        i += 1
    return blocks


def looks_like_sql(text: str) -> bool:
    """Return True if text contains SQL-like keywords."""
    return bool(
        re.search(
            r"\b(?:SELECT|INSERT|UPDATE|DELETE|REPLACE)\b",
            text,
            re.IGNORECASE,
        )
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/bigpapi/Desktop/Sniper && python3 -m pytest tests/test_sql_ref_utils.py -v`
Expected: All 10 tests PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/sql_ref_utils.py tests/test_sql_ref_utils.py
git commit -m "feat: shared SQL/string extraction helpers for brain index + validator"
```

---

### Task 2: Refactor brain_index.py to Use Shared Helpers

**Files:**
- Modify: `scripts/brain_index.py`

- [ ] **Step 1: Replace local `_sql_string_literals` with import from sql_ref_utils**

In `scripts/brain_index.py`, remove the `_sql_string_literals` function (lines 213-243) and the `_db_imports` function (lines 246-260). Replace with imports from `sql_ref_utils`:

At the top of the file, after the existing imports, add:

```python
from sql_ref_utils import sql_string_literals
```

In `generate_dependency_index()`, replace the call `for literal in _sql_string_literals(content):` with:

```python
        for literal, _lineno in sql_string_literals(content):
```

For `_db_imports`, move it to `sql_ref_utils.py` as `db_imports(content)` — or keep it local since only brain_index uses it. Keeping it local is simpler since the spec only requires `sql_string_literals`, `extract_markdown_code_blocks`, and `looks_like_sql` in the shared module.

Decision: keep `_db_imports` local in brain_index.py. Only move the string literal extraction.

- [ ] **Step 2: Run brain_index tests to verify nothing broke**

Run: `cd /home/bigpapi/Desktop/Sniper && python3 -m pytest tests/test_brain_index.py -v`
Expected: All 16 tests PASS

- [ ] **Step 3: Run the generate command to verify it still works**

Run: `cd /home/bigpapi/Desktop/Sniper && python3 scripts/brain_index.py generate`
Expected: "61 tables", "Done."

- [ ] **Step 4: Commit**

```bash
git add scripts/brain_index.py
git commit -m "refactor: brain_index imports sql_string_literals from shared module"
```

---

### Task 3: Schema Reference Validator

**Files:**
- Create: `scripts/validate_schema_refs.py`
- Create: `tests/test_validate_schema_refs.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_validate_schema_refs.py`:

```python
"""Tests for validate_schema_refs — schema column reference validator."""

import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

# Minimal schema for testing — two tables with known columns
TEST_SCHEMA = {
    "paper_limit_orders": {
        "order_id", "strategy_id", "condition_id", "market_title",
        "event_slug", "favorite_token_id", "favorite_token_label",
        "favorite_price_at_entry", "bid_price", "dip_pct", "min_fill_size",
        "state", "cancel_reason", "placed_at", "updated_at", "closed_at",
        "espn_event_id", "tour", "player1", "player2",
    },
    "paper_limit_fills": {
        "fill_id", "order_id", "fill_label", "trade_price", "trade_size",
        "trade_side", "trade_token_id", "trade_tx_hash", "trade_ts",
        "bid_price", "slippage", "created_at",
    },
    "user_analysis": {
        "wallet", "analyzed_ts", "position_count", "pnl",
        "insider_score", "classification",
    },
}

KNOWN_TABLES = set(TEST_SCHEMA.keys())


def test_extract_insert_valid():
    from validate_schema_refs import extract_sql_column_refs

    sql = "INSERT INTO paper_limit_orders (strategy_id, condition_id, bid_price) VALUES (?, ?, ?)"
    refs = extract_sql_column_refs(sql, KNOWN_TABLES)
    tables = {t for t, c in refs}
    cols = {c for t, c in refs}
    assert "paper_limit_orders" in tables
    assert "strategy_id" in cols
    assert "bid_price" in cols


def test_extract_insert_unknown_column():
    from validate_schema_refs import extract_sql_column_refs, validate_refs

    sql = "INSERT INTO paper_limit_orders (strategy_id, side, match_slug) VALUES (?, ?, ?)"
    refs = extract_sql_column_refs(sql, KNOWN_TABLES)
    violations = validate_refs(refs, TEST_SCHEMA)
    bad_cols = {v.column for v in violations}
    assert "side" in bad_cols
    assert "match_slug" in bad_cols
    assert "strategy_id" not in bad_cols


def test_extract_insert_or_replace():
    from validate_schema_refs import extract_sql_column_refs

    sql = "INSERT OR REPLACE INTO user_analysis (wallet, pnl) VALUES (?, ?)"
    refs = extract_sql_column_refs(sql, KNOWN_TABLES)
    cols = {c for t, c in refs}
    assert "wallet" in cols
    assert "pnl" in cols


def test_extract_select_from():
    from validate_schema_refs import extract_sql_column_refs

    sql = "SELECT wallet, insider_score FROM user_analysis WHERE wallet = ?"
    refs = extract_sql_column_refs(sql, KNOWN_TABLES)
    cols = {c for t, c in refs}
    assert "wallet" in cols
    assert "insider_score" in cols


def test_extract_select_star_skipped():
    from validate_schema_refs import extract_sql_column_refs

    sql = "SELECT * FROM user_analysis"
    refs = extract_sql_column_refs(sql, KNOWN_TABLES)
    assert len(refs) == 0


def test_extract_update_set():
    from validate_schema_refs import extract_sql_column_refs

    sql = "UPDATE paper_limit_orders SET state = ?, updated_at = ? WHERE order_id = ?"
    refs = extract_sql_column_refs(sql, KNOWN_TABLES)
    cols = {c for t, c in refs}
    assert "state" in cols
    assert "updated_at" in cols


def test_skip_create_table():
    from validate_schema_refs import extract_sql_column_refs

    sql = "CREATE TABLE IF NOT EXISTS fake_table (fake_col TEXT)"
    refs = extract_sql_column_refs(sql, KNOWN_TABLES)
    assert len(refs) == 0


def test_skip_join():
    from validate_schema_refs import extract_sql_column_refs

    sql = "SELECT a.wallet FROM user_analysis a JOIN paper_limit_orders b ON a.wallet = b.condition_id"
    refs = extract_sql_column_refs(sql, KNOWN_TABLES)
    assert len(refs) == 0


def test_skip_unknown_table():
    from validate_schema_refs import extract_sql_column_refs

    sql = "INSERT INTO nonexistent_table (col1, col2) VALUES (?, ?)"
    refs = extract_sql_column_refs(sql, KNOWN_TABLES)
    assert len(refs) == 0


def test_multiline_insert():
    from validate_schema_refs import extract_sql_column_refs

    sql = textwrap.dedent("""\
        INSERT INTO paper_limit_orders
            (strategy_id, condition_id, market_title,
             favorite_token_id, bid_price)
        VALUES (?, ?, ?, ?, ?)
    """)
    refs = extract_sql_column_refs(sql, KNOWN_TABLES)
    cols = {c for t, c in refs}
    assert "strategy_id" in cols
    assert "market_title" in cols
    assert "bid_price" in cols


def test_validate_py_file(tmp_path):
    from validate_schema_refs import validate_file

    py_file = tmp_path / "test_example.py"
    py_file.write_text(textwrap.dedent('''\
        import sqlite3
        conn.execute("""
            INSERT INTO paper_limit_orders
            (strategy_id, side, match_slug)
            VALUES (?, ?, ?)
        """)
    '''))
    violations = validate_file(str(py_file), TEST_SCHEMA, KNOWN_TABLES)
    bad_cols = {v.column for v in violations}
    assert "side" in bad_cols
    assert "match_slug" in bad_cols


def test_validate_md_file(tmp_path):
    from validate_schema_refs import validate_file

    md_file = tmp_path / "plan.md"
    md_file.write_text(textwrap.dedent('''\
        # Plan

        <!-- schema-validator: ignore-next-block -->

        ```python
        conn.execute("INSERT INTO paper_limit_fills (order_id, filled_at) VALUES (?, ?)")
        ```
    '''))
    violations = validate_file(str(md_file), TEST_SCHEMA, KNOWN_TABLES)
    bad_cols = {v.column for v in violations}
    assert "filled_at" in bad_cols


def test_validate_md_sql_block(tmp_path):
    from validate_schema_refs import validate_file

    md_file = tmp_path / "spec.md"
    md_file.write_text(textwrap.dedent('''\
        # Spec

        <!-- schema-validator: ignore-next-block -->

        ```sql
        INSERT INTO paper_limit_fills (order_id, filled_at) VALUES (1, 'now');
        ```
    '''))
    violations = validate_file(str(md_file), TEST_SCHEMA, KNOWN_TABLES)
    bad_cols = {v.column for v in violations}
    assert "filled_at" in bad_cols


def test_validate_clean_file(tmp_path):
    from validate_schema_refs import validate_file

    py_file = tmp_path / "clean.py"
    py_file.write_text(textwrap.dedent('''\
        conn.execute("INSERT INTO paper_limit_orders (strategy_id, bid_price) VALUES (?, ?)")
    '''))
    violations = validate_file(str(py_file), TEST_SCHEMA, KNOWN_TABLES)
    assert len(violations) == 0


def test_validate_md_fallback_on_invalid_python(tmp_path):
    """Fenced python block that fails AST parse but contains SQL should still be checked."""
    from validate_schema_refs import validate_file

    md_file = tmp_path / "plan.md"
    md_file.write_text(textwrap.dedent('''\
        # Plan

        <!-- schema-validator: ignore-next-block -->

        ```python
        conn.execute("""INSERT INTO paper_limit_orders
            (strategy_id, fake_column) VALUES (?, ?)""")
        # incomplete block, missing closing paren for some context
        ```
    '''))
    violations = validate_file(str(md_file), TEST_SCHEMA, KNOWN_TABLES)
    bad_cols = {v.column for v in violations}
    assert "fake_column" in bad_cols
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/bigpapi/Desktop/Sniper && python3 -m pytest tests/test_validate_schema_refs.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'validate_schema_refs'`

- [ ] **Step 3: Implement the validator**

Create `scripts/validate_schema_refs.py`:

<!-- schema-validator: ignore-next-block -->
```python
#!/usr/bin/env python3
"""Schema Reference Validator — checks SQL column references against brain index.

Scans test fixtures, plans/specs, and application code for SQL column references
and validates them against index/schema_registry.json.

Usage:
    python scripts/validate_schema_refs.py              # scan all files
    python scripts/validate_schema_refs.py --files a.py b.md  # scan specific files
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from sql_ref_utils import extract_markdown_code_blocks, looks_like_sql, sql_string_literals

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_FILE = REPO_ROOT / "index" / "schema_registry.json"

_SKIP_DIRS = frozenset(
    {".venv", "venv", "__pycache__", "node_modules", ".git", "archive", "index"}
)

# Matches INSERT INTO [OR REPLACE|OR IGNORE] table (col1, col2, ...)
_INSERT_RE = re.compile(
    r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+(\w+)\s*\(([^)]+)\)",
    re.IGNORECASE | re.DOTALL,
)

# Matches SELECT col1, col2 FROM table
_SELECT_RE = re.compile(
    r"SELECT\s+(.+?)\s+FROM\s+(\w+)",
    re.IGNORECASE | re.DOTALL,
)

# Matches UPDATE table SET col1 = ..., col2 = ...
_UPDATE_RE = re.compile(
    r"UPDATE\s+(\w+)\s+SET\s+(.+?)(?:\s+WHERE\b|\s*$|\s*;)",
    re.IGNORECASE | re.DOTALL,
)


@dataclass
class Violation:
    file: str
    line: int
    table: str
    column: str
    pattern: str


def load_schema(path: Path | str) -> dict[str, set[str]]:
    """Load schema_registry.json, return {table_name: {col_names}}."""
    with open(path) as f:
        registry = json.load(f)
    return {
        table: {c["name"] for c in info["columns"]}
        for table, info in registry["tables"].items()
    }


def extract_sql_column_refs(sql: str, known_tables: set[str]) -> list[tuple[str, str]]:
    """Extract (table, column) pairs from a SQL string.

    Only handles single-table queries. Skips DDL and JOINs.
    """
    refs: list[tuple[str, str]] = []

    # Skip DDL
    if re.match(r"\s*(CREATE|ALTER|DROP)\b", sql, re.IGNORECASE):
        return refs

    # Skip JOINs — v1 only validates single-table queries
    if re.search(r"\bJOIN\b", sql, re.IGNORECASE):
        return refs

    # INSERT INTO table (col1, col2, ...)
    m = _INSERT_RE.search(sql)
    if m:
        table = m.group(1)
        if table in known_tables:
            for col in m.group(2).split(","):
                col = col.strip().strip('"').strip("'").strip()
                if col:
                    refs.append((table, col))
        return refs

    # SELECT cols FROM table
    m = _SELECT_RE.search(sql)
    if m:
        cols_str = m.group(1).strip()
        table = m.group(2)
        if table in known_tables and cols_str != "*":
            for part in cols_str.split(","):
                part = part.strip()
                if not part or part == "*" or "(" in part:
                    continue
                # Handle "col AS alias" — take the first word
                col = part.split()[0].strip()
                # Handle "table.col"
                if "." in col:
                    col = col.split(".")[-1]
                if col:
                    refs.append((table, col))

    # UPDATE table SET col = ...
    m = _UPDATE_RE.search(sql)
    if m:
        table = m.group(1)
        if table in known_tables:
            for assignment in m.group(2).split(","):
                col = assignment.split("=")[0].strip().strip('"').strip("'").strip()
                if col:
                    refs.append((table, col))

    return refs


def validate_refs(
    refs: list[tuple[str, str]], schema: dict[str, set[str]]
) -> list[Violation]:
    """Check extracted refs against schema, return violations."""
    violations: list[Violation] = []
    for table, col in refs:
        if table in schema and col not in schema[table]:
            violations.append(
                Violation(file="", line=0, table=table, column=col, pattern="")
            )
    return violations


def _extract_refs_from_sql_text(
    sql: str, known_tables: set[str], file: str, line_offset: int
) -> list[Violation]:
    """Extract and validate column refs from raw SQL text."""
    refs = extract_sql_column_refs(sql, known_tables)
    return [
        Violation(file=file, line=line_offset, table=t, column=c, pattern="SQL")
        for t, c in refs
    ]


def validate_file(
    file_path: str,
    schema: dict[str, set[str]],
    known_tables: set[str],
) -> list[Violation]:
    """Scan one file for schema violations."""
    path = Path(file_path)
    violations: list[Violation] = []

    try:
        content = path.read_text(errors="replace")
    except OSError:
        return violations

    if path.suffix == ".py":
        for literal, lineno in sql_string_literals(content):
            refs = extract_sql_column_refs(literal, known_tables)
            for table, col in refs:
                if col not in schema.get(table, set()):
                    violations.append(
                        Violation(
                            file=file_path,
                            line=lineno,
                            table=table,
                            column=col,
                            pattern="SQL in .py",
                        )
                    )

    elif path.suffix == ".md":
        for lang, block_content, start_line in extract_markdown_code_blocks(content):
            if lang == "sql":
                # Parse SQL directly
                refs = extract_sql_column_refs(block_content, known_tables)
                for table, col in refs:
                    if col not in schema.get(table, set()):
                        violations.append(
                            Violation(
                                file=file_path,
                                line=start_line,
                                table=table,
                                column=col,
                                pattern="SQL block in .md",
                            )
                        )
            elif lang in ("python", "py"):
                # Try AST first
                extracted = sql_string_literals(block_content)
                if extracted:
                    for literal, rel_lineno in extracted:
                        refs = extract_sql_column_refs(literal, known_tables)
                        for table, col in refs:
                            if col not in schema.get(table, set()):
                                violations.append(
                                    Violation(
                                        file=file_path,
                                        line=start_line + rel_lineno - 1,
                                        table=table,
                                        column=col,
                                        pattern="Python block in .md",
                                    )
                                )
                elif looks_like_sql(block_content):
                    # AST failed but block contains SQL — regex fallback
                    refs = extract_sql_column_refs(block_content, known_tables)
                    for table, col in refs:
                        if col not in schema.get(table, set()):
                            violations.append(
                                Violation(
                                    file=file_path,
                                    line=start_line,
                                    table=table,
                                    column=col,
                                    pattern="Python/SQL block in .md (fallback)",
                                )
                            )

    return violations


def collect_files(file_list: list[str] | None = None) -> list[str]:
    """Collect files to scan. If file_list given, use those. Otherwise scan repo."""
    if file_list:
        return file_list

    files: list[str] = []
    for py_file in sorted(REPO_ROOT.rglob("*.py")):
        rel = py_file.relative_to(REPO_ROOT)
        if any(p in _SKIP_DIRS for p in rel.parts):
            continue
        files.append(str(py_file))

    for md_pattern in ("docs/superpowers/plans/*.md", "docs/superpowers/specs/*.md"):
        for md_file in sorted(REPO_ROOT.glob(md_pattern)):
            files.append(str(md_file))

    return files


def main() -> int:
    if not SCHEMA_FILE.exists():
        print(f"Schema registry not found: {SCHEMA_FILE}")
        print("Run: python scripts/brain_index.py generate")
        return 1

    schema = load_schema(SCHEMA_FILE)
    known_tables = set(schema.keys())

    # Parse --files flag
    file_list: list[str] | None = None
    if "--files" in sys.argv:
        idx = sys.argv.index("--files")
        file_list = sys.argv[idx + 1:]

    files = collect_files(file_list)
    all_violations: list[Violation] = []

    for f in files:
        all_violations.extend(validate_file(f, schema, known_tables))

    if all_violations:
        for v in all_violations:
            # Show path relative to repo root if possible
            try:
                rel = str(Path(v.file).relative_to(REPO_ROOT))
            except ValueError:
                rel = v.file
            print(f"FAIL: {rel}:{v.line}")
            print(f"  {v.table}: unknown column '{v.column}'")
        print(f"\n{len(all_violations)} schema violation(s) found")
        return 1

    print(f"OK: {len(files)} files scanned, no schema violations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/bigpapi/Desktop/Sniper && python3 -m pytest tests/test_validate_schema_refs.py -v`
Expected: All 16 tests PASS

- [ ] **Step 5: Run the validator against the repo to see current state**

Run: `cd /home/bigpapi/Desktop/Sniper && python3 scripts/validate_schema_refs.py`
Expected: Either "OK" or a list of violations in existing code (which we will triage — not all may be real bugs)

- [ ] **Step 6: Commit**

```bash
git add scripts/validate_schema_refs.py tests/test_validate_schema_refs.py
git commit -m "feat: schema reference validator — catches hallucinated column names in SQL"
```

---

### Task 4: CI Workflow

**Files:**
- Create: `.github/workflows/schema-validate.yml`

- [ ] **Step 1: Create the workflow**

Create `.github/workflows/schema-validate.yml`:

```yaml
name: Schema Validation

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  schema-check:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.13"

      - name: Install dependencies
        run: |
          pip install -r requirements.txt
          pip install pyyaml

      - name: Regenerate brain index artifacts
        run: python scripts/brain_index.py generate

      - name: Check for stale index artifacts
        run: |
          if ! git diff --quiet index/schema_registry.json index/dependency_index.json; then
            echo "::error::Generated index artifacts are stale."
            echo "Run 'python scripts/brain_index.py generate' locally and commit the results."
            git diff --stat index/schema_registry.json index/dependency_index.json
            exit 1
          fi
          echo "Index artifacts are fresh."

      - name: Validate schema references
        run: python scripts/validate_schema_refs.py
```

- [ ] **Step 2: Verify YAML syntax**

Run: `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/schema-validate.yml')); print('YAML OK')"`

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/schema-validate.yml
git commit -m "ci: add schema validation workflow — staleness check + reference validator"
```

---

### Task 5: Pre-Commit Hook + CLAUDE.md

**Files:**
- Create: `scripts/pre-commit-schema-check.sh`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Create the pre-commit hook script**

Create `scripts/pre-commit-schema-check.sh`:

```bash
#!/usr/bin/env bash
# Optional pre-commit hook for schema reference validation.
# Runs the same validator as CI, but only on staged files.
#
# Install:
#   cp scripts/pre-commit-schema-check.sh .git/hooks/pre-commit
#   chmod +x .git/hooks/pre-commit

set -euo pipefail

STAGED_FILES=$(git diff --cached --name-only --diff-filter=ACM | grep -E '\.(py|md)$' || true)

if [ -z "$STAGED_FILES" ]; then
    exit 0
fi

echo "Schema reference check on staged files..."
# shellcheck disable=SC2086
python3 scripts/validate_schema_refs.py --files $STAGED_FILES
```

- [ ] **Step 2: Make it executable**

Run: `chmod +x scripts/pre-commit-schema-check.sh`

- [ ] **Step 3: Add to CLAUDE.md**

Find the Brain Index section in CLAUDE.md (after "### Brain Index — authoritative schema/pattern/dependency lookup") and append after the existing content:

```markdown
### Schema Reference Validator — CI-enforced column correctness

CI runs `scripts/validate_schema_refs.py` on every push/PR to main. It catches unknown column names in SQL statements across tests, plans, specs, and application code.

```bash
# Run locally (same check as CI)
python3 scripts/validate_schema_refs.py

# Run on specific files only
python3 scripts/validate_schema_refs.py --files tests/test_foo.py docs/superpowers/plans/my-plan.md
```

**CI also checks index freshness:** if `index/schema_registry.json` or `index/dependency_index.json` are stale (don't match what `brain_index.py generate` produces), CI fails before the validator even runs.

**Optional pre-commit hook** (runs validator on staged files only):
```bash
cp scripts/pre-commit-schema-check.sh .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit
```
```

- [ ] **Step 4: Run full test suite**

Run: `cd /home/bigpapi/Desktop/Sniper && python3 -m pytest tests/ -q`
Expected: All tests pass (2400+), including the new sql_ref_utils and validate_schema_refs tests.

- [ ] **Step 5: Commit**

```bash
git add scripts/pre-commit-schema-check.sh CLAUDE.md
git commit -m "feat: optional pre-commit hook + CLAUDE.md docs for schema validator"
```
