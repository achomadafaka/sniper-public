-- Rollback for supabase_026_drop_redundant_memories_type_index.sql
CREATE INDEX IF NOT EXISTS idx_memories_type ON public.memories USING btree (type);
