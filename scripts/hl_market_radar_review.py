#!/usr/bin/env python3
"""Cron-friendly Hyperliquid Market Radar paper review."""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sqlite3
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.algos.hl_microstructure.review import build_performance_report, format_markdown_report


def _load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _send_telegram(text: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text[:3900],
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    with urllib.request.urlopen(req, timeout=10):
        return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Review Hyperliquid Market Radar paper performance")
    ap.add_argument("--db-path", default="data/hl_microstructure_paper.sqlite")
    ap.add_argument("--output", default=".logs/hl_market_radar_review_latest.md")
    ap.add_argument("--append-log", default=".logs/hl_market_radar_review.log")
    ap.add_argument("--telegram", action="store_true")
    args = ap.parse_args(argv)

    _load_dotenv()
    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(args.append_log) or ".", exist_ok=True)
    conn = sqlite3.connect(args.db_path)
    conn.row_factory = sqlite3.Row
    try:
        report = build_performance_report(conn)
    finally:
        conn.close()
    text = format_markdown_report(report)
    stamp = dt.datetime.now(dt.timezone.utc).isoformat()
    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(text)
    with open(args.append_log, "a", encoding="utf-8") as fh:
        fh.write(f"\n--- {stamp} ---\n{text}")
    if args.telegram:
        _send_telegram(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
