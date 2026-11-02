-- supabase_027_drop_orphaned_sao_indexes.sql
-- strategy_agent_observations.confidence and .strategy_ids have NO code consumer
-- (verified 2026-06-20: no filter/order on these columns anywhere in the repo).
-- CLAUDE.md's advisor section explicitly greenlit dropping these speculative
-- indexes once no consuming query materialized. The created_at / strategy_type /
-- obs_type indexes are KEPT — brain_health.py and domain_query.py filter/order by
-- them (they read as "unused" only because the table is small).
DROP INDEX IF EXISTS public.strategy_agent_observations_confidence_idx;
DROP INDEX IF EXISTS public.strategy_agent_observations_strategy_ids_idx;
