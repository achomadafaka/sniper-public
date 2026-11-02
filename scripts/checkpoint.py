#!/usr/bin/env python3
"""
scripts/checkpoint.py — Sniper AI context checkpoint.

Writes a compact, structured project state snapshot to Supabase so the next
Claude session can restore context instantly without re-reading logs.

Usage:
  .venv/bin/python3 scripts/checkpoint.py              # full checkpoint
  .venv/bin/python3 scripts/checkpoint.py --quiet      # no terminal output
  .venv/bin/python3 scripts/checkpoint.py --reason "pre-compact"  # tag reason

Triggered by:
  • .git/hooks/post-commit        (after every commit)
  • bot/relay.py PreCompact hook  (before bot session context is compacted)
  • bot/relay.py Stop hook        (when bot session ends)
  • manually by either Claude instance at any time
"""

import os
import sys
import sqlite3
import subprocess
import argparse
from datetime import datetime, timezone
from pathlib import Path

# Load .env
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent

# Ensure project root is on sys.path so `from bot.xxx import ...` works
# whether this script is run via `python scripts/checkpoint.py` or as a module.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _run(cmd: str) -> str:
    """Run a shell command and return stdout, empty string on error."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            cwd=str(PROJECT_ROOT), timeout=15
        )
        return result.stdout.strip()
    except Exception:
        return ""


def _db():
    db_path = os.getenv("DB_PATH", "sniper_tracker.db")
    if not Path(db_path).is_absolute():
        db_path = str(PROJECT_ROOT / db_path)
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def build_checkpoint(reason: str = "manual") -> dict:
    """Collect all project state into a structured dict."""
    now = datetime.now(timezone.utc)
    conn = _db()
    c = conn.cursor()

    # --- Git state ---
    git_branch = _run("git rev-parse --abbrev-ref HEAD")
    git_hash = _run("git rev-parse --short HEAD")
    git_msg = _run("git log -1 --format='%s'")
    git_diff_stat = _run("git diff --stat HEAD~1 HEAD 2>/dev/null | tail -1")

    # --- Proof bar ---
    TERMINAL = ("resolution_win", "resolution_loss", "early_exit",
                "source_exited", "stale", "manual")
    TERMINAL_SQL = "('resolution_win','resolution_loss','early_exit','source_exited','stale','manual')"
    ph = ",".join("?" for _ in TERMINAL)
    try:
        row = c.execute(f"""
            SELECT
                COUNT(*) closed_total,
                SUM(CASE WHEN signal_insider_score>=60 AND status IN {TERMINAL_SQL} THEN 1 ELSE 0 END) closed_60,
                SUM(CASE WHEN signal_insider_score>=60 AND status IN {TERMINAL_SQL} AND pnl_usdc>0 THEN 1 ELSE 0 END) wins_60,
                ROUND(SUM(CASE WHEN signal_insider_score>=60 AND status IN {TERMINAL_SQL} THEN pnl_usdc ELSE 0 END),2) pnl_60,
                ROUND(AVG(CASE WHEN signal_insider_score>=60 AND status IN {TERMINAL_SQL} THEN roi_percent END),1) roi_60,
                SUM(CASE WHEN status='open' AND duplicate_of IS NULL THEN 1 ELSE 0 END) open_ct
            FROM paper_trades
            WHERE duplicate_of IS NULL
        """).fetchone()
        closed_60 = int(row["closed_60"] or 0)
        wins_60 = int(row["wins_60"] or 0)
        wr_60 = wins_60 / max(closed_60, 1) * 100
        pnl_60 = float(row["pnl_60"] or 0)
        roi_60 = float(row["roi_60"] or 0)
        open_ct = int(row["open_ct"] or 0)

        # 7-day pace
        pace_row = c.execute(f"""
            SELECT COUNT(*) cnt FROM paper_trades
            WHERE status IN ({ph}) AND duplicate_of IS NULL
              AND signal_insider_score>=60
              AND closed_at >= DATE('now','-7 days')
        """, TERMINAL).fetchone()
        pace = (pace_row["cnt"] or 0) / 7
        eta_days = int((150 - closed_60) / max(pace, 0.01))
    except Exception:
        closed_60 = wins_60 = 0
        wr_60 = pnl_60 = roi_60 = 0.0
        open_ct = 0
        pace = 0.0
        eta_days = 9999

    # --- Score bucket performance ---
    try:
        buckets = c.execute(f"""
            SELECT
                CASE
                    WHEN signal_insider_score < 55 THEN '<55'
                    WHEN signal_insider_score < 60 THEN '55-59'
                    WHEN signal_insider_score < 65 THEN '60-64'
                    WHEN signal_insider_score < 70 THEN '65-69'
                    WHEN signal_insider_score < 75 THEN '70-74'
                    ELSE '75+'
                END as bucket,
                COUNT(*) t, ROUND(100.0*SUM(CASE WHEN pnl_usdc>0 THEN 1 ELSE 0 END)/COUNT(*),1) wr,
                ROUND(SUM(pnl_usdc),2) pnl, ROUND(AVG(roi_percent),1) roi
            FROM paper_trades
            WHERE status IN ({ph}) AND duplicate_of IS NULL
            GROUP BY bucket ORDER BY bucket
        """, TERMINAL).fetchall()
        bucket_lines = [
            f"  {r['bucket']:6s}: {r['t']:3d}T  WR={r['wr']}%  PnL=${r['pnl']:.0f}  ROI={r['roi']}%"
            for r in buckets
        ]
    except Exception:
        bucket_lines = ["  (error reading buckets)"]

    # --- Signal pipeline ---
    try:
        sig_row = c.execute("""
            SELECT COUNT(*) total,
                   SUM(CASE WHEN position_insider_score>=65 THEN 1 ELSE 0 END) hi,
                   SUM(CASE WHEN position_insider_score>=55 AND position_insider_score<65 THEN 1 ELSE 0 END) mid
            FROM position_signals WHERE is_open=1
        """).fetchone()
        sig_total = int(sig_row["total"] or 0)
        sig_hi = int(sig_row["hi"] or 0)
        sig_mid = int(sig_row["mid"] or 0)
    except Exception:
        sig_total = sig_hi = sig_mid = 0

    # --- Recent closed trades ---
    try:
        recent = c.execute(f"""
            SELECT substr(closed_at,1,10) dt, signal_insider_score ins,
                   ROUND(entry_price,3) ep, ROUND(exit_price,3) xp,
                   ROUND(pnl_usdc,2) pnl, signal_type, exit_reason,
                   substr(market_title,1,40) title
            FROM paper_trades
            WHERE status IN ({ph}) AND duplicate_of IS NULL
            ORDER BY closed_at DESC LIMIT 6
        """, TERMINAL).fetchall()
        recent_lines = [
            f"  {'✅' if r['pnl']>0 else '❌'} {r['dt']} ins={r['ins']:.0f} "
            f"p={r['ep']}→{r['xp']} pnl=${r['pnl']:+.0f} {r['exit_reason']} | {r['title']}"
            for r in recent
        ]
    except Exception:
        recent_lines = ["  (error)"]

    # --- Pending issues ---
    pending = [
        "Monitor 55-64 bucket: 50% WR, -23% ROI. Threshold lowered 60→55 on 2026-03-15.",
        "geniusMC: classified arb_fund but $2.59M profit, $1.4M single win. Re-analyze (priority 100).",
        "majorexploiter: 3 trades, $3.67M PnL. Concentrated directional. Re-analyze (priority 100).",
        "GamblingIsAllYouNeed: arb_fund score 68. Re-analyze (priority 90).",
        "FlyingPlaty: score 100/WR 100% in DB but lifetime PnL=-$90K. Misleading score.",
        "Proof bar WR at 56-58% (target 60%). Low_signal quality gate now active.",
        "SNIPER_API_BASE fix: paper_trader was using port 5000, server on 8080. Fixed 2026-03-16.",
        "PAPER_STOP_LOSS_PCT was not imported → silent NameError → stop-loss never fired. Fixed 2026-03-16.",
    ]

    # --- Strategy positions summary ---
    try:
        strat_rows = conn.execute("""
            SELECT s.label, sp.state, COUNT(*) cnt, ROUND(SUM(sp.realized_pnl), 2) pnl
            FROM strategy_positions sp
            JOIN strategies s ON CAST(sp.strategy_id AS INTEGER) = s.id
            GROUP BY s.label, sp.state ORDER BY s.label
        """).fetchall()
        strat_lines = [
            f"  {r['label']} [{r['state']}]: {r['cnt']} trades, pnl=${r['pnl']}"
            for r in strat_rows
        ] if strat_rows else ["  (no strategy positions)"]
    except Exception:
        strat_lines = ["  (error reading strategy_positions)"]

    # --- Last commit info ---
    last_3_commits = _run("git log -3 --oneline")

    conn.close()

    return {
        "timestamp": now.isoformat(),
        "reason": reason,
        "git": {
            "branch": git_branch,
            "hash": git_hash,
            "message": git_msg,
            "last_3": last_3_commits,
            "diff_stat": git_diff_stat,
        },
        "proof_bar": {
            "closed_60": closed_60,
            "target": 150,
            "wins_60": wins_60,
            "wr_60_pct": round(wr_60, 1),
            "pnl_60": pnl_60,
            "roi_60": roi_60,
            "open": open_ct,
            "pace_per_day": round(pace, 2),
            "eta_days": eta_days,
        },
        "score_buckets": bucket_lines,
        "signal_pipeline": {
            "total_open": sig_total,
            "score_65plus": sig_hi,
            "score_55_64": sig_mid,
        },
        "recent_trades": recent_lines,
        "strategy_positions": strat_lines,
        "pending_issues": pending,
    }


def format_for_supabase(cp: dict) -> str:
    """Format checkpoint dict as a dense, readable memory content string."""
    ts = cp["timestamp"][:16].replace("T", " ")
    g = cp["git"]
    pb = cp["proof_bar"]
    sp = cp["signal_pipeline"]
    reason = cp.get("reason", "manual")

    lines = [
        f"# Sniper Project Checkpoint — {ts} UTC ({reason})",
        "",
        "## Git State",
        f"  Branch: {g['branch']}  Hash: {g['hash']}",
        f"  Last commit: {g['message']}",
        f"  Recent commits:",
    ] + [f"    {l}" for l in g["last_3"].splitlines()] + [
        "",
        "## Proof Bar",
        f"  Progress: {pb['closed_60']}/150 trades at ins≥60",
        f"  Win Rate: {pb['wr_60_pct']}% ({pb['wins_60']}W/{pb['closed_60']-pb['wins_60']}L) — target ≥60%",
        f"  PnL: ${pb['pnl_60']:+.2f}  AvgROI: {pb['roi_60']}%  (targets: PnL>0, ROI≥10%)",
        f"  Open positions: {pb['open']}  Pace: {pb['pace_per_day']:.1f}/day  ETA: ~{pb['eta_days']}d",
        "",
        "## Score Bucket Performance (closed, no dupes)",
    ] + cp["score_buckets"] + [
        "",
        "## Signal Pipeline",
        f"  Total open signals: {sp['total_open']}",
        f"  Score 65+: {sp['score_65plus']}  Score 55-64: {sp['score_55_64']}",
        "",
        "## Recent Closed Trades (last 6)",
    ] + cp["recent_trades"] + [
        "",
        "## Strategy Positions",
    ] + cp.get("strategy_positions", ["  (none)"]) + [
        "",
        "## Pending Issues / Watchlist",
    ] + [f"  • {p}" for p in cp["pending_issues"]] + [
        "",
        "## Key Settings (paper_settings.py + .env)",
        "  PAPER_ENTRY_MIN_INSIDER_SCORE=55  PAPER_MAX_CONCURRENT=25",
        "  PAPER_STOP_LOSS_PCT=0.15  PAPER_STOP_LOSS_RESOLUTION_SKIP_DAYS=3",
        "  PAPER_SKIP_CRYPTO_MARKETS=true  PAPER_MAX_DAYS_TO_RESOLUTION=365",
        "  SNIPER_API_BASE=http://127.0.0.1:8080  (Flask port)",
        "  PROOF_MIN_TRADES=150  PROOF_MIN_WIN_RATE=60  (proof bar gate)",
        "",
        "## How to Restore Context Fast",
        "  .venv/bin/python3 scripts/brain_briefing.py          # live status",
        "  .venv/bin/python3 scripts/brain_briefing.py --full   # + Supabase memories",
        "  .venv/bin/python3 bot/supabase_helper.py read-full  # all memories",
        "  .venv/bin/python3 bot/supabase_helper.py read-logs 5  # session history",
    ]
    return "\n".join(lines)


def save_checkpoint(reason: str = "manual", quiet: bool = False) -> bool:
    """Build checkpoint and save to Supabase. Returns True on success."""
    try:
        from bot.supabase_helper import upsert_memory
    except ImportError:
        if not quiet:
            print("⚠️  supabase_helper not available — checkpoint skipped", file=sys.stderr)
        return False

    if not os.getenv("SUPABASE_URL") or not (os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_ANON_KEY")):
        if not quiet:
            print("⚠️  SUPABASE_URL/KEY not set — checkpoint skipped", file=sys.stderr)
        return False

    cp = build_checkpoint(reason)
    content = format_for_supabase(cp)

    try:
        upsert_memory(
            type="project",
            name="Session Checkpoint",
            description=(
                f"Auto-snapshot: {cp['proof_bar']['closed_60']}/150 proof trades, "
                f"WR={cp['proof_bar']['wr_60_pct']}%, "
                f"git={cp['git']['hash']} ({reason})"
            ),
            content=content,
        )
        if not quiet:
            pb = cp["proof_bar"]
            print(
                f"✅ Checkpoint saved to Supabase  "
                f"[{pb['closed_60']}/150 trades, WR={pb['wr_60_pct']}%, "
                f"git={cp['git']['hash']}, reason={reason}]"
            )
        return True
    except Exception as e:
        if not quiet:
            print(f"❌ Checkpoint failed: {e}", file=sys.stderr)
        return False


def main():
    parser = argparse.ArgumentParser(description="Save Sniper project checkpoint to Supabase")
    parser.add_argument("--quiet", "-q", action="store_true", help="Suppress terminal output")
    parser.add_argument("--reason", default="manual", help="Reason tag for this checkpoint")
    parser.add_argument("--print-only", action="store_true", help="Print checkpoint without saving")
    args = parser.parse_args()

    if args.print_only:
        cp = build_checkpoint(args.reason)
        print(format_for_supabase(cp))
        return

    ok = save_checkpoint(reason=args.reason, quiet=args.quiet)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
