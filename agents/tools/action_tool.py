#!/usr/bin/env python3
"""
Safe strategy action tool for strategy agents.

Agents call this via Bash to apply controlled corrective actions.
Only "safe" actions are permitted — parameter changes require human review.

SAFE (agent can apply immediately):
  pause       — Pause a bleeding active strategy (sets status=paused, pause_reason=agent_safety)
  stop        — Permanently stop a dead/degraded strategy (sets status=stopped)
  flag        — Flag a strategy/wallet for human attention via Telegram
  recommend   — Record a parameter recommendation in Supabase (human must apply)
  alert       — Send urgent Telegram notification
  create      — Create a new copy strategy in paused state (Scout discovery)
  activate    — Activate a paused strategy after vetting

Usage:
    # Pause a strategy
    python3 agents/tools/action_tool.py pause \
        --strategy-id 5 \
        --reason "Strategy lost 22% in 48h, wallet score declining (90→63→41)"

    # Flag for review (sends Telegram, saves observation)
    python3 agents/tools/action_tool.py flag \
        --strategy-id 7 \
        --reason "Wallet 0xABC has not updated positions in 14 days"

    # Record recommendation (human-review required)
    python3 agents/tools/action_tool.py recommend \
        --strategy-id 5 \
        --recommendation "Raise min_insider_score from 55 to 70 — analysis shows <70 entries have 31% win rate vs 67% for >70"
        --reason "Insufficient entry quality filter based on last 30 closed trades"

    # Send urgent alert
    python3 agents/tools/action_tool.py alert \
        --severity critical \
        --message "Wallet 0xABC exited all positions — possible insider event, review open trades"

Outputs JSON to stdout.
"""

import argparse
import json
import os
import sqlite3
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from dotenv import load_dotenv
load_dotenv()

DB_PATH = os.getenv("DB_PATH", "sniper_tracker.db")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_ANON_KEY", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN_AI", os.getenv("TELEGRAM_BOT_TOKEN", ""))
TELEGRAM_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")


def _send_telegram(text: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        return False
    try:
        # Add Eastern time to all agent notifications
        try:
            from zoneinfo import ZoneInfo
            et = datetime.now(ZoneInfo("America/New_York")).strftime("%I:%M %p ET")
            text = f"[{et}] {text}"
        except Exception:
            pass
        payload = json.dumps({
            "chat_id": TELEGRAM_CHAT,
            "text": text,
            "parse_mode": "HTML",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data=payload,
            method="POST",
        )
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=10):
            return True
    except Exception:
        return False


def _write_obs(strategy_type: str, obs_type: str, content: str,
               confidence: str = "high", strategy_ids: list | None = None) -> None:
    """Best-effort Supabase observation write."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    try:
        payload = json.dumps({
            "strategy_type": strategy_type,
            "obs_type": obs_type,
            "content": content,
            "confidence": confidence,
            "wallet_addresses": "[]",
            "strategy_ids": json.dumps(strategy_ids or []),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).encode()
        url = f"{SUPABASE_URL}/rest/v1/strategy_agent_observations"
        req = urllib.request.Request(url, data=payload, method="POST")
        req.add_header("apikey", SUPABASE_KEY)
        req.add_header("Authorization", f"Bearer {SUPABASE_KEY}")
        req.add_header("Content-Type", "application/json")
        req.add_header("Prefer", "return=minimal")
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass  # Non-critical — action already applied


def _get_strategy_info(strategy_id: int) -> dict:
    """Pull basic strategy info for context in notifications."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, label, strategy_type, status FROM strategies WHERE id=?",
            (strategy_id,)
        ).fetchone()
        conn.close()
        return dict(row) if row else {}
    except Exception:
        return {}


def cmd_pause(args) -> int:
    """Pause an active strategy (agent_safety reason)."""
    now = datetime.now(timezone.utc).isoformat()
    info = _get_strategy_info(args.strategy_id)

    try:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        result = conn.execute(
            """UPDATE strategies
               SET status='paused', paused_at=?, pause_reason='agent_safety', updated_at=?
               WHERE id=? AND status='active'""",
            (now, now, args.strategy_id)
        )
        conn.commit()
        affected = result.rowcount
        conn.close()
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1

    if affected == 0:
        print(json.dumps({
            "ok": False,
            "warning": f"Strategy {args.strategy_id} was not active — no change made",
            "current_status": info.get("status", "unknown")
        }))
        return 0

    strategy_name = info.get("label", f"#{args.strategy_id}")
    strategy_type = info.get("strategy_type", "unknown")

    # Telegram alert
    msg = (
        f"🤖 <b>Agent Safety Pause</b>\n"
        f"Strategy <b>{strategy_name}</b> (#{args.strategy_id}) paused automatically.\n"
        f"<i>{args.reason}</i>"
    )
    _send_telegram(msg)

    # Supabase audit record
    _write_obs(
        strategy_type=strategy_type,
        obs_type="action_taken",
        content=f"PAUSED strategy {args.strategy_id} ({strategy_name}). Reason: {args.reason}",
        confidence="high",
        strategy_ids=[args.strategy_id],
    )

    print(json.dumps({
        "ok": True,
        "action": "paused",
        "strategy_id": args.strategy_id,
        "strategy_name": strategy_name,
        "telegram_sent": True,
    }))
    return 0


