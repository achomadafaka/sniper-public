#!/usr/bin/env python3
"""Structure-reading exit counterfactual — the operator's 'read the chart' engine, formalized.

Policy S (structure): no absolute profit thresholds. Per position, on its own executable tape:
  - Detect swing lows/highs (local extremum confirmed by k marks on each side).
  - While the structure makes higher-highs/higher-lows: HOLD (no trail, no TP).
  - Exit the whole bag when price breaks the most recent CONFIRMED higher-low (structure break).
  - Before any swing structure exists, only the catastrophe floor (-70%) protects.
  - Optional flow gate (future): tick features confirm the break; not available for this cohort.

Parameters are STRUCTURAL (confirmation width k, in marks), not price levels. Honesty: k is
still a parameter — 'parameter-light', not parameter-free. Sim fills at the breaking mark
x(1-3% haircut). Same cohort + conventions as memecoin_partial_derisk_counterfactual.py.

Benchmarks (same 29 tapes): actual -166.65 | combo(20,.5,trail30) -82.07 | combo+stall(N=3) -39.88.
"""

import sys
import pathlib as _research_pathlib
sys.path.insert(0, str(_research_pathlib.Path(__file__).resolve().parents[2]))
from sniper.research.wallet_execution.publication import legacy_launch as _legacy_launch
_legacy_launch(__name__)
import sqlite3

DB = "/home/bigpapi/Desktop/Sniper/sniper_tracker.db"
CUTOFF = "2026-07-18T13:50:27"
HAIRCUT = 0.03
CATASTROPHE = -0.70


def load_cohort(c):
    rows = c.execute("""
      SELECT id, mint, network_id, entry_price, entry_notional, realized_pnl, exit_reason
           , opened_at, closed_at
      FROM memecoin_positions
      WHERE execution_mode='real' AND is_open=0 AND opened_at>=? ORDER BY opened_at""",
      (CUTOFF,)).fetchall()
    out = []
    for r in rows:
        tape = [t[0] for t in c.execute("""
          SELECT executable_exit_value FROM memecoin_position_marks
          WHERE mint=? AND chain_id=? AND sweep_ts BETWEEN ? AND ?
            AND executable_exit_value IS NOT NULL AND executable_exit_value > 0
          ORDER BY sweep_ts""", (r["mint"], r["network_id"], r["opened_at"], r["closed_at"]))]
        out.append({**dict(r), "tape": tape})
    return out


def swing_lows(prices, k):
    """Indices of confirmed swing lows: strictly lower than the k marks on each side."""
    out = []
    for i in range(k, len(prices) - k):
        w = prices[i - k:i] + prices[i + 1:i + 1 + k]
        if prices[i] < min(w):
            out.append(i)
    return out


def sim_structure(pos, k, partial=None):
    """Replay policy S on one tape. `partial=(x, f)` optionally banks fraction f at the first
    +x crossing first (composing with the live v2 partial), remainder runs the structure read.
    Returns (pnl, exit_tag)."""
    n = pos["entry_notional"]
    e = pos["entry_price"]
    tape = pos["tape"]
    if not e or e <= 0 or len(tape) < 2:
        return pos["realized_pnl"], "no_tape"
    rets = [p / e - 1.0 for p in tape]

    banked, frac_left, start = 0.0, 1.0, 0
    if partial is not None:
        x, f = partial
        for i, r in enumerate(rets):
            if r >= x:
                banked = f * n * ((1.0 + r) * (1.0 - HAIRCUT) - 1.0)
                frac_left, start = 1.0 - f, i
                break

    last_hl = None          # price level of the most recent confirmed higher-low
    prev_low = None
    for i in range(start, len(tape)):
        r = rets[i]
        if r <= CATASTROPHE:
            return banked + frac_left * n * ((1.0 + r) * (1.0 - HAIRCUT) - 1.0), "catastrophe"
        # confirm swing lows with full lookahead up to i (k marks each side, all <= i)
        if i >= 2 * 3:      # cheap guard; real confirmation below
            pass
        lows = [j for j in swing_lows(tape[start:i + 1], k)]
        if lows:
            j = lows[-1]
            level = tape[start + j]
            if prev_low is None or level > prev_low:
                prev_low = level if prev_low is None else prev_low
                # a HIGHER low than the previous confirmed one arms the structure stop
                if last_hl is None or level > last_hl:
                    if last_hl is not None or len(lows) >= 2:
                        last_hl = level
                    elif len(lows) == 1:
                        last_hl = level     # first confirmed low arms too (entry is the ref)
        if last_hl is not None and tape[i] < last_hl:
            return banked + frac_left * n * ((1.0 + r) * (1.0 - HAIRCUT) - 1.0), "structure_break"
    return banked + frac_left * n * (pos["realized_pnl"] / n if n else 0.0), "actual_fate"


def main():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    cohort = load_cohort(c)
    actual = sum(p["realized_pnl"] for p in cohort)
    print(f"n={len(cohort)} actual={actual:+.2f} | combo trail=-82.07 | combo stall(3)=-39.88\n")
    winners = sorted(cohort, key=lambda p: -p["realized_pnl"])[:4]
    for label, partial in (("S only", None), ("partial(20,.5)+S", (0.20, 0.5))):
        for k in (2, 3, 4):
            tot, tags, wd = 0.0, {}, []
            for p in cohort:
                pnl, tag = sim_structure(p, k, partial=partial)
                tot += pnl
                tags[tag] = tags.get(tag, 0) + 1
                if p in winners:
                    wd.append(pnl - p["realized_pnl"])
            print(f"{label:18s} k={k}  total={tot:+8.2f} (d{tot-actual:+7.2f})  "
                  f"exits={tags}  top4winnersD={[f'{d:+.1f}' for d in wd]}")



# Operator protocol: legacy hypothetical conclusions cannot publish by default.
from sniper.research.wallet_execution.publication import quarantine_exports as _quarantine_exports
_quarantine_exports(globals(), ('main',))

if __name__ == "__main__":
    main()
