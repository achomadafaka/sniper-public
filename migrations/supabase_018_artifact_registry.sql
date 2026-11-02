-- Migration: supabase_018_artifact_registry.sql
-- Slice 6 — append-only registry for content-addressed local artifacts.
--
-- Append-only contract: each row keyed by `content_hash`. Once written,
-- the row's identity (key + content) never changes. `paths` is mutable
-- metadata (PATCH-only) — a second write of the same content_hash from
-- a different filesystem path appends to the JSONB array. New content
-- always gets a new row. There is NO `status` column and no supersede
-- semantics (mark_superseded(table='artifact_registry', ...) raises).
--
-- Idempotent: safe to re-run.

CREATE TABLE IF NOT EXISTS public.artifact_registry (
    content_hash TEXT NOT NULL,
    kind TEXT NOT NULL
        CHECK (kind IN (
            'wallet_warehouse',
            'market_warehouse',
            'fixture_export',
            'report_blob',
            'csv_export'
        )),
    paths JSONB NOT NULL DEFAULT '[]'::jsonb,
    byte_count BIGINT,
    schema_version TEXT,
    capture_run_id TEXT,
    metadata_json JSONB,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (content_hash)
);

CREATE INDEX IF NOT EXISTS artifact_registry_kind_idx
    ON public.artifact_registry (kind);

ALTER TABLE public.artifact_registry ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "artifact_registry_select" ON public.artifact_registry;
CREATE POLICY "artifact_registry_select"
    ON public.artifact_registry
    FOR SELECT USING (true);
-- _write policy intentionally omitted — service-key writers bypass RLS
-- entirely (see migrations/supabase_006_drop_write_policies.sql).

COMMENT ON TABLE public.artifact_registry IS
    'Slice 6 append-only artifact registry. Rows keyed by content_hash; '
    '`paths` is mutable metadata for the same content. New content '
    'always gets a new row. No status / supersede semantics.';
