-- supabase_032_brain_graph.sql
-- The brain as a verified knowledge graph
-- (docs/superpowers/specs/2026-09-28-brain-knowledge-graph-design.md; plan
-- docs/superpowers/plans/2026-09-28-brain-knowledge-graph-phase1.md, Task 1 with the review amendments).
--
-- Seven tables beside `memories`, which is untouched. The DATABASE enforces:
--   * an append-only episode log;
--   * immutable fact statements, kinds, authors, entities and valid_from (supersede instead);
--   * valid_to and expired_at written once, so a superseded fact cannot be revived;
--   * a changed evidence REFERENCE voids an earlier verification;
--   * no verification of a disputed or judge-pending fact;
--   * server-set recorded_at, version and default expiry;
--   * a verifier who is not the author (R15: the author string is self-asserted, so this is an
--     audit rule, not access control);
--   * every entity has a section (otherwise its facts are invisible at boot) and a known type;
--   * entity names and aliases resolve exactly (R1): name_lc is unique per type, aliases are
--     lower-cased and unique per type.
-- Every session writes with the service_role key, which bypasses RLS and grants, so these rules are
-- triggers and CHECKs, not permissions. Anon and authenticated are denied on every table, and every
-- RPC is revoked from them (the supabase_031 lesson: Supabase auto-grants EXECUTE on new functions).

CREATE TABLE IF NOT EXISTS public.brain_sections (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    parent_id uuid REFERENCES public.brain_sections(id),
    slug text NOT NULL UNIQUE,
    title text NOT NULL,
    summary text,
    summary_at timestamptz
);

CREATE TABLE IF NOT EXISTS public.brain_episodes (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind text NOT NULL CHECK (kind IN ('handoff_line','operator_order','crew_report','command_output',
                                       'commit','doc','transcript_excerpt','legacy_memory','handover',
                                       'selftest')),
    body text NOT NULL CHECK (length(body) <= 1800),
    parent_id uuid REFERENCES public.brain_episodes(id),
    part_no int NOT NULL DEFAULT 1 CHECK (part_no >= 1),
    author text NOT NULL CHECK (author IN ('alpha','beta','gamma','lambda','operator','cron','import')),
    session_id text,
    -- NULL means the time is UNKNOWN (an undated HANDOFF block), never "now".
    occurred_at timestamptz,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    source_ref text,
    embedding extensions.vector(384),
    search_vector tsvector GENERATED ALWAYS AS (to_tsvector('english', body)) STORED
);
CREATE INDEX IF NOT EXISTS brain_episodes_fts ON public.brain_episodes USING gin (search_vector);
CREATE INDEX IF NOT EXISTS brain_episodes_recorded ON public.brain_episodes (recorded_at DESC);
CREATE INDEX IF NOT EXISTS brain_episodes_parent ON public.brain_episodes (parent_id);

CREATE OR REPLACE FUNCTION public.brain_episodes_append_only() RETURNS trigger
LANGUAGE plpgsql SET search_path = '' AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'brain_episodes is append-only';
    END IF;
    IF (NEW.kind, NEW.body, NEW.parent_id, NEW.part_no, NEW.author, NEW.session_id,
        NEW.occurred_at, NEW.recorded_at, NEW.source_ref)
       IS DISTINCT FROM
       (OLD.kind, OLD.body, OLD.parent_id, OLD.part_no, OLD.author, OLD.session_id,
        OLD.occurred_at, OLD.recorded_at, OLD.source_ref) THEN
        RAISE EXCEPTION 'brain_episodes is append-only: only the embedding may be set';
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS brain_episodes_append_only ON public.brain_episodes;
CREATE TRIGGER brain_episodes_append_only BEFORE UPDATE OR DELETE ON public.brain_episodes
    FOR EACH ROW EXECUTE FUNCTION public.brain_episodes_append_only();

CREATE TABLE IF NOT EXISTS public.brain_entities (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    type text NOT NULL CHECK (type IN ('actor','chain','commit','process','exit_rail','venue','path',
                                       'provider','wallet','knob','lane','table','column','position',
                                       'gate','spec','audit','task','release','mint','doctrine',
                                       'credential_id','tool','selftest')),
    name text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 300),
    name_lc text GENERATED ALWAYS AS (lower(name)) STORED,
    aliases text[] NOT NULL DEFAULT '{}',
    summary text CHECK (summary IS NULL OR length(summary) <= 400),
    -- NOT NULL: views select facts through their entities' sections, so an unsectioned entity's
    -- facts would never reach a boot or a verify queue.
    section_id uuid NOT NULL REFERENCES public.brain_sections(id),
    embedding extensions.vector(384),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS brain_entities_type_name ON public.brain_entities (type, name_lc);
