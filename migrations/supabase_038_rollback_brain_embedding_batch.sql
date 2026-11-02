-- Additive rollback; existing embeddings and the single-row writer remain.
BEGIN;
DROP FUNCTION IF EXISTS public.brain_set_embeddings(jsonb);
COMMIT;
