-- supabase_028_memories_fts.sql
-- Full-text search for the `memories` brain.
--
-- Why: retrieval was ilike-substring only (`search_memories()`), so an agent
-- found a memory only by guessing an exact substring of its name/content.
-- Multi-word or stemmed queries ("memecoin exit latency") missed everything.
-- pgvector is available but not installed; native Postgres FTS needs no
-- extension and directly serves "agents find relevant research".
--
-- This migration is ADDITIVE and NON-BREAKING. The existing substring
-- `search_memories()` path is left untouched because live callers
-- (world_analyst / domain_query / sync_session_findings) use it as a
-- structured-key PREFIX lookup ("country:", "flag:", "session/"), which FTS
-- tokenization would silently break. New free-text retrieval goes through
-- `search_memories_fts()`.

-- 1. Weighted generated tsvector column.
--    Weights: name = A (highest), summary+description = B, content = C.
--    to_tsvector('english', ...) (the 2-arg regconfig,text overload) is
--    IMMUTABLE — required for a generated column. The 1-arg form is only
--    STABLE and would be rejected.
--    Tags are intentionally omitted: array_to_string() is STABLE (not
--    immutable) so it cannot appear here, and it isn't needed — save_finding()
--    inlines tags into `content` (indexed below at weight C) and the dedicated
--    idx_memories_tags_gin serves exact-tag filters.
ALTER TABLE public.memories
  ADD COLUMN IF NOT EXISTS search_vector tsvector
  GENERATED ALWAYS AS (
       setweight(to_tsvector('english', coalesce(name, '')), 'A')
    || setweight(to_tsvector('english',
         coalesce(summary, '') || ' ' || coalesce(description, '')), 'B')
    || setweight(to_tsvector('english', coalesce(content, '')), 'C')
  ) STORED;

-- 2. GIN index backing the FTS filter.
CREATE INDEX IF NOT EXISTS idx_memories_search_vector
  ON public.memories USING gin (search_vector);

-- 3. Ranked RPC. SECURITY INVOKER so row-level security still applies to the
--    caller (anon sees only what memories_select allows; service_role bypasses
--    RLS). SET search_path = '' + fully-qualified public.memories satisfies the
--    function_search_path_mutable security advisor; pg_catalog FTS builtins
--    (websearch_to_tsquery / ts_rank) resolve implicitly.
CREATE OR REPLACE FUNCTION public.search_memories_fts(
    q text,
    active_only boolean DEFAULT true,
    max_results integer DEFAULT 50
)
RETURNS SETOF public.memories
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = ''
AS $$
    SELECT m.*
    FROM public.memories m,
         websearch_to_tsquery('english', q) AS query
    WHERE m.search_vector @@ query
      AND (NOT active_only OR m.status = 'active')
    ORDER BY ts_rank(m.search_vector, query) DESC, m.updated_at DESC
    LIMIT greatest(1, max_results);
$$;

GRANT EXECUTE ON FUNCTION public.search_memories_fts(text, boolean, integer)
  TO anon, authenticated, service_role;

-- Tell PostgREST to expose the new RPC immediately.
NOTIFY pgrst, 'reload schema';
