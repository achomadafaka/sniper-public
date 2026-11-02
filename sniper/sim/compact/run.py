"""Compactor CLI — scan raw directory, compact unprocessed .jsonl.gz files."""
from __future__ import annotations

import gzip
import logging
import os
from collections import defaultdict
from typing import Any

from sniper.sim.compact.manifest import CompactManifest
from sniper.sim.compact.router import route_event
from sniper.sim.compact.writer import ParquetTableWriter
from sniper.sim.serialize import jsonl_to_event

LOG = logging.getLogger(__name__)


def compact_directory(
    raw_dir: str,
    output_dir: str,
    manifest_path: str,
    venue: str = "polymarket",
) -> dict[str, Any]:
    """Compact all unprocessed .jsonl.gz files under raw_dir/{venue}/.

    Returns a report dict with counts.
    """
    manifest = CompactManifest(manifest_path)
    writer = ParquetTableWriter(base_dir=output_dir)

    venue_dir = os.path.join(raw_dir, venue)
    if not os.path.exists(venue_dir):
        return {"files_processed": 0, "records_read": 0, "records_rejected": 0, "tables": {}}

    gz_files = []
    for root, _dirs, files in os.walk(venue_dir):
        for fname in sorted(files):
            full_path = os.path.join(root, fname)
            rel_path = os.path.relpath(full_path, raw_dir)
            if manifest.should_process(rel_path):
                gz_files.append((full_path, rel_path))

    total_read = 0
    total_rejected = 0
    table_counts: dict[str, int] = defaultdict(int)

    for full_path, rel_path in gz_files:
        LOG.info("Compacting %s", rel_path)
        with gzip.open(full_path, "rt") as f:
            for line in f:
                total_read += 1
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = jsonl_to_event(line)
                except Exception:
                    total_rejected += 1
                    continue

                # Extract date from capture_ts for partitioning
                date = evt.capture_ts[:10] if evt.capture_ts else "unknown"

                result = route_event(evt)
                if isinstance(result, tuple):
                    table_name, row = result
                    writer.append(table_name, date, [row])
                    table_counts[table_name] += 1
                elif isinstance(result, list):
                    for table_name, row in result:
                        writer.append(table_name, date, [row])
                        table_counts[table_name] += 1

        # Flush Parquet BEFORE marking processed — crash-idempotent ordering.
        # If flush succeeds but mark/save fails, worst case is a harmless
        # re-concat on rerun (Parquet append is additive, not destructive).
        # If flush fails, the file is NOT marked, so it will be retried.
        writer.flush()
        manifest.mark_processed(rel_path)
        manifest.save()

    return {
        "files_processed": len(gz_files),
        "records_read": total_read,
        "records_rejected": total_rejected,
        "tables": dict(table_counts),
    }


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(description="Compact raw JSONL to Parquet")
    parser.add_argument("--raw-dir", default=os.path.expanduser("~/sniper/raw"))
    parser.add_argument("--output-dir", default=os.path.expanduser("~/Desktop/polymarket-research/normalized"))
    parser.add_argument("--manifest", default=os.path.expanduser("~/Desktop/polymarket-research/compact_manifest.json"))
    parser.add_argument("--venue", default="polymarket")
    args = parser.parse_args()

    report = compact_directory(args.raw_dir, args.output_dir, args.manifest, args.venue)
    print(f"Compacted {report['files_processed']} files, {report['records_read']} records")
    print(f"Rejected: {report['records_rejected']}")
    for table, count in sorted(report["tables"].items()):
        print(f"  {table}: {count}")
