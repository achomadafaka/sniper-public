-- Widen memories.status lifecycle to cover the full KSG vocabulary.
--
-- Background: supabase_015 locked the CHECK constraint to
-- {active, superseded, retired}. The KSG validator
-- (scripts/knowledge_sync.py) already accepted draft/archived/rejected/
-- needs_review entries, so any non-active entry validated locally then
-- failed at write time. This migration aligns the DB contract with the
-- Python contract.
--
-- New vocabulary:
--   active          — live, surfaced to default reads
--   superseded      — replaced by a newer entry (carry superseded_by)
--   retired         — intentionally withdrawn, kept for history
--   draft           — staged but not committed; not yet authoritative
--   archived        — frozen snapshot kept for context, not live work
--   rejected        — submitted but declined; do not act on
--   needs_review    — flagged for human triage before promotion
--
-- Default read filter (`status=eq.active`) is preserved by leaving
-- `status NOT NULL DEFAULT 'active'` intact.
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
    CHECK (status IN (
        'active',
        'superseded',
        'retired',
        'draft',
        'archived',
        'rejected',
        'needs_review'
    ));
