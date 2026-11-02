"""Score existing trade_ideas against market_outcomes.

This is the live scoring pipeline — the simple complement to the backtesting
harness. Whenever a market resolves, this script finds matching trade_ideas
and fills in resolution_outcome, resolution_price, and brier_score.

Usage:
    python3 scripts/score_trade_ideas.py           # score all unresolved ideas
    python3 scripts/score_trade_ideas.py --summary  # show scoring stats
    python3 scripts/score_trade_ideas.py --dry-run  # preview without writing

Run this on a cron or after any market_outcomes update.
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import get_connection  # noqa: E402

DB_PATH = "/home/sniper/sniper/sniper_tracker.db"


def _ensure_brier_column(conn) -> None:
    """Add brier_score column to trade_ideas if it doesn't exist yet."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(trade_ideas)").fetchall()}
    if "brier_score" not in cols:
        conn.execute("ALTER TABLE trade_ideas ADD COLUMN brier_score REAL")
        conn.commit()
        print("  [migration] Added brier_score column to trade_ideas")


def score_unresolved(conn, dry_run: bool = False) -> list[dict]:
    """Find trade_ideas with no resolution and score them against market_outcomes.

    Handles Polymarket's data quirk where market_outcomes may store only one side
    (e.g., only 'no' row for a market that resolved YES). We infer the actual
    outcome probability from whichever side is available:
      - outcome='yes' row: p_yes = resolved_price
      - outcome='no' row:  p_yes = 1.0 - resolved_price
    Then score the analyst's stated confidence against p_yes (for buy_yes)
    or p_no = 1 - p_yes (for buy_no).
    """

    # Fetch ANY market_outcomes row for each trade_idea's condition_id.
    # We use a priority: prefer the 'yes' row, fall back to 'no' row.
    rows = conn.execute(
        """
        SELECT
            ti.id,
            ti.condition_id,
            ti.market_title,
            ti.direction,
            ti.confidence,
            ti.source_agent,
            mo.outcome        AS mo_outcome,
            mo.resolved_price AS mo_resolved_price,
            mo.resolved_at    AS mo_resolved_at
        FROM trade_ideas ti
        JOIN market_outcomes mo
          ON mo.condition_id = ti.condition_id
         AND mo.outcome      IN ('yes', 'no')
        WHERE ti.resolution_outcome IS NULL
          AND mo.resolved_price IS NOT NULL
        GROUP BY ti.id
        HAVING mo.outcome = MIN(mo.outcome)   -- deterministic: 'no' < 'yes', prefer 'no'
        """
    ).fetchall()

    scored = []
    seen_ids: set[int] = set()
    for row in rows:
        (tid, cid, title, direction, raw_conf, agent,
         mo_out, resolved_price, resolved_at) = row

        if tid in seen_ids:
            continue
        seen_ids.add(tid)

        # Derive p_yes regardless of which side we got
        rp = float(resolved_price)
        if mo_out == "yes":
            p_yes = rp
        else:
            # 'no' row: resolved_price is P(NO). P(YES) = 1 - P(NO)
            p_yes = 1.0 - rp

        # confidence is stored as string label ('high','medium','low') or numeric
        _CONF_MAP = {"high": 0.80, "medium": 0.60, "low": 0.40}
        try:
            conf_val = float(raw_conf) if raw_conf is not None else 0.5
        except (ValueError, TypeError):
            conf_val = _CONF_MAP.get(str(raw_conf).lower(), 0.5)

        # Determine actual for the analyst's predicted side
        if direction == "buy_yes":
            actual = p_yes
            conf = conf_val
        else:  # buy_no
            actual = 1.0 - p_yes   # = p_no
            conf = conf_val

        # Brier score: (stated_confidence - actual)^2
        brier = (conf - actual) ** 2

        # "Correct" = the side we bet on was the winner (probability > 0.5)
        correct = int(actual >= 0.5)

        # What did the market actually resolve to (for display)?
        actual_label = "yes" if p_yes >= 0.5 else "no"

        resolved_dt = (
            datetime.datetime.fromtimestamp(resolved_at).isoformat()
            if resolved_at
            else None
        )

        scored.append(
            {
                "id": tid,
                "condition_id": cid,
                "market_title": title,
                "direction": direction,
                "confidence": conf,
                "source_agent": agent,
                "actual": actual,
                "brier_score": brier,
                "correct": correct,
                "resolution_outcome": actual_label,  # inferred: 'yes' or 'no' winner
                "resolution_price": resolved_price,
                "resolved_at": resolved_dt,
            }
        )

    if not dry_run and scored:
        for s in scored:
            conn.execute(
                """
                UPDATE trade_ideas
                SET resolution_outcome = ?,
                    resolution_price   = ?,
                    resolved_at        = ?,
                    brier_score        = ?
                WHERE id = ?
                """,
                (
                    s["resolution_outcome"],
                    s["resolution_price"],
                    s["resolved_at"],
                    s["brier_score"],
                    s["id"],
                ),
            )
        conn.commit()

    return scored


