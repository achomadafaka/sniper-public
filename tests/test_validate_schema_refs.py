"""Tests for scripts/validate_schema_refs.py — schema reference validator.

TDD: written before the implementation.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

# Make scripts/ importable
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from validate_schema_refs import (  # noqa: E402
    Violation,
    collect_files,
    collect_unknown_table_warnings,
    extract_sql_column_refs,
    validate_file,
    validate_refs,
)

# ---------------------------------------------------------------------------
# Shared test schema
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# extract_sql_column_refs tests
# ---------------------------------------------------------------------------


class TestExtractSqlColumnRefs:
    def test_insert_basic(self):
        sql = "INSERT INTO paper_limit_orders (order_id, strategy_id, state) VALUES (?, ?, ?)"
        refs = extract_sql_column_refs(sql, KNOWN_TABLES)
        assert ("paper_limit_orders", "order_id") in refs
        assert ("paper_limit_orders", "strategy_id") in refs
        assert ("paper_limit_orders", "state") in refs

    def test_insert_or_replace(self):
        sql = "INSERT OR REPLACE INTO paper_limit_fills (fill_id, order_id, slippage) VALUES (?, ?, ?)"
        refs = extract_sql_column_refs(sql, KNOWN_TABLES)
        assert ("paper_limit_fills", "fill_id") in refs
        assert ("paper_limit_fills", "order_id") in refs
        assert ("paper_limit_fills", "slippage") in refs

    def test_insert_or_ignore(self):
        sql = "INSERT OR IGNORE INTO user_analysis (wallet, pnl) VALUES (?, ?)"
        refs = extract_sql_column_refs(sql, KNOWN_TABLES)
        assert ("user_analysis", "wallet") in refs
        assert ("user_analysis", "pnl") in refs

    def test_select_named_columns(self):
        sql = "SELECT wallet, insider_score, classification FROM user_analysis WHERE pnl > 0"
        refs = extract_sql_column_refs(sql, KNOWN_TABLES)
        assert ("user_analysis", "wallet") in refs
        assert ("user_analysis", "insider_score") in refs
        assert ("user_analysis", "classification") in refs

    def test_select_star_returns_no_refs(self):
        sql = "SELECT * FROM user_analysis"
        refs = extract_sql_column_refs(sql, KNOWN_TABLES)
        assert refs == []

    def test_create_table_skipped(self):
        sql = "CREATE TABLE user_analysis (wallet TEXT, pnl REAL)"
        refs = extract_sql_column_refs(sql, KNOWN_TABLES)
        assert refs == []

    def test_join_skipped(self):
        # JOIN clauses should not be picked up as column refs
        sql = "SELECT a.wallet FROM user_analysis a JOIN paper_limit_orders b ON a.wallet = b.order_id"
        refs = extract_sql_column_refs(sql, KNOWN_TABLES)
        # We just verify no crash and JOIN-extracted "columns" aren't treated as
        # column refs for a known table incorrectly — exact behaviour: no refs returned
        assert isinstance(refs, list)

    def test_unknown_table_returns_empty(self):
        sql = "INSERT INTO nonexistent_table (col_a, col_b) VALUES (?, ?)"
        refs = extract_sql_column_refs(sql, KNOWN_TABLES)
        assert refs == []

    def test_multiline_insert(self):
        sql = textwrap.dedent("""\
            INSERT INTO paper_limit_orders (
                order_id,
                condition_id,
                bid_price,
                state
            ) VALUES (?, ?, ?, ?)
        """)
        refs = extract_sql_column_refs(sql, KNOWN_TABLES)
        assert ("paper_limit_orders", "order_id") in refs
        assert ("paper_limit_orders", "condition_id") in refs
        assert ("paper_limit_orders", "bid_price") in refs
        assert ("paper_limit_orders", "state") in refs

    def test_update_set(self):
        sql = "UPDATE paper_limit_orders SET state = ?, updated_at = ? WHERE order_id = ?"
        refs = extract_sql_column_refs(sql, KNOWN_TABLES)
        assert ("paper_limit_orders", "state") in refs
        assert ("paper_limit_orders", "updated_at") in refs


# ---------------------------------------------------------------------------
# validate_refs tests
# ---------------------------------------------------------------------------


class TestValidateRefs:
    def test_good_refs_no_violations(self):
        refs = [
            ("paper_limit_orders", "order_id"),
            ("paper_limit_orders", "state"),
            ("user_analysis", "wallet"),
        ]
        violations = validate_refs(refs, TEST_SCHEMA)
        assert violations == []

    def test_unknown_column_caught(self):
        refs = [
            ("paper_limit_orders", "order_id"),
            ("paper_limit_orders", "fake_col"),
        ]
        violations = validate_refs(refs, TEST_SCHEMA)
        assert len(violations) == 1
        v = violations[0]
        assert v.table == "paper_limit_orders"
        assert v.column == "fake_col"

    def test_multiple_violations(self):
        refs = [
            ("paper_limit_fills", "fill_id"),
            ("paper_limit_fills", "ghost_col"),
            ("user_analysis", "nonexistent"),
        ]
        violations = validate_refs(refs, TEST_SCHEMA)
        assert len(violations) == 2
        bad_cols = {(v.table, v.column) for v in violations}
        assert ("paper_limit_fills", "ghost_col") in bad_cols
        assert ("user_analysis", "nonexistent") in bad_cols

    def test_unknown_table_in_refs_is_skipped(self):
        # validate_refs should silently skip tables not in schema
        refs = [("mystery_table", "some_col")]
        violations = validate_refs(refs, TEST_SCHEMA)
        assert violations == []


# ---------------------------------------------------------------------------
# validate_file tests
# ---------------------------------------------------------------------------


class TestValidateFilePython:
    def test_py_file_with_bad_column(self, tmp_path):
        src = textwrap.dedent("""\
            import db

            def run():
                db.execute(
                    "INSERT INTO paper_limit_orders (order_id, fake_col) VALUES (?, ?)"
                )
        """)
        f = tmp_path / "example.py"
        f.write_text(src)
        violations = validate_file(str(f), TEST_SCHEMA, KNOWN_TABLES)
        assert any(v.column == "fake_col" and v.table == "paper_limit_orders" for v in violations)

    def test_py_file_clean(self, tmp_path):
        src = textwrap.dedent("""\
            import db

            def run():
                db.execute(
                    "INSERT INTO paper_limit_orders (order_id, state) VALUES (?, ?)"
                )
        """)
        f = tmp_path / "clean.py"
        f.write_text(src)
        violations = validate_file(str(f), TEST_SCHEMA, KNOWN_TABLES)
        assert violations == []

    def test_py_file_no_sql(self, tmp_path):
        src = textwrap.dedent("""\
            def greet(name):
                return f"hello {name}"
        """)
        f = tmp_path / "noop.py"
        f.write_text(src)
        violations = validate_file(str(f), TEST_SCHEMA, KNOWN_TABLES)
        assert violations == []


class TestValidateFileMarkdown:
    def test_md_python_block_bad_column(self, tmp_path):
        md = textwrap.dedent("""\
            # Example

            ```python
            sql = "INSERT INTO paper_limit_orders (order_id, bad_col) VALUES (?, ?)"
            db.execute(sql)
            ```
        """)
        f = tmp_path / "plan.md"
        f.write_text(md)
        violations = validate_file(str(f), TEST_SCHEMA, KNOWN_TABLES)
        assert any(v.column == "bad_col" for v in violations)

    def test_md_sql_block_bad_column(self, tmp_path):
        md = textwrap.dedent("""\
            # Migration

            ```sql
            INSERT INTO user_analysis (wallet, ghost_field) VALUES ('0xabc', 0);
            ```
        """)
        f = tmp_path / "migration.md"
        f.write_text(md)
        violations = validate_file(str(f), TEST_SCHEMA, KNOWN_TABLES)
        assert any(v.column == "ghost_field" and v.table == "user_analysis" for v in violations)

    def test_md_file_clean(self, tmp_path):
        md = textwrap.dedent("""\
            # Notes

            ```sql
            SELECT wallet, pnl FROM user_analysis WHERE insider_score > 50;
            ```
        """)
        f = tmp_path / "clean.md"
        f.write_text(md)
        violations = validate_file(str(f), TEST_SCHEMA, KNOWN_TABLES)
        assert violations == []

    def test_md_python_block_ast_fail_fallback(self, tmp_path):
        """Python block that fails AST parse but looks like SQL → fallback to raw SQL parse."""
        md = textwrap.dedent("""\
            # Pseudocode

            ```python
            INSERT INTO paper_limit_fills (fill_id, hallucinated_col) VALUES (?, ?)
            ```
        """)
        f = tmp_path / "pseudo.md"
        f.write_text(md)
        violations = validate_file(str(f), TEST_SCHEMA, KNOWN_TABLES)
        # fallback should detect the hallucinated column
        assert any(v.column == "hallucinated_col" for v in violations)

    def test_md_ignore_next_block_marker_skips_validation(self, tmp_path):
        md = textwrap.dedent("""\
            # Example

            <!-- schema-validator: ignore-next-block -->

            ```sql
            INSERT INTO paper_limit_orders (order_id, fake_col) VALUES (1, 2);
            ```
        """)
        f = tmp_path / "ignored.md"
        f.write_text(md)
        violations = validate_file(str(f), TEST_SCHEMA, KNOWN_TABLES)
        assert violations == []


class TestValidateColumnsMarker:
    """`<!-- schema:validate-columns-for=<table> -->` validates the first column
    of each data row in the pipe-table that immediately follows."""

    def test_marker_flags_unknown_column_in_pipe_table(self, tmp_path):
        md = textwrap.dedent("""\
            # Proposed columns

            <!-- schema:validate-columns-for=user_analysis -->

            | Column        | Type |
            |---------------|------|
            | wallet        | TEXT |
            | ghost_column  | REAL |
            | insider_score | REAL |
        """)
        f = tmp_path / "plan.md"
        f.write_text(md)
        violations = validate_file(str(f), TEST_SCHEMA, KNOWN_TABLES)
        bad = [v for v in violations if v.pattern == "md:marker"]
        assert any(v.column == "ghost_column" and v.table == "user_analysis" for v in bad)
        # wallet and insider_score are valid — must not fire
        assert not any(v.column in ("wallet", "insider_score") for v in bad)

    def test_marker_with_all_valid_columns_is_clean(self, tmp_path):
        md = textwrap.dedent("""\
            <!-- schema:validate-columns-for=user_analysis -->

            | Column        | Type |
            |---------------|------|
            | wallet        | TEXT |
            | pnl           | REAL |
        """)
        f = tmp_path / "clean.md"
        f.write_text(md)
        violations = validate_file(str(f), TEST_SCHEMA, KNOWN_TABLES)
        assert [v for v in violations if v.pattern == "md:marker"] == []

    def test_marker_for_unknown_table_itself_is_violation(self, tmp_path):
        """Asserting columns against a table that doesn't exist is itself an error."""
        md = textwrap.dedent("""\
            <!-- schema:validate-columns-for=nonexistent_table -->

            | Column | Type |
            |--------|------|
            | foo    | TEXT |
        """)
        f = tmp_path / "bad.md"
        f.write_text(md)
        violations = validate_file(str(f), TEST_SCHEMA, KNOWN_TABLES)
        assert any(v.pattern == "md:marker(unknown-table)" for v in violations)

    def test_marker_strips_backticks_and_qualifiers(self, tmp_path):
        """First-cell column names may have backticks or trailing type text."""
        md = textwrap.dedent("""\
            <!-- schema:validate-columns-for=paper_limit_orders -->

            | Column          | Type    |
            |-----------------|---------|
            | `order_id`      | INTEGER |
            | state NOT NULL  | TEXT    |
            | wrong_col       | TEXT    |
        """)
        f = tmp_path / "backtick.md"
        f.write_text(md)
        violations = validate_file(str(f), TEST_SCHEMA, KNOWN_TABLES)
        bad_cols = {v.column for v in violations if v.pattern == "md:marker"}
        assert bad_cols == {"wrong_col"}

    def test_marker_without_following_table_is_noop(self, tmp_path):
        """Marker with no table after it emits nothing (defensive)."""
        md = textwrap.dedent("""\
            <!-- schema:validate-columns-for=user_analysis -->

            just some prose, no table.
        """)
        f = tmp_path / "no_table.md"
        f.write_text(md)
        violations = validate_file(str(f), TEST_SCHEMA, KNOWN_TABLES)
        assert violations == []


