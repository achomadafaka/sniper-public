#!/usr/bin/env python3
"""Liquidation monitor -- funding extremes, OI spikes, risk flags.

Reads from hyperliquid_snapshots to detect overleveraged conditions.
Writes risk flags to Supabase brain and sends Telegram alerts.

Detection rules:
  A. Funding rate extreme: abs(funding_rate) > 0.05% per 8h
     - Positive funding = longs overleveraged = bearish signal
     - Negative funding = shorts overleveraged = bullish signal
  B. Open interest spike: OI increased > 10% in the last hour
     - Requires at least 12 snapshots (1h at 5min intervals)

Cron: every 15 minutes (2,17,32,47 * * * * *)

Usage:
    python3 scripts/liquidation_monitor.py              # full run
    python3 scripts/liquidation_monitor.py --quiet       # no Telegram
    python3 scripts/liquidation_monitor.py --dry-run     # print, don't write
    python3 scripts/liquidation_monitor.py --status      # current snapshot, no changes
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Project root bootstrapping
# ---------------------------------------------------------------------------
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Load .env
_env_path = os.path.join(ROOT, ".env")
if os.path.isfile(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                if _k.strip() and _k.strip() not in os.environ:
                    os.environ[_k.strip()] = _v.strip()

logger = logging.getLogger("liquidation_monitor")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DB_PATH = os.getenv("DB_PATH", os.path.join(ROOT, "sniper_tracker.db"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Funding rate extreme threshold (per 8h): 0.0005 = 0.05%
FUNDING_EXTREME_THRESHOLD = 0.0005

# OI spike threshold: 10% increase in 1 hour
OI_SPIKE_THRESHOLD = 0.10

# Minimum snapshots needed for OI spike calc (1h at 5min = 12)
MIN_SNAPSHOTS_FOR_OI = 12

# Assets to monitor
MONITORED_ASSETS = ("BTC", "ETH", "SOL", "HYPE")


# ---------------------------------------------------------------------------
# DB reader
# ---------------------------------------------------------------------------

def _get_db_conn(path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def get_latest_snapshot(conn, asset: str) -> Optional[dict]:
    """Get the most recent snapshot for an asset."""
    row = conn.execute(
        """SELECT asset, mark_price, funding_rate, open_interest, volume_24h, captured_at
           FROM hyperliquid_snapshots
           WHERE asset = ?
           ORDER BY captured_at DESC LIMIT 1""",
        (asset,),
    ).fetchone()
    return dict(row) if row else None


def get_snapshot_1h_ago(conn, asset: str) -> Optional[dict]:
    """Get the snapshot closest to 1 hour ago for an asset."""
    row = conn.execute(
        """SELECT asset, mark_price, funding_rate, open_interest, volume_24h, captured_at
           FROM hyperliquid_snapshots
           WHERE asset = ? AND captured_at <= datetime('now', '-55 minutes')
           ORDER BY captured_at DESC LIMIT 1""",
        (asset,),
    ).fetchone()
    return dict(row) if row else None


def count_recent_snapshots(conn, asset: str, hours: int = 1) -> int:
    """Count snapshots in the last N hours for an asset."""
    row = conn.execute(
        """SELECT COUNT(*) as cnt FROM hyperliquid_snapshots
           WHERE asset = ? AND captured_at >= datetime('now', ? || ' hours')""",
        (asset, str(-hours)),
    ).fetchone()
    return row["cnt"] if row else 0


# ---------------------------------------------------------------------------
# Detection engine
# ---------------------------------------------------------------------------

def detect_funding_extreme(snapshot: dict) -> Optional[dict]:
    """Check if funding rate is extreme.

    Returns alert dict or None.
    """
    funding = float(snapshot.get("funding_rate", 0))
    if abs(funding) <= FUNDING_EXTREME_THRESHOLD:
        return None

    asset = snapshot["asset"]
    funding_pct = funding * 100

    if funding > 0:
        # Longs paying shorts -- longs overleveraged
        direction = "bearish"
        risk = "longs overleveraged, potential long squeeze"
    else:
        # Shorts paying longs -- shorts overleveraged
        direction = "bullish"
        risk = "shorts overleveraged, potential short squeeze"

    severity = "HIGH" if abs(funding) > FUNDING_EXTREME_THRESHOLD * 2 else "MEDIUM"

    return {
        "type": "funding_extreme",
        "asset": asset,
        "severity": severity,
        "direction": direction,
        "funding_rate": funding,
        "funding_pct": round(funding_pct, 4),
        "risk_description": risk,
        "flag": f"flag:finance->all:{asset.lower()}-funding-extreme",
    }


def detect_oi_spike(
    current: dict,
    historical: Optional[dict],
    snapshot_count: int,
) -> Optional[dict]:
    """Check if OI spiked >10% in the last hour.

    Returns alert dict or None.
    """
    if snapshot_count < MIN_SNAPSHOTS_FOR_OI:
        return None

    if not historical:
        return None

    current_oi = float(current.get("open_interest", 0))
    hist_oi = float(historical.get("open_interest", 0))

    if hist_oi <= 0:
        return None

    change_pct = (current_oi - hist_oi) / hist_oi

    if change_pct <= OI_SPIKE_THRESHOLD:
        return None

    asset = current["asset"]
    severity = "HIGH" if change_pct > OI_SPIKE_THRESHOLD * 2 else "MEDIUM"

    return {
        "type": "oi_spike",
        "asset": asset,
        "severity": severity,
        "direction": "neutral",  # OI spike is directionally ambiguous
        "oi_current": round(current_oi),
        "oi_1h_ago": round(hist_oi),
        "change_pct": round(change_pct * 100, 2),
        "risk_description": f"OI up {change_pct*100:.1f}% in 1h -- leverage building, cascade risk",
        "flag": f"flag:finance->all:{asset.lower()}-oi-spike",
    }


def evaluate_all_assets(conn) -> Dict[str, Any]:
    """Run detection on all monitored assets.

    Returns {
        "timestamp": ...,
        "alerts": [...],
        "risk_levels": {"BTC": "HIGH", "ETH": "LOW", ...},
        "summaries": {"BTC": {...}, ...},
    }
    """
    now = datetime.now(timezone.utc).isoformat()
    alerts: List[dict] = []
    risk_levels: Dict[str, str] = {}
    summaries: Dict[str, dict] = {}

    for asset in MONITORED_ASSETS:
        latest = get_latest_snapshot(conn, asset)
        if not latest:
            risk_levels[asset] = "NO_DATA"
            continue

        asset_alerts = []

        # A. Funding rate extreme
        funding_alert = detect_funding_extreme(latest)
        if funding_alert:
            asset_alerts.append(funding_alert)

        # B. OI spike
        snap_count = count_recent_snapshots(conn, asset, hours=1)
        hist = get_snapshot_1h_ago(conn, asset)
        oi_alert = detect_oi_spike(latest, hist, snap_count)
        if oi_alert:
            asset_alerts.append(oi_alert)

        # Determine risk level
        if any(a["severity"] == "HIGH" for a in asset_alerts):
            risk_levels[asset] = "HIGH"
        elif any(a["severity"] == "MEDIUM" for a in asset_alerts):
            risk_levels[asset] = "MEDIUM"
        else:
            risk_levels[asset] = "LOW"

        alerts.extend(asset_alerts)

        summaries[asset] = {
            "price": round(float(latest["mark_price"]), 2),
            "funding_rate": float(latest["funding_rate"]),
            "funding_pct": round(float(latest["funding_rate"]) * 100, 4),
            "open_interest": round(float(latest["open_interest"])),
            "risk_level": risk_levels[asset],
            "captured_at": latest["captured_at"],
        }

    return {
        "timestamp": now,
        "alerts": alerts,
        "risk_levels": risk_levels,
        "summaries": summaries,
    }


# ---------------------------------------------------------------------------
# Brain write
# ---------------------------------------------------------------------------

def write_brain_risk(report: dict) -> bool:
    """Write liquidation risk report to Supabase brain."""
    try:
        from bot.supabase_helper import upsert_memory

        content = json.dumps(report, indent=2, default=str)
        upsert_memory(
            type="knowledge",
            name="markets/liquidation_risk",
            description=f"Liquidation risk snapshot {report['timestamp'][:16]}",
            content=content,
            source="vps",
        )
        return True
    except Exception as e:
        logger.warning("Failed to write brain risk: %s", e)
        return False


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(msg: str) -> bool:
    """Send a Telegram notification."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.debug("Telegram not configured, skipping")
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = json.dumps({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "HTML",
        }).encode()
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        logger.warning("Telegram send failed: %s", e)
        return False


