"""Compact, idempotent long-horizon evidence for the opportunity Radar."""

from __future__ import annotations

import hashlib
import json
import logging
import random
import sqlite3
import zlib
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Mapping

from sniper.util.clock import real_utc_now_iso
from .diagnostics import (
    LOSS_BARRIER_BPS,
    ExecutablePoint,
    evaluate_target_ladder,
    near_miss,
)
from .outcomes import OutcomeKind
from .venue_policy import scoreable_venues
from .paper_lifecycle import LIFECYCLE_VERSION

logger = logging.getLogger(__name__)

INSIGHT_SCHEMA_VERSION = 10

# The forward confirmation arm under judgement, named once. `_confirmation_microscope_v4`
# keeps its function name because callers and tests bind to it, but the arm it selects is
# data, not a name — publishing "v4" while selecting v5 rows is exactly the identity
# confusion the v4 measurement-integrity design exists to prevent.
FORWARD_CONFIRMATION_RULE = "pump-confirmation-truth-v6"
FORWARD_CONFIRMATION_ARM = "truth_v6"
FORWARD_CONFIRMATION_LABEL = "Confirmation v6"


def is_scored_outcome(value: object) -> bool:
    """Return whether an immutable outcome carries valid performance evidence."""
    if value is None:
        return False
    try:
        kind = OutcomeKind(str(value).lower())
    except ValueError:
        return False
    return kind not in {OutcomeKind.CENSORED_DATA_GAP, OutcomeKind.CHAIN_REORG}


FORWARD_MEASUREMENT_VERSION = "alert-outcome-v4"
# The target the operator surfaces; the persisted ladder keeps every other cell.
HEADLINE_TARGET_BPS = 1_200
HISTORICAL_PILOT_LABEL = "Withdrawn pre-v4 measurement pilots"


def is_forward_measurement(alert: Mapping[str, object]) -> bool:
    """True only for alerts scored under the current measurement regime.

    v1-v3 priced entry from a paced remote quote and had no elapsed-zero exit, so
    their returns sit on a different scale. Rule names such as `pump-ignition-v1`
    span the cutover, so the rule alone cannot separate the two regimes.
    """
    return str(alert.get("evaluator_version") or "") == FORWARD_MEASUREMENT_VERSION


def _outcome_score_status(entry_id: object, outcome_kind: object) -> str:
    if entry_id is None:
        return "UNSCORED_LEGACY"
    if outcome_kind is None:
        return "PENDING"
    if is_scored_outcome(outcome_kind):
        return "SCORED"
    return (
        "DATA_GAP_INVALID"
        if str(outcome_kind).upper() == "CENSORED_DATA_GAP"
        else "REORG_INVALID"
    )

_SCHEMA = """
CREATE TABLE IF NOT EXISTS op_insight_schema_version(version INTEGER NOT NULL);
INSERT INTO op_insight_schema_version(version)
SELECT 9 WHERE NOT EXISTS (SELECT 1 FROM op_insight_schema_version);

CREATE TABLE IF NOT EXISTS op_projected_launches(
  launch_id TEXT PRIMARY KEY,
  launch_fingerprint TEXT NOT NULL,
  projected_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS op_insight_samples(
  sample_id TEXT PRIMARY KEY,
  launch_id TEXT NOT NULL,
  network_id TEXT NOT NULL,
  venue_id TEXT NOT NULL,
  token_id TEXT NOT NULL,
  creator_id TEXT,
  sample_role TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  zone TEXT NOT NULL,
  would_fire INTEGER NOT NULL CHECK(would_fire IN (0,1)),
  rule_version TEXT NOT NULL,
  distance_bps INTEGER NOT NULL,
  dominant_risk TEXT NOT NULL,
  outcome_kind TEXT,
  terminal_net_return_bps INTEGER,
  elapsed_ms INTEGER,
  feature_blob BLOB NOT NULL,
  label_version TEXT NOT NULL,
  source_fingerprint TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(launch_id, sample_role, rule_version)
);
CREATE INDEX IF NOT EXISTS idx_op_insight_samples_time
ON op_insight_samples(observed_at);

CREATE TABLE IF NOT EXISTS op_insight_daily(
  day_utc TEXT NOT NULL,
  venue_id TEXT NOT NULL,
  rule_version TEXT NOT NULL,
  sample_role TEXT NOT NULL,
  zone TEXT NOT NULL,
  sample_count INTEGER NOT NULL,
  fire_count INTEGER NOT NULL,
  jump_count INTEGER NOT NULL,
  dump_count INTEGER NOT NULL,
  exit_failure_count INTEGER NOT NULL,
  terminal_return_bps_sum INTEGER NOT NULL,
  PRIMARY KEY(day_utc, venue_id, rule_version, sample_role, zone)
);

CREATE TABLE IF NOT EXISTS op_insight_targets(
  launch_id TEXT NOT NULL,
  venue_id TEXT NOT NULL,
  target_bps INTEGER NOT NULL,
  horizon_ms INTEGER NOT NULL,
  evaluator_version TEXT NOT NULL,
  result TEXT NOT NULL,
  best_return_bps INTEGER,
  worst_return_bps INTEGER,
  time_to_target_ms INTEGER,
  created_at TEXT NOT NULL,
  PRIMARY KEY(launch_id, target_bps, horizon_ms)
);

CREATE TABLE IF NOT EXISTS op_insight_target_daily(
  day_utc TEXT NOT NULL,
  venue_id TEXT NOT NULL,
  target_bps INTEGER NOT NULL,
  horizon_ms INTEGER NOT NULL,
  evaluator_version TEXT NOT NULL,
  result TEXT NOT NULL,
  sample_count INTEGER NOT NULL,
  PRIMARY KEY(day_utc,venue_id,target_bps,horizon_ms,evaluator_version,result)
);

CREATE TABLE IF NOT EXISTS op_insight_near_misses(
  launch_id TEXT PRIMARY KEY,
  venue_id TEXT NOT NULL,
  token_id TEXT NOT NULL,
  outcome_kind TEXT NOT NULL,
  best_return_bps INTEGER NOT NULL,
  time_to_best_ms INTEGER NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS op_insight_alerts(
  alert_id TEXT PRIMARY KEY,
  launch_id TEXT NOT NULL,
  venue_id TEXT NOT NULL,
  token_id TEXT NOT NULL,
  entry_id TEXT,
  rule_version TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  quote_observed_at TEXT,
  quote_completed_at TEXT,
  quote_age_ms INTEGER,
  entry_effective_at TEXT,
  provider TEXT,
  evaluator_version TEXT,
  quote_drag_bps INTEGER,
  score_status TEXT NOT NULL,
  outcome_kind TEXT,
  terminal_return_bps INTEGER,
  outcome_elapsed_ms INTEGER,
  mfe_bps INTEGER,
  mae_bps INTEGER,
  time_to_best_ms INTEGER,
  feature_version TEXT,
  feature_json TEXT,
  -- Denormalised from the launch. The matched-control gate clusters by creator, and
  -- creator identity used to be readable only by joining op_insight_samples, which
  -- prunes on the sample clock. Storing it here keeps the exact gate evidence whole
  -- once the samples behind it are gone.
  creator_id TEXT,
  source_fingerprint TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_op_insight_alerts_time
ON op_insight_alerts(observed_at);

-- `radar-buyer-breadth-v1` decision labels, one row per launch, projected from the hot
-- store. The forward gate reads these; it must never re-derive a class from
-- `feature_json`, because a re-derived threshold is a threshold that can move.
CREATE TABLE IF NOT EXISTS op_insight_breadth_labels(
  launch_id TEXT PRIMARY KEY,
  network_id TEXT NOT NULL,
  venue_id TEXT NOT NULL,
  decision_snapshot_id TEXT NOT NULL,
  rule_version TEXT NOT NULL,
  breadth_class TEXT NOT NULL,
  censor_reason TEXT,
  eligible_buyer_count INTEGER,
  entity_buyer_count INTEGER,
  launch_mode TEXT,
  decision_age_ms INTEGER,
  progress_bps INTEGER,
  modeled_round_trip_cost_bps INTEGER,
  observed_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_op_insight_breadth_labels_class
ON op_insight_breadth_labels(breadth_class, launch_mode);

CREATE TABLE IF NOT EXISTS op_insight_alert_targets(
  alert_id TEXT NOT NULL,
  target_bps INTEGER NOT NULL,
  horizon_ms INTEGER NOT NULL,
  evaluator_version TEXT NOT NULL,
  result TEXT NOT NULL,
  best_return_bps INTEGER,
  worst_return_bps INTEGER,
  time_to_target_ms INTEGER,
  created_at TEXT NOT NULL,
  PRIMARY KEY(alert_id, target_bps, horizon_ms)
);
"""


def connect_insights(path: str | Path) -> sqlite3.Connection:
    resolved = Path(path).expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(resolved, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA journal_size_limit=16777216")  # trim the WAL after a checkpoint reset
    conn.execute("PRAGMA wal_autocheckpoint=1000")
    compacted_targets = False
    conn.executescript(f"BEGIN IMMEDIATE;{_SCHEMA}COMMIT;")
    row = conn.execute("SELECT version FROM op_insight_schema_version").fetchall()
    if len(row) == 1 and row[0][0] in (1, 2, 3, 4, 5):
        conn.execute("BEGIN IMMEDIATE")
        alert_columns = {
            item[1] for item in conn.execute("PRAGMA table_info(op_insight_alerts)")
        }
        if "rule_version" not in alert_columns:
            conn.execute(
                "ALTER TABLE op_insight_alerts ADD COLUMN rule_version TEXT "
                "NOT NULL DEFAULT 'legacy-unknown'"
            )
        if "feature_version" not in alert_columns:
            conn.execute("ALTER TABLE op_insight_alerts ADD COLUMN feature_version TEXT")
        if "feature_json" not in alert_columns:
            conn.execute("ALTER TABLE op_insight_alerts ADD COLUMN feature_json TEXT")
        for table in ("op_insight_targets", "op_insight_alert_targets"):
            target_columns = {
                item[1] for item in conn.execute(f"PRAGMA table_info({table})")
            }
            if "evaluator_version" not in target_columns:
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN evaluator_version TEXT "
                    "NOT NULL DEFAULT 'target-ladder-v1'"
                )
        conn.execute("UPDATE op_insight_schema_version SET version=6")
        conn.execute("COMMIT")
        row = conn.execute("SELECT version FROM op_insight_schema_version").fetchall()
    if len(row) == 1 and row[0][0] == 6:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO op_insight_target_daily "
            "SELECT substr(created_at,1,10),venue_id,target_bps,horizon_ms,"
            "evaluator_version,result,COUNT(*) FROM op_insight_targets "
            "GROUP BY substr(created_at,1,10),venue_id,target_bps,horizon_ms,"
            "evaluator_version,result ON CONFLICT DO UPDATE SET "
            "sample_count=sample_count+excluded.sample_count"
        )
        conn.execute("DELETE FROM op_insight_targets")
        conn.execute("UPDATE op_insight_schema_version SET version=7")
        conn.execute("COMMIT")
        compacted_targets = True
        row = conn.execute("SELECT version FROM op_insight_schema_version").fetchall()
    if len(row) == 1 and row[0][0] == 7:
        conn.execute("BEGIN IMMEDIATE")
        sample_columns = {
            item[1] for item in conn.execute("PRAGMA table_info(op_insight_samples)")
        }
        if "label_version" not in sample_columns:
            conn.execute(
                "ALTER TABLE op_insight_samples ADD COLUMN label_version TEXT "
                "NOT NULL DEFAULT 'selected-outcome-v1'"
            )
        conn.execute("UPDATE op_insight_schema_version SET version=8")
        conn.execute("COMMIT")
        row = conn.execute("SELECT version FROM op_insight_schema_version").fetchall()
    if len(row) == 1 and row[0][0] == 8:
        conn.execute("BEGIN IMMEDIATE")
        alert_columns = {
            item[1] for item in conn.execute("PRAGMA table_info(op_insight_alerts)")
        }
        if "creator_id" not in alert_columns:
            conn.execute("ALTER TABLE op_insight_alerts ADD COLUMN creator_id TEXT")
        # Backfill from the samples that are still alive. Run once, at upgrade, so the
        # cohort already in flight keeps its creator clustering after its samples prune.
        # Rows whose samples have already gone stay NULL and read as coverage-incomplete,
        # which is the honest answer rather than an invented creator. A pre-v3 table may
        # predate `launch_id`, in which case there is nothing to join on and NULL stands.
        if "launch_id" in alert_columns:
            conn.execute(
                "UPDATE op_insight_alerts SET creator_id=("
                "SELECT MAX(s.creator_id) FROM op_insight_samples s "
                "WHERE s.launch_id=op_insight_alerts.launch_id AND s.creator_id IS NOT NULL"
                ") WHERE creator_id IS NULL"
            )
        conn.execute("UPDATE op_insight_schema_version SET version=9")
        conn.execute("COMMIT")
        row = conn.execute("SELECT version FROM op_insight_schema_version").fetchall()
    if len(row) == 1 and row[0][0] == 9:
        # Schema 10 adds `op_insight_breadth_labels`. The schema block above already
        # created it on this read-write path. The version must still advance, because
        # `connect_existing_insights` opens read-only and cannot create a table: a
        # version-9 database would otherwise pass the check and then fail the first read.
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE op_insight_schema_version SET version=10")
        conn.execute("COMMIT")
        row = conn.execute("SELECT version FROM op_insight_schema_version").fetchall()
    if len(row) != 1 or row[0][0] != INSIGHT_SCHEMA_VERSION:
        conn.close()
        raise RuntimeError("unsupported opportunity insight schema")
    if compacted_targets:
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        free_pages = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
        if free_pages:
            conn.execute(f"PRAGMA incremental_vacuum({free_pages})").fetchall()
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    return conn


