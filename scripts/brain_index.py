#!/usr/bin/env python3
"""Brain Index — repo-local engineering index for Sniper.

Generates machine-readable artifacts from the actual codebase:
- schema_registry.json  — SQLite table schemas via init_db() introspection
- dependency_index.json — heuristic file→table dependency mapping
- docs_registry.json    — markdown docs with memory_* frontmatter (plans, specs, audits, research, design)
- pattern_registry.yaml — hand-curated code pattern reference (not generated)

Usage:
    python scripts/brain_index.py generate          # regenerate schema + deps + docs
    python scripts/brain_index.py schema <table>    # lookup table schema
    python scripts/brain_index.py schema --list     # list all tables
    python scripts/brain_index.py pattern <name>    # lookup pattern
    python scripts/brain_index.py pattern --list    # list all patterns
    python scripts/brain_index.py deps <file>       # file dependencies
    python scripts/brain_index.py deps --table <t>  # table readers/writers
    python scripts/brain_index.py doc <query>       # find docs by name or tag
    python scripts/brain_index.py doc --list        # list all indexed docs
"""

import argparse
import ast
import importlib.util
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import yaml

from sql_ref_utils import sql_string_literals

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX_DIR = REPO_ROOT / "index"
SCHEMA_FILE = INDEX_DIR / "schema_registry.json"
DEPS_FILE = INDEX_DIR / "dependency_index.json"
PATTERN_FILE = INDEX_DIR / "pattern_registry.yaml"
DOCS_FILE = INDEX_DIR / "docs_registry.json"

DOCS_SCAN_DIRS = [
    "docs/superpowers/plans",
    "docs/superpowers/specs",
    "docs/audits",
    "docs/research",
    "docs/design",
]
_DOC_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_SUPABASE_MIGRATIONS_DIR = REPO_ROOT / "migrations"
_SUPABASE_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+public\.([a-zA-Z_][\w]*)\s*\(",
    re.IGNORECASE,
)
_WALLET_LEDGER_SUPABASE_MIGRATION_RE = re.compile(r"supabase_0(?:1[6-9]|[2-9]\d)_")


def _bootstrap_paper_trader_tables(conn: sqlite3.Connection) -> str:
    from paper_trader import PaperTrader  # noqa: PLC0415

    pt = PaperTrader.__new__(PaperTrader)
    pt.conn = conn
    pt._init_tables()
    return "paper_trader"


