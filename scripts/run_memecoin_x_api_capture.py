#!/usr/bin/env python3
"""Run default-OFF official X API cashtag capture or an offline fixture smoke."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal
import sqlite3
import sys
import threading


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sniper.analysis.memecoin_x.clock_health import x_observed_at_or_none
from sniper.persistence import db_memecoin_x as X
from sniper.services.memecoin_x_api_capture import (
    ingest_payload, reconcile_rules, run_recovery, run_stream,
)
from sniper.services.memecoin_x_api_client import XApiClient
from sniper.settings.x_intel import XIntelSettings


def _connection(path):
    conn = sqlite3.connect(Path(path).expanduser(), timeout=30)
    conn.row_factory = sqlite3.Row
    X.ensure_memecoin_x_tables(conn)
    return conn


def _fixture(args) -> int:
    payload = json.loads(Path(args.fixture).read_text())
    actor_id = str(payload["data"]["author_id"])
    users = {str(row["id"]): row for row in payload.get("includes", {}).get("users", [])}
    actor = users[actor_id]
    conn = _connection(args.db or ":memory:")
    try:
        conn.execute(
            "INSERT OR IGNORE INTO memecoin_x_accounts "
            "(handle,display_handle,x_user_id,role,active) VALUES (?,?,?,?,1)",
            (actor["username"], actor["username"], actor_id, "tier1_trader"))
        conn.commit()
        result = ingest_payload(
            conn, payload, source="fixture",
            settings=type("FixtureSettings", (), {
                "max_post_reads_per_day": 500, "recovery_max_pages": 1})(),
            observed_at_fn=lambda: "2026-07-26T12:00:05+00:00")
    finally:
        conn.close()
    print(json.dumps({
        "eligible": result["eligible"], "events_created": result["events_created"],
        "post_id": result["post_id"], "source": "fixture"}, sort_keys=True))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture")
    parser.add_argument("--db")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if args.fixture:
        return _fixture(args)
    try:
        settings = XIntelSettings.from_env()
        if not settings.enabled:
            print(json.dumps({"state": "disabled"}))
            return 0
        observed_at, health = x_observed_at_or_none(path=settings.clock_offset_file)
        if observed_at is None:
            raise ValueError(f"healthy corrected clock required: {health['reason']}")
        clock = lambda: x_observed_at_or_none(path=settings.clock_offset_file)[0]
        conn = _connection(args.db or settings.db_path)
        try:
            if not X.active_api_roster(conn):
                raise ValueError("active curated roster is empty")
            client = XApiClient(settings.bearer_token)
            reconcile_rules(conn, client, observed_at_fn=clock)
            recovery = run_recovery(
                conn, client, settings=settings, observed_at_fn=clock)
            if args.once:
                print(json.dumps(recovery, sort_keys=True))
                return 0
            stop = threading.Event()
            signal.signal(signal.SIGTERM, lambda *_: stop.set())
            signal.signal(signal.SIGINT, lambda *_: stop.set())
            print(json.dumps(run_stream(
                conn, client, settings=settings, observed_at_fn=clock,
                stop_event=stop), sort_keys=True))
            return 0
        finally:
            conn.close()
    except Exception as exc:
        message = str(exc)
        token = locals().get("settings") and settings.bearer_token
        if token:
            message = message.replace(token, "<redacted>")
        print(message[:500], file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