CREATE INDEX IF NOT EXISTS brain_entities_aliases ON public.brain_entities USING gin (aliases);
CREATE INDEX IF NOT EXISTS brain_entities_section ON public.brain_entities (section_id);

-- Aliases are stored lower-cased and must not name another entity of the same type (by name or
-- alias). Generated columns are computed after BEFORE triggers, so this reads lower(NEW.name).
-- ponytail: two concurrent inserts can both pass the alias check; the unique name index still
-- holds, and alias collisions are rare enough to repair by hand.
CREATE OR REPLACE FUNCTION public.brain_entities_before() RETURNS trigger
LANGUAGE plpgsql SET search_path = '' AS $$
DECLARE clash uuid;
BEGIN
    NEW.aliases := ARRAY(SELECT DISTINCT lower(btrim(a)) FROM unnest(NEW.aliases) AS a
                          WHERE btrim(a) <> '' AND lower(btrim(a)) <> lower(NEW.name));
    SELECT e.id INTO clash FROM public.brain_entities e
     WHERE e.type = NEW.type AND e.id <> NEW.id
       AND (lower(e.name) = ANY(NEW.aliases) OR e.aliases && (NEW.aliases || lower(NEW.name)))
     LIMIT 1;
    IF clash IS NOT NULL THEN
        RAISE EXCEPTION 'alias_conflict: % % collides with entity %', NEW.type, NEW.name, clash;
    END IF;
    IF TG_OP = 'UPDATE' THEN
        NEW.updated_at := now();
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS brain_entities_before ON public.brain_entities;
CREATE TRIGGER brain_entities_before BEFORE INSERT OR UPDATE ON public.brain_entities
    FOR EACH ROW EXECUTE FUNCTION public.brain_entities_before();

CREATE TABLE IF NOT EXISTS public.brain_facts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind text NOT NULL CHECK (kind IN ('rule','trap','decision','settled','finding','measurement',
                                       'task','observation')),
    statement text NOT NULL CHECK (length(statement) BETWEEN 8 AND 600),
    detail text CHECK (detail IS NULL OR length(detail) <= 4096),
    entity_ids uuid[] NOT NULL CHECK (cardinality(entity_ids) >= 1),
    polarity smallint NOT NULL DEFAULT 0 CHECK (polarity IN (-1, 0, 1)),
    status text NOT NULL DEFAULT 'unverified' CHECK (status IN ('unverified','verified','verified_partial',
                                       'disputed','superseded','retracted','refuted','evidence_broken')),
    judge_pending boolean NOT NULL DEFAULT false,
    valid_from timestamptz NOT NULL DEFAULT now(),
    valid_to timestamptz,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    expired_at timestamptz,
    verify_by timestamptz NOT NULL,
    evidence jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(evidence) = 'array'),
    author text NOT NULL CHECK (author IN ('alpha','beta','gamma','lambda','operator','cron','import')),
    author_session text,
    verified_by text CHECK (verified_by IS NULL OR verified_by IN ('alpha','beta','gamma','lambda','operator')),
    verified_at timestamptz,
    verify_note text,
    episode_ids uuid[] NOT NULL DEFAULT '{}',
    version int NOT NULL DEFAULT 1,
    embedding extensions.vector(384),
    search_vector tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english', statement), 'A')
        || setweight(to_tsvector('english', coalesce(detail, '')), 'B')) STORED,
    CHECK (verified_by IS NULL OR verified_by <> author),
    CHECK (status NOT IN ('verified','verified_partial','refuted') OR verified_by IS NOT NULL),
    CHECK (valid_to IS NULL OR valid_to >= valid_from),
    CHECK (kind = 'observation' OR jsonb_array_length(evidence) >= 1),
    CHECK (kind <> 'observation' OR cardinality(episode_ids) >= 1)
);
CREATE INDEX IF NOT EXISTS brain_facts_entities ON public.brain_facts USING gin (entity_ids);
CREATE INDEX IF NOT EXISTS brain_facts_fts ON public.brain_facts USING gin (search_vector);
CREATE INDEX IF NOT EXISTS brain_facts_live ON public.brain_facts (status, verify_by) WHERE valid_to IS NULL;