def _bootstrap_trade_ideas_tables(conn: sqlite3.Connection) -> str:
    brain_path = REPO_ROOT / "brain.py"
    spec = importlib.util.spec_from_file_location("_brain_index_root_brain", brain_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load module spec for {brain_path}")
    brain_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(brain_module)

    brain_module.ensure_trade_ideas_schema(conn)
    return "brain.ensure_trade_ideas_schema"


def _bootstrap_real_trader_tables(conn: sqlite3.Connection) -> str:
    from sniper.services.real_trader import RealTrader  # noqa: PLC0415

    rt = RealTrader.__new__(RealTrader)
    rt.conn = conn
    rt._init_tables()
    return "real_trader"


def _bootstrap_intel_paper_tables(conn: sqlite3.Connection) -> str:
    from sniper.persistence.intel_paper import ensure_schema  # noqa: PLC0415

    ensure_schema(conn)
    return "intel_paper"


def _bootstrap_alert_watermarks_table(conn: sqlite3.Connection) -> str:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS alert_watermarks ("
        "key TEXT PRIMARY KEY, value INTEGER NOT NULL DEFAULT 0)"
    )
    return "alert_watermarks"


def _bootstrap_paper_sell_signals_table(conn: sqlite3.Connection) -> str:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS paper_sell_signals ("
        "wallet TEXT, condition_id TEXT, asset_id TEXT, "
        "price REAL, size REAL, timestamp INTEGER, processed INTEGER DEFAULT 0)"
    )
    return "paper_sell_signals"


def _bootstrap_far_whale_store_tables(conn: sqlite3.Connection) -> str:
    """The fwlm-v0 study tables live in sniper/hyperlive/far_whale_store.py, a standalone
    persistence module (not wired through db.init_db()) — bootstrap it explicitly so the
    9 hl_far_whale_liq_* tables are visible to `schema <table>` and dependency tracking."""
    from sniper.hyperlive import far_whale_store  # noqa: PLC0415

    far_whale_store.ensure_schema(conn)
    return "far_whale_store"


def _bootstrap_maker_v33_tables(conn: sqlite3.Connection) -> str:
    """The v3.3-learner tables live behind maker_adaptive_v33.ensure_v33_tables()
    (runner-created, not db.init_db()) — bootstrap so `schema <table>` sees them."""
    from sniper.algos.polymarket_crypto5m import maker_adaptive_v33  # noqa: PLC0415

    maker_adaptive_v33.ensure_v33_tables(conn)
    return "maker_adaptive_v33.ensure_v33_tables"


def _bootstrap_memecoin_tables(conn: sqlite3.Connection) -> str:
    """The memecoin copy-mirror + trader-analytics tables live in
    sniper/persistence/db_memecoin.py behind ensure_memecoin_tables(), which is
    called by the runner/routes — not by db.init_db(). The X source-graph tables
    live in db_memecoin_x.py behind ensure_memecoin_x_tables(). Bootstrap both
    explicitly so every memecoin_*/memecoin_x_* table is visible to
    `schema <table>` and dependency tracking."""
    from sniper.persistence import (  # noqa: PLC0415
        db_axiom, db_memecoin, db_memecoin_notifications, db_memecoin_x)

    db_memecoin.ensure_memecoin_tables(conn)
    db_memecoin.ensure_known_group_observer_tables(conn)  # Standalone private observer, indexed in memory only.
    db_memecoin_x.ensure_memecoin_x_tables(conn)
    # j7_receipts + axiom_verified_addresses live in db_axiom.py behind ensure_axiom_tables()
    # (2026-09-22: `schema j7_receipts` answered "not found" while the table was live).
    db_axiom.ensure_axiom_tables(conn)
    # The Telegram worker creates its notification tables from this SCHEMA (Codex, 2026-09-13).
    conn.executescript(db_memecoin_notifications.SCHEMA)
    return ("db_memecoin.ensure_memecoin_tables+db_memecoin_x.ensure_memecoin_x_tables"
            "+db_memecoin_notifications.SCHEMA")


def _bootstrap_memecoin_v6_paper_tables(conn: sqlite3.Connection) -> str:
    """Index the isolated V6 paper ledger without changing the primary DB schema version."""
    from sniper.persistence import db_memecoin_v6_paper  # noqa: PLC0415

    primary_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    conn.executescript(db_memecoin_v6_paper.SCHEMA)
    conn.execute(f"PRAGMA user_version={primary_version}")
    return "db_memecoin_v6_paper.SCHEMA"


def _bootstrap_situation_model_table(conn: sqlite3.Connection) -> str:
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS situation_model ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "slug TEXT NOT NULL, claim_type TEXT NOT NULL DEFAULT 'fact', "
        "subject TEXT NOT NULL, predicate TEXT NOT NULL, object TEXT, "
        "confidence REAL NOT NULL DEFAULT 0.5, "
        "source_tier TEXT NOT NULL DEFAULT 'factual', sources TEXT, "
        "diversity_score REAL, valid_from TEXT NOT NULL, valid_until TEXT, "
        "created_at TEXT NOT NULL DEFAULT (datetime('now')), "
        "updated_at TEXT NOT NULL DEFAULT (datetime('now')));\n"
        "CREATE INDEX IF NOT EXISTS idx_situation_slug ON situation_model (slug);\n"
        "CREATE INDEX IF NOT EXISTS idx_situation_subject ON situation_model (subject);\n"
        "CREATE INDEX IF NOT EXISTS idx_situation_valid ON situation_model (valid_from, valid_until);"
    )
    return "situation_model"


