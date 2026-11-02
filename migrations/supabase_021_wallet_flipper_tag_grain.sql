-- Migration: supabase_021_wallet_flipper_tag_grain.sql
-- Flipper Inference Phase 1.1 — new table for per-wallet flipper tag-grain
-- persistence. See docs/superpowers/specs/2026-05-15-flipper-inference-phase1-
-- persistence-design.md.
--
-- Rollback companion: supabase_021_rollback_wallet_flipper_tag_grain.sql
--
-- Run in: Supabase Dashboard → SQL Editor
-- Safe to re-run: every statement uses IF NOT EXISTS / DO ... NOT EXISTS guards.
--
-- Constraints per spec §1 + §4:
--   * Additive (new table; no change to existing tables).
--   * Idempotent.
--   * No data backfill. Population happens via the Phase 1.2 writer.
--   * RLS: matches the existing memories_select / memories_write convention
--     from supabase_005_rls_scope_all.sql — anon-scoped writes; public reads.
--   * Active-supersession invariants enforced via CHECK constraints.

-- ============================================================
-- 1. Table
-- ============================================================

CREATE TABLE IF NOT EXISTS public.wallet_flipper_tag_grain (
  -- Identity
  wallet                       TEXT          NOT NULL,
  grain                        TEXT          NOT NULL,
  tag                          TEXT          NOT NULL,
  wallet_fixture_hash          TEXT          NOT NULL,

  -- Per-grain metrics from flipper_branch.tag_grains
  n_at_grain                   INTEGER       NOT NULL,
  grain_roi                    DOUBLE PRECISION NOT NULL,
  grain_win_rate               DOUBLE PRECISION NOT NULL,
  grain_total_pnl              DOUBLE PRECISION NOT NULL,
  grain_max_realized_loss      DOUBLE PRECISION,

  -- Wallet-level context (copied per row for filter efficiency)
  flip_rate                    DOUBLE PRECISION NOT NULL,
  walk_full_history            BOOLEAN       NOT NULL,
  analyzed_at                  TIMESTAMPTZ   NOT NULL,
  protocol_version             TEXT          NOT NULL,
  code_version                 TEXT          NOT NULL,

  -- Lifecycle
  status                       TEXT          NOT NULL DEFAULT 'active',
  superseded_by_fixture_hash   TEXT,

  -- Audit
  created_at                   TIMESTAMPTZ   NOT NULL DEFAULT now(),
  updated_at                   TIMESTAMPTZ   NOT NULL DEFAULT now(),

  PRIMARY KEY (wallet, grain, tag, wallet_fixture_hash)
);

-- ============================================================
-- 2. Check constraints (idempotent via DO blocks)
-- ============================================================

-- grain ∈ {leaf, nav} — matches enumerate_tag_grains output
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'wftg_grain_check'
  ) THEN
    ALTER TABLE public.wallet_flipper_tag_grain
      ADD CONSTRAINT wftg_grain_check
      CHECK (grain IN ('leaf', 'nav'));
  END IF;
END$$;

-- status lifecycle
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'wftg_status_check'
  ) THEN
    ALTER TABLE public.wallet_flipper_tag_grain
      ADD CONSTRAINT wftg_status_check
      CHECK (status IN ('active', 'superseded'));
  END IF;
END$$;

-- Active-supersession invariant: active row → superseded_by_fixture_hash IS NULL
-- AND superseded row → superseded_by_fixture_hash IS NOT NULL
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'wftg_supersession_invariant'
  ) THEN
    ALTER TABLE public.wallet_flipper_tag_grain
      ADD CONSTRAINT wftg_supersession_invariant
      CHECK (
        (status = 'active' AND superseded_by_fixture_hash IS NULL)
        OR
        (status = 'superseded' AND superseded_by_fixture_hash IS NOT NULL)
      );
  END IF;
END$$;

-- n_at_grain ≥ 1 — spec says writer skips zero-row grains; defense in depth
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'wftg_n_at_grain_positive'
  ) THEN
    ALTER TABLE public.wallet_flipper_tag_grain
      ADD CONSTRAINT wftg_n_at_grain_positive
      CHECK (n_at_grain >= 1);
  END IF;
END$$;

