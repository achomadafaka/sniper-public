-- Keep all facts, links and historical decisions; disable new maintenance first.
DROP FUNCTION IF EXISTS public.brain_queue_candidate(uuid,uuid,text);
DROP FUNCTION IF EXISTS public.brain_judge_pair(uuid,uuid,int,int,text,text);
