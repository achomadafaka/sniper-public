-- Migration: supabase_002_agent_runs.sql
-- Create the public.agent_runs table used by brain health + agent heartbeat logging.
--
-- Why this exists:
--   The repo had policy migrations for public.agent_runs but no committed
--   table-creation migration. That left the live schema easy to drift and made
--   "column agent_runs.notes does not exist" style errors hard to reason about.
--
-- Expected shape from repo runtime:
--   agent_type, model, cost_usd, turns, duration_seconds, findings_summary,
--   status, error_message, created_at
--
-- Important:
--   There is intentionally NO `notes` column here. Any live query that still
--   references `agent_runs.notes` is stale and should be fixed at the caller.
--
-- Run in: Supabase Dashboard -> SQL Editor
-- Safe to re-run: CREATE IF NOT EXISTS / DO blocks are idempotent.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS public.agent_runs (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_type       TEXT NOT NULL,
    model            TEXT,
    cost_usd         DOUBLE PRECISION,
    turns            INTEGER,
    duration_seconds DOUBLE PRECISION,
    findings_summary TEXT,
    actions_taken    JSONB NOT NULL DEFAULT '[]'::jsonb,
    status           TEXT NOT NULL DEFAULT 'success'
                     CHECK (status IN ('success', 'error', 'timeout', 'budget_exceeded')),
    error_message    TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_type
    ON public.agent_runs (agent_type, created_at DESC);

ALTER TABLE public.agent_runs ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'agent_runs'
          AND policyname = 'agent_runs_select'
    ) THEN
        EXECUTE 'CREATE POLICY "agent_runs_select" ON public.agent_runs FOR SELECT USING (true)';
    END IF;
END $$;

GRANT SELECT ON public.agent_runs TO anon;
GRANT ALL PRIVILEGES ON public.agent_runs TO service_role;

-- To find stale Supabase code that still references agent_runs.notes, run:
--
-- SELECT schemaname, viewname, definition
-- FROM pg_views
-- WHERE definition ILIKE '%agent_runs%' AND definition ILIKE '%notes%';
--
-- SELECT routine_schema, routine_name, routine_definition
-- FROM information_schema.routines
-- WHERE routine_definition ILIKE '%agent_runs%' AND routine_definition ILIKE '%notes%';