_SCHEMA_BOOTSTRAPS = (
    _bootstrap_paper_trader_tables,
    _bootstrap_trade_ideas_tables,
    _bootstrap_real_trader_tables,
    _bootstrap_intel_paper_tables,
    _bootstrap_alert_watermarks_table,
    _bootstrap_paper_sell_signals_table,
    _bootstrap_situation_model_table,
    _bootstrap_far_whale_store_tables,
    _bootstrap_memecoin_tables,
    _bootstrap_memecoin_v6_paper_tables,
    _bootstrap_maker_v33_tables,
)


def _run_schema_bootstraps(conn: sqlite3.Connection) -> list[str]:
    bootstrapped: list[str] = []
    for bootstrap in _SCHEMA_BOOTSTRAPS:
        try:
            bootstrapped.append(bootstrap(conn))
        except Exception as exc:  # pragma: no cover - defensive fail-fast
            raise RuntimeError(
                f"Schema bootstrap failed for {bootstrap.__name__}: {exc}"
            ) from exc
    return bootstrapped


def _split_sql_items(body: str) -> list[str]:
    """Split comma-separated SQL clauses without splitting nested calls."""
    items: list[str] = []
    start = 0
    depth = 0
    in_single_quote = False
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "'":
            in_single_quote = not in_single_quote
        elif not in_single_quote:
            if ch == "(":
                depth += 1
            elif ch == ")" and depth > 0:
                depth -= 1
            elif ch == "," and depth == 0:
                items.append(body[start:i].strip())
                start = i + 1
        i += 1
    tail = body[start:].strip()
    if tail:
        items.append(tail)
    return items


def _strip_sql_line_comments(text: str) -> str:
    return "\n".join(line.split("--", 1)[0] for line in text.splitlines())


def _find_matching_paren(text: str, open_idx: int) -> int:
    depth = 0
    in_single_quote = False
    for idx in range(open_idx, len(text)):
        ch = text[idx]
        if ch == "'":
            in_single_quote = not in_single_quote
        elif not in_single_quote:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return idx
    return -1


def _parse_identifier_list(value: str) -> list[str]:
    return [
        part.strip().strip('"')
        for part in value.split(",")
        if part.strip()
    ]


def _parse_supabase_table(table_name: str, body: str) -> dict:
    columns: list[dict] = []
    indexes: list[dict] = []
    fks: list[dict] = []
    primary_key_cols: set[str] = set()

    clauses = _split_sql_items(body)
    for clause in clauses:
        normalized = " ".join(clause.split())
        upper = normalized.upper()
        if upper.startswith("PRIMARY KEY"):
            match = re.search(r"\(([^)]*)\)", normalized)
            if match:
                primary_key_cols.update(_parse_identifier_list(match.group(1)))
            continue
        if "FOREIGN KEY" in upper and "REFERENCES PUBLIC." in upper:
            fk_match = re.search(
                r"FOREIGN\s+KEY\s*\(([^)]*)\)\s*REFERENCES\s+public\.([a-zA-Z_][\w]*)\s*\(([^)]*)\)",
                normalized,
                re.IGNORECASE,
            )
            if fk_match:
                from_cols = _parse_identifier_list(fk_match.group(1))
                to_table = fk_match.group(2)
                to_cols = _parse_identifier_list(fk_match.group(3))
                for from_col, to_col in zip(from_cols, to_cols):
                    fks.append(
                        {
                            "from_column": from_col,
                            "to_table": to_table,
                            "to_column": to_col,
                        }
                    )
            continue
        if upper.startswith(("CONSTRAINT ", "UNIQUE ", "CHECK ")):
            continue

        parts = normalized.split()
        if len(parts) < 2:
            continue
        col_name = parts[0].strip('"')
        col_type = parts[1].upper()
        if col_type in {"DOUBLE", "TIMESTAMP"} and len(parts) >= 3:
            col_type = f"{col_type} {parts[2].upper()}"
        notnull = "NOT NULL" in upper
        inline_pk = "PRIMARY KEY" in upper
        default_match = re.search(
            r"\bDEFAULT\s+(.+?)(?:\s+CHECK\b|\s+PRIMARY\b|\s+NOT\b|$)",
            normalized,
            re.IGNORECASE,
        )
        columns.append(
            {
                "name": col_name,
                "type": col_type,
                "notnull": notnull,
                "pk": inline_pk,
                "default": default_match.group(1).strip() if default_match else None,
            }
        )
        if inline_pk:
            primary_key_cols.add(col_name)

    if primary_key_cols:
        for col in columns:
            if col["name"] in primary_key_cols:
                col["pk"] = True

    return {
        "columns": columns,
        "indexes": indexes,
        "foreign_keys": fks,
    }


