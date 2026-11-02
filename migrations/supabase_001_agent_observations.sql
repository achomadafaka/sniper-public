-- ============================================================
-- Supabase Migration: strategy_agent_observations
-- ============================================================
-- Run this ONCE in the Supabase SQL Editor:
--   Dashboard → SQL Editor → New Query → paste + Run
--
-- This creates the persistent memory table used by all strategy
-- agents to store diagnoses, theses, wallet research, and
-- recommendations across cron runs.
-- ============================================================

CREATE TABLE IF NOT EXISTS strategy_agent_observations (
    id               BIGSERIAL PRIMARY KEY,

    -- Which strategy type generated this observation
    strategy_type    TEXT NOT NULL,          -- e.g. 'insider_copy_v1', 'btc_momentum'

    -- What kind of observation this is
    obs_type         TEXT NOT NULL
                     CHECK (obs_type IN (
                         'diagnosis',        -- per-run health summary
                         'thesis',           -- rolling hypothesis about the strategy
                         'wallet_research',  -- findings about a specific wallet
                         'pattern',          -- repeatable pattern discovered
                         'recommendation',   -- suggested change (human must apply)
                         'action_taken',     -- something the agent actually did
                         'run_summary'       -- end-of-run summary
                     )),

    -- The actual content (specific, evidence-based)
    content          TEXT NOT NULL,

    -- How confident the agent is in this finding
    confidence       TEXT NOT NULL DEFAULT 'medium'
                     CHECK (confidence IN ('low', 'medium', 'high')),

    -- Related wallet addresses (JSON array of strings)
    wallet_addresses TEXT NOT NULL DEFAULT '[]',

    -- Related strategy IDs (JSON array of integers)
    strategy_ids     TEXT NOT NULL DEFAULT '[]',

    -- Whether a human has reviewed/acted on this (for recommendations)
    acted_on         BOOLEAN NOT NULL DEFAULT FALSE,

    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Indexes ──────────────────────────────────────────────────
-- Agent reads by strategy_type + created_at (most recent first)
CREATE INDEX IF NOT EXISTS idx_sao_strategy_type
    ON strategy_agent_observations (strategy_type, created_at DESC);

-- Filter by observation type
CREATE INDEX IF NOT EXISTS idx_sao_obs_type
    ON strategy_agent_observations (obs_type);

-- Quick lookup of unacted recommendations
CREATE INDEX IF NOT EXISTS idx_sao_acted_on
    ON strategy_agent_observations (acted_on)
    WHERE acted_on = FALSE;

-- ── Row Level Security ────────────────────────────────────────
-- Enable RLS and allow the anon key full access (agents use anon key from .env)
ALTER TABLE strategy_agent_observations ENABLE ROW LEVEL SECURITY;

-- Split SELECT (lint-excluded) from writes (TO anon) per lint=0024.
DROP POLICY IF EXISTS "Allow anon full access" ON strategy_agent_observations;
CREATE POLICY "strategy_agent_observations_select"
    ON strategy_agent_observations FOR SELECT USING (true);
CREATE POLICY "strategy_agent_observations_write"
    ON strategy_agent_observations FOR ALL TO anon
    USING (true) WITH CHECK (true);

-- ── Grants ────────────────────────────────────────────────────
GRANT SELECT, INSERT, UPDATE ON strategy_agent_observations TO anon;
GRANT USAGE, SELECT ON SEQUENCE strategy_agent_observations_id_seq TO anon;

-- ── Verify ───────────────────────────────────────────────────
-- After running, confirm with:
-- SELECT table_name FROM information_schema.tables WHERE table_name = 'strategy_agent_observations';
