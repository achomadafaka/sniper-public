-- Rollback for 040: restore the EXISTS ... e.id = ANY(f.entity_ids) section filter.
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
       AND EXISTS (SELECT 1 FROM public.brain_entities e
                    WHERE e.id = ANY(f.entity_ids) AND e.section_id = ANY(p_section_ids));
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
       AND EXISTS (SELECT 1 FROM public.brain_entities e
                    WHERE e.id = ANY(f.entity_ids) AND e.section_id = ANY(p_section_ids))
       AND (p_kinds IS NULL OR f.kind = ANY(p_kinds))
       AND (NOT p_queue_only OR f.status = 'unverified' OR f.judge_pending)
       AND (p_exclude_author IS NULL OR f.author <> p_exclude_author)
     ORDER BY CASE WHEN p_oldest_first THEN f.recorded_at END ASC,
              CASE WHEN NOT p_oldest_first THEN f.recorded_at END DESC
     LIMIT greatest(1, p_limit);
$function$;
