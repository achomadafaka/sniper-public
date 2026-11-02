-- supabase_029_memories_semantic.sql
-- Semantic (vector) search for the `memories` brain — layered ON TOP of the
-- migration-028 full-text search, not a replacement.
--
-- Why: FTS (028) matches stems/keywords; it misses paraphrase ("keep tests from
-- being too optimistic" ↔ a memory about "walk-forward validation / overfitting"
-- that shares no keywords). Embeddings catch conceptual matches.
--
-- Embeddings are produced locally by fastembed BAAI/bge-small-en (384-dim,
-- ONNX/CPU, no API key, no data egress — sourced from the Qdrant GCS bucket
-- because huggingface.co is DNS-blocked on this host). See scripts/embed_memories.py.
--
-- Vector params are TEXT (cast to vector inside) so the raw PostgREST/urllib path
-- can pass "[0.1,0.2,...]" without JSON-array→vector coercion ambiguity.

CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE public.memories
  ADD COLUMN IF NOT EXISTS embedding vector(384),
  ADD COLUMN IF NOT EXISTS embedding_hash text;  -- sha256 of embedded text; skip re-embed when unchanged

CREATE INDEX IF NOT EXISTS idx_memories_embedding_hnsw
  ON public.memories USING hnsw (embedding vector_cosine_ops);

-- Ranked nearest-neighbour search. SECURITY INVOKER → RLS still applies to the
-- caller; SET search_path = '' + fully-qualified refs satisfy the advisor.
CREATE OR REPLACE FUNCTION public.match_memories(
    query_embedding text,
    match_count integer DEFAULT 10,
    active_only boolean DEFAULT true
)
RETURNS TABLE (
    id uuid,
    type text,
    name text,
    description text,
    content text,
    status text,
    updated_at timestamptz,
    similarity real
)
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = ''
AS $$
    -- pgvector installed in `public`; qualify type + operator since search_path=''.
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

-- Backfill writer. service_role only (bypasses RLS); the embed job holds the
-- service key. Not exposed to anon/authenticated.
CREATE OR REPLACE FUNCTION public.set_memory_embedding(
    p_id uuid,
    p_embedding text,
    p_hash text
)
RETURNS void
LANGUAGE sql
SECURITY INVOKER
SET search_path = ''
AS $$
    UPDATE public.memories
    SET embedding = p_embedding::public.vector,
        embedding_hash = p_hash
    WHERE id = p_id;
$$;

GRANT EXECUTE ON FUNCTION public.set_memory_embedding(uuid, text, text)
  TO service_role;

NOTIFY pgrst, 'reload schema';
