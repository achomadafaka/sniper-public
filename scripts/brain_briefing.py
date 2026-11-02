#!/usr/bin/env python3
"""
brain.py — Sniper AI one-command context loader.

Run this before any session to get fully oriented in seconds.

Usage:
    .venv/bin/python3 scripts/brain_briefing.py           # standard briefing
    .venv/bin/python3 scripts/brain_briefing.py --full    # + Supabase memories
    .venv/bin/python3 scripts/brain_briefing.py --quick   # services + proof bar only
    .venv/bin/python3 scripts/brain_briefing.py --wallet 0xABCD...  # deep-dive one wallet
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "sniper_tracker.db")

# ANSI colours
G  = "\033[92m"   # green
Y  = "\033[93m"   # yellow
R  = "\033[91m"   # red
B  = "\033[94m"   # blue
C  = "\033[96m"   # cyan
W  = "\033[97m"   # white bold
DIM = "\033[2m"
RST = "\033[0m"

def _db(path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn

def _pct(n, d) -> str:
    if not d: return "—"
    return f"{n/d*100:.1f}%"

def _bar(n: int, total: int, width: int = 28) -> str:
    filled = int(width * n / max(total, 1))
    filled = min(filled, width)
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}] {n}/{total}"


# ─────────────────────────────────────────────────────────────────────────────
# SECTION: Services
# ─────────────────────────────────────────────────────────────────────────────
def section_services():
    print(f"\n{C}{'─'*60}{RST}")
    print(f"{W}🔄  SERVICES{RST}")
    print(f"{C}{'─'*60}{RST}")
    svcs = ["sniper-web", "sniper-stream", "sniper-analyzer", "sniper-paper", "sniper-alerter"]
    result = subprocess.run(
        ["systemctl", "--user", "show", "--property=ActiveState,SubState,ActiveEnterTimestamp"] + svcs,
        capture_output=True, text=True,
    )
    # Parse output blocks
    blocks = result.stdout.strip().split("\n\n")
    for svc, block in zip(svcs, blocks):
        props = dict(line.split("=", 1) for line in block.strip().splitlines() if "=" in line)
        state = props.get("ActiveState", "?")
        sub   = props.get("SubState", "?")
        ts    = props.get("ActiveEnterTimestamp", "")
        color = G if state == "active" else R
        uptime = ""
        try:
            if ts and ts != "n/a":
                dt_obj = datetime.strptime(ts.rsplit(' ', 1)[0], "%a %Y-%m-%d %H:%M:%S")
                diff = datetime.now(timezone.utc) - dt_obj.replace(tzinfo=timezone.utc)
                h, rem = divmod(int(diff.total_seconds()), 3600)
                m = rem // 60
                uptime = f"  {DIM}(up {h}h {m}m){RST}"
        except Exception:
            pass
        icon = "✅" if state == "active" else "❌"
        print(f"  {icon} {color}{svc:<24}{RST} {state}/{sub}{uptime}")


# ─────────────────────────────────────────────────────────────────────────────
# SECTION: Proof Bar
# ─────────────────────────────────────────────────────────────────────────────
def section_proof_bar(conn: sqlite3.Connection):
    print(f"\n{C}{'─'*60}{RST}")
    print(f"{W}📊  PROOF BAR  (target: 150 closed, insider≥60, ≥60% WR, PnL>0){RST}")
    print(f"{C}{'─'*60}{RST}")

    TERMINAL = ("resolution_win", "resolution_loss", "early_exit", "source_exited", "stale", "manual")
    ph = ",".join("?" * len(TERMINAL))

    row = conn.execute(f"""
        SELECT
            COUNT(*) total,
            SUM(CASE WHEN duplicate_of IS NULL THEN 1 ELSE 0 END) real_trades,
            SUM(CASE WHEN status IN ({ph}) AND duplicate_of IS NULL THEN 1 ELSE 0 END) closed,
            SUM(CASE WHEN status='open' AND duplicate_of IS NULL THEN 1 ELSE 0 END) open_ct,
            SUM(CASE WHEN duplicate_of IS NOT NULL THEN 1 ELSE 0 END) dupes,
            SUM(CASE WHEN status IN ({ph}) AND duplicate_of IS NULL
                      AND signal_insider_score >= 60 THEN 1 ELSE 0 END) closed_60,
            SUM(CASE WHEN status IN ({ph}) AND duplicate_of IS NULL
                      AND signal_insider_score >= 60 AND pnl_usdc > 0 THEN 1 ELSE 0 END) wins_60,
            ROUND(SUM(CASE WHEN status IN ({ph}) AND duplicate_of IS NULL
                      AND signal_insider_score >= 60 THEN pnl_usdc ELSE 0 END), 2) pnl_60,
            ROUND(AVG(CASE WHEN status IN ({ph}) AND duplicate_of IS NULL
                      AND signal_insider_score >= 60 THEN roi_percent END), 1) avg_roi_60,
            ROUND(SUM(CASE WHEN status IN ({ph}) AND duplicate_of IS NULL
                      THEN pnl_usdc ELSE 0 END), 2) total_pnl
        FROM paper_trades
    """, TERMINAL * 6).fetchone()

    closed_60  = row["closed_60"] or 0
    wins_60    = row["wins_60"] or 0
    wr_60      = wins_60 / max(closed_60, 1) * 100
    losses_60  = closed_60 - wins_60
    target     = 150
    pct        = closed_60 / target * 100

    bar_color  = G if pct >= 50 else (Y if pct >= 25 else R)
    wr_color   = G if wr_60 >= 60 else (Y if wr_60 >= 55 else R)
    pnl_color  = G if (row["pnl_60"] or 0) > 0 else R
    roi_color  = G if (row["avg_roi_60"] or 0) >= 10 else Y

    print(f"  Closed (ins≥60):  {bar_color}{_bar(closed_60, target)}{RST}  {pct:.1f}%")
    print(f"  Win Rate:         {wr_color}{wr_60:.1f}%{RST}  ({wins_60}W / {losses_60}L)  target ≥60%  {'✅' if wr_60>=60 else '🔴'}")
    print(f"  PnL (ins≥60):     {pnl_color}${row['pnl_60']:,.2f}{RST}   target >$0  {'✅' if (row['pnl_60'] or 0)>0 else '🔴'}")
    print(f"  Avg ROI:          {roi_color}{row['avg_roi_60'] or 0:.1f}%{RST}   target ≥10%  {'✅' if (row['avg_roi_60'] or 0)>=10 else '🔴'}")
    print(f"  Total PnL (all):  ${row['total_pnl']:,.2f}  |  Open: {row['open_ct']}  |  Dupes filtered: {row['dupes']}")

    # Pace estimate
    pace_row = conn.execute(f"""
        SELECT COUNT(*) cnt FROM paper_trades
        WHERE status IN ({ph}) AND duplicate_of IS NULL
          AND signal_insider_score >= 60
          AND closed_at >= DATE('now', '-7 days')
    """, TERMINAL).fetchone()
    pace_per_day = (pace_row["cnt"] or 0) / 7
    days_left    = (target - closed_60) / max(pace_per_day, 0.01)
    pace_color   = G if pace_per_day >= 3 else (Y if pace_per_day >= 1 else R)
    print(f"  7-day pace:       {pace_color}{pace_per_day:.1f}/day{RST}  →  ETA ~{days_left:.0f} days at this pace")

    # --- strategy_positions (strategy_runner) ---
    sp_row = conn.execute("""
        SELECT
            COUNT(*) sp_closed,
            SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) sp_wins,
            ROUND(SUM(realized_pnl), 2) sp_pnl
        FROM strategy_positions
        WHERE state = 'closed'
    """).fetchone()

    sp_closed = sp_row["sp_closed"] or 0
    sp_wins   = sp_row["sp_wins"] or 0
    sp_pnl    = sp_row["sp_pnl"] or 0.0
    sp_losses = sp_closed - sp_wins
    sp_wr     = sp_wins / max(sp_closed, 1) * 100

    # Combined totals
    comb_closed = closed_60 + sp_closed
    comb_wins   = wins_60 + sp_wins
    comb_losses = losses_60 + sp_losses
    comb_wr     = comb_wins / max(comb_closed, 1) * 100
    comb_pnl    = (row["pnl_60"] or 0.0) + sp_pnl

    comb_wr_c   = G if comb_wr >= 60 else (Y if comb_wr >= 55 else R)
    comb_pnl_c  = G if comb_pnl > 0 else R
    sp_wr_c     = G if sp_wr >= 60 else (Y if sp_wr >= 55 else R)
    sp_pnl_c    = G if sp_pnl > 0 else R

    print(f"\n{C}{'─'*60}{RST}")
    print(f"{W}📊  COMBINED LEDGER  paper_trades(ins≥60) + strategy_runner{RST}")
    print(f"{C}{'─'*60}{RST}")
    print(f"   Closed: {comb_closed}  Wins: {comb_wins}  "
          f"WR: {comb_wr_c}{comb_wr:.1f}%{RST}  "
          f"PnL: {comb_pnl_c}${comb_pnl:,.2f}{RST}")
    print(f"   ├ paper_trader (ins≥60): {closed_60} closed, {wins_60} wins, "
          f"{wr_color}{wr_60:.1f}%{RST}, "
          f"{pnl_color}${row['pnl_60']:,.2f}{RST}")
    print(f"   └ strategy_runner:       {sp_closed} closed, {sp_wins} wins, "
          f"{sp_wr_c}{sp_wr:.1f}%{RST}, "
          f"{sp_pnl_c}${sp_pnl:,.2f}{RST}")


# ─────────────────────────────────────────────────────────────────────────────
# SECTION: Signal Pipeline
# ─────────────────────────────────────────────────────────────────────────────
def section_pipeline(conn: sqlite3.Connection):
    print(f"\n{C}{'─'*60}{RST}")
    print(f"{W}🎯  SIGNAL PIPELINE{RST}")
    print(f"{C}{'─'*60}{RST}")

    row = conn.execute("""
        SELECT
            COUNT(*) total_open,
            SUM(CASE WHEN position_insider_score >= 55
                      AND COALESCE(position_speculator_score,0) < 70
                      AND current_price BETWEEN 0.25 AND 0.75
                 THEN 1 ELSE 0 END) qualifying,
            SUM(CASE WHEN position_insider_score >= 60 THEN 1 ELSE 0 END) ins60_plus,
            SUM(CASE WHEN position_insider_score >= 75 THEN 1 ELSE 0 END) ins75_plus,
            MAX(position_insider_score) max_score,
            ROUND(AVG(position_insider_score),1) avg_score
        FROM position_signals WHERE is_open=1
    """).fetchone()

    q_color = G if (row["qualifying"] or 0) >= 5 else (Y if (row["qualifying"] or 0) >= 2 else R)
    print(f"  Open signals:     {row['total_open']:,}  (avg pos score: {row['avg_score']:.1f}, max: {row['max_score']:.0f})")
    print(f"  Qualifying now:   {q_color}{row['qualifying']}{RST}  (ins≥55, spec<70, price 0.25-0.75)")
    print(f"  Score ≥60:        {row['ins60_plus']}   Score ≥75: {row['ins75_plus']}")

    # Untraded qualifying signals
    untraded = conn.execute("""
        SELECT ps.wallet, substr(ps.title,1,42) title,
               ROUND(ps.position_insider_score,0) ins,
               ROUND(ps.current_price,3) price,
               ps.position_classification cls
        FROM position_signals ps
        WHERE ps.is_open=1
          AND ps.position_insider_score >= 55
          AND COALESCE(ps.position_speculator_score,0) < 70
          AND ps.current_price BETWEEN 0.25 AND 0.75
          AND NOT EXISTS (
              SELECT 1 FROM paper_trades pt
              WHERE pt.condition_id = ps.condition_id
                AND pt.wallet = ps.wallet
                AND pt.status = 'open'
                AND pt.duplicate_of IS NULL
          )
        ORDER BY ps.position_insider_score DESC LIMIT 8
    """).fetchall()
    if untraded:
        print(f"\n  {Y}Untraded qualifying signals:{RST}")
        for r in untraded:
            print(f"    ins={r['ins']:.0f}  p={r['price']:.3f}  {r['cls']:<32}  {r['title']}")
    else:
        print(f"  {DIM}All qualifying signals are already being tracked.{RST}")


# ─────────────────────────────────────────────────────────────────────────────
# SECTION: Performance by Signal Type
# ─────────────────────────────────────────────────────────────────────────────
def section_performance(conn: sqlite3.Connection):
    print(f"\n{C}{'─'*60}{RST}")
    print(f"{W}🏆  PAPER TRADE PERFORMANCE  (closed, no dupes){RST}")
    print(f"{C}{'─'*60}{RST}")

    TERMINAL = ("resolution_win", "resolution_loss", "early_exit", "source_exited", "stale", "manual")
    ph = ",".join("?" * len(TERMINAL))

    rows = conn.execute(f"""
        SELECT signal_type,
               COUNT(*) cnt,
               SUM(CASE WHEN pnl_usdc>0 THEN 1 ELSE 0 END) wins,
               ROUND(SUM(pnl_usdc),2) pnl,
               ROUND(AVG(roi_percent),1) avg_roi
        FROM paper_trades
        WHERE status IN ({ph}) AND duplicate_of IS NULL
        GROUP BY signal_type ORDER BY cnt DESC
    """, TERMINAL).fetchall()

    print(f"  {'Signal Type':<26} {'Trades':>6}  {'WR':>6}  {'PnL':>10}  {'AvgROI':>8}")
    print(f"  {'─'*26} {'─'*6}  {'─'*6}  {'─'*10}  {'─'*8}")
    for r in rows:
        wr = r["wins"] / max(r["cnt"], 1) * 100
        wr_c = G if wr >= 60 else (Y if wr >= 50 else R)
        pnl_c = G if (r["pnl"] or 0) > 0 else R
        print(f"  {r['signal_type'] or 'unknown':<26} {r['cnt']:>6}  "
              f"{wr_c}{wr:5.1f}%{RST}  "
              f"{pnl_c}${r['pnl']:>8,.2f}{RST}  "
              f"{r['avg_roi'] or 0:>7.1f}%")


# ─────────────────────────────────────────────────────────────────────────────
# SECTION: Recent Closed Trades
# ─────────────────────────────────────────────────────────────────────────────
def section_recent_trades(conn: sqlite3.Connection, n: int = 8):
    print(f"\n{C}{'─'*60}{RST}")
    print(f"{W}💰  LAST {n} CLOSED TRADES{RST}")
    print(f"{C}{'─'*60}{RST}")

    TERMINAL = ("resolution_win", "resolution_loss", "early_exit", "source_exited", "stale", "manual")
    ph = ",".join("?" * len(TERMINAL))
    rows = conn.execute(f"""
        SELECT substr(wallet,1,8)||'..' wallet,
               substr(market_title,1,36) title,
               signal_type, ROUND(signal_insider_score,0) score,
               ROUND(entry_price,3) entry, ROUND(current_price,3) exit_p,
               ROUND(pnl_usdc,2) pnl, status,
               substr(closed_at,1,10) closed
        FROM paper_trades
        WHERE status IN ({ph}) AND duplicate_of IS NULL
        ORDER BY closed_at DESC LIMIT {n}
    """, TERMINAL).fetchall()

    for r in rows:
        pnl_c = G if (r["pnl"] or 0) > 0 else R
        print(f"  {r['closed']}  {r['wallet']}  "
              f"ins={r['score'] or 0:.0f}  "
              f"p={r['entry'] or 0:.3f}→{r['exit_p'] or 0:.3f}  "
              f"{pnl_c}${r['pnl']:>7,.2f}{RST}  "
              f"{(r['signal_type'] or 'unk'):<22}  {r['title']}")


# ─────────────────────────────────────────────────────────────────────────────
# SECTION: Active Strategies
# ─────────────────────────────────────────────────────────────────────────────
def section_strategies(conn: sqlite3.Connection):
    print(f"\n{C}{'─'*60}{RST}")
    print(f"{W}📋  ACTIVE STRATEGIES{RST}")
    print(f"{C}{'─'*60}{RST}")

    rows = conn.execute("""
        SELECT id, wallet, label, strategy_type, status, parameters,
               ROUND(pnl_usdc,2) pnl, trades_triggered
        FROM strategies WHERE status='active' ORDER BY id
    """).fetchall()
    for r in rows:
        pnl_c = G if (r["pnl"] or 0) >= 0 else R
        wallet_short = (r["wallet"] or "")[:14] + ".." if len(r["wallet"] or "") > 16 else r["wallet"]
        print(f"  ID={r['id']}  {r['label'] or wallet_short:<36}  "
              f"{r['strategy_type']:<28}  "
              f"{pnl_c}${r['pnl'] or 0:>8,.2f}{RST}  "
              f"trades={r['trades_triggered'] or 0}")


# ─────────────────────────────────────────────────────────────────────────────
# SECTION: Top Wallets
# ─────────────────────────────────────────────────────────────────────────────
def section_top_wallets(conn: sqlite3.Connection):
    print(f"\n{C}{'─'*60}{RST}")
    print(f"{W}🔍  TOP WALLETS IN PIPELINE  (open signals, insider≥60){RST}")
    print(f"{C}{'─'*60}{RST}")

    rows = conn.execute("""
        SELECT ua.lb_username username,
               ua.classification cls,
               ROUND(ua.insider_score,0) wal_score,
               ROUND(ua.win_rate,3) wr,
               ua.closed_sample_size sample,
               COUNT(ps.condition_id) open_signals,
               MAX(ps.position_insider_score) max_pos_score,
               ROUND(AVG(ps.position_insider_score),1) avg_pos_score
        FROM user_analysis ua
        JOIN position_signals ps ON ps.wallet=ua.wallet AND ps.is_open=1
        WHERE ua.insider_score >= 60
        GROUP BY ua.wallet
        ORDER BY max_pos_score DESC, ua.insider_score DESC
        LIMIT 15
    """).fetchall()

    print(f"  {'Username':<22}  {'Class':<22}  {'WalSc':>5}  {'WR':>6}  {'Smp':>4}  {'Sigs':>4}  {'MaxPos':>6}  {'AvgPos':>6}")
    print(f"  {'─'*22}  {'─'*22}  {'─'*5}  {'─'*6}  {'─'*4}  {'─'*4}  {'─'*6}  {'─'*6}")
    for r in rows:
        wr_c = G if (r["wr"] or 0) >= 0.65 else (Y if (r["wr"] or 0) >= 0.50 else RST)
        pos_c = G if (r["max_pos_score"] or 0) >= 55 else (Y if (r["max_pos_score"] or 0) >= 40 else RST)
        print(f"  {(r['username'] or '—'):<22}  {r['cls']:<22}  {r['wal_score']:>5.0f}  "
              f"{wr_c}{r['wr'] or 0:>5.1%}{RST}  {r['sample']:>4}  {r['open_signals']:>4}  "
              f"{pos_c}{r['max_pos_score'] or 0:>6.1f}{RST}  {r['avg_pos_score'] or 0:>6.1f}")


# ─────────────────────────────────────────────────────────────────────────────
# SECTION: Wallet deep-dive
# ─────────────────────────────────────────────────────────────────────────────
def section_wallet(conn: sqlite3.Connection, wallet: str):
    print(f"\n{C}{'─'*60}{RST}")
    print(f"{W}🔎  WALLET DEEP-DIVE: {wallet[:20]}...{RST}")
    print(f"{C}{'─'*60}{RST}")

    ua = conn.execute("""
        SELECT * FROM user_analysis WHERE wallet=?
    """, (wallet,)).fetchone()
    if not ua:
        print(f"  {R}Wallet not found in user_analysis.{RST}")
        return
    print(f"  Username:      {ua['lb_username'] or '—'}  (rank #{ua['lb_rank'] or '—'})")
    print(f"  Classification:{ua['classification']}   Insider: {ua['insider_score']:.1f}   Spec: {ua['speculator_score']:.1f}")
    print(f"  Win Rate:      {ua['win_rate']:.1%}  ({ua['wins']}W/{ua['losses']}L, sample={ua['closed_sample_size']})")
    print(f"  high_wr_qualified: {bool(ua['high_winrate_qualified'])}   insider_score: {ua['insider_score']:.0f}")

    sigs = conn.execute("""
        SELECT substr(title,1,44) title, ROUND(position_insider_score,1) pos_sc,
               ROUND(current_price,3) price, position_classification cls,
               buys_24h, buys_since_entry
        FROM position_signals WHERE wallet=? AND is_open=1
        ORDER BY position_insider_score DESC
    """, (wallet,)).fetchall()
    if sigs:
        print(f"\n  Open signals ({len(sigs)}):")
        for s in sigs:
            print(f"    pos_score={s['pos_sc']:.0f}  p={s['price']:.3f}  b24={s['buys_24h']}  {s['cls']:<30}  {s['title']}")

    trades = conn.execute("""
        SELECT signal_type, ROUND(pnl_usdc,2) pnl, status, substr(closed_at,1,10) closed,
               ROUND(entry_price,3) entry, ROUND(signal_insider_score,0) score
        FROM paper_trades WHERE wallet=? AND duplicate_of IS NULL
        ORDER BY opened_at DESC LIMIT 10
    """, (wallet,)).fetchall()
    if trades:
        print(f"\n  Paper trades (last {len(trades)}):")
        for t in trades:
            pnl_c = G if (t["pnl"] or 0) > 0 else R
            print(f"    {t['closed'] or 'open':10}  ins={t['score']:.0f}  entry={t['entry']:.3f}  "
                  f"{pnl_c}${t['pnl'] or 0:>8,.2f}{RST}  {t['status']}  {t['signal_type'] or '—'}")


# ─────────────────────────────────────────────────────────────────────────────
# SECTION: Professional knowledge (always shown in standard + full runs)
# ─────────────────────────────────────────────────────────────────────────────
def section_knowledge():
    """Load knowledge-type Supabase entries — professional PM risk management."""
    root = os.path.join(os.path.dirname(__file__), "..")
    try:
        sys.path.insert(0, root)
        from bot.supabase_helper import read_memories
        entries = [m for m in read_memories() if m.get("type") == "knowledge"]
        if not entries:
            return
        print(f"\n{C}{'─'*60}{RST}")
        print(f"{W}📚  PROFESSIONAL PM KNOWLEDGE  ({len(entries)} entries){RST}")
        print(f"{C}{'─'*60}{RST}")
        for m in entries:
            print(f"\n{Y}▶ {m['name']}{RST}  — {m.get('description', '')}")
            # Show first 600 chars of content to keep output compact
            content = (m.get("content") or "").strip()
            preview = content[:700] + ("…" if len(content) > 700 else "")
            for line in preview.splitlines():
                print(f"  {line}")
    except Exception as e:
        print(f"  {Y}Supabase world events unavailable: {e}{RST}")


# ─────────────────────────────────────────────────────────────────────────────
# SECTION: Supabase memories
# ─────────────────────────────────────────────────────────────────────────────
def section_supabase():
    print(f"\n{C}{'─'*60}{RST}")
    print(f"{W}🧠  SUPABASE BRAIN  (shared memories){RST}")
    print(f"{C}{'─'*60}{RST}")
    root = os.path.join(os.path.dirname(__file__), "..")
    try:
        result = subprocess.run(
            [os.path.join(root, ".venv", "bin", "python3"),
             os.path.join(root, "bot", "supabase_helper.py"), "read"],
            capture_output=True, text=True, cwd=root, timeout=15,
        )
        if result.returncode == 0:
            print(result.stdout)
        else:
            print(f"  {Y}Supabase unavailable: {result.stderr[:120]}{RST}")
    except Exception as e:
        print(f"  {Y}Could not load Supabase memories: {e}{RST}")

    # Also show last 3 session logs
    try:
        result = subprocess.run(
            [os.path.join(root, ".venv", "bin", "python3"),
             os.path.join(root, "bot", "supabase_helper.py"), "read-logs", "3"],
            capture_output=True, text=True, cwd=root, timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            print(f"\n{W}  Recent session logs:{RST}")
            print(result.stdout)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# KNOWN ISSUES reminder
# ─────────────────────────────────────────────────────────────────────────────
def section_known_issues():
    print(f"\n{C}{'─'*60}{RST}")
    print(f"{W}⚠️   KNOWN ISSUES & WATCHLIST{RST}")
    print(f"{C}{'─'*60}{RST}")
    issues = [
        ("geniusMC", "Classified arb_fund but top-15 leaderboard, $2.59M profit — may be misclassified. Re-analyze."),
        ("FlyingPlaty", "Score 100/WR 100% in DB but LIFETIME P&L = -$90K on Polymarket. Misleading score."),
        ("horusheresy", "6-week account, currently -$8K P&L, false positive. Need sample ≥15 for high_winrate_review."),
        ("Sharky6999", "Confirmed automated arb bot (25K+ trades, cross-market pricing gap). Strategy 17 PAUSED."),
        ("pos_score gap", "Patient holders (0 buys_24h) score low even if wallet is proven. Pedigree bonus added v.now."),
        ("'unknown' type", "37 closed trades with signal_type='unknown' = pre-date mapping. Not a real signal type."),
    ]
    for title, desc in issues:
        print(f"  {Y}▸ {title:<18}{RST}  {desc}")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Sniper AI brain briefing")
    parser.add_argument("--full",   action="store_true", help="Include Supabase memories")
    parser.add_argument("--quick",  action="store_true", help="Services + proof bar only")
    parser.add_argument("--wallet", metavar="ADDR",      help="Deep-dive a specific wallet")
    args = parser.parse_args()

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{W}{'═'*60}")
    print(f"  🧠  SNIPER AI BRAIN  —  {now}")
    print(f"{'═'*60}{RST}")

    conn = _db()

    section_services()
    section_proof_bar(conn)

    if args.wallet:
        section_wallet(conn, args.wallet)
        conn.close()
        return

    if not args.quick:
        section_pipeline(conn)
        section_performance(conn)
        section_recent_trades(conn)
        section_strategies(conn)
        section_top_wallets(conn)
        section_known_issues()

    if not args.quick:
        section_knowledge()

    if args.full:
        section_supabase()

    print(f"\n{C}{'─'*60}{RST}")
    print(f"{DIM}Run with --full for all Supabase memories  |  --wallet ADDR for deep-dive{RST}\n")
    conn.close()


if __name__ == "__main__":
    main()
