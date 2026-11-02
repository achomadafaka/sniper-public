#!/usr/bin/env python3
"""
Daily Briefing — sends a morning summary to Telegram.
Run via cron at 13:00 UTC (8 AM EST).

Covers: discovery pipeline (high-scorers, queue), sports edges, system health.
"""

import time
import os
import sys
import requests
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from db import get_connection

DB_PATH = os.getenv("DB_PATH", "sniper_tracker.db")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Try loading from .env file if not in environment
if not BOT_TOKEN or not CHAT_ID:
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k == "TELEGRAM_BOT_TOKEN" and not BOT_TOKEN:
                    BOT_TOKEN = v
                if k == "TELEGRAM_CHAT_ID" and not CHAT_ID:
                    CHAT_ID = v


def get_conn():
    return get_connection(DB_PATH)


def discovery_status(conn):
    """Discovery pipeline health."""
    rows = conn.execute(
        "SELECT state, COUNT(*) as c FROM wallet_analysis_queue GROUP BY state"
    ).fetchall()
    states = {r["state"]: r["c"] for r in rows}
    
    now = int(time.time())
    r = conn.execute(
        "SELECT COUNT(*) as c FROM wallet_analysis_queue WHERE state = 'done' AND updated_ts > ?",
        (now - 86400,),
    ).fetchone()
    done_24h = r["c"]
    
    r = conn.execute(
        "SELECT COUNT(*) as c FROM user_analysis WHERE updated_at > datetime('now', '-24 hours')"
    ).fetchone()
    analyzed_24h = r["c"]
    
    # New high-scorers: must have real win rate and a meaningful classification.
    # low_signal = not enough trades to trust the win rate — exclude entirely.
    rows = conn.execute("""
        SELECT wallet, insider_score, classification, win_rate
        FROM user_analysis
        WHERE insider_score >= 60
        AND win_rate >= 0.50
        AND classification != 'low_signal'
        AND updated_at > datetime('now', '-24 hours')
        ORDER BY insider_score DESC
        LIMIT 5
    """).fetchall()
    
    return {
        "queue": states,
        "done_24h": done_24h,
        "analyzed_24h": analyzed_24h,
        "high_scorers": rows,
    }


def sports_status(conn):
    """Sports strategy status."""
    now = int(time.time())
    
    r = conn.execute(
        "SELECT COUNT(*) as c FROM edge_observations WHERE observed_at > datetime('now', '-24 hours')"
    ).fetchone()
    edges_24h = r["c"]
    
    r = conn.execute(
        "SELECT COUNT(*) as c FROM edge_observations WHERE met_threshold = 1 AND observed_at > datetime('now', '-24 hours')"
    ).fetchone()
    signals_24h = r["c"]
    
    rows = conn.execute("""
        SELECT sp.* FROM strategy_positions sp
        JOIN strategies s ON sp.strategy_id = s.id
        WHERE s.strategy_type = 'sports_line_dislocation_v1'
        ORDER BY sp.opened_ts DESC LIMIT 3
    """).fetchall()
    
    return {
        "edges_24h": edges_24h,
        "signals_24h": signals_24h,
        "recent_positions": rows,
    }


def system_health():
    """System resource check."""
    import shutil
    
    # Disk
    usage = shutil.disk_usage("/")
    disk_pct = usage.used / usage.total * 100
    
    # Memory
    with open("/proc/meminfo") as f:
        meminfo = {}
        for line in f:
            parts = line.split()
            meminfo[parts[0].rstrip(":")] = int(parts[1])
    
    total = meminfo["MemTotal"]
    available = meminfo["MemAvailable"]
    ram_pct = (1 - available / total) * 100
    
    swap_total = meminfo.get("SwapTotal", 0)
    swap_free = meminfo.get("SwapFree", 0)
    swap_pct = ((swap_total - swap_free) / swap_total * 100) if swap_total else 0
    
    # DB size
    db_size = os.path.getsize(DB_PATH) / 1024 / 1024
    wal_path = DB_PATH + "-wal"
    try:
        wal_size = os.path.getsize(wal_path) / 1024 / 1024
    except FileNotFoundError:
        wal_size = 0
    
    return {
        "disk_pct": disk_pct,
        "ram_pct": ram_pct,
        "swap_pct": swap_pct,
        "db_mb": db_size,
        "wal_mb": wal_size,
    }


def format_briefing(discovery, sports, health):
    """Format the briefing message."""
    lines = ["☀️ *Daily Briefing*\n"]

    # Discovery
    queued = discovery["queue"].get("queued", 0)
    done = discovery["queue"].get("done", 0)
    lines.append(f"🔍 *Discovery Pipeline*")
    lines.append(f"  Queue: {queued} waiting | {done} done")
    lines.append(f"  Analyzed (24h): {discovery['analyzed_24h']}")
    
    if discovery["high_scorers"]:
        lines.append(f"  🌟 New high-scorers:")
        for r in discovery["high_scorers"]:
            lines.append(f"    {r['wallet'][:10]}… ins={r['insider_score']:.0f} WR={r['win_rate']:.0%} [{r['classification']}]")
    else:
        lines.append(f"  No new high-scorers (ins≥60)")
    
    lines.append("")
    
    # Sports
    lines.append(f"🏀 *Sports Strategy*")
    lines.append(f"  Edges (24h): {sports['edges_24h']} | Signals: {sports['signals_24h']}")
    if sports["recent_positions"]:
        for p in sports["recent_positions"]:
            state = p["state"]
            pnl = p["realized_pnl"] or 0
            icon = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⏳"
            lines.append(f"    {icon} [{state}] {p['outcome'][:30]} ${pnl:.2f}")
    else:
        lines.append(f"  No positions yet")
    
    lines.append("")
    
    # Health
    ram_icon = "🟢" if health["ram_pct"] < 85 else "🟡" if health["ram_pct"] < 95 else "🔴"
    swap_icon = "🟢" if health["swap_pct"] < 40 else "🟡" if health["swap_pct"] < 70 else "🔴"
    lines.append(f"🖥️ *System Health*")
    lines.append(f"  {ram_icon} RAM: {health['ram_pct']:.0f}% | {swap_icon} Swap: {health['swap_pct']:.0f}%")
    lines.append(f"  💾 DB: {health['db_mb']:.0f}MB | WAL: {health['wal_mb']:.1f}MB")
    lines.append(f"  📀 Disk: {health['disk_pct']:.0f}%")
    
    return "\n".join(lines)


def send_telegram(text):
    """Send message via Telegram bot."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    resp = requests.post(url, json={
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }, timeout=15)
    if resp.status_code != 200:
        print(f"Telegram send failed: {resp.status_code} {resp.text}", file=sys.stderr)
        sys.exit(1)
    print(f"Briefing sent to chat {CHAT_ID}")


def main():
    dry_run = "--dry-run" in sys.argv

    conn = get_conn()

    discovery = discovery_status(conn)
    sports = sports_status(conn)
    health = system_health()

    conn.close()

    message = format_briefing(discovery, sports, health)
    
    if dry_run:
        print(message)
        print(f"\n--- Would send to chat {CHAT_ID} ---")
    else:
        send_telegram(message)


if __name__ == "__main__":
    main()
