-- Discovery Events: Unified multi-stream wallet discovery
-- Codex-designed schema (2026-02-19)

CREATE TABLE IF NOT EXISTS discovery_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet TEXT NOT NULL,
    stream_type TEXT NOT NULL CHECK (stream_type IN ('low_buy','large_bet','fresh_account','volume_spike')),
    detected_at INTEGER NOT NULL,
    
    -- Core trade data (first-class columns for efficient querying)
    source_tx_hash TEXT NOT NULL DEFAULT '',
    event_fingerprint TEXT NOT NULL DEFAULT '',
    condition_id TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT '',
    side TEXT NOT NULL DEFAULT '',
    price REAL,
    notional REAL,
    
    -- Stream-specific extras (JSON)
    trigger_reason TEXT NOT NULL DEFAULT '',
    event_data_json TEXT NOT NULL DEFAULT '{}',
    
    -- Quick-scan scoring
    quick_score INTEGER NOT NULL DEFAULT 0,
    score_version INTEGER NOT NULL DEFAULT 0,
    scored_at INTEGER NOT NULL DEFAULT 0,
    
    -- Analysis workflow state machine
    analysis_status TEXT NOT NULL DEFAULT 'new' CHECK (
        analysis_status IN ('new','scored','queued','analyzed','skipped','error')
    ),
    analysis_version INTEGER NOT NULL DEFAULT 0,
    analyzed_at INTEGER NOT NULL DEFAULT 0
);

-- Indexes for efficient queries
CREATE INDEX IF NOT EXISTS idx_de_status_score_time 
    ON discovery_events(analysis_status, quick_score DESC, detected_at ASC);

CREATE INDEX IF NOT EXISTS idx_de_wallet_time 
    ON discovery_events(wallet, detected_at DESC);

CREATE INDEX IF NOT EXISTS idx_de_stream_time 
    ON discovery_events(stream_type, detected_at DESC);

-- Prevent duplicate events from same transaction
CREATE UNIQUE INDEX IF NOT EXISTS uq_de_stream_tx 
    ON discovery_events(stream_type, source_tx_hash) 
    WHERE source_tx_hash <> '';

-- Fallback dedupe when tx hash is missing/unstable.
CREATE UNIQUE INDEX IF NOT EXISTS uq_de_stream_fingerprint
    ON discovery_events(stream_type, event_fingerprint)
    WHERE event_fingerprint <> '';


-- Volume spike detection: Pre-aggregated hourly activity
CREATE TABLE IF NOT EXISTS wallet_activity_hourly (
    wallet TEXT NOT NULL,
    bucket_ts INTEGER NOT NULL,  -- Hour epoch timestamp
    trade_count INTEGER NOT NULL DEFAULT 0,
    total_notional REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (wallet, bucket_ts)
);

CREATE INDEX IF NOT EXISTS idx_wah_bucket 
    ON wallet_activity_hourly(bucket_ts);


-- Wallet-level analysis queue (deduped, not event-level)
CREATE TABLE IF NOT EXISTS wallet_analysis_queue (
    wallet TEXT PRIMARY KEY,
    priority_score INTEGER NOT NULL,
    first_event_ts INTEGER NOT NULL,
    last_event_ts INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued' CHECK (
        state IN ('queued', 'processing', 'retry', 'done', 'dead')
    ),
    locked_by TEXT NOT NULL DEFAULT '',
    locked_ts INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    next_retry_ts INTEGER NOT NULL DEFAULT 0,
    lease_expires_ts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    last_analysis_version INTEGER NOT NULL DEFAULT 0,
    updated_ts INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_waq_state_priority 
    ON wallet_analysis_queue(state, priority_score DESC);

CREATE INDEX IF NOT EXISTS idx_waq_sched
    ON wallet_analysis_queue(state, next_retry_ts, priority_score DESC, last_event_ts DESC);

CREATE INDEX IF NOT EXISTS idx_waq_lease
    ON wallet_analysis_queue(state, lease_expires_ts);
