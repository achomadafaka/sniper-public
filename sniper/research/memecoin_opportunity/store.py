"""Isolated SQLite ledger for immutable launch-opportunity facts."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping

from .identity import canonical_json, content_fingerprint, normalize_identity, require_utc_iso
from .types import CanonicalEvent, NetworkId, RawEvent, VenueId


DEFAULT_DB_PATH = "data/memecoin_opportunity.db"
SCHEMA_VERSION = 17


# The paper-adapter ledger, as individual statements so the same DDL can be appended
# to SCHEMA for fresh databases and replayed statement-by-statement by _migrate_schema
# for existing ones. It cannot be one executescript blob: that implicitly commits, and
# the migration runs inside an open transaction. One source, so the two paths cannot
# drift.
#
# Both tables sit OUTSIDE the retention delete set, like op_creator_outcomes. A receipt
# whose launch pruned minutes later still has to be readable, and a handoff cursor that
# can lose rows to retention is not a cursor. Their delete triggers deliberately omit
# the op_retention_delete_allowed() exemption, so retention authority cannot unlock
# them even by accident.
_PAPER_ADAPTER_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS op_paper_fill_requests (
    request_id TEXT PRIMARY KEY,
    alert_id TEXT NOT NULL,
    launch_id TEXT NOT NULL,
    network_id TEXT NOT NULL,
    venue_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    creator_id TEXT,
    launch_mode TEXT NOT NULL,
    state_fingerprint TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    evaluator_version TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    fill_model_version TEXT NOT NULL,
    clip_usd_micros INTEGER NOT NULL,
    decision_at TEXT NOT NULL,
    decision_age_ms INTEGER NOT NULL,
    intent_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    local_qty_atomic TEXT,
    row_fingerprint TEXT NOT NULL,
    inserted_at TEXT NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS op_paper_fills (
    receipt_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('filled','entry_unfilled')),
    unfilled_reason TEXT,
    quote_provider TEXT,
    route_binding_json TEXT,
    quote_in_asset TEXT,
    quote_in_atomic TEXT,
    quote_in_decimals INTEGER,
    quote_out_asset TEXT,
    quote_out_decimals INTEGER,
    expected_out_atomic TEXT,
    floor_out_atomic TEXT,
    slippage_bps INTEGER,
    quote_payload_json TEXT,
    quote_fingerprint TEXT,
    quote_started_at TEXT,
    quote_completed_at TEXT,
    receipt_at TEXT NOT NULL,
    queue_delay_ms INTEGER,
    quote_latency_ms INTEGER,
    receipt_latency_ms INTEGER,
    row_fingerprint TEXT NOT NULL,
    inserted_at TEXT NOT NULL,
    CHECK ((status='filled') = (unfilled_reason IS NULL))
)""",
    "CREATE TRIGGER IF NOT EXISTS op_paper_fill_requests_no_update "
    "BEFORE UPDATE ON op_paper_fill_requests "
    "BEGIN SELECT RAISE(ABORT, 'op_paper_fill_requests immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS op_paper_fill_requests_no_delete "
    "BEFORE DELETE ON op_paper_fill_requests "
    "BEGIN SELECT RAISE(ABORT, 'op_paper_fill_requests immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS op_paper_fills_no_update "
    "BEFORE UPDATE ON op_paper_fills "
    "BEGIN SELECT RAISE(ABORT, 'op_paper_fills immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS op_paper_fills_no_delete "
    "BEFORE DELETE ON op_paper_fills "
    "BEGIN SELECT RAISE(ABORT, 'op_paper_fills immutable'); END",
)

_PAPER_LIFECYCLE_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS op_paper_lifecycle_activations (
    lifecycle_version TEXT PRIMARY KEY,
    activated_at TEXT NOT NULL,
    config_json TEXT NOT NULL,
    row_fingerprint TEXT NOT NULL,
    inserted_at TEXT NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS op_paper_exit_observations (
    observation_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    lifecycle_version TEXT NOT NULL,
    scheduled_age_ms INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('executable','unavailable')),
    failure_reason TEXT,
    quote_provider TEXT,
    quote_in_asset TEXT,
    quote_in_atomic TEXT,
    quote_in_decimals INTEGER,
    quote_out_asset TEXT,
    quote_out_atomic TEXT,
    quote_out_decimals INTEGER,
    expected_out_atomic TEXT,
    floor_out_atomic TEXT,
    slippage_bps INTEGER,
    route_binding_json TEXT,
    quote_payload_json TEXT,
    quote_started_at TEXT,
    quote_completed_at TEXT,
    observed_at TEXT NOT NULL,
    return_bps INTEGER,
    row_fingerprint TEXT NOT NULL,
    inserted_at TEXT NOT NULL,
    UNIQUE(request_id, lifecycle_version, scheduled_age_ms),
    CHECK ((status='executable') = (failure_reason IS NULL))
)""",
    """CREATE TABLE IF NOT EXISTS op_paper_outcomes (
    outcome_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    lifecycle_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('valid','censored')),
    result_code TEXT NOT NULL CHECK (
        result_code IN (
            'target_first','stop_first','time_exit','route_censored',
            'adaptive_exit','trail_exit','creator_exit'
        )
    ),
    terminal_observation_id TEXT,
    entry_quote_atomic TEXT NOT NULL,
    exit_floor_atomic TEXT,
    return_bps INTEGER,
    baseline_return_bps INTEGER,
    baseline_move_bps INTEGER,
    outcome_at TEXT NOT NULL,
    origin_kind TEXT,
    policy_hash TEXT,
    decision_facts_json TEXT,
    exit_reason_codes_json TEXT,
    row_fingerprint TEXT NOT NULL,
    inserted_at TEXT NOT NULL,
    UNIQUE(request_id, lifecycle_version),
    CHECK ((status='valid') = (return_bps IS NOT NULL))
)""",
    "CREATE TRIGGER IF NOT EXISTS op_paper_lifecycle_activations_no_update "
    "BEFORE UPDATE ON op_paper_lifecycle_activations "
    "BEGIN SELECT RAISE(ABORT, 'op_paper_lifecycle_activations immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS op_paper_lifecycle_activations_no_delete "
    "BEFORE DELETE ON op_paper_lifecycle_activations "
    "BEGIN SELECT RAISE(ABORT, 'op_paper_lifecycle_activations immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS op_paper_exit_observations_no_update "
    "BEFORE UPDATE ON op_paper_exit_observations "
    "BEGIN SELECT RAISE(ABORT, 'op_paper_exit_observations immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS op_paper_exit_observations_no_delete "
    "BEFORE DELETE ON op_paper_exit_observations "
    "BEGIN SELECT RAISE(ABORT, 'op_paper_exit_observations immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS op_paper_outcomes_no_update "
    "BEFORE UPDATE ON op_paper_outcomes "
    "BEGIN SELECT RAISE(ABORT, 'op_paper_outcomes immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS op_paper_outcomes_no_delete "
    "BEFORE DELETE ON op_paper_outcomes "
    "BEGIN SELECT RAISE(ABORT, 'op_paper_outcomes immutable'); END",
)

# `radar-buyer-breadth-v1` decision labels. OUTSIDE the retention delete set, like
# op_creator_outcomes: the forward clock reads these long after their launches prune.
# One row per launch, frozen at the control decision. A label is not an arm and takes no
# slot -- see breadth_label.py for why that distinction decides the measurement.
_BREADTH_LABEL_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS op_breadth_labels (
    launch_id TEXT PRIMARY KEY,
    network_id TEXT NOT NULL,
    venue_id TEXT NOT NULL,
    decision_snapshot_id TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    breadth_class TEXT NOT NULL
        CHECK (breadth_class IN ('pass','middle','control','censored')),
    censor_reason TEXT,
    eligible_buyer_count INTEGER,
    entity_buyer_count INTEGER,
    launch_mode TEXT,
    decision_age_ms INTEGER,
    progress_bps INTEGER,
    modeled_round_trip_cost_bps INTEGER,
    observed_at TEXT NOT NULL,
    row_fingerprint TEXT NOT NULL,
    inserted_at TEXT NOT NULL,
    CHECK ((breadth_class='censored') = (eligible_buyer_count IS NULL)),
    CHECK ((breadth_class='censored') = (censor_reason IS NOT NULL))
)""",
    "CREATE INDEX IF NOT EXISTS idx_op_breadth_labels_class "
    "ON op_breadth_labels(breadth_class, launch_mode)",
    "CREATE TRIGGER IF NOT EXISTS op_breadth_labels_no_update "
    "BEFORE UPDATE ON op_breadth_labels "
    "BEGIN SELECT RAISE(ABORT, 'op_breadth_labels immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS op_breadth_labels_no_delete "
    "BEFORE DELETE ON op_breadth_labels "
    "BEGIN SELECT RAISE(ABORT, 'op_breadth_labels immutable'); END",
)