def print_summary(conn) -> None:
    """Print calibration summary across all scored trade_ideas."""
    _ensure_brier_column(conn)

    rows = conn.execute(
        """
        SELECT
            source_agent,
            confidence,
            direction,
            resolution_outcome,
            resolution_price,
            brier_score,
            market_title
        FROM trade_ideas
        WHERE resolution_outcome IS NOT NULL
          AND brier_score        IS NOT NULL
        ORDER BY source_agent, brier_score
        """
    ).fetchall()

    if not rows:
        print("No scored trade ideas yet.")
        print("Run without --summary to score any newly resolved markets.")
        return

    total = len(rows)

    def _is_correct(direction, resolution_outcome):
        """Direction buy_yes is correct if market resolved 'yes', buy_no if 'no'."""
        if direction == "buy_yes":
            return resolution_outcome == "yes"
        else:
            return resolution_outcome == "no"

    correct = sum(1 for r in rows if _is_correct(r[2], r[3]))
    mean_bs = sum(r[5] for r in rows) / total
    # Brier reference: if we always said 0.5, BS = 0.25
    bss = 1.0 - (mean_bs / 0.25)

    print(f"\n{'='*50}")
    print(f"  TRADE IDEA SCORING SUMMARY")
    print(f"{'='*50}")
    print(f"  Total scored: {total}")
    print(f"  Correct:      {correct}/{total}  ({correct/total*100:.0f}%)")
    print(f"  Mean Brier:   {mean_bs:.4f}  (lower = better)")
    print(f"  Brier Skill:  {bss:+.3f}  (0=coin-flip, +1=perfect)")
    print(f"  Min needed for calibration stats: 50 (currently {total})")
    print()

    # Per-agent breakdown
    agents: dict[str, list] = {}
    for r in rows:
        agents.setdefault(r[0] or "unknown", []).append(r)

    for agent, agent_rows in agents.items():
        n = len(agent_rows)
        n_correct = sum(1 for r in agent_rows if _is_correct(r[2], r[3]))
        mbs = sum(r[5] for r in agent_rows) / n
        print(f"  [{agent}]  n={n}  acc={n_correct/n*100:.0f}%  BS={mbs:.4f}")

    _CONF_MAP = {"high": 0.80, "medium": 0.60, "low": 0.40}

    print()
    print("  Individual predictions:")
    for r in rows:
        agent, raw_conf, direction, mo_out, resolved_price, bs, title = r
        try:
            conf_f = float(raw_conf)
        except (ValueError, TypeError):
            conf_f = _CONF_MAP.get(str(raw_conf).lower(), 0.5)
        won = "✓" if _is_correct(direction, mo_out) else "✗"
        short = (title or "")[:55]
        print(f"    {won}  BS={bs:.3f}  {direction}@{conf_f:.2f}  →{mo_out}  {short}")

    print()
    if total < 50:
        gap = 50 - total
        print(f"  ⚠  Need {gap} more scored predictions before calibration stats are meaningful.")
        print(f"     Run backtest_analyst.py to bootstrap with historical data.")
    else:
        print(f"  ✓  Enough data for basic reliability diagram and per-tier analysis.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Score trade ideas against resolved market outcomes")
    parser.add_argument("--summary", action="store_true", help="Show scoring stats")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = parser.parse_args()

    conn = get_connection(DB_PATH)
    _ensure_brier_column(conn)

    if args.summary:
        print_summary(conn)
        return

    print(f"Scoring unresolved trade ideas...  [dry_run={args.dry_run}]")
    scored = score_unresolved(conn, dry_run=args.dry_run)

    if not scored:
        print("  No newly resolvable ideas found.")
    else:
        for s in scored:
            won = "✓" if s["correct"] else "✗"
            print(
                f"  {won} idea #{s['id']:>3d}  {s['source_agent']}  "
                f"{s['direction']}@{s['confidence']:.2f}  "
                f"→actual={s['actual']:.1f}  BS={s['brier_score']:.4f}  "
                f"{'(DRY RUN)' if args.dry_run else 'WRITTEN'}"
            )

    print_summary(conn)
    conn.close()


if __name__ == "__main__":
    main()
