-- Migration: supabase_013_agent_runs_calibration_context.sql
-- Add calibration_context JSONB column to agent_runs for the v1 calibration loop.
--
-- Why this exists:
--   Calibration loop v1 writes per-run calibration stats (the stats the agent
--   saw at run start) as a JSONB blob on agent_runs. Stats themselves are
--   computed just-in-time from agent_predictions; only the snapshot shown to
--   the agent is persisted here for auditability.
--
-- Nullable because legacy runs and runs from agents not-yet-migrated do not
-- populate it. Consumers (brain_health CAL1) already handle NULL.
--
-- Safe to re-run: IF NOT EXISTS guard.

ALTER TABLE public.agent_runs
  ADD COLUMN IF NOT EXISTS calibration_context JSONB NULL;

COMMENT ON COLUMN public.agent_runs.calibration_context IS
'Calibration stats snapshot shown to the agent at run start. Populated by predict_tool start-run; null for runs from agents not in CALIBRATION_ENABLED_AGENTS.';
