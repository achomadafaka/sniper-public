-- Migration: supabase_009_drop_unused_indexes_2.sql
-- Second batch of unused indexes (lint: 0005_unused_index), all with idx_scan=0.
-- Confirmed safe after code audit:
--
--   idx_industry_impacts_direction  — impact_direction never used as a filter
--   idx_opportunities_platform      — market_opportunities queries filter on status only
--   idx_opportunities_event         — market_opportunities queries filter on status only
--   idx_session_logs_source         — source_filter param exists but is never passed by any caller
--   idx_world_events_region_status  — world_events queries filter on status only (no region join)

DROP INDEX IF EXISTS public.idx_industry_impacts_direction;
DROP INDEX IF EXISTS public.idx_opportunities_platform;
DROP INDEX IF EXISTS public.idx_opportunities_event;
DROP INDEX IF EXISTS public.idx_session_logs_source;
DROP INDEX IF EXISTS public.idx_world_events_region_status;
