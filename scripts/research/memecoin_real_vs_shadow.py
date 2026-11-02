#!/usr/bin/env python3
"""
Memecoin real-vs-shadow diagnostic (read-only).

Answers three operator questions off the live sniper_tracker.db:

  1. ENTRY SLIPPAGE  — for every real fill, how much worse was our on-chain
     entry price than (a) the paper `real_shadow` twin on the same
     (mint, source_user_id) signal, and (b) the DEX quote trajectory
     (memecoin_quote_observations_v2) around the fill time. This is the
     execution-quality gate: real should track shadow within a few %.

  2. WALLET PROMOTION — per-wallet copy-PnL of the `real_shadow` arm (the exact
     rails that run live), realized + open marked-to-market, TP/stop counts,
     flagged against current real_approved (arming is global). Ranks who to add
     to (or drop from) the real allowlist.

  3. SAFETY-GATE IMPACT — for each mint blocked in memecoin_pending_entries
     (token_safety / slippage_cap / tx-retry), what the same mint did in the
     paper arms (counterfactual: did the gate save us or cost us?).

Usage:
    python3 scripts/research/memecoin_real_vs_shadow.py [--db sniper_tracker.db]
                                                        [--real-live 2026-07-10T23:02:00+00:00]

Nothing is written. Numbers are only as honest as the DB; real N is tiny —
read directionally, not as a verdict.
"""
from __future__ import annotations

import sys
import pathlib as _research_pathlib
sys.path.insert(0, str(_research_pathlib.Path(__file__).resolve().parents[2]))
from sniper.research.wallet_execution.publication import legacy_launch as _legacy_launch
_legacy_launch(__name__)
import argparse
import sqlite3
from datetime import datetime, timezone

REAL_ARM = ("alpha_b", "real")            # (strategy, execution_mode) of the live real leg
SHADOW_ARM = ("real_shadow", "paper")     # paper arm that mirrors the real rails


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _conn(db: str) -> sqlite3.Connection:
    c = sqlite3.connect(db, timeout=30)
    c.row_factory = sqlite3.Row
    return c


# ---------------------------------------------------------------------------
# 1. Entry slippage
# ---------------------------------------------------------------------------
def entry_slippage(c: sqlite3.Connection) -> None:
    print("=" * 78)
    print("1. ENTRY SLIPPAGE — real fill vs shadow twin vs DEX quote")
    print("=" * 78)
    reals = c.execute(
        """SELECT id, ticker, mint, source_user_id, source_handle, entry_price,
                  entry_notional, opened_at, is_open, realized_pnl, exit_reason
             FROM memecoin_positions
            WHERE strategy=? AND execution_mode=? ORDER BY opened_at""",
        REAL_ARM,
    ).fetchall()
    if not reals:
        print("  no real fills found.")
        return

    hdr = f"{'ticker':10}{'realPx':>13}{'shadowPx':>13}{'shad%':>8}{'dexPx@fill':>13}{'dex%':>8}  note"
    print(hdr)
    shad_deltas, dex_deltas = [], []
    for r in reals:
        real_px = r["entry_price"]
        note = f"open={r['is_open']} pnl={r['realized_pnl']} {r['exit_reason'] or ''}"

        # (a) shadow twin: same (mint, source_user_id), nearest opened_at
        twin = None
        if r["source_user_id"]:
            twin = c.execute(
                """SELECT entry_price, opened_at,
                          ABS(strftime('%s',opened_at)-strftime('%s',?)) dt
                     FROM memecoin_positions
                    WHERE mint=? AND source_user_id=? AND strategy=? AND execution_mode=?
                    ORDER BY dt LIMIT 1""",
                (r["opened_at"], r["mint"], r["source_user_id"], *SHADOW_ARM),
            ).fetchone()
        shad_px = twin["entry_price"] if twin else None
        shad_pct = ((real_px / shad_px) - 1) * 100 if (shad_px and real_px) else None

        # (b) DEX quote nearest the fill time
        dex = c.execute(
            """SELECT price, observed_at,
                      ABS(strftime('%s',observed_at)-strftime('%s',?)) dt
                 FROM memecoin_quote_observations_v2
                WHERE mint=? AND price>0 ORDER BY dt LIMIT 1""",
            (r["opened_at"], r["mint"]),
        ).fetchone()
        dex_px = dex["price"] if dex else None
        dex_pct = ((real_px / dex_px) - 1) * 100 if (dex_px and real_px) else None

        if shad_pct is not None:
            shad_deltas.append(shad_pct)
        if dex_pct is not None:
            dex_deltas.append(dex_pct)

        def f(x, w=13, p=6):
            return (f"{x:.{p}g}".rjust(w)) if x is not None else "—".rjust(w)

        def pc(x, w=8):
            return (f"{x:+.1f}%".rjust(w)) if x is not None else "—".rjust(w)

        print(f"{r['ticker'][:10]:10}{f(real_px)}{f(shad_px)}{pc(shad_pct)}{f(dex_px)}{pc(dex_pct)}  {note}")

    def avg(xs):
        return sum(xs) / len(xs) if xs else float("nan")

    print("-" * 78)
    print(f"  mean real-vs-shadow entry premium : {avg(shad_deltas):+.1f}%  (n={len(shad_deltas)})")
    print(f"  mean real-vs-DEX-quote  premium   : {avg(dex_deltas):+.1f}%  (n={len(dex_deltas)})")
    print("  >0% = real paid up vs the paper mark / DEX mid → execution drag.\n")


