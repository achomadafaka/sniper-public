-- Migration: supabase_007_fk_indexes.sql
-- Add covering indexes for foreign key columns that were flagged as unindexed
-- by the Supabase advisor (lint: 0001_unindexed_foreign_keys).
--
-- Tables affected:
--   public.agent_lessons          → agent_lessons_supersedes_fkey       (column: supersedes)
--   public.agent_predictions      → agent_predictions_run_id_fkey       (column: run_id)
--   public.strategy_agent_observations → strategy_agent_observations_run_id_fkey (column: run_id)

CREATE INDEX IF NOT EXISTS idx_agent_lessons_supersedes
    ON public.agent_lessons (supersedes);

CREATE INDEX IF NOT EXISTS idx_agent_predictions_run_id
    ON public.agent_predictions (run_id);

CREATE INDEX IF NOT EXISTS idx_strategy_agent_observations_run_id
    ON public.strategy_agent_observations (run_id);
