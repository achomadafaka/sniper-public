-- Restore 035 before removing 033. Existing imported rows are preserved.
-- Apply before the new client. The old brain_supersede RPC remains compatible.
-- One stable client UUID owns the fact and all judged changes; a lost reply is safe to replay.
BEGIN;
CREATE OR REPLACE FUNCTION public.brain_write_fact(p_new jsonb, p_actions jsonb)
RETURNS SETOF public.brain_facts
LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
DECLARE
    new_id uuid := (p_new->>'id')::uuid;
    new_row public.brain_facts;
    old_row public.brain_facts;
    action jsonb;
    relation text;
    starts timestamptz := coalesce((p_new->>'valid_from')::timestamptz, now());
BEGIN
    IF new_id IS NULL OR jsonb_typeof(p_actions) IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION 'brain_write_fact: id and action array required';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(new_id::text, 0));
    SELECT * INTO new_row FROM public.brain_facts WHERE id = new_id;
    IF FOUND THEN
        IF (new_row.kind, new_row.statement, new_row.author, new_row.entity_ids)
           IS DISTINCT FROM (p_new->>'kind', p_new->>'statement', p_new->>'author',
               ARRAY(SELECT jsonb_array_elements_text(p_new->'entity_ids'))::uuid[]) THEN
            RAISE EXCEPTION 'brain_write_fact: operation_identity_conflict';
        END IF;
        RETURN NEXT new_row;
        RETURN;
    END IF;
    IF EXISTS (SELECT 1 FROM jsonb_array_elements(p_actions) a
               WHERE coalesce(a->>'verdict', '') NOT IN
                   ('supersedes','contradicts','refines','unrelated','candidate')
                  OR (a->>'id')::uuid IS NULL OR (a->>'id')::uuid = new_id)
       OR (SELECT count(*) <> count(DISTINCT a->>'id') FROM jsonb_array_elements(p_actions) a) THEN
        RAISE EXCEPTION 'brain_write_fact: invalid or duplicate action';
    END IF;
    -- Lock in the same order across writers. A stale judgement retries in the client; it never
    -- silently overwrites a newer lifecycle state. All changes roll back on a conflict.
    FOR action IN SELECT a FROM jsonb_array_elements(p_actions) a
                  WHERE a->>'verdict' IN ('supersedes','contradicts') ORDER BY a->>'id' LOOP
        SELECT * INTO old_row FROM public.brain_facts WHERE id = (action->>'id')::uuid FOR UPDATE;
        IF NOT FOUND OR old_row.version IS DISTINCT FROM (action->>'version')::int
           OR old_row.valid_to IS NOT NULL
           OR old_row.status NOT IN ('unverified','verified','verified_partial','disputed','evidence_broken') THEN
            RAISE EXCEPTION 'version_conflict';
        END IF;
        IF action->>'verdict' = 'supersedes' AND starts < old_row.valid_from THEN
            RAISE EXCEPTION 'supersede_backdated';
        END IF;
    END LOOP;
    INSERT INTO public.brain_facts (id, kind, statement, detail, entity_ids, polarity, evidence,
                                    author, author_session, episode_ids, valid_from, verify_by,
                                    judge_pending, status)
    VALUES (new_id, p_new->>'kind', p_new->>'statement', p_new->>'detail',
            ARRAY(SELECT jsonb_array_elements_text(p_new->'entity_ids'))::uuid[],
            coalesce((p_new->>'polarity')::smallint, 0), coalesce(p_new->'evidence', '[]'::jsonb),
            p_new->>'author', p_new->>'author_session',
            ARRAY(SELECT jsonb_array_elements_text(coalesce(p_new->'episode_ids', '[]'::jsonb)))::uuid[],
            starts, (p_new->>'verify_by')::timestamptz,
            EXISTS (SELECT 1 FROM jsonb_array_elements(p_actions) a WHERE a->>'verdict' = 'candidate'),
            CASE WHEN EXISTS (SELECT 1 FROM jsonb_array_elements(p_actions) a WHERE a->>'verdict' = 'contradicts')
                 THEN 'disputed' ELSE 'unverified' END)
    RETURNING * INTO new_row;
    FOR action IN SELECT a FROM jsonb_array_elements(p_actions) a LOOP
        relation := CASE action->>'verdict' WHEN 'refines' THEN 'derived_from'
                    WHEN 'unrelated' THEN NULL ELSE action->>'verdict' END;
        IF action->>'verdict' = 'supersedes' THEN
            UPDATE public.brain_facts SET status = 'superseded', valid_to = starts, expired_at = now()
             WHERE id = (action->>'id')::uuid;
        ELSIF action->>'verdict' = 'contradicts' THEN
            UPDATE public.brain_facts SET status = 'disputed' WHERE id = (action->>'id')::uuid;
        END IF;
        IF relation IS NOT NULL THEN
            INSERT INTO public.brain_links (from_id, from_kind, to_id, to_kind, rel, author)
            VALUES (new_id, 'fact', (action->>'id')::uuid, 'fact', relation, new_row.author);
        END IF;
    END LOOP;
    RETURN NEXT new_row;
END $$;
REVOKE EXECUTE ON FUNCTION public.brain_write_fact(jsonb, jsonb) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.brain_write_fact(jsonb, jsonb) TO service_role;
NOTIFY pgrst, 'reload schema';
COMMIT;
