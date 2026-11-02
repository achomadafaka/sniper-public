-- Migration: supabase_010_drop_unused_indexes_3.sql
-- Third batch of unused indexes (lint: 0005_unused_index), both idx_scan=0.
-- market_opportunities queries only filter on status; instrument and
-- platform are write-only fields never used in SELECT WHERE clauses.
--
--   idx_opportunities_instrument     — instrument never filtered
--   idx_opportunities_platform_status — platform/status composite never hit
--                                       (status-only queries don't use this composite)

DROP INDEX IF EXISTS public.idx_opportunities_instrument;
DROP INDEX IF EXISTS public.idx_opportunities_platform_status;
