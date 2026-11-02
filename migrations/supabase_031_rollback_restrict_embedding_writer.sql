-- supabase_031_rollback_restrict_embedding_writer.sql
-- Restores the pre-031 (Supabase-default) grants on the embedding writer.

GRANT EXECUTE ON FUNCTION public.set_memory_embedding(uuid, text, text)
  TO anon, authenticated;

NOTIFY pgrst, 'reload schema';
