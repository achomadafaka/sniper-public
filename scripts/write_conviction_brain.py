#!/usr/bin/env python3
"""
Build and store the 2D Beta-Binomial conviction model to Supabase brain.

Reads resolved paper_trades + strategy_positions, builds a 2D grid of
(price_bucket × score_bucket) empirical win rates using Beta-Binomial
conjugate updating, then writes the result to Supabase brain as:

    type: "strategies"
    name: "conviction_model"

Re-run daily (or after significant new resolved trades) to keep the
posteriors current.  The conviction_engine.py module reads this brain
entry on each strategy-runner cycle.

Usage:
    cd /home/sniper/sniper
    .venv/bin/python3 scripts/write_conviction_brain.py [--dry-run]

Cell grid design
────────────────
price_bucket : floor(entry_price / 0.10) × 0.10
              e.g., 0.42 → 0.40; 0.78 → 0.70

score_bucket : 0  = unknown / score < 60 (these are treated as no-score)
               60 = [60, 70)
               70 = [70, 80)
               80 = [80, 90)
               90 = [90, ∞)

Additionally, a "price-only" aggregate row (score_bucket=0) is stored for
each price_bucket using ALL trades regardless of score.  This serves as a
fallback when the 2D cell has too little data.

Each cell stores:
    wins, losses, n, posterior_mean_wr, ev_at_bucket, data_quality

Beta-Binomial prior: Beta(1,1) = Laplace smoothing.
  posterior_mean = (1 + wins) / (2 + wins + losses)
  At n=0: posterior = 0.5 (maximum uncertainty — do not bet against market)
  At n=25, wins=13: posterior ≈ 0.519 (small positive shift vs. prior)
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Constants (must match conviction_engine.py)
# ---------------------------------------------------------------------------

PRIOR_ALPHA       = 1.0
PRIOR_BETA        = 1.0
PRICE_BUCKET_STEP = 0.10

TERMINAL_STATUSES = (
    "resolution_win", "resolution_loss", "early_exit",
    "source_exited", "stale", "manual", "closed",
)

# Wallets whose trading history contaminates the model.
# 0xde9f7f4e: pure ATP/WTA tennis match bettor — 57 score=100 trades,
# 64.9% WR which is BELOW the ~70% sports-favorite baseline.
# Including it makes the score=90+ band look worse than it is for
# geopolitical / political signals.
EXCLUDED_WALLETS: tuple = (
    "0xde9f7f4e77a1595623ceb58e469f776257ccd43c",
)

# Market title patterns to auto-classify sports match-vs-match trades.
# Used to detect and report sports concentration per wallet — not used
# for blanket exclusion (wallet blocklist is more precise).
import re as _re
_SPORTS_MATCH_RE = _re.compile(
    r"(?:ATP|WTA|ITF|NBA|NFL|NHL|MLB|Serie A|La Liga|Premier League|"
    r"Champions League|Bundesliga|Ligue 1|MLS|NCAA)|\bvs\.?\b",
    _re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _price_bucket(price: float) -> float:
    return round(int(price / PRICE_BUCKET_STEP) * PRICE_BUCKET_STEP, 2)


def _score_bucket(score: float) -> int:
    if score < 60:
        return 0
    if score < 70:
        return 60
    if score < 80:
        return 70
    if score < 90:
        return 80
    return 90


def _cell_key(pb: float, sb: int) -> str:
    return f"{pb:.2f}|{sb}"


def _beta_mean(wins: float, losses: float) -> float:
    return (PRIOR_ALPHA + wins) / (PRIOR_ALPHA + PRIOR_BETA + wins + losses)


def _data_quality(n: int) -> str:
    if n >= 20:
        return "strong"
    if n >= 10:
        return "moderate"
    if n >= 3:
        return "sparse"
    return "prior_dominates"


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_paper_trades(
    db_path: str,
    excluded_wallets: tuple = EXCLUDED_WALLETS,
) -> List[Dict[str, Any]]:
    """Load resolved paper_trades (non-duplicate, with valid entry_price).

    Excludes wallets in *excluded_wallets* to prevent contamination of the
    conviction model from structurally different signal sources (e.g. pure
    sports match bettors whose score=100 reflects match familiarity, not
    the same edge as political/geo insider signals).
    """
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    ph = ",".join("?" * len(TERMINAL_STATUSES))

    excluded_lower = tuple(w.lower() for w in excluded_wallets)
    if excluded_lower:
        excl_ph = ",".join("?" * len(excluded_lower))
        params = list(TERMINAL_STATUSES) + list(excluded_lower)
        rows = conn.execute(
            f"""SELECT entry_price,
                       signal_insider_score,
                       pnl_usdc,
                       outcome,
                       wallet
                FROM paper_trades
                WHERE status IN ({ph})
                  AND duplicate_of IS NULL
                  AND entry_price IS NOT NULL AND entry_price > 0 AND entry_price < 1
                  AND pnl_usdc IS NOT NULL
                  AND LOWER(wallet) NOT IN ({excl_ph})""",
            params,
        ).fetchall()
        # Also report how many trades were excluded
        n_excluded = conn.execute(
            f"""SELECT COUNT(*) FROM paper_trades
                WHERE status IN ({ph})
                  AND duplicate_of IS NULL
                  AND LOWER(wallet) IN ({excl_ph})""",
            params,
        ).fetchone()[0]
        if n_excluded:
            print(f"  paper_trades: excluded {n_excluded} trades from {len(excluded_lower)} blocked wallet(s)")
    else:
        rows = conn.execute(
            f"""SELECT entry_price,
                       signal_insider_score,
                       pnl_usdc,
                       outcome,
                       wallet
                FROM paper_trades
                WHERE status IN ({ph})
                  AND duplicate_of IS NULL
                  AND entry_price IS NOT NULL AND entry_price > 0 AND entry_price < 1
                  AND pnl_usdc IS NOT NULL""",
            TERMINAL_STATUSES,
        ).fetchall()

    conn.close()
    return [dict(r) for r in rows]


def load_strategy_positions(
    db_path: str,
    excluded_wallets: tuple = EXCLUDED_WALLETS,
) -> List[Dict[str, Any]]:
    """Load closed strategy_positions with valid entry price.

    Excludes positions whose wallet is in *excluded_wallets*.
    """
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row

    excluded_lower = tuple(w.lower() for w in excluded_wallets)
    if excluded_lower:
        excl_ph = ",".join("?" * len(excluded_lower))
        rows = conn.execute(
            f"""SELECT avg_entry_price   AS entry_price,
                       entry_insider_score AS signal_insider_score,
                       realized_pnl        AS pnl_usdc,
                       outcome,
                       wallet
               FROM strategy_positions
               WHERE state = 'closed'
                 AND avg_entry_price IS NOT NULL
                 AND avg_entry_price > 0 AND avg_entry_price < 1
                 AND realized_pnl IS NOT NULL
                 AND LOWER(wallet) NOT IN ({excl_ph})""",
            excluded_lower,
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT avg_entry_price   AS entry_price,
                      entry_insider_score AS signal_insider_score,
                      realized_pnl        AS pnl_usdc,
                      outcome,
                      wallet
              FROM strategy_positions
              WHERE state = 'closed'
                AND avg_entry_price IS NOT NULL
                AND avg_entry_price > 0 AND avg_entry_price < 1
                AND realized_pnl IS NOT NULL"""
        ).fetchall()

    conn.close()
    return [dict(r) for r in rows]