_SUPABASE_ALTER_ADD_COLUMN_RE = re.compile(
    r"ALTER\s+TABLE\s+(?:public\.)?([a-zA-Z_]\w*)\s+ADD\s+COLUMN\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?([a-zA-Z_]\w*)\s+([^;]+?)\s*;",
    re.IGNORECASE,
)


def _alter_column_dict(col_name: str, type_clause: str) -> dict:
    """Build a column dict (same shape as _parse_supabase_table) from an ADD COLUMN clause."""
    normalized = " ".join(type_clause.split())
    upper = normalized.upper()
    parts = normalized.split()
    col_type = parts[0].upper() if parts else ""
    if col_type in {"DOUBLE", "TIMESTAMP"} and len(parts) >= 2:
        col_type = f"{col_type} {parts[1].upper()}"
    default_match = re.search(
        r"\bDEFAULT\s+(.+?)(?:\s+CHECK\b|\s+NOT\b|$)", normalized, re.IGNORECASE
    )
    return {
        "name": col_name,
        "type": col_type,
        "notnull": "NOT NULL" in upper,
        "pk": False,
        "default": default_match.group(1).strip() if default_match else None,
    }


def _parse_supabase_migration_tables() -> dict[str, dict]:
    """Parse committed Supabase CREATE TABLE migrations into the schema index.

    The primary schema registry is SQLite-backed, but PR #6 introduces
    canonical Supabase tables that agents query through the same
    `brain_index.py schema <table>` workflow. This parser is intentionally
    narrow: it indexes `CREATE TABLE IF NOT EXISTS public.<table>` blocks and
    applies later `ALTER TABLE ... ADD COLUMN` migrations to those tables.
    """
    tables: dict[str, dict] = {}
    if not _SUPABASE_MIGRATIONS_DIR.exists():
        return tables

    migrations = [
        m for m in sorted(_SUPABASE_MIGRATIONS_DIR.glob("supabase_*.sql"))
        if _WALLET_LEDGER_SUPABASE_MIGRATION_RE.match(m.name)
    ]

    # Pass 1: CREATE TABLE blocks.
    for migration in migrations:
        text = _strip_sql_line_comments(migration.read_text())
        for match in _SUPABASE_CREATE_TABLE_RE.finditer(text):
            table_name = match.group(1)
            open_idx = text.find("(", match.end() - 1)
            close_idx = _find_matching_paren(text, open_idx)
            if open_idx == -1 or close_idx == -1:
                continue
            body = text[open_idx + 1 : close_idx]
            parsed = _parse_supabase_table(table_name, body)
            parsed["source"] = str(migration.relative_to(REPO_ROOT))
            tables[table_name] = parsed

    # Pass 2: ALTER TABLE ... ADD COLUMN on already-indexed tables (in migration order).
    for migration in migrations:
        text = _strip_sql_line_comments(migration.read_text())
        for m in _SUPABASE_ALTER_ADD_COLUMN_RE.finditer(text):
            table_name, col_name, type_clause = m.group(1), m.group(2), m.group(3)
            entry = tables.get(table_name)
            if entry is None or any(c["name"] == col_name for c in entry["columns"]):
                continue
            entry["columns"].append(_alter_column_dict(col_name, type_clause))
    return tables


