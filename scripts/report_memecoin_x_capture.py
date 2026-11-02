#!/usr/bin/env python3
"""Read-only coverage and integrity report for the memecoin X capture lane."""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sniper.analysis.memecoin_x.clock_health import x_observed_at_or_none
from sniper.persistence.db_core import get_readonly_connection


DEGRADED_STATUSES = frozenset({
    "coverage_gap", "wrong_feed", "logged_out", "challenge", "rate_limited",
    "page_unready", "clock_unhealthy", "parser_error", "error",
})
UNRESOLVED_OPERATOR_STATUSES = frozenset({
    "wrong_feed", "logged_out", "challenge", "rate_limited",
})
SECRET_KEYS = frozenset({
    "authorization", "cookie", "cookies", "headers", "local_storage", "localstorage",
    "profile_dir", "profile_path", "session_storage", "sessionstorage", "storage_state",
})
FORBIDDEN_IMPORT_PARTS = (
    "real_trader", "trade_executor", "memecoin_executor", "funded_runner",
)
FORBIDDEN_JS_PATTERNS = (
    re.compile(r"\.(?:click|fill|type|press|submit)\s*\("),
    re.compile(r"(?<![\w.])fetch\s*\("),
    re.compile(r"\blocation\.(?:assign|replace)\s*\("),
    re.compile(r"(?:\b|\.)location\s*="),
    re.compile(r"data-testid\s*=\s*[\"']?(?:like|follow|unfollow|retweet|reply)\b", re.I),
)
VALID_CADENCE_BUCKETS = frozenset({
    "new_5_8m", "idle_10_15m", "recovery_5_8m", "gap_10_15m",
    "page_unready_15m", "frozen", "delivery_retry_60s",
})
VALID_RELOAD_REASONS = frozenset({None, "none", "healthy_ack"})
MIN_FINISHED_RUNS = 90
MAX_COVERAGE_GAP_S = 960  # 15-minute idle cadence plus bounded pass overhead


def _parse(value):
    parsed = datetime.fromisoformat(str(value))
    if parsed.utcoffset() is None:
        raise ValueError(f"naive timestamp in capture report: {value}")
    return parsed.astimezone(timezone.utc)


def _rate(numerator, denominator):
    return None if not denominator else numerator / denominator


def _json_secret_hits(value):
    if value is None:
        return 0
    try:
        payload = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return 0

    def walk(node):
        if isinstance(node, dict):
            hits = sum(1 for key in node if str(key).lower() in SECRET_KEYS)
            return hits + sum(walk(child) for child in node.values())
        if isinstance(node, list):
            return sum(walk(child) for child in node)
        return 0

    return walk(payload)


def _forbidden_source_hits(source_root):
    root = Path(source_root)
    candidates = [
        root / "scripts" / "memecoin_x_capture.user.js",
        root / "scripts" / "run_memecoin_x_receiver.py",
        root / "sniper" / "services" / "memecoin_x_receiver.py",
    ]
    analysis = root / "sniper" / "analysis" / "memecoin_x"
    if analysis.exists():
        candidates.extend(sorted(analysis.glob("*.py")))
    hits = 0
    for path in candidates:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".js":
            hits += sum(len(pattern.findall(text)) for pattern in FORBIDDEN_JS_PATTERNS)
            continue
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError:
            hits += 1
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            hits += sum(
                1 for name in names if any(part in name.lower() for part in FORBIDDEN_IMPORT_PARTS))
    return hits


