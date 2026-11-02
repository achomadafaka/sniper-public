-- supabase_029_rollback_memories_semantic.sql
-- Reverses supabase_029_memories_semantic.sql.
-- Leaves the `vector` extension installed (other objects may depend on it;
-- dropping an extension is a separate, deliberate op).

DROP FUNCTION IF EXISTS public.set_memory_embedding(uuid, text, text);
DROP FUNCTION IF EXISTS public.match_memories(text, integer, boolean);
DROP INDEX IF EXISTS public.idx_memories_embedding_hnsw;
ALTER TABLE public.memories
  DROP COLUMN IF EXISTS embedding,
  DROP COLUMN IF EXISTS embedding_hash;

NOTIFY pgrst, 'reload schema';