-- The identity of an evidence list: its (type, ref) pairs, without the check annotations the gate
-- and the nightly job write. Changing a reference voids a verification; re-annotating does not.
CREATE OR REPLACE FUNCTION public.brain_evidence_refs(ev jsonb) RETURNS jsonb
LANGUAGE sql IMMUTABLE SET search_path = '' AS $$
    SELECT coalesce(jsonb_agg(jsonb_build_object('type', e->>'type', 'ref', e->>'ref')
                              ORDER BY e->>'type', e->>'ref'), '[]'::jsonb)
      FROM jsonb_array_elements(coalesce(ev, '[]'::jsonb)) AS e
$$;

CREATE OR REPLACE FUNCTION public.brain_facts_before() RETURNS trigger
LANGUAGE plpgsql SET search_path = '' AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        NEW.recorded_at := now();
        NEW.version := 1;
        IF NEW.verify_by IS NULL THEN
            NEW.verify_by := CASE NEW.kind
                WHEN 'measurement' THEN now() + interval '7 days'
                WHEN 'settled' THEN now() + interval '60 days'
                WHEN 'task' THEN now() + interval '3 days'
                WHEN 'observation' THEN 'infinity'::timestamptz
                ELSE now() + interval '30 days' END;
        END IF;
        RETURN NEW;
    END IF;
    IF (NEW.statement, NEW.kind, NEW.author, NEW.recorded_at, NEW.entity_ids, NEW.valid_from,
        NEW.polarity, NEW.author_session)
       IS DISTINCT FROM
       (OLD.statement, OLD.kind, OLD.author, OLD.recorded_at, OLD.entity_ids, OLD.valid_from,
        OLD.polarity, OLD.author_session) THEN
        RAISE EXCEPTION 'brain_facts: statement, kind, author, entities, valid_from and recorded_at are immutable; supersede instead';
    END IF;
    IF (OLD.valid_to IS NOT NULL AND NEW.valid_to IS DISTINCT FROM OLD.valid_to)
       OR (OLD.expired_at IS NOT NULL AND NEW.expired_at IS DISTINCT FROM OLD.expired_at) THEN
        RAISE EXCEPTION 'brain_facts: valid_to is write-once (a closed fact cannot be revived)';
    END IF;
    IF public.brain_evidence_refs(NEW.evidence) IS DISTINCT FROM public.brain_evidence_refs(OLD.evidence) THEN
        -- new evidence has not been checked by anyone else yet, and it reopens a refuted fact
        -- (keeping 'refuted' with verified_by cleared would violate the verified_by CHECK)
        IF NEW.status IN ('verified', 'verified_partial', 'refuted') THEN
            NEW.status := 'unverified';
        END IF;
        NEW.verified_by := NULL;
        NEW.verified_at := NULL;
    END IF;
    IF NEW.status IN ('verified', 'verified_partial', 'refuted') AND NEW.status IS DISTINCT FROM OLD.status THEN
        IF OLD.status = 'disputed' THEN
            RAISE EXCEPTION 'brain_facts: resolve the dispute first (supersede, or judge the pair)';
        END IF;
        IF NEW.judge_pending THEN
            RAISE EXCEPTION 'brain_facts: judge_pending candidates must be judged before verification';
        END IF;
    END IF;
    NEW.version := OLD.version + 1;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS brain_facts_before ON public.brain_facts;
CREATE TRIGGER brain_facts_before BEFORE INSERT OR UPDATE ON public.brain_facts
    FOR EACH ROW EXECUTE FUNCTION public.brain_facts_before();

CREATE OR REPLACE FUNCTION public.brain_facts_no_delete() RETURNS trigger
LANGUAGE plpgsql SET search_path = '' AS $$
BEGIN
    RAISE EXCEPTION 'brain_facts rows are never deleted; retract or supersede';
END $$;
DROP TRIGGER IF EXISTS brain_facts_no_delete ON public.brain_facts;
CREATE TRIGGER brain_facts_no_delete BEFORE DELETE ON public.brain_facts
    FOR EACH ROW EXECUTE FUNCTION public.brain_facts_no_delete();

