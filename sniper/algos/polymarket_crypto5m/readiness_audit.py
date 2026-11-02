"""Data-readiness + leakage audit (Phase 1B #10).

``leakage_checks`` runs STRUCTURAL guards over the persisted data — any violation must fail the dataset
build (``dataset_export.export_dataset(enforce_leakage=True)`` raises ``LeakageError``). ``readiness_report``
reports counts at each stage (captured -> resolved -> feature rows -> model-eligible) plus feature-group
coverage and learning-curve targets, so a modeling decision is made on evidence, not on code existence.
"""
from __future__ import annotations

from collections import Counter

from . import dataset_export as DX
from . import feature_registry as FR

# decision-time Price-to-Beat quality vocabulary; anything else (e.g. a post-resolution Gamma value) is a
# leakage smell because the boundary value must come from the RTDS reconstruction lane, not the oracle.
ALLOWED_P2B_QUALITY = {"unavailable", "validated_rtds_reconstruction", "exact_chainlink_report"}
LEARNING_CURVE_TARGETS = [200, 500, 1000, 2000, 4000]


class LeakageError(Exception):
    """Raised when a dataset build is attempted while leakage checks fail."""

    def __init__(self, violations):
        self.violations = violations
        super().__init__(f"leakage checks failed: {violations}")


def leakage_checks(conn) -> dict:
    violations = []

    # 1. feature cutoff must not be after the decision (a lookahead window).
    n = conn.execute("SELECT COUNT(*) FROM crypto5m_feature_snapshots "
                     "WHERE feature_cutoff_ts > decision_ts").fetchone()[0]
    if n:
        violations.append({"check": "cutoff_after_decision", "count": n})

    # 2. decision time must equal window_start + checkpoint (no tampering with the clock).
    n = conn.execute("SELECT COUNT(*) FROM crypto5m_feature_snapshots "
                     "WHERE decision_ts != window_start_ts + checkpoint").fetchone()[0]
    if n:
        violations.append({"check": "decision_not_window_aligned", "count": n})

    # 2b. the decision-time Price-to-Beat must have been KNOWN by the decision (no boundary lookahead).
    n = conn.execute("SELECT COUNT(*) FROM crypto5m_feature_snapshots "
                     "WHERE price_to_beat_known_ts IS NOT NULL "
                     "AND price_to_beat_known_ts > decision_ts").fetchone()[0]
    if n:
        violations.append({"check": "price_to_beat_known_after_decision", "count": n})

    # 3. Price-to-Beat quality must be in the decision-time vocabulary (no post-hoc Gamma oracle).
    qs = [r[0] for r in conn.execute(
        "SELECT DISTINCT price_to_beat_quality FROM crypto5m_feature_snapshots")]
    bad = [q for q in qs if q is not None and q not in ALLOWED_P2B_QUALITY]
    if bad:
        violations.append({"check": "unknown_price_to_beat_quality", "values": bad})

    # 4. no predictor may be a post-resolution field (registry invariant).
    post = [e["feature_name"] for e in FR.build_registry()
            if e["time_scope"] == "post_resolution_label"]
    if post:
        violations.append({"check": "post_resolution_predictor", "features": post})

    # 5. eligible-row invariant: cross-check each model-eligible row against the RAW snapshot columns
    # (independent of build_rows' own eligibility expression) — catches a build_rows regression where the
    # eligibility flag and the underlying quality flags diverge.
    bad = []
    for r in DX.build_rows(conn):
        if not r["model_eligible"]:
            continue
        raw = conn.execute("SELECT receive_time_missing, feature_status FROM crypto5m_feature_snapshots "
                           "WHERE condition_id=? AND checkpoint=? AND feature_version=? LIMIT 1",
                           (r["condition_id"], r["checkpoint"], r["feature_version"])).fetchone()
        if raw is None or raw["receive_time_missing"] or raw["feature_status"] != "ok" \
                or not r["label_available"]:
            bad.append((r["condition_id"], r["checkpoint"]))
    if bad:
        violations.append({"check": "eligible_row_invariant", "count": len(bad)})

    return {"passed": len(violations) == 0, "violations": violations}


def _group_coverage(cov: dict, prefixes) -> float:
    keys = [k for k in cov if any(k.startswith(p) for p in prefixes)]
    return (sum(cov[k] for k in keys) / len(keys)) if keys else 0.0


def readiness_report(conn) -> dict:
    rows = DX.build_rows(conn)
    by_cp = Counter(r["checkpoint"] for r in rows)
    eligible = sum(1 for r in rows if r["model_eligible"])
    windows_captured = conn.execute("SELECT COUNT(*) FROM crypto5m_markets").fetchone()[0]
    windows_resolved = conn.execute(
        "SELECT COUNT(*) FROM crypto5m_settlements s "
        "JOIN crypto5m_markets m ON m.condition_id = s.condition_id "
        "WHERE s.winning_outcome IS NOT NULL AND s.reconciliation_status != 'conflict'").fetchone()[0]
    fee_known = conn.execute(
        "SELECT COUNT(*) FROM crypto5m_fee_configs WHERE fee_status='known'").fetchone()[0]
    cov = FR.feature_availability(conn)["coverage_by_feature"]
    feature_group_coverage = {
        "chainlink_path": _group_coverage(cov, ["chainlink_"]),
        "binance_price": _group_coverage(cov, ["binance_return", "binance_realized_vol", "binance_ema",
                                               "binance_price", "binance_short_return", "binance_trend"]),
        "binance_volume": _group_coverage(cov, ["binance_volume", "binance_vwap",
                                                "binance_distance_to_vwap"]),
        "basis": _group_coverage(cov, ["basis_", "binance_chainlink_basis"]),
        "executable": _group_coverage(cov, ["up_executable", "down_executable", "up_fee_inclusive",
                                            "down_fee_inclusive", "up_slippage", "down_slippage"]),
    }
    return {
        "windows_captured": windows_captured,
        "windows_resolved": windows_resolved,
        "feature_rows": len(rows),
        "feature_rows_by_checkpoint": dict(by_cp),
        "model_eligible": eligible,
        "fee_state_known": fee_known,
        "feature_group_coverage": feature_group_coverage,
        "learning_curve_targets": LEARNING_CURVE_TARGETS,
        "resolved_meets_target": {t: (windows_resolved >= t) for t in LEARNING_CURVE_TARGETS},
        "leakage": leakage_checks(conn),
    }
