-- Bounded missing-only embedding writes. No new fact/status authority.
BEGIN;
CREATE OR REPLACE FUNCTION public.brain_set_embeddings(p_rows jsonb)
RETURNS integer LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
DECLARE
    r jsonb;
    target_id uuid;
    written integer := 0;
BEGIN
    IF p_rows IS NULL OR jsonb_typeof(p_rows) <> 'array' THEN
        RAISE EXCEPTION 'brain_set_embeddings: expected array';
    END IF;
    IF jsonb_array_length(p_rows) > 64 THEN
        RAISE EXCEPTION 'brain_set_embeddings: maximum 64 rows';
    END IF;
    -- Consistent ordering prevents overlapping batch writers taking reverse locks.
    FOR r IN SELECT value FROM jsonb_array_elements(p_rows) ORDER BY value->>'table', value->>'id'
    LOOP
        IF jsonb_typeof(r) <> 'object' OR r->>'id' IS NULL OR r->>'embedding' IS NULL THEN
            RAISE EXCEPTION 'brain_set_embeddings: malformed row';
        END IF;
        target_id := (r->>'id')::uuid;
        CASE r->>'table'
        WHEN 'brain_facts' THEN
            PERFORM id FROM public.brain_facts WHERE id = target_id AND embedding IS NULL FOR UPDATE;
        WHEN 'brain_entities' THEN
            PERFORM id FROM public.brain_entities WHERE id = target_id AND embedding IS NULL FOR UPDATE;
        WHEN 'brain_episodes' THEN
            PERFORM id FROM public.brain_episodes WHERE id = target_id AND embedding IS NULL FOR UPDATE;
        ELSE
            RAISE EXCEPTION 'brain_set_embeddings: unknown table';
        END CASE;
        IF FOUND THEN
            PERFORM public.brain_set_embedding(r->>'table', target_id, r->>'embedding');
            written := written + 1;
        END IF;
    END LOOP;
    RETURN written;
END $$;
REVOKE EXECUTE ON FUNCTION public.brain_set_embeddings(jsonb) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.brain_set_embeddings(jsonb) TO service_role;
COMMIT;
