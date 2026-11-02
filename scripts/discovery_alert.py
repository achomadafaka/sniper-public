#!/usr/bin/env python3
"""
Discovery Alert — runs every 30 min, alerts on new high-value wallets.

Sends Telegram alert when:
- New wallet classified as insider_candidate or emerging_insider
- Wallet newly scores insider >= 70 (wasn't before)
- New wallet matches ALL criteria (matches_criteria = 1)
- Paper trade P&L moves significantly (>$50 change)

Tracks state in a JSON file to avoid duplicate alerts.
"""

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

DB_PATH = Path(__file__).resolve().parent.parent / os.getenv("DB_PATH", "sniper_tracker.db")
STATE_FILE = Path(__file__).resolve().parent.parent / "logs" / "discovery_alert_state.json"

# Load telegram config from .env
BOT_TOKEN = ""
CHAT_ID = ""
env_path = Path(__file__).resolve().parent.parent / ".env"
if env_path.exists():
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k == "TELEGRAM_BOT_TOKEN":
                BOT_TOKEN = v
            if k == "TELEGRAM_CHAT_ID":
                CHAT_ID = v


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "known_insiders": [],
        "known_high_scorers": [],
        "known_criteria_wallets": [],
        "last_pnl": 0,
        "last_run": 0,
    }


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def send_telegram(text):
    if not BOT_TOKEN or not CHAT_ID:
        print(f"No telegram config, would send: {text[:200]}")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }, timeout=15)
        if resp.status_code != 200:
            print(f"Telegram error: {resp.status_code}", file=sys.stderr)
    except Exception as e:
        print(f"Telegram send failed: {e}", file=sys.stderr)


def _wallet_short(wallet):
    w = str(wallet or "")
    return f"{w[:6]}..{w[-4:]}" if len(w) > 12 else w


def _get_top_market(conn, wallet):
    """Get the top open position market title for a wallet."""
    row = conn.execute("""
        SELECT title, outcome, current_price, position_value
        FROM position_signals
        WHERE wallet = ? AND is_open = 1
        ORDER BY position_value DESC
        LIMIT 1
    """, (wallet,)).fetchone()
    if row and row["title"]:
        return row["title"][:50]
    return None


def check_new_insiders(conn, state):
    """Alert on new insider_candidate or emerging_insider classifications."""
    rows = conn.execute("""
        SELECT wallet, insider_score, classification, win_rate, qualifying_roi, pnl
        FROM user_analysis
        WHERE classification IN ('insider_candidate', 'emerging_insider')
    """).fetchall()

    known = set(state.get("known_insiders", []))
    alerts = []

    for r in rows:
        if r["wallet"] not in known:
            alerts.append(r)
            known.add(r["wallet"])

    state["known_insiders"] = list(known)
    return alerts


def check_high_scorers(conn, state):
    """Alert on wallets that newly cross insider >= 70."""
    rows = conn.execute("""
        SELECT wallet, insider_score, classification, win_rate, qualifying_roi, pnl,
               closed_sample_size
        FROM user_analysis
        WHERE insider_score >= 70
        AND classification NOT IN ('arb_fund')
    """).fetchall()

    known = set(state.get("known_high_scorers", []))
    alerts = []

    for r in rows:
        if r["wallet"] not in known:
            alerts.append(r)
            known.add(r["wallet"])

    state["known_high_scorers"] = list(known)
    return alerts


def check_criteria_wallets(conn, state):
    """Alert on wallets that newly match ALL criteria."""
    rows = conn.execute("""
        SELECT wallet, insider_score, classification, win_rate, qualifying_roi, pnl,
               closed_sample_size
        FROM user_analysis
        WHERE matches_criteria = 1
        AND classification NOT IN ('arb_fund')
    """).fetchall()

    known = set(state.get("known_criteria_wallets", []))
    alerts = []

    for r in rows:
        if r["wallet"] not in known:
            alerts.append(r)
            known.add(r["wallet"])

    state["known_criteria_wallets"] = list(known)
    return alerts


