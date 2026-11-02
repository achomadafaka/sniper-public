-- Migration: supabase_003_fix_search_path.sql
-- Fixes Supabase security advisory: function_search_path_mutable
-- Ref: https://supabase.com/docs/guides/database/database-linter?lint=0011_function_search_path_mutable
--
-- The function public.set_updated_at_opportunities had a mutable search_path,
-- meaning a malicious schema could shadow pg_catalog functions it relies on.
-- Fix: pin search_path to '' (empty = pg_catalog implicitly, no user schemas).
--
-- Run in: Supabase Dashboard → SQL Editor
-- Safe to re-run: CREATE OR REPLACE is idempotent.

CREATE OR REPLACE FUNCTION public.set_updated_at_opportunities()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = ''
AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;
