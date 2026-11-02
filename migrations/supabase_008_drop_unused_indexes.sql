-- Migration: supabase_008_drop_unused_indexes.sql
-- Drop indexes flagged as unused (0 scans) by the Supabase advisor
-- (lint: 0005_unused_index). All confirmed safe to remove after code audit:
--
--   idx_sao_acted_on          — acted_on column never filtered in any query
--   idx_world_events_region   — world_events queries filter on status only
--   idx_world_events_country  — world_events queries filter on status only
--   idx_industry_impacts_event    — industry_impacts never read by application
--   idx_industry_impacts_industry — industry_impacts never read by application

DROP INDEX IF EXISTS public.idx_sao_acted_on;
DROP INDEX IF EXISTS public.idx_world_events_region;
DROP INDEX IF EXISTS public.idx_world_events_country;
DROP INDEX IF EXISTS public.idx_industry_impacts_event;
DROP INDEX IF EXISTS public.idx_industry_impacts_industry;
