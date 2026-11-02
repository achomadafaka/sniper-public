-- Migration: supabase_006_drop_write_policies.sql
-- Drops all _write RLS policies that are now redundant.
--
-- Run in: Supabase Dashboard → SQL Editor
-- Safe to re-run: all DROP IF EXISTS are idempotent.
--
-- Context:
--   All Python processes now use SUPABASE_SERVICE_KEY which bypasses
--   RLS entirely, making the _write policies completely dead code.
--   Dropping them removes the rls_policy_always_true Supabase advisor
--   warnings (lint=0024) since the linter flags any policy with
--   WITH CHECK (true) regardless of role scoping.
--
--   The _select policies (USING(true), no role restriction) are kept
--   since they are intentionally excluded from the lint check.

DROP POLICY IF EXISTS "agent_lessons_write"                    ON public.agent_lessons;
DROP POLICY IF EXISTS "agent_predictions_write"                ON public.agent_predictions;
DROP POLICY IF EXISTS "agent_runs_write"                       ON public.agent_runs;
DROP POLICY IF EXISTS "geo_context_write"                      ON public.geo_context;
DROP POLICY IF EXISTS "industry_impacts_write"                 ON public.industry_impacts;
DROP POLICY IF EXISTS "world_events_write"                     ON public.world_events;
DROP POLICY IF EXISTS "market_opportunities_write"             ON public.market_opportunities;
DROP POLICY IF EXISTS "memories_write"                         ON public.memories;
DROP POLICY IF EXISTS "session_logs_write"                     ON public.session_logs;
DROP POLICY IF EXISTS "strategy_agent_observations_write"      ON public.strategy_agent_observations;
DROP POLICY IF EXISTS "country_profiles_write"                 ON public.country_profiles;

-- Verify: after running this, all remaining policies should be _select only.
-- SELECT tablename, policyname, cmd, roles
-- FROM pg_policies
-- WHERE schemaname = 'public'
-- ORDER BY tablename, policyname;
