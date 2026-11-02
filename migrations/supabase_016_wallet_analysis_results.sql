-- Migration: supabase_016_wallet_analysis_results.sql
-- Slice 6 — durable analysis-results store for wallet protocol runs.
--
-- One row per `(wallet, run_id, window_id)`. Insert-ignore semantics:
-- create paths use Prefer: resolution=ignore-duplicates. Corrective
-- writes flip `status` via mark_superseded(...) PATCH; the row's
-- payload never overwrites in place.
--
-- Idempotent: safe to re-run.

CREATE TABLE IF NOT EXISTS public.wallet_analysis_results (
    wallet TEXT NOT NULL,
    run_id TEXT NOT NULL,
    window_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'superseded', 'rolled_back')),
    superseded_by_run_id TEXT,

    -- Headline summary (indexed reads).
    overall_verdict TEXT,
    gate_result TEXT,
    trading_mode TEXT,
    primary_edge_type TEXT,
    n_closed_positions INTEGER,
    n_held_to_resolution INTEGER,
    net_pnl DOUBLE PRECISION,
    dw_roi_point DOUBLE PRECISION,
    ew_roi_point DOUBLE PRECISION,

    -- Full report + provenance.
    report_json JSONB NOT NULL,
    replay_surface_meta JSONB,
    manifest_hashes JSONB,
    artifact_hashes JSONB,
    wallet_warehouse_path TEXT,

    -- Schema/code versions for the row's payload.
    protocol_version TEXT,
    code_version TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (wallet, run_id, window_id)
);

CREATE INDEX IF NOT EXISTS wallet_analysis_results_wallet_status_idx
    ON public.wallet_analysis_results (wallet, status);

ALTER TABLE public.wallet_analysis_results ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "wallet_analysis_results_select" ON public.wallet_analysis_results;
CREATE POLICY "wallet_analysis_results_select"
    ON public.wallet_analysis_results
    FOR SELECT USING (true);
-- _write policy intentionally omitted — service-key writers bypass RLS
-- entirely (see migrations/supabase_006_drop_write_policies.sql).

COMMENT ON TABLE public.wallet_analysis_results IS
    'Slice 6 durable output. One row per (wallet, run_id, window_id). '
    'Insert-ignore on create; mark_superseded() PATCH on correction.';
