-- Migration: supabase_012_agent_runs_run_summary_compat.sql
-- Compatibility shim for stale external queries that reference
-- public.agent_runs.run_summary.
--
-- Why this exists:
--   Supabase Postgres logs show "column agent_runs.run_summary does not exist"
--   from an external caller (manual SQL, BI/dashboard, or untracked script).
--   The canonical column in this repo is findings_summary; run_summary is a
--   naming mismatch. A STORED GENERATED column aliases findings_summary so
--   reads stay in sync automatically and no writer needs to be updated.
--
--   Deliberately NOT adding memories.value here — that is a different-table
--   contract confusion (canonical memories uses `content`; `value` belongs to
--   the older agent_memory shape). Shimming it would hide a real caller bug.
--
-- Safe to re-run.

ALTER TABLE public.agent_runs
ADD COLUMN IF NOT EXISTS run_summary TEXT
GENERATED ALWAYS AS (findings_summary) STORED;

COMMENT ON COLUMN public.agent_runs.run_summary IS
'Compatibility alias for stale external queries. Generated from findings_summary; do not write to this column.';
