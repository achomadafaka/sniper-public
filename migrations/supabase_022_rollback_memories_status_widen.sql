-- Rollback supabase_022_memories_status_widen.sql.
--
-- Restores the legacy 3-status check constraint from supabase_015. Any
-- rows already written with widened lifecycle statuses MUST be
-- reconciled to one of the legacy three before running this rollback,
-- or ADD CONSTRAINT will fail because existing data violates the check.
-- See the widened migration for the full list and pick collapse rules
-- appropriate to your data before running this file.
--
-- Idempotent: DROP CONSTRAINT IF EXISTS, ADD CONSTRAINT IF NOT EXISTS.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'memories_status_check'
          AND conrelid = 'memories'::regclass
    ) THEN
        ALTER TABLE memories DROP CONSTRAINT memories_status_check;
    END IF;
END $$;

ALTER TABLE memories
    ADD CONSTRAINT memories_status_check
    CHECK (status IN ('active', 'superseded', 'retired'));
