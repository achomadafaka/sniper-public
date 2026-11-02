"""SQLite schema for the wallet dossier.

Current slice (`v5_behavioral_descriptors`) has eighteen tables:
  positions, position_exit_episodes, result_semantics,
  mechanism_evidence, market_semantics, diagnostic_accounting,
  hypothesis_results, surfaces, crosstabs, thesis_candidates,
  data_lineage, data_gaps, dossier_meta,
  descriptor_hour_distribution, descriptor_cadence,
  descriptor_sizing, descriptor_time_in_position,
  descriptor_fills_per_position.

Spec sections covered: §6.1-§6.11 + v4 thesis lifecycle + v5 behavioral descriptors.
v4 adds `thesis_candidates` for durable tracking of promoted
mechanism_evidence rows. Spec: docs/superpowers/specs/2026-05-27-
wallet-dossier-thesis-lifecycle-v4.md.
v5 adds 5 descriptor tables derived from activity_rows + closed_positions.
Spec: docs/superpowers/specs/2026-05-27-wallet-dossier-descriptor-surfaces-v5.md.
"""

from __future__ import annotations

DOSSIER_SCHEMA_VERSION = "v5_behavioral_descriptors"

# Columns deferred to later spec checkpoints (post-v0). Currently empty:
# v0_complete populates `position_pattern` (read from position_building
# surface) and `data_quality_flags` (aggregated from position_building +
# exit_attribution). Kept as a hook for future per-column deferral.
_DEFERRED_POSITIONS_COLUMNS: frozenset[str] = frozenset()


