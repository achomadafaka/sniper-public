#!/usr/bin/env python3
"""One-off audited repair of phantom mirror closes (2026-07-23, Jimothy).

The mirror-close path had no bad-tick guard (fixed in memecoin_copy_trader the same day): a
wrong-denominated DexScreener mid booked paper exits at 3,812x-8,937x entry — +$6.0M of fiction
across arms, poisoning arm totals AND per-wallet copy-PnL (arming evidence; casino847 showed
+$1.02M fake). This repairs the LEDGER: for each closed PAPER position whose exit fill printed
>= SPIKE_RATIO x the position's own maximum clean tape mark (or >= HARD_MULT x entry when no
tape exists), the exit is re-marked at the last clean tape mid before the close (modeled_fill
with the mark's own liquidity) and realized_pnl is recomputed from the fills. Originals are
preserved in memecoin_pnl_repairs. Real (execution_mode='real') rows are NEVER touched — their
fills are chain truth.

Dry-run by default; --apply to write. Idempotent (already-repaired rows are skipped).
"""
import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sniper.util.envfiles import load_env_files  # noqa: E402

load_env_files(str(ROOT))

from sniper.execution.solana_quote import modeled_fill  # noqa: E402
from sniper.util.clock import real_utc_now_iso  # noqa: E402

SPIKE_RATIO = 10.0
HARD_MULT = 100.0


def main(apply=False):
    c = sqlite3.connect(str(ROOT / "sniper_tracker.db"), timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS memecoin_pnl_repairs (
        id INTEGER PRIMARY KEY, position_id INTEGER NOT NULL, fill_id INTEGER NOT NULL,
        old_exit_price REAL, new_exit_price REAL, old_realized REAL, new_realized REAL,
        reason TEXT, repaired_at TEXT NOT NULL, UNIQUE(fill_id))""")
    now = real_utc_now_iso()
    suspects = c.execute("""
        SELECT p.id, p.strategy, p.ticker, p.mint, p.network_id, p.entry_price,
               p.realized_pnl, p.opened_at, p.closed_at,
               f.id AS fill_id, f.price AS exit_price, f.qty AS exit_qty, f.kind
        FROM memecoin_positions p
        JOIN memecoin_fills f ON f.position_id = p.id AND f.kind != 'entry'
        WHERE p.is_open = 0 AND COALESCE(p.execution_mode,'paper') != 'real'
          AND p.entry_price > 0 AND f.price / p.entry_price >= ?
        ORDER BY p.id""", (SPIKE_RATIO,)).fetchall()
    repaired, kept, total_removed = 0, 0, 0.0
    for s in suspects:
        if c.execute("SELECT 1 FROM memecoin_pnl_repairs WHERE fill_id=?",
                     (s["fill_id"],)).fetchone():
            continue
        marks = c.execute("""
            SELECT mid, liquidity FROM memecoin_position_marks
            WHERE mint=? AND chain_id=? AND sweep_ts BETWEEN ? AND ?
              AND COALESCE(quality,'') != 'quarantined_spike' AND mid > 0
            ORDER BY sweep_ts""",
            (s["mint"], s["network_id"] or "solana", s["opened_at"], s["closed_at"])).fetchall()
        max_clean = max((r["mid"] for r in marks), default=0.0)
        mult_vs_tape = (s["exit_price"] / max_clean) if max_clean > 0 else None
        mult_vs_entry = s["exit_price"] / s["entry_price"]
        is_phantom = ((mult_vs_tape is not None and mult_vs_tape >= SPIKE_RATIO)
                      or (mult_vs_tape is None and mult_vs_entry >= HARD_MULT))
        if not is_phantom:
            kept += 1                       # a real 10x+ corroborated by its own tape — keep
            continue
        if marks:
            clean = marks[-1]
            new_price = modeled_fill(clean["mid"], clean["mid"] * (s["exit_qty"] or 0.0),
                                     clean["liquidity"], "sell")
        else:
            new_price = s["entry_price"]    # no tape at all: 0-PnL convention (never invent)
        # recompute realized from all fills with the repaired exit leg
        old_realized = s["realized_pnl"]
        legs = c.execute("SELECT id, kind, price, qty FROM memecoin_fills WHERE position_id=?",
                         (s["id"],)).fetchall()
        realized = 0.0
        for leg in legs:
            px = new_price if leg["id"] == s["fill_id"] else leg["price"]
            if leg["kind"] == "entry":
                realized -= (px or 0.0) * (leg["qty"] or 0.0)
            else:
                realized += (px or 0.0) * (leg["qty"] or 0.0)
        removed = (old_realized or 0.0) - realized
        total_removed += removed
        tag = (f"tape {mult_vs_tape:.0f}x" if mult_vs_tape is not None
               else f"entry {mult_vs_entry:.0f}x, no tape")
        print(f"  pos={s['id']:6d} {s['strategy']:11s} {s['ticker'] or s['mint'][:8]:10s} "
              f"exit {s['exit_price']:.6g} -> {new_price:.6g} ({tag}) "
              f"realized {old_realized:+,.0f} -> {realized:+,.0f}")
        if apply:
            c.execute("INSERT INTO memecoin_pnl_repairs (position_id, fill_id, old_exit_price,"
                      " new_exit_price, old_realized, new_realized, reason, repaired_at)"
                      " VALUES (?,?,?,?,?,?,?,?)",
                      (s["id"], s["fill_id"], s["exit_price"], new_price, old_realized,
                       realized, tag, now))
            c.execute("UPDATE memecoin_fills SET price=?, notional=? WHERE id=?",
                      (new_price, new_price * (s["exit_qty"] or 0.0), s["fill_id"]))
            c.execute("UPDATE memecoin_positions SET realized_pnl=? WHERE id=?",
                      (realized, s["id"]))
        repaired += 1
    if apply:
        c.commit()
    print(f"\n{'APPLIED' if apply else 'DRY-RUN'}: repaired={repaired} "
          f"kept_as_real_winners={kept} fiction_removed=${total_removed:,.0f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    main(apply=ap.parse_args().apply)
