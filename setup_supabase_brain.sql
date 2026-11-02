-- ============================================================
-- SNIPER SUPABASE BRAIN — Schema Setup
-- Run this in the Supabase SQL Editor:
--   https://supabase.com/dashboard/project/nqtebcfwutftgaylmaon/sql/new
-- ============================================================

-- 1. Agent Memory — key/value persistent store
-- Claude reads this on every session start; writes notes, decisions, observations
CREATE TABLE IF NOT EXISTS agent_memory (
    id          BIGSERIAL PRIMARY KEY,
    key         TEXT NOT NULL UNIQUE,
    value       TEXT NOT NULL,
    category    TEXT NOT NULL DEFAULT 'general',  -- 'session', 'strategy', 'market', 'note'
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_agent_memory_key ON agent_memory(key);
CREATE INDEX IF NOT EXISTS idx_agent_memory_category ON agent_memory(category);

-- 2. News Items — scraped headlines with metadata
CREATE TABLE IF NOT EXISTS news_items (
    id              BIGSERIAL PRIMARY KEY,
    source          TEXT NOT NULL,          -- 'bbc_world', 'guardian', etc.
    title           TEXT NOT NULL,
    description     TEXT,
    url             TEXT,
    published_at    TIMESTAMPTZ,
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    topic_tags      TEXT[],                 -- extracted topics: ['iran', 'military', 'us']
    sentiment       TEXT,                   -- 'bullish_yes', 'bullish_no', 'neutral'
    processed       BOOLEAN DEFAULT FALSE,
    UNIQUE(source, title)
);
CREATE INDEX IF NOT EXISTS idx_news_items_fetched ON news_items(fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_items_source ON news_items(source);
CREATE INDEX IF NOT EXISTS idx_news_items_processed ON news_items(processed);

-- 3. Trade Ideas — news-driven market opportunities
CREATE TABLE IF NOT EXISTS trade_ideas (
    id              BIGSERIAL PRIMARY KEY,
    condition_id    TEXT NOT NULL,
    market_title    TEXT NOT NULL,
    outcome         TEXT NOT NULL,          -- 'YES' or 'NO'
    direction       TEXT NOT NULL,          -- 'buy_yes' or 'buy_no'
    current_price   REAL,
    target_price    REAL,
    confidence      TEXT NOT NULL DEFAULT 'low',  -- 'low', 'medium', 'high'
    reasoning       TEXT NOT NULL,
    news_trigger    TEXT,                   -- the news headline that triggered this idea
    news_source     TEXT,
    status          TEXT NOT NULL DEFAULT 'open',  -- 'open', 'acted', 'expired', 'rejected'
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ,
    acted_at        TIMESTAMPTZ,
    notes           TEXT
);
CREATE INDEX IF NOT EXISTS idx_trade_ideas_status ON trade_ideas(status);
CREATE INDEX IF NOT EXISTS idx_trade_ideas_created ON trade_ideas(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trade_ideas_condition ON trade_ideas(condition_id);

-- 4. Enable Row Level Security (open read for anon)
ALTER TABLE agent_memory ENABLE ROW LEVEL SECURITY;
ALTER TABLE news_items   ENABLE ROW LEVEL SECURITY;
ALTER TABLE trade_ideas  ENABLE ROW LEVEL SECURITY;

-- Allow anon read + write (service uses anon key)
CREATE POLICY "anon_all" ON agent_memory FOR ALL TO anon USING (true) WITH CHECK (true);
CREATE POLICY "anon_all" ON news_items   FOR ALL TO anon USING (true) WITH CHECK (true);
CREATE POLICY "anon_all" ON trade_ideas  FOR ALL TO anon USING (true) WITH CHECK (true);

-- 5. Seed initial memory with current system state
INSERT INTO agent_memory (key, value, category) VALUES
    ('system_version',    'sniper-v4', 'session'),
    ('active_strategies', 'sports_line_dislocation_v1, sports_no_value_v1, crypto_momentum_v1, cross_arb_v1, political_no_value_v1', 'strategy'),
    ('db_path',           '/home/sniper/sniper/sniper_tracker.db', 'session'),
    ('web_port',          '8080', 'session'),
    ('last_session_date', CURRENT_DATE::TEXT, 'session'),
    ('operator_notes',    'User wants news intelligence + trade ideas surfaced automatically. Political NO strategy now live. Cross-arb finds 0 opportunities - may need tuning.', 'note')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW();

SELECT 'Brain schema created successfully!' AS result;
