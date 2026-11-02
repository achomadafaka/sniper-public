"""Health checks for the data collector.

Provides two checks:
- check_freshness: finds the newest .jsonl.gz file and checks its age
- check_schema_conformance: reads a sample of records and validates them via
  jsonl_to_event() round-trip (not just key presence)
"""
from __future__ import annotations

import gzip
import json
import os
from datetime import datetime, timezone
from typing import Any

from sniper.sim.serialize import jsonl_to_event


def check_freshness(
    raw_dir: str,
    venue: str = "polymarket",
    max_age_minutes: int = 10,
) -> dict[str, Any]:
    """Check if raw data files are recent enough.

    Returns a dict with:
      status: PASS | WARN | FAIL
      reason: human-readable explanation (on non-PASS)
      last_file: path to newest file (or None)
      age_minutes: float age of newest file (on PASS/WARN/FAIL with file)
    """
    venue_dir = os.path.join(raw_dir, venue)
    if not os.path.exists(venue_dir):
        return {"status": "FAIL", "reason": f"No directory: {venue_dir}", "last_file": None}

    latest_file = None
    latest_mtime = 0.0
    for root, _dirs, files in os.walk(venue_dir):
        for fname in files:
            if fname.endswith(".jsonl") or fname.endswith(".jsonl.gz"):
                path = os.path.join(root, fname)
                mtime = os.path.getmtime(path)
                if mtime > latest_mtime:
                    latest_mtime = mtime
                    latest_file = path

    if latest_file is None:
        return {"status": "FAIL", "reason": "No .jsonl or .jsonl.gz files found", "last_file": None}

    age_seconds = datetime.now(timezone.utc).timestamp() - latest_mtime
    age_minutes = age_seconds / 60

    if age_minutes > max_age_minutes:
        # WARN if between 1× and 3× the threshold; FAIL beyond 3×
        status = "FAIL" if age_minutes > max_age_minutes * 3 else "WARN"
        return {
            "status": status,
            "reason": f"Latest file is {age_minutes:.1f}min old (threshold {max_age_minutes}min)",
            "last_file": latest_file,
            "age_minutes": round(age_minutes, 1),
        }

    return {
        "status": "PASS",
        "last_file": latest_file,
        "age_minutes": round(age_minutes, 1),
    }


def check_schema_conformance(
    filepath: str,
    sample_size: int = 100,
) -> dict[str, Any]:
    """Check that records in a raw .jsonl.gz file conform to the canonical schema.

    Validates via jsonl_to_event() round-trip, not just key presence.

    Returns a dict with:
      status: PASS | FAIL
      checked: number of records inspected
      missing_fields: number of records that failed validation
      details: list of up to 10 failure descriptions
    """
    checked = 0
    missing_count = 0
    missing_details: list[str] = []

    def _open_file(path: str):
        if path.endswith(".gz"):
            return gzip.open(path, "rt")
        return open(path, "r", encoding="utf-8")

    try:
        with _open_file(filepath) as f:
            for line in f:
                if checked >= sample_size:
                    break
                line = line.strip()
                if not line:
                    continue

                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    missing_count += 1
                    missing_details.append(f"line {checked}: invalid JSON")
                    checked += 1
                    continue

                # Check required keys first (gives clearer error messages)
                required = {
                    "venue",
                    "market_id",
                    "venue_market_id",
                    "market_type",
                    "event_type",
                    "capture_ts",
                    "ingest_seq",
                    "payload",
                }
                missing = required - set(record.keys())
                if missing:
                    missing_count += 1
                    missing_details.append(f"line {checked}: missing fields {sorted(missing)}")
                else:
                    # Full round-trip validation via jsonl_to_event
                    try:
                        jsonl_to_event(json.dumps(record, separators=(",", ":")))
                    except Exception as exc:
                        missing_count += 1
                        missing_details.append(f"line {checked}: round-trip failed ({exc})")

                checked += 1

    except (OSError, gzip.BadGzipFile) as exc:
        return {"status": "FAIL", "reason": str(exc), "checked": 0, "missing_fields": 0}

    status = "PASS" if missing_count == 0 else "FAIL"
    return {
        "status": status,
        "checked": checked,
        "missing_fields": missing_count,
        "details": missing_details[:10],
    }