# The EVM chain scan position, kept OUTSIDE `op_source_frontiers` on purpose.
#
# A source frontier records "events are durable through cursor C" and its writer refuses
# a cursor with no row in `op_raw_events`. A scan cursor records something different:
# "the chain was enumerated through block N", which is true even when the range held no
# event at all. Storing one in the other conflates enumeration with event durability --
# the exact mistake that let a venue's frozen EVENT frontier be read as the chain
# position, and that let a restart skip a downtime interval.
#
# One row per chain, monotonic, and outside the retention delete set.
_EVM_SCAN_CURSOR_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS op_evm_scan_cursors (
    chain_id TEXT PRIMARY KEY,
    scanned_through_block INTEGER NOT NULL CHECK (scanned_through_block >= 0),
    seeded_from TEXT NOT NULL,
    updated_at TEXT NOT NULL
)""",
    "CREATE TRIGGER IF NOT EXISTS op_evm_scan_cursors_no_rewind "
    "BEFORE UPDATE ON op_evm_scan_cursors "
    "FOR EACH ROW WHEN NEW.scanned_through_block < OLD.scanned_through_block "
    "BEGIN SELECT RAISE(ABORT, 'op_evm_scan_cursors must not rewind'); END",
    "CREATE TRIGGER IF NOT EXISTS op_evm_scan_cursors_no_delete "
    "BEFORE DELETE ON op_evm_scan_cursors "
    "BEGIN SELECT RAISE(ABORT, 'op_evm_scan_cursors immutable'); END",
)


_PAPER_V3_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS op_paper_position_actions (
    action_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    lifecycle_version TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('DERISK','EXIT_FULL')),
    fraction_ppm INTEGER NOT NULL,
    quantity_atomic TEXT NOT NULL,
    expected_out_atomic TEXT NOT NULL,
    scored_out_atomic TEXT NOT NULL,
    reason_codes_json TEXT NOT NULL,
    policy_hash TEXT NOT NULL,
    quote_evidence_id TEXT,
    market_evidence_id TEXT,
    observed_at TEXT NOT NULL,
    row_fingerprint TEXT NOT NULL,
    inserted_at TEXT NOT NULL,
    UNIQUE(request_id, lifecycle_version, sequence)
)""",
    """CREATE TABLE IF NOT EXISTS op_paper_position_states (
    state_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    lifecycle_version TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    policy_hash TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    row_fingerprint TEXT NOT NULL,
    inserted_at TEXT NOT NULL,
    UNIQUE(request_id, lifecycle_version, sequence)
)""",
    """CREATE TABLE IF NOT EXISTS op_paper_exit_followups (
    followup_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    lifecycle_version TEXT NOT NULL,
    horizon_s INTEGER NOT NULL,
    due_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('scheduled','executable','unavailable')),
    expected_out_atomic TEXT,
    return_bps INTEGER,
    failure_reason TEXT,
    observed_at TEXT,
    row_fingerprint TEXT NOT NULL,
    inserted_at TEXT NOT NULL,
    UNIQUE(request_id, lifecycle_version, horizon_s)
)""",
    "CREATE TRIGGER IF NOT EXISTS op_paper_position_actions_no_update "
    "BEFORE UPDATE ON op_paper_position_actions "
    "BEGIN SELECT RAISE(ABORT, 'op_paper_position_actions immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS op_paper_position_actions_no_delete "
    "BEFORE DELETE ON op_paper_position_actions "
    "BEGIN SELECT RAISE(ABORT, 'op_paper_position_actions immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS op_paper_position_states_no_update "
    "BEFORE UPDATE ON op_paper_position_states "
    "BEGIN SELECT RAISE(ABORT, 'op_paper_position_states immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS op_paper_position_states_no_delete "
    "BEFORE DELETE ON op_paper_position_states "
    "BEGIN SELECT RAISE(ABORT, 'op_paper_position_states immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS op_paper_exit_followups_no_delete "
    "BEFORE DELETE ON op_paper_exit_followups "
    "BEGIN SELECT RAISE(ABORT, 'op_paper_exit_followups immutable'); END",
)


class ImmutableConflict(RuntimeError):
    """An immutable identity was redelivered with different content."""


class MissingOpportunityDatabase(RuntimeError):
    """The requested opportunity database does not exist."""


class OpportunitySchemaError(RuntimeError):
    """The opportunity database schema version is unsupported."""


_VENUE_NETWORK = {
    VenueId.PUMPFUN_SOLANA: NetworkId.SOLANA,
    VenueId.BAGS_SOLANA: NetworkId.SOLANA,
    VenueId.BAGS_ROBINHOOD_4663: NetworkId.ROBINHOOD_4663,
    VenueId.PONS_V1_ROBINHOOD_4663: NetworkId.ROBINHOOD_4663,
}