def generate_schema_registry() -> dict:
    """Create in-memory SQLite DB via init_db(), introspect all tables.

    Also bootstraps tables owned by PaperTrader (paper_trades, paper_performance)
    since those are created in paper_trader.py, not in init_db().
    """
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from db import init_db

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    bootstrapped = _run_schema_bootstraps(conn)

    conn.commit()
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
                # index_info reports NULL for expression keys (e.g.
                # COALESCE(execution_mode,'paper') on memecoin_positions)
                c["name"] if c["name"] is not None else "<expr>"
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

    supabase_tables = {
        name: table
        for name, table in _parse_supabase_migration_tables().items()
        if name not in tables
    }
    tables.update(supabase_tables)

    return {
        "schema_version": schema_version,
        "bootstrap_sources": bootstrapped
        + (["supabase_migrations"] if supabase_tables else []),
        "table_count": len(tables),
        "tables": tables,
    }


# SQL keywords near a table name that indicate write operations
_WRITE_KW = re.compile(r"\b(?:INSERT|UPDATE|DELETE|REPLACE|CREATE|ALTER|DROP)\b", re.IGNORECASE)
# SQL keywords near a table name that indicate read operations
_READ_KW = re.compile(r"\b(?:SELECT|FROM|JOIN)\b", re.IGNORECASE)

_SKIP_DIRS = frozenset(
    {
        "tests", ".venv", "venv", "__pycache__", "index", "node_modules",
        ".git", ".claude", ".worktrees", "archive",
    }
)



def _db_imports(content: str) -> set[str]:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return set()

    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("db"):
                    imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("db"):
            imports.add(node.module)
    return imports


def _tracked_py_files() -> list[Path]:
    """Git-tracked .py files only, so the generated index matches a clean CI checkout regardless of
    untracked/uncommitted WIP (e.g. a parallel agent's files). Falls back to rglob if git is unavailable."""
    try:
        out = subprocess.check_output(["git", "-C", str(REPO_ROOT), "ls-files", "*.py"],
                                      text=True, stderr=subprocess.DEVNULL)
        files = [REPO_ROOT / line for line in out.splitlines() if line.strip()]
        if files:
            return sorted(files)
    except Exception:  # noqa: BLE001
        pass
    return sorted(REPO_ROOT.rglob("*.py"))


def generate_dependency_index(schema: dict) -> dict:
    """String-literal-backed extraction of file→table dependencies (git-tracked files only)."""
    table_names = set(schema["tables"].keys())
    table_patterns = {t: re.compile(rf"\b{re.escape(t)}\b") for t in table_names}

    file_deps: dict[str, dict] = {}
    table_readers: dict[str, list[str]] = {}
    table_writers: dict[str, list[str]] = {}

    for py_file in _tracked_py_files():
        rel = py_file.relative_to(REPO_ROOT)
        if any(p in _SKIP_DIRS for p in rel.parts):
            continue

        try:
            content = py_file.read_text(errors="replace")
        except OSError:
            continue

        reads: set[str] = set()
        writes: set[str] = set()
        imports = _db_imports(content)

        for literal, _lineno in sql_string_literals(content):
            for table, pattern in table_patterns.items():
                if not pattern.search(literal):
                    continue
                if _WRITE_KW.search(literal):
                    writes.add(table)
                elif _READ_KW.search(literal):
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
        "file_count": len(file_deps),
        "files": file_deps,
        "table_readers": {t: sorted(v) for t, v in table_readers.items()},
        "table_writers": {t: sorted(v) for t, v in table_writers.items()},
    }



# ---------------------------------------------------------------------------
# Formatting functions (for CLI output)
# ---------------------------------------------------------------------------


def _similar_names(target: str, candidates: list[str], limit: int = 5) -> list[str]:
    """Return up to *limit* candidate names closest to *target* (case-insensitive substring + difflib)."""
    import difflib
    target_lower = target.lower()
    substring_matches = [c for c in candidates if target_lower in c.lower() or c.lower() in target_lower]
    fuzzy = difflib.get_close_matches(target, candidates, n=limit, cutoff=0.5)
    seen = set()
    out = []
    for name in list(substring_matches) + fuzzy:
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
        if len(out) >= limit:
            break
    return out


