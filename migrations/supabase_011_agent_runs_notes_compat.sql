-- Migration: supabase_011_agent_runs_notes_compat.sql
-- Compatibility shim for stale external queries that still reference
-- public.agent_runs.notes.
--
-- Why this exists:
--   The live Supabase table is healthy, but Postgres logs show
--   "column agent_runs.notes does not exist" and catalog audit found no
--   view/function/trigger inside the database itself still referencing it.
--   That means the bad caller is external (manual SQL, BI/dashboard tool,
--   or untracked script). Adding a nullable compatibility column stops the
--   runtime error immediately without changing current repo code paths.
--
-- Safe to re-run.

ALTER TABLE public.agent_runs
ADD COLUMN IF NOT EXISTS notes TEXT;

COMMENT ON COLUMN public.agent_runs.notes IS
'Compatibility column for stale external queries. Repo runtime uses findings_summary, not notes.';