def cmd_flag(args) -> int:
    """Flag a strategy/wallet for human attention."""
    info = _get_strategy_info(args.strategy_id) if args.strategy_id else {}
    strategy_name = info.get("label", f"#{args.strategy_id}") if args.strategy_id else "N/A"
    strategy_type = info.get("strategy_type", args.strategy or "unknown")

    msg = (
        f"🔍 <b>Agent Flag: Review Required</b>\n"
        f"Strategy: <b>{strategy_name}</b>\n"
        f"<i>{args.reason}</i>"
    )
    sent = _send_telegram(msg)

    _write_obs(
        strategy_type=strategy_type,
        obs_type="recommendation",
        content=f"FLAGGED FOR REVIEW — {args.reason}",
        confidence="medium",
        strategy_ids=[args.strategy_id] if args.strategy_id else [],
    )

    print(json.dumps({
        "ok": True,
        "action": "flagged",
        "telegram_sent": sent,
    }))
    return 0


def cmd_recommend(args) -> int:
    """Record a parameter/config recommendation for human review."""
    info = _get_strategy_info(args.strategy_id) if args.strategy_id else {}
    strategy_type = info.get("strategy_type", args.strategy or "unknown")

    content = (
        f"RECOMMENDATION: {args.recommendation}\n"
        f"Rationale: {args.reason}"
    )
    _write_obs(
        strategy_type=strategy_type,
        obs_type="recommendation",
        content=content,
        confidence="medium",
        strategy_ids=[args.strategy_id] if args.strategy_id else [],
    )

    print(json.dumps({
        "ok": True,
        "action": "recommendation_recorded",
        "note": "Recommendation saved to Supabase. Human review required to apply.",
    }))
    return 0


def cmd_alert(args) -> int:
    """Send an urgent Telegram alert."""
    prefix = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}.get(args.severity, "ℹ️")
    msg = f"{prefix} <b>Strategy Agent Alert</b>\n{args.message}"
    sent = _send_telegram(msg)
    print(json.dumps({"ok": True, "telegram_sent": sent}))
    return 0


def cmd_stop(args) -> int:
    """Stop a strategy permanently (agent decision — won't auto-resume)."""
    now = datetime.now(timezone.utc).isoformat()
    info = _get_strategy_info(args.strategy_id)
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        result = conn.execute(
            "UPDATE strategies SET status='stopped', pause_reason=?, paused_at=? WHERE id=? AND status IN ('active','paused')",
            (f"scout_stop: {args.reason}", now, args.strategy_id),
        )
        conn.commit()
        conn.close()
        if result.rowcount == 0:
            print(json.dumps({"ok": False, "error": "strategy not found or already stopped"}))
            return 1
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1
    label = info.get("label", f"#{args.strategy_id}")
    _send_telegram(f"🔴 <b>Scout stopped</b> strategy <b>{label}</b>\n{args.reason}")
    _write_obs(
        info.get("strategy_type", "unknown"), "action_taken",
        f"STOPPED strategy {label} (id={args.strategy_id}): {args.reason}",
        confidence="high", strategy_ids=[args.strategy_id],
    )
    print(json.dumps({"ok": True, "action": "stop", "strategy_id": args.strategy_id}))
    return 0


