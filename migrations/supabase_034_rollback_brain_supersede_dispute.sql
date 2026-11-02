-- Restore migration 032's supersede function. Does not change existing fact rows.
CREATE OR REPLACE FUNCTION public.brain_supersede(p_old_id uuid, p_old_version int, p_new jsonb)
RETURNS SETOF public.brain_facts
LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
DECLARE
    new_row public.brain_facts;
    starts timestamptz := coalesce((p_new->>'valid_from')::timestamptz, now());
    old_from timestamptz;
BEGIN
    -- A new fact cannot begin before the fact it replaces. Refuse by name rather than move a date:
    -- the importer's valid_from is evidence, and silently changing it would falsify history.
    SELECT valid_from INTO old_from FROM public.brain_facts WHERE id = p_old_id;
    IF old_from IS NOT NULL AND starts < old_from THEN
        RAISE EXCEPTION 'supersede_backdated: the new fact starts % but the old one started %', starts, old_from;
    END IF;
    UPDATE public.brain_facts
       SET valid_to = starts, expired_at = now(), status = 'superseded'
     WHERE id = p_old_id AND version = p_old_version AND valid_to IS NULL;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'version_conflict';
    END IF;
    INSERT INTO public.brain_facts (kind, statement, detail, entity_ids, polarity, evidence, author,
                                    author_session, episode_ids, valid_from, verify_by, judge_pending)
    VALUES (p_new->>'kind', p_new->>'statement', p_new->>'detail',
            ARRAY(SELECT jsonb_array_elements_text(p_new->'entity_ids'))::uuid[],
            coalesce((p_new->>'polarity')::smallint, 0), coalesce(p_new->'evidence', '[]'::jsonb),
            p_new->>'author', p_new->>'author_session',
            ARRAY(SELECT jsonb_array_elements_text(coalesce(p_new->'episode_ids', '[]'::jsonb)))::uuid[],
            starts, (p_new->>'verify_by')::timestamptz, coalesce((p_new->>'judge_pending')::boolean, false))
    RETURNING * INTO new_row;
    INSERT INTO public.brain_links (from_id, from_kind, to_id, to_kind, rel, author)
    VALUES (new_row.id, 'fact', p_old_id, 'fact', 'supersedes', new_row.author);
    RETURN NEXT new_row;
END $$;
