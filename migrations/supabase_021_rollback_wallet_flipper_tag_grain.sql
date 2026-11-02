-- Migration: supabase_021_rollback_wallet_flipper_tag_grain.sql
-- Rollback companion for supabase_021_wallet_flipper_tag_grain.sql.
--
-- WARNING: dropping the table is destructive — every per-wallet
-- per-grain row is lost. Use only when:
--   * Data has been backed up, OR
--   * The forward migration was applied in error and no production
--     data has been written yet.
--
-- Safe to re-run: every statement uses IF EXISTS.

-- Drop trigger + function
DROP TRIGGER IF EXISTS wftg_set_updated_at ON public.wallet_flipper_tag_grain;
DROP FUNCTION IF EXISTS public.set_wftg_updated_at();

-- Drop policies (table drop would cascade but being explicit avoids
-- surprises if RLS is reapplied later under a different schema).
DROP POLICY IF EXISTS wftg_select ON public.wallet_flipper_tag_grain;
DROP POLICY IF EXISTS wftg_write ON public.wallet_flipper_tag_grain;

-- Drop the table (indexes and constraints cascade)
DROP TABLE IF EXISTS public.wallet_flipper_tag_grain;
