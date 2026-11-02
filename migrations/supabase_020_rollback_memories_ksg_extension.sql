-- Migration: supabase_020_rollback_memories_ksg_extension.sql
-- Rollback companion for supabase_020_memories_ksg_extension.sql.
--
-- WARNING: dropping columns is destructive — any data written to the
-- new columns since the forward migration is lost. Use only when:
--   * The forward migration's data has been backed up, OR
--   * No production data has been written to the new columns yet.
--
-- Safe to re-run: every statement uses IF EXISTS.

-- ============================================================
-- 1. Drop indexes (must precede column drops because gin uses tags)
-- ============================================================

DROP INDEX IF EXISTS public.idx_memories_entry_type;
DROP INDEX IF EXISTS public.idx_memories_source_pr;
DROP INDEX IF EXISTS public.idx_memories_tags_gin;
DROP INDEX IF EXISTS public.idx_memories_source_branch;

-- ============================================================
-- 2. Drop constraints
-- ============================================================

ALTER TABLE public.memories
  DROP CONSTRAINT IF EXISTS memories_confidence_check,
  DROP CONSTRAINT IF EXISTS memories_entry_type_check;

-- ============================================================
-- 3. Drop columns
-- ============================================================

ALTER TABLE public.memories
  DROP COLUMN IF EXISTS slug,
  DROP COLUMN IF EXISTS entry_type,
  DROP COLUMN IF EXISTS summary,
  DROP COLUMN IF EXISTS tags,
  DROP COLUMN IF EXISTS source_branch,
  DROP COLUMN IF EXISTS source_pr,
  DROP COLUMN IF EXISTS source_files,
  DROP COLUMN IF EXISTS supersedes_ids,
  DROP COLUMN IF EXISTS valid_to_commit,
  DROP COLUMN IF EXISTS confidence,
  DROP COLUMN IF EXISTS reviewed_by,
  DROP COLUMN IF EXISTS reviewed_at,
  DROP COLUMN IF EXISTS metadata;