# ---------------------------------------------------------------------------
# 2. Wallet promotion ranking
# ---------------------------------------------------------------------------
def wallet_ranking(c: sqlite3.Connection, real_live: str, min_closed: int = 2) -> None:
    print("=" * 78)
    print(f"2. WALLET PROMOTION — real_shadow copy-PnL per wallet (opened >= {real_live[:10]})")
    print("=" * 78)
    rows = c.execute(
        """SELECT COALESCE(w.handle, p.source_user_id) wallet, p.source_user_id,
                  w.real_approved,
                  SUM(p.is_open=0) closed,
                  ROUND(SUM(CASE WHEN p.is_open=0 THEN p.realized_pnl ELSE 0 END),1) realpnl,
                  SUM(p.exit_reason='real_tp') tp,
                  SUM(p.exit_reason='b_stop') stop,
                  ROUND(SUM(CASE WHEN p.is_open=1 AND p.entry_price>0 AND p.last_quote_price IS NOT NULL
                       THEN (p.last_quote_price/p.entry_price-1)*p.entry_notional ELSE 0 END),1) unrl
             FROM memecoin_positions p
             LEFT JOIN memecoin_wallets w ON p.source_user_id=w.source_user_id
            WHERE p.strategy=? AND p.execution_mode=? AND p.opened_at>=?
            GROUP BY p.source_user_id HAVING closed>=?""",
        (*SHADOW_ARM, real_live, min_closed),
    ).fetchall()
    ranked = sorted(rows, key=lambda r: (r["realpnl"] or 0) + (r["unrl"] or 0), reverse=True)

    print(f"{'wallet':24}{'clo':>4}{'realPnL':>9}{'unrl':>8}{'total':>9}{'TP':>3}{'stp':>4}{'appr':>6}")
    promote, demote = [], []
    for r in ranked:
        total = (r["realpnl"] or 0) + (r["unrl"] or 0)
        appr = bool(r["real_approved"])
        print(f"@{str(r['wallet'])[:23]:23}{r['closed']:>4}{r['realpnl'] or 0:>9.1f}"
              f"{r['unrl'] or 0:>8.1f}{total:>9.1f}{r['tp']:>3}{r['stop']:>4}"
              f"{'Y' if appr else chr(183):>6}")
        if total >= 40 and not appr:
            promote.append((r["wallet"], total, r["closed"]))
        if total <= -40 and appr:
            demote.append((r["wallet"], total, r["stop"]))

    print("-" * 78)
    if promote:
        print("  PROMOTE candidates (>= +$40 shadow copy-PnL, not on allowlist):")
        for w, t, n in promote:
            print(f"     @{w}  +${t:.0f}  ({n} closes)  ← verify chain has a live executor before arming")
    if demote:
        print("  DEMOTE candidates (<= -$40 shadow copy-PnL, currently approved):")
        for w, t, s in demote:
            print(f"     @{w}  -${abs(t):.0f}  ({s} stops)")
    print("  NOTE: 2-13 closes/wallet — noisy. The auto-demote rule is the real guard.\n")


# ---------------------------------------------------------------------------
# 3. Safety-gate counterfactual
# ---------------------------------------------------------------------------
def safety_counterfactual(c: sqlite3.Connection) -> None:
    print("=" * 78)
    print("3. SAFETY-GATE IMPACT — blocked real entries vs paper counterfactual")
    print("=" * 78)
    blocked = c.execute(
        """SELECT mint, resolution_reason, network_id, COUNT(*) attempts
             FROM memecoin_pending_entries WHERE status='failed'
            GROUP BY mint, resolution_reason"""
    ).fetchall()
    if not blocked:
        print("  no failed pending entries.")
        return
    for b in blocked:
        # best mark-to-market the same mint achieved in real_shadow (what we'd have earned)
        shad = c.execute(
            """SELECT ROUND(SUM(CASE WHEN is_open=0 THEN realized_pnl ELSE 0 END),1) closed_pnl,
                      COUNT(*) n, MAX(max_gain_pct) best_gain,
                      SUM(exit_reason='real_tp') tp, SUM(exit_reason IN ('unquotable')) dead
                 FROM memecoin_positions
                WHERE mint=? AND strategy=? AND execution_mode=?""",
            (b["mint"], *SHADOW_ARM),
        ).fetchone()
        reason = b["resolution_reason"] or "tx_fail/retry"
        if not shad or shad["n"] == 0:
            verdict = "no shadow twin"
        elif (shad["closed_pnl"] or 0) > 15:
            verdict = f"COST us ~${shad['closed_pnl']:.0f} (shadow won, {shad['tp']} TP)"
        elif (shad["dead"] or 0) == shad["n"]:
            verdict = "SAVED (token went dead/unquotable)"
        elif (shad["closed_pnl"] or 0) < -15:
            verdict = f"SAVED ~${abs(shad['closed_pnl']):.0f} (shadow lost)"
        else:
            verdict = f"marginal (shadow {shad['closed_pnl']:+.0f})"
        print(f"  [{reason:14} net={str(b['network_id']):8}] {b['mint'][:10]}  "
              f"x{b['attempts']} attempts → {verdict}")
    print()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="sniper_tracker.db")
    ap.add_argument("--real-live", default="2026-07-10T23:02:00+00:00",
                    help="ISO ts the real leg went live; scopes the fair-window wallet ranking")
    args = ap.parse_args()
    c = _conn(args.db)
    print(f"\nmemecoin real-vs-shadow diagnostic — db={args.db}\n")
    entry_slippage(c)
    wallet_ranking(c, args.real_live)
    safety_counterfactual(c)



# Operator protocol: legacy hypothetical conclusions cannot publish by default.
from sniper.research.wallet_execution.publication import quarantine_exports as _quarantine_exports
_quarantine_exports(globals(), ('wallet_ranking', 'safety_counterfactual', 'main'))

if __name__ == "__main__":
    main()