def connect_existing_insights(path: str | Path) -> sqlite3.Connection:
    resolved = Path(path).expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(resolved)
    conn = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT version FROM op_insight_schema_version").fetchall()
    if len(row) != 1 or row[0][0] != INSIGHT_SCHEMA_VERSION:
        conn.close()
        raise RuntimeError("unsupported opportunity insight schema")
    return conn


def _launch_diagnostics(hot: sqlite3.Connection, launch_id: str):
    entry = hot.execute(
        "SELECT e.entry_id FROM op_counterfactual_entries e "
        "JOIN op_landmark_states lm ON lm.landmark_id=e.landmark_id "
        "WHERE lm.launch_id=? ORDER BY e.entry_observed_at,e.entry_id LIMIT 1",
        (launch_id,),
    ).fetchone()
    if entry is None:
        return (), None
    points = tuple(
        ExecutablePoint(
            int(row["elapsed_ms"]), str(row["exit_kind"]),
            int(row["net_return_bps"]) if row["net_return_bps"] is not None else None,
            bool(row["source_healthy"]), bool(row["migration"]),
        )
        for row in hot.execute(
            "SELECT elapsed_ms,exit_kind,net_return_bps,source_healthy,migration "
            "FROM op_executable_observations WHERE entry_id=? "
            "ORDER BY elapsed_ms,observation_id",
            (entry["entry_id"],),
        ).fetchall()
    )
    return evaluate_target_ladder(points) if points else (), near_miss(points)


def _sample_id(
    launch_id: str, role: str, rule_version: str, label_version: str
) -> str:
    material = f"{launch_id}\x1f{role}\x1f{rule_version}\x1f{label_version}".encode()
    return hashlib.sha256(material).hexdigest()


def _candidate_rows(hot: sqlite3.Connection, launch_id: str) -> list[sqlite3.Row]:
    return hot.execute(
        "SELECT p.*,s.state_json,s.row_fingerprint AS snapshot_fingerprint "
        "FROM op_predictions p JOIN op_state_snapshots s ON s.snapshot_id=p.snapshot_id "
        "WHERE s.launch_id=? ORDER BY p.observed_at,p.prediction_id",
        (launch_id,),
    ).fetchall()


def _outcome(hot: sqlite3.Connection, launch_id: str) -> sqlite3.Row | None:
    return hot.execute(
        "SELECT o.* FROM op_competing_outcomes o "
        "JOIN op_counterfactual_entries e ON e.entry_id=o.entry_id "
        "JOIN op_landmark_states l ON l.landmark_id=e.landmark_id "
        "WHERE l.launch_id=? ORDER BY o.inserted_at DESC,o.outcome_id DESC LIMIT 1",
        (launch_id,),
    ).fetchone()


def _selected_samples(rows: list[sqlite3.Row]) -> list[tuple[str, sqlite3.Row]]:
    if not rows:
        return []
    fired = [row for row in rows if row["would_fire"]]
    action = min(fired or rows, key=lambda row: (row["distance_bps"], row["observed_at"]))
    control_pool = [row for row in rows if not row["would_fire"] and row["prediction_id"] != action["prediction_id"]]
    selected = [("fired" if fired else "closest", action)]
    if control_pool:
        selected.append(("control", max(control_pool, key=lambda row: (row["distance_bps"], row["observed_at"]))))
    return selected


def _selected_sample_outcome(
    hot: sqlite3.Connection,
    launch_id: str,
    row: sqlite3.Row,
) -> sqlite3.Row | None:
    linked = hot.execute(
        "SELECT o.* FROM op_forward_alert_entries link "
        "JOIN op_competing_outcomes o ON o.entry_id=link.entry_id "
        "WHERE link.snapshot_id=? ORDER BY o.inserted_at DESC,o.outcome_id DESC LIMIT 1",
        (row["snapshot_id"],),
    ).fetchone()
    if linked is not None:
        return linked
    return hot.execute(
        "SELECT o.* FROM op_state_snapshots s "
        "JOIN op_landmark_states l ON l.launch_id=s.launch_id "
        "AND l.observed_event_id=s.observed_event_id "
        "JOIN op_counterfactual_entries e ON e.landmark_id=l.landmark_id "
        "JOIN op_competing_outcomes o ON o.entry_id=e.entry_id "
        "WHERE s.snapshot_id=? AND s.launch_id=? "
        "ORDER BY o.inserted_at DESC,o.outcome_id DESC LIMIT 1",
        (row["snapshot_id"], launch_id),
    ).fetchone()


def _project_breadth_label(
    hot: sqlite3.Connection,
    insights: sqlite3.Connection,
    *,
    launch_id: str,
    created_at: str,
) -> None:
    """Carry the frozen breadth label across to the durable DB. Never re-derive it.

    A hot store older than schema 16 has no label table. That is a structural state, not
    a fault, so it is tested for rather than caught: swallowing OperationalError here
    would also hide a genuine read regression.
    """
    if not hot.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='op_breadth_labels'"
    ).fetchone():
        return
    row = hot.execute(
        "SELECT launch_id,network_id,venue_id,decision_snapshot_id,rule_version,"
        "breadth_class,censor_reason,eligible_buyer_count,entity_buyer_count,launch_mode,"
        "decision_age_ms,progress_bps,modeled_round_trip_cost_bps,observed_at "
        "FROM op_breadth_labels WHERE launch_id=?",
        (launch_id,),
    ).fetchone()
    if row is None:
        return
    insights.execute(
        "INSERT OR IGNORE INTO op_insight_breadth_labels "
        "(launch_id,network_id,venue_id,decision_snapshot_id,rule_version,breadth_class,"
        "censor_reason,eligible_buyer_count,entity_buyer_count,launch_mode,decision_age_ms,"
        "progress_bps,modeled_round_trip_cost_bps,observed_at,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (*tuple(row), created_at),
    )


def _project_alert_evidence(
    hot: sqlite3.Connection,
    insights: sqlite3.Connection,
    *,
    launch_id: str,
    created_at: str,
    creator_id: str | None = None,
    score_status_override: str | None = None,
) -> None:
    _project_breadth_label(hot, insights, launch_id=launch_id, created_at=created_at)
    for alert in _alert_evidence_hot(hot):
        if alert["launch_id"] != launch_id:
            continue
        if score_status_override is not None:
            alert = {**alert, "score_status": score_status_override}
        source_fingerprint = hashlib.sha256(
            json.dumps(alert, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        insights.execute(
            "INSERT OR IGNORE INTO op_insight_alerts "
            "(alert_id,launch_id,venue_id,token_id,entry_id,rule_version,observed_at,"
            "quote_observed_at,quote_completed_at,quote_age_ms,entry_effective_at,provider,"
            "evaluator_version,quote_drag_bps,score_status,outcome_kind,terminal_return_bps,"
            "outcome_elapsed_ms,mfe_bps,mae_bps,time_to_best_ms,feature_version,feature_json,"
            "creator_id,source_fingerprint,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                alert["alert_id"], alert["launch_id"], alert["venue_id"], alert["token_id"],
                alert["entry_id"], alert["rule_version"], alert["observed_at"],
                alert["quote_observed_at"],
                alert["quote_completed_at"], alert["quote_age_ms"], alert["entry_effective_at"],
                alert["provider"], alert["evaluator_version"], alert["quote_drag_bps"],
                alert["score_status"], alert["outcome_kind"], alert["terminal_return_bps"],
                alert["outcome_elapsed_ms"], alert["mfe_bps"], alert["mae_bps"],
                alert["time_to_best_ms"], alert.get("feature_version"),
                json.dumps(alert.get("features") or {}, sort_keys=True, separators=(",", ":")),
                creator_id, source_fingerprint, created_at,
            ),
        )
        for target in alert["targets"]:
            insights.execute(
                "INSERT OR IGNORE INTO op_insight_alert_targets "
                "(alert_id,target_bps,horizon_ms,evaluator_version,result,best_return_bps,"
                "worst_return_bps,time_to_target_ms,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    alert["alert_id"], target["target_bps"], target["horizon_ms"],
                    target["evaluator_version"], target["result"],
                    target["best_return_bps"], target["worst_return_bps"],
                    target["time_to_target_ms"], created_at,
                ),
            )


def project_matured_insights(
    hot: sqlite3.Connection,
    insights: sqlite3.Connection,
    *,
    now_iso: str | None = None,
    launch_ids: list[str] | tuple[str, ...] | None = None,
    allow_incomplete: bool = False,
) -> int:
    """Project completed launch evidence exactly once; return launch receipts added."""
    now_iso = now_iso or real_utc_now_iso()
    launches = hot.execute(
        "SELECT * FROM op_launches" + (
            " WHERE launch_id IN (%s)" % ",".join("?" for _ in launch_ids)
            if launch_ids else ""
        ),
        tuple(launch_ids or ()),
    ).fetchall()
    added = 0
    insights.execute("BEGIN IMMEDIATE")
    try:
        for launch in launches:
            if insights.execute(
                "SELECT 1 FROM op_projected_launches WHERE launch_id=?", (launch["launch_id"],)
            ).fetchone():
                continue
            incomplete = hot.execute(
                "SELECT 1 FROM op_counterfactual_entries e "
                "JOIN op_landmark_states l ON l.landmark_id=e.landmark_id "
                "WHERE l.launch_id=? AND NOT EXISTS ("
                "SELECT 1 FROM op_competing_outcomes o WHERE o.entry_id=e.entry_id) LIMIT 1",
                (launch["launch_id"],),
            ).fetchone()
            if incomplete and not allow_incomplete:
                continue
            if incomplete:
                _project_alert_evidence(
                    hot,
                    insights,
                    launch_id=launch["launch_id"],
                    created_at=now_iso,
                    creator_id=launch["creator_id"],
                    score_status_override="DATA_GAP_INVALID",
                )
                insights.execute(
                    "INSERT INTO op_projected_launches VALUES(?,?,?)",
                    (launch["launch_id"], launch["row_fingerprint"], now_iso),
                )
                added += 1
                continue
            outcome = _outcome(hot, launch["launch_id"])
            target_rows, miss = _launch_diagnostics(hot, launch["launch_id"])
            for target in target_rows:
                insights.execute(
                    "INSERT INTO op_insight_target_daily VALUES(?,?,?,?,?,?,1) "
                    "ON CONFLICT DO UPDATE SET sample_count=sample_count+1",
                    (
                        now_iso[:10], launch["venue_id"], target["target_bps"],
                        target["horizon_ms"], target["evaluator_version"], target["result"],
                    ),
                )
            if miss is not None:
                insights.execute(
                    "INSERT OR IGNORE INTO op_insight_near_misses VALUES(?,?,?,?,?,?,?)",
                    (
                        launch["launch_id"], launch["venue_id"], launch["token_id"],
                        str(outcome["outcome_kind"] if outcome else "pending").upper(),
                        miss["best_return_bps"], miss["time_to_best_ms"], now_iso,
                    ),
                )
            _project_alert_evidence(
                hot,
                insights,
                launch_id=launch["launch_id"],
                created_at=now_iso,
                creator_id=launch["creator_id"],
            )
            for role, row in _selected_samples(_candidate_rows(hot, launch["launch_id"])):
                label_version = "selected-outcome-v2"
                sample_outcome = _selected_sample_outcome(
                    hot, launch["launch_id"], row
                )
                feature_blob = zlib.compress(row["state_json"].encode(), level=6)
                source_fingerprint = hashlib.sha256(
                    f"{launch['row_fingerprint']}:{row['row_fingerprint']}:{row['snapshot_fingerprint']}".encode()
                ).hexdigest()
                values = (
                    _sample_id(
                        launch["launch_id"], role, row["rule_version"], label_version
                    ),
                    launch["launch_id"], launch["network_id"], launch["venue_id"],
                    launch["token_id"], launch["creator_id"], role, row["observed_at"],
                    row["zone"], row["would_fire"], row["rule_version"], row["distance_bps"],
                    row["dominant_risk"], sample_outcome["outcome_kind"] if sample_outcome else None,
                    sample_outcome["terminal_net_return_bps"] if sample_outcome else None,
                    sample_outcome["elapsed_ms"] if sample_outcome else None,
                    feature_blob, label_version, source_fingerprint, now_iso,
                )
                insights.execute(
                    "INSERT INTO op_insight_samples "
                    "(sample_id,launch_id,network_id,venue_id,token_id,creator_id,sample_role,"
                    "observed_at,zone,would_fire,rule_version,distance_bps,dominant_risk,"
                    "outcome_kind,terminal_net_return_bps,elapsed_ms,feature_blob,label_version,"
                    "source_fingerprint,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    values,
                )
                key = (row["observed_at"][:10], launch["venue_id"], row["rule_version"], role, row["zone"])
                kind = str(sample_outcome["outcome_kind"] if sample_outcome else "").upper()
                insights.execute(
                    "INSERT INTO op_insight_daily VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(day_utc,venue_id,rule_version,sample_role,zone) DO UPDATE SET "
                    "sample_count=sample_count+excluded.sample_count,fire_count=fire_count+excluded.fire_count,"
                    "jump_count=jump_count+excluded.jump_count,dump_count=dump_count+excluded.dump_count,"
                    "exit_failure_count=exit_failure_count+excluded.exit_failure_count,"
                    "terminal_return_bps_sum=terminal_return_bps_sum+excluded.terminal_return_bps_sum",
                    (*key, 1, int(row["would_fire"]), int(kind == "PROFIT_FIRST"),
                     int(kind == "LOSS_FIRST"), int(kind == "PERSISTENT_EXIT_FAILURE_FIRST"),
                     int(sample_outcome["terminal_net_return_bps"] or 0)
                     if sample_outcome else 0),
                )
            insights.execute(
                "INSERT INTO op_projected_launches VALUES(?,?,?)",
                (launch["launch_id"], launch["row_fingerprint"], now_iso),
            )
            added += 1
        insights.execute("COMMIT")
    except BaseException:
        insights.execute("ROLLBACK")
        raise
    # Checkpoint sealing rides the projection cadence but can never break it: a sealing
    # failure loses one attempt (retried next cycle), never a projection.
    try:
        seal_gate_checkpoints(hot, insights, now_iso=now_iso)
    except Exception:
        logger.exception("gate checkpoint sealing failed; will retry next projection")
    return added