def _not_found_schema_hint(table_name: str, known_tables: list[str]) -> str:
    """Actionable message when `schema <table>` misses."""
    lines = [f"Table '{table_name}' not found in schema_registry.json.", ""]
    similar = _similar_names(table_name, known_tables)
    if similar:
        lines.append(f"Did you mean: {', '.join(similar)}?")
        lines.append("")
    lines.append("If not a typo, this table may be:")
    lines.append(f"  1. Not yet created — grep: `CREATE TABLE IF NOT EXISTS {table_name}`")
    lines.append("  2. Named differently — run: python scripts/brain_index.py schema --list")
    lines.append("")
    lines.append("Do NOT invent column names before resolving this.")
    return "\n".join(lines)


def format_schema_table(registry: dict, table_name: str) -> str:
    """Format a single table's schema for terminal display."""
    tables = registry.get("tables", {})
    if table_name not in tables:
        return _not_found_schema_hint(table_name, sorted(tables.keys()))

    t = tables[table_name]
    lines = [f"── {table_name} ──", ""]

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

    if t["indexes"]:
        lines.append("")
        lines.append("Indexes:")
        for idx in t["indexes"]:
            uniq = "UNIQUE " if idx["unique"] else ""
            # Expression indexes (e.g. COALESCE(...)) report NULL column names in
            # PRAGMA index_info; render them as <expr> instead of crashing on join.
            cols = ", ".join(c if c is not None else "<expr>" for c in idx["columns"])
            lines.append(f"  {idx['name']:50s} {uniq}({cols})")

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
        similar = _similar_names(file_path, sorted(files.keys()))
        hint = f" Did you mean: {', '.join(similar)}?" if similar else ""
        return (
            f"File '{file_path}' not found in dependency index.{hint}\n"
            "The index covers tracked Python files only. If the file exists, "
            "regenerate: python scripts/brain_index.py generate"
        )

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
        all_tables = sorted(set(deps.get("table_readers", {}).keys()) | set(deps.get("table_writers", {}).keys()))
        similar = _similar_names(table_name, all_tables)
        lines = [f"Table '{table_name}' not found in SQLite dependency index."]
        if similar:
            lines.append(f"Did you mean: {', '.join(similar)}?")
        lines.append("")
        lines.append("If not a typo, this table may be:")
        lines.append("  1. Supabase-only (this index covers SQLite reads/writes only)")
        lines.append("  2. Not referenced by any tracked Python file (look for SQL in .sql/.md files instead)")
        lines.append(f"  3. Named differently — run: python scripts/brain_index.py schema --list")
        return "\n".join(lines)

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


# ---------------------------------------------------------------------------
# Docs registry (plans, specs, audits, research, design memos)
# ---------------------------------------------------------------------------


def _parse_doc_frontmatter(text: str) -> dict | None:
    match = _DOC_FRONTMATTER_RE.match(text)
    if not match:
        return None
    block = match.group(1)
    out: dict = {}
    for line in block.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            out[key] = [v.strip().strip("'\"") for v in value[1:-1].split(",") if v.strip()]
        else:
            out[key] = value.strip().strip("'\"")
    return out


