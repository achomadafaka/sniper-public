-- Migration: supabase_014_agent_runs_status_add_running.sql
-- Extend agent_runs.status CHECK constraint to include 'running' for the
-- calibration loop v1. 'running' is a transient state written by
-- predict_tool start-run and replaced by a terminal status (success, error,
-- timeout, budget_exceeded) by predict_tool finish-run.
--
-- Original constraint at supabase_002_agent_runs.sql:31
--   CHECK (status IN ('success', 'error', 'timeout', 'budget_exceeded'))
--
-- Idempotent: dropping by name then re-adding with a stable name.

ALTER TABLE public.agent_runs
  DROP CONSTRAINT IF EXISTS agent_runs_status_check;

ALTER TABLE public.agent_runs
  ADD CONSTRAINT agent_runs_status_check
  CHECK (status IN ('success', 'error', 'timeout', 'budget_exceeded', 'running'));
