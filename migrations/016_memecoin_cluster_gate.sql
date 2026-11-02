PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS cluster_funder_cache (
    owner TEXT NOT NULL,
    parser_version INTEGER NOT NULL,
    funder TEXT,
    status TEXT NOT NULL,
    source_signature TEXT,
    resolved_at TEXT NOT NULL,
    PRIMARY KEY (owner, parser_version)
);

CREATE TABLE IF NOT EXISTS cluster_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    mint TEXT NOT NULL,
    snapshot_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cluster_snapshot_holders (
    snapshot_id TEXT NOT NULL,
    ata TEXT NOT NULL,
    owner TEXT,
    balance_atomic TEXT NOT NULL,
    exclusion TEXT,
    PRIMARY KEY (snapshot_id, ata),
    FOREIGN KEY (snapshot_id) REFERENCES cluster_snapshots(snapshot_id)
);

CREATE TABLE IF NOT EXISTS cluster_decisions (
    decision_id TEXT PRIMARY KEY,
    snapshot_id TEXT,
    source_path TEXT NOT NULL,
    allowed INTEGER NOT NULL,
    reason TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    snapshot_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    metrics_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cluster_work_queue (
    owner TEXT PRIMARY KEY,
    observed_at TEXT NOT NULL,
    status TEXT NOT NULL,
    next_attempt_at TEXT,
    claimed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_cluster_queue_newest
ON cluster_work_queue(status, observed_at DESC);