-- win_rate ∈ [0, 1]
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'wftg_win_rate_range'
  ) THEN
    ALTER TABLE public.wallet_flipper_tag_grain
      ADD CONSTRAINT wftg_win_rate_range
      CHECK (grain_win_rate >= 0.0 AND grain_win_rate <= 1.0);
  END IF;
END$$;

-- flip_rate ∈ [0, 1]
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'wftg_flip_rate_range'
  ) THEN
    ALTER TABLE public.wallet_flipper_tag_grain
      ADD CONSTRAINT wftg_flip_rate_range
      CHECK (flip_rate >= 0.0 AND flip_rate <= 1.0);
  END IF;
END$$;

-- ============================================================
-- 3. Indexes
-- ============================================================

-- Primary aggregator query (§4): WHERE grain = $1 AND tag = $2 AND status='active'
CREATE INDEX IF NOT EXISTS idx_wftg_grain_tag_active
  ON public.wallet_flipper_tag_grain (grain, tag)
  WHERE status = 'active';

-- "At most one active row per (wallet, grain, tag)" — partial unique index.
-- Defends against the race where a concurrent writer inserts a new row
-- but never marks the prior row superseded. The aggregator (§4) groups
-- by (grain, tag) and AVERAGES grain_roi — duplicate active rows for
-- the same wallet would double-count.
CREATE UNIQUE INDEX IF NOT EXISTS idx_wftg_one_active_per_wgt
  ON public.wallet_flipper_tag_grain (wallet, grain, tag)
  WHERE status = 'active';

-- Per-wallet lookups (supersede path needs to find active rows for a wallet)
CREATE INDEX IF NOT EXISTS idx_wftg_wallet_active
  ON public.wallet_flipper_tag_grain (wallet)
  WHERE status = 'active';

-- Protocol-version filtering — partial-indexed on active rows
CREATE INDEX IF NOT EXISTS idx_wftg_protocol_version_active
  ON public.wallet_flipper_tag_grain (protocol_version)
  WHERE status = 'active';

-- Recency filtering (future use per spec §7 open question)
CREATE INDEX IF NOT EXISTS idx_wftg_analyzed_at_active
  ON public.wallet_flipper_tag_grain (analyzed_at DESC)
  WHERE status = 'active';

-- ============================================================
-- 4. updated_at trigger
-- ============================================================

CREATE OR REPLACE FUNCTION public.set_wftg_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger WHERE tgname = 'wftg_set_updated_at'
  ) THEN
    CREATE TRIGGER wftg_set_updated_at
    BEFORE UPDATE ON public.wallet_flipper_tag_grain
    FOR EACH ROW EXECUTE FUNCTION public.set_wftg_updated_at();
  END IF;
END$$;

-- ============================================================
-- 5. RLS — matches supabase_005_rls_scope_all.sql pattern
-- ============================================================

ALTER TABLE public.wallet_flipper_tag_grain ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policy
    WHERE polname = 'wftg_select' AND polrelid = 'public.wallet_flipper_tag_grain'::regclass
  ) THEN
    EXECUTE 'CREATE POLICY wftg_select ON public.wallet_flipper_tag_grain FOR SELECT USING (true)';
  END IF;
END$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policy
    WHERE polname = 'wftg_write' AND polrelid = 'public.wallet_flipper_tag_grain'::regclass
  ) THEN
    EXECUTE 'CREATE POLICY wftg_write ON public.wallet_flipper_tag_grain FOR ALL TO anon USING (true) WITH CHECK (true)';
  END IF;
END$$;

-- ============================================================
-- 6. Verification queries (informational)
-- ============================================================

-- Confirm table + columns:
--   SELECT column_name, data_type FROM information_schema.columns
--   WHERE table_name = 'wallet_flipper_tag_grain'
--   ORDER BY ordinal_position;

-- Confirm constraints:
--   SELECT conname, pg_get_constraintdef(oid)
--   FROM pg_constraint
--   WHERE conrelid = 'public.wallet_flipper_tag_grain'::regclass
--   ORDER BY conname;

-- Confirm indexes:
--   SELECT indexname, indexdef FROM pg_indexes
--   WHERE tablename = 'wallet_flipper_tag_grain'
--   ORDER BY indexname;

-- Confirm RLS:
--   SELECT polname, polcmd, polroles, pg_get_expr(polqual, polrelid)
--   FROM pg_policy
--   WHERE polrelid = 'public.wallet_flipper_tag_grain'::regclass;