class TestUnknownTableWarnings:
    """`collect_unknown_table_warnings` surfaces SQL references to tables not in the index."""

    def test_unknown_table_in_plan_is_warned(self, tmp_path):
        md = textwrap.dedent("""\
            # Plan

            ```sql
            SELECT slug, status FROM world_events WHERE status = 'active';
            ```
        """)
        f = tmp_path / "plan.md"
        f.write_text(md)
        warnings = collect_unknown_table_warnings(str(f), KNOWN_TABLES)
        tables = {w[2] for w in warnings}
        assert "world_events" in tables

    def test_known_table_does_not_warn(self, tmp_path):
        md = textwrap.dedent("""\
            ```sql
            SELECT wallet, pnl FROM user_analysis;
            ```
        """)
        f = tmp_path / "clean.md"
        f.write_text(md)
        warnings = collect_unknown_table_warnings(str(f), KNOWN_TABLES)
        assert warnings == []

    def test_external_marker_silences_warning(self, tmp_path):
        md = textwrap.dedent("""\
            <!-- schema:external=world_events -->

            ```sql
            SELECT slug FROM world_events;
            ```
        """)
        f = tmp_path / "ext.md"
        f.write_text(md)
        warnings = collect_unknown_table_warnings(str(f), KNOWN_TABLES)
        assert warnings == []

    def test_py_files_are_not_warned(self, tmp_path):
        """Warnings are plan/spec-only — .py files stay silent (too noisy otherwise)."""
        src = 'sql = "SELECT x FROM world_events"\n'
        f = tmp_path / "noop.py"
        f.write_text(src)
        warnings = collect_unknown_table_warnings(str(f), KNOWN_TABLES)
        assert warnings == []

    def test_cte_alias_is_not_warned(self, tmp_path):
        """CTE aliases (`WITH foo AS (...) SELECT ... FROM foo`) are valid self-refs
        within the same block — they must not produce unknown-table warnings.
        Regression: `first_seen` in docs/superpowers/plans/2026-03-11-replay-engine.md.
        """
        md = textwrap.dedent("""\
            ```python
            sql = f'''
                WITH first_seen AS (
                    SELECT wallet, condition_id
                    FROM signal_history
                    WHERE entry_anchor_ts > 0
                )
                SELECT wallet, condition_id FROM first_seen WHERE rn = 1
            '''
            ```
        """)
        f = tmp_path / "cte.md"
        f.write_text(md)
        warnings = collect_unknown_table_warnings(str(f), KNOWN_TABLES)
        tables = {w[2] for w in warnings}
        assert "first_seen" not in tables, f"CTE alias leaked as unknown table: {warnings}"

    def test_chained_cte_aliases_are_not_warned(self, tmp_path):
        """Chained CTEs (`WITH a AS (...), b AS (...) SELECT ... FROM b`) — second
        and subsequent aliases must also be whitelisted."""
        md = textwrap.dedent("""\
            ```sql
            WITH raw AS (
                SELECT wallet FROM signal_history
            ), ranked AS (
                SELECT wallet FROM raw
            )
            SELECT wallet FROM ranked
            ```
        """)
        f = tmp_path / "chain.md"
        f.write_text(md)
        warnings = collect_unknown_table_warnings(str(f), KNOWN_TABLES)
        tables = {w[2] for w in warnings}
        assert "raw" not in tables
        assert "ranked" not in tables


