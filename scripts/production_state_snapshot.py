#!/usr/bin/env python3
"""
production_state_snapshot.py — Single-command production health snapshot.

Emits a JSON document covering:
  - duplicate open positions
  - stopped strategies with open positions
  - paused strategies with open positions
  - open trade ideas by source / approval state
  - strategy_executions status mismatches
  - service / timer state
  - world_analyst / brain freshness
  - recent health issues

Usage:
    python3 scripts/production_state_snapshot.py            # JSON to stdout
    python3 scripts/production_state_snapshot.py --pretty   # human-readable
    python3 scripts/production_state_snapshot.py --out /tmp/snapshot.json
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "sniper_tracker.db"

# ── helpers ───────────────────────────────────────────────────────────────────

def _conn():
    conn = sqlite3.connect(str(DB_PATH), timeout=20)
    conn.row_factory = sqlite3.Row
    return conn


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _systemctl_user(args: list[str]) -> str:
    try:
        r = subprocess.run(
            ["systemctl", "--user"] + args,
            capture_output=True, text=True, timeout=10,
        )
        return r.stdout.strip()
    except Exception as e:
        return f"error: {e}"


# ── checkers ──────────────────────────────────────────────────────────────────

def check_duplicate_open_positions(conn) -> dict:
    rows = conn.execute("""
        SELECT condition_id, outcome, strategy_id, COUNT(*) as n,
               GROUP_CONCAT(position_id) as position_ids
        FROM strategy_positions
        WHERE state = 'open'
        GROUP BY condition_id, outcome, strategy_id
        HAVING COUNT(*) > 1
    """).fetchall()
    items = [dict(r) for r in rows]
    return {"ok": len(items) == 0, "count": len(items), "items": items}


def check_stopped_strategies_with_open_positions(conn) -> dict:
    rows = conn.execute("""
        SELECT s.id as strategy_id, s.label, s.strategy_type, s.status,
               COUNT(sp.position_id) as open_count,
               ROUND(SUM(sp.size_usdc), 2) as total_exposure_usdc
        FROM strategies s
        JOIN strategy_positions sp ON s.id = sp.strategy_id
        WHERE s.status = 'stopped' AND sp.state = 'open'
        GROUP BY s.id
    """).fetchall()
    items = [dict(r) for r in rows]
    return {"ok": len(items) == 0, "count": len(items), "items": items}


def check_paused_strategies_with_open_positions(conn) -> dict:
    rows = conn.execute("""
        SELECT s.id as strategy_id, s.label, s.strategy_type, s.status,
               COUNT(sp.position_id) as open_count,
               ROUND(SUM(sp.size_usdc), 2) as total_exposure_usdc
        FROM strategies s
        JOIN strategy_positions sp ON s.id = sp.strategy_id
        WHERE s.status = 'paused' AND sp.state = 'open'
        GROUP BY s.id
    """).fetchall()
    items = [dict(r) for r in rows]
    # paused+open is expected operational state; flag only if exposure > $200
    total_exposure = sum(i.get("total_exposure_usdc") or 0 for i in items)
    return {
        "ok": total_exposure <= 200,
        "count": len(items),
        "total_exposure_usdc": round(total_exposure, 2),
        "items": items,
        "note": "paused strategies still manage open positions — this is expected unless exposure is large",
    }


def check_stale_executions(conn) -> dict:
    """strategy_executions stuck at 'open' when paired position is 'closed'."""
    rows = conn.execute("""
        SELECT se.id, se.strategy_id, se.condition_id, se.status as exec_status,
               sp.state as pos_state
        FROM strategy_executions se
        JOIN strategy_positions sp
            ON se.condition_id = sp.condition_id
            AND se.strategy_id = sp.strategy_id
        WHERE se.status = 'open' AND sp.state = 'closed'
        LIMIT 50
    """).fetchall()
    items = [dict(r) for r in rows]
    return {"ok": len(items) == 0, "count": len(items), "items": items}


def check_open_trade_ideas(conn) -> dict:
    rows = conn.execute("""
        SELECT id, source_agent, market_title, outcome, direction, confidence,
               current_price, target_price, status,
               adversarial_approved, created_at, expires_at
        FROM trade_ideas
        WHERE status = 'open'
        ORDER BY created_at DESC
    """).fetchall()
    items = [dict(r) for r in rows]
    unapproved = [i for i in items if not i.get("adversarial_approved")]
    return {
        "ok": True,  # open trade ideas are not errors
        "count": len(items),
        "unapproved_count": len(unapproved),
        "items": items,
    }


def check_strategies_summary(conn) -> dict:
    rows = conn.execute("""
        SELECT s.status,
               COUNT(DISTINCT s.id) as strategy_count,
               COUNT(CASE WHEN sp.state = 'open' THEN 1 END) as open_positions,
               ROUND(SUM(CASE WHEN sp.state = 'open' THEN sp.size_usdc ELSE 0 END), 2) as open_exposure_usdc,
               ROUND(SUM(s.pnl_usdc), 2) as total_pnl_usdc
        FROM strategies s
        LEFT JOIN strategy_positions sp ON s.id = sp.strategy_id
        GROUP BY s.status
        ORDER BY s.status
    """).fetchall()
    return {"ok": True, "by_status": [dict(r) for r in rows]}


def check_service_timer_state() -> dict:
    services = [
        "sniper-web", "sniper-stream", "sniper-analyzer",
        "sniper-paper", "sniper-alerter",
    ]
    timers = [
        "sniper-world-analyst", "sniper-finance-analyst",
        "sniper-event-trigger", "sniper-brain-health",
        "sniper-agent-insider-copy", "sniper-agent-wallet-scout",
    ]
    svc_state = {}
    for svc in services:
        out = _systemctl_user(["is-active", f"{svc}.service"])
        svc_state[svc] = out.strip()

    timer_state = {}
    timer_list_raw = _systemctl_user([
        "list-timers", "--no-pager",
        *[f"{t}.timer" for t in timers],
    ])
    for line in timer_list_raw.splitlines():
        for t in timers:
            if t in line:
                parts = line.split()
                # NEXT LEFT LAST PASSED UNIT
                timer_state[t] = line.strip()
                break

    down_services = [k for k, v in svc_state.items() if v != "active"]
    return {
        "ok": len(down_services) == 0,
        "down_services": down_services,
        "services": svc_state,
        "timers_raw": timer_state,
    }


def check_brain_freshness() -> dict:
    """Check world_state and market_boosts freshness via world_analyst --check."""
    try:
        r = subprocess.run(
            [str(ROOT / ".venv/bin/python3"), str(ROOT / "scripts/world_analyst.py"), "--check"],
            capture_output=True, text=True, timeout=30, cwd=str(ROOT),
        )
        fresh = r.returncode == 0
        status_line = (r.stdout + r.stderr).strip().split("\n")[-1]
        return {"ok": fresh, "status": status_line}
    except Exception as e:
        return {"ok": False, "status": f"check failed: {e}"}


def check_recent_brain_health(conn) -> dict:
    """Last brain health observation from Supabase / obs_tool."""
    try:
        sys.path.insert(0, str(ROOT))
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
        from bot.supabase_helper import get_memory
        # Try both type filters — brain_health writes under 'knowledge' or 'observations'
        m = get_memory("brain_health_latest") or get_memory("brain_health_latest", type_filter="knowledge")
        if m:
            return {
                "ok": True,
                "content_preview": str(m.get("content", ""))[:300],
                "updated_at": m.get("updated_at"),
            }
    except Exception:
        pass
    # Not critical — brain health is informational
    return {"ok": True, "status": "brain_health_latest not found in Supabase (non-blocking)"}


# ── main ──────────────────────────────────────────────────────────────────────

def build_snapshot() -> dict:
    conn = _conn()
    snapshot = {
        "snapshot_ts": _now_iso(),
        "checks": {},
    }

    checks = snapshot["checks"]

    checks["duplicate_open_positions"]            = check_duplicate_open_positions(conn)
    checks["stopped_strategies_with_open_pos"]    = check_stopped_strategies_with_open_positions(conn)
    checks["paused_strategies_with_open_pos"]     = check_paused_strategies_with_open_positions(conn)
    checks["stale_executions"]                    = check_stale_executions(conn)
    checks["open_trade_ideas"]                    = check_open_trade_ideas(conn)
    checks["strategies_summary"]                  = check_strategies_summary(conn)
    checks["service_timer_state"]                 = check_service_timer_state()
    checks["brain_freshness"]                     = check_brain_freshness()
    checks["brain_health_observation"]            = check_recent_brain_health(conn)

    # Roll-up: any check with ok=False
    failures = [k for k, v in checks.items() if isinstance(v, dict) and v.get("ok") is False]
    snapshot["overall_ok"] = len(failures) == 0
    snapshot["failures"] = failures

    conn.close()
    return snapshot


def print_pretty(snap: dict) -> None:
    ts = snap["snapshot_ts"]
    ok = snap["overall_ok"]
    failures = snap.get("failures", [])
    checks = snap["checks"]

    print(f"\n{'='*64}")
    print(f"  PRODUCTION STATE SNAPSHOT — {ts}")
    print(f"  Overall: {'✅ OK' if ok else '⚠️  ISSUES FOUND'}")
    if failures:
        print(f"  Failing checks: {', '.join(failures)}")
    print(f"{'='*64}\n")

    def _section(name, title):
        c = checks.get(name, {})
        status = "✅" if c.get("ok") else "❌"
        print(f"{status} {title}")
        return c

    c = _section("duplicate_open_positions", "Duplicate open positions")
    if c.get("count", 0):
        for i in c["items"]:
            print(f"    strategy={i['strategy_id']} {i['outcome']} x{i['n']} ids={i['position_ids']}")

    c = _section("stopped_strategies_with_open_pos", "Stopped strategies with open positions")
    if c.get("count", 0):
        for i in c["items"]:
            print(f"    strategy {i['strategy_id']} [{i['status']}] {i['label'][:45]} — {i['open_count']} pos ${i['total_exposure_usdc']:.2f}")

    c = _section("paused_strategies_with_open_pos", "Paused strategies with open positions")
    for i in c.get("items", []):
        print(f"    strategy {i['strategy_id']} [{i['status']}] {i['label'][:45]} — {i['open_count']} pos ${i['total_exposure_usdc']:.2f}")

    c = _section("stale_executions", "Stale strategy_executions (pos closed, exec=open)")
    if c.get("count", 0):
        print(f"    {c['count']} stale executions need backfill")

    c = _section("open_trade_ideas", "Open trade ideas")
    print(f"    {c.get('count',0)} open, {c.get('unapproved_count',0)} unapproved")
    for i in c.get("items", []):
        appr = "✅" if i.get("adversarial_approved") else "⏳"
        print(f"    {appr} [{i['source_agent']}] {str(i['market_title'])[:50]} conf={i['confidence']}")

    print()
    c = _section("service_timer_state", "Services & timers")
    for svc, state in c.get("services", {}).items():
        icon = "✅" if state == "active" else "❌"
        print(f"    {icon} {svc}: {state}")

    print()
    c = _section("brain_freshness", "Brain freshness (world_state + boosts)")
    print(f"    {c.get('status','')}")

    print()
    c = _section("strategies_summary", "Strategies summary")
    for row in c.get("by_status", []):
        print(f"    {row['status']:10} strategies={row['strategy_count']} open_pos={row['open_positions']} exposure=${row['open_exposure_usdc'] or 0:.2f} pnl=${row['total_pnl_usdc'] or 0:.2f}")

    print()


def main():
    parser = argparse.ArgumentParser(description="Production state snapshot")
    parser.add_argument("--pretty", action="store_true", help="Human-readable output")
    parser.add_argument("--out", help="Write JSON to this file path")
    args = parser.parse_args()

    snap = build_snapshot()

    if args.out:
        Path(args.out).write_text(json.dumps(snap, indent=2, default=str))
        print(f"Snapshot written to {args.out}")

    if args.pretty:
        print_pretty(snap)
    else:
        print(json.dumps(snap, indent=2, default=str))

    sys.exit(0 if snap["overall_ok"] else 1)


if __name__ == "__main__":
    main()