def generate_docs_registry() -> dict:
    """Scan docs dirs for markdown with memory_* frontmatter; produce an index.

    Each entry: {name, type, description, tags, path}.
    """
    entries: list[dict] = []
    for sub in DOCS_SCAN_DIRS:
        d = REPO_ROOT / sub
        if not d.exists():
            continue
        for path in sorted(d.rglob("*.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            fm = _parse_doc_frontmatter(text)
            if not fm or "memory_name" not in fm:
                continue
            entries.append({
                "name": fm["memory_name"],
                "type": fm.get("memory_type", "reference"),
                "description": fm.get("memory_description", ""),
                "tags": fm.get("memory_tags", []),
                "path": str(path.relative_to(REPO_ROOT)),
            })
    return {
        "doc_count": len(entries),
        "docs": sorted(entries, key=lambda e: e["name"]),
    }


def format_doc_entry(data: dict, name: str) -> str:
    docs = data.get("docs", [])
    matches = [d for d in docs if name in d["name"] or name in " ".join(d.get("tags", []))]
    if not matches:
        known = [d["name"] for d in docs]
        similar = _similar_names(name, known)
        hint = f"\nDid you mean: {', '.join(similar)}" if similar else ""
        return f"No docs matched {name!r}.{hint}"
    lines = []
    for d in matches:
        tags = ", ".join(d.get("tags", [])) or "—"
        lines.extend([
            f"── {d['name']} ── ({d['type']})",
            f"  path: {d['path']}",
            f"  tags: {tags}",
            f"  tldr: {d['description']}",
            "",
        ])
    return "\n".join(lines).rstrip()


def format_doc_list(data: dict) -> str:
    docs = data.get("docs", [])
    lines = [f"{len(docs)} docs indexed:\n"]
    for d in docs:
        lines.append(f"  {d['name']:60s} {d['description'][:80]}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


_QUERIES = {
    "schema": (SCHEMA_FILE, "table", format_schema_table, format_schema_list),
    "pattern": (PATTERN_FILE, "name", format_pattern, format_pattern_list),
    "deps": (DEPS_FILE, "file", format_deps_file, format_deps_table),
    "doc": (DOCS_FILE, "name", format_doc_entry, format_doc_list),
}


def _load(path: Path) -> dict | None:
    if not path.exists():
        print(f"File not found: {path}\nRun first: python scripts/brain_index.py generate")
        return None
    with open(path) as f:
        return yaml.safe_load(f) if path.suffix == ".yaml" else json.load(f)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Brain Index — repo-local engineering index for Sniper"
    )
    sub = parser.add_subparsers(dest="command")

    generate_parser = sub.add_parser("generate", help="Regenerate index artifacts")
    generate_parser.add_argument(
        "--core-only", action="store_true",
        help="regenerate schema + dependency artifacts only (used by pre-commit)",
    )

    for cmd, (_, pos_arg, _, _) in _QUERIES.items():
        p = sub.add_parser(cmd, help=f"Query {cmd}")
        p.add_argument(pos_arg, nargs="?", help=f"{cmd.title()} to look up")
        p.add_argument("--list", action="store_true", help=f"List all {cmd}")
        if cmd == "deps":
            p.add_argument("--table", help="Table name to look up readers/writers")

    args = parser.parse_args()

    if args.command == "generate":
        INDEX_DIR.mkdir(exist_ok=True)
        print("Generating schema registry...")
        schema = generate_schema_registry()
        with open(SCHEMA_FILE, "w") as f:
            json.dump(schema, f, indent=2, sort_keys=True)
            f.write("\n")
        print(f"  → {SCHEMA_FILE} ({schema['table_count']} tables)")
        print("Generating dependency index...")
        deps = generate_dependency_index(schema)
        with open(DEPS_FILE, "w") as f:
            json.dump(deps, f, indent=2, sort_keys=True)
            f.write("\n")
        print(f"  → {DEPS_FILE} ({deps['file_count']} files)")
        if not args.core_only:
            print("Generating docs registry...")
            docs = generate_docs_registry()
            with open(DOCS_FILE, "w") as f:
                json.dump(docs, f, indent=2, sort_keys=True)
                f.write("\n")
            print(f"  → {DOCS_FILE} ({docs['doc_count']} docs)")
        print("Done.")

    elif args.command in _QUERIES:
        file_path, pos_arg, detail_fn, list_fn = _QUERIES[args.command]
        data = _load(file_path)
        if data is None:
            return
        if args.list:
            print(list_fn(data))
        elif getattr(args, pos_arg, None):
            print(detail_fn(data, getattr(args, pos_arg)))
        elif args.command == "deps" and getattr(args, "table", None):
            print(format_deps_table(data, args.table))

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