# ---------------------------------------------------------------------------
# collect_files tests
# ---------------------------------------------------------------------------


class TestCollectFiles:
    def test_explicit_file_list(self, tmp_path):
        a = tmp_path / "a.py"
        a.write_text("x = 1")
        b = tmp_path / "b.md"
        b.write_text("# hi")
        result = collect_files(file_list=[str(a), str(b)])
        assert str(a) in result
        assert str(b) in result

    def test_no_args_returns_files(self):
        """collect_files() with no args scans the repo and returns .py and .md files."""
        result = collect_files()
        assert isinstance(result, list)
        assert len(result) > 0
        exts = {Path(f).suffix for f in result}
        assert exts <= {".py", ".md"}

    def test_explicit_file_list_respects_excluded_dirs(self):
        """--files mode must honor _EXCLUDE_DIRS so the pre-commit hook and
        bulk CI run agree on what gets validated. Tests/ contains intentional
        legacy-schema references for migration tests; the bulk run skips them
        and the hook must too."""
        from pathlib import Path as _P

        # tests/test_tag_wallet_pnl.py is a real legacy-schema test file
        # under tests/ that contains an INSERT referencing avg_hold_days
        # (a dropped column) — exactly the case we want excluded.
        legacy_test = "tests/test_tag_wallet_pnl.py"
        assert _P(legacy_test).exists(), (
            "Fixture path went stale; pick another tests/ file"
        )
        result = collect_files(file_list=[legacy_test, "db.py"])
        assert "db.py" in result
        assert legacy_test not in result, (
            f"--files mode must skip tests/ to match bulk-run behavior; got {result}"
        )


