-- Roll back the client first and drain any new outbox/inflight writes before dropping the RPC.
-- Existing facts, links, and the original brain_supersede RPC are unchanged.
BEGIN;
DROP FUNCTION IF EXISTS public.brain_write_fact(jsonb, jsonb);
NOTIFY pgrst, 'reload schema';
COMMIT;
