#!/usr/bin/env python3
"""Emit a read-only, stratified human-audit sample from the memecoin X store."""
from __future__ import annotations

import argparse
import csv
import io
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sniper.persistence.db_core import get_readonly_connection
from sniper.analysis.memecoin_x.normalize import PARSER_VERSION


def _open_audit_connection(db_path):
    return get_readonly_connection(str(Path(db_path).expanduser()))


def _json_list(value):
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _aware_iso(value):
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("since must be a timezone-aware ISO timestamp") from exc
    if parsed.utcoffset() is None:
        raise ValueError("since must be a timezone-aware ISO timestamp")
    return parsed.astimezone(timezone.utc).isoformat()


def _load_rows(conn, *, parser_version=None, since=None):
    observations = conn.execute(
        """
        SELECT o.id AS observation_id, o.capture_run_id, o.canonical_post_id,
               COALESCE(p.url, '') AS post_url, o.author_handle, o.actor_handle,
               o.relationship, o.observed_at, o.clock_domain, o.full_text,
               o.text_complete, o.extracted_urls, o.parser_version,
               o.raw_card_json AS observation_raw_card_json,
               p.raw_card_json AS post_raw_card_json
        FROM memecoin_x_card_observations o
        LEFT JOIN memecoin_x_forward_posts p
          ON p.canonical_post_id=o.canonical_post_id
        WHERE (? IS NULL OR o.parser_version=?)
          AND (? IS NULL OR o.observed_at>=?)
        ORDER BY o.observed_at DESC, o.id DESC
        """,
        (parser_version, parser_version, since, since),
    ).fetchall()
    if not observations:
        return []

    mentions = defaultdict(list)
    for row in conn.execute(
        """
        SELECT source_observation_id, network_id, mint, ticker, extraction_method,
               ambiguous, eligible
        FROM memecoin_x_coin_mentions
        WHERE source_observation_id IS NOT NULL
        ORDER BY source_observation_id, id
        """
    ):
        mentions[row["source_observation_id"]].append({
            "network_id": row["network_id"],
            "mint": row["mint"],
            "ticker": row["ticker"],
            "extraction_method": row["extraction_method"],
            "ambiguous": bool(row["ambiguous"]),
            "eligible": bool(row["eligible"]),
        })

    exact_diagnostics = defaultdict(set)
    for row in conn.execute(
        """
        SELECT capture_run_id, canonical_post_id, diagnostic_code
        FROM memecoin_x_capture_diagnostics
        ORDER BY id
        """
    ):
        if row["canonical_post_id"]:
            exact_diagnostics[(row["capture_run_id"], row["canonical_post_id"])].add(
                row["diagnostic_code"])

    result = []
    for row in observations:
        codes = exact_diagnostics[(row["capture_run_id"], row["canonical_post_id"])]
        result.append({
            "observation_id": row["observation_id"],
            "post_url": row["post_url"],
            "canonical_post_id": row["canonical_post_id"],
            "author_handle": row["author_handle"],
            "actor_handle": row["actor_handle"],
            "relationship": row["relationship"],
            "observed_at": row["observed_at"],
            "clock_domain": row["clock_domain"],
            "full_text": row["full_text"],
            "text_complete": bool(row["text_complete"]),
            "extracted_urls": _json_list(row["extracted_urls"]),
            "mentions": mentions[row["observation_id"]],
            "parser_version": row["parser_version"],
            "diagnostic_codes": sorted(codes),
            "raw_card_available": bool(
                row["observation_raw_card_json"] or row["post_raw_card_json"]),
        })
    return result


def _strata(row):
    labels = {f"relationship:{row['relationship']}"}
    if row["diagnostic_codes"]:
        labels.add("diagnostic:present")
    if not row["mentions"]:
        labels.add("network:none")
    for mention in row["mentions"]:
        network = mention["network_id"]
        if network == "solana":
            labels.add("network:solana")
        elif network == "4663":
            labels.add("network:4663")
        elif network:
            labels.add("network:other_evm")
        labels.add(f"eligible:{str(mention['eligible']).lower()}")
    return labels


def sample_audit_rows(conn, *, limit=30, parser_version=None, since=None):
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be positive")
    if (parser_version is not None and (
            isinstance(parser_version, bool) or not isinstance(parser_version, int)
            or parser_version <= 0)):
        raise ValueError("parser_version must be a positive integer")
    since = _aware_iso(since) if since is not None else None
    rows = _load_rows(conn, parser_version=parser_version, since=since)
    if len(rows) <= limit:
        return rows

    buckets = defaultdict(list)
    for row in rows:
        for label in _strata(row):
            buckets[label].append(row)
    order = [
        "relationship:direct", "relationship:repost", "relationship:quote",
        "relationship:reply", "network:solana", "network:4663", "network:other_evm",
        "network:none", "eligible:true", "eligible:false", "diagnostic:present",
    ]
    order.extend(sorted(set(buckets) - set(order)))

    selected = []
    selected_ids = set()
    positions = defaultdict(int)
    while len(selected) < limit:
        added = False
        for label in order:
            bucket = buckets[label]
            while (positions[label] < len(bucket)
                   and bucket[positions[label]]["observation_id"] in selected_ids):
                positions[label] += 1
            if positions[label] >= len(bucket):
                continue
            row = bucket[positions[label]]
            positions[label] += 1
            selected.append(row)
            selected_ids.add(row["observation_id"])
            added = True
            if len(selected) >= limit:
                break
        if not added:
            break
    for row in rows:
        if len(selected) >= limit:
            break
        if row["observation_id"] not in selected_ids:
            selected.append(row)
            selected_ids.add(row["observation_id"])
    return selected


def _render(rows, output_format):
    if output_format == "json":
        return json.dumps(rows, indent=2, sort_keys=True) + "\n"
    fields = list(rows[0].keys()) if rows else [
        "observation_id", "post_url", "canonical_post_id", "author_handle",
        "actor_handle", "relationship", "observed_at", "clock_domain", "full_text",
        "text_complete", "extracted_urls", "mentions", "parser_version",
        "diagnostic_codes", "raw_card_available",
    ]
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        flat = dict(row)
        for field in ("extracted_urls", "mentions", "diagnostic_codes"):
            flat[field] = json.dumps(flat[field], separators=(",", ":"), sort_keys=True)
        writer.writerow(flat)
    return stream.getvalue()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--parser-version", type=int, default=PARSER_VERSION)
    parser.add_argument("--since", help="aware ISO lower bound for the acceptance window")
    parser.add_argument("--format", choices=("json", "csv"), default="json")
    parser.add_argument("--output", help="optional artifact path; use .logs/ for live audits")
    args = parser.parse_args(argv)
    try:
        conn = _open_audit_connection(args.db)
        try:
            rows = sample_audit_rows(
                conn, limit=args.limit, parser_version=args.parser_version,
                since=args.since)
        finally:
            conn.close()
        rendered = _render(rows, args.format)
        if args.output:
            output = Path(args.output).expanduser()
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(rendered)
        else:
            sys.stdout.write(rendered)
    except (OSError, sqlite3.Error, ValueError) as exc:
        detail = " ".join(str(exc).split())[:240]
        print(f"audit sample unavailable: {detail}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