def format_alert_message(report: dict) -> Optional[str]:
    """Format alerts into a Telegram message. Returns None if no alerts."""
    alerts = report.get("alerts", [])
    high_alerts = [a for a in alerts if a["severity"] == "HIGH"]

    if not high_alerts:
        return None

    lines = ["<b>Liquidation Monitor Alert</b>"]
    for a in high_alerts:
        asset = a["asset"]
        if a["type"] == "funding_extreme":
            lines.append(
                f"\n<b>{asset}</b> funding extreme: {a['funding_pct']:+.4f}%/8h"
                f"\n  {a['risk_description']}"
            )
        elif a["type"] == "oi_spike":
            lines.append(
                f"\n<b>{asset}</b> OI spike: +{a['change_pct']:.1f}% in 1h"
                f"\n  OI: ${a['oi_current']:,.0f} (was ${a['oi_1h_ago']:,.0f})"
                f"\n  {a['risk_description']}"
            )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(
    *,
    quiet: bool = False,
    dry_run: bool = False,
    status_only: bool = False,
) -> dict:
    """Execute the liquidation monitor.

    Returns the full report dict.
    """
    conn = _get_db_conn()
    try:
        report = evaluate_all_assets(conn)
    finally:
        conn.close()

    alerts = report.get("alerts", [])
    risk_levels = report.get("risk_levels", {})

    if status_only or not quiet:
        print(f"Liquidation Monitor: {len(alerts)} alerts")
        for asset, level in risk_levels.items():
            summary = report["summaries"].get(asset, {})
            if summary:
                print(
                    f"  {asset:>5s}: risk={level:<7s} "
                    f"price=${summary.get('price', 0):>10,.2f}  "
                    f"funding={summary.get('funding_pct', 0):+.4f}%  "
                    f"OI=${summary.get('open_interest', 0):>13,.0f}"
                )
            else:
                print(f"  {asset:>5s}: {level}")
        if alerts:
            print("\nAlerts:")
            for a in alerts:
                print(f"  [{a['severity']}] {a['asset']} {a['type']}: {a['risk_description']}")

    if status_only:
        return report

    if dry_run:
        print("\n[DRY RUN] Would write brain + send alerts for HIGH severity")
        return report

    # Write to brain
    brain_ok = write_brain_risk(report)
    if brain_ok:
        logger.info("Brain risk report written")

    # Telegram for HIGH alerts
    if not quiet:
        msg = format_alert_message(report)
        if msg:
            send_telegram(msg)

    logger.info(
        "Liquidation monitor: %d alerts, risk=%s",
        len(alerts),
        json.dumps(risk_levels),
    )
    return report


def main():
    parser = argparse.ArgumentParser(description="Liquidation monitor -- Hyperliquid risk detection")
    parser.add_argument("--quiet", action="store_true", help="Suppress Telegram notifications")
    parser.add_argument("--dry-run", action="store_true", help="Print report without writing")
    parser.add_argument("--status", action="store_true", help="Show current status only")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    try:
        run(quiet=args.quiet, dry_run=args.dry_run, status_only=args.status)
    except Exception:
        logger.exception("Liquidation monitor failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
