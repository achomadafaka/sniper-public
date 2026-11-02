"""Read-only Ink shadow coverage report. It grants no funded authority."""

import argparse
import json
import sqlite3
from pathlib import Path


def build_report(conn, manifest):
    if manifest.get("chain_id") != 57073:
        raise ValueError("Ink manifest has wrong chain")
    states = dict(conn.execute(
        "SELECT state,count(*) FROM ink_stream_observations GROUP BY state").fetchall())
    canonical_blocks = conn.execute("SELECT count(*) FROM ink_canonical_blocks").fetchone()[0]
    gaps = [{"from_block": start, "through_block": end, "reason": reason}
            for start, end, reason in conn.execute(
                "SELECT block_number,coalesce(end_block_number,block_number),reason "
                "FROM ink_stream_gaps WHERE resolved_at IS NULL ORDER BY block_number,reason")]
    raw = dict(conn.execute(
        "SELECT state,count(*) FROM ink_stream_observations "
        "WHERE payload_id NOT LIKE 'canonical:%' GROUP BY state").fetchall())
    resolved = sum(raw.get(state, 0) for state in
                   ("canonical_unsafe", "safe", "finalized", "retracted"))
    matched = sum(raw.get(state, 0) for state in
                  ("canonical_unsafe", "safe", "finalized"))
    match_rate = (matched / resolved if resolved and not gaps and not raw.get("provisional")
                  else None)
    return {
        "chain_id": 57073, "funded_activation": manifest.get("funded_activation"),
        "observations_by_state": states, "canonical_blocks": canonical_blocks,
        "open_gaps": gaps, "canonical_match_rate": match_rate,
        "match_rate_denominator": "resolved raw subblock transaction observations only",
        "g2_shadow_ready": False,  # transaction observations alone cannot prove 24h frame coverage
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", required=True)
    parser.add_argument("--manifest", default=str(Path(__file__).resolve().parents[2] /
                        "docs/superpowers/research/2026-09-29-ink-coverage-manifest.json"))
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    manifest = json.loads(Path(args.manifest).read_text())
    with sqlite3.connect(f"file:{Path(args.journal).resolve()}?mode=ro", uri=True) as conn:
        report = build_report(conn, manifest)
    Path(args.output).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
