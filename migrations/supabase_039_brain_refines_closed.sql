-- 039 (2026-09-30): brain_judge_pair accepts `refines` when only the OLDER fact is closed.
-- Before: a fact written with a judgement against a closed fact (refuted, retracted, superseded or
-- valid_to set) stayed judge_pending, because every verdict but `unrelated` raised closed_fact.
-- Only this one condition changes; the rest of the function is migration 037's, unchanged.
-- Rollback: supabase_039_rollback_brain_refines_closed.sql (restores 037's definition).
CREATE OR REPLACE FUNCTION public.brain_judge_pair(p_newer uuid,p_older uuid,p_newer_version int,p_older_version int,p_verdict text,p_author text)
RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
DECLARE n public.brain_facts; o public.brain_facts; prior text; relation text; n_closed boolean; o_closed boolean;
BEGIN
 IF p_newer=p_older THEN RAISE EXCEPTION 'same_fact'; END IF;
 IF p_author NOT IN ('alpha','beta','gamma','lambda','operator') OR p_author IS NULL THEN RAISE EXCEPTION 'invalid_judge'; END IF;
 IF p_verdict NOT IN ('duplicate','refines','supersedes','contradicts','unrelated') OR p_verdict IS NULL THEN RAISE EXCEPTION 'invalid_verdict'; END IF;
 PERFORM id FROM public.brain_facts WHERE id IN (p_newer,p_older) ORDER BY id FOR UPDATE;
 SELECT * INTO STRICT n FROM public.brain_facts WHERE id=p_newer;
 SELECT * INTO STRICT o FROM public.brain_facts WHERE id=p_older;
 relation:=CASE p_verdict WHEN 'duplicate' THEN 'same_as' WHEN 'refines' THEN 'derived_from' WHEN 'supersedes' THEN 'supersedes' WHEN 'contradicts' THEN 'contradicts' END;
 SELECT verdict INTO prior FROM public.brain_judgements WHERE pair_lo=least(p_newer,p_older) AND pair_hi=greatest(p_newer,p_older);
 IF FOUND THEN
  IF prior<>p_verdict THEN RAISE EXCEPTION 'judgement_conflict'; END IF;
  IF p_verdict IN ('duplicate','refines','supersedes') AND NOT EXISTS (
   SELECT 1 FROM public.brain_links WHERE from_id=p_newer AND to_id=p_older AND rel=relation) THEN RAISE EXCEPTION 'judgement_orientation_conflict'; END IF;
  RETURN jsonb_build_object('verdict',prior,'replayed',true);
 END IF;
 IF n.version<>p_newer_version OR o.version<>p_older_version OR p_newer_version IS NULL OR p_older_version IS NULL THEN RAISE EXCEPTION 'version_conflict'; END IF;
 n_closed:=n.valid_to IS NOT NULL OR n.status NOT IN ('unverified','verified','verified_partial','disputed','evidence_broken');
 o_closed:=o.valid_to IS NOT NULL OR o.status NOT IN ('unverified','verified','verified_partial','disputed','evidence_broken');
 -- 039: `refines` adds a derived_from link and changes no status, so a LIVE newer fact may refine a
 -- CLOSED older one. A closed newer fact, and every other verdict against a closed fact, still refuse.
 IF p_verdict<>'unrelated' AND (n_closed OR (o_closed AND p_verdict<>'refines')) THEN RAISE EXCEPTION 'closed_fact'; END IF;
 IF p_verdict='duplicate' THEN
  UPDATE public.brain_facts SET status='retracted',valid_to=now(),expired_at=now() WHERE id=p_newer;
 ELSIF p_verdict='supersedes' THEN
  IF n.valid_from<o.valid_from THEN RAISE EXCEPTION 'backdated_supersession'; END IF;
  UPDATE public.brain_facts SET status='superseded',valid_to=n.valid_from,expired_at=now() WHERE id=p_older;
 ELSIF p_verdict='contradicts' THEN
  UPDATE public.brain_facts SET status='disputed' WHERE id IN (p_newer,p_older);
 END IF;
 IF relation IS NOT NULL THEN
  INSERT INTO public.brain_links(from_id,from_kind,to_id,to_kind,rel,author)
  VALUES(p_newer,'fact',p_older,'fact',relation,p_author) ON CONFLICT DO NOTHING;
 END IF;
 INSERT INTO public.brain_judgements(pair_lo,pair_hi,verdict,author)
 VALUES(least(p_newer,p_older),greatest(p_newer,p_older),p_verdict,p_author);
 UPDATE public.brain_facts f SET judge_pending=false WHERE f.id IN (p_newer,p_older) AND f.judge_pending AND NOT EXISTS (
  SELECT 1 FROM public.brain_links l WHERE l.rel='candidate' AND l.from_kind='fact' AND l.to_kind='fact' AND (l.from_id=f.id OR l.to_id=f.id)
  AND NOT EXISTS (SELECT 1 FROM public.brain_judgements j WHERE j.pair_lo=least(l.from_id,l.to_id) AND j.pair_hi=greatest(l.from_id,l.to_id)));
 RETURN jsonb_build_object('verdict',p_verdict,'replayed',false);
END $$;
REVOKE ALL ON FUNCTION public.brain_judge_pair(uuid,uuid,int,int,text,text) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.brain_judge_pair(uuid,uuid,int,int,text,text) TO service_role;
