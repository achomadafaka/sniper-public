"""Integration test: writer survives full lifecycle including rotation and crash recovery."""
import gzip
import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import patch

from sniper.sim.collector.writer import RawWriter
from sniper.sim.schema import RawEvent, EventType
from sniper.sim.serialize import jsonl_to_event


def _make_event(seq: int) -> RawEvent:
    return RawEvent(
        venue="polymarket",
        market_id="pm:0xtest",
        venue_market_id="0xtest",
        market_type="geopolitical",
        event_type=EventType.TRADE,
        capture_ts="2026-04-02T22:00:00.000Z",
        ingest_seq=seq,
        payload={"price": 0.65, "size": 100},
    )


def test_rotation_produces_readable_gz():
    """Simulate hour change: write to hour 22, rotate to hour 23, verify 22 is readable gz."""
    with tempfile.TemporaryDirectory() as tmpdir:
        w = RawWriter(raw_dir=tmpdir, venue="polymarket")

        # Write 100 events during "hour 22"
        with patch.object(w, '_hour_key', return_value="2026-04-02/22"):
            for i in range(100):
                w.write(_make_event(i))
            w.flush()

        # Verify active file is plain text
        h22_path = os.path.join(tmpdir, "polymarket", "2026-04-02", "22.jsonl")
        assert os.path.exists(h22_path)
        with open(h22_path, "r") as f:
            assert len(f.readlines()) == 100

        # Rotate to "hour 23" — triggers close() on hour 22
        with patch.object(w, '_hour_key', return_value="2026-04-02/23"):
            w.write(_make_event(100))

        # Hour 22 should now be compressed and fully readable
        h22_gz = h22_path + ".gz"
        assert os.path.exists(h22_gz), "Compressed file should exist after rotation"
        assert not os.path.exists(h22_path), "Original should be removed after compression"

        with gzip.open(h22_gz, "rt") as f:
            lines = f.readlines()
            assert len(lines) == 100, f"Expected 100 lines, got {len(lines)}"
            # Verify first and last are valid events
            first = jsonl_to_event(lines[0].strip())
            last = jsonl_to_event(lines[-1].strip())
            assert first.ingest_seq == 0
            assert last.ingest_seq == 99

        # Hour 23 active file should exist as plain text
        h23_path = os.path.join(tmpdir, "polymarket", "2026-04-02", "23.jsonl")
        assert os.path.exists(h23_path)

        w.close()


def test_crash_recovery_no_data_loss():
    """Simulate crash: write events, don't call close(), verify data is recoverable."""
    with tempfile.TemporaryDirectory() as tmpdir:
        w = RawWriter(raw_dir=tmpdir, venue="polymarket")

        with patch.object(w, '_hour_key', return_value="2026-04-02/22"):
            for i in range(50):
                w.write(_make_event(i))
            w.flush()

        # Simulate crash — don't call close()
        active_path = w._current_path
        w._file.close()  # OS-level close without our compression step
        w._file = None

        # The file should be plain .jsonl and fully readable
        assert active_path.endswith(".jsonl")
        with open(active_path, "r") as f:
            lines = f.readlines()
            assert len(lines) == 50
            last = jsonl_to_event(lines[-1].strip())
            assert last.ingest_seq == 49


def test_preexisting_gz_does_not_corrupt():
    """If a .gz file already exists (from a prior crash), compression skips or overwrites safely."""
    with tempfile.TemporaryDirectory() as tmpdir:
        h22_dir = os.path.join(tmpdir, "polymarket", "2026-04-02")
        os.makedirs(h22_dir)

        # Create a pre-existing corrupt .gz file (simulating old writer crash)
        corrupt_gz = os.path.join(h22_dir, "22.jsonl.gz")
        with open(corrupt_gz, "wb") as f:
            f.write(b"this is not valid gzip data")

        w = RawWriter(raw_dir=tmpdir, venue="polymarket")

        # Write to hour 22
        with patch.object(w, '_hour_key', return_value="2026-04-02/22"):
            for i in range(30):
                w.write(_make_event(i))

        # Rotate to hour 23 — close() compresses hour 22, overwrites the corrupt .gz
        with patch.object(w, '_hour_key', return_value="2026-04-02/23"):
            w.write(_make_event(30))

        # The new .gz should be valid (overwritten the corrupt one)
        with gzip.open(corrupt_gz, "rt") as f:
            lines = f.readlines()
            assert len(lines) == 30

        w.close()


def test_multiple_rotations():
    """Three consecutive hours of rotation — all produce valid compressed files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        w = RawWriter(raw_dir=tmpdir, venue="polymarket")

        for hour in ["20", "21", "22"]:
            with patch.object(w, '_hour_key', return_value=f"2026-04-02/{hour}"):
                for i in range(25):
                    w.write(_make_event(i))

        w.close()

        # All three should be compressed and readable
        for hour in ["20", "21", "22"]:
            gz_path = os.path.join(tmpdir, "polymarket", "2026-04-02", f"{hour}.jsonl.gz")
            assert os.path.exists(gz_path), f"Missing {gz_path}"
            with gzip.open(gz_path, "rt") as f:
                lines = f.readlines()
                assert len(lines) == 25, f"Hour {hour}: expected 25, got {len(lines)}"
