#!/usr/bin/env python3
"""Run one disabled-by-default memecoin X informational notification pass."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from urllib import parse, request


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sniper.analysis.memecoin_x.clock_health import x_observed_at_or_none
from sniper.persistence.db_core import get_readonly_connection
from sniper.services.memecoin_x_notifications import deliver_pending, preview_pending


def _telegram_sender(token, chat_id):
    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"

    def send(text):
        started = time.monotonic()
        data = parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
        req = request.Request(endpoint, data=data, method="POST")
        try:
            with request.urlopen(req, timeout=15) as response:
                ok = 200 <= response.status < 300
                response.read(4096)
            error = None if ok else "non-success response"
        except Exception as exc:
            # The endpoint contains the bot token. Persist only the exception class;
            # transport exception text can echo a request URL and leak credentials.
            ok, error = False, type(exc).__name__
        return ok, (time.monotonic() - started) * 1000, error

    return send


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--send", action="store_true")
    mode.add_argument("--send-if-enabled", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--clock-offset-file", default=str(ROOT / "data/clock_offset.json"))
    args = parser.parse_args(argv)
    gate_enabled = os.getenv("X_INTEL_TELEGRAM_ENABLED") == "1"
    if args.send_if_enabled and not gate_enabled:
        print(json.dumps({"enabled": False, "selected": 0, "sent": 0, "failed": 0}))
        return 0
    observed_at, health = x_observed_at_or_none(path=args.clock_offset_file)
    if observed_at is None:
        parser.error(f"healthy corrected clock required: {health.get('reason', 'unknown')}")
    enabled = bool(args.send or args.send_if_enabled)
    if args.send and not gate_enabled:
        parser.error("sending requires X_INTEL_TELEGRAM_ENABLED=1")
    token = os.getenv("TELEGRAM_BOT_TOKEN") if enabled else None
    chat_id = os.getenv("TELEGRAM_CHAT_ID") if enabled else None
    if enabled and (not token or not chat_id):
        parser.error("sending requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
    if enabled:
        conn = sqlite3.connect(Path(args.db).expanduser(), timeout=30)
        conn.row_factory = sqlite3.Row
        sender = _telegram_sender(token, chat_id)
    else:
        conn = get_readonly_connection(str(Path(args.db).expanduser()))
    try:
        if enabled:
            result = deliver_pending(
                conn, sender=sender, observed_at=observed_at,
                enabled=True, limit=args.limit)
        else:
            previews = preview_pending(conn, limit=args.limit)
            result = {
                "enabled": False, "dry_run": True,
                "selected": len(previews), "previews": previews,
            }
    finally:
        conn.close()
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
