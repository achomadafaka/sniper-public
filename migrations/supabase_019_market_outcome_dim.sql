-- Migration: supabase_019_market_outcome_dim.sql
-- Wallet Fact Ledger spec §7 — market_outcome_dim canonical Supabase dim.
--
-- Per-token outcome registry shared across all ledger consumers. The
-- local `market_outcome_dim_snapshot` (DuckDB) is a physical column-by-
-- column copy of these rows for the run's condition/token set —
-- analysis reads snapshots, never live canonical rows.
--
-- Resolution freezes per token when `resolution_price` is stable or
-- source-confirmed. Market-level `resolved=True` is NOT enough — see
-- spec §7.
--
-- `outcome_index` may be NULL. It is derived only when `clobTokenIds`
-- and `outcomes` align exactly. If unresolved, `dim_quality_flags_json`
-- carries the `outcome_index_unresolved` flag.
--
-- RLS pattern follows post-006: `_select` policy only, RLS enabled,
-- no `_write` block. Service-key writers bypass RLS entirely.
--
-- Idempotent: safe to re-run.

CREATE TABLE IF NOT EXISTS public.market_outcome_dim (
    token_id TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    outcome_index INTEGER,
    outcome_label TEXT,
    outcome_count INTEGER,
    is_binary BOOLEAN,
    market_structure TEXT,
    negative_risk BOOLEAN,
    market_slug TEXT,
    event_slug TEXT,
    title TEXT,
    end_date TIMESTAMPTZ,
    resolved BOOLEAN NOT NULL DEFAULT FALSE,
    resolved_at TIMESTAMPTZ,
    resolution_price DOUBLE PRECISION,
    resolution_source TEXT,
    dim_quality_flags_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    raw_market_json JSONB,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (token_id),
    CONSTRAINT market_outcome_dim_condition_token_uk
        UNIQUE (condition_id, token_id)
);

-- Spec §7: UNIQUE(condition_id, outcome_index) when outcome_index IS NOT
-- NULL. Postgres UNIQUE on a nullable column treats NULLs as distinct
-- (multiple NULL outcome_index rows per condition are permitted), so a
-- partial unique index is the canonical implementation.
CREATE UNIQUE INDEX IF NOT EXISTS
    market_outcome_dim_condition_outcome_idx_uk
    ON public.market_outcome_dim (condition_id, outcome_index)
    WHERE outcome_index IS NOT NULL;

CREATE INDEX IF NOT EXISTS market_outcome_dim_condition_idx
    ON public.market_outcome_dim (condition_id);

CREATE INDEX IF NOT EXISTS market_outcome_dim_resolved_idx
    ON public.market_outcome_dim (resolved);

ALTER TABLE public.market_outcome_dim ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "market_outcome_dim_select" ON public.market_outcome_dim;
CREATE POLICY "market_outcome_dim_select"
    ON public.market_outcome_dim
    FOR SELECT USING (true);

COMMENT ON TABLE public.market_outcome_dim IS
    'Wallet Fact Ledger spec §7 canonical per-token outcome registry. '
    'Local market_outcome_dim_snapshot (DuckDB) physically copies rows '
    'for a run''s condition/token set. Resolution freezes per token; '
    'market-level resolved=True is not sufficient.';

COMMENT ON COLUMN public.market_outcome_dim.outcome_index IS
    'Spec §7: NULL when clobTokenIds and outcomes do not align exactly. '
    'Unresolved rows carry the outcome_index_unresolved flag in '
    'dim_quality_flags_json.';

COMMENT ON COLUMN public.market_outcome_dim.dim_quality_flags_json IS
    'Spec §7: JSON array of dim-quality flags (e.g. '
    'outcome_index_unresolved). Empty array = clean.';