@dataclass(frozen=True)
class LaunchIdentity:
    launch_id: str
    network_id: NetworkId
    venue_id: VenueId
    token_id: str
    creator_id: str | None
    launch_event_id: str
    launched_at: str

    def __post_init__(self) -> None:
        network = NetworkId(self.network_id)
        venue = VenueId(self.venue_id)
        if _VENUE_NETWORK[venue] != network:
            raise ValueError("venue_id is incompatible with network_id")
        object.__setattr__(self, "network_id", network)
        object.__setattr__(self, "venue_id", venue)
        for field_name in ("launch_id", "launch_event_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
            object.__setattr__(self, field_name, value.strip())
        object.__setattr__(self, "token_id", normalize_identity(network.value, self.token_id))
        if self.creator_id is not None:
            object.__setattr__(
                self,
                "creator_id",
                normalize_identity(network.value, self.creator_id),
            )
        object.__setattr__(self, "launched_at", require_utc_iso(self.launched_at))


SCHEMA = """
CREATE TABLE IF NOT EXISTS op_schema_version (
    version INTEGER NOT NULL
);
INSERT INTO op_schema_version(version)
SELECT 12 WHERE NOT EXISTS (SELECT 1 FROM op_schema_version);

CREATE TABLE IF NOT EXISTS op_raw_events (
    raw_event_id TEXT PRIMARY KEY,
    network_id TEXT NOT NULL,
    venue_id TEXT NOT NULL,
    source_key TEXT NOT NULL,
    source_provider TEXT NOT NULL,
    source_cursor TEXT NOT NULL,
    token_id TEXT NOT NULL,
    transaction_id TEXT NOT NULL,
    block_or_slot INTEGER NOT NULL,
    transaction_index INTEGER,
    event_index INTEGER NOT NULL,
    block_hash TEXT,
    chain_timestamp TEXT NOT NULL,
    received_at TEXT NOT NULL,
    confirmation TEXT NOT NULL,
    raw_payload_json TEXT NOT NULL,
    raw_payload_hash TEXT NOT NULL,
    row_fingerprint TEXT NOT NULL,
    inserted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS op_canonical_events (
    canonical_event_id TEXT PRIMARY KEY,
    raw_event_id TEXT NOT NULL REFERENCES op_raw_events(raw_event_id),
    launch_id TEXT NOT NULL,
    network_id TEXT NOT NULL,
    venue_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    lifecycle TEXT NOT NULL,
    event_kind TEXT NOT NULL,
    transaction_id TEXT NOT NULL,
    block_or_slot INTEGER NOT NULL,
    transaction_index INTEGER,
    event_index INTEGER NOT NULL,
    block_hash TEXT,
    chain_timestamp TEXT NOT NULL,
    received_at TEXT NOT NULL,
    decoded_at TEXT NOT NULL,
    state_applied_at TEXT NOT NULL,
    confirmation TEXT NOT NULL,
    actor_id TEXT,
    quote_delta_atomic TEXT,
    token_delta_atomic TEXT,
    progress_bps INTEGER,
    creator_linked INTEGER,
    decoder_version TEXT NOT NULL,
    correction_of TEXT REFERENCES op_canonical_events(canonical_event_id),
    row_fingerprint TEXT NOT NULL,
    inserted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS op_launches (
    launch_id TEXT PRIMARY KEY,
    network_id TEXT NOT NULL,
    venue_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    creator_id TEXT,
    launch_event_id TEXT NOT NULL REFERENCES op_canonical_events(canonical_event_id),
    launched_at TEXT NOT NULL,
    row_fingerprint TEXT NOT NULL,
    inserted_at TEXT NOT NULL,
    UNIQUE(network_id, venue_id, token_id)
);

-- Creator track record, deliberately OUTSIDE the retention delete set. Launches and
-- outcomes are pruned within minutes; a prior built from them would be worthless if it
-- were pruned with them. Chain-scoped: a creator identity does not cross networks.
CREATE TABLE IF NOT EXISTS op_creator_outcomes (
    network_id TEXT NOT NULL,
    venue_id TEXT NOT NULL,
    creator_id TEXT NOT NULL,
    scored_outcomes INTEGER NOT NULL,
    sum_return_bps INTEGER NOT NULL,
    last_outcome_at TEXT NOT NULL,
    PRIMARY KEY(network_id, venue_id, creator_id)
);


CREATE TABLE IF NOT EXISTS op_source_frontiers (
    source_key TEXT PRIMARY KEY,
    durable_cursor TEXT,
    resume_cursor TEXT,
    latest_observed_cursor TEXT,
    has_open_gap INTEGER NOT NULL CHECK (has_open_gap IN (0,1)),
    version INTEGER NOT NULL CHECK (version > 0),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS op_data_gaps (
    gap_id TEXT PRIMARY KEY,
    source_key TEXT NOT NULL REFERENCES op_source_frontiers(source_key),
    gap_kind TEXT NOT NULL,
    previous_durable_cursor TEXT,
    resume_cursor TEXT,
    newest_observed_cursor TEXT,
    detail TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    resolved_at TEXT,
    row_fingerprint TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS op_landmark_states (
    landmark_id TEXT PRIMARY KEY,
    launch_id TEXT NOT NULL REFERENCES op_launches(launch_id),
    observed_event_id TEXT NOT NULL REFERENCES op_canonical_events(canonical_event_id),
    observed_at TEXT NOT NULL,
    age_ms INTEGER NOT NULL CHECK (age_ms >= 0),
    progress_bps INTEGER NOT NULL CHECK (progress_bps BETWEEN 0 AND 10000),
    crossed_time_ms_json TEXT NOT NULL,
    crossed_progress_bps_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('tentative','canonical','orphaned','ineligible')),
    state_fingerprint TEXT NOT NULL,
    supersedes_landmark_id TEXT REFERENCES op_landmark_states(landmark_id),
    ineligible_reason TEXT,
    row_fingerprint TEXT NOT NULL,
    inserted_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS op_landmark_observed_state
ON op_landmark_states(launch_id, observed_event_id, status, state_fingerprint);

CREATE TABLE IF NOT EXISTS op_counterfactual_entries (
    entry_id TEXT PRIMARY KEY,
    landmark_id TEXT NOT NULL REFERENCES op_landmark_states(landmark_id),
    clip_usd_atomic TEXT NOT NULL,
    entry_latency_ms INTEGER NOT NULL CHECK (entry_latency_ms >= 0),
    entry_observed_at TEXT NOT NULL,
    entry_effective_at TEXT NOT NULL,
    acquired_token_atomic TEXT NOT NULL,
    entry_cost_usd_atomic TEXT NOT NULL,
    entry_price_impact_bps INTEGER NOT NULL,
    quote_provider TEXT NOT NULL,
    quote_version TEXT NOT NULL,
    row_fingerprint TEXT NOT NULL,
    inserted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS op_competing_outcomes (
    outcome_id TEXT PRIMARY KEY,
    entry_id TEXT NOT NULL REFERENCES op_counterfactual_entries(entry_id),
    outcome_kind TEXT NOT NULL,
    elapsed_ms INTEGER NOT NULL CHECK (elapsed_ms >= 0),
    terminal_observation_id TEXT NOT NULL,
    terminal_net_return_bps INTEGER,
    profit_barrier_bps INTEGER NOT NULL,
    loss_barrier_bps INTEGER NOT NULL,
    horizon_ms INTEGER NOT NULL CHECK (horizon_ms > 0),
    last_executable_observation_id TEXT,
    last_executable_return_bps INTEGER,
    evaluator_version TEXT NOT NULL,
    row_fingerprint TEXT NOT NULL,
    inserted_at TEXT NOT NULL,
    UNIQUE(entry_id, profit_barrier_bps, loss_barrier_bps, horizon_ms)
);

CREATE TABLE IF NOT EXISTS op_executable_observations (
    observation_id TEXT PRIMARY KEY,
    entry_id TEXT NOT NULL REFERENCES op_counterfactual_entries(entry_id),
    elapsed_ms INTEGER NOT NULL CHECK (elapsed_ms >= 0),
    canonical_state_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    exit_kind TEXT NOT NULL CHECK (exit_kind IN ('executable','deterministic_failure','transient_failure')),
    net_return_bps INTEGER,
    net_proceeds_usd_atomic TEXT,
    source_healthy INTEGER NOT NULL CHECK (source_healthy IN (0,1)),
    migration INTEGER NOT NULL CHECK (migration IN (0,1)),
    orphaned INTEGER NOT NULL CHECK (orphaned IN (0,1)),
    provider TEXT NOT NULL,
    blocker_code TEXT,
    latency_ms INTEGER NOT NULL CHECK (latency_ms >= 0),
    row_fingerprint TEXT NOT NULL,
    inserted_at TEXT NOT NULL,
    UNIQUE(entry_id, canonical_state_id)
);

CREATE TABLE IF NOT EXISTS op_venue_event_facts (
    canonical_event_id TEXT PRIMARY KEY REFERENCES op_canonical_events(canonical_event_id),
    decoder_version TEXT NOT NULL,
    facts_json TEXT NOT NULL,
    row_fingerprint TEXT NOT NULL,
    inserted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS op_state_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    launch_id TEXT NOT NULL REFERENCES op_launches(launch_id),
    observed_event_id TEXT NOT NULL REFERENCES op_canonical_events(canonical_event_id),
    observed_at TEXT NOT NULL,
    state_json TEXT NOT NULL,
    blocker_codes_json TEXT NOT NULL,
    state_version TEXT NOT NULL,
    row_fingerprint TEXT NOT NULL,
    inserted_at TEXT NOT NULL,
    UNIQUE(launch_id, observed_event_id, state_version)
);

CREATE TABLE IF NOT EXISTS op_entity_edges (
    edge_id TEXT PRIMARY KEY,
    launch_id TEXT NOT NULL REFERENCES op_launches(launch_id),
    left_identity TEXT NOT NULL,
    right_identity TEXT NOT NULL,
    evidence_kind TEXT NOT NULL,
    evidence_strength TEXT NOT NULL CHECK (evidence_strength IN ('exact','strong','weak')),
    observed_at TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    row_fingerprint TEXT NOT NULL,
    inserted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS op_quote_observations (
    quote_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES op_state_snapshots(snapshot_id),
    clip_usd_micros INTEGER NOT NULL CHECK (clip_usd_micros > 0),
    quote_in_atomic TEXT,
    tokens_out_atomic TEXT,
    immediate_exit_quote_atomic TEXT,
    round_trip_return_bps INTEGER,
    entry_price_impact_bps INTEGER,
    status TEXT NOT NULL,
    blocker_code TEXT,
    provider TEXT NOT NULL,
    is_remote INTEGER NOT NULL CHECK (is_remote IN (0,1)),
    latency_ms INTEGER CHECK (latency_ms IS NULL OR latency_ms >= 0),
    quote_version TEXT NOT NULL,
    market_input_as_of TEXT,
    network_cost_quote_atomic TEXT,
    observed_at TEXT NOT NULL,
    row_fingerprint TEXT NOT NULL,
    inserted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS op_predictions (
    prediction_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES op_state_snapshots(snapshot_id),
    zone TEXT NOT NULL CHECK (zone IN ('OUTER','TRACKING','APPROACHING','INNER','CENTER')),
    would_fire INTEGER NOT NULL CHECK (would_fire IN (0,1)),
    reason_codes_json TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    truth_label TEXT NOT NULL,
    distance_bps INTEGER NOT NULL CHECK (distance_bps BETWEEN 0 AND 10000),
    dominant_risk TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    row_fingerprint TEXT NOT NULL,
    inserted_at TEXT NOT NULL,
    UNIQUE(snapshot_id, rule_version)
);

CREATE TABLE IF NOT EXISTS op_forward_alerts (
    alert_id TEXT PRIMARY KEY,
    launch_id TEXT NOT NULL,
    network_id TEXT NOT NULL,
    venue_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    clip_usd_micros INTEGER NOT NULL CHECK(clip_usd_micros > 0),
    rule_version TEXT NOT NULL,
    quote_provider TEXT NOT NULL,
    round_trip_return_bps INTEGER,
    truth_label TEXT NOT NULL,
    row_fingerprint TEXT NOT NULL,
    inserted_at TEXT NOT NULL,
    UNIQUE(launch_id, rule_version, clip_usd_micros)
);

CREATE TABLE IF NOT EXISTS op_forward_alert_entries (
    alert_id TEXT PRIMARY KEY REFERENCES op_forward_alerts(alert_id),
    entry_id TEXT NOT NULL UNIQUE REFERENCES op_counterfactual_entries(entry_id),
    snapshot_id TEXT NOT NULL REFERENCES op_state_snapshots(snapshot_id),
    quote_observed_at TEXT NOT NULL,
    quote_completed_at TEXT NOT NULL,
    quote_age_ms INTEGER NOT NULL CHECK(quote_age_ms >= 0),
    entry_effective_at TEXT NOT NULL,
    provider TEXT NOT NULL,
    evaluator_version TEXT NOT NULL,
    row_fingerprint TEXT NOT NULL,
    inserted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS op_forward_alert_features (
    alert_id TEXT PRIMARY KEY REFERENCES op_forward_alerts(alert_id),
    feature_version TEXT NOT NULL,
    feature_json TEXT NOT NULL,
    row_fingerprint TEXT NOT NULL,
    inserted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS op_model_registry (
    model_id TEXT PRIMARY KEY,
    venue_id TEXT NOT NULL,
    lifecycle TEXT NOT NULL,
    model_level TEXT NOT NULL,
    status TEXT NOT NULL,
    trained_through TEXT,
    artifact_uri TEXT,
    evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    row_fingerprint TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS op_runtime_status (
    source_key TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    blocker_code TEXT,
    last_event_at TEXT,
    heartbeat_at TEXT NOT NULL,
    queue_depth INTEGER NOT NULL CHECK (queue_depth >= 0),
    dropped_events INTEGER NOT NULL CHECK (dropped_events >= 0),
    detail_json TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0)
);

CREATE INDEX IF NOT EXISTS idx_op_raw_source_cursor
ON op_raw_events(source_key, source_cursor);
CREATE INDEX IF NOT EXISTS idx_op_canonical_launch_order
ON op_canonical_events(launch_id, block_or_slot, transaction_index, event_index);
CREATE INDEX IF NOT EXISTS idx_op_canonical_token_order
ON op_canonical_events(network_id, venue_id, token_id, block_or_slot, transaction_index, event_index);
CREATE INDEX IF NOT EXISTS idx_op_canonical_correction
ON op_canonical_events(correction_of, confirmation);
CREATE INDEX IF NOT EXISTS idx_op_canonical_raw_event
ON op_canonical_events(raw_event_id);
CREATE INDEX IF NOT EXISTS idx_op_snapshots_launch_time
ON op_state_snapshots(launch_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_op_predictions_zone_time
ON op_predictions(zone, observed_at);
CREATE INDEX IF NOT EXISTS idx_op_forward_alerts_time
ON op_forward_alerts(observed_at);
CREATE INDEX IF NOT EXISTS idx_op_executable_observations_entry_time
ON op_executable_observations(entry_id, elapsed_ms);

CREATE TRIGGER IF NOT EXISTS op_raw_events_no_update
BEFORE UPDATE ON op_raw_events BEGIN SELECT RAISE(ABORT, 'op_raw_events immutable'); END;
CREATE TRIGGER IF NOT EXISTS op_raw_events_no_delete
BEFORE DELETE ON op_raw_events WHEN op_retention_delete_allowed()=0
BEGIN SELECT RAISE(ABORT, 'op_raw_events immutable'); END;
CREATE TRIGGER IF NOT EXISTS op_canonical_events_no_update
BEFORE UPDATE ON op_canonical_events BEGIN SELECT RAISE(ABORT, 'op_canonical_events immutable'); END;
CREATE TRIGGER IF NOT EXISTS op_canonical_events_no_delete
BEFORE DELETE ON op_canonical_events WHEN op_retention_delete_allowed()=0
BEGIN SELECT RAISE(ABORT, 'op_canonical_events immutable'); END;
CREATE TRIGGER IF NOT EXISTS op_launches_no_update
BEFORE UPDATE ON op_launches BEGIN SELECT RAISE(ABORT, 'op_launches immutable'); END;
CREATE TRIGGER IF NOT EXISTS op_launches_no_delete
BEFORE DELETE ON op_launches WHEN op_retention_delete_allowed()=0
BEGIN SELECT RAISE(ABORT, 'op_launches immutable'); END;
CREATE TRIGGER IF NOT EXISTS op_data_gaps_guard_update
BEFORE UPDATE ON op_data_gaps
WHEN NOT (
    OLD.resolved_at IS NULL
    AND NEW.resolved_at IS NOT NULL
    AND NEW.gap_id IS OLD.gap_id
    AND NEW.source_key IS OLD.source_key
    AND NEW.gap_kind IS OLD.gap_kind
    AND NEW.previous_durable_cursor IS OLD.previous_durable_cursor
    AND NEW.resume_cursor IS OLD.resume_cursor
    AND NEW.newest_observed_cursor IS OLD.newest_observed_cursor
    AND NEW.detail IS OLD.detail
    AND NEW.opened_at IS OLD.opened_at
    AND NEW.row_fingerprint IS OLD.row_fingerprint
)
BEGIN SELECT RAISE(ABORT, 'op_data_gaps immutable except resolution'); END;
CREATE TRIGGER IF NOT EXISTS op_data_gaps_no_delete
BEFORE DELETE ON op_data_gaps WHEN op_retention_delete_allowed()=0
BEGIN SELECT RAISE(ABORT, 'op_data_gaps immutable'); END;
CREATE TRIGGER IF NOT EXISTS op_landmark_states_no_update
BEFORE UPDATE ON op_landmark_states BEGIN SELECT RAISE(ABORT, 'op_landmark_states immutable'); END;
CREATE TRIGGER IF NOT EXISTS op_landmark_states_no_delete
BEFORE DELETE ON op_landmark_states WHEN op_retention_delete_allowed()=0
BEGIN SELECT RAISE(ABORT, 'op_landmark_states immutable'); END;
CREATE TRIGGER IF NOT EXISTS op_counterfactual_entries_no_update
BEFORE UPDATE ON op_counterfactual_entries
BEGIN SELECT RAISE(ABORT, 'op_counterfactual_entries immutable'); END;
CREATE TRIGGER IF NOT EXISTS op_counterfactual_entries_no_delete
BEFORE DELETE ON op_counterfactual_entries WHEN op_retention_delete_allowed()=0
BEGIN SELECT RAISE(ABORT, 'op_counterfactual_entries immutable'); END;
CREATE TRIGGER IF NOT EXISTS op_competing_outcomes_no_update
BEFORE UPDATE ON op_competing_outcomes
BEGIN SELECT RAISE(ABORT, 'op_competing_outcomes immutable'); END;
CREATE TRIGGER IF NOT EXISTS op_competing_outcomes_no_delete
BEFORE DELETE ON op_competing_outcomes WHEN op_retention_delete_allowed()=0
BEGIN SELECT RAISE(ABORT, 'op_competing_outcomes immutable'); END;
CREATE TRIGGER IF NOT EXISTS op_executable_observations_no_update
BEFORE UPDATE ON op_executable_observations
BEGIN SELECT RAISE(ABORT, 'op_executable_observations immutable'); END;
CREATE TRIGGER IF NOT EXISTS op_executable_observations_no_delete
BEFORE DELETE ON op_executable_observations WHEN op_retention_delete_allowed()=0
BEGIN SELECT RAISE(ABORT, 'op_executable_observations immutable'); END;
CREATE TRIGGER IF NOT EXISTS op_venue_event_facts_no_update
BEFORE UPDATE ON op_venue_event_facts BEGIN SELECT RAISE(ABORT, 'op_venue_event_facts immutable'); END;
CREATE TRIGGER IF NOT EXISTS op_venue_event_facts_no_delete
BEFORE DELETE ON op_venue_event_facts WHEN op_retention_delete_allowed()=0
BEGIN SELECT RAISE(ABORT, 'op_venue_event_facts immutable'); END;
CREATE TRIGGER IF NOT EXISTS op_state_snapshots_no_update
BEFORE UPDATE ON op_state_snapshots BEGIN SELECT RAISE(ABORT, 'op_state_snapshots immutable'); END;
CREATE TRIGGER IF NOT EXISTS op_state_snapshots_no_delete
BEFORE DELETE ON op_state_snapshots WHEN op_retention_delete_allowed()=0
BEGIN SELECT RAISE(ABORT, 'op_state_snapshots immutable'); END;
CREATE TRIGGER IF NOT EXISTS op_entity_edges_no_update
BEFORE UPDATE ON op_entity_edges BEGIN SELECT RAISE(ABORT, 'op_entity_edges immutable'); END;
CREATE TRIGGER IF NOT EXISTS op_entity_edges_no_delete
BEFORE DELETE ON op_entity_edges WHEN op_retention_delete_allowed()=0
BEGIN SELECT RAISE(ABORT, 'op_entity_edges immutable'); END;
CREATE TRIGGER IF NOT EXISTS op_quote_observations_no_update
BEFORE UPDATE ON op_quote_observations BEGIN SELECT RAISE(ABORT, 'op_quote_observations immutable'); END;
CREATE TRIGGER IF NOT EXISTS op_quote_observations_no_delete
BEFORE DELETE ON op_quote_observations WHEN op_retention_delete_allowed()=0
BEGIN SELECT RAISE(ABORT, 'op_quote_observations immutable'); END;
CREATE TRIGGER IF NOT EXISTS op_predictions_no_update
BEFORE UPDATE ON op_predictions BEGIN SELECT RAISE(ABORT, 'op_predictions immutable'); END;
CREATE TRIGGER IF NOT EXISTS op_predictions_no_delete
BEFORE DELETE ON op_predictions WHEN op_retention_delete_allowed()=0
BEGIN SELECT RAISE(ABORT, 'op_predictions immutable'); END;
CREATE TRIGGER IF NOT EXISTS op_forward_alerts_no_update
BEFORE UPDATE ON op_forward_alerts BEGIN SELECT RAISE(ABORT, 'op_forward_alerts immutable'); END;
CREATE TRIGGER IF NOT EXISTS op_forward_alerts_no_delete
BEFORE DELETE ON op_forward_alerts BEGIN SELECT RAISE(ABORT, 'op_forward_alerts immutable'); END;
CREATE TRIGGER IF NOT EXISTS op_forward_alert_entries_no_update
BEFORE UPDATE ON op_forward_alert_entries BEGIN SELECT RAISE(ABORT, 'op_forward_alert_entries immutable'); END;
CREATE TRIGGER IF NOT EXISTS op_forward_alert_features_no_update
BEFORE UPDATE ON op_forward_alert_features BEGIN SELECT RAISE(ABORT, 'op_forward_alert_features immutable'); END;
CREATE TRIGGER IF NOT EXISTS op_forward_alert_features_no_delete
BEFORE DELETE ON op_forward_alert_features WHEN op_retention_delete_allowed()=0
BEGIN SELECT RAISE(ABORT, 'op_forward_alert_features immutable'); END;
CREATE TRIGGER IF NOT EXISTS op_model_registry_no_update
BEFORE UPDATE ON op_model_registry BEGIN SELECT RAISE(ABORT, 'op_model_registry immutable'); END;
CREATE TRIGGER IF NOT EXISTS op_model_registry_no_delete
BEFORE DELETE ON op_model_registry WHEN op_retention_delete_allowed()=0
BEGIN SELECT RAISE(ABORT, 'op_model_registry immutable'); END;

"""


IN_MEMORY_DB = ":memory:"


def opportunity_db_path(path: str | Path | None = None) -> Path:
    if path is not None:
        # `:memory:` is SQLite's in-memory sentinel, not a filesystem path. Resolving
        # it produced a real file of that name in the working directory, so callers
        # asking for a private throwaway DB silently shared one on disk — fixtures
        # leaked between tests and the file outlived the process.
        if str(path) == IN_MEMORY_DB:
            return Path(IN_MEMORY_DB)
        return Path(path).expanduser().resolve()
    from sniper.util.state_paths import state_path

    return state_path(*DEFAULT_DB_PATH.split("/"))


class OpportunityConnection(sqlite3.Connection):
    retention_delete_allowed = False


_RETENTION_DELETE_TABLES = (
    "op_raw_events", "op_canonical_events", "op_launches", "op_data_gaps",
    "op_landmark_states", "op_counterfactual_entries", "op_competing_outcomes",
    "op_executable_observations", "op_venue_event_facts", "op_state_snapshots",
    "op_entity_edges", "op_quote_observations", "op_predictions", "op_model_registry",
    "op_forward_alerts", "op_forward_alert_entries", "op_forward_alert_features",
)


SCHEMA = (
    SCHEMA
    + "\n"
    + ";\n".join(
        _PAPER_ADAPTER_STATEMENTS
        + _PAPER_LIFECYCLE_STATEMENTS
        + _PAPER_V3_STATEMENTS
        + _BREADTH_LABEL_STATEMENTS
        + _EVM_SCAN_CURSOR_STATEMENTS
    )
    + ";\n"
)


def _configure(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None
    if isinstance(conn, OpportunityConnection):
        conn.create_function(
            "op_retention_delete_allowed",
            0,
            lambda: int(conn.retention_delete_allowed),
        )
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


# A READER accepts the current version or the one before it. The writer still migrates
# and still requires an exact match.
#
# Without this window a schema bump cannot be deployed at all: the runtimes share one
# database, so whichever is repointed second rejects it for the whole window. That is
# not hypothetical -- the 15->16 bump took the Radar UI dark until the web release
# caught up, because the reader demanded exact equality.
#
# Deploy order follows from it: the READER goes first, since new reader code accepts the
# old and the new version. A reader one version behind the database only misses tables
# that version added, so every reader of a new table must tolerate its absence.
READER_COMPATIBLE_SCHEMA_VERSIONS = (SCHEMA_VERSION, SCHEMA_VERSION - 1)


def _validate_schema_version(
    conn: sqlite3.Connection,
    *,
    allow_previous: bool = False,
) -> None:
    accepted = (
        READER_COMPATIBLE_SCHEMA_VERSIONS if allow_previous else (SCHEMA_VERSION,)
    )
    rows = conn.execute("SELECT version FROM op_schema_version").fetchall()
    if len(rows) != 1 or rows[0][0] not in accepted:
        versions = [row[0] for row in rows]
        raise OpportunitySchemaError(
            f"unsupported opportunity schema version rows: {versions!r} "
            f"(accepted: {list(accepted)!r})"
        )


def _migrate_schema(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT version FROM op_schema_version").fetchall()
    if len(rows) != 1:
        raise OpportunitySchemaError(f"unsupported opportunity schema version rows: {[row[0] for row in rows]!r}")
    version = rows[0][0]
    if version == SCHEMA_VERSION:
        return
    if version not in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16):
        raise OpportunitySchemaError(f"unsupported opportunity schema version rows: {[version]!r}")
    with transaction(conn):
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(op_quote_observations)").fetchall()
        }
        if "market_input_as_of" not in columns:
            conn.execute("ALTER TABLE op_quote_observations ADD COLUMN market_input_as_of TEXT")
        if "network_cost_quote_atomic" not in columns:
            conn.execute("ALTER TABLE op_quote_observations ADD COLUMN network_cost_quote_atomic TEXT")
        outcome_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(op_competing_outcomes)").fetchall()
        }
        if "evaluator_version" not in outcome_columns:
            conn.execute(
                "ALTER TABLE op_competing_outcomes ADD COLUMN evaluator_version TEXT "
                "NOT NULL DEFAULT 'opportunity-outcome-v1'"
            )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS op_forward_alert_features ("
            "alert_id TEXT PRIMARY KEY REFERENCES op_forward_alerts(alert_id),"
            "feature_version TEXT NOT NULL,feature_json TEXT NOT NULL,"
            "row_fingerprint TEXT NOT NULL,inserted_at TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TRIGGER IF NOT EXISTS op_forward_alert_features_no_update "
            "BEFORE UPDATE ON op_forward_alert_features "
            "BEGIN SELECT RAISE(ABORT, 'op_forward_alert_features immutable'); END"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS op_creator_outcomes ("
            "network_id TEXT NOT NULL,venue_id TEXT NOT NULL,creator_id TEXT NOT NULL,"
            "scored_outcomes INTEGER NOT NULL,sum_return_bps INTEGER NOT NULL,"
            "last_outcome_at TEXT NOT NULL,"
            "PRIMARY KEY(network_id, venue_id, creator_id))"
        )
        # executescript, not split(";"): trigger bodies contain their own semicolons
        # ("BEGIN SELECT RAISE(...); END;") and naive splitting truncates them.
        for statement in _PAPER_ADAPTER_STATEMENTS:
            conn.execute(statement)
        for statement in _PAPER_LIFECYCLE_STATEMENTS:
            conn.execute(statement)
        for statement in _PAPER_V3_STATEMENTS:
            conn.execute(statement)
        for statement in _BREADTH_LABEL_STATEMENTS:
            conn.execute(statement)
        for statement in _EVM_SCAN_CURSOR_STATEMENTS:
            conn.execute(statement)
        paper_outcome_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(op_paper_outcomes)").fetchall()
        }
        for column in (
            "origin_kind",
            "policy_hash",
            "decision_facts_json",
            "exit_reason_codes_json",
        ):
            if column not in paper_outcome_columns:
                conn.execute(
                    f"ALTER TABLE op_paper_outcomes ADD COLUMN {column} TEXT"
                )
        # `radar-realistic-v3` keeps the cash return in `return_bps` and the movement
        # away from the on-time first quote in its own column. One number cannot carry
        # both: the cash return holds the round-trip cost the stop must ignore.
        for column in ("baseline_return_bps", "baseline_move_bps"):
            if column not in paper_outcome_columns:
                conn.execute(
                    f"ALTER TABLE op_paper_outcomes ADD COLUMN {column} INTEGER"
                )
        _migrate_paper_atomic_columns(conn)
        conn.execute("UPDATE op_schema_version SET version=?", (SCHEMA_VERSION,))


