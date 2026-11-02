-- supabase_031_restrict_embedding_writer.sql
-- Lock the embedding WRITER RPC to service_role.
--
-- set_memory_embedding() runs `UPDATE public.memories SET embedding=...`. It is
-- meant to be service_role-only (the backfill job holds the service key). But
-- Supabase's default privileges auto-GRANT EXECUTE on new public functions to
-- anon + authenticated, and the 029/030 `GRANT ... TO service_role` only ADDED
-- to that — it did not revoke the default. Net effect: anon could EXECUTE the
-- writer.
--
-- Not exploitable today: memories has RLS enabled with only a public SELECT
-- policy (no anon UPDATE policy), so the SECURITY INVOKER UPDATE is denied for
-- anon. This REVOKE is defense-in-depth so the writer never depends solely on
-- that RLS shape staying put. Reader RPCs (match_memories / search_memories_fts)
-- intentionally keep anon EXECUTE — anon can already SELECT memories.

REVOKE EXECUTE ON FUNCTION public.set_memory_embedding(uuid, text, text)
  FROM PUBLIC, anon, authenticated;

NOTIFY pgrst, 'reload schema';
