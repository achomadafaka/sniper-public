-- Migration: supabase_020_memories_ksg_extension.sql
-- KSG Phase 3: additive columns to public.memories per
-- docs/superpowers/specs/2026-05-15-branch-close-knowledge-sync-design.md §5.1
--
-- Rollback companion: supabase_020_rollback_memories_ksg_extension.sql
--
-- Run in: Supabase Dashboard → SQL Editor
-- Safe to re-run: every statement uses IF NOT EXISTS / IF EXISTS.
--
-- Constraints per spec §5.1 "Migration safety constraints (binding on Phase 3)":
--   * Additive columns only. No column rename. No type change.
--     No constraint tightening on existing columns.
--   * Idempotent.
--   * No data backfill in this migration (lazy backfill: each entry
--     gets entry_type / source_branch / source_pr the next time it is
--     touched).
--   * RLS inheritance: existing memories_select / memories_write policies
--     cover new columns by default. No new policy required.
--
-- Verification queries at the bottom of this file.

-- ============================================================
-- 1. Additive columns
-- ============================================================

ALTER TABLE public.memories
  ADD COLUMN IF NOT EXISTS slug             TEXT,
  ADD COLUMN IF NOT EXISTS entry_type       TEXT,
  ADD COLUMN IF NOT EXISTS summary          TEXT,
  ADD COLUMN IF NOT EXISTS tags             TEXT[] DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS source_branch    TEXT,
  ADD COLUMN IF NOT EXISTS source_pr        INTEGER,
  ADD COLUMN IF NOT EXISTS source_files     TEXT[],
  ADD COLUMN IF NOT EXISTS supersedes_ids   UUID[] DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS valid_to_commit  TEXT,
  ADD COLUMN IF NOT EXISTS confidence       TEXT,
  ADD COLUMN IF NOT EXISTS reviewed_by      TEXT,
  ADD COLUMN IF NOT EXISTS reviewed_at      TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS metadata         JSONB DEFAULT '{}'::jsonb;

-- ============================================================
-- 2. Check constraint on confidence — additive, applies only to
--    rows that set confidence
-- ============================================================

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'memories_confidence_check'
  ) THEN
    ALTER TABLE public.memories
      ADD CONSTRAINT memories_confidence_check
      CHECK (confidence IS NULL OR confidence IN ('low','medium','high','locked'));
  END IF;
END$$;

-- ============================================================
-- 3. Check constraint on entry_type — additive, applies only to
--    rows that set entry_type. NULL allowed so legacy entries are
--    untouched until lazy backfill.
-- ============================================================

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'memories_entry_type_check'
  ) THEN
    ALTER TABLE public.memories
      ADD CONSTRAINT memories_entry_type_check
      CHECK (entry_type IS NULL OR entry_type IN (
        'doctrine',
        'architecture',
        'workflow',
        'test_command',
        'bug_lesson',
        'semantic_fix',
        'wallet_insight',
        'strategy_thesis',
        'simulation_candidate',
        'legacy_retirement',
        'open_question',
        'research_note',
        'finding'
      ));
  END IF;
END$$;

-- ============================================================
-- 4. Indexes for the new query facets
-- ============================================================

CREATE INDEX IF NOT EXISTS idx_memories_entry_type
  ON public.memories (entry_type)
  WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_memories_source_pr
  ON public.memories (source_pr)
  WHERE source_pr IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_memories_tags_gin
  ON public.memories USING GIN (tags);

CREATE INDEX IF NOT EXISTS idx_memories_source_branch
  ON public.memories (source_branch)
  WHERE source_branch IS NOT NULL;

-- ============================================================
-- 5. Verification queries (informational — run after the migration)
-- ============================================================

-- Confirm new columns are present (expect 13 rows):
--   SELECT column_name, data_type
--   FROM information_schema.columns
--   WHERE table_name = 'memories'
--     AND column_name IN (
--       'slug','entry_type','summary','tags','source_branch','source_pr',
--       'source_files','supersedes_ids','valid_to_commit','confidence',
--       'reviewed_by','reviewed_at','metadata'
--     )
--   ORDER BY ordinal_position;

-- Confirm RLS inheritance is intact:
--   SELECT polname, polroles, polcmd FROM pg_policy
--   WHERE polrelid = 'public.memories'::regclass;

-- Confirm new constraints are present:
--   SELECT conname, pg_get_constraintdef(oid)
--   FROM pg_constraint
--   WHERE conrelid = 'public.memories'::regclass
--     AND conname IN ('memories_confidence_check','memories_entry_type_check');

-- Confirm indexes exist:
--   SELECT indexname FROM pg_indexes
--   WHERE tablename = 'memories'
--     AND indexname IN (
--       'idx_memories_entry_type',
--       'idx_memories_source_pr',
--       'idx_memories_tags_gin',
--       'idx_memories_source_branch'
--     );