# ---------------------------------------------------------------------------
# Supabase table grants (Data API stops auto-granting new tables 2026-10-30)
# ---------------------------------------------------------------------------

from validate_schema_refs import check_supabase_migration_grants  # noqa: E402


def _mig(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(textwrap.dedent(body))
    return p


def test_new_table_without_service_role_grant_fails(tmp_path: Path):
    p = _mig(tmp_path, "supabase_032_foo.sql", """
        CREATE TABLE IF NOT EXISTS public.foo (id uuid primary key);
    """)
    problems = check_supabase_migration_grants([p])
    assert len(problems) == 1
    assert "foo" in problems[0] and "service_role" in problems[0]


def test_a_commented_out_grant_does_not_count(tmp_path: Path):
    """Codex review 2026-09-23 Q1: the guard scanned raw text, so `-- GRANT ...` satisfied it."""
    p = _mig(tmp_path, "supabase_032_foo.sql", """
        create table public.foo (id uuid primary key);
        -- grant select on public.foo to service_role;
        /* grant select on public.foo to service_role; */
    """)
    problems = check_supabase_migration_grants([p])
    assert len(problems) == 1 and "foo" in problems[0]


def test_new_table_with_service_role_grant_passes(tmp_path: Path):
    p = _mig(tmp_path, "supabase_032_foo.sql", """
        create table public.foo (id uuid primary key);
        grant select, insert, update, delete on public.foo to service_role;
    """)
    assert check_supabase_migration_grants([p]) == []


def test_migrations_before_032_are_exempt(tmp_path: Path):
    p = _mig(tmp_path, "supabase_023_old.sql", """
        CREATE TABLE IF NOT EXISTS public.old (id uuid primary key);
    """)
    assert check_supabase_migration_grants([p]) == []


def test_committed_migrations_pass():
    paths = sorted((REPO_ROOT / "migrations").glob("supabase_*.sql"))
    assert check_supabase_migration_grants(paths) == []
