-- supabase_032_rollback_brain_graph.sql
-- Reverses supabase_032_brain_graph.sql. Leaves the vector extension installed. Tables go before the
-- trigger functions they use; DROP TABLE removes their triggers and policies.
DROP FUNCTION IF EXISTS public.brain_set_embedding(text, uuid, text);
DROP FUNCTION IF EXISTS public.brain_match_entities(text, int);
DROP FUNCTION IF EXISTS public.brain_match_facts(text, int);
DROP FUNCTION IF EXISTS public.brain_facts_for_entities(uuid[], text[], boolean);
DROP FUNCTION IF EXISTS public.brain_queue_count(uuid[], text);
DROP FUNCTION IF EXISTS public.brain_facts_in_sections(uuid[], text[], boolean, text, boolean, int);
DROP FUNCTION IF EXISTS public.brain_supersede(uuid, int, jsonb);
DROP TABLE IF EXISTS public.brain_links;
DROP TABLE IF EXISTS public.brain_facts;
DROP TABLE IF EXISTS public.brain_entities;
DROP TABLE IF EXISTS public.brain_episodes;
DROP TABLE IF EXISTS public.brain_views;
DROP TABLE IF EXISTS public.brain_health;
DROP TABLE IF EXISTS public.brain_sections;
DROP FUNCTION IF EXISTS public.brain_facts_no_delete();
DROP FUNCTION IF EXISTS public.brain_facts_before();
DROP FUNCTION IF EXISTS public.brain_evidence_refs(jsonb);
DROP FUNCTION IF EXISTS public.brain_entities_before();
DROP FUNCTION IF EXISTS public.brain_episodes_append_only();
NOTIFY pgrst, 'reload schema';
