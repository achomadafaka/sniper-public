#!/usr/bin/env python3
"""
Snapshot live system state into the HANDOFF.md "System State RIGHT NOW" section.

Replaces ONLY the auto-generated section between:
  ## System State RIGHT NOW
and the next top-level ## heading (or EOF).

All other sections (Completed, Pending, etc.) are preserved exactly.

Runs every 20 minutes via sniper-handoff-snapshot.timer.
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HANDOFF_PATH = Path.home() / ".claude/projects/-home-sniper-sniper/memory/HANDOFF.md"
DB_PATH = ROOT / os.getenv("DB_PATH", "sniper_tracker.db")

SERVICES = [
    "sniper-stream",
    "sniper-analyzer",
    "sniper-alerter",
    "sniper-paper",
    "sniper-web",
]

MEMORY_SERVICES = ["sniper-stream", "sniper-analyzer"]


def _run(cmd, timeout=10):
    """Run a command, return stdout string or empty string on failure."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def _brain_health():
    """Run brain_health.py --json and return (overall, warn_lines)."""
    out = _run(
        [sys.executable, str(ROOT / "scripts/brain_health.py"), "--json"],
        timeout=30,
    )
    if not out:
        return "UNKNOWN", []
    try:
        data = json.loads(out)
    except Exception:
        return "UNKNOWN", []
    overall = data.get("overall", "UNKNOWN")
    checks = data.get("checks", [])
    warn_lines = []
    for c in checks:
        sev = c.get("severity", "")
        if sev in ("WARN", "FAIL"):
            icon = "❌" if sev == "FAIL" else "⚠️"
            warn_lines.append(f"  {icon} {c.get('check_id', '?')} {c.get('name', '')}: {c.get('message', '')}")
    return overall, warn_lines


def _service_states():
    """Return dict of {service: 'active'|'inactive'|'failed'|'unknown'}."""
    states = {}
    for svc in SERVICES:
        out = _run(["systemctl", "--user", "is-active", svc])
        states[svc] = out if out else "unknown"
    return states


def _service_mem(svc):
    """Return (current_mb, max_mb) for a service, or (None, None)."""
    out = _run(["systemctl", "--user", "show", svc,
                "--property=MemoryCurrent,MemoryMax"])
    cur = max_m = None
    for line in out.splitlines():
        k, _, v = line.partition("=")
        try:
            val = int(v)
        except (ValueError, TypeError):
            continue
        if k == "MemoryCurrent" and val > 0 and val != 2**64 - 1:
            cur = val / (1024 * 1024)
        elif k == "MemoryMax" and val > 0 and val != 2**64 - 1:
            max_m = val / (1024 * 1024)
    return cur, max_m


def _last_analyzer_run():
    """Return (ts_str, open_signal_count) from SQLite."""
    if not DB_PATH.exists():
        return None, None
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        row = conn.execute(
            "SELECT MAX(committed_ts) FROM analysis_runs WHERE status='committed'"
        ).fetchone()
        last_ts = row[0] if row else None
        # Count signals written in the last committed run (batch size × passes)
        sig_row = conn.execute(
            """SELECT COUNT(*) FROM signal_history
               WHERE analyzed_ts = (SELECT MAX(committed_ts) FROM analysis_runs
                                    WHERE status='committed')"""
        ).fetchone()
        open_sigs = sig_row[0] if sig_row else 0
        conn.close()
        return last_ts, open_sigs
    except Exception:
        return None, None


def _top_signals(n=5):
    """Return list of (wallet, outcome, size_usdc, price, title) for top open insider signals."""
    if not DB_PATH.exists():
        return []
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        rows = conn.execute(
            """
            SELECT sh.wallet, sh.outcome, MAX(sh.entry_notional) as notional,
                   sh.entry_price, sh.title,
                   COALESCE(ua.lb_username, substr(sh.wallet, 1, 10)) as display_name
            FROM signal_history sh
            JOIN user_analysis ua ON sh.wallet = ua.wallet
            WHERE sh.position_insider_score >= 60
              AND ua.classification NOT IN ('low_signal', 'speculator_bot', 'arb_fund')
              AND sh.analyzed_ts > (strftime('%s','now') - 86400 * 3)
              AND sh.wallet NOT IN (SELECT wallet FROM ignored_wallets)
            GROUP BY sh.wallet, sh.condition_id, sh.outcome
            ORDER BY notional DESC
            LIMIT ?
            """,
            (n,),
        ).fetchall()
        conn.close()
        return rows
    except Exception:
        return []


def _paper_trades_today(n=8):
    """Return list of recent paper trades (opened today or still open)."""
    if not DB_PATH.exists():
        return []
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        rows = conn.execute(
            """
            SELECT pt.wallet, pt.outcome, pt.status, pt.roi_percent, pt.market_title,
                   COALESCE(ua.lb_username, substr(pt.wallet, 1, 10)) as display_name
            FROM paper_trades pt
            LEFT JOIN user_analysis ua ON pt.wallet = ua.wallet
            WHERE pt.opened_at >= datetime('now', '-1 day')
            ORDER BY pt.opened_at DESC
            LIMIT ?
            """,
            (n,),
        ).fetchall()
        conn.close()
        return rows
    except Exception:
        return []


