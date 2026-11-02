-- Wallets that Pablo has decided to follow
CREATE TABLE IF NOT EXISTS followed_wallets (
    wallet TEXT PRIMARY KEY,
    label TEXT NOT NULL DEFAULT '',
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    added_reason TEXT,
    status TEXT DEFAULT 'active',
    strategy_json TEXT,
    notes TEXT,
    total_positions INTEGER DEFAULT 0,
    total_pnl_usdc REAL DEFAULT 0,
    last_copied_at TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Positions that Pablo has copied from followed wallets
CREATE TABLE IF NOT EXISTS copied_positions (
    position_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_wallet TEXT NOT NULL,
    source_trade_id TEXT,
    condition_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    market_title TEXT,
    direction TEXT NOT NULL,

    -- Entry
    entry_price REAL NOT NULL,
    entry_size_usdc REAL NOT NULL,
    opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    -- Current state
    current_price REAL,
    last_price_update TIMESTAMP,

    -- Exit
    exit_price REAL,
    exit_size_usdc REAL,
    closed_at TIMESTAMP,

    -- Performance
    pnl_usdc REAL,
    roi_percent REAL,

    -- Status & control
    status TEXT DEFAULT 'open',
    close_reason TEXT,

    -- Strategy (at time of copy)
    strategy_take_profit REAL,
    strategy_stop_loss REAL,

    -- Metadata
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (source_wallet) REFERENCES followed_wallets(wallet)
);

-- Alerts/notifications sent to Pablo
CREATE TABLE IF NOT EXISTS alerts_sent (
    alert_id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_type TEXT NOT NULL,
    wallet TEXT,
    message TEXT NOT NULL,
    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    channel TEXT DEFAULT 'telegram',
    acknowledged BOOLEAN DEFAULT 0,
    response TEXT
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_followed_status ON followed_wallets(status);
CREATE INDEX IF NOT EXISTS idx_copied_wallet ON copied_positions(source_wallet);
CREATE INDEX IF NOT EXISTS idx_copied_status ON copied_positions(status);
CREATE INDEX IF NOT EXISTS idx_copied_opened ON copied_positions(opened_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_type ON alerts_sent(alert_type);
CREATE INDEX IF NOT EXISTS idx_alerts_sent ON alerts_sent(sent_at DESC);