def check_pnl_change(conn, state):
    """Alert on significant paper trade PnL changes."""
    r = conn.execute("""
        SELECT SUM(pnl_usdc) as total_pnl,
               COUNT(CASE WHEN status IN ('resolution_win','resolution_loss') THEN 1 END) as closed,
               COUNT(CASE WHEN status = 'resolution_win' THEN 1 END) as wins
        FROM paper_trades
        WHERE duplicate_of IS NULL
    """).fetchone()

    current_pnl = r["total_pnl"] or 0
    last_pnl = state.get("last_pnl", 0)
    change = current_pnl - last_pnl

    state["last_pnl"] = current_pnl

    if abs(change) >= 50:
        return {
            "change": change,
            "total_pnl": current_pnl,
            "closed": r["closed"],
            "wins": r["wins"],
        }
    return None


def _format_wallet_line(conn, r):
    """Format a single wallet alert line with meaningful context."""
    wallet = r["wallet"]
    ins = float(r["insider_score"] or 0)
    wr = float(r["win_rate"] or 0)
    pnl = float(r["pnl"] or 0)
    sample = int(r["closed_sample_size"] or 0) if "closed_sample_size" in r.keys() else 0
    cls = str(r["classification"] or "")

    top_market = _get_top_market(conn, wallet)
    market_line = f"\n  └ _{top_market}_" if top_market else ""

    return (
        f"• `{_wallet_short(wallet)}` ins={ins:.0f} WR={wr:.0%}"
        f"{f' ${pnl:,.0f} PnL' if pnl != 0 else ''}"
        f"{f' ({sample} trades)' if sample > 0 else ''}"
        f"{market_line}"
    )


def main():
    dry_run = "--dry-run" in sys.argv

    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row

    state = load_state()
    messages = []

    # Check for new insiders
    new_insiders = check_new_insiders(conn, state)
    if new_insiders:
        lines = [f"🔴 *New Insider Wallet(s)* ({len(new_insiders)})\n"]
        for r in new_insiders[:5]:
            lines.append(_format_wallet_line(conn, r))
        if len(new_insiders) > 5:
            lines.append(f"  ...+{len(new_insiders)-5} more")
        messages.append("\n".join(lines))

    # Check for high scorers — batch into a single message
    high_scorers = check_high_scorers(conn, state)
    if high_scorers and len(high_scorers) <= 10:
        lines = [f"🌟 *High-Score Wallets (ins≥70)* ({len(high_scorers)})\n"]
        for r in high_scorers[:5]:
            lines.append(_format_wallet_line(conn, r))
        if len(high_scorers) > 5:
            lines.append(f"  ...+{len(high_scorers)-5} more")
        messages.append("\n".join(lines))

    # Check for criteria wallets
    criteria = check_criteria_wallets(conn, state)
    if criteria and len(criteria) <= 10:
        lines = [f"✅ *Wallets Matching ALL Criteria* ({len(criteria)})\n"]
        for r in criteria[:5]:
            lines.append(_format_wallet_line(conn, r))
        if len(criteria) > 5:
            lines.append(f"  ...+{len(criteria)-5} more")
        messages.append("\n".join(lines))

    # Check PnL changes
    pnl_change = check_pnl_change(conn, state)
    if pnl_change:
        direction = "📈" if pnl_change["change"] > 0 else "📉"
        wr = pnl_change["wins"] / pnl_change["closed"] * 100 if pnl_change["closed"] else 0
        messages.append(
            f"{direction} *Paper PnL Update*\n"
            f"  Change: ${pnl_change['change']:+.0f}\n"
            f"  Total: ${pnl_change['total_pnl']:.0f} | {pnl_change['closed']} trades | {wr:.0f}% WR"
        )

    conn.close()

    if messages:
        # Send as ONE message to avoid notification spam
        full_message = "\n\n".join(messages)
        if dry_run:
            print(full_message)
            print(f"\n--- Would send to Telegram ---")
        else:
            send_telegram(full_message)
            print(f"Sent {len(messages)} alert section(s) in 1 message")
    else:
        print("No alerts")

    save_state(state)


if __name__ == "__main__":
    main()
