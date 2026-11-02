-- Memory lifecycle metadata.
--
-- Human-reviewed retirement is intentional: agents propose status changes, but
-- no automated process should flip active memories to superseded/retired
-- without an explicit batch update.

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active';

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS superseded_by TEXT;

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS valid_as_of_commit TEXT;

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

CREATE INDEX IF NOT EXISTS idx_memories_status
    ON memories (status);

CREATE INDEX IF NOT EXISTS idx_memories_type_status_updated
    ON memories (type, status, updated_at DESC);

-- Keep schema aligned with bot.supabase_helper.save_finding(source='agent').
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'memories_source_check'
          AND conrelid = 'memories'::regclass
    ) THEN
        ALTER TABLE memories DROP CONSTRAINT memories_source_check;
    END IF;
END $$;

ALTER TABLE memories
    ADD CONSTRAINT memories_source_check
    CHECK (source IN ('vps', 'local', 'agent'));
