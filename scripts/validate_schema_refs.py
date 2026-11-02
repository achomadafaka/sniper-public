"""Schema reference validator.

Scans Python and Markdown files for SQL column references and validates them
against ``index/schema_registry.json``.

Usage::

    python3 scripts/validate_schema_refs.py [--files file1.py file2.md ...]

Exits 0 when no violations are found, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "index" / "schema_registry.json"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
from sql_ref_utils import (  # noqa: E402
    extract_markdown_code_blocks,
    looks_like_sql,
    sql_string_literals,
)

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class Violation:
    file: str
    line: int
    table: str
    column: str
    pattern: str


# ---------------------------------------------------------------------------
# SQL column reference extraction
# ---------------------------------------------------------------------------

# A valid bare SQL column name: starts with letter/underscore, alphanumeric+underscore
_BARE_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# SQL keywords that can appear as the first word in a fragment — skip these tokens
_SQL_KEYWORDS = frozenset({
    "SELECT", "INSERT", "UPDATE", "DELETE", "FROM", "WHERE", "AND", "OR",
    "NOT", "IN", "LIKE", "IS", "NULL", "TRUE", "FALSE", "CASE", "WHEN",
    "THEN", "ELSE", "END", "AS", "BY", "ON", "SET", "INTO", "VALUES",
    "DISTINCT", "ALL", "EXISTS", "BETWEEN", "HAVING", "GROUP", "ORDER",
    "LIMIT", "OFFSET", "WITH", "UNION", "EXCEPT", "INTERSECT", "INNER",
    "LEFT", "RIGHT", "OUTER", "FULL", "CROSS", "NATURAL", "JOIN", "USING",
    "COUNT", "SUM", "AVG", "MIN", "MAX", "COALESCE", "NULLIF", "IFNULL",
    "IIF", "CAST", "TYPEOF", "REPLACE", "IGNORE", "OR", "ROWID", "OID",
    "RETURNING", "DO", "NOTHING",
})

# Regex to capture INSERT [OR REPLACE|OR IGNORE] INTO <table> (<col-list>)
# Stops the column list at the first ) — does NOT use [^)]+ to avoid greedily
# consuming nested parens in VALUES clauses.
_INSERT_RE = re.compile(
    r"\bINSERT\s+(?:OR\s+\w+\s+)?INTO\s+(\w+)\s*\(([^)]+)\)",
    re.IGNORECASE | re.DOTALL,
)

# Regex to capture SELECT <cols> FROM <table>
# DOTALL but lazy — stops at FROM.
_SELECT_RE = re.compile(
    r"\bSELECT\s+(.*?)\s+FROM\s+(\w+)\b",
    re.IGNORECASE | re.DOTALL,
)

# Regex to capture UPDATE <table> SET col = ...[, col = ...]
_UPDATE_RE = re.compile(
    r"\bUPDATE\s+(\w+)\s+SET\s+(.+?)(?:\s+WHERE|\s*;|$)",
    re.IGNORECASE | re.DOTALL,
)

# DDL patterns — skip entirely
_DDL_RE = re.compile(
    r"\b(?:CREATE|ALTER|DROP)\s+(?:TABLE|INDEX|VIEW|TRIGGER)\b",
    re.IGNORECASE,
)

# JOIN pattern — skip statements containing a JOIN
_JOIN_RE = re.compile(r"\bJOIN\b", re.IGNORECASE)

# CTE definition pattern — matches both the first CTE (`WITH name AS (`) and
# chained CTEs (`), name AS (`). Used to whitelist CTE aliases as valid table
# references inside the same SQL block, preventing false-positive unknown-table
# warnings for inline query helpers.
_CTE_DEF_RE = re.compile(
    r"(?:\bWITH\s+(?:RECURSIVE\s+)?|,\s*)(\w+)\s+AS\s*\(",
    re.IGNORECASE,
)

# Function call in column fragment (e.g. COUNT(*), MAX(x), COALESCE(...))
_FUNC_RE = re.compile(r"\(")


def _is_plain_identifier(token: str) -> bool:
    """Return True only if *token* is a bare SQL column name (no keywords, no special chars)."""
    if not _BARE_IDENT_RE.match(token):
        return False
    if token.upper() in _SQL_KEYWORDS:
        return False
    return True


def _split_col_list(raw: str) -> list[str]:
    """Split a comma-separated column list and return only plain identifiers.

    Filters out: expressions with parens (functions/CASE), SQL keywords,
    qualified names that look like expressions, and anything that doesn't
    match ``[A-Za-z_][A-Za-z0-9_]*``.
    """
    cols = []
    for part in raw.split(","):
        # Skip fragments that contain open parens — they're expressions
        if _FUNC_RE.search(part):
            continue
        col = part.strip().strip("`\"'")
        if not col or col == "*":
            continue
        # Take only the first token (handles "col AS alias" or "DISTINCT col")
        tokens = col.split()
        if not tokens:
            continue
        first_token = tokens[0]
        # Skip DISTINCT keyword at front of SELECT col list
        if first_token.upper() == "DISTINCT" and len(tokens) > 1:
            first_token = tokens[1]
        # Strip table-qualifier prefix (table.col → col)
        if "." in first_token:
            first_token = first_token.split(".")[-1]
        if _is_plain_identifier(first_token):
            cols.append(first_token)
    return cols


def _split_update_set(set_clause: str) -> list[str]:
    """Extract LHS column names from an UPDATE SET clause.

    Handles ``col = value, col2 = COALESCE(...)`` by tokenising on ``=`` then
    picking the last word before the ``=`` sign.  Skips any fragment that
    contains a nested parenthesis on the LHS.
    """
    cols = []
    # Split on commas that are NOT inside parentheses (depth tracking)
    depth = 0
    current: list[str] = []
    assignments: list[str] = []
    for ch in set_clause:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            assignments.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        assignments.append("".join(current))

    for assignment in assignments:
        parts = assignment.split("=", 1)
        lhs = parts[0].strip().strip("`\"'")
        # LHS must be a plain identifier
        if _is_plain_identifier(lhs):
            cols.append(lhs)
    return cols


def extract_sql_column_refs(
    sql: str,
    known_tables: set[str],
) -> list[tuple[str, str]]:
    """Extract (table, column) pairs from a SQL string.

    Rules
    -----
    - DDL (CREATE/ALTER/DROP) → skip entirely
    - Statements containing JOIN → skip entirely
    - INSERT [OR …] INTO table (col, …) → extract table + columns
    - SELECT col, … FROM table → extract table + columns (skip *, functions)
    - UPDATE table SET col = … → extract table + columns
    - Unknown tables → return no refs for that statement
    - Tokens that are not plain SQL identifiers are silently dropped
    """
    refs: list[tuple[str, str]] = []

    # Skip DDL
    if _DDL_RE.search(sql):
        return []

    # Skip JOIN-containing statements
    if _JOIN_RE.search(sql):
        return []

    # INSERT / INSERT OR REPLACE / INSERT OR IGNORE
    for m in _INSERT_RE.finditer(sql):
        table = m.group(1)
        if table not in known_tables:
            continue
        for col in _split_col_list(m.group(2)):
            refs.append((table, col))

    # SELECT
    for m in _SELECT_RE.finditer(sql):
        col_raw = m.group(1)
        table = m.group(2)
        if table not in known_tables:
            continue
        if col_raw.strip() in ("*", "DISTINCT *"):
            continue
        # If the column list contains a subquery or complex expression
        # (i.e., an unmatched open paren), skip — regex can't safely parse it.
        if "(" in col_raw:
            continue
        for col in _split_col_list(col_raw):
            refs.append((table, col))

    # UPDATE … SET
    for m in _UPDATE_RE.finditer(sql):
        table = m.group(1)
        if table not in known_tables:
            continue
        set_clause = m.group(2)
        for col in _split_update_set(set_clause):
            refs.append((table, col))

    return refs


# ---------------------------------------------------------------------------
# Violation detection
# ---------------------------------------------------------------------------


def validate_refs(
    refs: list[tuple[str, str]],
    schema: dict[str, set[str]],
    file: str = "",
    line: int = 0,
    pattern: str = "",
) -> list[Violation]:
    """Cross-reference (table, column) pairs against the schema dict.

    Returns a list of Violation for any column not found in its table's
    column set.  Tables not present in the schema are silently skipped
    (they may be in-flight migrations or external tables).
    """
    violations: list[Violation] = []
    for table, col in refs:
        if table not in schema:
            continue
        if col not in schema[table]:
            violations.append(
                Violation(
                    file=file,
                    line=line,
                    table=table,
                    column=col,
                    pattern=pattern,
                )
            )
    return violations


# ---------------------------------------------------------------------------
# File-level validation
# ---------------------------------------------------------------------------


def _check_sql_text(
    sql: str,
    known_tables: set[str],
    schema: dict[str, set[str]],
    file: str,
    line: int,
    pattern: str,
) -> list[Violation]:
    """Extract refs from *sql* and validate them."""
    refs = extract_sql_column_refs(sql, known_tables)
    return validate_refs(refs, schema, file=file, line=line, pattern=pattern)


def validate_file(
    file_path: str,
    schema: dict[str, set[str]],
    known_tables: set[str],
) -> list[Violation]:
    """Validate a single file and return all column-reference violations.

    Supports:
    - .py  → AST-parse, extract SQL string literals
    - .md  → extract fenced code blocks, then:
               - sql blocks  → parse SQL directly
               - python blocks → try AST; if AST fails but looks like SQL,
                                 fall back to raw-text SQL parse
    """
    path = Path(file_path)
    violations: list[Violation] = []

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    # Display path (relative to repo root when possible)
    try:
        display = str(path.relative_to(REPO_ROOT))
    except ValueError:
        display = str(path)

    suffix = path.suffix.lower()

    if suffix == ".py":
        for sql_text, lineno in sql_string_literals(content):
            if not looks_like_sql(sql_text):
                continue
            violations.extend(
                _check_sql_text(
                    sql_text, known_tables, schema, display, lineno, "py:string"
                )
            )

    elif suffix == ".md":
        for lang, code, start_line in extract_markdown_code_blocks(content):
            if _markdown_block_ignored(content, start_line):
                continue
            if lang == "sql":
                violations.extend(
                    _check_sql_text(
                        code, known_tables, schema, display, start_line, "md:sql"
                    )
                )
            elif lang in ("python", "py"):
                # Try AST path first
                literals = sql_string_literals(code)
                if literals:
                    for sql_text, rel_line in literals:
                        if not looks_like_sql(sql_text):
                            continue
                        violations.extend(
                            _check_sql_text(
                                sql_text,
                                known_tables,
                                schema,
                                display,
                                start_line + rel_line - 1,
                                "md:python",
                            )
                        )
                else:
                    # AST returned nothing (parse failed or no strings found).
                    # If the raw block looks like SQL, treat it directly.
                    if looks_like_sql(code):
                        violations.extend(
                            _check_sql_text(
                                code,
                                known_tables,
                                schema,
                                display,
                                start_line,
                                "md:python(fallback)",
                            )
                        )

        # Opt-in: markdown pipe-table column validation via marker
        for line_no, table, col in _extract_marker_validated_columns(content):
            if table not in schema:
                # Asserting against an unknown table is itself a violation —
                # author explicitly claimed columns for a table we don't know.
                violations.append(
                    Violation(
                        file=display,
                        line=line_no,
                        table=table,
                        column=col,
                        pattern="md:marker(unknown-table)",
                    )
                )
                continue
            if col not in schema[table]:
                violations.append(
                    Violation(
                        file=display,
                        line=line_no,
                        table=table,
                        column=col,
                        pattern="md:marker",
                    )
                )

    return violations


def collect_unknown_table_warnings(
    file_path: str,
    known_tables: set[str],
) -> list[tuple[str, int, str]]:
    """Return ``(display_path, line, table)`` for SQL in plan/spec markdown that
    references a table NOT in the schema index. Non-fatal — intended to surface
    Supabase / external table references that the main validator silently skips.
    """
    path = Path(file_path)
    if path.suffix.lower() != ".md":
        return []
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    try:
        display = str(path.relative_to(REPO_ROOT))
    except ValueError:
        display = str(path)
    external = _collect_external_markers(content)
    warnings: list[tuple[str, int, str]] = []
    for line, tbl in _extract_unknown_table_refs(content, known_tables):
        if tbl in external:
            continue
        warnings.append((display, line, tbl))
    return warnings


# ---------------------------------------------------------------------------
# File collection
# ---------------------------------------------------------------------------

# Directories to exclude when scanning .py files
_EXCLUDE_DIRS = {
    ".venv",
    "venv",
    "tests",
    "archive",
    "__pycache__",
    ".git",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    # Full repo COPIES living inside the repo. Walking them made the validator scan ~20 checkouts:
    #   .worktrees/  — other agents' in-progress branches (their unfinished schema edits were
    #                  reported as violations of OUR tree — a false failure we don't own)
    #   .runtime/    — the pinned memecoin release worktrees (17 of them, one per deploy)
    # Cost: 57s, which blew the suite's 60s per-test timeout and crashed the worker running
    # test_knowledge_sync::test_schema_validator_runs. Excluding both: 57s -> ~2s.
    ".worktrees",
    ".runtime",
}

# Markdown directories to scan (relative to REPO_ROOT)
_MD_DIRS = [
    "docs/superpowers/plans",
    "docs/superpowers/specs",
]

_MD_IGNORE_MARKER = "<!-- schema-validator: ignore-next-block -->"
_MD_VALIDATE_COLUMNS_RE = re.compile(
    r"<!--\s*schema:validate-columns-for=([A-Za-z_][A-Za-z0-9_]*)\s*-->",
    re.IGNORECASE,
)
_MD_EXTERNAL_MARKER_RE = re.compile(
    r"<!--\s*schema:external=([A-Za-z_][A-Za-z0-9_]*)\s*-->",
    re.IGNORECASE,
)


def _markdown_block_ignored(content: str, start_line: int) -> bool:
    """Return True when the markdown block at *start_line* is explicitly ignored."""
    lines = content.splitlines()
    fence_index = start_line - 2  # 0-indexed line containing the opening fence
    i = fence_index - 1
    while i >= 0:
        stripped = lines[i].strip()
        if not stripped:
            i -= 1
            continue
        return stripped == _MD_IGNORE_MARKER
    return False


# ---------------------------------------------------------------------------
# Markdown pipe-table column validation (opt-in)
# ---------------------------------------------------------------------------
#
# When a plan/spec needs to assert "table X has columns A, B, C" in a markdown
# pipe table, place the marker `<!-- schema:validate-columns-for=<table> -->`
# immediately before the table. The FIRST cell of each data row is then
# validated as a column name for that table. Unknown columns become violations.
#
# Markdown pipe tables have this shape:
#
#     | Column | Type    | Default |
#     |--------|---------|---------|
#     | wallet | TEXT    | ''      |
#     | pnl    | REAL    | 0       |


def _extract_marker_validated_columns(
    content: str,
) -> list[tuple[int, str, str]]:
    """Find every ``<!-- schema:validate-columns-for=<table> -->`` marker + the
    pipe-table that follows, and return ``(line, table, column)`` tuples for each
    data row (first cell). Returns [] when no markers exist.

    Matching rules:
    - Marker must appear on its own line (after stripping).
    - Next non-blank content after marker must be a pipe-table header line.
    - First data row starts after the separator line (``|---|---|``).
    - Parsing stops at the first non-table line.
    - First cell of each data row is treated as the column name; leading/trailing
      whitespace and surrounding backticks are stripped.
    - Only plain-identifier first-cells are emitted (same rule as SQL extraction).
    """
    lines = content.splitlines()
    triples: list[tuple[int, str, str]] = []

    for idx, line in enumerate(lines):
        m = _MD_VALIDATE_COLUMNS_RE.search(line)
        if not m:
            continue
        table_name = m.group(1)

        # Find next non-blank line — must be a pipe-table header
        j = idx + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        if j >= len(lines):
            continue
        header = lines[j].strip()
        if not (header.startswith("|") and header.endswith("|")):
            continue

        # Next line must be the separator `|---|---|`
        if j + 1 >= len(lines):
            continue
        separator = lines[j + 1].strip()
        if not (separator.startswith("|") and "---" in separator):
            continue

        # Data rows start at j+2, continue while lines look like table rows
        k = j + 2
        while k < len(lines):
            row = lines[k].rstrip()
            stripped = row.strip()
            if not stripped.startswith("|") or not stripped.endswith("|"):
                break
            # Split cells; first and last are empty bookends
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if cells:
                first_cell = cells[0].strip().strip("`").strip()
                # Strip anything after a space (e.g. `col NOT NULL` → `col`)
                if first_cell:
                    token = first_cell.split()[0]
                    if _is_plain_identifier(token):
                        # Lines are 1-indexed for display
                        triples.append((k + 1, table_name, token))
            k += 1

    return triples


# SQLite system tables — never part of schema_registry.json but universally valid
_BUILTIN_EXTERNAL_TABLES = frozenset({
    "sqlite_master",
    "sqlite_sequence",
    "sqlite_temp_master",
    "sqlite_schema",
})


def _collect_external_markers(content: str) -> set[str]:
    """Tables the file explicitly declares as external (Supabase etc.), merged
    with the built-in set of SQLite system tables."""
    explicit = {m.group(1) for m in _MD_EXTERNAL_MARKER_RE.finditer(content)}
    return explicit | set(_BUILTIN_EXTERNAL_TABLES)


def _extract_unknown_table_refs(
    content: str,
    known_tables: set[str],
) -> list[tuple[int, str]]:
    """Scan SQL code blocks in markdown for INSERT/SELECT/UPDATE against tables
    NOT in ``known_tables``. Returns ``(line, table)`` pairs. Intended for
    plan/spec files — surfaces Supabase / proposed-table references that the
    main validator silently skips today.
    """
    unknowns: list[tuple[int, str]] = []
    for lang, code, start_line in extract_markdown_code_blocks(content):
        if lang not in ("sql", "python", "py"):
            continue
        if _markdown_block_ignored(content, start_line):
            continue
        # Skip DDL / JOIN (same rules as SQL extractor)
        if _DDL_RE.search(code) or _JOIN_RE.search(code):
            continue
        # CTE aliases (`WITH foo AS (...) SELECT ... FROM foo`) are valid refs
        # within the block — treat them as known for this block only.
        cte_names = {m.group(1) for m in _CTE_DEF_RE.finditer(code)}
        block_known = known_tables | cte_names
        for m in _INSERT_RE.finditer(code):
            tbl = m.group(1)
            if tbl not in block_known:
                unknowns.append((start_line, tbl))
        for m in _SELECT_RE.finditer(code):
            tbl = m.group(2)
            if tbl not in block_known:
                unknowns.append((start_line, tbl))
        for m in _UPDATE_RE.finditer(code):
            tbl = m.group(1)
            if tbl not in block_known:
                unknowns.append((start_line, tbl))
    return unknowns


def collect_files(file_list: Optional[list[str]] = None) -> list[str]:
    """Return the list of files to validate.

    If *file_list* is provided, honor the same directory exclusions as bulk
    mode so the pre-commit hook and CI behave consistently. Otherwise scan
    the repository:
      - All .py files outside excluded directories
      - .md files under docs/superpowers/plans/ and docs/superpowers/specs/
    """
    if file_list is not None:
        out: list[str] = []
        for raw in file_list:
            try:
                rel = Path(raw).resolve().relative_to(REPO_ROOT)
            except ValueError:
                # Path outside repo — keep it; caller's responsibility
                out.append(raw)
                continue
            if set(rel.parts) & _EXCLUDE_DIRS:
                continue
            out.append(raw)
        return out

    files: list[str] = []

    # Python files
    for py_file in REPO_ROOT.rglob("*.py"):
        # Check if any part of the path is in the exclusion set
        rel_parts = set(py_file.relative_to(REPO_ROOT).parts)
        if rel_parts & _EXCLUDE_DIRS:
            continue
        files.append(str(py_file))

    # Markdown files in designated dirs
    for md_dir in _MD_DIRS:
        target = REPO_ROOT / md_dir
        if target.is_dir():
            for md_file in target.rglob("*.md"):
                files.append(str(md_file))

    return sorted(files)


# ---------------------------------------------------------------------------
# Supabase table grants
# ---------------------------------------------------------------------------

# From 2026-10-30 Supabase no longer auto-grants Data API access to new public
# tables. A migration that creates a table must grant service_role (our only
# writer role) in the same file, or the table is unreachable. Files numbered
# below this got their grants from the old default privileges and stay exempt.
_SUPABASE_GRANT_MIN_MIGRATION = 32
_SUPABASE_MIGRATION_NUM_RE = re.compile(r"^supabase_(\d{3})_")
_SUPABASE_CREATE_TABLE_RE = re.compile(
    r"\bcreate\s+table\s+(?:if\s+not\s+exists\s+)?public\.(\w+)", re.IGNORECASE
)


def check_supabase_migration_grants(paths: list[Path]) -> list[str]:
    """Return one problem line per new public table with no service_role grant."""
    problems: list[str] = []
    for path in paths:
        m = _SUPABASE_MIGRATION_NUM_RE.match(path.name)
        if not m or int(m.group(1)) < _SUPABASE_GRANT_MIN_MIGRATION:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        # Executable SQL only: a `-- GRANT` in prose is not a grant (Codex review 2026-09-23, Q1).
        text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
        text = re.sub(r"--[^\n]*", "", text)
        for table in _SUPABASE_CREATE_TABLE_RE.findall(text):
            grant = re.compile(
                r"\bgrant\b[^;]*\bon\s+public\." + re.escape(table)
                + r"\b[^;]*\bto\b[^;]*\bservice_role\b",
                re.IGNORECASE | re.DOTALL,
            )
            if not grant.search(text):
                problems.append(
                    f"{path}: table '{table}' has no GRANT ... ON public.{table} "
                    "TO service_role (required for Supabase migrations >= 032)"
                )
    return problems


# ---------------------------------------------------------------------------
# Schema loading
# ---------------------------------------------------------------------------


def load_schema() -> dict[str, set[str]]:
    """Load index/schema_registry.json and return {table: {col, ...}} mapping."""
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        registry = json.load(f)

    schema: dict[str, set[str]] = {}
    for table_name, table_info in registry["tables"].items():
        schema[table_name] = {col["name"] for col in table_info["columns"]}
    return schema


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate SQL column references against schema_registry.json"
    )
    parser.add_argument(
        "--files",
        nargs="+",
        metavar="FILE",
        help="Specific files to check (default: scan whole repo)",
    )
    parser.add_argument(
        "--warn-unknown-tables",
        action="store_true",
        help="Emit WARN lines for plan/spec SQL that references tables not in the index "
             "(Supabase / proposed tables). Non-fatal — exit code unaffected.",
    )
    args = parser.parse_args()

    schema = load_schema()
    known_tables = set(schema.keys())
    files = collect_files(file_list=args.files)

    all_violations: list[Violation] = []
    all_warnings: list[tuple[str, int, str]] = []
    for file_path in files:
        all_violations.extend(validate_file(file_path, schema, known_tables))
        if args.warn_unknown_tables:
            all_warnings.extend(
                collect_unknown_table_warnings(file_path, known_tables)
            )

    grant_problems = check_supabase_migration_grants(
        sorted((REPO_ROOT / "migrations").glob("supabase_*.sql"))
    )
    for line in grant_problems:
        print(f"FAIL: {line}")

    # Emit warnings first so they're visible even when violations also exist
    if all_warnings:
        by_file_warn: dict[str, list[tuple[int, str]]] = {}
        for display, line, tbl in all_warnings:
            by_file_warn.setdefault(display, []).append((line, tbl))
        for display, entries in sorted(by_file_warn.items()):
            for line, tbl in entries:
                print(
                    f"WARN: {display}:{line} references '{tbl}' — "
                    "not in SQLite schema_registry.json. Verify it's intentional "
                    "(Supabase / external), or add <!-- schema:external="
                    f"{tbl} --> to silence."
                )
        print()

    if not all_violations and not grant_problems:
        if all_warnings:
            print(f"No schema violations. {len(all_warnings)} unknown-table warning(s).")
        else:
            print("No schema violations found.")
        return 0

    # Group by file for readable output
    by_file: dict[str, list[Violation]] = {}
    for v in all_violations:
        by_file.setdefault(v.file, []).append(v)

    for file_display, violations in sorted(by_file.items()):
        for v in violations:
            print(f"FAIL: {v.file}:{v.line}")
            print(f"  {v.table}: unknown column '{v.column}'  ({v.pattern})")
        print()

    print(f"{len(all_violations) + len(grant_problems)} schema violation(s) found")
    return 1


if __name__ == "__main__":
    sys.exit(main())
