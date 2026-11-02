-- 040: brain boot reads were CPU-bound in Postgres (Alpha, 2026-10-08).
-- brain_queue_count took 5.1 s and brain_facts_in_sections ~6 s, so every boot read timed out at
-- the 3 s client timeout and the 10 s boot fell back to a stale snapshot ("BRAIN UNREACHABLE").
-- The section filter `EXISTS (... WHERE e.id = ANY(f.entity_ids) AND e.section_id = ANY(...))`
-- scanned brain_entities once per candidate fact. Unnesting the fact's own entity ids and joining
-- on the entity primary key gives the same rows (checked: identical counts and id sets on two
-- parameter sets) at ~1.2 s. Signatures, return types, STABLE and search_path are unchanged.
-- Rollback: migrations/supabase_040_rollback_brain_section_filter_join.sql.

CREATE OR REPLACE FUNCTION public.brain_queue_count(p_section_ids uuid[], p_exclude_author text DEFAULT NULL::text)
 RETURNS bigint
 LANGUAGE sql
 STABLE
 SET search_path TO ''
AS $function$
    SELECT count(*) FROM public.brain_facts f
     WHERE f.valid_to IS NULL AND f.kind <> 'observation'
       AND (f.status = 'unverified' OR f.judge_pending)
       AND (p_exclude_author IS NULL OR f.author <> p_exclude_author)
       AND EXISTS (SELECT 1 FROM unnest(f.entity_ids) AS u(id)
                     JOIN public.brain_entities e ON e.id = u.id
                    WHERE e.section_id = ANY(p_section_ids));
$function$;

CREATE OR REPLACE FUNCTION public.brain_facts_in_sections(p_section_ids uuid[], p_kinds text[] DEFAULT NULL::text[], p_queue_only boolean DEFAULT false, p_exclude_author text DEFAULT NULL::text, p_oldest_first boolean DEFAULT false, p_limit integer DEFAULT 500)
 RETURNS SETOF public.brain_facts
 LANGUAGE sql
 STABLE
 SET search_path TO ''
AS $function$
    SELECT f.* FROM public.brain_facts f
     WHERE f.valid_to IS NULL
       AND f.status IN ('unverified','verified','verified_partial','disputed','evidence_broken')
       AND f.kind <> 'observation'
       AND EXISTS (SELECT 1 FROM unnest(f.entity_ids) AS u(id)
                     JOIN public.brain_entities e ON e.id = u.id
                    WHERE e.section_id = ANY(p_section_ids))
       AND (p_kinds IS NULL OR f.kind = ANY(p_kinds))
       AND (NOT p_queue_only OR f.status = 'unverified' OR f.judge_pending)
       AND (p_exclude_author IS NULL OR f.author <> p_exclude_author)
     ORDER BY CASE WHEN p_oldest_first THEN f.recorded_at END ASC,
              CASE WHEN NOT p_oldest_first THEN f.recorded_at END DESC
     LIMIT greatest(1, p_limit);
$function$;
