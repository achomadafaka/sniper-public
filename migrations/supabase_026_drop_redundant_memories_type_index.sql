-- supabase_026_drop_redundant_memories_type_index.sql
-- idx_memories_type (type) is redundant: idx_memories_type_status_updated
-- (type, status, updated_at DESC) leads with `type`, so it serves any type-prefix
-- lookup the standalone index would. Removing the duplicate loses no query coverage.
DROP INDEX IF EXISTS public.idx_memories_type;
