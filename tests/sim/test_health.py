"""Tests for sim.collector.health — freshness and schema conformance checks."""
import gzip
import json
import os
import tempfile
from datetime import datetime, timezone

from sniper.sim.collector.health import check_freshness, check_schema_conformance


def _write_sample_raw(tmpdir: str, age_minutes: int = 5) -> str:
    """Write a sample raw JSONL file and return its path."""
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    hour_str = now.strftime("%H")
    dir_path = os.path.join(tmpdir, "polymarket", date_str)
    os.makedirs(dir_path, exist_ok=True)
    filepath = os.path.join(dir_path, f"{hour_str}.jsonl.gz")

    record = {
        "venue": "polymarket",
        "market_id": "pm:0xtest",
        "venue_market_id": "0xtest",
        "market_type": "geopolitical",
        "event_type": "trade",
        "capture_ts": now.isoformat(timespec="milliseconds") + "Z",
        "ingest_seq": 1,
        "payload": {"price": "0.65"},
    }
    with gzip.open(filepath, "wt") as f:
        f.write(json.dumps(record) + "\n")
    return filepath


def test_freshness_pass():
    with tempfile.TemporaryDirectory() as tmpdir:
        _write_sample_raw(tmpdir)
        result = check_freshness(tmpdir, venue="polymarket", max_age_minutes=10)
        assert result["status"] == "PASS"


def test_freshness_fail_no_files():
    with tempfile.TemporaryDirectory() as tmpdir:
        result = check_freshness(tmpdir, venue="polymarket", max_age_minutes=10)
        assert result["status"] == "FAIL"


def test_schema_conformance_pass():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = _write_sample_raw(tmpdir)
        result = check_schema_conformance(path, sample_size=1)
        assert result["status"] == "PASS"
        assert result["checked"] == 1
        assert result["missing_fields"] == 0


def test_schema_conformance_fail_missing_field():
    with tempfile.TemporaryDirectory() as tmpdir:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        hour_str = datetime.now(timezone.utc).strftime("%H")
        dir_path = os.path.join(tmpdir, "polymarket", date_str)
        os.makedirs(dir_path, exist_ok=True)
        filepath = os.path.join(dir_path, f"{hour_str}.jsonl.gz")

        bad_record = {"venue": "polymarket", "payload": {}}  # missing required fields
        with gzip.open(filepath, "wt") as f:
            f.write(json.dumps(bad_record) + "\n")

        result = check_schema_conformance(filepath, sample_size=1)
        assert result["status"] == "FAIL"
        assert result["missing_fields"] > 0