def _number(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _outcome_return(row: sqlite3.Row) -> int | None:
    keys = set(row.keys())
    value = row["terminal_net_return_bps"]
    if value is None and "last_executable_return_bps" in keys:
        value = row["last_executable_return_bps"]
    return int(value) if value is not None else None


def _entry_points(hot: sqlite3.Connection, entry_id: str) -> tuple[ExecutablePoint, ...]:
    return tuple(
        ExecutablePoint(
            int(row["elapsed_ms"]),
            str(row["exit_kind"]),
            int(row["net_return_bps"]) if row["net_return_bps"] is not None else None,
            bool(row["source_healthy"]),
            bool(row["migration"]),
        )
        for row in hot.execute(
            "SELECT elapsed_ms,exit_kind,net_return_bps,source_healthy,migration "
            "FROM op_executable_observations WHERE entry_id=? "
            "ORDER BY elapsed_ms,observation_id",
            (entry_id,),
        ).fetchall()
    )


def _headline_alert_targets(points) -> list[dict[str, object]]:
    """Keep only target cells consumed by the compact Radar headline."""
    return [
        row for row in evaluate_target_ladder(points)
        if int(row.get("target_bps") or 0) == HEADLINE_TARGET_BPS
        and int(row.get("horizon_ms") or 0) in {60_000, 300_000}
    ]


def _alert_evidence_hot(hot: sqlite3.Connection) -> list[dict[str, object]]:
    rows = hot.execute(
        "SELECT a.alert_id,a.launch_id,a.venue_id,a.token_id,a.observed_at,a.rule_version,"
        "a.round_trip_return_bps AS quote_drag_bps,link.entry_id,"
        "link.quote_observed_at,link.quote_completed_at,link.quote_age_ms,"
        "link.entry_effective_at,link.provider,link.evaluator_version,"
        "feature.feature_version,feature.feature_json,"
        "l.creator_id,"
        "o.outcome_kind,o.terminal_net_return_bps,o.last_executable_return_bps,"
        "o.elapsed_ms AS outcome_elapsed_ms "
        "FROM op_forward_alerts a LEFT JOIN op_launches l ON l.launch_id=a.launch_id "
        "LEFT JOIN op_forward_alert_entries link "
        "ON link.alert_id=a.alert_id LEFT JOIN op_competing_outcomes o "
        "ON o.entry_id=link.entry_id LEFT JOIN op_forward_alert_features feature "
        "ON feature.alert_id=a.alert_id ORDER BY a.observed_at DESC,a.alert_id DESC"
    ).fetchall()
    evidence: list[dict[str, object]] = []
    for row in rows:
        entry_id = str(row["entry_id"]) if row["entry_id"] is not None else None
        points = _entry_points(hot, entry_id) if entry_id else ()
        executable = [
            point for point in points
            if point.exit_kind == "executable" and point.net_return_bps is not None
        ]
        best = max(executable, key=lambda point: (int(point.net_return_bps), -point.elapsed_ms)) if executable else None
        latest = executable[-1] if executable else None
        outcome_kind = str(row["outcome_kind"]).upper() if row["outcome_kind"] else None
        score_status = _outcome_score_status(entry_id, outcome_kind)
        terminal = row["terminal_net_return_bps"]
        if terminal is None:
            terminal = row["last_executable_return_bps"]
        try:
            features = json.loads(row["feature_json"] or "{}")
        except (TypeError, ValueError):
            features = {}
        evidence.append(
            {
                "alert_id": str(row["alert_id"]),
                "launch_id": str(row["launch_id"]),
                "venue_id": str(row["venue_id"]),
                "token_id": str(row["token_id"]),
                "creator_id": row["creator_id"],
                "entry_id": entry_id,
                "rule_version": str(row["rule_version"]),
                "observed_at": str(row["observed_at"]),
                "quote_observed_at": row["quote_observed_at"],
                "quote_completed_at": row["quote_completed_at"],
                "quote_age_ms": row["quote_age_ms"],
                "entry_effective_at": row["entry_effective_at"],
                "provider": row["provider"],
                "evaluator_version": row["evaluator_version"],
                "quote_drag_bps": row["quote_drag_bps"],
                "score_status": score_status,
                "outcome_kind": outcome_kind,
                "terminal_return_bps": int(terminal) if terminal is not None else None,
                "latest_observed_return_bps": (
                    int(latest.net_return_bps) if latest is not None else None
                ),
                "latest_observed_elapsed_ms": latest.elapsed_ms if latest is not None else None,
                "outcome_elapsed_ms": row["outcome_elapsed_ms"],
                "mfe_bps": max(int(point.net_return_bps) for point in executable) if executable else None,
                "mae_bps": min(int(point.net_return_bps) for point in executable) if executable else None,
                "time_to_best_ms": best.elapsed_ms if best is not None else None,
                "feature_version": row["feature_version"],
                "features": features,
                # The whole target x horizon ladder, not just the headline cell.
                # Persisting only (1200, 60s) made every exit-policy question
                # unanswerable: the v4 cohort has one cell, so "would a smaller
                # take-profit have done better" cannot be asked of it at all.
                # The board payload drops `targets` in compaction, so this is
                # storage-only and does not widen any response.
                "targets": evaluate_target_ladder(points) if points else [],
            }
        )
    return evidence


def _alert_evidence_historical(
    historical: sqlite3.Connection | None,
) -> list[dict[str, object]]:
    if historical is None:
        return []
    alerts = [dict(row) for row in historical.execute(
        "SELECT alert_id,launch_id,venue_id,token_id,entry_id,rule_version,observed_at,"
        "quote_observed_at,quote_completed_at,quote_age_ms,entry_effective_at,provider,"
        "evaluator_version,quote_drag_bps,score_status,outcome_kind,terminal_return_bps,"
        "outcome_elapsed_ms,mfe_bps,mae_bps,time_to_best_ms,feature_version,feature_json,"
        "creator_id "
        "FROM op_insight_alerts ORDER BY observed_at DESC,alert_id DESC"
    ).fetchall()]
    targets: dict[str, list[dict[str, object]]] = {}
    for row in historical.execute(
        "SELECT alert_id,target_bps,horizon_ms,evaluator_version,result,best_return_bps,worst_return_bps,"
        "time_to_target_ms FROM op_insight_alert_targets "
        "WHERE target_bps=1200 AND horizon_ms IN (60000,300000) "
        "ORDER BY alert_id,target_bps,horizon_ms"
    ).fetchall():
        targets.setdefault(str(row["alert_id"]), []).append(dict(row))
    creators = {
        str(row["launch_id"]): row["creator_id"]
        for row in historical.execute(
            "SELECT launch_id,MAX(creator_id) AS creator_id FROM op_insight_samples "
            "WHERE creator_id IS NOT NULL GROUP BY launch_id"
        ).fetchall()
    }
    for alert in alerts:
        retained_status = str(alert.get("score_status") or "")
        alert["score_status"] = (
            retained_status
            if retained_status not in {"SCORED", "PENDING"}
            else _outcome_score_status(alert.get("entry_id"), alert.get("outcome_kind"))
        )
        alert["latest_observed_return_bps"] = None
        alert["latest_observed_elapsed_ms"] = None
        # The stored column is authoritative: it outlives the samples it was copied from.
        # The join only covers rows written before schema 9 whose samples are still alive.
        alert["creator_id"] = alert.get("creator_id") or creators.get(str(alert["launch_id"]))
        try:
            alert["features"] = json.loads(alert.pop("feature_json") or "{}")
        except (TypeError, ValueError):
            alert["features"] = {}
        alert["targets"] = targets.get(str(alert["alert_id"]), [])
    return alerts


def _alert_summary(alerts: list[dict[str, object]]) -> dict[str, int]:
    summary = {
        "alerts": len(alerts),
        "scored": 0,
        "pending": 0,
        "unscored_legacy": 0,
        "profit_first": 0,
        "loss_first": 0,
        "other": 0,
    }
    for alert in alerts:
        status = str(alert["score_status"])
        if status == "SCORED":
            summary["scored"] += 1
        elif status == "PENDING":
            summary["pending"] += 1
        else:
            summary["unscored_legacy"] += 1
        if status != "SCORED":
            continue
        kind = str(alert["outcome_kind"] or "")
        if kind == "PROFIT_FIRST":
            summary["profit_first"] += 1
        elif kind == "LOSS_FIRST":
            summary["loss_first"] += 1
        else:
            summary["other"] += 1
    return summary


def _arm_comparison(alerts: list[dict[str, object]]) -> dict[str, dict[str, dict[str, int | None]]]:
    rules = (
        "pump-clean-start-v1",
        "pump-curve-break-v1",
        "pump-ignition-v1",
        "pump-confirmation-v1",
        "pump-confirmation-truth-v2",
        "pump-confirmation-independent-v1",
        "pump-confirmation-clean-v1",
        "pump-confirmation-low-friction-v1",
        "pump-confirmation-delayed-v1",
        "pump-confirmation-truth-v3",
        "pump-confirmation-delayed-v2",
        "pump-confirmation-truth-v4",
        "pump-confirmation-truth-v5",
        "pump-confirmation-truth-v6",
        "pump-creator-prior-v1",
        "route-control-v1",
        "route-control-v2",
    )
    horizons = (60_000, 300_000)
    comparison = {
        rule: {
            str(horizon): {
                "hits": 0,
                "losses": 0,
                "timeouts": 0,
                "other": 0,
                "invalid": 0,
                "pending": 0,
                "coverage_missing": 0,
                "scored": 0,
                "samples": 0,
                "hit_rate_bps": 0,
                "final_return_samples": 0,
                "avg_final_return_bps": None,
            }
            for horizon in horizons
        }
        for rule in rules
    }
    final_return_sums = {
        rule: {str(horizon): 0 for horizon in horizons}
        for rule in rules
    }
    for alert in alerts:
        rule = str(alert.get("rule_version") or "")
        if rule not in comparison:
            continue
        status = str(alert.get("score_status") or "")
        if status != "SCORED":
            count_key = "pending" if status == "PENDING" else "invalid"
            for horizon in horizons:
                comparison[rule][str(horizon)][count_key] += 1
            continue
        targets = alert.get("targets") or []
        for horizon in horizons:
            target = next(
                (
                    row for row in targets
                    if int(row.get("target_bps") or 0) == 1_200
                    and int(row.get("horizon_ms") or 0) == horizon
                ),
                None,
            )
            if target is None:
                continue
            cell = comparison[rule][str(horizon)]
            result = str(target.get("result") or "")
            legacy_early_timeout = (
                result == "timeout"
                and target.get("evaluator_version") == "target-ladder-v1"
                and int(alert.get("outcome_elapsed_ms") or 0) < horizon
            )
            if result == "pending" or legacy_early_timeout:
                cell["coverage_missing"] += 1
                continue
            cell["samples"] += 1
            cell["scored"] += 1
            cell["hits"] += int(result == "hit")
            cell["losses"] += int(result == "loss")
            cell["timeouts"] += int(result == "timeout")
            cell["other"] += int(result not in {"hit", "loss", "timeout"})
            terminal_return = alert.get("terminal_return_bps")
            if horizon == 60_000 and terminal_return is not None:
                cell["final_return_samples"] += 1
                final_return_sums[rule][str(horizon)] += int(terminal_return)
    for rule, by_horizon in comparison.items():
        for horizon, cell in by_horizon.items():
            cell["hit_rate_bps"] = (
                cell["hits"] * 10_000 // cell["samples"]
                if cell["samples"] else 0
            )
            cell["avg_final_return_bps"] = (
                round(final_return_sums[rule][horizon] / cell["final_return_samples"])
                if cell["final_return_samples"] else None
            )
    return comparison


_MICROSCOPE_ARMS = (
    ("truth", "pump-confirmation-truth-v2", "All confirmations"),
    ("independent", "pump-confirmation-independent-v1", "New eligible buyers rising"),
    ("clean", "pump-confirmation-clean-v1", "New eligible buyers + low selling"),
    ("low_friction", "pump-confirmation-low-friction-v1", "Low entry/exit drag"),
    ("delayed", "pump-confirmation-delayed-v1", "Wait one more update"),
)


def _confirmation_microscope_v2_pilot(alerts: list[dict[str, object]]) -> dict[str, object]:
    """Summarize exact 60-second action value at preregistered checkpoints."""
    rows: list[dict[str, object]] = []
    by_rule = {
        rule: [alert for alert in alerts if alert.get("rule_version") == rule]
        for _, rule, _ in _MICROSCOPE_ARMS
    }
    for key, rule, label in _MICROSCOPE_ARMS:
        arm = by_rule[rule]
        scored = [row for row in arm if row.get("score_status") == "SCORED"]
        returns = [
            int(row["terminal_return_bps"])
            for row in scored
            if row.get("terminal_return_bps") is not None
        ]
        rows.append(
            {
                "key": key,
                "rule_version": rule,
                "label": label,
                "completed": len(scored),
                "pending": sum(row.get("score_status") == "PENDING" for row in arm),
                "invalid": sum(
                    row.get("score_status") not in {"SCORED", "PENDING"} for row in arm
                ),
                "profit_hits": sum(row.get("outcome_kind") == "PROFIT_FIRST" for row in scored),
                "losses": sum(row.get("outcome_kind") == "LOSS_FIRST" for row in scored),
                "time_exits": sum(row.get("outcome_kind") == "TIMEOUT" for row in scored),
                "exit_failures": sum(
                    row.get("outcome_kind") == "PERSISTENT_EXIT_FAILURE_FIRST"
                    for row in scored
                ),
                "return_samples": len(returns),
                "sum_return_bps": sum(returns),
                "mean_return_bps": round(sum(returns) / len(returns)) if returns else None,
                "median_return_bps": round(median(returns)) if returns else None,
                "checkpoint_status": "READY" if len(scored) >= 25 else "COLLECTING",
            }
        )

    truth_by_launch = {
        str(row.get("launch_id")): int(row["terminal_return_bps"])
        for row in by_rule["pump-confirmation-truth-v2"]
        if row.get("score_status") == "SCORED" and row.get("terminal_return_bps") is not None
    }
    delayed_by_launch = {
        str(row.get("launch_id")): int(row["terminal_return_bps"])
        for row in by_rule["pump-confirmation-delayed-v1"]
        if row.get("score_status") == "SCORED" and row.get("terminal_return_bps") is not None
    }
    deltas = [
        truth_by_launch[launch_id] - delayed_by_launch[launch_id]
        for launch_id in truth_by_launch.keys() & delayed_by_launch.keys()
    ]
    candidates = [
        row for row in rows
        if row["key"] != "delayed" and row["mean_return_bps"] is not None
    ]
    checkpoint_candidates = [row for row in candidates if int(row["completed"]) >= 25]
    best = max(
        checkpoint_candidates or candidates,
        key=lambda row: (int(row["mean_return_bps"]), int(row["completed"])),
        default=None,
    )
    controls_by_launch = {
        str(row.get("launch_id")): int(row["terminal_return_bps"])
        for row in alerts
        if row.get("rule_version") == "route-control-v1"
        and row.get("score_status") == "SCORED"
        and row.get("terminal_return_bps") is not None
    }
    baseline_by_rule = {
        "pump-confirmation-truth-v2": controls_by_launch,
        "pump-confirmation-independent-v1": truth_by_launch,
        "pump-confirmation-clean-v1": truth_by_launch,
        "pump-confirmation-low-friction-v1": truth_by_launch,
    }
    best_alerts = (
        by_rule[str(best["rule_version"])] if best is not None else []
    )
    best_scored = [
        row for row in best_alerts
        if row.get("score_status") == "SCORED"
        and row.get("terminal_return_bps") is not None
    ]
    checkpoint = max(
        (value for value in (25, 50, 100) if len(best_scored) >= value),
        default=None,
    )
    blockers: list[str] = []
    paired_baseline = baseline_by_rule.get(str(best["rule_version"])) if best else None
    paired_deltas = [
        int(row["terminal_return_bps"]) - paired_baseline[str(row["launch_id"])]
        for row in best_scored
        if paired_baseline is not None and str(row["launch_id"]) in paired_baseline
    ]
    if checkpoint is not None:
        if sum(int(row["terminal_return_bps"]) for row in best_scored) <= 0:
            blockers.append("CUMULATIVE_RETURN_NOT_POSITIVE")
        if len(paired_deltas) != len(best_scored):
            blockers.append("PAIRED_BASELINE_INCOMPLETE")
        elif sum(paired_deltas) <= 0:
            blockers.append("PAIRED_BASELINE_NOT_IMPROVED")

        def concentration(field):
            grouped: dict[str, int] = {}
            for row in best_scored:
                key = field(row)
                if not key:
                    return None
                grouped[str(key)] = grouped.get(str(key), 0) + int(row["terminal_return_bps"])
            positive = [value for value in grouped.values() if value > 0]
            total = sum(positive)
            return max(positive, default=0) * 10_000 // total if total > 0 else 0

        creator_concentration = concentration(lambda row: row.get("creator_id"))
        mode_concentration = concentration(
            lambda row: (
                "cashback" if (row.get("features") or {}).get("cashback_enabled")
                else "mayhem" if (row.get("features") or {}).get("mayhem_mode")
                else "standard"
            )
        )
        if creator_concentration is None:
            blockers.append("CREATOR_COVERAGE_INCOMPLETE")
        elif creator_concentration > 5_000:
            blockers.append("CREATOR_CONCENTRATION_OVER_50")
        if mode_concentration is None:
            blockers.append("MODE_COVERAGE_INCOMPLETE")
        elif mode_concentration > 5_000:
            blockers.append("MODE_CONCENTRATION_OVER_50")
    else:
        creator_concentration = None
        mode_concentration = None
    status = "COLLECTING"
    if checkpoint is not None:
        if any("CONCENTRATION" in code for code in blockers):
            status = "CONCENTRATION_BLOCKED"
        elif blockers:
            status = "EVIDENCE_BLOCKED"
        else:
            status = "PROMISING_DIRECTION"
    truth_launches = {
        str(row["launch_id"])
        for row in by_rule["pump-confirmation-truth-v2"]
        if row.get("score_status") == "SCORED"
    }
    overlap = {}
    for key, rule, _label in _MICROSCOPE_ARMS:
        if key in {"truth", "delayed"}:
            continue
        arm_launches = {
            str(row["launch_id"])
            for row in by_rule[rule]
            if row.get("score_status") == "SCORED"
        }
        intersection = len(truth_launches & arm_launches)
        union = len(truth_launches | arm_launches)
        overlap[key] = {
            "truth_samples": len(truth_launches),
            "arm_samples": len(arm_launches),
            "overlap": intersection,
            "incremental_samples": len(arm_launches - truth_launches),
            "jaccard_bps": intersection * 10_000 // union if union else None,
        }
    return {
        "status": status,
        "best_arm": best["key"] if best is not None else None,
        "best_label": best["label"] if best is not None else None,
        "checkpoint": checkpoint,
        "blocker_codes": blockers,
        "paired_baseline": {
            "pairs": len(paired_deltas),
            "sum_arm_minus_baseline_bps": sum(paired_deltas),
        },
        "concentration": {
            "creator_max_gain_share_bps": creator_concentration,
            "mode_max_gain_share_bps": mode_concentration,
        },
        "arm_overlap": overlap,
        "arms": rows,
        "paired_delay": {
            "pairs": len(deltas),
            "immediate_better": sum(delta > 0 for delta in deltas),
            "delayed_better": sum(delta < 0 for delta in deltas),
            "ties": sum(delta == 0 for delta in deltas),
            "mean_immediate_minus_delayed_bps": (
                round(sum(deltas) / len(deltas)) if deltas else None
            ),
        },
    }


_V3_CONDITIONS = (
    ("independent", "confirmation_independent_pass", "New eligible buyers rising"),
    ("clean", "confirmation_clean_pass", "New eligible buyers + low selling"),
    ("low_friction", "confirmation_low_friction_pass", "Low entry/exit drag"),
)


def _confirmation_receipt_metric(rows: list[dict[str, object]]) -> dict[str, object]:
    scored = [row for row in rows if row.get("score_status") == "SCORED"]
    returns = [
        int(row["terminal_return_bps"])
        for row in scored
        if row.get("terminal_return_bps") is not None
    ]
    profit_hits = sum(row.get("outcome_kind") == "PROFIT_FIRST" for row in scored)
    loss_hits = sum(row.get("outcome_kind") == "LOSS_FIRST" for row in scored)
    pending = sum(row.get("score_status") == "PENDING" for row in rows)
    invalid = len(rows) - len(scored) - pending
    return {
        "valid": len(scored),
        "completed": len(scored),
        "pending": pending,
        "invalid": invalid,
        "total": len(rows),
        "return_samples": len(returns),
        "sum_return_bps": sum(returns),
        "mean_return_bps": round(sum(returns) / len(returns)) if returns else None,
        "median_return_bps": round(median(returns)) if returns else None,
        "min_return_bps": min(returns) if returns else None,
        "max_return_bps": max(returns) if returns else None,
        "profit_hits": profit_hits,
        "losses": loss_hits,
        "time_exits": sum(row.get("outcome_kind") == "TIMEOUT" for row in scored),
        "profit_hit_rate_bps": (
            profit_hits * 10_000 // len(scored) if scored else None
        ),
    }


def _confirmation_microscope_v3(alerts: list[dict[str, object]]) -> dict[str, object]:
    """Evaluate one v3 receipt per launch; keep overlapping v2 arms as pilot only."""
    primary_rows = [
        row for row in alerts
        if row.get("rule_version") == "pump-confirmation-truth-v3"
    ]
    primary_scored = [row for row in primary_rows if row.get("score_status") == "SCORED"]
    primary = _confirmation_receipt_metric(primary_rows)

    conditions: list[dict[str, object]] = []
    condition_collecting = False
    blockers: list[str] = []
    for key, feature_key, label in _V3_CONDITIONS:
        pass_rows = [
            row for row in primary_rows
            if (row.get("features") or {}).get(feature_key) is True
        ]
        excluded_rows = [
            row for row in primary_rows
            if (row.get("features") or {}).get(feature_key) is False
        ]
        unavailable = len(primary_rows) - len(pass_rows) - len(excluded_rows)
        pass_metric = _confirmation_receipt_metric(pass_rows)
        excluded_metric = _confirmation_receipt_metric(excluded_rows)
        ready = int(pass_metric["valid"]) >= 25 and int(excluded_metric["valid"]) >= 25
        condition_collecting = condition_collecting or not ready
        pass_mean = pass_metric["mean_return_bps"]
        excluded_mean = excluded_metric["mean_return_bps"]
        pass_median = pass_metric["median_return_bps"]
        excluded_median = excluded_metric["median_return_bps"]
        pass_hit_rate = pass_metric["profit_hit_rate_bps"]
        excluded_hit_rate = excluded_metric["profit_hit_rate_bps"]
        mean_advantage = (
            int(pass_mean) - int(excluded_mean)
            if pass_mean is not None and excluded_mean is not None else None
        )
        median_advantage = (
            int(pass_median) - int(excluded_median)
            if pass_median is not None and excluded_median is not None else None
        )
        hit_rate_advantage = (
            int(pass_hit_rate) - int(excluded_hit_rate)
            if pass_hit_rate is not None and excluded_hit_rate is not None else None
        )
        if ready and (mean_advantage is None or mean_advantage <= 0):
            blockers.append(f"{key.upper()}_NOT_IMPROVED")
        conditions.append({
            "key": key,
            "label": label,
            "feature_key": feature_key,
            "pass": pass_metric,
            "excluded": excluded_metric,
            "unavailable": unavailable,
            "incremental_samples": int(excluded_metric["valid"]),
            "mean_advantage_bps": mean_advantage,
            "median_advantage_bps": median_advantage,
            "profit_hit_rate_advantage_bps": hit_rate_advantage,
            "checkpoint_status": "READY" if ready else "COLLECTING",
        })

    mode_groups: dict[str, list[dict[str, object]]] = {}
    for row in primary_rows:
        mode = (row.get("features") or {}).get("launch_mode")
        if mode:
            mode_groups.setdefault(str(mode), []).append(row)
    modes = []
    mode_collecting = len(mode_groups) < 2
    for mode, rows in sorted(mode_groups.items()):
        metric = _confirmation_receipt_metric(rows)
        ready = int(metric["valid"]) >= 25
        mode_collecting = mode_collecting or not ready
        if ready and int(metric["sum_return_bps"]) <= 0:
            blockers.append(f"MODE_{mode.upper()}_NOT_POSITIVE")
        modes.append({"mode": mode, **metric, "checkpoint_status": "READY" if ready else "COLLECTING"})

    creator_concentration = None
    if primary_scored:
        creator_gains: dict[str, int] = {}
        creator_coverage = True
        for row in primary_scored:
            creator = row.get("creator_id")
            if not creator:
                creator_coverage = False
                continue
            value = int(row.get("terminal_return_bps") or 0)
            if value > 0:
                creator_gains[str(creator)] = creator_gains.get(str(creator), 0) + value
        total_gain = sum(creator_gains.values())
        if creator_coverage:
            creator_concentration = (
                max(creator_gains.values(), default=0) * 10_000 // total_gain
                if total_gain > 0 else 0
            )

    valid = int(primary["valid"])
    checkpoint = max((value for value in (25, 50, 100) if valid >= value), default=None)
    if checkpoint is not None:
        if int(primary["sum_return_bps"]) <= 0:
            blockers.append("CUMULATIVE_RETURN_NOT_POSITIVE")
        if any(row["unavailable"] for row in conditions):
            blockers.append("CONDITION_LABEL_COVERAGE_INCOMPLETE")
        if creator_concentration is None:
            blockers.append("CREATOR_COVERAGE_INCOMPLETE")
        elif creator_concentration > 5_000:
            blockers.append("CREATOR_CONCENTRATION_OVER_50")

    immediate_by_launch = {
        str(row.get("launch_id")): int(row["terminal_return_bps"])
        for row in primary_scored if row.get("terminal_return_bps") is not None
    }
    delayed_by_launch = {
        str(row.get("launch_id")): int(row["terminal_return_bps"])
        for row in alerts
        if row.get("rule_version") == "pump-confirmation-delayed-v2"
        and row.get("score_status") == "SCORED"
        and row.get("terminal_return_bps") is not None
    }
    deltas = [
        immediate_by_launch[launch_id] - delayed_by_launch[launch_id]
        for launch_id in immediate_by_launch.keys() & delayed_by_launch.keys()
    ]

    if checkpoint is None:
        status = "COLLECTING"
    elif blockers:
        status = "EVIDENCE_BLOCKED"
    elif condition_collecting:
        status = "SEPARATION_COLLECTING"
    elif mode_collecting:
        status = "MODE_REPLICATION_COLLECTING"
    else:
        status = "PROMISING_DIRECTION"

    historical = _confirmation_microscope_v2_pilot(alerts)
    return {
        "status": status,
        "checkpoint": checkpoint,
        "blocker_codes": sorted(set(blockers)),
        "best_arm": "truth_v3" if primary_rows else None,
        "best_label": "Confirmation v3",
        "forward_primary": primary,
        "conditions": conditions,
        "modes": modes,
        "concentration": {"creator_max_gain_share_bps": creator_concentration},
        "paired_delay": {
            "pairs": len(deltas),
            "immediate_better": sum(delta > 0 for delta in deltas),
            "delayed_better": sum(delta < 0 for delta in deltas),
            "ties": sum(delta == 0 for delta in deltas),
            "mean_immediate_minus_delayed_bps": (
                round(sum(deltas) / len(deltas)) if deltas else None
            ),
            "median_immediate_minus_delayed_bps": (
                round(median(deltas)) if deltas else None
            ),
            "checkpoint_status": "READY" if len(deltas) >= 25 else "COLLECTING",
        },
        "historical_pilot": {
            "label": "Historical overlapping v2 pilot",
            "arms": historical["arms"],
        },
        # Compatibility for consumers that still render the immutable pilot rows.
        "arms": historical["arms"],
        "arm_overlap": historical["arm_overlap"],
    }


def _parse_alert_time(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


_BAND_SENSITIVITY_SCALES = (1, 2, 4)

# Anti-blitz DIAGNOSTIC split (2026-08-07, operator approved). The clean-universe scan
# (route-control-v2 standard, n=824 launches / 764 creators) found fast capital influx
# the strongest decision-time discriminator: capital_acceleration t=-5.9, velocity
# t=-5.2, LOCO-stable -- but it attenuates 2-4x in the second half and does NOT clear
# the bar inside v5-standard (t=-1.77). So it is published as a labelled split of the
# existing cohorts, never a gate and never a new arm. The threshold is the clean-
# universe MEDIAN velocity, frozen here so it can never be tuned on outcomes.
SLOW_CAPITAL_MAX_VELOCITY_ATOMIC_PER_S = 3_500_000_000


def _slow_capital_cells(rows: list[dict[str, object]]) -> dict[str, object]:
    """n and mean terminal return on each side of the frozen velocity threshold."""
    cells: dict[str, object] = {}
    for side, keep in (("slow", True), ("fast", False)):
        returns = []
        for row in rows:
            if row.get("score_status") != "SCORED" or row.get("terminal_return_bps") is None:
                continue
            velocity = (row.get("features") or {}).get("eligible_capital_velocity_atomic_per_s")
            if velocity is None:
                continue
            if (int(velocity) <= SLOW_CAPITAL_MAX_VELOCITY_ATOMIC_PER_S) is keep:
                returns.append(int(row["terminal_return_bps"]))
        cells[side] = {
            "n": len(returns),
            "mean_return_bps": round(sum(returns) / len(returns)) if returns else None,
        }
    return cells

# The scale the FUNDING GATE matches at. Promoted 1 -> 2 on 2026-08-07 (operator
# approved): at scale=1 the binding constraint was the decision-age band, yielding
# ~2-3 matched pairs/day and a 25-pair verdict 8-13 days out; scale=2 (decision-age
# +/-20s, progress +/-2,000 bps) was ALREADY a published band-sensitivity cell -- not
# a post-hoc widening -- and reaches 25 pairs in ~3 days. The arm predicate, outcome
# scoring, and comparability covariates are untouched; scale=1 stays published in
# `band_sensitivity` so narrowing remains visible.
GATE_MATCH_SCALE = 2


def _band_sensitivity(
    truth_rows: list[dict[str, object]],
    control_rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Publish the matched advantage against band width, never as one number.

    A point estimate from a narrow band that moves when the band widens is
    selection noise, not a result. Reporting it alone invites the reader to
    treat the preregistered cell as evidence before it is.
    """
    rows: list[dict[str, object]] = []
    for scale in _BAND_SENSITIVITY_SCALES:
        pairs, _unused = _match_v4_controls(truth_rows, control_rows, scale=scale)
        deltas = [
            int(truth["terminal_return_bps"]) - int(control["terminal_return_bps"])
            for truth, control in pairs
        ]
        rows.append(
            {
                "scale": scale,
                "max_decision_age_delta_ms": 10_000 * scale,
                "max_progress_delta_bps": 1_000 * scale,
                "pairs": len(pairs),
                "mean_arm_minus_control_bps": (
                    round(sum(deltas) / len(deltas)) if deltas else None
                ),
            }
        )
    return rows


def _match_v4_controls(
    truth_rows: list[dict[str, object]],
    control_rows: list[dict[str, object]],
    *,
    scale: int = 1,
) -> tuple[list[tuple[dict[str, object], dict[str, object]]], int]:
    """Greedy one-to-one matching inside fixed, preregistered comparability bands.

    `scale` widens only the age and progress tolerances. The funding gate matches at
    `GATE_MATCH_SCALE` (=2, promoted 2026-08-07 from the published sensitivity cell);
    the band-sensitivity diagnostic still reports scales 1/2/4 so narrowing stays
    visible.
    """
    max_age_delta = 10_000 * int(scale)
    max_progress_delta = 1_000 * int(scale)
    available = {
        str(row.get("alert_id")): row
        for row in control_rows
        if row.get("score_status") == "SCORED"
        and row.get("terminal_return_bps") is not None
    }
    pairs: list[tuple[dict[str, object], dict[str, object]]] = []
    ordered_truth = sorted(
        (
            row for row in truth_rows
            if row.get("score_status") == "SCORED"
            and row.get("terminal_return_bps") is not None
        ),
        key=lambda row: (str(row.get("observed_at") or ""), str(row.get("alert_id") or "")),
    )
    for truth in ordered_truth:
        tf = truth.get("features") or {}
        truth_time = _parse_alert_time(truth.get("observed_at"))
        candidates: list[tuple[int, str, dict[str, object]]] = []
        for control_id, control in available.items():
            if str(control.get("launch_id")) == str(truth.get("launch_id")):
                continue
            cf = control.get("features") or {}
            control_time = _parse_alert_time(control.get("observed_at"))
            if (
                truth_time is None
                or control_time is None
                or tf.get("launch_mode") != cf.get("launch_mode")
                or tf.get("quote_mint") != cf.get("quote_mint")
            ):
                continue
            try:
                time_ms = abs(int((truth_time - control_time).total_seconds() * 1_000))
                age_delta = abs(int(tf["decision_age_ms"]) - int(cf["decision_age_ms"]))
                progress_delta = abs(int(tf["progress_bps"]) - int(cf["progress_bps"]))
                cost_delta = abs(
                    int(tf["modeled_round_trip_cost_bps"])
                    - int(cf["modeled_round_trip_cost_bps"])
                )
            except (KeyError, TypeError, ValueError):
                continue
            if (
                time_ms > 3_600_000
                or age_delta > max_age_delta
                or progress_delta > max_progress_delta
                or cost_delta > 100
            ):
                continue
            distance = time_ms // 1_000 + age_delta + progress_delta * 10 + cost_delta * 100
            candidates.append((distance, control_id, control))
        if not candidates:
            continue
        _distance, control_id, control = min(candidates, key=lambda item: (item[0], item[1]))
        pairs.append((truth, control))
        available.pop(control_id)
    return pairs, len(available)


def _creator_cluster_ci95(
    pairs: list[tuple[dict[str, object], dict[str, object]]],
    *,
    resamples: int = 1_000,
) -> list[int] | None:
    clusters: dict[str, list[int]] = {}
    for truth, control in pairs:
        creator = truth.get("creator_id")
        if not creator:
            return None
        clusters.setdefault(str(creator), []).append(
            int(truth["terminal_return_bps"]) - int(control["terminal_return_bps"])
        )
    if not clusters:
        return None
    values = list(clusters.values())
    rng = random.Random(0x5241444152)
    means: list[float] = []
    for _ in range(resamples):
        sample = [values[rng.randrange(len(values))] for _ in values]
        flattened = [delta for cluster in sample for delta in cluster]
        means.append(sum(flattened) / len(flattened))
    means.sort()
    return [
        round(means[int(0.025 * (len(means) - 1))]),
        round(means[int(0.975 * (len(means) - 1))]),
    ]


def _gain_concentration_bps(
    rows: list[dict[str, object]], field,
) -> int | None:
    grouped: dict[str, int] = {}
    for row in rows:
        key = field(row)
        if not key:
            return None
        gain = max(0, int(row.get("terminal_return_bps") or 0))
        grouped[str(key)] = grouped.get(str(key), 0) + gain
    total = sum(grouped.values())
    return max(grouped.values(), default=0) * 10_000 // total if total else 0


def _dominant_gain_group(rows: list[dict[str, object]], field) -> str | None:
    grouped: dict[str, int] = {}
    for row in rows:
        key = field(row)
        if not key:
            return None
        grouped[str(key)] = grouped.get(str(key), 0) + max(
            0, int(row.get("terminal_return_bps") or 0)
        )
    return max(grouped, key=lambda key: (grouped[key], key), default=None)


def _leave_one_creator_out_min_bps(
    pairs: list[tuple[dict[str, object], dict[str, object]]],
) -> int | None:
    by_creator: dict[str, list[int]] = {}
    for truth, control in pairs:
        creator = truth.get("creator_id")
        if not creator:
            return None
        by_creator.setdefault(str(creator), []).append(
            int(truth["terminal_return_bps"]) - int(control["terminal_return_bps"])
        )
    if len(by_creator) < 2:
        return None
    results = []
    for omitted in by_creator:
        remaining = [
            delta
            for creator, deltas in by_creator.items()
            if creator != omitted
            for delta in deltas
        ]
        if not remaining:
            return None
        results.append(round(sum(remaining) / len(remaining)))
    return min(results)


def _launch_mode_cost(alerts: list[dict[str, object]]) -> dict[str, object]:
    """Round-trip cost per launch mode, and whether it is knowable before entry.

    Cost is the binding constraint: the median launch returns roughly the fee and
    nothing else. So a cheaper fee tier is the only improvement that needs no
    prediction at all -- if it is identifiable at decision time.

    The tier is identifiable. Across 717 v4 alerts the mapping was exact with no
    exceptions: cashback -> 95 bps fee -> -191 MODELED cost; standard and mayhem ->
    125 -> -250. The flag arrives on the launch event, before entry.

    RETRACTED 2026-08-07 -- the cheaper tier is not cheaper. `modeled_cost_bps` below is
    the local curve model's number, and the model is not the route. Over 5,026 alerts
    that carry both the modeled cost and the MEASURED `observed_pair_return_bps` from two
    live Jupiter quotes at the same instant:

        mode      modeled   measured (median)   error
        standard  -250      -448                -198
        cashback  -191      -456                -265
        mayhem    -250      -1,816              -1,566

    Cashback is 8 bps WORSE to round trip than standard, not 59 bps better, and its model
    error is 34% larger. The fee discount does not reach the taker. Do not restate the
    53 bps median saving; it was a difference between two model outputs.

    `mayhem` remains the largest effect and the sign is unchanged, but the cause is now
    identified: the model misprices its round trip by 1,566 bps.

    v6 acts on the measurement -- `launch_mode == "standard"` -- not on the fee tier.
    """
    scored = [
        row for row in alerts
        if is_forward_measurement(row)
        and row.get("score_status") == "SCORED"
        and row.get("terminal_return_bps") is not None
        and (row.get("features") or {}).get("launch_mode")
    ]
    grouped: dict[str, list[dict[str, object]]] = {}
    for row in scored:
        grouped.setdefault(str((row["features"] or {})["launch_mode"]), []).append(row)
    modes = []
    for mode, rows in sorted(grouped.items()):
        returns = sorted(int(row["terminal_return_bps"]) for row in rows)
        costs = sorted({
            int(f["modeled_round_trip_cost_bps"])
            for f in ((row.get("features") or {}) for row in rows)
            if f.get("modeled_round_trip_cost_bps") is not None
        })
        fees = sorted({
            int(f["effective_fee_bps"])
            for f in ((row.get("features") or {}) for row in rows)
            if f.get("effective_fee_bps") is not None
        })
        modes.append(
            {
                "launch_mode": mode,
                "alerts": len(rows),
                "effective_fee_bps": fees,
                "modeled_cost_bps": costs,
                "cost_deterministic": len(costs) == 1,
                "mean_return_bps": round(sum(returns) / len(returns)),
                "median_return_bps": round(median(returns)),
                "p10_return_bps": returns[len(returns) // 10] if returns else None,
            }
        )
    priced = [row for row in modes if row["modeled_cost_bps"]]
    cheapest = max(priced, key=lambda row: row["modeled_cost_bps"][0]) if priced else None
    spread = None
    if priced:
        best = max(row["modeled_cost_bps"][0] for row in priced)
        worst = min(row["modeled_cost_bps"][0] for row in priced)
        spread = best - worst
    return {
        "modes": modes,
        "cheapest_mode": cheapest["launch_mode"] if cheapest else None,
        "cost_spread_bps": spread,
        "note": (
            "Mode arrives on the launch event, so the fee tier is known before entry. "
            "The saving is real at the median and ~0 in the mean. Diagnostic only -- no "
            "rule filters on mode."
        ),
    }



def _outcome_resolved_at(row: Mapping[str, object]):
    """When this alert's outcome actually became known."""
    started = _parse_alert_time(row.get("entry_effective_at") or row.get("observed_at"))
    elapsed = row.get("outcome_elapsed_ms")
    if started is None or elapsed is None:
        return None
    try:
        return started + timedelta(milliseconds=int(elapsed))
    except (TypeError, ValueError):
        return None


def _creator_prior(alerts: list[dict[str, object]]) -> dict[str, object]:
    """Does a creator's already-known record predict the next launch? Causal.

    An earlier ALERT is not an earlier RESULT. A prior alert's outcome resolves up to
    60 seconds after it fires (300 for candidates), so counting it at a decision that
    happened before it resolved uses information that did not exist yet.

    That leak was the finding. Ordering only by alert time gave +1,530 bps separation
    at t=+4.11 across 294 pairs; requiring the prior outcome to have RESOLVED before
    the decision leaves 191 pairs and **+392 bps at t=+0.70** -- no effect. 103 of 294
    pairs, over a third, were built on outcomes that had not happened yet.

    Reported as a diagnostic. The forward arm's own store is causal by construction
    (credited when an outcome finalizes), so the arm is an untested hypothesis, not an
    evidence-backed one.
    """
    scored = [
        row for row in alerts
        if is_forward_measurement(row)
        and row.get("score_status") == "SCORED"
        and row.get("terminal_return_bps") is not None
        and row.get("creator_id")
        and row.get("observed_at")
    ]
    scored.sort(key=lambda row: str(row.get("observed_at")))
    hour_returns: dict[str, list[int]] = {}
    for row in scored:
        hour_returns.setdefault(str(row["observed_at"])[:13], []).append(
            int(row["terminal_return_bps"])
        )
    hour_mean = {
        hour: sum(values) / len(values) for hour, values in hour_returns.items()
    }
    history: dict[str, list[dict[str, object]]] = {}
    pairs: list[dict[str, object]] = []
    excluded = 0
    for row in scored:
        creator = str(row["creator_id"])
        realized = int(row["terminal_return_bps"])
        decided_at = _parse_alert_time(row.get("observed_at"))
        resolved_at = _outcome_resolved_at(row)
        seen = history.get(creator) or []
        known = [
            item for item in seen
            if decided_at is not None
            and item["resolved_at"] is not None
            and item["resolved_at"] <= decided_at
        ]
        excluded += len(seen) - len(known)
        if known:
            pairs.append(
                {
                    "creator": creator,
                    "prior_mean_bps": sum(item["realized"] for item in known) / len(known),
                    "realized_bps": realized,
                    "adjusted_bps": realized - hour_mean[str(row["observed_at"])[:13]],
                }
            )
        history.setdefault(creator, []).append(
            {"realized": realized, "resolved_at": resolved_at}
        )

    def side(predicate, key):
        values = [row[key] for row in pairs if predicate(row["prior_mean_bps"])]
        return {
            "n": len(values),
            "mean_bps": round(sum(values) / len(values)) if values else None,
        }

    positive = side(lambda prior: prior > 0, "realized_bps")
    negative = side(lambda prior: prior <= 0, "realized_bps")
    positive_adj = side(lambda prior: prior > 0, "adjusted_bps")
    negative_adj = side(lambda prior: prior <= 0, "adjusted_bps")
    separation = (
        positive["mean_bps"] - negative["mean_bps"]
        if positive["mean_bps"] is not None and negative["mean_bps"] is not None
        else None
    )
    adjusted = (
        positive_adj["mean_bps"] - negative_adj["mean_bps"]
        if positive_adj["mean_bps"] is not None and negative_adj["mean_bps"] is not None
        else None
    )
    return {
        "pairs": len(pairs),
        "unresolved_priors_excluded": excluded,
        "distinct_creators": len({row["creator"] for row in pairs}),
        "positive_prior": positive,
        "negative_prior": negative,
        "separation_bps": separation,
        "market_adjusted_separation_bps": adjusted,
        "note": (
            "Causal: a prior counts only once its own outcome had RESOLVED before this "
            "decision. Ordering by alert time alone leaked future results and inflated "
            "the separation roughly fourfold. Adjusted removes each hour's own mean, "
            "which is a contemporaneous baseline rather than a tradeable one. "
            "Diagnostic only -- no rule reads this."
        ),
    }


def _decision_latency(alerts: list[dict[str, object]]) -> dict[str, object]:
    """Split decision age into the part a rule chose to wait for and the part that is plumbing.

    Measured on the v4 cohort: 9,725 ms total, of which 2,588 ms (26.6%) is the gap
    between the state acted on and the decision, and that gap is almost entirely the
    remote quote round trip (corr +0.83 with `quote_latency_ms`). The other 73.4% is
    the arm waiting for its own age and curve-progress bands to qualify.

    Neither share is free to cut. The quote is what verifies a tradeable route and it
    rejects most candidates, and given that a bonding-curve exit cannot be placed at a
    chosen price (see `_exit_realism`), an unverified route is the last thing to assume.
    The rule-wait share is a strategy choice, not latency.
    """
    forward = [
        (row.get("features") or {}) for row in alerts if is_forward_measurement(row)
    ]
    def series(key):
        return sorted(
            int(f[key]) for f in forward if f.get(key) is not None
        )
    ages, engineering, quotes = series("decision_age_ms"), series("chain_state_age_ms"), series("quote_latency_ms")
    if not ages or not engineering:
        return {"samples": 0}
    median_age = round(median(ages))
    median_engineering = round(median(engineering))
    return {
        "samples": len(ages),
        "median_decision_age_ms": median_age,
        "median_engineering_ms": median_engineering,
        "median_rule_wait_ms": median_age - median_engineering,
        "engineering_share_bps": (
            median_engineering * 10_000 // median_age if median_age else None
        ),
        "median_quote_latency_ms": round(median(quotes)) if quotes else None,
        "note": (
            "Engineering share is the remote quote round trip, which verifies a "
            "tradeable route and rejects most candidates. The rule-wait share is the "
            "arm waiting for its own entry bands, which is strategy, not latency."
        ),
    }


def _exit_realism(alerts: list[dict[str, object]]) -> dict[str, object]:
    """How far the realised exit lands from the barrier that was supposed to stop it.

    A bonding curve only reprices on a trade, so an exit cannot be placed at a
    chosen level — it happens at the next trade, wherever that is. On the v4
    cohort a -800 bps stop realised a median near -2600. That is not slippage
    around a stop; it means there is no stop. Derived from every scored forward
    alert so the number is read, not rediscovered.
    """
    scored = [
        row for row in alerts
        if is_forward_measurement(row)
        and row.get("score_status") == "SCORED"
        and row.get("terminal_return_bps") is not None
    ]
    losses = [
        int(row["terminal_return_bps"]) for row in scored
        if str(row.get("outcome_kind") or "").upper() == "LOSS_FIRST"
    ]
    profits = [
        int(row["terminal_return_bps"]) for row in scored
        if str(row.get("outcome_kind") or "").upper() == "PROFIT_FIRST"
    ]
    median_loss = round(median(losses)) if losses else None
    median_profit = round(median(profits)) if profits else None
    return {
        "loss_barrier_bps": LOSS_BARRIER_BPS,
        "profit_barrier_bps": HEADLINE_TARGET_BPS,
        "scored_alerts": len(scored),
        "loss_samples": len(losses),
        "profit_samples": len(profits),
        "median_realized_loss_bps": median_loss,
        "median_realized_profit_bps": median_profit,
        # Negative means the realised loss ran past the barrier.
        "loss_overshoot_bps": (
            median_loss - LOSS_BARRIER_BPS if median_loss is not None else None
        ),
        "note": (
            "A bonding curve reprices only on a trade, so the exit lands at the next "
            "trade rather than at the barrier. Treat the barrier as a label, not a stop."
        ),
    }


def _confirmation_microscope_v4(
    alerts: list[dict[str, object]],
    *,
    gate_alerts: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    # The forward confirmation arm. v4 stopped firing when v5 replaced it; its rows stay
    # readable but are no longer the thing being judged, and the clock restarts at zero.
    # The arm identity is named once, here: the reporter must never publish a version
    # label that differs from the rows it actually selected.
    gate_rows = alerts if gate_alerts is None else gate_alerts
    truth_rows = [
        row for row in gate_rows
        if row.get("rule_version") == FORWARD_CONFIRMATION_RULE
    ]
    control_rows = [
        row for row in gate_rows if row.get("rule_version") == "route-control-v2"
    ]
    primary = _confirmation_receipt_metric(truth_rows)
    control = _confirmation_receipt_metric(control_rows)
    pairs, unused_controls = _match_v4_controls(
        truth_rows, control_rows, scale=GATE_MATCH_SCALE)
    band_sensitivity = _band_sensitivity(truth_rows, control_rows)
    deltas = [
        int(truth["terminal_return_bps"]) - int(control_row["terminal_return_bps"])
        for truth, control_row in pairs
    ]
    ci95 = _creator_cluster_ci95(pairs)
    truth_scored = [
        row for row in truth_rows
        if row.get("score_status") == "SCORED"
        and row.get("terminal_return_bps") is not None
    ]
    creator_concentration = _gain_concentration_bps(
        truth_scored, lambda row: row.get("creator_id")
    )
    mode_concentration = _gain_concentration_bps(
        truth_scored, lambda row: (row.get("features") or {}).get("launch_mode")
    )
    dominant_mode = _dominant_gain_group(
        truth_scored, lambda row: (row.get("features") or {}).get("launch_mode")
    )
    mode_pairs: dict[str, list[tuple[dict[str, object], dict[str, object]]]] = {}
    for truth, control_row in pairs:
        mode = (truth.get("features") or {}).get("launch_mode")
        if mode:
            mode_pairs.setdefault(str(mode), []).append((truth, control_row))
    mode_evidence = []
    for mode, grouped_pairs in sorted(mode_pairs.items()):
        grouped_deltas = [
            int(truth["terminal_return_bps"])
            - int(control_row["terminal_return_bps"])
            for truth, control_row in grouped_pairs
        ]
        mode_evidence.append(
            {
                "mode": mode,
                "pairs": len(grouped_pairs),
                "mean_arm_minus_control_bps": round(
                    sum(grouped_deltas) / len(grouped_deltas)
                ),
                "creator_cluster_ci95_bps": _creator_cluster_ci95(grouped_pairs),
            }
        )
    dominant_evidence = next(
        (row for row in mode_evidence if row["mode"] == dominant_mode), None
    )
    mode_specific_ready = bool(
        mode_concentration is not None
        and mode_concentration > 5_000
        and dominant_evidence is not None
        and int(dominant_evidence["pairs"]) >= 25
        and dominant_evidence["creator_cluster_ci95_bps"] is not None
        and int(dominant_evidence["creator_cluster_ci95_bps"][0]) > 0
    )
    leave_one_creator_out_min = _leave_one_creator_out_min_bps(pairs)
    blockers: list[str] = []
    if len(pairs) < 25:
        blockers.append("MATCHED_CONTROL_BELOW_25")
    else:
        if int(primary["sum_return_bps"]) <= 0:
            blockers.append("CUMULATIVE_RETURN_NOT_POSITIVE")
        if not deltas or sum(deltas) <= 0:
            blockers.append("MATCHED_CONTROL_NOT_IMPROVED")
        if ci95 is None:
            blockers.append("CREATOR_CLUSTER_COVERAGE_INCOMPLETE")
        elif ci95[0] <= 0:
            blockers.append("MATCHED_EV_INTERVAL_CROSSES_ZERO")
        if creator_concentration is None:
            blockers.append("CREATOR_COVERAGE_INCOMPLETE")
        elif creator_concentration > 5_000 and (
            leave_one_creator_out_min is None or leave_one_creator_out_min <= 0
        ):
            blockers.append("CREATOR_CONCENTRATION_OVER_50")
        if mode_concentration is None:
            blockers.append("MODE_COVERAGE_INCOMPLETE")
        elif mode_concentration > 5_000 and not mode_specific_ready:
            blockers.append("MODE_CONCENTRATION_OVER_50")
    # Report-only mirror of the conditions evaluated above. `/25` is the first gate, not
    # the only one, and a board that shows only the pair count reads as "two pairs away"
    # when other prerequisites are also unmet. Thresholds are read from the same values
    # the gate uses -- nothing here changes what passes.
    prerequisites = [
        {
            "code": "MATCHED_PAIRS",
            "label": "Matched pairs",
            "value": len(pairs),
            "threshold": 25,
            "satisfied": len(pairs) >= 25,
        },
        {
            "code": "CUMULATIVE_RETURN_POSITIVE",
            "label": "Cumulative arm return positive",
            "value": int(primary["sum_return_bps"]),
            "threshold": 0,
            "satisfied": int(primary["sum_return_bps"]) > 0,
        },
        {
            "code": "MATCHED_ADVANTAGE_POSITIVE",
            "label": "Matched advantage positive",
            "value": sum(deltas) if deltas else None,
            "threshold": 0,
            "satisfied": bool(deltas) and sum(deltas) > 0,
        },
        {
            "code": "CREATOR_INTERVAL_ABOVE_ZERO",
            "label": "Creator-clustered 95% lower bound above zero",
            "value": ci95[0] if ci95 else None,
            "threshold": 0,
            "satisfied": ci95 is not None and ci95[0] > 0,
        },
        {
            "code": "CREATOR_CONCENTRATION",
            "label": "Creator gain share at or below 50% (or leave-one-out positive)",
            "value": creator_concentration,
            "threshold": 5_000,
            "satisfied": creator_concentration is not None and (
                creator_concentration <= 5_000
                or (leave_one_creator_out_min is not None and leave_one_creator_out_min > 0)
            ),
        },
        {
            "code": "MODE_CONCENTRATION",
            "label": "Mode gain share at or below 50% (or dominant mode independently proven)",
            "value": mode_concentration,
            "threshold": 5_000,
            "satisfied": mode_concentration is not None and (
                mode_concentration <= 5_000 or mode_specific_ready
            ),
        },
    ]
    if len(pairs) < 25:
        status = "INSUFFICIENT_MATCHED_CONTROL"
    elif mode_specific_ready and not blockers:
        status = "MODE_SPECIFIC_DIRECTION"
    elif any("CONCENTRATION" in blocker for blocker in blockers):
        status = "CONCENTRATION_BLOCKED"
    elif blockers:
        status = "EVIDENCE_BLOCKED"
    else:
        status = "PROMISING_DIRECTION"
    historical = _confirmation_microscope_v3(alerts)
    return {
        "status": status,
        "outcome_source": LIFECYCLE_VERSION,
        "checkpoint": max(
            (value for value in (25, 50, 100) if len(pairs) >= value), default=None
        ),
        "blocker_codes": blockers,
        "best_arm": (
            FORWARD_CONFIRMATION_ARM if status == "PROMISING_DIRECTION"
            else f"{FORWARD_CONFIRMATION_ARM}:{dominant_mode}"
            if status == "MODE_SPECIFIC_DIRECTION"
            else None
        ),
        "best_label": FORWARD_CONFIRMATION_LABEL if truth_rows else None,
        "forward_rule_version": FORWARD_CONFIRMATION_RULE,
        "gate_prerequisites": prerequisites,
        "gate_prerequisites_unmet": [
            item["code"] for item in prerequisites if not item["satisfied"]
        ],
        "forward_primary": primary,
        "control": control,
        "matched_control": {
            "pairs": len(pairs),
            "unmatched_truth": max(0, int(primary["valid"]) - len(pairs)),
            "unmatched_control": unused_controls,
            "mean_arm_minus_control_bps": (
                round(sum(deltas) / len(deltas)) if deltas else None
            ),
            "median_arm_minus_control_bps": (
                round(median(deltas)) if deltas else None
            ),
            "creator_cluster_ci95_bps": ci95,
            "leave_one_creator_out_min_bps": leave_one_creator_out_min,
            "band_sensitivity": band_sensitivity,
            "slow_capital_split": {
                "threshold_atomic_per_s": SLOW_CAPITAL_MAX_VELOCITY_ATOMIC_PER_S,
                "truth": _slow_capital_cells(truth_rows),
                "control": _slow_capital_cells(control_rows),
            },
        },
        "concentration": {
            "creator_max_gain_share_bps": creator_concentration,
            "mode_max_gain_share_bps": mode_concentration,
        },
        "conditions": [],
        "modes": mode_evidence,
        "paired_delay": historical["paired_delay"],
        "historical_pilot": {
            "label": "Withdrawn pre-v4 measurement pilots",
            "v3": historical,
            "arms": historical["historical_pilot"]["arms"],
        },
        "arms": historical["arms"],
        "arm_overlap": historical["arm_overlap"],
    }


def _confirmation_microscope(
    alerts: list[dict[str, object]],
    *,
    gate_alerts: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return _confirmation_microscope_v4(alerts, gate_alerts=gate_alerts)


def _metric(rows: list[dict[str, object]]) -> dict[str, int | None]:
    returns = [int(row["return_bps"]) for row in rows if row["return_bps"] is not None]
    profit = sum(row["outcome_kind"] == "PROFIT_FIRST" for row in rows)
    return {
        "launch_samples": len(rows),
        "profit_rate_bps": (profit * 10_000 // len(rows)) if rows else None,
        "avg_return_bps": (round(sum(returns) / len(returns))) if returns else None,
    }


def _diagnostic_receipts(hot: sqlite3.Connection) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    rows = hot.execute(
        "SELECT x.entry_id,x.elapsed_ms,x.exit_kind,x.net_return_bps,x.source_healthy,x.migration,"
        "e.entry_observed_at,lm.launch_id,l.venue_id,l.token_id,o.outcome_kind "
        "FROM op_executable_observations x "
        "JOIN op_counterfactual_entries e ON e.entry_id=x.entry_id "
        "JOIN op_landmark_states lm ON lm.landmark_id=e.landmark_id "
        "JOIN op_launches l ON l.launch_id=lm.launch_id "
        "LEFT JOIN op_competing_outcomes o ON o.entry_id=e.entry_id "
        "ORDER BY lm.launch_id,e.entry_observed_at,e.entry_id,x.elapsed_ms,x.observation_id"
    ).fetchall()
    entries: dict[str, dict[str, object]] = {}
    for row in rows:
        item = entries.setdefault(
            str(row["entry_id"]),
            {
                "entry_id": str(row["entry_id"]),
                "entry_observed_at": str(row["entry_observed_at"]),
                "launch_id": str(row["launch_id"]),
                "venue_id": str(row["venue_id"]),
                "token_id": str(row["token_id"]),
                "outcome_kind": str(row["outcome_kind"] or "pending").upper(),
                "points": [],
            },
        )
        item["points"].append(
            ExecutablePoint(
                int(row["elapsed_ms"]),
                str(row["exit_kind"]),
                int(row["net_return_bps"]) if row["net_return_bps"] is not None else None,
                bool(row["source_healthy"]),
                bool(row["migration"]),
            )
        )
    # One counterfactual path per launch prevents high-event launches from
    # dominating the exploratory rates.
    by_launch: dict[str, dict[str, object]] = {}
    for entry in entries.values():
        current = by_launch.get(str(entry["launch_id"]))
        if current is None or (entry["entry_observed_at"], entry["entry_id"]) < (
            current["entry_observed_at"], current["entry_id"]
        ):
            by_launch[str(entry["launch_id"])] = entry

    # evaluator_version is part of EVERY cell key (audit 2026-08-07): dropping it pooled
    # v1/v2/v3 rows into one cell, which is exactly the cross-version pooling the ladder
    # version bump exists to prevent.
    cells: dict[tuple[str, int, int, str], dict[str, object]] = {}
    near: list[dict[str, object]] = []
    for entry in by_launch.values():
        points = tuple(entry["points"])
        for result in evaluate_target_ladder(points):
            key = (
                str(entry["venue_id"]),
                int(result["target_bps"]),
                int(result["horizon_ms"]),
                str(result["evaluator_version"]),
            )
            cell = cells.setdefault(
                key,
                {
                    "venue_id": key[0],
                    "target_bps": key[1],
                    "horizon_ms": key[2],
                    "evaluator_version": key[3],
                    "launch_samples": 0,
                    "hits": 0,
                    "losses": 0,
                    "exit_failures": 0,
                    "migrations": 0,
                    "data_gaps": 0,
                },
            )
            cell["launch_samples"] += 1
            cell["hits"] += int(result["result"] == "hit")
            cell["losses"] += int(result["result"] == "loss")
            cell["exit_failures"] += int(result["result"] == "exit_failure")
            cell["migrations"] += int(result["result"] == "migration")
            cell["data_gaps"] += int(result["result"] == "data_gap")
        miss = near_miss(points)
        if miss is not None:
            near.append(
                {
                    "launch_id": entry["launch_id"],
                    "venue_id": entry["venue_id"],
                    "token_id": entry["token_id"],
                    "outcome_kind": entry["outcome_kind"],
                    **miss,
                }
            )
    ladder = []
    for cell in cells.values():
        cell["hit_rate_bps"] = cell["hits"] * 10_000 // cell["launch_samples"]
        ladder.append(cell)
    ladder.sort(key=lambda row: (
        row["venue_id"], row["target_bps"], row["horizon_ms"], row["evaluator_version"]))
    near.sort(key=lambda row: (-row["best_return_bps"], row["time_to_best_ms"], row["launch_id"]))
    return ladder, near[:20]


def _historical_diagnostics(historical: sqlite3.Connection | None):
    if historical is None:
        return [], []
    cells: dict[tuple[str, int, int], dict[str, object]] = {}
    for row in historical.execute("SELECT * FROM op_insight_target_daily").fetchall():
        if (
            row["evaluator_version"] == "target-ladder-v1"
            and row["result"] == "timeout"
        ):
            continue
        key = (str(row["venue_id"]), int(row["target_bps"]), int(row["horizon_ms"]),
               str(row["evaluator_version"]))
        cell = cells.setdefault(
            key,
            {
                "venue_id": key[0], "target_bps": key[1], "horizon_ms": key[2],
                "evaluator_version": key[3],
                "launch_samples": 0, "hits": 0, "losses": 0,
                "exit_failures": 0, "migrations": 0, "data_gaps": 0,
            },
        )
        count = int(row["sample_count"])
        cell["launch_samples"] += count
        cell["hits"] += count * int(row["result"] == "hit")
        cell["losses"] += count * int(row["result"] == "loss")
        cell["exit_failures"] += count * int(row["result"] == "exit_failure")
        cell["migrations"] += count * int(row["result"] == "migration")
        cell["data_gaps"] += count * int(row["result"] == "data_gap")
    ladder = list(cells.values())
    for cell in ladder:
        cell["hit_rate_bps"] = cell["hits"] * 10_000 // cell["launch_samples"]
    near = [dict(row) for row in historical.execute(
        "SELECT launch_id,venue_id,token_id,outcome_kind,best_return_bps,time_to_best_ms "
        "FROM op_insight_near_misses ORDER BY best_return_bps DESC,time_to_best_ms LIMIT 20"
    ).fetchall()]
    return ladder, near


def _merge_diagnostics(live_ladder, live_near, historical):
    historical_ladder, historical_near = _historical_diagnostics(historical)
    cells = {}
    for row in [*historical_ladder, *live_ladder]:
        key = (row["venue_id"], row["target_bps"], row["horizon_ms"],
               row["evaluator_version"])
        cell = cells.setdefault(
            key,
            {**row, "launch_samples": 0, "hits": 0, "losses": 0,
             "exit_failures": 0, "migrations": 0, "data_gaps": 0},
        )
        for field in ("launch_samples", "hits", "losses", "exit_failures", "migrations", "data_gaps"):
            cell[field] += int(row.get(field) or 0)
    ladder = list(cells.values())
    for cell in ladder:
        cell["hit_rate_bps"] = cell["hits"] * 10_000 // cell["launch_samples"]
    ladder.sort(key=lambda row: (
        row["venue_id"], row["target_bps"], row["horizon_ms"], row["evaluator_version"]))
    near_by_launch = {row["launch_id"]: row for row in [*historical_near, *live_near]}
    near = sorted(
        near_by_launch.values(),
        key=lambda row: (-row["best_return_bps"], row["time_to_best_ms"], row["launch_id"]),
    )[:20]
    return ladder, near


def _merged_alert_evidence(
    hot: sqlite3.Connection, historical: sqlite3.Connection | None
) -> list[dict[str, object]]:
    """One merge of hot + durable alert evidence, shared by the report and the sealer."""
    alert_by_id = {
        str(row["alert_id"]): row for row in _alert_evidence_historical(historical)
    }
    for row in _alert_evidence_hot(hot):
        alert_id = str(row["alert_id"])
        retained = alert_by_id.get(alert_id)
        if retained is None or retained.get("score_status") != "SCORED":
            alert_by_id[alert_id] = row
    return sorted(
        alert_by_id.values(),
        key=lambda row: (str(row["observed_at"]), str(row["alert_id"])),
        reverse=True,
    )


_REALISTIC_OUTCOME_KIND = {
    "target_first": "PROFIT_FIRST",
    "stop_first": "LOSS_FIRST",
    "time_exit": "TIMEOUT",
}


def _realistic_gate_evidence(
    hot: sqlite3.Connection,
    alerts: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Attach valid realistic outcomes to the frozen alert facts."""
    outcomes = {
        str(row["alert_id"]): row
        for row in hot.execute(
            "SELECT r.alert_id,r.rule_version,o.result_code,o.return_bps "
            "FROM op_paper_outcomes o "
            "JOIN op_paper_fill_requests r USING(request_id) "
            "WHERE o.lifecycle_version=? AND o.status='valid' "
            "AND r.rule_version IN (?,?) "
            "GROUP BY r.alert_id HAVING COUNT(*)=1",
            (LIFECYCLE_VERSION, FORWARD_CONFIRMATION_RULE, "route-control-v2"),
        ).fetchall()
    }
    evidence = []
    for alert in alerts:
        outcome = outcomes.get(str(alert["alert_id"]))
        if outcome is None or outcome["rule_version"] != alert.get("rule_version"):
            continue
        result_code = str(outcome["result_code"])
        outcome_kind = _REALISTIC_OUTCOME_KIND.get(result_code)
        if outcome_kind is None or outcome["return_bps"] is None:
            continue
        evidence.append({
            **alert,
            "score_status": "SCORED",
            "outcome_kind": outcome_kind,
            "terminal_return_bps": int(outcome["return_bps"]),
        })
    return evidence


GATE_CHECKPOINTS = (25, 50, 100)


def seal_gate_checkpoints(
    hot: sqlite3.Connection,
    insights: sqlite3.Connection,
    *,
    now_iso: str | None = None,
) -> int:
    """Seal an immutable receipt the FIRST time the gate's matched-pair count crosses
    each preregistered checkpoint (audit 2026-08-07 finding 5).

    The funding contract promises verdicts at 25/50/100 pairs, but the live pair set
    moves with every new alert and with retention -- a verdict computed later over
    "whatever the set is now" cannot prove what the gate saw at the crossing. The
    receipt freezes the pair identities (manifest + sha256) and the matched statistics
    at seal time. INSERT OR IGNORE on the primary key makes the first seal the only
    seal; nothing updates or deletes these rows.
    """
    now_iso = now_iso or real_utc_now_iso()
    alerts = _merged_alert_evidence(hot, insights)
    gate_alerts = _realistic_gate_evidence(hot, alerts)
    truth_rows = [
        row for row in gate_alerts
        if row.get("rule_version") == FORWARD_CONFIRMATION_RULE
    ]
    control_rows = [
        row for row in gate_alerts if row.get("rule_version") == "route-control-v2"
    ]
    pairs, _unused = _match_v4_controls(truth_rows, control_rows, scale=GATE_MATCH_SCALE)
    sealed = 0
    insights.execute("BEGIN IMMEDIATE")
    try:
        insights.execute(
            "CREATE TABLE IF NOT EXISTS op_insight_gate_checkpoints("
            " rule_version TEXT NOT NULL,"
            " match_scale INTEGER NOT NULL,"
            " checkpoint INTEGER NOT NULL,"
            " sealed_at TEXT NOT NULL,"
            " pairs_at_seal INTEGER NOT NULL,"
            " pair_manifest_json TEXT NOT NULL,"
            " pair_manifest_sha256 TEXT NOT NULL,"
            " mean_arm_minus_control_bps INTEGER,"
            " median_arm_minus_control_bps INTEGER,"
            " creator_ci95_lo_bps INTEGER,"
            " creator_ci95_hi_bps INTEGER,"
            " PRIMARY KEY(rule_version, match_scale, checkpoint))"
        )
        for checkpoint in GATE_CHECKPOINTS:
            if len(pairs) < checkpoint:
                break
            subset = pairs[:checkpoint]
            manifest = [[str(t["alert_id"]), str(c["alert_id"])] for t, c in subset]
            manifest_json = json.dumps(manifest, separators=(",", ":"))
            deltas = [
                int(t["terminal_return_bps"]) - int(c["terminal_return_bps"])
                for t, c in subset
            ]
            ci = _creator_cluster_ci95(subset)
            cursor = insights.execute(
                "INSERT OR IGNORE INTO op_insight_gate_checkpoints VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    FORWARD_CONFIRMATION_RULE,
                    GATE_MATCH_SCALE,
                    checkpoint,
                    now_iso,
                    len(pairs),
                    manifest_json,
                    hashlib.sha256(manifest_json.encode()).hexdigest(),
                    round(sum(deltas) / len(deltas)),
                    round(median(deltas)),
                    ci[0] if ci else None,
                    ci[1] if ci else None,
                ),
            )
            sealed += cursor.rowcount
        insights.execute("COMMIT")
    except BaseException:
        insights.execute("ROLLBACK")
        raise
    return sealed


def read_live_insights(
    hot: sqlite3.Connection, *, historical: sqlite3.Connection | None = None
) -> dict[str, object]:
    """Build an honest launch-clustered exploratory insight surface from the hot ledger."""
    # Scoreable venues only. A retired venue has no forward cohort to report, and a
    # row of zeros beside the live ones reads as "no results yet" rather than "retired".
    venue_ids = tuple(sorted(venue.value for venue in scoreable_venues()))
    launch_counts = {
        row["venue_id"]: int(row["n"])
        for row in hot.execute(
            "SELECT venue_id,count(*) AS n FROM op_launches GROUP BY venue_id"
        ).fetchall()
    }
    alert_evidence = _merged_alert_evidence(hot, historical)
    alert_summary = _alert_summary(alert_evidence)
    arm_comparison = _arm_comparison(
        [row for row in alert_evidence if is_forward_measurement(row)]
    )
    arm_comparison_historical_pilot = {
        "label": HISTORICAL_PILOT_LABEL,
        "arms": _arm_comparison(
            [row for row in alert_evidence if not is_forward_measurement(row)]
        ),
    }
    confirmation_microscope = _confirmation_microscope(
        alert_evidence,
        gate_alerts=_realistic_gate_evidence(hot, alert_evidence),
    )
    outcome_rows = hot.execute(
        "SELECT o.*,lm.launch_id,l.venue_id FROM op_competing_outcomes o "
        "JOIN op_counterfactual_entries e ON e.entry_id=o.entry_id "
        "JOIN op_landmark_states lm ON lm.landmark_id=e.landmark_id "
        "JOIN op_launches l ON l.launch_id=lm.launch_id "
        "ORDER BY o.inserted_at DESC,o.outcome_id DESC"
    ).fetchall()
    outcome_by_launch: dict[str, sqlite3.Row] = {}
    for row in outcome_rows:
        outcome_by_launch.setdefault(str(row["launch_id"]), row)
    samples: list[dict[str, object]] = []
    hot_features = {
        str(row["alert_id"]): json.loads(row["state_json"])
        for row in hot.execute(
            "SELECT link.alert_id,s.state_json FROM op_forward_alert_entries link "
            "JOIN op_state_snapshots s ON s.snapshot_id=link.snapshot_id"
        ).fetchall()
    }
    outcomes: dict[str, int] = {}
    versions: set[str] = set()
    for alert in alert_evidence:
        if alert["score_status"] != "SCORED" or alert["outcome_kind"] is None:
            continue
        kind = str(alert["outcome_kind"])
        outcomes[kind] = outcomes.get(kind, 0) + 1
        if alert.get("evaluator_version"):
            versions.add(str(alert["evaluator_version"]))
        features = dict(hot_features.get(str(alert["alert_id"])) or {})
        features.update(alert.get("features") or {})
        samples.append(
            {
                "launch_id": str(alert["launch_id"]),
                "venue_id": str(alert["venue_id"]),
                "rule_version": str(alert.get("rule_version") or "unknown"),
                "evaluator_version": str(alert.get("evaluator_version") or "unknown"),
                "outcome_kind": kind,
                "return_bps": alert["terminal_return_bps"],
                "features": features,
            }
        )
    generic_outcomes: dict[str, int] = {}
    for outcome in outcome_by_launch.values():
        kind = str(outcome["outcome_kind"]).upper()
        generic_outcomes[kind] = generic_outcomes.get(kind, 0) + 1

    executable = hot.execute(
        "SELECT count(*) AS n,min(net_return_bps) AS lo,max(net_return_bps) AS hi,"
        "count(DISTINCT net_return_bps) AS distinct_n FROM op_executable_observations "
        "WHERE exit_kind='executable' AND net_return_bps IS NOT NULL"
    ).fetchone()
    executable_n = int(executable["n"] or 0)
    return_span = (
        int(executable["hi"]) - int(executable["lo"])
        if executable["hi"] is not None and executable["lo"] is not None
        else None
    )
    degenerate = executable_n >= 20 and return_span is not None and return_span <= 10
    target_ladder, near_misses = _diagnostic_receipts(hot)
    target_ladder, near_misses = _merge_diagnostics(
        target_ladder, near_misses, historical
    )
    venue_rows = []
    for venue_id in venue_ids:
        cohort = [row for row in samples if row["venue_id"] == venue_id]
        metric = _metric(cohort)
        kinds: dict[str, int] = {}
        for row in cohort:
            kind = str(row["outcome_kind"])
            kinds[kind] = kinds.get(kind, 0) + 1
        venue_rows.append(
            {
                "venue_id": venue_id,
                "launches": launch_counts.get(venue_id, 0),
                "matured_launches": len(cohort),
                "outcomes": kinds,
                **metric,
            }
        )

    status = (
        "DEGENERATE — NO PRICE INFORMATION"
        if degenerate
        else "LEARNING — EDGE NOT YET ESTABLISHED"
        if samples
        else "COLLECTING — NO SCORED ALERTS YET"
    )
    return {
        "status": status,
        "degenerate": degenerate,
        "receipt": {
            "launches": sum(launch_counts.values()),
            "matured_launches": alert_summary["scored"],
            "generic_matured_launches": len(outcome_by_launch),
            "executable_observations": executable_n,
            "min_return_bps": executable["lo"],
            "max_return_bps": executable["hi"],
            "return_span_bps": return_span,
            "distinct_returns": int(executable["distinct_n"] or 0),
            "evaluator_versions": sorted(versions),
        },
        "outcomes": outcomes,
        "generic_outcomes": generic_outcomes,
        "venues": venue_rows,
        "patterns": [],
        "pattern_status": "BLOCKED_UNTIL_MATCHED_CONTROLS",
        "alert_summary": alert_summary,
        "arm_comparison": arm_comparison,
        "arm_comparison_historical_pilot": arm_comparison_historical_pilot,
        "confirmation_microscope": confirmation_microscope,
        "alert_evidence": alert_evidence[:50],
        "target_ladder": target_ladder,
        "exit_realism": _exit_realism(alert_evidence),
        "decision_latency": _decision_latency(alert_evidence),
        "creator_prior": _creator_prior(alert_evidence),
        "launch_mode_cost": _launch_mode_cost(alert_evidence),
        "near_misses": near_misses,
    }


def prune_insight_samples(
    insights: sqlite3.Connection,
    *,
    cutoff_iso: str,
    alert_cutoff_iso: str | None = None,
    max_rows: int = 2_500,
) -> int:
    """Prune samples at `cutoff_iso` and alert evidence at `alert_cutoff_iso`.

    They are separate clocks because they answer different questions. Samples are the
    bulk (~68% of the file) and are per-launch diagnostics. The alert rows are the exact
    evidence the matched-control gate is computed from — deleting them makes the gate a
    rolling-window statistic that cannot reproduce its own pairs or bootstrap. Defaults to
    the sample cutoff so existing callers keep their behaviour unchanged.
    """
    alert_cutoff_iso = cutoff_iso if alert_cutoff_iso is None else alert_cutoff_iso
    sample_rows = insights.execute(
        "SELECT sample_id FROM op_insight_samples WHERE observed_at<? "
        "ORDER BY observed_at,sample_id LIMIT ?",
        (cutoff_iso, max_rows),
    ).fetchall()
    alert_rows = insights.execute(
        "SELECT alert_id FROM op_insight_alerts WHERE observed_at<? "
        "ORDER BY observed_at,alert_id LIMIT ?",
        (alert_cutoff_iso, max_rows),
    ).fetchall()
    if not sample_rows and not alert_rows:
        return 0
    before = insights.total_changes
    insights.execute("BEGIN IMMEDIATE")
    try:
        insights.executemany(
            "DELETE FROM op_insight_alert_targets WHERE alert_id=?",
            [(row[0],) for row in alert_rows],
        )
        insights.executemany(
            "DELETE FROM op_insight_alerts WHERE alert_id=?",
            [(row[0],) for row in alert_rows],
        )
        insights.executemany(
            "DELETE FROM op_insight_samples WHERE sample_id=?",
            [(row[0],) for row in sample_rows],
        )
        insights.execute("COMMIT")
    except BaseException:
        insights.execute("ROLLBACK")
        raise
    return insights.total_changes - before
