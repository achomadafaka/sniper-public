"""The buyer-breadth gate: score matched pass-vs-control pairs, and seal a receipt.

The label in `breadth_label.py` freezes a launch's breadth class. Nothing read it
until this module. The contract is
`docs/superpowers/specs/2026-08-19-radar-buyer-breadth-preregistration.md`.

Three frozen decisions live here, because a gate that picks its own source later is
not a preregistered gate:

1. The OUTCOME SOURCE groups all `alert-outcome-v4` rows by launch. The engine emits
   at least one alert after each label. All arms use one economic decision receipt.
   A rule filter would select on buyer breadth, so the source has no rule filter.
   Duplicate alerts must agree on all economic fields. A conflict censors the launch.
2. The RECEIPT is write-once. `INSERT OR IGNORE` on the primary key makes the first
   seal the only seal.
3. The two censor rates carry FROZEN denominators and never merge into one number.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from statistics import median

from .breadth_label import CLASS_CENSORED, CLASS_CONTROL, CLASS_PASS
from .insights import (
    GATE_CHECKPOINTS,
    GATE_MATCH_SCALE,
    _creator_cluster_ci95,
    _match_v4_controls,
    real_utc_now_iso,
)

BREADTH_GATE_VERSION = "radar-buyer-breadth-gate-v2"
BREADTH_OUTCOME_EVALUATOR = "alert-outcome-v4"
# The source uses all rules. A rule filter would restore the selection defect.
BREADTH_OUTCOME_SOURCE = (
    f"op_insight_alerts@all-rules@{BREADTH_OUTCOME_EVALUATOR}@launch-union-v1"
)

# The gate reports these modes even when a mode carries no label. A mode that
# vanishes reads as a question nobody asked. A mode with None rates reads as a
# question with no answer yet, which is the true state.
REQUIRED_MODES = ("standard", "cashback")

# This Gate 2 clock starts after the source contract. The earlier window is calibration.
FORWARD_CLOCK_ISO = "2026-08-21T00:00:00Z"
BREADTH_MATCH_SCALE = GATE_MATCH_SCALE
BREADTH_CHECKPOINTS = GATE_CHECKPOINTS

_CHECKPOINT_TABLE = """
CREATE TABLE IF NOT EXISTS op_insight_breadth_checkpoints(
  gate_version TEXT NOT NULL,
  launch_mode TEXT NOT NULL,
  match_scale INTEGER NOT NULL,
  checkpoint INTEGER NOT NULL,
  sealed_at TEXT NOT NULL,
  outcome_source TEXT NOT NULL,
  pairs_at_seal INTEGER NOT NULL,
  pair_manifest_json TEXT NOT NULL,
  pair_manifest_sha256 TEXT NOT NULL,
  mean_pass_minus_control_bps INTEGER,
  median_pass_minus_control_bps INTEGER,
  creator_ci95_lo_bps INTEGER,
  creator_ci95_hi_bps INTEGER,
  label_censor_rate_ppm INTEGER,
  outcome_censor_rate_pass_ppm INTEGER,
  outcome_censor_rate_control_ppm INTEGER,
  PRIMARY KEY(gate_version, launch_mode, match_scale, checkpoint)
)
"""

# INSERT OR IGNORE stops a second INSERT. It does NOT stop an UPDATE or a DELETE,
# and a sealed number that can be edited is not sealed.
_CHECKPOINT_STATEMENTS = (
    _CHECKPOINT_TABLE,
    "CREATE TRIGGER IF NOT EXISTS op_insight_breadth_checkpoints_no_update "
    "BEFORE UPDATE ON op_insight_breadth_checkpoints "
    "BEGIN SELECT RAISE(ABORT, 'op_insight_breadth_checkpoints immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS op_insight_breadth_checkpoints_no_delete "
    "BEFORE DELETE ON op_insight_breadth_checkpoints "
    "BEGIN SELECT RAISE(ABORT, 'op_insight_breadth_checkpoints immutable'); END",
)


def _rate(numerator: int, denominator: int) -> float | None:
    """A rate with no denominator is UNKNOWN. It is never zero."""
    if denominator <= 0:
        return None
    return numerator / denominator


def _ppm(rate: float | None) -> int | None:
    return None if rate is None else round(rate * 1_000_000)


def _launch_outcomes(insights: sqlite3.Connection) -> dict[str, tuple]:
    """Return one rule-independent v4 outcome per launch.

    Multiple arms use separate entry identities. Their economic receipt must agree.
    A missing or conflicting receipt stays absent and becomes an outcome censor.
    """
    grouped: dict[str, list[tuple]] = {}
    for row in insights.execute(
        "SELECT launch_id,observed_at,entry_effective_at,quote_observed_at,"
        "quote_completed_at,quote_age_ms,provider,score_status,outcome_kind,terminal_return_bps,"
        "outcome_elapsed_ms,creator_id,feature_json FROM op_insight_alerts "
        "WHERE evaluator_version=?",
        (BREADTH_OUTCOME_EVALUATOR,),
    ):
        grouped.setdefault(str(row[0]), []).append(tuple(row[1:]))
    outcomes: dict[str, tuple] = {}
    for launch_id, rows in grouped.items():
        if len(set(rows)) != 1:
            continue
        row = rows[0]
        if row[6] != "SCORED" or row[8] is None:
            continue
        try:
            features = json.loads(row[11]) if row[11] else {}
        except (TypeError, ValueError):
            continue
        outcomes[launch_id] = (row[0], row[8], row[10], features)
    return outcomes


def _load(insights: sqlite3.Connection, *, since_iso: str):
    """Join every breadth label to its outcome, if the frozen source carries one.

    The window filters `observed_at`, never the projection's `created_at`. A
    pre-clock launch copied across after the clock keeps its pre-clock observation,
    so filtering the copy time would readmit exactly what the clock excludes.
    """
    labels = insights.execute(
        "SELECT launch_id,breadth_class,launch_mode,decision_age_ms,progress_bps,"
        "modeled_round_trip_cost_bps,observed_at FROM op_insight_breadth_labels "
        "WHERE observed_at >= ?",
        (since_iso,),
    ).fetchall()
    outcomes = _launch_outcomes(insights)
    by_mode: dict[str, dict] = {}
    for launch_id, klass, mode, age, progress, cost, observed in labels:
        mode = str(mode or "unknown")
        bucket = by_mode.setdefault(
            mode,
            {"labels": 0, "censored": 0, CLASS_PASS: [], CLASS_CONTROL: [],
             "missing": {CLASS_PASS: 0, CLASS_CONTROL: 0},
             "classed": {CLASS_PASS: 0, CLASS_CONTROL: 0}},
        )
        bucket["labels"] += 1
        if klass == CLASS_CENSORED:
            bucket["censored"] += 1
            continue
        if klass not in (CLASS_PASS, CLASS_CONTROL):
            continue
        bucket["classed"][klass] += 1
        outcome = outcomes.get(str(launch_id))
        if outcome is None or outcome[0] != observed:
            bucket["missing"][klass] += 1
            continue
        try:
            features = outcome[3]
        except (IndexError, TypeError):
            bucket["missing"][klass] += 1
            continue
        bucket[klass].append({
            "alert_id": launch_id,
            "launch_id": launch_id,
            "observed_at": observed,
            "score_status": "SCORED",
            "terminal_return_bps": outcome[1],
            "creator_id": outcome[2],
            "features": {
                "launch_mode": mode,
                "quote_mint": features.get("quote_mint"),
                "decision_age_ms": age,
                "progress_bps": progress,
                "modeled_round_trip_cost_bps": cost,
            },
        })
    return by_mode


def score_breadth(
    insights: sqlite3.Connection,
    *,
    since_iso: str | None = None,
) -> dict[str, dict]:
    """Score every launch mode separately. No pooled number crosses modes.

    `since_iso=None` means the FROZEN clock, never "all time". An all-time read is
    a diagnostic and must name its own early timestamp out loud.
    """
    window = since_iso or FORWARD_CLOCK_ISO
    buckets = _load(insights, since_iso=window)
    for mode in REQUIRED_MODES:
        buckets.setdefault(mode, {
            "labels": 0, "censored": 0, CLASS_PASS: [], CLASS_CONTROL: [],
            "missing": {CLASS_PASS: 0, CLASS_CONTROL: 0},
            "classed": {CLASS_PASS: 0, CLASS_CONTROL: 0},
        })
    scored: dict[str, dict] = {}
    for mode, bucket in buckets.items():
        pairs, _unused = _match_v4_controls(
            bucket[CLASS_PASS], bucket[CLASS_CONTROL], scale=BREADTH_MATCH_SCALE
        )
        deltas = [
            int(p["terminal_return_bps"]) - int(c["terminal_return_bps"])
            for p, c in pairs
        ]
        ci = _creator_cluster_ci95(pairs) if pairs else None
        scored[mode] = {
            "pass_labels": bucket["classed"][CLASS_PASS],
            "control_labels": bucket["classed"][CLASS_CONTROL],
            "pass_scored": len(bucket[CLASS_PASS]),
            "control_scored": len(bucket[CLASS_CONTROL]),
            "matched_pairs": len(pairs),
            "mean_advantage_bps": round(sum(deltas) / len(deltas)) if deltas else None,
            "median_advantage_bps": round(median(deltas)) if deltas else None,
            "creator_ci95_bps": ci,
            "label_censor_rate": _rate(bucket["censored"], bucket["labels"]),
            "outcome_censor_rate_pass": _rate(
                bucket["missing"][CLASS_PASS], bucket["classed"][CLASS_PASS]
            ),
            "outcome_censor_rate_control": _rate(
                bucket["missing"][CLASS_CONTROL], bucket["classed"][CLASS_CONTROL]
            ),
            "_pairs": pairs,
        }
    return scored


def seal_breadth_checkpoints(
    insights: sqlite3.Connection,
    *,
    now_iso: str | None = None,
) -> int:
    """Freeze the pair identities and the statistics the FIRST time a mode crosses a
    checkpoint. A later re-run cannot move a sealed number.

    This function takes NO window argument. It always reads `FORWARD_CLOCK_ISO`. A
    caller-supplied window would let one careless call seal a pre-clock cohort
    permanently, and the receipt is write-once by design.
    """
    now_iso = now_iso or real_utc_now_iso()
    scored = score_breadth(insights, since_iso=FORWARD_CLOCK_ISO)
    sealed = 0
    insights.execute("BEGIN IMMEDIATE")
    try:
        for statement in _CHECKPOINT_STATEMENTS:
            insights.execute(statement)
        for mode in sorted(scored):
            stats = scored[mode]
            pairs = stats["_pairs"]
            for checkpoint in BREADTH_CHECKPOINTS:
                if len(pairs) < checkpoint:
                    break
                subset = pairs[:checkpoint]
                manifest = json.dumps(
                    [[str(p["alert_id"]), str(c["alert_id"])] for p, c in subset],
                    separators=(",", ":"),
                )
                deltas = [
                    int(p["terminal_return_bps"]) - int(c["terminal_return_bps"])
                    for p, c in subset
                ]
                ci = _creator_cluster_ci95(subset)
                cursor = insights.execute(
                    "INSERT OR IGNORE INTO op_insight_breadth_checkpoints VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        BREADTH_GATE_VERSION, mode, BREADTH_MATCH_SCALE, checkpoint,
                        now_iso, BREADTH_OUTCOME_SOURCE, len(pairs),
                        manifest, hashlib.sha256(manifest.encode()).hexdigest(),
                        round(sum(deltas) / len(deltas)), round(median(deltas)),
                        ci[0] if ci else None, ci[1] if ci else None,
                        _ppm(stats["label_censor_rate"]),
                        _ppm(stats["outcome_censor_rate_pass"]),
                        _ppm(stats["outcome_censor_rate_control"]),
                    ),
                )
                sealed += cursor.rowcount
        insights.execute("COMMIT")
    except BaseException:
        insights.execute("ROLLBACK")
        raise
    return sealed


def validate_cli(*, seal: bool, since: str | None) -> None:
    """A seal always reads the frozen clock. Printing one window while sealing
    another would show the operator a cohort that the receipt does not contain.
    """
    if seal and since is not None:
        raise ValueError("--seal reads the frozen clock; drop --since")


def format_report(scored: dict[str, dict]) -> str:
    """One line per launch mode. Modes never pool."""
    lines = [
        f"{'mode':10s} {'pass':>5s} {'ctrl':>5s} {'pairs':>6s} {'mean':>7s} "
        f"{'median':>7s} {'creator CI95':>18s} {'lbl_cens':>9s} {'out_P':>6s} {'out_C':>6s}"
    ]
    for mode in sorted(scored):
        stats = scored[mode]

        def show(value, digits=3):
            if value is None:
                return "n/a"
            return f"{value:.{digits}f}" if isinstance(value, float) else str(value)

        lines.append(
            f"{mode:10s} {stats['pass_labels']:5d} {stats['control_labels']:5d} "
            f"{stats['matched_pairs']:6d} {show(stats['mean_advantage_bps']):>7s} "
            f"{show(stats['median_advantage_bps']):>7s} "
            f"{str(stats['creator_ci95_bps']):>18s} "
            f"{show(stats['label_censor_rate']):>9s} "
            f"{show(stats['outcome_censor_rate_pass']):>6s} "
            f"{show(stats['outcome_censor_rate_control']):>6s}"
        )
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover - operator entry point
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/memecoin_opportunity_insights.db")
    parser.add_argument(
        "--since", default=None,
        help="diagnostic read window; ignored by --seal, which uses the frozen clock",
    )
    parser.add_argument(
        "--seal", action="store_true",
        help="write the immutable receipt for any mode at or above a checkpoint",
    )
    args = parser.parse_args()
    validate_cli(seal=args.seal, since=args.since)
    uri = f"file:{args.db}" + ("" if args.seal else "?mode=ro")
    conn = sqlite3.connect(uri, uri=True)
    print(format_report(score_breadth(conn, since_iso=args.since)))
    if args.seal:
        count = seal_breadth_checkpoints(conn)
        conn.commit()
        print(f"sealed {count} checkpoint(s)")
    else:
        print(f"checkpoints at {BREADTH_CHECKPOINTS}; --seal to write receipts")
