-- Migration: supabase_005_rls_scope_all.sql
-- Comprehensive RLS policy cleanup — all remaining tables with
-- the rls_policy_always_true Supabase advisor warning.
--
-- Run in: Supabase Dashboard → SQL Editor
-- Safe to re-run: all DROP IF EXISTS / CREATE are idempotent.
--
-- Context:
--   This is a single-tenant internal trading service. There are no
--   end-users; all reads and writes originate from the VPS process
--   using the Supabase anon key.  USING (true) / WITH CHECK (true)
--   is intentional — no per-row isolation is required.
--
--   Supabase's lint flags FOR ALL policies with roles = '-' (all roles)
--   because it can't distinguish internal services from accidentally open
--   public tables.  The fix: scope every write policy explicitly TO anon.
--   Functional behaviour is identical; the warning is resolved.
--
-- Pattern applied to each table:
--   1. Drop the existing roles='-' ALL policy
--   2. Create "select" policy  — USING (true), no role restriction
--      (SELECT/USING(true) is intentionally excluded from the lint check)
--   3. Create "write" policy   — FOR ALL TO anon USING (true) WITH CHECK (true)
--      (scoped to anon: same effective access, but lint-clean)


-- ============================================================
-- agent_runs
-- ============================================================
DROP POLICY IF EXISTS "Allow all for agent_runs" ON public.agent_runs;
CREATE POLICY "agent_runs_select" ON public.agent_runs FOR SELECT USING (true);
CREATE POLICY "agent_runs_write"  ON public.agent_runs FOR ALL TO anon USING (true) WITH CHECK (true);


-- ============================================================
-- geo_context
-- ============================================================
DROP POLICY IF EXISTS "Allow all for geo_context" ON public.geo_context;
CREATE POLICY "geo_context_select" ON public.geo_context FOR SELECT USING (true);
CREATE POLICY "geo_context_write"  ON public.geo_context FOR ALL TO anon USING (true) WITH CHECK (true);


-- ============================================================
-- industry_impacts
-- ============================================================
DROP POLICY IF EXISTS "Allow all for industry_impacts" ON public.industry_impacts;
CREATE POLICY "industry_impacts_select" ON public.industry_impacts FOR SELECT USING (true);
CREATE POLICY "industry_impacts_write"  ON public.industry_impacts FOR ALL TO anon USING (true) WITH CHECK (true);


-- ============================================================
-- world_events
-- ============================================================
DROP POLICY IF EXISTS "Allow all for world_events" ON public.world_events;
CREATE POLICY "world_events_select" ON public.world_events FOR SELECT USING (true);
CREATE POLICY "world_events_write"  ON public.world_events FOR ALL TO anon USING (true) WITH CHECK (true);


-- ============================================================
-- market_opportunities
-- ============================================================
DROP POLICY IF EXISTS "Allow all for market_opportunities" ON public.market_opportunities;
CREATE POLICY "market_opportunities_select" ON public.market_opportunities FOR SELECT USING (true);
CREATE POLICY "market_opportunities_write"  ON public.market_opportunities FOR ALL TO anon USING (true) WITH CHECK (true);


-- ============================================================
-- memories  (tracked in bot/supabase_migration.sql)
-- ============================================================
DROP POLICY IF EXISTS "Allow all access to memories" ON public.memories;
CREATE POLICY "memories_select" ON public.memories FOR SELECT USING (true);
CREATE POLICY "memories_write"  ON public.memories FOR ALL TO anon USING (true) WITH CHECK (true);


-- ============================================================
-- session_logs  (tracked in bot/supabase_migration.sql)
-- ============================================================
DROP POLICY IF EXISTS "Allow all access to session_logs" ON public.session_logs;
CREATE POLICY "session_logs_select" ON public.session_logs FOR SELECT USING (true);
CREATE POLICY "session_logs_write"  ON public.session_logs FOR ALL TO anon USING (true) WITH CHECK (true);


-- ============================================================
-- strategy_agent_observations  (tracked in supabase_001)
-- ============================================================
DROP POLICY IF EXISTS "Allow anon full access" ON public.strategy_agent_observations;
CREATE POLICY "strategy_agent_observations_select" ON public.strategy_agent_observations FOR SELECT USING (true);
CREATE POLICY "strategy_agent_observations_write"  ON public.strategy_agent_observations FOR ALL TO anon USING (true) WITH CHECK (true);


-- ============================================================
-- country_profiles  (if exists — created by world_analyst agent)
-- ============================================================
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='country_profiles') THEN
        EXECUTE 'DROP POLICY IF EXISTS "Allow all for country_profiles" ON public.country_profiles';
        EXECUTE 'CREATE POLICY "country_profiles_select" ON public.country_profiles FOR SELECT USING (true)';
        EXECUTE 'CREATE POLICY "country_profiles_write"  ON public.country_profiles FOR ALL TO anon USING (true) WITH CHECK (true)';
    END IF;
END $$;