SCHEMA_DDL = [
    # ---- 6.1 positions ----
    """
    CREATE TABLE IF NOT EXISTS positions (
      position_id                    TEXT PRIMARY KEY,
      wallet                         TEXT NOT NULL,
      condition_id                   TEXT NOT NULL,
      asset_id                       TEXT NOT NULL,
      outcome_index                  INTEGER,
      outcome_label                  TEXT,
      title                          TEXT,
      entry_price                    REAL,
      size                           REAL,
      cost_basis_usdc                REAL,
      realized_pnl                   REAL,
      unrealized_pnl                 REAL,
      entry_ts                       INTEGER,
      exit_ts                        INTEGER,
      exit_mode                      TEXT,
      n_exit_episodes                INTEGER,
      dominant_counterfactual_class  TEXT,
      market_type                    TEXT,
      entry_price_bucket             TEXT,
      outcome_role                   TEXT,
      outcome_role_rank              INTEGER,
      outcome_role_rank_source       TEXT,
      position_pattern               TEXT,
      selected_token_resolution      TEXT,
      resolution_source              TEXT,
      resolution_confidence          TEXT,
      data_quality_flags             TEXT  -- JSON array
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_positions_condition ON positions(condition_id)",
    "CREATE INDEX IF NOT EXISTS idx_positions_wallet ON positions(wallet)",
    # ---- 6.2 position_exit_episodes ----
    """
    CREATE TABLE IF NOT EXISTS position_exit_episodes (
      episode_id                                  TEXT PRIMARY KEY,
      position_id                                 TEXT NOT NULL REFERENCES positions(position_id),
      episode_index                               INTEGER NOT NULL,
      episode_type                                TEXT NOT NULL,    -- partial_sell|full_sell|held_to_resolution|redemption
      timestamp                                   INTEGER NOT NULL,
      size_exited                                 REAL NOT NULL,
      price_exit                                  REAL NOT NULL,
      price_entry_for_episode                     REAL NOT NULL,
      episode_pnl                                 REAL NOT NULL,
      trade_result                                TEXT NOT NULL,    -- win|loss|flat
      selected_token_resolution_at_episode_close  TEXT NOT NULL,
      counterfactual_class                        TEXT NOT NULL,    -- one of the 10 classes (§6.3)
      counterfactual_source                       TEXT NOT NULL,
      counterfactual_confidence                   TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_episodes_position ON position_exit_episodes(position_id)",
    "CREATE INDEX IF NOT EXISTS idx_episodes_class ON position_exit_episodes(counterfactual_class)",
    # ---- 6.5 result_semantics ----
    """
    CREATE TABLE IF NOT EXISTS result_semantics (
      grain_id                       TEXT PRIMARY KEY,
      grain_key                      TEXT NOT NULL,  -- JSON object
      n_positions                    INTEGER NOT NULL,
      n_episodes                     INTEGER NOT NULL,
      episodes_by_class              TEXT NOT NULL,  -- JSON dict
      outcome_win_rate_held          REAL,
      trade_win_rate_preres          REAL,
      sold_winners_early_share       REAL,
      sold_winner_at_loss_share      REAL,
      loss_cut_correctness_rate      REAL,
      pnl_total                      REAL NOT NULL,
      pnl_concentration_top5_share   REAL,
      total_size_usdc                REAL NOT NULL,
      attribution_coverage           REAL NOT NULL,
      grain_quality                  TEXT NOT NULL,
      grain_quality_reasons          TEXT NOT NULL   -- JSON array
    )
    """,
    # ---- 6.6 mechanism_evidence ----
    """
    CREATE TABLE IF NOT EXISTS mechanism_evidence (
      grain_id                         TEXT NOT NULL REFERENCES result_semantics(grain_id),
      mechanism                        TEXT NOT NULL,
      evidence_strength                TEXT NOT NULL,
      supporting_metrics               TEXT NOT NULL,  -- JSON dict
      sample_size                      INTEGER NOT NULL,
      caveats                          TEXT NOT NULL,  -- JSON array
      missing_data                     TEXT NOT NULL,  -- JSON array
      candidate_thesis_evidence        TEXT,
      not_promoted_reason              TEXT,
      derived_from_fixture_fetched_at  INTEGER,
      PRIMARY KEY (grain_id, mechanism)
    )
    """,
    # ---- 6.4 market_semantics ----
    """
    CREATE TABLE IF NOT EXISTS market_semantics (
      position_id                   TEXT PRIMARY KEY REFERENCES positions(position_id),
      market_type                   TEXT NOT NULL,
      market_type_source            TEXT NOT NULL,
      n_outcomes_in_market          INTEGER,
      category                      TEXT NOT NULL,  -- literal "data_unavailable" in v0; nav category in v1
      category_source               TEXT NOT NULL,
      category_unavailable_reason   TEXT,
      leaf_tags_json                TEXT,           -- JSON array; post-skip, post-ambiguous, alphabetical (v1)
      event_family                  TEXT NOT NULL,  -- literal "data_unavailable" in v0
      event_family_source           TEXT NOT NULL,
      outcome_role                  TEXT NOT NULL,
      favorite_status               TEXT NOT NULL,  -- literal "unknown" in v0
      favorite_status_source        TEXT NOT NULL,
      entry_price_bucket            TEXT NOT NULL
    )
    """,
    # ---- 6.4b diagnostic_accounting ----
    # Physically separate from market_semantics so YES/NO/OTHER cannot
    # leak into any mechanism_evidence grain join (§9 hard rule).
    """
    CREATE TABLE IF NOT EXISTS diagnostic_accounting (
      position_id        TEXT PRIMARY KEY REFERENCES positions(position_id),
      accounting_bucket  TEXT NOT NULL,        -- YES / NO / OTHER
      outcome_label_raw  TEXT,
      classifier_version TEXT NOT NULL
    )
    """,
    # ---- 6.10 data_lineage ----
    # One row per semantic claim. Every non-key non-null cell in every
    # table must trace to at least one row here (enforced by tests).
    """
    CREATE TABLE IF NOT EXISTS data_lineage (
      claim_id            TEXT PRIMARY KEY,
      "table"             TEXT NOT NULL,
      "column"            TEXT NOT NULL,
      row_key             TEXT NOT NULL,
      source              TEXT NOT NULL,   -- fixture_field | derived | protocol_report_surface | data_unavailable
      source_detail       TEXT,
      confidence          TEXT NOT NULL,   -- high | medium | low
      unavailable_reason  TEXT,
      derivation_rule     TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_lineage_table_col ON data_lineage(\"table\", \"column\")",
    # ---- 6.11 data_gaps ----
    """
    CREATE TABLE IF NOT EXISTS data_gaps (
      gap_name              TEXT PRIMARY KEY,
      affected_columns      TEXT NOT NULL,  -- JSON array
      affected_grain_count  INTEGER,
      unblocking_spec       TEXT,
      severity              TEXT NOT NULL   -- high | medium | low
    )
    """,
    # ---- 6.7 hypothesis_results ----
    # Flat mirror of ProtocolReport.held_analyst_branch.hypotheses +
    # flipper_branch.hypotheses. No mutation, no derivation — direct copy.
    """
    CREATE TABLE IF NOT EXISTS hypothesis_results (
      branch        TEXT NOT NULL,   -- "held_analyst" | "flipper"
      name          TEXT NOT NULL,   -- H1..H6 / FH1..FH6
      verdict       TEXT NOT NULL,
      n             INTEGER NOT NULL,
      p_value       REAL,            -- BH-adjusted per branch
      effect_size   REAL,            -- best-effort from HypothesisResult.details; NULL if absent
      note          TEXT,            -- "; ".join(HypothesisResult.notes)
      PRIMARY KEY (branch, name)
    )
    """,
    # ---- 6.8 surfaces ----
    # Read-only mirror of each *_surface dict on ProtocolReport. One row
    # per surface_name; key_metrics holds the surface's typed body minus
    # data_state and caveats.
    """
    CREATE TABLE IF NOT EXISTS surfaces (
      surface_name  TEXT PRIMARY KEY,
      data_state    TEXT,
      key_metrics   TEXT,   -- JSON object
      caveats       TEXT    -- JSON array
    )
    """,
    # ---- 6.9 crosstabs ----
    # Pre-computed grain aggregates. Each row carries a structured
    # grain_key JSON. `crosstab_name` identifies which of the four
    # standard v0 crosstabs the row belongs to. Per spec §6.9: emitted
    # by SQL view materialization, not Python loops — we materialize
    # via aggregate queries against positions / position_exit_episodes
    # / diagnostic_accounting during the build.
    """
    CREATE TABLE IF NOT EXISTS crosstabs (
      crosstab_id            TEXT PRIMARY KEY,
      crosstab_name          TEXT NOT NULL,   -- e.g. "market_type_x_entry_bucket_x_exit_mode"
      grain_key              TEXT NOT NULL,   -- JSON object naming the dimensions
      n                      INTEGER NOT NULL,
      pnl                    REAL NOT NULL,
      outcome_win_rate_held  REAL,
      trade_win_rate_preres  REAL,
      is_diagnostic_only     INTEGER NOT NULL DEFAULT 0   -- 1 for the accounting-bucket crosstab
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_crosstabs_name ON crosstabs(crosstab_name)",
    # ---- v4 thesis_candidates ----
    # Durable lifecycle table for promoted mechanism_evidence rows.
    # Emitted ONLY when mechanism_evidence.candidate_thesis_evidence IS NOT NULL.
    # Status='candidate' in v4; later slices add paper_validated/live_validated.
    """
    CREATE TABLE IF NOT EXISTS thesis_candidates (
      thesis_id              TEXT PRIMARY KEY,
      grain_id               TEXT NOT NULL,
      mechanism              TEXT NOT NULL,
      status                 TEXT NOT NULL,        -- "candidate" in v4
      thesis_text            TEXT NOT NULL,
      supporting_metrics     TEXT NOT NULL,        -- JSON dict
      required_test_data     TEXT NOT NULL,        -- JSON array
      simulation_plan        TEXT NOT NULL,        -- JSON object (deterministic v4 stub)
      source_schema_version  TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_thesis_candidates_grain ON thesis_candidates(grain_id)",
    "CREATE INDEX IF NOT EXISTS idx_thesis_candidates_mechanism ON thesis_candidates(mechanism)",
    # ---- meta: track which schema/build produced this dossier ----
    """
    CREATE TABLE IF NOT EXISTS dossier_meta (
      key   TEXT PRIMARY KEY,
      value TEXT
    )
    """,
    # ---- v5 behavioral descriptor tables ----
    # §1 descriptor_hour_distribution: trade volume by (hour_utc, side, category)
    """
    CREATE TABLE IF NOT EXISTS descriptor_hour_distribution (
      hour_utc         INTEGER NOT NULL,
      side             TEXT NOT NULL,
      market_category  TEXT NOT NULL,
      n_trades         INTEGER NOT NULL,
      total_size_usdc  REAL NOT NULL,
      PRIMARY KEY (hour_utc, side, market_category)
    )
    """,
    # §2 descriptor_cadence: inter-trade timing by (side, category)
    """
    CREATE TABLE IF NOT EXISTS descriptor_cadence (
      side                            TEXT NOT NULL,
      market_category                 TEXT NOT NULL,
      n_intervals                     INTEGER NOT NULL,
      median_seconds_between_trades   INTEGER NOT NULL,
      p10_seconds_between_trades      INTEGER NOT NULL,
      p90_seconds_between_trades      INTEGER NOT NULL,
      burst_share                     REAL NOT NULL,
      PRIMARY KEY (side, market_category)
    )
    """,
    # §3 descriptor_sizing: fill-size distribution by (side, category)
    """
    CREATE TABLE IF NOT EXISTS descriptor_sizing (
      side                  TEXT NOT NULL,
      market_category       TEXT NOT NULL,
      n_fills               INTEGER NOT NULL,
      median_size_usdc      REAL NOT NULL,
      p10_size_usdc         REAL NOT NULL,
      p90_size_usdc         REAL NOT NULL,
      max_size_usdc         REAL NOT NULL,
      coeff_of_variation    REAL NOT NULL,
      PRIMARY KEY (side, market_category)
    )
    """,
    # §4 descriptor_time_in_position: hold duration by exit_mode
    """
    CREATE TABLE IF NOT EXISTS descriptor_time_in_position (
      exit_mode                     TEXT PRIMARY KEY,
      n_positions                   INTEGER NOT NULL,
      median_seconds_in_position    INTEGER NOT NULL,
      p10_seconds_in_position       INTEGER NOT NULL,
      p90_seconds_in_position       INTEGER NOT NULL
    )
    """,
    # §5 descriptor_fills_per_position: fills-per-position by category
    """
    CREATE TABLE IF NOT EXISTS descriptor_fills_per_position (
      market_category    TEXT PRIMARY KEY,
      n_positions        INTEGER NOT NULL,
      median_fills       INTEGER NOT NULL,
      p10_fills          INTEGER NOT NULL,
      p90_fills          INTEGER NOT NULL,
      single_fill_share  REAL NOT NULL
    )
    """,
]


def is_deferred_positions_column(column_name: str) -> bool:
    """Return True if a positions column is intentionally deferred in v0."""
    return column_name in _DEFERRED_POSITIONS_COLUMNS


def all_dossier_tables() -> list[str]:
    return [
        "positions",
        "position_exit_episodes",
        "market_semantics",
        "diagnostic_accounting",
        "result_semantics",
        "mechanism_evidence",
        "hypothesis_results",
        "surfaces",
        "crosstabs",
        "thesis_candidates",
        "data_lineage",
        "data_gaps",
        "dossier_meta",
        # v5 behavioral descriptors
        "descriptor_hour_distribution",
        "descriptor_cadence",
        "descriptor_sizing",
        "descriptor_time_in_position",
        "descriptor_fills_per_position",
    ]
