-- Append-only pair decisions. Historical decisions survive closed facts.
CREATE TABLE IF NOT EXISTS public.brain_judgements (
 pair_lo uuid NOT NULL REFERENCES public.brain_facts(id),
 pair_hi uuid NOT NULL REFERENCES public.brain_facts(id),
 verdict text NOT NULL CHECK (verdict IN ('duplicate','refines','supersedes','contradicts','unrelated')),
 author text NOT NULL CHECK (author IN ('alpha','beta','gamma','lambda','operator','cron','import')),
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(pair_lo,pair_hi), CHECK(pair_lo<pair_hi)
);
ALTER TABLE public.brain_judgements ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.brain_judgements FROM PUBLIC, anon, authenticated;
REVOKE UPDATE, DELETE, TRUNCATE ON public.brain_judgements FROM service_role;
GRANT SELECT, INSERT ON public.brain_judgements TO service_role;
