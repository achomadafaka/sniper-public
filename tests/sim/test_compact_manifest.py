# tests/sim/test_compact_manifest.py
import os
import tempfile

from sniper.sim.compact.manifest import CompactManifest


def test_mark_and_check():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "manifest.json")
        m = CompactManifest(path)
        assert not m.is_processed("raw/pm/2026-04-03/12.jsonl.gz")
        m.mark_processed("raw/pm/2026-04-03/12.jsonl.gz")
        assert m.is_processed("raw/pm/2026-04-03/12.jsonl.gz")


def test_persistence():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "manifest.json")
        m1 = CompactManifest(path)
        m1.mark_processed("file1.jsonl.gz")
        m1.save()

        m2 = CompactManifest(path)
        assert m2.is_processed("file1.jsonl.gz")
        assert not m2.is_processed("file2.jsonl.gz")


def test_skips_non_gz():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "manifest.json")
        m = CompactManifest(path)
        # Active .jsonl files should not be processable
        assert not m.should_process("raw/pm/2026-04-03/12.jsonl")
        assert m.should_process("raw/pm/2026-04-03/12.jsonl.gz")