def build_section():
    """Build the full System State RIGHT NOW section string."""
    now_utc = datetime.now(timezone.utc)
    ts_str = now_utc.strftime("%Y-%m-%d %H:%M UTC")

    lines = [f"## System State RIGHT NOW *(auto-updated {ts_str})*", ""]

    # Brain health
    overall, warn_lines = _brain_health()
    icon_map = {"HEALTHY": "✅", "DEGRADED": "⚠️", "UNHEALTHY": "❌"}
    icon = icon_map.get(overall, "❓")
    lines.append("### Brain Health")
    lines.append(f"Brain: {icon} {overall}")
    if warn_lines:
        lines.extend(warn_lines)
    else:
        lines.append("  All checks passing")
    lines.append("")

    # Service states
    lines.append("### Services")
    states = _service_states()
    for svc, state in states.items():
        s_icon = "✅" if state == "active" else "❌"
        lines.append(f"  {s_icon} {svc}: {state}")
    lines.append("")

    # Memory
    lines.append("### Memory")
    for svc in MEMORY_SERVICES:
        cur, max_m = _service_mem(svc)
        if cur is not None and max_m is not None:
            lines.append(f"  {svc}: {cur:.0f}MB / {max_m:.0f}MB")
        elif cur is not None:
            lines.append(f"  {svc}: {cur:.0f}MB")
        else:
            lines.append(f"  {svc}: (memory unavailable)")
    lines.append("")

    # Analyzer
    last_ts, open_sigs = _last_analyzer_run()
    lines.append("### Analyzer")
    if last_ts:
        dt = datetime.fromtimestamp(last_ts, tz=timezone.utc)
        mins_ago = int((now_utc - dt).total_seconds() / 60)
        time_str = dt.strftime("%H:%M UTC")
        lines.append(f"  Last run: {time_str} ({mins_ago}m ago) | Last batch: {open_sigs} signals")
    else:
        lines.append("  Last run: unknown")
    lines.append("")

    # Top signals
    sigs = _top_signals(5)
    if sigs:
        lines.append("### Top Insider Signals (open)")
        for wallet, outcome, size, price, title, display_name in sigs:
            short_title = (title[:60] + "…") if title and len(title) > 60 else (title or "")
            lines.append(f"  - [{display_name}] {outcome} ${size:,.0f} @ {price:.2f} | {short_title}")
        lines.append("")

    # Paper trades today
    trades = _paper_trades_today(8)
    if trades:
        lines.append("### Paper Trades Today")
        for wallet, outcome, status, roi, title, display_name in trades:
            short_title = (title[:50] + "…") if title and len(title) > 50 else (title or "")
            if status == "open":
                roi_str = "open —"
            elif roi is not None:
                pct = f"{roi:+.1f}%"
                roi_str = f"{status} {pct}"
            else:
                roi_str = status
            lines.append(f"  - [{display_name}] {outcome} | {roi_str} | {short_title}")
        lines.append("")

    return "\n".join(lines)


def _replace_section(content: str, new_section: str) -> str:
    """Replace the System State section in content, preserving everything else."""
    # Find section start
    start_pat = re.compile(r"^## System State RIGHT NOW.*$", re.MULTILINE)
    m = start_pat.search(content)
    if not m:
        # Section not found — append before the trailing How-to section if present
        end_pat = re.compile(r"^## How to Use", re.MULTILINE)
        em = end_pat.search(content)
        if em:
            insert_at = em.start()
            return content[:insert_at] + new_section + "\n---\n\n" + content[insert_at:]
        return content + "\n---\n\n" + new_section + "\n"

    section_start = m.start()

    # Find where the section ends: next ## heading at column 0, or EOF
    end_pat = re.compile(r"^---\s*\n", re.MULTILINE)
    em = end_pat.search(content, section_start + 1)
    if em:
        section_end = em.end()  # include the --- separator
        return content[:section_start] + new_section + "\n---\n\n" + content[section_end:]
    else:
        return content[:section_start] + new_section + "\n"


def main():
    if not HANDOFF_PATH.exists():
        print(f"HANDOFF.md not found at {HANDOFF_PATH}", file=sys.stderr)
        return 2

    content = HANDOFF_PATH.read_text()
    new_section = build_section()
    updated = _replace_section(content, new_section)

    # Atomic write
    tmp = HANDOFF_PATH.with_suffix(".tmp")
    tmp.write_text(updated)
    os.replace(tmp, HANDOFF_PATH)

    now_str = datetime.now(timezone.utc).strftime("%H:%M UTC")
    print(f"HANDOFF.md updated at {now_str}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
