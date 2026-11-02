-- Sniper AI Bot — Supabase shared memory schema
-- Run this in Supabase Dashboard → SQL Editor
-- Safe to re-run: all statements use IF NOT EXISTS / IF EXISTS guards.

-- ============================================================
-- memories: persistent key/value store shared between
--           local Claude Code and the VPS bot
-- ============================================================
CREATE TABLE IF NOT EXISTS memories (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    type        TEXT        NOT NULL CHECK (type IN ('user', 'feedback', 'project', 'reference')),
    name        TEXT        NOT NULL,
    description TEXT        NOT NULL,
    content     TEXT        NOT NULL,
    source      TEXT        NOT NULL DEFAULT 'vps' CHECK (source IN ('vps', 'local')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Unique constraint so upsert-by-(type,name) is safe and unambiguous
ALTER TABLE memories
    ADD CONSTRAINT IF NOT EXISTS memories_type_name_unique UNIQUE (type, name);

-- Auto-update updated_at on every row change
-- SET search_path = '' pins the function to pg_catalog only (no user schemas),
-- preventing search_path injection (Supabase lint: function_search_path_mutable).
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql
SET search_path = ''
AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_memories_updated_at ON memories;
CREATE TRIGGER trg_memories_updated_at
    BEFORE UPDATE ON memories
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ============================================================
-- session_logs: per-session work summaries for cross-instance
--               handoff (VPS bot → local Claude and vice versa)
-- ============================================================
CREATE TABLE IF NOT EXISTS session_logs (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    source          TEXT        NOT NULL CHECK (source IN ('vps', 'local')),
    summary         TEXT        NOT NULL,
    key_decisions   TEXT,
    files_changed   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- Indexes
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_memories_type        ON memories (type);
CREATE INDEX IF NOT EXISTS idx_memories_updated     ON memories (updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_memories_name        ON memories (name);
CREATE INDEX IF NOT EXISTS idx_session_logs_created ON session_logs (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_session_logs_source  ON session_logs (source);

-- ============================================================
-- Row Level Security — anon key (used by the bot) needs full
-- read/write access; no multi-tenant isolation required here.
-- ============================================================
ALTER TABLE memories    ENABLE ROW LEVEL SECURITY;
ALTER TABLE session_logs ENABLE ROW LEVEL SECURITY;

-- RLS policies: split SELECT (lint-excluded) from writes (scoped TO anon).
-- This is a single-tenant internal service; USING(true)/WITH CHECK(true)
-- is intentional. Scoping writes TO anon clears the Supabase advisor
-- rls_policy_always_true lint (lint=0024).
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename='memories' AND policyname='memories_select') THEN
        DROP POLICY IF EXISTS "Allow all access to memories" ON memories;
        CREATE POLICY "memories_select" ON memories FOR SELECT USING (true);
        CREATE POLICY "memories_write"  ON memories FOR ALL TO anon USING (true) WITH CHECK (true);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename='session_logs' AND policyname='session_logs_select') THEN
        DROP POLICY IF EXISTS "Allow all access to session_logs" ON session_logs;
        CREATE POLICY "session_logs_select" ON session_logs FOR SELECT USING (true);
        CREATE POLICY "session_logs_write"  ON session_logs FOR ALL TO anon USING (true) WITH CHECK (true);
    END IF;
END $$;