def audit_wallet_concentration(trades: List[Dict[str, Any]]) -> None:
    """Print a sports-concentration audit so future contamination is obvious."""
    from collections import Counter
    wallet_counts: Dict[str, int] = Counter()
    wallet_sports: Dict[str, int] = Counter()

    for t in trades:
        w = str(t.get("wallet") or "unknown").lower()
        wallet_counts[w] += 1
        # Use market_title if present (paper_trades); strategy_positions won't have it
        title = str(t.get("market_title") or "")
        if _SPORTS_MATCH_RE.search(title):
            wallet_sports[w] += 1

    print("  Sports concentration audit (top wallets by score=90+ trade count):")
    high_score = [t for t in trades if float(t.get("signal_insider_score") or 0) >= 90]
    hs_wallet: Dict[str, int] = Counter(
        str(t.get("wallet") or "unknown").lower() for t in high_score
    )
    for w, n in hs_wallet.most_common(8):
        sports_pct = wallet_sports[w] / wallet_counts[w] if wallet_counts[w] else 0
        wins = sum(1 for t in high_score
                   if str(t.get("wallet") or "").lower() == w and float(t.get("pnl_usdc") or 0) > 0)
        wr = wins / n if n else 0
        print(f"    {w[:22]}... n={n:>3} hs-trades  WR={wr:.0%}  sports={sports_pct:.0%} of all trades")


