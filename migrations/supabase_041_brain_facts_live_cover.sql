-- 041 (applied 2026-10-08, Alpha): the session-start and per-prompt brain hooks have a 2.5 s budget.
-- After 040 the boot reads took ~1.2 s idle but timed out when several crew sessions booted at once.
-- A covering partial index lets brain_queue_count read status/kind/judge_pending/author/entity_ids
-- without fetching the wide brain_facts rows (inline count 1.2 s -> 24 ms, function 344 ms), and a
-- recorded_at index serves the newest-first section read (78 ms). Boot 0.9-1.3 s, hook 1.4 s.
-- Rollback: migrations/supabase_041_rollback_brain_facts_live_cover.sql.
CREATE INDEX IF NOT EXISTS brain_facts_live_cover ON public.brain_facts (status)
    INCLUDE (kind, judge_pending, author, entity_ids, recorded_at) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS brain_facts_live_recent ON public.brain_facts (recorded_at DESC)
    WHERE valid_to IS NULL;
ANALYZE public.brain_facts;
