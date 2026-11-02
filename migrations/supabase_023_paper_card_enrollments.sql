-- supabase_023_paper_card_enrollments.sql
-- D3 of the thesis-extractor conveyor: durable storage for forward-paper enrollments.
-- One narrow persistence table. NO strategy logic, NO verdict mutation, NO paper
-- evaluation, NO live-trade fields (no price/size/order_id/pnl). The forward-tracking
-- job (D4) owns state advancement + forward_metric/verdict (nullable here, inert at
-- enrollment). Contract: docs/superpowers/specs/2026-05-30-forward-paper-enrollment-contract.md.

CREATE TABLE IF NOT EXISTS public.paper_card_enrollments (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    wallet         text NOT NULL,
    card_id        text NOT NULL,
    card_version   text NOT NULL,
    profile_run_id text NOT NULL,
    -- embedded admission snapshot (the gate values that admitted this card)
    rank           integer NOT NULL,
    q_value        double precision,
    evidence_state text NOT NULL,
    data_tier      text,
    forward_test_design jsonb NOT NULL,
    -- paper lifecycle (router enrolls 'queued'; D4 advances)
    state          text NOT NULL DEFAULT 'queued'
                   CHECK (state IN ('queued','active','completed','killed','promoted')),
    -- D4-owned, NULL at enrollment (no paper evaluation yet)
    forward_metric jsonb,
    verdict        text,
    enrolled_at    timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT paper_card_enrollments_idem_key
        UNIQUE (wallet, card_id, card_version, profile_run_id)
);

-- RLS on, no policy: anon/authenticated see nothing; the service key (used by the
-- runtime adapter) bypasses RLS. Avoids the rls_disabled_in_public advisor error
-- without adding policy surface.
ALTER TABLE public.paper_card_enrollments ENABLE ROW LEVEL SECURITY;