# ---------------------------------------------------------------------------
# Cell builder
# ---------------------------------------------------------------------------

def build_cells(trades: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Compute 2D Beta-Binomial cells from resolved trades.

    Returns dict keyed by cell_key → {wins, losses, n, posterior_mean_wr, ...}
    """
    # 2D accumulator: (price_bucket, score_bucket) → wins/losses
    grid_2d: Dict[str, Dict[str, int]] = defaultdict(lambda: {"wins": 0, "losses": 0})
    # Price-only aggregates (score_bucket=0): used as fallback
    grid_1d: Dict[str, Dict[str, int]] = defaultdict(lambda: {"wins": 0, "losses": 0})

    for t in trades:
        ep = float(t.get("entry_price") or 0)
        score = float(t.get("signal_insider_score") or 0)
        pnl = float(t.get("pnl_usdc") or 0)
        outcome = str(t.get("outcome") or "YES").upper()

        # Normalise to YES probability for consistent cell assignment.
        # A trade on NO at price 0.30 corresponds to YES at 0.70.
        yes_price = (1.0 - ep) if outcome == "NO" else ep
        if not (0.0 < yes_price < 1.0):
            continue

        pb = _price_bucket(yes_price)
        sb = _score_bucket(score)
        won = pnl > 0

        key_2d = _cell_key(pb, sb)
        grid_2d[key_2d]["wins" if won else "losses"] += 1

        key_1d = _cell_key(pb, 0)  # score_bucket=0 = "all scores"
        grid_1d[key_1d]["wins" if won else "losses"] += 1

    cells: Dict[str, Dict[str, Any]] = {}

    # 2D cells
    for key, v in grid_2d.items():
        wins, losses = v["wins"], v["losses"]
        n = wins + losses
        pmean = _beta_mean(wins, losses)
        pb_str, _ = key.split("|")
        pb = float(pb_str)
        mid_price = pb + PRICE_BUCKET_STEP / 2
        cells[key] = {
            "wins": wins,
            "losses": losses,
            "n": n,
            "posterior_mean_wr": round(pmean, 4),
            "ev_at_bucket_mid": round(pmean - mid_price, 4),
            "data_quality": _data_quality(n),
            "cell_type": "2d",
        }

    # Price-only aggregate cells (fallback layer, keyed as "X.XX|0")
    for key, v in grid_1d.items():
        wins, losses = v["wins"], v["losses"]
        n = wins + losses
        pmean = _beta_mean(wins, losses)
        pb_str, _ = key.split("|")
        pb = float(pb_str)
        mid_price = pb + PRICE_BUCKET_STEP / 2
        # Only add if not already in cells dict (shouldn't conflict since sb=0)
        cells[key] = {
            "wins": wins,
            "losses": losses,
            "n": n,
            "posterior_mean_wr": round(pmean, 4),
            "ev_at_bucket_mid": round(pmean - mid_price, 4),
            "data_quality": _data_quality(n),
            "cell_type": "price_only",
        }

    return cells


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_report(cells: Dict[str, Dict[str, Any]], total_trades: int) -> None:
    print()
    print("=" * 80)
    print(f"CONVICTION MODEL — 2D CELL GRID  ({total_trades} total resolved trades)")
    print("=" * 80)
    print(
        f"  {'Cell':<12}  {'n':>4}  {'Wins':>4}  {'WR':>6}  "
        f"{'PostMean':>9}  {'EV@mid':>7}  {'Quality':<16}  Type"
    )
    print("  " + "-" * 75)

    for key in sorted(cells.keys()):
        c = cells[key]
        if c["cell_type"] == "price_only":
            continue  # show 2D cells first, print price_only separately
        wr = c["wins"] / max(c["n"], 1)
        ev_str = f"{c['ev_at_bucket_mid']:+.4f}"
        print(
            f"  {key:<12}  {c['n']:>4}  {c['wins']:>4}  {wr:>6.1%}  "
            f"{c['posterior_mean_wr']:>9.4f}  {ev_str:>7}  "
            f"{c['data_quality']:<16}  {c['cell_type']}"
        )

    print()
    print("  Price-only aggregates (fallback layer):")
    print("  " + "-" * 75)
    for key in sorted(cells.keys()):
        c = cells[key]
        if c["cell_type"] != "price_only":
            continue
        wr = c["wins"] / max(c["n"], 1)
        ev_str = f"{c['ev_at_bucket_mid']:+.4f}"
        print(
            f"  {key:<12}  {c['n']:>4}  {c['wins']:>4}  {wr:>6.1%}  "
            f"{c['posterior_mean_wr']:>9.4f}  {ev_str:>7}  "
            f"{c['data_quality']:<16}  {c['cell_type']}"
        )

    print()
    print("  Key interpretation:")
    print("  Cell 'X.XX|Y': price_bucket=X.XX, score_bucket=Y (0=any, 60-90=insider bands)")
    print("  posterior_mean_wr = (1+wins)/(2+n) — Beta(1,1) prior → 0.50 at n=0")
    print("  EV@mid = posterior_mean_wr - (price_bucket + 0.05)")
    print("  ConvictionEngine: skips entry when EV = p_adjusted - entry_price ≤ 0")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Build and store conviction model to brain")
    parser.add_argument("--dry-run", action="store_true", help="Print cells but don't write to brain")
    parser.add_argument(
        "--exclude-wallet", action="append", dest="extra_excluded", metavar="WALLET",
        help="Additional wallet address to exclude (can be repeated). "
             f"Default blocklist: {EXCLUDED_WALLETS}",
    )
    parser.add_argument(
        "--no-default-exclusions", action="store_true",
        help="Disable the default EXCLUDED_WALLETS blocklist (use with care)",
    )
    args = parser.parse_args()

    excluded: tuple
    if args.no_default_exclusions:
        excluded = tuple(args.extra_excluded or [])
    else:
        excluded = EXCLUDED_WALLETS + tuple(args.extra_excluded or [])

    if excluded:
        print(f"Excluded wallets ({len(excluded)}): {[w[:20]+'...' for w in excluded]}")

    db_path = os.getenv(
        "DB_PATH",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sniper_tracker.db"),
    )
    print(f"Database: {db_path}")

    paper = load_paper_trades(db_path, excluded_wallets=excluded)
    strat = load_strategy_positions(db_path, excluded_wallets=excluded)
    all_trades = paper + strat
    print(f"Resolved trades: paper={len(paper)}, strategy_positions={len(strat)}, total={len(all_trades)}")
    print()
    audit_wallet_concentration(all_trades)

    cells = build_cells(all_trades)
    cells_2d = sum(1 for c in cells.values() if c["cell_type"] == "2d")
    cells_1d = sum(1 for c in cells.values() if c["cell_type"] == "price_only")
    print(f"Cells built: {cells_2d} 2D cells, {cells_1d} price-only aggregates")

    print_report(cells, len(all_trades))

    if args.dry_run:
        print("[dry-run] Not writing to brain.")
        return

    # Build payload
    ts = datetime.now(timezone.utc).isoformat()
    payload = {
        "meta": {
            "built_at": ts,
            "total_trades": len(all_trades),
            "paper_trades": len(paper),
            "strategy_positions": len(strat),
            "n_cells_2d": cells_2d,
            "n_cells_total": len(cells),
            "prior_alpha": PRIOR_ALPHA,
            "prior_beta": PRIOR_BETA,
            "price_bucket_step": PRICE_BUCKET_STEP,
            "excluded_wallets": list(excluded),
            "note": (
                "2D Beta-Binomial conviction model. "
                "Cell key: 'price_bucket|score_bucket'. "
                "score_bucket=0 = price-only fallback. "
                "Re-run after each significant batch of resolved trades."
            ),
        },
        "cells": cells,
    }

    try:
        from bot.supabase_helper import upsert_memory
        content = json.dumps(payload, indent=2)
        upsert_memory(
            type="knowledge",
            name="conviction_model",
            description=(
                f"2D Beta-Binomial conviction model ({ts[:10]}) — "
                f"{len(all_trades)} trades, {len(cells)} cells"
            ),
            content=content,
            source="vps",
        )
        print(f"✅ Stored conviction_model to Supabase brain ({len(cells)} cells, {len(all_trades)} trades)")
    except Exception as exc:
        print(f"❌ Failed to store: {exc}")
        print("\nPayload preview (first 3000 chars):")
        print(json.dumps(payload, indent=2)[:3000])
        sys.exit(1)


if __name__ == "__main__":
    main()