def _migrate_paper_atomic_columns(conn: sqlite3.Connection) -> None:
    """Change exact atomic quantities from 64-bit INTEGER to decimal TEXT."""
    table_specs = (
        (
            "op_paper_fill_requests",
            _PAPER_ADAPTER_STATEMENTS[0],
            ("local_qty_atomic",),
        ),
        (
            "op_paper_fills",
            _PAPER_ADAPTER_STATEMENTS[1],
            ("quote_in_atomic", "expected_out_atomic", "floor_out_atomic"),
        ),
        (
            "op_paper_exit_observations",
            _PAPER_LIFECYCLE_STATEMENTS[1],
            (
                "quote_in_atomic",
                "quote_out_atomic",
                "expected_out_atomic",
                "floor_out_atomic",
            ),
        ),
        (
            "op_paper_outcomes",
            _PAPER_LIFECYCLE_STATEMENTS[2],
            ("entry_quote_atomic", "exit_floor_atomic"),
        ),
    )
    rebuild = []
    for table, statement, atomic_columns in table_specs:
        declared = {
            str(row[1]): str(row[2]).upper()
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        table_sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        table_sql = str(table_sql_row[0] if table_sql_row else "")
        result_codes_are_old = (
            table == "op_paper_outcomes" and "adaptive_exit" not in table_sql
        )
        if result_codes_are_old or any(
            declared.get(column) != "TEXT" for column in atomic_columns
        ):
            rebuild.append((table, statement))
    if not rebuild:
        return

    trigger_names = (
        "op_paper_fill_requests_no_update",
        "op_paper_fill_requests_no_delete",
        "op_paper_fills_no_update",
        "op_paper_fills_no_delete",
        "op_paper_exit_observations_no_update",
        "op_paper_exit_observations_no_delete",
        "op_paper_outcomes_no_update",
        "op_paper_outcomes_no_delete",
    )
    for trigger in trigger_names:
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    for table, statement in rebuild:
        legacy = f"{table}_v12_atomic_integer"
        conn.execute(f"ALTER TABLE {table} RENAME TO {legacy}")
        conn.execute(statement)
        columns = [
            str(row[1])
            for row in conn.execute(f"PRAGMA table_info({legacy})").fetchall()
        ]
        column_list = ",".join(columns)
        conn.execute(
            f"INSERT INTO {table} ({column_list}) "
            f"SELECT {column_list} FROM {legacy}"
        )
        conn.execute(f"DROP TABLE {legacy}")
    for statement in _PAPER_ADAPTER_STATEMENTS[2:]:
        conn.execute(statement)
    for statement in _PAPER_LIFECYCLE_STATEMENTS[3:]:
        conn.execute(statement)


def _install_retention_delete_triggers(conn: sqlite3.Connection) -> None:
    with transaction(conn):
        for table in _RETENTION_DELETE_TABLES:
            conn.execute(f"DROP TRIGGER IF EXISTS {table}_no_delete")
            conn.execute(
                f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} "
                "WHEN op_retention_delete_allowed()=0 "
                f"BEGIN SELECT RAISE(ABORT, '{table} immutable'); END"
            )


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    resolved = opportunity_db_path(path)
    if str(resolved) != IN_MEMORY_DB:
        resolved.parent.mkdir(parents=True, exist_ok=True)
    conn = _configure(sqlite3.connect(resolved, timeout=30, factory=OpportunityConnection))
    try:
        conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
        conn.execute("PRAGMA journal_mode=WAL")
        # Trim the WAL back after a checkpoint resets it (see db_core.get_connection). Without
        # this the file parks at its all-time peak: capture bursts left a 64.9MB WAL on a 6.6MB
        # database, which retention then counted against the hot-storage cap.
        conn.execute("PRAGMA journal_size_limit=16777216")
        # Research capture is replayable from chain-native sources. NORMAL WAL
        # durability avoids two full fsyncs per append-only confirmation event,
        # which otherwise makes finality reconciliation fall behind forever.
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA wal_autocheckpoint=1000")
        conn.executescript(f"BEGIN IMMEDIATE;\n{SCHEMA}\nCOMMIT;")
        _migrate_schema(conn)
        _install_retention_delete_triggers(conn)
        _validate_schema_version(conn)
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()
        raise
    return conn