CREATE TABLE IF NOT EXISTS public.brain_links (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    from_id uuid NOT NULL,
    from_kind text NOT NULL CHECK (from_kind IN ('fact','entity','episode')),
    to_id uuid NOT NULL,
    to_kind text NOT NULL CHECK (to_kind IN ('fact','entity','episode')),
    rel text NOT NULL CHECK (rel IN ('supports','contradicts','supersedes','depends_on',
                                     'derived_from','same_as','part_of','candidate')),
    author text NOT NULL CHECK (author IN ('alpha','beta','gamma','lambda','operator','cron','import')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (from_id, to_id, rel)
);
CREATE INDEX IF NOT EXISTS brain_links_to ON public.brain_links (to_id);

CREATE TABLE IF NOT EXISTS public.brain_views (
    name text PRIMARY KEY,
    description text NOT NULL,
    char_limit int NOT NULL DEFAULT 12000,
    query_spec jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS public.brain_health (
    id bigserial PRIMARY KEY,
    run_at timestamptz NOT NULL DEFAULT now(),
    metrics jsonb NOT NULL
);

-- Anon and authenticated are denied on every table (the 2026-06-20 pattern). Only service_role
-- reads and writes, because it bypasses RLS. A process holding only the anon key would read an
-- EMPTY brain rather than an error, so the client refuses to run without the service key.
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['brain_sections','brain_episodes','brain_entities','brain_facts',
                             'brain_links','brain_views','brain_health'] LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', t || '_no_anon', t);
        EXECUTE format('CREATE POLICY %I ON public.%I FOR ALL TO anon, authenticated USING (false)',
                       t || '_no_anon', t);
    END LOOP;
END $$;

-- Table grants (required from migration 032: from 2026-10-30 Supabase no longer auto-grants Data API
-- access to new public tables). Least privilege for the one writer role: no DELETE on the append-only
-- tables, and TRUNCATE revoked there because it skips the row-level delete triggers.
GRANT SELECT, INSERT, UPDATE ON public.brain_sections TO service_role;
GRANT SELECT, INSERT, UPDATE ON public.brain_episodes TO service_role;
GRANT SELECT, INSERT, UPDATE ON public.brain_entities TO service_role;
GRANT SELECT, INSERT, UPDATE ON public.brain_facts TO service_role;
GRANT SELECT, INSERT ON public.brain_links TO service_role;
GRANT SELECT, INSERT, UPDATE ON public.brain_views TO service_role;
GRANT SELECT, INSERT ON public.brain_health TO service_role;
GRANT USAGE, SELECT ON SEQUENCE public.brain_health_id_seq TO service_role;
REVOKE DELETE, TRUNCATE ON public.brain_facts, public.brain_episodes, public.brain_links FROM service_role;

-- Supersede one fact with a new one in one transaction. The old fact's validity ends where the new
-- one's begins; valid_from, verify_by and judge_pending come from p_new when the importer sets them.
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

-- Facts on the entities of some sections, joined in the database: the entity list of a section
-- can hold thousands of ids and does not fit a URL. The result is cut at p_limit with NO truncation
-- signal: a caller that needs completeness compares its row count with brain_queue_count (or the
-- limit) and prints the gap (plan review amendment 3).
CREATE OR REPLACE FUNCTION public.brain_facts_in_sections(p_section_ids uuid[], p_kinds text[] DEFAULT NULL,
    p_queue_only boolean DEFAULT false, p_exclude_author text DEFAULT NULL,
    p_oldest_first boolean DEFAULT false, p_limit int DEFAULT 500)
RETURNS SETOF public.brain_facts
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = '' AS $$
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
$$;

CREATE OR REPLACE FUNCTION public.brain_queue_count(p_section_ids uuid[], p_exclude_author text DEFAULT NULL)
RETURNS bigint
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = '' AS $$
    SELECT count(*) FROM public.brain_facts f
     WHERE f.valid_to IS NULL AND f.kind <> 'observation'
       AND (f.status = 'unverified' OR f.judge_pending)
       AND (p_exclude_author IS NULL OR f.author <> p_exclude_author)
       AND EXISTS (SELECT 1 FROM public.brain_entities e
                    WHERE e.id = ANY(f.entity_ids) AND e.section_id = ANY(p_section_ids));
$$;

-- Every fact on some entities, oldest first. No row cap: the contradiction candidate stage must
-- see the OLD facts a new one may reverse, and a newest-first cap drops exactly those.
CREATE OR REPLACE FUNCTION public.brain_facts_for_entities(p_entity_ids uuid[], p_kinds text[] DEFAULT NULL,
    p_live_only boolean DEFAULT true)
RETURNS SETOF public.brain_facts
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = '' AS $$
    SELECT f.* FROM public.brain_facts f
     WHERE f.entity_ids && p_entity_ids
       AND (p_kinds IS NULL OR f.kind = ANY(p_kinds))
       AND (NOT p_live_only OR (f.valid_to IS NULL AND
            f.status IN ('unverified','verified','verified_partial','disputed','evidence_broken')))
     ORDER BY f.recorded_at ASC;
$$;

CREATE OR REPLACE FUNCTION public.brain_match_facts(query_embedding text, match_count int DEFAULT 20)
RETURNS TABLE (id uuid, similarity real)
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = '' AS $$
    SELECT f.id, (1 - (f.embedding OPERATOR(extensions.<=>) query_embedding::extensions.vector))::real
      FROM public.brain_facts f
     WHERE f.embedding IS NOT NULL AND f.valid_to IS NULL
       AND f.status IN ('unverified','verified','verified_partial','disputed','evidence_broken')
     ORDER BY f.embedding OPERATOR(extensions.<=>) query_embedding::extensions.vector
     LIMIT greatest(1, match_count);
$$;

CREATE OR REPLACE FUNCTION public.brain_match_entities(query_embedding text, match_count int DEFAULT 5)
RETURNS TABLE (id uuid, similarity real)
LANGUAGE sql STABLE SECURITY INVOKER SET search_path = '' AS $$
    SELECT e.id, (1 - (e.embedding OPERATOR(extensions.<=>) query_embedding::extensions.vector))::real
      FROM public.brain_entities e
     WHERE e.embedding IS NOT NULL
     ORDER BY e.embedding OPERATOR(extensions.<=>) query_embedding::extensions.vector
     LIMIT greatest(1, match_count);
$$;

CREATE OR REPLACE FUNCTION public.brain_set_embedding(p_table text, p_id uuid, p_embedding text)
RETURNS void LANGUAGE plpgsql SECURITY INVOKER SET search_path = '' AS $$
BEGIN
    IF p_table = 'brain_facts' THEN
        UPDATE public.brain_facts SET embedding = p_embedding::extensions.vector WHERE id = p_id;
    ELSIF p_table = 'brain_entities' THEN
        UPDATE public.brain_entities SET embedding = p_embedding::extensions.vector WHERE id = p_id;
    ELSIF p_table = 'brain_episodes' THEN
        UPDATE public.brain_episodes SET embedding = p_embedding::extensions.vector WHERE id = p_id;
    ELSE
        RAISE EXCEPTION 'brain_set_embedding: unknown table %', p_table;
    END IF;
END $$;

REVOKE EXECUTE ON FUNCTION public.brain_supersede(uuid, int, jsonb) FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.brain_facts_in_sections(uuid[], text[], boolean, text, boolean, int) FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.brain_queue_count(uuid[], text) FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.brain_facts_for_entities(uuid[], text[], boolean) FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.brain_match_facts(text, int) FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.brain_match_entities(text, int) FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.brain_set_embedding(text, uuid, text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.brain_supersede(uuid, int, jsonb) TO service_role;
GRANT EXECUTE ON FUNCTION public.brain_facts_in_sections(uuid[], text[], boolean, text, boolean, int) TO service_role;
GRANT EXECUTE ON FUNCTION public.brain_queue_count(uuid[], text) TO service_role;
GRANT EXECUTE ON FUNCTION public.brain_facts_for_entities(uuid[], text[], boolean) TO service_role;
GRANT EXECUTE ON FUNCTION public.brain_match_facts(text, int) TO service_role;
GRANT EXECUTE ON FUNCTION public.brain_match_entities(text, int) TO service_role;
GRANT EXECUTE ON FUNCTION public.brain_set_embedding(text, uuid, text) TO service_role;

NOTIFY pgrst, 'reload schema';
