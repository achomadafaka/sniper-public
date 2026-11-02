# tests/sim/test_writer.py
import gzip
import json
import os
import tempfile
from datetime import datetime, timezone

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
        capture_ts=datetime.now(timezone.utc).isoformat(timespec="milliseconds") + "Z",
        ingest_seq=seq,
        payload={"price": 0.65, "size": 100},
    )


def test_writer_compresses_on_close():
    """After close(), the file is gzipped and the original .jsonl is removed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        w = RawWriter(raw_dir=tmpdir, venue="polymarket", rotation_seconds=3600)
        w.write(_make_event(1))
        w.close()

        files = []
        for root, dirs, fnames in os.walk(tmpdir):
            for f in fnames:
                files.append(os.path.join(root, f))

        assert len(files) == 1
        assert files[0].endswith(".jsonl.gz")

        with gzip.open(files[0], "rt") as f:
            line = f.readline()
            evt = jsonl_to_event(line.strip())
            assert evt.venue == "polymarket"
            assert evt.ingest_seq == 1


def test_active_file_is_plain_text():
    """While writing, the file is plain .jsonl (not gzipped) for crash safety."""
    with tempfile.TemporaryDirectory() as tmpdir:
        w = RawWriter(raw_dir=tmpdir, venue="polymarket", rotation_seconds=3600)
        w.write(_make_event(1))
        w.flush()

        # Before close, file should be plain .jsonl
        assert w._current_path.endswith(".jsonl")
        assert not w._current_path.endswith(".jsonl.gz")

        # Should be readable as plain text
        with open(w._current_path, "r") as f:
            line = f.readline()
            evt = jsonl_to_event(line.strip())
            assert evt.ingest_seq == 1

        w.close()


def test_writer_batches_multiple_events():
    with tempfile.TemporaryDirectory() as tmpdir:
        w = RawWriter(raw_dir=tmpdir, venue="polymarket", rotation_seconds=3600)
        for i in range(10):
            w.write(_make_event(i))
        w.close()

        files = []
        for root, dirs, fnames in os.walk(tmpdir):
            for f in fnames:
                files.append(os.path.join(root, f))

        assert len(files) == 1
        with gzip.open(files[0], "rt") as f:
            lines = f.readlines()
            assert len(lines) == 10


def test_writer_file_path_format():
    with tempfile.TemporaryDirectory() as tmpdir:
        w = RawWriter(raw_dir=tmpdir, venue="polymarket", rotation_seconds=3600)
        w.write(_make_event(0))
        # Active path is .jsonl (plain text)
        path = w._current_path
        assert path.endswith(".jsonl")
        assert not path.endswith(".jsonl.gz")

        parts = path.replace(tmpdir + "/", "").split("/")
        assert parts[0] == "polymarket"
        assert len(parts) == 3  # venue/date/hour.jsonl

        w.close()
        # After close, should be compressed
        assert os.path.exists(path + ".gz")
        assert not os.path.exists(path)
