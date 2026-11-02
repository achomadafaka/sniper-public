-- supabase_030_vector_to_extensions.sql
-- Clears the `extension_in_public` security advisor WARN introduced by 029
-- (CREATE EXTENSION vector defaulted into public). Move pgvector into the
-- `extensions` schema and repoint the two RPCs (their search_path='' bodies
-- must fully-qualify the type/operator, now extensions.*).
--
-- The `memories.embedding` column, the HNSW index, and stored embeddings are
-- unaffected: they reference the moved type/opclass by OID, which is stable
-- across a schema move.

ALTER EXTENSION vector SET SCHEMA extensions;

CREATE OR REPLACE FUNCTION public.match_memories(
    query_embedding text,
    match_count integer DEFAULT 10,
    active_only boolean DEFAULT true
)
RETURNS TABLE (
    id uuid, type text, name text, description text, content text,
    status text, updated_at timestamptz, similarity real
)
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = ''
AS $$
    SELECT m.id, m.type, m.name, m.description, m.content, m.status, m.updated_at,
           (1 - (m.embedding OPERATOR(extensions.<=>) query_embedding::extensions.vector))::real AS similarity
    FROM public.memories m
    WHERE m.embedding IS NOT NULL
      AND (NOT active_only OR m.status = 'active')
    ORDER BY m.embedding OPERATOR(extensions.<=>) query_embedding::extensions.vector
    LIMIT greatest(1, match_count);
$$;

GRANT EXECUTE ON FUNCTION public.match_memories(text, integer, boolean)
  TO anon, authenticated, service_role;

CREATE OR REPLACE FUNCTION public.set_memory_embedding(
    p_id uuid, p_embedding text, p_hash text
)
RETURNS void LANGUAGE sql SECURITY INVOKER SET search_path = ''
AS $$
    UPDATE public.memories
    SET embedding = p_embedding::extensions.vector, embedding_hash = p_hash
    WHERE id = p_id;
$$;

GRANT EXECUTE ON FUNCTION public.set_memory_embedding(uuid, text, text)
  TO service_role;

NOTIFY pgrst, 'reload schema';