def build_report(conn, *, since, as_of=None, source_root=None):
    if as_of is None:
        raise ValueError("as_of is required; use report_from_path for corrected current UTC")
    since_dt = _parse(since)
    as_of_dt = _parse(as_of)
    if as_of_dt <= since_dt:
        raise ValueError("as_of must be after since")
    since = since_dt.isoformat()
    as_of = as_of_dt.isoformat()
    runs = conn.execute("""
        SELECT * FROM memecoin_x_capture_runs
        WHERE finished_at IS NOT NULL AND started_at >= ? AND finished_at <= ?
        ORDER BY started_at, id
    """, (since, as_of)).fetchall()
    running = conn.execute("""
        SELECT * FROM memecoin_x_capture_runs
        WHERE status='running' AND finished_at IS NULL
          AND started_at >= ? AND started_at <= ?
        ORDER BY started_at, id
    """, (since, as_of)).fetchall()
    run_ids = [row["run_id"] for row in runs]
    status_counts = Counter(row["status"] for row in runs)
    run_count = len(runs)
    cards_seen = sum(int(row["cards_seen"] or 0) for row in runs)
    cards_scanned = sum(int(row["cards_scanned"] or 0) for row in runs)
    stop_reason_counts = Counter(row["stop_reason"] for row in runs if row["stop_reason"])
    parse_failures = sum(int(row["parse_failures"] or 0) for row in runs)
    healthy = sum(row["clock_domain"] == "observed_real_utc" for row in runs)
    coverage_gaps = sum(row["status"] in DEGRADED_STATUSES for row in runs)
    sentinel_rows = [row for row in runs if row["sentinel_found"] is not None]
    sentinel_found = sum(bool(row["sentinel_found"]) for row in sentinel_rows)
    pacing_counts = Counter(row["cadence_bucket"] for row in runs if row["cadence_bucket"])
    page_unready_runs = status_counts.get("page_unready", 0)
    operator_postponed_runs = sum(row["operator_postponed"] == 1 for row in runs)
    max_dom_wait_ms = max((int(row["dom_wait_ms"] or 0) for row in runs), default=0)
    invalid_pacing_runs = sum(
        row["cadence_bucket"] not in VALID_CADENCE_BUCKETS
        or isinstance(row["scroll_count"], bool)
        or not isinstance(row["scroll_count"], int)
        or not 0 <= row["scroll_count"] <= 3
        or isinstance(row["dom_wait_ms"], bool)
        or not isinstance(row["dom_wait_ms"], int)
        or not 0 <= row["dom_wait_ms"] <= 30000
        or row["reload_reason"] not in VALID_RELOAD_REASONS
        or row["recovery_used"] not in (0, 1)
        or row["operator_postponed"] not in (0, 1)
        for row in runs)

    wall_hours = (as_of_dt - since_dt).total_seconds() / 3600
    if runs:
        previous_finish = since_dt
        leading_gap_seconds = max(
            0.0, (_parse(runs[0]["started_at"]) - since_dt).total_seconds())
        max_gap_seconds = leading_gap_seconds
        for row in runs:
            started = _parse(row["started_at"])
            finished = _parse(row["finished_at"])
            max_gap_seconds = max(
                max_gap_seconds, max(0.0, (started - previous_finish).total_seconds()))
            previous_finish = max(previous_finish, finished)
        trailing_gap_seconds = max(0.0, (as_of_dt - previous_finish).total_seconds())
        max_gap_seconds = max(max_gap_seconds, trailing_gap_seconds)
    else:
        leading_gap_seconds = trailing_gap_seconds = max_gap_seconds = (
            as_of_dt - since_dt).total_seconds()

    stale_running_runs = sum(
        (as_of_dt - _parse(row["started_at"])).total_seconds() > 900 for row in running)
    latest_status = None
    if runs:
        latest = max(runs, key=lambda row: (_parse(row["finished_at"]), row["id"]))
        latest_status = latest["status"]

    diagnostics = []
    observations = relationships = 0
    secret_scan_hits = 0
    if run_ids:
        placeholders = ",".join("?" for _ in run_ids)
        diagnostics = conn.execute(f"""
            SELECT d.* FROM memecoin_x_capture_diagnostics d
            WHERE d.capture_run_id IN ({placeholders})
        """, run_ids).fetchall()
        observations = conn.execute(f"""
            SELECT COUNT(*) FROM memecoin_x_card_observations
            WHERE capture_run_id IN ({placeholders})
        """, run_ids).fetchone()[0]
        relationships = conn.execute(f"""
            SELECT COUNT(*) FROM memecoin_x_relationships
            WHERE capture_run_id IN ({placeholders})
        """, run_ids).fetchone()[0]
        for row in conn.execute(f"""
            SELECT raw_card_json FROM memecoin_x_card_observations
            WHERE capture_run_id IN ({placeholders})
        """, run_ids):
            secret_scan_hits += _json_secret_hits(row[0])
        for row in conn.execute(f"""
            SELECT raw_card_json FROM memecoin_x_forward_posts
            WHERE capture_run_id IN ({placeholders})
        """, run_ids):
            secret_scan_hits += _json_secret_hits(row[0])
        for row in diagnostics:
            secret_scan_hits += _json_secret_hits(row["details_json"])
            secret_scan_hits += _json_secret_hits(row["raw_card_json"])

    diagnostic_counts = Counter(row["diagnostic_code"] for row in diagnostics)
    profile_locks = diagnostic_counts.get("profile_locked", 0)
    source_root = source_root or Path(__file__).resolve().parents[1]
    forbidden_source_hits = _forbidden_source_hits(source_root)
    healthy_clock_rate = _rate(healthy, run_count)
    coverage_gap_rate = _rate(coverage_gaps, run_count)
    parse_failure_rate = _rate(parse_failures, cards_seen)

    blocking = []
    if wall_hours < 24:
        blocking.append("wall_hours_below_24")
    if run_count < MIN_FINISHED_RUNS:
        blocking.append(f"finished_runs_below_{MIN_FINISHED_RUNS}")
    if healthy_clock_rate is None or healthy_clock_rate < 0.95:
        blocking.append("healthy_clock_rate_below_0.95")
    if coverage_gap_rate is None or coverage_gap_rate > 0.05:
        blocking.append("coverage_gap_rate_above_0.05")
    if max_gap_seconds > MAX_COVERAGE_GAP_S:
        blocking.append(f"max_gap_seconds_above_{MAX_COVERAGE_GAP_S}")
    if stale_running_runs:
        blocking.append("stale_running_run_present")
    if latest_status in UNRESOLVED_OPERATOR_STATUSES:
        blocking.append("unresolved_operator_state")
    if latest_status == "page_unready":
        blocking.append("unresolved_page_unready")
    if profile_locks:
        blocking.append("profile_lock_present")
    if cards_seen == 0:
        blocking.append("insufficient_activity")
    elif parse_failure_rate > 0.01:
        blocking.append("parse_failure_rate_above_0.01")
    if secret_scan_hits:
        blocking.append("secret_material_present")
    if forbidden_source_hits:
        blocking.append("forbidden_source_present")
    if invalid_pacing_runs:
        blocking.append("invalid_pacing_present")

    return {
        "since": since,
        "as_of": as_of,
        "wall_hours": wall_hours,
        "runs": run_count,
        "minimum_finished_runs": MIN_FINISHED_RUNS,
        "max_allowed_gap_seconds": MAX_COVERAGE_GAP_S,
        "status_counts": dict(sorted(status_counts.items())),
        "cards_seen": cards_seen,
        "cards_scanned": cards_scanned,
        "stop_reason_counts": dict(sorted(stop_reason_counts.items())),
        "pacing_counts": dict(sorted(pacing_counts.items())),
        "page_unready_runs": page_unready_runs,
        "operator_postponed_runs": operator_postponed_runs,
        "max_dom_wait_ms": max_dom_wait_ms,
        "invalid_pacing_runs": invalid_pacing_runs,
        "observations": observations,
        "relationships": relationships,
        "parse_failure_rate": parse_failure_rate,
        "healthy_clock_rate": healthy_clock_rate,
        "sentinel_found_rate": _rate(sentinel_found, len(sentinel_rows)),
        "coverage_gaps": coverage_gaps,
        "coverage_gap_rate": coverage_gap_rate,
        "max_gap_seconds": max_gap_seconds,
        "leading_gap_seconds": leading_gap_seconds,
        "trailing_gap_seconds": trailing_gap_seconds,
        "running_runs": len(running),
        "stale_running_runs": stale_running_runs,
        "latest_status": latest_status,
        "profile_locks": profile_locks,
        "diagnostic_counts": dict(sorted(diagnostic_counts.items())),
        "secret_scan_hits": secret_scan_hits,
        "forbidden_source_hits": forbidden_source_hits,
        "acceptance": not blocking,
        "blocking_reasons": blocking,
    }


def report_from_path(
        db_path, *, since, as_of=None, source_root=None, offset_path=None):
    if as_of is None:
        as_of, health = x_observed_at_or_none(path=offset_path)
        if as_of is None:
            raise ValueError(
                f"healthy corrected clock required: {health.get('reason', 'unknown')}")
    conn = get_readonly_connection(db_path)
    try:
        return build_report(conn, since=since, as_of=as_of, source_root=source_root)
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--since", required=True)
    parser.add_argument("--as-of", help="aware ISO cutoff; defaults to current UTC")
    parser.add_argument(
        "--clock-offset-file",
        help="trusted fresh clock-offset snapshot used when --as-of is omitted",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = report_from_path(
        args.db, since=args.since, as_of=args.as_of,
        offset_path=args.clock_offset_file,
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"accepted={report['acceptance']} runs={report['runs']} "
              f"wall_hours={report['wall_hours']:.2f}")
        for reason in report["blocking_reasons"]:
            print(f"BLOCK {reason}")


if __name__ == "__main__":
    main()
