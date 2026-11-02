"""Tests for `supabase_022_memories_status_widen.sql` (+ rollback).

The DB-level CHECK constraint on `memories.status` is the ground-truth
contract. Python helpers can validate locally, but a row whose status the
DB rejects will fail at write time regardless. This migration widens the
constraint to the full KSG lifecycle so the helper, the validator, and
the live DB all agree.

These tests read the migration files as text — they do not connect to
Supabase. The semantics being pinned:

  * The forward migration drops the existing `memories_status_check`
    constraint (idempotent / DROP IF EXISTS pattern) and re-adds it with
    the full 7-status set.
  * The rollback migration restores the original 3-status set so a bad
    deploy can be reversed without data loss.
  * Both migrations preserve `status NOT NULL DEFAULT 'active'` semantics.
"""
from __future__ import annotations

from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

FORWARD = MIGRATIONS_DIR / "supabase_022_memories_status_widen.sql"
ROLLBACK = MIGRATIONS_DIR / "supabase_022_rollback_memories_status_widen.sql"

EXPECTED_FULL_STATUSES = {
    "active", "superseded", "retired",
    "draft", "archived", "rejected", "needs_review",
}

EXPECTED_LEGACY_STATUSES = {"active", "superseded", "retired"}


def _read(path: Path) -> str:
    assert path.is_file(), f"missing migration: {path}"
    return path.read_text()


def test_forward_migration_exists():
    assert FORWARD.is_file(), (
        f"forward migration {FORWARD.name} not found — required to widen "
        "memories.status_check beyond active/superseded/retired"
    )


def test_rollback_migration_exists():
    assert ROLLBACK.is_file(), (
        f"rollback migration {ROLLBACK.name} not found — every status-vocab "
        "change must ship with a rollback companion (project convention)"
    )


def test_forward_migration_drops_existing_status_check():
    """Idempotency: must DROP CONSTRAINT IF EXISTS before re-adding so the
    migration can re-run safely on partially-applied environments."""
    sql = _read(FORWARD)
    sql_lower = sql.lower()
    assert "drop constraint" in sql_lower and "memories_status_check" in sql_lower, (
        "forward migration must drop memories_status_check before re-adding"
    )


def test_forward_migration_lists_all_seven_statuses():
    """The new CHECK constraint must enumerate every widened status."""
    sql = _read(FORWARD)
    for status in EXPECTED_FULL_STATUSES:
        assert f"'{status}'" in sql, (
            f"forward migration does not include status {status!r} in the "
            "CHECK clause"
        )


def test_forward_migration_re_adds_status_check_constraint():
    sql = _read(FORWARD).lower()
    assert "add constraint" in sql and "memories_status_check" in sql, (
        "forward migration must re-ADD memories_status_check after dropping"
    )


def test_forward_migration_preserves_not_null_default_active():
    """Must not break the existing default. Either no ALTER COLUMN
    touching status, or an explicit re-assertion of NOT NULL DEFAULT 'active'.
    Easiest check: never drop NOT NULL, never set a different default."""
    sql = _read(FORWARD).lower()
    assert "drop not null" not in sql, (
        "forward migration must not drop NOT NULL on memories.status"
    )
    assert "default 'superseded'" not in sql and "default 'retired'" not in sql, (
        "forward migration must not change the column default away from 'active'"
    )


def test_rollback_migration_restores_three_statuses_only():
    """Rollback must produce the original 3-status constraint."""
    sql = _read(ROLLBACK)
    for status in EXPECTED_LEGACY_STATUSES:
        assert f"'{status}'" in sql, (
            f"rollback migration missing legacy status {status!r}"
        )
    for status in EXPECTED_FULL_STATUSES - EXPECTED_LEGACY_STATUSES:
        assert f"'{status}'" not in sql, (
            f"rollback migration still references widened status {status!r}"
        )


def test_rollback_migration_drops_existing_constraint_first():
    sql = _read(ROLLBACK).lower()
    assert "drop constraint" in sql and "memories_status_check" in sql, (
        "rollback must drop the widened constraint before re-adding legacy"
    )
