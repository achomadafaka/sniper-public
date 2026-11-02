-- SQLite funded ledger; additive and default OFF. Applied atomically by db_memecoin.
ALTER TABLE memecoin_desk_cohorts ADD COLUMN stop_mode TEXT NOT NULL DEFAULT 'loss_limit';
ALTER TABLE memecoin_desk_cohorts ADD COLUMN admission_enabled INTEGER NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS memecoin_known_group_candidates (
    candidate_id TEXT PRIMARY KEY, cohort_id TEXT NOT NULL, mint TEXT NOT NULL,
    creation_signature TEXT NOT NULL, proof_json TEXT NOT NULL, proof_hash TEXT NOT NULL, request_json TEXT NOT NULL,
    received_at_utc TEXT NOT NULL, deadline_utc TEXT NOT NULL, known_at_utc TEXT NOT NULL,
    eligible INTEGER NOT NULL DEFAULT 1, invalidated_at TEXT, reason TEXT,
    UNIQUE(cohort_id,mint,creation_signature)
);
CREATE INDEX IF NOT EXISTS idx_known_group_candidates_mint ON memecoin_known_group_candidates(mint,eligible);
CREATE TABLE IF NOT EXISTS memecoin_known_group_sells (
    signature TEXT NOT NULL, owner TEXT NOT NULL, mint TEXT NOT NULL, payload_json TEXT NOT NULL,
    observed_at TEXT NOT NULL, PRIMARY KEY(signature,owner,mint)
);
