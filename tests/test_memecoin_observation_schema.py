import sqlite3
from sniper.persistence.db_memecoin import ensure_memecoin_tables

EXPECTED = {
    "memecoin_token_episodes": {"episode_id", "chain_id", "token_address", "started_ts",
        "ended_ts", "first_signal_id", "consensus_cluster_count", "status", "last_signal_ts",
        "first_source_price", "first_executable_price"},
    "memecoin_episode_signals": {"episode_id", "upstream_event_id", "observed_at",
        "source_price", "executable_price"},
    "memecoin_radar_ratings": {"id", "attempt_id", "upstream_event_id", "snapshot_id",
        "episode_id", "network_id", "mint", "signal_source", "rating_version", "rating",
        "reason_codes_json", "facts_json", "facts_hash", "decided_at", "created_at",
        "clock_domain"},
    "memecoin_shadow_signals": {"shadow_signal_id", "episode_id", "origin_event_id",
        "network_id", "mint", "source_user_id", "source_handle", "signal_source",
        "observed_at", "claimed_source_lag_s", "source_price", "quote_observation_id",
        "quote_price", "quote_observed_at", "quote_age_s", "market_cap", "liquidity",
        "consensus_at_signal", "wallet_ab_pnl", "wallet_ab_closes", "rating",
        "reason_codes_json", "facts_json", "facts_hash", "created_at", "clock_domain"},
    "memecoin_holder_evidence": {"network_id", "mint", "source_user_id", "source_handle",
        "evidence_state", "remaining_usd", "observed_at", "source", "upstream_event_id",
        "confirmation_state", "successful_poll_at", "invalidated_at", "invalidation_reason",
        "updated_at", "clock_domain", "source_event_at", "source_timestamp_trust",
        "evidence_hash"},
    "memecoin_holder_evidence_history": {"id", "network_id", "mint", "source_user_id",
        "source_handle", "evidence_state", "remaining_usd", "observed_at", "source",
        "upstream_event_id", "confirmation_state", "successful_poll_at", "invalidated_at",
        "invalidation_reason", "recorded_at", "clock_domain", "source_event_at",
        "source_timestamp_trust", "evidence_hash"},
    "memecoin_entry_snapshots": {"snapshot_id", "upstream_event_id", "episode_id", "chain", "mint",
        "action", "decided_at", "mcap", "liquidity", "token_age_s", "consensus_n_60m",
        "wallet_trust_tier", "their_size_usd", "regime_id", "features_json", "feature_version",
        "data_quality_json", "entry_latency_json", "snapshot_hash"},
    "memecoin_entry_attempts": {"attempt_id", "classification_version", "classification_hash",
        "classification_json", "funded_contract_version", "funded_contract_hash",
        "funded_contract_json"},
    "memecoin_pending_entries": {"id", "funded_contract_version", "funded_contract_hash",
        "funded_contract_json"},
    "memecoin_positions": {"id", "funded_contract_version", "funded_contract_hash",
        "funded_contract_json", "paper_cohort_id", "paper_cohort_json",
        "experiment_status"},
    "memecoin_position_marks": {"id", "chain_id", "mint", "sweep_ts", "mid", "executable_exit_value",
                                 "liquidity", "mcap", "route_quote_id", "source", "quality"},
    "memecoin_policy_versions": {"policy_id", "version", "template", "parameters_json",
                                  "code_commit", "immutable_hash", "activated_ts", "retired_ts"},
    "memecoin_policy_assignments": {"episode_id", "arm_id", "policy_id", "policy_version",
        "entry_snapshot_id",
                                     "assignment_reason", "assigned_ts"},
    "memecoin_arm_outcomes": {"episode_id", "arm_id", "entry_cost", "net_exit_value", "net_pnl",
        "net_return", "max_executable_return", "max_drawdown", "exit_reason", "route_failures",
        "closed_ts"},
}

def test_observation_tables_have_expected_columns():
    c = sqlite3.connect(":memory:")
    ensure_memecoin_tables(c)
    for table, cols in EXPECTED.items():
        got = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        assert cols <= got, f"{table} missing {cols - got}"

def test_entry_snapshot_upstream_event_id_unique():
    c = sqlite3.connect(":memory:")
    ensure_memecoin_tables(c)
    c.execute("INSERT INTO memecoin_entry_snapshots(upstream_event_id, action) VALUES('e1','opened')")
    import pytest
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO memecoin_entry_snapshots(upstream_event_id, action) VALUES('e1','x')")


def test_contract_columns_migration_is_idempotent():
    c = sqlite3.connect(":memory:")
    ensure_memecoin_tables(c)
    ensure_memecoin_tables(c)
    for table in ("memecoin_entry_attempts", "memecoin_pending_entries", "memecoin_positions"):
        cols = [r[1] for r in c.execute(f"PRAGMA table_info({table})")]
        assert len(cols) == len(set(cols))
