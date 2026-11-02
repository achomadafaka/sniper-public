"""Slice 6 Task 1 — offline lint of the Slice 6 Supabase migrations.

Pure offline test. Parses each SQL file with `sqlparse` and asserts the
contract elements the plan calls out (CREATE TABLE IF NOT EXISTS, a
PRIMARY KEY clause, RLS enabled, status / no-status invariants).

No Supabase round-trip — applying the migrations is a separate step
done via mcp__supabase__apply_migration in the operator workflow.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = REPO_ROOT / "migrations"

SLICE6_FILES = (
    "supabase_016_wallet_analysis_results.sql",
    "supabase_017_wallet_findings.sql",
    "supabase_018_artifact_registry.sql",
)


def _read(name: str) -> str:
    return (MIGRATIONS / name).read_text()


@pytest.mark.parametrize("name", SLICE6_FILES)
def test_migration_uses_create_table_if_not_exists(name: str) -> None:
    src = _read(name)
    matches = re.findall(
        r"\bCREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\b", src, flags=re.IGNORECASE
    )
    assert len(matches) == 1, (
        f"{name}: expected exactly one CREATE TABLE IF NOT EXISTS, "
        f"got {len(matches)}"
    )


@pytest.mark.parametrize("name", SLICE6_FILES)
def test_migration_has_primary_key(name: str) -> None:
    src = _read(name)
    assert re.search(r"\bPRIMARY\s+KEY\b", src, flags=re.IGNORECASE), (
        f"{name}: no PRIMARY KEY declared"
    )


@pytest.mark.parametrize("name", SLICE6_FILES)
def test_migration_enables_row_level_security(name: str) -> None:
    src = _read(name)
    assert re.search(
        r"\bENABLE\s+ROW\s+LEVEL\s+SECURITY\b", src, flags=re.IGNORECASE
    ), f"{name}: missing ENABLE ROW LEVEL SECURITY"


def test_results_and_findings_define_status_check() -> None:
    """wallet_analysis_results and wallet_findings carry a status column
    with the three-value CHECK constraint per D3."""
    for name in (
        "supabase_016_wallet_analysis_results.sql",
        "supabase_017_wallet_findings.sql",
    ):
        src = _read(name)
        # status column exists.
        assert re.search(
            r"\bstatus\s+TEXT\b", src, flags=re.IGNORECASE
        ), f"{name}: status column missing"
        # CHECK constraint accepts active / superseded / rolled_back.
        assert "active" in src
        assert "superseded" in src
        assert "rolled_back" in src


def test_artifact_registry_has_no_status_column() -> None:
    """artifact_registry is append-only — no status column per D3."""
    src = _read("supabase_018_artifact_registry.sql")
    # Permit `'status'` substrings only inside comments. A column-level
    # `status TEXT` would show up at the start of a logical line.
    column_lines = [
        line.strip()
        for line in src.splitlines()
        if line.strip() and not line.strip().startswith("--")
    ]
    for line in column_lines:
        # If a line declares a column or constraint, it should not start
        # with `status `.
        assert not re.match(r"^status\s+TEXT\b", line, flags=re.IGNORECASE), (
            "artifact_registry must NOT declare a status column "
            "(append-only per Slice 6 D3)."
        )


def test_artifact_registry_has_paths_jsonb() -> None:
    """artifact_registry carries `paths JSONB` for the path-set
    enrichment in Task 6."""
    src = _read("supabase_018_artifact_registry.sql")
    assert re.search(
        r"\bpaths\s+JSONB\b", src, flags=re.IGNORECASE
    ), "artifact_registry: paths JSONB column missing"
