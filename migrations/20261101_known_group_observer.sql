-- SQLite private data-only observer state, separate from the funded ledger.
CREATE TABLE IF NOT EXISTS memecoin_known_group_tail (
    source TEXT PRIMARY KEY, identity TEXT NOT NULL, cursor INTEGER NOT NULL, anchor TEXT,
    baseline INTEGER NOT NULL, started_at REAL NOT NULL, last_host REAL NOT NULL,
    state_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memecoin_known_group_evidence (
    proof_ref TEXT PRIMARY KEY, sha256 TEXT NOT NULL, raw_size INTEGER NOT NULL,
    profile_zlib BLOB NOT NULL
);
