-- supabase_030_rollback_vector_to_extensions.sql
-- Reverses supabase_030: move pgvector back to public and repoint the RPCs.

ALTER EXTENSION vector SET SCHEMA public;

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
           (1 - (m.embedding OPERATOR(public.<=>) query_embedding::public.vector))::real AS similarity
    FROM public.memories m
    WHERE m.embedding IS NOT NULL
      AND (NOT active_only OR m.status = 'active')
    ORDER BY m.embedding OPERATOR(public.<=>) query_embedding::public.vector
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
    SET embedding = p_embedding::public.vector, embedding_hash = p_hash
    WHERE id = p_id;
$$;

GRANT EXECUTE ON FUNCTION public.set_memory_embedding(uuid, text, text)
  TO service_role;

NOTIFY pgrst, 'reload schema';