def cmd_create(args) -> int:
    """Create a new copy strategy in paused state (Scout discovery)."""
    try:
        params = json.loads(args.params) if args.params else {}
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"invalid params JSON: {e}"}))
        return 1
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        existing = conn.execute(
            "SELECT id, status FROM strategies WHERE strategy_type=? AND wallet=? AND status != 'stopped'",
            (args.strategy_type, args.wallet),
        ).fetchone()
        if existing:
            print(json.dumps({"ok": False, "error": f"Strategy already exists (id={existing['id']}, status={existing['status']})"}))
            conn.close()
            return 1
        from sniper.services.strategy_service import create_strategy
        result = create_strategy(
            conn, wallet=args.wallet, label=args.label or f"Scout: {args.wallet[:10]}",
            strategy_type=args.strategy_type, parameters=params,
        )
        conn.close()
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1
    sid = result.get("id", "?")
    label = args.label or f"Scout: {args.wallet[:10]}"
    _send_telegram(
        f"🔍 <b>Scout created</b> strategy <b>{label}</b> (id={sid})\n"
        f"Wallet: <code>{args.wallet[:14]}...</code>\n"
        f"Reason: {args.reason}\n"
        f"Status: <b>paused</b> (awaiting activation)"
    )
    _write_obs(
        args.strategy_type, "action_taken",
        f"CREATED strategy {label} (id={sid}) for wallet {args.wallet}: {args.reason}",
        confidence="high", strategy_ids=[sid],
    )
    print(json.dumps({"ok": True, "action": "create", "strategy_id": sid, "status": "paused"}))
    return 0


def cmd_activate(args) -> int:
    """Activate a paused strategy (Scout-vetted, ready to trade)."""
    info = _get_strategy_info(args.strategy_id)
    if info.get("status") != "paused":
        print(json.dumps({"ok": False, "error": f"strategy status is '{info.get('status', 'unknown')}', must be 'paused'"}))
        return 1
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.execute(
            "UPDATE strategies SET status='active', pause_reason=NULL, paused_at=NULL WHERE id=? AND status='paused'",
            (args.strategy_id,),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1
    label = info.get("label", f"#{args.strategy_id}")
    _send_telegram(f"🟢 <b>Scout activated</b> strategy <b>{label}</b>\n{args.reason}")
    _write_obs(
        info.get("strategy_type", "unknown"), "action_taken",
        f"ACTIVATED strategy {label} (id={args.strategy_id}): {args.reason}",
        confidence="high", strategy_ids=[args.strategy_id],
    )
    print(json.dumps({"ok": True, "action": "activate", "strategy_id": args.strategy_id}))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Strategy agent action tool")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # pause
    p = sub.add_parser("pause", help="Pause a bleeding active strategy")
    p.add_argument("--strategy-id", required=True, type=int, dest="strategy_id")
    p.add_argument("--reason", required=True)

    # flag
    f = sub.add_parser("flag", help="Flag for human review + send Telegram")
    f.add_argument("--strategy-id", type=int, dest="strategy_id", default=None)
    f.add_argument("--strategy", default=None, help="strategy_type for Supabase record")
    f.add_argument("--reason", required=True)

    # recommend
    r = sub.add_parser("recommend", help="Record a param recommendation (human review required)")
    r.add_argument("--strategy-id", type=int, dest="strategy_id", default=None)
    r.add_argument("--strategy", default=None, help="strategy_type for Supabase record")
    r.add_argument("--recommendation", required=True)
    r.add_argument("--reason", required=True)

    # alert
    a = sub.add_parser("alert", help="Send urgent Telegram notification")
    a.add_argument("--message", required=True)
    a.add_argument("--severity", choices=["info", "warning", "critical"], default="warning")

    # stop
    p_stop = sub.add_parser("stop", help="Stop a dead/degraded strategy permanently")
    p_stop.add_argument("--strategy-id", type=int, required=True, dest="strategy_id")
    p_stop.add_argument("--reason", required=True)
    p_stop.set_defaults(func=cmd_stop)

    # create
    p_create = sub.add_parser("create", help="Create a new copy strategy (paused state)")
    p_create.add_argument("--wallet", required=True, help="0x wallet address")
    p_create.add_argument("--strategy-type", required=True, default="insider_copy_v1", dest="strategy_type")
    p_create.add_argument("--label", default="", help="Strategy display name")
    p_create.add_argument("--params", default="{}", help="JSON strategy parameters")
    p_create.add_argument("--reason", required=True, help="Why this wallet was selected")
    p_create.set_defaults(func=cmd_create)

    # activate
    p_activate = sub.add_parser("activate", help="Activate a paused strategy")
    p_activate.add_argument("--strategy-id", type=int, required=True, dest="strategy_id")
    p_activate.add_argument("--reason", required=True)
    p_activate.set_defaults(func=cmd_activate)

    args = parser.parse_args()
    dispatch = {
        "pause": cmd_pause,
        "flag": cmd_flag,
        "recommend": cmd_recommend,
        "alert": cmd_alert,
        "stop": cmd_stop,
        "create": cmd_create,
        "activate": cmd_activate,
    }
    return dispatch[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