def connect_existing(path: str | Path | None = None) -> sqlite3.Connection:
    resolved = opportunity_db_path(path)
    if not resolved.exists():
        raise MissingOpportunityDatabase(f"opportunity database does not exist: {resolved}")
    conn = _configure(
        sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True, timeout=30)
    )
    try:
        _validate_schema_version(conn, allow_previous=True)
    except BaseException:
        conn.close()
        raise
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    # Append helpers are individually atomic, but hot reconciliation needs to
    # group hundreds of immutable corrections into one durable commit. An
    # existing owner transaction remains responsible for commit/rollback.
    if getattr(conn, "in_transaction", False):
        yield conn
        return
    if not hasattr(conn, "in_transaction"):  # lightweight unit-test seams
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


@contextmanager
def retention_delete_authority(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Temporarily authorize trigger-guarded retention deletes on this writer only."""
    if not isinstance(conn, OpportunityConnection):
        raise TypeError("retention authority requires the writable opportunity connection")
    previous = conn.retention_delete_allowed
    conn.retention_delete_allowed = True
    try:
        yield conn
    finally:
        conn.retention_delete_allowed = previous


def _now_iso() -> str:
    from sniper.util.clock import real_utc_now_iso

    return require_utc_iso(real_utc_now_iso())


def _insert_immutable(
    conn: sqlite3.Connection,
    *,
    table: str,
    identity_column: str,
    identity_value: str,
    values: Mapping[str, object],
) -> bool:
    content = dict(values)
    fingerprint = content_fingerprint(content)
    stored = {**content, "row_fingerprint": fingerprint, "inserted_at": _now_iso()}
    columns = tuple(stored)
    placeholders = ",".join("?" for _ in columns)
    cursor = conn.execute(
        f"INSERT OR IGNORE INTO {table} ({','.join(columns)}) VALUES ({placeholders})",
        tuple(stored[column] for column in columns),
    )
    if cursor.rowcount == 1:
        return True
    row = conn.execute(
        f"SELECT row_fingerprint FROM {table} WHERE {identity_column}=?",
        (identity_value,),
    ).fetchone()
    if row is not None and row[0] == fingerprint:
        return False
    raise ImmutableConflict(
        f"{table}.{identity_column}={identity_value!r} conflicts with immutable content"
    )


def append_forward_alert_features(
    conn: sqlite3.Connection,
    alert_id: str,
    features: Mapping[str, object],
    feature_version: str,
) -> bool:
    """Persist one compact point-in-time feature contract with an alert."""
    payload = canonical_json(dict(features))
    if len(payload.encode("utf-8")) > 4_096:
        raise ValueError("forward alert feature_json exceeds 4096 bytes")
    alert = conn.execute(
        "SELECT 1 FROM op_forward_alerts WHERE alert_id=?",
        (str(alert_id),),
    ).fetchone()
    if alert is None:
        raise ValueError("forward alert identity is unavailable")
    values = {
        "alert_id": str(alert_id),
        "feature_version": str(feature_version),
        "feature_json": payload,
    }
    with transaction(conn):
        return _insert_immutable(
            conn,
            table="op_forward_alert_features",
            identity_column="alert_id",
            identity_value=str(alert_id),
            values=values,
        )


def append_raw_event(conn: sqlite3.Connection, event: RawEvent) -> bool:
    values = {
        "raw_event_id": event.raw_event_id,
        "network_id": event.network_id.value,
        "venue_id": event.venue_id.value,
        "source_key": event.source_key,
        "source_provider": event.source_provider,
        "source_cursor": event.source_cursor,
        "token_id": event.token_id,
        "transaction_id": event.transaction_id,
        "block_or_slot": event.chain_order.block_or_slot,
        "transaction_index": event.chain_order.transaction_index,
        "event_index": event.chain_order.event_index,
        "block_hash": event.chain_order.block_hash,
        "chain_timestamp": event.chain_timestamp,
        "received_at": event.received_at,
        "confirmation": event.confirmation.value,
        "raw_payload_json": event.raw_payload_json,
        "raw_payload_hash": event.raw_payload_hash,
    }
    with transaction(conn):
        return _insert_immutable(
            conn,
            table="op_raw_events",
            identity_column="raw_event_id",
            identity_value=event.raw_event_id,
            values=values,
        )


def append_canonical_event(conn: sqlite3.Connection, event: CanonicalEvent) -> bool:
    values = {
        "canonical_event_id": event.canonical_event_id,
        "raw_event_id": event.raw_event_id,
        "launch_id": event.launch_id,
        "network_id": event.network_id.value,
        "venue_id": event.venue_id.value,
        "token_id": event.token_id,
        "lifecycle": event.lifecycle.value,
        "event_kind": event.event_kind.value,
        "transaction_id": event.transaction_id,
        "block_or_slot": event.chain_order.block_or_slot,
        "transaction_index": event.chain_order.transaction_index,
        "event_index": event.chain_order.event_index,
        "block_hash": event.chain_order.block_hash,
        "chain_timestamp": event.chain_timestamp,
        "received_at": event.received_at,
        "decoded_at": event.decoded_at,
        "state_applied_at": event.state_applied_at,
        "confirmation": event.confirmation.value,
        "actor_id": event.actor_id,
        "quote_delta_atomic": None if event.quote_delta_atomic is None else str(event.quote_delta_atomic),
        "token_delta_atomic": None if event.token_delta_atomic is None else str(event.token_delta_atomic),
        "progress_bps": event.progress_bps,
        "creator_linked": None if event.creator_linked is None else int(event.creator_linked),
        "decoder_version": event.decoder_version,
        "correction_of": event.correction_of,
    }
    with transaction(conn):
        if conn.execute(
            "SELECT 1 FROM op_raw_events WHERE raw_event_id=?", (event.raw_event_id,)
        ).fetchone() is None:
            raise ValueError(f"raw_event_id does not exist: {event.raw_event_id}")
        return _insert_immutable(
            conn,
            table="op_canonical_events",
            identity_column="canonical_event_id",
            identity_value=event.canonical_event_id,
            values=values,
        )


def upsert_launch_identity(conn: sqlite3.Connection, launch: LaunchIdentity) -> bool:
    values = {
        "launch_id": launch.launch_id,
        "network_id": launch.network_id.value,
        "venue_id": launch.venue_id.value,
        "token_id": launch.token_id,
        "creator_id": launch.creator_id,
        "launch_event_id": launch.launch_event_id,
        "launched_at": launch.launched_at,
    }
    with transaction(conn):
        event = conn.execute(
            "SELECT launch_id, network_id, venue_id, token_id, event_kind "
            "FROM op_canonical_events WHERE canonical_event_id=?",
            (launch.launch_event_id,),
        ).fetchone()
        if event is None:
            raise ValueError(f"launch_event_id does not exist: {launch.launch_event_id}")
        if (
            event["launch_id"] != launch.launch_id
            or event["network_id"] != launch.network_id.value
            or event["venue_id"] != launch.venue_id.value
            or event["token_id"] != launch.token_id
            or event["event_kind"] != "launch"
        ):
            raise ValueError("launch_event_id does not match launch identity")
        return _insert_immutable(
            conn,
            table="op_launches",
            identity_column="launch_id",
            identity_value=launch.launch_id,
            values=values,
        )


# Outcomes that carry no information about the creator: a censored path and a reorg say
# something about our capture, not about them.
_UNSCORED_OUTCOME_KINDS = frozenset({"censored_data_gap", "chain_reorg"})


def _has_evm_scan_cursor_table(conn: sqlite3.Connection) -> bool:
    """A store older than schema 17, or a synthetic connection, has no cursor table.

    Tested for rather than caught: swallowing OperationalError here would also hide a
    genuine read fault on a migrated store.
    """
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='op_evm_scan_cursors'"
    ).fetchone() is not None


def load_evm_scan_cursor(conn: sqlite3.Connection, chain_id: str) -> int | None:
    """The block this chain was enumerated THROUGH, or None before the first seed."""
    if not _has_evm_scan_cursor_table(conn):
        return None
    row = conn.execute(
        "SELECT scanned_through_block FROM op_evm_scan_cursors WHERE chain_id=?",
        (str(chain_id),),
    ).fetchone()
    return None if row is None else int(row[0])


def advance_evm_scan_cursor(
    conn: sqlite3.Connection,
    *,
    chain_id: str,
    through_block: int,
    now_iso: str,
    seeded_from: str = "range_complete",
) -> int:
    """Record that the chain is enumerated through `through_block`.

    The caller must have committed every accepted event in the range FIRST. This runs in
    its own short transaction afterwards, so a crash between the two replays the range
    rather than skipping it; duplicate events are rejected by their own identity.

    An EMPTY range advances the cursor too: enumerating a range and finding nothing is
    still proof that the range was covered.

    Monotonic. A lower block is ignored rather than written, and the table's trigger
    refuses a rewind even if a caller tries.
    """
    block = int(through_block)
    if block < 0:
        raise ValueError("through_block must not be negative")
    if not _has_evm_scan_cursor_table(conn):
        return block
    current = load_evm_scan_cursor(conn, chain_id)
    if current is not None and block <= current:
        return current
    with transaction(conn):
        conn.execute(
            "INSERT INTO op_evm_scan_cursors(chain_id,scanned_through_block,seeded_from,updated_at) "
            "VALUES(?,?,?,?) ON CONFLICT(chain_id) DO UPDATE SET "
            "scanned_through_block=excluded.scanned_through_block,updated_at=excluded.updated_at",
            (str(chain_id), block, str(seeded_from), require_utc_iso(now_iso)),
        )
    return block


def network_for_venue(venue_id) -> str:
    """The chain a venue lives on. Creator identity never crosses chains."""
    return _VENUE_NETWORK[VenueId(venue_id)].value


def credit_creator_outcome(
    conn: sqlite3.Connection,
    *,
    network_id: str,
    venue_id: str,
    creator_id: str | None,
    outcome_kind: str,
    terminal_net_return_bps: int | None,
    observed_at: str,
) -> bool:
    """Fold one finalized outcome into its creator's running record.

    Causality is structural: an outcome can only finalize after the decision that
    created it, so a launch can never contribute to its own prior.
    """
    if not creator_id or terminal_net_return_bps is None:
        return False
    if str(outcome_kind).lower() in _UNSCORED_OUTCOME_KINDS:
        return False
    conn.execute(
        "INSERT INTO op_creator_outcomes("
        "network_id,venue_id,creator_id,scored_outcomes,sum_return_bps,last_outcome_at) "
        "VALUES(?,?,?,1,?,?) "
        "ON CONFLICT(network_id,venue_id,creator_id) DO UPDATE SET "
        "scored_outcomes=scored_outcomes+1,"
        "sum_return_bps=sum_return_bps+excluded.sum_return_bps,"
        "last_outcome_at=MAX(last_outcome_at,excluded.last_outcome_at)",
        (
            str(network_id), str(venue_id), str(creator_id),
            int(terminal_net_return_bps), require_utc_iso(observed_at),
        ),
    )
    return True


def read_creator_prior(
    conn: sqlite3.Connection,
    *,
    network_id: str,
    venue_id: str,
    creator_id: str | None,
) -> dict[str, object]:
    """This creator's record BEFORE the launch being decided. Never their current one."""
    if not creator_id:
        return {"samples": 0, "mean_return_bps": None}
    row = conn.execute(
        "SELECT scored_outcomes,sum_return_bps FROM op_creator_outcomes "
        "WHERE network_id=? AND venue_id=? AND creator_id=?",
        (str(network_id), str(venue_id), str(creator_id)),
    ).fetchone()
    if row is None or not row[0]:
        return {"samples": 0, "mean_return_bps": None}
    return {
        "samples": int(row[0]),
        "mean_return_bps": int(row[1]) // int(row[0]),
    }
