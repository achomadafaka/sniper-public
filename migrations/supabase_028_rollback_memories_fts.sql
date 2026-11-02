-- supabase_028_rollback_memories_fts.sql
-- Reverses supabase_028_memories_fts.sql.

DROP FUNCTION IF EXISTS public.search_memories_fts(text, boolean, integer);
DROP INDEX IF EXISTS public.idx_memories_search_vector;
ALTER TABLE public.memories DROP COLUMN IF EXISTS search_vector;

NOTIFY pgrst, 'reload schema';
