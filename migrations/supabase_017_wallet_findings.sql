-- Migration: supabase_017_wallet_findings.sql
-- Slice 6 — structured numeric findings derived from each analysis.
--
-- `finding_uid = sha256(wallet | run_id | window_id | finding_type |
-- source_hypothesis_id)` so re-deriving the same findings is a no-op.
-- finding_schema_version pins the deriver's payload contract — bump on
-- any matrix change so the Phase-2D LLM input layer can guard.
--
-- Insert-ignore on create, mark_superseded() PATCH on correction.
--
-- Idempotent: safe to re-run.

CREATE TABLE IF NOT EXISTS public.wallet_findings (
    finding_uid TEXT NOT NULL,
    wallet TEXT NOT NULL,
    run_id TEXT NOT NULL,
    window_id TEXT NOT NULL,
    finding_type TEXT NOT NULL,
    severity TEXT NOT NULL
        CHECK (severity IN ('info', 'warn', 'insight')),
    confidence TEXT,
    source_hypothesis_id TEXT,

    numeric_evidence_json JSONB NOT NULL,
    notes JSONB,

    finding_schema_version INTEGER NOT NULL DEFAULT 1,

    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'superseded', 'rolled_back')),
    superseded_by_finding_uid TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (finding_uid),

    -- Spec §15: every finding belongs to a (wallet, run_id, window_id)
    -- analysis result. ON DELETE RESTRICT preserves the
    -- supersede/rollback contract — finding rows must be explicitly
    -- transitioned via mark_superseded() before the parent result row
    -- can be removed.
    CONSTRAINT wallet_findings_result_fk
        FOREIGN KEY (wallet, run_id, window_id)
        REFERENCES public.wallet_analysis_results (wallet, run_id, window_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS wallet_findings_wallet_run_window_status_idx
    ON public.wallet_findings (wallet, run_id, window_id, status);

CREATE INDEX IF NOT EXISTS wallet_findings_wallet_status_idx
    ON public.wallet_findings (wallet, status);

ALTER TABLE public.wallet_findings ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "wallet_findings_select" ON public.wallet_findings;
CREATE POLICY "wallet_findings_select"
    ON public.wallet_findings
    FOR SELECT USING (true);
-- _write policy intentionally omitted — service-key writers bypass RLS
-- entirely (see migrations/supabase_006_drop_write_policies.sql).

COMMENT ON TABLE public.wallet_findings IS
    'Slice 6 structured findings. finding_uid = sha256(natural-key + '
    'finding_type + source_hypothesis_id). Default reads filter '
    'status=active.';
